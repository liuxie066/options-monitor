from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "investment.quality_status.v1"
POLICY_VERSION = "quality-policy-v1"
DATASET_STATUSES = ("trusted", "partial", "untrusted", "unavailable")


def utc_iso(value: datetime | None = None) -> str:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def sha256_json(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def opaque_id(prefix: str, value: Any) -> str:
    return f"{prefix}-{sha256_json(value)[:20]}"


def evidence_ref(*, kind: str, observed_at_utc: str, value: Any, artifact_ref: str) -> dict[str, Any]:
    return {
        "evidence_id": opaque_id("ev", {"kind": kind, "value": value}),
        "kind": kind,
        "observed_at_utc": observed_at_utc,
        "sha256": sha256_json(value),
        "artifact_ref": artifact_ref,
        "redacted": True,
    }


def check_result(
    *,
    check_id: str,
    status: str,
    scope: dict[str, Any],
    observed_at_utc: str,
    reason_code: str,
    message: str,
    evidence_refs: list[dict[str, Any]] | None = None,
    severity: str | None = None,
    observed: Any | None = None,
    expected: Any | None = None,
    thresholds: dict[str, Any] | None = None,
    extensions: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if severity is None:
        severity = "info" if status == "pass" else "warning" if status == "warn" else "blocking"
    out: dict[str, Any] = {
        "check_id": check_id,
        "status": status,
        "severity": severity,
        "scope": {key: value for key, value in scope.items() if value not in (None, "")},
        "observed_at_utc": observed_at_utc,
        "reason_code": reason_code,
        "message": message,
        "evidence_refs": list(evidence_refs or []),
    }
    if observed is not None:
        out["observed"] = observed
    if expected is not None:
        out["expected"] = expected
    if thresholds:
        out["thresholds"] = thresholds
    if extensions:
        out["extensions"] = extensions
    return out


def freshness(
    *,
    observed_at_utc: str,
    status: str = "fresh",
    age_seconds: float | None = None,
    grace_seconds: float | None = None,
    expected_by_utc: str | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "status": status,
        "observed_at_utc": observed_at_utc,
    }
    if age_seconds is not None:
        out["age_seconds"] = max(0.0, float(age_seconds))
    if grace_seconds is not None:
        out["grace_seconds"] = max(0.0, float(grace_seconds))
    if expected_by_utc:
        out["expected_by_utc"] = expected_by_utc
    return out


def dataset_status(
    *,
    dataset_id: str,
    scope: dict[str, Any],
    status: str,
    as_of_utc: str,
    checks: list[dict[str, Any]],
    evidence_refs: list[dict[str, Any]] | None = None,
    source_snapshots: list[dict[str, Any]] | None = None,
    usable_for: list[str] | None = None,
    blocked_consumers: list[str] | None = None,
    blocked_by: list[str] | None = None,
    reason_codes: list[str] | None = None,
    freshness_value: dict[str, Any] | None = None,
    extensions: dict[str, Any] | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "dataset_id": dataset_id,
        "scope": {key: value for key, value in scope.items() if value not in (None, "")},
        "status": status,
        "as_of_utc": as_of_utc,
        "required_evidence_complete": status in {"trusted", "partial"},
        "freshness": freshness_value or freshness(observed_at_utc=as_of_utc),
        "checks": checks,
        "evidence_refs": list(evidence_refs or []),
        "usable_for": sorted(set(usable_for or [])),
        "blocked_consumers": sorted(set(blocked_consumers or [])),
        "blocked_by": sorted(set(blocked_by or [])),
        "reason_codes": sorted(set(reason_codes or [])),
    }
    if source_snapshots is not None:
        out["source_snapshots"] = source_snapshots
    if extensions:
        out["extensions"] = extensions
    return out


def summarize(payload: dict[str, Any]) -> dict[str, Any]:
    datasets = [item for item in payload.get("datasets") or [] if isinstance(item, dict)]
    counts = {status: 0 for status in DATASET_STATUSES}
    blocked: set[str] = set()
    for item in datasets:
        status = str(item.get("status") or "")
        if status in counts:
            counts[status] += 1
        blocked.update(str(value) for value in item.get("blocked_consumers") or [] if str(value))
    runtime = payload.get("runtime") if isinstance(payload.get("runtime"), dict) else {}
    return {
        "runtime_status": str(runtime.get("status") or "unknown"),
        "dataset_counts": counts,
        "blocking_consumers": sorted(blocked),
        "message": (
            "OM quality evidence is trusted for all declared consumers."
            if not blocked and not counts["unavailable"] and not counts["untrusted"]
            else f"OM quality blocks {len(blocked)} declared consumer(s)."
        ),
    }


def validate_payload(payload: dict[str, Any], *, schema_path: Path) -> None:
    from jsonschema import Draft202012Validator, FormatChecker

    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(payload), key=lambda item: list(item.absolute_path))
    if errors:
        detail = "; ".join(
            f"{'.'.join(str(part) for part in error.absolute_path) or '<root>'}: {error.message}"
            for error in errors[:5]
        )
        raise ValueError(f"quality payload schema validation failed: {detail}")


__all__ = [
    "POLICY_VERSION",
    "SCHEMA_VERSION",
    "check_result",
    "dataset_status",
    "evidence_ref",
    "freshness",
    "opaque_id",
    "sha256_json",
    "summarize",
    "utc_iso",
    "validate_payload",
]
