from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.application.assistant.task_runtime import AgentTask


EVIDENCE_PLAN_SCHEMA_VERSION = "om-agent-evidence-plan-v1"


@dataclass(frozen=True)
class EvidenceCall:
    tool_name: str
    arguments: dict[str, Any]
    purpose: str

    def public_payload(self) -> dict[str, Any]:
        return {
            "tool_name": self.tool_name,
            "arguments": dict(self.arguments),
            "purpose": self.purpose,
        }


@dataclass(frozen=True)
class EvidencePlan:
    task_name: str
    calls: tuple[EvidenceCall, ...]
    required_views: tuple[str, ...]
    schema_version: str = EVIDENCE_PLAN_SCHEMA_VERSION

    def public_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "task_name": self.task_name,
            "calls": [call.public_payload() for call in self.calls],
            "required_views": list(self.required_views),
        }


def plan_task_evidence(task: AgentTask) -> EvidencePlan:
    views: list[str] = []
    calls: list[EvidenceCall] = []
    for profile in task.profiles:
        profile_views = _unique(profile.required_views)
        views.extend(profile_views)
        if profile_views:
            calls.append(
                EvidenceCall(
                    tool_name=profile.tool_name,
                    arguments=_analysis_arguments(task=task, views=profile_views),
                    purpose=f"read {profile.name} evidence",
                )
            )
    return EvidencePlan(
        task_name=task.name,
        calls=tuple(calls),
        required_views=_unique(views),
    )


def _analysis_arguments(*, task: AgentTask, views: tuple[str, ...]) -> dict[str, Any]:
    months = [str(item).strip() for item in task.scope.get("requested_months", []) if str(item).strip()]
    return {
        "views": list(views),
        "month": months[0] if len(months) == 1 else None,
        "months": months if len(months) > 1 else [],
        "limit": 200,
    }


def _unique(values: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return tuple(out)


__all__ = ["EVIDENCE_PLAN_SCHEMA_VERSION", "EvidenceCall", "EvidencePlan", "plan_task_evidence"]
