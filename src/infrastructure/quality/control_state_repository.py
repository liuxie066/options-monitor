from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.infrastructure.quality.artifact_repository import QualityArtifactRepository


class QualityControlStateRepository:
    """Persist only quality-control timing state; never broker or ledger payloads."""

    def __init__(self, path: str | Path) -> None:
        self._artifact = QualityArtifactRepository(path)

    def read(self) -> dict[str, Any]:
        payload = self._artifact.read()
        if not isinstance(payload, dict) or payload.get("schema_version") != "om.quality_control_state.v1":
            return {
                "schema_version": "om.quality_control_state.v1",
                "position_mismatches": {},
                "lifecycle_first_deep_reconcile": {},
            }
        payload.setdefault("position_mismatches", {})
        payload.setdefault("lifecycle_first_deep_reconcile", {})
        return payload

    def write(self, payload: dict[str, Any]) -> Path:
        safe = {
            "schema_version": "om.quality_control_state.v1",
            "updated_at_utc": payload.get("updated_at_utc"),
            "position_mismatches": dict(payload.get("position_mismatches") or {}),
            "lifecycle_first_deep_reconcile": dict(payload.get("lifecycle_first_deep_reconcile") or {}),
        }
        return self._artifact.write_atomic(safe)


__all__ = ["QualityControlStateRepository"]
