import pytest
from domain.domain.short_vol_assessment import ShortVolPortfolioContext, portfolio_concentration_fields
from test_assignment_position_sizing import evidence, option


def context(*, cash=9000, shares=100, positions=(), account="lx"):
    data = evidence(cash=cash, shares=shares)
    for row in data["holdings"]:
        row["account"] = account
    data["scope"]["accounts"] = [account]
    data["account_status"] = [{"account": account, "status": "complete"}]
    return ShortVolPortfolioContext(
        None, {}, {}, 0, account=account, portfolio_evidence=data, assignment_positions=tuple(positions)
    )


def candidate(**overrides):
    return {"symbol": "US.NVDA", "strike": 8, "multiplier": 100, "currency": "CNY", "net_income_cny": 50, **overrides}


def test_put_uses_stock_spot_cash_at_strike_and_candidate_net_premium():
    fields = portfolio_concentration_fields(candidate(), mode="put", risk_ctx=context(positions=[option("put")]))
    assert fields["symbol_concentration_current"] == 0.1
    assert fields["symbol_concentration_after_existing_assignments"] == round(2000 / 10200, 6)
    assert fields["symbol_concentration_after"] == round(3000 / 10450, 6)
    assert fields["portfolio_nav_after_candidate_assignment_cny"] == 10450
    assert fields["concentration_evaluable"]
    assert "symbol_concentration_after_existing_puts" not in fields


def test_cc_and_put_share_mixed_existing_scenario():
    ctx = context(positions=[option("put", strike=8), option("call", strike=12)])
    put = portfolio_concentration_fields(candidate(), mode="put", risk_ctx=ctx)
    call = portfolio_concentration_fields(candidate(strike=12), mode="call", risk_ctx=ctx)
    assert (
        put["symbol_concentration_after_existing_assignments"]
        == call["symbol_concentration_after_existing_assignments"]
        == round(1000 / 10400, 6)
    )
    assert call["symbol_concentration_after"] == 0


def test_current_and_existing_independent_of_missing_candidate_premium():
    fields = portfolio_concentration_fields(candidate(net_income_cny=None), mode="put", risk_ctx=context())
    assert fields["symbol_concentration_current"] == 0.1
    assert fields["symbol_concentration_after_existing_assignments"] == 0.1
    assert fields["symbol_concentration_after"] is None
    assert "candidate_net_premium_missing" in fields["concentration_unavailable_reason"]


def test_zero_and_greater_than_one_preserved():
    assert (
        portfolio_concentration_fields(candidate(), mode="put", risk_ctx=context(shares=0))[
            "symbol_concentration_current"
        ]
        == 0
    )
    assert (
        portfolio_concentration_fields(candidate(), mode="put", risk_ctx=context(cash=-500))[
            "symbol_concentration_current"
        ]
        == 2
    )


def test_account_and_symbol_scope():
    fields = portfolio_concentration_fields(
        candidate(), mode="put", risk_ctx=context(account="sy", positions=[option("put", account="lx")])
    )
    assert fields["symbol_concentration_after_existing_assignments"] == 0.1


@pytest.mark.parametrize("cash", [-1000, -2000])
def test_nonpositive_net_assets_unknown(cash):
    fields = portfolio_concentration_fields(candidate(), mode="put", risk_ctx=context(cash=cash))
    assert fields["symbol_concentration_current"] is None


def test_missing_frozen_evidence_is_not_legacy_fallback():
    fields = portfolio_concentration_fields(
        candidate(), mode="put", risk_ctx=ShortVolPortfolioContext(10000, {"NVDA": 1000}, {}, 0)
    )
    assert fields["symbol_concentration_current"] is None
    assert fields["symbol_concentration_after"] is None


def test_missing_option_snapshot_only_blocks_assigned_scenarios():
    ctx = context()
    from dataclasses import replace

    fields = portfolio_concentration_fields(candidate(), mode="put", risk_ctx=replace(ctx, assignment_positions=None))
    assert fields["symbol_concentration_current"] == 0.1
    assert fields["symbol_concentration_after_existing_assignments"] is None
