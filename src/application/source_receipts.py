from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from domain.domain.decision_state_fingerprint import canonical_sha256
from src.application.payload_helpers import required_text


_required_text = required_text


# Persisted quote receipts predate the Close Advice simplification. Keep the
# wire identifier so existing sealed required-data snapshots remain readable;
# this module no longer exposes the retired Position Advice source-manifest or
# snapshot-adoption control plane.
SOURCE_RECEIPT_SCHEMA = "position_advice_source_receipt.v1"
SOURCE_MAX_AGE_SECONDS = {"quotes": 1800}
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class SourceReceiptError(RuntimeError):
    """Raised when immutable required-data quote evidence is invalid."""


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def source_snapshot_id(
    *,
    source_kind: str,
    source_native_id: str,
    source_observed_at: str,
    payload_sha256: str,
    producer_policy_hash: str,
) -> str:
    return canonical_sha256(
        {
            "source_kind": _source_kind(source_kind),
            "source_native_id": _required_text(
                source_native_id,
                "source_native_id",
            ),
            "source_observed_at": _timestamp(
                source_observed_at,
                "source_observed_at",
            ),
            "payload_sha256": _sha256(payload_sha256, "payload_sha256"),
            "producer_policy_hash": _sha256(
                producer_policy_hash,
                "producer_policy_hash",
            ),
        }
    )


def publish_source_receipt(
    *,
    producer_root: Path,
    receipt_relpath: str,
    payload_relpath: str,
    payload_bytes: bytes,
    source_kind: str,
    producer_schema_version: str,
    producer_run_id: str,
    broker: str | None,
    included_markets: Iterable[str],
    source_native_id: str,
    source_observed_at: str,
    completed_at: str,
    producer_policy_hash: str,
    before_receipt_commit: Callable[[Mapping[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Publish an immutable quote payload first and its receipt last."""

    root = _validated_root_for_write(producer_root)
    payload_path = _safe_new_relative_path(root, payload_relpath)
    receipt_path = _safe_new_relative_path(root, receipt_relpath)
    if payload_path == receipt_path:
        raise ValueError("payload and receipt paths must differ")
    _write_once_or_verify(payload_path, bytes(payload_bytes))
    payload_hash = sha256_bytes(bytes(payload_bytes))
    kind = _source_kind(source_kind)
    observed_at = _timestamp(source_observed_at, "source_observed_at")
    receipt = {
        "schema_version": SOURCE_RECEIPT_SCHEMA,
        "source_kind": kind,
        "producer_schema_version": _required_text(
            producer_schema_version,
            "producer_schema_version",
        ),
        "producer_run_id": _required_text(producer_run_id, "producer_run_id"),
        "producer_scope": "market",
        "producer_account_run_id": None,
        "broker": _optional_text(broker, "broker"),
        "account": None,
        "portfolio_account_identity_hash": None,
        "included_markets": _markets(included_markets),
        "snapshot_id": source_snapshot_id(
            source_kind=kind,
            source_native_id=source_native_id,
            source_observed_at=observed_at,
            payload_sha256=payload_hash,
            producer_policy_hash=producer_policy_hash,
        ),
        "source_native_id": _required_text(
            source_native_id,
            "source_native_id",
        ),
        "source_observed_at": observed_at,
        "completed_at": _timestamp(completed_at, "completed_at"),
        "payload_relpath": _normalized_relpath(payload_relpath),
        "payload_sha256": payload_hash,
        "producer_policy_hash": _sha256(
            producer_policy_hash,
            "producer_policy_hash",
        ),
        # Retained only as null/empty wire fields for existing receipt readers.
        "dependencies": [],
        "capacity_pool_authority_id": None,
        "completed": True,
    }
    validate_source_receipt(
        receipt,
        producer_root=root,
        now=_parse_timestamp(receipt["completed_at"]),
        require_fresh=False,
    )
    receipt_bytes = (
        json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    if before_receipt_commit is not None:
        before_receipt_commit(dict(receipt))
    _write_once_or_verify(receipt_path, receipt_bytes)
    return receipt


def validate_source_receipt(
    receipt: Mapping[str, Any],
    *,
    producer_root: Path,
    now: datetime | str,
    require_fresh: bool = True,
    expected_source_kind: str | None = None,
) -> dict[str, Any]:
    """Validate one immutable required-data quote receipt."""

    payload = dict(receipt or {})
    required_fields = {
        "schema_version",
        "source_kind",
        "producer_schema_version",
        "producer_run_id",
        "producer_scope",
        "producer_account_run_id",
        "broker",
        "account",
        "portfolio_account_identity_hash",
        "included_markets",
        "snapshot_id",
        "source_native_id",
        "source_observed_at",
        "completed_at",
        "payload_relpath",
        "payload_sha256",
        "producer_policy_hash",
        "dependencies",
        "capacity_pool_authority_id",
        "completed",
    }
    if set(payload) != required_fields:
        raise SourceReceiptError("source receipt fields do not match schema")
    if payload.get("schema_version") != SOURCE_RECEIPT_SCHEMA:
        raise SourceReceiptError("source receipt schema is invalid")
    if payload.get("completed") is not True:
        raise SourceReceiptError("source receipt is incomplete")

    kind = _source_kind(payload.get("source_kind"))
    if expected_source_kind is not None and kind != _source_kind(
        expected_source_kind
    ):
        raise SourceReceiptError("source kind mismatch")
    if payload.get("producer_scope") != "market":
        raise SourceReceiptError("quote source must be market scoped")
    if any(
        payload.get(field) is not None
        for field in (
            "producer_account_run_id",
            "account",
            "portfolio_account_identity_hash",
            "capacity_pool_authority_id",
        )
    ):
        raise SourceReceiptError("quote source contains account control-plane state")
    if payload.get("dependencies") != []:
        raise SourceReceiptError("quote source dependencies must be empty")

    producer_run_id = _required_text(
        payload.get("producer_run_id"),
        "producer_run_id",
    )
    _optional_text(payload.get("broker"), "broker")
    _markets(payload.get("included_markets"))
    _required_text(payload.get("producer_schema_version"), "producer_schema_version")

    observed_at = _timestamp(
        payload.get("source_observed_at"),
        "source_observed_at",
    )
    completed_at = _timestamp(payload.get("completed_at"), "completed_at")
    observed_dt = _parse_timestamp(observed_at)
    completed_dt = _parse_timestamp(completed_at)
    if completed_dt < observed_dt:
        raise SourceReceiptError("source completion precedes observation")
    now_dt = _parse_timestamp(now)
    if observed_dt > now_dt:
        raise SourceReceiptError("source observation is in the future")

    policy_hash = _sha256(
        payload.get("producer_policy_hash"),
        "producer_policy_hash",
    )
    payload_hash = _sha256(payload.get("payload_sha256"), "payload_sha256")
    native_id = _required_text(
        payload.get("source_native_id"),
        "source_native_id",
    )
    expected_snapshot_id = source_snapshot_id(
        source_kind=kind,
        source_native_id=native_id,
        source_observed_at=observed_at,
        payload_sha256=payload_hash,
        producer_policy_hash=policy_hash,
    )
    if payload.get("snapshot_id") != expected_snapshot_id:
        raise SourceReceiptError("source snapshot id mismatch")

    root = _validated_existing_root(producer_root)
    payload_path = safe_existing_relative_path(
        root,
        payload.get("payload_relpath"),
    )
    payload_bytes = payload_path.read_bytes()
    if sha256_bytes(payload_bytes) != payload_hash:
        raise SourceReceiptError("source payload hash mismatch")

    expires_at_dt = observed_dt + timedelta(
        seconds=SOURCE_MAX_AGE_SECONDS[kind]
    )
    expires_at = _format_timestamp(expires_at_dt)
    if require_fresh and now_dt >= expires_at_dt:
        raise SourceReceiptError("source receipt is stale")

    return {
        "receipt": payload,
        "source_kind": kind,
        "producer_run_id": producer_run_id,
        "producer_account_run_id": None,
        "snapshot_id": expected_snapshot_id,
        "payload_path": payload_path,
        "payload_bytes": payload_bytes,
        "payload_sha256": payload_hash,
        "source_observed_at": observed_at,
        "expires_at": expires_at,
        "max_age_seconds": SOURCE_MAX_AGE_SECONDS[kind],
        "dependencies": [],
        "portfolio_account_identity_hash": None,
    }


def safe_existing_relative_path(root: Path, relpath: Any) -> Path:
    root_path = _validated_existing_root(root)
    normalized = _normalized_relpath(relpath)
    current = root_path
    for part in PurePosixPath(normalized).parts:
        current = current / part
        if current.is_symlink():
            raise SourceReceiptError("source path may not contain symlinks")
    if not current.exists() or not current.is_file():
        raise SourceReceiptError("source path is missing or not a file")
    if current.stat().st_nlink != 1:
        raise SourceReceiptError("source path may not be a hardlink")
    return _assert_path_within_root(root_path, current)


def _source_kind(value: Any) -> str:
    kind = str(value or "").strip()
    if kind != "quotes":
        raise ValueError(f"unsupported source kind: {value}")
    return kind


def _optional_text(value: Any, field: str) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        raise ValueError(f"{field} must be null or non-empty")
    return text


def _sha256(value: Any, field: str) -> str:
    text = _required_text(value, field)
    if not _SHA256_RE.fullmatch(text):
        raise ValueError(f"{field} must be SHA-256")
    return text


def _markets(values: Iterable[Any] | Any) -> list[str]:
    if isinstance(values, str) or not isinstance(values, Iterable):
        raise ValueError("included_markets must be a list")
    markets = sorted({_required_text(item, "market").upper() for item in values})
    if not markets or any(item not in {"US", "HK"} for item in markets):
        raise ValueError("included_markets are invalid")
    return markets


def _timestamp(value: datetime | str | Any, field: str) -> str:
    try:
        return _format_timestamp(_parse_timestamp(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a timezone-aware timestamp") from exc


def _parse_timestamp(value: datetime | str | Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must be timezone aware")
    return parsed.astimezone(timezone.utc)


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _normalized_relpath(value: Any) -> str:
    text = str(value or "").strip()
    pure = PurePosixPath(text)
    if (
        not text
        or pure.is_absolute()
        or text != pure.as_posix()
        or any(part in {"", ".", ".."} for part in pure.parts)
    ):
        raise ValueError("path must be a canonical relative POSIX path")
    return text


def _validated_existing_root(root: Path) -> Path:
    path = Path(root)
    if not path.exists() or not path.is_dir() or path.is_symlink():
        raise SourceReceiptError("source root is missing, invalid, or a symlink")
    return path.resolve()


def _validated_root_for_write(root: Path) -> Path:
    path = Path(root)
    path.mkdir(parents=True, exist_ok=True)
    return _validated_existing_root(path)


def _assert_path_within_root(root: Path, path: Path) -> Path:
    resolved_root = root.resolve()
    resolved_path = path.resolve(strict=True)
    try:
        resolved_path.relative_to(resolved_root)
    except ValueError as exc:
        raise SourceReceiptError("source path escapes its root") from exc
    return resolved_path


def _safe_new_relative_path(root: Path, relpath: Any) -> Path:
    root_path = root.resolve()
    normalized = _normalized_relpath(relpath)
    current = root_path
    for part in PurePosixPath(normalized).parts[:-1]:
        current = current / part
        if current.exists() and current.is_symlink():
            raise SourceReceiptError("output path may not contain symlinks")
        current.mkdir(exist_ok=True)
    destination = current / PurePosixPath(normalized).name
    if destination.exists() and destination.is_symlink():
        raise SourceReceiptError("output path may not be a symlink")
    try:
        destination.parent.resolve().relative_to(root_path)
    except ValueError as exc:
        raise SourceReceiptError("output path escapes its root") from exc
    return destination


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp.{uuid.uuid4().hex}")
    try:
        temporary.write_bytes(payload)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _write_once_or_verify(path: Path, payload: bytes) -> None:
    if path.exists():
        if path.is_symlink() or path.read_bytes() != payload:
            raise SourceReceiptError("source receipt destination conflicts")
        return
    _atomic_write_bytes(path, payload)


__all__ = [
    "SourceReceiptError",
    "SOURCE_MAX_AGE_SECONDS",
    "SOURCE_RECEIPT_SCHEMA",
    "publish_source_receipt",
    "safe_existing_relative_path",
    "sha256_bytes",
    "source_snapshot_id",
    "validate_source_receipt",
]
