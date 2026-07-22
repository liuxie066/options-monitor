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


def apply_prefilters(
    *,
    symbol: str,
    sp: dict,
    cc: dict,
    want_put: bool,
    want_call: bool,
    portfolio_ctx: dict | None,
) -> PrefilterResult:
    # Pre-filter (call): sell_call must be based on account-level portfolio context.
    # If portfolio_ctx is unavailable for this account, skip sell_call entirely.
    stock = None
    if want_call:
        if not portfolio_ctx:
            want_call = False
        else:
            try:
                stock = (portfolio_ctx.get('stocks_by_symbol') or {}).get(symbol)
            except Exception:
                stock = None
            if not stock:
                want_call = False

    return PrefilterResult(
        want_put=bool(want_put),
        want_call=bool(want_call),
        sp=sp,
        cc=cc,
        stock=stock,
    )
