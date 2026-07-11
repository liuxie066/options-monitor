from __future__ import annotations

from copy import deepcopy
from typing import Any, Callable

from src.application.copilot.contracts import AppEvent, AppResult, new_id, utc_now_iso


EventSink = Callable[[AppEvent], None]


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
