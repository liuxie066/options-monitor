from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.application.quality.model import check_result, dataset_status, evidence_ref


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


def _oldest_pending_age_seconds(payload: Any, *, now: datetime) -> float | None:
    if not isinstance(payload, dict):
        return None
    times: list[datetime] = []
    for bucket in ("failed_deal_ids", "unresolved_deal_ids"):
        rows = payload.get(bucket) if isinstance(payload.get(bucket), dict) else {}
        for item in rows.values():
            if not isinstance(item, dict):
                continue
            if _is_delegated_lifecycle_pending(item):
                continue
            parsed = _pending_item_timestamp(item)
            if parsed:
                times.append(parsed)
    if not times:
        return None
    return max(0.0, (now.astimezone(timezone.utc) - min(times)).total_seconds())


def _is_delegated_lifecycle_pending(item: dict[str, Any]) -> bool:
    if str(item.get("reason") or "").strip().lower() != "waiting_settlement_evidence":
        return False
    diagnostics = item.get("diagnostics") if isinstance(item.get("diagnostics"), dict) else {}
    adoption = (
        diagnostics.get("lifecycle_adoption")
        if isinstance(diagnostics.get("lifecycle_adoption"), dict)
        else {}
    )
    return bool(
        diagnostics.get("broker_evidence_accepted")
        and str(adoption.get("status") or "").strip().lower() == "accepted"
        and str(adoption.get("case_id") or "").strip()
    )


def _delegated_lifecycle_pending_count(payload: Any) -> int:
    if not isinstance(payload, dict):
        return 0
    rows = (
        payload.get("unresolved_deal_ids")
        if isinstance(payload.get("unresolved_deal_ids"), dict)
        else {}
    )
    return sum(
        1
        for item in rows.values()
        if isinstance(item, dict) and _is_delegated_lifecycle_pending(item)
    )


def _pending_item_timestamp(item: dict[str, Any]) -> datetime | None:
    candidates = [item]
    receipt = item.get("receipt")
    if isinstance(receipt, dict):
        candidates.append(receipt)
    for candidate in candidates:
        for key in (
            "first_seen_at_utc",
            "created_at_utc",
            "updated_at_utc",
            "last_seen_at_utc",
            "updated_at",
        ):
            parsed = _parse_utc(candidate.get(key))
            if parsed:
                return parsed
        for key in ("first_seen_at_ms", "created_at_ms", "updated_at_ms"):
            try:
                value = int(candidate.get(key) or 0)
            except (TypeError, ValueError):
                value = 0
            if value > 0:
                return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    return None


def _state_oldest_pending_age_seconds(path: Path, *, now: datetime) -> float | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return None
    return _oldest_pending_age_seconds(payload, now=now)


def build_trade_intake_datasets(
    *,
    runtime_statuses: list[dict[str, Any]],
    accounts: list[str],
    market: str,
    repo_root: Path,
    observed_at_utc: str,
    now: datetime,
    pending_grace_seconds: int = 300,
) -> list[dict[str, Any]]:
    source_rows: list[dict[str, Any]] = []
    intake_enabled = False
    for runtime in runtime_statuses:
        intake = runtime.get("trade_intake") if isinstance(runtime.get("trade_intake"), dict) else {}
        if not bool(intake.get("enabled")):
            continue
        intake_enabled = True
        source_rows.extend(
            item
            for item in intake.get("sources") or []
            if isinstance(item, dict) and bool(item.get("enabled"))
        )
    if not intake_enabled:
        out: list[dict[str, Any]] = []
        for account in accounts:
            checks = [
                check_result(
                    check_id=check_id,
                    status="pass",
                    scope={"account": account, "market": market, "source": "trade-intake"},
                    observed_at_utc=observed_at_utc,
                    reason_code="TRADE_INTAKE_NOT_APPLICABLE",
                    message="Trade intake is disabled for this runtime configuration.",
                    evidence_refs=[],
                )
                for check_id in ("OM-INT-001", "OM-INT-002", "OM-INT-003")
            ]
            out.append(
                dataset_status(
                    dataset_id="om.trade_intake",
                    scope={"account": account, "market": market, "source": "trade-intake"},
                    status="trusted",
                    as_of_utc=observed_at_utc,
                    checks=checks,
                    usable_for=["option_positions", "lifecycle", "close_advice"],
                )
            )
        return out
    if not source_rows:
        source_rows = [{"id": "trade-intake", "account": None, "summary": {}}]

    out = []
    for source in source_rows:
        source_account = str(source.get("account") or "").strip().lower()
        scoped_accounts = [source_account] if source_account else list(accounts)
        summary = source.get("summary") if isinstance(source.get("summary"), dict) else {}
        pending_count = int(summary.get("pending_count") or 0)
        failed_count = int(summary.get("failed_count") or 0)
        unresolved_count = int(summary.get("unresolved_count") or 0)
        state_info = source.get("state") if isinstance(source.get("state"), dict) else {}
        state_path_value = str(state_info.get("path") or "").strip()
        state_path = (repo_root / state_path_value).resolve() if state_path_value else None
        state_payload = state_info.get("json")
        state_delegated_lifecycle_pending_count = (
            _delegated_lifecycle_pending_count(state_payload)
        )
        preview_available = bool(summary.get("reconciliation_preview_available"))
        preview_delegated_lifecycle_pending_count = (
            int(summary.get("delegated_lifecycle_pending_count") or 0)
            if preview_available
            else 0
        )
        delegated_lifecycle_pending_count = max(
            state_delegated_lifecycle_pending_count,
            preview_delegated_lifecycle_pending_count,
        )
        actionable_pending_count = max(
            0,
            pending_count - delegated_lifecycle_pending_count,
        )
        actionable_unresolved_count = max(
            0,
            unresolved_count - delegated_lifecycle_pending_count,
        )
        pending_age = _oldest_pending_age_seconds(state_payload, now=now)
        if (
            pending_age is None
            and state_path is not None
            and state_path.is_relative_to(repo_root.resolve())
        ):
            pending_age = _state_oldest_pending_age_seconds(state_path, now=now)
        evidence = evidence_ref(
            kind="trade-intake-state",
            observed_at_utc=observed_at_utc,
            value={
                "source": source.get("id"),
                "pending_count": pending_count,
                "actionable_pending_count": actionable_pending_count,
                "failed_count": failed_count,
                "unresolved_count": unresolved_count,
                "actionable_unresolved_count": actionable_unresolved_count,
                "delegated_lifecycle_pending_count": delegated_lifecycle_pending_count,
                "state_delegated_lifecycle_pending_count": state_delegated_lifecycle_pending_count,
                "preview_delegated_lifecycle_pending_count": preview_delegated_lifecycle_pending_count,
                "pending_age_seconds": pending_age,
                "reconciliation_preview_available": summary.get("reconciliation_preview_available"),
                "pending_after_reconcile_count": summary.get("pending_after_reconcile_count"),
                "actionable_pending_after_reconcile_count": summary.get(
                    "actionable_pending_after_reconcile_count"
                ),
            },
            artifact_ref=f"om-evidence:trade-intake:{str(source.get('id') or 'unknown')}",
        )
        for account in scoped_accounts:
            if actionable_pending_count == 0:
                intake_status, intake_reason = "pass", "INTAKE_PENDING_CLEAR"
                intake_message = "No actionable trade intake row is pending."
            elif pending_age is None:
                intake_status, intake_reason = "unknown", "INTAKE_PENDING_AGE_UNKNOWN"
                intake_message = "Pending trade intake rows exist, but their age cannot be proven."
            elif pending_age <= pending_grace_seconds:
                intake_status, intake_reason = "warn", "INTAKE_PENDING_WITHIN_GRACE"
                intake_message = "Trade intake rows are pending within the five-minute grace period."
            else:
                intake_status, intake_reason = "fail", "INTAKE_PENDING_OVERDUE"
                intake_message = "Trade intake rows remain pending beyond the five-minute grace period."
            check_pending = check_result(
                check_id="OM-INT-001",
                status=intake_status,
                scope={"account": account, "market": market, "source": str(source.get("id") or "trade-intake")},
                observed_at_utc=observed_at_utc,
                reason_code=intake_reason,
                message=intake_message,
                observed={
                    "pending_count": pending_count,
                    "actionable_pending_count": actionable_pending_count,
                    "delegated_lifecycle_pending_count": delegated_lifecycle_pending_count,
                    "oldest_pending_age_seconds": pending_age,
                },
                expected={"actionable_pending_count": 0},
                thresholds={"pending_grace_seconds": pending_grace_seconds},
                evidence_refs=[evidence],
            )
            unresolved = failed_count + actionable_unresolved_count
            check_unresolved = check_result(
                check_id="OM-INT-002",
                status="fail" if unresolved else "pass",
                scope={"account": account, "market": market, "source": str(source.get("id") or "trade-intake")},
                observed_at_utc=observed_at_utc,
                reason_code="INTAKE_UNRESOLVED_ROWS" if unresolved else "INTAKE_NO_UNRESOLVED_ROWS",
                message=(
                    "Failed or unresolved trade-intake rows require repair."
                    if unresolved
                    else "No failed or unresolved trade-intake row remains."
                ),
                observed={
                    "failed_count": failed_count,
                    "unresolved_count": unresolved_count,
                    "actionable_unresolved_count": actionable_unresolved_count,
                    "delegated_lifecycle_pending_count": delegated_lifecycle_pending_count,
                },
                expected={"failed_count": 0, "actionable_unresolved_count": 0},
                evidence_refs=[evidence],
            )
            terminal_available = preview_available
            raw_terminal_missing = int(
                summary.get("pending_after_reconcile_count") or 0
            )
            if (
                terminal_available
                and summary.get("actionable_pending_after_reconcile_count")
                is not None
            ):
                terminal_missing = int(
                    summary.get("actionable_pending_after_reconcile_count") or 0
                )
            else:
                terminal_missing = max(
                    0,
                    raw_terminal_missing - delegated_lifecycle_pending_count,
                )
            broker_check = check_result(
                check_id="OM-INT-003",
                status=(
                    "fail"
                    if terminal_available and terminal_missing
                    else "pass"
                    if terminal_available
                    else "unknown"
                ),
                scope={"account": account, "market": market, "source": str(source.get("id") or "trade-intake")},
                observed_at_utc=observed_at_utc,
                reason_code=(
                    "BROKER_DEAL_LOCAL_EVENT_MISSING"
                    if terminal_available and terminal_missing
                    else "BROKER_TERMINAL_EVIDENCE_RECONCILED"
                    if terminal_available
                    else "BROKER_TERMINAL_WINDOW_UNAVAILABLE"
                ),
                message=(
                    "Broker-terminal evidence has no matching local terminal event."
                    if terminal_available and terminal_missing
                    else "Broker-terminal evidence agrees with local terminal events."
                    if terminal_available
                    else "Broker-terminal evidence window is unavailable."
                ),
                observed={
                    "pending_after_reconcile_count": raw_terminal_missing,
                    "delegated_lifecycle_pending_count": delegated_lifecycle_pending_count,
                    "missing_local_terminal_count": terminal_missing,
                },
                expected={"missing_local_terminal_count": 0},
                evidence_refs=[evidence],
            )
            blocking = [
                item
                for item in (check_pending, check_unresolved, broker_check)
                if item["status"] in {"fail", "unknown"}
            ]
            warning = [item for item in (check_pending, check_unresolved, broker_check) if item["status"] == "warn"]
            status = "unavailable" if any(item["status"] == "unknown" for item in blocking) else "untrusted" if blocking else "partial" if warning else "trusted"
            out.append(
                dataset_status(
                    dataset_id="om.trade_intake",
                    scope={"account": account, "market": market, "source": str(source.get("id") or "trade-intake")},
                    status=status,
                    as_of_utc=observed_at_utc,
                    checks=[check_pending, check_unresolved, broker_check],
                    evidence_refs=[evidence],
                    usable_for=[] if blocking else ["option_positions", "lifecycle", "close_advice"],
                    blocked_consumers=(
                        ["option_position_report", "lifecycle", "close_advice"] if blocking else []
                    ),
                    blocked_by=[item["check_id"] for item in blocking],
                    reason_codes=[item["reason_code"] for item in [*blocking, *warning]],
                )
            )
    return out


__all__ = ["build_trade_intake_datasets"]
