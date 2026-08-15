from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from src.application.channels.feishu_reply_renderer import (
    FEISHU_CARD_SCHEMA_VERSION,
    has_markdown_table,
    sanitize_feishu_markdown,
    truncate_feishu_markdown,
)
from src.application.payload_helpers import text_sha256 as _sha256


FEISHU_NOTIFICATION_ENVELOPE_SCHEMA_VERSION = "feishu-proactive-notification.v1"
FEISHU_NOTIFICATION_RENDERER_VERSION = "1"
FEISHU_NOTIFICATION_CONTENT_BUDGET_BYTES = 24 * 1024
FEISHU_NOTIFICATION_MAX_CHARS = 12_000


def render_feishu_notification_card(
    *,
    markdown: str,
    fallback_text: str,
    max_chars: int = FEISHU_NOTIFICATION_MAX_CHARS,
) -> dict[str, Any]:
    source = str(markdown or "").strip()
    fallback = str(fallback_text or "").strip()
    if not source:
        raise ValueError("Feishu notification card markdown is required")
    if not fallback:
        raise ValueError("Feishu notification fallback text is required")
    sanitized = sanitize_feishu_markdown(source)
    rendered, truncated = truncate_feishu_markdown(
        sanitized,
        max_chars=max_chars,
        max_bytes=FEISHU_NOTIFICATION_CONTENT_BUDGET_BYTES,
    )
    transport = {
        "msg_type": "interactive",
        "content": {
            "schema": FEISHU_CARD_SCHEMA_VERSION,
            "body": {
                "elements": [
                    {
                        "tag": "markdown",
                        "element_id": "notification_body",
                        "content": rendered,
                        "text_size": "normal",
                    }
                ]
            },
        },
    }
    rendered_json = _canonical_json(transport)
    envelope = {
        "schema_version": FEISHU_NOTIFICATION_ENVELOPE_SCHEMA_VERSION,
        "provider": "feishu_app",
        "text": fallback,
        "render_mode": "card_markdown_v2",
        "transport": transport,
        "fallback": {
            "msg_type": "post",
            "markdown": fallback,
        },
        "render_meta": {
            "renderer_version": FEISHU_NOTIFICATION_RENDERER_VERSION,
            "card_schema": FEISHU_CARD_SCHEMA_VERSION,
            "source_chars": len(source),
            "source_sha256": _sha256(source),
            "rendered_sha256": _sha256(rendered_json),
            "rendered_content_bytes": len(rendered.encode("utf-8")),
            "markdown_table_detected": has_markdown_table(rendered),
            "truncated": bool(truncated),
        },
    }
    return normalize_feishu_notification_envelope(envelope, expected_text=fallback)


def is_feishu_notification_envelope(value: Any) -> bool:
    return (
        isinstance(value, Mapping)
        and str(value.get("schema_version") or "") == FEISHU_NOTIFICATION_ENVELOPE_SCHEMA_VERSION
        and str(value.get("provider") or "") == "feishu_app"
        and isinstance(value.get("transport"), Mapping)
    )


def normalize_feishu_notification_envelope(
    value: Any,
    *,
    expected_text: str | None = None,
) -> dict[str, Any]:
    if not is_feishu_notification_envelope(value):
        raise ValueError("unsupported Feishu notification envelope")
    envelope = dict(value)
    text = str(envelope.get("text") or "").strip()
    if not text:
        raise ValueError("Feishu notification envelope text is required")
    if expected_text is not None and text != str(expected_text or "").strip():
        raise ValueError("Feishu notification fallback text does not match rendered message")
    if str(envelope.get("render_mode") or "") != "card_markdown_v2":
        raise ValueError("Feishu notification render mode is invalid")

    transport = _mapping(envelope.get("transport"))
    if str(transport.get("msg_type") or "") != "interactive":
        raise ValueError("Feishu notification primary transport must be interactive")
    content = _mapping(transport.get("content"))
    if str(content.get("schema") or "") != FEISHU_CARD_SCHEMA_VERSION:
        raise ValueError("Feishu notification card schema is invalid")
    body = _mapping(content.get("body"))
    elements = body.get("elements")
    if not isinstance(elements, list) or len(elements) != 1 or not isinstance(elements[0], Mapping):
        raise ValueError("Feishu notification card must contain one Markdown element")
    element = dict(elements[0])
    markdown = str(element.get("content") or "").strip()
    if (
        str(element.get("tag") or "") != "markdown"
        or str(element.get("element_id") or "") != "notification_body"
        or not markdown
    ):
        raise ValueError("Feishu notification Markdown element is invalid")
    if len(markdown.encode("utf-8")) > FEISHU_NOTIFICATION_CONTENT_BUDGET_BYTES:
        raise ValueError("Feishu notification Markdown exceeds the local content budget")

    fallback = _mapping(envelope.get("fallback"))
    if str(fallback.get("msg_type") or "") != "post" or str(fallback.get("markdown") or "").strip() != text:
        raise ValueError("Feishu notification fallback transport is invalid")

    render_meta = _mapping(envelope.get("render_meta"))
    rendered_json = _canonical_json({"msg_type": "interactive", "content": content})
    if str(render_meta.get("rendered_sha256") or "") != _sha256(rendered_json):
        raise ValueError("Feishu notification rendered digest mismatch")
    if int(render_meta.get("rendered_content_bytes") or -1) != len(markdown.encode("utf-8")):
        raise ValueError("Feishu notification rendered byte count mismatch")
    if bool(render_meta.get("markdown_table_detected")) != has_markdown_table(markdown):
        raise ValueError("Feishu notification table detection metadata mismatch")

    return {
        "schema_version": FEISHU_NOTIFICATION_ENVELOPE_SCHEMA_VERSION,
        "provider": "feishu_app",
        "text": text,
        "render_mode": "card_markdown_v2",
        "transport": {
            "msg_type": "interactive",
            "content": content,
        },
        "fallback": {
            "msg_type": "post",
            "markdown": text,
        },
        "render_meta": dict(render_meta),
    }


def feishu_notification_envelope_sha256(value: Mapping[str, Any]) -> str:
    normalized = normalize_feishu_notification_envelope(value)
    return _sha256(_canonical_json(normalized))


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


__all__ = [
    "FEISHU_NOTIFICATION_CONTENT_BUDGET_BYTES",
    "FEISHU_NOTIFICATION_ENVELOPE_SCHEMA_VERSION",
    "FEISHU_NOTIFICATION_MAX_CHARS",
    "FEISHU_NOTIFICATION_RENDERER_VERSION",
    "feishu_notification_envelope_sha256",
    "is_feishu_notification_envelope",
    "normalize_feishu_notification_envelope",
    "render_feishu_notification_card",
]
