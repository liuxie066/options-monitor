from __future__ import annotations

from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Mapping

from domain.domain.decision_state_fingerprint import canonical_sha256
from domain.domain.option_lifecycle import FINAL_STATUSES, PENDING_STATUSES
from domain.domain.symbol_identity import symbol_market
from src.application.quality.model import check_result, dataset_status, freshness, utc_iso


EXTERNAL_REVIEW_STATUSES = {"external_adjustment_pending_review", "external_adjustment", "manual_review"}
LIFECYCLE_SUMMARY_DATASET_ID = "om.lifecycle_evidence_summary"


def _parse_date(value: Any) -> date | None:
    raw = str(value or "").strip()
    try:
        return date.fromisoformat(raw[:10]) if raw else None
    except ValueError:
        return None


def _parse_utc(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def next_trading_day(expiration: date, trading_days: list[date]) -> date | None:
    return next((day for day in sorted(set(trading_days)) if day > expiration), None)


def lifecycle_deadline(
    *,
    expiration: date,
    trading_days: list[date],
    first_deep_reconcile_at: datetime | None,
) -> datetime | None:
    next_day = next_trading_day(expiration, trading_days)
    if next_day is None or first_deep_reconcile_at is None:
        return None
    not_before = datetime.combine(next_day, time.min, tzinfo=timezone.utc)
    first = max(first_deep_reconcile_at.astimezone(timezone.utc), not_before)
    return first + timedelta(hours=2)


def build_lifecycle_datasets(
    *,
    cases: list[dict[str, Any]],
    evidence_rows: list[dict[str, Any]],
    account: str,
    market: str,
    observed_at_utc: str,
    now: datetime,
    trading_days: list[date],
    first_deep_by_case: dict[str, str],
    timing_policies_by_case: dict[str, dict[str, Any]] | None = None,
    read_models_by_case: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    del trading_days, first_deep_by_case
    timing_policies = dict(timing_policies_by_case or {})
    read_models = dict(read_models_by_case or {})
    evidence_count_by_case: dict[str, int] = {}
    for item in evidence_rows:
        case_id = str(item.get("case_id") or "").strip()
        if case_id:
            evidence_count_by_case[case_id] = evidence_count_by_case.get(case_id, 0) + 1

    out: list[dict[str, Any]] = []
    for case in cases:
        if str(case.get("account") or "").strip().lower() != account:
            continue
        case_market = str(
            case.get("market") or symbol_market(case.get("symbol")) or ""
        ).strip().lower()
        if case_market and case_market != market.strip().lower():
            continue
        case_id = str(case.get("case_id") or "").strip()
        case_status = str(case.get("status") or "").strip().lower()
        if case_status == "superseded":
            continue
        scope = {"account": account, "market": market, "lifecycle_case_id": case_id}
        evidence_count = evidence_count_by_case.get(case_id, 0)
        is_legacy_gap = bool(
            case.get("legacy_evidence_gap")
            or case.get("migration_evidence_complete") is False
            or str(case.get("quality_classification") or "").lower() == "legacy_evidence_gap"
        )
        is_external = (
            case_status in EXTERNAL_REVIEW_STATUSES
            or str(case.get("decision_type") or "").strip().lower() in EXTERNAL_REVIEW_STATUSES
        )
        if is_external:
            check = check_result(
                check_id="OM-LCY-002",
                status="unknown",
                scope=scope,
                observed_at_utc=observed_at_utc,
                reason_code="EXTERNAL_ADJUSTMENT_PENDING_REVIEW",
                message="External adjustment evidence requires explicit human classification.",
                observed={"evidence_count": evidence_count, "status": case_status},
                expected={"review_status": "classified"},
                evidence_refs=[],
            )
            out.append(
                dataset_status(
                    dataset_id="om.lifecycle_evidence",
                    scope=scope,
                    status="unavailable",
                    as_of_utc=observed_at_utc,
                    checks=[check],
                    blocked_consumers=["lifecycle_report", "close_advice", "option_performance"],
                    blocked_by=["OM-LCY-002"],
                    reason_codes=[check["reason_code"]],
                )
            )
            continue
        if is_legacy_gap:
            check = check_result(
                check_id="OM-LCY-003",
                status="fail",
                severity="warning",
                scope=scope,
                observed_at_utc=observed_at_utc,
                reason_code="LEGACY_EVIDENCE_GAP",
                message="Historical lifecycle evidence is incomplete and isolated from current operations.",
                observed={"evidence_count": evidence_count},
                expected={"migration_evidence_complete": True},
                evidence_refs=[],
            )
            out.append(
                dataset_status(
                    dataset_id="om.lifecycle_history",
                    scope=scope,
                    status="untrusted",
                    as_of_utc=observed_at_utc,
                    checks=[check],
                    usable_for=[],
                    blocked_consumers=["option_performance"],
                    blocked_by=["OM-LCY-003"],
                    reason_codes=[check["reason_code"]],
                )
            )
            continue
        if case_status in FINAL_STATUSES:
            check = check_result(
                check_id="OM-LCY-001",
                status="pass",
                scope=scope,
                observed_at_utc=observed_at_utc,
                reason_code="LIFECYCLE_TERMINAL_EVIDENCE_COMPLETE",
                message="Lifecycle case has complete terminal evidence.",
                observed={"status": case_status, "evidence_count": evidence_count},
                expected={"status": sorted(FINAL_STATUSES)},
                evidence_refs=[],
            )
            out.append(
                dataset_status(
                    dataset_id="om.lifecycle_evidence",
                    scope=scope,
                    status="trusted",
                    as_of_utc=observed_at_utc,
                    checks=[check],
                    usable_for=["lifecycle_report", "close_advice", "option_performance"],
                )
            )
            continue

        read_model = (
            dict(read_models.get(case_id) or {})
            if isinstance(read_models.get(case_id), dict)
            else {}
        )
        timing_policy = (
            dict(timing_policies.get(case_id) or {})
            if isinstance(timing_policies.get(case_id), dict)
            else {}
        )
        deadline_ms = (
            read_model.get("pending_until_ms")
            if read_model.get("pending_until_ms") is not None
            else timing_policy.get("settlement_deadline_ms")
        )
        try:
            deadline = (
                datetime.fromtimestamp(
                    int(deadline_ms) / 1000,
                    tz=timezone.utc,
                )
                if deadline_ms is not None
                and int(deadline_ms) > 0
                else None
            )
        except (TypeError, ValueError, OverflowError):
            deadline = None
        pending = case_status in PENDING_STATUSES or not case_status
        if deadline is None:
            status = "unknown"
            reason = "LIFECYCLE_DEADLINE_UNAVAILABLE"
            message = "Lifecycle deadline is unavailable because no immutable timing policy is bound."
            dataset_verdict = "unavailable"
        elif now.astimezone(timezone.utc) <= deadline and pending:
            status = "warn"
            reason = "LIFECYCLE_PENDING_WITHIN_DEADLINE"
            message = "Lifecycle evidence is pending within the approved market-calendar deadline."
            dataset_verdict = "partial"
        else:
            status = "fail"
            reason = "LIFECYCLE_EVIDENCE_OVERDUE"
            message = "Lifecycle evidence is stale after its immutable settlement deadline."
            dataset_verdict = "untrusted"
        check = check_result(
            check_id="OM-LCY-001",
            status=status,
            scope=scope,
            observed_at_utc=observed_at_utc,
            reason_code=reason,
            message=message,
            observed={
                "status": case_status or None,
                "evidence_count": evidence_count,
                "reason_state": read_model.get("reason_state"),
                "timing_policy_hash": read_model.get(
                    "timing_policy_hash"
                ),
            },
            expected={"terminal_statuses": sorted(FINAL_STATUSES)},
            thresholds={
                "deadline_rule": (
                    "immutable_lifecycle_settlement_deadline"
                )
            },
            evidence_refs=[],
        )
        is_blocking = dataset_verdict in {"untrusted", "unavailable"}
        out.append(
            dataset_status(
                dataset_id="om.lifecycle_evidence",
                scope=scope,
                status=dataset_verdict,
                as_of_utc=observed_at_utc,
                checks=[check],
                freshness_value=freshness(
                    observed_at_utc=observed_at_utc,
                    status="stale" if dataset_verdict == "untrusted" else "unknown" if dataset_verdict == "unavailable" else "fresh",
                    expected_by_utc=utc_iso(deadline) if deadline else None,
                    grace_seconds=7200,
                ),
                usable_for=[] if is_blocking else ["lifecycle_report"],
                blocked_consumers=(
                    ["lifecycle_report", "close_advice", "option_performance"] if is_blocking else []
                ),
                blocked_by=["OM-LCY-001"] if is_blocking else [],
                reason_codes=[reason] if status != "pass" else [],
            )
        )
    return out


def build_lifecycle_quality_migration_summary(
    *,
    legacy_datasets: list[dict[str, Any]],
    current_quality: Mapping[str, Any],
    account: str,
    market: str,
    observed_at_utc: str,
    now_ms: int,
    case_status_by_id: Mapping[str, str],
    read_models_by_case: Mapping[str, Mapping[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    account_key = str(account or "").strip().lower()
    market_key = str(market or "").strip().upper()
    legacy = sorted(
        (
            dict(item)
            for item in legacy_datasets
            if str(item.get("dataset_id") or "")
            in {"om.lifecycle_evidence", "om.lifecycle_history"}
            and str((item.get("scope") or {}).get("account") or "")
            .strip()
            .lower()
            == account_key
            and str((item.get("scope") or {}).get("market") or "")
            .strip()
            .upper()
            == market_key
        ),
        key=lambda item: str((item.get("scope") or {}).get("lifecycle_case_id") or ""),
    )
    legacy_by_case = {
        str((item.get("scope") or {}).get("lifecycle_case_id") or "").strip(): item
        for item in legacy
    }
    if len(legacy_by_case) != len(legacy) or "" in legacy_by_case:
        raise ValueError("legacy lifecycle quality case identity is invalid")

    current_aggregate = next(
        (
            dict(item)
            for item in current_quality.get("aggregate_by_market") or []
            if isinstance(item, Mapping)
            and str(item.get("market") or "").strip().upper() == market_key
        ),
        None,
    )
    current_details = [
        dict(item)
        for item in current_quality.get("operational_cases") or []
        if isinstance(item, Mapping)
        and str(item.get("market") or "").strip().upper() == market_key
    ]

    legacy_summary = {
        "total_case_count": len(legacy),
        "case_status_counts": _counts(
            case_status_by_id.get(case_id)
            for case_id in legacy_by_case
        ),
        "trust_class_counts": _counts(
            _legacy_trust_class(item) for item in legacy
        ),
        "dataset_status_counts": _counts(item.get("status") for item in legacy),
        "blocked_consumer_counts": _counts(
            consumer
            for item in legacy
            for consumer in item.get("blocked_consumers") or []
        ),
    }
    current_summary = {
        "total_case_count": int(
            (current_aggregate or {}).get("total_case_count") or 0
        ),
        "case_status_counts": dict(
            (current_aggregate or {}).get("status_counts") or {}
        ),
        "trust_class_counts": dict(
            (current_aggregate or {}).get("trust_class_counts") or {}
        ),
        "dataset_status_counts": dict(
            (current_aggregate or {}).get("dataset_status_counts") or {}
        ),
        "blocked_consumer_counts": dict(
            (current_aggregate or {}).get("blocked_consumer_counts") or {}
        ),
    }
    legacy_details = {
        case_id: _legacy_operational_detail(
            dataset,
            case_status=case_status_by_id.get(case_id),
            read_model=read_models_by_case.get(case_id),
        )
        for case_id, dataset in legacy_by_case.items()
        if str(case_status_by_id.get(case_id) or "").strip().lower()
        not in FINAL_STATUSES
    }
    current_detail_view = {
        str(item.get("case_id") or "").strip(): {
            "case_id": str(item.get("case_id") or "").strip(),
            "case_status": item.get("status"),
            "trust_class": item.get("trust_class"),
            "evidence_count": item.get("evidence_count"),
            "settlement_deadline_ms": item.get("settlement_deadline_ms"),
            "reason_state": item.get("reason_state"),
            "timing_policy_hash": item.get("timing_policy_hash"),
            "dataset_status": item.get("dataset_status"),
            "blocked_consumers": item.get("blocked_consumers"),
            "reason_codes": _current_quality_reason_codes(
                item,
                now_ms=int(now_ms),
            ),
        }
        for item in current_details
    }
    comparison = _quality_comparison(
        legacy_summary=legacy_summary,
        current_summary=current_summary,
        legacy_details=legacy_details,
        current_details=current_detail_view,
    )
    status_counts = current_summary["dataset_status_counts"]
    current_verdict = (
        "unavailable"
        if status_counts.get("unavailable")
        else "untrusted"
        if status_counts.get("untrusted")
        else "partial"
        if status_counts.get("partial")
        else "trusted"
    )
    verdict = (
        current_verdict
        if comparison["status"] == "matched"
        else "unavailable"
    )
    blocked_consumers = sorted(
        set(current_summary["blocked_consumer_counts"])
        | set(legacy_summary["blocked_consumer_counts"])
    )
    check = check_result(
        check_id="OM-LCY-SHADOW-001",
        status="pass" if comparison["status"] == "matched" else "fail",
        scope={"account": account_key, "market": market_key.lower()},
        observed_at_utc=observed_at_utc,
        reason_code=(
            "CURRENT_DECISION_QUALITY_MATCHED"
            if comparison["status"] == "matched"
            else "CURRENT_DECISION_QUALITY_MISMATCH"
        ),
        message="Current lifecycle quality matches legacy authority."
        if comparison["status"] == "matched"
        else "Current lifecycle quality differs from legacy authority.",
        observed={
            "legacy_sha256": comparison["legacy_sha256"],
            "current_sha256": comparison["current_sha256"],
            "mismatch_count": comparison["mismatch_count"],
        },
        expected={"legacy_parity": True},
        evidence_refs=[],
    )
    return (
        dataset_status(
            dataset_id=LIFECYCLE_SUMMARY_DATASET_ID,
            scope={"account": account_key, "market": market_key.lower()},
            status=verdict,
            as_of_utc=observed_at_utc,
            checks=[check],
            usable_for=[],
            blocked_consumers=blocked_consumers,
            blocked_by=(
                ["OM-LCY-SHADOW-001"]
                if comparison["status"] != "matched"
                else []
            ),
            reason_codes=(
                ["CURRENT_DECISION_QUALITY_MISMATCH"]
                if comparison["status"] != "matched"
                else []
            ),
            extensions={
                "schema_version": "current_lifecycle_quality_shadow.v1",
                "aggregate": current_summary,
                "operational_cases": [
                    current_detail_view[key]
                    for key in sorted(current_detail_view)
                ],
                "comparison": comparison,
            },
        ),
        comparison,
    )


def _counts(values: Any) -> dict[str, int]:
    return dict(
        sorted(
            Counter(
                str(value).strip()
                for value in values
                if str(value or "").strip()
            ).items()
        )
    )


def _legacy_trust_class(dataset: Mapping[str, Any]) -> str:
    reasons = {str(item) for item in dataset.get("reason_codes") or []}
    if "EXTERNAL_ADJUSTMENT_PENDING_REVIEW" in reasons:
        return "external_review"
    if str(dataset.get("dataset_id") or "") == "om.lifecycle_history":
        return "legacy_gap"
    return "trusted"


def _legacy_operational_detail(
    dataset: Mapping[str, Any],
    *,
    case_status: Any,
    read_model: Mapping[str, Any] | None,
) -> dict[str, Any]:
    scope = dataset.get("scope") if isinstance(dataset.get("scope"), Mapping) else {}
    checks = [item for item in dataset.get("checks") or [] if isinstance(item, Mapping)]
    check = checks[0] if checks else {}
    observed = check.get("observed") if isinstance(check.get("observed"), Mapping) else {}
    model = dict(read_model or {})
    expected_by = str(
        ((dataset.get("freshness") or {}).get("expected_by_utc") or "")
    ).strip()
    deadline_ms = model.get("pending_until_ms")
    if deadline_ms is None and expected_by:
        parsed = _parse_utc(expected_by)
        deadline_ms = int(parsed.timestamp() * 1000) if parsed else None
    return {
        "case_id": str(scope.get("lifecycle_case_id") or "").strip(),
        "case_status": case_status,
        "trust_class": _legacy_trust_class(dataset),
        "evidence_count": observed.get("evidence_count"),
        "settlement_deadline_ms": deadline_ms,
        "reason_state": model.get("reason_state"),
        "timing_policy_hash": model.get("timing_policy_hash"),
        "dataset_status": dataset.get("status"),
        "blocked_consumers": list(dataset.get("blocked_consumers") or []),
        "reason_codes": list(dataset.get("reason_codes") or []),
    }


def _current_quality_reason_codes(
    detail: Mapping[str, Any],
    *,
    now_ms: int,
) -> list[str]:
    trust = detail.get("trust_class")
    deadline = detail.get("settlement_deadline_ms")
    if trust == "external_review":
        return ["EXTERNAL_ADJUSTMENT_PENDING_REVIEW"]
    if trust == "legacy_gap":
        return ["LEGACY_EVIDENCE_GAP"]
    if detail.get("status") == "ledger_written":
        return []
    if deadline is None:
        return ["LIFECYCLE_DEADLINE_UNAVAILABLE"]
    if detail.get("dataset_status") == "partial":
        return ["LIFECYCLE_PENDING_WITHIN_DEADLINE"]
    return ["LIFECYCLE_EVIDENCE_OVERDUE"]


def _quality_comparison(
    *,
    legacy_summary: Mapping[str, Any],
    current_summary: Mapping[str, Any],
    legacy_details: Mapping[str, Any],
    current_details: Mapping[str, Any],
) -> dict[str, Any]:
    mismatches: list[dict[str, Any]] = []
    if legacy_summary != current_summary:
        mismatches.append({"key": "summary"})
    for case_id in sorted(set(legacy_details) | set(current_details)):
        if (
            case_id not in legacy_details
            or case_id not in current_details
            or legacy_details[case_id] != current_details[case_id]
        ):
            mismatches.append({"key": case_id})
    return {
        "status": "matched" if not mismatches else "mismatch",
        "legacy_sha256": canonical_sha256(
            {"summary": legacy_summary, "operational_cases": legacy_details}
        ),
        "current_sha256": canonical_sha256(
            {"summary": current_summary, "operational_cases": current_details}
        ),
        "legacy_case_count": len(legacy_details),
        "current_case_count": len(current_details),
        "mismatch_count": len(mismatches),
        "mismatch_samples": mismatches[:10],
    }


__all__ = [
    "LIFECYCLE_SUMMARY_DATASET_ID",
    "build_lifecycle_datasets",
    "build_lifecycle_quality_migration_summary",
    "lifecycle_deadline",
    "next_trading_day",
]
