"""Assessment identity, canonical digests and 24-hour evidence batches.

Evidence lives under ``<data_dir>/assessments/<assessment_id>`` and expires as
one batch.  Nothing in this module reads accounts, configuration or reports.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import uuid
from typing import Any

from .contracts import RequestContext

ASSESSMENT_SCHEMA = "group_query_assessment.v1"
BINDING_SCHEMA = "group_query_assessment_binding.v1"
SCENARIO_PLAN_SCHEMA = "group_query_scenario_plan.v1"
EVIDENCE_RETENTION_HOURS = 24


class AssessmentError(RuntimeError):
    """Evidence is missing, expired, foreign or internally inconsistent."""


def canonical_json_bytes(payload: Any) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def sha256_hex(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def conversation_scope_sha256(instance_id: str, context: RequestContext) -> str:
    if not instance_id:
        raise ValueError("instance identity is required")
    return sha256_hex(canonical_json_bytes({
        "instance_id": instance_id,
        "tenant_key": context.tenant_key,
        "chat_id": context.chat_id,
        "sender_open_id": context.sender_open_id,
        "thread_id": context.thread_id,
    }))


def input_sha256(*, operation: str, parameters: dict[str, Any],
                 contract_evidence: dict[str, Any]) -> str:
    """Hash the confirmed business input and verified contract facts.

    Request identifiers, deadlines and the confirmation timestamp are
    deliberately excluded so a retry of the same confirmed input matches.
    """
    return sha256_hex(canonical_json_bytes({
        "operation": operation,
        "parameters": parameters,
        "contract_evidence": contract_evidence,
    }))


def new_assessment_id() -> str:
    return uuid.uuid4().hex


def utc_text(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("assessment timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def parse_utc(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise AssessmentError(f"{field} is not a timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise AssessmentError(f"{field} is not a timestamp") from exc
    if parsed.tzinfo is None:
        raise AssessmentError(f"{field} lacks a timezone")
    return parsed.astimezone(timezone.utc)


def build_binding(
    *,
    principal_id: str,
    scope_sha256: str,
    input_id: str,
    input_sha256_value: str,
    input_as_of_utc: str,
    assessment_id: str,
    assessed_at_utc: datetime,
    plan: dict[str, Any],
    snapshot_manifest_sha256: str | None,
    receipts: list[dict[str, Any]],
    payloads: list[dict[str, Any]],
    policy_sha256: str,
    strategy_version: str,
    fee_model_id: str,
    fee_model_version: str,
) -> dict[str, Any]:
    payload = {
        "schema": BINDING_SCHEMA,
        "principal_id": principal_id,
        "conversation_scope_sha256": scope_sha256,
        "input_id": input_id,
        "input_sha256": input_sha256_value,
        "input_as_of_utc": input_as_of_utc,
        "assessment_id": assessment_id,
        "assessed_at_utc": utc_text(assessed_at_utc),
        "plan_schema": str(plan.get("schema") or ""),
        "plan_sha256": sha256_hex(canonical_json_bytes(plan)),
        "snapshot_manifest_sha256": snapshot_manifest_sha256,
        "receipts": [dict(item) for item in receipts],
        "payloads": [dict(item) for item in payloads],
        "public_policy_sha256": policy_sha256,
        "strategy_version": strategy_version,
        "fee_model_id": fee_model_id,
        "fee_model_version": fee_model_version,
    }
    payload["binding_sha256"] = sha256_hex(canonical_json_bytes(payload))
    return payload


def _write_atomic(path: Path, payload: dict[str, Any]) -> None:
    raw = canonical_json_bytes(payload) + b"\n"
    temporary = path.with_name(path.name + f".{uuid.uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        os.write(descriptor, raw)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def assessment_dir(data_dir: Path, assessment_id: str) -> Path:
    if not assessment_id or any(char not in "0123456789abcdef" for char in assessment_id):
        raise AssessmentError("assessment identity is invalid")
    return Path(data_dir) / "assessments" / assessment_id


def write_assessment(
    *,
    data_dir: Path,
    assessment_id: str,
    scope_sha256: str,
    created_at_utc: datetime,
    plan: dict[str, Any],
    binding: dict[str, Any],
) -> Path:
    window_start = created_at_utc.astimezone(timezone.utc)
    expires = window_start + timedelta(hours=EVIDENCE_RETENTION_HOURS)
    directory = assessment_dir(data_dir, assessment_id)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    _write_atomic(directory / "created.json", {
        "schema": ASSESSMENT_SCHEMA,
        "assessment_id": assessment_id,
        "conversation_scope_sha256": scope_sha256,
        "created_at_utc": utc_text(window_start),
        "expires_at_utc": utc_text(expires),
    })
    _write_atomic(directory / "plan.json", plan)
    _write_atomic(directory / "binding.json", binding)
    return directory


def load_assessment(*, data_dir: Path, assessment_id: str, scope_sha256: str,
                    now_utc: datetime) -> dict[str, Any]:
    directory = assessment_dir(data_dir, assessment_id)
    try:
        created = json.loads((directory / "created.json").read_text(encoding="utf-8"))
        plan = json.loads((directory / "plan.json").read_text(encoding="utf-8"))
        binding = json.loads((directory / "binding.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AssessmentError("assessment evidence is unavailable") from exc
    if created.get("schema") != ASSESSMENT_SCHEMA:
        raise AssessmentError("assessment evidence schema mismatch")
    if created.get("conversation_scope_sha256") != scope_sha256:
        raise AssessmentError("assessment evidence belongs to another scope")
    expires = parse_utc(created.get("expires_at_utc"), "expires_at_utc")
    if now_utc.astimezone(timezone.utc) >= expires:
        raise AssessmentError("assessment evidence has expired")
    if binding.get("schema") != BINDING_SCHEMA:
        raise AssessmentError("assessment binding schema mismatch")
    claimed = binding.get("binding_sha256")
    unhashed = {key: value for key, value in binding.items() if key != "binding_sha256"}
    if claimed != sha256_hex(canonical_json_bytes(unhashed)):
        raise AssessmentError("assessment binding digest mismatch")
    if binding.get("plan_sha256") != sha256_hex(canonical_json_bytes(plan)):
        raise AssessmentError("assessment plan digest mismatch")
    return {"created": created, "plan": plan, "binding": binding}


def purge_expired_assessments(data_dir: Path, now_utc: datetime) -> int:
    """Delete expired evidence batches; only this plugin's own directory."""
    root = Path(data_dir) / "assessments"
    if not root.is_dir():
        return 0
    removed = 0
    for entry in root.iterdir():
        if not entry.is_dir() or entry.is_symlink():
            continue
        try:
            created = json.loads((entry / "created.json").read_text(encoding="utf-8"))
            expires = parse_utc(created.get("expires_at_utc"), "expires_at_utc")
        except (OSError, UnicodeError, json.JSONDecodeError, AssessmentError):
            continue
        if now_utc.astimezone(timezone.utc) < expires:
            continue
        for child in sorted(entry.rglob("*"), reverse=True):
            if child.is_file() or child.is_symlink():
                child.unlink(missing_ok=True)
            elif child.is_dir():
                child.rmdir()
        entry.rmdir()
        removed += 1
    return removed


__all__ = [
    "ASSESSMENT_SCHEMA",
    "BINDING_SCHEMA",
    "EVIDENCE_RETENTION_HOURS",
    "SCENARIO_PLAN_SCHEMA",
    "AssessmentError",
    "assessment_dir",
    "build_binding",
    "canonical_json_bytes",
    "conversation_scope_sha256",
    "input_sha256",
    "load_assessment",
    "new_assessment_id",
    "parse_utc",
    "purge_expired_assessments",
    "sha256_hex",
    "utc_text",
    "write_assessment",
]
