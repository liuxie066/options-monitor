from decimal import Decimal

import pytest

from domain.domain.portfolio_assignment_scenario import (
    POSITION_SIZING_BASIS,
    project_assignment_scenario,
    project_non_option_assignment_assets,
)
from test_portfolio_assignment_scenario import _evidence, _quote


def evidence(*, cash=9000, shares=100, price=10):
    return _evidence(
        holdings=[
            {
                "account": "lx",
                "broker": "富途",
                "code": "CNY",
                "asset_type": "cash",
                "currency": "CNY",
                "quantity": cash,
                "market_value_cny": cash,
            },
            {
                "account": "lx",
                "broker": "富途",
                "code": "NVDA",
                "asset_type": "us_stock",
                "currency": "CNY",
                "quantity": shares,
                "market_value_cny": shares * price,
            },
        ],
        quotes=[_quote("NVDA", currency="CNY", price=price, cny_price=price, fx=1)],
    )


def option(kind, *, strike=8, account="lx", broker="富途", multiplier=100):
    return {
        "account": account,
        "broker": broker,
        "symbol": "NVDA",
        "option_type": kind,
        "status": "open",
        "side": "short",
        "contracts_open": 1,
        "multiplier": multiplier,
        "strike": strike,
        "currency": "CNY",
        "expiration_ymd": "2026-12-18",
    }


@pytest.mark.parametrize("kind,expected_net,expected_stock", [("put", 10200, 2000), ("call", 9800, 0)])
def test_assignment_moves_shares_and_cash_together(kind, expected_net, expected_stock):
    out = project_non_option_assignment_assets(
        accounts=["lx"], portfolio_evidence=evidence(), option_positions=[option(kind)]
    )
    assert out["basis"] == POSITION_SIZING_BASIS
    assert Decimal(out["net_assets_cny"]) == expected_net
    assert Decimal(out["stock_value_cny_by_symbol"]["NVDA"]) == expected_stock
    assert Decimal(out["weight_of_net_assets_by_symbol"]["NVDA"]) == Decimal(expected_stock / expected_net).quantize(
        Decimal("0.000001")
    )


def test_mixed_assignment_and_candidate_premium_once():
    out = project_non_option_assignment_assets(
        accounts=["lx"],
        portfolio_evidence=evidence(),
        option_positions=[option("put", strike=8), option("call", strike=12)],
        candidate_net_premium_cny=50,
    )
    assert out["net_assets_cny"] == "10450.00"
    assert out["stock_shares_by_symbol"]["NVDA"] == "100"


def test_equal_strike_spot_conserves_assets_and_query_reuses_projection():
    opts = [option("put", strike=10)]
    out = project_assignment_scenario(
        accounts=["lx"], portfolio_evidence=evidence(), option_positions=opts, snapshot={}
    )
    assert out["position_sizing"] == project_non_option_assignment_assets(
        accounts=["lx"], portfolio_evidence=evidence(), option_positions=opts
    )
    assert out["position_sizing"]["net_assets_cny"] == "10000.00"


def test_signed_assets_greater_than_one_and_nonpositive_denominator():
    out = project_non_option_assignment_assets(
        accounts=["lx"], portfolio_evidence=evidence(cash=-500), option_positions=[]
    )
    assert out["weight_of_net_assets_by_symbol"]["NVDA"] == "2.000000"
    out = project_non_option_assignment_assets(
        accounts=["lx"], portfolio_evidence=evidence(cash=-1500), option_positions=[]
    )
    assert out["weight_of_net_assets_by_symbol"]["NVDA"] is None
    assert "non_positive_net_assets" in out["unavailable_reasons"]


def test_account_isolation_partial_and_missing_quote_are_not_zero():
    out = project_non_option_assignment_assets(
        accounts=["lx"], portfolio_evidence=evidence(), option_positions=[option("put", account="sy")]
    )
    assert out["net_assets_cny"] == "10000.00"
    data = evidence()
    data["quotes"] = []
    out = project_non_option_assignment_assets(accounts=["lx"], portfolio_evidence=data, option_positions=[])
    assert out["net_assets_cny"] is None
    assert out["unavailable_reasons"]


def test_missing_non_futu_baseline_and_multiplier_are_unknown():
    for opts in ([option("put", broker="银行")], [option("put", multiplier=0)]):
        out = project_non_option_assignment_assets(
            accounts=["lx"], portfolio_evidence=evidence(), option_positions=opts
        )
        assert out["net_assets_cny"] is None
        assert out["unavailable_reasons"]


def test_cash_and_stocks_use_same_explicit_fx_instead_of_embedded_values():
    data = evidence()
    data["fx_rates_to_cny"] = {"USDCNY": 7}
    data["holdings"][0].update(currency="USD", quantity=100, market_value_cny=800)
    data["holdings"][1].update(currency="USD", quantity=100, market_value_cny=8000)
    data["quotes"] = [_quote("NVDA", currency="USD", price=10, cny_price=80, fx=8)]
    out = project_non_option_assignment_assets(accounts=["lx"], portfolio_evidence=data, option_positions=[])
    assert out["net_assets_cny"] == "7700.00"
    assert out["stock_value_cny_by_symbol"]["NVDA"] == "7000.00"


def test_complete_non_futu_stock_and_cash_baseline_is_included():
    data = evidence()
    data["holdings"] += [
        {
            "account": "lx",
            "broker": "银行",
            "code": "CNY",
            "asset_type": "cash",
            "currency": "CNY",
            "quantity": 1000,
            "market_value_cny": 1000,
        },
        {
            "account": "lx",
            "broker": "银行",
            "code": "NVDA",
            "asset_type": "stock",
            "currency": "CNY",
            "quantity": 100,
            "market_value_cny": 1,
        },
    ]
    out = project_non_option_assignment_assets(
        accounts=["lx"], portfolio_evidence=data, option_positions=[option("call", strike=12, broker="银行")]
    )
    assert out["net_assets_cny"] == "12200.00"
    assert out["stock_value_cny_by_symbol"]["NVDA"] == "1000.00"


@pytest.mark.parametrize("lifecycle", [
    {"lifecycle_state": "conflict"},
    {"pending_close_contracts_by_lot": {"lot": 1}},
    {"reserved_contracts_by_lot": {"lot": 1}},
])
def test_unsettled_option_lifecycle_cannot_produce_known_assignment_weight(lifecycle):
    out = project_non_option_assignment_assets(
        accounts=["lx"], portfolio_evidence=evidence(), option_positions=[{**option("put"), **lifecycle}],
    )
    assert out["net_assets_cny"] is None
    assert out["unavailable_reasons"]
