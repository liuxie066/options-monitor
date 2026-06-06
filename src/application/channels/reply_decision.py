from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


PERMISSION_DENIED_CODE = "PERMISSION_DENIED"


@dataclass(frozen=True)
class InboundReplyDecision:
    should_send: bool
    status: dict[str, Any]
    text: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    inbound_result: dict[str, Any] = field(default_factory=dict)
    send_reason: str = "sent"


def decide_inbound_reply(
    inbound: dict[str, Any],
    *,
    reply_enabled: bool,
    max_reply_chars: int,
    permission_denied_message_fn: Callable[[dict[str, Any]], str] | None = None,
) -> InboundReplyDecision:
    data = inbound_message_data(inbound)
    if data is None:
        return InboundReplyDecision(False, {"attempted": False, "ok": True, "reason": "not_message"})

    inbound_result = inbound_message_result(data)
    permission_denied = inbound_error_code(inbound_result) == PERMISSION_DENIED_CODE
    if permission_denied and permission_denied_should_stay_silent(inbound_result):
        return InboundReplyDecision(
            False,
            {"attempted": False, "ok": True, "reason": "permission_denied"},
            data=data,
            inbound_result=inbound_result,
        )
    if not reply_enabled:
        return InboundReplyDecision(
            False,
            {"attempted": False, "ok": True, "reason": "reply_disabled"},
            data=data,
            inbound_result=inbound_result,
        )

    response_text = trim_reply(str(data.get("response_text") or ""), max_chars=max_reply_chars)
    send_reason = "permission_denied_sent" if permission_denied else "sent"
    if not response_text and permission_denied and permission_denied_message_fn is not None:
        response_text = trim_reply(permission_denied_message_fn(inbound_result), max_chars=max_reply_chars)
    if not response_text:
        return InboundReplyDecision(
            False,
            {"attempted": False, "ok": True, "reason": "empty_response"},
            data=data,
            inbound_result=inbound_result,
        )
    return InboundReplyDecision(
        True,
        {},
        text=response_text,
        data=data,
        inbound_result=inbound_result,
        send_reason=send_reason,
    )


def inbound_message_data(inbound: dict[str, Any]) -> dict[str, Any] | None:
    data = _dict(inbound.get("data"))
    return data if data.get("kind") == "message" else None


def inbound_message_result(data: dict[str, Any]) -> dict[str, Any]:
    return _dict(data.get("inbound_result"))


def inbound_error_code(inbound_result: dict[str, Any]) -> str | None:
    error = _dict(inbound_result.get("error"))
    return _first_text(error.get("code"))


def inbound_command_id(inbound_result: dict[str, Any]) -> str | None:
    data = _dict(inbound_result.get("data"))
    return _first_text(data.get("command_id"))


def permission_denied_should_stay_silent(inbound_result: dict[str, Any]) -> bool:
    error = _dict(inbound_result.get("error"))
    details = _dict(error.get("details"))
    reason = str(details.get("reason") or "").strip()
    message = str(error.get("message") or "").strip()
    return reason in {"sender_not_allowed", "missing_sender"} or message in {
        "sender is not allowed to use assistant control",
        "sender is not allowed to use inbound control",
    }


def permission_denied_message(inbound_result: dict[str, Any]) -> str:
    error = _dict(inbound_result.get("error"))
    message = str(error.get("message") or "写入权限未开启").strip()
    hint = str(error.get("hint") or "").strip()
    return f"{message}{(' ' + hint) if hint else ''}".strip()


def trim_reply(value: str, *, max_chars: int) -> str:
    text = str(value or "").strip()
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 20)].rstrip() + "\n...(truncated)"


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _first_text(*values: Any) -> str | None:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return None


__all__ = [
    "InboundReplyDecision",
    "PERMISSION_DENIED_CODE",
    "decide_inbound_reply",
    "inbound_command_id",
    "inbound_error_code",
    "inbound_message_data",
    "inbound_message_result",
    "permission_denied_message",
    "permission_denied_should_stay_silent",
    "trim_reply",
]
