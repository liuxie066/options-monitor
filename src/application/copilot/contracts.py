from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4


COPILOT_SAFE_ERROR_CODES = {
    "BUDGET_EXHAUSTED",
    "CANCELLED",
    "CONFIG_ERROR",
    "CONFIRMATION_REQUIRED",
    "DEPENDENCY_MISSING",
    "FIXTURE_ERROR",
    "INPUT_ERROR",
    "INTERNAL_ERROR",
    "INVALID_ACTION",
    "MODEL_ACTION_INVALID",
    "MODEL_ERROR",
    "MODEL_REQUIRED",
    "NEEDS_CLARIFICATION",
    "OBSERVATION_ERROR",
    "PERMISSION_DENIED",
    "POLICY_ERROR",
    "READ_ERROR",
    "TOOL_ERROR",
    "TOOL_EXCEPTION",
}


def safe_error_code(value: Any, *, default: str) -> str:
    if not isinstance(value, str):
        return default
    text = value.strip().upper()
    if not text:
        return default
    return text if text in COPILOT_SAFE_ERROR_CODES else "TOOL_ERROR"


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class CopilotScope:
    config_key: str | None = None
    symbol: str | None = None
    month: str | None = None


@dataclass(frozen=True)
class CopilotRequest:
    request_id: str
    source_entry: str
    user_message: str
    explicit_scope: CopilotScope = field(default_factory=CopilotScope)
    context_messages: tuple[dict[str, Any], ...] = ()
    execution_environment: str = "local"
    debug_overrides: dict[str, Any] = field(default_factory=dict)
    trusted_tool_scope: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ExecutionContract:
    contract_id: str
    request_id: str
    scene_name: str
    execution_environment: str
    input: dict[str, Any]
    policy: dict[str, Any]
    decision_trace: dict[str, Any]


@dataclass(frozen=True)
class SceneManifest:
    run_id: str
    scene_name: str
    execution_environment: str
    messages: list[dict[str, Any]]
    allowed_tools: list[str]
    limits: dict[str, Any]
    output_schema: dict[str, Any]
    task_guidance: dict[str, Any] = field(default_factory=dict)
    tool_descriptions: list[dict[str, Any]] = field(default_factory=list)
    tool_static_payloads: dict[str, dict[str, Any]] = field(default_factory=dict)
    scene_version: str = ""
    selected_toolsets: tuple[str, ...] = ()
    fixed_tool_input: dict[str, Any] = field(default_factory=dict)
    provenance: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AppEvent:
    event_id: str
    run_id: str
    type: str
    timestamp: str
    payload: dict[str, Any]
    visible_ref: str | None = None


@dataclass(frozen=True)
class AppResult:
    status: str
    user_response: str = ""
    error: dict[str, Any] | None = None
    request_id: str | None = None
    contract_id: str | None = None
    run_id: str | None = None
    events: list[AppEvent] = field(default_factory=list)
    decision_trace: dict[str, Any] = field(default_factory=dict)
    control_request: dict[str, Any] | None = None
    ok: bool = True


def to_payload(result: AppResult, *, include_events: bool = False) -> dict[str, Any]:
    payload = asdict(result)
    if not include_events:
        payload["event_count"] = len(result.events)
        payload.pop("events", None)
    payload["ok"] = bool(result.ok)
    return payload


def contract_to_payload(contract: ExecutionContract) -> dict[str, Any]:
    return asdict(contract)


def contract_from_payload(payload: dict[str, Any]) -> ExecutionContract:
    return ExecutionContract(
        contract_id=str(payload.get("contract_id") or ""),
        request_id=str(payload.get("request_id") or ""),
        scene_name=str(payload.get("scene_name") or ""),
        execution_environment=str(payload.get("execution_environment") or ""),
        input=dict(payload.get("input") or {}),
        policy=dict(payload.get("policy") or {}),
        decision_trace=dict(payload.get("decision_trace") or {}),
    )
