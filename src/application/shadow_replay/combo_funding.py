from __future__ import annotations

"""Project sealed Combo Funding Put decisions into an isolated dataset."""

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from src.application.candidate_evidence_history import (
    SUPPORTED,
    load_account_candidate_evidence,
)
from src.application.candidate_snapshot_manifest import (
    CANDIDATE_SNAPSHOT_MANIFEST_SCHEMA,
)
from src.application.combo_yield_candidate_snapshot import (
    COMBO_YIELD_CANDIDATE_SNAPSHOT_SCHEMA,
    project_combo_yield_funding_put_decisions,
)
from src.application.shadow_replay.common import (
    dataset_dir_from_arg,
    dataset_read_lock,
    dataset_write_lock,
    read_jsonl,
    refresh_dataset_manifest,
    safety_payload,
    text,
    validate_dataset_integrity,
    write_json,
    write_jsonl,
)


COMBO_FUNDING_PUT_FILE = "combo_owned_funding_put_decisions.v1.jsonl"
COMBO_FUNDING_PUT_RECEIPT_FILE = "combo_owned_funding_put_decisions.v1.source.json"
COMBO_FUNDING_PUT_ROW_SCHEMA = "shadow_combo_funding_put_decision.v1"
COMBO_FUNDING_PUT_RECEIPT_SCHEMA = "shadow_combo_funding_put_source.v2"


def prepare_combo_funding_puts(
    *,
    dataset: str | Path,
    source_run_id: str,
    source_runs_root: str | Path,
    write: bool = False,
) -> dict[str, Any]:
    """Project terminal Funding Put decisions from one manifest-bound SP+LC run."""

    dataset_dir = dataset_dir_from_arg(dataset)
    manifest = _load_object(dataset_dir / "manifest.json", label="Combo capture manifest")
    run_id = text(source_run_id)
    if not run_id:
        raise ValueError("source_run_id is required")
    runs_root = Path(source_runs_root).expanduser().resolve()
    if runs_root.name != "output_runs":
        raise ValueError("source_runs_root must name an output_runs directory")
    account = text(manifest.get("account")).lower()
    if not account:
        raise ValueError("Combo capture manifest account is missing")

    evidence = load_account_candidate_evidence(
        base=runs_root.parent,
        run_id=run_id,
        account=account,
        runs_root=runs_root,
    )
    classification = dict(evidence.classification)
    if classification.get("status") != SUPPORTED:
        raise ValueError(
            "Combo Funding Put source lacks strict sealed authority: "
            f"{classification.get('status')}:{classification.get('reason_code')}"
        )
    snapshot = evidence.owners.get("sp_lc")
    if not isinstance(snapshot, dict):
        raise ValueError("source run has no manifest-bound SP+LC candidate snapshot")
    if snapshot.get("schema_version") != COMBO_YIELD_CANDIDATE_SNAPSHOT_SCHEMA:
        raise ValueError("source SP+LC candidate snapshot schema mismatch")
    capture_symbols = _validate_capture_scope(manifest=manifest, snapshot=snapshot)

    candidate_manifest = dict(evidence.manifest or {})
    owner_entry = next(
        (
            dict(item)
            for item in candidate_manifest.get("owner_snapshots") or []
            if isinstance(item, Mapping) and item.get("candidate_owner") == "sp_lc"
        ),
        None,
    )
    if owner_entry is None:
        raise ValueError("candidate manifest SP+LC binding is missing")
    source_manifest_path = evidence.account_dir / "state" / "candidate_snapshot_manifest.v1.json"
    source_snapshot_path = evidence.account_dir / str(owner_entry["relpath"])
    if not source_manifest_path.is_file() or not source_snapshot_path.is_file():
        raise ValueError("bound Combo Funding Put source file is missing")

    rows = _project_rows(
        snapshot=snapshot,
        run_id=run_id,
        account=account,
        allowed_symbols=capture_symbols,
        candidate_manifest_content_sha256=text(candidate_manifest.get("content_sha256")),
    )
    output_path = dataset_dir / COMBO_FUNDING_PUT_FILE
    receipt_path = dataset_dir / COMBO_FUNDING_PUT_RECEIPT_FILE
    preview = {
        "schema_version": "shadow_combo_funding_put_preparation.v2",
        "dataset_dir": str(dataset_dir),
        "symbols": sorted(capture_symbols),
        "source_run_id": run_id,
        "source_account": account,
        "source_classification": classification,
        "output_path": str(output_path),
        "receipt_path": str(receipt_path),
        "row_count": len(rows),
        "accepted_row_count": sum(_accepted(row) for row in rows),
        "written": bool(write),
        "safety": safety_payload(writes_local_dataset=bool(write)),
    }
    if not write:
        return preview

    with dataset_write_lock(dataset_dir):
        validate_dataset_integrity(dataset_dir)
        current_manifest = _load_object(
            dataset_dir / "manifest.json",
            label="Combo capture manifest",
        )
        if current_manifest.get("dataset_id") != manifest.get("dataset_id"):
            raise ValueError("Combo capture manifest changed before publication")
        current_account = text(current_manifest.get("account")).lower()
        if current_account != account:
            raise ValueError("Combo capture account changed before publication")
        capture_symbols = _validate_capture_scope(
            manifest=current_manifest,
            snapshot=snapshot,
        )
        rows = _project_rows(
            snapshot=snapshot,
            run_id=run_id,
            account=account,
            allowed_symbols=capture_symbols,
            candidate_manifest_content_sha256=text(
                candidate_manifest.get("content_sha256")
            ),
        )
        receipt = {
            "schema_version": COMBO_FUNDING_PUT_RECEIPT_SCHEMA,
            "dataset_id": current_manifest.get("dataset_id"),
            "source_run_id": run_id,
            "source_account": account,
            "source_market": text(snapshot.get("market")).lower(),
            "source_symbols": sorted(capture_symbols),
            "candidate_manifest": {
                "schema_version": candidate_manifest.get("schema_version"),
                "content_sha256": candidate_manifest.get("content_sha256"),
                "file_sha256": _file_sha256(source_manifest_path),
            },
            "combo_snapshot": {
                "schema_version": snapshot.get("schema_version"),
                "content_sha256": snapshot.get("content_sha256"),
                "file_sha256": _file_sha256(source_snapshot_path),
                "manifest_file_sha256": owner_entry.get("sha256"),
            },
            "funding_put_jsonl_sha256": _jsonl_sha256(rows),
            "row_count": len(rows),
            "accepted_row_count": sum(_accepted(row) for row in rows),
            "projection": "manifest_bound_combo_v2_terminal_decisions",
        }
        _publish_projection(
            output_path=output_path,
            receipt_path=receipt_path,
            rows=rows,
            receipt=receipt,
        )
        current_manifest["combo_funding_put_facet"] = _projection_facet(
            output_path=output_path,
            receipt_path=receipt_path,
            receipt=receipt,
        )
        write_json(dataset_dir / "manifest.json", current_manifest)
        refreshed = refresh_dataset_manifest(dataset_dir)
        preview.update(
            {
                "symbols": sorted(capture_symbols),
                "row_count": len(rows),
                "accepted_row_count": sum(_accepted(row) for row in rows),
                "funding_put_jsonl_sha256": receipt["funding_put_jsonl_sha256"],
                "dataset_integrity": refreshed["integrity"],
            }
        )
    return preview


def validate_combo_funding_put_source(
    *,
    dataset: str | Path,
    funding_put_path: str | Path,
) -> dict[str, Any]:
    receipt, _rows, _source_sha256 = load_combo_funding_put_source(
        dataset=dataset,
        funding_put_path=funding_put_path,
    )
    return receipt


def load_combo_funding_put_source(
    *,
    dataset: str | Path,
    funding_put_path: str | Path,
) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
    """Load manifest-bound Funding Put decisions under one dataset read lock."""

    dataset_dir = dataset_dir_from_arg(dataset)
    source = Path(funding_put_path).expanduser().resolve()
    canonical = (dataset_dir / COMBO_FUNDING_PUT_FILE).resolve()
    if source != canonical:
        raise ValueError("Funding Put decisions must use the dataset-owned canonical JSONL")
    receipt_path = (dataset_dir / COMBO_FUNDING_PUT_RECEIPT_FILE).resolve()
    with dataset_read_lock(dataset_dir):
        if not source.is_file():
            raise ValueError(f"Combo Funding Put decision artifact is missing: {source}")
        validate_dataset_integrity(dataset_dir)
        manifest = _load_object(
            dataset_dir / "manifest.json",
            label="Combo capture manifest",
        )
        receipt = _load_object(receipt_path, label="Combo Funding Put source receipt")
        if receipt.get("schema_version") != COMBO_FUNDING_PUT_RECEIPT_SCHEMA:
            raise ValueError("Combo Funding Put source receipt schema mismatch")
        if receipt.get("dataset_id") != manifest.get("dataset_id"):
            raise ValueError("Combo Funding Put receipt dataset_id mismatch")
        _validate_receipt(receipt)
        expected_facet = _projection_facet(
            output_path=canonical,
            receipt_path=receipt_path,
            receipt=receipt,
        )
        if manifest.get("combo_funding_put_facet") != expected_facet:
            raise ValueError("Combo Funding Put manifest facet mismatch")
        rows = read_jsonl(source)
        _validate_rows(rows, receipt=receipt)
        source_sha256 = _file_sha256(source)
        if receipt.get("funding_put_jsonl_sha256") != source_sha256:
            raise ValueError("Combo Funding Put decision artifact hash mismatch")
        validate_dataset_integrity(dataset_dir)
        return receipt, rows, source_sha256


def _validate_receipt(receipt: Mapping[str, Any]) -> None:
    if not text(receipt.get("dataset_id")):
        raise ValueError("Combo Funding Put receipt dataset_id is missing")
    if not text(receipt.get("source_run_id")) or not text(
        receipt.get("source_account")
    ):
        raise ValueError("Combo Funding Put receipt source identity is missing")
    if not text(receipt.get("source_market")) or not isinstance(
        receipt.get("source_symbols"), list
    ):
        raise ValueError("Combo Funding Put receipt source scope is invalid")
    candidate_manifest = receipt.get("candidate_manifest")
    if not isinstance(candidate_manifest, Mapping) or candidate_manifest.get(
        "schema_version"
    ) != CANDIDATE_SNAPSHOT_MANIFEST_SCHEMA:
        raise ValueError("Combo Funding Put receipt candidate manifest binding is invalid")
    combo_snapshot = receipt.get("combo_snapshot")
    if not isinstance(combo_snapshot, Mapping) or combo_snapshot.get(
        "schema_version"
    ) != COMBO_YIELD_CANDIDATE_SNAPSHOT_SCHEMA:
        raise ValueError("Combo Funding Put receipt Combo snapshot binding is invalid")
    for binding, fields in (
        (candidate_manifest, ("content_sha256", "file_sha256")),
        (
            combo_snapshot,
            ("content_sha256", "file_sha256", "manifest_file_sha256"),
        ),
    ):
        if any(not _is_sha256(binding.get(field)) for field in fields):
            raise ValueError("Combo Funding Put receipt source hash binding is invalid")
    if not _is_sha256(receipt.get("funding_put_jsonl_sha256")):
        raise ValueError("Combo Funding Put receipt projection hash is invalid")
    for field in ("row_count", "accepted_row_count"):
        value = receipt.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("Combo Funding Put receipt counts are invalid")
    if receipt.get("accepted_row_count", 0) > receipt.get("row_count", 0):
        raise ValueError("Combo Funding Put receipt counts are invalid")
    if receipt.get("projection") != "manifest_bound_combo_v2_terminal_decisions":
        raise ValueError("Combo Funding Put receipt projection is invalid")


def _validate_capture_scope(
    *,
    manifest: Mapping[str, Any],
    snapshot: Mapping[str, Any],
) -> set[str]:
    capture_market = text(manifest.get("market")).lower()
    source_market = text(snapshot.get("market")).lower()
    if not capture_market or capture_market != source_market:
        raise ValueError("Combo capture and source run market mismatch")
    capture_symbols = {
        text(value).upper() for value in manifest.get("symbols") or [] if text(value)
    }
    source_symbols = {
        text(item.get("symbol")).upper()
        for item in snapshot.get("scope_results") or []
        if isinstance(item, Mapping)
    }
    missing = sorted(capture_symbols - source_symbols)
    if missing:
        raise ValueError(
            "source SP+LC snapshot does not cover capture symbol(s): " + ", ".join(missing)
        )
    return capture_symbols


def _project_rows(
    *,
    snapshot: Mapping[str, Any],
    run_id: str,
    account: str,
    allowed_symbols: set[str],
    candidate_manifest_content_sha256: str,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for decision in project_combo_yield_funding_put_decisions(snapshot):
        opening = dict(decision.get("opening_decision") or {})
        normalized = dict(decision.get("normalized_input") or {})
        if text(normalized.get("symbol")).upper() not in allowed_symbols:
            continue
        rows.append(
            {
                "schema_version": COMBO_FUNDING_PUT_ROW_SCHEMA,
                "source_run_id": run_id,
                "source_account": account,
                "source_candidate_manifest_content_sha256": candidate_manifest_content_sha256,
                "source_combo_snapshot_content_sha256": snapshot.get("content_sha256"),
                "decision_id": decision.get("decision_id"),
                "accepted": bool(opening.get("accepted")),
                "normalized_input": normalized,
                "opening_decision": opening,
            }
        )
    return sorted(rows, key=lambda row: text(row.get("decision_id")))


def _validate_rows(rows: list[dict[str, Any]], *, receipt: Mapping[str, Any]) -> None:
    seen: set[str] = set()
    expected_manifest_hash = text(
        (receipt.get("candidate_manifest") or {}).get("content_sha256")
    )
    expected_snapshot_hash = text(
        (receipt.get("combo_snapshot") or {}).get("content_sha256")
    )
    for row in rows:
        if row.get("schema_version") != COMBO_FUNDING_PUT_ROW_SCHEMA:
            raise ValueError("Combo Funding Put decision row schema mismatch")
        decision_id = text(row.get("decision_id"))
        if not decision_id or decision_id in seen:
            raise ValueError("Combo Funding Put decision identity is missing or duplicated")
        seen.add(decision_id)
        if row.get("source_run_id") != receipt.get("source_run_id") or row.get(
            "source_account"
        ) != receipt.get("source_account"):
            raise ValueError("Combo Funding Put decision source identity mismatch")
        if row.get("source_candidate_manifest_content_sha256") != expected_manifest_hash:
            raise ValueError("Combo Funding Put decision manifest binding mismatch")
        if row.get("source_combo_snapshot_content_sha256") != expected_snapshot_hash:
            raise ValueError("Combo Funding Put decision snapshot binding mismatch")
        opening = row.get("opening_decision")
        normalized = row.get("normalized_input")
        if not isinstance(opening, dict) or not isinstance(normalized, dict):
            raise ValueError("Combo Funding Put decision payload is invalid")
        if (
            not isinstance(row.get("accepted"), bool)
            or row["accepted"] != bool(opening.get("accepted"))
        ):
            raise ValueError("Combo Funding Put decision terminal status mismatch")
    if len(rows) != receipt.get("row_count"):
        raise ValueError("Combo Funding Put decision row count mismatch")
    if sum(_accepted(row) for row in rows) != receipt.get("accepted_row_count"):
        raise ValueError("Combo Funding Put accepted row count mismatch")


def _publish_projection(
    *,
    output_path: Path,
    receipt_path: Path,
    rows: list[dict[str, Any]],
    receipt: dict[str, Any],
) -> None:
    if output_path.exists():
        existing_rows = read_jsonl(output_path)
        if existing_rows != rows:
            raise ValueError("Combo Funding Put decision artifact conflicts with existing bytes")
    else:
        write_jsonl(output_path, rows)
    if _file_sha256(output_path) != receipt["funding_put_jsonl_sha256"]:
        raise ValueError("Combo Funding Put projection hash mismatch after publication")
    if receipt_path.exists():
        if _load_object(receipt_path, label="Combo Funding Put source receipt") != receipt:
            raise ValueError("Combo Funding Put source receipt conflicts with existing receipt")
    else:
        write_json(receipt_path, receipt)


def _projection_facet(
    *,
    output_path: Path,
    receipt_path: Path,
    receipt: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        **dict(receipt),
        "output_path": str(output_path.resolve()),
        "receipt_path": str(receipt_path.resolve()),
    }


def _accepted(row: Mapping[str, Any]) -> int:
    return int(row.get("accepted") is True)


def _jsonl_sha256(rows: list[dict[str, Any]]) -> str:
    payload = "".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n"
        for row in rows
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _load_object(path: Path, *, label: str) -> dict[str, Any]:
    if not path.is_file():
        raise ValueError(f"{label} is missing: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is unreadable: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must be a JSON object")
    return payload


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_sha256(value: Any) -> bool:
    candidate = text(value).lower()
    return len(candidate) == 64 and all(char in "0123456789abcdef" for char in candidate)


__all__ = [
    "COMBO_FUNDING_PUT_FILE",
    "COMBO_FUNDING_PUT_RECEIPT_FILE",
    "COMBO_FUNDING_PUT_RECEIPT_SCHEMA",
    "COMBO_FUNDING_PUT_ROW_SCHEMA",
    "load_combo_funding_put_source",
    "prepare_combo_funding_puts",
    "validate_combo_funding_put_source",
]
