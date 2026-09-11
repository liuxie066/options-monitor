from __future__ import annotations

import re
from copy import deepcopy
from typing import Any, Callable

from src.application.bot.contracts import AppEvent, AppResult, new_id, safe_error_code, utc_now_iso
from src.application.research.redaction import redact_value


EventSink = Callable[[AppEvent], None]

_PUBLIC_PROGRESS = {
    "contract_received": "正在分析",
    "model_turn_started": "正在分析",
    "tool_call": "正在读取数据",
    "model_continuation_requested": "正在继续分析",
    "agent_terminated": "正在整理结论",
    "control_preview_requested": "等待确认",
    "run_cancelled": "已取消",
    "final_result": "执行完成",
}


class BotEventLog:
    def __init__(self, run_id: str, *, sink: EventSink | None = None) -> None:
        self.run_id = run_id
        self.events: list[AppEvent] = []
        self._final_recorded = False
        self._sink = sink

    def record(
        self,
        event_type: str,
        payload: dict[str, Any],
        visible_ref: str | None = None,
    ) -> None:
        if self._final_recorded:
            return
        self._append(event_type, payload, visible_ref)

    def _append(
        self,
        event_type: str,
        payload: dict[str, Any],
        visible_ref: str | None = None,
    ) -> None:
        event = AppEvent(
                event_id=new_id("evt"),
                run_id=self.run_id,
                type=event_type,
                timestamp=utc_now_iso(),
                payload=deepcopy(payload),
                visible_ref=visible_ref,
        )
        self.events.append(event)
        if self._sink is not None:
            self._sink(event)

    def record_final_result(self, result: AppResult) -> None:
        if self._final_recorded:
            return
        self._append(
            "final_result",
            {
                "status": result.status,
                "ok": result.ok,
                "request_id": result.request_id,
                "contract_id": result.contract_id,
                "error_code": str((result.error or {}).get("code") or ""),
            },
        )
        self._final_recorded = True


def public_progress_event(event: AppEvent | dict[str, Any]) -> dict[str, Any] | None:
    event_type = event.type if isinstance(event, AppEvent) else str(event.get("type") or "")
    label = _PUBLIC_PROGRESS.get(event_type)
    if not label:
        return None
    event_id = event.event_id if isinstance(event, AppEvent) else str(event.get("event_id") or "")
    timestamp = event.timestamp if isinstance(event, AppEvent) else str(event.get("timestamp") or "")
    return {"event_id": event_id, "type": event_type, "label": label, "timestamp": timestamp}


def safe_failure_cause(data: dict[str, Any], category: str) -> dict[str, Any]:
    """Keep diagnostic identifiers, never raw exception messages or result values."""
    details = data.get("details") if isinstance(data.get("details"), dict) else {}
    cause: dict[str, Any] = {"category": category}
    for key in ("tool_name", "reason", "account", "run_id"):
        value = data.get(key) or details.get(key)
        if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,119}", value):
            cause[key] = value
    code = data.get("code") or data.get("error")
    if isinstance(code, str):
        cause["code"] = safe_error_code(code, default="TOOL_ERROR")
    retryable = data.get("retryable", details.get("retryable"))
    if isinstance(retryable, bool):
        cause["retryable"] = retryable
    hint = data.get("hint") or details.get("hint")
    if isinstance(hint, str) and hint.strip():
        cause["hint"] = " ".join(str(redact_value(hint)).split())[:240]
    return redact_value(cause)


def incomplete_progress_response(response: str, progress: dict[str, Any]) -> str:
    """Present only durable read progress; unsubmitted model prose is not a conclusion."""
    checks = progress.get("completed_checks") or {}
    count = int(checks.get("read_count") or 0)
    partial = int(checks.get("partial_count") or 0)
    failed = int(checks.get("failed_count") or 0)
    reason = {
        "time_deadline": "达到本次运行时限",
        "time_reserve": "剩余时间不足以完成答案",
        "model_turn_limit": "达到本次分析轮次上限",
        "tool_call_limit": "达到本次查询次数上限",
        "tool_failure_limit": "连续工具调用失败",
        "BUDGET_EXHAUSTED": "本次运行容量已用尽",
    }.get(progress.get("termination_reason"))
    lines = [f"本次未完成：{reason}。" if reason else response]
    if count:
        labels = "、".join(" ".join(str(label).split()) for label in checks.get("sources", []))
        lines.append(f"已完成：读取 {count} 份证据" + (f"（{labels}）" if labels else "") + "；尚未形成经核实的最终结论。")
    else:
        lines.append("已完成：尚未取得可用的业务证据。")
    if partial:
        lines.append(f"证据缺口：{partial} 份读取结果覆盖不完整。")
    classified_keys = ("failed_read_count", "failed_submission_count", "failed_internal_count")
    if all(key in checks for key in classified_keys):
        failures = [f"{int(checks[key])} 次{label}" for key, label in zip(
            classified_keys, ("读取失败", "答案提交失败", "内部工具失败")
        ) if checks[key]]
        if failures:
            lines.append("未完成检查：" + "，".join(failures) + "。")
    elif failed:
        lines.append(f"未完成检查：{failed} 次工具失败（历史记录未分类）。")
    for raw in (checks.get("failure_causes") or [])[:3]:
        if not isinstance(raw, dict):
            continue
        cause = safe_failure_cause(raw, str(raw.get("category") or "internal"))
        identity = " / ".join(str(cause[key]) for key in ("tool_name", "account", "run_id") if cause.get(key))
        reason_code = cause.get("reason") or cause.get("code") or "原因未知"
        retry = ("；当前条件下不宜原样重试" if cause.get("retryable") is False
                 else "；依赖恢复后可重试" if cause.get("retryable") is True else "；重试条件未知")
        lines.append(f"原因：{identity or '工具'}：{reason_code}{retry}。" + (f"下一步：{cause['hint']}" if cause.get("hint") else ""))
    lines.append("本次未安排自动重试；继续前需确认上述条件，继续时将重新读取证据。")
    goal = " ".join(str(progress.get("goal") or "").split())[:160]
    lines.append(f"未完成：{goal or '原问题'}的最终查证与回答；继续时将重新核实当前状态。")
    lines.append(f"进度已保留，可发送「继续 {progress['progress_ref']}」。")
    return "\n".join(lines)
