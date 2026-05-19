from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

from domain.domain.multi_tick import (
    FEISHU_APP_NOTIFICATION_PROVIDER,
    OPENCLAW_NOTIFICATION_PROVIDER,
    SUPPORTED_NOTIFICATION_PROVIDERS,
    normalize_notification_provider,
)
from domain.domain.tool_boundary import normalize_notify_subprocess_output, normalize_subprocess_adapter_payload
from src.application.secret_resolver import resolve_feishu_bot_config
from src.infrastructure.external_services import send_openclaw_message_process
from src.infrastructure.feishu_bitable import FeishuError
from src.infrastructure.feishu_bot import send_text_message


@dataclass(frozen=True)
class NotificationDeliveryAdapter:
    send_fn: Callable[..., Any]
    normalize_fn: Callable[..., dict[str, Any]]
    failure_stage: str


def resolve_feishu_bot_send_target(
    *,
    notifications: dict[str, Any] | None = None,
) -> str:
    return resolve_feishu_bot_config(notifications).user_open_id


def send_feishu_app_message(
    *,
    base: Path,
    channel: str,
    target: str,
    message: str,
    notifications: dict[str, Any] | None = None,
    receive_id_type: str = "open_id",
) -> dict[str, Any]:
    del base, target
    resolved_channel = str(channel or "").strip().lower()
    if resolved_channel != FEISHU_APP_NOTIFICATION_PROVIDER:
        raise ValueError(f"unsupported notification provider for feishu app sender: {channel}")

    bot_cfg = resolve_feishu_bot_config(notifications)
    missing = bot_cfg.credential_missing_fields
    if missing:
        raise ValueError("Feishu bot env missing required fields: " + ", ".join(missing))

    receive_id = bot_cfg.user_open_id
    if not receive_id:
        raise ValueError("Feishu bot user open_id is required")
    if receive_id_type != "open_id":
        raise ValueError(f"unsupported receive_id_type for phase1: {receive_id_type}")

    request_path = f"/open-apis/im/v1/messages?receive_id_type={receive_id_type}"
    try:
        response_json = send_text_message(
            app_id=bot_cfg.app_id,
            app_secret=bot_cfg.app_secret,
            open_id=receive_id,
            text=str(message or ""),
        )
        return {
            "ok": True,
            "http_status": 200,
            "request_path": request_path,
            "response_json": response_json,
            "response_tail": json.dumps(response_json, ensure_ascii=False)[-500:],
        }
    except FeishuError as exc:
        response = exc.response if isinstance(exc.response, dict) else {}
        body_text = str(response.get("body") or "")
        response_json = response if isinstance(response.get("code"), int) else None
        if body_text:
            try:
                parsed = json.loads(body_text)
                if isinstance(parsed, dict):
                    response_json = parsed
            except Exception:
                pass
        return {
            "ok": False,
            "http_status": response.get("http_status"),
            "request_path": request_path,
            "response_json": response_json,
            "response_tail": body_text[-500:],
            "error_type": type(exc).__name__,
            "error_message": str(exc),
        }


def normalize_feishu_app_send_output(*, send_result: dict[str, Any]) -> dict[str, Any]:
    result = send_result if isinstance(send_result, dict) else {}
    raw_response_json = result.get("response_json")
    response_json: dict[str, Any] = raw_response_json if isinstance(raw_response_json, dict) else {}
    raw_data = response_json.get("data")
    data: dict[str, Any] = raw_data if isinstance(raw_data, dict) else {}
    message_id = data.get("message_id")
    http_status = result.get("http_status")
    feishu_code = response_json.get("code") if isinstance(response_json.get("code"), int) else None
    feishu_msg = str(response_json.get("msg") or result.get("error_message") or "").strip()
    request_path = str(result.get("request_path") or "/open-apis/im/v1/messages?receive_id_type=open_id")
    response_tail = str(result.get("response_tail") or "")

    command_ok = http_status == 200
    delivery_confirmed = bool(command_ok and feishu_code == 0 and message_id)
    ok = delivery_confirmed

    if ok:
        message = f"message_id={message_id}"
    elif command_ok and feishu_code == 0 and not message_id:
        message = "feishu send returned success but data.message_id is missing"
    else:
        parts = [
            f"http_status={http_status}",
            f"feishu_code={feishu_code}",
            f"feishu_msg={feishu_msg or ''}",
            f"message_id={message_id}",
            f"request_path={request_path}",
        ]
        if response_tail:
            parts.append(f"response_tail={response_tail}")
        message = " ".join(parts)

    return normalize_subprocess_adapter_payload(
        adapter="notify",
        tool_name="feishu_app_message_send",
        returncode=(0 if command_ok else 1),
        stdout=response_tail,
        stderr="",
        ok=ok,
        message=message,
        extra={
            "command_ok": command_ok,
            "delivery_confirmed": delivery_confirmed,
            "message_id": (None if message_id is None else str(message_id)),
            "http_status": http_status,
            "feishu_code": feishu_code,
            "feishu_msg": feishu_msg,
            "request_path": request_path,
            "response_tail": response_tail,
        },
    )


def send_feishu_app_message_process(
    *,
    base: Path,
    channel: str,
    target: str,
    message: str,
    notifications: dict[str, Any] | None = None,
) -> Any:
    send_result = send_feishu_app_message(
        base=base,
        channel=channel,
        target=target,
        message=message,
        notifications=notifications,
    )
    normalized = normalize_feishu_app_send_output(send_result=send_result)
    stdout = ""
    if isinstance(send_result, dict):
        response_json = send_result.get("response_json")
        if isinstance(response_json, dict) and response_json:
            stdout = json.dumps(response_json, ensure_ascii=False)
        elif send_result.get("response_tail"):
            stdout = str(send_result.get("response_tail") or "")
    stderr = "" if bool(normalized.get("command_ok")) else str(normalized.get("message") or "")
    return SimpleNamespace(returncode=int(normalized.get("returncode") or 0), stdout=stdout, stderr=stderr, raw=send_result)


def select_notification_delivery_adapter(provider: Any) -> NotificationDeliveryAdapter:
    resolved_provider = normalize_notification_provider(provider)
    if resolved_provider == FEISHU_APP_NOTIFICATION_PROVIDER:
        return NotificationDeliveryAdapter(
            send_fn=send_feishu_app_message_process,
            normalize_fn=normalize_feishu_app_send_output,
            failure_stage="send_feishu_app_message",
        )
    if resolved_provider == OPENCLAW_NOTIFICATION_PROVIDER:
        return NotificationDeliveryAdapter(
            send_fn=send_openclaw_message_process,
            normalize_fn=normalize_notify_subprocess_output,
            failure_stage="send_openclaw_message",
        )
    allowed = ", ".join(SUPPORTED_NOTIFICATION_PROVIDERS)
    raise ValueError(f"unsupported notification provider: {provider}; expected one of: {allowed}")

