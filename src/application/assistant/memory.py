from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from src.application.agent_tool_config import repo_base


ASSISTANT_MEMORY_SCHEMA_VERSION = "om-assistant-memory-v1"
ASSISTANT_MEMORY_ITEM_SCHEMA_VERSION = "om-assistant-memory-item-v1"
DEFAULT_ASSISTANT_MEMORY_DIRNAME = "assistant_memory"
MEMORY_ENTRYPOINT_NAME = "MEMORY.md"
MAX_ASSISTANT_MEMORY_CHARS = 6000
MAX_MEMORY_ITEM_CHARS = 1200
MAX_MEMORY_FILES = 50

ASSISTANT_MEMORY_TYPES = frozenset(
    {
        "collaboration_preference",
        "om_usage_preference",
        "parameter_tuning_preference",
        "parameter_change_rationale",
        "correction_feedback",
        "terminology",
        "workflow_pattern",
    }
)

_SENSITIVE_TOKENS = (
    "access_token",
    "api key",
    "api_key",
    "authorization",
    "cookie",
    "password",
    "private_key",
    "secret",
    "token",
    "webhook",
)


def default_assistant_memory_dir() -> Path:
    return (repo_base() / DEFAULT_ASSISTANT_MEMORY_DIRNAME).resolve()


def load_assistant_memory_context(
    *,
    path: str | Path | None = None,
    query: str | None = None,
    max_memories: int = 5,
    max_chars: int = MAX_ASSISTANT_MEMORY_CHARS,
) -> dict[str, Any]:
    memory_dir = Path(path).expanduser().resolve() if path else default_assistant_memory_dir()
    if not memory_dir.exists():
        return _empty_memory(reason="missing")
    if not memory_dir.is_dir():
        return _empty_memory(reason="not_dir")

    query_terms = _query_terms(query)
    items: list[dict[str, Any]] = []
    for file_path in _memory_files(memory_dir):
        item = _load_memory_item(memory_dir=memory_dir, file_path=file_path, query_terms=query_terms)
        if item is not None:
            items.append(item)

    if not items:
        return _empty_memory(reason="empty")

    ranked = sorted(
        items,
        key=lambda item: (
            int(item.get("relevance", {}).get("score") or 0),
            str(item.get("title") or ""),
            str(item.get("memory_id") or ""),
        ),
        reverse=True,
    )
    if query_terms:
        ranked = [item for item in ranked if int(item.get("relevance", {}).get("score") or 0) > 0]
    selected: list[dict[str, Any]] = []
    total_chars = 0
    for item in ranked[: max(0, int(max_memories or 0))]:
        size = len(str(item.get("content") or "")) + len(str(item.get("summary") or ""))
        if selected and total_chars + size > max(1, int(max_chars or MAX_ASSISTANT_MEMORY_CHARS)):
            break
        selected.append(item)
        total_chars += size

    if not selected:
        return _empty_memory(reason="no_relevant_memory")

    return {
        "schema_version": ASSISTANT_MEMORY_SCHEMA_VERSION,
        "provided": True,
        "source": DEFAULT_ASSISTANT_MEMORY_DIRNAME,
        "format": "markdown_topic_files",
        "memory_count": len(selected),
        "memories": selected,
        "policy": _memory_policy(),
    }


def assistant_memory_trace(memory: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(memory, dict) or not bool(memory.get("provided")):
        reason = memory.get("reason") if isinstance(memory, dict) else None
        return {"provided": False, "reason": str(reason or "missing")}
    return {
        "provided": True,
        "source": str(memory.get("source") or DEFAULT_ASSISTANT_MEMORY_DIRNAME),
        "format": str(memory.get("format") or "markdown_topic_files"),
        "memory_count": int(memory.get("memory_count") or 0),
        "types": sorted(
            {
                str(item.get("type") or "")
                for item in memory.get("memories") or []
                if isinstance(item, dict) and str(item.get("type") or "").strip()
            }
        ),
    }


def _empty_memory(*, reason: str) -> dict[str, Any]:
    return {
        "schema_version": ASSISTANT_MEMORY_SCHEMA_VERSION,
        "provided": False,
        "source": DEFAULT_ASSISTANT_MEMORY_DIRNAME,
        "reason": reason,
    }


def _memory_files(memory_dir: Path) -> list[Path]:
    files: list[Path] = []
    try:
        for file_path in sorted(memory_dir.glob("*.md")):
            if file_path.name == MEMORY_ENTRYPOINT_NAME:
                continue
            if file_path.is_file():
                files.append(file_path)
            if len(files) >= MAX_MEMORY_FILES:
                break
    except OSError:
        return []
    return files


def _load_memory_item(*, memory_dir: Path, file_path: Path, query_terms: set[str]) -> dict[str, Any] | None:
    try:
        raw = file_path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None
    frontmatter, body = _split_frontmatter(raw)
    memory_type = str(frontmatter.get("type") or "").strip()
    if memory_type not in ASSISTANT_MEMORY_TYPES:
        return None
    status = str(frontmatter.get("status") or "active").strip().lower()
    if status not in {"active", "accepted"}:
        return None
    redacted_body, redacted_line_count = _redact_sensitive_lines(body.strip())
    title, title_redactions = _redact_sensitive_scalar(str(frontmatter.get("title") or file_path.stem).strip())
    summary, summary_redactions = _redact_sensitive_scalar(
        str(frontmatter.get("summary") or frontmatter.get("description") or "").strip()
    )
    tags, tag_redactions = _redact_sensitive_list(_list_value(frontmatter.get("tags")))
    redacted_line_count += title_redactions + summary_redactions + tag_redactions
    searchable = "\n".join([title, summary, " ".join(tags), redacted_body])
    score, matched_terms = _relevance(query_terms=query_terms, searchable=searchable)
    content = redacted_body[:MAX_MEMORY_ITEM_CHARS]
    return {
        "schema_version": ASSISTANT_MEMORY_ITEM_SCHEMA_VERSION,
        "memory_id": _memory_id(memory_dir=memory_dir, file_path=file_path),
        "type": memory_type,
        "title": title[:160],
        "summary": summary[:240],
        "content": content,
        "tags": tags[:8],
        "status": status,
        "redacted_line_count": redacted_line_count,
        "relevance": {
            "score": score,
            "matched_terms": matched_terms[:8],
        },
    }


def _split_frontmatter(raw: str) -> tuple[dict[str, Any], str]:
    text = raw.strip()
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()
    end_index = None
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            end_index = index
            break
    if end_index is None:
        return {}, text
    frontmatter = _parse_frontmatter_lines(lines[1:end_index])
    body = "\n".join(lines[end_index + 1 :])
    return frontmatter, body


def _parse_frontmatter_lines(lines: list[str]) -> dict[str, Any]:
    parsed: dict[str, Any] = {}
    for line in lines:
        if not line.strip() or line.lstrip().startswith("#") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        parsed[key.strip()] = _parse_frontmatter_value(value.strip())
    return parsed


def _parse_frontmatter_value(value: str) -> Any:
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [item.strip().strip("'\"") for item in inner.split(",") if item.strip()]
    return value.strip("'\"")


def _list_value(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip()[:80] for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()[:80]]
    return []


def _memory_id(*, memory_dir: Path, file_path: Path) -> str:
    try:
        rel = file_path.relative_to(memory_dir)
    except ValueError:
        rel = Path(file_path.name)
    return rel.with_suffix("").as_posix().replace("/", ".")


def _query_terms(query: str | None) -> set[str]:
    text = str(query or "").strip().lower()
    if not text:
        return set()
    terms = {part for part in re.findall(r"[a-z0-9_.-]{2,}", text)}
    terms.update(part for part in re.findall(r"[\u4e00-\u9fff]{2,}", text))
    for marker in ("收益", "收入", "净收入", "参数", "调参", "流动性", "通过率", "候选", "配置"):
        if marker in text:
            terms.add(marker)
    return terms


def _relevance(*, query_terms: set[str], searchable: str) -> tuple[int, list[str]]:
    if not query_terms:
        return 1, []
    lowered = searchable.lower()
    matched = sorted(term for term in query_terms if term and term in lowered)
    return len(matched), matched


def _redact_sensitive_lines(content: str) -> tuple[str, int]:
    lines: list[str] = []
    redacted = 0
    for line in content.splitlines():
        if _looks_sensitive(line):
            lines.append("[redacted sensitive line]")
            redacted += 1
        else:
            lines.append(line)
    return "\n".join(lines), redacted


def _redact_sensitive_scalar(value: str) -> tuple[str, int]:
    text = str(value or "").strip()
    if _looks_sensitive(text):
        return "[redacted sensitive line]", 1
    return text, 0


def _redact_sensitive_list(values: list[str]) -> tuple[list[str], int]:
    out: list[str] = []
    redacted = 0
    for value in values:
        text, count = _redact_sensitive_scalar(value)
        out.append(text)
        redacted += count
    return out, redacted


def _looks_sensitive(line: str) -> bool:
    lowered = line.lower()
    if not any(token in lowered for token in _SENSITIVE_TOKENS):
        return False
    return any(marker in lowered for marker in ("=", ":", "sk-", "bearer ", "http://", "https://"))


def _memory_policy() -> dict[str, bool]:
    return {
        "memory_is_hint_only": True,
        "current_message_wins": True,
        "tool_evidence_wins": True,
        "do_not_treat_memory_as_market_or_ledger_fact": True,
        "memory_cannot_authorize_writes": True,
    }


__all__ = [
    "ASSISTANT_MEMORY_SCHEMA_VERSION",
    "ASSISTANT_MEMORY_TYPES",
    "DEFAULT_ASSISTANT_MEMORY_DIRNAME",
    "MAX_ASSISTANT_MEMORY_CHARS",
    "assistant_memory_trace",
    "default_assistant_memory_dir",
    "load_assistant_memory_context",
]
