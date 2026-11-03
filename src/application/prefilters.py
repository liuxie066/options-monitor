"""Symbol prefilters.

Extracted from pipeline_symbol.py (Stage 3).

Goal: keep process_symbol small. Prefilters must be best-effort and never raise.
"""

from __future__ import annotations

from dataclasses import dataclass
from domain.domain.risk_capacity import broker_capacity_binding
from domain.domain.symbol_identity import canonical_symbol
from src.application.portfolio_context_service import portfolio_position_snapshot_errors


@dataclass
class PrefilterResult:
    want_put: bool
    want_call: bool
    sp: dict
    cc: dict
    stock: dict | None
    call_skip_reason: str | None = None


def covered_call_capacity_binding(context: dict) -> dict:
    """Keep the Futu CC contract; require explicit binding for Longbridge."""
    if str(context.get("portfolio_source_name") or "").strip().lower() == "futu":
        authority = context.get("capacity_authority")
        return {**authority, "broker_id": "futu"} if isinstance(authority, dict) else {}
    return broker_capacity_binding(context)


def stock_matches_capacity_binding(stock: dict, context: dict, *, symbol: str) -> bool:
    """Bind stock capacity to the same physical account as the portfolio."""
    binding = covered_call_capacity_binding(context)
    if binding.get("broker_id") == "futu":
        return str(binding.get("status") or "").strip().lower() == "available"
    if binding.get("status") != "available" or any(not binding.get(key) for key in (
        "broker_id", "broker_account_id", "logical_account", "trd_env", "market",
    )):
        return False
    broker = binding["broker_id"]
    expected = {
        "account": binding["logical_account"], "trd_env": binding["trd_env"],
        "market": binding["market"], "capacity_authority_status": "available",
        "capacity_identity_hash": context.get("capacity_identity_hash"),
        "symbol": canonical_symbol(symbol),
        "broker_id": broker, "broker_account_id": binding["broker_account_id"],
    }
    if broker == "longbridge" and "futu_account_id" in stock:
        return False
    return all(value is not None and stock.get(key) == value
               for key, value in expected.items())


def apply_prefilters(
    *,
    symbol: str,
    sp: dict,
    cc: dict,
    want_put: bool,
    want_call: bool,
    portfolio_ctx: dict | None,
    demo_capacity: bool = False,
) -> PrefilterResult:
    # Pre-filter (call): sell_call must be based on account-level portfolio context.
    # If portfolio_ctx is unavailable for this account, skip sell_call entirely.
    stock = None
    call_skip_reason = None
    if want_call and not demo_capacity:
        if not isinstance(portfolio_ctx, dict):
            want_call = False
            call_skip_reason = "covered_call_portfolio_context_unavailable"
        else:
            try:
                authority = covered_call_capacity_binding(portfolio_ctx)
                stock_context_available = not portfolio_position_snapshot_errors(portfolio_ctx)
                if (
                    authority.get("status") != "available"
                    or not stock_context_available
                ):
                    want_call = False
                    call_skip_reason = "covered_call_portfolio_context_unavailable"
                elif not isinstance(portfolio_ctx.get("stocks_by_symbol"), dict):
                    want_call = False
                    call_skip_reason = "covered_call_portfolio_context_unavailable"
                else:
                    stocks_by_symbol = portfolio_ctx["stocks_by_symbol"]
                    if symbol not in stocks_by_symbol:
                        want_call = False
                        call_skip_reason = "covered_call_underlying_not_held"
                    else:
                        raw_stock = stocks_by_symbol.get(symbol)
                        if (isinstance(raw_stock, dict) and raw_stock
                                and stock_matches_capacity_binding(raw_stock, portfolio_ctx, symbol=symbol)):
                            stock = raw_stock
                        else:
                            want_call = False
                            call_skip_reason = "covered_call_portfolio_context_unavailable"
            except Exception:
                want_call = False
                call_skip_reason = "covered_call_portfolio_context_unavailable"

    return PrefilterResult(
        want_put=bool(want_put),
        want_call=bool(want_call),
        sp=sp,
        cc=cc,
        stock=stock,
        call_skip_reason=call_skip_reason,
    )
