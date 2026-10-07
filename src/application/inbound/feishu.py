from __future__ import annotations

import json
import re
import time
from typing import Any, Callable, cast

from src.application.agent_tool_contracts import AgentToolError, build_response
from src.application.bot.control.contracts import BotInboundRequest
from src.application.bot.control.policy import check_sender_allowed
from src.application.payload_helpers import as_dict as _dict
from src.application.payload_helpers import first_text as _first_text

ExecuteToolFn = Callable[[str, dict[str, Any]], dict[str, Any]]


def prepare_feishu_ack_target(
    payload: dict[str, Any],
    *,
    allowed_senders: str | None,
) -> dict[str, Any]:
    """Return a side-effect-free target for a best-effort Feishu ACK."""

    event_type = _extract_event_type(payload)
    if event_type != "im.message.receive_v1":
        return {"ready": False, "reason": "unsupported_event"}

    try:
        request = feishu_payload_to_inbound_request(payload)
    except AgentToolError:
        return {"ready": False, "reason": "invalid_message"}

    message_id = str(request.message_id or "").strip()
    if not message_id:
        return {"ready": False, "reason": "invalid_message"}

    decision = check_sender_allowed(
        channel="feishu",
        sender_id=request.sender_id,
        allowed_senders=allowed_senders,
    )
    if not decision.allowed:
        return {
            "ready": False,
            "reason": "permission_denied",
            "sender_decision": decision.public_payload(),
        }
    return {
        "ready": True,
        "reason": "accepted_sender",
        "message_id": message_id,
        "sender_decision": decision.public_payload(),
    }


def prepare_feishu_analysis_control(
    payload: dict[str, Any], *, allowed_senders: str | None, config_key: str | None,
    config_path: str | None, audit_db: str | None, received_monotonic: float,
    bot_config_path: str | None = None,
) -> dict[str, Any] | None:
    from src.application.bot.control.audit import InboundAuditStore
    from src.application.bot.channel_facade import analysis_control_replacement, cancel_channel_analysis

    if _extract_event_type(payload) != "im.message.receive_v1":
        return None
    try:
        request = feishu_payload_to_inbound_request(payload, config_key=config_key,
            config_path=config_path, audit_db=audit_db, bot_config_path=bot_config_path,
            received_monotonic=received_monotonic)
    except AgentToolError:
        return None
    if analysis_control_replacement(request.text) is None:
        return None
    decision = check_sender_allowed(channel="feishu", sender_id=request.sender_id, allowed_senders=allowed_senders)
    if not decision.allowed:
        raise AgentToolError(code="PERMISSION_DENIED", message="analysis control sender is not authorized")
    if time.monotonic() >= received_monotonic + 180:
        raise AgentToolError(code="BUDGET_EXHAUSTED", message="analysis control deadline exceeded")
    return cancel_channel_analysis(request=request,
        audit_store=InboundAuditStore(audit_db, deadline_monotonic=received_monotonic + 180))


def handle_feishu_payload(
    payload: dict[str, Any],
    *,
    config_key: str | None = None,
    config_path: str | None = None,
    audit_db: str | None = None,
    execute_tool_fn: ExecuteToolFn | None = None,
    allowed_senders: str | None = None,
    bot_settings: Any | None = None,
    bot_config_path: str | None = None,
    bot_reply_options: dict[str, Any] | None = None,
    received_monotonic: float | None = None,
) -> dict[str, Any]:
    received_monotonic = time.monotonic() if received_monotonic is None else received_monotonic
    event_type = _extract_event_type(payload)
    if event_type and event_type != "im.message.receive_v1":
        return build_response(
            tool_name="inbound.feishu",
            ok=True,
            data={
                "kind": "ignored_event",
                "event_type": event_type,
                "reason": "unsupported_event_type",
            },
        )

    request = feishu_payload_to_inbound_request(
        payload,
        config_key=config_key,
        config_path=config_path,
        audit_db=audit_db,
        bot_config_path=bot_config_path,
        received_monotonic=received_monotonic,
        bot_reply_options=bot_reply_options,
    )
    kwargs: dict[str, Any] = {"allowed_senders": allowed_senders}
    if execute_tool_fn is not None:
        kwargs["execute_tool_fn"] = execute_tool_fn
    settings = bot_settings or _bot_settings(
        config_key=config_key,
        config_path=config_path,
        bot_config_path=bot_config_path,
    )
    from src.application.bot.control.runtime import handle_bot_turn

    kwargs["settings"] = settings
    turn = handle_bot_turn(request, **kwargs)
    inbound_result = turn.public_payload()
    return build_response(
        tool_name="inbound.feishu",
        ok=turn.ok,
        data={
            "kind": "message",
            "event_type": event_type,
            "request": request.public_payload(),
            "response_text": turn.response_text,
            "inbound_result": inbound_result,
        },
        error=turn.error if not turn.ok else None,
        meta=dict(turn.meta or {}),
    )


def _bot_settings(
    *,
    config_key: str | None,
    config_path: str | None,
    bot_config_path: str | None = None,
) -> Any:
    from src.application.bot.control.settings import BotSettings
    from src.application.bot.control.config_loader import load_bot_config

    del config_key, config_path
    bot_explicit = bool(bot_config_path is not None and str(bot_config_path).strip())
    bot_path, bot_cfg = load_bot_config(config_path=bot_config_path, missing_ok=not bot_explicit)
    del bot_path
    if bot_cfg:
        configured = BotSettings.from_runtime_config(bot_cfg)
        return BotSettings(
            enabled=configured.enabled,
            context_window_messages=configured.context_window_messages,
            default_market_scope=configured.default_market_scope,
            llm=configured.llm,
        )

    return BotSettings()


def feishu_payload_to_inbound_request(
    payload: dict[str, Any],
    *,
    config_key: str | None = None,
    config_path: str | None = None,
    audit_db: str | None = None,
    bot_config_path: str | None = None,
    bot_reply_options: dict[str, Any] | None = None,
    received_monotonic: float | None = None,
) -> BotInboundRequest:
    received_monotonic = time.monotonic() if received_monotonic is None else received_monotonic
    event = _dict(payload.get("event"))
    message = _dict(event.get("message"))
    sender = _dict(event.get("sender"))
    sender_ids = _dict(sender.get("sender_id"))

    sender_id = _first_text(
        sender_ids.get("open_id"),
        sender_ids.get("user_id"),
        sender_ids.get("union_id"),
        sender.get("open_id"),
        sender.get("user_id"),
        sender.get("union_id"),
    )
    if not sender_id:
        raise AgentToolError(
            code="INPUT_ERROR",
            message="failed to extract Feishu sender id",
        )

    message_id = _first_text(
        message.get("message_id"),
        _dict(payload.get("header")).get("event_id"),
        payload.get("uuid"),
    )
    chat_id = _first_text(message.get("chat_id"), _dict(message.get("chat")).get("chat_id"))
    text = _extract_message_text(message)
    if not text:
        raise AgentToolError(
            code="INPUT_ERROR",
            message="failed to extract Feishu text message",
            hint="Only Feishu text messages are supported by the thin inbound adapter.",
        )

    return BotInboundRequest(
        text=text,
        reply_context=dict(bot_reply_options or {}),
        received_monotonic=received_monotonic,
        sender_id=sender_id,
        channel="feishu",
        message_id=message_id,
        conversation_id=f"feishu:{chat_id}:{sender_id}" if chat_id else None,
        config_key=config_key,
        config_path=config_path,
        audit_db=audit_db,
        bot_config_path=bot_config_path,
    )


def _extract_event_type(payload: dict[str, Any]) -> str | None:
    header = _dict(payload.get("header"))
    return _first_text(header.get("event_type"), _dict(payload.get("event")).get("type"))


def _extract_message_text(message: dict[str, Any]) -> str | None:
    message_type = str(message.get("message_type") or "").strip().lower()
    if message_type and message_type != "text":
        return None
    content = message.get("content")
    if isinstance(content, str):
        parsed = _parse_json_object(content)
        if parsed is not None:
            text = _first_text(parsed.get("text"), parsed.get("content"))
        else:
            text = content
    elif isinstance(content, dict):
        text = _first_text(content.get("text"), content.get("content"))
    else:
        text = _first_text(message.get("text"))
    return _clean_text(text)


def _parse_json_object(raw: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(raw)
    except Exception:
        return None
    return cast(dict[str, Any], parsed) if isinstance(parsed, dict) else None


def _clean_text(text: str | None) -> str | None:
    value = str(text or "").strip()
    if not value:
        return None
    value = re.sub(r"<at\b[^>]*>.*?</at>", "", value, flags=re.IGNORECASE | re.DOTALL)
    value = re.sub(r"\s+", " ", value).strip()
    return value or None
