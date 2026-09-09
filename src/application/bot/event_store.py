from __future__ import annotations

from copy import deepcopy
from typing import Any, Callable

from src.application.bot.contracts import AppEvent, AppResult, new_id, utc_now_iso


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
        "tool_failure_limit": "连续读取失败",
        "BUDGET_EXHAUSTED": "本次运行容量已用尽",
    }.get(progress.get("termination_reason"))
    lines = [f"本次未完成：{reason}。" if reason else response]
    if count:
        labels = "、".join(" ".join(str(label).split()) for label in checks.get("sources", []))
        lines.append(f"已完成：读取 {count} 份证据" + (f"（{labels}）" if labels else "") + "；尚未形成经核实的最终结论。")
    else:
        lines.append("已完成：尚未取得可用的业务证据。")
    if partial or failed:
        lines.append(f"证据缺口：{partial} 份读取结果覆盖不完整，{failed} 次读取失败。")
    goal = " ".join(str(progress.get("goal") or "").split())[:160]
    lines.append(f"未完成：{goal or '原问题'}的最终查证与回答；继续时将重新核实当前状态。")
    lines.append(f"进度已保留，可发送「继续 {progress['progress_ref']}」。")
    return "\n".join(lines)
