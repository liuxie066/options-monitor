from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from src.application.agent_tool_contracts import AgentToolError
from src.application.assistant.llm_provider_registry import provider_api_kind
from src.application.assistant.model_events import (
    AssistantEvent,
    ModelFinalAnswerEvent,
    ModelToolCallEvent,
    ToolResultEvent,
    model_events_from_provider_response,
)


MODEL_CONTINUATION_SCHEMA_VERSION = "om-assistant-model-continuation-v1"

CreateModelContinuationResponseFn = Callable[[dict[str, Any]], dict[str, Any]]
ContinuationEvent = ModelToolCallEvent | ModelFinalAnswerEvent | AssistantEvent


@dataclass(frozen=True)
class ModelContinuationOutcome:
    provider: str
    request_payload: dict[str, Any]
    response: dict[str, Any]
    events: tuple[ContinuationEvent, ...]
    schema_version: str = MODEL_CONTINUATION_SCHEMA_VERSION

    def public_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "provider": self.provider,
            "request_payload": _copy_mapping(self.request_payload),
            "event_count": len(self.events),
            "events": [event.public_payload() for event in self.events],
        }


def openai_responses_continuation_input(
    *,
    model_event: ModelToolCallEvent,
    tool_result_event: ToolResultEvent,
) -> list[dict[str, Any]]:
    _require_matching_tool_call(model_event=model_event, tool_result_event=tool_result_event)
    return [
        {
            "type": "function_call",
            "call_id": model_event.tool_call_id,
            "name": model_event.tool_name,
            "arguments": _json_text(model_event.arguments),
        },
        {
            "type": "function_call_output",
            "call_id": tool_result_event.tool_call_id,
            "output": _json_text(tool_result_event.provider_tool_result_payload()),
        },
    ]


def openai_responses_continuation_input_batch(
    *,
    results: tuple[tuple[ModelToolCallEvent, ToolResultEvent], ...],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for model_event, tool_result_event in results:
        out.extend(
            openai_responses_continuation_input(
                model_event=model_event,
                tool_result_event=tool_result_event,
            )
        )
    return out


def chat_completions_continuation_messages(
    *,
    model_event: ModelToolCallEvent,
    tool_result_event: ToolResultEvent,
) -> list[dict[str, Any]]:
    _require_matching_tool_call(model_event=model_event, tool_result_event=tool_result_event)
    assistant_message = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": model_event.tool_call_id,
                "type": "function",
                "function": {
                    "name": model_event.tool_name,
                    "arguments": _json_text(model_event.arguments),
                },
            }
        ],
    }
    _attach_chat_reasoning_content(assistant_message, (model_event,))
    return [
        assistant_message,
        {
            "role": "tool",
            "tool_call_id": tool_result_event.tool_call_id,
            "content": _json_text(tool_result_event.provider_tool_result_payload()),
        },
    ]


def chat_completions_continuation_messages_batch(
    *,
    results: tuple[tuple[ModelToolCallEvent, ToolResultEvent], ...],
) -> list[dict[str, Any]]:
    if not results:
        return []
    tool_calls: list[dict[str, Any]] = []
    tool_messages: list[dict[str, Any]] = []
    for model_event, tool_result_event in results:
        _require_matching_tool_call(model_event=model_event, tool_result_event=tool_result_event)
        tool_calls.append(
            {
                "id": model_event.tool_call_id,
                "type": "function",
                "function": {
                    "name": model_event.tool_name,
                    "arguments": _json_text(model_event.arguments),
                },
            }
        )
        tool_messages.append(
            {
                "role": "tool",
                "tool_call_id": tool_result_event.tool_call_id,
                "content": _json_text(tool_result_event.provider_tool_result_payload()),
            }
        )
    assistant_message = {"role": "assistant", "content": "", "tool_calls": tool_calls}
    _attach_chat_reasoning_content(assistant_message, tuple(model_event for model_event, _tool_result in results))
    return [assistant_message, *tool_messages]


def provider_continuation_payload(
    *,
    provider: str,
    model_event: ModelToolCallEvent,
    tool_result_event: ToolResultEvent,
    base_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = _copy_mapping(base_payload or {})
    if provider_api_kind(provider) == "chat_completions":
        messages = payload.get("messages")
        existing_messages = [_copy_mapping(item) for item in messages] if _is_dict_list(messages) else []
        payload["messages"] = existing_messages + chat_completions_continuation_messages(
            model_event=model_event,
            tool_result_event=tool_result_event,
        )
        return payload

    input_value = payload.get("input")
    existing_input = [_copy_mapping(item) for item in input_value] if _is_dict_list(input_value) else []
    payload["input"] = existing_input + openai_responses_continuation_input(
        model_event=model_event,
        tool_result_event=tool_result_event,
    )
    return payload


def provider_continuation_payload_batch(
    *,
    provider: str,
    results: tuple[tuple[ModelToolCallEvent, ToolResultEvent], ...],
    base_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not results:
        raise AgentToolError(code="INVALID_MODEL_EVENT", message="model continuation requires at least one tool result")
    payload = _copy_mapping(base_payload or {})
    if provider_api_kind(provider) == "chat_completions":
        messages = payload.get("messages")
        existing_messages = [_copy_mapping(item) for item in messages] if _is_dict_list(messages) else []
        payload["messages"] = existing_messages + chat_completions_continuation_messages_batch(results=results)
        return payload

    input_value = payload.get("input")
    existing_input = [_copy_mapping(item) for item in input_value] if _is_dict_list(input_value) else []
    payload["input"] = existing_input + openai_responses_continuation_input_batch(results=results)
    return payload


def continue_model_after_tool_result(
    *,
    provider: str,
    create_response_fn: CreateModelContinuationResponseFn,
    model_event: ModelToolCallEvent,
    tool_result_event: ToolResultEvent,
    base_payload: dict[str, Any] | None = None,
    parent_event_id: str | None = None,
) -> ModelContinuationOutcome:
    request_payload = provider_continuation_payload(
        provider=provider,
        model_event=model_event,
        tool_result_event=tool_result_event,
        base_payload=base_payload,
    )
    response = create_response_fn(_copy_mapping(request_payload))
    if not isinstance(response, dict):
        raise AgentToolError(code="INVALID_MODEL_EVENT", message="provider continuation response must be an object")
    events = model_events_from_provider_response(
        response,
        provider=provider,
        parent_event_id=parent_event_id or tool_result_event.event_id,
    )
    return ModelContinuationOutcome(
        provider=provider,
        request_payload=request_payload,
        response=_copy_mapping(response),
        events=events,
    )


def continue_model_after_tool_results(
    *,
    provider: str,
    create_response_fn: CreateModelContinuationResponseFn,
    results: tuple[tuple[ModelToolCallEvent, ToolResultEvent], ...],
    base_payload: dict[str, Any] | None = None,
    parent_event_id: str | None = None,
) -> ModelContinuationOutcome:
    request_payload = provider_continuation_payload_batch(
        provider=provider,
        results=results,
        base_payload=base_payload,
    )
    response = create_response_fn(_copy_mapping(request_payload))
    if not isinstance(response, dict):
        raise AgentToolError(code="INVALID_MODEL_EVENT", message="provider continuation response must be an object")
    events = model_events_from_provider_response(
        response,
        provider=provider,
        parent_event_id=parent_event_id or results[-1][1].event_id,
    )
    return ModelContinuationOutcome(
        provider=provider,
        request_payload=request_payload,
        response=_copy_mapping(response),
        events=events,
    )


def _require_matching_tool_call(*, model_event: ModelToolCallEvent, tool_result_event: ToolResultEvent) -> None:
    if model_event.tool_call_id != tool_result_event.tool_call_id:
        raise AgentToolError(
            code="INVALID_MODEL_EVENT",
            message="tool result does not match model tool call id",
            details={
                "model_tool_call_id": model_event.tool_call_id,
                "tool_result_call_id": tool_result_event.tool_call_id,
            },
        )
    if model_event.tool_name != tool_result_event.tool_name:
        raise AgentToolError(
            code="INVALID_MODEL_EVENT",
            message="tool result does not match model tool name",
            details={
                "model_tool_name": model_event.tool_name,
                "tool_result_name": tool_result_event.tool_name,
            },
        )


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _attach_chat_reasoning_content(message: dict[str, Any], model_events: tuple[ModelToolCallEvent, ...]) -> None:
    for model_event in model_events:
        metadata = model_event.provider_metadata if isinstance(model_event.provider_metadata, dict) else {}
        reasoning_content = metadata.get("reasoning_content")
        if isinstance(reasoning_content, str) and reasoning_content:
            message["reasoning_content"] = reasoning_content
            return


def _is_dict_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, dict) for item in value)


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


__all__ = [
    "MODEL_CONTINUATION_SCHEMA_VERSION",
    "ContinuationEvent",
    "CreateModelContinuationResponseFn",
    "ModelContinuationOutcome",
    "chat_completions_continuation_messages",
    "chat_completions_continuation_messages_batch",
    "continue_model_after_tool_result",
    "continue_model_after_tool_results",
    "openai_responses_continuation_input",
    "openai_responses_continuation_input_batch",
    "provider_continuation_payload",
    "provider_continuation_payload_batch",
]
