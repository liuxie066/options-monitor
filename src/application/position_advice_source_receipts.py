from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping

from domain.domain.decision_state_fingerprint import canonical_sha256
SOURCE_RECEIPT_SCHEMA = "position_advice_source_receipt.v1"
SOURCE_MANIFEST_SCHEMA = "position_advice_source_manifest.v2"
FRESHNESS_POLICY_SCHEMA = "position_advice_freshness.v2"
SOURCE_SNAPSHOT_DIRECTORY = "source_snapshots"
MAX_NON_FX_OBSERVED_SKEW_SECONDS = 300

SOURCE_MAX_AGE_SECONDS = {
    "quotes": 1800,
    "candidate_decisions": 1800,
    "portfolio": 1800,
    "ledger_decision_state": 1800,
    "cash_capacity": 1800,
    "share_coverage": 1800,
    "fx": 86400,
}
SOURCE_SCOPES = frozenset({"account", "market", "global"})
ACCOUNT_SCOPED_SOURCES = frozenset(
    {
        "candidate_decisions",
        "portfolio",
        "ledger_decision_state",
        "cash_capacity",
        "share_coverage",
    }
)
DERIVED_SOURCE_REQUIRED_DEPENDENCIES = {
    "candidate_decisions": frozenset({"quotes"}),
    "cash_capacity": frozenset({"portfolio", "ledger_decision_state", "fx"}),
    "share_coverage": frozenset({"portfolio", "ledger_decision_state"}),
}
SOURCE_SKEW_KINDS = frozenset(
    {"quotes", "portfolio", "ledger_decision_state"}
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class PositionAdviceSourceError(RuntimeError):
    """Raised when an external source cannot be proven immutable and fresh."""


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
            "source_native_id": _required_text(source_native_id, "source_native_id"),
            "source_observed_at": _timestamp(source_observed_at, "source_observed_at"),
            "payload_sha256": _sha256(payload_sha256, "payload_sha256"),
            "producer_policy_hash": _sha256(producer_policy_hash, "producer_policy_hash"),
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
    producer_scope: str,
    producer_account_run_id: str | None,
    broker: str | None,
    account: str | None,
    portfolio_account_identity_hash: str | None,
    included_markets: Iterable[str],
    source_native_id: str,
    source_observed_at: str,
    completed_at: str,
    producer_policy_hash: str,
    dependencies: Iterable[Mapping[str, Any]] = (),
    capacity_pool_authority_id: str | None = None,
    before_receipt_commit: Callable[[Mapping[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Publish payload first and its immutable completion receipt last.

    A commit validator runs after the receipt has been fully validated and
    serialized but immediately before its write-once commit. If it rejects,
    the immutable payload may remain orphaned, while no completion receipt is
    published.
    """

    root = _validated_root_for_write(producer_root)
    payload_path = _safe_new_relative_path(root, payload_relpath)
    receipt_path = _safe_new_relative_path(root, receipt_relpath)
    if payload_path == receipt_path:
        raise ValueError("payload and receipt paths must differ")
    _write_once_or_verify(payload_path, bytes(payload_bytes))
    payload_hash = sha256_bytes(bytes(payload_bytes))
    observed_at = _timestamp(source_observed_at, "source_observed_at")
    receipt = {
        "schema_version": SOURCE_RECEIPT_SCHEMA,
        "source_kind": _source_kind(source_kind),
        "producer_schema_version": _required_text(
            producer_schema_version,
            "producer_schema_version",
        ),
        "producer_run_id": _required_text(producer_run_id, "producer_run_id"),
        "producer_scope": _producer_scope(producer_scope),
        "producer_account_run_id": _optional_text(
            producer_account_run_id,
            "producer_account_run_id",
        ),
        "broker": _optional_text(broker, "broker"),
        "account": _optional_text(account, "account"),
        "portfolio_account_identity_hash": _optional_sha256(
            portfolio_account_identity_hash,
            "portfolio_account_identity_hash",
        ),
        "included_markets": _markets(included_markets),
        "snapshot_id": source_snapshot_id(
            source_kind=source_kind,
            source_native_id=source_native_id,
            source_observed_at=observed_at,
            payload_sha256=payload_hash,
            producer_policy_hash=producer_policy_hash,
        ),
        "source_native_id": _required_text(source_native_id, "source_native_id"),
        "source_observed_at": observed_at,
        "completed_at": _timestamp(completed_at, "completed_at"),
        "payload_relpath": _normalized_relpath(payload_relpath),
        "payload_sha256": payload_hash,
        "producer_policy_hash": _sha256(producer_policy_hash, "producer_policy_hash"),
        "dependencies": [_normalize_dependency(item) for item in dependencies],
        "capacity_pool_authority_id": _optional_sha256(
            capacity_pool_authority_id,
            "capacity_pool_authority_id",
        ),
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
    expected_account: str | None = None,
    expected_identity_hash: str | None = None,
    expected_producer_account_run_id: str | None = None,
) -> dict[str, Any]:
    """Validate one producer receipt and return its derived freshness facts."""

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
        raise PositionAdviceSourceError("source receipt fields do not match schema")
    if payload.get("schema_version") != SOURCE_RECEIPT_SCHEMA:
        raise PositionAdviceSourceError("source receipt schema is invalid")
    if payload.get("completed") is not True:
        raise PositionAdviceSourceError("source receipt is incomplete")

    kind = _source_kind(payload.get("source_kind"))
    if expected_source_kind is not None and kind != _source_kind(expected_source_kind):
        raise PositionAdviceSourceError("source kind mismatch")
    producer_scope = _producer_scope(payload.get("producer_scope"))
    producer_run_id = _required_text(payload.get("producer_run_id"), "producer_run_id")
    producer_account_run_id = _optional_text(
        payload.get("producer_account_run_id"),
        "producer_account_run_id",
    )
    if producer_scope == "account" and producer_account_run_id is None:
        raise PositionAdviceSourceError("account-scoped source lacks producer account run id")
    if kind in ACCOUNT_SCOPED_SOURCES and producer_scope != "account":
        raise PositionAdviceSourceError("account source has invalid producer scope")
    if expected_producer_account_run_id is not None and (
        producer_account_run_id != expected_producer_account_run_id
    ):
        raise PositionAdviceSourceError("producer account run id mismatch")

    account = _optional_text(payload.get("account"), "account")
    broker = _optional_text(payload.get("broker"), "broker")
    identity_hash = _optional_sha256(
        payload.get("portfolio_account_identity_hash"),
        "portfolio_account_identity_hash",
    )
    if kind in ACCOUNT_SCOPED_SOURCES and (
        account is None or broker is None or identity_hash is None
    ):
        raise PositionAdviceSourceError(
            "account source lacks broker, account, or portfolio identity"
        )
    if expected_account is not None and account != _required_text(
        expected_account,
        "expected_account",
    ):
        raise PositionAdviceSourceError("source account mismatch")
    if expected_identity_hash is not None and (
        identity_hash != _sha256(expected_identity_hash, "expected_identity_hash")
    ):
        raise PositionAdviceSourceError("source portfolio identity mismatch")

    observed_at = _timestamp(payload.get("source_observed_at"), "source_observed_at")
    completed_at = _timestamp(payload.get("completed_at"), "completed_at")
    observed_dt = _parse_timestamp(observed_at)
    completed_dt = _parse_timestamp(completed_at)
    if completed_dt < observed_dt:
        raise PositionAdviceSourceError("source completion precedes observation")
    now_dt = _parse_timestamp(now)
    if observed_dt > now_dt:
        raise PositionAdviceSourceError("source observation is in the future")

    policy_hash = _sha256(payload.get("producer_policy_hash"), "producer_policy_hash")
    payload_hash = _sha256(payload.get("payload_sha256"), "payload_sha256")
    native_id = _required_text(payload.get("source_native_id"), "source_native_id")
    expected_snapshot_id = source_snapshot_id(
        source_kind=kind,
        source_native_id=native_id,
        source_observed_at=observed_at,
        payload_sha256=payload_hash,
        producer_policy_hash=policy_hash,
    )
    if payload.get("snapshot_id") != expected_snapshot_id:
        raise PositionAdviceSourceError("source snapshot id mismatch")

    root = _validated_existing_root(producer_root)
    payload_path = safe_existing_relative_path(root, payload.get("payload_relpath"))
    payload_bytes = payload_path.read_bytes()
    if sha256_bytes(payload_bytes) != payload_hash:
        raise PositionAdviceSourceError("source payload hash mismatch")

    dependencies = [_normalize_dependency(item) for item in _dependency_list(payload)]
    dependency_kinds = {item["source_kind"] for item in dependencies}
    required_dependency_kinds = DERIVED_SOURCE_REQUIRED_DEPENDENCIES.get(kind, frozenset())
    if not required_dependency_kinds.issubset(dependency_kinds):
        raise PositionAdviceSourceError("derived source dependencies are incomplete")
    dependency_snapshot_ids = [item["snapshot_id"] for item in dependencies]
    if len(dependency_snapshot_ids) != len(set(dependency_snapshot_ids)):
        raise PositionAdviceSourceError("source dependency snapshot ids are duplicated")

    expires_at_dt = observed_dt + timedelta(seconds=SOURCE_MAX_AGE_SECONDS[kind])
    for dependency in dependencies:
        expires_at_dt = min(expires_at_dt, _parse_timestamp(dependency["expires_at"]))
    expires_at = _format_timestamp(expires_at_dt)
    if require_fresh and now_dt >= expires_at_dt:
        raise PositionAdviceSourceError("source receipt is stale")

    if kind in {"cash_capacity"} and payload.get("capacity_pool_authority_id") is None:
        raise PositionAdviceSourceError("cash capacity authority id is unavailable")
    _markets(payload.get("included_markets"))
    _required_text(payload.get("producer_schema_version"), "producer_schema_version")
    return {
        "receipt": payload,
        "source_kind": kind,
        "producer_run_id": producer_run_id,
        "producer_account_run_id": producer_account_run_id,
        "snapshot_id": expected_snapshot_id,
        "payload_path": payload_path,
        "payload_sha256": payload_hash,
        "source_observed_at": observed_at,
        "expires_at": expires_at,
        "max_age_seconds": SOURCE_MAX_AGE_SECONDS[kind],
        "dependencies": dependencies,
        "portfolio_account_identity_hash": identity_hash,
    }


def source_dependency_from_receipt(
    *,
    receipt_path: Path,
    producer_root: Path,
    now: datetime | str,
    expected_source_kind: str | None = None,
) -> dict[str, Any]:
    root = _validated_existing_root(producer_root)
    path = _safe_existing_supplied_path(root, Path(receipt_path))
    receipt_bytes = path.read_bytes()
    try:
        receipt = json.loads(receipt_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PositionAdviceSourceError("dependency receipt is unreadable") from exc
    if not isinstance(receipt, dict):
        raise PositionAdviceSourceError("dependency receipt must be an object")
    validated = validate_source_receipt(
        receipt,
        producer_root=root,
        now=now,
        expected_source_kind=expected_source_kind,
    )
    return {
        "source_kind": validated["source_kind"],
        "snapshot_id": validated["snapshot_id"],
        "receipt_hash": sha256_bytes(receipt_bytes),
        "payload_sha256": validated["payload_sha256"],
        "expires_at": validated["expires_at"],
    }


def adopt_source_snapshot(
    *,
    receipt_path: Path,
    producer_root: Path,
    consumer_run_root: Path,
    consumer_account_run_id: str,
    now: datetime | str,
    expected_account: str | None = None,
    expected_identity_hash: str | None = None,
    max_attempts: int = 2,
) -> dict[str, Any]:
    """Copy verified source bytes into an account run without links or field repair."""

    attempts = int(max_attempts)
    if attempts < 1 or attempts > 2:
        raise ValueError("source adoption supports one attempt plus at most one retry")
    source_root = _validated_existing_root(producer_root)
    source_receipt_path = _safe_existing_supplied_path(
        source_root,
        Path(receipt_path),
    )
    consumer_root = _validated_root_for_write(consumer_run_root)
    consumer_id = _required_text(consumer_account_run_id, "consumer_account_run_id")

    last_changed = False
    for _attempt in range(attempts):
        receipt_bytes_a = source_receipt_path.read_bytes()
        try:
            receipt = json.loads(receipt_bytes_a.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PositionAdviceSourceError("source receipt is unreadable") from exc
        if not isinstance(receipt, dict):
            raise PositionAdviceSourceError("source receipt must be an object")
        validated = validate_source_receipt(
            receipt,
            producer_root=source_root,
            now=now,
            expected_account=expected_account,
            expected_identity_hash=expected_identity_hash,
            expected_producer_account_run_id=(
                consumer_id if receipt.get("producer_scope") == "account" else None
            ),
        )
        payload_bytes = validated["payload_path"].read_bytes()
        receipt_bytes_b = source_receipt_path.read_bytes()
        if receipt_bytes_a != receipt_bytes_b:
            last_changed = True
            continue
        if sha256_bytes(payload_bytes) != validated["payload_sha256"]:
            last_changed = True
            continue

        snapshot_root = (
            consumer_root / SOURCE_SNAPSHOT_DIRECTORY / str(validated["snapshot_id"])
        )
        snapshot_root.mkdir(parents=True, exist_ok=True)
        payload_destination = _safe_new_relative_path(
            snapshot_root,
            receipt["payload_relpath"],
        )
        receipt_destination = snapshot_root / "source_receipt.json"
        _write_once_or_verify(payload_destination, payload_bytes)
        _write_once_or_verify(receipt_destination, receipt_bytes_a)
        if os.path.samestat(
            validated["payload_path"].stat(),
            payload_destination.stat(),
        ):
            raise PositionAdviceSourceError("adopted payload may not be a hardlink")
        return {
            "source_kind": validated["source_kind"],
            "producer_run_id": validated["producer_run_id"],
            "consumer_account_run_id": consumer_id,
            "receipt_hash": sha256_bytes(receipt_bytes_a),
            "snapshot_id": validated["snapshot_id"],
            "payload_sha256": validated["payload_sha256"],
            "source_observed_at": validated["source_observed_at"],
            "expires_at": validated["expires_at"],
            "max_age_seconds": validated["max_age_seconds"],
            "required_for_actions": [],
            "receipt_relpath": _relative_to_root(consumer_root, receipt_destination),
            "payload_relpath": _relative_to_root(consumer_root, payload_destination),
            "dependencies": validated["dependencies"],
            "portfolio_account_identity_hash": validated[
                "portfolio_account_identity_hash"
            ],
            "capacity_pool_authority_id": receipt.get("capacity_pool_authority_id"),
        }
    if last_changed:
        raise PositionAdviceSourceError("source_changed_during_adoption")
    raise PositionAdviceSourceError("source adoption failed")


def build_source_manifest(
    *,
    account_run_id: str,
    portfolio_scope_id: str,
    portfolio_account_identity_hash: str,
    adopted_sources: Iterable[Mapping[str, Any]],
    required_for_actions: Mapping[str, Iterable[str]] | None = None,
) -> dict[str, Any]:
    consumer_id = _required_text(account_run_id, "account_run_id")
    scope_id = _sha256(portfolio_scope_id, "portfolio_scope_id")
    identity_hash = _sha256(
        portfolio_account_identity_hash,
        "portfolio_account_identity_hash",
    )
    required_map = {
        _source_kind(kind): sorted(
            {_required_text(action, "required_for_action") for action in actions}
        )
        for kind, actions in dict(required_for_actions or {}).items()
    }
    entries: list[dict[str, Any]] = []
    by_snapshot: dict[str, dict[str, Any]] = {}
    for raw in adopted_sources:
        item = dict(raw)
        kind = _source_kind(item.get("source_kind"))
        if item.get("consumer_account_run_id") != consumer_id:
            raise PositionAdviceSourceError("source belongs to another consumer account run")
        if item.get("portfolio_account_identity_hash") not in {None, identity_hash}:
            raise PositionAdviceSourceError("source manifest identity mismatch")
        normalized = {
            "source_kind": kind,
            "producer_run_id": _required_text(
                item.get("producer_run_id"),
                "producer_run_id",
            ),
            "consumer_account_run_id": consumer_id,
            "receipt_hash": _sha256(item.get("receipt_hash"), "receipt_hash"),
            "snapshot_id": _sha256(item.get("snapshot_id"), "snapshot_id"),
            "payload_sha256": _sha256(
                item.get("payload_sha256"),
                "payload_sha256",
            ),
            "source_observed_at": _timestamp(
                item.get("source_observed_at"),
                "source_observed_at",
            ),
            "expires_at": _timestamp(item.get("expires_at"), "expires_at"),
            "max_age_seconds": SOURCE_MAX_AGE_SECONDS[kind],
            "required_for_actions": required_map.get(kind, []),
            "receipt_relpath": _normalized_relpath(item.get("receipt_relpath")),
            "payload_relpath": _normalized_relpath(item.get("payload_relpath")),
            "dependencies": [_normalize_dependency(dep) for dep in item.get("dependencies", [])],
            "capacity_pool_authority_id": _optional_sha256(
                item.get("capacity_pool_authority_id"),
                "capacity_pool_authority_id",
            ),
        }
        if normalized["snapshot_id"] in by_snapshot:
            raise PositionAdviceSourceError("duplicate source snapshot in manifest")
        by_snapshot[normalized["snapshot_id"]] = normalized
        entries.append(normalized)
    if not entries:
        raise PositionAdviceSourceError("source manifest is empty")
    adopted_kinds = {entry["source_kind"] for entry in entries}
    if not set(required_map).issubset(adopted_kinds):
        raise PositionAdviceSourceError(
            "action requirement refers to a source that was not adopted"
        )

    for entry in entries:
        for dependency in entry["dependencies"]:
            matched = by_snapshot.get(dependency["snapshot_id"])
            if matched is None:
                raise PositionAdviceSourceError("source dependency is not adopted")
            if (
                matched["receipt_hash"] != dependency["receipt_hash"]
                or matched["payload_sha256"] != dependency["payload_sha256"]
                or matched["expires_at"] != dependency["expires_at"]
                or matched["source_kind"] != dependency["source_kind"]
            ):
                raise PositionAdviceSourceError("source dependency manifest mismatch")
    entries.sort(key=lambda item: (item["source_kind"], item["snapshot_id"]))
    payload = {
        "schema_version": SOURCE_MANIFEST_SCHEMA,
        "freshness_policy": FRESHNESS_POLICY_SCHEMA,
        "account_run_id": consumer_id,
        "portfolio_scope_id": scope_id,
        "portfolio_account_identity_hash": identity_hash,
        "source_manifest": entries,
        "completed": True,
    }
    payload["source_manifest_hash"] = canonical_sha256(payload)
    return payload


def validate_source_manifest(
    manifest: Mapping[str, Any],
    *,
    consumer_run_root: Path,
    now: datetime | str,
    expected_account_run_id: str,
    expected_scope_id: str,
    expected_identity_hash: str,
    require_fresh: bool = True,
) -> dict[str, Any]:
    payload = dict(manifest or {})
    manifest_hash = payload.pop("source_manifest_hash", None)
    if manifest_hash != canonical_sha256(payload):
        raise PositionAdviceSourceError("source manifest hash mismatch")
    if payload.get("schema_version") != SOURCE_MANIFEST_SCHEMA:
        raise PositionAdviceSourceError("source manifest schema is invalid")
    if payload.get("freshness_policy") != FRESHNESS_POLICY_SCHEMA:
        raise PositionAdviceSourceError("source freshness policy is invalid")
    if payload.get("completed") is not True:
        raise PositionAdviceSourceError("source manifest is incomplete")
    if payload.get("account_run_id") != expected_account_run_id:
        raise PositionAdviceSourceError("source manifest account run mismatch")
    if payload.get("portfolio_scope_id") != expected_scope_id:
        raise PositionAdviceSourceError("source manifest scope mismatch")
    if payload.get("portfolio_account_identity_hash") != expected_identity_hash:
        raise PositionAdviceSourceError("source manifest identity mismatch")
    root = _validated_existing_root(consumer_run_root)
    now_dt = _parse_timestamp(now)
    entries = payload.get("source_manifest")
    if not isinstance(entries, list) or not entries:
        raise PositionAdviceSourceError("source manifest entries are missing")
    seen: set[str] = set()
    observed_non_fx: list[datetime] = []
    validated_entries: list[dict[str, Any]] = []
    for raw in entries:
        if not isinstance(raw, dict):
            raise PositionAdviceSourceError("source manifest entry is invalid")
        item = dict(raw)
        kind = _source_kind(item.get("source_kind"))
        snapshot_id = _sha256(item.get("snapshot_id"), "snapshot_id")
        if snapshot_id in seen:
            raise PositionAdviceSourceError("source manifest snapshot is duplicated")
        seen.add(snapshot_id)
        if item.get("consumer_account_run_id") != expected_account_run_id:
            raise PositionAdviceSourceError("source manifest consumer run mismatch")
        receipt_path = safe_existing_relative_path(root, item.get("receipt_relpath"))
        payload_path = safe_existing_relative_path(root, item.get("payload_relpath"))
        expected_snapshot_root = (
            root / SOURCE_SNAPSHOT_DIRECTORY / snapshot_id
        ).resolve()
        if (
            receipt_path.parent.resolve() != expected_snapshot_root
            or receipt_path.name != "source_receipt.json"
            or expected_snapshot_root not in payload_path.parents
        ):
            raise PositionAdviceSourceError(
                "source manifest paths do not belong to the adopted snapshot"
            )
        receipt_bytes = receipt_path.read_bytes()
        if sha256_bytes(receipt_bytes) != _sha256(item.get("receipt_hash"), "receipt_hash"):
            raise PositionAdviceSourceError("adopted receipt hash mismatch")
        try:
            adopted_receipt = json.loads(receipt_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PositionAdviceSourceError("adopted receipt is unreadable") from exc
        if not isinstance(adopted_receipt, dict):
            raise PositionAdviceSourceError("adopted receipt must be an object")
        validated_receipt = validate_source_receipt(
            adopted_receipt,
            producer_root=expected_snapshot_root,
            now=now_dt,
            require_fresh=require_fresh,
            expected_source_kind=kind,
            expected_identity_hash=(
                expected_identity_hash
                if kind in ACCOUNT_SCOPED_SOURCES
                else None
            ),
            expected_producer_account_run_id=(
                expected_account_run_id
                if adopted_receipt.get("producer_scope") == "account"
                else None
            ),
        )
        if validated_receipt["payload_path"] != payload_path:
            raise PositionAdviceSourceError(
                "source manifest payload path does not match adopted receipt"
            )
        _validate_manifest_entry_against_receipt(
            item,
            validated_receipt=validated_receipt,
            adopted_receipt=adopted_receipt,
        )
        observed_at = _parse_timestamp(validated_receipt["source_observed_at"])
        if kind in SOURCE_SKEW_KINDS:
            observed_non_fx.append(observed_at)
        validated_entries.append(item)
    if observed_non_fx and (
        max(observed_non_fx) - min(observed_non_fx)
    ).total_seconds() > MAX_NON_FX_OBSERVED_SKEW_SECONDS:
        raise PositionAdviceSourceError("source observation skew exceeds policy")
    return {
        **payload,
        "source_manifest_hash": manifest_hash,
        "source_manifest": validated_entries,
    }


def _validate_manifest_entry_against_receipt(
    item: Mapping[str, Any],
    *,
    validated_receipt: Mapping[str, Any],
    adopted_receipt: Mapping[str, Any],
) -> None:
    expected_fields = {
        "source_kind",
        "producer_run_id",
        "consumer_account_run_id",
        "receipt_hash",
        "snapshot_id",
        "payload_sha256",
        "source_observed_at",
        "expires_at",
        "max_age_seconds",
        "required_for_actions",
        "receipt_relpath",
        "payload_relpath",
        "dependencies",
        "capacity_pool_authority_id",
    }
    if set(item) != expected_fields:
        raise PositionAdviceSourceError(
            "source manifest entry fields do not match schema"
        )
    required_actions = item.get("required_for_actions")
    if not isinstance(required_actions, list):
        raise PositionAdviceSourceError("required source actions must be a list")
    normalized_actions = sorted(
        {
            _required_text(action, "required_for_action")
            for action in required_actions
        }
    )
    if required_actions != normalized_actions:
        raise PositionAdviceSourceError(
            "required source actions must be sorted and unique"
        )
    expected_values = {
        "source_kind": validated_receipt["source_kind"],
        "producer_run_id": validated_receipt["producer_run_id"],
        "snapshot_id": validated_receipt["snapshot_id"],
        "payload_sha256": validated_receipt["payload_sha256"],
        "source_observed_at": validated_receipt["source_observed_at"],
        "expires_at": validated_receipt["expires_at"],
        "max_age_seconds": validated_receipt["max_age_seconds"],
        "dependencies": validated_receipt["dependencies"],
        "capacity_pool_authority_id": adopted_receipt.get(
            "capacity_pool_authority_id"
        ),
    }
    for field, expected in expected_values.items():
        if item.get(field) != expected:
            raise PositionAdviceSourceError(
                f"source manifest {field} does not match adopted receipt"
            )


def safe_existing_relative_path(root: Path, relpath: Any) -> Path:
    root_path = _validated_existing_root(root)
    normalized = _normalized_relpath(relpath)
    current = root_path
    for part in PurePosixPath(normalized).parts:
        current = current / part
        if current.is_symlink():
            raise PositionAdviceSourceError("source path may not contain symlinks")
    if not current.exists() or not current.is_file():
        raise PositionAdviceSourceError("source path is missing or not a file")
    if current.stat().st_nlink != 1:
        raise PositionAdviceSourceError("source path may not be a hardlink")
    return _assert_path_within_root(root_path, current)


def _safe_existing_supplied_path(root: Path, path: Path) -> Path:
    root_path = _validated_existing_root(root)
    absolute = Path(os.path.abspath(os.fspath(path)))
    try:
        relpath = absolute.relative_to(root_path).as_posix()
    except ValueError as exc:
        raise PositionAdviceSourceError("source path escapes its root") from exc
    return safe_existing_relative_path(root_path, relpath)


def _source_kind(value: Any) -> str:
    kind = str(value or "").strip()
    if kind not in SOURCE_MAX_AGE_SECONDS:
        raise ValueError(f"unsupported source kind: {value}")
    return kind


def _producer_scope(value: Any) -> str:
    scope = str(value or "").strip()
    if scope not in SOURCE_SCOPES:
        raise ValueError(f"unsupported producer scope: {value}")
    return scope


def _required_text(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field} is required")
    return text


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


def _optional_sha256(value: Any, field: str) -> str | None:
    if value is None:
        return None
    return _sha256(value, field)


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


def _dependency_list(receipt: Mapping[str, Any]) -> list[Any]:
    dependencies = receipt.get("dependencies")
    if not isinstance(dependencies, list):
        raise PositionAdviceSourceError("source dependencies must be a list")
    return dependencies


def _normalize_dependency(raw: Mapping[str, Any] | Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ValueError("source dependency must be an object")
    item = dict(raw)
    if set(item) != {
        "source_kind",
        "snapshot_id",
        "receipt_hash",
        "payload_sha256",
        "expires_at",
    }:
        raise ValueError("source dependency fields do not match schema")
    return {
        "source_kind": _source_kind(item.get("source_kind")),
        "snapshot_id": _sha256(item.get("snapshot_id"), "dependency snapshot_id"),
        "receipt_hash": _sha256(item.get("receipt_hash"), "dependency receipt_hash"),
        "payload_sha256": _sha256(
            item.get("payload_sha256"),
            "dependency payload_sha256",
        ),
        "expires_at": _timestamp(item.get("expires_at"), "dependency expires_at"),
    }


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
        raise PositionAdviceSourceError("source root is missing, invalid, or a symlink")
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
        raise PositionAdviceSourceError("source path escapes its root") from exc
    return resolved_path


def _safe_new_relative_path(root: Path, relpath: Any) -> Path:
    root_path = root.resolve()
    normalized = _normalized_relpath(relpath)
    current = root_path
    for part in PurePosixPath(normalized).parts[:-1]:
        current = current / part
        if current.exists() and current.is_symlink():
            raise PositionAdviceSourceError("output path may not contain symlinks")
        current.mkdir(exist_ok=True)
    destination = current / PurePosixPath(normalized).name
    if destination.exists() and destination.is_symlink():
        raise PositionAdviceSourceError("output path may not be a symlink")
    try:
        destination.parent.resolve().relative_to(root_path)
    except ValueError as exc:
        raise PositionAdviceSourceError("output path escapes its root") from exc
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
            raise PositionAdviceSourceError("adopted source destination conflicts")
        return
    _atomic_write_bytes(path, payload)


def _relative_to_root(root: Path, path: Path) -> str:
    return path.resolve(strict=True).relative_to(root.resolve()).as_posix()


__all__ = [
    "ACCOUNT_SCOPED_SOURCES",
    "DERIVED_SOURCE_REQUIRED_DEPENDENCIES",
    "FRESHNESS_POLICY_SCHEMA",
    "MAX_NON_FX_OBSERVED_SKEW_SECONDS",
    "PositionAdviceSourceError",
    "SOURCE_MANIFEST_SCHEMA",
    "SOURCE_MAX_AGE_SECONDS",
    "SOURCE_RECEIPT_SCHEMA",
    "adopt_source_snapshot",
    "build_source_manifest",
    "publish_source_receipt",
    "safe_existing_relative_path",
    "sha256_bytes",
    "source_dependency_from_receipt",
    "source_snapshot_id",
    "validate_source_manifest",
    "validate_source_receipt",
]
