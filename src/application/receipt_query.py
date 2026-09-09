"""Bounded, read-only receipt projections shared by their business owners."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MAX_SOURCE_ROWS = 500
MAX_SOURCE_BYTES = 2 * 1024 * 1024


def read_receipt_json(path: Path) -> dict[str, Any]:
    # Read at most the ceiling even if a concurrent writer grows the file.
    with path.open("rb") as stream:
        raw = stream.read(MAX_SOURCE_BYTES + 1)
    if len(raw) > MAX_SOURCE_BYTES:
        raise ValueError("source_size_limit")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("source_invalid")
    return value


def receipt_digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def receipt_time(value: Any) -> str | None:
    if value is None or value == "":
        return None
    try:
        stamp = (datetime.fromtimestamp(value / 1000, timezone.utc) if isinstance(value, (int, float))
                 else datetime.fromisoformat(str(value).replace("Z", "+00:00")))
        if stamp.tzinfo is None:
            return None
        return stamp.astimezone(timezone.utc).isoformat()
    except (ValueError, TypeError, OverflowError):
        return None


def receipt_event(*, source: str, event_id: str, account: str, market: str | None,
                  kind: str, occurred: Any, recorded: Any = None, revision: Any = None,
                  body: str | None = None, business_result: Any = None,
                  delivery: str | None = None, confirmed: bool = False,
                  deal_id: str | None = None, run_id: str | None = None,
                  symbol: str | None = None, diagnostic_code: str | None = None,
                  related: Any = None) -> dict[str, Any]:
    provenance = "frozen" if body is not None else ("reconstructed" if business_result is not None else "unavailable")
    return {
        "event_ref": {"source": source, "source_event_id": str(event_id)},
        "source_ref": source + ":" + str(event_id),
        "account": account, "market": str(market or "").upper() or None, "type": kind,
        "occurred_at": receipt_time(occurred), "recorded_at": receipt_time(recorded or occurred),
        "revision": revision, "deal_id": deal_id, "related_run": run_id, "symbol": symbol,
        "business_result": business_result, "diagnostic_code": diagnostic_code,
        "receipt_body": body if body is not None else (json.dumps(business_result, ensure_ascii=False, sort_keys=True) if business_result is not None else None),
        "body_provenance": provenance,
        "delivery_state": "confirmed" if confirmed or delivery in {"sent", "confirmed", "delivery_confirmed"} else {
            "pending": "prepared", "prepared": "prepared", "sending": "attempted", "accepted": "attempted",
            "failed": "failed", "explicit_failed": "failed"}.get(str(delivery), "unknown"),
        "related": related,
    }


def receipt_matches(row: dict[str, Any], query: dict[str, Any]) -> bool:
    owners = row.get("accounts") or [row.get("account")]
    if query.get("account") and str(query["account"]).lower() not in {str(owner).lower() for owner in owners}:
        return False
    for key, field in (("type", "type"),
                       ("deal_id", "deal_id"), ("run_id", "related_run"), ("symbol", "symbol")):
        if query.get(key) and str(row.get(field) or "").upper() != str(query[key]).upper():
            return False
    if query.get("event_id") and query["event_id"] not in {row["source_ref"], row["event_ref"]["source_event_id"]}:
        return False
    timestamp = row.get("occurred_at")
    if query.get("start_time") and (not timestamp or timestamp < query["start_time"]):
        return False
    if query.get("end_time") and (not timestamp or timestamp > query["end_time"]):
        return False
    if query.get("market"):
        if not row.get("market"):
            raise ValueError("receipt_market_unlinkable")
        if str(row["market"]).upper() != str(query["market"]).upper():
            return False
    return True
