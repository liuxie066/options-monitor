from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


EVIDENCE_LEDGER_SCHEMA_VERSION = "om-copilot-evidence-ledger-v1"
MAX_CELLS_PER_OBSERVATION = 160
MAX_PREVIEW_ROWS = 12


@dataclass(frozen=True)
class EvidenceObservation:
    ref_id: str
    tool_name: str
    ok: bool
    purpose: str
    row_count: int | None
    columns: tuple[str, ...]
    cells: tuple[dict[str, Any], ...]
    warnings: tuple[str, ...]
    error: dict[str, Any] | None = None

    def public_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ref_id": self.ref_id,
            "tool_name": self.tool_name,
            "ok": bool(self.ok),
            "purpose": self.purpose,
            "row_count": self.row_count,
            "columns": list(self.columns),
            "cells": list(self.cells),
            "warnings": list(self.warnings),
        }
        if self.error:
            payload["error"] = dict(self.error)
        return payload


@dataclass
class EvidenceLedger:
    observations: list[EvidenceObservation] = field(default_factory=list)
    missing_data: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)

    def add_tool_result(self, *, ref_id: str, tool_name: str, purpose: str, result: dict[str, Any]) -> EvidenceObservation:
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        cells = tuple(_extract_cells(ref_id, data))
        observation = EvidenceObservation(
            ref_id=ref_id,
            tool_name=tool_name,
            ok=bool(result.get("ok")),
            purpose=purpose,
            row_count=_row_count(data),
            columns=tuple(_columns(data)),
            cells=cells,
            warnings=tuple(str(item) for item in result.get("warnings") or [] if str(item).strip()),
            error=dict(result.get("error") or {}) if isinstance(result.get("error"), dict) else None,
        )
        self.observations.append(observation)
        return observation

    def known_refs(self) -> set[str]:
        refs = {obs.ref_id for obs in self.observations}
        for obs in self.observations:
            refs.update(str(cell.get("ref")) for cell in obs.cells if str(cell.get("ref") or "").strip())
        return refs

    def successful_evidence_count(self, *, include_catalog: bool = False) -> int:
        return sum(
            1
            for obs in self.observations
            if obs.ok and (include_catalog or obs.tool_name != "analysis_catalog")
        )

    def public_payload(self) -> dict[str, Any]:
        return {
            "schema_version": EVIDENCE_LEDGER_SCHEMA_VERSION,
            "observations": [obs.public_payload() for obs in self.observations],
            "missing_data": list(self.missing_data),
            "conflicts": list(self.conflicts),
        }

    def compact_for_model(self) -> dict[str, Any]:
        return self.public_payload()


def _row_count(data: dict[str, Any]) -> int | None:
    for key in ("row_count", "count", "view_count"):
        value = data.get(key)
        if isinstance(value, int):
            return value
    rows = _first_rows(data)
    if rows is not None:
        return len(rows)
    return None


def _columns(data: dict[str, Any]) -> list[str]:
    raw_columns = data.get("columns")
    if isinstance(raw_columns, list):
        return [str(item) for item in raw_columns if str(item).strip()]
    rows = _first_rows(data)
    if rows:
        keys: list[str] = []
        for row in rows[:MAX_PREVIEW_ROWS]:
            if isinstance(row, dict):
                for key in row:
                    if key not in keys:
                        keys.append(str(key))
        return keys
    return []


def _first_rows(data: dict[str, Any]) -> list[Any] | None:
    for key in ("rows", "items", "results", "data", "summary", "views"):
        value = data.get(key)
        if isinstance(value, list):
            return value
    return None


def _extract_cells(ref_id: str, data: dict[str, Any]) -> list[dict[str, Any]]:
    cells: list[dict[str, Any]] = []
    _walk_cells(ref_id, data, cells)
    return cells[:MAX_CELLS_PER_OBSERVATION]


def _walk_cells(path: str, value: Any, cells: list[dict[str, Any]]) -> None:
    if len(cells) >= MAX_CELLS_PER_OBSERVATION:
        return
    if isinstance(value, dict):
        for key, item in value.items():
            _walk_cells(f"{path}.{_safe_ref_part(key)}", item, cells)
            if len(cells) >= MAX_CELLS_PER_OBSERVATION:
                return
        return
    if isinstance(value, list):
        for idx, item in enumerate(value[:MAX_PREVIEW_ROWS]):
            _walk_cells(f"{path}.{idx}", item, cells)
            if len(cells) >= MAX_CELLS_PER_OBSERVATION:
                return
        return
    if value is None or isinstance(value, (str, int, float, bool)):
        cells.append({"ref": path, "value": value})


def _safe_ref_part(value: Any) -> str:
    text = str(value or "").strip()
    safe = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in text)
    return safe or "value"


__all__ = [
    "EVIDENCE_LEDGER_SCHEMA_VERSION",
    "EvidenceLedger",
    "EvidenceObservation",
]
