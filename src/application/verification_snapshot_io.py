from __future__ import annotations

"""Load and stamp verification snapshot payloads for option-position reconciliation.

Owned by the application layer so CLI shells stay thin. The two public helpers
build deterministic snapshot ids and normalize a heterogeneous on-disk payload
into the canonical verification snapshot dict consumed by
``reconcile_option_positions_snapshot``.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

__all__ = [
    "generate_verification_snapshot_id",
    "load_verification_snapshot_payload",
]


def generate_verification_snapshot_id() -> str:
    return f"verify-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}"


def load_verification_snapshot_payload(path: str) -> dict[str, object]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(payload, dict) and payload.get("snapshot_type"):
        return payload
    if isinstance(payload, dict) and isinstance(payload.get("lots"), list):
        lots = payload.get("lots") or []
        snapshot_id = str(payload.get("snapshot_id") or generate_verification_snapshot_id()).strip()
        return {
            "snapshot_id": snapshot_id,
            "snapshot_type": "verification",
            "snapshot_at_utc": str(payload.get("snapshot_at_utc") or datetime.now().astimezone().isoformat()),
            "source_name": str(payload.get("source_name") or "cli_reconcile"),
            "source_type": str(payload.get("source_type") or "manual_verification"),
            "note": payload.get("note"),
            "lots": lots,
        }
    if isinstance(payload, list):
        return {
            "snapshot_id": generate_verification_snapshot_id(),
            "snapshot_type": "verification",
            "snapshot_at_utc": datetime.now().astimezone().isoformat(),
            "source_name": "cli_reconcile",
            "source_type": "manual_verification",
            "lots": payload,
        }
    raise ValueError("verification snapshot file must be a snapshot object or a lots array")
