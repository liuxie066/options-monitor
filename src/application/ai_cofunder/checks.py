from __future__ import annotations

from typing import Any


SEVERITY_ORDER = {"info": 0, "warn": 1, "fail": 2}


def run_deterministic_checks(evidence: dict[str, Any]) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    categories: list[str] = []

    scheduler = _dict(evidence.get("scheduler_evidence"))
    _check_scheduler(scheduler, findings=findings, categories=categories)

    runtime = _dict(evidence.get("runtime_status"))
    _check_runtime(runtime, findings=findings, categories=categories)
    _check_prefetch(runtime, findings=findings, categories=categories)
    _check_notification(runtime, findings=findings, categories=categories)
    _check_position_maintenance(runtime, findings=findings, categories=categories)
    _check_trade_intake(runtime, findings=findings, categories=categories)

    category = _primary_category(findings, categories)
    status = _overall_status(findings)
    return {
        "schema_version": "ai_cofunder_diagnosis.v1",
        "status": status,
        "category": category,
        "findings": findings,
        "issue_candidates": _issue_candidates(findings),
        "summary": {
            "finding_count": len(findings),
            "fail_count": sum(1 for item in findings if item.get("severity") == "fail"),
            "warn_count": sum(1 for item in findings if item.get("severity") == "warn"),
            "category": category,
            "status": status,
        },
    }


def _check_scheduler(scheduler: dict[str, Any], *, findings: list[dict[str, Any]], categories: list[str]) -> None:
    if not scheduler.get("provided"):
        categories.append("scheduler_unknown")
        findings.append(
            _finding(
                severity="warn",
                category="scheduler_unknown",
                code="SCHEDULER_EVIDENCE_MISSING",
                message="Online scheduler evidence was not provided; local runtime files cannot prove whether the job triggered.",
                evidence=[{"source": "input.scheduler_evidence", "observed": None, "expected": "online scheduler status"}],
            )
        )
        return

    status = str(scheduler.get("last_status") or "").strip().lower()
    exit_code = _as_int_or_none(scheduler.get("last_exit_code"))
    timed_out = _truthy(scheduler.get("timeout"))
    if status in {"failed", "failure", "error", "timeout", "timed_out"} or timed_out or (exit_code is not None and exit_code != 0):
        categories.append("scheduler_failed")
        findings.append(
            _finding(
                severity="fail",
                category="scheduler_failed",
                code="SCHEDULER_FAILED",
                message="The online scheduler reported a failed or timed-out job.",
                evidence=[
                    {"source": "scheduler_evidence.last_status", "observed": scheduler.get("last_status"), "expected": "success"},
                    {"source": "scheduler_evidence.last_exit_code", "observed": scheduler.get("last_exit_code"), "expected": 0},
                ],
            )
        )
        return

    if not scheduler.get("last_triggered_at"):
        categories.append("scheduler_unknown")
        findings.append(
            _finding(
                severity="warn",
                category="scheduler_unknown",
                code="SCHEDULER_TRIGGER_TIME_MISSING",
                message="The scheduler evidence did not include last_triggered_at.",
                evidence=[{"source": "scheduler_evidence.last_triggered_at", "observed": None, "expected": "timestamp"}],
            )
        )


def _check_runtime(runtime: dict[str, Any], *, findings: list[dict[str, Any]], categories: list[str]) -> None:
    summary = _dict(runtime.get("summary"))
    freshness = _dict(runtime.get("freshness"))
    latest_run_selection = _dict(runtime.get("latest_run_selection"))
    latest_scanned_selection = _dict(runtime.get("latest_scanned_run_selection"))

    if freshness.get("stale") is True:
        categories.append("runtime_failed")
        findings.append(
            _finding(
                severity="fail",
                category="runtime_failed",
                code="RUNTIME_OUTPUT_STALE",
                message="Runtime output is stale relative to the ai-cofunder freshness threshold.",
                evidence=[
                    {"source": "runtime_status.freshness.status", "observed": freshness.get("status"), "expected": "fresh"},
                    {"source": "runtime_status.freshness.age_seconds", "observed": freshness.get("age_seconds"), "expected": f"<= {freshness.get('max_age_minutes')} minutes"},
                ],
            )
        )

    if latest_run_selection.get("found") is False:
        categories.append("runtime_failed")
        findings.append(
            _finding(
                severity="fail",
                category="runtime_failed",
                code="LATEST_RUN_MISSING",
                message="AI Cofunder could not find the latest runtime run directory.",
                evidence=[{"source": "runtime_status.latest_run_selection", "observed": latest_run_selection, "expected": "found=true"}],
            )
        )

    if latest_scanned_selection.get("found") is False:
        categories.append("insufficient_evidence")
        findings.append(
            _finding(
                severity="warn",
                category="insufficient_evidence",
                code="LATEST_SCANNED_RUN_MISSING",
                message="No scanned run was found; strategy and scan-quality checks have limited evidence.",
                evidence=[{"source": "runtime_status.latest_scanned_run_selection", "observed": latest_scanned_selection, "expected": "found=true"}],
            )
        )

    tick_metrics = _json_payload(_nested(runtime, "latest_run", "state", "tick_metrics"))
    scheduler = _dict(tick_metrics.get("scheduler_decision"))
    if scheduler.get("should_run_scan") is False:
        reason = str(scheduler.get("reason") or tick_metrics.get("reason") or "")
        categories.append("expected_skip")
        findings.append(
            _finding(
                severity="info",
                category="expected_skip",
                code="SCHEDULER_EXPECTED_SKIP",
                message="The runtime scheduler decided not to scan.",
                evidence=[
                    {"source": "latest_run.state.tick_metrics.scheduler_decision.should_run_scan", "observed": False, "expected": "false only when schedule/market rules require skip"},
                    {"source": "latest_run.state.tick_metrics.scheduler_decision.reason", "observed": reason, "expected": "explicit skip reason"},
                ],
            )
        )

    account_failures = _account_failures(tick_metrics)
    if account_failures:
        categories.append("partial_account_failure")
        findings.append(
            _finding(
                severity="fail",
                category="partial_account_failure",
                code="ACCOUNT_FAILURES",
                message="One or more accounts reported failed or error status in tick metrics.",
                evidence=[{"source": "latest_run.state.tick_metrics.accounts", "observed": account_failures, "expected": "all accounts ok or skipped"}],
            )
        )

    if summary.get("ok") is False:
        categories.append("runtime_failed")
        findings.append(
            _finding(
                severity="warn",
                category="runtime_failed",
                code="RUNTIME_STATUS_WARNINGS",
                message="runtime_status summary is not ok.",
                evidence=[{"source": "runtime_status.summary", "observed": summary, "expected": "ok=true"}],
            )
        )


def _check_prefetch(runtime: dict[str, Any], *, findings: list[dict[str, Any]], categories: list[str]) -> None:
    prefetch = _dict(runtime.get("latest_scanned_run_required_data_prefetch") or runtime.get("required_data_prefetch"))
    if not prefetch:
        return
    errors = _as_int_or_none(prefetch.get("total_errors"))
    if errors is not None and errors > 0:
        categories.append("data_source_issue")
        findings.append(
            _finding(
                severity="fail",
                category="data_source_issue",
                code="PREFETCH_ERRORS",
                message="Required-data prefetch reported errors.",
                evidence=[{"source": "runtime_status.required_data_prefetch.total_errors", "observed": errors, "expected": 0}],
            )
        )


def _check_notification(runtime: dict[str, Any], *, findings: list[dict[str, Any]], categories: list[str]) -> None:
    diagnosis = _dict(runtime.get("notification_diagnosis"))
    status = str(diagnosis.get("status") or "").strip().lower()
    if status in {"failed", "unconfirmed", "partial_failed", "notification_route_missing"}:
        categories.append("notification_issue")
        findings.append(
            _finding(
                severity="fail",
                category="notification_issue",
                code="NOTIFICATION_DELIVERY_ISSUE",
                message="Notification diagnosis indicates delivery or route failure.",
                evidence=[{"source": "runtime_status.notification_diagnosis", "observed": diagnosis, "expected": "sent or no_notification_content"}],
            )
        )


def _check_position_maintenance(runtime: dict[str, Any], *, findings: list[dict[str, Any]], categories: list[str]) -> None:
    sync_status = _nested(runtime, "summary", "option_positions_feishu_sync_status")
    sync_receipt_status = _nested(runtime, "summary", "option_positions_feishu_sync_receipt_status")
    bad_values = {"failed", "error", "unconfirmed"}
    if str(sync_status or "").lower() in bad_values or str(sync_receipt_status or "").lower() in bad_values:
        categories.append("position_maintenance_issue")
        findings.append(
            _finding(
                severity="warn",
                category="position_maintenance_issue",
                code="OPTION_POSITION_SYNC_ISSUE",
                message="Option-position Feishu sync or receipt reported a problem.",
                evidence=[
                    {"source": "runtime_status.summary.option_positions_feishu_sync_status", "observed": sync_status, "expected": "applied or skipped"},
                    {"source": "runtime_status.summary.option_positions_feishu_sync_receipt_status", "observed": sync_receipt_status, "expected": "sent or skipped"},
                ],
            )
        )


def _check_trade_intake(runtime: dict[str, Any], *, findings: list[dict[str, Any]], categories: list[str]) -> None:
    summary = _dict(_nested(runtime, "trade_intake", "summary"))
    failed_count = _as_int_or_none(summary.get("failed_count")) or 0
    unresolved_count = _as_int_or_none(summary.get("unresolved_count")) or 0
    if failed_count > 0 or unresolved_count > 0:
        categories.append("position_maintenance_issue")
        findings.append(
            _finding(
                severity="warn",
                category="position_maintenance_issue",
                code="TRADE_INTAKE_PENDING_FAILURES",
                message="Trade intake has failed or unresolved deal ids.",
                evidence=[{"source": "runtime_status.trade_intake.summary", "observed": summary, "expected": "failed_count=0 and unresolved_count=0"}],
            )
        )


def _primary_category(findings: list[dict[str, Any]], categories: list[str]) -> str:
    fail = [item for item in findings if item.get("severity") == "fail"]
    warn = [item for item in findings if item.get("severity") == "warn"]
    if fail:
        return str(fail[0].get("category") or "runtime_failed")
    if warn:
        return str(warn[0].get("category") or "insufficient_evidence")
    if "expected_skip" in categories:
        return "expected_skip"
    return "ok"


def _overall_status(findings: list[dict[str, Any]]) -> str:
    severity = max((SEVERITY_ORDER.get(str(item.get("severity") or "info"), 0) for item in findings), default=0)
    if severity >= 2:
        return "fail"
    if severity >= 1:
        return "warn"
    return "ok"


def _issue_candidates(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in findings:
        category = str(item.get("category") or "")
        if item.get("severity") == "fail" and category in {"runtime_failed", "partial_account_failure", "data_source_issue", "notification_issue"}:
            out.append(
                {
                    "create_issue": False,
                    "category": category,
                    "basis": item.get("code"),
                    "reason": "Needs AI or human triage before filing as a code bug.",
                }
            )
    return out


def _finding(*, severity: str, category: str, code: str, message: str, evidence: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "severity": severity,
        "category": category,
        "code": code,
        "message": message,
        "evidence": evidence,
    }


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _json_payload(file_info: Any) -> dict[str, Any]:
    info = _dict(file_info)
    payload = info.get("json")
    return payload if isinstance(payload, dict) else {}


def _nested(payload: Any, *keys: str) -> Any:
    cur = payload
    for key in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _as_int_or_none(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except Exception:
        return None


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "timeout", "timed_out"}


def _account_failures(tick_metrics: dict[str, Any]) -> list[dict[str, Any]]:
    raw = tick_metrics.get("accounts")
    if isinstance(raw, dict):
        items = [{"account": account, **_dict(value)} for account, value in raw.items()]
    elif isinstance(raw, list):
        items = [_dict(value) for value in raw]
    else:
        items = []
    failures: list[dict[str, Any]] = []
    for item in items:
        status = str(item.get("status") or item.get("last_status") or "").strip().lower()
        reason = str(item.get("reason") or item.get("error") or "").strip().lower()
        if status in {"failed", "fail", "error"} or "traceback" in reason or "exception" in reason:
            failures.append(item)
    return failures
