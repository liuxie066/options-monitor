"""Offline runtime-control propagation through the existing portfolio owners."""
from concurrent.futures import CancelledError
from copy import deepcopy
from datetime import datetime, timezone
import importlib.util
from pathlib import Path
from threading import Event
from time import monotonic

import pytest

from src.application import futu_portfolio_context as futu
from src.application import portfolio_context_service as service
from src.infrastructure import exchange_rates, futu_gateway
from tests.cash_evidence_helpers import cash_config, cash_portfolio


NOW = datetime(2026, 11, 13, 2, tzinfo=timezone.utc)


class FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW if tz is not None else NOW.replace(tzinfo=None)


def config():
    result = cash_config()
    result["account_settings"]["lx"]["futu"].update(host="offline", port=11111)
    return result


def load(state_dir, fetch, **kwargs):
    return service.load_account_portfolio_context(
        market="富途", account="lx", state_dir=state_dir, log=lambda _: None,
        runtime_config=config(), portfolio_source="futu",
        fetch_futu_portfolio_context_fn=fetch, write_cache=False,
        exchange_rate_cache_path=state_dir / "fx.json", now_utc=NOW,
        required_position_asset_types=("stock",), **kwargs,
    )


@pytest.fixture
def transport(monkeypatch):
    """Record gateway and FX boundaries while keeping portfolio owners real."""
    events = []
    fail_at = {}
    signal_at = {}

    def record(stage, kwargs):
        events.append((stage, kwargs))
        if stage in signal_at:
            signal_at[stage].set()
        if stage in fail_at:
            raise fail_at[stage]

    class Gateway:
        def get_account_balance(self, **kwargs):
            record("balance", kwargs)
            return [{"acc_id": 123, "trd_env": "REAL", "cn_cash": 1000}]

        def get_positions(self, **kwargs):
            record("positions", kwargs)
            return [{"acc_id": 123, "trd_env": "REAL", "code": "US.NVDA",
                     "qty": 100, "can_sell_qty": 100, "cost_price": 100,
                     "currency": "USD", "position_side": "LONG"}]

        def close(self):
            events.append(("close", {}))

    gateway = Gateway()

    def build(**kwargs):
        record("ready", kwargs)
        return gateway

    def fx(**kwargs):
        record("fx", kwargs)
        return None

    monkeypatch.setattr(futu, "datetime", FrozenDateTime)
    monkeypatch.setattr(futu, "build_ready_futu_broker_gateway", build)
    monkeypatch.setattr(futu, "get_exchange_rates_or_fetch_latest", fx)
    return events, fail_at, signal_at


@pytest.mark.parametrize("controlled", ["deadline", "cancelled", "both"])
def test_controls_reach_stock_sdk_and_fx_without_changing_provenance(tmp_path, transport, controlled):
    events, _, _ = transport
    cfg = config()
    original_config = deepcopy(cfg)
    uncontrolled = load(tmp_path, futu.fetch_futu_portfolio_context, load_json_fn=lambda _: None)
    assert [stage for stage, _ in events] == ["ready", "balance", "fx", "positions", "close"]
    assert all("deadline" not in kwargs and "cancelled" not in kwargs for _, kwargs in events)
    events.clear()
    cancelled = Event().is_set
    deadline = monotonic() + 60
    controls = {}
    if controlled in {"deadline", "both"}:
        controls["deadline"] = deadline
    if controlled in {"cancelled", "both"}:
        controls["cancelled"] = cancelled
    result = service.load_account_portfolio_context(
        market="富途", account="lx", state_dir=tmp_path, log=lambda _: None,
        runtime_config=cfg, portfolio_source="futu",
        fetch_futu_portfolio_context_fn=futu.fetch_futu_portfolio_context,
        load_json_fn=lambda _: None, write_cache=False,
        exchange_rate_cache_path=tmp_path / "fx.json", now_utc=NOW,
        required_position_asset_types=("stock",), **controls,
    )
    assert result == uncontrolled
    assert cfg == original_config
    assert service.portfolio_position_snapshot_errors(result, account="lx", now_utc=NOW) == []
    assert result["stocks_by_symbol"]["NVDA"]["shares"] == 100
    assert not (tmp_path / "portfolio_context.json").exists()
    assert [stage for stage, _ in events] == ["ready", "balance", "fx", "positions", "close"]
    for stage, kwargs in events:
        if stage != "close":
            assert kwargs["deadline"] is controls.get("deadline")
            assert kwargs["cancelled"] is controls.get("cancelled")


@pytest.mark.parametrize("stop_after_query", [False, True])
def test_other_broker_keeps_its_existing_signature_and_rejects_cancelled_data(
    tmp_path, monkeypatch, stop_after_query,
):
    from tests import test_broker_provider as longbridge_fixtures

    for key, value in longbridge_fixtures.CREDENTIALS.items():
        monkeypatch.setenv(key, value)
    cancelled = Event()
    calls = []

    def provider(*, cfg, account, market, base_currency, write_cache, exchange_rate_cache_path):
        calls.append(account)
        if stop_after_query:
            cancelled.set()
        return longbridge_fixtures.observation()

    monkeypatch.setattr(service, "resolve_broker_portfolio_provider", lambda *_args, **_kwargs: provider)

    def run():
        return service.load_account_portfolio_context(
            market="长桥", account="lb", state_dir=tmp_path, log=lambda _: None,
            runtime_config=longbridge_fixtures.config(), portfolio_source="auto",
            fetch_futu_portfolio_context_fn=lambda **_: pytest.fail("Futu provider selected for Longbridge"),
            load_json_fn=lambda _: None, write_cache=False,
            now_utc=longbridge_fixtures.NOW, required_position_asset_types=("stock",),
            deadline=monotonic() + 60, cancelled=cancelled.is_set,
        )

    if stop_after_query:
        with pytest.raises(TimeoutError):
            run()
    else:
        assert run()["context_source"] == "longbridge_direct"
    assert calls == ["lb"]


@pytest.mark.parametrize("controls", [{"deadline": 0.0}, {"cancelled": lambda: True}])
def test_expired_or_cancelled_service_does_not_read_cache_or_call_provider(tmp_path, controls):
    def forbidden(*_args, **_kwargs):
        pytest.fail("runtime cancellation reached a cache/provider read")

    with pytest.raises(TimeoutError):
        load(tmp_path, forbidden, load_json_fn=forbidden, **controls)


def test_cancellation_during_cache_read_does_not_admit_cached_portfolio(tmp_path):
    cancelled = Event()
    cached = cash_portfolio({"source_observed_at": NOW.isoformat()}, evaluated_at=NOW)

    def read(_path):
        cancelled.set()
        return cached

    with pytest.raises(TimeoutError):
        load(tmp_path, lambda **_: pytest.fail("cancelled cache fell through to provider"),
             load_json_fn=read, cancelled=cancelled.is_set)


def partial_cached_portfolio():
    cached = cash_portfolio({"source_observed_at": NOW.isoformat()}, evaluated_at=NOW)
    cached["position_snapshot_input"] = futu.build_futu_position_snapshot(
        rows=[], broker_account_ref={"broker_id": "futu", "external_account_id": "123",
                                    "environment": "REAL", "account_label": "lx",
                                    "broker_account_id": "futu:REAL:123"},
        markets=["US", "HK"], asset_types=["stock"], observed_at_utc=NOW.isoformat(),
        completeness="partial", source_errors=["position_query_failed"],
    )
    return cached


@pytest.mark.parametrize("failure", ["timeout", "cancelled", "signal_then_error", "ordinary_error"])
def test_runtime_stops_cannot_be_converted_to_cash_cache_fallback(tmp_path, failure):
    cached = partial_cached_portfolio()
    assert service.cash_snapshot_is_usable(cached)
    assert service.portfolio_position_snapshot_errors(cached, now_utc=NOW)
    cancelled = Event()
    calls = []

    def fetch(**kwargs):
        calls.append(kwargs)
        if failure == "timeout":
            raise TimeoutError("SDK deadline")
        if failure == "cancelled":
            raise CancelledError("SDK cancelled")
        if failure == "signal_then_error":
            cancelled.set()
        raise OSError("broker unavailable")

    if failure == "ordinary_error":
        result = load(tmp_path, fetch, load_json_fn=lambda _: cached, cancelled=cancelled.is_set)
        assert result["context_source"] == "account_cache"
        assert result["position_refresh_error"] == "OSError"
        assert result["capacity_identity_hash"] == cached["capacity_identity_hash"]
    else:
        expected = CancelledError if failure == "cancelled" else TimeoutError
        with pytest.raises(expected):
            load(tmp_path, fetch, load_json_fn=lambda _: cached, cancelled=cancelled.is_set)
    assert len(calls) == 1
    assert not (tmp_path / "portfolio_context.json").exists()


@pytest.mark.parametrize("stage", ["balance", "fx", "positions"])
@pytest.mark.parametrize("error_type", [TimeoutError, CancelledError])
def test_sdk_stop_propagates_and_closes_gateway(tmp_path, transport, stage, error_type):
    events, fail_at, _ = transport
    fail_at[stage] = error_type("runtime stop")
    with pytest.raises(error_type):
        load(tmp_path, futu.fetch_futu_portfolio_context, load_json_fn=lambda _: None,
             deadline=monotonic() + 60, cancelled=lambda: False)
    stages = [name for name, _ in events]
    assert stages[-1] == "close"
    assert stages.count("close") == 1
    ordered = ["ready", "balance", "fx", "positions"]
    assert stages == ordered[:ordered.index(stage) + 1] + ["close"]


@pytest.mark.parametrize("stage", ["balance", "fx", "positions"])
def test_sdk_data_returned_after_cancellation_is_rejected(tmp_path, transport, stage):
    events, _, signal_at = transport
    cancelled = Event()
    signal_at[stage] = cancelled
    with pytest.raises(TimeoutError):
        load(tmp_path, futu.fetch_futu_portfolio_context, load_json_fn=lambda _: None,
             deadline=monotonic() + 60, cancelled=cancelled.is_set)
    assert events[-1][0] == "close"


@pytest.mark.parametrize("controlled", [False, True])
def test_fx_compatibility_view_forwards_controls_to_current_owner(tmp_path, monkeypatch, controlled):
    seen = []

    def current(**kwargs):
        seen.append(kwargs)
        return {"pairs": {"USDCNY": {}, "HKDCNY": {}}}

    monkeypatch.setattr(exchange_rates, "current_exchange_rate_snapshot", current)
    controls = {"deadline": monotonic() + 60, "cancelled": Event().is_set} if controlled else {}
    path = tmp_path / "fx.json"
    assert futu._fetch_market_exchange_rate_observation(
        cache_path=path, write_cache=False, **controls,
    ) is None
    assert seen == [{"cache_path": path, "write_cache": False, **controls}]


@pytest.fixture
def broker_plugin():
    path = Path(__file__).resolve().parents[1] / "plugins/broker-futu/src/om_broker_futu/__init__.py"
    spec = importlib.util.spec_from_file_location("tick_control_broker_futu", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("controlled", [False, True])
def test_broker_plugin_passes_controls_only_when_present(monkeypatch, broker_plugin, controlled):
    seen = []

    class Gateway:
        def ensure_broker_ready(self, **kwargs):
            seen.append(kwargs)

        def close(self):
            pytest.fail("successful gateway unexpectedly closed")

    gateway = Gateway()
    monkeypatch.setattr(futu_gateway, "build_futu_gateway", lambda **_: gateway)
    deadline = monotonic() + 60
    cancelled = Event().is_set
    controls = {"deadline": deadline, "cancelled": cancelled} if controlled else {}
    assert broker_plugin.build_ready_broker_gateway(
        expected_account_ids={"123"}, trd_env="REAL", **controls,
    ) is gateway
    assert seen == [{"expected_account_ids": {"123"}, "trd_env": "REAL", **controls}]


@pytest.mark.parametrize("error_type", [TimeoutError, CancelledError])
def test_broker_plugin_closes_gateway_on_readiness_stop(monkeypatch, broker_plugin, error_type):
    closed = []

    class Gateway:
        def ensure_broker_ready(self, **kwargs):
            raise error_type("readiness stopped")

        def close(self):
            closed.append(True)

    monkeypatch.setattr(futu_gateway, "build_futu_gateway", lambda **_: Gateway())
    with pytest.raises(error_type):
        broker_plugin.build_ready_broker_gateway(
            expected_account_ids={"123"}, trd_env="REAL", deadline=monotonic() + 60,
        )
    assert closed == [True]
