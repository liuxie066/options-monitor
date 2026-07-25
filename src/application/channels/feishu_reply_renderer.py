from __future__ import annotations

import hashlib
import html
import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit


FEISHU_REPLY_ENVELOPE_SCHEMA_VERSION = "feishu-conversation-reply.v1"
FEISHU_CARD_SCHEMA_VERSION = "2.0"
FEISHU_REPLY_RENDERER_VERSION = "1"
FEISHU_REPLY_CONTENT_BUDGET_BYTES = 24 * 1024
FEISHU_REPLY_TRUNCATION_NOTICE = "…（内容较长，已在完整内容边界截断）"

_HTML_TAG_RE = re.compile(r"<\s*/?\s*[A-Za-z][^>\n]*>")
_IMAGE_RE = re.compile(r"!\[([^\]\n]*)\]\(([^)\n]*)\)")
_REFERENCE_IMAGE_RE = re.compile(r"!\[([^\]\n]*)\]\[[^\]\n]*\]")
_LINK_RE = re.compile(r"(?<!!)\[([^\]\n]+)\]\(([^)\n]*)\)")
_REFERENCE_DEFINITION_RE = re.compile(r"(?m)^(\s*\[[^\]\n]+\]:\s*)(\S+)(.*)$")
_HEADING_RE = re.compile(r"(?m)^\s{0,3}#{1,6}\s+\S")
_LIST_RE = re.compile(r"(?m)^\s*(?:[-+*]|\d+[.)])\s+\S")
_FENCE_RE = re.compile(r"(?m)^\s*```")
_BOLD_RE = re.compile(r"\*\*[^*\n]+\*\*")
_TABLE_SEPARATOR_CELL_RE = re.compile(r"^:?-{3,}:?$")


@dataclass(frozen=True)
class _MarkdownBlock:
    kind: str
    text: str


def render_feishu_conversation_reply(
    *,
    message_id: str,
    text: str,
    reply_in_thread: bool,
    max_chars: int,
    render_route: str | None,
) -> dict[str, Any]:
    source = _normalize_text(text)
    sanitized = sanitize_feishu_markdown(source)
    rendered, truncated = truncate_feishu_markdown(
        sanitized,
        max_chars=max_chars,
        max_bytes=FEISHU_REPLY_CONTENT_BUDGET_BYTES,
    )
    markdown_detected = has_rich_markdown(rendered)
    use_card = str(render_route or "").strip() == "copilot" or markdown_detected
    if use_card:
        render_mode = "card_markdown_v2"
        fallback_text = flatten_markdown_tables(rendered) or _flatten_non_table_markdown(rendered) or rendered
        rollback_text = fallback_text
        transport = {
            "msg_type": "interactive",
            "content": {
                "schema": FEISHU_CARD_SCHEMA_VERSION,
                "body": {
                    "elements": [
                        {
                            "tag": "markdown",
                            "element_id": "reply_body",
                            "content": rendered,
                            "text_size": "normal",
                        }
                    ]
                },
            },
        }
        fallback = {
            "msg_type": "text",
            "content": {"text": fallback_text},
        }
    else:
        render_mode = "text"
        rollback_text = rendered
        transport = {"msg_type": "text", "content": {"text": rendered}}
        fallback = None

    rendered_json = json.dumps(transport, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    envelope: dict[str, Any] = {
        "schema_version": FEISHU_REPLY_ENVELOPE_SCHEMA_VERSION,
        "message_id": str(message_id or "").strip(),
        "reply_in_thread": bool(reply_in_thread),
        "text": rollback_text,
        "render_mode": render_mode,
        "transport": transport,
        "render_meta": {
            "renderer_version": FEISHU_REPLY_RENDERER_VERSION,
            "card_schema": FEISHU_CARD_SCHEMA_VERSION if use_card else None,
            "source_chars": len(source),
            "source_sha256": _sha256(source),
            "rendered_sha256": _sha256(rendered_json),
            "rendered_content_bytes": len(rendered.encode("utf-8")),
            "markdown_table_detected": has_markdown_table(rendered),
            "truncated": bool(truncated),
        },
    }
    if fallback is not None:
        envelope["fallback"] = fallback
    return envelope


def legacy_text_envelope(payload: dict[str, Any]) -> dict[str, Any]:
    text = str(payload.get("text") or "")
    return {
        "schema_version": "feishu-conversation-reply.legacy-text",
        "message_id": str(payload.get("message_id") or "").strip(),
        "reply_in_thread": bool(payload.get("reply_in_thread")),
        "text": text,
        "render_mode": "text",
        "transport": {"msg_type": "text", "content": {"text": text}},
        "render_meta": {
            "renderer_version": "legacy",
            "source_chars": len(text),
            "source_sha256": _sha256(text),
            "rendered_sha256": _sha256(text),
            "rendered_content_bytes": len(text.encode("utf-8")),
            "markdown_table_detected": has_markdown_table(text),
            "truncated": False,
        },
    }


def is_feishu_reply_envelope(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    return (
        str(payload.get("schema_version") or "") == FEISHU_REPLY_ENVELOPE_SCHEMA_VERSION
        and isinstance(payload.get("transport"), dict)
        and bool(str(payload.get("message_id") or "").strip())
    )


def sanitize_feishu_markdown(value: str) -> str:
    text = _normalize_text(value)
    text = _HTML_TAG_RE.sub(lambda match: html.escape(match.group(0), quote=False), text)
    text = _IMAGE_RE.sub(_replace_image, text)
    text = _REFERENCE_IMAGE_RE.sub(lambda match: str(match.group(1) or "").strip() or "图片", text)
    text = _REFERENCE_DEFINITION_RE.sub(_replace_reference_definition, text)
    return _LINK_RE.sub(_replace_link, text)


def has_rich_markdown(value: str) -> bool:
    text = str(value or "")
    return bool(
        has_markdown_table(text)
        or _HEADING_RE.search(text)
        or _LIST_RE.search(text)
        or _FENCE_RE.search(text)
        or _BOLD_RE.search(text)
    )


def has_markdown_table(value: str) -> bool:
    lines = str(value or "").splitlines()
    return any(
        index + 1 < len(lines)
        and _looks_like_table_row(lines[index])
        and _is_table_separator(lines[index + 1])
        for index in range(len(lines))
    )


def truncate_feishu_markdown(
    value: str,
    *,
    max_chars: int,
    max_bytes: int,
) -> tuple[str, bool]:
    text = _normalize_text(value)
    if _fits(text, max_chars=max_chars, max_bytes=max_bytes):
        return text, False

    blocks = _split_markdown_blocks(text)
    kept: list[str] = []
    for block in blocks:
        candidate = _join_blocks([*kept, block.text])
        if _fits(
            _with_notice(candidate),
            max_chars=max_chars,
            max_bytes=max_bytes,
        ):
            kept.append(block.text)
            continue
        partial = _partial_block(
            block,
            prefix_blocks=kept,
            max_chars=max_chars,
            max_bytes=max_bytes,
        )
        if partial:
            kept.append(partial)
        break

    truncated = _with_notice(_join_blocks(kept))
    if _fits(truncated, max_chars=max_chars, max_bytes=max_bytes):
        return truncated, True
    notice = FEISHU_REPLY_TRUNCATION_NOTICE
    if _fits(notice, max_chars=max_chars, max_bytes=max_bytes):
        return notice, True
    return _fit_plain_prefix(notice, max_chars=max_chars, max_bytes=max_bytes), True


def flatten_markdown_tables(value: str) -> str:
    rendered: list[str] = []
    for block in _split_markdown_blocks(_normalize_text(value)):
        if block.kind != "table":
            rendered.append(_flatten_non_table_markdown(block.text))
            continue
        lines = block.text.splitlines()
        headers = _split_table_row(lines[0])
        rows = [_split_table_row(line) for line in lines[2:]]
        for row in rows:
            pairs = [
                f"{header}：{row[index]}"
                for index, header in enumerate(headers)
                if header and index < len(row) and row[index]
            ]
            if pairs:
                rendered.append("\n".join(pairs))
    return _join_blocks([item for item in rendered if item.strip()])


def _normalize_text(value: str) -> str:
    return str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def _replace_image(match: re.Match[str]) -> str:
    alt = str(match.group(1) or "").strip()
    destination = _markdown_destination(match.group(2))
    if destination and _is_safe_http_url(destination):
        return f"{alt or '图片'}（{destination}）"
    return alt or "图片"


def _replace_link(match: re.Match[str]) -> str:
    label = str(match.group(1) or "").strip()
    destination = _markdown_destination(match.group(2))
    if destination and _is_safe_http_url(destination):
        return match.group(0)
    return label


def _replace_reference_definition(match: re.Match[str]) -> str:
    destination = _markdown_destination(match.group(2))
    return match.group(0) if destination and _is_safe_http_url(destination) else ""


def _markdown_destination(value: str) -> str:
    raw = str(value or "").strip()
    if raw.startswith("<") and ">" in raw:
        return raw[1 : raw.index(">")].strip()
    return raw.split(maxsplit=1)[0].strip()


def _is_safe_http_url(value: str) -> bool:
    try:
        return urlsplit(value).scheme.lower() in {"http", "https"}
    except Exception:
        return False


def _split_markdown_blocks(value: str) -> list[_MarkdownBlock]:
    lines = str(value or "").splitlines()
    blocks: list[_MarkdownBlock] = []
    index = 0
    while index < len(lines):
        if not lines[index].strip():
            index += 1
            continue
        if lines[index].lstrip().startswith("```"):
            start = index
            index += 1
            while index < len(lines):
                if lines[index].lstrip().startswith("```"):
                    index += 1
                    break
                index += 1
            blocks.append(_MarkdownBlock("fence", "\n".join(lines[start:index])))
            continue
        if (
            index + 1 < len(lines)
            and _looks_like_table_row(lines[index])
            and _is_table_separator(lines[index + 1])
        ):
            start = index
            index += 2
            while index < len(lines) and lines[index].strip() and _looks_like_table_row(lines[index]):
                index += 1
            blocks.append(_MarkdownBlock("table", "\n".join(lines[start:index])))
            continue
        start = index
        index += 1
        while index < len(lines):
            if not lines[index].strip():
                break
            if lines[index].lstrip().startswith("```"):
                break
            if (
                index + 1 < len(lines)
                and _looks_like_table_row(lines[index])
                and _is_table_separator(lines[index + 1])
            ):
                break
            index += 1
        blocks.append(_MarkdownBlock("text", "\n".join(lines[start:index])))
    return blocks


def _partial_block(
    block: _MarkdownBlock,
    *,
    prefix_blocks: list[str],
    max_chars: int,
    max_bytes: int,
) -> str:
    if block.kind == "fence":
        return ""
    lines = block.text.splitlines()
    minimum_lines = 2 if block.kind == "table" else 0
    partial: list[str] = []
    for line in lines:
        candidate_partial = "\n".join([*partial, line])
        candidate = _with_notice(_join_blocks([*prefix_blocks, candidate_partial]))
        if _fits(candidate, max_chars=max_chars, max_bytes=max_bytes):
            partial.append(line)
            continue
        if partial or block.kind == "table":
            break
        prefix = _fit_line_with_context(
            line,
            prefix_blocks=prefix_blocks,
            max_chars=max_chars,
            max_bytes=max_bytes,
        )
        if prefix:
            partial.append(prefix)
        break
    if block.kind == "table" and len(partial) < minimum_lines:
        return ""
    return "\n".join(partial).rstrip()


def _fit_line_with_context(
    line: str,
    *,
    prefix_blocks: list[str],
    max_chars: int,
    max_bytes: int,
) -> str:
    low = 0
    high = len(line)
    best = ""
    while low <= high:
        middle = (low + high) // 2
        candidate_prefix = line[:middle].rstrip()
        candidate = _with_notice(_join_blocks([*prefix_blocks, candidate_prefix]))
        if candidate_prefix and _fits(candidate, max_chars=max_chars, max_bytes=max_bytes):
            best = candidate_prefix
            low = middle + 1
        else:
            high = middle - 1
    return best


def _fit_plain_prefix(value: str, *, max_chars: int, max_bytes: int) -> str:
    return _fit_line_with_context(
        value,
        prefix_blocks=[],
        max_chars=max_chars,
        max_bytes=max_bytes,
    )


def _fits(value: str, *, max_chars: int, max_bytes: int) -> bool:
    return (max_chars <= 0 or len(value) <= max_chars) and (
        max_bytes <= 0 or len(value.encode("utf-8")) <= max_bytes
    )


def _with_notice(value: str) -> str:
    text = str(value or "").strip()
    return f"{text}\n\n{FEISHU_REPLY_TRUNCATION_NOTICE}" if text else FEISHU_REPLY_TRUNCATION_NOTICE


def _join_blocks(blocks: list[str]) -> str:
    return "\n\n".join(str(block).strip() for block in blocks if str(block).strip())


def _looks_like_table_row(line: str) -> bool:
    return len(_split_table_row(line)) >= 2


def _is_table_separator(line: str) -> bool:
    cells = _split_table_row(line)
    return len(cells) >= 2 and all(_TABLE_SEPARATOR_CELL_RE.fullmatch(cell) for cell in cells)


def _split_table_row(line: str) -> list[str]:
    value = str(line or "").strip()
    if value.startswith("|"):
        value = value[1:]
    if value.endswith("|") and not value.endswith(r"\|"):
        value = value[:-1]
    return [cell.strip().replace(r"\|", "|") for cell in re.split(r"(?<!\\)\|", value)]


def _flatten_non_table_markdown(value: str) -> str:
    text = re.sub(r"(?m)^\s{0,3}#{1,6}\s+", "", str(value or ""))
    text = re.sub(r"(?m)^\s*[-+*]\s+", "• ", text)
    text = text.replace("**", "").replace("__", "").replace("```", "").replace("`", "")
    return text.strip()


def _sha256(value: str) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


__all__ = [
    "FEISHU_CARD_SCHEMA_VERSION",
    "FEISHU_REPLY_CONTENT_BUDGET_BYTES",
    "FEISHU_REPLY_ENVELOPE_SCHEMA_VERSION",
    "FEISHU_REPLY_RENDERER_VERSION",
    "FEISHU_REPLY_TRUNCATION_NOTICE",
    "flatten_markdown_tables",
    "has_markdown_table",
    "has_rich_markdown",
    "is_feishu_reply_envelope",
    "legacy_text_envelope",
    "render_feishu_conversation_reply",
    "sanitize_feishu_markdown",
    "truncate_feishu_markdown",
]
