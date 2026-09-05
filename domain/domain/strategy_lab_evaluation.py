from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from decimal import Decimal
from numbers import Real
from typing import Any

from domain.domain.decision_state_fingerprint import canonical_sha256


def calculate_csp_economics(
    opening_net: Decimal,
    strike: Decimal,
    multiplier: Decimal,
    underlying_close: Decimal,
    opening_fx: Decimal,
    terminal_fx: Decimal,
    terminal_fee: Decimal,
    holding_days: int,
) -> dict[str, Decimal]:
    values = (
        opening_net,
        strike,
        multiplier,
        underlying_close,
        opening_fx,
        terminal_fx,
        terminal_fee,
    )
    if (
        any(not isinstance(value, Decimal) or not value.is_finite() for value in values)
        or any(value <= 0 for value in values[:-1])
        or terminal_fee < 0
        or type(holding_days) is not int
        or holding_days <= 0
    ):
        raise ValueError("CSP economics inputs are invalid")
    intrinsic = max(strike - underlying_close, Decimal("0")) * multiplier
    capital_cny = (strike * multiplier - opening_net) * opening_fx
    if capital_cny <= 0:
        raise ValueError("CSP return capital basis is invalid")
    pnl_cny = opening_net * opening_fx - (intrinsic + terminal_fee) * terminal_fx
    annualized = pnl_cny / capital_cny * Decimal(365) / Decimal(holding_days)
    return {
        "economic_pnl_cny": pnl_cny,
        "annualized_return": annualized,
        "return_capital_basis_cny": capital_cny,
        "terminal_intrinsic_loss": intrinsic,
    }


def _insufficient(reason_code: str) -> dict[str, Any]:
    return {
        "status": "insufficient_evidence",
        "reason_code": reason_code,
        "passed": False,
        "daily_aggregates": [],
    }


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, Real):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


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
        if not isinstance(point_id, str) or not point_id or point_id in indexed:
            return None, "comparison_duplicate_point"
        if (
            point_id not in expected_days
            or value.get("trading_day") != expected_days[point_id]
            or value.get("arm") != side
            or value.get("status") not in {"available", "no_fill"}
        ):
            return None, "comparison_point_identity_mismatch"
        indexed[point_id] = value
    return indexed, None


def compare_single_recommendations(
    expected_points: object,
    baseline_projections: object,
    challenger_projections: object,
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
        baseline_projections,
        expected_days=expected_days,
        side="baseline",
    )
    if reason is not None or baseline is None:
        return _insufficient(reason or "comparison_result_invalid")
    challenger, reason = _point_index(
        challenger_projections,
        expected_days=expected_days,
        side="challenger",
    )
    if reason is not None or challenger is None:
        return _insufficient(reason or "comparison_result_invalid")
    if set(baseline) != set(expected_days) or set(challenger) != set(expected_days):
        return _insufficient("comparison_point_missing")

    variants = {value.get("variant_id") for value in challenger.values()}
    if len(variants) != 1:
        return _insufficient("comparison_variant_mismatch")
    variant_id = next(iter(variants))
    if not isinstance(variant_id, str) or not variant_id:
        return _insufficient("comparison_variant_mismatch")

    point_deltas: dict[str, tuple[float, float, bool]] = {}
    for point_id in expected_days:
        left = baseline[point_id]
        right = challenger[point_id]
        baseline_return = _number(left.get("annualized_return"))
        challenger_return = _number(right.get("annualized_return"))
        baseline_pnl = _number(left.get("economic_pnl_cny"))
        challenger_pnl = _number(right.get("economic_pnl_cny"))
        left_candidate = left.get("candidate_identity")
        right_candidate = right.get("candidate_identity")
        if (
            None in {baseline_return, challenger_return, baseline_pnl, challenger_pnl}
            or not isinstance(left_candidate, str)
            or not left_candidate
            or not isinstance(right_candidate, str)
            or not right_candidate
        ):
            return _insufficient("comparison_result_invalid")
        point_deltas[point_id] = (
            challenger_return - baseline_return,
            challenger_pnl - baseline_pnl,
            left_candidate != right_candidate,
        )

    daily: list[dict[str, Any]] = []
    for trading_day in sorted(day_points):
        values = [point_deltas[point_id] for point_id in day_points[trading_day]]
        daily.append(
            {
                "trading_day": trading_day,
                "expected_point_count": len(values),
                "effective_point_count": len(values),
                "top1_change_count": sum(item[2] for item in values),
                "mean_annualized_return_delta": math.fsum(item[0] for item in values) / len(values),
                "mean_pnl_delta_cny": math.fsum(item[1] for item in values) / len(values),
            }
        )
    annualized_delta = math.fsum(item["mean_annualized_return_delta"] for item in daily) / len(daily)
    pnl_delta = math.fsum(item["mean_pnl_delta_cny"] for item in daily) / len(daily)
    return {
        "status": "complete",
        "reason_code": None,
        "variant_id": variant_id,
        "expected_point_count": len(expected_days),
        "effective_point_count": len(expected_days),
        "top1_change_count": sum(item[2] for item in point_deltas.values()),
        "daily_aggregates": daily,
        "mean_daily_annualized_return_delta": annualized_delta,
        "mean_daily_pnl_delta_cny": pnl_delta,
        "passed": annualized_delta > 0 and pnl_delta >= 0,
    }


def select_research_leader(
    variant_comparisons: object,
    variant_preference: object,
) -> dict[str, Any]:
    if (
        not isinstance(variant_comparisons, Sequence)
        or isinstance(variant_comparisons, (str, bytes))
        or not isinstance(variant_preference, Sequence)
        or isinstance(variant_preference, (str, bytes))
    ):
        return _leader_insufficient("variant_comparison_invalid")
    preference = list(variant_preference)
    if (
        not preference
        or any(not isinstance(value, str) or not value for value in preference)
        or len(preference) != len(set(preference))
    ):
        return _leader_insufficient("variant_preference_invalid")
    preference_index = {variant: index for index, variant in enumerate(preference)}
    comparisons: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    for value in variant_comparisons:
        if not isinstance(value, Mapping) or value.get("status") != "complete":
            return _leader_insufficient("variant_comparison_insufficient")
        variant = value.get("variant_id")
        annualized = _number(value.get("mean_daily_annualized_return_delta"))
        pnl = _number(value.get("mean_daily_pnl_delta_cny"))
        if (
            not isinstance(variant, str)
            or variant not in preference_index
            or variant in seen
            or annualized is None
            or pnl is None
            or not isinstance(value.get("passed"), bool)
            or value["passed"] != (annualized > 0 and pnl >= 0)
            or type(value.get("expected_point_count")) is not int
            or value["expected_point_count"] <= 0
            or type(value.get("effective_point_count")) is not int
            or value["effective_point_count"] != value["expected_point_count"]
            or type(value.get("top1_change_count")) is not int
            or not 0 <= value["top1_change_count"] <= value["effective_point_count"]
        ):
            return _leader_insufficient("variant_comparison_invalid")
        seen.add(variant)
        comparisons.append(value)
    if not comparisons:
        return _leader_insufficient("variant_comparison_invalid")
    passing = [value for value in comparisons if value["passed"]]
    passing.sort(
        key=lambda value: (
            -float(value["mean_daily_annualized_return_delta"]),
            -float(value["mean_daily_pnl_delta_cny"]),
            preference_index[str(value["variant_id"])],
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
    return {
        "status": "leader",
        "reason_code": None,
        "leader": {
            "variant_id": winner["variant_id"],
            "mean_daily_annualized_return_delta": winner["mean_daily_annualized_return_delta"],
            "mean_daily_pnl_delta_cny": winner["mean_daily_pnl_delta_cny"],
            "expected_point_count": winner["expected_point_count"],
            "effective_point_count": winner["effective_point_count"],
            "top1_change_count": winner["top1_change_count"],
            "comparison_sha256": canonical_sha256(winner),
        },
        "passing_variant_ids": [str(value["variant_id"]) for value in passing],
    }


def _leader_insufficient(reason_code: str) -> dict[str, Any]:
    return {
        "status": "insufficient_evidence",
        "reason_code": reason_code,
        "leader": None,
        "passing_variant_ids": [],
    }


__all__ = [
    "calculate_csp_economics",
    "compare_single_recommendations",
    "select_research_leader",
]
