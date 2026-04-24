from __future__ import annotations

from unittest.mock import patch

from scripts.pipeline_steps import derive_put_max_strike_from_cash
from scripts.prefilters import apply_prefilters


def test_apply_prefilters_disables_sell_call_without_portfolio_context() -> None:
    pf = apply_prefilters(
        symbol='NVDA',
        sp={'enabled': False},
        cc={'enabled': True, 'avg_cost': 100, 'shares': 100},
        want_put=False,
        want_call=True,
        portfolio_ctx=None,
        usd_per_cny_exchange_rate=None,
        cny_per_hkd_exchange_rate=None,
    )
    assert pf.want_call is False


def test_apply_prefilters_keeps_sell_call_with_futu_portfolio_stock() -> None:
    pf = apply_prefilters(
        symbol='NVDA',
        sp={'enabled': False},
        cc={'enabled': True},
        want_put=False,
        want_call=True,
        portfolio_ctx={
            'portfolio_source_name': 'futu',
            'stocks_by_symbol': {
                'NVDA': {'symbol': 'NVDA', 'shares': 200, 'avg_cost': 100.0, 'currency': 'USD'}
            },
        },
        usd_per_cny_exchange_rate=None,
        cny_per_hkd_exchange_rate=None,
    )
    assert pf.want_call is True
    assert pf.stock is not None
    assert pf.stock['shares'] == 200
    assert pf.stock['avg_cost'] == 100.0


def test_derive_put_cash_cap_uses_cny_fallback_for_us_symbols() -> None:
    # 70,000 CNY -> 9,800 USD at 0.14; minus 2,000 USD secured => 7,800 USD free.
    # With multiplier 100, strike cap should be 78.
    ctx = {
        'cash_by_currency': {'CNY': 70000.0},
        'option_ctx': {'cash_secured_total_by_ccy': {'USD': 2000.0}},
    }
    with patch('scripts.multiplier_cache.load_cache', return_value={}):
        with patch('scripts.multiplier_cache.get_cached_multiplier', return_value=100):
            out = derive_put_max_strike_from_cash('NVDA', ctx, usd_per_cny_exchange_rate=0.14, cny_per_hkd_exchange_rate=None)
    assert out is not None
    assert abs(float(out) - 78.0) < 1e-9
