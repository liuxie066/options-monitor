"""Symbol prefilters.

Extracted from pipeline_symbol.py (Stage 3).

Goal: keep process_symbol small. Prefilters must be best-effort and never raise.
"""

from __future__ import annotations

from dataclasses import dataclass

from scripts.pipeline_steps import derive_put_max_strike_from_cash


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
    fx_usd_per_cny: float | None,
    hkdcny: float | None,
) -> PrefilterResult:
    # Pre-filter (call): if this account has no holdings row for the symbol, skip sell_call fully.
    stock = None
    if want_call and portfolio_ctx:
        try:
            stock = (portfolio_ctx.get('stocks_by_symbol') or {}).get(symbol)
        except Exception:
            stock = None
        if not stock:
            want_call = False

    # Pre-filter (put): derive a cash-based max_strike cap to reduce chain size.
    if want_put:
        try:
            cash_cap = derive_put_max_strike_from_cash(symbol, portfolio_ctx, fx_usd_per_cny, hkdcny)
            if cash_cap and cash_cap > 0:
                if sp.get('max_strike') is None or float(sp.get('max_strike')) > float(cash_cap):
                    sp = dict(sp)
                    sp['max_strike'] = float(cash_cap)
            if sp.get('max_strike') is not None and float(sp.get('max_strike')) <= 0:
                want_put = False
        except Exception:
            pass

    return PrefilterResult(
        want_put=bool(want_put),
        want_call=bool(want_call),
        sp=sp,
        cc=cc,
        stock=stock,
    )
