from __future__ import annotations

import base64
import binascii
import csv
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
from typing import Any, Mapping

from domain.domain.decision_state_fingerprint import canonical_sha256
from src.application.opend_symbol_outputs import (
    REQUIRED_DATA_COLUMNS,
    REQUIRED_DATA_QUOTE_SNAPSHOT_SCHEMA,
    resolve_exact_fresh_required_data_quote_receipt,
    validate_required_data_source_outcome,
)
from src.application.position_advice_source_receipts import (
    PositionAdviceSourceError,
    safe_existing_relative_path,
    sha256_bytes,
    validate_source_receipt,
)
from src.application.required_data_plan_identity import required_data_plan_id
from src.infrastructure.io_utils import atomic_write_json


REQUIRED_DATA_SNAPSHOT_MANIFEST_SCHEMA = "required_data_snapshot_manifest.v1"
_TERMINAL_STATUSES = frozenset({"complete", "partial", "failed"})


class RequiredDataSnapshotError(RuntimeError):
    """Raised when a run-scoped required-data snapshot cannot be sealed."""


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
    result_index = _prefetch_result_index(summary)
    symbol_entries: dict[str, dict[str, Any]] = {}
    for plan_item in normalized_plan_symbols:
        symbol = _required_text(plan_item.get("symbol"), "plan symbol").upper()
        evidence = resolve_exact_fresh_required_data_quote_receipt(
            producer_root=root,
            symbol=symbol,
            expected_producer_run_id=run_id_norm,
        )
        if evidence is None:
            failure = result_index.get(symbol, {})
            symbol_entries[symbol] = {
                "status": "failed",
                "reason": str(
                    failure.get("message")
                    or failure.get("reason")
                    or failure.get("status")
                    or "quote_receipt_unavailable"
                ).strip(),
                "error_type": str(
                    failure.get("error_type")
                    or failure.get("error_code")
                    or "RequiredDataFetchError"
                ).strip(),
            }
            continue
        symbol_entries[symbol] = _ready_manifest_entry(
            root=root,
            run_id=run_id_norm,
            symbol=symbol,
            plan_item=plan_item,
            evidence=evidence,
        )

    ready = sum(1 for item in symbol_entries.values() if item.get("status") == "ready")
    failed = len(symbol_entries) - ready
    if ready == len(symbol_entries) and symbol_entries:
        status = "complete"
    elif ready > 0:
        status = "partial"
    else:
        status = "failed"

    target = Path(manifest_path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    root_relpath = os.path.relpath(root, target.parent)
    payload = {
        "schema_version": REQUIRED_DATA_SNAPSHOT_MANIFEST_SCHEMA,
        "run_id": run_id_norm,
        "status": status,
        "plan_id": plan_id,
        "sealed_at_utc": (sealed_at or datetime.now(timezone.utc)).isoformat(),
        "required_data_root_relpath": Path(root_relpath).as_posix(),
        "symbols": {key: symbol_entries[key] for key in sorted(symbol_entries)},
        "summary": {
            "symbols_total": len(symbol_entries),
            "ready": ready,
            "failed": failed,
        },
    }
    if close_advice_required_data_plan_path is not None:
        plan_path = Path(close_advice_required_data_plan_path).resolve()
        if not plan_path.is_file() or plan_path.is_symlink():
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
    payload["content_sha256"] = canonical_sha256(payload)
    atomic_write_json(target, payload)
    return payload


def load_required_data_snapshot_manifest(
    *,
    manifest_path: Path,
    expected_run_id: str,
    expected_required_data_root: Path | None = None,
) -> tuple[dict[str, Any], Path]:
    path = Path(manifest_path).resolve()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RequiredDataSnapshotError("required-data snapshot manifest is unreadable") from exc
    if not isinstance(payload, dict):
        raise RequiredDataSnapshotError("required-data snapshot manifest must be an object")
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
    root_relpath = _required_text(
        payload.get("required_data_root_relpath"),
        "required_data_root_relpath",
    )
    root = (path.parent / root_relpath).resolve()
    root = _existing_directory(root, "manifest required_data_root")
    if expected_required_data_root is not None and root != Path(expected_required_data_root).resolve():
        raise RequiredDataSnapshotError("required-data snapshot root mismatch")
    symbols = payload.get("symbols")
    if not isinstance(symbols, dict):
        raise RequiredDataSnapshotError("required-data snapshot symbols are invalid")
    return payload, root


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
        manifest, root = load_required_data_snapshot_manifest(
            manifest_path=manifest_path,
            expected_run_id=expected_run_id,
            expected_required_data_root=required_data_root,
        )
    except RequiredDataSnapshotError as exc:
        raise FrozenRequiredDataUnavailable(
            symbol=symbol_norm,
            reason="manifest_invalid",
            detail=str(exc),
        ) from exc
    entry = (manifest.get("symbols") or {}).get(symbol_norm)
    if not isinstance(entry, Mapping):
        raise FrozenRequiredDataUnavailable(
            symbol=symbol_norm,
            reason="symbol_entry_missing",
        )
    status = str(entry.get("status") or "").strip().lower()
    if status != "ready":
        raise FrozenRequiredDataUnavailable(
            symbol=symbol_norm,
            reason=str(entry.get("reason") or "symbol_snapshot_failed"),
            detail=str(entry.get("error_type") or ""),
        )
    try:
        validated, csv_bytes = _validate_ready_entry(
            root=root,
            run_id=str(manifest["run_id"]),
            symbol=symbol_norm,
            entry=entry,
            now=now or datetime.now(timezone.utc),
        )
    except (
        OSError,
        ValueError,
        TypeError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        binascii.Error,
        PositionAdviceSourceError,
        RequiredDataSnapshotError,
    ) as exc:
        raise FrozenRequiredDataUnavailable(
            symbol=symbol_norm,
            reason="receipt_or_payload_mismatch",
            detail=str(exc),
            snapshot_id=str(entry.get("snapshot_id") or ""),
            receipt_relpath=str(entry.get("receipt_relpath") or ""),
        ) from exc
    manifest_bytes = Path(manifest_path).resolve().read_bytes()
    return (
        {
            **validated,
            "manifest_path": str(Path(manifest_path).resolve()),
            "manifest_sha256": sha256_bytes(manifest_bytes),
            "plan_id": str(manifest["plan_id"]),
        },
        csv_bytes,
    )


def _ready_manifest_entry(
    *,
    root: Path,
    run_id: str,
    symbol: str,
    plan_item: Mapping[str, Any],
    evidence: Mapping[str, Any],
) -> dict[str, Any]:
    receipt_relpath = _required_text(evidence.get("receipt_relpath"), "receipt_relpath")
    receipt_path = safe_existing_relative_path(root, receipt_relpath)
    receipt_bytes = receipt_path.read_bytes()
    receipt = json.loads(receipt_bytes.decode("utf-8"))
    validated = validate_source_receipt(
        receipt,
        producer_root=root,
        now=datetime.now(timezone.utc),
        expected_source_kind="quotes",
    )
    if str(validated.get("producer_run_id") or "") != run_id:
        raise RequiredDataSnapshotError(f"{symbol} quote receipt run mismatch")
    bundle = json.loads(validated["payload_path"].read_text(encoding="utf-8"))
    if (
        not isinstance(bundle, dict)
        or bundle.get("schema_version") != REQUIRED_DATA_QUOTE_SNAPSHOT_SCHEMA
        or str(bundle.get("symbol") or "").strip().upper() != symbol
    ):
        raise RequiredDataSnapshotError(f"{symbol} quote bundle is invalid")
    source_outcome, reason_code = _validate_complete_required_data_bundle(
        bundle
    )
    fetch_policy_hash = str(
        bundle.get("fetch_policy_hash")
        or receipt.get("producer_policy_hash")
        or ""
    ).strip()
    if not _is_sha256(fetch_policy_hash):
        raise RequiredDataSnapshotError(f"{symbol} fetch policy hash is invalid")
    entry = {
        "status": "ready",
        "fetch_plan": dict(bundle.get("fetch_plan") or plan_item.get("fetch_plan") or {}),
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
        raise PositionAdviceSourceError("manifest receipt hash mismatch")
    receipt = json.loads(receipt_bytes.decode("utf-8"))
    validated = validate_source_receipt(
        receipt,
        producer_root=root,
        now=now,
        expected_source_kind="quotes",
    )
    if str(validated.get("producer_run_id") or "") != run_id:
        raise PositionAdviceSourceError("manifest receipt producer run mismatch")
    if str(validated.get("snapshot_id") or "") != str(entry.get("snapshot_id") or ""):
        raise PositionAdviceSourceError("manifest snapshot id mismatch")
    if str(validated.get("payload_sha256") or "") != str(
        entry.get("payload_sha256") or ""
    ):
        raise PositionAdviceSourceError("manifest payload hash mismatch")
    if str(validated.get("source_observed_at") or "") != str(
        entry.get("source_observed_at") or ""
    ):
        raise PositionAdviceSourceError("manifest source timestamp mismatch")
    if str(validated.get("expires_at") or "") != str(
        entry.get("expires_at") or ""
    ):
        raise PositionAdviceSourceError("manifest expiry mismatch")
    bundle = json.loads(validated["payload_path"].read_text(encoding="utf-8"))
    if (
        not isinstance(bundle, dict)
        or bundle.get("schema_version") != REQUIRED_DATA_QUOTE_SNAPSHOT_SCHEMA
        or str(bundle.get("symbol") or "").strip().upper() != symbol
    ):
        raise PositionAdviceSourceError("required-data quote bundle mismatch")
    source_outcome, reason_code = _validate_complete_required_data_bundle(
        bundle
    )
    raw_relpath = _required_text(entry.get("raw_json_relpath"), "raw_json_relpath")
    csv_relpath = _required_text(
        entry.get("required_data_csv_relpath"),
        "required_data_csv_relpath",
    )
    if raw_relpath != str(bundle.get("raw_json_relpath") or ""):
        raise PositionAdviceSourceError("required-data JSON path mismatch")
    if csv_relpath != str(bundle.get("required_data_csv_relpath") or ""):
        raise PositionAdviceSourceError("required-data CSV path mismatch")
    if dict(entry.get("fetch_plan") or {}) != dict(bundle.get("fetch_plan") or {}):
        raise PositionAdviceSourceError("required-data fetch plan mismatch")
    if str(entry.get("fetch_policy_hash") or "") != str(
        bundle.get("fetch_policy_hash") or ""
    ):
        raise PositionAdviceSourceError("required-data fetch policy mismatch")
    if str(entry.get("source_outcome") or "") != source_outcome:
        raise PositionAdviceSourceError(
            "required-data source outcome mismatch"
        )
    if str(entry.get("reason_code") or "") != str(reason_code or ""):
        raise PositionAdviceSourceError(
            "required-data reason code mismatch"
        )
    raw_bytes = safe_existing_relative_path(root, raw_relpath).read_bytes()
    csv_bytes = safe_existing_relative_path(root, csv_relpath).read_bytes()
    captured_raw = base64.b64decode(
        _required_text(bundle.get("raw_json_base64"), "raw_json_base64"),
        validate=True,
    )
    captured_csv = base64.b64decode(
        _required_text(bundle.get("required_data_csv_base64"), "required_data_csv_base64"),
        validate=True,
    )
    if raw_bytes != captured_raw or csv_bytes != captured_csv:
        raise PositionAdviceSourceError("required-data bytes do not match the sealed receipt")
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
        },
        captured_csv,
    )


def _validate_complete_required_data_bundle(
    bundle: Mapping[str, Any],
) -> tuple[str, str | None]:
    try:
        raw_bytes = base64.b64decode(
            _required_text(bundle.get("raw_json_base64"), "raw_json_base64"),
            validate=True,
        )
        raw_payload = json.loads(raw_bytes.decode("utf-8"))
    except (
        ValueError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        binascii.Error,
    ) as exc:
        raise PositionAdviceSourceError(
            "required-data bundle raw JSON is unreadable"
        ) from exc
    meta = raw_payload.get("meta") if isinstance(raw_payload, dict) else None
    if str((meta or {}).get("status") or "").strip().lower() != "ok":
        raise PositionAdviceSourceError(
            "required-data bundle is not complete"
        )
    rows = raw_payload.get("rows") if isinstance(raw_payload, dict) else None
    if not isinstance(rows, list):
        raise PositionAdviceSourceError(
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
            csv_bytes = base64.b64decode(
                _required_text(
                    bundle.get("required_data_csv_base64"),
                    "required_data_csv_base64",
                ),
                validate=True,
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
            raise PositionAdviceSourceError(
                "success-empty required-data CSV is unreadable"
            ) from exc
        if (
            not csv_rows
            or csv_rows[0] != REQUIRED_DATA_COLUMNS
            or len(csv_rows) != 1
        ):
            raise PositionAdviceSourceError(
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


def _required_text(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise RequiredDataSnapshotError(f"{field} is required")
    return text


def _existing_directory(path: Path, field: str) -> Path:
    candidate = Path(path)
    if not candidate.exists() or not candidate.is_dir() or candidate.is_symlink():
        raise RequiredDataSnapshotError(f"{field} is invalid")
    return candidate.resolve()


def _is_sha256(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return len(text) == 64 and all(char in "0123456789abcdef" for char in text)


__all__ = [
    "FrozenRequiredDataUnavailable",
    "REQUIRED_DATA_SNAPSHOT_MANIFEST_SCHEMA",
    "RequiredDataSnapshotError",
    "load_required_data_snapshot_manifest",
    "resolve_frozen_required_data",
    "resolve_frozen_required_data_csv_bytes",
    "seal_required_data_snapshot",
]
