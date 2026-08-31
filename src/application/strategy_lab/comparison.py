from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from numbers import Real
from typing import Any

from src.application.strategy_lab.contracts import NEAR_RETURN_THRESHOLDS, canonical_sha256


def _insufficient(reason_code: str) -> dict[str, Any]:
    return {
        "status": "insufficient_evidence",
        "reason_code": reason_code,
        "passed": False,
        "daily_aggregates": [],
    }


def _point_index(
    values: object,
    *,
    expected_days: Mapping[str, str],
    side: str,
) -> tuple[dict[str, Mapping[str, Any]] | None, str | None]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        return None, "comparison_result_invalid"
    indexed: dict[str, Mapping[str, Any]] = {}
    for value in values:
        if not isinstance(value, Mapping):
            return None, "comparison_result_invalid"
        point_id = value.get("recommendation_point_id")
        trading_day = value.get("trading_day")
        if not isinstance(point_id, str) or not point_id or point_id in indexed:
            return None, "comparison_duplicate_point"
        if point_id not in expected_days:
            return None, "comparison_unexpected_point"
        if trading_day != expected_days[point_id]:
            return None, "comparison_point_identity_mismatch"
        if value.get("arm") != side:
            return None, "comparison_point_identity_mismatch"
        indexed[point_id] = value
    return indexed, None


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, Real):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _candidate_identity(value: Mapping[str, Any]) -> str | None:
    candidate = value.get("candidate_ref")
    if isinstance(candidate, str) and candidate:
        return candidate
    if isinstance(candidate, Mapping) and candidate:
        return canonical_sha256(candidate)
    candidate_id = value.get("candidate_id")
    return candidate_id if isinstance(candidate_id, str) and candidate_id else None


def _variant_identity(
    challenger_results: Mapping[str, Mapping[str, Any]],
) -> tuple[str | None, float | None]:
    variants = [value.get("variant_id") for value in challenger_results.values()]
    thresholds = [value.get("near_return_threshold") for value in challenger_results.values()]
    if (
        not variants
        or any(value != variants[0] for value in variants[1:])
        or any(value != thresholds[0] for value in thresholds[1:])
    ):
        return None, None
    variant = variants[0]
    threshold = _number(thresholds[0])
    if (
        not isinstance(variant, str)
        or not variant
        or threshold not in NEAR_RETURN_THRESHOLDS
    ):
        return None, None
    return variant, threshold


def compare_single_recommendations(
    expected_points: object,
    baseline_results: object,
    challenger_results: object,
) -> dict[str, Any]:
    if not isinstance(expected_points, Sequence) or isinstance(expected_points, (str, bytes)):
        return _insufficient("comparison_expected_points_invalid")
    expected_days: dict[str, str] = {}
    day_points: dict[str, list[str]] = defaultdict(list)
    for point in expected_points:
        if not isinstance(point, Mapping):
            return _insufficient("comparison_expected_points_invalid")
        point_id = point.get("recommendation_point_id")
        trading_day = point.get("trading_day")
        if (
            not isinstance(point_id, str)
            or not point_id
            or point_id in expected_days
            or not isinstance(trading_day, str)
            or not trading_day
        ):
            return _insufficient("comparison_expected_points_invalid")
        expected_days[point_id] = trading_day
        day_points[trading_day].append(point_id)
    if not expected_days:
        return _insufficient("comparison_expected_points_invalid")

    baseline, reason = _point_index(
        baseline_results, expected_days=expected_days, side="baseline"
    )
    if reason is not None or baseline is None:
        return _insufficient(reason or "comparison_result_invalid")
    challenger, reason = _point_index(
        challenger_results, expected_days=expected_days, side="challenger"
    )
    if reason is not None or challenger is None:
        return _insufficient(reason or "comparison_result_invalid")
    if set(baseline) != set(expected_days) or set(challenger) != set(expected_days):
        return _insufficient("comparison_point_missing")

    variant_id, threshold = _variant_identity(challenger)
    if variant_id is None or threshold is None:
        return _insufficient("comparison_variant_mismatch")

    point_deltas: dict[str, tuple[float, float, bool]] = {}
    for point_id in expected_days:
        left = baseline[point_id]
        right = challenger[point_id]
        left_fill = left.get("fill_status")
        right_fill = right.get("fill_status")
        if (
            left_fill == "not_evaluable"
            or right_fill == "not_evaluable"
            or left.get("outcome_status") in {"pending_outcome", "not_evaluable"}
            or right.get("outcome_status") in {"pending_outcome", "not_evaluable"}
            or left.get("safety_status") != "pass"
            or right.get("safety_status") != "pass"
        ):
            return _insufficient("comparison_result_not_evaluable")
        if (
            left_fill not in {"simulated_fill", "observed_fill", "no_fill"}
            or right_fill not in {"simulated_fill", "observed_fill", "no_fill"}
            or (left_fill == "no_fill")
            != (left.get("outcome_status") == "not_applicable")
            or (right_fill == "no_fill")
            != (right.get("outcome_status") == "not_applicable")
            or (left_fill != "no_fill" and left.get("outcome_status") != "available")
            or (right_fill != "no_fill" and right.get("outcome_status") != "available")
        ):
            return _insufficient("comparison_result_invalid")
        baseline_return = _number(left.get("annualized_return"))
        challenger_return = _number(right.get("annualized_return"))
        baseline_pnl = _number(left.get("economic_pnl_cny"))
        challenger_pnl = _number(right.get("economic_pnl_cny"))
        left_candidate = _candidate_identity(left)
        right_candidate = _candidate_identity(right)
        if None in {baseline_return, challenger_return, baseline_pnl, challenger_pnl}:
            return _insufficient("comparison_result_invalid")
        if left_candidate is None or right_candidate is None:
            return _insufficient("comparison_result_invalid")
        point_deltas[point_id] = (
            challenger_return - baseline_return,
            challenger_pnl - baseline_pnl,
            left_candidate != right_candidate,
        )

    daily: list[dict[str, Any]] = []
    for trading_day in sorted(day_points):
        values = [
            point_deltas[point_id] for point_id in sorted(day_points[trading_day])
        ]
        daily.append(
            {
                "trading_day": trading_day,
                "expected_point_count": len(values),
                "effective_point_count": len(values),
                "top1_change_count": sum(item[2] for item in values),
                "mean_annualized_return_delta": math.fsum(item[0] for item in values)
                / len(values),
                "mean_pnl_delta_cny": math.fsum(item[1] for item in values) / len(values),
            }
        )
    annualized_delta = math.fsum(
        item["mean_annualized_return_delta"] for item in daily
    ) / len(daily)
    pnl_delta = math.fsum(item["mean_pnl_delta_cny"] for item in daily) / len(daily)
    return {
        "status": "complete",
        "reason_code": None,
        "variant_id": variant_id,
        "near_return_threshold": threshold,
        "expected_point_count": len(expected_days),
        "effective_point_count": len(expected_days),
        "top1_change_count": sum(item[2] for item in point_deltas.values()),
        "daily_aggregates": daily,
        "mean_daily_annualized_return_delta": annualized_delta,
        "mean_daily_pnl_delta_cny": pnl_delta,
        "passed": annualized_delta > 0 and pnl_delta >= 0,
    }


def select_research_leader(variant_comparisons: object) -> dict[str, Any]:
    if not isinstance(variant_comparisons, Sequence) or isinstance(
        variant_comparisons, (str, bytes)
    ):
        return {
            "status": "insufficient_evidence",
            "reason_code": "variant_comparison_invalid",
            "leader": None,
            "passing_variant_ids": [],
        }
    comparisons: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    seen_thresholds: set[float] = set()
    for value in variant_comparisons:
        if not isinstance(value, Mapping) or value.get("status") != "complete":
            return {
                "status": "insufficient_evidence",
                "reason_code": "variant_comparison_insufficient",
                "leader": None,
                "passing_variant_ids": [],
            }
        variant = value.get("variant_id")
        threshold = _number(value.get("near_return_threshold"))
        annualized = _number(value.get("mean_daily_annualized_return_delta"))
        pnl = _number(value.get("mean_daily_pnl_delta_cny"))
        if (
            not isinstance(variant, str)
            or not variant
            or variant in seen
            or threshold in seen_thresholds
            or threshold is None
            or annualized is None
            or pnl is None
            or not isinstance(value.get("passed"), bool)
            or threshold not in NEAR_RETURN_THRESHOLDS
            or value["passed"] != (annualized > 0 and pnl >= 0)
            or type(value.get("expected_point_count")) is not int
            or value["expected_point_count"] <= 0
            or type(value.get("effective_point_count")) is not int
            or value["effective_point_count"] != value["expected_point_count"]
            or type(value.get("top1_change_count")) is not int
            or not 0 <= value["top1_change_count"] <= value["effective_point_count"]
        ):
            return {
                "status": "insufficient_evidence",
                "reason_code": "variant_comparison_invalid",
                "leader": None,
                "passing_variant_ids": [],
            }
        seen.add(variant)
        seen_thresholds.add(threshold)
        comparisons.append(value)
    if not comparisons:
        return {
            "status": "insufficient_evidence",
            "reason_code": "variant_comparison_invalid",
            "leader": None,
            "passing_variant_ids": [],
        }
    passing = [value for value in comparisons if value["passed"]]
    passing.sort(
        key=lambda value: (
            -float(value["mean_daily_annualized_return_delta"]),
            -float(value["mean_daily_pnl_delta_cny"]),
            float(value["near_return_threshold"]),
        )
    )
    if not passing:
        return {
            "status": "no_leader",
            "reason_code": "no_challenger_passed",
            "leader": None,
            "passing_variant_ids": [],
        }
    winner = passing[0]
    leader = {
        "variant_id": winner["variant_id"],
        "near_return_threshold": winner["near_return_threshold"],
        "mean_daily_annualized_return_delta": winner[
            "mean_daily_annualized_return_delta"
        ],
        "mean_daily_pnl_delta_cny": winner["mean_daily_pnl_delta_cny"],
        "expected_point_count": winner["expected_point_count"],
        "effective_point_count": winner["effective_point_count"],
        "top1_change_count": winner["top1_change_count"],
        "comparison_sha256": canonical_sha256(winner),
    }
    return {
        "status": "leader",
        "reason_code": None,
        "leader": leader,
        "passing_variant_ids": [str(value["variant_id"]) for value in passing],
    }


__all__ = ["compare_single_recommendations", "select_research_leader"]
