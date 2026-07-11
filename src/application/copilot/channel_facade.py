from __future__ import annotations

import json
from datetime import date
from typing import Any

from src.application.copilot.contracts import (
    AppResult,
    CopilotRequest,
    CopilotScope,
    ExecutionContract,
    new_id,
)
from src.application.copilot.local_harness import run_prepared_contract
from src.application.copilot.host import host_lane_slot, record_session_turn, session_messages, session_run_slot
from src.application.copilot.host_store import CopilotHostStore
from src.application.copilot.model_config import load_assistant_llm_config, model_api_key_configured
from src.application.copilot.service import prepare_contract


def run_channel_request(
    *,
    user_message: str,
    config_key: str | None,
    request_id: str | None = None,
    reference_year: int | None = None,
    assistant_config_path: str | None = None,
    channel: str | None = None,
    sender_id: str | None = None,
    conversation_id: str | None = None,
    host_db_path: str | None = None,
    control_preview_specs: tuple[dict[str, object], ...] = (),
    control_context: tuple[dict[str, Any], ...] = (),
) -> AppResult:
    year = reference_year or date.today().year
    model_gate = _channel_model_gate(assistant_config_path)
    if model_gate:
        request = _channel_request(
            user_message=user_message,
            config_key=config_key,
            request_id=request_id,
            context_messages=(),
        )
        return _request_not_ready(
            request,
            reason=model_gate,
            message="渠道 Copilot 需要显式可用的 assistant 模型配置",
        )
    session_key = _channel_session_key(channel=channel, sender_id=sender_id, conversation_id=conversation_id)
    host_store = CopilotHostStore(host_db_path) if str(host_db_path or "").strip() else None
    with session_run_slot(session_key, host_store=host_store, ttl_seconds=300) as entered:
        if not entered:
            request = _channel_request(
                user_message=user_message,
                config_key=config_key,
                request_id=request_id,
                context_messages=(),
            )
            return _request_not_ready(
                request,
                reason="channel_run_already_running",
                message="同一会话已有 Copilot 分析正在运行",
            )
        request = _channel_request(
            user_message=user_message,
            config_key=config_key,
            request_id=request_id,
            context_messages=_context_messages(
                session_messages(session_key, host_store=host_store),
                control_context=control_context,
            ),
        )
        try:
            prepared = prepare_contract(request, reference_year=year)
        except Exception:
            return _channel_prepare_failed(request)
        if isinstance(prepared, AppResult):
            return prepared
        with host_lane_slot("chat_read", host_store=host_store, limit=2, ttl_seconds=300) as lane_entered:
            if not lane_entered:
                return _request_not_ready(
                    request,
                    reason="channel_capacity_exhausted",
                    message="Copilot 当前分析任务已达到并发上限",
                )
            try:
                result = run_prepared_contract(
                    prepared,
                    assistant_config_path=assistant_config_path,
                    host_store=host_store,
                    session_key=session_key,
                    control_preview_specs=control_preview_specs,
                )
            except Exception:
                result = _channel_run_failed(prepared)
        if result.user_response.strip():
            record_session_turn(
                session_key,
                user_message,
                result.user_response,
                host_store=host_store,
                tool_uses=_tool_uses(result),
                warnings=_event_messages(result, "warning"),
                errors=_event_messages(result, "model_error", "tool_failure_fallback"),
            )
        return result


def record_channel_turn(
    *,
    channel: str | None,
    sender_id: str | None,
    conversation_id: str | None,
    host_db_path: str | None,
    user_message: str,
    assistant_message: str,
) -> None:
    session_key = _channel_session_key(channel=channel, sender_id=sender_id, conversation_id=conversation_id)
    host_store = CopilotHostStore(host_db_path) if str(host_db_path or "").strip() else None
    record_session_turn(session_key, user_message, assistant_message, host_store=host_store)


def _tool_uses(result: AppResult) -> tuple[dict[str, Any], ...]:
    calls: dict[str, dict[str, Any]] = {}
    completed: list[dict[str, Any]] = []
    for event in result.events:
        call_id = str(event.payload.get("tool_call_id") or "")
        if event.type == "tool_call":
            calls[call_id] = {
                "name": str(event.payload.get("tool_name") or ""),
                "arguments": dict(event.payload.get("tool_input") or {}),
            }
        elif event.type == "tool_result":
            item = dict(calls.get(call_id) or {})
            item["ok"] = bool(event.payload.get("ok"))
            item["result_summary"] = event.payload.get("summary") or event.payload.get("error") or ""
            completed.append(item)
    return tuple(item for item in completed if item.get("name"))


def _event_messages(result: AppResult, *event_types: str) -> tuple[str, ...]:
    allowed = set(event_types)
    messages: list[str] = []
    for event in result.events:
        if event.type not in allowed:
            continue
        text = str(event.payload.get("message") or event.payload.get("reason") or event.type).strip()
        if text:
            messages.append(text)
    return tuple(messages)


def _context_messages(
    messages: tuple[dict[str, Any], ...],
    *,
    control_context: tuple[dict[str, Any], ...],
) -> tuple[dict[str, Any], ...]:
    snapshot = json.dumps(list(control_context), ensure_ascii=False, sort_keys=True, default=str)
    return (
        *messages,
        {
            "role": "system",
            "content": (
                "Authoritative pending Control operations for this conversation, refreshed from the operation store. "
                "These are previews only, not proof of execution. Treat this snapshot as newer than chat history. "
                f"pending_operations={snapshot}"
            ),
        },
    )


def _channel_model_gate(assistant_config_path: str | None) -> str | None:
    if not str(assistant_config_path or "").strip():
        return "channel_model_config_missing"
    raw, load_error = load_assistant_llm_config(config_path=assistant_config_path, require_config=True)
    if load_error:
        return load_error
    if not raw:
        return "channel_model_profile_missing"
    ok, key_error = model_api_key_configured(raw)
    if not ok:
        if key_error == "model_api_key_missing":
            return "channel_model_api_key_missing"
        return key_error
    return None


def _channel_session_key(*, channel: str | None, sender_id: str | None, conversation_id: str | None) -> str:
    channel_key = str(channel or "unknown").strip().lower() or "unknown"
    conversation_key = str(conversation_id or "").strip() or f"sender:{str(sender_id or '').strip() or 'unknown'}"
    return f"{channel_key}:{conversation_key}"


def _channel_request(
    *,
    user_message: str,
    config_key: str | None,
    request_id: str | None,
    context_messages: tuple[dict[str, str], ...],
) -> CopilotRequest:
    return CopilotRequest(
        request_id=request_id or new_id("req"),
        source_entry="channel",
        user_message=user_message,
        explicit_scope=CopilotScope(config_key=config_key),
        context_messages=tuple(dict(item) for item in context_messages),
        execution_environment="channel",
    )


def _request_not_ready(request: CopilotRequest, *, reason: str, message: str) -> AppResult:
    return AppResult(
        status="not_ready",
        user_response=f"{message}；本次没有调用工具。",
        error={"code": "CHANNEL_NOT_READY", "reason": reason},
        request_id=request.request_id,
        decision_trace={"channel_gate": reason},
    )


def _channel_prepare_failed(request: CopilotRequest) -> AppResult:
    return AppResult(
        status="failed",
        user_response="Copilot 未能准备渠道执行合同。",
        error={"code": "CHANNEL_PREPARE_FAILED"},
        request_id=request.request_id,
        decision_trace={"service_error": "channel_prepare_contract_failed"},
        ok=False,
    )


def _channel_run_failed(contract: ExecutionContract) -> AppResult:
    return AppResult(
        status="failed",
        user_response="Copilot 渠道执行失败，未返回分析结果。",
        error={"code": "CHANNEL_RUN_FAILED"},
        request_id=contract.request_id,
        contract_id=contract.contract_id,
        decision_trace={**contract.decision_trace, "channel_error": "channel_run_failed"},
        ok=False,
    )


__all__ = ["record_channel_turn", "run_channel_request"]
