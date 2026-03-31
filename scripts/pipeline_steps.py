"""Pipeline step helpers.

Stage 3 refactor target: keep run_pipeline orchestration-only.

These helpers should be deterministic, side-effect free, and easy to unit test.
"""

from __future__ import annotations

from pathlib import Path


def derive_put_max_strike_from_cash(
    symbol: str,
    portfolio_ctx: dict | None,
    fx_usd_per_cny: float | None,
    hkdcny: float | None,
    *,
    fallback_multiplier: int = 100,
) -> float | None:
    """Return a cash-based max_strike cap to prefilter sell_put.

    Preferred cash source (native currency):
    - cash_by_currency[USD/HKD] from holdings
    - minus option_ctx.cash_secured_total_by_ccy[USD/HKD] (short puts already occupying cash)

    strike_cap ~= free_cash_native / multiplier

    Multiplier source: scripts/multiplier_cache.py (best-effort), else fallback 100.

    Note: fx_usd_per_cny / hkdcny are currently unused in this step (kept for call-site compatibility).
    """
    _ = (fx_usd_per_cny, hkdcny)

    if not portfolio_ctx:
        return None

    sym_u = str(symbol).strip().upper()
    want_ccy = 'HKD' if sym_u.endswith('.HK') else 'USD'

    # 1) cash available in native currency (from holdings)
    cash_native = None
    try:
        cash_by = portfolio_ctx.get('cash_by_currency') or {}
        if isinstance(cash_by, dict):
            v = cash_by.get(want_ccy)
            cash_native = float(v) if v is not None else None
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
        cache = multiplier_cache.load_cache(multiplier_cache.default_cache_path(repo_base))
        mult = multiplier_cache.get_cached_multiplier(cache, sym_u)
    except Exception:
        mult = None

    if not mult or mult <= 0:
        mult = int(fallback_multiplier) if fallback_multiplier and fallback_multiplier > 0 else 100

    return free_native / float(mult)
