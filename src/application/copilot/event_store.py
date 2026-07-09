from __future__ import annotations

from copy import deepcopy
from typing import Any

from src.application.copilot.contracts import AppEvent, AppResult, new_id, utc_now_iso


MAX_FINAL_MISSING_DATA_CHARS = 160


class CopilotEventLog:
    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self.events: list[AppEvent] = []
        self._final_recorded = False

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
        self.events.append(
            AppEvent(
                event_id=new_id("evt"),
                run_id=self.run_id,
                type=event_type,
                timestamp=utc_now_iso(),
                payload=deepcopy(payload),
                visible_ref=visible_ref,
            )
        )

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
                "evidence_refs": _evidence_refs(result),
                "missing_data": _missing_data(result),
            },
        )
        self._final_recorded = True


def _evidence_refs(result: AppResult) -> list[str]:
    refs = result.answer_report.evidence_refs if result.answer_report else []
    if not isinstance(refs, list):
        return []
    return [item for item in refs if isinstance(item, str)]


def _missing_data(result: AppResult) -> list[str]:
    values = result.answer_report.missing_data if result.answer_report else []
    if not isinstance(values, list):
        return []
    missing: list[str] = []
    for item in values:
        if not isinstance(item, str):
            continue
        text = " ".join(item.split())
        if not text:
            continue
        if len(text) > MAX_FINAL_MISSING_DATA_CHARS:
            text = f"{text[: MAX_FINAL_MISSING_DATA_CHARS - 3]}..."
        missing.append(text)
    return missing
