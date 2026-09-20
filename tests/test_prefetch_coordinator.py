from __future__ import annotations

from typing import Any

from domain.domain.tool_boundary import normalize_tool_execution_payload
from src.application.multi_tick.prefetch_coordinator import PrefetchCoordinator


def _payload(symbol: str, *, status: str, ok: bool, message: str = "ok", source: str = "futu") -> dict[str, Any]:
    return normalize_tool_execution_payload(
        tool_name="required_data_prefetch",
        symbol=symbol,
        source=source,
        limit_exp=8,
        status=status,
        ok=ok,
        message=message,
        returncode=(0 if ok else 1),
    )


def _futu_cfg(symbol: str) -> dict[str, Any]:
    return {"symbol": symbol, "fetch": {"source": "futu", "limit_expirations": 8}}


def _run_coordinator(
    symbol_cfgs: list[dict[str, Any]],
    dispatch_fn: Any,
    *,
    fail_budget_consecutive: int = 3,
    fail_budget_total: int = 5,
    **overrides: Any,
):
    return PrefetchCoordinator(
        symbol_cfgs=symbol_cfgs,
        max_workers=1,
        execution_mode="inprocess",
        fail_budget_consecutive=fail_budget_consecutive,
        fail_budget_total=fail_budget_total,
        dispatch_fn=dispatch_fn,
        **overrides,
    ).run()


def test_prefetch_coordinator_short_circuits_rate_limited_symbol_class() -> None:
    cfgs = [_futu_cfg("AAPL"), _futu_cfg("MSFT")]
    dispatched: list[str] = []

    def _dispatch(symbol_cfg: dict[str, Any]) -> dict[str, Any]:
        symbol = str(symbol_cfg["symbol"])
        dispatched.append(symbol)
        return _payload(symbol, status="error", ok=False, message="rate limit")

    result = _run_coordinator(cfgs, _dispatch)

    assert dispatched == ["AAPL"]
    assert result.errors == 1
    assert result.skipped == 1
    assert result.submitted_count == 1
    assert result.completed_count == 1
    assert result.results["MSFT"] == "opend_rate_limit_short_circuit class=US"
    assert result.symbol_items[1]["status"] == "skipped"
    assert sorted(result.opend_rate_limit_classes) == ["US"]
    assert result.opend_rate_limit_items[0]["market"] == "US"


def test_prefetch_coordinator_records_nested_us_expiration_rate_limit() -> None:
    cfgs = [_futu_cfg("NVDA")]

    def _dispatch(symbol_cfg: dict[str, Any]) -> dict[str, Any]:
        symbol = str(symbol_cfg["symbol"])
        payload = _payload(symbol, status="fetched", ok=True, message="partial")
        payload["payload"] = {
            "meta": {
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
        }
        return payload

    result = _run_coordinator(
        cfgs, _dispatch, short_circuit_rate_limits=False, stop_on_failure_budget=False
    )

    assert result.fetched_ok == 1
    assert result.errors == 0
    assert sorted(result.opend_rate_limit_classes) == ["US"]
    assert result.opend_rate_limit_items == [
        {
            "symbol": "NVDA",
            "market": "US",
            "expiration": "2026-09-18",
            "endpoint": "option_chain",
            "error_code": "RATE_LIMIT",
            "message": "too frequent",
        },
    ]


def test_prefetch_coordinator_stops_queued_work_on_failure_budget() -> None:
    cfgs = [_futu_cfg("AAPL"), _futu_cfg("0700.HK"), _futu_cfg("MSFT")]
    dispatched: list[str] = []

    def _dispatch(symbol_cfg: dict[str, Any]) -> dict[str, Any]:
        symbol = str(symbol_cfg["symbol"])
        dispatched.append(symbol)
        return _payload(symbol, status="error", ok=False, message="transient failure")

    result = _run_coordinator(cfgs, _dispatch, fail_budget_consecutive=1)

    assert dispatched == ["AAPL"]
    assert result.budget_triggered is True
    assert result.errors == 1
    assert result.skipped == 2
    assert "prefetch_failure_budget_exceeded" in result.audit_items[1]["message"]
    assert result.results["0700.HK"] == "prefetch_stopped_by_failure_budget"
    assert result.results["MSFT"] == "prefetch_stopped_by_failure_budget"


def test_prefetch_coordinator_can_run_without_early_stops() -> None:
    cfgs = [_futu_cfg("AAPL"), _futu_cfg("MSFT")]
    dispatched: list[str] = []

    def _dispatch(symbol_cfg: dict[str, Any]) -> dict[str, Any]:
        symbol = str(symbol_cfg["symbol"])
        dispatched.append(symbol)
        return _payload(symbol, status="error", ok=False, message="rate limit")

    result = _run_coordinator(
        cfgs,
        _dispatch,
        fail_budget_consecutive=1,
        fail_budget_total=1,
        short_circuit_rate_limits=False,
        stop_on_failure_budget=False,
    )

    assert dispatched == ["AAPL", "MSFT"]
    assert result.errors == 2
    assert result.skipped == 0
    assert result.budget_triggered is False


def test_prefetch_coordinator_normalizes_dispatch_exceptions_and_continues() -> None:
    cfgs = [_futu_cfg("AAPL"), _futu_cfg("MSFT")]
    dispatched: list[str] = []

    def _dispatch(symbol_cfg: dict[str, Any]) -> dict[str, Any]:
        symbol = str(symbol_cfg["symbol"])
        dispatched.append(symbol)
        if symbol == "AAPL":
            raise RuntimeError("adapter failed")
        return _payload(symbol, status="fetched", ok=True, message="fetched")

    result = _run_coordinator(cfgs, _dispatch)

    assert dispatched == ["AAPL", "MSFT"]
    assert result.errors == 1
    assert result.fetched_ok == 1
    assert result.completed_count == 2
    assert result.results["AAPL"] == "RuntimeError: adapter failed"
    assert result.results["MSFT"] == "fetched"
    assert result.audit_items[0]["status"] == "error"
    assert result.audit_items[0]["execution_mode"] == "inprocess"
