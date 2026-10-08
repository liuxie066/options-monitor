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


def test_put_sizing_does_not_require_a_candidate_notional() -> None:
    ctx = ShortVolPortfolioContext(1_000.0, {"NVDA": 80.0}, {"NVDA": 120.0}, 120.0)
    fields = portfolio_concentration_fields({"symbol": "US.NVDA"}, mode="put", risk_ctx=ctx)
    assert fields["symbol_concentration_current"] == 0.08
    assert fields["symbol_concentration_after_existing_puts"] == 0.2
    assert fields["symbol_concentration_after"] is None
    assert fields["concentration_evaluable"] is False


def test_put_sizing_preserves_trusted_zero_and_unclamped_assignment() -> None:
    ctx = ShortVolPortfolioContext(100.0, {}, {"0700.HK": 120.0}, 120.0)
    fields = portfolio_concentration_fields({"symbol": "HK.700", "cash_required_cny": 30.0}, mode="put", risk_ctx=ctx)
    assert fields["symbol_concentration_current"] == 0.0
    assert fields["symbol_concentration_after_existing_puts"] == 1.2
    assert fields["symbol_concentration_after"] == 1.5
    empty = portfolio_concentration_fields({"symbol": "NVDA"}, mode="put", risk_ctx=ctx)
    assert empty["symbol_concentration_after_existing_puts"] == 0.0


def test_put_sizing_is_bound_to_the_supplied_account_context() -> None:
    row = {"symbol": "NVDA", "cash_required_cny": 30.0}
    lx = portfolio_concentration_fields(row, mode="put", risk_ctx=ShortVolPortfolioContext(1_000.0, {"NVDA": 80.0}, {"NVDA": 120.0}, 120.0))
    sy = portfolio_concentration_fields(row, mode="put", risk_ctx=ShortVolPortfolioContext(500.0, {"NVDA": 100.0}, {}, 0.0))
    assert [lx[k] for k in ("symbol_concentration_current", "symbol_concentration_after_existing_puts", "symbol_concentration_after")] == [0.08, 0.2, 0.23]
    assert [sy[k] for k in ("symbol_concentration_current", "symbol_concentration_after_existing_puts", "symbol_concentration_after")] == [0.2, 0.2, 0.26]


def test_put_sizing_rejects_invalid_or_incomplete_context() -> None:
    contexts = [
        ShortVolPortfolioContext(nav, {"NVDA": 80.0}, {}, 0.0) for nav in (None, 0.0, -1.0, float("nan"), float("inf"))
    ] + [
        ShortVolPortfolioContext(1_000.0, {"NVDA": value}, {}, 0.0) for value in (float("nan"), float("inf"), -1.0)
    ] + [
        ShortVolPortfolioContext(1_000.0, {"NVDA": 80.0}, {}, None),
        ShortVolPortfolioContext(1_000.0, {"NVDA": 80.0}, {}, 0.0, ("portfolio_snapshot_stale",)),
    ]
    for ctx in contexts:
        fields = portfolio_concentration_fields({"symbol": "NVDA", "cash_required_cny": 30.0}, mode="put", risk_ctx=ctx)
        assert fields["symbol_concentration_current"] is None
        assert fields["symbol_concentration_after_existing_puts"] is None


def test_call_does_not_gain_put_sizing_fields() -> None:
    fields = portfolio_concentration_fields({"symbol": "NVDA", "covered_notional_cny": 100.0}, mode="call", risk_ctx=ShortVolPortfolioContext(1_000.0, {}, {}, 0.0))
    assert "symbol_concentration_current" not in fields
    assert "symbol_concentration_after_existing_puts" not in fields
