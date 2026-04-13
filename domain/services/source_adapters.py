from __future__ import annotations

from typing import Any

from domain.domain.canonical_schema import normalize_source_snapshot
from domain.domain.error_policy import classify_failure
from domain.domain.tool_boundary import SCHEMA_KIND_TOOL_EXECUTION, validate_schema_payload


def adapt_opend_tool_payload(payload: dict[str, Any] | Any) -> dict[str, Any]:
    src = validate_schema_payload((payload if isinstance(payload, dict) else {}), kind=SCHEMA_KIND_TOOL_EXECUTION)
    ok = bool(src.get("ok"))
    source_norm = str(src.get("source") or "").strip().lower()
    fallback_used = bool(source_norm and source_norm != "opend")
    status_norm = ("ok" if ok and (not fallback_used) else ("fallback" if ok else "error"))
    message = str(src.get("message") or "").strip()
    failure = (
        classify_failure(
            error_code=str(src.get("error_code") or "TOOL_EXEC_FAILED"),
            message=message,
            upstream="opend",
        )
        if not ok
        else None
    )
    return normalize_source_snapshot(
        source_name="opend",
        status=status_norm,
        as_of_utc=str(src.get("finished_at_utc") or src.get("started_at_utc") or ""),
        payload={
            "symbol": str(src.get("symbol") or "").upper(),
            "tool_name": str(src.get("tool_name") or ""),
            "idempotency_key": str(src.get("idempotency_key") or ""),
            "returncode": src.get("returncode"),
            "message": message,
            "raw_error_code": str(src.get("error_code") or ""),
        },
        fallback_used=fallback_used,
        error_code=(failure or {}).get("error_code"),
        error_category=(failure or {}).get("category"),
        error_message=(None if ok else (message or "tool execution failed")),
    )


def adapt_holdings_context(payload: dict[str, Any] | Any) -> dict[str, Any]:
    ctx = payload if isinstance(payload, dict) else {}
    stocks_by_symbol = ctx.get("stocks_by_symbol")
    cash_by_currency = ctx.get("cash_by_currency")
    if not isinstance(stocks_by_symbol, dict):
        raise ValueError("holdings adapter expects stocks_by_symbol as dict")
    if not isinstance(cash_by_currency, dict):
        raise ValueError("holdings adapter expects cash_by_currency as dict")
    return normalize_source_snapshot(
        source_name="holdings",
        status="ok",
        as_of_utc=str(ctx.get("as_of_utc") or ""),
        payload={
            "filters": (ctx.get("filters") if isinstance(ctx.get("filters"), dict) else {}),
            "stocks_count": len(stocks_by_symbol),
            "cash_currencies": sorted(cash_by_currency.keys()),
            "raw_selected_count": int(ctx.get("raw_selected_count") or 0),
        },
    )


def adapt_option_positions_context(payload: dict[str, Any] | Any) -> dict[str, Any]:
    ctx = payload if isinstance(payload, dict) else {}
    locked_shares = ctx.get("locked_shares_by_symbol")
    cash_secured = ctx.get("cash_secured_by_symbol_by_ccy")
    if not isinstance(locked_shares, dict):
        raise ValueError("option_positions adapter expects locked_shares_by_symbol as dict")
    if not isinstance(cash_secured, dict):
        raise ValueError("option_positions adapter expects cash_secured_by_symbol_by_ccy as dict")
    return normalize_source_snapshot(
        source_name="option_positions",
        status="ok",
        as_of_utc=str(ctx.get("as_of_utc") or ""),
        payload={
            "filters": (ctx.get("filters") if isinstance(ctx.get("filters"), dict) else {}),
            "locked_symbols": len(locked_shares),
            "cash_secured_symbols": len(cash_secured),
            "raw_selected_count": int(ctx.get("raw_selected_count") or 0),
        },
    )
