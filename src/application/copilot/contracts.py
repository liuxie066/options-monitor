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
    execution_environment: str = "local"
    debug_overrides: dict[str, Any] = field(default_factory=dict)


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
class CapabilityHintDefinition:
    capability: str
    activation_terms: tuple[tuple[str, ...], ...] = ()
    activation_reason: str | None = None
    required_scope: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()


@dataclass(frozen=True)
class SceneDefinition:
    name: str
    capabilities: tuple[str, ...]
    required_scope: tuple[str, ...]
    allowed_tools: tuple[str, ...]
    environments: tuple[str, ...]
    phase_readiness: str
    requires_answer_synthesis: bool = False
    requires_recommendations: bool = False
    allow_mock_observations: bool = False
    mock_environments: tuple[str, ...] = ()
    fixture_ids: tuple[str, ...] = ()
    capability_hints: tuple[CapabilityHintDefinition, ...] = ()
    task_guidance: tuple[str, ...] = ()
    answer_dimensions: tuple[str, ...] = ()
    tool_static_payloads: dict[str, dict[str, Any]] = field(default_factory=dict)
    output_schema: dict[str, Any] = field(default_factory=lambda: {"type": "AnswerReport"})


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


@dataclass(frozen=True)
class AppEvent:
    event_id: str
    run_id: str
    type: str
    timestamp: str
    payload: dict[str, Any]
    visible_ref: str | None = None


@dataclass(frozen=True)
class AnswerReport:
    conclusion: str
    attempted_checks: list[str] = field(default_factory=list)
    findings: list[dict[str, Any]] = field(default_factory=list)
    recommendations: list[dict[str, Any]] = field(default_factory=list)
    missing_data: list[str] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class AppResult:
    status: str
    user_response: str = ""
    answer_report: AnswerReport | None = None
    request_id: str | None = None
    contract_id: str | None = None
    run_id: str | None = None
    events: list[AppEvent] = field(default_factory=list)
    decision_trace: dict[str, Any] = field(default_factory=dict)
    ok: bool = True


def to_payload(result: AppResult, *, include_events: bool = False) -> dict[str, Any]:
    payload = asdict(result)
    if not include_events:
        payload["event_count"] = len(result.events)
        payload.pop("events", None)
    payload["ok"] = bool(result.ok)
    return payload
