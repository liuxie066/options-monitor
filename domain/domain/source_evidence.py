from __future__ import annotations

import hashlib
import json
import re
from typing import Any


SOURCE_EVIDENCE_VERSION = "source_evidence.v1"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def build_source_evidence(
    *,
    source: str,
    source_id: str,
    account: str | None,
    data_type: str,
    source_record_identity: str,
    payload_version: str,
    content_digest: str,
    received_at_ms: int,
    original_time: Any = None,
    source_timezone: str | None = None,
    adapter_version: str,
) -> dict[str, Any]:
    """Build one immutable provenance record for a retained raw payload."""

    values = {
        "source": str(source or "").strip().lower(),
        "source_id": str(source_id or "").strip(),
        "data_type": str(data_type or "").strip().lower(),
        "source_record_identity": str(source_record_identity or "").strip(),
        "payload_version": str(payload_version or "").strip(),
        "adapter_version": str(adapter_version or "").strip(),
    }
    missing = [name for name, value in values.items() if not value]
    digest = str(content_digest or "").strip().lower()
    if missing:
        raise ValueError("source evidence fields missing: " + ",".join(missing))
    if _SHA256_RE.fullmatch(digest) is None:
        raise ValueError("source evidence content_digest must be SHA256")
    received = int(received_at_ms)
    if received <= 0:
        raise ValueError("source evidence received_at_ms must be positive")
    identity = {
        **values,
        "account": str(account or "").strip().lower() or None,
        "content_digest": digest,
    }
    evidence_id = "source-evidence:v1:" + hashlib.sha256(
        json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {
        "schema_version": SOURCE_EVIDENCE_VERSION,
        "evidence_id": evidence_id,
        **identity,
        "received_at_ms": received,
        "raw_payload_ref": evidence_id,
        "original_time": None if original_time is None else str(original_time),
        "source_timezone": str(source_timezone or "").strip() or None,
    }


__all__ = ["SOURCE_EVIDENCE_VERSION", "build_source_evidence"]
