from __future__ import annotations

from dataclasses import dataclass
from typing import Any


TASK_COMPLETION_SCHEMA_VERSION = "om-agent-task-completion-v1"


@dataclass(frozen=True)
class TaskCompletion:
    status: str
    missing_views: tuple[str, ...] = ()
    missing_answer: tuple[str, ...] = ()
    next_action: str = "synthesize"
    reason: str = ""
    schema_version: str = TASK_COMPLETION_SCHEMA_VERSION

    def public_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "status": self.status,
            "missing_views": list(self.missing_views),
            "missing_answer": list(self.missing_answer),
            "next_action": self.next_action,
            "reason": self.reason,
        }


def check_task_completion(
    *,
    task: Any,
    covered_views: set[str],
    successful_tool_count: int,
) -> TaskCompletion:
    if successful_tool_count <= 0:
        return TaskCompletion(status="need_more_evidence", next_action="followup_tool", reason="no_successful_evidence")
    required_views = _task_required_views(task)
    missing = tuple(sorted(set(required_views) - {str(item).strip() for item in covered_views if str(item).strip()}))
    if missing:
        return TaskCompletion(status="need_more_evidence", missing_views=missing, next_action="followup_tool")
    return TaskCompletion(status="ready_to_synthesize")


def _task_required_views(task: Any) -> tuple[str, ...]:
    if isinstance(task, dict):
        value = task.get("required_views")
    else:
        value = getattr(task, "required_views", ())
    return tuple(str(item).strip() for item in value or [] if str(item).strip())


__all__ = ["TASK_COMPLETION_SCHEMA_VERSION", "TaskCompletion", "check_task_completion"]
