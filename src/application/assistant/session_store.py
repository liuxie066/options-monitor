from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from src.application.agent_tool_contracts import mask_path
from src.application.assistant.audit import connect_inbound_sqlite, default_audit_db_path, inbound_sqlite_error, utc_now_iso
from src.application.assistant.contracts import AssistantRequest


AGENT_SESSION_STORE_SCHEMA_VERSION = "om-agent-session-store-v1"
ASSISTANT_TRACE_SCHEMA_VERSION = "om-assistant-trace-v1"


class AgentSessionStore:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path).expanduser().resolve() if path else default_audit_db_path()

    def upsert_snapshot(
        self,
        *,
        snapshot: dict[str, Any],
        command_id: str | None,
        request: AssistantRequest,
        response: dict[str, Any],
    ) -> bool:
        session_id = str(snapshot.get("session_id") or "").strip()
        if not session_id:
            return False
        request_payload = snapshot.get("request") if isinstance(snapshot.get("request"), dict) else {}
        evidence = snapshot.get("evidence_bundle") if isinstance(snapshot.get("evidence_bundle"), dict) else {}
        answer_trace = snapshot.get("answer_trace") if isinstance(snapshot.get("answer_trace"), dict) else {}
        final_response = answer_trace.get("final_response") if isinstance(answer_trace.get("final_response"), dict) else {}
        plan_revisions = snapshot.get("plan_revisions") if isinstance(snapshot.get("plan_revisions"), list) else []
        tool_transcript = snapshot.get("tool_transcript") if isinstance(snapshot.get("tool_transcript"), list) else []
        response_text = _response_text(response)
        now = utc_now_iso()
        self._ensure_schema()
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO agent_sessions (
                        session_id,
                        command_id,
                        channel,
                        sender_id,
                        conversation_id,
                        message_id,
                        raw_text,
                        config_key,
                        goal,
                        task_state,
                        plan_revision_count,
                        tool_call_count,
                        fact_count,
                        dataset_count,
                        missing_data_count,
                        conflict_count,
                        response_status,
                        response_reason,
                        response_text,
                        snapshot_json,
                        created_at,
                        updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(session_id) DO UPDATE SET
                        command_id = excluded.command_id,
                        channel = excluded.channel,
                        sender_id = excluded.sender_id,
                        conversation_id = excluded.conversation_id,
                        message_id = excluded.message_id,
                        raw_text = excluded.raw_text,
                        config_key = excluded.config_key,
                        goal = excluded.goal,
                        task_state = excluded.task_state,
                        plan_revision_count = excluded.plan_revision_count,
                        tool_call_count = excluded.tool_call_count,
                        fact_count = excluded.fact_count,
                        dataset_count = excluded.dataset_count,
                        missing_data_count = excluded.missing_data_count,
                        conflict_count = excluded.conflict_count,
                        response_status = excluded.response_status,
                        response_reason = excluded.response_reason,
                        response_text = excluded.response_text,
                        snapshot_json = excluded.snapshot_json,
                        updated_at = excluded.updated_at
                    """,
                    (
                        session_id,
                        _optional_str(command_id),
                        _first_text(request_payload.get("channel"), request.channel) or "local",
                        _first_text(request_payload.get("sender_id"), request.sender_id) or "",
                        _first_text(request_payload.get("conversation_id"), request.conversation_id),
                        _first_text(request_payload.get("message_id"), request.message_id),
                        request.text,
                        _first_text(request_payload.get("config_key"), request.config_key),
                        str(snapshot.get("goal") or "").strip(),
                        str(snapshot.get("task_state") or "").strip(),
                        len(plan_revisions),
                        len(tool_transcript),
                        _safe_int(evidence.get("fact_count")),
                        _safe_int(evidence.get("dataset_count")),
                        _safe_int(evidence.get("missing_data_count")),
                        _safe_int(evidence.get("conflict_count")),
                        _optional_str(final_response.get("status")),
                        _optional_str(final_response.get("reason")),
                        response_text,
                        _json(snapshot),
                        now,
                        now,
                    ),
                )
        except sqlite3.Error as exc:
            raise inbound_sqlite_error(self.path, exc) from exc
        return True

    def list_recent(
        self,
        *,
        session_id: str | None = None,
        command_id: str | None = None,
        channel: str | None = None,
        sender_id: str | None = None,
        conversation_id: str | None = None,
        message_id: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        if not self.has_schema():
            return []
        where: list[str] = []
        params: list[Any] = []
        for column, value in (
            ("session_id", session_id),
            ("command_id", command_id),
            ("channel", str(channel or "").strip().lower() or None),
            ("sender_id", sender_id),
            ("conversation_id", conversation_id),
            ("message_id", message_id),
        ):
            text = str(value or "").strip()
            if not text:
                continue
            where.append(f"{column} = ?")
            params.append(text)
        params.append(_bounded_limit(limit))
        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    f"""
                    SELECT *
                    FROM agent_sessions
                    {where_sql}
                    ORDER BY updated_at DESC, id DESC
                    LIMIT ?
                    """,
                    tuple(params),
                ).fetchall()
        except sqlite3.Error as exc:
            raise inbound_sqlite_error(self.path, exc) from exc
        return [_session_row_to_dict(row) for row in rows]

    def has_schema(self) -> bool:
        if not self.path.exists():
            return False
        try:
            with sqlite3.connect(f"{self.path.as_uri()}?mode=ro", uri=True) as conn:
                row = conn.execute(
                    """
                    SELECT 1
                    FROM sqlite_master
                    WHERE type = 'table' AND name = 'agent_sessions'
                    LIMIT 1
                    """
                ).fetchone()
        except sqlite3.Error as exc:
            raise inbound_sqlite_error(self.path, exc) from exc
        return row is not None

    def _connect(self) -> sqlite3.Connection:
        conn = connect_inbound_sqlite(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self) -> None:
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS agent_sessions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        session_id TEXT NOT NULL UNIQUE,
                        command_id TEXT,
                        channel TEXT NOT NULL,
                        sender_id TEXT NOT NULL,
                        conversation_id TEXT,
                        message_id TEXT,
                        raw_text TEXT,
                        config_key TEXT,
                        goal TEXT,
                        task_state TEXT,
                        plan_revision_count INTEGER NOT NULL DEFAULT 0,
                        tool_call_count INTEGER NOT NULL DEFAULT 0,
                        fact_count INTEGER NOT NULL DEFAULT 0,
                        dataset_count INTEGER NOT NULL DEFAULT 0,
                        missing_data_count INTEGER NOT NULL DEFAULT 0,
                        conflict_count INTEGER NOT NULL DEFAULT 0,
                        response_status TEXT,
                        response_reason TEXT,
                        response_text TEXT,
                        snapshot_json TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_agent_sessions_command
                    ON agent_sessions(command_id)
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_agent_sessions_scope
                    ON agent_sessions(channel, sender_id, conversation_id, updated_at)
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_agent_sessions_message
                    ON agent_sessions(channel, message_id)
                    WHERE message_id IS NOT NULL AND message_id != ''
                    """
                )
        except sqlite3.Error as exc:
            raise inbound_sqlite_error(self.path, exc) from exc


def collect_assistant_trace(
    *,
    audit_db: str | None = None,
    session_id: str | None = None,
    command_id: str | None = None,
    channel: str | None = None,
    sender_id: str | None = None,
    conversation_id: str | None = None,
    message_id: str | None = None,
    limit: int = 10,
    include_snapshot: bool = False,
) -> dict[str, Any]:
    store = AgentSessionStore(audit_db)
    filters = {
        "session_id": _optional_str(session_id),
        "command_id": _optional_str(command_id),
        "channel": _optional_str(channel),
        "sender_id": _optional_str(sender_id),
        "conversation_id": _optional_str(conversation_id),
        "message_id": _optional_str(message_id),
        "limit": _bounded_limit(limit),
        "include_snapshot": bool(include_snapshot),
    }
    warnings: list[str] = []
    if not store.path.exists():
        warnings.append("audit_db_missing")
        traces: list[dict[str, Any]] = []
        audit_db_exists = False
    elif not store.has_schema():
        warnings.append("agent_session_store_missing")
        traces = []
        audit_db_exists = True
    else:
        rows = store.list_recent(
            session_id=session_id,
            command_id=command_id,
            channel=channel,
            sender_id=sender_id,
            conversation_id=conversation_id,
            message_id=message_id,
            limit=limit,
        )
        traces = [_trace_from_row(row, include_snapshot=include_snapshot) for row in rows]
        audit_db_exists = True
        if not traces:
            warnings.append("agent_session_not_found")
    return {
        "schema_version": ASSISTANT_TRACE_SCHEMA_VERSION,
        "audit_db": mask_path(store.path),
        "audit_db_exists": audit_db_exists,
        "filters": {key: value for key, value in filters.items() if value not in (None, "", [], {})},
        "trace_count": len(traces),
        "traces": traces,
        "warnings": warnings,
        "response_text": format_assistant_trace(traces, filters=filters, warnings=warnings),
    }


def format_assistant_trace(traces: list[dict[str, Any]], *, filters: dict[str, Any], warnings: list[str]) -> str:
    scope = _scope_text(filters)
    if not traces:
        return f"Assistant trace：0 条\nscope：{scope}\n没有匹配的 Agent session。"
    lines = [f"Assistant trace：{len(traces)} 条", f"scope：{scope}"]
    if warnings:
        lines.append("warnings：" + ",".join(sorted(set(warnings))))
    for trace in traces:
        identity = trace.get("identity") if isinstance(trace.get("identity"), dict) else {}
        task = trace.get("task") if isinstance(trace.get("task"), dict) else {}
        plan = trace.get("plan") if isinstance(trace.get("plan"), dict) else {}
        evidence = trace.get("evidence") if isinstance(trace.get("evidence"), dict) else {}
        answer = trace.get("answer") if isinstance(trace.get("answer"), dict) else {}
        lines.append(
            "- "
            f"{task.get('updated_at') or '-'} "
            f"{task.get('state') or '-'} "
            f"command={identity.get('command_id') or '-'}"
        )
        goal = str(task.get("goal") or "").strip()
        lines.append(f"  任务：{_clip(goal, 180) if goal else '-'}")
        lines.append(f"  能力：{_trace_capability_text(trace.get('capability_selection'))}")
        lines.append(f"  进度：{_trace_progress_text(trace.get('progress'))}")
        lines.append(f"  工具：{_trace_tools_text(trace.get('tools'))}")
        lines.append(f"  证据：{_trace_evidence_text(evidence)}")
        lines.append(f"  缺口：{_trace_gap_text(evidence)}")
        lines.append(f"  校验：{_trace_verification_text(trace)}")
        lines.append(f"  最终：{_trace_final_text(answer)}")
    return "\n".join(lines)


def _trace_tools_text(value: Any) -> str:
    tools = [item for item in value or [] if isinstance(item, dict)] if isinstance(value, list) else []
    if not tools:
        return "无工具调用"
    parts: list[str] = []
    for tool in tools[:3]:
        label = _tool_display_label(tool)
        status = _tool_status_label(tool)
        row_count = _tool_row_count(tool)
        row_suffix = f"，{row_count} 行" if row_count is not None else ""
        parts.append(f"{label}（{status}{row_suffix}）")
    if len(tools) > 3:
        parts.append(f"另 {len(tools) - 3} 个工具")
    return "；".join(parts)


def _trace_capability_text(value: Any) -> str:
    capability = value if isinstance(value, dict) else {}
    if not capability:
        return "未记录"
    selected = [item for item in capability.get("selected") or [] if isinstance(item, dict)]
    required = _string_list(capability.get("required"))
    rejected = [item for item in capability.get("rejected") or [] if isinstance(item, dict)]
    parts: list[str] = []
    if selected:
        effects = _unique(str(item.get("effect") or "read") for item in selected if str(item.get("effect") or "").strip())
        parts.append(f"selected={len(selected)}" + (f"（{','.join(effects[:3])}）" if effects else ""))
    if required:
        parts.append(f"required={len(required)}")
    if rejected:
        parts.append(f"rejected={len(rejected)}")
    return "，".join(parts) if parts else "无显式能力选择"


def _trace_progress_text(value: Any) -> str:
    progress = value if isinstance(value, dict) else {}
    if not progress:
        return "未记录"
    summary = str(progress.get("summary") or progress.get("state") or "").strip() or "未记录"
    tool_call_count = _safe_int(progress.get("tool_call_count"))
    completed = _safe_int(progress.get("completed_step_count"))
    failed = _safe_int(progress.get("failed_step_count"))
    denied = _safe_int(progress.get("denied_step_count"))
    coverage_status = str(progress.get("coverage_status") or "").strip()
    next_action = str(progress.get("next_action") or "").strip()
    blockers = [item for item in progress.get("blocked_by") or [] if isinstance(item, dict)]
    parts = [summary]
    if tool_call_count:
        parts.append(f"工具 {completed}/{tool_call_count} 完成")
    if failed:
        parts.append(f"失败 {failed}")
    if denied:
        parts.append(f"拒绝 {denied}")
    if coverage_status and coverage_status != "not_applicable":
        parts.append(f"coverage={coverage_status}")
    if blockers:
        parts.append("blocked_by=" + "、".join(_blocker_label(item) for item in blockers[:3]))
    if next_action:
        parts.append(f"next={next_action}")
    return "，".join(parts)


def _blocker_label(item: dict[str, Any]) -> str:
    kind = str(item.get("kind") or "blocker").strip()
    if kind == "evidence_gap":
        gap_kind = str(item.get("gap_kind") or "").strip()
        return f"evidence_gap:{_clip(gap_kind, 48)}" if gap_kind else "evidence_gap"
    if kind == "missing_answer_key":
        key = str(item.get("key") or "").strip()
        return f"missing:{_clip(key, 48)}" if key else "missing"
    if kind == "followup_stop":
        status = str(item.get("status") or "").strip()
        return f"followup:{_clip(status, 32)}" if status else "followup"
    if kind == "clarification":
        return "clarification"
    if kind == "permission":
        return "permission"
    if kind == "permission_denial":
        return "permission_denial"
    if kind == "tool_failure":
        return "tool_failure"
    return _clip(kind, 48)


def _tool_display_label(tool: dict[str, Any]) -> str:
    evidence = tool.get("evidence_summary") if isinstance(tool.get("evidence_summary"), dict) else {}
    renderer = str(evidence.get("canonical_renderer") or "").strip()
    labels = {
        "analysis_result": "读取分析证据",
        "assigned_stock_lifecycle": "读取指派正股持仓",
        "monthly_income": "读取收益账本",
        "position_rows": "读取期权持仓",
        "position_exit_analysis": "读取平仓建议",
        "runtime_status": "读取 runtime 状态",
        "runtime_runs": "读取运行记录",
        "runtime_logs": "读取运行日志",
        "healthcheck": "读取健康检查",
        "assistant_trace": "读取 Agent trace",
        "config_validate": "读取配置校验",
        "symbol_config": "读取标的配置",
    }
    if renderer in labels:
        return labels[renderer]
    source = str(evidence.get("source_label") or "").strip()
    if source:
        return "读取" + _clip(source, 48)
    return "读取工具证据"


def _tool_status_label(tool: dict[str, Any]) -> str:
    if tool.get("authorized") is False:
        return "denied"
    if tool.get("ok") is True:
        return "ok"
    error = str(tool.get("error_code") or "").strip()
    return f"error:{error}" if error else "unknown"


def _tool_row_count(tool: dict[str, Any]) -> int | None:
    evidence = tool.get("evidence_summary") if isinstance(tool.get("evidence_summary"), dict) else {}
    value = evidence.get("row_count")
    if value is None:
        return None
    try:
        return int(value)
    except Exception:
        return None


def _trace_evidence_text(evidence: dict[str, Any]) -> str:
    parts = [
        f"facts={_safe_int(evidence.get('fact_count'))}",
        f"diagnostics={_safe_int(evidence.get('diagnostic_count'))}",
    ]
    sources = _string_list(evidence.get("sources"))
    if sources:
        parts.append("sources=" + "、".join(_clip(item, 36) for item in sources[:3]))
    return "，".join(parts)


def _trace_gap_text(evidence: dict[str, Any]) -> str:
    parts: list[str] = []
    missing = _safe_int(evidence.get("missing_data_count"))
    conflicts = _safe_int(evidence.get("conflict_count"))
    if missing:
        parts.append(f"missing={missing}")
    if conflicts:
        parts.append(f"conflicts={conflicts}")
    domains = _string_list(evidence.get("diagnostic_domains"))
    if domains:
        parts.append("诊断域=" + "、".join(_clip(item, 32) for item in domains[:4]))
    return "无" if not parts else "，".join(parts)


def _trace_verification_text(trace: dict[str, Any]) -> str:
    hooks = _trace_hook_results(trace)
    if not hooks:
        return "无 hook 记录"
    notable = [item for item in hooks if str(item.get("status") or "") not in {"", "pass"}]
    selected = notable or [item for item in hooks if str(item.get("hook") or "") in _DISPLAY_HOOKS]
    if not selected:
        selected = hooks
    return "，".join(_hook_result_label(item) for item in selected[:6])


_DISPLAY_HOOKS = {
    "action_policy",
    "action_safety",
    "scope_guard",
    "planner_argument_guard",
    "freshness",
    "missing_data",
    "coverage",
    "receipt",
    "confirmation_guard",
    "operation_readback",
    "action_lifecycle",
    "operation_identity",
    "final_status",
    "answer_guard",
    "final_response",
}


def _trace_hook_results(trace: dict[str, Any]) -> list[dict[str, Any]]:
    hooks: list[dict[str, Any]] = []
    tools = trace.get("tools") if isinstance(trace.get("tools"), list) else []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        for item in tool.get("hook_results") or []:
            if isinstance(item, dict):
                hooks.append(item)
    answer = trace.get("answer") if isinstance(trace.get("answer"), dict) else {}
    for item in answer.get("hook_results") or []:
        if isinstance(item, dict):
            hooks.append(item)
    return hooks


def _hook_result_label(item: dict[str, Any]) -> str:
    stage = _stage_label(item.get("stage"))
    hook = str(item.get("hook") or "hook").strip()
    status = str(item.get("status") or "unknown").strip()
    code = str(item.get("code") or "").strip()
    suffix = f"/{code}" if code and code not in {status, "ok"} else ""
    return f"{stage}/{hook}={status}{suffix}"


def _stage_label(value: Any) -> str:
    text = str(value or "").strip()
    return {
        "pre_tool": "pre",
        "post_tool": "post",
        "answer": "answer",
    }.get(text, text or "hook")


def _trace_final_text(answer: dict[str, Any]) -> str:
    route = _trace_final_route(answer)
    reason = _friendly_final_reason(answer)
    return f"{route}（{reason}）" if reason else route


def _trace_final_route(answer: dict[str, Any]) -> str:
    guard = answer.get("answer_guard") if isinstance(answer.get("answer_guard"), dict) else {}
    guard_status = str(guard.get("status") or "").strip()
    response_status = str(answer.get("response_status") or "").strip()
    synthesis_reason = str(answer.get("synthesis_reason") or "").strip()
    fallback = str(answer.get("fallback") or "").strip()
    if response_status in {"needs_clarification", "clarify"}:
        return "ask"
    if response_status in {"pending_permission", "preview"}:
        return "preview"
    if guard_status == "failed_then_rewritten":
        return "rewrite->pass"
    if guard_status == "failed_then_fallback" or fallback or "fallback" in synthesis_reason:
        return "fallback"
    if response_status in {"synthesized", "rendered"}:
        return "pass"
    if response_status:
        return response_status
    return "unknown"


def _friendly_final_reason(answer: dict[str, Any]) -> str:
    guard = answer.get("answer_guard") if isinstance(answer.get("answer_guard"), dict) else {}
    guard_status = str(guard.get("status") or "").strip()
    synthesis_reason = str(answer.get("synthesis_reason") or "").strip()
    fallback = str(answer.get("fallback") or "").strip()
    response_reason = str(answer.get("response_reason") or "").strip()
    if guard_status == "failed_then_rewritten":
        return "重写后通过证据校验"
    if guard_status == "failed_then_fallback":
        return "证据校验失败后使用保底回答"
    if fallback == "task_contract" or synthesis_reason == "task_contract_fallback":
        return "使用任务形状保底"
    if fallback == "analysis_result_renderer" or synthesis_reason == "analysis_result_fallback":
        return "使用分析结果保底"
    if fallback == "canonical_renderer" or synthesis_reason == "agent_renderer_fallback":
        return "使用确定性 renderer"
    if synthesis_reason in {"agent_composed_response", "synthesized", "synthesized_after_answer_guard"}:
        return "LLM 回答通过证据校验"
    if response_reason:
        return _clip(response_reason, 80)
    return _clip(synthesis_reason, 80)


def _trace_from_row(row: dict[str, Any], *, include_snapshot: bool) -> dict[str, Any]:
    snapshot = row.get("snapshot") if isinstance(row.get("snapshot"), dict) else {}
    evidence = snapshot.get("evidence_bundle") if isinstance(snapshot.get("evidence_bundle"), dict) else {}
    answer_trace = snapshot.get("answer_trace") if isinstance(snapshot.get("answer_trace"), dict) else {}
    final_response = answer_trace.get("final_response") if isinstance(answer_trace.get("final_response"), dict) else {}
    synthesis = answer_trace.get("synthesis") if isinstance(answer_trace.get("synthesis"), dict) else {}
    permission_state = snapshot.get("permission_state") if isinstance(snapshot.get("permission_state"), dict) else {}
    trace: dict[str, Any] = {
        "schema_version": "om-assistant-trace-entry-v1",
        "identity": {
            "session_id": row.get("session_id"),
            "command_id": row.get("command_id"),
            "channel": row.get("channel"),
            "sender_id": row.get("sender_id"),
            "conversation_id": row.get("conversation_id"),
            "message_id": row.get("message_id"),
            "config_key": row.get("config_key"),
        },
        "task": {
            "goal": row.get("goal"),
            "state": row.get("task_state"),
            "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at"),
            "raw_text": row.get("raw_text"),
        },
        "plan": {
            "revision_count": int(row.get("plan_revision_count") or 0),
            "revisions": _compact_plan_revisions(snapshot.get("plan_revisions")),
        },
        "capability_selection": _compact_capability_selection(snapshot.get("capability_selection")),
        "progress": _compact_progress(snapshot.get("progress")),
        "tools": _compact_tools(snapshot.get("tool_transcript")),
        "evidence": {
            "fact_count": int(row.get("fact_count") or 0),
            "dataset_count": int(row.get("dataset_count") or 0),
            "diagnostic_count": int(evidence.get("diagnostic_count") or 0),
            "missing_data_count": int(row.get("missing_data_count") or 0),
            "conflict_count": int(row.get("conflict_count") or 0),
            "sources": list(evidence.get("sources") or []),
            "tools": list(evidence.get("tools") or []),
            "guard_profiles": list(evidence.get("guard_profiles") or []),
            "diagnostic_domains": list(evidence.get("diagnostic_domains") or []),
        },
        "answer": {
            "response_status": row.get("response_status") or final_response.get("status"),
            "response_reason": row.get("response_reason") or final_response.get("reason"),
            "synthesis_reason": synthesis.get("reason"),
            "fallback": synthesis.get("fallback"),
            "answer_guard": synthesis.get("answer_guard") if isinstance(synthesis.get("answer_guard"), dict) else None,
            "clarification_request": _compact_clarification_request(final_response.get("clarification_request")),
            "hook_results": _compact_hook_results(final_response.get("hook_results") or synthesis.get("hook_results")),
            "response_text_chars": len(str(row.get("response_text") or "")),
        },
        "permission_state": permission_state,
    }
    if include_snapshot:
        trace["snapshot"] = snapshot
    return trace


def _compact_plan_revisions(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    out: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        plan = item.get("plan") if isinstance(item.get("plan"), dict) else {}
        steps = plan.get("steps") if isinstance(plan.get("steps"), list) else []
        out.append(
            {
                "revision": item.get("revision"),
                "reason": item.get("reason"),
                "goal": plan.get("goal"),
                "selected_recipe": _compact_selected_recipe(plan.get("selected_recipe")),
                "steps": [
                    {
                        "tool_name": step.get("tool_name"),
                        "arguments": dict(step.get("arguments") or {}) if isinstance(step, dict) else {},
                        "purpose": step.get("purpose") if isinstance(step, dict) else None,
                    }
                    for step in steps
                    if isinstance(step, dict)
                ],
            }
        )
    return out


def _compact_selected_recipe(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    allowed = {
        "name",
        "match_source",
        "evidence_needs",
        "primary_views",
        "source_tools",
        "external_evidence",
        "followup_tool",
        "answer_shape",
        "reason",
    }
    return {key: value.get(key) for key in sorted(allowed) if key in value}


def _compact_capability_selection(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    allowed_top = {"schema_version", "required", "satisfied", "selected_tools"}
    out = {key: value.get(key) for key in sorted(allowed_top) if key in value}
    out["selected"] = [
        _compact_capability_item(item)
        for item in value.get("selected") or []
        if isinstance(item, dict)
    ]
    out["rejected"] = [
        _compact_capability_item(item)
        for item in value.get("rejected") or []
        if isinstance(item, dict)
    ]
    return out


def _compact_capability_item(value: dict[str, Any]) -> dict[str, Any]:
    allowed = {"capability_id", "tool_name", "revision", "effect", "reason"}
    return {key: value.get(key) for key in sorted(allowed) if key in value}


def _compact_progress(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    allowed = {
        "schema_version",
        "state",
        "summary",
        "plan_revision_count",
        "planned_step_count",
        "tool_call_count",
        "completed_step_count",
        "failed_step_count",
        "denied_step_count",
        "coverage_status",
        "coverage_next_action",
        "pending_operation_ids",
        "next_action",
    }
    out = {key: value.get(key) for key in sorted(allowed) if key in value}
    out["blocked_by"] = [
        _compact_progress_blocker(item)
        for item in value.get("blocked_by") or []
        if isinstance(item, dict)
    ]
    return out


def _compact_progress_blocker(value: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "kind",
        "tool_name",
        "next_action",
        "count",
        "code",
        "gap_kind",
        "recoverable_by",
        "suggested_tool",
        "key",
        "status",
        "reason",
    }
    return {key: value.get(key) for key in sorted(allowed) if key in value}


def _compact_clarification_request(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    questions = []
    for item in value.get("questions") or []:
        if not isinstance(item, dict):
            continue
        questions.append(
            {
                "slot": item.get("slot"),
                "question": item.get("question"),
                "options": [
                    {
                        "label": option.get("label"),
                        "description": option.get("description"),
                    }
                    for option in item.get("options") or []
                    if isinstance(option, dict)
                ],
            }
        )
    return {
        "schema_version": value.get("schema_version"),
        "status": value.get("status"),
        "questions": questions,
    }


def _compact_tools(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    out: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        out.append(
            {
                "index": item.get("index"),
                "tool_name": item.get("tool_name"),
                "payload": dict(item.get("payload") or {}) if isinstance(item.get("payload"), dict) else {},
                "authorized": bool(item.get("authorized", False)),
                "authorization_reason": item.get("authorization_reason"),
                "action_policy": dict(item.get("action_policy") or {}) if isinstance(item.get("action_policy"), dict) else {},
                "action_safety": _compact_action_safety(item.get("action_safety")),
                "precheck": _compact_tool_check(item.get("precheck")),
                "postcheck": _compact_tool_check(item.get("postcheck")),
                "hook_results": _compact_hook_results(item.get("hook_results")),
                "evidence_summary": _compact_evidence_summary(item.get("evidence_summary")),
                "action_lifecycle": _compact_action_lifecycle(item.get("action_lifecycle")),
                "ok": bool(item.get("ok", False)),
                "error_code": item.get("error_code"),
                "summary": dict(item.get("summary") or {}) if isinstance(item.get("summary"), dict) else {},
            }
        )
    return out


def _compact_action_lifecycle(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    allowed = {
        "schema_version",
        "operation_id",
        "operation_type",
        "status",
        "phase",
        "stages",
        "required_next_action",
        "verify_status",
        "audit_status",
        "source",
        "result_status",
    }
    return {key: value.get(key) for key in sorted(allowed) if key in value}


def _compact_hook_results(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    out: list[dict[str, Any]] = []
    allowed = {"schema_version", "hook", "stage", "status", "code", "impact", "recoverable", "recoverable_by"}
    for item in value:
        if not isinstance(item, dict):
            continue
        out.append({key: item.get(key) for key in sorted(allowed) if key in item})
    return out


def _compact_action_safety(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    allowed = {
        "schema_version",
        "status",
        "code",
        "requested_effect",
        "proposed_tool",
        "proposed_effect",
        "route",
        "reason",
    }
    return {key: value.get(key) for key in sorted(allowed) if key in value}


def _compact_tool_check(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {
        "schema_version": value.get("schema_version"),
        "stage": value.get("stage"),
        "status": value.get("status"),
        "tool_name": value.get("tool_name"),
        "output_contract_present": value.get("output_contract_present"),
    }


def _compact_evidence_summary(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    allowed = {
        "tool_name",
        "source_label",
        "canonical_renderer",
        "guard_profile",
        "primary_rows",
        "row_count",
        "fact_field_count",
        "missing_data_count",
    }
    return {key: value.get(key) for key in sorted(allowed) if key in value}


def _session_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    item = {key: row[key] for key in row.keys()}
    item["snapshot"] = _loads_object(item.get("snapshot_json"))
    return item


def _response_text(response: dict[str, Any]) -> str | None:
    data = response.get("data") if isinstance(response.get("data"), dict) else {}
    text = str(data.get("response_text") or "").strip()
    return text or None


def _loads_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    try:
        loaded = json.loads(str(value or "{}"))
    except Exception:
        return {}
    return dict(loaded) if isinstance(loaded, dict) else {}


def _json(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True)


def _optional_str(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _first_text(*values: Any) -> str | None:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return None


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _unique(values: Any) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def _bounded_limit(value: Any) -> int:
    try:
        return max(1, min(int(value or 10), 50))
    except Exception:
        return 10


def _scope_text(filters: dict[str, Any]) -> str:
    parts = []
    for key in ("session_id", "command_id", "channel", "sender_id", "conversation_id", "message_id"):
        value = filters.get(key)
        if value:
            parts.append(f"{key}={value}")
    parts.append(f"limit={filters.get('limit') or 10}")
    return ", ".join(parts)


def _clip(value: str, limit: int) -> str:
    text = str(value or "")
    return text if len(text) <= limit else text[: max(0, limit - 1)] + "..."


__all__ = [
    "AGENT_SESSION_STORE_SCHEMA_VERSION",
    "ASSISTANT_TRACE_SCHEMA_VERSION",
    "AgentSessionStore",
    "collect_assistant_trace",
    "format_assistant_trace",
]
