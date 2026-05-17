from __future__ import annotations

from typing import Any


def render_handoff(*, evidence: dict[str, Any], diagnosis: dict[str, Any], ai_result: dict[str, Any] | None) -> str:
    deployment = _dict(evidence.get("deployment"))
    runtime = _dict(evidence.get("runtime_status"))
    summary = _dict(runtime.get("summary"))
    scheduler = _dict(evidence.get("scheduler_evidence"))
    result = ai_result if isinstance(ai_result, dict) else {}
    ai_available = _ai_available(result)
    primary_result = result if ai_available else {}
    status = str(primary_result.get("status") or diagnosis.get("status") or "warn")
    category = str(primary_result.get("category") or diagnosis.get("category") or "insufficient_evidence")
    confidence = str(primary_result.get("confidence") or ("medium" if diagnosis.get("status") == "fail" else "low"))

    lines = [
        "## Online Doctor Conclusion",
        f"Status: {status}",
        f"Category: {category}",
        f"Confidence: {confidence}",
        "",
        "## Problem",
        _problem_text(result=primary_result, diagnosis=diagnosis),
        "",
        "## Impact",
        str(primary_result.get("impact") or _impact_from_diagnosis(diagnosis)),
        "",
        "## Online Context",
        f"- version: {deployment.get('version')}",
        f"- git_commit: {deployment.get('git_commit')}",
        f"- git_branch: {deployment.get('git_branch')}",
        f"- config_key: {deployment.get('config_key')}",
        f"- config_digest: {deployment.get('config_digest')}",
        f"- accounts: {', '.join(str(item) for item in deployment.get('accounts') or [])}",
        f"- run_id: {_run_id(runtime)}",
        f"- latest_run_path: {summary.get('latest_run_path')}",
        f"- latest_scanned_run_path: {summary.get('latest_scanned_run_path')}",
        f"- scheduler_provider: {scheduler.get('provider')}",
        f"- scheduler_job: {scheduler.get('job_name')}",
        f"- scheduler_status: {scheduler.get('last_status')}",
        f"- scheduler_exit_code: {scheduler.get('last_exit_code')}",
        f"- runtime_status: {summary.get('latest_status')}",
        "",
        "## Evidence",
    ]
    evidence_rows = _evidence_rows(result=primary_result, diagnosis=diagnosis)
    if evidence_rows:
        for row in evidence_rows:
            lines.extend(
                [
                    f"- source: {row.get('source')}",
                    f"  observed: {row.get('observed')}",
                    f"  expected: {row.get('expected')}",
                ]
            )
    else:
        lines.append("- No concrete evidence rows were produced.")

    lines.extend(
        [
            "",
        "## AI Diagnosis",
        str(result.get("ai_diagnosis") or result.get("problem") or "AI triage was not run; using deterministic doctor findings."),
        "",
        "## Strategy Evidence",
        *_strategy_evidence_lines(evidence),
        "",
        "## Strategy Improvement Directions",
        *_strategy_direction_lines(result),
        "",
        "## Suspected Code Area",
    ]
    )
    code_areas = result.get("suspected_code_area") if isinstance(result.get("suspected_code_area"), list) else []
    if code_areas:
        lines.extend(f"- {item}" for item in code_areas)
    else:
        lines.append("- Not identified.")

    lines.extend(["", "## Suggested Local Debug Steps"])
    steps = result.get("local_debug_steps") if isinstance(result.get("local_debug_steps"), list) else []
    if steps:
        lines.extend(f"{idx}. {step}" for idx, step in enumerate(steps, start=1))
    else:
        lines.extend(_default_debug_steps(diagnosis))

    issue = result.get("issue_candidate") if isinstance(result.get("issue_candidate"), dict) else {}
    if not ai_available:
        issue = {}
    if not issue:
        issue_candidates = diagnosis.get("issue_candidates") if isinstance(diagnosis.get("issue_candidates"), list) else []
        issue = issue_candidates[0] if issue_candidates and isinstance(issue_candidates[0], dict) else {"create_issue": False, "reason": "No issue candidate."}
    lines.extend(
        [
            "",
            "## Issue Candidate",
            f"create_issue: {bool(issue.get('create_issue'))}",
            f"reason: {issue.get('reason')}",
            "",
            "## Privacy",
            "This handoff is generated from redacted doctor evidence. API keys, webhooks, and long account identifiers are not included.",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _ai_available(result: dict[str, Any]) -> bool:
    return bool(result) and str(result.get("status") or "").strip().lower() != "unavailable" and str(result.get("category") or "").strip().lower() != "ai_unavailable"


def _problem_text(*, result: dict[str, Any], diagnosis: dict[str, Any]) -> str:
    if str(result.get("problem") or "").strip():
        return str(result["problem"]).strip()
    findings = diagnosis.get("findings") if isinstance(diagnosis.get("findings"), list) else []
    if findings:
        return str(_dict(findings[0]).get("message") or "Doctor found a production-quality issue.")
    return "Doctor did not find a production-quality issue."


def _impact_from_diagnosis(diagnosis: dict[str, Any]) -> str:
    status = str(diagnosis.get("status") or "warn")
    if status == "fail":
        return "Production runtime quality is degraded and needs inspection."
    if status == "warn":
        return "Production runtime quality is uncertain; review the warning evidence before treating the run as healthy."
    return "No production-quality impact was detected by deterministic checks."


def _run_id(runtime: dict[str, Any]) -> Any:
    for path in (("latest_run", "state", "last_run"), ("shared", "last_run")):
        payload = runtime
        for key in path:
            payload = _dict(payload).get(key)
        json_payload = _dict(_dict(payload).get("json"))
        if json_payload.get("run_id"):
            return json_payload.get("run_id")
    return None


def _evidence_rows(*, result: dict[str, Any], diagnosis: dict[str, Any]) -> list[dict[str, Any]]:
    rows = result.get("evidence")
    if isinstance(rows, list) and rows:
        return [_dict(row) for row in rows if isinstance(row, dict)]
    out: list[dict[str, Any]] = []
    for finding in diagnosis.get("findings") or []:
        if not isinstance(finding, dict):
            continue
        for row in finding.get("evidence") or []:
            if isinstance(row, dict):
                out.append(row)
    return out[:12]


def _strategy_evidence_lines(evidence: dict[str, Any]) -> list[str]:
    strategy = _dict(evidence.get("strategy_evidence"))
    summary = _dict(strategy.get("summary"))
    if not summary:
        return ["- No strategy evidence summary was collected."]
    return [
        f"- candidate_file_count: {summary.get('candidate_file_count')}",
        f"- candidate_row_count: {summary.get('candidate_row_count')}",
        f"- filter_trace_file_count: {summary.get('filter_trace_file_count')}",
        f"- strategy_replay_file_count: {summary.get('strategy_replay_file_count')}",
        f"- evidence_level: {summary.get('evidence_level')}",
    ]


def _strategy_direction_lines(result: dict[str, Any]) -> list[str]:
    observations = result.get("strategy_observations") if isinstance(result.get("strategy_observations"), list) else []
    directions = result.get("strategy_improvement_directions") if isinstance(result.get("strategy_improvement_directions"), list) else []
    lines: list[str] = []
    if observations:
        lines.append("Observations:")
        lines.extend(f"- {item}" for item in observations)
    if directions:
        lines.append("Directions:")
        lines.extend(f"- {item}" for item in directions)
    if not lines:
        lines.append("- AI did not provide evidence-backed strategy directions.")
    return lines


def _default_debug_steps(diagnosis: dict[str, Any]) -> list[str]:
    category = str(diagnosis.get("category") or "")
    if category == "scheduler_failed":
        return [
            "1. Inspect the online scheduler job status and stderr tail from the handoff.",
            "2. Re-run doctor with scheduler_evidence after the next scheduled run.",
        ]
    if category == "runtime_failed":
        return [
            "1. Inspect the latest run directory and tick_metrics evidence referenced above.",
            "2. Reproduce the failing path locally with focused tests before changing runtime code.",
        ]
    return [
        "1. Inspect the deterministic findings and evidence rows above.",
        "2. Decide whether this is a runtime bug, an operations issue, or expected behavior.",
    ]
