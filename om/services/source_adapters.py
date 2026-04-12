from __future__ import annotations

from typing import Any

from om.domain.canonical_schema import normalize_source_snapshot


def adapt_opend_tool_payload(payload: dict[str, Any] | Any) -> dict[str, Any]:
    src = payload if isinstance(payload, dict) else {}
    status = str(src.get("status") or "").lower()
    ok = bool(src.get("ok"))
    status_norm = "ok" if ok and status in {"fetched", "cached"} else ("error" if not ok else "fallback")
    return normalize_source_snapshot(
        source_name="opend",
        status=status_norm,
        as_of_utc=str(src.get("finished_at_utc") or src.get("started_at_utc") or ""),
        payload={
            "symbol": str(src.get("symbol") or "").upper(),
            "tool_name": str(src.get("tool_name") or ""),
            "idempotency_key": str(src.get("idempotency_key") or ""),
            "returncode": src.get("returncode"),
            "message": str(src.get("message") or ""),
        },
        fallback_used=(str(src.get("source") or "").lower() != "opend"),
        error_code=("TOOL_EXEC_FAILED" if not ok else None),
        error_message=(None if ok else str(src.get("message") or "tool execution failed")),
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
