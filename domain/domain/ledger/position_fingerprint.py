from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from typing import Any


POSITION_LOTS_FINGERPRINT_SCHEMA = "position_lots_fingerprint.v1"


def _record_parts(record: Any) -> tuple[str, Mapping[str, Any]]:
    if isinstance(record, tuple) and len(record) == 2:
        lot_id, fields = record
    elif isinstance(record, Mapping):
        lot_id = record.get("record_id")
        fields = record.get("fields")
    else:
        # A declared record object, not a persisted row: its field converged
        # onto ``lot_id``, so the object branch reads that name while the
        # Mapping branch above keeps the stored ``record_id`` key.
        lot_id = getattr(record, "lot_id", None)
        fields = getattr(record, "fields", None)

    normalized_lot_id = str(lot_id or "")
    if not normalized_lot_id or normalized_lot_id != normalized_lot_id.strip():
        raise ValueError("position lot fingerprint requires a canonical record_id")
    if not isinstance(fields, Mapping):
        raise TypeError("position lot fingerprint fields must be an object")
    return normalized_lot_id, fields


def _canonical_record_bytes(lot_id: str, fields: Mapping[str, Any]) -> bytes:
    payload = {
        "record_id": lot_id,
        "fields": dict(fields),
    }
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def ordered_position_lots_fingerprint(records: Iterable[Any]) -> str:
    """Hash records already ordered by record_id without retaining their payloads."""

    digest = hashlib.sha256()
    digest.update(b"[")
    previous_lot_id: str | None = None
    first = True
    for record in records:
        lot_id, fields = _record_parts(record)
        if previous_lot_id is not None and lot_id <= previous_lot_id:
            raise ValueError("position lot fingerprint rows must have unique ascending record_id values")
        if not first:
            digest.update(b",")
        digest.update(_canonical_record_bytes(lot_id, fields))
        first = False
        previous_lot_id = lot_id
    digest.update(b"]")
    return digest.hexdigest()


def position_lots_fingerprint(records: Iterable[Any]) -> str:
    """Hash an arbitrary record iterable using the frozen v1 row ordering."""

    normalized = [_record_parts(record) for record in records]
    normalized.sort(key=lambda item: item[0])
    return ordered_position_lots_fingerprint(normalized)


__all__ = [
    "POSITION_LOTS_FINGERPRINT_SCHEMA",
    "ordered_position_lots_fingerprint",
    "position_lots_fingerprint",
]
