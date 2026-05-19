from __future__ import annotations

import json
from typing import Any, Callable
from urllib.parse import quote

from src.infrastructure.feishu_bitable import http_json, with_tenant_token_retry


HttpJsonFn = Callable[..., dict[str, Any]]


def reply_text_message(
    *,
    app_id: str,
    app_secret: str,
    message_id: str,
    text: str,
    uuid: str | None = None,
    reply_in_thread: bool | None = None,
    http_json_fn: HttpJsonFn = http_json,
) -> dict[str, Any]:
    message_id_value = str(message_id or "").strip()
    text_value = str(text or "").strip()
    if not message_id_value:
        raise ValueError("message_id is required")
    if not text_value:
        raise ValueError("text is required")

    url = f"https://open.feishu.cn/open-apis/im/v1/messages/{quote(message_id_value, safe='')}/reply"
    payload: dict[str, Any] = {
        "msg_type": "text",
        "content": json.dumps({"text": text_value}, ensure_ascii=False),
    }
    if uuid:
        payload["uuid"] = str(uuid)
    if reply_in_thread is not None:
        payload["reply_in_thread"] = bool(reply_in_thread)

    def _send(tenant_token: str) -> dict[str, Any]:
        return http_json_fn(
            "POST",
            url,
            payload,
            headers={
                "Authorization": f"Bearer {tenant_token}",
                "Content-Type": "application/json; charset=utf-8",
            },
        )

    return with_tenant_token_retry(app_id, app_secret, _send)


def add_message_reaction(
    *,
    app_id: str,
    app_secret: str,
    message_id: str,
    emoji_type: str,
    http_json_fn: HttpJsonFn = http_json,
) -> dict[str, Any]:
    message_id_value = str(message_id or "").strip()
    emoji_type_value = str(emoji_type or "").strip().upper()
    if not message_id_value:
        raise ValueError("message_id is required")
    if not emoji_type_value:
        raise ValueError("emoji_type is required")

    url = f"https://open.feishu.cn/open-apis/im/v1/messages/{quote(message_id_value, safe='')}/reactions"
    payload = {"reaction_type": {"emoji_type": emoji_type_value}}

    def _send(tenant_token: str) -> dict[str, Any]:
        return http_json_fn(
            "POST",
            url,
            payload,
            headers={
                "Authorization": f"Bearer {tenant_token}",
                "Content-Type": "application/json; charset=utf-8",
            },
        )

    return with_tenant_token_retry(app_id, app_secret, _send)


def send_text_message(
    *,
    app_id: str,
    app_secret: str,
    open_id: str,
    text: str,
    http_json_fn: HttpJsonFn = http_json,
) -> dict[str, Any]:
    open_id_value = str(open_id or "").strip()
    text_value = str(text or "").strip()
    if not open_id_value:
        raise ValueError("open_id is required")
    if not text_value:
        raise ValueError("text is required")

    request_path = "/open-apis/im/v1/messages?receive_id_type=open_id"
    url = f"https://open.feishu.cn{request_path}"
    payload = {
        "receive_id": open_id_value,
        "msg_type": "text",
        "content": json.dumps({"text": text_value}, ensure_ascii=False),
    }

    def _send(tenant_token: str) -> dict[str, Any]:
        return http_json_fn(
            "POST",
            url,
            payload,
            headers={
                "Authorization": f"Bearer {tenant_token}",
                "Content-Type": "application/json; charset=utf-8",
            },
        )

    return with_tenant_token_retry(app_id, app_secret, _send)
