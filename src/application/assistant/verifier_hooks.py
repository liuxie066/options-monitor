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
    if name == "planner_argument_guard":
        return "tool call supplied host-controlled arguments"
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


__all__ = [
    "HOOK_RESULT_SCHEMA_VERSION",
    "HookResult",
    "hook_results_from_tool_check",
]
