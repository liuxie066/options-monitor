from __future__ import annotations

import base64
import json
import random
import socket
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable
from uuid import uuid4


DEFAULT_ILINK_BASE_URL = "https://ilinkai.weixin.qq.com"
HttpJsonFn = Callable[..., dict[str, Any]]
ILINK_CHANNEL_VERSION = "1.0.0"


class WechatClawbotError(RuntimeError):
    pass


def http_json(
    method: str,
    url: str,
    payload: dict[str, Any] | None = None,
    *,
    headers: dict[str, str] | None = None,
    timeout: int = 20,
) -> dict[str, Any]:
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=str(method or "GET").upper(), headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        raise WechatClawbotError(f"iLink HTTP error {getattr(exc, 'code', None)}: {body[-500:]}") from exc
    except (urllib.error.URLError, socket.timeout) as exc:
        raise WechatClawbotError(f"iLink network error: {type(exc).__name__}: {exc}") from exc

    try:
        parsed = json.loads(body or "{}")
    except Exception as exc:
        raise WechatClawbotError(f"iLink returned invalid JSON: {body[-500:]}") from exc
    if not isinstance(parsed, dict):
        raise WechatClawbotError("iLink JSON response must be an object")
    return parsed


def _random_wechat_uin() -> str:
    raw = str(random.randint(1, 2**32 - 1)).encode("ascii")
    return base64.b64encode(raw).decode("ascii")


class WechatClawbotClient:
    def __init__(
        self,
        *,
        bot_token: str | None = None,
        base_url: str = DEFAULT_ILINK_BASE_URL,
        timeout: int = 20,
        http_json_fn: HttpJsonFn = http_json,
    ) -> None:
        token = str(bot_token or "").strip()
        self.bot_token = token
        self.base_url = str(base_url or DEFAULT_ILINK_BASE_URL).rstrip("/")
        self.timeout = int(timeout or 20)
        self.http_json_fn = http_json_fn

    def headers(self, *, require_token: bool = True) -> dict[str, str]:
        if require_token and not self.bot_token:
            raise ValueError("bot_token is required")
        headers = {
            "Content-Type": "application/json",
            "AuthorizationType": "ilink_bot_token",
            "X-WECHAT-UIN": _random_wechat_uin(),
        }
        if self.bot_token:
            headers["Authorization"] = f"Bearer {self.bot_token}"
        return headers

    def get_bot_qrcode(self, *, bot_type: int = 3) -> dict[str, Any]:
        query = urllib.parse.urlencode({"bot_type": int(bot_type)})
        return self.http_json_fn(
            "GET",
            f"{self.base_url}/ilink/bot/get_bot_qrcode?{query}",
            None,
            headers=self.headers(require_token=False),
            timeout=self.timeout,
        )

    def get_qrcode_status(self, *, qrcode: str) -> dict[str, Any]:
        query = urllib.parse.urlencode({"qrcode": str(qrcode or "")})
        return self.http_json_fn(
            "GET",
            f"{self.base_url}/ilink/bot/get_qrcode_status?{query}",
            None,
            headers=self.headers(require_token=False),
            timeout=self.timeout,
        )

    def get_updates(self, *, get_updates_buf: str = "") -> dict[str, Any]:
        payload: dict[str, Any] = {"base_info": _base_info()}
        if str(get_updates_buf or "").strip():
            payload["get_updates_buf"] = str(get_updates_buf)
        return self.http_json_fn(
            "POST",
            f"{self.base_url}/ilink/bot/getupdates",
            payload,
            headers=self.headers(),
            timeout=self.timeout,
        )

    def get_config(self, *, ilink_user_id: str, context_token: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"ilink_user_id": str(ilink_user_id or ""), "base_info": _base_info()}
        if str(context_token or "").strip():
            payload["context_token"] = str(context_token or "").strip()
        return self.http_json_fn(
            "POST",
            f"{self.base_url}/ilink/bot/getconfig",
            payload,
            headers=self.headers(),
            timeout=self.timeout,
        )

    def send_typing(self, *, ilink_user_id: str, typing_ticket: str, status: int) -> dict[str, Any]:
        return self.http_json_fn(
            "POST",
            f"{self.base_url}/ilink/bot/sendtyping",
            {
                "ilink_user_id": str(ilink_user_id or ""),
                "typing_ticket": str(typing_ticket or ""),
                "status": int(status),
                "base_info": _base_info(),
            },
            headers=self.headers(),
            timeout=self.timeout,
        )

    def send_text_message(
        self,
        *,
        to_user_id: str,
        context_token: str,
        text: str,
        group_id: str | None = None,
        client_id: str | None = None,
    ) -> dict[str, Any]:
        resolved_client_id = str(client_id or "").strip() or uuid4().hex
        msg: dict[str, Any] = {
            "message_type": 2,
            "message_state": 2,
            "context_token": str(context_token or ""),
            "from_user_id": "",
            "to_user_id": str(to_user_id or ""),
            "client_id": resolved_client_id,
            "item_list": [
                {
                    "type": 1,
                    "text_item": {"text": str(text or "")},
                }
            ],
        }
        if str(group_id or "").strip():
            msg["group_id"] = str(group_id or "").strip()
        return self.http_json_fn(
            "POST",
            f"{self.base_url}/ilink/bot/sendmessage",
            {
                "base_info": _base_info(),
                "msg": msg,
            },
            headers=self.headers(),
            timeout=self.timeout,
        )


def _base_info() -> dict[str, str]:
    return {"channel_version": ILINK_CHANNEL_VERSION}
