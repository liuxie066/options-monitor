from __future__ import annotations

import json
import re
from typing import Any, cast

from src.application.agent_tool_contracts import AgentToolError, build_response
from src.application.inbound.contracts import InboundRequest
from src.application.inbound.router import ExecuteToolFn, handle_inbound_request


def handle_feishu_payload(
    payload: dict[str, Any],
    *,
    config_key: str | None = "us",
    config_path: str | None = None,
    audit_db: str | None = None,
    execute_tool_fn: ExecuteToolFn | None = None,
    allowed_senders: str | None = None,
) -> dict[str, Any]:
    challenge = _extract_challenge(payload)
    if challenge:
        return build_response(
            tool_name="inbound.feishu",
            ok=True,
            data={
                "kind": "url_verification",
                "challenge": challenge,
                "response": {"challenge": challenge},
            },
        )

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
    )
    kwargs: dict[str, Any] = {"allowed_senders": allowed_senders}
    if execute_tool_fn is not None:
        kwargs["execute_tool_fn"] = execute_tool_fn
    inbound_result = handle_inbound_request(request, **kwargs)
    data_raw = inbound_result.get("data")
    data = cast(dict[str, Any], data_raw) if isinstance(data_raw, dict) else {}
    return build_response(
        tool_name="inbound.feishu",
        ok=bool(inbound_result.get("ok", False)),
        data={
            "kind": "message",
            "event_type": event_type,
            "request": request.public_payload(),
            "response_text": str(data.get("response_text") or ""),
            "inbound_result": inbound_result,
        },
        error=inbound_result.get("error") if not bool(inbound_result.get("ok", False)) else None,
        meta=dict(inbound_result.get("meta") or {}),
    )


def feishu_payload_to_inbound_request(
    payload: dict[str, Any],
    *,
    config_key: str | None = "us",
    config_path: str | None = None,
    audit_db: str | None = None,
) -> InboundRequest:
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
    text = _extract_message_text(message)
    if not text:
        raise AgentToolError(
            code="INPUT_ERROR",
            message="failed to extract Feishu text message",
            hint="Only Feishu text messages are supported by the thin inbound adapter.",
        )

    return InboundRequest(
        text=text,
        sender_id=sender_id,
        channel="feishu",
        message_id=message_id,
        config_key=config_key,
        config_path=config_path,
        audit_db=audit_db,
    )


def _extract_challenge(payload: dict[str, Any]) -> str | None:
    if str(payload.get("type") or "").strip() == "url_verification":
        return _first_text(payload.get("challenge"))
    if str(payload.get("schema") or "").strip() == "2.0" and payload.get("challenge"):
        return _first_text(payload.get("challenge"))
    return None


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


def _dict(value: Any) -> dict[str, Any]:
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def _first_text(*values: Any) -> str | None:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return None
