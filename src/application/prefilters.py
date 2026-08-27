"""Symbol prefilters.

Extracted from pipeline_symbol.py (Stage 3).

Goal: keep process_symbol small. Prefilters must be best-effort and never raise.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class PrefilterResult:
    want_put: bool
    want_call: bool
    sp: dict
    cc: dict
    stock: dict | None
    call_skip_reason: str | None = None


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
                authority = portfolio_ctx.get("capacity_authority")
                portfolio_source = str(
                    portfolio_ctx.get("portfolio_source_name") or ""
                ).strip().lower()
                authority_status = str(
                    authority.get("status") if isinstance(authority, dict) else ""
                ).strip().lower()
                if (
                    portfolio_source != "futu"
                    or not isinstance(authority, dict)
                    or authority_status != "available"
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
                        if isinstance(raw_stock, dict) and raw_stock:
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
