from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

from src.application.agent_tool_contracts import AgentToolError


MODEL_EVENT_SCHEMA_VERSION = "om-assistant-model-event-v1"
TOOL_RESULT_ADAPTER_SCHEMA_VERSION = "om-assistant-tool-result-adapter-v1"
MODEL_OBSERVATION_SCHEMA_VERSION = "om-assistant-model-observation-v1"
EVIDENCE_DELTA_SCHEMA_VERSION = "om-assistant-evidence-delta-v1"
TRACE_PAYLOAD_SCHEMA_VERSION = "om-assistant-tool-trace-payload-v1"
PROVIDER_TOOL_SCHEMA_VERSION = "om-assistant-provider-tool-schema-v1"
CLARIFICATION_REQUEST_SCHEMA_VERSION = "om-agent-clarification-request-v1"

SYSTEM_SCOPED_TOOL_ARGUMENTS = frozenset(
    {
        "audit_db",
        "config_key",
        "config_path",
        "data_config",
        "logs_root",
        "opend_telnet_host",
        "output_dir",
        "report_path",
        "run_dir",
        "state",
        "state_dir",
        "timeout_sec",
        "trigger",
    }
)

ModelEventType = Literal[
    "user_message",
    "context_projected",
    "model_tool_call",
    "tool_guard_decision",
    "tool_result",
    "evidence_updated",
    "model_final_answer",
    "clarification_request",
    "preview_request",
    "loop_stopped",
]


@dataclass(frozen=True)
class AssistantEvent:
    event_id: str
    event_type: ModelEventType
    payload: dict[str, Any]
    parent_event_id: str | None = None
    schema_version: str = MODEL_EVENT_SCHEMA_VERSION

    def public_payload(self) -> dict[str, Any]:
        payload = {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "event_type": self.event_type,
            "payload": _copy_mapping(self.payload),
        }
        if self.parent_event_id:
            payload["parent_event_id"] = self.parent_event_id
        return payload


@dataclass(frozen=True)
class ModelToolCallEvent:
    event_id: str
    tool_call_id: str
    tool_name: str
    arguments: dict[str, Any]
    purpose: str = ""
    provider: str | None = None
    parent_event_id: str | None = None
    protocol_error: dict[str, Any] | None = None
    schema_version: str = MODEL_EVENT_SCHEMA_VERSION

    @property
    def event_type(self) -> Literal["model_tool_call"]:
        return "model_tool_call"

    def public_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "event_type": self.event_type,
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "arguments": _copy_mapping(self.arguments),
        }
        if self.purpose:
            payload["purpose"] = self.purpose
        if self.provider:
            payload["provider"] = self.provider
        if self.parent_event_id:
            payload["parent_event_id"] = self.parent_event_id
        if isinstance(self.protocol_error, dict) and self.protocol_error:
            payload["protocol_error"] = _copy_mapping(self.protocol_error)
        return payload


@dataclass(frozen=True)
class ToolGuardDecisionEvent:
    event_id: str
    tool_call_id: str
    tool_name: str
    allowed: bool
    decision: str
    reason: str
    risk_class: str
    scope_source: str
    normalized_payload: dict[str, Any]
    duplicate_signature: str | None = None
    error_code: str | None = None
    parent_event_id: str | None = None
    schema_version: str = MODEL_EVENT_SCHEMA_VERSION

    @property
    def event_type(self) -> Literal["tool_guard_decision"]:
        return "tool_guard_decision"

    def public_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "event_type": self.event_type,
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "allowed": bool(self.allowed),
            "decision": self.decision,
            "reason": self.reason,
            "risk_class": self.risk_class,
            "scope_source": self.scope_source,
            "normalized_payload": _copy_mapping(self.normalized_payload),
        }
        if self.duplicate_signature:
            payload["duplicate_signature"] = self.duplicate_signature
        if self.error_code:
            payload["error_code"] = self.error_code
        if self.parent_event_id:
            payload["parent_event_id"] = self.parent_event_id
        return payload


@dataclass(frozen=True)
class ToolResultEvent:
    event_id: str
    tool_call_id: str
    tool_name: str
    ok: bool
    observation: dict[str, Any]
    evidence_delta: dict[str, Any]
    trace_payload: dict[str, Any]
    missing_data: tuple[dict[str, Any], ...] = ()
    conflicts: tuple[dict[str, Any], ...] = ()
    error_code: str | None = None
    parent_event_id: str | None = None
    schema_version: str = MODEL_EVENT_SCHEMA_VERSION

    @property
    def event_type(self) -> Literal["tool_result"]:
        return "tool_result"

    def public_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "event_type": self.event_type,
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "ok": bool(self.ok),
            "observation": _copy_mapping(self.observation),
            "evidence_delta": _copy_mapping(self.evidence_delta),
            "trace_payload": _copy_mapping(self.trace_payload),
            "missing_data": [_copy_mapping(item) for item in self.missing_data],
            "conflicts": [_copy_mapping(item) for item in self.conflicts],
        }
        if self.error_code:
            payload["error_code"] = self.error_code
        if self.parent_event_id:
            payload["parent_event_id"] = self.parent_event_id
        return payload

    def provider_tool_result_payload(self) -> dict[str, Any]:
        return {
            "type": "tool_result",
            "tool_call_id": self.tool_call_id,
            "is_error": not self.ok,
            "content": _copy_mapping(self.observation),
        }


@dataclass(frozen=True)
class ToolResultAdapterOutput:
    raw_result: dict[str, Any]
    event: ToolResultEvent
    schema_version: str = TOOL_RESULT_ADAPTER_SCHEMA_VERSION

    def public_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event": self.event.public_payload(),
        }


@dataclass(frozen=True)
class ModelFinalAnswerEvent:
    event_id: str
    answer_text: str
    answer_route: str = "llm_from_evidence"
    parent_event_id: str | None = None
    schema_version: str = MODEL_EVENT_SCHEMA_VERSION

    @property
    def event_type(self) -> Literal["model_final_answer"]:
        return "model_final_answer"

    def public_payload(self) -> dict[str, Any]:
        payload = {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "event_type": self.event_type,
            "answer_text": self.answer_text,
            "answer_route": self.answer_route,
        }
        if self.parent_event_id:
            payload["parent_event_id"] = self.parent_event_id
        return payload


def model_tool_call_from_provider_block(
    block: dict[str, Any],
    *,
    provider: str,
    event_id: str | None = None,
    parent_event_id: str | None = None,
) -> ModelToolCallEvent:
    if not isinstance(block, dict):
        raise AgentToolError(code="INVALID_MODEL_EVENT", message="provider tool call block must be an object")

    block_type = str(block.get("type") or "").strip()
    protocol_error: dict[str, Any] | None = None
    if block_type == "tool_use":
        tool_call_id = str(block.get("id") or "").strip()
        tool_name = str(block.get("name") or "").strip()
        arguments, protocol_error = _provider_arguments_or_protocol_error(block.get("input"))
    elif block_type in {"function_call", "tool_call"}:
        tool_call_id = str(block.get("call_id") or block.get("id") or "").strip()
        tool_name = str(block.get("name") or "").strip()
        arguments, protocol_error = _provider_arguments_or_protocol_error(block.get("arguments"))
    elif isinstance(block.get("function"), dict):
        function = block["function"]
        tool_call_id = str(block.get("id") or "").strip()
        tool_name = str(function.get("name") or "").strip()
        arguments, protocol_error = _provider_arguments_or_protocol_error(function.get("arguments"))
    else:
        raise AgentToolError(
            code="INVALID_MODEL_EVENT",
            message="provider block is not a structured tool call",
            details={"block_type": block_type},
        )

    if not tool_call_id:
        raise AgentToolError(code="INVALID_MODEL_EVENT", message="provider tool call is missing id")
    if not tool_name:
        raise AgentToolError(code="INVALID_MODEL_EVENT", message="provider tool call is missing tool name")

    return ModelToolCallEvent(
        event_id=event_id or f"event_{tool_call_id}",
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        arguments=arguments,
        provider=provider,
        parent_event_id=parent_event_id,
        protocol_error=protocol_error,
    )


def model_tool_calls_from_provider_content(
    content: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    *,
    provider: str,
    parent_event_id: str | None = None,
) -> tuple[ModelToolCallEvent, ...]:
    events: list[ModelToolCallEvent] = []
    for index, block in enumerate(content, start=1):
        if not isinstance(block, dict) or not _is_provider_tool_call_block(block):
            continue
        events.append(
            model_tool_call_from_provider_block(
                block,
                provider=provider,
                event_id=f"model_tool_call_{index}",
                parent_event_id=parent_event_id,
            )
        )
    return tuple(events)


def model_tool_calls_from_provider_response(
    response: dict[str, Any],
    *,
    provider: str,
    parent_event_id: str | None = None,
) -> tuple[ModelToolCallEvent, ...]:
    if not isinstance(response, dict):
        raise AgentToolError(code="INVALID_MODEL_EVENT", message="provider response must be an object")

    events: list[ModelToolCallEvent] = []
    for index, block in enumerate(_provider_tool_call_blocks_from_response(response), start=1):
        events.append(
            model_tool_call_from_provider_block(
                block,
                provider=provider,
                event_id=f"model_tool_call_{index}",
                parent_event_id=parent_event_id,
            )
        )
    return tuple(events)


def model_final_answer_from_provider_response(
    response: dict[str, Any],
    *,
    provider: str,
    event_id: str | None = None,
    parent_event_id: str | None = None,
    answer_route: str = "llm_from_tool_observation",
) -> ModelFinalAnswerEvent | None:
    if not isinstance(response, dict):
        raise AgentToolError(code="INVALID_MODEL_EVENT", message="provider response must be an object")
    if model_tool_calls_from_provider_response(response, provider=provider, parent_event_id=parent_event_id):
        return None
    text = _provider_text_from_response(response)
    if not text or _looks_like_text_json_plan(text):
        return None
    return ModelFinalAnswerEvent(
        event_id=event_id or "model_final_answer_1",
        parent_event_id=parent_event_id,
        answer_text=text,
        answer_route=answer_route,
    )


def model_events_from_provider_response(
    response: dict[str, Any],
    *,
    provider: str,
    parent_event_id: str | None = None,
) -> tuple[ModelToolCallEvent | ModelFinalAnswerEvent | AssistantEvent, ...]:
    tool_calls = model_tool_calls_from_provider_response(
        response,
        provider=provider,
        parent_event_id=parent_event_id,
    )
    if tool_calls:
        return tool_calls

    control_events = _model_control_events_from_provider_response(response, parent_event_id=parent_event_id)
    if control_events:
        return control_events

    final_answer = model_final_answer_from_provider_response(
        response,
        provider=provider,
        parent_event_id=parent_event_id,
    )
    return (final_answer,) if final_answer is not None else ()


def provider_tool_schema_from_manifest(
    manifest_item: dict[str, Any],
    *,
    omit_arguments: set[str] | frozenset[str] = SYSTEM_SCOPED_TOOL_ARGUMENTS,
) -> dict[str, Any]:
    if not isinstance(manifest_item, dict):
        raise AgentToolError(code="INVALID_TOOL_SCHEMA", message="tool manifest item must be an object")

    name = str(manifest_item.get("name") or "").strip()
    if not name:
        raise AgentToolError(code="INVALID_TOOL_SCHEMA", message="tool manifest item is missing name")

    raw_input_schema = manifest_item.get("input_schema")
    if not isinstance(raw_input_schema, dict):
        raw_input_schema = {}

    properties: dict[str, Any] = {}
    required: list[str] = []
    for raw_key, raw_description in raw_input_schema.items():
        key = str(raw_key or "").strip()
        if not key or key in omit_arguments:
            continue
        if _tool_argument_is_host_owned(raw_description):
            continue
        description = _tool_argument_description(raw_description)
        properties[key] = _tool_argument_schema_from_manifest_value(
            key=key,
            value=raw_description,
            description=description,
        )
        if _tool_argument_is_required(description) or _tool_argument_declares_required(raw_description):
            required.append(key)

    parameters: dict[str, Any] = {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
    }
    if required:
        parameters["required"] = required

    return {
        "schema_version": PROVIDER_TOOL_SCHEMA_VERSION,
        "name": name,
        "description": str(manifest_item.get("description") or "").strip(),
        "parameters": parameters,
        "capabilities": [str(item) for item in manifest_item.get("capabilities") or [] if str(item).strip()],
        "risk_level": str(manifest_item.get("risk_level") or "").strip(),
    }


def provider_tool_schemas_from_manifest(
    manifest_items: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    *,
    allowed_tool_names: set[str] | frozenset[str] | tuple[str, ...] | list[str] | None = None,
    read_only_only: bool = True,
) -> tuple[dict[str, Any], ...]:
    allowed = {str(name).strip() for name in allowed_tool_names or [] if str(name).strip()}
    schemas: list[dict[str, Any]] = []
    for item in manifest_items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        if allowed and name not in allowed:
            continue
        if read_only_only and not _manifest_item_is_read_auto(item):
            continue
        schemas.append(provider_tool_schema_from_manifest(item))
    return tuple(schemas)


def openai_responses_tools_payload(
    manifest_items: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    *,
    allowed_tool_names: set[str] | frozenset[str] | tuple[str, ...] | list[str] | None = None,
    read_only_only: bool = True,
) -> list[dict[str, Any]]:
    return [
        _openai_responses_tool_payload(schema)
        for schema in provider_tool_schemas_from_manifest(
            manifest_items,
            allowed_tool_names=allowed_tool_names,
            read_only_only=read_only_only,
        )
    ]


def chat_completions_tools_payload(
    manifest_items: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    *,
    allowed_tool_names: set[str] | frozenset[str] | tuple[str, ...] | list[str] | None = None,
    read_only_only: bool = True,
) -> list[dict[str, Any]]:
    return [
        _chat_completions_tool_payload(schema)
        for schema in provider_tool_schemas_from_manifest(
            manifest_items,
            allowed_tool_names=allowed_tool_names,
            read_only_only=read_only_only,
        )
    ]


def adapt_tool_result(
    *,
    event_id: str,
    tool_call_id: str,
    tool_name: str,
    raw_result: dict[str, Any],
    normalized_payload: dict[str, Any] | None = None,
    guard_decision: ToolGuardDecisionEvent | None = None,
    parent_event_id: str | None = None,
) -> ToolResultAdapterOutput:
    if not isinstance(raw_result, dict):
        raise AgentToolError(code="INVALID_TOOL_RESULT", message="raw tool result must be an object")

    ok = bool(raw_result.get("ok"))
    error = raw_result.get("error") if isinstance(raw_result.get("error"), dict) else {}
    data = raw_result.get("data") if isinstance(raw_result.get("data"), dict) else {}
    missing_data = tuple(_dict_items(data.get("missing_data")))
    conflicts = tuple(_dict_items(data.get("conflicts")))
    error_code = str(error.get("code") or "") or None

    observation = {
        "schema_version": MODEL_OBSERVATION_SCHEMA_VERSION,
        "tool_name": tool_name,
        "ok": ok,
        "status": "ok" if ok else "error",
        "data_summary": _data_summary(data),
        "missing_data": [_copy_mapping(item) for item in missing_data],
        "conflicts": [_copy_mapping(item) for item in conflicts],
    }
    if error:
        observation["error"] = {
            "code": error_code,
            "message": str(error.get("message") or ""),
        }
    if guard_decision is not None and not ok:
        observation["guard_decision"] = _guard_decision_observation(guard_decision)

    evidence_delta = {
        "schema_version": EVIDENCE_DELTA_SCHEMA_VERSION,
        "tool_name": tool_name,
        "ok": ok,
        "datasets": [_dataset_summary(tool_name=tool_name, data=data, raw_result=raw_result)],
        "missing_data": [_copy_mapping(item) for item in missing_data],
        "conflicts": [_copy_mapping(item) for item in conflicts],
    }
    if error:
        evidence_delta["error"] = {"code": error_code}

    trace_payload = {
        "schema_version": TRACE_PAYLOAD_SCHEMA_VERSION,
        "tool_name": tool_name,
        "tool_call_id": tool_call_id,
        "ok": ok,
        "normalized_payload": _copy_mapping(normalized_payload or {}),
        "guard_decision": guard_decision.public_payload() if guard_decision else {},
        "result_size": _rough_size(raw_result),
        "error_code": error_code,
        "observation_summary": _data_summary(data),
    }

    event = ToolResultEvent(
        event_id=event_id,
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        ok=ok,
        observation=observation,
        evidence_delta=evidence_delta,
        trace_payload=trace_payload,
        missing_data=missing_data,
        conflicts=conflicts,
        error_code=error_code,
        parent_event_id=parent_event_id,
    )
    return ToolResultAdapterOutput(raw_result=_copy_mapping(raw_result), event=event)


def _guard_decision_observation(event: ToolGuardDecisionEvent) -> dict[str, Any]:
    payload = {
        "tool_name": event.tool_name,
        "allowed": bool(event.allowed),
        "decision": event.decision,
        "reason": event.reason,
        "risk_class": event.risk_class,
        "scope_source": event.scope_source,
        "error_code": event.error_code,
    }
    return {key: value for key, value in payload.items() if value not in (None, "", [], {})}


def event_transcript_payload(events: list[Any] | tuple[Any, ...]) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for event in events:
        public_payload = getattr(event, "public_payload", None)
        if callable(public_payload):
            payloads.append(public_payload())
        elif isinstance(event, dict):
            payloads.append(_copy_mapping(event))
        else:
            raise AgentToolError(code="INVALID_MODEL_EVENT", message="event transcript item is not serializable")
    return payloads


def _model_control_events_from_provider_response(
    response: dict[str, Any],
    *,
    parent_event_id: str | None,
) -> tuple[AssistantEvent, ...]:
    if not isinstance(response, dict):
        raise AgentToolError(code="INVALID_MODEL_EVENT", message="provider response must be an object")
    events: list[AssistantEvent] = []
    for index, block in enumerate(_provider_control_blocks_from_response(response), start=1):
        event_type = str(block.get("type") or "").strip()
        payload = {str(key): _copy_value(value) for key, value in block.items() if str(key) != "type"}
        if event_type == "clarification_request":
            payload = _clarification_event_payload(payload)
        events.append(
            AssistantEvent(
                event_id=f"{event_type}_{index}",
                event_type=event_type,  # type: ignore[arg-type]
                payload=payload,
                parent_event_id=parent_event_id,
            )
        )
    return tuple(events)


def _provider_arguments(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return _copy_mapping(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise AgentToolError(
                code="INVALID_MODEL_EVENT",
                message="provider tool call arguments are not valid JSON",
                details={
                    "reason": "provider_arguments_malformed",
                    "error": str(exc),
                    "argument_type": "str",
                    "argument_chars": len(text),
                },
            ) from exc
        if not isinstance(parsed, dict):
            raise AgentToolError(
                code="INVALID_MODEL_EVENT",
                message="provider tool call arguments must be an object",
                details={"reason": "provider_arguments_not_object", "argument_type": type(parsed).__name__},
            )
        return _copy_mapping(parsed)
    raise AgentToolError(
        code="INVALID_MODEL_EVENT",
        message="provider tool call arguments must be an object",
        details={"reason": "provider_arguments_not_object", "argument_type": type(value).__name__},
    )


def _provider_arguments_or_protocol_error(value: Any) -> tuple[dict[str, Any], dict[str, Any] | None]:
    try:
        return _provider_arguments(value), None
    except AgentToolError as err:
        return {}, _agent_tool_error_payload(err)


def _agent_tool_error_payload(err: AgentToolError) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "code": str(err.code or "INVALID_MODEL_EVENT"),
        "message": str(err.message or "provider tool call protocol error"),
    }
    if err.hint:
        payload["hint"] = str(err.hint)
    if isinstance(err.details, dict) and err.details:
        payload["details"] = _copy_mapping(err.details)
    return payload


def _provider_tool_call_blocks_from_response(response: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    blocks: list[dict[str, Any]] = []
    blocks.extend(_top_level_tool_call_blocks(response))

    output = response.get("output")
    if isinstance(output, list):
        for output_index, item in enumerate(output, start=1):
            if not isinstance(item, dict):
                continue
            if _is_provider_tool_call_block(item):
                blocks.append(item)
            content = item.get("content")
            if isinstance(content, list):
                blocks.extend(block for block in content if isinstance(block, dict) and _is_provider_tool_call_block(block))
            tool_calls = item.get("tool_calls")
            if isinstance(tool_calls, list):
                blocks.extend(block for block in tool_calls if isinstance(block, dict) and _is_provider_tool_call_block(block))
            function_call = item.get("function_call")
            if isinstance(function_call, dict):
                blocks.append(_legacy_function_call_block(function_call, fallback_id=f"function_call_output_{output_index}"))

    choices = response.get("choices")
    if isinstance(choices, list):
        for choice_index, choice in enumerate(choices, start=1):
            if not isinstance(choice, dict):
                continue
            message = choice.get("message")
            if not isinstance(message, dict):
                continue
            blocks.extend(_chat_message_tool_call_blocks(message, choice_index=choice_index))

    return tuple(blocks)


def _provider_control_blocks_from_response(response: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    blocks: list[dict[str, Any]] = []
    if _is_provider_control_block(response):
        blocks.append(response)

    output = response.get("output")
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict):
                continue
            if _is_provider_control_block(item):
                blocks.append(item)
            content = item.get("content")
            if isinstance(content, list):
                blocks.extend(block for block in content if isinstance(block, dict) and _is_provider_control_block(block))

    choices = response.get("choices")
    if isinstance(choices, list):
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            message = choice.get("message")
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if isinstance(content, list):
                blocks.extend(block for block in content if isinstance(block, dict) and _is_provider_control_block(block))
    return tuple(blocks)


def _top_level_tool_call_blocks(response: dict[str, Any]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    tool_calls = response.get("tool_calls")
    if isinstance(tool_calls, list):
        blocks.extend(block for block in tool_calls if isinstance(block, dict) and _is_provider_tool_call_block(block))
    function_call = response.get("function_call")
    if isinstance(function_call, dict):
        blocks.append(_legacy_function_call_block(function_call, fallback_id="function_call_top_level"))
    return blocks


def _chat_message_tool_call_blocks(message: dict[str, Any], *, choice_index: int) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, list):
        blocks.extend(block for block in tool_calls if isinstance(block, dict) and _is_provider_tool_call_block(block))
    function_call = message.get("function_call")
    if isinstance(function_call, dict):
        blocks.append(_legacy_function_call_block(function_call, fallback_id=f"function_call_choice_{choice_index}"))
    content = message.get("content")
    if isinstance(content, list):
        blocks.extend(block for block in content if isinstance(block, dict) and _is_provider_tool_call_block(block))
    return blocks


def _legacy_function_call_block(function_call: dict[str, Any], *, fallback_id: str) -> dict[str, Any]:
    return {
        "type": "function_call",
        "call_id": str(function_call.get("call_id") or function_call.get("id") or fallback_id),
        "name": str(function_call.get("name") or ""),
        "arguments": function_call.get("arguments"),
    }


def _is_provider_tool_call_block(block: dict[str, Any]) -> bool:
    block_type = str(block.get("type") or "").strip()
    return block_type in {"tool_use", "function_call", "tool_call"} or isinstance(block.get("function"), dict)


def _is_provider_control_block(block: dict[str, Any]) -> bool:
    return str(block.get("type") or "").strip() in {"clarification_request", "preview_request"}


def _provider_text_from_response(response: dict[str, Any]) -> str:
    output_text = response.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text.strip()

    chunks: list[str] = []
    output = response.get("output")
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict):
                continue
            item_text = item.get("text")
            if isinstance(item_text, str) and item_text.strip():
                chunks.append(item_text.strip())
            content = item.get("content")
            if isinstance(content, str) and content.strip():
                chunks.append(content.strip())
            if isinstance(content, list):
                chunks.extend(_text_chunks_from_content_blocks(content))

    choices = response.get("choices")
    if isinstance(choices, list):
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            message = choice.get("message")
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if isinstance(content, str) and content.strip():
                chunks.append(content.strip())
            if isinstance(content, list):
                chunks.extend(_text_chunks_from_content_blocks(content))
    return "\n".join(chunks).strip()


def _text_chunks_from_content_blocks(content: list[Any]) -> list[str]:
    chunks: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        text = block.get("text")
        if isinstance(text, str) and text.strip():
            chunks.append(text.strip())
    return chunks


def _looks_like_text_json_plan(text: str) -> bool:
    value = _strip_json_code_fence(str(text or "").strip())
    if not value.startswith("{"):
        return False
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return False
    if not isinstance(parsed, dict):
        return False
    keys = {str(key) for key in parsed}
    return bool(keys & {"tool_name", "arguments", "steps", "required_capabilities", "task_contract"})


def _strip_json_code_fence(text: str) -> str:
    value = str(text or "").strip()
    if not value.startswith("```"):
        return value
    lines = value.splitlines()
    if lines and lines[0].strip().startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _clarification_event_payload(payload: dict[str, Any]) -> dict[str, Any]:
    out = _copy_mapping(payload)
    if isinstance(out.get("clarification_request"), dict):
        out["clarification_request"] = _copy_mapping(out["clarification_request"])
        return out
    question = str(out.get("question") or out.get("text") or out.get("reason") or "").strip()
    if not question:
        question = "需要补充范围后才能继续。"
    slot = str(out.get("slot") or "scope").strip() or "scope"
    out["clarification_request"] = {
        "schema_version": CLARIFICATION_REQUEST_SCHEMA_VERSION,
        "status": "needs_user_input",
        "questions": [
            {
                "slot": slot,
                "question": question,
                "options": [],
            }
        ],
    }
    return out


def _manifest_item_is_read_auto(item: dict[str, Any]) -> bool:
    if str(item.get("operation_action") or "").strip():
        return False
    if bool(item.get("requires_confirm")):
        return False
    side_effects = item.get("side_effects")
    if isinstance(side_effects, list) and side_effects:
        return False
    annotations = item.get("annotations") if isinstance(item.get("annotations"), dict) else {}
    if annotations.get("read_only") is True:
        return True
    if item.get("read_only") is True:
        return True
    risk_level = str(item.get("risk_level") or "").strip()
    if risk_level and risk_level != "read_only":
        return False
    capabilities = {str(value).strip() for value in item.get("capabilities") or [] if str(value).strip()}
    return "read_only" in capabilities


def _tool_argument_json_schema(*, key: str, description: str) -> dict[str, Any]:
    lowered = f"{key} {description}".lower()
    enum = _tool_argument_enum(description)
    if enum:
        schema: dict[str, Any] = {"type": "string", "enum": enum}
    elif " bool" in lowered or "boolean" in lowered or lowered.endswith(" bool"):
        schema = {"type": "boolean"}
    elif " int" in lowered or "integer" in lowered or lowered.endswith(" int"):
        schema = {"type": "integer"}
    elif "numeric" in lowered or " number" in lowered or lowered.endswith(" number"):
        schema = {"type": "number"}
    elif " object" in lowered or "structured" in lowered:
        schema = {"type": "object"}
    elif _tool_argument_is_array(key=key, description=description):
        schema = {"type": "array", "items": {"type": "string"}}
    else:
        schema = {"type": "string"}
    if description:
        schema["description"] = description
    return schema


def _tool_argument_is_host_owned(value: Any) -> bool:
    return isinstance(value, dict) and value.get("host_owned") is True


def _tool_argument_schema_from_manifest_value(*, key: str, value: Any, description: str) -> dict[str, Any]:
    if isinstance(value, dict):
        schema = {
            str(raw_key): _copy_value(item)
            for raw_key, item in value.items()
            if str(raw_key) in _TOOL_ARGUMENT_JSON_SCHEMA_KEYS and item not in (None, "", [], {})
        }
        if schema:
            return _provider_compatible_argument_schema(schema)
    return _provider_compatible_argument_schema(_tool_argument_json_schema(key=key, description=description))


def _provider_compatible_argument_schema(schema: dict[str, Any]) -> dict[str, Any]:
    out = _copy_mapping(schema)
    raw_type = out.get("type")
    if isinstance(raw_type, list):
        non_null_types = [item for item in raw_type if item != "null"]
        if len(non_null_types) == 1:
            out["type"] = non_null_types[0]
    raw_enum = out.get("enum")
    if isinstance(raw_enum, list):
        enum_values = [item for item in raw_enum if item is not None]
        if enum_values:
            out["enum"] = enum_values
        else:
            out.pop("enum", None)
    properties = out.get("properties")
    if isinstance(properties, dict):
        out["properties"] = {
            str(prop_name): _provider_compatible_argument_schema(prop_schema)
            if isinstance(prop_schema, dict)
            else _copy_value(prop_schema)
            for prop_name, prop_schema in properties.items()
        }
    items = out.get("items")
    if isinstance(items, dict):
        out["items"] = _provider_compatible_argument_schema(items)
    return out


_TOOL_ARGUMENT_JSON_SCHEMA_KEYS = frozenset(
    {
        "type",
        "enum",
        "items",
        "properties",
        "additionalProperties",
        "description",
        "minimum",
        "maximum",
        "minItems",
        "maxItems",
    }
)


def _tool_argument_description(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("description") or "").strip()
    return str(value or "").strip()


def _tool_argument_declares_required(value: Any) -> bool:
    return isinstance(value, dict) and value.get("required") is True


def _tool_argument_is_array(*, key: str, description: str) -> bool:
    tokens = f"{key} {description}".lower().replace("/", " ").replace(",", " ").replace(";", " ").split()
    return "array" in tokens or "list" in tokens


def _tool_argument_is_required(description: str) -> bool:
    lowered = description.lower()
    return "required" in lowered and "optional" not in lowered


def _tool_argument_enum(description: str) -> list[str]:
    text = description.strip()
    lowered = text.lower()
    if lowered.startswith("optional "):
        text = text[len("optional ") :].strip()
    if " " in text or "|" not in text:
        return []
    values = [item.strip() for item in text.split("|") if item.strip()]
    if len(values) < 2:
        return []
    return values if all(_enum_value_is_simple(value) for value in values) else []


def _enum_value_is_simple(value: str) -> bool:
    return bool(value) and all(ch.isalnum() or ch in {"_", "-", "."} for ch in value)


def _openai_responses_tool_payload(schema: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "type": "function",
        "name": str(schema.get("name") or ""),
        "description": str(schema.get("description") or ""),
        "parameters": _copy_mapping(schema.get("parameters") if isinstance(schema.get("parameters"), dict) else {}),
    }
    return {key: value for key, value in payload.items() if value not in ("", {})}


def _chat_completions_tool_payload(schema: dict[str, Any]) -> dict[str, Any]:
    function = {
        "name": str(schema.get("name") or ""),
        "description": str(schema.get("description") or ""),
        "parameters": _copy_mapping(schema.get("parameters") if isinstance(schema.get("parameters"), dict) else {}),
    }
    return {"type": "function", "function": {key: value for key, value in function.items() if value not in ("", {})}}


def _copy_mapping(value: dict[str, Any]) -> dict[str, Any]:
    return {str(key): _copy_value(item) for key, item in value.items()}


def _copy_value(value: Any) -> Any:
    if isinstance(value, dict):
        return _copy_mapping(value)
    if isinstance(value, list):
        return [_copy_value(item) for item in value]
    if isinstance(value, tuple):
        return [_copy_value(item) for item in value]
    return value


def _dict_items(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [_copy_mapping(item) for item in value if isinstance(item, dict)]


def _data_summary(data: dict[str, Any]) -> dict[str, Any]:
    rows = data.get("rows")
    summary = {
        "keys": _public_data_keys(data),
        "row_count": len(rows) if isinstance(rows, list) else _optional_int(data.get("row_count")),
    }
    output_contract = data.get("output_contract")
    if isinstance(output_contract, dict):
        summary["output_contract"] = {
            "canonical_renderer": str(output_contract.get("canonical_renderer") or ""),
            "source_label": str(output_contract.get("source_label") or ""),
        }
    return {key: value for key, value in summary.items() if value not in (None, "", [])}


def _dataset_summary(*, tool_name: str, data: dict[str, Any], raw_result: dict[str, Any]) -> dict[str, Any]:
    return {
        "tool_name": tool_name,
        "schema_version": str(raw_result.get("schema_version") or ""),
        "data_summary": _data_summary(data),
    }


def _public_data_keys(data: dict[str, Any]) -> list[str]:
    return sorted(str(key) for key in data.keys() if not _sensitive_summary_key(str(key)))[:20]


def _sensitive_summary_key(key: str) -> bool:
    lowered = key.lower()
    return any(token in lowered for token in ("sql", "path", "secret", "token", "password", "raw", "config"))


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def _rough_size(value: Any) -> int:
    try:
        return len(json.dumps(value, ensure_ascii=False, sort_keys=True))
    except (TypeError, ValueError):
        return len(str(value))


__all__ = [
    "CLARIFICATION_REQUEST_SCHEMA_VERSION",
    "EVIDENCE_DELTA_SCHEMA_VERSION",
    "MODEL_EVENT_SCHEMA_VERSION",
    "MODEL_OBSERVATION_SCHEMA_VERSION",
    "PROVIDER_TOOL_SCHEMA_VERSION",
    "SYSTEM_SCOPED_TOOL_ARGUMENTS",
    "TOOL_RESULT_ADAPTER_SCHEMA_VERSION",
    "TRACE_PAYLOAD_SCHEMA_VERSION",
    "AssistantEvent",
    "ModelFinalAnswerEvent",
    "ModelToolCallEvent",
    "ToolGuardDecisionEvent",
    "ToolResultAdapterOutput",
    "ToolResultEvent",
    "adapt_tool_result",
    "chat_completions_tools_payload",
    "event_transcript_payload",
    "model_events_from_provider_response",
    "model_final_answer_from_provider_response",
    "model_tool_call_from_provider_block",
    "model_tool_calls_from_provider_content",
    "model_tool_calls_from_provider_response",
    "openai_responses_tools_payload",
    "provider_tool_schema_from_manifest",
    "provider_tool_schemas_from_manifest",
]
