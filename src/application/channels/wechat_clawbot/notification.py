from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

from domain.domain.tool_boundary import normalize_subprocess_adapter_payload
from domain.domain.multi_tick import WECHAT_CLAWBOT_NOTIFICATION_PROVIDER, notification_route_identity_hash
from src.application.channels.wechat_clawbot.ilink_client import WechatClawbotClient
from src.application.channels.wechat_clawbot.message import response_code, response_message_id, response_success
from src.application.channels.wechat_clawbot.state import (
    load_wechat_clawbot_binding,
    load_wechat_clawbot_state,
    resolve_wechat_clawbot_state_dir,
    resolve_wechat_clawbot_target,
)

from src.application.channels.wechat_clawbot.state_store import WechatClawbotStateStore


ClientFactory = Callable[..., WechatClawbotClient]


def _response_code(response: dict[str, Any]) -> int | None:
    top_level = response_code(response)
    if top_level is not None:
        return top_level
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


def _context_token_rejected(response: dict[str, Any]) -> bool:
    return _response_code(response) == -2


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
    resolved = resolve_wechat_clawbot_target(target, notifications=notifications)
    state_dir = resolve_wechat_clawbot_state_dir(base=base, label=resolved.label, notifications=notifications)
    with WechatClawbotStateStore(state_dir).read_directory() as store:
        binding = load_wechat_clawbot_binding(base=base, target=target, notifications=notifications, store=store)
        state = load_wechat_clawbot_state(base=base, label=binding.label, notifications=notifications, store=store)
        sender_scope = store.sender_directory_scope()
    attribution_route_key = (notification_route_identity_hash(
        provider=WECHAT_CLAWBOT_NOTIFICATION_PROVIDER, channel=WECHAT_CLAWBOT_NOTIFICATION_PROVIDER,
        sender=[state.label, *sender_scope],
        recipient=[binding.to_user_id, binding.group_id, binding.chat_key]) if sender_scope else None)
    timeout_sec = int((notifications or {}).get("send_timeout_sec") or 60)
    client = client_factory(bot_token=state.bot_token, base_url=state.base_url, timeout=timeout_sec)
    client_id = _client_id_from_idempotency_key(idempotency_key)
    response_json = client.send_text_message(
        to_user_id=binding.to_user_id,
        group_id=binding.group_id,
        context_token=binding.context_token,
        text=text,
        client_id=client_id,
    )
    initial_response_json = response_json
    fallback_response_json: dict[str, Any] | None = None
    if binding.context_token and _context_token_rejected(response_json):
        fallback_response_json = client.send_text_message(
            to_user_id=binding.to_user_id,
            group_id=binding.group_id,
            context_token="",
            text=text,
            client_id=client_id,
        )
        response_json = fallback_response_json
    ok = response_success(response_json)
    message_id = response_message_id(response_json)
    local_receipt_id = _local_receipt_id(idempotency_key=idempotency_key, target=binding.target, message=text)
    return {
        "ok": ok,
        "http_status": 200,
        "request_path": "/ilink/bot/sendmessage",
        "message_id": message_id,
        "provider_response_code": _response_code(response_json),
        "context_token_fallback_attempted": fallback_response_json is not None,
        "context_token_fallback_response_code": (
            None if fallback_response_json is None else _response_code(fallback_response_json)
        ),
        "initial_response_code": (None if fallback_response_json is None else _response_code(initial_response_json)),
        "local_receipt_id": local_receipt_id,
        "idempotency_key": idempotency_key,
        "binding_label": binding.label,
        "binding_name": binding.name,
        "target": binding.target,
        "attribution_route_key": attribution_route_key,
    }


def normalize_wechat_clawbot_send_output(*, send_result: dict[str, Any]) -> dict[str, Any]:
    result = send_result if isinstance(send_result, dict) else {}
    response_json = result.get("response_json") if isinstance(result.get("response_json"), dict) else {}
    command_ok = bool(result.get("http_status") == 200)
    # An explicit sender failure or absent response cannot prove delivery.
    business_ok = (result.get("ok") is True if "ok" in result else
                   isinstance(result.get("response_json"), dict) and response_success(response_json))
    upstream_message_id = response_message_id(response_json) or result.get("message_id")
    local_receipt_id = result.get("local_receipt_id")
    message_id = upstream_message_id or local_receipt_id
    provider_response_code = result.get("provider_response_code")
    if not isinstance(provider_response_code, int):
        provider_response_code = _response_code(response_json)
    delivery_confirmed = bool(command_ok and business_ok and message_id)
    if delivery_confirmed:
        message = f"message_id={message_id}"
    elif command_ok and business_ok:
        message = (
            "wechat_clawbot send returned success but upstream message_id is missing "
            f"provider_response_code={provider_response_code}"
        ).strip()
    else:
        message = (
            "wechat_clawbot send failed "
            f"http_status={result.get('http_status')} "
            f"provider_response_code={provider_response_code}"
        ).strip()
    return normalize_subprocess_adapter_payload(
        adapter="notify",
        tool_name="wechat_clawbot_message_send",
        returncode=(0 if command_ok else 1),
        stdout="",
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
            "idempotency_key": result.get("idempotency_key"),
            "local_receipt_id": local_receipt_id,
            "binding_label": result.get("binding_label"),
            "binding_name": result.get("binding_name"),
            "attribution_route_key": result.get("attribution_route_key"),
            "context_token_fallback_attempted": bool(result.get("context_token_fallback_attempted")),
            "context_token_fallback_response_code": result.get("context_token_fallback_response_code"),
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
    stdout = json.dumps(
        {
            "ok": bool(normalized.get("ok")),
            "http_status": normalized.get("http_status"),
            "provider_response_code": normalized.get("provider_response_code"),
            "message_id": normalized.get("message_id"),
        },
        ensure_ascii=False,
    )
    stderr = "" if bool(normalized.get("command_ok")) else str(normalized.get("message") or "")
    return SimpleNamespace(returncode=int(normalized.get("returncode") or 0), stdout=stdout, stderr=stderr, raw=send_result)
