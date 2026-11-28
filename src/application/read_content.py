"""Bounded, redacted content pages; callers own source reads and authorization."""
from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from typing import Any

from src.application.query_cursor import CursorError, decode_cursor, digest, encode_cursor
from src.application.research.redaction import redact_text

MAX_FILE_BYTES = 1024 * 1024
MAX_OUTPUT_BYTES = 9000
MAX_PROJECT_OUTPUT_BYTES = 24000
REDACTION_VERSION = "project-lines-v1"


def check_read_budget(deadline_monotonic: float | None, cancelled: Callable[[], bool] | None) -> None:
    if cancelled is not None and cancelled():
        raise CursorError("cancelled")
    if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
        raise CursorError("time_deadline")


def set_continuation(result: dict[str, Any], state: dict[str, Any] | None) -> None:
    result["next_cursor"] = None
    if state is None:
        return
    try:
        cursor = encode_cursor(state)
        if len(cursor) > 8192:
            raise CursorError("scope_too_broad")
        result["next_cursor"] = cursor
    except CursorError as exc:
        if exc.code != "continuation_unavailable":
            raise
        result["continuation_status"] = exc.code
        coverage = result.get("coverage")
        if isinstance(coverage, dict):
            coverage.update(status="partial", complete=False, has_more=True)
        if "body_complete" in result:
            result["body_complete"] = False


def redacted_text(raw: bytes) -> str:
    if len(raw) > MAX_FILE_BYTES:
        raise CursorError("file_too_large")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise CursorError("unsupported") from None
    if any(ord(char) < 32 and char not in "\n\r\t" for char in text):
        raise CursorError("unsupported")
    return redact_text(text, preserve_newlines=True)


def page_text(raw: bytes, *, relative_name: str, scope: dict[str, Any], cursor: str | None = None,
              start_line: int = 1, max_lines: int = 80, resource: str = "project",
              project_token_limit: int = 2800, max_chars: int | None = None,
              deadline_monotonic: float | None = None, cancelled: Callable[[], bool] | None = None) -> dict[str, Any]:
    check_read_budget(deadline_monotonic, cancelled)
    if type(start_line) is not int or start_line < 1 or type(max_lines) is not int or not 1 <= max_lines <= 300:
        raise CursorError("INPUT_ERROR")
    if max_chars is not None and (type(max_chars) is not int or not 1 <= max_chars <= MAX_FILE_BYTES):
        raise CursorError("INPUT_ERROR")
    binding = digest({"resource": resource, "action": "read", "name": relative_name, "scope": scope})
    state = decode_cursor(cursor, binding)
    raw_hash = hashlib.sha256(raw).hexdigest()
    if state and state.get("hash") != raw_hash:
        raise CursorError("source_changed")
    text = redacted_text(raw)
    check_read_budget(deadline_monotonic, cancelled)
    lines = text.splitlines(keepends=True) or [""]
    offset = int(state["offset"]) if state else sum(len(line) for line in lines[:start_line - 1])
    if not 0 <= offset <= len(text):
        raise CursorError("cursor_invalidated")
    line = text.count("\n", 0, offset) + 1
    char = offset - (text.rfind("\n", 0, offset) + 1)
    end = offset
    for _ in range(max_lines):
        boundary = text.find("\n", end)
        end = len(text) if boundary < 0 else boundary + 1
        if end == len(text):
            break
    end = min(end, offset + (max_chars if max_chars is not None else MAX_PROJECT_OUTPUT_BYTES if resource == "project" else 4000))
    lower, upper, best = offset, end, None
    while True:
        check_read_budget(deadline_monotonic, cancelled)
        result = {"text": text[offset:end], "relative_name": redact_text(relative_name),
                  "source": {"resource": resource, "revision": raw_hash, "content_hash": raw_hash, "redaction_version": REDACTION_VERSION},
                  "body_range": {"start_line": line, "start_char": char, "end_line": text.count("\n", 0, end) + 1, "end_char": end - (text.rfind("\n", 0, end) + 1)},
                  "body_complete": end == len(text),
                  "coverage": {"status": "complete", "complete_for": "point", "has_more": end < len(text)},
                  "freshness": {"status": "not_applicable"}, "action": "read", "resource": resource}
        result["source"].update(relative_name=result["relative_name"], body_range=result["body_range"])
        result["scope"] = {**{k: scope[k] for k in ("account", "market", "config_key", "run_id") if k in scope}, "resource": resource, "relative_name": result["relative_name"], "revision": raw_hash, "body_range": result["body_range"]}
        set_continuation(result, {"binding": binding, "hash": raw_hash, "offset": end} if end < len(text) else None)
        if resource != "project":
            if len(json.dumps(result, ensure_ascii=False).encode()) <= MAX_OUTPUT_BYTES:
                return result
            if end <= offset + 1:
                raise CursorError("scope_too_broad")
            end = offset + max(1, (end - offset) // 2)
            continue
        if project_output_fits(result, project_token_limit):
            lower, best = end, result
        else:
            upper = end - 1
        if upper <= lower:
            if best is not None:
                return best
            raise CursorError("scope_too_broad")
        end = (lower + upper + 1) // 2


def project_output_fits(result: dict[str, Any], token_limit: int = 2800) -> bool:
    # Reserve room for the Bot contract and Host metadata before consuming a page.
    # Scope is copied both at the observation root and inside coverage.
    serialized = json.dumps([result, *(result.get(key) for key in
        ("source", "scope", "scope", "coverage", "freshness"))], ensure_ascii=False)
    non_ascii = sum(ord(char) > 127 for char in serialized)
    estimate = ((len(serialized) - non_ascii) / 4 + non_ascii) * 1.10
    return estimate <= token_limit and len(json.dumps(result, ensure_ascii=False).encode()) <= MAX_PROJECT_OUTPUT_BYTES
