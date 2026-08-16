from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
from typing import Any

from src.application.ledger.lifecycle_migration import MIGRATION_SCHEMA
from src.application.ledger.notification_outbox import canonical_payload_hash
from src.application.research import historical_cleanup as module


NOW = datetime(2030, 2, 1, tzinfo=timezone.utc)


def _runtime(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "runtime"
    ledger = root / "output_shared/state/option_positions.sqlite3"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    if not ledger.exists():
        with sqlite3.connect(ledger) as conn:
            conn.execute("CREATE TABLE proof_row(id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
            conn.execute("INSERT INTO proof_row VALUES(1, 'same logical state')")
            conn.execute("CREATE TABLE trade_lifecycle_cases(case_id TEXT PRIMARY KEY,account TEXT NOT NULL)")
            conn.execute("INSERT INTO trade_lifecycle_cases VALUES('case-1', 'lx')")
    return root, ledger


def _inventory(path: Path, *, needs_review: bool = False) -> Path:
    rows = [
        {
            "target_key": "lifecycle:case-1",
            "kind": "lifecycle_case",
            "selected": False,
            "mapping_status": "needs_review" if needs_review else "exact",
            "review_reason_codes": ["target_manifest_missing"] if needs_review else [],
            "case_id": "case-1",
            "account": "lx",
        }
    ]
    body = {"schema_version": MIGRATION_SCHEMA, "rows": rows}
    path.write_text(
        json.dumps({**body, "manifest_hash": canonical_payload_hash(body)}),
        encoding="utf-8",
    )
    return path


def _patch_inputs(monkeypatch) -> None:
    monkeypatch.setattr(
        module,
        "quality_hot_path_cutover_preview",
        lambda _path: {
            "status": "eligible",
            "evidence_sha256": "a" * 64,
            "consumer_inventory_sha256": "b" * 64,
            "eligible_market_day_counts": {"hk": 14, "us": 14},
        },
    )
    monkeypatch.setattr(
        module,
        "position_projection_migration_status",
        lambda _path: {
            "readiness": "ready",
            "reasons": [],
            "checkpoint_mode": "enabled",
            "source_generation": 7,
            "head_count": 1,
            "trusted_head_count": 1,
            "checkpoint_count": 1,
            "trusted_checkpoint_count": 1,
            "checkpoint_state_bytes": 100,
            "checkpoint_max_state_bytes": 100,
            "checkpoint_k_within_bound": True,
            "checkpoint_space_within_bound": True,
            "last_full_verified_source_generation": 7,
            "fingerprint_scope": {"rows": 1, "fields_json_bytes": 20},
        },
    )
    monkeypatch.setattr(
        module,
        "current_decision_projection_migration_status",
        lambda _path: {
            "status": "clean",
            "readiness": "ready",
            "readiness_reasons": [],
            "account_count": 1,
            "repair": {
                "projection_missing_count": 0,
                "projection_dirty_count": 0,
                "projection_mismatch_count": 0,
            },
            "shadow_status": "eligible",
            "mixed_version_guard_status": "active",
        },
    )
    monkeypatch.setattr(
        module,
        "collect_storage_runtime_baseline",
        lambda **_kwargs: {
            "schema_version": "storage_runtime_baseline.v1",
            "status": "complete",
            "sqlite": {
                "page": {
                    "page_size_bytes": 4096,
                    "page_count": 10,
                    "freelist_count": 2,
                }
            },
            "research_storage": {"growth": {"status": "complete"}},
            "thresholds": {
                "status": "ok",
                "forecast_90d_free_bytes": 100_000_000,
                "warning_reasons": [],
                "critical_reasons": [],
                "operator_decision_required": False,
            },
        },
    )
    monkeypatch.setattr(
        module,
        "preview_scan_blob_gc",
        lambda **_kwargs: {
            "deletion_allowed": True,
            "plan_sha256": "c" * 64,
            "summary": {
                "candidate_blob_count": 1,
                "candidate_bytes": 123,
            },
            "blockers": [],
            "candidates": [
                {
                    "blob_sha256": "d" * 64,
                    "blob_relpath": "output_shared/blobs/sha256/dd/" + "d" * 64 + ".json.gz",
                    "compressed_size_bytes": 123,
                    "age_hours": 48.0,
                }
            ],
        },
    )


def _preview(monkeypatch, tmp_path: Path, *, proof: Path | None = None, needs_review: bool = False) -> dict[str, Any]:
    _patch_inputs(monkeypatch)
    root, ledger = _runtime(tmp_path)
    inventory = _inventory(tmp_path / "lifecycle.json", needs_review=needs_review)
    (tmp_path / "quality.json").write_text("{}", encoding="utf-8")
    return module.build_historical_cleanup_preview(
        repo_root=Path.cwd(),
        runtime_root=root,
        ledger_sqlite=ledger,
        lifecycle_inventory=inventory,
        quality_cutover_evidence=tmp_path / "quality.json",
        backup_proof=proof,
        history_reports=[tmp_path / "old-baseline.json"],
        now_fn=lambda: NOW,
    )


def _write_backup_proof(
    path: Path,
    *,
    ledger: Path,
    backup: Path,
    bindings: dict[str, Any],
) -> Path:
    raw = backup.read_bytes()
    path.write_text(
        json.dumps(
            {
                "schema_version": module.HISTORICAL_CLEANUP_BACKUP_PROOF_SCHEMA,
                "ledger_sqlite": str(ledger.resolve()),
                "backup_path": str(backup.resolve()),
                "backup_sha256": hashlib.sha256(raw).hexdigest(),
                "backup_size_bytes": len(raw),
                "created_at_utc": "2030-01-31T00:00:00Z",
                "restore_verified": True,
                **bindings,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return path


def test_cleanup_preview_is_blocked_without_backup_and_never_mutates(monkeypatch, tmp_path: Path) -> None:
    result = _preview(monkeypatch, tmp_path)

    assert result["status"] == "not_ready"
    assert result["preview_ready"] is False
    assert result["candidates"] == []
    assert result["safety"] == {
        "read_only": True,
        "mutation_operations": 0,
        "automatic_actions": [],
        "actual_cleanup_authorized": False,
    }
    assert {item["reason"] for item in result["blockers"]} == {"backup_proof_missing"}
    assert {item["class"] for item in result["excluded_cleanup_classes"]} == {
        "required_data_legacy_csv_and_base64",
        "ledger_history_rows",
        "research_generation_roots",
    }


def test_cleanup_preview_verifies_backup_and_has_stable_plan_hash(monkeypatch, tmp_path: Path) -> None:
    first = _preview(monkeypatch, tmp_path / "first")
    first_root = tmp_path / "first/runtime"
    ledger = first_root / "output_shared/state/option_positions.sqlite3"
    backup = tmp_path / "first/backup.sqlite3"
    shutil.copyfile(ledger, backup)
    proof = _write_backup_proof(
        tmp_path / "first/backup-proof.json",
        ledger=ledger,
        backup=backup,
        bindings=first["expected_backup_bindings"],
    )
    ledger_before = ledger.read_bytes()
    backup_before = backup.read_bytes()

    ready = _preview(monkeypatch, tmp_path / "first", proof=proof)
    later = module.build_historical_cleanup_preview(
        repo_root=Path.cwd(),
        runtime_root=first_root,
        ledger_sqlite=ledger,
        lifecycle_inventory=tmp_path / "first/lifecycle.json",
        quality_cutover_evidence=tmp_path / "first/quality.json",
        backup_proof=proof,
        history_reports=[tmp_path / "first/old-baseline.json"],
        now_fn=lambda: datetime(2030, 2, 1, 1, tzinfo=timezone.utc),
    )

    assert ready["status"] == "ready_for_authorization"
    assert ready["preview_ready"] is True
    assert ready["authorization_required"] is True
    assert ready["gates"]["backup_restore"]["status"] == "pass"
    assert [item["kind"] for item in ready["candidates"]] == [
        "scan_blob_delete",
        "sqlite_vacuum",
    ]
    assert ready["summary"] == {
        "candidate_count": 2,
        "candidate_bytes": 8315,
        "blocker_count": 0,
    }
    assert ready["plan_sha256"] == later["plan_sha256"]
    assert ready["observed_at_utc"] != later["observed_at_utc"]
    assert ledger.read_bytes() == ledger_before
    assert backup.read_bytes() == backup_before


def test_cleanup_preview_rejects_backup_with_different_logical_state(monkeypatch, tmp_path: Path) -> None:
    first = _preview(monkeypatch, tmp_path)
    ledger = tmp_path / "runtime/output_shared/state/option_positions.sqlite3"
    backup = tmp_path / "backup.sqlite3"
    shutil.copyfile(ledger, backup)
    with sqlite3.connect(backup) as conn:
        conn.execute("INSERT INTO proof_row VALUES(2, 'different')")
    proof = _write_backup_proof(
        tmp_path / "backup-proof.json",
        ledger=ledger,
        backup=backup,
        bindings=first["expected_backup_bindings"],
    )

    result = _preview(monkeypatch, tmp_path, proof=proof)

    assert result["status"] == "not_ready"
    assert result["candidates"] == []
    assert result["gates"]["backup_restore"] == {
        "status": "blocked",
        "reason": "backup_proof_invalid",
        "error_type": "ValueError",
    }


def test_cleanup_preview_blocks_lifecycle_rows_needing_review(monkeypatch, tmp_path: Path) -> None:
    result = _preview(monkeypatch, tmp_path, needs_review=True)

    assert result["gates"]["lifecycle_reconciliation"]["review_count"] == 1
    assert {item["reason"] for item in result["blockers"]} == {
        "backup_proof_missing",
        "lifecycle_targets_need_review",
    }


def test_cleanup_preview_blocks_lifecycle_inventory_from_another_ledger(monkeypatch, tmp_path: Path) -> None:
    _patch_inputs(monkeypatch)
    root, ledger = _runtime(tmp_path)
    inventory = _inventory(tmp_path / "lifecycle.json")
    (tmp_path / "quality.json").write_text("{}", encoding="utf-8")
    payload = json.loads(inventory.read_text(encoding="utf-8"))
    payload["rows"][0]["case_id"] = "case-other"
    payload["rows"][0]["target_key"] = "lifecycle:case-other"
    body = {"schema_version": MIGRATION_SCHEMA, "rows": payload["rows"]}
    inventory.write_text(
        json.dumps({**body, "manifest_hash": canonical_payload_hash(body)}),
        encoding="utf-8",
    )

    result = module.build_historical_cleanup_preview(
        repo_root=Path.cwd(),
        runtime_root=root,
        ledger_sqlite=ledger,
        lifecycle_inventory=inventory,
        quality_cutover_evidence=tmp_path / "quality.json",
        history_reports=[tmp_path / "old-baseline.json"],
        now_fn=lambda: NOW,
    )

    assert result["gates"]["lifecycle_reconciliation"]["case_coverage_matches"] is False
    assert "lifecycle_inventory_ledger_mismatch" in {item["reason"] for item in result["blockers"]}
