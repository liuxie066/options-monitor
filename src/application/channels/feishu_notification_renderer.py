from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from src.application.channels.feishu_reply_renderer import (
    FEISHU_CARD_SCHEMA_VERSION,
    FEISHU_REPLY_TRUNCATION_NOTICE,
    has_markdown_table,
    sanitize_feishu_markdown,
    truncate_feishu_markdown,
)
from src.application.payload_helpers import text_sha256 as _sha256
from src.infrastructure.feishu_bot import (
    FEISHU_POST_REQUEST_BUDGET_BYTES,
    FEISHU_SEND_REQUEST_BUDGET_BYTES,
    build_message_payload,
    build_post_message_payload,
    feishu_request_body_bytes,
)


FEISHU_NOTIFICATION_ENVELOPE_SCHEMA_VERSION = "feishu-proactive-notification.v1"
FEISHU_NOTIFICATION_RENDERER_VERSION = "4"
FEISHU_NOTIFICATION_CONTENT_BUDGET_BYTES = 24 * 1024
FEISHU_NOTIFICATION_MAX_CHARS = 12_000
# Production compacts every logical key to om-<32 hex>; fallback appends :fallback.
_FALLBACK_UUID_BUDGET = "om-" + "0" * 32 + ":fallback"


def _fallback_request_bytes(markdown: str, open_id: str) -> int:
    return feishu_request_body_bytes(build_post_message_payload(
        open_id=open_id, markdown=markdown, uuid=_FALLBACK_UUID_BUDGET,
    ))


def feishu_notification_blocks_fit(
    markdown_blocks: list[str], fallback_text: str, *, open_id: str,
    max_chars: int = FEISHU_NOTIFICATION_MAX_CHARS,
) -> bool:
    """Measure the exact primary and fallback bodies without truncating either."""
    sanitized = [sanitize_feishu_markdown(block) for block in markdown_blocks]
    markdown = "\n\n".join(sanitized)
    return (
        bool(markdown and fallback_text)
        and max(len(markdown), len(fallback_text)) <= max_chars
        and len(markdown.encode("utf-8")) <= FEISHU_NOTIFICATION_CONTENT_BUDGET_BYTES
        and len(fallback_text.encode("utf-8")) <= FEISHU_NOTIFICATION_CONTENT_BUDGET_BYTES
        and _primary_request_bytes(_card_transport(markdown, sanitized), open_id)
        <= FEISHU_SEND_REQUEST_BUDGET_BYTES
        and _fallback_request_bytes(fallback_text, open_id) <= FEISHU_POST_REQUEST_BUDGET_BYTES
    )


def _bounded_fallback(text: str, *, open_id: str, max_chars: int) -> tuple[str, bool]:
    if _fallback_request_bytes(text, open_id) <= FEISHU_POST_REQUEST_BUDGET_BYTES:
        return text, False
    notice_bytes = len(FEISHU_REPLY_TRUNCATION_NOTICE.encode("utf-8"))
    if _fallback_request_bytes(FEISHU_REPLY_TRUNCATION_NOTICE, open_id) > FEISHU_POST_REQUEST_BUDGET_BYTES:
        raise ValueError("Feishu notification fallback identity exceeds the request budget")
    low, high = notice_bytes, len(text.encode("utf-8"))
    rendered = FEISHU_REPLY_TRUNCATION_NOTICE
    while low <= high:
        max_bytes = (low + high) // 2
        candidate, _ = truncate_feishu_markdown(text, max_chars=max_chars, max_bytes=max_bytes)
        if _fallback_request_bytes(candidate, open_id) <= FEISHU_POST_REQUEST_BUDGET_BYTES:
            rendered = candidate
            low = max_bytes + 1
        else:
            high = max_bytes - 1
    return rendered, True


def render_feishu_notification_card(
    *,
    markdown: str,
    fallback_text: str,
    fallback_open_id: str,
    markdown_blocks: list[str] | None = None,
    max_chars: int = FEISHU_NOTIFICATION_MAX_CHARS,
    preserve_blocks: bool = False,
) -> dict[str, Any]:
    source = str(markdown or "").strip()
    fallback = str(fallback_text or "").strip()
    if not source:
        raise ValueError("Feishu notification card markdown is required")
    if not fallback:
        raise ValueError("Feishu notification fallback text is required")
    if markdown_blocks is not None and not isinstance(markdown_blocks, list):
        raise ValueError("Feishu notification Markdown blocks must be a list")
    blocks = [source] if markdown_blocks is None else list(markdown_blocks)
    if not blocks or any(not isinstance(block, str) or not block.strip() for block in blocks):
        raise ValueError("Feishu notification Markdown blocks must be nonempty strings")
    if "\n\n".join(blocks).strip() != source:
        raise ValueError("Feishu notification Markdown blocks do not match source")
    if preserve_blocks and not feishu_notification_blocks_fit(
        blocks, fallback, open_id=fallback_open_id, max_chars=max_chars,
    ):
        raise ValueError("render_unavailable: selected Daily Brief exceeds the Feishu budgets")
    fallback_markdown, fallback_truncated = (
        (fallback, False) if preserve_blocks else _bounded_fallback(
            fallback, open_id=fallback_open_id, max_chars=max_chars,
        )
    )
    sanitized_blocks = [sanitize_feishu_markdown(block) for block in blocks]
    sanitized = "\n\n".join(sanitized_blocks)
    rendered, truncated = (
        (sanitized, False) if preserve_blocks else truncate_feishu_markdown(
            sanitized,
            max_chars=max_chars,
            max_bytes=FEISHU_NOTIFICATION_CONTENT_BUDGET_BYTES,
        )
    )
    transport = _card_transport(rendered, sanitized_blocks)
    if _primary_request_bytes(transport, fallback_open_id) > FEISHU_SEND_REQUEST_BUDGET_BYTES:
        low, high = len(FEISHU_REPLY_TRUNCATION_NOTICE.encode("utf-8")), FEISHU_NOTIFICATION_CONTENT_BUDGET_BYTES
        best = None
        while low <= high:
            budget = (low + high) // 2
            candidate, _ = truncate_feishu_markdown(sanitized, max_chars=max_chars, max_bytes=budget)
            candidate_transport = _card_transport(candidate, sanitized_blocks)
            if _primary_request_bytes(candidate_transport, fallback_open_id) <= FEISHU_SEND_REQUEST_BUDGET_BYTES:
                best = candidate, candidate_transport
                low = budget + 1
            else:
                high = budget - 1
        if best is None:
            raise ValueError("Feishu notification primary identity exceeds the request budget")
        rendered, transport = best
        truncated = True
    rendered_json = _canonical_json(transport)
    envelope = {
        "schema_version": FEISHU_NOTIFICATION_ENVELOPE_SCHEMA_VERSION,
        "provider": "feishu_app",
        "text": fallback,
        "render_mode": "card_markdown_v2",
        "transport": transport,
        "fallback": {
            "msg_type": "post",
            "markdown": fallback_markdown,
        },
        "render_meta": {
            "renderer_version": FEISHU_NOTIFICATION_RENDERER_VERSION,
            "fallback_sha256": _sha256(fallback_markdown),
            "fallback_truncated": fallback_truncated,
            "fallback_receive_id_json_bytes": len(json.dumps(str(fallback_open_id or "").strip(), ensure_ascii=False).encode("utf-8")),
            "fallback_request_body_bytes": _fallback_request_bytes(fallback_markdown, fallback_open_id),
            "primary_request_body_bytes": _primary_request_bytes(transport, fallback_open_id),
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


def _primary_request_bytes(transport: Mapping[str, Any], open_id: str) -> int:
    return feishu_request_body_bytes(build_message_payload(
        open_id=open_id, msg_type="interactive", content=transport["content"],
        uuid="om-" + "0" * 32,
    ))


def _card_transport(rendered: str, sanitized_blocks: list[str]) -> dict[str, Any]:
    # Retain supplied boundaries through truncation; the final element may be partial.
    rendered_blocks = []
    remainder = rendered
    for block in sanitized_blocks[:-1]:
        if not remainder.startswith(block + "\n\n"):
            break
        rendered_blocks.append(block)
        remainder = remainder[len(block) + 2:]
    if remainder:
        rendered_blocks.append(remainder)
    return {
        "msg_type": "interactive",
        "content": {
            "schema": FEISHU_CARD_SCHEMA_VERSION,
            "body": {
                "elements": [
                    {
                        "tag": "markdown",
                        "element_id": f"notification_{index}",
                        "content": block,
                        "text_size": "normal",
                        "margin": "12px 0px 0px 0px" if index > 1 and block.startswith("## ") else "0px 0px 0px 0px",
                    }
                    for index, block in enumerate(rendered_blocks, start=1)
                ]
            },
        },
    }


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
    render_meta = _mapping(envelope.get("render_meta"))
    version = str(render_meta.get("renderer_version") or "")
    if version not in {"1", "2", "3", "4"}:
        raise ValueError("Feishu notification renderer version is invalid")
    if not isinstance(elements, list) or not elements:
        raise ValueError("Feishu notification card must contain Markdown elements")
    if version in {"1", "2"} and len(elements) != 1:
        raise ValueError("legacy Feishu notification card must contain one Markdown element")
    blocks = []
    for index, raw in enumerate(elements, start=1):
        element = _mapping(raw)
        block = str(element.get("content") or "").strip()
        expected_id = f"notification_{index}" if version in {"3", "4"} else "notification_body"
        if element.get("tag") != "markdown" or element.get("element_id") != expected_id or not block:
            raise ValueError("Feishu notification Markdown element is invalid")
        if version in {"3", "4"} and (
            element.get("text_size") != "normal"
            or element.get("margin") != (
                "12px 0px 0px 0px" if index > 1 and (version == "3" or block.startswith("## "))
                else "0px 0px 0px 0px"
            )
        ):
            raise ValueError("Feishu notification Markdown element spacing is invalid")
        blocks.append(block)
    markdown = "\n\n".join(blocks)
    if len(markdown.encode("utf-8")) > FEISHU_NOTIFICATION_CONTENT_BUDGET_BYTES:
        raise ValueError("Feishu notification Markdown exceeds the local content budget")

    fallback = _mapping(envelope.get("fallback"))
    fallback_markdown = str(fallback.get("markdown") or "").strip()
    if str(fallback.get("msg_type") or "") != "post" or not fallback_markdown:
        raise ValueError("Feishu notification fallback transport is invalid")
    if version in {"2", "3", "4"}:
        if str(render_meta.get("fallback_sha256") or "") != _sha256(fallback_markdown):
            raise ValueError("Feishu notification fallback transport digest mismatch")
        # The receiver is private routing metadata; retain its exact JSON byte cost, not its value.
        receiver_bytes = int(render_meta.get("fallback_receive_id_json_bytes") or 0)
        size = _fallback_request_bytes(fallback_markdown, "") + receiver_bytes - 2
        if receiver_bytes < 2 or size != int(render_meta.get("fallback_request_body_bytes") or 0):
            raise ValueError("Feishu notification fallback request byte count mismatch")
        if size > FEISHU_POST_REQUEST_BUDGET_BYTES:
            raise ValueError("Feishu notification fallback exceeds the request byte budget")
    elif fallback_markdown != text:
        raise ValueError("Feishu notification fallback transport is invalid")
    rendered_json = _canonical_json({"msg_type": "interactive", "content": content})
    if str(render_meta.get("rendered_sha256") or "") != _sha256(rendered_json):
        raise ValueError("Feishu notification rendered digest mismatch")
    if int(render_meta.get("rendered_content_bytes") or -1) != len(markdown.encode("utf-8")):
        raise ValueError("Feishu notification rendered byte count mismatch")
    if bool(render_meta.get("markdown_table_detected")) != has_markdown_table(markdown):
        raise ValueError("Feishu notification table detection metadata mismatch")

    if version in {"3", "4"}:
        receiver_bytes = int(render_meta.get("fallback_receive_id_json_bytes") or 0)
        size = _primary_request_bytes(transport, "") + receiver_bytes - 2
        if size != int(render_meta.get("primary_request_body_bytes") or 0):
            raise ValueError("Feishu notification primary request byte count mismatch")
        if size > FEISHU_SEND_REQUEST_BUDGET_BYTES:
            raise ValueError("Feishu notification primary exceeds the request byte budget")

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
            "markdown": fallback_markdown,
        },
        "render_meta": dict(render_meta),
    }


def feishu_notification_visible_markdown(value: Mapping[str, Any]) -> tuple[str, str]:
    """Return the final budgeted primary and fallback bodies of a frozen envelope."""
    normalized = normalize_feishu_notification_envelope(value)
    elements = normalized["transport"]["content"]["body"]["elements"]
    primary = "\n\n".join(element["content"] for element in elements)
    return primary, normalized["fallback"]["markdown"]


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
    "feishu_notification_visible_markdown",
    "is_feishu_notification_envelope",
    "normalize_feishu_notification_envelope",
    "render_feishu_notification_card",
]
