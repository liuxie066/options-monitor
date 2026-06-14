from __future__ import annotations

from dataclasses import dataclass
from typing import Any


HOOK_RESULT_SCHEMA_VERSION = "om-agent-hook-result-v1"


@dataclass(frozen=True)
class HookResult:
    hook: str
    stage: str
    status: str
    code: str
    message: str = ""
    impact: str = ""
    recoverable: bool = False
    recoverable_by: str | None = None
    evidence_refs: tuple[str, ...] = ()
    schema_version: str = HOOK_RESULT_SCHEMA_VERSION

    def public_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": self.schema_version,
            "hook": self.hook,
            "stage": self.stage,
            "status": self.status,
            "code": self.code,
            "message": self.message,
            "impact": self.impact,
            "recoverable": bool(self.recoverable),
            "evidence_refs": list(self.evidence_refs),
        }
        if self.recoverable_by:
            payload["recoverable_by"] = self.recoverable_by
        return {key: value for key, value in payload.items() if value not in ("", [], None)}


def hook_results_from_tool_check(check: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(check, dict):
        return []
    stage = str(check.get("stage") or "tool")
    tool_name = str(check.get("tool_name") or "")
    out: list[dict[str, Any]] = []
    for item in check.get("checks") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        raw_status = str(item.get("status") or "").strip()
        if not name:
            continue
        out.append(
            HookResult(
                hook=name,
                stage=stage,
                status=_hook_status(raw_status),
                code=str(item.get("code") or raw_status or "unknown"),
                message=str(item.get("reason") or "").strip(),
                impact=_tool_check_impact(name=name, raw_status=raw_status, item=item),
                recoverable=_tool_check_recoverable(name=name, raw_status=raw_status),
                recoverable_by=_tool_check_recoverable_by(name=name, raw_status=raw_status),
                evidence_refs=tuple(ref for ref in (f"tool:{tool_name}",) if ref != "tool:"),
            ).public_payload()
        )
    return out


def hook_results_from_coverage(coverage: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(coverage, dict):
        return []
    status = str(coverage.get("status") or "").strip()
    gaps = [item for item in coverage.get("gaps") or [] if isinstance(item, dict)]
    missing = [str(item) for item in coverage.get("missing") or [] if str(item).strip()]
    recoverable_by = _first_recoverable_by(gaps)
    return [
        HookResult(
            hook="coverage",
            stage="answer",
            status=_coverage_hook_status(status),
            code=status or "unknown",
            message="coverage verifier result",
            impact=_coverage_impact(status=status, missing=missing, gaps=gaps),
            recoverable=bool(recoverable_by),
            recoverable_by=recoverable_by,
            evidence_refs=tuple(_coverage_refs(gaps)),
        ).public_payload()
    ]


def hook_results_from_answer_trace(
    *,
    synthesis_trace: dict[str, Any] | None,
    final_response: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    trace = synthesis_trace if isinstance(synthesis_trace, dict) else {}
    final = final_response if isinstance(final_response, dict) else {}
    out = [
        HookResult(
            hook="final_response",
            stage="answer",
            status="pass",
            code=str(final.get("status") or "unknown"),
            message=str(final.get("reason") or "").strip(),
            impact="final response route selected",
        ).public_payload()
    ]
    answer_guard = trace.get("answer_guard")
    if isinstance(answer_guard, dict):
        guard_status = str(answer_guard.get("status") or "").strip()
        out.append(
            HookResult(
                hook="answer_guard",
                stage="answer",
                status=_answer_guard_hook_status(guard_status),
                code=guard_status or "unknown",
                message="answer guard result",
                impact=_answer_guard_impact(answer_guard),
                recoverable=guard_status == "failed_then_rewritten",
                recoverable_by="rewrite" if guard_status == "failed_then_rewritten" else None,
                evidence_refs=tuple(_answer_guard_refs(answer_guard)),
            ).public_payload()
        )
    return out


def _hook_status(raw_status: str) -> str:
    normalized = str(raw_status or "").strip()
    if normalized in {"pass", "not_applicable"}:
        return "pass"
    if normalized in {"warning", "not_declared"}:
        return "warning"
    if normalized == "recoverable_gap":
        return "recoverable_gap"
    if normalized == "deny":
        return "deny"
    if normalized in {"fail", "ask", "suspicious"}:
        return "fail"
    return "warning" if normalized else "pass"


def _tool_check_impact(*, name: str, raw_status: str, item: dict[str, Any]) -> str:
    if raw_status in {"pass", "not_applicable"}:
        return ""
    if name == "action_policy":
        return "tool authorization did not allow execution"
    if name == "action_safety":
        return "tool call does not match current task effect or scope"
    if name == "planner_argument_guard":
        return "planner supplied host-controlled arguments"
    if name == "scope_guard":
        return str(item.get("reason") or "tool call scope is outside task scope")
    if name == "write_guard":
        return "tool effect is outside the allowed write boundary"
    if name == "freshness":
        return "freshness-sensitive facts may not be current"
    if name == "missing_data":
        count = item.get("count")
        return f"tool reported missing data count {count}" if count is not None else "tool reported missing data"
    if name == "output_contract":
        return "tool output contract is missing or incomplete"
    if name == "evidence_contract":
        missing = item.get("missing_fields")
        return f"evidence contract missing fields: {missing}" if missing else "evidence contract warning"
    if name == "result_status":
        return "tool returned a failed result"
    return f"{name} returned {raw_status}"


def _tool_check_recoverable(*, name: str, raw_status: str) -> bool:
    return name in {"freshness", "missing_data"} and raw_status == "warning"


def _tool_check_recoverable_by(*, name: str, raw_status: str) -> str | None:
    if name == "freshness" and raw_status == "warning":
        return "refresh_or_requery"
    if name == "missing_data" and raw_status == "warning":
        return "bounded_followup"
    return None


def _coverage_hook_status(status: str) -> str:
    if status == "complete":
        return "pass"
    if status == "recoverable_gap":
        return "recoverable_gap"
    if status == "unrecoverable_gap":
        return "fail"
    return "warning" if status else "pass"


def _coverage_impact(*, status: str, missing: list[str], gaps: list[dict[str, Any]]) -> str:
    if status == "complete":
        return ""
    if gaps:
        kinds = ", ".join(str(item.get("kind") or "") for item in gaps if str(item.get("kind") or "").strip())
        return f"coverage gaps: {kinds}" if kinds else "coverage gaps remain"
    if missing:
        return "missing required answer keys: " + ", ".join(missing)
    return f"coverage status is {status}"


def _first_recoverable_by(gaps: list[dict[str, Any]]) -> str | None:
    for gap in gaps:
        if gap.get("recoverable") is False:
            continue
        text = str(gap.get("recoverable_by") or "").strip()
        if text:
            return text
    return None


def _coverage_refs(gaps: list[dict[str, Any]]) -> list[str]:
    refs: list[str] = []
    for gap in gaps[:4]:
        kind = str(gap.get("kind") or "").strip()
        if kind:
            refs.append(f"coverage_gap:{kind}")
    return refs


def _answer_guard_hook_status(status: str) -> str:
    if status == "passed":
        return "pass"
    if status == "failed_then_rewritten":
        return "warning"
    if status == "failed_then_fallback":
        return "fail"
    return "warning" if status else "pass"


def _answer_guard_impact(answer_guard: dict[str, Any]) -> str:
    violations = _answer_guard_refs(answer_guard)
    if violations:
        return "answer guard violations: " + ", ".join(violations[:4])
    return ""


def _answer_guard_refs(answer_guard: dict[str, Any]) -> list[str]:
    refs: list[str] = []
    for key in ("violations", "retry_violations"):
        values = answer_guard.get(key)
        if not isinstance(values, list):
            continue
        for item in values:
            if not isinstance(item, dict):
                continue
            violation_type = str(item.get("type") or "").strip()
            if violation_type:
                refs.append(f"{key}:{violation_type}")
    return refs


__all__ = [
    "HOOK_RESULT_SCHEMA_VERSION",
    "HookResult",
    "hook_results_from_answer_trace",
    "hook_results_from_coverage",
    "hook_results_from_tool_check",
]
