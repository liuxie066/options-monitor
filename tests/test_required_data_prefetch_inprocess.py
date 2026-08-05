from __future__ import annotations

from datetime import date, datetime, timezone
import json
from pathlib import Path
import time

import pytest

from src.application.multi_tick import required_data_prefetch as mod
from src.application.required_data_prefetch_planning import strategy_prefetch_kwargs


class _Gateway:
    def __init__(self, *, host: str = "127.0.0.1", port: int = 11111) -> None:
        self.host = host
        self.port = int(port)
        self.close_calls = 0

    def is_connected(self) -> bool:
        return True

    def close(self) -> None:
        self.close_calls += 1


@pytest.fixture(autouse=True)
def _keep_prefetch_planning_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    import src.application.required_data_planning as planning

    monkeypatch.setattr(planning, "get_underlier_spot", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        planning,
        "list_option_expirations",
        lambda *args, **kwargs: ["2026-08-21", "2026-09-18"],
    )


def _patch_0700_plan_discovery(
    monkeypatch,
    expirations: list[str] | None = None,
    *,
    spot: float | None = 444.8,
) -> None:
    import src.application.opend_symbol_chain_fetching as chain_fetching
    import src.application.opend_utils as opend_utils
    import src.application.required_data_planning as planning

    monkeypatch.setattr(
        planning,
        "get_underlier_spot",
        lambda *args, **kwargs: spot,
    )
    monkeypatch.setattr(
        planning,
        "_load_existing_spot",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        planning,
        "list_option_expirations",
        lambda *args, **kwargs: list(expirations or ["2026-06-29"]),
    )
    fixed_trading_date = lambda market: date(2026, 6, 8)
    monkeypatch.setattr(chain_fetching, "get_trading_date", fixed_trading_date)
    monkeypatch.setattr(opend_utils, "get_trading_date", fixed_trading_date)


def _patch_us_budget_plan_discovery(monkeypatch) -> None:
    import src.application.opend_symbol_chain_fetching as chain_fetching
    import src.application.opend_utils as opend_utils
    import src.application.required_data_planning as planning

    monkeypatch.setattr(
        planning,
        "list_option_expirations",
        lambda *args, **kwargs: [
            "2026-06-19",
            "2026-07-17",
            "2026-08-21",
            "2026-09-18",
        ],
    )
    fixed_trading_date = lambda market: date(2026, 6, 1)
    monkeypatch.setattr(chain_fetching, "get_trading_date", fixed_trading_date)
    monkeypatch.setattr(opend_utils, "get_trading_date", fixed_trading_date)


def _strict_success_rows_payload(
    symbol: str,
    rows: list[dict[str, object]],
    *,
    trading_date: str,
    host: str = "127.0.0.1",
    port: int = 11111,
    meta: dict[str, object] | None = None,
) -> dict[str, object]:
    """Build provider evidence accepted by the strict S1 finalizer contract."""

    from domain.domain.symbol_identity import resolve_symbol_identity

    normalized_rows: list[dict[str, object]] = []
    for index, raw_row in enumerate(rows):
        row = dict(raw_row)
        option_type = str(row.get("option_type") or "put").lower()
        expiration = str(row.get("expiration") or "2026-06-19")
        strike = float(row.get("strike") or 100.0)
        row.update(
            {
                "symbol": symbol,
                "option_type": option_type,
                "expiration": expiration,
                "dte": int(row.get("dte") or 30),
                "contract_symbol": str(
                    row.get("contract_symbol")
                    or f"{symbol}-{expiration}-{option_type}-{strike:g}-{index}"
                ),
                "strike": strike,
                "spot": float(row.get("spot") or 100.0),
                "realized_volatility_20": float(
                    row.get("realized_volatility_20") or 0.20
                ),
                "realized_volatility_60": float(
                    row.get("realized_volatility_60") or 0.24
                ),
                "realized_volatility_120": float(
                    row.get("realized_volatility_120") or 0.28
                ),
                "realized_volatility_estimate": float(
                    row.get("realized_volatility_estimate") or 0.25
                ),
            }
        )
        normalized_rows.append(row)
    code_set = [str(row["contract_symbol"]) for row in normalized_rows]
    observed_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    payload_meta: dict[str, object] = {
        "status": "ok",
        "error": "",
        "source": "opend",
        "host": host,
        "port": int(port),
        "trading_date": trading_date,
        "source_outcome": "success_rows",
        "reason_code": None,
        "source_observed_at": observed_at,
        "completed_at_utc": observed_at,
        "snapshot_requested_codes": len(code_set),
        "snapshot_returned_codes": len(code_set),
        "snapshot_missing_codes": 0,
        "snapshot_unexpected_codes": 0,
        "snapshot_requested_code_set": code_set,
        "snapshot_returned_code_set": code_set,
        "snapshot_missing_code_set": [],
        "snapshot_unexpected_code_set": [],
        "snapshot_complete": True,
        "realized_volatility": {
            "status": "ok",
            "sample_count": 120,
            "realized_volatility_20": 0.20,
            "realized_volatility_60": 0.24,
            "realized_volatility_120": 0.28,
            "realized_volatility_estimate": 0.25,
        },
    }
    payload_meta.update(meta or {})
    payload_meta.update(
        {
            "snapshot_requested_codes": len(code_set),
            "snapshot_returned_codes": len(code_set),
            "snapshot_missing_codes": 0,
            "snapshot_unexpected_codes": 0,
            "snapshot_requested_code_set": code_set,
            "snapshot_returned_code_set": code_set,
            "snapshot_missing_code_set": [],
            "snapshot_unexpected_code_set": [],
            "snapshot_complete": True,
        }
    )
    expirations = sorted(
        {str(row["expiration"]) for row in normalized_rows}
    )
    identity = resolve_symbol_identity(symbol)
    assert identity is not None
    return {
        "symbol": symbol,
        "underlier_code": identity.futu_code,
        "trading_date": trading_date,
        "expiration_count": len(expirations),
        "expirations": expirations,
        "rows": normalized_rows,
        "meta": payload_meta,
    }


def _strict_success_rows_for_fetch(
    symbol: str,
    fetch_kwargs: dict[str, object],
    *,
    meta: dict[str, object] | None = None,
) -> dict[str, object]:
    from src.application.opend_utils import get_trading_date

    expirations = list(fetch_kwargs.get("explicit_expirations") or ["2026-06-19"])
    option_types = [
        item.strip()
        for item in str(fetch_kwargs.get("option_types") or "put,call").split(",")
        if item.strip()
    ]
    windows = fetch_kwargs.get("side_strike_windows")
    windows = windows if isinstance(windows, dict) else {}
    spot = fetch_kwargs.get("spot_override")
    spot_value = float(spot) if spot not in (None, "") else 100.0
    market = "hk" if str(symbol).strip().upper().endswith(".HK") else "us"
    planned_trading_date = fetch_kwargs.get("trading_date")
    trading_date = (
        date.fromisoformat(str(planned_trading_date))
        if planned_trading_date not in (None, "")
        else get_trading_date(market)
    )
    rows: list[dict[str, object]] = []
    for option_type in option_types:
        side_window = windows.get(option_type)
        side_window = side_window if isinstance(side_window, dict) else {}
        lower = side_window.get("min_strike")
        upper = side_window.get("max_strike")
        strikes = {
            float(value)
            for value in (lower, upper)
            if value not in (None, "")
        } or {100.0}
        for expiration in expirations:
            for strike in sorted(strikes):
                rows.append(
                    {
                        "symbol": symbol,
                        "option_type": option_type,
                        "expiration": str(expiration),
                        "dte": (
                            date.fromisoformat(str(expiration)) - trading_date
                        ).days,
                        "strike": strike,
                        "spot": spot_value,
                    }
                )
    return _strict_success_rows_payload(
        symbol,
        rows,
        trading_date=trading_date.isoformat(),
        host=str(fetch_kwargs.get("host") or "127.0.0.1"),
        port=int(fetch_kwargs.get("port") or 11111),
        meta=meta,
    )


def _patch_success_finalizer(
    monkeypatch: pytest.MonkeyPatch,
    calls: list[dict[str, object]] | None = None,
) -> None:
    def finalize(**kwargs: object) -> dict[str, object]:
        if calls is not None:
            calls.append(dict(kwargs))
        return {
            "mode": kwargs.get("mode"),
            "quote_receipt_path": None,
            "source_observed_at": "2026-06-01T00:00:00Z",
            "completed_at": "2026-06-01T00:00:01Z",
        }

    monkeypatch.setattr(mod, "finalize_required_data_quote_candidate", finalize)


def test_prefetch_builds_complete_global_plan_before_first_chain_fetch(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import src.application.opend_symbol_chain_fetching as chain_fetching
    import src.application.opend_utils as opend_utils
    import src.application.required_data_planning as planning

    watchlist = [
        {
            "symbol": "AAPL",
            "fetch": {"source": "futu", "host": "127.0.0.1", "port": 11111},
            "sell_put": {"enabled": True, "min_dte": 1, "max_dte": 60, "max_strike": 200},
        },
        {
            "symbol": "MSFT",
            "fetch": {"source": "futu", "host": "127.0.0.1", "port": 11111},
            "sell_put": {"enabled": True, "min_dte": 1, "max_dte": 60, "max_strike": 500},
        },
    ]
    events: list[str] = []

    def discover(symbol: str, **kwargs):
        events.append(f"plan:{symbol}")
        return ["2026-06-19", "2026-07-17"]

    def fetch(symbol: str, **kwargs):
        events.append(f"fetch:{symbol}")
        return _strict_success_rows_for_fetch(symbol, kwargs)

    monkeypatch.setattr(planning, "list_option_expirations", discover)
    fixed_trading_date = lambda market: date(2026, 6, 1)
    monkeypatch.setattr(chain_fetching, "get_trading_date", fixed_trading_date)
    monkeypatch.setattr(opend_utils, "get_trading_date", fixed_trading_date)
    monkeypatch.setattr(
        "src.infrastructure.futu_gateway.build_ready_futu_gateway",
        lambda **kwargs: _Gateway(),
    )
    monkeypatch.setattr(mod, "resolve_watchlist_config", lambda cfg: watchlist)
    monkeypatch.setattr(mod, "has_shared_required_data", lambda symbol, root: False)
    monkeypatch.setattr(mod, "fetch_symbol", fetch)
    _patch_success_finalizer(monkeypatch)
    monkeypatch.setattr(
        mod,
        "adapt_opend_tool_payload",
        lambda payload: {"source_name": "opend", "payload": payload},
    )
    monkeypatch.setattr(
        mod.state_repo,
        "append_source_snapshot_event",
        lambda *args, **kwargs: None,
    )

    result = mod.prefetch_required_data(
        vpy=tmp_path / "python",
        base=tmp_path,
        cfg={"runtime": {"prefetch": {"execution_mode": "inprocess", "max_workers": 2}}},
        shared_required=tmp_path / "shared_required",
    )

    assert events[:2] == ["plan:AAPL", "plan:MSFT"]
    assert all(item.startswith("fetch:") for item in events[2:])
    assert result["global_required_data_plan"]["discovery_complete"] is True
    assert result["global_required_data_plan"]["strategy_expiration_limit"] is None
    assert [
        item["expiration_count"]
        for item in result["global_required_data_plan"]["symbols"]
    ] == [2, 2]


def test_prefetch_fails_closed_when_global_expiration_discovery_is_incomplete(
    tmp_path: Path,
    monkeypatch,
) -> None:
    watchlist = [
        {
            "symbol": "AAPL",
            "fetch": {"source": "futu", "host": "127.0.0.1", "port": 11111},
            "sell_put": {"enabled": True, "min_dte": 1, "max_dte": 60, "max_strike": 200},
        }
    ]
    fetch_calls: list[str] = []

    monkeypatch.setattr(mod, "resolve_watchlist_config", lambda cfg: watchlist)
    monkeypatch.setattr(
        "src.application.required_data_planning.list_option_expirations",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("discovery unavailable")),
    )
    monkeypatch.setattr(
        mod,
        "fetch_symbol",
        lambda symbol, **kwargs: fetch_calls.append(symbol),
    )

    with pytest.raises(RuntimeError, match="global required-data plan incomplete"):
        mod.prefetch_required_data(
            vpy=tmp_path / "python",
            base=tmp_path,
            cfg={"runtime": {"prefetch": {"execution_mode": "inprocess"}}},
            shared_required=tmp_path / "shared_required",
        )

    assert fetch_calls == []


def test_prefetch_fails_closed_before_gateway_for_symbol_without_demand(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    watchlist = [
        {
            "symbol": "AAPL",
            "fetch": {
                "source": "futu",
                "host": "127.0.0.1",
                "port": 11111,
            },
        }
    ]
    gateway_calls: list[dict[str, object]] = []
    finalizer_calls: list[dict[str, object]] = []

    monkeypatch.setattr(mod, "resolve_watchlist_config", lambda cfg: watchlist)
    monkeypatch.setattr(
        "src.infrastructure.futu_gateway.build_ready_futu_gateway",
        lambda **kwargs: gateway_calls.append(dict(kwargs)),
    )
    monkeypatch.setattr(
        mod,
        "finalize_required_data_quote_candidate",
        lambda **kwargs: finalizer_calls.append(dict(kwargs)),
    )

    with pytest.raises(ValueError, match="lacks fetch requests"):
        mod.prefetch_required_data(
            vpy=tmp_path / "python",
            base=tmp_path,
            cfg={"runtime": {"prefetch": {"execution_mode": "inprocess"}}},
            shared_required=tmp_path / "shared_required",
            producer_run_id="run-no-demand",
        )

    assert gateway_calls == []
    assert finalizer_calls == []
    assert list(tmp_path.glob("shared_required/**/receipt.json")) == []


def test_prefetch_required_data_inprocess_reuses_gateways(tmp_path: Path, monkeypatch) -> None:
    watchlist = [
        {"symbol": "AAPL", "fetch": {"source": "futu", "host": "127.0.0.1", "port": 11111, "limit_expirations": 2}, "sell_put": {"enabled": True, "min_dte": 1, "max_dte": 60, "max_strike": 200}},
        {"symbol": "MSFT", "fetch": {"source": "futu", "host": "127.0.0.1", "port": 11111, "limit_expirations": 2}, "sell_put": {"enabled": True, "min_dte": 1, "max_dte": 60, "max_strike": 500}},
        {"symbol": "NVDA", "fetch": {"source": "futu", "host": "127.0.0.1", "port": 11111, "limit_expirations": 2}, "sell_put": {"enabled": True, "min_dte": 1, "max_dte": 60, "max_strike": 200}},
    ]
    built: list[_Gateway] = []
    finalized: list[dict[str, object]] = []
    appended: list[dict[str, object]] = []
    adapted: list[dict[str, object]] = []
    execute_calls: list[object] = []

    def fake_build_ready_futu_gateway(**kwargs):
        gw = _Gateway()
        built.append(gw)
        return gw

    def fake_fetch_symbol(symbol: str, **kwargs: object) -> dict[str, object]:
        assert kwargs["gateway"] in built
        assert kwargs["snapshot_batch_size"] == 200
        return _strict_success_rows_for_fetch(symbol, kwargs)

    def fake_adapt(payload: dict[str, object]) -> dict[str, object]:
        adapted.append(payload)
        return {"source_name": "opend", "payload": {"symbol": payload.get("symbol")}}

    def fake_append(base: Path, snapshot: dict[str, object]) -> None:
        appended.append(snapshot)

    def fail_execute(self, intent):
        execute_calls.append(intent)
        raise AssertionError("subprocess path should not run in inprocess mode")

    monkeypatch.setattr("src.infrastructure.futu_gateway.build_ready_futu_gateway", fake_build_ready_futu_gateway)
    monkeypatch.setattr(mod, "resolve_watchlist_config", lambda cfg: watchlist)
    monkeypatch.setattr(mod, "has_shared_required_data", lambda symbol, root: False)
    monkeypatch.setattr(mod, "fetch_symbol", fake_fetch_symbol)
    _patch_success_finalizer(monkeypatch, finalized)
    monkeypatch.setattr(mod, "adapt_opend_tool_payload", fake_adapt)
    monkeypatch.setattr(mod.state_repo, "append_source_snapshot_event", fake_append)
    monkeypatch.setattr(mod.ToolExecutionService, "execute", fail_execute)

    result = mod.prefetch_required_data(
        vpy=tmp_path / "python",
        base=tmp_path,
        cfg={"runtime": {"prefetch": {"execution_mode": "inprocess", "max_workers": 2}}},
        shared_required=tmp_path / "shared_required",
    )

    assert result["execution_mode"] == "inprocess"
    assert result["fetched_ok"] == 3
    assert len(finalized) == 3
    assert {str(item["mode"]) for item in finalized} == {"fresh"}
    assert len(adapted) == 3
    assert len(appended) == 3
    assert not execute_calls
    assert 1 <= len(built) <= 2
    assert all(gw.close_calls >= 1 for gw in built)
    assert (tmp_path / "output_shared" / "state" / "required_data_prefetch.lock").exists()


def test_prefetch_required_data_inprocess_reuses_gateways_per_endpoint(tmp_path: Path, monkeypatch) -> None:
    watchlist = [
        {"symbol": "AAPL", "fetch": {"source": "futu", "host": "127.0.0.1", "port": 11111, "limit_expirations": 2}, "sell_put": {"enabled": True, "min_dte": 1, "max_dte": 60, "max_strike": 200}},
        {"symbol": "MSFT", "fetch": {"source": "futu", "host": "127.0.0.1", "port": 22222, "limit_expirations": 2}, "sell_put": {"enabled": True, "min_dte": 1, "max_dte": 60, "max_strike": 500}},
        {"symbol": "NVDA", "fetch": {"source": "futu", "host": "127.0.0.1", "port": 11111, "limit_expirations": 2}, "sell_put": {"enabled": True, "min_dte": 1, "max_dte": 60, "max_strike": 200}},
    ]
    built: list[_Gateway] = []
    fetch_calls: list[dict[str, object]] = []

    def fake_build_ready_futu_gateway(**kwargs):
        gw = _Gateway(host=str(kwargs["host"]), port=int(kwargs["port"]))
        built.append(gw)
        return gw

    def fake_fetch_symbol(symbol: str, **kwargs: object) -> dict[str, object]:
        gateway = kwargs["gateway"]
        assert isinstance(gateway, _Gateway)
        assert (gateway.host, gateway.port) == (kwargs["host"], kwargs["port"])
        fetch_calls.append({"symbol": symbol, "gateway": gateway, "port": kwargs["port"]})
        return _strict_success_rows_for_fetch(symbol, kwargs)

    monkeypatch.setattr("src.infrastructure.futu_gateway.build_ready_futu_gateway", fake_build_ready_futu_gateway)
    monkeypatch.setattr(mod, "resolve_watchlist_config", lambda cfg: watchlist)
    monkeypatch.setattr(mod, "has_shared_required_data", lambda symbol, root: False)
    monkeypatch.setattr(mod, "fetch_symbol", fake_fetch_symbol)
    _patch_success_finalizer(monkeypatch)
    monkeypatch.setattr(mod, "adapt_opend_tool_payload", lambda payload: {"source_name": "opend", "payload": payload})
    monkeypatch.setattr(mod.state_repo, "append_source_snapshot_event", lambda *args, **kwargs: None)

    result = mod.prefetch_required_data(
        vpy=tmp_path / "python",
        base=tmp_path,
        cfg={"runtime": {"prefetch": {"execution_mode": "inprocess"}, "prefetch_max_workers": 1}},
        shared_required=tmp_path / "shared_required",
    )

    assert result["fetched_ok"] == 3
    assert [(gw.host, gw.port) for gw in built] == [("127.0.0.1", 11111), ("127.0.0.1", 22222)]
    assert fetch_calls[0]["gateway"] is fetch_calls[2]["gateway"]
    assert fetch_calls[1]["gateway"] is not fetch_calls[0]["gateway"]
    assert all(gw.close_calls >= 1 for gw in built)


def test_inprocess_provider_typed_error_marks_gateway_failure_without_receipt(
    tmp_path: Path,
    monkeypatch,
) -> None:
    watchlist = [
        {
            "symbol": "AAPL",
            "fetch": {"source": "futu", "host": "127.0.0.1", "port": 11111},
            "sell_put": {"enabled": True, "min_dte": 1, "max_dte": 60, "max_strike": 200},
        }
    ]
    gateway_successes: list[None] = []
    gateway_failures: list[Exception] = []

    monkeypatch.setattr(
        "src.infrastructure.futu_gateway.build_ready_futu_gateway",
        lambda **kwargs: _Gateway(),
    )
    monkeypatch.setattr(mod, "resolve_watchlist_config", lambda cfg: watchlist)
    monkeypatch.setattr(mod, "has_shared_required_data", lambda symbol, root: False)
    monkeypatch.setattr(
        mod,
        "fetch_symbol",
        lambda symbol, **kwargs: {
            "symbol": symbol,
            "rows": [],
            "meta": {
                "status": "error",
                "source_outcome": "provider_error",
                "error_code": "SNAPSHOT_COVERAGE_INCOMPLETE",
                "error": "required option snapshots are incomplete",
            },
        },
    )
    monkeypatch.setattr(
        mod._gateway_pool,
        "mark_success",
        lambda: gateway_successes.append(None),
    )
    monkeypatch.setattr(
        mod._gateway_pool,
        "mark_failure",
        lambda exc: gateway_failures.append(exc),
    )
    monkeypatch.setattr(mod._gateway_pool, "close_current_thread", lambda: None)

    result = mod.prefetch_required_data(
        vpy=tmp_path / "python",
        base=tmp_path,
        cfg={"runtime": {"prefetch": {"execution_mode": "inprocess", "max_workers": 1}}},
        shared_required=tmp_path / "shared_required",
        producer_run_id="run-provider-error",
    )

    assert result["fetched_ok"] == 0
    assert result["errors"] == 1
    assert gateway_successes == []
    assert len(gateway_failures) == 1
    assert result["quote_receipts"] == {}


def test_inprocess_artifact_failure_does_not_poison_healthy_gateway(
    tmp_path: Path,
    monkeypatch,
) -> None:
    watchlist = [
        {
            "symbol": "AAPL",
            "fetch": {"source": "futu", "host": "127.0.0.1", "port": 11111},
            "sell_put": {"enabled": True, "min_dte": 1, "max_dte": 60, "max_strike": 200},
        }
    ]
    gateway_successes: list[None] = []
    gateway_failures: list[Exception] = []

    monkeypatch.setattr(
        "src.infrastructure.futu_gateway.build_ready_futu_gateway",
        lambda **kwargs: _Gateway(),
    )
    monkeypatch.setattr(mod, "resolve_watchlist_config", lambda cfg: watchlist)
    monkeypatch.setattr(mod, "has_shared_required_data", lambda symbol, root: False)
    monkeypatch.setattr(
        mod,
        "fetch_symbol",
        lambda symbol, **kwargs: _strict_success_rows_for_fetch(symbol, kwargs),
    )
    monkeypatch.setattr(
        mod,
        "finalize_required_data_quote_candidate",
        lambda **kwargs: (_ for _ in ()).throw(OSError("artifact write failed")),
    )
    monkeypatch.setattr(
        mod._gateway_pool,
        "mark_success",
        lambda: gateway_successes.append(None),
    )
    monkeypatch.setattr(
        mod._gateway_pool,
        "mark_failure",
        lambda exc: gateway_failures.append(exc),
    )
    monkeypatch.setattr(mod._gateway_pool, "close_current_thread", lambda: None)

    result = mod.prefetch_required_data(
        vpy=tmp_path / "python",
        base=tmp_path,
        cfg={"runtime": {"prefetch": {"execution_mode": "inprocess", "max_workers": 1}}},
        shared_required=tmp_path / "shared_required",
        producer_run_id="run-artifact-error",
    )

    assert result["fetched_ok"] == 0
    assert result["errors"] == 1
    assert gateway_successes == [None]
    assert gateway_failures == []
    assert result["quote_receipts"] == {}


def test_prefetch_required_data_subprocess_mode_preserves_existing_dispatch(tmp_path: Path, monkeypatch) -> None:
    watchlist = [
        {"symbol": "AAPL", "fetch": {"source": "futu", "host": "127.0.0.1", "port": 11111, "limit_expirations": 8}, "sell_put": {"enabled": True, "min_dte": 1, "max_dte": 60, "max_strike": 200}},
        {"symbol": "MSFT", "fetch": {"source": "futu", "host": "127.0.0.1", "port": 11111, "limit_expirations": 8}, "sell_put": {"enabled": True, "min_dte": 1, "max_dte": 60, "max_strike": 500}},
        {"symbol": "NVDA", "fetch": {"source": "futu", "host": "127.0.0.1", "port": 11111, "limit_expirations": 8}, "sell_put": {"enabled": True, "min_dte": 1, "max_dte": 60, "max_strike": 200}},
    ]
    execute_calls: list[object] = []

    def fake_execute(self, intent):
        execute_calls.append(intent)
        return {
            "schema_kind": "tool_execution",
            "schema_version": "1.0",
            "tool_name": "required_data_prefetch",
            "symbol": intent.symbol,
            "source": intent.source,
            "limit_exp": intent.limit_exp,
            "idempotency_key": f"k-{intent.symbol}",
            "status": "fetched",
            "ok": True,
            "message": "fetched",
            "returncode": 0,
            "started_at_utc": "2026-01-01T00:00:00+00:00",
            "finished_at_utc": "2026-01-01T00:00:01+00:00",
        }

    monkeypatch.setattr(mod, "resolve_watchlist_config", lambda cfg: watchlist)
    monkeypatch.setattr(mod, "has_shared_required_data", lambda symbol, root: False)
    monkeypatch.setattr(mod.ToolExecutionService, "execute", fake_execute)
    finalized: list[dict[str, object]] = []
    _patch_success_finalizer(monkeypatch, finalized)

    result = mod.prefetch_required_data(
        vpy=tmp_path / "python",
        base=tmp_path,
        cfg={"runtime": {"prefetch": {"execution_mode": "subprocess", "max_workers": 2}}},
        shared_required=tmp_path / "shared_required",
    )

    assert result["execution_mode"] == "subprocess"
    assert result["fetched_ok"] == 3
    assert len(execute_calls) == 3
    assert len(finalized) == 3
    assert {str(item["mode"]) for item in finalized} == {"subprocess"}
    assert all(item.get("payload") is None for item in finalized)
    for intent in execute_calls:
        cmd = list(getattr(intent, "cmd"))
        assert "--snapshot-batch-size" in cmd
        assert "--snapshot-fallback-max-codes" in cmd
        assert "--snapshot-fallback-batch-size" in cmd
        assert "--trading-date" in cmd
        trading_date_arg = cmd[cmd.index("--trading-date") + 1]
        date.fromisoformat(trading_date_arg)


@pytest.mark.parametrize("execution_mode", ["inprocess", "subprocess"])
def test_prefetch_success_empty_uses_single_frozen_discovery_and_no_chain_fetch(
    tmp_path: Path,
    monkeypatch,
    execution_mode: str,
) -> None:
    import src.application.opend_symbol_chain_fetching as chain_mod
    import src.application.opend_utils as opend_utils
    import src.application.required_data_planning as planning
    from src.application.opend_symbol_outputs import REQUIRED_DATA_COLUMNS

    discovery_calls: list[str] = []
    forbidden_calls: list[str] = []
    watchlist = [
        {
            "symbol": "0700.HK",
            "fetch": {
                "source": "futu",
                "host": "127.0.0.1",
                "port": 11111,
            },
            "sell_put": {
                "enabled": True,
                "min_dte": 7,
                "max_dte": 45,
            },
            "sell_call": {"enabled": False},
        },
        {
            "symbol": "0700.HK",
            "fetch": {
                "source": "futu",
                "host": "127.0.0.1",
                "port": 11111,
            },
            "sell_put": {"enabled": False},
            "sell_call": {
                "enabled": True,
                "min_dte": 7,
                "max_dte": 45,
            },
        },
    ]

    def discover(symbol: str, **kwargs):
        discovery_calls.append(symbol)
        return []

    def forbid(name: str):
        def inner(*args, **kwargs):
            forbidden_calls.append(name)
            raise AssertionError(f"{name} must not run")

        return inner

    monkeypatch.setattr(planning, "get_underlier_spot", lambda *args, **kwargs: None)
    monkeypatch.setattr(planning, "list_option_expirations", discover)
    monkeypatch.setattr(
        opend_utils,
        "get_trading_date",
        lambda _market: date(2026, 7, 30),
    )
    monkeypatch.setattr(
        chain_mod,
        "get_trading_date",
        lambda _market: date(2026, 7, 30),
    )
    monkeypatch.setattr(mod, "resolve_watchlist_config", lambda cfg: watchlist)
    monkeypatch.setattr(mod, "fetch_symbol", forbid("fetch_symbol"))
    monkeypatch.setattr(
        "src.infrastructure.futu_gateway.build_ready_futu_gateway",
        forbid("gateway"),
    )
    monkeypatch.setattr(mod.ToolExecutionService, "execute", forbid("execute"))
    monkeypatch.setattr(
        mod,
        "adapt_opend_tool_payload",
        lambda payload: {"source_name": "opend", "payload": payload},
    )
    monkeypatch.setattr(
        mod.state_repo,
        "append_source_snapshot_event",
        lambda *args, **kwargs: None,
    )
    required_root = tmp_path / "shared_required"

    result = mod.prefetch_required_data(
        vpy=tmp_path / "python",
        base=tmp_path,
        cfg={
            "runtime": {
                "prefetch": {
                    "execution_mode": execution_mode,
                    "max_workers": 2,
                }
            }
        },
        shared_required=required_root,
        producer_run_id="run-empty",
    )

    assert discovery_calls == ["0700.HK"]
    assert forbidden_calls == []
    assert result["fetched_ok"] == 1
    assert result["errors"] == 0
    assert result["prefetch_budget_plan"]["estimated_option_chain_calls"] == 0
    plan_item = result["global_required_data_plan"]["symbols"][0]
    assert plan_item["projection_outcome"] == "success_empty"
    discovery = plan_item["fetch_plan"]["expiration_discovery"]
    assert discovery["outcome"] == "success_empty"
    assert discovery["reason_code"] == "no_expirations"
    raw = json.loads(
        (
            required_root
            / "raw"
            / "0700.HK_required_data.json"
        ).read_text(encoding="utf-8")
    )
    assert raw["rows"] == []
    assert raw["meta"]["status"] == "ok"
    assert raw["meta"]["source_outcome"] == "success_empty"
    assert raw["meta"]["reason_code"] == "no_expirations"
    assert raw["meta"]["trading_date"] == "2026-07-30"
    assert raw["meta"]["trading_date"] == discovery["request_identity"][
        "trading_date"
    ]
    assert raw["meta"]["source_observed_at"] == discovery["observed_at_utc"]
    csv_header = (
        required_root
        / "parsed"
        / "0700.HK_required_data.csv"
    ).read_text(encoding="utf-8").strip()
    assert csv_header == ",".join(REQUIRED_DATA_COLUMNS)
    receipt_path = required_root / result["quote_receipts"]["0700.HK"]
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["source_observed_at"] == discovery["observed_at_utc"]


def test_prefetch_worker_count_defaults_to_two() -> None:
    assert mod._resolve_prefetch_max_workers({}) == 2


def test_prefetch_worker_count_reads_runtime_value() -> None:
    assert mod._resolve_prefetch_max_workers({"runtime": {"prefetch_max_workers": 3}}) == 3
    assert mod._resolve_prefetch_max_workers({"runtime": {"prefetch": {"max_workers": 4}}}) == 4
    assert mod._resolve_prefetch_max_workers({"prefetch": {"max_workers": 5}}) == 5


def test_prefetch_worker_count_prefers_flat_runtime_override() -> None:
    assert mod._resolve_prefetch_max_workers(
        {"runtime": {"prefetch_max_workers": 3, "prefetch": {"max_workers": 4}}, "prefetch": {"max_workers": 5}}
    ) == 3


def test_prefetch_worker_count_invalid_value_falls_back_to_default() -> None:
    assert mod._resolve_prefetch_max_workers({"runtime": {"prefetch_max_workers": "bad"}}) == 2
    assert mod._resolve_prefetch_max_workers({"runtime": {"prefetch_max_workers": 0}}) == 2
    assert mod._resolve_prefetch_max_workers({"prefetch": {"max_workers": -1}}) == 2


def test_strategy_prefetch_kwargs_uses_strategy_dte_and_strike_bounds() -> None:
    out = strategy_prefetch_kwargs(
        {
            "symbol": "0700.HK",
            "sell_put": {
                "enabled": True,
                "strategy": "insurance_underwriting",
                "min_dte": 20,
                "max_dte": 60,
                "max_strike": 450,
            },
            "sell_call": {"enabled": True, "min_dte": 30, "max_dte": 90, "min_strike": 550},
            "combo_yield": {"enabled": True, "max_dte": 120},
        },
        enabled=True,
    )

    assert out["option_types"] == "put,call"
    assert out["min_dte"] == 20
    assert out["max_dte"] == 90
    assert out["side_strike_windows"]["put"]["min_strike"] == 360
    assert out["side_strike_windows"]["put"]["max_strike"] == 450
    assert out["side_strike_windows"]["call"]["min_strike"] == 550
    assert out["side_strike_windows"]["call"]["max_strike"] > 660
    assert out["include_realized_volatility"] is True


def test_strategy_prefetch_kwargs_fetches_combo_call_with_underwriting_rv() -> None:
    out = strategy_prefetch_kwargs(
        {
            "symbol": "NVDA",
            "sell_put": {"enabled": True, "strategy": "insurance_underwriting", "min_dte": 20, "max_dte": 60, "max_strike": 95},
            "sell_call": {"enabled": False},
            "combo_yield": {"enabled": True, "call": {"min_delta": 0.08, "max_delta": 0.20}},
        },
        enabled=True,
    )

    assert out["option_types"] == "put,call"
    assert out["min_dte"] == 20
    assert out["max_dte"] == 60
    assert out["side_strike_windows"]["put"]["max_strike"] == 95
    assert "call" in out["side_strike_windows"]
    assert out["include_realized_volatility"] is True


def test_strategy_prefetch_kwargs_rejects_unexpanded_template_strategy_config() -> None:
    try:
        strategy_prefetch_kwargs(
            {
                "symbol": "NVDA",
                "use": ["put_base"],
                "sell_put": {"enabled": True},
                "sell_call": {"enabled": False},
            },
            enabled=True,
        )
        raise AssertionError("expected unresolved strategy config failure")
    except ValueError as exc:
        assert "apply templates/profiles" in str(exc)


def test_strategy_prefetch_kwargs_requires_rv_for_sell_put_underwriting() -> None:
    out = strategy_prefetch_kwargs(
        {
            "symbol": "NVDA",
            "sell_put": {"enabled": True, "strategy": "insurance_underwriting"},
            "sell_call": {"enabled": False},
        },
        enabled=True,
    )

    assert out["option_types"] == "put"
    assert out["include_realized_volatility"] is True


def test_strategy_prefetch_kwargs_requires_rv_for_covered_call_underwriting() -> None:
    out = strategy_prefetch_kwargs(
        {
            "symbol": "NVDA",
            "sell_put": {"enabled": False},
            "sell_call": {"enabled": True, "strategy": "insurance_underwriting"},
        },
        enabled=True,
    )

    assert out["option_types"] == "call"
    assert out["include_realized_volatility"] is True


def test_inprocess_prefetch_passes_strategy_bounds_to_fetch_symbol(tmp_path: Path, monkeypatch) -> None:
    _patch_0700_plan_discovery(monkeypatch, spot=None)
    watchlist = [
        {
            "symbol": "0700.HK",
            "fetch": {"source": "futu", "host": "127.0.0.1", "port": 11111, "limit_expirations": 8},
            "sell_put": {"enabled": True, "min_dte": 20, "max_dte": 60, "max_strike": 450},
            "sell_call": {"enabled": False},
        }
    ]
    built: list[_Gateway] = []
    captured: dict[str, object] = {}

    def fake_build_ready_futu_gateway(**kwargs):
        gw = _Gateway()
        built.append(gw)
        return gw

    def fake_fetch_symbol(symbol: str, **kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return _strict_success_rows_for_fetch(symbol, kwargs)

    monkeypatch.setattr("src.infrastructure.futu_gateway.build_ready_futu_gateway", fake_build_ready_futu_gateway)
    monkeypatch.setattr(mod, "resolve_watchlist_config", lambda cfg: watchlist)
    monkeypatch.setattr(mod, "has_shared_required_data", lambda symbol, root: False)
    monkeypatch.setattr(mod, "fetch_symbol", fake_fetch_symbol)
    _patch_success_finalizer(monkeypatch)
    monkeypatch.setattr(mod, "adapt_opend_tool_payload", lambda payload: {"source_name": "opend", "payload": payload})
    monkeypatch.setattr(mod.state_repo, "append_source_snapshot_event", lambda *args, **kwargs: None)

    mod.prefetch_required_data(
        vpy=tmp_path / "python",
        base=tmp_path,
        cfg={"runtime": {"prefetch": {"execution_mode": "inprocess", "max_workers": 1}}},
        shared_required=tmp_path / "shared_required",
    )

    assert captured["option_types"] == "put"
    assert captured["min_dte"] == 20
    assert captured["max_dte"] == 60
    assert captured["side_strike_windows"] == {"put": {"min_strike": 360.0, "max_strike": 450.0}}
    assert captured["include_realized_volatility"] is True


def test_inprocess_prefetch_uses_spot_aware_plan_for_combo_yield_call_floor(tmp_path: Path, monkeypatch) -> None:
    _patch_0700_plan_discovery(monkeypatch)
    watchlist = [
        {
            "symbol": "0700.HK",
            "fetch": {"source": "futu", "host": "127.0.0.1", "port": 11111, "limit_expirations": 8},
            "sell_put": {
                "enabled": True,
                "strategy": "insurance_underwriting",
                "min_dte": 20,
                "max_dte": 90,
                "max_strike": 450,
            },
            "sell_call": {"enabled": True, "min_dte": 20, "max_dte": 90, "min_strike": 550},
            "combo_yield": {"enabled": True},
        }
    ]
    captured: dict[str, object] = {}

    def fake_build_ready_futu_gateway(**kwargs):
        return _Gateway()

    def fake_fetch_symbol(symbol: str, **kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return _strict_success_rows_for_fetch(symbol, kwargs)

    monkeypatch.setattr("src.infrastructure.futu_gateway.build_ready_futu_gateway", fake_build_ready_futu_gateway)
    monkeypatch.setattr(mod, "resolve_watchlist_config", lambda cfg: watchlist)
    monkeypatch.setattr(mod, "fetch_symbol", fake_fetch_symbol)
    monkeypatch.setattr(mod, "adapt_opend_tool_payload", lambda payload: {"source_name": "opend", "payload": payload})
    monkeypatch.setattr(mod.state_repo, "append_source_snapshot_event", lambda *args, **kwargs: None)

    result = mod.prefetch_required_data(
        vpy=tmp_path / "python",
        base=tmp_path,
        cfg={"runtime": {"prefetch": {"execution_mode": "inprocess", "max_workers": 1}}},
        shared_required=tmp_path / "shared_required",
    )

    assert result["fetched_ok"] == 1
    side_strike_windows = captured["side_strike_windows"]
    assert isinstance(side_strike_windows, dict)
    assert side_strike_windows["call"]["min_strike"] == 444.8
    assert side_strike_windows["call"]["min_strike"] < 500
    assert side_strike_windows["call"]["max_strike"] > 670
    assert captured["spot_override"] == 444.8


def test_prefetch_dedupes_same_run_symbol_and_merges_strategy_bounds(tmp_path: Path, monkeypatch) -> None:
    _patch_0700_plan_discovery(
        monkeypatch,
        ["2026-07-17"],
        spot=None,
    )
    watchlist = [
        {
            "symbol": "0700.HK",
            "fetch": {"source": "futu", "host": "127.0.0.1", "port": 11111, "limit_expirations": 4},
            "sell_put": {"enabled": True, "min_dte": 20, "max_dte": 60, "max_strike": 450},
            "sell_call": {"enabled": False},
        },
        {
            "symbol": "0700.HK",
            "fetch": {"source": "futu", "host": "127.0.0.1", "port": 11111, "limit_expirations": 8},
            "sell_put": {"enabled": False},
            "sell_call": {"enabled": True, "min_dte": 30, "max_dte": 90, "min_strike": 550},
        },
    ]
    captured_calls: list[dict[str, object]] = []

    def fake_build_ready_futu_gateway(**kwargs):
        return _Gateway()

    def fake_fetch_symbol(symbol: str, **kwargs: object) -> dict[str, object]:
        captured_calls.append({"symbol": symbol, **kwargs})
        return _strict_success_rows_for_fetch(
            symbol,
            kwargs,
            meta={
                "expiration_opend_calls": 1,
                "expiration_cache_hits": 0,
                "opend_call_count": 2,
                "rate_gate_wait_sec": 0.5,
                "from_cache_expirations": [],
                "fetched_expirations": ["2026-06-19"],
                "snapshot_requested_codes": 12,
                "snapshot_opend_call_count": 1,
                "snapshots_rows": 12,
            },
        )

    monkeypatch.setattr("src.infrastructure.futu_gateway.build_ready_futu_gateway", fake_build_ready_futu_gateway)
    monkeypatch.setattr(mod, "resolve_watchlist_config", lambda cfg: watchlist)
    monkeypatch.setattr(mod, "has_shared_required_data", lambda symbol, root: False)
    monkeypatch.setattr(mod, "fetch_symbol", fake_fetch_symbol)
    _patch_success_finalizer(monkeypatch)
    monkeypatch.setattr(mod, "adapt_opend_tool_payload", lambda payload: {"source_name": "opend", "payload": payload})
    monkeypatch.setattr(mod.state_repo, "append_source_snapshot_event", lambda *args, **kwargs: None)

    result = mod.prefetch_required_data(
        vpy=tmp_path / "python",
        base=tmp_path,
        cfg={"runtime": {"prefetch": {"execution_mode": "inprocess", "max_workers": 2}}},
        shared_required=tmp_path / "shared_required",
    )

    assert len(captured_calls) == 1
    captured = captured_calls[0]
    assert captured["symbol"] == "0700.HK"
    assert captured["limit_expirations"] == 0
    assert captured["option_types"] == "put,call"
    assert captured["min_dte"] == 20
    assert captured["max_dte"] == 90
    side_strike_windows = captured["side_strike_windows"]
    assert isinstance(side_strike_windows, dict)
    assert side_strike_windows["put"] == {"min_strike": 360.0, "max_strike": 450.0}
    assert side_strike_windows["call"]["min_strike"] == 550
    assert result["symbols_total"] == 2
    assert result["unique_symbols_total"] == 1
    assert result["deduped_count"] == 1
    assert result["to_fetch"] == 1
    assert result["fetched_ok"] == 1
    assert result["fetch_metrics"]["expiration_opend_calls"] == 1
    assert result["run_fetch_summary"]["opend_calls"]["total"] == 4
    assert result["run_fetch_summary"]["bottleneck"] == "option_chain_rate_gate"


def test_inprocess_multi_spec_executes_each_exact_request_and_finalizes_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.application import opend_symbol_outputs
    from src.application.required_data_plan_identity import (
        required_data_request_sha256,
    )

    _patch_0700_plan_discovery(
        monkeypatch,
        ["2026-06-29", "2026-07-17"],
        spot=None,
    )
    watchlist = [
        {
            "symbol": "0700.HK",
            "fetch": {
                "source": "futu",
                "host": "127.0.0.1",
                "port": 11111,
            },
            "sell_put": {
                "enabled": True,
                "min_dte": 20,
                "max_dte": 25,
                "max_strike": 450,
            },
            "sell_call": {"enabled": False},
        },
        {
            "symbol": "0700.HK",
            "fetch": {
                "source": "futu",
                "host": "127.0.0.1",
                "port": 11111,
            },
            "sell_put": {"enabled": False},
            "sell_call": {
                "enabled": True,
                "min_dte": 30,
                "max_dte": 60,
                "min_strike": 550,
            },
        },
    ]
    gateway = _Gateway()
    fetch_calls: list[dict[str, object]] = []
    merge_calls: list[list[dict[str, object]]] = []
    finalize_calls: list[dict[str, object]] = []
    save_calls: list[dict[str, object]] = []

    def fake_fetch_symbol(symbol: str, **kwargs: object) -> dict[str, object]:
        fetch_calls.append({"symbol": symbol, **kwargs})
        return _strict_success_rows_for_fetch(symbol, kwargs)

    original_merge = mod.merge_required_data_payloads

    def merge_once(**kwargs: object) -> dict[str, object]:
        payloads = kwargs.get("payloads")
        assert isinstance(payloads, list)
        merge_calls.append(payloads)
        return original_merge(**kwargs)  # type: ignore[arg-type]

    original_finalize = mod.finalize_required_data_quote_candidate

    def finalize_once(**kwargs: object) -> dict[str, object]:
        finalize_calls.append(dict(kwargs))
        return original_finalize(**kwargs)  # type: ignore[arg-type]

    original_save = opend_symbol_outputs.save_outputs

    def save_once(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        save_calls.append(dict(kwargs))
        return original_save(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(
        "src.infrastructure.futu_gateway.build_ready_futu_gateway",
        lambda **kwargs: gateway,
    )
    monkeypatch.setattr(mod, "resolve_watchlist_config", lambda cfg: watchlist)
    monkeypatch.setattr(mod, "fetch_symbol", fake_fetch_symbol)
    monkeypatch.setattr(mod, "merge_required_data_payloads", merge_once)
    monkeypatch.setattr(
        mod,
        "finalize_required_data_quote_candidate",
        finalize_once,
    )
    monkeypatch.setattr(opend_symbol_outputs, "save_outputs", save_once)
    monkeypatch.setattr(
        mod.state_repo,
        "append_source_snapshot_event",
        lambda *args, **kwargs: None,
    )
    shared_required = tmp_path / "shared_required"

    result = mod.prefetch_required_data(
        vpy=tmp_path / "python",
        base=tmp_path,
        cfg={
            "runtime": {
                "prefetch": {
                    "execution_mode": "inprocess",
                    "max_workers": 1,
                }
            }
        },
        shared_required=shared_required,
        producer_run_id="run-real-two-spec",
    )

    assert result["fetched_ok"] == 1
    assert result["errors"] == 0
    assert len(fetch_calls) == 2
    assert [call["option_types"] for call in fetch_calls] == ["put", "call"]
    assert [call["explicit_expirations"] for call in fetch_calls] == [
        ["2026-06-29"],
        ["2026-07-17"],
    ]
    assert [call["trading_date"] for call in fetch_calls] == [
        "2026-06-08",
        "2026-06-08",
    ]
    assert all(call["fetch_spot_if_missing"] is False for call in fetch_calls)
    assert all(call["gateway"] is gateway for call in fetch_calls)
    assert len(merge_calls) == 1
    assert len(finalize_calls) == 1
    assert len(save_calls) == 1
    assert len(list(shared_required.rglob("receipt.json"))) == 1

    raw = json.loads(
        (shared_required / "raw" / "0700.HK_required_data.json").read_text(
            encoding="utf-8"
        )
    )
    children = raw["meta"]["requests"]
    planned = result["global_required_data_plan"]["symbols"][0][
        "fetch_plan"
    ]["merged_requests"]
    assert [child["request_index"] for child in children] == [0, 1]
    assert [child["planned_request_sha256"] for child in children] == [
        required_data_request_sha256(request) for request in planned
    ]
    assert {child["request_symbol"] for child in children} == {"0700.HK"}
    assert {child["request_underlier_code"] for child in children} == {
        "HK.00700"
    }


def test_inprocess_empty_put_rv_demand_is_carried_by_single_active_call_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.application import opend_symbol_outputs

    _patch_0700_plan_discovery(
        monkeypatch,
        ["2026-06-29", "2026-07-17"],
        spot=None,
    )
    watchlist = [
        {
            "symbol": "0700.HK",
            "fetch": {
                "source": "futu",
                "host": "127.0.0.1",
                "port": 11111,
            },
            "sell_put": {
                "enabled": True,
                "strategy": "insurance_underwriting",
                "min_dte": 80,
                "max_dte": 90,
                "max_strike": 450,
            },
            "sell_call": {"enabled": False},
        },
        {
            "symbol": "0700.HK",
            "fetch": {
                "source": "futu",
                "host": "127.0.0.1",
                "port": 11111,
            },
            "sell_put": {"enabled": False},
            "sell_call": {
                "enabled": True,
                "min_dte": 30,
                "max_dte": 60,
                "min_strike": 550,
            },
        },
    ]
    gateway = _Gateway()
    fetch_calls: list[dict[str, object]] = []
    finalize_calls: list[dict[str, object]] = []
    save_calls: list[dict[str, object]] = []

    def fake_fetch_symbol(symbol: str, **kwargs: object) -> dict[str, object]:
        fetch_calls.append({"symbol": symbol, **kwargs})
        return _strict_success_rows_for_fetch(symbol, kwargs)

    original_finalize = mod.finalize_required_data_quote_candidate

    def finalize_once(**kwargs: object) -> dict[str, object]:
        finalize_calls.append(dict(kwargs))
        return original_finalize(**kwargs)  # type: ignore[arg-type]

    original_save = opend_symbol_outputs.save_outputs

    def save_once(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        save_calls.append(dict(kwargs))
        return original_save(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(
        "src.infrastructure.futu_gateway.build_ready_futu_gateway",
        lambda **kwargs: gateway,
    )
    monkeypatch.setattr(mod, "resolve_watchlist_config", lambda cfg: watchlist)
    monkeypatch.setattr(mod, "fetch_symbol", fake_fetch_symbol)
    monkeypatch.setattr(
        mod,
        "finalize_required_data_quote_candidate",
        finalize_once,
    )
    monkeypatch.setattr(opend_symbol_outputs, "save_outputs", save_once)
    monkeypatch.setattr(
        mod.state_repo,
        "append_source_snapshot_event",
        lambda *args, **kwargs: None,
    )
    shared_required = tmp_path / "shared_required"

    result = mod.prefetch_required_data(
        vpy=tmp_path / "python",
        base=tmp_path,
        cfg={
            "runtime": {
                "prefetch": {
                    "execution_mode": "inprocess",
                    "max_workers": 1,
                }
            }
        },
        shared_required=shared_required,
        producer_run_id="run-empty-put-rv-active-call",
    )

    assert result["fetched_ok"] == 1
    assert result["errors"] == 0
    assert len(fetch_calls) == 1
    assert fetch_calls[0]["option_types"] == "call"
    assert fetch_calls[0]["explicit_expirations"] == ["2026-07-17"]
    assert fetch_calls[0]["include_realized_volatility"] is True
    assert fetch_calls[0]["trading_date"] == "2026-06-08"
    assert fetch_calls[0]["gateway"] is gateway
    assert len(finalize_calls) == 1
    assert len(save_calls) == 1
    assert len(list(shared_required.rglob("receipt.json"))) == 1

    fetch_plan = result["global_required_data_plan"]["symbols"][0][
        "fetch_plan"
    ]
    side_plans = {
        str(side_plan["option_type"]): side_plan
        for side_plan in fetch_plan["side_plans"]
    }
    assert side_plans["put"]["explicit_expirations"] == []
    assert side_plans["call"]["explicit_expirations"] == ["2026-07-17"]
    assert fetch_plan["require_realized_volatility"] is True
    assert len(fetch_plan["merged_requests"]) == 1
    active_request = fetch_plan["merged_requests"][0]
    assert active_request["option_types"] == ["call"]
    assert active_request["explicit_expirations"] == ["2026-07-17"]
    assert active_request["include_realized_volatility"] is True
    assert active_request["trading_date"] == "2026-06-08"
    assert all(
        request["explicit_expirations"]
        for request in fetch_plan["merged_requests"]
    )


def test_inprocess_multi_spec_preserves_nested_connection_failure_for_gateway(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.infrastructure.futu_gateway_pool import is_gateway_connection_error

    _patch_0700_plan_discovery(
        monkeypatch,
        ["2026-06-29", "2026-07-17"],
        spot=None,
    )
    watchlist = [
        {
            "symbol": "0700.HK",
            "fetch": {"source": "futu", "host": "127.0.0.1", "port": 11111},
            "sell_put": {
                "enabled": True,
                "min_dte": 20,
                "max_dte": 25,
                "max_strike": 450,
            },
            "sell_call": {"enabled": False},
        },
        {
            "symbol": "0700.HK",
            "fetch": {"source": "futu", "host": "127.0.0.1", "port": 11111},
            "sell_put": {"enabled": False},
            "sell_call": {
                "enabled": True,
                "min_dte": 30,
                "max_dte": 60,
                "min_strike": 550,
            },
        },
    ]
    connection_failure: dict[str, object] = {
        "symbol": "0700.HK",
        "underlier_code": "HK.00700",
        "rows": [],
        "meta": {
            "status": "error",
            "source": "opend",
            "source_outcome": "provider_error",
            "error": "first child failed",
            "errors": [
                {
                    "error_code": "CONNECTION_ERROR",
                    "message": "cannot connect to OpenD",
                }
            ],
        },
    }
    fetch_calls: list[dict[str, object]] = []
    gateway_successes: list[None] = []
    gateway_failures: list[object] = []
    finalize_calls: list[dict[str, object]] = []

    def fake_fetch_symbol(symbol: str, **kwargs: object) -> dict[str, object]:
        fetch_calls.append({"symbol": symbol, **kwargs})
        if len(fetch_calls) > 1:
            raise AssertionError("later child request must not execute")
        return connection_failure

    monkeypatch.setattr(
        "src.infrastructure.futu_gateway.build_ready_futu_gateway",
        lambda **kwargs: _Gateway(),
    )
    monkeypatch.setattr(mod, "resolve_watchlist_config", lambda cfg: watchlist)
    monkeypatch.setattr(mod, "fetch_symbol", fake_fetch_symbol)
    monkeypatch.setattr(
        mod,
        "finalize_required_data_quote_candidate",
        lambda **kwargs: finalize_calls.append(dict(kwargs)),
    )
    monkeypatch.setattr(
        mod._gateway_pool,
        "mark_success",
        lambda: gateway_successes.append(None),
    )
    monkeypatch.setattr(
        mod._gateway_pool,
        "mark_failure",
        lambda failure: gateway_failures.append(failure),
    )
    monkeypatch.setattr(mod._gateway_pool, "close_current_thread", lambda: None)
    monkeypatch.setattr(
        mod.state_repo,
        "append_source_snapshot_event",
        lambda *args, **kwargs: None,
    )
    shared_required = tmp_path / "shared_required"

    result = mod.prefetch_required_data(
        vpy=tmp_path / "python",
        base=tmp_path,
        cfg={
            "runtime": {
                "prefetch": {"execution_mode": "inprocess", "max_workers": 1}
            }
        },
        shared_required=shared_required,
        producer_run_id="run-child-connection-failure",
    )

    assert len(fetch_calls) == 1
    assert gateway_successes == []
    assert gateway_failures == [connection_failure]
    assert is_gateway_connection_error(gateway_failures[0]) is True
    assert finalize_calls == []
    assert result["fetched_ok"] == 0
    assert result["errors"] == 1
    assert result["quote_receipts"] == {}
    assert [path for path in shared_required.rglob("*") if path.is_file()] == []


@pytest.mark.parametrize("conflicting", [False, True])
def test_inprocess_multi_spec_duplicate_child_contract_fails_without_receipt(
    conflicting: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_0700_plan_discovery(
        monkeypatch,
        ["2026-06-29", "2026-07-17"],
        spot=None,
    )
    watchlist = [
        {
            "symbol": "0700.HK",
            "fetch": {"source": "futu", "host": "127.0.0.1", "port": 11111},
            "sell_put": {
                "enabled": True,
                "min_dte": 20,
                "max_dte": 25,
                "max_strike": 450,
            },
            "sell_call": {"enabled": False},
        },
        {
            "symbol": "0700.HK",
            "fetch": {"source": "futu", "host": "127.0.0.1", "port": 11111},
            "sell_put": {"enabled": False},
            "sell_call": {
                "enabled": True,
                "min_dte": 30,
                "max_dte": 60,
                "min_strike": 550,
            },
        },
    ]
    fetch_calls: list[dict[str, object]] = []
    finalize_calls: list[dict[str, object]] = []

    def fake_fetch_symbol(symbol: str, **kwargs: object) -> dict[str, object]:
        fetch_calls.append({"symbol": symbol, **kwargs})
        payload = _strict_success_rows_for_fetch(symbol, kwargs)
        if len(fetch_calls) == 1:
            duplicate = dict(payload["rows"][0])
            if conflicting:
                duplicate["mid"] = 9.9
            payload["rows"].append(duplicate)
        return payload

    monkeypatch.setattr(
        "src.infrastructure.futu_gateway.build_ready_futu_gateway",
        lambda **kwargs: _Gateway(),
    )
    monkeypatch.setattr(mod, "resolve_watchlist_config", lambda cfg: watchlist)
    monkeypatch.setattr(mod, "fetch_symbol", fake_fetch_symbol)
    monkeypatch.setattr(
        mod,
        "finalize_required_data_quote_candidate",
        lambda **kwargs: finalize_calls.append(dict(kwargs)),
    )
    monkeypatch.setattr(
        mod.state_repo,
        "append_source_snapshot_event",
        lambda *args, **kwargs: None,
    )
    shared_required = tmp_path / "shared_required"

    result = mod.prefetch_required_data(
        vpy=tmp_path / "python",
        base=tmp_path,
        cfg={
            "runtime": {
                "prefetch": {"execution_mode": "inprocess", "max_workers": 1}
            }
        },
        shared_required=shared_required,
        producer_run_id=f"run-duplicate-child-{conflicting}",
    )

    assert len(fetch_calls) == 2
    assert finalize_calls == []
    assert result["fetched_ok"] == 0
    assert result["errors"] == 1
    assert result["quote_receipts"] == {}
    assert [path for path in shared_required.rglob("*") if path.is_file()] == []


def test_subprocess_multi_spec_fails_before_execution_or_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_0700_plan_discovery(
        monkeypatch,
        ["2026-06-29", "2026-07-17"],
        spot=None,
    )
    watchlist = [
        {
            "symbol": "0700.HK",
            "fetch": {"source": "futu", "host": "127.0.0.1", "port": 11111},
            "sell_put": {
                "enabled": True,
                "min_dte": 20,
                "max_dte": 25,
                "max_strike": 450,
            },
            "sell_call": {"enabled": False},
        },
        {
            "symbol": "0700.HK",
            "fetch": {"source": "futu", "host": "127.0.0.1", "port": 11111},
            "sell_put": {"enabled": False},
            "sell_call": {
                "enabled": True,
                "min_dte": 30,
                "max_dte": 60,
                "min_strike": 550,
            },
        },
    ]
    effects: list[str] = []

    def forbidden(name: str):
        def invoke(*args: object, **kwargs: object) -> object:
            effects.append(name)
            raise AssertionError(f"{name} must not run")

        return invoke

    monkeypatch.setattr(mod, "resolve_watchlist_config", lambda cfg: watchlist)
    monkeypatch.setattr(mod.ToolExecutionService, "execute", forbidden("execute"))
    monkeypatch.setattr(mod, "fetch_symbol", forbidden("fetch_symbol"))
    monkeypatch.setattr(
        mod,
        "finalize_required_data_quote_candidate",
        forbidden("finalize"),
    )
    shared_required = tmp_path / "shared_required"

    result = mod.prefetch_required_data(
        vpy=tmp_path / "python",
        base=tmp_path,
        cfg={
            "runtime": {
                "prefetch": {
                    "execution_mode": "subprocess",
                    "max_workers": 1,
                }
            }
        },
        shared_required=shared_required,
        producer_run_id="run-subprocess-two-spec",
    )

    assert effects == []
    assert result["fetched_ok"] == 0
    assert result["errors"] == 1
    assert result["results"] == {
        "0700.HK": "required_data_multi_spec_subprocess_unsupported"
    }
    assert result["audit"][0]["error_code"] == (
        "REQUIRED_DATA_MULTI_SPEC_SUBPROCESS_UNSUPPORTED"
    )
    assert [path for path in shared_required.rglob("*") if path.is_file()] == []


def test_prefetch_shared_required_data_candidate_universe_stable_for_same_account_configs(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _patch_0700_plan_discovery(monkeypatch)
    shared_required = tmp_path / "shared_required"
    watchlist = [
        {
            "symbol": "0700.HK",
            "fetch": {"source": "futu", "host": "127.0.0.1", "port": 11111, "limit_expirations": 8},
            "sell_put": {
                "enabled": True,
                "strategy": "insurance_underwriting",
                "min_dte": 20,
                "max_dte": 90,
                "max_strike": 450,
            },
            "sell_call": {"enabled": True, "min_dte": 20, "max_dte": 90, "min_strike": 550},
            "combo_yield": {"enabled": True},
        }
    ]
    fetch_calls: list[str] = []

    def fake_build_ready_futu_gateway(**kwargs):
        return _Gateway()

    def fake_fetch_symbol(symbol: str, **kwargs: object) -> dict[str, object]:
        fetch_calls.append(symbol)
        return _strict_success_rows_for_fetch(symbol, kwargs)

    monkeypatch.setattr("src.infrastructure.futu_gateway.build_ready_futu_gateway", fake_build_ready_futu_gateway)
    monkeypatch.setattr(mod, "resolve_watchlist_config", lambda cfg: watchlist)
    monkeypatch.setattr(mod, "fetch_symbol", fake_fetch_symbol)
    monkeypatch.setattr(mod, "adapt_opend_tool_payload", lambda payload: {"source_name": "opend", "payload": payload})
    monkeypatch.setattr(mod.state_repo, "append_source_snapshot_event", lambda *args, **kwargs: None)

    first = mod.prefetch_required_data(
        vpy=tmp_path / "python",
        base=tmp_path,
        cfg={"runtime": {"prefetch": {"execution_mode": "inprocess", "max_workers": 1}}},
        shared_required=shared_required,
    )
    assert first["fetched_ok"] == 1, first["results"]
    import pandas as pd

    parsed = shared_required / "parsed" / "0700.HK_required_data.csv"
    first_universe = set(pd.read_csv(parsed)["contract_symbol"].dropna().astype(str).tolist())

    second = mod.prefetch_required_data(
        vpy=tmp_path / "python",
        base=tmp_path,
        cfg={"runtime": {"prefetch": {"execution_mode": "inprocess", "max_workers": 1}}},
        shared_required=shared_required,
    )
    second_universe = set(pd.read_csv(parsed)["contract_symbol"].dropna().astype(str).tolist())

    assert first["fetched_ok"] == 1
    assert second["cached"] == 1
    assert fetch_calls == ["0700.HK"]
    assert first_universe == second_universe
    assert any("-call-" in contract for contract in second_universe)


def test_inprocess_prefetch_executes_budgeted_waves_with_safe_option_chain_limit(tmp_path: Path, monkeypatch) -> None:
    watchlist = [
        {"symbol": "AAPL", "fetch": {"source": "futu", "host": "127.0.0.1", "port": 11111}, "sell_put": {"enabled": True, "min_dte": 1, "max_dte": 120, "max_strike": 200}},
        {"symbol": "MSFT", "fetch": {"source": "futu", "host": "127.0.0.1", "port": 11111}, "sell_put": {"enabled": True, "min_dte": 1, "max_dte": 120, "max_strike": 500}},
        {"symbol": "NVDA", "fetch": {"source": "futu", "host": "127.0.0.1", "port": 11111}, "sell_put": {"enabled": True, "min_dte": 1, "max_dte": 120, "max_strike": 200}},
    ]
    captured_calls: list[dict[str, object]] = []

    def fake_build_ready_futu_gateway(**kwargs):
        return _Gateway()

    def fake_fetch_symbol(symbol: str, **kwargs: object) -> dict[str, object]:
        captured_calls.append({"symbol": symbol, **kwargs})
        return _strict_success_rows_for_fetch(
            symbol,
            kwargs,
            meta={"opend_call_count": 1},
        )

    monkeypatch.setattr("src.infrastructure.futu_gateway.build_ready_futu_gateway", fake_build_ready_futu_gateway)
    monkeypatch.setattr(mod, "resolve_watchlist_config", lambda cfg: watchlist)
    monkeypatch.setattr(mod, "has_shared_required_data", lambda symbol, root: False)
    monkeypatch.setattr(mod, "fetch_symbol", fake_fetch_symbol)
    _patch_success_finalizer(monkeypatch)
    monkeypatch.setattr(mod, "adapt_opend_tool_payload", lambda payload: {"source_name": "opend", "payload": payload})
    monkeypatch.setattr(mod.state_repo, "append_source_snapshot_event", lambda *args, **kwargs: None)
    _patch_us_budget_plan_discovery(monkeypatch)

    result = mod.prefetch_required_data(
        vpy=tmp_path / "python",
        base=tmp_path,
        cfg={
            "runtime": {
                "prefetch": {"execution_mode": "inprocess", "max_workers": 3},
                "opend_rate_limits": {"option_chain": {"max_calls": 10, "window_sec": 30, "max_wait_sec": 90}},
            }
        },
        shared_required=tmp_path / "shared_required",
    )

    assert len(captured_calls) == 3
    assert {call["option_chain_max_calls"] for call in captured_calls} == {8}
    assert result["effective_prefetch_workers"] == 2
    assert result["prefetch_budget_plan"]["safe_option_chain_calls_per_window"] == 8
    assert [wave["symbols"] for wave in result["prefetch_budget_plan"]["waves"]] == [["AAPL", "MSFT"], ["NVDA"]]


def test_inprocess_prefetch_waits_after_rate_limited_wave_before_next_wave(tmp_path: Path, monkeypatch) -> None:
    watchlist = [
        {"symbol": "AAPL", "fetch": {"source": "futu", "host": "127.0.0.1", "port": 11111}, "sell_put": {"enabled": True, "min_dte": 1, "max_dte": 120, "max_strike": 200}},
        {"symbol": "MSFT", "fetch": {"source": "futu", "host": "127.0.0.1", "port": 11111}, "sell_put": {"enabled": True, "min_dte": 1, "max_dte": 120, "max_strike": 500}},
        {"symbol": "NVDA", "fetch": {"source": "futu", "host": "127.0.0.1", "port": 11111}, "sell_put": {"enabled": True, "min_dte": 1, "max_dte": 120, "max_strike": 200}},
    ]
    sleeps: list[float] = []

    def fake_build_ready_futu_gateway(**kwargs):
        return _Gateway()

    def fake_fetch_symbol(symbol: str, **kwargs: object) -> dict[str, object]:
        meta: dict[str, object] = {"status": "ok", "error": "", "source": "opend", "opend_call_count": 1}
        if symbol == "AAPL":
            meta = {
                "status": "partial",
                "error_code": "RATE_LIMIT",
                "errors": [
                    {
                        "expiration": "2026-09-18",
                        "error_code": "RATE_LIMIT",
                        "message": "too frequent",
                    }
                ],
            }
        return {
            "symbol": symbol,
            "expiration_count": 1,
            "rows": [{"symbol": symbol, "option_type": "put", "expiration": "2026-06-19", "strike": 100}],
            "meta": meta,
        }

    monkeypatch.setattr("src.infrastructure.futu_gateway.build_ready_futu_gateway", fake_build_ready_futu_gateway)
    monkeypatch.setattr(mod, "resolve_watchlist_config", lambda cfg: watchlist)
    monkeypatch.setattr(mod, "has_shared_required_data", lambda symbol, root: False)
    monkeypatch.setattr(mod, "fetch_symbol", fake_fetch_symbol)
    _patch_success_finalizer(monkeypatch)
    monkeypatch.setattr(mod, "adapt_opend_tool_payload", lambda payload: {"source_name": "opend", "payload": payload})
    monkeypatch.setattr(mod.state_repo, "append_source_snapshot_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(mod, "_sleep_after_rate_limit_wave", lambda wait_sec: sleeps.append(float(wait_sec)))
    _patch_us_budget_plan_discovery(monkeypatch)

    result = mod.prefetch_required_data(
        vpy=tmp_path / "python",
        base=tmp_path,
        cfg={
            "runtime": {
                "prefetch": {"execution_mode": "inprocess", "max_workers": 3},
                "opend_rate_limits": {"option_chain": {"max_calls": 10, "window_sec": 30, "max_wait_sec": 90}},
            }
        },
        shared_required=tmp_path / "shared_required",
    )

    assert sleeps == [30.0]
    assert result["rate_limit_cooldowns"] == [
        {"after_wave": 1, "reason": "opend_rate_limit", "wait_sec": 30.0}
    ]
    assert result["opend_rate_limit_classes"] == ["US"]


def test_inprocess_prefetch_summary_records_partial_expiration_rate_limit_class(tmp_path: Path, monkeypatch) -> None:
    _patch_0700_plan_discovery(
        monkeypatch,
        ["2026-06-29", "2026-09-29"],
    )
    watchlist = [
        {
            "symbol": "3690.HK",
            "fetch": {"source": "futu", "host": "127.0.0.1", "port": 11111, "limit_expirations": 8},
            "sell_call": {"enabled": True, "min_dte": 30, "max_dte": 180, "min_strike": 110},
        }
    ]

    def fake_build_ready_futu_gateway(**kwargs):
        return _Gateway()

    def fake_fetch_symbol(symbol: str, **kwargs: object) -> dict[str, object]:
        return {
            "symbol": symbol,
            "expiration_count": 2,
            "rows": [{"symbol": symbol, "option_type": "call", "expiration": "2026-06-29", "strike": 110}],
            "meta": {
                "status": "partial",
                "error_code": "RATE_LIMIT",
                "error": (
                    "get_option_chain(2026-09-29) failed: "
                    "获取期权链频率太高，请求失败，每30秒最多10次。"
                ),
                "expiration_statuses": {"2026-06-29": "fetched", "2026-09-29": "error"},
                "errors": [
                    {
                        "expiration": "2026-09-29",
                        "error_code": "RATE_LIMIT",
                        "message": "获取期权链频率太高，请求失败，每30秒最多10次。",
                    }
                ],
            },
        }

    monkeypatch.setattr("src.infrastructure.futu_gateway.build_ready_futu_gateway", fake_build_ready_futu_gateway)
    monkeypatch.setattr(mod, "resolve_watchlist_config", lambda cfg: watchlist)
    monkeypatch.setattr(mod, "has_shared_required_data", lambda symbol, root: False)
    monkeypatch.setattr(mod, "fetch_symbol", fake_fetch_symbol)
    monkeypatch.setattr(mod, "adapt_opend_tool_payload", lambda payload: {"source_name": "opend", "payload": payload})
    monkeypatch.setattr(mod.state_repo, "append_source_snapshot_event", lambda *args, **kwargs: None)

    result = mod.prefetch_required_data(
        vpy=tmp_path / "python",
        base=tmp_path,
        cfg={"runtime": {"prefetch": {"execution_mode": "inprocess", "max_workers": 1}}},
        shared_required=tmp_path / "shared_required",
    )

    assert result["fetched_ok"] == 0
    assert result["errors"] == 1
    assert result["opend_rate_limit_classes"] == ["HK"]
    assert result["opend_rate_limit_items"] == [
        {
            "symbol": "3690.HK",
            "market": "HK",
            "expiration": "2026-09-29",
            "endpoint": "option_chain",
            "error_code": "RATE_LIMIT",
            "message": "获取期权链频率太高，请求失败，每30秒最多10次。",
        }
    ]


def test_prefetch_refetches_legacy_cache_without_strict_completeness_evidence(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _patch_0700_plan_discovery(
        monkeypatch,
        ["2026-06-19", "2026-07-17"],
        spot=None,
    )
    shared_required = tmp_path / "shared_required"
    (shared_required / "raw").mkdir(parents=True)
    (shared_required / "parsed").mkdir(parents=True)
    (shared_required / "raw" / "0700.HK_required_data.json").write_text("{}\n", encoding="utf-8")
    (shared_required / "parsed" / "0700.HK_required_data.csv").write_text(
        "\n".join(
            [
                "symbol,option_type,expiration,dte,strike,realized_volatility_estimate",
                "0700.HK,put,2026-06-19,30,360,0.25",
                "0700.HK,put,2026-06-19,30,400,0.25",
                "0700.HK,put,2026-06-19,30,450,0.25",
                "0700.HK,put,2026-07-17,60,360,0.25",
                "0700.HK,put,2026-07-17,60,400,0.25",
                "0700.HK,put,2026-07-17,60,450,0.25",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    watchlist = [
        {
            "symbol": "0700.HK",
            "fetch": {"source": "futu", "host": "127.0.0.1", "port": 11111, "limit_expirations": 8},
            "sell_put": {"enabled": True, "min_dte": 20, "max_dte": 60, "max_strike": 450},
            "sell_call": {"enabled": False},
        }
    ]
    fetched: list[str] = []

    def fake_fetch_symbol(symbol: str, **kwargs: object) -> dict[str, object]:
        fetched.append(symbol)
        return _strict_success_rows_for_fetch(symbol, kwargs)

    monkeypatch.setattr(
        "src.infrastructure.futu_gateway.build_ready_futu_gateway",
        lambda **kwargs: _Gateway(),
    )
    monkeypatch.setattr(mod, "resolve_watchlist_config", lambda cfg: watchlist)
    monkeypatch.setattr(mod, "fetch_symbol", fake_fetch_symbol)
    _patch_success_finalizer(monkeypatch)

    result = mod.prefetch_required_data(
        vpy=tmp_path / "python",
        base=tmp_path,
        cfg={"runtime": {"prefetch": {"execution_mode": "inprocess", "max_workers": 1}}},
        shared_required=shared_required,
    )

    assert result["symbols_total"] == 1
    assert result["to_fetch"] == 1
    assert result["cached_unique_symbols"] == 0
    assert result["fetched_ok"] == 1
    assert fetched == ["0700.HK"]


def test_prefetch_reuses_strict_cache_without_resaving_raw_observation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _patch_0700_plan_discovery(
        monkeypatch,
        ["2026-06-19", "2026-07-17"],
        spot=None,
    )
    shared_required = tmp_path / "shared_required"
    watchlist = [
        {
            "symbol": "0700.HK",
            "fetch": {"source": "futu", "host": "127.0.0.1", "port": 11111, "limit_expirations": 8},
            "sell_put": {"enabled": True, "min_dte": 20, "max_dte": 60, "max_strike": 450},
            "sell_call": {"enabled": False},
        }
    ]
    fetched: list[str] = []
    finalize_calls: list[dict[str, object]] = []
    real_finalize = mod.finalize_required_data_quote_candidate
    real_validate = mod.validate_required_data_quote_candidate
    validation_errors: list[str] = []

    def fake_fetch_symbol(symbol: str, **kwargs: object) -> dict[str, object]:
        fetched.append(symbol)
        return _strict_success_rows_for_fetch(symbol, kwargs)

    def track_finalize(**kwargs: object) -> dict[str, object]:
        finalize_calls.append(dict(kwargs))
        return real_finalize(**kwargs)

    def track_validate(**kwargs: object) -> None:
        try:
            real_validate(**kwargs)
        except Exception as exc:
            validation_errors.append(str(exc))
            raise

    monkeypatch.setattr(
        "src.infrastructure.futu_gateway.build_ready_futu_gateway",
        lambda **kwargs: _Gateway(),
    )
    monkeypatch.setattr(mod, "resolve_watchlist_config", lambda cfg: watchlist)
    monkeypatch.setattr(mod, "fetch_symbol", fake_fetch_symbol)
    monkeypatch.setattr(mod, "finalize_required_data_quote_candidate", track_finalize)
    monkeypatch.setattr(mod, "validate_required_data_quote_candidate", track_validate)
    monkeypatch.setattr(
        mod.state_repo,
        "append_source_snapshot_event",
        lambda *args, **kwargs: None,
    )

    first = mod.prefetch_required_data(
        vpy=tmp_path / "python",
        base=tmp_path,
        cfg={"runtime": {"prefetch": {"execution_mode": "inprocess", "max_workers": 1}}},
        shared_required=shared_required,
    )
    assert first["fetched_ok"] == 1, first["results"]
    raw_path = shared_required / "raw" / "0700.HK_required_data.json"
    before_bytes = raw_path.read_bytes()
    before_mtime_ns = raw_path.stat().st_mtime_ns
    before_raw = json.loads(before_bytes)

    second = mod.prefetch_required_data(
        vpy=tmp_path / "python",
        base=tmp_path,
        cfg={"runtime": {"prefetch": {"execution_mode": "inprocess", "max_workers": 1}}},
        shared_required=shared_required,
    )

    assert first["fetched_ok"] == 1
    assert second["to_fetch"] == 0, validation_errors
    assert second["cached"] == 1
    assert second["fetched"] == 0
    assert fetched == ["0700.HK"]
    assert [call["mode"] for call in finalize_calls] == ["fresh", "cached"]
    assert finalize_calls[0]["payload"] is not None
    assert finalize_calls[1].get("payload") is None
    assert raw_path.read_bytes() == before_bytes
    assert raw_path.stat().st_mtime_ns == before_mtime_ns
    assert before_raw["meta"]["source_observed_at"]
    assert before_raw["meta"]["completed_at_utc"]


def test_prefetch_refetches_stale_strict_cache_before_publishing_current_receipt(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from src.application.opend_symbol_outputs import save_outputs

    _patch_0700_plan_discovery(
        monkeypatch,
        ["2026-06-19", "2026-07-17"],
        spot=None,
    )
    shared_required = tmp_path / "shared_required"
    stale_observed_at = "2020-01-01T00:00:00Z"
    stale_payload = _strict_success_rows_payload(
        "0700.HK",
        [
            {"option_type": "put", "expiration": expiration, "strike": strike}
            for expiration in ("2026-06-19", "2026-07-17")
            for strike in (360, 450)
        ],
        trading_date="2026-06-08",
        meta={
            "source_observed_at": stale_observed_at,
            "completed_at_utc": stale_observed_at,
        },
    )
    save_outputs(
        tmp_path,
        "0700.HK",
        stale_payload,
        output_root=shared_required,
    )
    watchlist = [
        {
            "symbol": "0700.HK",
            "fetch": {"source": "futu", "host": "127.0.0.1", "port": 11111},
            "sell_put": {"enabled": True, "min_dte": 20, "max_dte": 60, "max_strike": 450},
            "sell_call": {"enabled": False},
        }
    ]
    fetched: list[str] = []

    def fake_fetch_symbol(symbol: str, **kwargs: object) -> dict[str, object]:
        fetched.append(symbol)
        return _strict_success_rows_for_fetch(symbol, kwargs)

    monkeypatch.setattr(
        "src.infrastructure.futu_gateway.build_ready_futu_gateway",
        lambda **kwargs: _Gateway(),
    )
    monkeypatch.setattr(mod, "resolve_watchlist_config", lambda cfg: watchlist)
    monkeypatch.setattr(mod, "fetch_symbol", fake_fetch_symbol)
    monkeypatch.setattr(
        mod.state_repo,
        "append_source_snapshot_event",
        lambda *args, **kwargs: None,
    )

    result = mod.prefetch_required_data(
        vpy=tmp_path / "python",
        base=tmp_path,
        cfg={"runtime": {"prefetch": {"execution_mode": "inprocess", "max_workers": 1}}},
        shared_required=shared_required,
        producer_run_id="run-after-stale",
    )

    assert result["to_fetch"] == 1
    assert result["fetched_ok"] == 1
    assert result["errors"] == 0
    assert fetched == ["0700.HK"]
    assert set(result["quote_receipts"]) == {"0700.HK"}
    raw = json.loads(
        (shared_required / "raw" / "0700.HK_required_data.json").read_text(
            encoding="utf-8"
        )
    )
    assert raw["meta"]["source_observed_at"] != stale_observed_at
    receipt_path = shared_required / result["quote_receipts"]["0700.HK"]
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["producer_run_id"] == "run-after-stale"
    assert len(list(shared_required.rglob("receipt.json"))) == 1


def test_prefetch_mixed_legacy_cache_and_fresh_partial_only_receipts_success(
    tmp_path: Path,
    monkeypatch,
) -> None:
    watchlist = [
        {
            "symbol": "AAPL",
            "fetch": {"source": "futu", "host": "127.0.0.1", "port": 11111},
            "sell_put": {"enabled": True, "min_dte": 1, "max_dte": 60, "max_strike": 200},
        },
        {
            "symbol": "MSFT",
            "fetch": {"source": "futu", "host": "127.0.0.1", "port": 11111},
            "sell_put": {"enabled": True, "min_dte": 1, "max_dte": 60, "max_strike": 500},
        },
    ]
    shared_required = tmp_path / "shared_required"
    (shared_required / "raw").mkdir(parents=True)
    (shared_required / "parsed").mkdir(parents=True)
    (shared_required / "raw" / "AAPL_required_data.json").write_text(
        "{}\n",
        encoding="utf-8",
    )
    (shared_required / "parsed" / "AAPL_required_data.csv").write_text(
        "symbol,option_type,expiration,dte,contract_symbol,strike\n",
        encoding="utf-8",
    )
    fetched: list[str] = []

    def fake_fetch_symbol(symbol: str, **kwargs: object) -> dict[str, object]:
        fetched.append(symbol)
        if symbol == "MSFT":
            return {
                "symbol": symbol,
                "rows": [],
                "meta": {
                    "status": "partial",
                    "source_outcome": "provider_error",
                    "error_code": "SNAPSHOT_COVERAGE_INCOMPLETE",
                    "error": "one required contract is missing",
                },
            }
        return _strict_success_rows_for_fetch(symbol, kwargs)

    monkeypatch.setattr(
        "src.infrastructure.futu_gateway.build_ready_futu_gateway",
        lambda **kwargs: _Gateway(),
    )
    monkeypatch.setattr(mod, "resolve_watchlist_config", lambda cfg: watchlist)
    monkeypatch.setattr(mod, "fetch_symbol", fake_fetch_symbol)
    monkeypatch.setattr(
        mod.state_repo,
        "append_source_snapshot_event",
        lambda *args, **kwargs: None,
    )

    result = mod.prefetch_required_data(
        vpy=tmp_path / "python",
        base=tmp_path,
        cfg={"runtime": {"prefetch": {"execution_mode": "inprocess", "max_workers": 2}}},
        shared_required=shared_required,
        producer_run_id="run-mixed",
    )

    assert result["to_fetch"] == 2
    assert result["fetched_ok"] == 1
    assert result["errors"] == 1
    assert set(fetched) == {"AAPL", "MSFT"}
    assert set(result["quote_receipts"]) == {"AAPL"}


def test_prefetch_refetches_when_cached_required_data_misses_strategy_side(tmp_path: Path, monkeypatch) -> None:
    _patch_0700_plan_discovery(
        monkeypatch,
        ["2026-06-19", "2026-07-17"],
    )
    shared_required = tmp_path / "shared_required"
    (shared_required / "raw").mkdir(parents=True)
    (shared_required / "parsed").mkdir(parents=True)
    (shared_required / "raw" / "0700.HK_required_data.json").write_text("{}\n", encoding="utf-8")
    (shared_required / "parsed" / "0700.HK_required_data.csv").write_text(
        "\n".join(
            [
                "symbol,option_type,expiration,dte,strike",
                "0700.HK,call,2026-06-19,30,560",
                "0700.HK,call,2026-06-19,30,600",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    watchlist = [
        {
            "symbol": "0700.HK",
            "fetch": {"source": "futu", "host": "127.0.0.1", "port": 11111, "limit_expirations": 8},
            "sell_put": {"enabled": True, "min_dte": 20, "max_dte": 60, "max_strike": 450},
            "sell_call": {"enabled": False},
        }
    ]
    built: list[_Gateway] = []
    fetched: list[str] = []

    def fake_build_ready_futu_gateway(**kwargs):
        gw = _Gateway()
        built.append(gw)
        return gw

    def fake_fetch_symbol(symbol: str, **kwargs: object) -> dict[str, object]:
        fetched.append(symbol)
        return _strict_success_rows_for_fetch(
            symbol,
            kwargs,
            meta={
                "opend_call_count": 2,
                "rate_gate_wait_sec": 1.25,
                "from_cache_expirations": ["2026-06-19"],
                "fetched_expirations": ["2026-07-17"],
                "option_codes": 12,
                "snapshot_requested_codes": 12,
                "snapshot_opend_call_count": 1,
                "snapshots_rows": 12,
            },
        )

    monkeypatch.setattr("src.infrastructure.futu_gateway.build_ready_futu_gateway", fake_build_ready_futu_gateway)
    monkeypatch.setattr(mod, "resolve_watchlist_config", lambda cfg: watchlist)
    monkeypatch.setattr(mod, "fetch_symbol", fake_fetch_symbol)
    _patch_success_finalizer(monkeypatch)
    monkeypatch.setattr(mod, "adapt_opend_tool_payload", lambda payload: {"source_name": "opend", "payload": payload})
    monkeypatch.setattr(mod.state_repo, "append_source_snapshot_event", lambda *args, **kwargs: None)

    result = mod.prefetch_required_data(
        vpy=tmp_path / "python",
        base=tmp_path,
        cfg={"runtime": {"prefetch": {"execution_mode": "inprocess", "max_workers": 1}}},
        shared_required=shared_required,
    )

    assert fetched == ["0700.HK"]
    assert result["fetched_ok"] == 1
    assert result["fetch_metrics"]["option_chain_opend_calls"] == 2
    assert result["fetch_metrics"]["option_chain_rate_gate_wait_sec"] == 1.25
    assert result["fetch_metrics"]["snapshot_opend_calls"] == 1
    assert result["fetch_metrics"]["snapshot_requested_codes"] == 2


def test_inprocess_prefetch_summary_includes_symbol_duration(tmp_path: Path, monkeypatch) -> None:
    watchlist = [{"symbol": "AAPL", "fetch": {"source": "futu", "host": "127.0.0.1", "port": 11111, "limit_expirations": 8}, "sell_put": {"enabled": True, "min_dte": 1, "max_dte": 60, "max_strike": 200}}]
    built: list[_Gateway] = []

    def fake_build_ready_futu_gateway(**kwargs):
        gw = _Gateway()
        built.append(gw)
        return gw

    def fake_fetch_symbol(symbol: str, **kwargs: object) -> dict[str, object]:
        time.sleep(0.01)
        return _strict_success_rows_for_fetch(symbol, kwargs)

    monkeypatch.setattr("src.infrastructure.futu_gateway.build_ready_futu_gateway", fake_build_ready_futu_gateway)
    monkeypatch.setattr(mod, "resolve_watchlist_config", lambda cfg: watchlist)
    monkeypatch.setattr(mod, "has_shared_required_data", lambda symbol, root: False)
    monkeypatch.setattr(mod, "fetch_symbol", fake_fetch_symbol)
    _patch_success_finalizer(monkeypatch)
    monkeypatch.setattr(mod, "adapt_opend_tool_payload", lambda payload: {"source_name": "opend", "payload": payload})
    monkeypatch.setattr(mod.state_repo, "append_source_snapshot_event", lambda *args, **kwargs: None)

    result = mod.prefetch_required_data(
        vpy=tmp_path / "python",
        base=tmp_path,
        cfg={"runtime": {"prefetch": {"execution_mode": "inprocess"}}},
        shared_required=tmp_path / "shared_required",
    )

    assert result["prefetch_max_workers"] == 2
    assert result["effective_prefetch_workers"] == 1
    assert result["submitted_count"] == 1
    assert result["completed_count"] == 1
    assert result["skipped_count"] == 0
    assert result["failed_count"] == 0
    assert result["symbols"][0]["symbol"] == "AAPL"
    assert result["symbols"][0]["execution_mode"] == "inprocess"
    assert result["symbols"][0]["duration_sec"] >= 0.0
    assert result["audit"][0]["duration_sec"] >= 0.0
    assert built and built[0].close_calls >= 1


def test_strategy_prefetch_kwargs_requests_combo_put_and_call_when_sell_put_disabled() -> None:
    out = strategy_prefetch_kwargs(
        {
            "symbol": "NVDA",
            "sell_put": {
                "enabled": False,
                "min_dte": 20,
                "max_dte": 60,
                "min_strike": 90,
                "max_strike": 96,
            },
            "sell_call": {"enabled": False},
            "combo_yield": {
                "enabled": True,
                "structure_mode": "staggered_expiry_pair",
                "min_expiry_gap_days": 1,
                "max_expiry_gap_days": 30,
            },
        },
        enabled=True,
    )

    assert out["option_types"] == "put,call"
    assert out["min_dte"] == 20
    assert out["max_dte"] == 90
    assert out["side_strike_windows"]["put"]["min_strike"] == 90.0
    assert out["side_strike_windows"]["put"]["max_strike"] == 96.0
    assert out["include_realized_volatility"] is True
