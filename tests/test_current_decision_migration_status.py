from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from domain.domain.ledger import ContractKey, TradeEvent
from src.application.ledger import api, current_decision_migration as migration
from src.application.ledger.position_projection_runtime import run_position_projection_forced_full
from src.application.ledger.repository import SQLiteOptionPositionsRepository
from src.application.research import historical_cleanup as cleanup


NOW = datetime(2030, 2, 1, tzinfo=timezone.utc)
NOW_MS = 1_896_134_400_000
INDEX = "idx_assigned_stock_events_account_time"
TRIGGER = "trg_current_decision_lifecycle_evidence_insert"
COUNTERS = (
    "assigned_account_mismatch_count",
    "case_target_mismatch_count",
    "case_fact_mismatch_count",
    "evidence_count_mismatch_count",
    "generation_missing_count",
    "projection_missing_count",
    "projection_dirty_count",
    "projection_mismatch_count",
)
OBSERVATIONS: list[dict[str, Any]] = []


def _clean_repair() -> dict[str, Any]:
    return {
        **dict.fromkeys(COUNTERS, 0),
        "missing_indexes": [],
        "missing_triggers": [],
        "position_schema_cookie_mismatch": False,
    }


POLICY_CASES = [
    {"name": "healthy", "repair": {}, "expected": "clean"},
    *[
        {
            "name": field,
            "repair": {field: 1},
            "expected": "mismatch" if field in {
                "projection_missing_count", "projection_mismatch_count",
            } else "dirty",
        }
        for field in COUNTERS
    ],
    {"name": "missing_indexes", "repair": {"missing_indexes": [INDEX]}, "expected": "dirty"},
    {"name": "missing_triggers", "repair": {"missing_triggers": [TRIGGER]}, "expected": "dirty"},
    {"name": "schema_cookie", "repair": {"position_schema_cookie_mismatch": True}, "expected": "dirty"},
    {
        "name": "all_repairs",
        "repair": {
            **dict.fromkeys(COUNTERS, 1),
            "missing_indexes": [INDEX],
            "missing_triggers": [TRIGGER],
            "position_schema_cookie_mismatch": True,
        },
        "expected": "dirty",
    },
    {
        "name": "partial_missing_before_schema",
        "repair": {"projection_missing_count": 1, "case_fact_mismatch_count": 1, "missing_indexes": [INDEX]},
        "expected": "mismatch",
    },
    {
        "name": "mismatch_before_schema",
        "repair": {"projection_mismatch_count": 1, "case_target_mismatch_count": 1, "missing_triggers": [TRIGGER]},
        "expected": "mismatch",
    },
    {
        "name": "dirty_before_mismatch",
        "repair": {"projection_dirty_count": 1, "projection_mismatch_count": 1},
        "expected": "dirty",
    },
    {
        "name": "all_missing_before_dirty",
        "repair": {"projection_missing_count": 2, "projection_dirty_count": 1, "case_fact_mismatch_count": 1, "missing_indexes": [INDEX]},
        "expected": "absent",
    },
    {"name": "empty_accounts_clean", "repair": {}, "accounts": [], "expected": "absent"},
    {
        "name": "empty_accounts_repair",
        "repair": {"case_fact_mismatch_count": 1, "missing_triggers": [TRIGGER]},
        "accounts": [],
        "expected": "absent",
    },
    {"name": "inventory_not_ready", "not_ready": True, "expected": "dirty"},
]


@pytest.mark.parametrize("case_name", [case["name"] for case in POLICY_CASES])
def test_status_uses_complete_repair_and_preserves_precedence(monkeypatch, case_name: str) -> None:
    case = next(row for row in POLICY_CASES if row["name"] == case_name)
    repair = {**_clean_repair(), **deepcopy(case.get("repair", {}))}
    inventory = {
        "readiness": "ready",
        "readiness_reasons": [],
        "accounts": case.get("accounts", [{"account": "lx"}, {"account": "sy"}]),
        "repair": repair,
        "manifest_hash": "1" * 64,
        "store_identity": {"identity_sha256": "2" * 64},
        "mixed_version_guard_status": "active",
    }
    if case.get("not_ready"):
        inventory.update(
            readiness="not_ready",
            readiness_reasons=["required_tables_missing"],
            accounts=[],
            repair={},
        )
    original = deepcopy(inventory)
    monkeypatch.setattr(migration, "build_current_decision_projection_migration_inventory", lambda *_args, **_kwargs: inventory)
    result = api.current_decision_projection_migration_status("unused-policy-fixture.sqlite3", now_ms=NOW_MS)
    OBSERVATIONS.append({"case": case_name, "kind": "policy", "inventory": original, "status": result})
    assert result["status"] == case["expected"], (case_name, result)
    ready = "ready" if case["expected"] == "clean" else "not_ready"
    shadow = "eligible" if case["expected"] == "clean" else "not_ready"
    assert (result["readiness"], result["shadow_status"]) == (ready, shadow)
    assert result["repair"] == original["repair"]
    assert result["readiness_reasons"] == original["readiness_reasons"]
    assert inventory == original


@pytest.mark.parametrize("error_name", ["database", "value"])
def test_status_preserves_inventory_errors_without_apply(monkeypatch, error_name: str) -> None:
    error_type = {"database": sqlite3.DatabaseError, "value": ValueError}[error_name]
    calls: list[str] = []

    def unavailable(*_args, **_kwargs):
        raise error_type("synthetic inventory unavailable")

    def unexpected_apply(*_args, **_kwargs):
        calls.append("apply")
        raise AssertionError("status must not apply migration")

    monkeypatch.setattr(migration, "build_current_decision_projection_migration_inventory", unavailable)
    monkeypatch.setattr(migration, "apply_current_decision_projection_migration", unexpected_apply)
    monkeypatch.setattr(api, "apply_current_decision_projection_migration", unexpected_apply)
    with pytest.raises(error_type, match="synthetic inventory unavailable"):
        api.current_decision_projection_migration_status("unused-policy-fixture.sqlite3", now_ms=NOW_MS)
    assert calls == []
    OBSERVATIONS.append({"case": error_name, "kind": "read_error", "error_type": error_type.__name__, "apply_calls": calls})


@pytest.fixture
def wal_keeper(tmp_path: Path, request: pytest.FixtureRequest) -> sqlite3.Connection:
    # Keep WAL identity stable throughout the measured read and replay windows.
    keeper = sqlite3.connect(tmp_path / "ledger.sqlite3")
    request.addfinalizer(keeper.close)
    assert keeper.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
    return keeper


def _fixture(tmp_path: Path, monkeypatch, *, accounts: tuple[str, ...] = ("lx",)) -> SQLiteOptionPositionsRepository:
    # Cleanup does not pass now_ms to status; bind the real owner's wall clock too.
    monkeypatch.setattr(migration.time, "time", lambda: NOW_MS / 1000)
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    if accounts:
        events = [
            TradeEvent(
                event_id=f"issue655-open-{account}",
                event_type="open",
                event_time_ms=NOW_MS - 1000,
                contract_key=ContractKey.from_values(
                    broker="futu", account=account, underlying_symbol="NVDA",
                    option_type="put", strike=100, expiration_ymd="2030-12-20",
                ),
                contracts=1, price=2, currency="USD", source="test",
                multiplier=100, lot_id=f"issue655-lot-{account}", raw_payload={"side": "sell"},
            )
            for account in accounts
        ]
        run_position_projection_forced_full(repo, events, seed_checkpoint=False)
        manifest = api.build_current_decision_projection_migration_inventory(repo.db_path, now_ms=NOW_MS)
        assert manifest["readiness"] == "ready", manifest
        api.apply_current_decision_projection_migration(repo.db_path, manifest)
        status = api.current_decision_projection_migration_status(repo.db_path, now_ms=NOW_MS)
        assert status["status"] == "clean", status
        assert status["repair"] == _clean_repair()
        for account in accounts:
            assert api.read_current_decision_projection(repo, account=account, now_ms=NOW_MS)["status"] == "trusted"
    return repo


def _snapshot(path: Path) -> dict[str, Any]:
    conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    try:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        schema = conn.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name").fetchall()
        rows = {}
        for kind, name, _table, _sql in schema:
            if kind == "table":
                quoted = str(name).replace('"', '""')
                rows[str(name)] = sorted(conn.execute(f'SELECT * FROM "{quoted}"').fetchall(), key=repr)
        logical = hashlib.sha256(repr((schema, rows)).encode()).hexdigest()
        cookie = conn.execute("PRAGMA schema_version").fetchone()[0]
        stored_cookie = conn.execute("SELECT sqlite_schema_cookie FROM position_projection_source_state WHERE singleton_id=1").fetchone()
    finally:
        conn.close()
    physical = {}
    for suffix in ("", "-wal"):
        item = Path(f"{path}{suffix}")
        if item.exists():
            info = item.stat()
            physical[suffix] = {
                "size": info.st_size, "mtime_ns": info.st_mtime_ns,
                "inode": info.st_ino, "sha256": hashlib.sha256(item.read_bytes()).hexdigest(),
            }
        else:
            physical[suffix] = None
    return {"logical_sha256": logical, "schema": schema, "row_counts": {key: len(value) for key, value in rows.items()}, "schema_cookie": cookie, "stored_schema_cookie": stored_cookie[0] if stored_cookie else None, "persistent": physical}


def _observe(repo: SQLiteOptionPositionsRepository, *, accounts: tuple[str, ...] = ("lx",)) -> dict[str, Any]:
    return {
        "inventory": api.build_current_decision_projection_migration_inventory(repo.db_path, now_ms=NOW_MS),
        "status": api.current_decision_projection_migration_status(repo.db_path, now_ms=NOW_MS),
        "verify": api.verify_current_decision_projection_migration(repo.db_path, now_ms=NOW_MS),
        "readers": {account: api.read_current_decision_projection(repo, account=account, now_ms=NOW_MS) for account in accounts},
    }


def _cleanup_preview(repo: SQLiteOptionPositionsRepository, monkeypatch) -> dict[str, Any]:
    # Only unrelated gates/collectors are simulated; current status and DB reads are real.
    for name in ("_quality_gate", "_position_gate", "_lifecycle_gate", "_backup_gate"):
        monkeypatch.setattr(cleanup, name, lambda *_args, **_kwargs: {"status": "pass", "reason": "isolated_fixture"})
    monkeypatch.setattr(cleanup, "collect_storage_runtime_baseline", lambda **_kwargs: {
        "status": "complete", "sqlite": {"page": {"page_size_bytes": 4096, "freelist_count": 0}},
        "research_storage": {"growth": {"status": "complete"}},
        "thresholds": {"status": "ok", "forecast_90d_free_bytes": 100_000_000},
    })
    monkeypatch.setattr(cleanup, "preview_scan_blob_gc", lambda **_kwargs: {
        "deletion_allowed": True, "plan_sha256": "3" * 64,
        "summary": {"candidate_blob_count": 1, "candidate_bytes": 123}, "blockers": [],
        "candidates": [{"blob_sha256": "4" * 64, "blob_relpath": "synthetic-uncreated-candidate.json.gz", "compressed_size_bytes": 123, "age_hours": 48.0}],
    })
    assert cleanup.current_decision_projection_migration_status is api.current_decision_projection_migration_status
    return cleanup.build_historical_cleanup_preview(
        repo_root=Path(__file__).resolve().parents[1], runtime_root=repo.db_path.parent,
        ledger_sqlite=repo.db_path, lifecycle_inventory=repo.db_path.parent / "unused-lifecycle.json",
        quality_cutover_evidence=None, backup_proof=None, now_fn=lambda: NOW,
    )


@pytest.mark.parametrize("defect", ["healthy", "schema-cookie", "missing-index", "missing-trigger"])
def test_schema_repairs_block_status_and_public_cleanup(tmp_path: Path, monkeypatch, defect: str, wal_keeper: sqlite3.Connection) -> None:
    repo = _fixture(tmp_path, monkeypatch)
    statements = {
        "schema-cookie": "CREATE TABLE issue655_schema_drift(marker INTEGER)",
        "missing-index": f"DROP INDEX {INDEX}",
        "missing-trigger": f"DROP TRIGGER {TRIGGER}",
    }
    if defect in statements:
        conn = sqlite3.connect(repo.db_path)
        try:
            conn.execute(statements[defect])
            conn.commit()
        finally:
            conn.close()
    before = _snapshot(repo.db_path)
    observed = _observe(repo)
    preview = _cleanup_preview(repo, monkeypatch)
    after = _snapshot(repo.db_path)
    OBSERVATIONS.append({"case": defect, "kind": "schema", "phase": "before_repair", "path": str(repo.db_path), "before": before, "after": after, **observed, "cleanup": preview})
    assert before == after, "read surfaces changed retained DB/WAL/schema/logical state"
    inventory, status, verify = observed["inventory"], observed["status"], observed["verify"]
    repair = inventory["repair"]
    assert inventory["readiness"] == "ready", inventory
    assert inventory["readiness_reasons"] == []
    assert all(repair[field] == 0 for field in COUNTERS), repair
    assert repair["missing_indexes"] == ([INDEX] if defect == "missing-index" else [])
    assert repair["missing_triggers"] == ([TRIGGER] if defect == "missing-trigger" else [])
    assert repair["position_schema_cookie_mismatch"] is (defect != "healthy")
    assert status["repair"] == repair
    assert status["readiness_reasons"] == inventory["readiness_reasons"]
    assert status["mixed_version_guard_status"] == "active"
    assert verify["status"] == "valid" and verify["comparisons"] == [{"account": "lx", "status": "matched"}]
    gate = preview["gates"]["current_decision_projection"]
    assert gate["details"]["repair"] == repair
    assert preview["safety"] == {"read_only": True, "mutation_operations": 0, "automatic_actions": [], "actual_cleanup_authorized": False}
    expected = "clean" if defect == "healthy" else "dirty"
    assert status["status"] == expected, (defect, "unexpected migration status", status)
    assert status["readiness"] == ("ready" if defect == "healthy" else "not_ready")
    assert status["shadow_status"] == ("eligible" if defect == "healthy" else "not_ready")
    assert observed["readers"]["lx"]["status"] == ("trusted" if defect == "healthy" else "data_unavailable")
    assert preview["preview_ready"] is (defect == "healthy")
    assert gate["status"] == ("pass" if defect == "healthy" else "blocked")
    assert gate["reason"] == ("current_decision_projection_reconciled" if defect == "healthy" else "current_decision_projection_not_ready")
    if defect == "healthy":
        assert preview["summary"]["candidate_count"] == 1
        return
    assert preview["candidates"] == []
    assert preview["blockers"] == [{"gate": "current_decision_projection", "reason": "current_decision_projection_not_ready"}]

    applied = api.apply_current_decision_projection_migration(repo.db_path, inventory)
    repair_before = _snapshot(repo.db_path)
    repaired = _observe(repo)
    repaired_preview = _cleanup_preview(repo, monkeypatch)
    repair_after = _snapshot(repo.db_path)
    OBSERVATIONS.append({"case": defect, "kind": "schema", "phase": "after_repair", "applied": applied, "before": repair_before, "after": repair_after, **repaired, "cleanup": repaired_preview})
    assert applied["write_applied"] is True
    assert applied["state"] == _clean_repair()
    assert repair_before == repair_after
    assert INDEX in {row[1] for row in repair_after["schema"] if row[0] == "index"}
    assert TRIGGER in {row[1] for row in repair_after["schema"] if row[0] == "trigger"}
    assert repair_after["stored_schema_cookie"] == repair_after["schema_cookie"]
    assert repaired["status"]["repair"] == _clean_repair()
    assert repaired["status"]["status"] == "clean"
    assert repaired["status"]["readiness"] == "ready"
    assert repaired["status"]["shadow_status"] == "eligible"
    assert repaired["verify"]["status"] == "valid"
    assert repaired["readers"]["lx"]["status"] == "trusted"
    assert repaired_preview["preview_ready"] is True
    if defect == "schema-cookie":
        assert repair_after["row_counts"]["issue655_schema_drift"] == 0
    replay = api.apply_current_decision_projection_migration(repo.db_path, inventory)
    replay_after = _snapshot(repo.db_path)
    OBSERVATIONS.append({"case": defect, "kind": "replay", "receipt": replay, "before": repair_after, "after": replay_after})
    assert replay["write_applied"] is False
    assert replay_after == repair_after, "no-op replay changed retained persistent state"


@pytest.mark.parametrize("control", ["generation-missing", "projection-absent", "projection-partial", "projection-dirty", "empty-account"])
def test_status_preserves_existing_sqlite_states(tmp_path: Path, monkeypatch, control: str, wal_keeper: sqlite3.Connection) -> None:
    accounts = () if control == "empty-account" else ("lx", "sy") if control == "projection-partial" else ("lx",)
    repo = _fixture(tmp_path, monkeypatch, accounts=accounts)
    statements = {
        "generation-missing": "DELETE FROM current_decision_input_generations WHERE account='lx'",
        "projection-absent": "DELETE FROM current_decision_projections WHERE account='lx'",
        "projection-partial": "DELETE FROM current_decision_projections WHERE account='sy'",
        "projection-dirty": "UPDATE current_decision_input_generations SET generation=generation+1 WHERE account='lx'",
    }
    if control in statements:
        conn = sqlite3.connect(repo.db_path)
        try:
            conn.execute(statements[control])
            conn.commit()
        finally:
            conn.close()
    before = _snapshot(repo.db_path)
    observed = _observe(repo, accounts=accounts)
    after = _snapshot(repo.db_path)
    OBSERVATIONS.append({"case": control, "kind": "state_control", "path": str(repo.db_path), "before": before, "after": after, **observed})
    assert before == after
    status, inventory, verify = observed["status"], observed["inventory"], observed["verify"]
    expected = {"generation-missing": "dirty", "projection-absent": "absent", "projection-partial": "mismatch", "projection-dirty": "dirty", "empty-account": "absent"}[control]
    assert status["status"] == expected, observed
    assert (status["readiness"], status["shadow_status"]) == ("not_ready", "not_ready")
    assert status["repair"] == inventory["repair"]
    assert inventory["readiness"] == "ready", inventory
    if control == "generation-missing":
        assert status["repair"]["generation_missing_count"] == 1
        assert status["repair"]["projection_dirty_count"] == 1
        assert observed["readers"]["lx"]["status"] == "data_unavailable"
    elif control == "projection-absent":
        assert status["repair"]["projection_missing_count"] == 1
        assert verify["status"] == "valid"
        assert verify["comparisons"] == [{"account": "lx", "status": "proposed"}]
        assert observed["readers"]["lx"]["status"] == "absent"
    elif control == "projection-partial":
        assert status["account_count"] == 2 and status["repair"]["projection_missing_count"] == 1
        assert observed["readers"]["lx"]["status"] == "trusted"
        assert observed["readers"]["sy"]["status"] == "absent"
    elif control == "projection-dirty":
        assert status["repair"]["projection_dirty_count"] == 1
        assert observed["readers"]["lx"]["status"] == "data_unavailable"
    else:
        assert status["account_count"] == 0
