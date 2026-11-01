"""Offline capability selection through the real business entry points."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import json
from types import SimpleNamespace

import pytest

from src.application import market_data_provider as providers
from src.application import option_candidates as candidates
from src.application import opend_symbol_fetching as opend
from src.application import required_data_fetching as fetching
from src.application.multi_tick import required_data_prefetch as scheduled
from src.application.opening_quote_evidence import OpeningUnderlierObservation
from src.application.tool_execution import execute_tool
from test_option_candidates_contract import NOW, _bundle, _row


@pytest.fixture(autouse=True)
def no_live_io(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("market provider test attempted live I/O")

    monkeypatch.setattr("src.infrastructure.futu_gateway.build_ready_futu_quote_gateway", forbidden)
    monkeypatch.setattr("socket.create_connection", forbidden)
    monkeypatch.setattr("urllib.request.urlopen", forbidden)


class RecordingProvider:
    name = "fixture"

    def __init__(self, payload):
        self.payload = payload
        self.calls = []
        self.closed = []
        self.gateway = SimpleNamespace(close=lambda: self.closed.append(True))

    def resolve_quote_route(self, config, **kwargs):
        self.calls.append(("route", kwargs))
        return SimpleNamespace(ok=True, host="offline.invalid", port=12345)

    def open_quote_gateway(self, **kwargs):
        self.calls.append(("open", kwargs))
        return self.gateway

    def get_pooled_quote_gateway(self, pool, **kwargs):
        self.calls.append(("pooled", kwargs))
        return pool.get_gateway(**kwargs)

    def fetch_symbol_request(self, request, **kwargs):
        self.calls.append(("fetch", request))
        return self.payload

    def fetch_earnings_calendar(self, **kwargs):
        self.calls.append(("earnings", kwargs))

    def prefetch_earnings_calendars(self, **kwargs):
        self.calls.append(("scheduled_earnings", kwargs))
        return {"markets": {}}

    def get_underlier_observations(self, gateway, codes, **kwargs):
        self.calls.append(("underliers", kwargs))
        return {code: OpeningUnderlierObservation.from_mapping(self.payload["meta"]["underlier_observation"])
                for code in codes}

    def get_underlier_observation(self, symbol, **kwargs):
        self.calls.append(("underlier", kwargs))
        return OpeningUnderlierObservation.from_mapping(self.payload["meta"]["underlier_observation"])

    def discover_option_expirations(self, symbol, **kwargs):
        self.calls.append(("expirations", kwargs))
        # Reuse the canonical evidence builder with an isolated enumeration.
        kwargs["list_expirations_fn"] = lambda *a, **kw: self.payload["expirations"]
        return providers.OpenDMarketDataProvider().discover_option_expirations(symbol, **kwargs)


def install(monkeypatch, provider):
    monkeypatch.setitem(providers._BUILTIN_PROVIDERS, provider.name, provider)
    return provider


@pytest.mark.parametrize("config", [{}, {"market_data": {}}, {"market_data": {"provider": "opend"}}])
def test_default_selection_has_no_io(config):
    assert providers.resolve_market_data_provider(config).name == "opend"


@pytest.mark.parametrize("value", [None, "", "other", "OpenD", False, []])
def test_unknown_provider_fails_without_fallback(value):
    with pytest.raises(ValueError, match="market_data.provider"):
        providers.resolve_market_data_provider({"market_data": {"provider": value}})


def test_opend_delegates_exact_request_and_evidence(monkeypatch, tmp_path):
    payload = _bundle([_row()], source="opend", source_observed_at=NOW.isoformat(),
                      snapshot_complete=False, reason_code="partial_snapshot")
    seen = []
    monkeypatch.setattr(opend, "fetch_symbol_request", lambda request, **kwargs:
                        seen.append((request, kwargs)) or payload)
    request = opend.FetchSymbolRequest("NVDA", base_dir=tmp_path, deadline=123,
        cancelled=lambda: False, snapshot_max_calls=3, history_kline_max_calls=2,
        freshness_policy="fresh_only", no_retry=True)
    assert providers.fetch_symbol_request(request, snapshot_fallback_max_codes=7) is payload
    assert seen == [(request, {"snapshot_fallback_max_codes": 7})]


def test_opend_auxiliary_capabilities_preserve_exact_contract(monkeypatch):
    from src.application import opend_market_snapshot_fetching as snapshots

    seen = []
    errors = []
    receipt = {"retcode": 0, "coverage_complete": False, "rows": []}
    states = [{"code": "US.NVDA", "market_state": "CLOSED"}]
    gateway = SimpleNamespace(
        get_trading_days_with_receipt=lambda **kw: seen.append(("calendar", kw)) or receipt,
        get_market_state=lambda codes: seen.append(("state", codes)) or states,
    )
    monkeypatch.setattr(snapshots, "get_spot_opend",
        lambda *a, **kw: seen.append(("spot", a, kw)) or 123.5)
    assert providers.get_stock_spot(gateway, "US.NVDA", snapshot_max_calls=2, errors=errors) == 123.5
    assert providers.get_trading_days_with_receipt(gateway, market="US", start="2026-10-26",
        end="2026-10-27") is receipt
    assert providers.get_market_state(gateway, ["US.NVDA"]) is states
    assert seen == [
        ("spot", (gateway, "US.NVDA"), {"snapshot_max_calls": 2, "errors": errors}),
        ("calendar", {"market": "US", "start": "2026-10-26", "end": "2026-10-27"}),
        ("state", ["US.NVDA"]),
    ]


@pytest.mark.parametrize("outcome", ["found", "none", "stale"])
def test_configured_interactive_entry_uses_alternate_and_preserves_freshness(monkeypatch, tmp_path, outcome):
    rows = [] if outcome == "none" else [_row()]
    if outcome == "stale":
        rows[0]["snapshot_received_at_utc"] = (NOW - timedelta(hours=1)).isoformat()
    provider = install(monkeypatch, RecordingProvider(_bundle(rows, source="fixture")))
    config = {"market_data": {"provider": "fixture"}, "symbols": [{"symbol": "NVDA"}]}
    original = deepcopy(config)
    monkeypatch.setattr(candidates, "load_runtime_config", lambda **kwargs: (tmp_path / "config.us.json", config))
    monkeypatch.setattr(candidates, "ensure_runtime_config_freshness", lambda *a, **kw: None)
    monkeypatch.setattr(candidates, "_now", lambda: NOW)
    monkeypatch.setattr(candidates, "current_exchange_rate_snapshot", lambda **kw: {})
    monkeypatch.setattr(candidates, "rates_for_purpose", lambda *a, **kw: {"USDCNY": 7.2})
    with candidates.option_candidates_context(runtime_root=tmp_path):
        response = execute_tool("option_candidates", {"queries": [{"symbol": "NVDA", "strategy": "short_put"}]})
    assert response["ok"], response
    result = response["data"]["results"][0]
    assert result["outcome"] == ("unable" if outcome == "stale" else outcome)
    assert [name for name, _ in provider.calls][:3] == ["route", "open", "fetch"]
    request = next(value for name, value in provider.calls if name == "fetch")
    assert request.market_data_provider == "fixture"
    assert request.gateway is provider.gateway and request.base_dir == tmp_path
    assert request.deadline is not None and callable(request.cancelled)
    assert request.query_filters and request.include_realized_volatility
    assert response["data"]["limits"]["budget_seconds"] == 60
    assert provider.closed == [True]
    assert config == original
    if outcome == "found":
        assert result["candidates"][0]["evidence"]["iv_source"] == "fixture"
        assert result["candidates"][0]["evidence"]["snapshot_received_at"] == NOW.isoformat()
        assert any(name == "earnings" for name, _ in provider.calls)


def test_required_data_fetch_dispatch_keeps_limits_and_observation(monkeypatch, tmp_path):
    payload = _bundle([_row()], source="fixture")
    provider = install(monkeypatch, RecordingProvider(payload))
    request = fetching.RequiredDataFetchRequest("NVDA", 0, market_data_provider="fixture",
        explicit_expirations=[_row()["expiration"]], trading_date=NOW.date().isoformat(),
        underlier_observation=payload["meta"]["underlier_observation"], fetch_spot_if_missing=False,
        freshness_policy="fresh_only", snapshot_max_calls=4, history_kline_max_calls=2)
    assert fetching.execute_required_data_opend(base=tmp_path, request=request) is payload
    forwarded = provider.calls[0][1]
    assert forwarded.underlier_observation == request.underlier_observation
    assert forwarded.freshness_policy == "fresh_only"
    assert forwarded.snapshot_max_calls == 4 and forwarded.history_kline_max_calls == 2
    assert forwarded.trading_date == request.trading_date and not forwarded.fetch_spot_if_missing


def test_scheduled_entry_selects_alternate_before_planning_and_persists_evidence(monkeypatch, tmp_path):
    from src.application import opend_utils
    from test_required_data_prefetch_inprocess import _strict_success_rows_for_fetch

    class ScheduledProvider(RecordingProvider):
        def fetch_symbol_request(self, request, **kwargs):
            self.calls.append(("fetch", request))
            return _strict_success_rows_for_fetch(request.symbol, vars(request), meta={"source": self.name})

    payload = _bundle([_row()])
    payload["expirations"] = [_row()["expiration"]]
    provider = install(monkeypatch, ScheduledProvider(payload))
    config = {"market_data": {"provider": "fixture"}, "runtime": {"prefetch_max_workers": 1},
              "symbols": [{"symbol": "NVDA", "fetch": {"source": "futu", "host": "offline.invalid", "port": 12345},
                           "sell_put": {"enabled": True, "min_dte": 1, "max_dte": 60, "max_strike": 100}}]}
    monkeypatch.setattr(opend_utils, "get_trading_date", lambda market: NOW.date())
    monkeypatch.setattr(scheduled, "_gateway_pool", SimpleNamespace(
        get_gateway=lambda **kw: provider.gateway, mark_success=lambda: None,
        mark_failure=lambda *a, **kw: None, close_current_thread=provider.gateway.close,
        close_registered=lambda: None))
    result = scheduled.prefetch_required_data(vpy=tmp_path / "unused-python", base=tmp_path,
        cfg=config, shared_required=tmp_path / "required", scan_at_utc=NOW,
        producer_run_id="market-provider-test")
    assert result["fetched_ok"] == 1, result
    names = [name for name, _ in provider.calls]
    assert names.index("underliers") < names.index("expirations") < names.index("fetch")
    assert "scheduled_earnings" in names and "pooled" in names
    request = next(value for name, value in provider.calls if name == "fetch")
    assert request.market_data_provider == "fixture"
    assert request.explicit_expirations == payload["expirations"]
    assert request.underlier_observation == payload["meta"]["underlier_observation"]
    assert request.include_realized_volatility and request.history_kline_max_calls > 0
    assert list((tmp_path / "required" / "raw").glob("*.json"))
    assert list((tmp_path / "required" / "parsed").glob("*.csv"))
    assert result["global_required_data_plan"]["symbols"][0]["fetch_binding"]["source"] == "fixture"
    assert result["quote_receipts"]
    snapshot = json.loads((tmp_path / "output_shared/state/current/source_snapshot.market_data.current.json").read_text())
    assert snapshot["source_name"] == "market_data"
    assert snapshot["payload"]["source"] == "fixture"
    assert snapshot["status"] == "ok" and snapshot["fallback_used"] is False


def test_failed_selection_precedes_scheduled_market_io(tmp_path):
    with pytest.raises(ValueError, match="market_data.provider"):
        scheduled.prefetch_required_data(vpy=tmp_path / "unused", base=tmp_path,
            cfg={"market_data": {"provider": "missing"}}, shared_required=tmp_path / "required")


def test_alternate_failure_is_not_retried_as_opend(monkeypatch):
    provider = install(monkeypatch, RecordingProvider(None))
    def fail(request, **kwargs):
        raise TimeoutError("fixture deadline")
    monkeypatch.setattr(provider, "fetch_symbol_request", fail)
    with pytest.raises(TimeoutError, match="fixture deadline"):
        providers.fetch_symbol_request(replace(opend.FetchSymbolRequest("NVDA"), market_data_provider="fixture"))


def test_symbol_scan_selects_provider_for_planning_and_fetch_step(monkeypatch, tmp_path):
    from src.application import opend_utils, symbol_monitoring
    from test_symbol_monitoring_fetch_spec_merge import _deps, _inputs

    payload = _bundle([_row()])
    payload["expirations"] = [_row()["expiration"]]
    provider = install(monkeypatch, RecordingProvider(payload))
    monkeypatch.setattr(opend_utils, "get_trading_date", lambda market: NOW.date())
    captured = []
    inputs = _inputs(tmp_path, fetch_only=True,
        runtime_config={"market_data": {"provider": "fixture"}},
        symbol_cfg={"symbol": "NVDA", "sell_put": {"enabled": True, "max_strike": 100}})
    symbol_monitoring.run_symbol_monitoring(inputs=inputs,
        deps=_deps(ensure_required_data_fn=lambda **kwargs: captured.append(kwargs)))
    assert [name for name, _ in provider.calls] == ["underlier", "expirations"]
    assert captured[0]["market_data_provider"] == "fixture"
    assert captured[0]["fetch_plan"].projected_expirations == payload["expirations"]


def test_required_data_step_passes_selected_provider_to_execution(monkeypatch, tmp_path):
    from src.application.required_data_steps import ensure_required_data

    provider = install(monkeypatch, RecordingProvider(None))
    seen = []
    def stopped(request, **kwargs):
        seen.append(request)
        raise TimeoutError("isolated provider failure")
    monkeypatch.setattr(provider, "fetch_symbol_request", stopped)
    with pytest.raises(TimeoutError, match="isolated provider failure"):
        ensure_required_data(py="unused", base=tmp_path, symbol="NVDA",
            required_data_dir=tmp_path / "required", limit_expirations=1,
            want_put=True, want_call=False, timeout_sec=1, is_scheduled=False,
            market_data_provider="fixture")
    assert len(seen) == 1 and seen[0].market_data_provider == "fixture"


def test_cancelled_interactive_request_never_opens_provider(monkeypatch, tmp_path):
    provider = install(monkeypatch, RecordingProvider(_bundle([_row()])))
    monkeypatch.setattr(candidates, "load_runtime_config", lambda **kw: pytest.fail("cancelled query loaded config"))
    with candidates.option_candidates_context(runtime_root=tmp_path, cancelled=lambda: True):
        response = execute_tool("option_candidates", {"queries": [{"symbol": "NVDA", "strategy": "short_put"}]})
    assert response["data"]["completion"] == "unavailable"
    assert provider.calls == []
