from domain.domain.short_vol_assessment import (
    ShortVolPortfolioContext,
    portfolio_concentration_fields,
)


def test_put_concentration_canonicalizes_symbol_and_preserves_warnings() -> None:
    fields = portfolio_concentration_fields(
        {"symbol": "US.NVDA", "cash_required_cny": 50.0},
        mode="put",
        risk_ctx=ShortVolPortfolioContext(
            nav_cny=1_000.0,
            stock_value_cny_by_symbol={"NVDA": 100.0},
            short_put_assignment_cny_by_symbol={"NVDA": 200.0},
            short_put_assignment_total_cny=300.0,
            warnings=("stock_value_estimated_from_avg_cost:NVDA",),
        ),
    )

    assert fields["assignment_notional_cny"] == 50.0
    assert fields["existing_stock_value_cny_symbol"] == 100.0
    assert fields["existing_short_put_assignment_cny_symbol"] == 200.0
    assert fields["single_trade_concentration"] == 0.05
    assert fields["symbol_concentration_after"] == 0.35
    assert fields["total_short_put_concentration_after"] == 0.35
    assert fields["concentration_score"] == 0.65
    assert fields["concentration_evaluable"] is True
    assert fields["portfolio_risk_warnings"] == "stock_value_estimated_from_avg_cost:NVDA"


def test_call_concentration_projects_covered_notional_for_canonical_symbol() -> None:
    fields = portfolio_concentration_fields(
        {"symbol": "HK.700", "underlying_notional_cny": 400.0},
        mode="call",
        risk_ctx=ShortVolPortfolioContext(
            nav_cny=1_000.0,
            stock_value_cny_by_symbol={"0700.HK": 250.0},
            short_put_assignment_cny_by_symbol={"0700.HK": 50.0},
            short_put_assignment_total_cny=100.0,
        ),
    )

    assert fields["assignment_notional_cny"] is None
    assert fields["covered_notional_cny"] == 400.0
    assert fields["single_trade_concentration"] == 0.4
    assert fields["symbol_concentration_after"] == 0.4
    assert fields["total_short_put_concentration_after"] == 0.1
    assert fields["concentration_score"] == 0.6
    assert fields["concentration_evaluable"] is True


def test_missing_concentration_is_explicit_and_keeps_warning_reasons() -> None:
    fields = portfolio_concentration_fields(
        {"symbol": "NVDA"},
        mode="put",
        risk_ctx=ShortVolPortfolioContext(
            nav_cny=None,
            stock_value_cny_by_symbol={},
            short_put_assignment_cny_by_symbol={},
            short_put_assignment_total_cny=None,
            unavailable_reasons=("holdings_context_missing",),
            warnings=("portfolio_snapshot_stale",),
        ),
    )

    assert fields["concentration_evaluable"] is False
    assert fields["single_trade_concentration"] is None
    assert fields["symbol_concentration_after"] is None
    assert fields["total_short_put_concentration_after"] is None
    assert fields["concentration_score"] is None
    assert fields["concentration_unavailable_reason"] == "holdings_context_missing"
    assert fields["portfolio_risk_warnings"] == "portfolio_snapshot_stale"
