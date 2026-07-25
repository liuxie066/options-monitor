from __future__ import annotations

import hashlib
import json
from typing import Any, Callable
from urllib.parse import quote

from src.infrastructure.feishu_bitable import FeishuPermanentError, http_json, with_tenant_token_retry


HttpJsonFn = Callable[..., dict[str, Any]]

FEISHU_REPLY_REQUEST_BUDGET_BYTES = 28 * 1024
FEISHU_REPLY_TOO_LARGE = "FEISHU_REPLY_TOO_LARGE"
_FEISHU_REPLY_MESSAGE_TYPES = {"text", "post", "interactive"}
FEISHU_SEND_REQUEST_BUDGET_BYTES = 28 * 1024
FEISHU_SEND_TOO_LARGE = "FEISHU_SEND_TOO_LARGE"
_FEISHU_SEND_MESSAGE_TYPES = {"text", "post", "interactive"}


def reply_message(
    *,
    app_id: str,
    app_secret: str,
    message_id: str,
    msg_type: str = "text",
    content: dict[str, Any] | None = None,
    text: str | None = None,
    uuid: str | None = None,
    reply_in_thread: bool | None = None,
    http_json_fn: HttpJsonFn = http_json,
) -> dict[str, Any]:
    message_id_value = str(message_id or "").strip()
    msg_type_value = str(msg_type or "").strip()
    if not message_id_value:
        raise ValueError("message_id is required")
    if msg_type_value not in _FEISHU_REPLY_MESSAGE_TYPES:
        raise ValueError(f"unsupported Feishu reply msg_type: {msg_type_value or '<empty>'}")

    content_value = dict(content) if isinstance(content, dict) else None
    if content_value is None and msg_type_value == "text":
        text_value = str(text or "").strip()
        if not text_value:
            raise ValueError("text is required")
        content_value = {"text": text_value}
    if not content_value:
        raise ValueError("content is required")

    url = f"https://open.feishu.cn/open-apis/im/v1/messages/{quote(message_id_value, safe='')}/reply"
    payload: dict[str, Any] = {
        "msg_type": msg_type_value,
        "content": json.dumps(content_value, ensure_ascii=False),
    }
    if uuid:
        payload["uuid"] = str(uuid)
    if reply_in_thread is not None:
        payload["reply_in_thread"] = bool(reply_in_thread)

    request_body_bytes = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    if request_body_bytes > FEISHU_REPLY_REQUEST_BUDGET_BYTES:
        canonical_content = json.dumps(
            content_value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        raise FeishuPermanentError(
            "feishu reply request exceeds local byte budget",
            response={
                "local_error_code": FEISHU_REPLY_TOO_LARGE,
                "http_status": None,
                "feishu_code": None,
                "http_attempts": [],
                "request_body_bytes": request_body_bytes,
                "request_body_budget_bytes": FEISHU_REPLY_REQUEST_BUDGET_BYTES,
                "content_sha256": hashlib.sha256(canonical_content.encode("utf-8")).hexdigest(),
            },
        )

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
    return reply_message(
        app_id=app_id,
        app_secret=app_secret,
        message_id=message_id,
        msg_type="text",
        text=text,
        uuid=uuid,
        reply_in_thread=reply_in_thread,
        http_json_fn=http_json_fn,
    )


def add_message_reaction(
    *,
    app_id: str,
    app_secret: str,
    message_id: str,
    emoji_type: str,
    http_json_fn: HttpJsonFn = http_json,
) -> dict[str, Any]:
    message_id_value = str(message_id or "").strip()
    emoji_type_value = str(emoji_type or "").strip()
    if not (any(char.islower() for char in emoji_type_value) and any(char.isupper() for char in emoji_type_value)):
        emoji_type_value = emoji_type_value.upper()
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
            timeout=2,
            retry_max_attempts=1,
        )

    return with_tenant_token_retry(
        app_id,
        app_secret,
        _send,
        token_timeout=2,
        token_retry_max_attempts=1,
        token_lock_timeout=0.0,
    )


def send_message(
    *,
    app_id: str,
    app_secret: str,
    open_id: str,
    msg_type: str,
    content: dict[str, Any],
    uuid: str | None = None,
    log_fn: Callable[[dict[str, Any]], Any] | None = None,
    http_json_fn: HttpJsonFn = http_json,
) -> dict[str, Any]:
    open_id_value = str(open_id or "").strip()
    msg_type_value = str(msg_type or "").strip()
    content_value = dict(content) if isinstance(content, dict) else {}
    if not open_id_value:
        raise ValueError("open_id is required")
    if msg_type_value not in _FEISHU_SEND_MESSAGE_TYPES:
        raise ValueError(f"unsupported Feishu send msg_type: {msg_type_value or '<empty>'}")
    if not content_value:
        raise ValueError("content is required")

    request_path = "/open-apis/im/v1/messages?receive_id_type=open_id"
    url = f"https://open.feishu.cn{request_path}"
    payload: dict[str, Any] = {
        "receive_id": open_id_value,
        "msg_type": msg_type_value,
        "content": json.dumps(content_value, ensure_ascii=False),
    }
    if uuid:
        payload["uuid"] = str(uuid)

    request_body_bytes = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    if request_body_bytes > FEISHU_SEND_REQUEST_BUDGET_BYTES:
        canonical_content = json.dumps(
            content_value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        raise FeishuPermanentError(
            "feishu send request exceeds local byte budget",
            response={
                "local_error_code": FEISHU_SEND_TOO_LARGE,
                "http_status": None,
                "feishu_code": None,
                "http_attempts": [],
                "request_body_bytes": request_body_bytes,
                "request_body_budget_bytes": FEISHU_SEND_REQUEST_BUDGET_BYTES,
                "content_sha256": hashlib.sha256(canonical_content.encode("utf-8")).hexdigest(),
            },
        )

    def _send(tenant_token: str) -> dict[str, Any]:
        return http_json_fn(
            "POST",
            url,
            payload,
            headers={
                "Authorization": f"Bearer {tenant_token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            retry_max_attempts=(3 if uuid else 1),
            log_fn=log_fn,
            log_success_attempts=bool(log_fn),
        )

    return with_tenant_token_retry(app_id, app_secret, _send)


def send_text_message(
    *,
    app_id: str,
    app_secret: str,
    open_id: str,
    text: str,
    uuid: str | None = None,
    log_fn: Callable[[dict[str, Any]], Any] | None = None,
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
    if uuid:
        payload["uuid"] = str(uuid)

    def _send(tenant_token: str) -> dict[str, Any]:
        return http_json_fn(
            "POST",
            url,
            payload,
            headers={
                "Authorization": f"Bearer {tenant_token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            retry_max_attempts=(3 if uuid else 1),
            log_fn=log_fn,
            log_success_attempts=bool(log_fn),
        )

    return with_tenant_token_retry(app_id, app_secret, _send)


FEISHU_POST_REQUEST_BUDGET_BYTES = 28 * 1024
FEISHU_POST_TOO_LARGE = "FEISHU_POST_TOO_LARGE"
_POST_BLANK_PARAGRAPH = [{"tag": "text", "text": "\u00a0"}]


def _post_md_paragraphs(markdown: str) -> list[list[dict[str, str]]]:
    """Split canonical Markdown into Feishu post paragraphs.

    Feishu collapses empty lines inside a single md node, while placeholder
    spacer characters (for example the zero-width space the daily brief uses
    as a visible blank line) leak into rendered lines on desktop and break
    line-start ``**bold**`` markers. Keep those placeholders out of md nodes
    and map each separator to a dedicated plain-text spacer paragraph.
    """

    paragraphs: list[list[dict[str, str]]] = []
    current: list[str] = []

    def _flush_current() -> None:
        nonlocal current
        if current:
            paragraphs.append([{"tag": "md", "text": "\n".join(current)}])
            current = []

    for line in markdown.split("\n"):
        if not line.strip(" \t\u200b"):
            _flush_current()
            if paragraphs and paragraphs[-1] != _POST_BLANK_PARAGRAPH:
                paragraphs.append([{"tag": "text", "text": "\u00a0"}])
            continue
        current.append(line)
    _flush_current()
    if paragraphs and paragraphs[-1] == _POST_BLANK_PARAGRAPH:
        paragraphs.pop()
    if not paragraphs:
        paragraphs.append([{"tag": "md", "text": markdown}])
    return paragraphs


def send_post_message(
    *,
    app_id: str,
    app_secret: str,
    open_id: str,
    markdown: str,
    uuid: str | None = None,
    log_fn: Callable[[dict[str, Any]], Any] | None = None,
    http_json_fn: HttpJsonFn = http_json,
) -> dict[str, Any]:
    open_id_value = str(open_id or "").strip()
    markdown_value = str(markdown or "").strip()
    if not open_id_value:
        raise ValueError("open_id is required")
    if not markdown_value:
        raise ValueError("markdown is required")

    request_path = "/open-apis/im/v1/messages?receive_id_type=open_id"
    url = f"https://open.feishu.cn{request_path}"
    payload = {
        "receive_id": open_id_value,
        "msg_type": "post",
        "content": json.dumps(
            {
                "zh_cn": {
                    "content": _post_md_paragraphs(markdown_value),
                }
            },
            ensure_ascii=False,
        ),
    }
    if uuid:
        payload["uuid"] = str(uuid)

    request_body_bytes = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    if request_body_bytes > FEISHU_POST_REQUEST_BUDGET_BYTES:
        raise FeishuPermanentError(
            "feishu post request exceeds local byte budget",
            response={
                "local_error_code": FEISHU_POST_TOO_LARGE,
                "http_status": None,
                "feishu_code": None,
                "http_attempts": [],
                "request_body_bytes": request_body_bytes,
                "request_body_budget_bytes": FEISHU_POST_REQUEST_BUDGET_BYTES,
                "normalized_markdown_chars": len(markdown_value),
                "normalized_markdown_sha256": hashlib.sha256(markdown_value.encode("utf-8")).hexdigest(),
            },
        )

    def _send(tenant_token: str) -> dict[str, Any]:
        return http_json_fn(
            "POST",
            url,
            payload,
            headers={
                "Authorization": f"Bearer {tenant_token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            retry_max_attempts=(3 if uuid else 1),
            log_fn=log_fn,
            log_success_attempts=bool(log_fn),
        )

    return with_tenant_token_retry(app_id, app_secret, _send)
