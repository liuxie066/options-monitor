from __future__ import annotations

import pytest

from src.application.prefilters import apply_prefilters


def test_apply_prefilters_disables_sell_call_without_portfolio_context() -> None:
    pf = apply_prefilters(
        symbol='NVDA',
        sp={'enabled': False},
        cc={'enabled': True, 'avg_cost': 100, 'shares': 100},
        want_put=False,
        want_call=True,
        portfolio_ctx=None,
    )
    assert pf.want_call is False
    assert pf.call_skip_reason == "covered_call_portfolio_context_unavailable"


def test_apply_prefilters_rejects_unbound_portfolio_stock() -> None:
    pf = apply_prefilters(
        symbol='NVDA',
        sp={'enabled': False},
        cc={'enabled': True},
        want_put=False,
        want_call=True,
        portfolio_ctx={
            'portfolio_source_name': 'futu',
            'stocks_by_symbol': {'NVDA': {'shares': 100}},
        },
    )

    assert pf.want_call is False
    assert pf.call_skip_reason == 'covered_call_portfolio_context_unavailable'


def test_apply_prefilters_treats_shared_symbol_without_account_holding_as_benign() -> None:
    pf = apply_prefilters(
        symbol="3690.HK",
        sp={"enabled": False},
        cc={"enabled": True},
        want_put=False,
        want_call=True,
        portfolio_ctx={
            "portfolio_source_name": "futu",
            "capacity_authority": {"status": "available"},
            "stocks_by_symbol": {
                "0700.HK": {
                    "symbol": "0700.HK",
                    "shares": 100,
                    "avg_cost": 470.0,
                    "currency": "HKD",
                }
            },
        },
    )

    assert pf.want_call is False
    assert pf.stock is None
    assert pf.call_skip_reason == "covered_call_underlying_not_held"


def test_apply_prefilters_keeps_sell_call_with_futu_portfolio_stock() -> None:
    pf = apply_prefilters(
        symbol='NVDA',
        sp={'enabled': False},
        cc={'enabled': True},
        want_put=False,
        want_call=True,
        portfolio_ctx={
            'portfolio_source_name': 'futu',
            'capacity_authority': {'status': 'available'},
            'stocks_by_symbol': {
                'NVDA': {'symbol': 'NVDA', 'shares': 200, 'avg_cost': 100.0, 'currency': 'USD'}
            },
        },
    )
    assert pf.want_call is True
    assert pf.stock is not None
    assert pf.stock['shares'] == 200
    assert pf.stock['avg_cost'] == 100.0
    assert pf.call_skip_reason is None


@pytest.mark.parametrize(
    "portfolio_ctx",
    [
        {
            "cash_by_currency": {"CNY": 520000.0, "HKD": 18000.0},
            "option_ctx": {"cash_secured_total_by_ccy": {"HKD": 120000.0, "USD": 500.0}},
        },
        {
            "cash_by_currency": {"CNY": 910000.0},
            "option_ctx": {"cash_secured_total_by_ccy": {"HKD": 260000.0, "USD": 1000.0}},
        },
        {"cash_by_currency": {"USD": 0.0}},
        None,
    ],
    ids=("lx-production-shape", "sy-no-usd", "zero-native-cash", "missing-context"),
)
def test_apply_prefilters_keeps_sell_put_market_config_account_invariant(
    portfolio_ctx: dict | None,
) -> None:
    sell_put = {
        "enabled": True,
        "min_dte": 7,
        "max_dte": 60,
        "max_strike": 45.0,
    }

    result = apply_prefilters(
        symbol="TCOM",
        sp=sell_put,
        cc={"enabled": False},
        want_put=True,
        want_call=False,
        portfolio_ctx=portfolio_ctx,
    )

    assert result.want_put is True
    assert result.sp == sell_put
