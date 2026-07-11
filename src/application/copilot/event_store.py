from __future__ import annotations

from copy import deepcopy
from typing import Any, Callable

from src.application.copilot.contracts import AppEvent, AppResult, new_id, utc_now_iso


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


class CopilotEventLog:
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
