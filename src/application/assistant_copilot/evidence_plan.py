from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.application.agent_tool_contracts import AgentToolError
from src.application.agent_tool_registry import get_tool_definition


EVIDENCE_PLAN_SCHEMA_VERSION = "om-copilot-evidence-plan-v1"
COPILOT_READ_TOOLS: tuple[str, ...] = (
    "analysis_catalog",
    "analysis_query",
    "monthly_income_report",
    "option_positions_read",
    "runtime_status",
)


@dataclass(frozen=True)
class EvidencePlanStep:
    tool_name: str
    purpose: str
    payload: dict[str, Any]

    def public_payload(self) -> dict[str, Any]:
        return {
            "tool_name": self.tool_name,
            "purpose": self.purpose,
            "payload": dict(self.payload),
        }


@dataclass(frozen=True)
class EvidencePlan:
    steps: tuple[EvidencePlanStep, ...]
    expected_evidence: tuple[str, ...]

    def public_payload(self) -> dict[str, Any]:
        return {
            "schema_version": EVIDENCE_PLAN_SCHEMA_VERSION,
            "steps": [step.public_payload() for step in self.steps],
            "expected_evidence": list(self.expected_evidence),
        }


def evidence_plan_from_model(
    payload: dict[str, Any],
    *,
    config_key: str | None,
    config_path: str | None,
    max_steps: int,
) -> EvidencePlan:
    if not isinstance(payload, dict):
        raise AgentToolError(code="COPILOT_MODEL_ERROR", message="evidence plan model output must be an object")
    raw_steps = payload.get("steps")
    if not isinstance(raw_steps, list):
        raise AgentToolError(code="COPILOT_MODEL_ERROR", message="evidence plan steps must be a list")

    steps: list[EvidencePlanStep] = []
    for raw_step in raw_steps[: max(0, int(max_steps))]:
        step = _coerce_step(raw_step, config_key=config_key, config_path=config_path)
        steps.append(step)
    expected = tuple(str(item).strip() for item in payload.get("expected_evidence") or [] if str(item).strip())
    return EvidencePlan(steps=tuple(steps), expected_evidence=expected)


def read_tool_manifest() -> list[dict[str, Any]]:
    manifest: list[dict[str, Any]] = []
    for name in COPILOT_READ_TOOLS:
        definition = get_tool_definition(name)
        if definition is None:
            continue
        item = definition.to_manifest()
        manifest.append({
            "name": item.get("name"),
            "description": item.get("description"),
            "capabilities": item.get("capabilities"),
            "input_schema": item.get("input_schema"),
            "input_json_schema": item.get("input_json_schema"),
            "planner_notes": item.get("planner_notes"),
            "planner_semantics": item.get("planner_semantics"),
            "output_contract": item.get("output_contract"),
            "read_only": item.get("read_only"),
            "risk_level": item.get("risk_level"),
        })
    return manifest


def validate_read_tool(tool_name: str) -> None:
    name = str(tool_name or "").strip()
    if name not in COPILOT_READ_TOOLS:
        raise AgentToolError(
            code="PERMISSION_DENIED",
            message=f"Copilot v2 may only call approved read-only tools; rejected tool: {name}",
            details={"allowed_tools": list(COPILOT_READ_TOOLS), "tool_name": name},
        )
    definition = get_tool_definition(name)
    if definition is None or not definition.is_pure_read():
        raise AgentToolError(
            code="PERMISSION_DENIED",
            message=f"Copilot v2 tool is not registry-declared pure read: {name}",
            details={"tool_name": name},
        )


def evidence_plan_instructions() -> str:
    return (
        "You plan read-only evidence collection for OM Copilot. Return JSON only. "
        "Choose only tools from the supplied manifest. Use analysis_catalog metadata when writing analysis_query SQL. "
        "Do not use fixed business recipes or hidden assumptions. Do not request write/config/notification/broker/service tools."
    )


def evidence_plan_json_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["schema_version", "steps", "expected_evidence"],
        "properties": {
            "schema_version": {"type": "string", "const": EVIDENCE_PLAN_SCHEMA_VERSION},
            "steps": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["tool_name", "purpose", "payload"],
                    "properties": {
                        "tool_name": {"type": "string", "enum": list(COPILOT_READ_TOOLS)},
                        "purpose": {"type": "string"},
                        "payload": {"type": "object", "additionalProperties": True},
                    },
                },
            },
            "expected_evidence": {"type": "array", "items": {"type": "string"}},
        },
    }


def _coerce_step(raw_step: Any, *, config_key: str | None, config_path: str | None) -> EvidencePlanStep:
    if not isinstance(raw_step, dict):
        raise AgentToolError(code="COPILOT_MODEL_ERROR", message="evidence plan step must be an object")
    tool_name = str(raw_step.get("tool_name") or "").strip()
    validate_read_tool(tool_name)
    payload = dict(raw_step.get("payload") or {}) if isinstance(raw_step.get("payload"), dict) else {}
    if config_key and not str(payload.get("config_key") or "").strip():
        payload["config_key"] = config_key
    if config_path and not str(payload.get("config_path") or "").strip():
        payload["config_path"] = config_path
    return EvidencePlanStep(
        tool_name=tool_name,
        purpose=str(raw_step.get("purpose") or tool_name).strip(),
        payload=payload,
    )


__all__ = [
    "COPILOT_READ_TOOLS",
    "EVIDENCE_PLAN_SCHEMA_VERSION",
    "EvidencePlan",
    "EvidencePlanStep",
    "evidence_plan_from_model",
    "evidence_plan_instructions",
    "evidence_plan_json_schema",
    "read_tool_manifest",
    "validate_read_tool",
]
