from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.application.agent_tool_contracts import AgentToolError


TASK_FRAME_SCHEMA_VERSION = "om-copilot-task-frame-v1"
TASK_KINDS = {"analysis", "diagnosis", "lookup", "comparison", "unknown"}


@dataclass(frozen=True)
class TaskFrame:
    task_id: str
    user_goal: str
    task_kind: str
    scope: dict[str, Any]
    constraints: dict[str, Any]
    answer_shape: dict[str, Any]
    missing_slots: tuple[str, ...]

    def public_payload(self) -> dict[str, Any]:
        return {
            "schema_version": TASK_FRAME_SCHEMA_VERSION,
            "task_id": self.task_id,
            "user_goal": self.user_goal,
            "task_kind": self.task_kind,
            "scope": dict(self.scope),
            "constraints": dict(self.constraints),
            "answer_shape": dict(self.answer_shape),
            "missing_slots": list(self.missing_slots),
        }


def task_frame_from_model(payload: dict[str, Any], *, task_id: str, user_text: str, config_key: str | None) -> TaskFrame:
    if not isinstance(payload, dict):
        raise AgentToolError(code="COPILOT_MODEL_ERROR", message="task frame model output must be an object")
    task_kind = str(payload.get("task_kind") or "unknown").strip().lower()
    if task_kind not in TASK_KINDS:
        task_kind = "unknown"
    scope = _dict(payload.get("scope"))
    if config_key and not str(scope.get("market") or "").strip():
        scope["market"] = str(config_key).strip().lower()
    constraints = {
        "read_only": True,
        "allow_realtime_quote_refresh": False,
        "allow_write_preview": False,
        **_dict(payload.get("constraints")),
    }
    constraints["read_only"] = True
    constraints["allow_write_preview"] = False
    answer_shape = {
        "requires_conclusion": True,
        "requires_recommendations": False,
        "requires_evidence": True,
        "allow_table": True,
        **_dict(payload.get("answer_shape")),
    }
    return TaskFrame(
        task_id=task_id,
        user_goal=str(payload.get("user_goal") or user_text).strip(),
        task_kind=task_kind,
        scope=scope,
        constraints=constraints,
        answer_shape=answer_shape,
        missing_slots=tuple(str(item).strip() for item in payload.get("missing_slots") or [] if str(item).strip()),
    )


def task_frame_instructions() -> str:
    return (
        "You frame an options-monitor user request for a read-only Copilot task. "
        "Return JSON only. Do not choose tools. Do not invent symbols, accounts, or months. "
        "Use the supplied request date to resolve relative month words. "
        "Free-form write/config/notification/broker changes must keep read_only=true and list the blocked intent in missing_slots."
    )


def task_frame_json_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["schema_version", "user_goal", "task_kind", "scope", "constraints", "answer_shape", "missing_slots"],
        "properties": {
            "schema_version": {"type": "string", "const": TASK_FRAME_SCHEMA_VERSION},
            "user_goal": {"type": "string"},
            "task_kind": {"type": "string", "enum": sorted(TASK_KINDS)},
            "scope": {
                "type": "object",
                "additionalProperties": True,
                "properties": {
                    "market": {"type": ["string", "null"]},
                    "accounts": {"type": "array", "items": {"type": "string"}},
                    "symbols": {"type": "array", "items": {"type": "string"}},
                    "months": {"type": "array", "items": {"type": "string"}},
                    "date_range": {"type": ["object", "null"], "additionalProperties": True},
                },
            },
            "constraints": {
                "type": "object",
                "additionalProperties": True,
                "properties": {
                    "read_only": {"type": "boolean"},
                    "allow_realtime_quote_refresh": {"type": "boolean"},
                    "allow_write_preview": {"type": "boolean"},
                },
            },
            "answer_shape": {
                "type": "object",
                "additionalProperties": True,
                "properties": {
                    "requires_conclusion": {"type": "boolean"},
                    "requires_recommendations": {"type": "boolean"},
                    "requires_evidence": {"type": "boolean"},
                    "allow_table": {"type": "boolean"},
                },
            },
            "missing_slots": {"type": "array", "items": {"type": "string"}},
        },
    }


def _dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


__all__ = [
    "TASK_FRAME_SCHEMA_VERSION",
    "TaskFrame",
    "task_frame_from_model",
    "task_frame_instructions",
    "task_frame_json_schema",
]
