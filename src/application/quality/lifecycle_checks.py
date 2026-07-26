from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from src.application.quality.model import check_result, dataset_status, freshness, utc_iso
from src.application.trades.lifecycle import FINAL_STATUSES, PENDING_STATUSES


EXTERNAL_REVIEW_STATUSES = {"external_adjustment_pending_review", "external_adjustment", "manual_review"}


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
) -> list[dict[str, Any]]:
    evidence_count_by_case: dict[str, int] = {}
    for item in evidence_rows:
        case_id = str(item.get("case_id") or "").strip()
        if case_id:
            evidence_count_by_case[case_id] = evidence_count_by_case.get(case_id, 0) + 1

    out: list[dict[str, Any]] = []
    for case in cases:
        if str(case.get("account") or "").strip().lower() != account:
            continue
        case_id = str(case.get("case_id") or "").strip()
        case_status = str(case.get("status") or "").strip().lower()
        expiration = _parse_date(case.get("expiration_ymd"))
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

        first_deep = _parse_utc(first_deep_by_case.get(case_id))
        deadline = (
            lifecycle_deadline(
                expiration=expiration,
                trading_days=trading_days,
                first_deep_reconcile_at=first_deep,
            )
            if expiration
            else None
        )
        pending = case_status in PENDING_STATUSES or not case_status
        if deadline is None:
            status = "unknown"
            reason = "LIFECYCLE_DEADLINE_UNAVAILABLE"
            message = "Lifecycle deadline cannot be proven without market-calendar and deep-reconcile evidence."
            dataset_verdict = "unavailable"
        elif now.astimezone(timezone.utc) <= deadline and pending:
            status = "warn"
            reason = "LIFECYCLE_PENDING_WITHIN_DEADLINE"
            message = "Lifecycle evidence is pending within the approved market-calendar deadline."
            dataset_verdict = "partial"
        else:
            status = "fail"
            reason = "LIFECYCLE_EVIDENCE_OVERDUE"
            message = "Lifecycle evidence is stale after the first next-market-day deep reconciliation plus two hours."
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
                "first_deep_reconcile_at_utc": utc_iso(first_deep) if first_deep else None,
            },
            expected={"terminal_statuses": sorted(FINAL_STATUSES)},
            thresholds={"deadline_rule": "next_market_day_first_deep_reconcile_plus_2h"},
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


__all__ = ["build_lifecycle_datasets", "lifecycle_deadline", "next_trading_day"]
