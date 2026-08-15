from __future__ import annotations

import math
import statistics as stdlib_statistics
from collections import defaultdict
from collections.abc import Mapping
from datetime import date
from typing import TypedDict, cast


_POLICY_KEYS = frozenset(
    {
        "required_days",
        "confidence_level",
        "worst_fraction",
        "require_concentration_non_increase",
    }
)
_POINT_KEYS = frozenset(
    {
        "recommendation_point_id",
        "trading_date",
        "baseline_candidate_id",
        "challenger_candidate_id",
        "baseline_efficiency",
        "challenger_efficiency",
        "hard_risk_status",
        "baseline_concentration",
        "challenger_concentration",
    }
)
_RISK_STATUSES = frozenset({"passed", "violated", "missing"})


class _Point(TypedDict):
    recommendation_point_id: str
    trading_date: str
    baseline_candidate_id: str | None
    challenger_candidate_id: str | None
    baseline_efficiency: float | None
    challenger_efficiency: float | None
    hard_risk_status: str
    baseline_concentration: float | None
    challenger_concentration: float | None


class _PointResult(TypedDict):
    recommendation_point_id: str
    trading_date: str
    status: str
    point_delta: float | None


class _DailyDelta(TypedDict):
    trading_date: str
    effective_point_count: int
    daily_delta: float


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a mapping")
    raw_mapping = cast(Mapping[object, object], value)
    if not all(isinstance(key, str) for key in raw_mapping):
        raise ValueError(f"{label} keys must be strings")
    return cast(Mapping[str, object], raw_mapping)


def _canonical_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{label} must be non-empty canonical text")
    return value


def _iso_date(value: object, label: str) -> str:
    text = _canonical_text(value, label)
    try:
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{label} must be a canonical ISO date") from exc
    if parsed.isoformat() != text:
        raise ValueError(f"{label} must be a canonical ISO date")
    return text


def _optional_number(value: object, label: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be null or numeric")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{label} must be finite")
    return number


def _optional_candidate(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _canonical_text(value, label)


def _result(
    *,
    decision: str,
    reason_codes: list[str],
    required_days: int,
    effective_days: int,
    point_results: list[_PointResult],
    daily_deltas: list[_DailyDelta],
    mean_daily_delta: float | None = None,
    sample_std: float | None = None,
    standard_error: float | None = None,
    t_critical: float | None = None,
    one_sided_lower_bound: float | None = None,
    worst_k: int | None = None,
    worst_tail_mean: float | None = None,
) -> dict[str, object]:
    return {
        "decision": decision,
        "reason_codes": reason_codes,
        "required_days": required_days,
        "effective_days": effective_days,
        "point_results": point_results,
        "daily_deltas": daily_deltas,
        "mean_daily_delta": mean_daily_delta,
        "sample_std": sample_std,
        "standard_error": standard_error,
        "t_critical": t_critical,
        "one_sided_lower_bound": one_sided_lower_bound,
        "worst_k": worst_k,
        "worst_tail_mean": worst_tail_mean,
        "serial_correlation_unadjusted": True,
    }


def _validated_policy(policy: object) -> tuple[int, float, float, bool]:
    item = _mapping(policy, "policy")
    if set(item) != set(_POLICY_KEYS):
        raise ValueError("policy keys are incomplete or unexpected")
    required_days = item["required_days"]
    if isinstance(required_days, bool) or not isinstance(required_days, int) or required_days < 2:
        raise ValueError("policy.required_days must be an integer >= 2")
    confidence = _optional_number(item["confidence_level"], "policy.confidence_level")
    worst_fraction = _optional_number(item["worst_fraction"], "policy.worst_fraction")
    if confidence is None or not 0 < confidence < 1:
        raise ValueError("policy.confidence_level must be between 0 and 1")
    if worst_fraction is None or not 0 < worst_fraction <= 1:
        raise ValueError("policy.worst_fraction must be in (0, 1]")
    require_concentration = item["require_concentration_non_increase"]
    if not isinstance(require_concentration, bool):
        raise ValueError("policy.require_concentration_non_increase must be boolean")
    return required_days, confidence, worst_fraction, require_concentration


def _validated_points(point_rows: object, required_days: int) -> list[_Point]:
    if not isinstance(point_rows, list):
        raise ValueError("point_rows must be a list")
    rows = cast(list[object], point_rows)
    normalized: list[_Point] = []
    point_ids: set[str] = set()
    trading_dates: set[str] = set()
    for index, raw in enumerate(rows):
        item = _mapping(raw, f"point_rows[{index}]")
        if set(item) != set(_POINT_KEYS):
            raise ValueError(f"point_rows[{index}] keys are incomplete or unexpected")
        point_id = _canonical_text(
            item["recommendation_point_id"],
            f"point_rows[{index}].recommendation_point_id",
        )
        if point_id in point_ids:
            raise ValueError("recommendation_point_id must be unique")
        point_ids.add(point_id)
        trading_date = _iso_date(item["trading_date"], f"point_rows[{index}].trading_date")
        trading_dates.add(trading_date)
        raw_risk_status = item["hard_risk_status"]
        if not isinstance(raw_risk_status, str) or raw_risk_status not in _RISK_STATUSES:
            raise ValueError(f"point_rows[{index}].hard_risk_status is invalid")
        risk_status = raw_risk_status
        normalized.append(
            {
                "recommendation_point_id": point_id,
                "trading_date": trading_date,
                "baseline_candidate_id": _optional_candidate(
                    item["baseline_candidate_id"],
                    f"point_rows[{index}].baseline_candidate_id",
                ),
                "challenger_candidate_id": _optional_candidate(
                    item["challenger_candidate_id"],
                    f"point_rows[{index}].challenger_candidate_id",
                ),
                "baseline_efficiency": _optional_number(
                    item["baseline_efficiency"],
                    f"point_rows[{index}].baseline_efficiency",
                ),
                "challenger_efficiency": _optional_number(
                    item["challenger_efficiency"],
                    f"point_rows[{index}].challenger_efficiency",
                ),
                "hard_risk_status": risk_status,
                "baseline_concentration": _optional_number(
                    item["baseline_concentration"],
                    f"point_rows[{index}].baseline_concentration",
                ),
                "challenger_concentration": _optional_number(
                    item["challenger_concentration"],
                    f"point_rows[{index}].challenger_concentration",
                ),
            }
        )
    if len(trading_dates) > required_days:
        raise ValueError("point_rows span more trading dates than required_days")
    return normalized


def summarize_paired_daily_deltas(
    point_rows: object,
    policy: object,
) -> dict[str, object]:
    required_days, confidence, worst_fraction, require_concentration = _validated_policy(policy)
    points = _validated_points(point_rows, required_days)

    if any(point["hard_risk_status"] == "violated" for point in points):
        return _result(
            decision="keep_baseline",
            reason_codes=["hard_risk_violation"],
            required_days=required_days,
            effective_days=0,
            point_results=[],
            daily_deltas=[],
        )
    if any(point["hard_risk_status"] == "missing" for point in points):
        return _result(
            decision="insufficient_evidence",
            reason_codes=["risk_evidence_missing"],
            required_days=required_days,
            effective_days=0,
            point_results=[],
            daily_deltas=[],
        )

    point_results: list[_PointResult] = []
    deltas_by_date: defaultdict[str, list[float]] = defaultdict(list)
    concentration_failed = False
    for point in points:
        baseline_id = point["baseline_candidate_id"]
        challenger_id = point["challenger_candidate_id"]
        point_delta: float | None
        if baseline_id is None and challenger_id is None:
            status = "no_evidence"
            point_delta = None
        elif baseline_id is None or challenger_id is None:
            return _result(
                decision="insufficient_evidence",
                reason_codes=["official_decision_incomplete"],
                required_days=required_days,
                effective_days=0,
                point_results=[],
                daily_deltas=[],
            )
        elif baseline_id == challenger_id:
            status = "same_candidate"
            point_delta = 0.0
        else:
            baseline_efficiency = point["baseline_efficiency"]
            challenger_efficiency = point["challenger_efficiency"]
            if baseline_efficiency is None or challenger_efficiency is None:
                return _result(
                    decision="insufficient_evidence",
                    reason_codes=["official_decision_incomplete"],
                    required_days=required_days,
                    effective_days=0,
                    point_results=[],
                    daily_deltas=[],
                )
            if require_concentration:
                baseline_concentration = point["baseline_concentration"]
                challenger_concentration = point["challenger_concentration"]
                if baseline_concentration is None or challenger_concentration is None:
                    return _result(
                        decision="insufficient_evidence",
                        reason_codes=["risk_evidence_missing"],
                        required_days=required_days,
                        effective_days=0,
                        point_results=[],
                        daily_deltas=[],
                    )
                concentration_failed = concentration_failed or (
                    challenger_concentration > baseline_concentration
                )
            status = "paired"
            point_delta = challenger_efficiency - baseline_efficiency

        point_results.append(
            {
                "recommendation_point_id": point["recommendation_point_id"],
                "trading_date": point["trading_date"],
                "status": status,
                "point_delta": point_delta,
            }
        )
        if point_delta is not None:
            deltas_by_date[point["trading_date"]].append(point_delta)

    daily_deltas: list[_DailyDelta] = []
    values: list[float] = []
    for trading_date, point_deltas in sorted(deltas_by_date.items()):
        daily_delta = stdlib_statistics.fmean(point_deltas)
        daily_deltas.append(
            {
                "trading_date": trading_date,
                "effective_point_count": len(point_deltas),
                "daily_delta": daily_delta,
            }
        )
        values.append(daily_delta)
    effective_days = len(daily_deltas)
    if concentration_failed:
        return _result(
            decision="keep_baseline",
            reason_codes=["concentration_non_increase_failed"],
            required_days=required_days,
            effective_days=effective_days,
            point_results=point_results,
            daily_deltas=daily_deltas,
        )
    if effective_days < required_days:
        return _result(
            decision="insufficient_evidence",
            reason_codes=["effective_days_below_required"],
            required_days=required_days,
            effective_days=effective_days,
            point_results=point_results,
            daily_deltas=daily_deltas,
        )

    mean = stdlib_statistics.fmean(values)
    sample_std = stdlib_statistics.stdev(values)
    standard_error = sample_std / math.sqrt(effective_days)
    worst_k = math.ceil(effective_days * worst_fraction)
    worst_tail_mean = stdlib_statistics.fmean(sorted(values)[:worst_k])
    try:
        from scipy.stats import t as student_t  # pyright: ignore[reportMissingTypeStubs]

        t_critical = float(
            student_t.ppf(confidence, df=effective_days - 1)  # pyright: ignore[reportUnknownMemberType]
        )
    except (ArithmeticError, AttributeError, ImportError, OSError, TypeError, ValueError):
        t_critical = math.nan
    if not math.isfinite(t_critical):
        return _result(
            decision="insufficient_evidence",
            reason_codes=["statistics_backend_unavailable"],
            required_days=required_days,
            effective_days=effective_days,
            point_results=point_results,
            daily_deltas=daily_deltas,
            mean_daily_delta=mean,
            sample_std=sample_std,
            standard_error=standard_error,
            worst_k=worst_k,
            worst_tail_mean=worst_tail_mean,
        )

    lower_bound = mean if sample_std == 0 else mean - t_critical * standard_error
    if mean <= 0:
        decision = "keep_baseline"
        reason_codes = ["non_positive_mean"]
    elif worst_tail_mean < 0:
        decision = "keep_baseline"
        reason_codes = ["negative_worst_tail"]
    elif lower_bound <= 0:
        decision = "insufficient_evidence"
        reason_codes = ["positive_mean_lcb_not_above_zero"]
    else:
        decision = "pass"
        reason_codes = [
            "positive_one_sided_lcb",
            "non_negative_worst_tail",
            "hard_risk_passed",
        ]
    return _result(
        decision=decision,
        reason_codes=reason_codes,
        required_days=required_days,
        effective_days=effective_days,
        point_results=point_results,
        daily_deltas=daily_deltas,
        mean_daily_delta=mean,
        sample_std=sample_std,
        standard_error=standard_error,
        t_critical=t_critical,
        one_sided_lower_bound=lower_bound,
        worst_k=worst_k,
        worst_tail_mean=worst_tail_mean,
    )
