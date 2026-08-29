from __future__ import annotations

import base64
import binascii
import csv
from dataclasses import dataclass
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
from typing import Any, Mapping

from domain.domain.decision_state_fingerprint import canonical_sha256
from domain.domain.fetch_source import normalize_fetch_source
from src.application.opend_symbol_outputs import (
    REQUIRED_DATA_COLUMNS,
    REQUIRED_DATA_QUOTE_SNAPSHOT_SCHEMA,
    resolve_exact_fresh_required_data_quote_receipt,
    validate_required_data_quote_bytes,
    validate_required_data_quote_candidate,
    validate_required_data_source_outcome,
)
from src.application.source_receipts import (
    SourceReceiptError,
    safe_existing_relative_path,
    sha256_bytes,
    validate_source_receipt,
)
from src.application.required_data_plan_identity import (
    required_data_plan_id,
    validate_required_data_expected_fetch_contract,
)
from src.application.required_data_blobs import (
    RequiredDataBlobError,
    fsync_required_data_bound_file,
    load_required_data_scan_blob,
    required_data_shadow_base64_matches,
    required_data_shadow_file_matches,
    retire_required_data_shadow_file,
    validate_required_data_scan_blob_ref,
)
from src.infrastructure.io_utils import atomic_write_json
from src.application.payload_helpers import required_text
from functools import partial


_required_text = partial(required_text, error=lambda m: RequiredDataSnapshotError(m))


REQUIRED_DATA_SNAPSHOT_MANIFEST_SCHEMA = "required_data_snapshot_manifest.v1"
_TERMINAL_STATUSES = frozenset({"complete", "partial", "failed"})


class RequiredDataSnapshotError(RuntimeError):
    """Raised when a run-scoped required-data snapshot cannot be sealed."""


class _RequiredDataSnapshotEntryError(RequiredDataSnapshotError):
    """Raised when manifest structure is valid but bound symbol evidence is not."""


class FrozenRequiredDataUnavailable(RuntimeError):
    """Typed fail-closed result for a frozen symbol snapshot."""

    def __init__(
        self,
        *,
        symbol: str,
        reason: str,
        detail: str | None = None,
        snapshot_id: str | None = None,
        receipt_relpath: str | None = None,
    ):
        self.symbol = str(symbol or "").strip().upper()
        self.reason = str(reason or "required_data_snapshot_unavailable").strip()
        self.detail = str(detail or "").strip()
        self.snapshot_id = str(snapshot_id or "").strip() or None
        self.receipt_relpath = str(receipt_relpath or "").strip() or None
        message = f"{self.symbol or 'UNKNOWN'}: {self.reason}"
        if self.detail:
            message += f": {self.detail}"
        super().__init__(message)


FrozenRequiredDataEntry = tuple[dict[str, Any], bytes]


@dataclass(frozen=True)
class FrozenRequiredDataBatch:
    manifest: dict[str, Any]
    manifest_bytes: bytes
    entries: dict[str, FrozenRequiredDataEntry]
    unavailable: dict[str, FrozenRequiredDataUnavailable]
    require_fresh: bool = True

    def resolve(
        self,
        symbol: str,
        *,
        now: datetime | None = None,
    ) -> FrozenRequiredDataEntry:
        symbol_norm = _required_text(symbol, "symbol").upper()
        resolved = self.entries.get(symbol_norm)
        if resolved is not None:
            if self.require_fresh:
                validated, _csv_bytes = resolved
                try:
                    _validate_resolved_entry_freshness(
                        validated=validated,
                        now=_manifest_timestamp(
                            now or datetime.now(timezone.utc),
                            "now",
                        ),
                    )
                except (RequiredDataSnapshotError, SourceReceiptError) as exc:
                    raise FrozenRequiredDataUnavailable(
                        symbol=symbol_norm,
                        reason="receipt_or_payload_mismatch",
                        detail=str(exc),
                        snapshot_id=str(validated.get("snapshot_id") or ""),
                        receipt_relpath=str(
                            validated.get("receipt_relpath") or ""
                        ),
                    ) from exc
            return resolved
        failure = self.unavailable.get(symbol_norm)
        if failure is not None:
            raise FrozenRequiredDataUnavailable(
                symbol=symbol_norm,
                reason=failure.reason,
                detail=failure.detail,
                snapshot_id=failure.snapshot_id,
                receipt_relpath=failure.receipt_relpath,
            )
        raise FrozenRequiredDataUnavailable(
            symbol=symbol_norm,
            reason="symbol_entry_missing",
        )


def seal_required_data_snapshot(
    *,
    manifest_path: Path,
    required_data_root: Path,
    run_id: str,
    prefetch_summary: Mapping[str, Any],
    close_advice_required_data_plan_path: Path | None = None,
    sealed_at: datetime | None = None,
) -> dict[str, Any]:
    """Publish the terminal run snapshot manifest as the only commit marker."""

    run_id_norm = _required_text(run_id, "run_id")
    root = _existing_directory(required_data_root, "required_data_root")
    target_input = Path(manifest_path)
    if target_input.is_symlink():
        raise RequiredDataSnapshotError(
            "required-data snapshot manifest path is a symlink"
        )
    target = target_input.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    existing_payload: dict[str, Any] | None = None
    if target.exists():
        existing_payload, _ = _load_required_data_snapshot_manifest_for_seal(
            manifest_path=target,
            expected_run_id=run_id_norm,
            expected_required_data_root=root,
        )
        seal_time = _manifest_timestamp(
            existing_payload.get("sealed_at_utc"),
            "sealed_at_utc",
        )
    else:
        seal_time = _manifest_timestamp(
            sealed_at or datetime.now(timezone.utc),
            "sealed_at",
        )
    summary = dict(prefetch_summary or {})
    plan = summary.get("global_required_data_plan")
    if not isinstance(plan, Mapping):
        raise RequiredDataSnapshotError("global required-data plan is unavailable")
    plan_payload = dict(plan)
    plan_id = str(plan_payload.get("plan_id") or "").strip()
    plan_symbols = plan_payload.get("symbols")
    if (
        not isinstance(plan_symbols, list)
        or any(not isinstance(item, Mapping) for item in plan_symbols)
    ):
        raise RequiredDataSnapshotError("global required-data plan symbols are invalid")
    normalized_plan_symbols = [dict(item) for item in plan_symbols]
    expected_plan_id = required_data_plan_id(normalized_plan_symbols)
    if plan_id != expected_plan_id:
        raise RequiredDataSnapshotError("global required-data plan id mismatch")
    validated_plan_symbols = _validate_global_plan_symbols(
        normalized_plan_symbols
    )
    result_index = _prefetch_result_index(summary)
    symbol_entries: dict[str, dict[str, Any]] = {}
    for plan_item, expected_contract in validated_plan_symbols:
        symbol = str(expected_contract["symbol"])
        try:
            evidence = resolve_exact_fresh_required_data_quote_receipt(
                runtime_root=_runtime_root_from_required_data_root(
                    root,
                    run_id_norm,
                ),
                producer_root=root,
                symbol=symbol,
                now=seal_time,
                expected_producer_run_id=run_id_norm,
                expected_fetch_contract=expected_contract,
            )
            if evidence is None:
                symbol_entries[symbol] = _failed_manifest_entry(
                    result_index.get(symbol, {}),
                    default_reason="quote_receipt_unavailable",
                )
                continue
            symbol_entries[symbol] = _ready_manifest_entry(
                root=root,
                run_id=run_id_norm,
                symbol=symbol,
                plan_item=plan_item,
                expected_fetch_contract=expected_contract,
                evidence=evidence,
                now=seal_time,
            )
        except Exception as exc:
            # The global plan was validated before this loop. Everything here
            # is symbol-scoped receipt/payload work and must fail in isolation.
            symbol_entries[symbol] = {
                "status": "failed",
                "reason": "quote_receipt_invalid",
                "error_type": type(exc).__name__,
                "detail": str(exc),
            }

    ready = sum(1 for item in symbol_entries.values() if item.get("status") == "ready")
    failed = len(symbol_entries) - ready
    if ready == len(symbol_entries) and symbol_entries:
        status = "complete"
    elif ready > 0:
        status = "partial"
    else:
        status = "failed"

    root_relpath = os.path.relpath(root, target.parent)
    payload = {
        "schema_version": REQUIRED_DATA_SNAPSHOT_MANIFEST_SCHEMA,
        "run_id": run_id_norm,
        "status": status,
        "plan_id": plan_id,
        "sealed_at_utc": seal_time.isoformat(),
        "required_data_root_relpath": Path(root_relpath).as_posix(),
        "symbols": {key: symbol_entries[key] for key in sorted(symbol_entries)},
        "summary": {
            "symbols_total": len(symbol_entries),
            "ready": ready,
            "failed": failed,
        },
    }
    if close_advice_required_data_plan_path is not None:
        plan_path_input = Path(close_advice_required_data_plan_path)
        if plan_path_input.is_symlink():
            raise RequiredDataSnapshotError(
                "close-advice required-data plan is unavailable"
            )
        plan_path = plan_path_input.resolve()
        if not plan_path.is_file():
            raise RequiredDataSnapshotError(
                "close-advice required-data plan is unavailable"
            )
        try:
            plan_relpath = plan_path.relative_to(target.parent)
        except ValueError as exc:
            raise RequiredDataSnapshotError(
                "close-advice required-data plan is outside run state"
            ) from exc
        payload.update(
            {
                "close_advice_required_data_plan_relpath": (
                    plan_relpath.as_posix()
                ),
                "close_advice_required_data_plan_sha256": sha256_bytes(
                    plan_path.read_bytes()
                ),
            }
        )
    _validate_manifest_close_advice_plan_for_seal(
        manifest_path=target,
        payload=payload,
    )
    _validate_manifest_symbols(
        payload=payload,
        root=root,
        run_id=run_id_norm,
        sealed_at=seal_time,
    )
    payload["content_sha256"] = canonical_sha256(payload)
    if existing_payload is not None:
        if existing_payload != payload:
            raise RequiredDataSnapshotError(
                "terminal required-data snapshot manifest conflicts"
            )
        return existing_payload
    if target.exists():
        adopted, _ = _load_required_data_snapshot_manifest_for_seal(
            manifest_path=target,
            expected_run_id=run_id_norm,
            expected_required_data_root=root,
        )
        if adopted != payload:
            raise RequiredDataSnapshotError(
                "terminal required-data snapshot manifest conflicts"
            )
        return adopted
    atomic_write_json(target, payload)
    written, _ = _load_required_data_snapshot_manifest_for_seal(
        manifest_path=target,
        expected_run_id=run_id_norm,
        expected_required_data_root=root,
    )
    if written != payload:
        raise RequiredDataSnapshotError(
            "terminal required-data snapshot manifest write mismatch"
        )
    return written


def retire_required_data_snapshot_shadows(
    *,
    manifest_path: Path,
    required_data_root: Path,
    manifest: Mapping[str, Any],
    manifest_bytes: bytes,
) -> dict[str, int]:
    """Best-effort retirement for compact canonical receipt shadows."""

    summary = {
        "removed_files": 0,
        "removed_bytes": 0,
        "absent_files": 0,
        "failed_files": 0,
    }
    declared_files = _declared_canonical_shadow_files(manifest)
    try:
        runtime_root, targets, durable_artifacts = (
            _required_data_shadow_cleanup_context(
                manifest_path=Path(manifest_path),
                required_data_root=Path(required_data_root),
                manifest=manifest,
                manifest_bytes=bytes(manifest_bytes),
            )
        )
    except Exception:
        summary["failed_files"] = declared_files
        return summary
    if not targets:
        return summary

    try:
        for root, relpath, expected_bytes in durable_artifacts:
            fsync_required_data_bound_file(
                root=root,
                relpath=relpath,
                expected_bytes=expected_bytes,
            )
    except Exception:
        summary["failed_files"] = 2 * len(targets)
        return summary

    for raw_relpath, csv_relpath, blob_ref in targets:
        try:
            loaded = load_required_data_scan_blob(
                runtime_root=runtime_root,
                blob_ref=blob_ref,
            )
        except RequiredDataBlobError:
            summary["failed_files"] += 2
            continue
        for relpath, expected_bytes in (
            (raw_relpath, loaded["raw_json_bytes"]),
            (csv_relpath, loaded["required_data_csv_bytes"]),
        ):
            try:
                status = retire_required_data_shadow_file(
                    root=required_data_root,
                    relpath=relpath,
                    expected_bytes=expected_bytes,
                )
            except RequiredDataBlobError:
                summary["failed_files"] += 1
                continue
            if status == "absent":
                summary["absent_files"] += 1
            else:
                summary["removed_files"] += 1
                summary["removed_bytes"] += len(expected_bytes)
    return summary


def _declared_canonical_shadow_files(manifest: Mapping[str, Any]) -> int:
    symbols = manifest.get("symbols")
    if not isinstance(symbols, Mapping):
        return 0
    return 2 * sum(
        1
        for entry in symbols.values()
        if isinstance(entry, Mapping)
        and str(entry.get("status") or "").strip().lower() == "ready"
        and entry.get("scan_blob_ref") is not None
    )


def _required_data_shadow_cleanup_context(
    *,
    manifest_path: Path,
    required_data_root: Path,
    manifest: Mapping[str, Any],
    manifest_bytes: bytes,
) -> tuple[
    Path,
    list[tuple[str, str, dict[str, Any]]],
    list[tuple[Path, str, bytes]],
]:
    payload = dict(manifest or {})
    try:
        if json.loads(manifest_bytes) != payload:
            raise RequiredDataSnapshotError(
                "required-data cleanup manifest bytes changed"
            )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RequiredDataSnapshotError(
            "required-data cleanup manifest bytes are invalid"
        ) from exc
    content = {key: value for key, value in payload.items() if key != "content_sha256"}
    if canonical_sha256(content) != str(payload.get("content_sha256") or ""):
        raise RequiredDataSnapshotError(
            "required-data cleanup manifest hash changed"
        )

    symbols = payload.get("symbols")
    if not isinstance(symbols, Mapping):
        raise RequiredDataSnapshotError(
            "required-data cleanup manifest symbols are invalid"
        )
    artifacts: list[tuple[Path, str, bytes]] = []
    targets: list[tuple[str, str, dict[str, Any]]] = []
    run_id = _required_text(payload.get("run_id"), "manifest run_id")
    runtime_root = _runtime_root_from_required_data_root(
        required_data_root,
        run_id,
    )
    for symbol_key, raw_entry in symbols.items():
        if not isinstance(raw_entry, Mapping):
            continue
        entry = dict(raw_entry)
        if str(entry.get("status") or "").strip().lower() != "ready":
            continue
        symbol = _required_text(symbol_key, "manifest symbol").upper()
        receipt_relpath = _required_text(
            entry.get("receipt_relpath"),
            "receipt_relpath",
        )
        receipt_bytes = safe_existing_relative_path(
            required_data_root,
            receipt_relpath,
        ).read_bytes()
        if sha256_bytes(receipt_bytes) != str(entry.get("receipt_hash") or ""):
            raise RequiredDataSnapshotError(
                f"{symbol} cleanup receipt hash mismatch"
            )
        receipt = json.loads(receipt_bytes)
        if not isinstance(receipt, Mapping):
            raise RequiredDataSnapshotError(
                f"{symbol} cleanup receipt is invalid"
            )
        validated = validate_source_receipt(
            receipt,
            producer_root=required_data_root,
            now=payload.get("sealed_at_utc"),
            require_fresh=False,
            expected_source_kind="quotes",
        )
        if (
            validated["producer_run_id"] != run_id
            or validated["snapshot_id"] != entry.get("snapshot_id")
            or validated["payload_sha256"] != entry.get("payload_sha256")
        ):
            raise RequiredDataSnapshotError(
                f"{symbol} cleanup receipt binding mismatch"
            )
        payload_relpath = _required_text(
            receipt.get("payload_relpath"),
            "payload_relpath",
        )
        payload_bytes = validated["payload_bytes"]
        bundle = json.loads(payload_bytes)
        if entry.get("scan_blob_ref") is None:
            continue
        if not isinstance(bundle, Mapping):
            raise RequiredDataSnapshotError(
                f"{symbol} cleanup bundle is invalid"
            )
        if "raw_json_base64" in bundle or "required_data_csv_base64" in bundle:
            continue
        expected_raw = f"raw/{symbol}_required_data.json"
        expected_csv = f"parsed/{symbol}_required_data.csv"
        if (
            entry.get("raw_json_relpath") != expected_raw
            or bundle.get("raw_json_relpath") != expected_raw
            or entry.get("required_data_csv_relpath") != expected_csv
            or bundle.get("required_data_csv_relpath") != expected_csv
        ):
            raise RequiredDataSnapshotError(
                f"{symbol} cleanup shadow path is not canonical"
            )
        bundle_ref = bundle.get("scan_blob_ref")
        if not isinstance(bundle_ref, Mapping):
            raise RequiredDataSnapshotError(
                f"{symbol} cleanup blob ref is invalid"
            )
        validated_ref = validate_required_data_scan_blob_ref(bundle_ref)
        if validated_ref != validate_required_data_scan_blob_ref(
            entry["scan_blob_ref"]
        ):
            raise RequiredDataSnapshotError(
                f"{symbol} cleanup blob ref mismatch"
            )
        artifact_prefix = Path("output_runs") / run_id / "required_data"
        artifacts.extend(
            (
                (
                    runtime_root,
                    (artifact_prefix / receipt_relpath).as_posix(),
                    receipt_bytes,
                ),
                (
                    runtime_root,
                    (artifact_prefix / payload_relpath).as_posix(),
                    payload_bytes,
                ),
            )
        )
        targets.append((expected_raw, expected_csv, validated_ref))

    plan_relpath = payload.get("close_advice_required_data_plan_relpath")
    if plan_relpath is not None:
        plan_relpath = _required_text(
            plan_relpath,
            "close_advice_required_data_plan_relpath",
        )
        plan_path = safe_existing_relative_path(
            manifest_path.parent,
            plan_relpath,
        )
        plan_bytes = plan_path.read_bytes()
        if sha256_bytes(plan_bytes) != str(
            payload.get("close_advice_required_data_plan_sha256") or ""
        ):
            raise RequiredDataSnapshotError(
                "close-advice cleanup plan hash mismatch"
            )
        try:
            durable_plan_relpath = plan_path.relative_to(runtime_root).as_posix()
        except ValueError as exc:
            raise RequiredDataSnapshotError(
                "close-advice cleanup plan is outside runtime root"
            ) from exc
        artifacts.append((runtime_root, durable_plan_relpath, plan_bytes))
    try:
        durable_manifest_relpath = (
            manifest_path.resolve().relative_to(runtime_root).as_posix()
        )
    except ValueError as exc:
        raise RequiredDataSnapshotError(
            "required-data cleanup manifest is outside runtime root"
        ) from exc
    artifacts.append((runtime_root, durable_manifest_relpath, manifest_bytes))
    return runtime_root, targets, artifacts


def load_required_data_snapshot_manifest(
    *,
    manifest_path: Path,
    expected_run_id: str,
    expected_required_data_root: Path | None = None,
) -> tuple[dict[str, Any], Path]:
    payload, root, _manifest_bytes = (
        load_required_data_snapshot_manifest_snapshot(
            manifest_path=manifest_path,
            expected_run_id=expected_run_id,
            expected_required_data_root=expected_required_data_root,
        )
    )
    return payload, root


def _load_required_data_snapshot_manifest_snapshot(
    *,
    manifest_path: Path,
    expected_run_id: str,
    expected_required_data_root: Path | None = None,
) -> tuple[
    dict[str, Any],
    Path,
    bytes,
    dict[str, FrozenRequiredDataEntry],
]:
    """Validate one exact manifest byte snapshot and return those bytes."""

    path_input = Path(manifest_path)
    if path_input.is_symlink():
        raise RequiredDataSnapshotError(
            "required-data snapshot manifest is unavailable"
        )
    path = path_input.resolve()
    if not path.is_file():
        raise RequiredDataSnapshotError(
            "required-data snapshot manifest is unreadable"
        )
    try:
        manifest_bytes = path.read_bytes()
        payload = json.loads(manifest_bytes)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RequiredDataSnapshotError("required-data snapshot manifest is unreadable") from exc
    if not isinstance(payload, dict):
        raise RequiredDataSnapshotError("required-data snapshot manifest must be an object")
    required_fields = {
        "schema_version",
        "run_id",
        "status",
        "plan_id",
        "sealed_at_utc",
        "required_data_root_relpath",
        "symbols",
        "summary",
        "content_sha256",
    }
    close_plan_fields = {
        "close_advice_required_data_plan_relpath",
        "close_advice_required_data_plan_sha256",
    }
    actual_fields = set(payload)
    if (
        actual_fields != required_fields
        and actual_fields != required_fields | close_plan_fields
    ):
        raise RequiredDataSnapshotError(
            "required-data snapshot manifest fields do not match schema"
        )
    if payload.get("schema_version") != REQUIRED_DATA_SNAPSHOT_MANIFEST_SCHEMA:
        raise RequiredDataSnapshotError("required-data snapshot manifest schema mismatch")
    run_id = _required_text(payload.get("run_id"), "manifest run_id")
    if run_id != _required_text(expected_run_id, "expected_run_id"):
        raise RequiredDataSnapshotError("required-data snapshot manifest run mismatch")
    if path.parent.name != "state" or path.parent.parent.name != run_id:
        raise RequiredDataSnapshotError("required-data snapshot manifest path is outside the current run")
    status = str(payload.get("status") or "").strip().lower()
    if status not in _TERMINAL_STATUSES:
        raise RequiredDataSnapshotError("required-data snapshot manifest is not terminal")
    if not _is_sha256(payload.get("plan_id")):
        raise RequiredDataSnapshotError("required-data snapshot plan id is invalid")
    content_sha256 = payload.get("content_sha256")
    if not _is_sha256(content_sha256):
        raise RequiredDataSnapshotError("required-data snapshot content hash is invalid")
    content = {
        key: value
        for key, value in payload.items()
        if key != "content_sha256"
    }
    if canonical_sha256(content) != content_sha256:
        raise RequiredDataSnapshotError("required-data snapshot content hash mismatch")
    sealed_at = _manifest_timestamp(
        payload.get("sealed_at_utc"),
        "sealed_at_utc",
    )
    root_relpath = _required_text(
        payload.get("required_data_root_relpath"),
        "required_data_root_relpath",
    )
    root = (path.parent / root_relpath).resolve()
    root = _existing_directory(root, "manifest required_data_root")
    if root.parent != path.parent.parent:
        raise RequiredDataSnapshotError(
            "required-data snapshot root is outside the current run"
        )
    if (
        expected_required_data_root is not None
        and root != Path(expected_required_data_root).resolve()
    ):
        raise RequiredDataSnapshotError("required-data snapshot root mismatch")
    resolved_entries = _validate_manifest_symbols(
        payload=payload,
        root=root,
        run_id=run_id,
        sealed_at=sealed_at,
    )
    return payload, root, manifest_bytes, resolved_entries


def load_required_data_snapshot_manifest_snapshot(
    *,
    manifest_path: Path,
    expected_run_id: str,
    expected_required_data_root: Path | None = None,
) -> tuple[dict[str, Any], Path, bytes]:
    """Validate one exact manifest byte snapshot and return those bytes."""

    payload, root, manifest_bytes, _resolved_entries = (
        _load_required_data_snapshot_manifest_snapshot(
            manifest_path=manifest_path,
            expected_run_id=expected_run_id,
            expected_required_data_root=expected_required_data_root,
        )
    )
    return payload, root, manifest_bytes


def _validate_manifest_close_advice_plan(
    *,
    manifest_path: Path,
    payload: Mapping[str, Any],
) -> None:
    relpath = payload.get("close_advice_required_data_plan_relpath")
    expected_hash = payload.get("close_advice_required_data_plan_sha256")
    if relpath is None and expected_hash is None:
        return
    if relpath is None or not _is_sha256(expected_hash):
        raise RequiredDataSnapshotError(
            "close-advice required-data plan authority is invalid"
        )
    try:
        plan_path = safe_existing_relative_path(
            manifest_path.parent,
            _required_text(
                relpath,
                "close_advice_required_data_plan_relpath",
            ),
        )
        actual_hash = sha256_bytes(plan_path.read_bytes())
    except (OSError, SourceReceiptError, ValueError) as exc:
        raise RequiredDataSnapshotError(
            "close-advice required-data plan authority is invalid"
        ) from exc
    if actual_hash != str(expected_hash):
        raise RequiredDataSnapshotError(
            "close-advice required-data plan hash mismatch"
        )


def _validate_manifest_close_advice_plan_for_seal(
    *,
    manifest_path: Path,
    payload: Mapping[str, Any],
) -> None:
    _validate_manifest_close_advice_plan(
        manifest_path=manifest_path,
        payload=payload,
    )


def _load_required_data_snapshot_manifest_for_seal(
    *,
    manifest_path: Path,
    expected_run_id: str,
    expected_required_data_root: Path | None,
) -> tuple[dict[str, Any], Path]:
    payload, root = load_required_data_snapshot_manifest(
        manifest_path=manifest_path,
        expected_run_id=expected_run_id,
        expected_required_data_root=expected_required_data_root,
    )
    _validate_manifest_close_advice_plan_for_seal(
        manifest_path=Path(manifest_path).resolve(),
        payload=payload,
    )
    return payload, root


def _validate_manifest_symbols(
    *,
    payload: Mapping[str, Any],
    root: Path,
    run_id: str,
    sealed_at: datetime,
) -> dict[str, FrozenRequiredDataEntry]:
    symbols = payload.get("symbols")
    if not isinstance(symbols, dict):
        raise RequiredDataSnapshotError(
            "required-data snapshot symbols are invalid"
        )
    ready_fields = {
        "status",
        "fetch_plan",
        "expected_fetch_contract",
        "expected_fetch_contract_sha256",
        "fetch_policy_hash",
        "receipt_relpath",
        "receipt_hash",
        "snapshot_id",
        "payload_sha256",
        "source_observed_at",
        "expires_at",
        "raw_json_relpath",
        "required_data_csv_relpath",
        "source_outcome",
    }
    failed_fields = {"status", "reason", "error_type"}
    resolved_entries: dict[str, FrozenRequiredDataEntry] = {}
    ready_count = 0
    for symbol_key, raw_entry in symbols.items():
        symbol = _required_text(symbol_key, "manifest symbol").upper()
        if symbol != symbol_key or not isinstance(raw_entry, Mapping):
            raise RequiredDataSnapshotError(
                "required-data snapshot symbol entry is invalid"
            )
        entry = dict(raw_entry)
        entry_status = str(entry.get("status") or "").strip().lower()
        if entry_status == "ready":
            allowed_fields = ready_fields
            if "reason_code" in entry:
                allowed_fields |= {"reason_code"}
            if "scan_blob_ref" in entry:
                allowed_fields |= {"scan_blob_ref"}
            if set(entry) != allowed_fields:
                raise RequiredDataSnapshotError(
                    f"{symbol} ready manifest entry fields do not match schema"
                )
            try:
                resolved_entries[symbol] = _validate_ready_entry(
                    root=root,
                    run_id=run_id,
                    symbol=symbol,
                    entry=entry,
                    now=sealed_at,
                )
            except Exception as exc:
                raise _RequiredDataSnapshotEntryError(
                    f"{symbol} ready manifest entry is invalid: {exc}"
                ) from exc
            ready_count += 1
            continue
        if entry_status != "failed":
            raise RequiredDataSnapshotError(
                f"{symbol} manifest entry status is invalid"
            )
        allowed_fields = failed_fields | (
            {"detail"} if "detail" in entry else set()
        )
        if set(entry) != allowed_fields:
            raise RequiredDataSnapshotError(
                f"{symbol} failed manifest entry fields do not match schema"
            )
        _required_text(entry.get("reason"), f"{symbol} failure reason")
        _required_text(entry.get("error_type"), f"{symbol} failure error_type")
        if "detail" in entry and not isinstance(entry["detail"], str):
            raise RequiredDataSnapshotError(
                f"{symbol} failure detail is invalid"
            )

    total = len(symbols)
    failed_count = total - ready_count
    summary = payload.get("summary")
    if not isinstance(summary, Mapping) or set(summary) != {
        "symbols_total",
        "ready",
        "failed",
    }:
        raise RequiredDataSnapshotError(
            "required-data snapshot summary is invalid"
        )
    expected_summary = {
        "symbols_total": total,
        "ready": ready_count,
        "failed": failed_count,
    }
    for key, expected in expected_summary.items():
        actual = summary.get(key)
        if isinstance(actual, bool) or not isinstance(actual, int) or actual != expected:
            raise RequiredDataSnapshotError(
                "required-data snapshot summary contradicts symbol entries"
            )
    if ready_count == total and total:
        expected_status = "complete"
    elif ready_count:
        expected_status = "partial"
    else:
        expected_status = "failed"
    if str(payload.get("status") or "").strip().lower() != expected_status:
        raise RequiredDataSnapshotError(
            "required-data snapshot status contradicts symbol entries"
        )
    return resolved_entries


def resolve_frozen_required_data(
    *,
    manifest_path: Path,
    expected_run_id: str,
    symbol: str,
    required_data_root: Path,
    now: datetime | None = None,
) -> dict[str, Any]:
    validated, _csv_bytes = resolve_frozen_required_data_csv_bytes(
        manifest_path=manifest_path,
        expected_run_id=expected_run_id,
        symbol=symbol,
        required_data_root=required_data_root,
        now=now,
    )
    return validated


def resolve_frozen_required_data_csv_bytes(
    *,
    manifest_path: Path,
    expected_run_id: str,
    symbol: str,
    required_data_root: Path,
    now: datetime | None = None,
) -> tuple[dict[str, Any], bytes]:
    symbol_norm = _required_text(symbol, "symbol").upper()
    try:
        batch = resolve_frozen_required_data_csv_bytes_batch(
            manifest_path=manifest_path,
            expected_run_id=expected_run_id,
            required_data_root=required_data_root,
            now=now,
            require_fresh=False,
        )
        validated, csv_bytes = batch.resolve(symbol_norm)
        try:
            _validate_resolved_entry_freshness(
                validated=validated,
                now=_manifest_timestamp(
                    now or datetime.now(timezone.utc),
                    "now",
                ),
            )
        except (RequiredDataSnapshotError, SourceReceiptError) as exc:
            raise FrozenRequiredDataUnavailable(
                symbol=symbol_norm,
                reason="receipt_or_payload_mismatch",
                detail=str(exc),
                snapshot_id=str(validated.get("snapshot_id") or ""),
                receipt_relpath=str(validated.get("receipt_relpath") or ""),
            ) from exc
        return validated, csv_bytes
    except FrozenRequiredDataUnavailable as exc:
        if exc.symbol == symbol_norm:
            raise
        raise FrozenRequiredDataUnavailable(
            symbol=symbol_norm,
            reason=exc.reason,
            detail=exc.detail,
            snapshot_id=exc.snapshot_id,
            receipt_relpath=exc.receipt_relpath,
        ) from exc


def resolve_frozen_required_data_csv_bytes_batch(
    *,
    manifest_path: Path,
    expected_run_id: str,
    required_data_root: Path,
    now: datetime | None = None,
    require_fresh: bool = True,
) -> FrozenRequiredDataBatch:
    try:
        manifest, _root, manifest_bytes, resolved_entries = (
            _load_required_data_snapshot_manifest_snapshot(
                manifest_path=manifest_path,
                expected_run_id=expected_run_id,
                expected_required_data_root=required_data_root,
            )
        )
    except _RequiredDataSnapshotEntryError as exc:
        raise FrozenRequiredDataUnavailable(
            symbol="UNKNOWN",
            reason="receipt_or_payload_mismatch",
            detail=str(exc),
        ) from exc
    except RequiredDataSnapshotError as exc:
        raise FrozenRequiredDataUnavailable(
            symbol="UNKNOWN",
            reason="manifest_invalid",
            detail=str(exc),
        ) from exc

    manifest_sha256 = sha256_bytes(manifest_bytes)
    resolved_at: datetime | None = None
    if require_fresh:
        try:
            resolved_at = _manifest_timestamp(
                now or datetime.now(timezone.utc),
                "now",
            )
        except RequiredDataSnapshotError as exc:
            raise FrozenRequiredDataUnavailable(
                symbol="UNKNOWN",
                reason="receipt_or_payload_mismatch",
                detail=str(exc),
            ) from exc
    entries: dict[str, FrozenRequiredDataEntry] = {}
    unavailable: dict[str, FrozenRequiredDataUnavailable] = {}
    for symbol, raw_entry in dict(manifest["symbols"]).items():
        entry = dict(raw_entry)
        if str(entry.get("status") or "").strip().lower() != "ready":
            unavailable[symbol] = FrozenRequiredDataUnavailable(
                symbol=symbol,
                reason=str(entry.get("reason") or "symbol_snapshot_failed"),
                detail=str(entry.get("error_type") or ""),
            )
            continue
        validated, csv_bytes = resolved_entries[symbol]
        if require_fresh:
            assert resolved_at is not None
            try:
                _validate_resolved_entry_freshness(
                    validated=validated,
                    now=resolved_at,
                )
            except SourceReceiptError as exc:
                unavailable[symbol] = FrozenRequiredDataUnavailable(
                    symbol=symbol,
                    reason="receipt_or_payload_mismatch",
                    detail=str(exc),
                    snapshot_id=str(entry.get("snapshot_id") or ""),
                    receipt_relpath=str(entry.get("receipt_relpath") or ""),
                )
                continue
        entries[symbol] = (
            {
                **validated,
                "manifest_path": str(Path(manifest_path).resolve()),
                "manifest_sha256": manifest_sha256,
                "plan_id": str(manifest["plan_id"]),
            },
            csv_bytes,
        )
    return FrozenRequiredDataBatch(
        manifest=manifest,
        manifest_bytes=manifest_bytes,
        entries=entries,
        unavailable=unavailable,
        require_fresh=require_fresh,
    )


def _validate_resolved_entry_freshness(
    *,
    validated: Mapping[str, Any],
    now: datetime,
) -> None:
    observed_at = _manifest_timestamp(
        validated.get("source_observed_at"),
        "source_observed_at",
    )
    expires_at = _manifest_timestamp(
        validated.get("expires_at"),
        "expires_at",
    )
    if observed_at > now:
        raise SourceReceiptError("source observation is in the future")
    if now >= expires_at:
        raise SourceReceiptError("source receipt is stale")


def _validate_global_plan_symbols(
    plan_symbols: list[dict[str, Any]],
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    validated: list[tuple[dict[str, Any], dict[str, Any]]] = []
    seen_symbols: set[str] = set()
    for plan_item in plan_symbols:
        symbol = _required_text(plan_item.get("symbol"), "plan symbol").upper()
        if symbol in seen_symbols:
            raise RequiredDataSnapshotError(
                f"global required-data plan duplicates symbol {symbol}"
            )
        seen_symbols.add(symbol)
        contract_payload = plan_item.get("expected_fetch_contract")
        if not isinstance(contract_payload, Mapping):
            raise RequiredDataSnapshotError(
                f"{symbol} expected fetch contract is unavailable"
            )
        try:
            contract = validate_required_data_expected_fetch_contract(
                contract_payload,
                expected_symbol=symbol,
            )
        except (TypeError, ValueError) as exc:
            raise RequiredDataSnapshotError(
                f"{symbol} expected fetch contract is invalid: {exc}"
            ) from exc
        fetch_plan = plan_item.get("fetch_plan")
        fetch_binding = plan_item.get("fetch_binding")
        if not isinstance(fetch_plan, Mapping) or dict(fetch_plan) != dict(
            contract["fetch_plan"]
        ):
            raise RequiredDataSnapshotError(
                f"{symbol} global plan fetch plan contradicts its contract"
            )
        if not isinstance(fetch_binding, Mapping) or dict(fetch_binding) != dict(
            contract["fetch_binding"]
        ):
            raise RequiredDataSnapshotError(
                f"{symbol} global plan physical binding contradicts its contract"
            )
        contract_plan = dict(contract["fetch_plan"])
        if str(contract_plan.get("symbol") or "").strip().upper() != symbol:
            raise RequiredDataSnapshotError(
                f"{symbol} expected fetch plan symbol mismatch"
            )
        coverage = dict(contract.get("coverage_policy") or {})
        projection_outcome = str(
            contract_plan.get("projection_outcome") or ""
        ).strip()
        if projection_outcome not in {"success_rows", "success_empty"}:
            raise RequiredDataSnapshotError(
                f"{symbol} expected fetch projection is not terminal-success"
            )
        if str(coverage.get("projection_outcome") or "").strip() != (
            projection_outcome
        ):
            raise RequiredDataSnapshotError(
                f"{symbol} coverage projection contradicts its fetch plan"
            )
        if str(plan_item.get("projection_outcome") or "").strip() != (
            projection_outcome
        ):
            raise RequiredDataSnapshotError(
                f"{symbol} global plan projection contradicts its contract"
            )
        if _normalize_source(plan_item.get("source")) != str(
            contract["fetch_binding"].get("source") or ""
        ):
            raise RequiredDataSnapshotError(
                f"{symbol} global plan source contradicts its contract"
            )
        validated.append((plan_item, contract))
    return validated


def _ready_manifest_entry(
    *,
    root: Path,
    run_id: str,
    symbol: str,
    plan_item: Mapping[str, Any],
    expected_fetch_contract: Mapping[str, Any],
    evidence: Mapping[str, Any],
    now: datetime,
) -> dict[str, Any]:
    receipt_relpath = _required_text(evidence.get("receipt_relpath"), "receipt_relpath")
    receipt_path = safe_existing_relative_path(root, receipt_relpath)
    receipt_bytes = receipt_path.read_bytes()
    receipt = json.loads(receipt_bytes.decode("utf-8"))
    validated = validate_source_receipt(
        receipt,
        producer_root=root,
        now=now,
        expected_source_kind="quotes",
    )
    if str(validated.get("producer_run_id") or "") != run_id:
        raise RequiredDataSnapshotError(f"{symbol} quote receipt run mismatch")
    bundle = json.loads(validated["payload_bytes"])
    if (
        not isinstance(bundle, dict)
        or bundle.get("schema_version") != REQUIRED_DATA_QUOTE_SNAPSHOT_SCHEMA
        or str(bundle.get("symbol") or "").strip().upper() != symbol
    ):
        raise RequiredDataSnapshotError(f"{symbol} quote bundle is invalid")
    raw_bytes, csv_bytes, read_source, scan_blob_ref = _bundle_scan_bytes(
        root=root,
        run_id=run_id,
        bundle=bundle,
        expected_scan_blob_ref=evidence.get("scan_blob_ref"),
    )
    contract = _validate_bundle_authority(
        bundle=bundle,
        receipt=receipt,
        symbol=symbol,
        expected_fetch_contract=expected_fetch_contract,
        raw_json_bytes=raw_bytes,
    )
    if str(evidence.get("expected_fetch_contract_sha256") or "") != str(
        contract["contract_sha256"]
    ):
        raise RequiredDataSnapshotError(
            f"{symbol} resolver contract evidence mismatch"
        )
    if read_source == "canonical_blob":
        validate_required_data_quote_bytes(
            raw_json_bytes=raw_bytes,
            required_data_csv_bytes=csv_bytes,
            expected_fetch_contract=contract,
        )
    else:
        _validate_canonical_bundle_candidate(
            root=root,
            bundle=bundle,
            expected_fetch_contract=contract,
        )
    source_outcome, reason_code = _validate_complete_required_data_bundle(
        bundle,
        raw_json_bytes=raw_bytes,
        required_data_csv_bytes=csv_bytes,
    )
    if dict(plan_item.get("fetch_plan") or {}) != dict(contract["fetch_plan"]):
        raise RequiredDataSnapshotError(f"{symbol} plan fetch contract mismatch")
    fetch_policy_hash = str(bundle["fetch_policy_hash"])
    entry = {
        "status": "ready",
        "fetch_plan": dict(contract["fetch_plan"]),
        "expected_fetch_contract": contract,
        "expected_fetch_contract_sha256": str(contract["contract_sha256"]),
        "fetch_policy_hash": fetch_policy_hash,
        "receipt_relpath": receipt_relpath,
        "receipt_hash": sha256_bytes(receipt_bytes),
        "snapshot_id": str(validated["snapshot_id"]),
        "payload_sha256": str(validated["payload_sha256"]),
        "source_observed_at": str(validated["source_observed_at"]),
        "expires_at": str(validated["expires_at"]),
        "raw_json_relpath": _required_text(bundle.get("raw_json_relpath"), "raw_json_relpath"),
        "required_data_csv_relpath": _required_text(
            bundle.get("required_data_csv_relpath"),
            "required_data_csv_relpath",
        ),
        "source_outcome": source_outcome,
    }
    if scan_blob_ref is not None:
        if (
            evidence.get("read_source") != "canonical_blob"
            or evidence.get("scan_blob_ref") != scan_blob_ref
        ):
            raise RequiredDataSnapshotError(
                f"{symbol} canonical blob resolver evidence mismatch"
            )
        entry["scan_blob_ref"] = scan_blob_ref
    if reason_code:
        entry["reason_code"] = reason_code
    return entry


def _validate_ready_entry(
    *,
    root: Path,
    run_id: str,
    symbol: str,
    entry: Mapping[str, Any],
    now: datetime,
) -> tuple[dict[str, Any], bytes]:
    receipt_relpath = _required_text(entry.get("receipt_relpath"), "receipt_relpath")
    receipt_path = safe_existing_relative_path(root, receipt_relpath)
    receipt_bytes = receipt_path.read_bytes()
    if sha256_bytes(receipt_bytes) != _required_text(entry.get("receipt_hash"), "receipt_hash"):
        raise SourceReceiptError("manifest receipt hash mismatch")
    receipt = json.loads(receipt_bytes.decode("utf-8"))
    validated = validate_source_receipt(
        receipt,
        producer_root=root,
        now=now,
        expected_source_kind="quotes",
    )
    if str(validated.get("producer_run_id") or "") != run_id:
        raise SourceReceiptError("manifest receipt producer run mismatch")
    if str(validated.get("snapshot_id") or "") != str(entry.get("snapshot_id") or ""):
        raise SourceReceiptError("manifest snapshot id mismatch")
    if str(validated.get("payload_sha256") or "") != str(
        entry.get("payload_sha256") or ""
    ):
        raise SourceReceiptError("manifest payload hash mismatch")
    if str(validated.get("source_observed_at") or "") != str(
        entry.get("source_observed_at") or ""
    ):
        raise SourceReceiptError("manifest source timestamp mismatch")
    if str(validated.get("expires_at") or "") != str(
        entry.get("expires_at") or ""
    ):
        raise SourceReceiptError("manifest expiry mismatch")
    bundle = json.loads(validated["payload_bytes"])
    if (
        not isinstance(bundle, dict)
        or bundle.get("schema_version") != REQUIRED_DATA_QUOTE_SNAPSHOT_SCHEMA
        or str(bundle.get("symbol") or "").strip().upper() != symbol
    ):
        raise SourceReceiptError("required-data quote bundle mismatch")
    expected_contract_payload = entry.get("expected_fetch_contract")
    if not isinstance(expected_contract_payload, Mapping):
        raise SourceReceiptError(
            "manifest expected fetch contract is missing"
        )
    expected_contract = validate_required_data_expected_fetch_contract(
        expected_contract_payload,
        expected_symbol=symbol,
    )
    if str(entry.get("expected_fetch_contract_sha256") or "") != str(
        expected_contract["contract_sha256"]
    ):
        raise SourceReceiptError(
            "manifest expected fetch contract hash mismatch"
        )
    raw_bytes, csv_bytes, read_source, scan_blob_ref = _bundle_scan_bytes(
        root=root,
        run_id=run_id,
        bundle=bundle,
        expected_scan_blob_ref=entry.get("scan_blob_ref"),
    )
    contract = _validate_bundle_authority(
        bundle=bundle,
        receipt=receipt,
        symbol=symbol,
        expected_fetch_contract=expected_contract,
        raw_json_bytes=raw_bytes,
    )
    if read_source == "canonical_blob":
        validate_required_data_quote_bytes(
            raw_json_bytes=raw_bytes,
            required_data_csv_bytes=csv_bytes,
            expected_fetch_contract=contract,
        )
    else:
        _validate_canonical_bundle_candidate(
            root=root,
            bundle=bundle,
            expected_fetch_contract=contract,
        )
    source_outcome, reason_code = _validate_complete_required_data_bundle(
        bundle,
        raw_json_bytes=raw_bytes,
        required_data_csv_bytes=csv_bytes,
    )
    raw_relpath = _required_text(entry.get("raw_json_relpath"), "raw_json_relpath")
    csv_relpath = _required_text(
        entry.get("required_data_csv_relpath"),
        "required_data_csv_relpath",
    )
    if raw_relpath != str(bundle.get("raw_json_relpath") or ""):
        raise SourceReceiptError("required-data JSON path mismatch")
    if csv_relpath != str(bundle.get("required_data_csv_relpath") or ""):
        raise SourceReceiptError("required-data CSV path mismatch")
    if dict(entry.get("fetch_plan") or {}) != dict(contract["fetch_plan"]):
        raise SourceReceiptError("required-data fetch plan mismatch")
    if str(entry.get("fetch_policy_hash") or "") != str(
        bundle.get("fetch_policy_hash") or ""
    ):
        raise SourceReceiptError("required-data fetch policy mismatch")
    if str(entry.get("source_outcome") or "") != source_outcome:
        raise SourceReceiptError(
            "required-data source outcome mismatch"
        )
    if str(entry.get("reason_code") or "") != str(reason_code or ""):
        raise SourceReceiptError(
            "required-data reason code mismatch"
        )
    return (
        {
            "receipt_relpath": receipt_relpath,
            "receipt_hash": sha256_bytes(receipt_bytes),
            "snapshot_id": str(validated["snapshot_id"]),
            "payload_sha256": str(validated["payload_sha256"]),
            "source_observed_at": str(validated["source_observed_at"]),
            "expires_at": str(validated["expires_at"]),
            "raw_json_relpath": raw_relpath,
            "required_data_csv_relpath": csv_relpath,
            "required_data_root": str(root),
            "source_outcome": source_outcome,
            "reason_code": reason_code,
            "expected_fetch_contract": contract,
            "expected_fetch_contract_sha256": str(
                contract["contract_sha256"]
            ),
            "scan_blob_ref": scan_blob_ref,
            "read_source": read_source,
        },
        csv_bytes,
    )


def _bundle_scan_bytes(
    *,
    root: Path,
    run_id: str,
    bundle: Mapping[str, Any],
    expected_scan_blob_ref: Any,
) -> tuple[bytes, bytes, str, dict[str, Any] | None]:
    bundle_ref = bundle.get("scan_blob_ref")
    if bundle_ref is None:
        if expected_scan_blob_ref is not None:
            raise SourceReceiptError("manifest canonical blob ref is unbound")
        raw_bytes = base64.b64decode(
            _required_text(bundle.get("raw_json_base64"), "raw_json_base64"),
            validate=True,
        )
        csv_bytes = base64.b64decode(
            _required_text(
                bundle.get("required_data_csv_base64"),
                "required_data_csv_base64",
            ),
            validate=True,
        )
        raw_relpath = _required_text(bundle.get("raw_json_relpath"), "raw_json_relpath")
        csv_relpath = _required_text(
            bundle.get("required_data_csv_relpath"),
            "required_data_csv_relpath",
        )
        if (
            safe_existing_relative_path(root, raw_relpath).read_bytes() != raw_bytes
            or safe_existing_relative_path(root, csv_relpath).read_bytes() != csv_bytes
        ):
            raise SourceReceiptError(
                "required-data bytes do not match the sealed receipt"
            )
        return raw_bytes, csv_bytes, "legacy_snapshot", None
    if not isinstance(bundle_ref, Mapping) or not isinstance(
        expected_scan_blob_ref,
        Mapping,
    ):
        raise SourceReceiptError("required-data canonical blob ref is invalid")
    validated_ref = validate_required_data_scan_blob_ref(bundle_ref)
    if validate_required_data_scan_blob_ref(expected_scan_blob_ref) != validated_ref:
        raise SourceReceiptError("manifest canonical blob ref mismatch")
    loaded = load_required_data_scan_blob(
        runtime_root=_runtime_root_from_required_data_root(root, run_id),
        blob_ref=validated_ref,
    )
    raw_bytes = loaded["raw_json_bytes"]
    csv_bytes = loaded["required_data_csv_bytes"]
    for field, expected_bytes in (
        ("raw_json_base64", raw_bytes),
        ("required_data_csv_base64", csv_bytes),
    ):
        if field in bundle and not required_data_shadow_base64_matches(
            _required_text(bundle.get(field), field),
            expected_bytes,
        ):
            raise SourceReceiptError("required-data inline shadow mismatch")
    for field, expected_bytes in (
        ("raw_json_relpath", raw_bytes),
        ("required_data_csv_relpath", csv_bytes),
    ):
        relpath = _required_text(bundle.get(field), field)
        relative = Path(relpath)
        if relative.is_absolute() or ".." in relative.parts:
            raise SourceReceiptError("required-data legacy shadow path is invalid")
        candidate = root / relative
        if (candidate.exists() or candidate.is_symlink()) and not (
            required_data_shadow_file_matches(
                safe_existing_relative_path(root, relpath),
                expected_bytes,
            )
        ):
            raise SourceReceiptError("required-data legacy shadow mismatch")
    return raw_bytes, csv_bytes, "canonical_blob", validated_ref


def _validate_bundle_authority(
    *,
    bundle: Mapping[str, Any],
    receipt: Mapping[str, Any],
    symbol: str,
    expected_fetch_contract: Mapping[str, Any],
    raw_json_bytes: bytes | None = None,
) -> dict[str, Any]:
    contract_payload = bundle.get("expected_fetch_contract")
    if not isinstance(contract_payload, Mapping):
        raise SourceReceiptError(
            "required-data bundle expected fetch contract is missing"
        )
    contract = validate_required_data_expected_fetch_contract(
        contract_payload,
        expected_symbol=symbol,
    )
    expected = validate_required_data_expected_fetch_contract(
        expected_fetch_contract,
        expected_symbol=symbol,
    )
    if contract != expected:
        raise SourceReceiptError(
            "required-data bundle expected fetch contract mismatch"
        )
    contract_hash = str(contract["contract_sha256"])
    if str(bundle.get("expected_fetch_contract_sha256") or "") != contract_hash:
        raise SourceReceiptError(
            "required-data bundle expected fetch contract hash mismatch"
        )
    if dict(bundle.get("fetch_plan") or {}) != dict(contract["fetch_plan"]):
        raise SourceReceiptError(
            "required-data bundle fetch plan contradicts its contract"
        )

    fetch_policy = bundle.get("fetch_policy")
    if not isinstance(fetch_policy, Mapping):
        raise SourceReceiptError(
            "required-data operational fetch policy is invalid"
        )
    policy_payload = {
        "schema": "required_data_fetch_policy.v2",
        "expected_fetch_contract": contract,
        "fetch_policy": dict(fetch_policy),
    }
    policy_hash = canonical_sha256(policy_payload)
    if (
        str(bundle.get("fetch_policy_hash") or "") != policy_hash
        or str(receipt.get("producer_policy_hash") or "") != policy_hash
    ):
        raise SourceReceiptError(
            "required-data operational fetch policy hash mismatch"
        )
    _validate_physical_binding(
        observed=fetch_policy,
        expected=contract["fetch_binding"],
        subject="operational fetch policy",
    )
    try:
        raw_bytes = (
            bytes(raw_json_bytes)
            if raw_json_bytes is not None
            else base64.b64decode(
                _required_text(bundle.get("raw_json_base64"), "raw_json_base64"),
                validate=True,
            )
        )
        raw_payload = json.loads(raw_bytes.decode("utf-8"))
    except (
        ValueError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        binascii.Error,
    ) as exc:
        raise SourceReceiptError(
            "required-data bundle raw JSON is unreadable"
        ) from exc
    meta = raw_payload.get("meta") if isinstance(raw_payload, Mapping) else None
    if not isinstance(meta, Mapping):
        raise SourceReceiptError(
            "required-data payload physical binding is missing"
        )
    _validate_physical_binding(
        observed=meta,
        expected=contract["fetch_binding"],
        subject="payload",
    )
    return contract


def _validate_canonical_bundle_candidate(
    *,
    root: Path,
    bundle: Mapping[str, Any],
    expected_fetch_contract: Mapping[str, Any],
) -> None:
    raw_relpath = _required_text(
        bundle.get("raw_json_relpath"),
        "raw_json_relpath",
    )
    csv_relpath = _required_text(
        bundle.get("required_data_csv_relpath"),
        "required_data_csv_relpath",
    )
    validate_required_data_quote_candidate(
        producer_root=root,
        raw_path=safe_existing_relative_path(root, raw_relpath),
        csv_path=safe_existing_relative_path(root, csv_relpath),
        expected_fetch_contract=expected_fetch_contract,
    )


def _validate_physical_binding(
    *,
    observed: Mapping[str, Any],
    expected: Mapping[str, Any],
    subject: str,
) -> None:
    actual_port = observed.get("port")
    expected_port = expected.get("port")
    if (
        isinstance(actual_port, bool)
        or not isinstance(actual_port, int)
        or isinstance(expected_port, bool)
        or not isinstance(expected_port, int)
    ):
        raise SourceReceiptError(
            f"required-data {subject} physical binding is invalid"
        )
    try:
        actual_binding = {
            "source": _normalize_source(observed.get("source")),
            "host": _required_text(observed.get("host"), f"{subject} host"),
            "port": actual_port,
        }
        expected_binding = {
            "source": _normalize_source(expected.get("source")),
            "host": _required_text(expected.get("host"), "expected fetch host"),
            "port": expected_port,
        }
    except (TypeError, ValueError) as exc:
        raise SourceReceiptError(
            f"required-data {subject} physical binding is invalid"
        ) from exc
    if not actual_binding["source"] or actual_binding != expected_binding:
        raise SourceReceiptError(
            f"required-data {subject} physical binding mismatch"
        )


def _validate_complete_required_data_bundle(
    bundle: Mapping[str, Any],
    *,
    raw_json_bytes: bytes | None = None,
    required_data_csv_bytes: bytes | None = None,
) -> tuple[str, str | None]:
    try:
        raw_bytes = (
            bytes(raw_json_bytes)
            if raw_json_bytes is not None
            else base64.b64decode(
                _required_text(bundle.get("raw_json_base64"), "raw_json_base64"),
                validate=True,
            )
        )
        raw_payload = json.loads(raw_bytes.decode("utf-8"))
    except (
        ValueError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        binascii.Error,
    ) as exc:
        raise SourceReceiptError(
            "required-data bundle raw JSON is unreadable"
        ) from exc
    meta = raw_payload.get("meta") if isinstance(raw_payload, dict) else None
    if str((meta or {}).get("status") or "").strip().lower() != "ok":
        raise SourceReceiptError(
            "required-data bundle is not complete"
        )
    rows = raw_payload.get("rows") if isinstance(raw_payload, dict) else None
    if not isinstance(rows, list):
        raise SourceReceiptError(
            "required-data bundle rows are invalid"
        )
    source_outcome, reason_code = validate_required_data_source_outcome(
        rows=rows,
        source_outcome=(meta or {}).get("source_outcome"),
        reason_code=(meta or {}).get("reason_code"),
        subject="bundle",
    )
    if not rows:
        try:
            csv_bytes = (
                bytes(required_data_csv_bytes)
                if required_data_csv_bytes is not None
                else base64.b64decode(
                    _required_text(
                        bundle.get("required_data_csv_base64"),
                        "required_data_csv_base64",
                    ),
                    validate=True,
                )
            )
            csv_rows = list(
                csv.reader(
                    io.StringIO(csv_bytes.decode("utf-8"))
                )
            )
        except (
            ValueError,
            UnicodeDecodeError,
            csv.Error,
            binascii.Error,
        ) as exc:
            raise SourceReceiptError(
                "success-empty required-data CSV is unreadable"
            ) from exc
        if (
            not csv_rows
            or csv_rows[0] != REQUIRED_DATA_COLUMNS
            or len(csv_rows) != 1
        ):
            raise SourceReceiptError(
                "success-empty required-data CSV is not header-only"
            )
        return source_outcome, reason_code
    return source_outcome, reason_code


def _prefetch_result_index(summary: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for key in ("symbols", "results"):
        values = summary.get(key)
        if isinstance(values, Mapping):
            iterator = [
                {"symbol": symbol, **(dict(value) if isinstance(value, Mapping) else {"reason": value})}
                for symbol, value in values.items()
            ]
        elif isinstance(values, list):
            iterator = [dict(value) for value in values if isinstance(value, Mapping)]
        else:
            iterator = []
        for item in iterator:
            symbol = str(item.get("symbol") or "").strip().upper()
            if symbol:
                out[symbol] = item
    return out


def _failed_manifest_entry(
    failure: Mapping[str, Any],
    *,
    default_reason: str,
) -> dict[str, Any]:
    return {
        "status": "failed",
        "reason": str(
            failure.get("message")
            or failure.get("reason")
            or failure.get("status")
            or default_reason
        ).strip(),
        "error_type": str(
            failure.get("error_type")
            or failure.get("error_code")
            or "RequiredDataFetchError"
        ).strip(),
    }


def _manifest_timestamp(value: Any, field: str) -> datetime:
    try:
        if isinstance(value, datetime):
            parsed = value
        else:
            text = _required_text(value, field)
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            parsed = datetime.fromisoformat(text)
    except (TypeError, ValueError) as exc:
        raise RequiredDataSnapshotError(f"{field} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RequiredDataSnapshotError(f"{field} must be timezone-aware")
    if parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise RequiredDataSnapshotError(f"{field} must be UTC")
    return parsed


def _existing_directory(path: Path, field: str) -> Path:
    candidate = Path(path)
    if not candidate.exists() or not candidate.is_dir() or candidate.is_symlink():
        raise RequiredDataSnapshotError(f"{field} is invalid")
    return candidate.resolve()


def _runtime_root_from_required_data_root(root: Path, run_id: str) -> Path:
    candidate = Path(root).resolve()
    if (
        candidate.name != "required_data"
        or candidate.parent.name != run_id
        or candidate.parent.parent.name != "output_runs"
    ):
        raise RequiredDataSnapshotError(
            "required-data root is outside the runtime run layout"
        )
    return candidate.parent.parent.parent


def _is_sha256(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return len(text) == 64 and all(char in "0123456789abcdef" for char in text)


def _normalize_source(value: Any) -> str:
    if not str(value or "").strip():
        return ""
    return normalize_fetch_source(value)


__all__ = [
    "FrozenRequiredDataBatch",
    "FrozenRequiredDataUnavailable",
    "REQUIRED_DATA_SNAPSHOT_MANIFEST_SCHEMA",
    "RequiredDataSnapshotError",
    "load_required_data_snapshot_manifest",
    "load_required_data_snapshot_manifest_snapshot",
    "resolve_frozen_required_data",
    "resolve_frozen_required_data_csv_bytes",
    "resolve_frozen_required_data_csv_bytes_batch",
    "retire_required_data_snapshot_shadows",
    "seal_required_data_snapshot",
]
