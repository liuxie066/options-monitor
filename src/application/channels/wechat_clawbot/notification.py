from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

from domain.domain.tool_boundary import normalize_subprocess_adapter_payload
from src.application.channels.wechat_clawbot.ilink_client import WechatClawbotClient
from src.application.channels.wechat_clawbot.state import (
    load_wechat_clawbot_binding,
    load_wechat_clawbot_state,
)


ClientFactory = Callable[..., WechatClawbotClient]


def _response_success(response: dict[str, Any]) -> bool:
    if response == {}:
        return True
    if response.get("ok") is True:
        return True
    for key in ("ret", "errcode", "code"):
        value = response.get(key)
        if isinstance(value, int):
            return value == 0
        if isinstance(value, str) and value.strip().lstrip("-").isdigit():
            return int(value) == 0
    return False


def _response_code(response: dict[str, Any]) -> int | None:
    for key in ("ret", "errcode", "code"):
        value = response.get(key)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.strip().lstrip("-").isdigit():
            return int(value)
    data = response.get("data")
    if isinstance(data, dict):
        nested = _response_code(data)
        if nested is not None:
            return nested
    result = response.get("result")
    if isinstance(result, dict):
        nested = _response_code(result)
        if nested is not None:
            return nested
    return None


def _extract_message_id(response: dict[str, Any]) -> str | None:
    for key in ("message_id", "messageId", "id", "client_msg_id"):
        value = response.get(key)
        if value:
            return str(value)
    data = response.get("data")
    if isinstance(data, dict):
        return _extract_message_id(data)
    result = response.get("result")
    if isinstance(result, dict):
        return _extract_message_id(result)
    return None


def _local_receipt_id(*, idempotency_key: str | None, target: str, message: str) -> str:
    if idempotency_key:
        return str(idempotency_key)
    raw = f"{target}\n{hashlib.sha256(str(message or '').encode('utf-8')).hexdigest()}"
    return "wechat-clawbot-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _client_id_from_idempotency_key(idempotency_key: str | None) -> str | None:
    key = str(idempotency_key or "").strip()
    if not key:
        return None
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]


def send_wechat_clawbot_message(
    *,
    base: Path,
    channel: str,
    target: str,
    message: str,
    notifications: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
    client_factory: ClientFactory = WechatClawbotClient,
) -> dict[str, Any]:
    if str(channel or "").strip().lower() != "wechat_clawbot":
        raise ValueError(f"unsupported notification provider for WeChat ClawBot sender: {channel}")
    text = str(message or "")
    if not text.strip():
        raise ValueError("message is required")
    binding = load_wechat_clawbot_binding(base=base, target=target, notifications=notifications)
    state = load_wechat_clawbot_state(base=base, label=binding.label, notifications=notifications)
    timeout_sec = int((notifications or {}).get("send_timeout_sec") or 60)
    client = client_factory(bot_token=state.bot_token, base_url=state.base_url, timeout=timeout_sec)
    response_json = client.send_text_message(
        to_user_id=binding.to_user_id,
        group_id=binding.group_id,
        context_token=binding.context_token,
        text=text,
        client_id=_client_id_from_idempotency_key(idempotency_key),
    )
    ok = _response_success(response_json)
    message_id = _extract_message_id(response_json)
    local_receipt_id = _local_receipt_id(idempotency_key=idempotency_key, target=binding.target, message=text)
    return {
        "ok": ok,
        "http_status": 200,
        "request_path": "/ilink/bot/sendmessage",
        "response_json": response_json,
        "response_tail": json.dumps(response_json, ensure_ascii=False)[-500:],
        "message_id": message_id,
        "provider_response_code": _response_code(response_json),
        "local_receipt_id": local_receipt_id,
        "idempotency_key": idempotency_key,
        "binding_label": binding.label,
        "binding_name": binding.name,
        "target": binding.target,
    }


def normalize_wechat_clawbot_send_output(*, send_result: dict[str, Any]) -> dict[str, Any]:
    result = send_result if isinstance(send_result, dict) else {}
    response_json = result.get("response_json") if isinstance(result.get("response_json"), dict) else {}
    command_ok = bool(result.get("http_status") == 200)
    business_ok = bool(result.get("ok") or _response_success(response_json))
    upstream_message_id = _extract_message_id(response_json) or result.get("message_id")
    local_receipt_id = result.get("local_receipt_id")
    message_id = upstream_message_id or local_receipt_id
    provider_response_code = _response_code(response_json)
    delivery_confirmed = bool(command_ok and business_ok and message_id)
    response_tail = str(result.get("response_tail") or "")
    if delivery_confirmed:
        message = f"message_id={message_id}"
    elif command_ok and business_ok:
        message = (
            "wechat_clawbot send returned success but upstream message_id is missing "
            f"response_tail={response_tail}"
        ).strip()
    else:
        message = (
            "wechat_clawbot send failed "
            f"http_status={result.get('http_status')} "
            f"response_tail={response_tail}"
        ).strip()
    return normalize_subprocess_adapter_payload(
        adapter="notify",
        tool_name="wechat_clawbot_message_send",
        returncode=(0 if command_ok else 1),
        stdout=response_tail,
        stderr="" if command_ok else message,
        ok=delivery_confirmed,
        message=message,
        extra={
            "command_ok": command_ok,
            "delivery_confirmed": delivery_confirmed,
            "message_id": (None if message_id is None else str(message_id)),
            "upstream_message_id": (None if upstream_message_id is None else str(upstream_message_id)),
            "http_status": result.get("http_status"),
            "provider_response_code": provider_response_code,
            "response_json": response_json,
            "response_tail": response_tail,
            "idempotency_key": result.get("idempotency_key"),
            "local_receipt_id": local_receipt_id,
            "binding_label": result.get("binding_label"),
            "binding_name": result.get("binding_name"),
        },
    )


def send_wechat_clawbot_message_process(
    *,
    base: Path,
    channel: str,
    target: str,
    message: str,
    notifications: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
) -> Any:
    send_result = send_wechat_clawbot_message(
        base=base,
        channel=channel,
        target=target,
        message=message,
        notifications=notifications,
        idempotency_key=idempotency_key,
    )
    normalized = normalize_wechat_clawbot_send_output(send_result=send_result)
    stdout = json.dumps(send_result.get("response_json") or {}, ensure_ascii=False)
    stderr = "" if bool(normalized.get("command_ok")) else str(normalized.get("message") or "")
    return SimpleNamespace(returncode=int(normalized.get("returncode") or 0), stdout=stdout, stderr=stderr, raw=send_result)
