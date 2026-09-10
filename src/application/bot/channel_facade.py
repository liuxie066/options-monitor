from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone
from typing import Any

from src.application.agent_tool_contracts import AgentToolError
from src.application.agent_tool_config import (
    DEFAULT_CONFIGS,
    load_runtime_config,
    repo_base,
    resolve_runtime_config_path,
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
from src.application.bot.model_config import load_assistant_llm_config, model_api_key_configured
from src.application.bot.service import prepare_contract
from src.application.runtime_config_freshness import (
    RuntimeConfigFreshnessError,
    ensure_runtime_config_freshness,
    infer_runtime_config_market,
)
from src.infrastructure.pi_agent_process import derive_pi_session_id


class BotConfigScopeError(ValueError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def run_channel_request(
    *,
    user_message: str,
    config_key: str | None,
    config_path: str | None = None,
    request_id: str | None = None,
    reference_year: int | None = None,
    assistant_config_path: str | None = None,
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
        session_key = _channel_session_key(
            channel=channel,
            sender_id=sender_id,
            conversation_id=conversation_id,
            authority_scope=authority_scope,
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
    model_gate = _channel_model_gate(assistant_config_path)
    if model_gate:
        return _request_not_ready(
            effective_request_id,
            reason=model_gate,
            message="渠道 Bot 需要显式可用的 assistant 模型配置",
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
                    assistant_config_path=assistant_config_path,
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


def resolve_trusted_config_scope(
    *,
    config_key: str | None,
    config_path: str | None,
) -> tuple[str, str, str]:
    key = str(config_key or "").strip().lower()
    raw_path = str(config_path or "").strip()
    if bool(key) == bool(raw_path):
        raise BotConfigScopeError("channel_identity_or_scope_invalid")
    if key and key not in DEFAULT_CONFIGS:
        raise BotConfigScopeError("channel_identity_or_scope_invalid")

    requested_path = resolve_runtime_config_path(
        config_key=key or None,
        config_path=raw_path or None,
    )
    try:
        if not requested_path.exists():
            raise BotConfigScopeError("config_missing")
        if not requested_path.is_file():
            raise BotConfigScopeError("channel_identity_or_scope_invalid")
        resolved, cfg = load_runtime_config(
            config_key=key or None,
            config_path=raw_path or None,
        )
    except BotConfigScopeError:
        raise
    except AgentToolError as exc:
        details = exc.details if isinstance(exc.details, dict) else {}
        errors = details.get("errors")
        reason = (
            "config_identity_mismatch"
            if isinstance(errors, list) and errors
            else "config_unreadable"
        )
        raise BotConfigScopeError(reason) from exc
    except (OSError, RuntimeError, ValueError) as exc:
        raise BotConfigScopeError("config_unreadable") from exc

    canonical_path = str(resolved.resolve())
    actual_market = infer_runtime_config_market(
        config_key=key or None,
        config_path=resolved,
        config=cfg,
    )
    if actual_market not in DEFAULT_CONFIGS:
        raise BotConfigScopeError("config_identity_mismatch")
    try:
        ensure_runtime_config_freshness(
            cfg,
            repo_root=repo_base(),
            market=actual_market,
            runtime_config_path=resolved,
        )
    except RuntimeConfigFreshnessError as exc:
        raise BotConfigScopeError("config_stale") from exc
    except OSError as exc:
        raise BotConfigScopeError("config_unreadable") from exc

    if key:
        return actual_market, canonical_path, f"key:{key}"
    path_digest = hashlib.sha256(canonical_path.encode("utf-8")).hexdigest()
    return actual_market, canonical_path, f"path:{path_digest}"


def config_scope_error_message(reason: str) -> str:
    return {
        "config_missing": "已授权市场的运行配置缺失，请先生成运行配置",
        "config_stale": "已授权市场的运行配置已过期，请重新生成运行配置",
        "config_unreadable": "已授权市场的运行配置当前无法读取",
        "config_identity_mismatch": "运行配置与已授权市场身份不一致",
    }.get(reason, "渠道身份或数据作用域不可用")


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
    return derive_pi_session_id(
        channel_key,
        sender_key,
        conversation_key,
        authority_scope,
    )


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
