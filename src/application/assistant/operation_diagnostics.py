from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from src.application.agent_tool_contracts import mask_path
from src.application.assistant.audit import InboundAuditStore, inbound_sqlite_error
from src.application.assistant.operation_lifecycle import build_action_lifecycle
from src.application.assistant.operation_store import InboundOperationStore
from src.application.assistant.renderer import render_pending_operations
from src.application.assistant.turn_result import copilot_events_from_response_data, copilot_trace_from_response_data
from src.application.payload_helpers import as_dict as _dict
from src.application.payload_helpers import first_text as _first_text


OPERATION_TIMELINE_SCHEMA_VERSION = "operation-timeline-v1"
_TIMELINE_STATUSES = ("previewed", "confirmed", "running", "applied", "cancelled", "expired", "failed")
_OPERATION_AUDIT_TOOLS = frozenset({"inbound.manual_trade", "inbound.symbols", "inbound.upgrade", "inbound.model", "inbound.monitor_run"})


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


def collect_operation_timeline(
    *,
    audit_db: str | None = None,
    channel: str | None = None,
    sender_id: str | None = None,
    conversation_id: str | None = None,
    operation_id: str | None = None,
    operation_types: list[str] | tuple[str, ...] | None = None,
    statuses: list[str] | tuple[str, ...] | None = None,
    limit: int = 10,
    audit_scan_limit: int | None = None,
) -> dict[str, Any]:
    store = InboundAuditStore(audit_db)
    path = store.path
    limit_value = _bounded_int(limit, default=10, low=1, high=50)
    audit_limit = _bounded_int(audit_scan_limit, default=max(100, limit_value * 10), low=20, high=500)
    operation_type_list = _string_list(operation_types)
    status_list = _string_list(statuses)
    filters = _filters(
        channel=channel,
        sender_id=sender_id,
        conversation_id=conversation_id,
        operation_id=operation_id,
        operation_types=operation_type_list,
        statuses=status_list,
        limit=limit_value,
        audit_scan_limit=audit_limit,
    )
    warnings: list[str] = []
    if not path.exists():
        warnings.append("audit_db_missing")
        return {
            "schema_version": OPERATION_TIMELINE_SCHEMA_VERSION,
            "audit_db": mask_path(path),
            "audit_db_exists": False,
            "filters": filters,
            "timeline_count": 0,
            "timelines": [],
            "warnings": warnings,
            "response_text": format_operation_timeline([], filters=filters, warnings=warnings),
        }

    try:
        with _connect_existing_sqlite(path) as conn:
            tables = _sqlite_tables(conn)
            operation_store_missing = "inbound_pending_operations" not in tables
            if "inbound_pending_operations" not in tables:
                warnings.append("operations_table_missing")
                operations: list[dict[str, Any]] = []
            else:
                operations = _read_operation_rows(
                    conn,
                    channel=channel,
                    sender_id=sender_id,
                    conversation_id=conversation_id,
                    operation_id=operation_id,
                    operation_types=set(operation_type_list),
                    statuses=set(status_list),
                    limit=limit_value,
                )
            if "inbound_command_audit" not in tables:
                warnings.append("audit_table_missing")
                audit_rows: list[dict[str, Any]] = []
            else:
                audit_rows = _read_audit_rows(
                    conn,
                    channel=channel,
                    sender_id=sender_id,
                    conversation_id=conversation_id,
                    limit=audit_limit,
                )
    except sqlite3.Error as exc:
        raise inbound_sqlite_error(path, exc) from exc

    timelines = [_build_operation_timeline(operation, audit_rows) for operation in operations]
    existing_operation_ids = {
        str(item.get("operation_id") or "").strip()
        for item in operations
        if str(item.get("operation_id") or "").strip()
    }
    remaining_limit = max(0, limit_value - len(timelines))
    if remaining_limit:
        timelines.extend(
            _build_audit_only_timelines(
                audit_rows,
                existing_operation_ids=existing_operation_ids,
                operation_id=operation_id,
                operation_types=set(operation_type_list),
                statuses=set(status_list),
                operation_store_missing=operation_store_missing,
                limit=remaining_limit,
            )
        )
    for item in timelines:
        for warning in item.get("warnings") or []:
            text = str(warning or "").strip()
            if text and text not in warnings:
                warnings.append(text)
    return {
        "schema_version": OPERATION_TIMELINE_SCHEMA_VERSION,
        "audit_db": mask_path(path),
        "audit_db_exists": True,
        "filters": filters,
        "timeline_count": len(timelines),
        "timelines": timelines,
        "warnings": warnings,
        "response_text": format_operation_timeline(timelines, filters=filters, warnings=warnings),
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


def format_operation_timeline(timelines: list[dict[str, Any]], *, filters: dict[str, Any], warnings: list[str]) -> str:
    scope = _scope_text(filters)
    if not timelines:
        return f"Operation timeline：0 条\nscope：{scope}\n没有匹配的 operator operation。"
    lines = [f"Operation timeline：{len(timelines)} 条", f"scope：{scope}"]
    if warnings:
        lines.append("warnings：" + ",".join(sorted(set(warnings))))
    for item in timelines:
        identity = _dict(item.get("identity"))
        operation = _dict(item.get("operation"))
        outcome = _dict(item.get("outcome"))
        lifecycle = _dict(item.get("action_lifecycle"))
        lines.append(
            "- "
            f"{operation.get('created_at') or '-'} "
            f"{operation.get('operation_type') or '-'} "
            f"status={outcome.get('status') or operation.get('status') or '-'} "
            f"phase={lifecycle.get('phase') or '-'} "
            f"verify={lifecycle.get('verify_status') or '-'} "
            f"operation_id={identity.get('operation_id') or '-'} "
            f"event={identity.get('ledger_event_id') or '-'} "
            f"record={identity.get('record_id') or '-'} "
            f"message={identity.get('inbound_message_id') or '-'}"
        )
        summary = str(operation.get("summary") or "").strip()
        if summary:
            lines.append(f"  summary: {_clip(summary, 180)}")
        item_warnings = [str(value) for value in (item.get("warnings") or []) if str(value).strip()]
        if item_warnings:
            lines.append("  warnings: " + ",".join(item_warnings))
    return "\n".join(lines)


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
        copilot = row.get("copilot") if isinstance(row.get("copilot"), dict) else {}
        if copilot:
            parts = [f"{key}={value}" for key, value in copilot.items() if value]
            if parts:
                lines.append("  copilot: " + " ".join(parts))
        copilot_events = row.get("copilot_events") if isinstance(row.get("copilot_events"), dict) else {}
        failures = copilot_events.get("failure_reasons") if isinstance(copilot_events.get("failure_reasons"), list) else []
        if failures:
            lines.append("  copilot_events: failures=" + ",".join(str(item) for item in failures if str(item).strip()))
        duplicate_count = int(row.get("duplicate_count") or 0)
        if duplicate_count:
            lines.append(f"  duplicates: {duplicate_count}")
    return "\n".join(lines)


def _audit_row_summary(row: dict[str, Any]) -> dict[str, Any]:
    response = _loads(row.get("response_json"))
    data = _dict(response.get("data"))
    error = _dict(response.get("error"))
    summary = {
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
        "control": _audit_control(row),
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
    copilot = copilot_trace_from_response_data(data)
    if copilot:
        summary["copilot"] = copilot
    copilot_events = copilot_events_from_response_data(data)
    if copilot_events:
        summary["copilot_events"] = copilot_events
    return summary


def _connect_existing_sqlite(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _sqlite_tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {str(row["name"]) for row in rows}


def _read_operation_rows(
    conn: sqlite3.Connection,
    *,
    channel: str | None,
    sender_id: str | None,
    conversation_id: str | None,
    operation_id: str | None,
    operation_types: set[str],
    statuses: set[str],
    limit: int,
) -> list[dict[str, Any]]:
    where: list[str] = []
    params: list[Any] = []
    normalized_operation_id = str(operation_id or "").strip()
    normalized_statuses = {str(item).strip() for item in statuses if str(item).strip()} or set(_TIMELINE_STATUSES)
    if normalized_operation_id:
        where.append("operation_id = ?")
        params.append(normalized_operation_id)
    else:
        where.append(f"status IN ({','.join('?' for _ in sorted(normalized_statuses))})")
        params.extend(sorted(normalized_statuses))
    normalized_channel = str(channel or "").strip().lower()
    normalized_sender = str(sender_id or "").strip()
    normalized_conversation = str(conversation_id or "").strip()
    if normalized_channel:
        where.append("channel = ?")
        params.append(normalized_channel)
    if normalized_sender:
        where.append("sender_id = ?")
        params.append(normalized_sender)
    if normalized_conversation:
        where.append("conversation_id = ?")
        params.append(normalized_conversation)
    if operation_types:
        where.append(f"operation_type IN ({','.join('?' for _ in sorted(operation_types))})")
        params.extend(sorted(operation_types))
    params.append(limit)
    rows = conn.execute(
        f"""
        SELECT *
        FROM inbound_pending_operations
        WHERE {' AND '.join(where)}
        ORDER BY created_at DESC, id DESC
        LIMIT ?
        """,
        tuple(params),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        item = {key: row[key] for key in row.keys()}
        item["payload"] = _loads(item.get("payload_json"))
        item["preview"] = _loads(item.get("preview_json"))
        item["result"] = _loads(item.get("result_json"))
        out.append(item)
    return out


def _read_audit_rows(
    conn: sqlite3.Connection,
    *,
    channel: str | None,
    sender_id: str | None,
    conversation_id: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    where: list[str] = []
    params: list[Any] = []
    normalized_channel = str(channel or "").strip().lower()
    normalized_sender = str(sender_id or "").strip()
    normalized_conversation = str(conversation_id or "").strip()
    if normalized_channel:
        where.append("channel = ?")
        params.append(normalized_channel)
    if normalized_sender:
        where.append("sender_id = ?")
        params.append(normalized_sender)
    if normalized_conversation:
        where.append("conversation_id = ?")
        params.append(normalized_conversation)
    params.append(limit)
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    rows = conn.execute(
        f"""
        SELECT *
        FROM inbound_command_audit
        {where_sql}
        ORDER BY created_at DESC, id DESC
        LIMIT ?
        """,
        tuple(params),
    ).fetchall()
    return [{key: row[key] for key in row.keys()} for row in rows]


def _build_operation_timeline(operation: dict[str, Any], audit_rows: list[dict[str, Any]]) -> dict[str, Any]:
    operation_id = str(operation.get("operation_id") or "").strip()
    command_id = str(operation.get("command_id") or "").strip()
    related_audit_rows = [
        row
        for row in audit_rows
        if _audit_row_matches_operation(row, operation_id=operation_id, command_id=command_id)
    ]
    preview_audit = next((row for row in related_audit_rows if str(row.get("command_id") or "") == command_id), None)
    apply_audits = [
        row
        for row in related_audit_rows
        if str(row.get("command_id") or "") != command_id and _response_operation_id(row) == operation_id
    ]
    result = _dict(operation.get("result"))
    ledger = _ledger_identity_from_result(result)
    receipt = _receipt_from_audit_rows(related_audit_rows)
    inbound_message_id = _first_text(
        preview_audit.get("message_id") if isinstance(preview_audit, dict) else None,
        *(row.get("message_id") for row in related_audit_rows),
    )
    outbound_message_id = _first_text(receipt.get("message_id"), receipt.get("outbound_message_id"))
    warnings = _timeline_warnings(
        operation=operation,
        preview_audit=preview_audit,
        apply_audits=apply_audits,
        ledger=ledger,
        receipt=receipt,
        inbound_message_id=inbound_message_id,
    )
    status = str(operation.get("status") or "").strip()
    operation_type = str(operation.get("operation_type") or "").strip()
    lifecycle = build_action_lifecycle(
        operation_id=operation_id,
        operation_type=operation_type,
        status=status,
        result=result,
        source="operation_timeline",
    )
    return {
        "schema_version": OPERATION_TIMELINE_SCHEMA_VERSION,
        "identity": {
            "command_id": command_id or None,
            "operation_id": operation_id or None,
            "run_id": _first_text(result.get("run_id")),
            "ledger_event_id": ledger.get("ledger_event_id"),
            "record_id": ledger.get("record_id"),
            "inbound_message_id": inbound_message_id,
            "outbound_message_id": outbound_message_id,
            "channel": operation.get("channel"),
            "sender_id": operation.get("sender_id"),
            "conversation_id": operation.get("conversation_id"),
        },
        "operation": {
            "operation_id": operation_id or None,
            "command_id": command_id or None,
            "operation_type": operation_type or None,
            "status": status or None,
            "summary": _operation_summary(operation),
            **_operation_version_fields(operation),
            "payload_hash": operation.get("payload_hash"),
            "created_at": operation.get("created_at"),
            "expires_at": operation.get("expires_at"),
            "confirmed_at": operation.get("confirmed_at"),
            "applied_at": operation.get("applied_at"),
            "cancelled_at": operation.get("cancelled_at"),
        },
        "action_lifecycle": lifecycle,
        "audit": {
            "preview_command_id": command_id or None,
            "preview_message_id": preview_audit.get("message_id") if isinstance(preview_audit, dict) else None,
            "related_count": len(related_audit_rows),
            "apply_count": len(apply_audits),
            "rows": [_audit_row_summary(row) for row in related_audit_rows],
        },
        "ledger": ledger,
        "receipt": receipt,
        "outcome": {
            "status": status or "unknown",
            "ok": status in {"previewed", "applied", "cancelled"},
            "warnings": warnings,
        },
        "warnings": warnings,
    }


def _build_audit_only_timelines(
    audit_rows: list[dict[str, Any]],
    *,
    existing_operation_ids: set[str],
    operation_id: str | None,
    operation_types: set[str],
    statuses: set[str],
    operation_store_missing: bool,
    limit: int,
) -> list[dict[str, Any]]:
    normalized_operation_id = str(operation_id or "").strip()
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in audit_rows:
        key = _audit_operation_key(row)
        if not key or key in existing_operation_ids:
            continue
        if normalized_operation_id and key != normalized_operation_id:
            continue
        row_operation_type = _audit_operation_type(row)
        if operation_types and row_operation_type not in operation_types:
            continue
        row_status = _audit_operation_status(row)
        if statuses and row_status not in statuses:
            continue
        if key not in groups and len(groups) >= limit:
            continue
        groups.setdefault(key, []).append(row)
    out: list[dict[str, Any]] = []
    for key, rows in groups.items():
        if len(out) >= limit:
            break
        out.append(_build_audit_only_timeline(key, rows, operation_store_missing=operation_store_missing))
    return out


def _build_audit_only_timeline(operation_id: str, rows: list[dict[str, Any]], *, operation_store_missing: bool) -> dict[str, Any]:
    preview_audit = next((row for row in rows if str(row.get("command_id") or "") == operation_id), None)
    apply_audits = [row for row in rows if str(row.get("command_id") or "") != operation_id and _response_operation_id(row) == operation_id]
    operation_type = _first_text(*(_audit_operation_type(row) for row in rows))
    status = _first_text(*(_audit_operation_status(row) for row in rows)) or "audit_only"
    ledger = _ledger_identity_from_audit_rows(rows)
    receipt = _receipt_from_audit_rows(rows)
    inbound_message_id = _first_text(
        preview_audit.get("message_id") if isinstance(preview_audit, dict) else None,
        *(row.get("message_id") for row in rows),
    )
    outbound_message_id = _first_text(receipt.get("message_id"), receipt.get("outbound_message_id"))
    warnings = ["operation_missing"]
    if operation_store_missing:
        warnings.append("operation_store_missing")
    warnings.extend(
        _timeline_warnings(
            operation={"status": status, "operation_type": operation_type},
            preview_audit=preview_audit,
            apply_audits=apply_audits,
            ledger=ledger,
            receipt=receipt,
            inbound_message_id=inbound_message_id,
        )
    )
    warnings = list(dict.fromkeys(warnings))
    lifecycle = build_action_lifecycle(
        operation_id=operation_id,
        operation_type=operation_type or "",
        status=status,
        result={"status": status},
        source="operation_timeline_audit_only",
    )
    return {
        "schema_version": OPERATION_TIMELINE_SCHEMA_VERSION,
        "identity": {
            "command_id": operation_id,
            "operation_id": operation_id,
            "run_id": None,
            "ledger_event_id": ledger.get("ledger_event_id"),
            "record_id": ledger.get("record_id"),
            "inbound_message_id": inbound_message_id,
            "outbound_message_id": outbound_message_id,
            "channel": _first_text(*(row.get("channel") for row in rows)),
            "sender_id": _first_text(*(row.get("sender_id") for row in rows)),
            "conversation_id": _first_text(*(row.get("conversation_id") for row in rows)),
        },
        "operation": {
            "operation_id": operation_id,
            "command_id": operation_id,
            "operation_type": operation_type,
            "status": status,
            "summary": operation_type or "audit_only",
            "payload_hash": None,
            "created_at": _first_text(*(row.get("created_at") for row in rows)),
            "expires_at": None,
            "confirmed_at": None,
            "applied_at": _first_text(*(row.get("finished_at") for row in rows if _audit_operation_status(row) == "applied")),
            "cancelled_at": None,
            "source": "audit_only",
        },
        "action_lifecycle": lifecycle,
        "audit": {
            "preview_command_id": operation_id,
            "preview_message_id": preview_audit.get("message_id") if isinstance(preview_audit, dict) else None,
            "related_count": len(rows),
            "apply_count": len(apply_audits),
            "rows": [_audit_row_summary(row) for row in rows],
        },
        "ledger": ledger,
        "receipt": receipt,
        "outcome": {
            "status": status,
            "ok": False,
            "warnings": warnings,
        },
        "warnings": warnings,
    }


def _audit_row_matches_operation(row: dict[str, Any], *, operation_id: str, command_id: str) -> bool:
    row_command_id = str(row.get("command_id") or "").strip()
    if row_command_id and row_command_id == command_id:
        return True
    return bool(operation_id and _response_operation_id(row) == operation_id)


def _response_operation_id(row: dict[str, Any]) -> str | None:
    response = _loads(row.get("response_json"))
    data = _dict(response.get("data"))
    return _first_text(
        data.get("operation_id"),
        data.get("resolved_operation_id"),
        _nested(data, "operation_resolution", "operation_id"),
    )


def _audit_operation_key(row: dict[str, Any]) -> str | None:
    operation_id = _response_operation_id(row)
    if operation_id:
        return operation_id
    control = _audit_control(row)
    safety_class = str(control.get("safety_class") or "").strip()
    requires_confirmation = bool(control.get("requires_confirmation"))
    tool_name = str(row.get("tool_name") or "").strip()
    if requires_confirmation or safety_class.startswith("write_"):
        return _first_text(row.get("command_id"))
    if tool_name in _OPERATION_AUDIT_TOOLS and _audit_operation_type(row):
        return _first_text(row.get("command_id"))
    return None


def _audit_operation_type(row: dict[str, Any]) -> str | None:
    response = _loads(row.get("response_json"))
    data = _dict(response.get("data"))
    operation_type = _first_text(data.get("operation_type"))
    if operation_type:
        return operation_type
    intent_name = str(row.get("intent_name") or "").strip()
    if intent_name.startswith("manual_trade_"):
        if intent_name in {"manual_trade_open", "manual_trade_confirm", "manual_trade_update"}:
            return "manual_open"
        if intent_name in {"manual_trade_close", "manual_trade_cancel"}:
            return "manual_close"
    if intent_name == "manual_assignment":
        return "manual_assignment"
    if intent_name == "manual_expiry":
        return "manual_expiry"
    return None


def _audit_operation_status(row: dict[str, Any]) -> str | None:
    response = _loads(row.get("response_json"))
    data = _dict(response.get("data"))
    status = _first_text(data.get("status"))
    if status:
        return status
    control = _audit_control(row)
    safety_class = str(control.get("safety_class") or "").strip()
    if safety_class == "write_preview":
        return "previewed"
    if safety_class == "write_apply":
        return "applied"
    return None


def _audit_control(row: dict[str, Any]) -> dict[str, Any]:
    return _loads(row.get("control_json"))


def _ledger_identity_from_result(result: dict[str, Any]) -> dict[str, Any]:
    ledger_result = _dict(result.get("result"))
    out = {
        "ledger_event_id": _first_text(ledger_result.get("event_id"), result.get("event_id")),
        "record_id": _first_text(ledger_result.get("record_id"), result.get("record_id"), result.get("snapshot_lot_id")),
        "created": ledger_result.get("created") if "created" in ledger_result else result.get("created"),
        "position_lot_count": ledger_result.get("position_lot_count") if "position_lot_count" in ledger_result else result.get("position_lot_count"),
        "result": ledger_result or result,
    }
    out["present"] = bool(out.get("ledger_event_id") or out.get("record_id"))
    return out


def _ledger_identity_from_audit_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    for row in rows:
        response = _loads(row.get("response_json"))
        data = _dict(response.get("data"))
        result = _dict(data.get("result"))
        ledger = _ledger_identity_from_result(result)
        if ledger.get("present"):
            return ledger
    return _ledger_identity_from_result({})


def _receipt_from_audit_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    for row in rows:
        response = _loads(row.get("response_json"))
        data = _dict(response.get("data"))
        receipt_raw = data.get("receipt")
        if isinstance(receipt_raw, dict):
            receipt = dict(receipt_raw)
            return {"status": "observed", **receipt}
        for key in ("delivery", "reply", "response_delivery"):
            value_raw = data.get(key)
            if isinstance(value_raw, dict):
                value = dict(value_raw)
            else:
                value = {}
            if value and (value.get("message_id") or value.get("delivery_confirmed") is not None):
                return {"status": "observed", **value}
    return {"status": "not_observed", "reason": "receipt_not_in_audit_or_operation_store"}


def _timeline_warnings(
    *,
    operation: dict[str, Any],
    preview_audit: dict[str, Any] | None,
    apply_audits: list[dict[str, Any]],
    ledger: dict[str, Any],
    receipt: dict[str, Any],
    inbound_message_id: str | None,
) -> list[str]:
    warnings: list[str] = []
    status = str(operation.get("status") or "").strip()
    operation_type = str(operation.get("operation_type") or "").strip()
    if preview_audit is None:
        warnings.append("preview_audit_missing")
    if not inbound_message_id:
        warnings.append("inbound_message_id_missing")
    if status == "applied":
        if not apply_audits:
            warnings.append("apply_audit_missing")
        if operation_type.startswith("manual_"):
            if not ledger.get("ledger_event_id"):
                warnings.append("ledger_event_id_missing")
            if not ledger.get("record_id"):
                warnings.append("record_id_missing")
    if str(receipt.get("status") or "") == "not_observed":
        warnings.append("receipt_not_observed")
    return warnings


def _operation_summary(operation: dict[str, Any]) -> str:
    payload = _dict(operation.get("payload"))
    args = _dict(payload.get("arguments"))
    operation_type = str(operation.get("operation_type") or "").strip()
    if operation_type == "manual_open":
        return " ".join(
            part
            for part in (
                str(args.get("account") or "-"),
                str(args.get("symbol") or "-"),
                f"{args.get('expiration_ymd') or '-'} {args.get('strike') if args.get('strike') is not None else '-'}{_option_type_suffix(args.get('option_type'))}",
                f"{args.get('side') or '-'} {args.get('option_type') or '-'}",
                f"{args.get('contracts') or '-'}张",
                f"premium {args.get('premium_per_share') if args.get('premium_per_share') is not None else '-'}",
            )
            if part
        )
    if operation_type == "manual_close":
        return " ".join(
            part
            for part in (
                f"record_id {args.get('record_id')}" if args.get("record_id") else str(args.get("symbol") or "-"),
                f"close {args.get('contracts_to_close') or '-'}张",
                f"@ {args.get('close_price') if args.get('close_price') is not None else '-'}",
            )
            if part
        )
    if operation_type == "upgrade_now":
        version = _operation_version_fields(operation)
        current = version.get("current_version") or "-"
        target = version.get("target_version") or "-"
        preview = _dict(operation.get("preview"))
        upgrade = _dict(preview.get("upgrade"))
        summary = _dict(preview.get("summary"))
        result = _dict(operation.get("result"))
        status = _first_text(
            summary.get("status"),
            upgrade.get("status"),
            result.get("status"),
            operation.get("status"),
        )
        return f"{current} -> {target} status {status or '-'}"
    return operation_type or "-"


def _operation_version_fields(operation: dict[str, Any]) -> dict[str, Any]:
    operation_type = str(operation.get("operation_type") or "").strip()
    if "upgrade" not in operation_type:
        return {}
    payload = _dict(operation.get("payload"))
    args = _dict(payload.get("arguments"))
    preview = _dict(operation.get("preview"))
    preview_upgrade = _dict(preview.get("upgrade"))
    preview_summary = _dict(preview.get("summary"))
    result = _dict(operation.get("result"))
    nested_result = _dict(result.get("result"))
    current_version = _first_text(
        operation.get("current_version"),
        preview_summary.get("current_version"),
        preview_upgrade.get("current_version"),
        _nested(preview_upgrade, "version_check", "current_version"),
        result.get("current_version"),
        _nested(result, "version_check", "current_version"),
        nested_result.get("current_version"),
        _nested(nested_result, "version_check", "current_version"),
    )
    target_version = _first_text(
        operation.get("target_version"),
        args.get("target_version"),
        preview_summary.get("target_version"),
        preview_upgrade.get("target_version"),
        preview_upgrade.get("latest_version"),
        _nested(preview_upgrade, "version_check", "target_version"),
        _nested(preview_upgrade, "version_check", "latest_version"),
        result.get("target_version"),
        result.get("latest_version"),
        _nested(result, "version_check", "target_version"),
        _nested(result, "version_check", "latest_version"),
        nested_result.get("target_version"),
        nested_result.get("latest_version"),
    )
    release_tag = _first_text(
        args.get("release_tag"),
        preview_summary.get("release_tag"),
        preview_upgrade.get("release_tag"),
        _nested(preview_upgrade, "version_check", "release_tag"),
        result.get("release_tag"),
        _nested(result, "version_check", "release_tag"),
        nested_result.get("release_tag"),
    )
    return {
        "current_version": current_version,
        "target_version": target_version,
        "release_tag": release_tag,
    }


def _option_type_suffix(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    if normalized == "put":
        return "P"
    if normalized == "call":
        return "C"
    return ""


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
    if filters.get("statuses"):
        parts.append("statuses=" + ",".join(str(item) for item in filters["statuses"]))
    if filters.get("operation_id"):
        parts.append(f"operation_id={filters['operation_id']}")
    parts.append(f"limit={int(filters.get('limit') or 20)}")
    if filters.get("include_expired"):
        parts.append("include_expired=yes")
    return " ".join(parts) if parts else "all"


def _loads(value: Any) -> dict[str, Any]:
    try:
        decoded = json.loads(str(value or "{}"))
    except Exception:
        return {}
    return _dict(decoded)


def _nested(payload: Any, *keys: str) -> Any:
    cur = payload
    for key in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _string_list(values: Any) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        raw_items = values.split(",")
    elif isinstance(values, (list, tuple, set)):
        raw_items = list(values)
    else:
        raw_items = [values]
    return [text for text in (str(item).strip() for item in raw_items) if text]


def _bounded_int(value: Any, *, default: int, low: int, high: int) -> int:
    try:
        parsed = int(value)
    except Exception:
        parsed = default
    return max(low, min(parsed, high))


def _clip(value: str, limit: int) -> str:
    text = str(value or "").strip().replace("\n", " / ")
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."
