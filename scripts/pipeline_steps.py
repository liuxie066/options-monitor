"""Pipeline step helpers.

Stage 3 refactor target: keep run_pipeline orchestration-only.

These helpers should be deterministic, side-effect free, and easy to unit test.
"""

from __future__ import annotations

from pathlib import Path

from scripts.trade_symbol_identity import canonical_symbol, symbol_currency


def derive_put_max_strike_from_cash(
    symbol: str,
    portfolio_ctx: dict | None,
    usd_per_cny_exchange_rate: float | None,
    cny_per_hkd_exchange_rate: float | None,
) -> float | None:
    """Return a cash-based max_strike cap to prefilter sell_put.

    Preferred cash source (native currency):
    - cash_by_currency[USD/HKD] from holdings
    - minus option_ctx.cash_secured_total_by_ccy[USD/HKD] (short puts already occupying cash)

    strike_cap ~= free_cash_native / multiplier

    Multiplier source: scripts/multiplier_cache.py (best-effort). Missing => return None.

    Note:
    - usd_per_cny_exchange_rate means USD per 1 CNY
    - cny_per_hkd_exchange_rate means CNY per 1 HKD
    """
    _ = (usd_per_cny_exchange_rate, cny_per_hkd_exchange_rate)

    if not portfolio_ctx:
        return None

    sym_u = canonical_symbol(symbol) or str(symbol or '').strip().upper()
    want_ccy = symbol_currency(sym_u) or 'USD'

    # 1) cash available in native currency (from holdings)
    # Preferred: direct native-currency cash (USD for US symbols, HKD for HK symbols).
    # Fallback: derive native cash from base CNY cash when exchange rates are available.
    cash_native = None
    try:
        cash_by = portfolio_ctx.get('cash_by_currency') or {}
        if isinstance(cash_by, dict):
            v = cash_by.get(want_ccy)
            cash_native = float(v) if v is not None else None
            if cash_native is None:
                cny = cash_by.get('CNY')
                cny_v = float(cny) if cny is not None else None
                if cny_v is not None:
                    if want_ccy == 'USD' and usd_per_cny_exchange_rate is not None and float(usd_per_cny_exchange_rate) > 0:
                        cash_native = cny_v * float(usd_per_cny_exchange_rate)
                    elif want_ccy == 'HKD' and cny_per_hkd_exchange_rate is not None and float(cny_per_hkd_exchange_rate) > 0:
                        cash_native = cny_v / float(cny_per_hkd_exchange_rate)
    except Exception:
        cash_native = None

    if cash_native is None:
        return None

    # 2) subtract cash-secured used in native currency (from option_ctx)
    used_native = 0.0
    try:
        option_ctx = portfolio_ctx.get('option_ctx') if isinstance(portfolio_ctx, dict) else None
        if isinstance(option_ctx, dict):
            tot_by_ccy = option_ctx.get('cash_secured_total_by_ccy') or {}
            if isinstance(tot_by_ccy, dict):
                used_native = float(tot_by_ccy.get(want_ccy) or 0.0)
    except Exception:
        used_native = 0.0

    free_native = float(cash_native) - float(used_native)
    if free_native <= 0:
        return 0.0

    # 3) multiplier from cache
    mult = None
    try:
        from scripts import multiplier_cache
        repo_base = Path(__file__).resolve().parents[1]
        mult = multiplier_cache.resolve_multiplier(
            repo_base=repo_base,
            symbol=sym_u,
            allow_opend_refresh=False,
        )
    except Exception:
        mult = None

    if not mult or mult <= 0:
        # No default: missing multiplier => can't derive a cash-based strike cap safely.
        return None

    return free_native / float(mult)
