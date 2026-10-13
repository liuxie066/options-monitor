"""Pure lifecycle receipt timing and its stable JSON identity."""
from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable

TIMING_POLICY_SCHEMA = "lifecycle_timing_policy.v1"
PAIRING_WINDOW_MS = 15 * 60 * 1000


def canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def derive_effective_lifecycle_timing(
    *,
    policy: dict[str, Any],
    option_close_evidence: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    timing = dict(policy or {})
    if str(timing.get("policy_schema") or "") != TIMING_POLICY_SCHEMA:
        raise ValueError("lifecycle timing policy is unavailable")
    accepted: list[int] = []
    for item in option_close_evidence:
        if (
            not isinstance(item, dict)
            or str(item.get("evidence_type") or "").strip().lower()
            != "option_zero_price_close"
        ):
            continue
        try:
            received_at_ms = int(item.get("received_at_ms") or 0)
        except (TypeError, ValueError, OverflowError):
            continue
        if received_at_ms > 0:
            accepted.append(received_at_ms)
    if not accepted:
        raise ValueError("first accepted option close receipt is unavailable")
    received_at_ms = min(accepted)
    return {
        "schema_version": "effective_lifecycle_timing.v1",
        "case_id": str(timing.get("case_id") or "").strip(),
        "last_trade_cutoff_ms": int(
            timing.get("last_trade_cutoff_ms") or 0
        ),
        "pairing_until_ms": received_at_ms + PAIRING_WINDOW_MS,
        "settlement_deadline_ms": int(
            timing.get("settlement_deadline_ms") or 0
        ),
        "settlement_style": str(
            timing.get("settlement_style") or ""
        ).strip().lower(),
        "timing_policy_hash": canonical_hash(timing),
        "first_option_close_received_at_ms": received_at_ms,
        "last_option_close_received_at_ms": max(accepted),
    }
