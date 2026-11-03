from __future__ import annotations

import json
from typing import Any, Callable

from src.application.channels.plugins import load_channel_plugin
from src.infrastructure.feishu_bitable import FeishuPermanentError, http_json, with_tenant_token_retry


HttpJsonFn = Callable[..., dict[str, Any]]

FEISHU_REPLY_REQUEST_BUDGET_BYTES = 28 * 1024
FEISHU_REPLY_TOO_LARGE = "FEISHU_REPLY_TOO_LARGE"
_FEISHU_REPLY_MESSAGE_TYPES = {"text", "post", "interactive"}
FEISHU_SEND_REQUEST_BUDGET_BYTES = 28 * 1024
FEISHU_SEND_TOO_LARGE = "FEISHU_SEND_TOO_LARGE"
_FEISHU_SEND_MESSAGE_TYPES = {"text", "post", "interactive"}


def permanent_failure_allows_fallback(exc: FeishuPermanentError, *, too_large_code: str) -> bool:
    """Report whether a permanent Feishu failure can be retried with a smaller payload.

    ``too_large_code`` is the ``local_error_code`` the calling operation raises when
    its payload exceeded the request budget (``FEISHU_REPLY_TOO_LARGE`` for replies,
    ``FEISHU_SEND_TOO_LARGE`` for sends); any other permanent failure counts only
    when it carries an explicit API code or a 4xx HTTP status.
    """
    response = exc.response if isinstance(exc.response, dict) else {}
    if str(response.get("local_error_code") or "") == too_large_code:
        return True
    http_status = response.get("http_status")
    return exc.code is not None or (
        isinstance(http_status, int) and 400 <= http_status <= 499
    )


def reply_message(
    *,
    app_id: str,
    app_secret: str,
    before_send: Callable[[], None] | None = None,
    message_id: str,
    msg_type: str = "text",
    content: dict[str, Any] | None = None,
    text: str | None = None,
    uuid: str | None = None,
    reply_in_thread: bool | None = None,
    http_json_fn: HttpJsonFn = http_json,
) -> dict[str, Any]:
    return load_channel_plugin("feishu", module="bot").reply_message(
        app_id=app_id,
        app_secret=app_secret,
        before_send=before_send,
        message_id=message_id,
        msg_type=msg_type,
        content=content,
        text=text,
        uuid=uuid,
        reply_in_thread=reply_in_thread,
        http_json_fn=http_json_fn,
        token_retry_fn=with_tenant_token_retry,
    )


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
    before_send: Callable[[], None] | None = None,
    http_json_fn: HttpJsonFn = http_json,
) -> dict[str, Any]:
    return load_channel_plugin("feishu", module="bot").add_message_reaction(
        app_id=app_id,
        app_secret=app_secret,
        message_id=message_id,
        emoji_type=emoji_type,
        before_send=before_send,
        http_json_fn=http_json_fn,
        token_retry_fn=with_tenant_token_retry,
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
    return load_channel_plugin("feishu", module="bot").send_message(
        app_id=app_id,
        app_secret=app_secret,
        open_id=open_id,
        msg_type=msg_type,
        content=content,
        uuid=uuid,
        log_fn=log_fn,
        http_json_fn=http_json_fn,
        token_retry_fn=with_tenant_token_retry,
    )


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
    return load_channel_plugin("feishu", module="bot").send_text_message(
        app_id=app_id,
        app_secret=app_secret,
        open_id=open_id,
        text=text,
        uuid=uuid,
        log_fn=log_fn,
        http_json_fn=http_json_fn,
        token_retry_fn=with_tenant_token_retry,
    )


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


def build_message_payload(
    *, open_id: str, msg_type: str, content: dict[str, Any], uuid: str | None = None,
) -> dict[str, Any]:
    """Canonical send body shared by preparation budgeting and the actual sender."""
    payload = {
        "receive_id": str(open_id or "").strip(),
        "msg_type": msg_type,
        "content": json.dumps(content, ensure_ascii=False),
    }
    if uuid:
        payload["uuid"] = str(uuid)
    return payload


def build_post_message_payload(*, open_id: str, markdown: str, uuid: str | None = None) -> dict[str, Any]:
    """Canonical post body shared by preparation budgeting and the actual sender."""
    payload = {
        "receive_id": str(open_id or "").strip(),
        "msg_type": "post",
        "content": json.dumps(
            {
                "zh_cn": {
                    "content": _post_md_paragraphs(str(markdown or "").strip()),
                }
            },
            ensure_ascii=False,
        ),
    }
    if uuid:
        payload["uuid"] = str(uuid)

    return payload


def feishu_request_body_bytes(payload: dict[str, Any]) -> int:
    return len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))


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
    return load_channel_plugin("feishu", module="bot").send_post_message(
        app_id=app_id,
        app_secret=app_secret,
        open_id=open_id,
        markdown=markdown,
        uuid=uuid,
        log_fn=log_fn,
        http_json_fn=http_json_fn,
        token_retry_fn=with_tenant_token_retry,
    )
