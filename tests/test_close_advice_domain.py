from __future__ import annotations

import math

from domain.domain.close_advice import (
    CloseAdviceConfig,
    CloseAdviceInput,
    EXIT_STATE_HOLD,
    EXIT_STATE_NOT_EVALUABLE,
    EXIT_STATE_PROFIT_CAPTURE,
    EXIT_STATE_SALVAGE,
    EXIT_STATE_TAKE_PROFIT,
    RECOMMENDATION_CLOSE,
    RECOMMENDATION_HOLD,
    RECOMMENDATION_NOT_EVALUABLE,
    apply_fee_economic_safety,
    current_policy_decision_fields,
    evaluate_close_advice,
    select_close_advice_notification_rows,
)


def _inp(*, premium: float = 1.0, mid: float = 0.1, dte: int = 10, option_type: str = "put") -> CloseAdviceInput:
    return CloseAdviceInput(
        account="lx",
        symbol="NVDA",
        option_type=option_type,
        side="short",
        expiration="2026-05-15",
        strike=100.0,
        contracts_open=1,
        premium=premium,
        close_mid=mid,
        bid=max(0.01, mid - 0.01),
        ask=mid + 0.01,
        dte=dte,
        multiplier=100,
        spot=120.0,
        currency="USD",
    )


def test_close_advice_strong_uses_dynamic_dte_thresholds() -> None:
    cfg = CloseAdviceConfig()

    assert evaluate_close_advice(_inp(premium=1.0, mid=0.10, dte=10), cfg)["tier"] == "strong"
    assert evaluate_close_advice(_inp(premium=1.0, mid=0.15, dte=20), cfg)["tier"] == "strong"
    assert evaluate_close_advice(_inp(premium=1.0, mid=0.20, dte=40), cfg)["tier"] == "strong"


def test_close_advice_medium_weak_optional_and_none() -> None:
    cfg = CloseAdviceConfig()

    assert evaluate_close_advice(_inp(premium=1.0, mid=0.27, dte=25), cfg)["tier"] == "medium"
    assert evaluate_close_advice(_inp(premium=1.0, mid=0.45, dte=40), cfg)["tier"] == "weak"
    assert evaluate_close_advice(
        CloseAdviceInput(
            account="lx",
            symbol="NVDA",
            option_type="put",
            side="short",
            expiration="2026-05-15",
            strike=100,
            contracts_open=1,
            premium=1.0,
            close_mid=0.05,
            bid=0.045,
            ask=0.055,
            dte=3,
            multiplier=100,
            currency="USD",
        ),
        cfg,
    )["tier"] == "optional"
    assert evaluate_close_advice(_inp(premium=1.0, mid=0.60, dte=20), cfg)["tier"] == "none"


def test_close_advice_metrics_for_put_and_call() -> None:
    put = evaluate_close_advice(_inp(premium=2.0, mid=0.4, dte=20), CloseAdviceConfig())
    assert round(put["capture_ratio"], 6) == 0.8
    assert round(put["remaining_premium"], 6) == 40.0
    assert round(put["realized_if_close"], 6) == 160.0
    assert round(put["estimated_pnl_if_close_gross"], 6) == 160.0
    assert put["estimated_close_fee"] is None
    assert put["estimated_pnl_if_close_net"] is None
    assert round(put["remaining_annualized_return"], 6) == 0.073

    call = evaluate_close_advice(_inp(premium=2.0, mid=0.4, dte=20, option_type="call"), CloseAdviceConfig())
    assert round(call["remaining_annualized_return"], 6) == 0.060833


def test_current_policy_decision_contract_preserves_p0_action_semantics() -> None:
    strong = current_policy_decision_fields(tier="strong", exit_state=EXIT_STATE_PROFIT_CAPTURE)
    medium = current_policy_decision_fields(tier="medium", exit_state=EXIT_STATE_PROFIT_CAPTURE)
    hold = current_policy_decision_fields(tier="none", exit_state=EXIT_STATE_HOLD)
    not_evaluable = current_policy_decision_fields(
        tier="not_evaluable",
        exit_state=EXIT_STATE_NOT_EVALUABLE,
    )

    assert strong["recommendation_state"] == RECOMMENDATION_CLOSE
    assert medium["recommendation_state"] == RECOMMENDATION_CLOSE
    assert hold["recommendation_state"] == RECOMMENDATION_HOLD
    assert not_evaluable["recommendation_state"] == RECOMMENDATION_NOT_EVALUABLE
    assert strong["decision_basis"] == ("profit_capture_strong",)
    assert not_evaluable["decision_evidence_status"] == "not_evaluable"


def test_close_advice_data_quality_blocks_notifications() -> None:
    invalid_premium = evaluate_close_advice(_inp(premium=0.0), CloseAdviceConfig())
    assert invalid_premium["tier"] == "not_evaluable"
    assert invalid_premium["exit_state"] == EXIT_STATE_NOT_EVALUABLE
    assert "invalid_premium" in invalid_premium["data_quality_flags"]

    no_quote = evaluate_close_advice(_inp(mid=0.0), CloseAdviceConfig())
    assert no_quote["tier"] == "not_evaluable"
    assert no_quote["exit_state"] == EXIT_STATE_NOT_EVALUABLE
    assert "invalid_mid" in no_quote["data_quality_flags"]

    wide = evaluate_close_advice(
        CloseAdviceInput(
            account="lx",
            symbol="NVDA",
            option_type="put",
            side="short",
            expiration="2026-05-15",
            strike=100,
            contracts_open=1,
            premium=1.0,
            close_mid=0.5,
            bid=0.1,
            ask=0.9,
            dte=30,
            multiplier=100,
            currency="USD",
        ),
        CloseAdviceConfig(max_spread_ratio=0.3),
    )
    assert wide["tier"] == "not_evaluable"
    assert wide["exit_state"] == EXIT_STATE_NOT_EVALUABLE
    assert "spread_too_wide" in wide["data_quality_flags"]

    unsupported = evaluate_close_advice(
        CloseAdviceInput(
            account="lx",
            symbol="NVDA",
            option_type="put",
            side="long",
            expiration="2026-05-15",
            strike=100,
            contracts_open=1,
            premium=1.0,
            close_mid=0.1,
            dte=30,
            multiplier=100,
            currency="USD",
        )
    )
    assert unsupported["tier"] == "not_evaluable"
    assert unsupported["exit_state"] == EXIT_STATE_NOT_EVALUABLE
    assert "unsupported_position" in unsupported["data_quality_flags"]


def test_close_advice_mid_above_premium_is_not_profit_advice() -> None:
    row = evaluate_close_advice(_inp(premium=1.0, mid=1.1, dte=30), CloseAdviceConfig())
    assert row["tier"] == "none"
    assert row["exit_state"] == EXIT_STATE_HOLD
    assert "not_profitable_to_close" in row["data_quality_flags"]


def test_close_advice_nan_quote_is_treated_as_missing_data() -> None:
    row = evaluate_close_advice(_inp(mid=math.nan), CloseAdviceConfig())
    assert row["tier"] == "not_evaluable"
    assert row["exit_state"] == EXIT_STATE_NOT_EVALUABLE
    assert "missing_mid" in row["data_quality_flags"]


def test_fee_safety_keeps_non_actionable_hold_unchanged_without_fee_evidence() -> None:
    row = {"tier": "none", "exit_state": EXIT_STATE_HOLD, "fee_calc_status": "unavailable"}

    assert apply_fee_economic_safety(row) == row


def test_fee_safety_fails_closed_for_actionable_row_without_usable_fee() -> None:
    row = apply_fee_economic_safety(
        {
            "tier": "strong",
            "exit_state": EXIT_STATE_PROFIT_CAPTURE,
            "fee_calc_status": "unsupported_currency",
            "estimated_pnl_if_close_net": None,
        }
    )

    assert row["tier"] == "not_evaluable"
    assert row["exit_state"] == EXIT_STATE_NOT_EVALUABLE
    assert row["evaluation_status"] == "not_evaluable"
    assert "fee_evidence_unavailable" in row["data_quality_flags"]


def test_fee_safety_requires_positive_net_lifetime_pnl_for_profit_actions() -> None:
    for exit_state in (EXIT_STATE_PROFIT_CAPTURE, EXIT_STATE_TAKE_PROFIT):
        blocked = apply_fee_economic_safety(
            {
                "tier": "medium",
                "exit_state": exit_state,
                "fee_calc_status": "schedule_estimate",
                "estimated_pnl_if_close_net": 0.0,
            }
        )
        allowed = apply_fee_economic_safety(
            {
                "tier": "medium",
                "exit_state": exit_state,
                "fee_calc_status": "schedule_estimate",
                "estimated_pnl_if_close_net": 0.01,
            }
        )

        assert blocked["tier"] == "none"
        assert blocked["exit_state"] == EXIT_STATE_HOLD
        assert "not_profitable_after_fee" in blocked["data_quality_flags"]
        assert allowed["tier"] == "medium"
        assert allowed["exit_state"] == exit_state


def test_fee_safety_uses_net_proceeds_for_long_salvage() -> None:
    allowed = apply_fee_economic_safety(
        {
            "tier": "optional",
            "exit_state": EXIT_STATE_SALVAGE,
            "fee_calc_status": "conservative_estimate",
            "estimated_pnl_if_close_net": -95.0,
            "net_close_proceeds": 5.0,
        }
    )
    blocked = apply_fee_economic_safety(
        {
            "tier": "optional",
            "exit_state": EXIT_STATE_SALVAGE,
            "fee_calc_status": "conservative_estimate",
            "estimated_pnl_if_close_net": -100.0,
            "net_close_proceeds": 0.0,
        }
    )

    assert allowed["exit_state"] == EXIT_STATE_SALVAGE
    assert blocked["tier"] == "none"
    assert blocked["exit_state"] == EXIT_STATE_HOLD
    assert "non_positive_net_close_proceeds" in blocked["data_quality_flags"]


def test_notification_selection_applies_pricing_ranking_and_account_limit() -> None:
    rows = [
        {
            "account": "lx",
            "symbol": "LX_MEDIUM",
            "tier": "medium",
            "capture_ratio": 0.90,
            "evaluation_status": "priced",
        },
        {
            "account": "lx",
            "symbol": "LX_SECOND",
            "tier": "strong",
            "capture_ratio": 0.80,
            "evaluation_status": "priced",
        },
        {
            "account": "LX",
            "symbol": "LX_FIRST",
            "tier": "strong",
            "capture_ratio": 0.95,
            "evaluation_status": "priced",
        },
        {
            "account": "sy",
            "symbol": "SY_FIRST",
            "tier": "strong",
            "capture_ratio": 0.70,
            "evaluation_status": "priced",
        },
        {
            "account": "sy",
            "symbol": "SY_UNPRICED",
            "tier": "strong",
            "capture_ratio": 0.99,
            "evaluation_status": "not_evaluable",
        },
        {
            "account": "sy",
            "symbol": "SY_OPTIONAL",
            "tier": "optional",
            "capture_ratio": 0.99,
            "evaluation_status": "priced",
        },
    ]

    selected = select_close_advice_notification_rows(
        rows,
        notify_levels={"strong", "medium"},
        max_items_per_account=1,
    )

    assert [row["symbol"] for row in selected] == ["LX_FIRST", "SY_FIRST"]


def test_notification_selection_zero_limit_means_unlimited() -> None:
    rows = [
        {
            "account": "lx",
            "symbol": symbol,
            "tier": tier,
            "capture_ratio": capture_ratio,
            "evaluation_status": "priced",
        }
        for symbol, tier, capture_ratio in (
            ("MEDIUM", "medium", 0.90),
            ("SECOND", "strong", 0.80),
            ("FIRST", "strong", 0.95),
        )
    ]

    selected = select_close_advice_notification_rows(
        rows,
        notify_levels={"strong", "medium"},
        max_items_per_account=0,
    )

    assert [row["symbol"] for row in selected] == [
        "FIRST",
        "SECOND",
        "MEDIUM",
    ]
