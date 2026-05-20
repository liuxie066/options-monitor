from __future__ import annotations

import json
from typing import Any

from src.application.agent_tool_contracts import mask_path
from src.application.inbound.audit import InboundAuditStore
from src.application.inbound.operation_store import InboundOperationStore
from src.application.inbound.renderer import render_pending_operations


def collect_pending_operations(
    *,
    audit_db: str | None = None,
    channel: str | None = None,
    sender_id: str | None = None,
    conversation_id: str | None = None,
    operation_types: list[str] | tuple[str, ...] | None = None,
    include_expired: bool = False,
    limit: int = 20,
) -> dict[str, Any]:
    store = InboundOperationStore(audit_db)
    types = {str(item).strip() for item in (operation_types or []) if str(item).strip()}
    operations = store.list_pending_operations(
        channel=channel,
        sender_id=sender_id,
        conversation_id=conversation_id,
        operation_types=types,
        include_expired=include_expired,
        limit=limit,
    )
    filters = _filters(
        channel=channel,
        sender_id=sender_id,
        conversation_id=conversation_id,
        operation_types=sorted(types),
        include_expired=include_expired,
        limit=limit,
    )
    return {
        "audit_db": mask_path(store.path),
        "filters": filters,
        "pending_count": len(operations),
        "pending_operations": operations,
        "response_text": format_pending_operations(operations, filters=filters),
    }


def collect_recent_audit(
    *,
    audit_db: str | None = None,
    channel: str | None = None,
    sender_id: str | None = None,
    conversation_id: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    store = InboundAuditStore(audit_db)
    rows = [
        _audit_row_summary(row)
        for row in store.list_recent(
            channel=channel,
            sender_id=sender_id,
            conversation_id=conversation_id,
            limit=limit,
        )
    ]
    filters = _filters(
        channel=channel,
        sender_id=sender_id,
        conversation_id=conversation_id,
        limit=limit,
    )
    return {
        "audit_db": mask_path(store.path),
        "filters": filters,
        "audit_count": len(rows),
        "audit_rows": rows,
        "response_text": format_recent_audit(rows, filters=filters),
    }


def format_pending_operations(operations: list[dict[str, Any]], *, filters: dict[str, Any]) -> str:
    scope = _scope_text(filters)
    if not operations:
        return f"Inbound pending：0 条\nscope：{scope}\n没有待确认操作。"
    rendered = render_pending_operations(operations)
    return f"Inbound pending：{len(operations)} 条\nscope：{scope}\n{rendered}"


def format_recent_audit(rows: list[dict[str, Any]], *, filters: dict[str, Any]) -> str:
    scope = _scope_text(filters)
    if not rows:
        return f"Inbound audit recent：0 条\nscope：{scope}\n没有匹配的 inbound 审计记录。"
    lines = [f"Inbound audit recent：{len(rows)} 条", f"scope：{scope}"]
    for row in rows:
        ok = "ok" if row.get("result_ok") is True else "failed"
        head = (
            f"- {row.get('created_at') or '-'} "
            f"{ok} "
            f"{row.get('decision') or '-'} "
            f"{row.get('intent_name') or '-'} "
            f"sender={row.get('channel') or '-'}:{row.get('sender_id') or '-'} "
            f"message={row.get('message_id') or '-'}"
        )
        lines.append(head)
        raw_text = str(row.get("raw_text") or "").strip()
        if raw_text:
            lines.append(f"  text: {_clip(raw_text, 160)}")
        response_text = str(row.get("response_text") or "").strip()
        if response_text:
            lines.append(f"  reply: {_clip(response_text, 180)}")
        error_code = str(row.get("error_code") or "").strip()
        if error_code:
            lines.append(f"  error: {error_code}")
        duplicate_count = int(row.get("duplicate_count") or 0)
        if duplicate_count:
            lines.append(f"  duplicates: {duplicate_count}")
    return "\n".join(lines)


def _audit_row_summary(row: dict[str, Any]) -> dict[str, Any]:
    response = _loads(row.get("response_json"))
    data = response.get("data") if isinstance(response.get("data"), dict) else {}
    error = response.get("error") if isinstance(response.get("error"), dict) else {}
    return {
        "command_id": row.get("command_id"),
        "channel": row.get("channel"),
        "sender_id": row.get("sender_id"),
        "conversation_id": row.get("conversation_id"),
        "message_id": row.get("message_id"),
        "raw_text": row.get("raw_text"),
        "parser": row.get("parser"),
        "intent_name": row.get("intent_name"),
        "tool_name": row.get("tool_name"),
        "tool_payload": _loads(row.get("tool_payload_json")),
        "decision": row.get("decision"),
        "result_ok": bool(row.get("result_ok")),
        "error_code": row.get("error_code") or error.get("code"),
        "response_text": data.get("response_text"),
        "created_at": row.get("created_at"),
        "finished_at": row.get("finished_at"),
        "duplicate_count": int(row.get("duplicate_count") or 0),
        "last_duplicate_at": row.get("last_duplicate_at"),
        "last_duplicate_sender_id": row.get("last_duplicate_sender_id"),
        "last_duplicate_decision": row.get("last_duplicate_decision"),
    }


def _filters(**kwargs: Any) -> dict[str, Any]:
    out = {}
    for key, value in kwargs.items():
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        if isinstance(value, list) and not value:
            continue
        out[key] = value
    return out


def _scope_text(filters: dict[str, Any]) -> str:
    parts = []
    for key in ("channel", "sender_id", "conversation_id"):
        value = filters.get(key)
        if value:
            parts.append(f"{key}={value}")
    if filters.get("operation_types"):
        parts.append("operation_types=" + ",".join(str(item) for item in filters["operation_types"]))
    parts.append(f"limit={int(filters.get('limit') or 20)}")
    if filters.get("include_expired"):
        parts.append("include_expired=yes")
    return " ".join(parts) if parts else "all"


def _loads(value: Any) -> dict[str, Any]:
    try:
        decoded = json.loads(str(value or "{}"))
    except Exception:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _clip(value: str, limit: int) -> str:
    text = str(value or "").strip().replace("\n", " / ")
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."
