from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Any

from src.infrastructure.private_storage import (
    PRIVATE_FILE_MODE,
    ensure_private_directory,
    private_path,
)


CUTOVER_EVIDENCE_SCHEMA = "om.quality_hot_path_cutover_evidence.v1"
CUTOVER_RECEIPT_SCHEMA = "om.quality_hot_path_cutover_receipt.v1"
MIN_ELIGIBLE_MARKET_DAYS = 14
QUALITY_CURRENT_CONSUMERS = (
    "agent_tools.close_advice_read_impl:assert_quality_allows",
    "agent_tools.materialization:assert_quality_allows",
    "agent_tools.materialization_impl:assert_quality_allows",
    "agent_tools.positions:assert_quality_allows",
    "agent_tools.quality:read_published",
    "interfaces.quality.cli:read_published",
    "interfaces.quality.http:read_published",
)
_MARKETS = ("hk", "us")
_MAX_EVIDENCE_BYTES = 1_048_576


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _is_sha256(value: Any) -> bool:
    raw = str(value or "").strip()
    return (
        raw == raw.lower()
        and len(raw) == 64
        and all(char in "0123456789abcdef" for char in raw)
    )


def _is_zero_integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value == 0


def _read_bounded_regular(path: str | Path, *, limit: int) -> bytes:
    target = private_path(path)
    fd = os.open(
        target,
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0),
    )
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > limit:
            raise ValueError("quality cutover input is not a bounded regular file")
        chunks: list[bytes] = []
        remaining = metadata.st_size + 1
        while remaining:
            chunk = os.read(fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > limit:
            raise ValueError("quality cutover input exceeds its size limit")
        return raw
    finally:
        os.close(fd)


def quality_current_consumer_inventory_sha256() -> str:
    return _sha256(
        _canonical_bytes(
            {
                "schema_version": "om.quality_current_consumer_inventory.v1",
                "consumers": list(QUALITY_CURRENT_CONSUMERS),
            }
        )
    )


def _strict_object(raw: bytes) -> dict[str, Any]:
    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON number is not allowed: {value}")

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in items:
            if key in out:
                raise ValueError(f"duplicate JSON key: {key}")
            out[key] = value
        return out

    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=pairs,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("quality cutover evidence is not strict UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("quality cutover evidence must be an object")
    return value


def validate_quality_hot_path_cutover_evidence(payload: dict[str, Any]) -> dict[str, Any]:
    if set(payload) != {
        "schema_version",
        "eligible_market_days",
        "static_consumer_inventory_sha256",
        "deployment_access",
    }:
        raise ValueError("quality cutover evidence shape is invalid")
    if payload["schema_version"] != CUTOVER_EVIDENCE_SCHEMA:
        raise ValueError("quality cutover evidence schema is invalid")
    inventory = str(payload["static_consumer_inventory_sha256"] or "").strip().lower()
    if inventory != quality_current_consumer_inventory_sha256():
        raise ValueError("quality cutover consumer inventory does not match this binary")

    deployment = payload["deployment_access"]
    if not isinstance(deployment, dict) or set(deployment) != {
        "evidence_sha256",
        "unexplained_reader_count",
    }:
        raise ValueError("quality cutover deployment access shape is invalid")
    deployment_sha = str(deployment["evidence_sha256"] or "").strip().lower()
    if not _is_sha256(deployment_sha):
        raise ValueError("quality cutover deployment access hash is invalid")
    if not _is_zero_integer(deployment["unexplained_reader_count"]):
        raise ValueError("quality cutover deployment access has unexplained readers")

    days = payload["eligible_market_days"]
    if not isinstance(days, list):
        raise ValueError("quality cutover eligible market days must be a list")
    normalized: list[dict[str, Any]] = []
    for item in days:
        if not isinstance(item, dict) or set(item) != {
            "market",
            "market_date",
            "scheduled_open",
            "comparison_status",
            "legacy_read_count",
            "unexplained_read_count",
        }:
            raise ValueError("quality cutover market-day shape is invalid")
        market = str(item["market"] or "").strip().lower()
        if market not in _MARKETS:
            raise ValueError("quality cutover market is invalid")
        try:
            market_date = date.fromisoformat(str(item["market_date"] or ""))
        except ValueError as exc:
            raise ValueError("quality cutover market date is invalid") from exc
        if item["scheduled_open"] is not True:
            raise ValueError("quality cutover evidence must include scheduled open days")
        if item["comparison_status"] != "matched":
            raise ValueError("quality cutover comparison is not matched")
        if not _is_zero_integer(
            item["legacy_read_count"]
        ) or not _is_zero_integer(item["unexplained_read_count"]):
            raise ValueError("quality cutover evidence contains legacy or unexplained reads")
        normalized.append({**item, "market": market, "market_date": market_date.isoformat()})
    expected = sorted(normalized, key=lambda item: (item["market"], item["market_date"]))
    identities = [(item["market"], item["market_date"]) for item in normalized]
    if normalized != expected or len(identities) != len(set(identities)):
        raise ValueError("quality cutover market days must be sorted and unique")
    counts = Counter(item["market"] for item in normalized)
    if set(counts) != set(_MARKETS) or any(
        counts[market] < MIN_ELIGIBLE_MARKET_DAYS for market in _MARKETS
    ):
        raise ValueError("quality cutover requires 14 eligible days for each market")
    return {
        **payload,
        "static_consumer_inventory_sha256": inventory,
        "deployment_access": {
            "evidence_sha256": deployment_sha,
            "unexplained_reader_count": 0,
        },
        "eligible_market_days": normalized,
    }


def load_quality_hot_path_cutover_evidence(path: str | Path) -> tuple[dict[str, Any], str]:
    raw = _read_bounded_regular(path, limit=_MAX_EVIDENCE_BYTES)
    payload = validate_quality_hot_path_cutover_evidence(_strict_object(raw))
    return payload, _sha256(_canonical_bytes(payload))


def quality_hot_path_cutover_preview(path: str | Path) -> dict[str, Any]:
    payload, evidence_sha = load_quality_hot_path_cutover_evidence(path)
    counts = Counter(item["market"] for item in payload["eligible_market_days"])
    return {
        "schema_version": "om.quality_hot_path_cutover_preview.v1",
        "status": "eligible",
        "apply_required": True,
        "evidence_sha256": evidence_sha,
        "consumer_inventory_sha256": payload["static_consumer_inventory_sha256"],
        "eligible_market_day_counts": dict(sorted(counts.items())),
        "deployment_access_evidence_sha256": payload["deployment_access"]["evidence_sha256"],
    }


def activate_quality_hot_path_cutover(
    evidence_path: str | Path,
    *,
    receipt_path: str | Path,
    now: datetime | None = None,
) -> dict[str, Any]:
    preview = quality_hot_path_cutover_preview(evidence_path)
    receipt = {
        "schema_version": CUTOVER_RECEIPT_SCHEMA,
        "status": "active",
        "activated_at_utc": (now or datetime.now(timezone.utc))
        .astimezone(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        "evidence_sha256": preview["evidence_sha256"],
        "consumer_inventory_sha256": preview["consumer_inventory_sha256"],
        "eligible_market_day_counts": preview["eligible_market_day_counts"],
        "deployment_access_evidence_sha256": preview[
            "deployment_access_evidence_sha256"
        ],
    }
    target = private_path(receipt_path)
    ensure_private_directory(target.parent)
    encoded = _canonical_bytes(receipt) + b"\n"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(target, flags, 0o600)
    except FileExistsError:
        current = read_quality_hot_path_cutover_receipt(target)
        if current.get("status") == "active" and current.get("evidence_sha256") == receipt[
            "evidence_sha256"
        ]:
            return current
        raise ValueError("quality hot-path cutover receipt already exists") from None
    try:
        os.fchmod(fd, PRIVATE_FILE_MODE)
        view = memoryview(encoded)
        written = 0
        while written < len(view):
            count = os.write(fd, view[written:])
            if count <= 0:
                raise OSError("quality cutover receipt write made no progress")
            written += count
        os.fsync(fd)
    except BaseException:
        try:
            target.unlink()
        except OSError:
            pass
        raise
    finally:
        os.close(fd)
    parent_fd = os.open(
        target.parent,
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0),
    )
    try:
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)
    return receipt


def read_quality_hot_path_cutover_receipt(path: str | Path) -> dict[str, Any]:
    try:
        raw = _read_bounded_regular(path, limit=64 * 1024)
    except FileNotFoundError:
        return {"schema_version": CUTOVER_RECEIPT_SCHEMA, "status": "inactive", "reason": "receipt_missing"}
    try:
        payload = _strict_object(raw)
    except ValueError as exc:
        return {"schema_version": CUTOVER_RECEIPT_SCHEMA, "status": "inactive", "reason": str(exc)}
    required = {
        "schema_version",
        "status",
        "activated_at_utc",
        "evidence_sha256",
        "consumer_inventory_sha256",
        "eligible_market_day_counts",
        "deployment_access_evidence_sha256",
    }
    if set(payload) != required or payload.get("schema_version") != CUTOVER_RECEIPT_SCHEMA:
        return {"schema_version": CUTOVER_RECEIPT_SCHEMA, "status": "inactive", "reason": "receipt_shape_invalid"}
    if payload.get("status") != "active":
        return {"schema_version": CUTOVER_RECEIPT_SCHEMA, "status": "inactive", "reason": "receipt_not_active"}
    activated_at = str(payload.get("activated_at_utc") or "").strip()
    try:
        activated = datetime.fromisoformat(activated_at.replace("Z", "+00:00"))
    except ValueError:
        activated = None
    if (
        activated is None
        or activated.tzinfo is None
        or activated.utcoffset() != timedelta(0)
    ):
        return {
            "schema_version": CUTOVER_RECEIPT_SCHEMA,
            "status": "inactive",
            "reason": "activation_time_invalid",
        }
    if payload.get("consumer_inventory_sha256") != quality_current_consumer_inventory_sha256():
        return {"schema_version": CUTOVER_RECEIPT_SCHEMA, "status": "inactive", "reason": "consumer_inventory_changed"}
    if not _is_sha256(payload.get("evidence_sha256")) or not _is_sha256(
        payload.get("deployment_access_evidence_sha256")
    ):
        return {
            "schema_version": CUTOVER_RECEIPT_SCHEMA,
            "status": "inactive",
            "reason": "receipt_hash_invalid",
        }
    counts = payload.get("eligible_market_day_counts")
    if not isinstance(counts, dict) or set(counts) != set(_MARKETS) or any(
        not isinstance(counts.get(market), int)
        or isinstance(counts.get(market), bool)
        or counts[market] < MIN_ELIGIBLE_MARKET_DAYS
        for market in _MARKETS
    ):
        return {"schema_version": CUTOVER_RECEIPT_SCHEMA, "status": "inactive", "reason": "eligible_days_invalid"}
    return payload


__all__ = [
    "CUTOVER_EVIDENCE_SCHEMA",
    "CUTOVER_RECEIPT_SCHEMA",
    "MIN_ELIGIBLE_MARKET_DAYS",
    "QUALITY_CURRENT_CONSUMERS",
    "activate_quality_hot_path_cutover",
    "load_quality_hot_path_cutover_evidence",
    "quality_current_consumer_inventory_sha256",
    "quality_hot_path_cutover_preview",
    "read_quality_hot_path_cutover_receipt",
    "validate_quality_hot_path_cutover_evidence",
]
