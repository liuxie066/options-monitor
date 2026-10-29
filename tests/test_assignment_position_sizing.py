from dataclasses import replace
from decimal import Decimal

import pytest

from domain.domain.portfolio_assignment_scenario import (
    POSITION_SIZING_BASIS,
    project_assignment_scenario,
    project_non_option_assignment_assets,
    select_assignment_positions,
)
from domain.domain.short_vol_assessment import (
    CANDIDATE_POSITION_SIZING_BASIS,
    ShortVolPortfolioContext,
    portfolio_concentration_fields,
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


@pytest.mark.parametrize("mode,existing_value,candidate_value", [("put", 4000, 5000), ("call", 2000, 1000)])
@pytest.mark.parametrize("same_direction", [False, True])
def test_candidate_sizing_only_assigns_its_own_direction(mode, existing_value, candidate_value, same_direction):
    same = option(mode, strike=8 if mode == "put" else 12)
    opposite = option("call" if mode == "put" else "put", strike=15)
    positions = [same] if same_direction else []
    row = {**same, "net_income_cny": 50}

    def calculate(options):
        return portfolio_concentration_fields(row, mode=mode, risk_ctx=ShortVolPortfolioContext(
            nav_cny=None, stock_value_cny_by_symbol={}, short_put_assignment_cny_by_symbol={},
            short_put_assignment_total_cny=None,
            account="lx", portfolio_evidence=evidence(shares=300), assignment_positions=tuple(options),
        ))

    result = calculate(positions)
    assert result["position_sizing_basis"] == CANDIDATE_POSITION_SIZING_BASIS
    assert result["symbol_concentration_current"] == 0.25
    assert result["symbol_concentration_after_existing_assignments"] == (
        round(existing_value / 12200, 6) if same_direction else 0.25
    )
    assert result["symbol_concentration_after"] == round(
        (candidate_value if same_direction else existing_value) / (12450 if same_direction else 12250), 6
    )
    # Opposite-direction failures and other scopes must not change this scenario.
    assert calculate([*positions, opposite, {**opposite, "multiplier": 0, "state_warning": "unsettled"},
                      {**same, "account": "sy"}, {**same, "symbol": "AAPL"}]) == result


@pytest.mark.parametrize("mode", ["put", "call"])
@pytest.mark.parametrize("problem", [
    {"multiplier": 0}, {"state_warning": "unsettled"}, {"lifecycle_state": "conflict"},
    {"pending_close_contracts_by_lot": {"lot": 1}}, {"reserved_contracts_by_lot": {"lot": 1}},
])
def test_directional_sizing_preserves_same_direction_evidence_failures(mode, problem):
    result = portfolio_concentration_fields(
        {**option(mode), "net_income_cny": 50}, mode=mode,
        risk_ctx=ShortVolPortfolioContext(nav_cny=None, stock_value_cny_by_symbol={},
                                         short_put_assignment_cny_by_symbol={}, short_put_assignment_total_cny=None,
                                         account="lx", portfolio_evidence=evidence(shares=300),
                                         assignment_positions=({**option(mode), **problem},)),
    )
    assert result["symbol_concentration_current"] == 0.25
    assert result["symbol_concentration_after_existing_assignments"] is None
    assert result["symbol_concentration_after"] is None
    assert result["concentration_unavailable_reason"]


@pytest.mark.parametrize("mode", ["put", "call"])
def test_directional_sizing_unknown_positions_premium_and_nonpositive_assets(mode):
    row = {**option(mode, strike=8 if mode == "put" else 12), "net_income_cny": 50}
    context = ShortVolPortfolioContext(nav_cny=None, stock_value_cny_by_symbol={},
                                       short_put_assignment_cny_by_symbol={}, short_put_assignment_total_cny=None,
                                       account="lx", portfolio_evidence=evidence(shares=300))
    unknown = portfolio_concentration_fields(row, mode=mode, risk_ctx=context)
    assert unknown["symbol_concentration_current"] == 0.25
    assert unknown["symbol_concentration_after_existing_assignments"] is None
    assert unknown["symbol_concentration_after"] is None
    context = replace(context, assignment_positions=())
    missing_premium = portfolio_concentration_fields({**row, "net_income_cny": None}, mode=mode, risk_ctx=context)
    assert missing_premium["symbol_concentration_after_existing_assignments"] == 0.25
    assert missing_premium["symbol_concentration_after"] is None
    context = replace(context, portfolio_evidence=evidence(cash=-4000, shares=300))
    nonpositive = portfolio_concentration_fields(row, mode=mode, risk_ctx=context)
    assert all(nonpositive[key] is None for key in (
        "symbol_concentration_current", "symbol_concentration_after_existing_assignments", "symbol_concentration_after",
    ))


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


@pytest.mark.parametrize("scenario,types,shares,cash,net,scope", [
    ("short_put", ["put"], "200", "8200.00", "10200.00", "all_open_short_put_assigned"),
    ("short_call", ["call"], "0", "10200.00", "10200.00", "all_open_short_call_assigned"),
    ("all_short", ["put", "call"], "100", "9400.00", "10400.00", "all_open_short_put_and_call_assigned"),
])
def test_account_scenarios_share_selection_and_sizing(scenario, types, shares, cash, net, scope):
    positions = [option("put", strike=8), option("call", strike=12)]
    selected = select_assignment_positions(positions, accounts=["lx"], scenario=scenario)
    out = project_assignment_scenario(
        accounts=["lx"], portfolio_evidence=evidence(), option_positions=positions, snapshot={}, scenario=scenario,
    )
    assert out["scope"]["scenario"] == scope
    assert [row["option_type"] for row in out["assignments"]] == types
    assert [row["option_type"] for row in out["fee_summary"]["items"]] == types
    assert out["summary"]["assignment_count"] == len(types)
    assert out["summary"]["short_put_count"] == types.count("put")
    assert out["summary"]["short_call_count"] == types.count("call")
    assert out["position_changes"][0]["ending_shares"] == shares
    assert out["cash_coverage"]["ending_cash_gross_cny"] == cash
    assert out["account_breakdown"][0]["put_assignment_outflow_cny"] == ("800.00" if "put" in types else "0.00")
    assert out["expiration_ladder"][0]["assignments"] == len(types)
    assert out["position_sizing"] == project_non_option_assignment_assets(
        accounts=["lx"], portfolio_evidence=evidence(), option_positions=selected,
    )
    assert out["position_sizing"]["stock_shares_by_symbol"]["NVDA"] == shares
    assert out["position_sizing"]["net_assets_cny"] == net
    if scenario == "all_short":
        assert out == project_assignment_scenario(
            accounts=["lx"], portfolio_evidence=evidence(), option_positions=positions, snapshot={},
        )


@pytest.mark.parametrize("kind", ["put", "call"])
def test_scenario_excludes_opposite_failures_but_preserves_selected_failures(kind):
    opposite = "call" if kind == "put" else "put"
    selected = option(kind)
    kwargs = dict(accounts=["lx"], portfolio_evidence=evidence(), snapshot={}, scenario=f"short_{kind}")
    expected = project_assignment_scenario(option_positions=[selected], **kwargs)
    assert project_assignment_scenario(option_positions=[
        selected, {**option(opposite), "symbol": "AAPL", "multiplier": 0, "state_warning": "unsettled"},
        option(kind, account="sy"), {**selected, "side": "long"}, {**selected, "status": "closed"},
    ], **kwargs) == expected
    failed = project_assignment_scenario(option_positions=[{**selected, "multiplier": 0}], **kwargs)
    assert failed["status"] == "partial"
    assert failed["position_sizing"]["net_assets_cny"] is None


def test_empty_direction_keeps_baseline_and_does_not_mask_missing_account_evidence():
    kwargs = dict(accounts=["lx"], option_positions=[option("call")], snapshot={}, scenario="short_put")
    out = project_assignment_scenario(portfolio_evidence=evidence(), **kwargs)
    assert out["status"] == "complete"
    assert out["assignments"] == []
    assert out["cash_coverage"]["ending_cash_net_estimated_cny"] == "9000.00"
    assert out["position_sizing"]["net_assets_cny"] == "10000.00"
    bad = evidence()
    bad["status"] = "unavailable"
    failed = project_assignment_scenario(portfolio_evidence=bad, **kwargs)
    assert failed["status"] == "unavailable"
    assert failed["position_sizing"]["net_assets_cny"] is None


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
