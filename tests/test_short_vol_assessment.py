from __future__ import annotations


def test_shared_short_vol_assessment_accepts_sell_put_and_covered_call() -> None:
    from domain.domain.short_vol_assessment import (
        ShortVolAssessmentConfig,
        ShortVolPortfolioContext,
        assess_short_vol_candidate,
    )

    cfg = ShortVolAssessmentConfig()
    ctx = ShortVolPortfolioContext(
        nav_cny=1_000_000.0,
        stock_value_cny_by_symbol={"NVDA": 50_000.0},
        short_put_assignment_cny_by_symbol={"NVDA": 50_000.0},
        short_put_assignment_total_cny=100_000.0,
    )

    put = assess_short_vol_candidate(
        {
            "symbol": "NVDA",
            "implied_volatility": 0.36,
            "realized_volatility_estimate": 0.24,
            "delta": -0.20,
            "spot": 120.0,
            "strike": 100.0,
            "dte": 30,
            "net_income_cny": 1_400.0,
            "option_contract_point_value_cny": 700.0,
            "cash_required_cny": 70_000.0,
            "event_source_status": "ok",
        },
        mode="put",
        cfg=cfg,
        risk_ctx=ctx,
    )
    call = assess_short_vol_candidate(
        {
            "symbol": "NVDA",
            "implied_volatility": 0.36,
            "realized_volatility_estimate": 0.24,
            "delta": 0.20,
            "spot": 120.0,
            "strike": 140.0,
            "dte": 30,
            "net_income_cny": 1_400.0,
            "option_contract_point_value_cny": 700.0,
            "covered_notional_cny": 70_000.0,
            "event_source_status": "ok",
        },
        mode="call",
        cfg=cfg,
        risk_ctx=ctx,
    )

    assert put["accepted"] is True
    assert call["accepted"] is True
    assert put["fields"]["short_gamma_profile"] == "short_gamma"
    assert call["fields"]["short_gamma_profile"] == "short_gamma"
    assert put["fields"]["equity_delta_equivalent"] == 0.2
    assert call["fields"]["equity_delta_equivalent"] == 0.8


def test_shared_short_vol_assessment_rejects_covered_call_without_vol_edge() -> None:
    from domain.domain.short_vol_assessment import (
        ShortVolAssessmentConfig,
        ShortVolPortfolioContext,
        assess_short_vol_candidate,
    )

    decision = assess_short_vol_candidate(
        {
            "symbol": "NVDA",
            "implied_volatility": 0.25,
            "realized_volatility_estimate": 0.24,
            "delta": 0.20,
            "covered_notional_cny": 70_000.0,
        },
        mode="call",
        cfg=ShortVolAssessmentConfig(),
        risk_ctx=ShortVolPortfolioContext(
            nav_cny=1_000_000.0,
            stock_value_cny_by_symbol={"NVDA": 100_000.0},
            short_put_assignment_cny_by_symbol={},
            short_put_assignment_total_cny=0.0,
        ),
    )

    assert decision["accepted"] is False
    assert decision["rule"] == "vol_edge_ratio_below_min"


def test_shared_short_vol_assessment_rejects_covered_call_concentration() -> None:
    from domain.domain.short_vol_assessment import (
        ShortVolAssessmentConfig,
        ShortVolPortfolioContext,
        assess_short_vol_candidate,
    )

    decision = assess_short_vol_candidate(
        {
            "symbol": "NVDA",
            "implied_volatility": 0.36,
            "realized_volatility_estimate": 0.24,
            "delta": 0.20,
            "covered_notional_cny": 120_000.0,
            "event_source_status": "ok",
        },
        mode="call",
        cfg=ShortVolAssessmentConfig(max_single_trade_nav_pct=0.08),
        risk_ctx=ShortVolPortfolioContext(
            nav_cny=1_000_000.0,
            stock_value_cny_by_symbol={"NVDA": 120_000.0},
            short_put_assignment_cny_by_symbol={},
            short_put_assignment_total_cny=0.0,
        ),
    )

    assert decision["accepted"] is False
    assert decision["rule"] == "single_trade_concentration_exceeded"


def test_shared_short_vol_assessment_rejects_event_before_expiry() -> None:
    from domain.domain.short_vol_assessment import (
        ShortVolAssessmentConfig,
        ShortVolPortfolioContext,
        assess_short_vol_candidate,
    )

    decision = assess_short_vol_candidate(
        {
            "symbol": "NVDA",
            "implied_volatility": 0.36,
            "realized_volatility_estimate": 0.24,
            "delta": -0.20,
            "spot": 120.0,
            "strike": 100.0,
            "dte": 30,
            "net_income_cny": 1_400.0,
            "option_contract_point_value_cny": 700.0,
            "cash_required_cny": 70_000.0,
            "event_flag": True,
            "event_types": "earnings",
            "event_dates": "2026-06-01",
            "event_source_status": "ok",
        },
        mode="put",
        cfg=ShortVolAssessmentConfig(),
        risk_ctx=ShortVolPortfolioContext(
            nav_cny=1_000_000.0,
            stock_value_cny_by_symbol={},
            short_put_assignment_cny_by_symbol={},
            short_put_assignment_total_cny=0.0,
        ),
    )

    assert decision["accepted"] is False
    assert decision["rule"] == "event_risk_within_expiry"
    assert decision["fields"]["event_risk_types"] == "earnings"


def test_shared_short_vol_assessment_rejects_event_source_unavailable() -> None:
    from domain.domain.short_vol_assessment import (
        ShortVolAssessmentConfig,
        ShortVolPortfolioContext,
        assess_short_vol_candidate,
    )

    decision = assess_short_vol_candidate(
        {
            "symbol": "NVDA",
            "implied_volatility": 0.36,
            "realized_volatility_estimate": 0.24,
            "delta": -0.20,
            "spot": 120.0,
            "strike": 100.0,
            "dte": 30,
            "net_income_cny": 1_400.0,
            "option_contract_point_value_cny": 700.0,
            "cash_required_cny": 70_000.0,
            "event_source_status": "error",
            "event_source_error": "timeout",
        },
        mode="put",
        cfg=ShortVolAssessmentConfig(),
        risk_ctx=ShortVolPortfolioContext(
            nav_cny=1_000_000.0,
            stock_value_cny_by_symbol={},
            short_put_assignment_cny_by_symbol={},
            short_put_assignment_total_cny=0.0,
        ),
    )

    assert decision["accepted"] is False
    assert decision["rule"] == "event_source_unavailable"


def test_shared_short_vol_assessment_rejects_sell_put_sigma_stress_loss() -> None:
    from domain.domain.short_vol_assessment import (
        ShortVolAssessmentConfig,
        ShortVolPortfolioContext,
        assess_short_vol_candidate,
    )

    decision = assess_short_vol_candidate(
        {
            "symbol": "NVDA",
            "implied_volatility": 0.70,
            "realized_volatility_estimate": 0.50,
            "delta": -0.20,
            "spot": 102.0,
            "strike": 100.0,
            "dte": 60,
            "net_income_cny": 140.0,
            "option_contract_point_value_cny": 700.0,
            "cash_required_cny": 70_000.0,
            "event_source_status": "ok",
        },
        mode="put",
        cfg=ShortVolAssessmentConfig(),
        risk_ctx=ShortVolPortfolioContext(
            nav_cny=1_000_000.0,
            stock_value_cny_by_symbol={},
            short_put_assignment_cny_by_symbol={},
            short_put_assignment_total_cny=0.0,
        ),
    )

    assert decision["accepted"] is False
    assert decision["rule"] == "put_sigma_stress_loss_exceeded"
    assert decision["fields"]["put_stress_down_loss_nav_pct"] > 0.02


def test_shared_short_vol_assessment_does_not_use_option_last_price_as_spot() -> None:
    from domain.domain.short_vol_assessment import (
        ShortVolAssessmentConfig,
        ShortVolPortfolioContext,
        assess_short_vol_candidate,
    )

    decision = assess_short_vol_candidate(
        {
            "symbol": "NVDA",
            "implied_volatility": 0.36,
            "realized_volatility_estimate": 0.24,
            "delta": -0.20,
            "last_price": 1.10,
            "strike": 100.0,
            "dte": 30,
            "net_income_cny": 1_400.0,
            "option_contract_point_value_cny": 700.0,
            "cash_required_cny": 70_000.0,
            "event_source_status": "ok",
        },
        mode="put",
        cfg=ShortVolAssessmentConfig(),
        risk_ctx=ShortVolPortfolioContext(
            nav_cny=1_000_000.0,
            stock_value_cny_by_symbol={},
            short_put_assignment_cny_by_symbol={},
            short_put_assignment_total_cny=0.0,
        ),
    )

    assert decision["accepted"] is False
    assert decision["rule"] == "path_stress_inputs_missing"
    assert "spot" in decision["fields"]["path_stress_unavailable_reason"]
    assert decision["fields"]["put_stress_down_loss_nav_pct"] is None


def test_shared_short_vol_assessment_requires_explicit_contract_point_value() -> None:
    from domain.domain.short_vol_assessment import (
        ShortVolAssessmentConfig,
        ShortVolPortfolioContext,
        assess_short_vol_candidate,
    )

    decision = assess_short_vol_candidate(
        {
            "symbol": "NVDA",
            "implied_volatility": 0.36,
            "realized_volatility_estimate": 0.24,
            "delta": -0.20,
            "spot": 120.0,
            "strike": 100.0,
            "dte": 30,
            "net_income_cny": 1_400.0,
            "cash_required_cny": 70_000.0,
            "event_source_status": "ok",
        },
        mode="put",
        cfg=ShortVolAssessmentConfig(),
        risk_ctx=ShortVolPortfolioContext(
            nav_cny=1_000_000.0,
            stock_value_cny_by_symbol={},
            short_put_assignment_cny_by_symbol={},
            short_put_assignment_total_cny=0.0,
        ),
    )

    assert decision["accepted"] is False
    assert decision["rule"] == "path_stress_inputs_missing"
    assert "option_contract_point_value_cny" in decision["fields"]["path_stress_unavailable_reason"]
    assert decision["fields"]["option_contract_point_value_cny"] is None


def test_shared_short_vol_assessment_reports_covered_call_gap_up_cost_within_budget() -> None:
    from domain.domain.short_vol_assessment import (
        ShortVolAssessmentConfig,
        ShortVolPortfolioContext,
        assess_short_vol_candidate,
    )

    decision = assess_short_vol_candidate(
        {
            "symbol": "NVDA",
            "implied_volatility": 0.36,
            "realized_volatility_estimate": 0.24,
            "delta": 0.20,
            "spot": 120.0,
            "strike": 125.0,
            "dte": 30,
            "net_income_cny": 1_400.0,
            "option_contract_point_value_cny": 700.0,
            "covered_notional_cny": 84_000.0,
            "event_source_status": "ok",
        },
        mode="call",
        cfg=ShortVolAssessmentConfig(max_single_trade_nav_pct=0.10),
        risk_ctx=ShortVolPortfolioContext(
            nav_cny=1_000_000.0,
            stock_value_cny_by_symbol={"NVDA": 84_000.0},
            short_put_assignment_cny_by_symbol={},
            short_put_assignment_total_cny=0.0,
        ),
    )

    assert decision["accepted"] is True
    assert decision["fields"]["call_gap_up_price"] == 132.0
    assert decision["fields"]["call_gap_up_opportunity_cost_cny"] == 3500.0
    assert decision["fields"]["call_gap_up_opportunity_cost_nav_pct"] == 0.0035
    assert decision["fields"]["call_gap_up_opportunity_cost_to_premium"] == 2.5


def test_shared_short_vol_assessment_rejects_covered_call_gap_up_nav_budget() -> None:
    from domain.domain.short_vol_assessment import (
        ShortVolAssessmentConfig,
        ShortVolPortfolioContext,
        assess_short_vol_candidate,
    )

    decision = assess_short_vol_candidate(
        {
            "symbol": "NVDA",
            "implied_volatility": 0.36,
            "realized_volatility_estimate": 0.24,
            "delta": 0.20,
            "spot": 120.0,
            "strike": 100.0,
            "dte": 30,
            "net_income_cny": 1_400.0,
            "option_contract_point_value_cny": 700.0,
            "covered_notional_cny": 84_000.0,
            "event_source_status": "ok",
        },
        mode="call",
        cfg=ShortVolAssessmentConfig(max_single_trade_nav_pct=0.10),
        risk_ctx=ShortVolPortfolioContext(
            nav_cny=1_000_000.0,
            stock_value_cny_by_symbol={"NVDA": 84_000.0},
            short_put_assignment_cny_by_symbol={},
            short_put_assignment_total_cny=0.0,
        ),
    )

    assert decision["accepted"] is False
    assert decision["rule"] == "call_gap_up_opportunity_cost_nav_exceeded"
    assert decision["fields"]["call_gap_up_opportunity_cost_nav_pct"] == 0.021


def test_shared_short_vol_assessment_rejects_covered_call_gap_up_premium_budget() -> None:
    from domain.domain.short_vol_assessment import (
        ShortVolAssessmentConfig,
        ShortVolPortfolioContext,
        assess_short_vol_candidate,
    )

    decision = assess_short_vol_candidate(
        {
            "symbol": "NVDA",
            "implied_volatility": 0.36,
            "realized_volatility_estimate": 0.24,
            "delta": 0.20,
            "spot": 120.0,
            "strike": 125.0,
            "dte": 30,
            "net_income_cny": 500.0,
            "option_contract_point_value_cny": 700.0,
            "covered_notional_cny": 84_000.0,
            "event_source_status": "ok",
        },
        mode="call",
        cfg=ShortVolAssessmentConfig(max_single_trade_nav_pct=0.10),
        risk_ctx=ShortVolPortfolioContext(
            nav_cny=1_000_000.0,
            stock_value_cny_by_symbol={"NVDA": 84_000.0},
            short_put_assignment_cny_by_symbol={},
            short_put_assignment_total_cny=0.0,
        ),
    )

    assert decision["accepted"] is False
    assert decision["rule"] == "call_gap_up_opportunity_cost_premium_exceeded"
    assert decision["fields"]["call_gap_up_opportunity_cost_to_premium"] == 8.8
