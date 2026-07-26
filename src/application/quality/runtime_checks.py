from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from src.application.quality.model import check_result, evidence_ref


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


def build_runtime_checks(
    *,
    runtime_statuses: list[dict[str, Any]],
    observed_at_utc: str,
    now: datetime,
    heartbeat_expected_seconds: int = 300,
) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    service_rows: list[dict[str, Any]] = []
    timer_rows: list[dict[str, Any]] = []
    for item in runtime_statuses:
        profile = item.get("service_profile") if isinstance(item.get("service_profile"), dict) else {}
        for row in profile.get("services") or []:
            if not isinstance(row, dict):
                continue
            name = str(row.get("name") or "").strip()
            (timer_rows if name.endswith(".timer") else service_rows).append(row)

    service_statuses = {str(item.get("status") or "").strip().lower() for item in service_rows}
    if service_rows and service_statuses == {"ok"}:
        service_status, service_reason, service_message = (
            "pass",
            "OM_SERVICES_ACTIVE",
            "Configured OM services are active.",
        )
    elif service_rows and "warn" in service_statuses:
        service_status, service_reason, service_message = (
            "fail",
            "OM_SERVICE_INACTIVE",
            "At least one configured OM service is inactive or failed.",
        )
    else:
        service_status, service_reason, service_message = (
            "unknown",
            "OM_SERVICE_STATUS_UNAVAILABLE",
            "OM service activity cannot be proven from checked service evidence.",
        )
    checks.append(
        check_result(
            check_id="RT-OM-001",
            status=service_status,
            scope={"source": "service-profile"},
            observed_at_utc=observed_at_utc,
            reason_code=service_reason,
            message=service_message,
            observed={
                "service_count": len(service_rows),
                "statuses": sorted(service_statuses),
            },
            expected={"statuses": ["ok"]},
            evidence_refs=[],
        )
    )

    source_rows: list[dict[str, Any]] = []
    intake_enabled = False
    for item in runtime_statuses:
        intake = item.get("trade_intake") if isinstance(item.get("trade_intake"), dict) else {}
        if not bool(intake.get("enabled")):
            continue
        intake_enabled = True
        source_rows.extend(
            row
            for row in intake.get("sources") or []
            if isinstance(row, dict) and bool(row.get("enabled"))
        )
    if not intake_enabled:
        checks.append(
            check_result(
                check_id="RT-OM-002",
                status="pass",
                scope={"source": "trade-intake"},
                observed_at_utc=observed_at_utc,
                reason_code="LISTENER_NOT_APPLICABLE",
                message="Trade intake is disabled for the selected runtime configs.",
                evidence_refs=[],
            )
        )
    elif not source_rows:
        checks.append(
            check_result(
                check_id="RT-OM-002",
                status="unknown",
                scope={"source": "trade-intake"},
                observed_at_utc=observed_at_utc,
                reason_code="LISTENER_EVIDENCE_MISSING",
                message="No trade listener evidence is available.",
                evidence_refs=[],
            )
        )
    for row in source_rows:
        summary = row.get("summary") if isinstance(row.get("summary"), dict) else {}
        heartbeat = _parse_utc(summary.get("last_heartbeat_utc"))
        age = (now.astimezone(timezone.utc) - heartbeat).total_seconds() if heartbeat else None
        listener_status = str(summary.get("listener_status") or "").strip().lower()
        if age is None:
            status, reason, message = "unknown", "LISTENER_HEARTBEAT_MISSING", "Listener heartbeat is missing."
        elif age <= heartbeat_expected_seconds * 2 and listener_status not in {"failed", "stopped"}:
            status, reason, message = "pass", "LISTENER_HEARTBEAT_FRESH", "Trade intake heartbeat is fresh."
        elif age <= heartbeat_expected_seconds * 5:
            status, reason, message = "warn", "LISTENER_HEARTBEAT_DELAYED", "Trade intake heartbeat is delayed."
        else:
            status, reason, message = "fail", "LISTENER_HEARTBEAT_STALE", "Trade intake heartbeat is stale."
        evidence = evidence_ref(
            kind="listener-heartbeat",
            observed_at_utc=heartbeat.isoformat().replace("+00:00", "Z") if heartbeat else observed_at_utc,
            value={
                "source": row.get("id"),
                "listener_status": listener_status or None,
                "heartbeat_age_seconds": age,
            },
            artifact_ref=f"om-evidence:listener:{str(row.get('id') or 'unknown')}",
        )
        checks.append(
            check_result(
                check_id="RT-OM-002",
                status=status,
                scope={"account": row.get("account"), "source": str(row.get("id") or "trade-intake")},
                observed_at_utc=observed_at_utc,
                reason_code=reason,
                message=message,
                observed={"heartbeat_age_seconds": age, "listener_status": listener_status or None},
                thresholds={
                    "healthy_max_seconds": heartbeat_expected_seconds * 2,
                    "unhealthy_after_seconds": heartbeat_expected_seconds * 5,
                },
                evidence_refs=[evidence],
            )
        )

    timer_statuses = {str(item.get("status") or "").strip().lower() for item in timer_rows}
    if timer_rows and timer_statuses == {"ok"}:
        timer_status, timer_reason, timer_message = (
            "pass",
            "TIMERS_ACTIVE",
            "Configured OM timers are active.",
        )
    elif timer_rows and "warn" in timer_statuses:
        timer_status, timer_reason, timer_message = (
            "fail",
            "TIMER_INACTIVE",
            "At least one configured OM timer is inactive or failed.",
        )
    else:
        timer_status, timer_reason, timer_message = (
            "unknown",
            "TIMER_STATUS_UNAVAILABLE",
            "Host timer activity cannot be proven from checked service evidence.",
        )
    checks.append(
        check_result(
            check_id="RT-OM-003",
            status=timer_status,
            scope={"source": "service-profile"},
            observed_at_utc=observed_at_utc,
            reason_code=timer_reason,
            message=timer_message,
            observed={
                "timer_count": len(timer_rows),
                "statuses": sorted(timer_statuses),
            },
            expected={"statuses": ["ok"]},
            evidence_refs=[],
        )
    )
    return checks


def runtime_verdict(checks: list[dict[str, Any]]) -> str:
    statuses = {str(item.get("status") or "") for item in checks}
    if "fail" in statuses:
        return "unhealthy"
    if "unknown" in statuses:
        return "unknown"
    if "warn" in statuses:
        return "degraded"
    return "healthy"


__all__ = ["build_runtime_checks", "runtime_verdict"]
