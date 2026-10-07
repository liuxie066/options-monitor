from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from typing import Any

from src.application.agent_tool_contracts import AgentToolError
from src.application.bot.config_scope import (
    BotConfigScopeError,
    config_scope_error_message,
    resolve_trusted_config_scope,
)
from src.application.bot.contracts import (
    AppResult,
    BotRequest,
    BotScope,
    ExecutionContract,
    new_id,
)
from src.application.bot.host import host_lane_slot, session_run_slot
from src.application.bot.host_store import BotHostStore
from src.application.bot.local_harness import _budget_exhausted, run_prepared_contract
from src.application.bot.model_config import load_bot_llm_config, load_bot_read_scope, model_api_key_configured
from src.application.bot.service import prepare_contract
from src.application.bot.session import derive_session_id


def analysis_control_replacement(text: str) -> str | None:
    """Only complete, unquoted analysis-control utterances have authority."""
    value = str(text or "").strip()
    if value in {"取消分析", "停止分析"}:
        return ""
    match = re.fullmatch(r"取消当前分析[，,]\s*改为[：:]?\s*(\S[^\r\n]*)", value)
    return match.group(1).strip() if match else None


def cancel_channel_analysis(*, request: Any, audit_store: Any) -> dict[str, Any]:
    # Sender authorization is performed by the inbound owner before this facade.
    if not request.message_id or not request.sender_id or request.channel != "feishu":
        raise AgentToolError(code="INPUT_ERROR", message="analysis control identity unavailable")
    try:
        primary_market, resolved_path, authority_scope = resolve_trusted_config_scope(
            config_key=request.config_key, config_path=request.config_path)
    except BotConfigScopeError as exc:
        raise AgentToolError(code="CHANNEL_NOT_READY", message=config_scope_error_message(exc.reason)) from exc
    try:
        _markets, generation = _channel_read_scope(request.bot_config_path, primary_market)
    except (OSError, RuntimeError, ValueError, AgentToolError):
        generation = ""  # Exact trusted identity below can still cancel one older active run.
    conversation = (None if request.conversation_id == f"{request.channel}:{request.sender_id}"
                    else request.conversation_id)
    session_key = _channel_session_key(channel=request.channel, sender_id=request.sender_id,
        conversation_id=conversation, authority_scope=_session_authority(authority_scope, generation))
    identity = {"authenticated_channel": request.channel, "authenticated_sender_id": request.sender_id,
        "authenticated_conversation_id": str(conversation or ""), "authority_scope": authority_scope,
        "config_path": resolved_path}
    store = BotHostStore(audit_store.path)
    return audit_store.record_analysis_control_once(channel=request.channel, sender_id=request.sender_id,
        conversation_id=request.conversation_id or f"{request.channel}:{request.sender_id}",
        message_id=request.message_id, text=request.text,
        scope=session_key, resolve=lambda connection: store.cancel_session_run(session_key,
            connection=connection, trusted_identity=identity))


def run_channel_request(
    *,
    user_message: str,
    config_key: str | None,
    config_path: str | None = None,
    request_id: str | None = None,
    reference_year: int | None = None,
    bot_config_path: str | None = None,
    channel: str | None = None,
    sender_id: str | None = None,
    conversation_id: str | None = None,
    host_db_path: str | None = None,
    control_preview_specs: tuple[dict[str, object], ...] = (),
    control_context: tuple[dict[str, Any], ...] = (),
    received_monotonic: float | None = None,
    authenticated_sender_id: str | None = None,
    reply_builder: Any = None,
) -> AppResult:
    received = time.monotonic() if received_monotonic is None else received_monotonic
    deadline = received + 180
    report_now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    effective_request_id = request_id or new_id("req")
    if time.monotonic() >= deadline:
        return _budget_exhausted(effective_request_id)
    try:
        resolved_key, resolved_path, authority_scope = resolve_trusted_config_scope(
            config_key=config_key,
            config_path=config_path,
        )
        read_markets, read_generation = _channel_read_scope(bot_config_path, resolved_key)
        session_key = _channel_session_key(
            channel=channel,
            sender_id=sender_id,
            conversation_id=conversation_id,
            authority_scope=_session_authority(authority_scope, read_generation),
        )
    except BotConfigScopeError as exc:
        return _request_not_ready(
            effective_request_id,
            reason=exc.reason,
            message=config_scope_error_message(exc.reason),
        )
    except (AgentToolError, OSError, RuntimeError, ValueError):
        return _request_not_ready(
            effective_request_id,
            reason="channel_identity_or_scope_invalid",
            message="渠道身份或数据作用域不可用",
        )
    model_gate = _channel_model_gate(bot_config_path)
    if model_gate:
        return _request_not_ready(
            effective_request_id,
            reason=model_gate,
            message="渠道 Bot 需要显式可用的 Bot 模型配置",
        )
    if time.monotonic() >= deadline:
        return _budget_exhausted(effective_request_id)
    host_store = BotHostStore(host_db_path) if str(host_db_path or "").strip() else None
    with session_run_slot(session_key, host_store=host_store, ttl_seconds=300, deadline_monotonic=deadline) as entered:
        if time.monotonic() >= deadline:
            return _budget_exhausted(effective_request_id)
        if not entered:
            return _request_not_ready(
                effective_request_id,
                reason="channel_run_already_running",
                message="同一会话已有 Bot 分析正在运行",
            )
        request = _channel_request(
            user_message=user_message,
            received_monotonic=received,
            deadline_monotonic=deadline,
            authenticated_sender_id=authenticated_sender_id,
            config_key=resolved_key,
            config_path=resolved_path,
            request_id=effective_request_id,
            context_messages=_context_messages(control_context=control_context),
            channel=channel,
            sender_id=sender_id,
            conversation_id=conversation_id,
            authority_scope=authority_scope,
            read_markets=read_markets,
            read_generation=read_generation,
            bot_config_path=bot_config_path,
        )
        try:
            prepared = prepare_contract(
                request,
                reference_year=reference_year,
                report_now_ms=report_now_ms,
            )
        except Exception:
            return _channel_prepare_failed(request)
        if time.monotonic() >= deadline:
            return _budget_exhausted(effective_request_id)
        if isinstance(prepared, AppResult):
            return prepared
        with host_lane_slot("chat_read", host_store=host_store, limit=2, ttl_seconds=300, deadline_monotonic=deadline) as lane_entered:
            if time.monotonic() >= deadline:
                return _budget_exhausted(effective_request_id)
            if not lane_entered:
                return _request_not_ready(
                    request.request_id,
                    reason="channel_capacity_exhausted",
                    message="Bot 当前分析任务已达到并发上限",
                )
            try:
                result = run_prepared_contract(
                    prepared,
                    bot_config_path=bot_config_path,
                    host_store=host_store,
                    session_key=session_key,
                    control_preview_specs=control_preview_specs,
                    reply_builder=reply_builder,
                )
            except Exception:
                result = _channel_run_failed(prepared)
        return result


def _context_messages(
    *,
    control_context: tuple[dict[str, Any], ...],
) -> tuple[dict[str, Any], ...]:
    snapshot = json.dumps(list(control_context), ensure_ascii=False, sort_keys=True, default=str)
    return (
        {
            "role": "system",
            "content": (
                "Authoritative pending Control operations for this conversation, refreshed from the operation store. "
                "These are previews only, not proof of execution. Treat this snapshot as newer than chat history. "
                f"pending_operations={snapshot}"
            ),
        },
    )


def _channel_model_gate(bot_config_path: str | None) -> str | None:
    if not str(bot_config_path or "").strip():
        return "channel_model_config_missing"
    raw, load_error = load_bot_llm_config(config_path=bot_config_path, require_config=True)
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


def _channel_session_key(
    *,
    channel: str | None,
    sender_id: str | None,
    conversation_id: str | None,
    authority_scope: str,
) -> str:
    channel_key = str(channel or "").strip().lower()
    sender_key = str(sender_id or "").strip()
    if not channel_key or not sender_key:
        raise ValueError("authenticated channel identity is required")
    conversation_key = str(conversation_id or "").strip() or f"sender:{sender_key}"
    return derive_session_id(
        channel_key,
        sender_key,
        conversation_key,
        authority_scope,
    )


def _channel_read_scope(config_path: str | None, primary_market: str) -> tuple[frozenset[str], str]:
    # Missing model configuration is rejected by _channel_model_gate for real runs.
    if not str(config_path or "").strip():
        return frozenset({primary_market}), ""
    return load_bot_read_scope(config_path=config_path, primary_market=primary_market)


def _session_authority(authority_scope: str, generation: str) -> str:
    return f"{authority_scope}|{generation}" if generation else authority_scope


def _channel_request(
    *,
    user_message: str,
    config_key: str | None,
    config_path: str | None,
    request_id: str | None,
    context_messages: tuple[dict[str, str], ...],
    channel: str | None,
    sender_id: str | None,
    conversation_id: str | None,
    authority_scope: str,
    read_markets: frozenset[str] = frozenset(),
    read_generation: str = "",
    bot_config_path: str | None = None,
    received_monotonic: float | None = None,
    deadline_monotonic: float | None = None,
    authenticated_sender_id: str | None = None,
) -> BotRequest:
    normalized_channel = str(channel or "").strip().lower()
    normalized_sender = str(sender_id or "").strip()
    normalized_conversation = str(conversation_id or "").strip()
    return BotRequest(
        request_id=request_id or new_id("req"),
        source_entry="channel",
        received_monotonic=received_monotonic,
        deadline_monotonic=deadline_monotonic,
        user_message=user_message,
        explicit_scope=BotScope(config_key=config_key, config_path=config_path),
        context_messages=tuple(dict(item) for item in context_messages),
        execution_environment="channel",
        trusted_tool_scope={
            "authenticated_channel": normalized_channel,
            "authenticated_sender_id": (normalized_sender if authenticated_sender_id == normalized_sender else ""),
            "authenticated_conversation_id": normalized_conversation,
            "authority_scope": authority_scope,
            "read_markets": sorted(read_markets),
            "read_generation": read_generation,
            "bot_config_path": str(bot_config_path or ""),
        },
    )


def _request_not_ready(request_id: str, *, reason: str, message: str) -> AppResult:
    return AppResult(
        status="not_ready",
        user_response=f"{message}；本次没有调用工具。",
        error={"code": "CHANNEL_NOT_READY", "reason": reason},
        request_id=request_id,
        decision_trace={"channel_gate": reason},
    )


def _channel_prepare_failed(request: BotRequest) -> AppResult:
    return AppResult(
        status="failed",
        user_response="Bot 未能准备渠道执行合同。",
        error={"code": "CHANNEL_PREPARE_FAILED"},
        request_id=request.request_id,
        decision_trace={"service_error": "channel_prepare_contract_failed"},
        ok=False,
    )


def _channel_run_failed(contract: ExecutionContract) -> AppResult:
    return AppResult(
        status="failed",
        user_response="Bot 渠道执行失败，未返回分析结果。",
        error={"code": "CHANNEL_RUN_FAILED"},
        request_id=contract.request_id,
        contract_id=contract.contract_id,
        decision_trace={**contract.decision_trace, "channel_error": "channel_run_failed"},
        ok=False,
    )


__all__ = [
    "BotConfigScopeError",
    "config_scope_error_message",
    "resolve_trusted_config_scope",
    "run_channel_request",
]
