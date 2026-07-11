from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from dataclasses import replace
from typing import Any, Callable

from src.application.copilot.agent import ModelRequest, ModelRunner, ModelTurn, ToolCall
from src.application.llm_provider_registry import (
    provider_api_kind,
    provider_chat_completion_payload_options,
    provider_requires_api_key,
    require_provider_spec,
)
from src.infrastructure.openai_chat_completions import create_chat_completion
from src.infrastructure.openai_responses import create_response


CreateResponseFn = Callable[..., dict[str, Any]]


@dataclass(frozen=True)
class CopilotModelSettings:
    provider: str
    model: str
    base_url: str = ""
    api_key_env: str = ""
    timeout_seconds: int = 90
    max_output_tokens: int = 2048
    max_attempts: int = 2

    @classmethod
    def from_config(cls, raw: dict[str, Any]) -> "CopilotModelSettings":
        cfg = raw if isinstance(raw, dict) else {}
        provider = str(cfg.get("provider") or "").strip().lower()
        spec = require_provider_spec(provider, path="copilot.model.provider")
        return cls(
            provider=spec.provider_id,
            model=str(cfg.get("model") or "").strip(),
            base_url=str(cfg.get("base_url") or spec.default_base_url).strip(),
            api_key_env=str(cfg.get("api_key_env") or spec.default_api_key_env).strip(),
            timeout_seconds=_bounded_int(cfg.get("timeout_seconds"), default=90, minimum=1, maximum=180),
            max_output_tokens=_bounded_int(cfg.get("max_output_tokens"), default=2048, minimum=256, maximum=8192),
            max_attempts=_bounded_int(cfg.get("max_attempts"), default=2, minimum=1, maximum=3),
        )


def build_model_runner(
    settings: CopilotModelSettings,
    *,
    environ: dict[str, str] | None = None,
    create_response_fn: CreateResponseFn | None = None,
    create_chat_completion_fn: CreateResponseFn | None = None,
    sleep_fn: Callable[[float], None] | None = None,
) -> ModelRunner:
    if not settings.model.strip():
        raise ValueError("model is required")
    api_key = _api_key_value(settings, environ=environ)
    if provider_requires_api_key(settings.provider) and not api_key:
        raise ValueError("api key env is not configured")

    def _run(request: ModelRequest) -> ModelTurn:
        last_error: Exception | None = None
        sleeper = sleep_fn or time.sleep
        for attempt in range(1, settings.max_attempts + 1):
            _raise_if_cancelled(request)
            try:
                if provider_api_kind(settings.provider) == "chat_completions":
                    raw = _call_chat_completion(
                        settings,
                        api_key,
                        request,
                        create_chat_completion_fn,
                    )
                    _raise_if_cancelled(request)
                    return replace(_parse_chat_completion(raw), attempt_count=attempt)
                raw = _call_response(settings, api_key, request, create_response_fn)
                _raise_if_cancelled(request)
                return replace(_parse_response(raw), attempt_count=attempt)
            except Exception as exc:
                last_error = exc
                if attempt >= settings.max_attempts or not _is_transient_model_error(exc):
                    break
                _cancellable_sleep(min(1.0, 0.25 * (2 ** (attempt - 1))), request, sleeper)
        assert last_error is not None
        try:
            setattr(last_error, "attempt_count", attempt)
        except Exception:
            pass
        raise last_error

    return _run


def _call_response(
    settings: CopilotModelSettings,
    api_key: str,
    request: ModelRequest,
    create_response_fn: CreateResponseFn | None,
) -> dict[str, Any]:
    instructions, messages = _split_system_messages(request.messages)
    return (create_response_fn or create_response)(
        api_key=api_key,
        base_url=settings.base_url,
        model=settings.model,
        input_items=_responses_input(messages),
        instructions=instructions,
        tools=[] if request.force_finish else _responses_tools(request.tools),
        timeout=_effective_timeout(settings, request),
        max_output_tokens=settings.max_output_tokens,
        temperature=0.0,
    )


def _call_chat_completion(
    settings: CopilotModelSettings,
    api_key: str,
    request: ModelRequest,
    create_chat_completion_fn: CreateResponseFn | None,
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "api_key": api_key,
        "base_url": settings.base_url,
        "model": settings.model,
        "messages": _chat_messages(request.messages),
        "tools": [] if request.force_finish else _chat_tools(request.tools),
        "timeout": _effective_timeout(settings, request),
        "max_output_tokens": settings.max_output_tokens,
    }
    kwargs.update(provider_chat_completion_payload_options(settings.provider))
    return (create_chat_completion_fn or create_chat_completion)(**kwargs)


def _effective_timeout(settings: CopilotModelSettings, request: ModelRequest) -> int:
    if request.timeout_seconds is None:
        return settings.timeout_seconds
    return max(1, min(settings.timeout_seconds, int(request.timeout_seconds)))


def _split_system_messages(messages: tuple[dict[str, Any], ...]) -> tuple[str, list[dict[str, Any]]]:
    instructions: list[str] = []
    remaining: list[dict[str, Any]] = []
    for item in messages:
        if str(item.get("role") or "") == "system":
            text = str(item.get("content") or "").strip()
            if text:
                instructions.append(text)
            continue
        remaining.append(dict(item))
    return "\n\n".join(instructions), remaining


def _responses_input(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for message in messages:
        role = str(message.get("role") or "")
        if role == "assistant" and isinstance(message.get("tool_calls"), list):
            text = str(message.get("content") or "").strip()
            if text:
                items.append({"role": "assistant", "content": text})
            for call in message["tool_calls"]:
                if not isinstance(call, dict):
                    continue
                items.append(
                    {
                        "type": "function_call",
                        "call_id": str(call.get("id") or ""),
                        "name": str(call.get("name") or ""),
                        "arguments": json.dumps(call.get("arguments") or {}, ensure_ascii=False),
                    }
                )
            continue
        if role == "tool":
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": str(message.get("tool_call_id") or ""),
                    "output": str(message.get("content") or ""),
                }
            )
            continue
        items.append({"role": role, "content": str(message.get("content") or "")})
    return items


def _chat_messages(messages: tuple[dict[str, Any], ...]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for message in messages:
        role = str(message.get("role") or "")
        if role == "assistant" and isinstance(message.get("tool_calls"), list):
            items.append(
                {
                    "role": "assistant",
                    "content": str(message.get("content") or ""),
                    "tool_calls": [
                        {
                            "id": str(call.get("id") or ""),
                            "type": "function",
                            "function": {
                                "name": str(call.get("name") or ""),
                                "arguments": json.dumps(call.get("arguments") or {}, ensure_ascii=False),
                            },
                        }
                        for call in message["tool_calls"]
                        if isinstance(call, dict)
                    ],
                }
            )
            continue
        if role == "tool":
            items.append(
                {
                    "role": "tool",
                    "tool_call_id": str(message.get("tool_call_id") or ""),
                    "content": str(message.get("content") or ""),
                }
            )
            continue
        items.append({"role": role, "content": str(message.get("content") or "")})
    return items


def _responses_tools(tools: tuple[dict[str, Any], ...]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "name": str(item.get("name") or ""),
            "description": str(item.get("description") or ""),
            "parameters": dict(item.get("input_schema") or {"type": "object", "properties": {}}),
        }
        for item in tools
    ]


def _chat_tools(tools: tuple[dict[str, Any], ...]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": str(item.get("name") or ""),
                "description": str(item.get("description") or ""),
                "parameters": dict(item.get("input_schema") or {"type": "object", "properties": {}}),
            },
        }
        for item in tools
    ]


def _parse_response(raw: dict[str, Any]) -> ModelTurn:
    texts: list[str] = []
    calls: list[ToolCall] = []
    for item in raw.get("output") or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "function_call":
            calls.append(
                ToolCall(
                    call_id=str(item.get("call_id") or item.get("id") or ""),
                    name=str(item.get("name") or ""),
                    arguments=_arguments(item.get("arguments")),
                )
            )
            continue
        for content in item.get("content") or []:
            if not isinstance(content, dict):
                continue
            text = content.get("text")
            if isinstance(text, str) and text.strip():
                texts.append(text.strip())
    output_text = raw.get("output_text")
    if isinstance(output_text, str) and output_text.strip() and not texts:
        texts.append(output_text.strip())
    incomplete = raw.get("incomplete_details") if isinstance(raw.get("incomplete_details"), dict) else {}
    finish_reason = None
    if str(raw.get("status") or "").strip().lower() == "incomplete":
        reason = str(incomplete.get("reason") or "incomplete").strip()
        finish_reason = "length" if reason == "max_output_tokens" else reason
    elif str(raw.get("status") or "").strip():
        finish_reason = str(raw.get("status")).strip()
    return ModelTurn(
        text="\n".join(texts).strip(),
        tool_calls=tuple(calls),
        finish_reason=finish_reason,
        usage=_normalized_usage(raw.get("usage")),
        raw=dict(raw),
    )


def _parse_chat_completion(raw: dict[str, Any]) -> ModelTurn:
    choices = raw.get("choices")
    first_choice = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
    message = first_choice.get("message") if isinstance(first_choice, dict) else {}
    if not isinstance(message, dict):
        message = {}
    calls: list[ToolCall] = []
    for item in message.get("tool_calls") or []:
        if not isinstance(item, dict):
            continue
        function = item.get("function") if isinstance(item.get("function"), dict) else {}
        calls.append(
            ToolCall(
                call_id=str(item.get("id") or ""),
                name=str(function.get("name") or ""),
                arguments=_arguments(function.get("arguments")),
            )
        )
    return ModelTurn(
        text=str(message.get("content") or "").strip(),
        tool_calls=tuple(calls),
        finish_reason=str(first_choice.get("finish_reason") or "").strip() or None,
        usage=_normalized_usage(raw.get("usage")),
        raw=dict(raw),
    )


def _normalized_usage(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    aliases = {
        "input_tokens": ("input_tokens", "prompt_tokens"),
        "output_tokens": ("output_tokens", "completion_tokens"),
        "total_tokens": ("total_tokens",),
    }
    usage: dict[str, int] = {}
    for target, keys in aliases.items():
        for key in keys:
            raw = value.get(key)
            if isinstance(raw, int) and raw >= 0:
                usage[target] = raw
                break
    return usage


def _is_transient_model_error(exc: Exception) -> bool:
    status = getattr(exc, "http_status", None)
    if isinstance(status, int):
        return status in {408, 409, 425, 429} or status >= 500
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    name = type(exc).__name__.lower()
    text = str(exc).lower()
    return any(token in name or token in text for token in ("timeout", "network", "connection", "temporar"))


def _raise_if_cancelled(request: ModelRequest) -> None:
    if request.is_cancelled and request.is_cancelled():
        error = RuntimeError("model request cancelled")
        setattr(error, "cancelled", True)
        raise error


def _cancellable_sleep(seconds: float, request: ModelRequest, sleeper: Callable[[float], None]) -> None:
    if request.is_cancelled is None:
        sleeper(max(0.0, float(seconds)))
        return
    remaining = max(0.0, float(seconds))
    while remaining > 0:
        _raise_if_cancelled(request)
        interval = min(0.1, remaining)
        sleeper(interval)
        remaining -= interval


def _arguments(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    try:
        parsed = json.loads(str(value or "{}"))
    except Exception:
        return {"__invalid_arguments__": str(value or "")}
    return dict(parsed) if isinstance(parsed, dict) else {"__invalid_arguments__": str(value or "")}


def _api_key_value(settings: CopilotModelSettings, *, environ: dict[str, str] | None) -> str:
    env = environ if environ is not None else os.environ
    return str(env.get(settings.api_key_env) or "").strip()


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except Exception:
        parsed = int(default)
    return max(int(minimum), min(parsed, int(maximum)))


__all__ = ["CopilotModelSettings", "build_model_runner"]
