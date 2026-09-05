from __future__ import annotations

from decimal import Decimal

import pytest

from domain.domain.strategy_lab_evaluation import (
    calculate_csp_economics,
    compare_single_recommendations,
    select_research_leader,
)
from src.application.strategy_lab.evidence import (
    StrategyLabEvidenceError,
    build_comparison_projection,
)


PREFERENCE = ("threshold_0.002", "threshold_0.004", "threshold_0.006")


def _result(
    point_id: str,
    trading_day: str,
    *,
    arm: str,
    annualized: float,
    pnl: float,
    candidate: str,
    variant: str = "threshold_0.002",
    fill_status: str = "simulated_fill",
) -> dict[str, object]:
    return {
        "recommendation_point_id": point_id,
        "trading_day": trading_day,
        "arm": arm,
        "variant_id": None if arm == "baseline" else variant,
        "candidate_identity": candidate,
        "status": "no_fill" if fill_status == "no_fill" else "available",
        "annualized_return": annualized,
        "economic_pnl_cny": pnl,
    }


def _comparison(
    *,
    annualized_delta: float = 0.01,
    pnl_delta: float = 1.0,
    variant: str = "threshold_0.002",
) -> dict[str, object]:
    expected = [{"recommendation_point_id": "point-1", "trading_day": "2026-08-01"}]
    baseline = [
        _result(
            "point-1",
            "2026-08-01",
            arm="baseline",
            annualized=0.1,
            pnl=10.0,
            candidate="baseline",
        )
    ]
    challenger = [
        _result(
            "point-1",
            "2026-08-01",
            arm="challenger",
            annualized=0.1 + annualized_delta,
            pnl=10.0 + pnl_delta,
            candidate="challenger",
            variant=variant,
        )
    ]
    return compare_single_recommendations(expected, baseline, challenger)


def test_comparison_strictly_pairs_then_equally_weights_daily_means() -> None:
    expected = [
        {"recommendation_point_id": "a", "trading_day": "2026-08-01"},
        {"recommendation_point_id": "b", "trading_day": "2026-08-01"},
        {"recommendation_point_id": "c", "trading_day": "2026-08-02"},
    ]
    baseline = [
        _result(point, day, arm="baseline", annualized=0.1, pnl=10, candidate=point)
        for point, day in (("a", "2026-08-01"), ("b", "2026-08-01"), ("c", "2026-08-02"))
    ]
    challenger = [
        _result("a", "2026-08-01", arm="challenger", annualized=0.2, pnl=12, candidate="x"),
        _result("b", "2026-08-01", arm="challenger", annualized=0.2, pnl=12, candidate="b"),
        _result("c", "2026-08-02", arm="challenger", annualized=0.4, pnl=16, candidate="y"),
    ]

    result = compare_single_recommendations(expected, baseline, challenger)

    assert result["status"] == "complete"
    assert result["mean_daily_annualized_return_delta"] == 0.2
    assert result["mean_daily_pnl_delta_cny"] == 4.0
    assert result["top1_change_count"] == 2
    assert result["passed"] is True
    assert [item["expected_point_count"] for item in result["daily_aggregates"]] == [2, 1]


def test_complete_twenty_day_window_produces_one_provisional_leader() -> None:
    expected = [
        {
            "recommendation_point_id": f"point-{day:02d}",
            "trading_day": f"2026-08-{day:02d}",
        }
        for day in range(1, 21)
    ]
    baseline = [
        _result(
            point["recommendation_point_id"],
            point["trading_day"],
            arm="baseline",
            annualized=0.1,
            pnl=10,
            candidate="baseline",
        )
        for point in expected
    ]
    challenger = [
        _result(
            point["recommendation_point_id"],
            point["trading_day"],
            arm="challenger",
            annualized=0.11,
            pnl=11,
            candidate="challenger",
        )
        for point in expected
    ]

    comparison = compare_single_recommendations(expected, baseline, challenger)
    selected = select_research_leader([comparison], PREFERENCE)

    assert comparison["effective_point_count"] == 20
    assert len(comparison["daily_aggregates"]) == 20
    assert selected["status"] == "leader"


def test_comparison_passes_only_on_positive_return_and_nonnegative_pnl() -> None:
    assert _comparison(annualized_delta=0.01, pnl_delta=0)["passed"] is True
    assert _comparison(annualized_delta=0, pnl_delta=1)["passed"] is False
    assert _comparison(annualized_delta=0.01, pnl_delta=-1)["passed"] is False
    same = _comparison(annualized_delta=0, pnl_delta=0)
    assert same["top1_change_count"] == 1
    assert same["passed"] is False

    expected = [{"recommendation_point_id": "point-1", "trading_day": "2026-08-01"}]
    baseline = _result(
        "point-1",
        "2026-08-01",
        arm="baseline",
        annualized=0,
        pnl=0,
        candidate="same",
        fill_status="no_fill",
    )
    challenger = _result(
        "point-1",
        "2026-08-01",
        arm="challenger",
        annualized=0,
        pnl=0,
        candidate="same",
        fill_status="no_fill",
    )
    no_fill = compare_single_recommendations(expected, [baseline], [challenger])
    assert no_fill["status"] == "complete"
    assert no_fill["passed"] is False


def test_missing_duplicate_or_not_evaluable_point_is_insufficient() -> None:
    expected = [{"recommendation_point_id": "point-1", "trading_day": "2026-08-01"}]
    baseline = [
        _result(
            "point-1",
            "2026-08-01",
            arm="baseline",
            annualized=0.1,
            pnl=10,
            candidate="a",
        )
    ]
    challenger = [
        _result(
            "point-1",
            "2026-08-01",
            arm="challenger",
            annualized=0.2,
            pnl=11,
            candidate="b",
        )
    ]

    assert compare_single_recommendations(expected, baseline, [])["reason_code"] == "comparison_point_missing"
    assert (
        compare_single_recommendations(expected, baseline + baseline, challenger)["reason_code"]
        == "comparison_duplicate_point"
    )
    challenger[0]["status"] = "not_evaluable"
    assert (
        compare_single_recommendations(expected, baseline, challenger)["reason_code"]
        == "comparison_point_identity_mismatch"
    )


def test_leader_order_is_return_then_pnl_then_threshold() -> None:
    lower_return = _comparison(
        annualized_delta=0.01,
        pnl_delta=100,
        variant="threshold_0.002",
    )
    higher_return = _comparison(
        annualized_delta=0.02,
        pnl_delta=1,
        variant="threshold_0.006",
    )
    tied_return_higher_pnl = _comparison(
        annualized_delta=0.02,
        pnl_delta=2,
        variant="threshold_0.004",
    )

    selected = select_research_leader(
        [lower_return, higher_return, tied_return_higher_pnl],
        PREFERENCE,
    )

    assert selected["status"] == "leader"
    assert selected["leader"]["variant_id"] == "threshold_0.004"
    assert "observations" not in selected["leader"]

    tie_low_threshold = dict(tied_return_higher_pnl)
    tie_low_threshold["variant_id"] = "threshold_0.002_tie"
    selected = select_research_leader(
        [tied_return_higher_pnl, tie_low_threshold],
        ("threshold_0.002_tie", "threshold_0.004"),
    )
    assert selected["leader"]["variant_id"] == "threshold_0.002_tie"


def test_no_leader_and_insufficient_are_deterministic() -> None:
    no_leader = select_research_leader(
        [_comparison(annualized_delta=0, pnl_delta=0)],
        PREFERENCE,
    )
    assert no_leader == {
        "status": "no_leader",
        "reason_code": "no_challenger_passed",
        "leader": None,
        "passing_variant_ids": [],
    }
    insufficient = select_research_leader(
        [{"status": "insufficient_evidence", "reason_code": "missing"}],
        PREFERENCE,
    )
    assert insufficient["status"] == "insufficient_evidence"
    assert insufficient["leader"] is None


def test_non_concentration_variant_uses_the_same_comparator() -> None:
    comparison = _comparison(variant="dte_30_45")
    selected = select_research_leader([comparison], ("dte_30_45",))

    assert comparison["variant_id"] == "dte_30_45"
    assert selected["leader"]["variant_id"] == "dte_30_45"


def test_application_projection_rejects_non_evaluable_result() -> None:
    with pytest.raises(StrategyLabEvidenceError) as raised:
        build_comparison_projection(
            {
                "recommendation_point_id": "point-1",
                "trading_day": "2026-08-01",
                "arm": "challenger",
                "variant_id": "dte_30_45",
                "candidate_ref": "candidate-1",
                "fill_status": "not_evaluable",
                "outcome_status": "not_evaluable",
                "safety_status": "pass",
                "annualized_return": None,
                "economic_pnl_cny": None,
            }
        )
    assert raised.value.reason_code == "comparison_result_not_evaluable"


def test_csp_economics_uses_cny_cash_basis_and_intrinsic_loss() -> None:
    result = calculate_csp_economics(
        Decimal("200"),
        Decimal("100"),
        Decimal("100"),
        Decimal("90"),
        Decimal("0.92"),
        Decimal("0.93"),
        Decimal("10"),
        30,
    )

    assert result["terminal_intrinsic_loss"] == Decimal("1000")
    assert result["return_capital_basis_cny"] == Decimal("9016.00")
    assert result["economic_pnl_cny"] == Decimal("-755.30")
