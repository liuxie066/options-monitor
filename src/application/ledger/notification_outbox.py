from __future__ import annotations

import hashlib
import json
from typing import Any


NOTIFICATION_INTENT_SCHEMA = "trade_lifecycle_notification_intent.v2"
STATE_FINGERPRINT_SCHEMA = "lifecycle_state_fingerprint.v1"


def canonical_payload_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        dict(payload or {}),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def canonical_state_fingerprint(payload: dict[str, Any]) -> str:
    return canonical_payload_hash(
        {
            "schema_version": STATE_FINGERPRINT_SCHEMA,
            "state": dict(payload or {}),
        }
    )


def build_notification_intent(
    *,
    case_id: str,
    transition_type: str,
    resolution_revision: int,
    delivery_revision: int = 0,
    transition_key: str,
    state_fingerprint: str,
    payload: dict[str, Any],
    status: str = "pending",
) -> dict[str, Any]:
    case_value = str(case_id or "").strip()
    transition = str(transition_type or "").strip().lower()
    revision = int(resolution_revision or 0)
    delivery = int(delivery_revision)
    transition_key_value = str(transition_key or "").strip()
    fingerprint = str(state_fingerprint or "").strip()
    status_value = str(status or "").strip().lower()
    if (
        not case_value
        or not transition
        or revision <= 0
        or delivery < 0
        or not transition_key_value
        or not fingerprint
        or status_value not in {"pending", "suppressed"}
    ):
        raise ValueError("notification intent identity is incomplete")
    frozen_payload = dict(payload or {})
    payload_hash = canonical_payload_hash(frozen_payload)
    outbox_id = "outbox_" + hashlib.sha256(
        "\x1f".join(
            (
                transition_key_value,
                str(revision),
                str(delivery),
                fingerprint,
                payload_hash,
            )
        ).encode("utf-8")
    ).hexdigest()
    return {
        "schema_version": NOTIFICATION_INTENT_SCHEMA,
        "outbox_id": outbox_id,
        "case_id": case_value,
        "transition_type": transition,
        "resolution_revision": revision,
        "delivery_revision": delivery,
        "transition_key": transition_key_value,
        "state_fingerprint": fingerprint,
        "status": status_value,
        "payload": frozen_payload,
        "payload_hash": payload_hash,
    }


__all__ = [
    "NOTIFICATION_INTENT_SCHEMA",
    "STATE_FINGERPRINT_SCHEMA",
    "build_notification_intent",
    "canonical_payload_hash",
    "canonical_state_fingerprint",
]
