from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import threading

import pytest

from domain.domain.ledger import ContractKey, TradeEvent
from src.application.ledger import position_projection_migration as module
from src.application.ledger import repository_core
from src.application.ledger.repository import SQLiteOptionPositionsRepository


_REAL_SOURCE_COMMIT = module._source_commit


@pytest.fixture(autouse=True)
def _stable_source_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(module, "_source_commit", lambda: "a" * 40)


def _event(event_id: str = "open-1", *, event_time_ms: int = 1_000) -> dict[str, object]:
    return TradeEvent(
        event_id=event_id,
        event_type="open",
        event_time_ms=event_time_ms,
        contract_key=ContractKey.from_values(
            broker="futu",
            account="lx",
            underlying_symbol="NVDA",
            option_type="put",
            position_side="short",
            strike=100,
            expiration_ymd="2028-12-15",
        ),
        contracts=1,
        price=2,
        currency="USD",
        source="test",
        multiplier=100,
        lot_id="lot-1",
        raw_payload={"source_type": "test", "side": "sell"},
    ).to_dict()


def _legacy_store(tmp_path: Path, *, name: str = "ledger.sqlite3") -> Path:
    path = tmp_path / name
    event = _event()
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE trade_events (
              event_id TEXT PRIMARY KEY,
              event_json TEXT NOT NULL,
              trade_time_ms INTEGER NOT NULL,
              created_at_ms INTEGER NOT NULL,
              updated_at_ms INTEGER NOT NULL
            );
            CREATE TABLE position_lots (
              record_id TEXT PRIMARY KEY,
              fields_json TEXT NOT NULL,
              source_event_id TEXT,
              expiration INTEGER,
              strike REAL,
              multiplier REAL,
              updated_at_ms INTEGER NOT NULL
            );
            """
        )
        conn.execute(
            """
            INSERT INTO trade_events (
              event_id,event_json,trade_time_ms,created_at_ms,updated_at_ms
            ) VALUES (?,?,?,?,?)
            """,
            (
                event["event_id"],
                json.dumps(event, ensure_ascii=False, sort_keys=True),
                event["event_time_ms"],
                1,
                1,
            ),
        )
    return path


def _persistent_artifact_sizes(path: Path) -> dict[str, int]:
    return {
        suffix or "db": Path(f"{path}{suffix}").stat().st_size
        if Path(f"{path}{suffix}").exists()
        else 0
        for suffix in ("", "-wal")
    }


def _apply(path: Path) -> dict[str, object]:
    inventory = module.build_position_projection_migration_inventory(path)
    return module.apply_position_projection_migration(path, inventory)


def _acceptance(shadow: dict[str, object]) -> dict[str, object]:
    return module._manifest(
        {
            "schema_version": module.ACCEPTANCE_SCHEMA,
            "generated_at_utc": "2026-08-14T00:00:00+00:00",
            "status": "pass",
            "readiness": "ready",
            "store_binding": shadow["store_binding"],
            "reference_host": {
                "comparable": True,
                "current_fingerprint": "b" * 64,
                "expected_fingerprint": "b" * 64,
            },
            "components": {
                "lot_diff_publication": {"status": "pass"},
                "checkpoint_tail": {"status": "pass"},
                "combined": {"status": "ready"},
            },
            "resource_failures": [],
            "parity_failures": [],
            "retained_lots_10x_guarantee": False,
        }
    )


def test_migration_write_connection_fails_closed_when_wal_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Cursor:
        def fetchone(self) -> tuple[str]:
            return ("delete",)

    class Connection:
        closed = False

        def execute(self, _sql: str) -> Cursor:
            return Cursor()

        def close(self) -> None:
            self.closed = True

    path = _legacy_store(tmp_path)
    connection = Connection()
    monkeypatch.setattr(repository_core, "connect_private_sqlite", lambda _path: connection)
    monkeypatch.setattr(repository_core, "initialize_ledger_connection", lambda _conn: None)

    with pytest.raises(RuntimeError, match="SQLite WAL mode is required"):
        with module._write_connection(path):
            raise AssertionError("migration writer body must not start")

    assert connection.closed is True


def test_migration_writer_waits_for_repository_connection_lifecycle(
    tmp_path: Path,
) -> None:
    path = _legacy_store(tmp_path)
    repo = module._repository(path)
    repository_entered = threading.Event()
    release_repository = threading.Event()
    migration_entered = threading.Event()

    def _repository_writer() -> None:
        with repo._writer_connection():
            repository_entered.set()
            assert release_repository.wait(2)

    def _migration_writer() -> None:
        with module._write_connection(path):
            migration_entered.set()

    with ThreadPoolExecutor(max_workers=2) as executor:
        repository = executor.submit(_repository_writer)
        assert repository_entered.wait(1)
        migration = executor.submit(_migration_writer)
        try:
            assert not migration_entered.wait(0.1)
        finally:
            release_repository.set()
        repository.result(timeout=1)
        migration.result(timeout=1)

    assert migration_entered.is_set()


def test_inventory_and_shadow_are_read_only_and_apply_verifies(tmp_path: Path) -> None:
    path = _legacy_store(tmp_path)
    before = _persistent_artifact_sizes(path)

    inventory = module.build_position_projection_migration_inventory(path)

    assert inventory["schema_version"] == module.INVENTORY_SCHEMA
    assert inventory["read_only"] is True
    assert inventory["counts"] == {"trade_events": 1, "position_lots": 0}
    assert _persistent_artifact_sizes(path) == before
    with sqlite3.connect(path) as conn:
        assert "account" not in {row[1] for row in conn.execute("PRAGMA table_info(trade_events)")}

    applied = module.apply_position_projection_migration(path, inventory)
    assert applied["write_applied"] is True
    assert applied["checkpoint_mode"] == "disabled"
    assert applied["projection"]["checkpoint_written"] is True

    after_apply = _persistent_artifact_sizes(path)
    verified = module.verify_position_projection_migration(path, shadow=True)
    assert verified["status"] == "pass"
    assert verified["readiness"] == "ready"
    assert verified["runtime_shadow"]["status"] == "pass"
    assert verified["checkpoint"]["k_within_bound"] is True
    assert _persistent_artifact_sizes(path) == after_apply
    status = module.position_projection_migration_status(path)
    assert status["readiness"] == "ready"
    assert status["fingerprint_scope"]["rows"] == 1
    assert status["fingerprint_scope"]["fields_json_bytes"] > 0
    assert status["runtime_telemetry"]["sample_count"] >= 1
    assert status["runtime_telemetry"]["sample_count"] <= status["runtime_telemetry"][
        "sample_limit"
    ]
    assert status["runtime_telemetry"]["mode_counts"]["full"] >= 1


def test_apply_rejects_stale_and_wrong_store_manifests(tmp_path: Path) -> None:
    first = _legacy_store(tmp_path, name="first.sqlite3")
    second = _legacy_store(tmp_path, name="second.sqlite3")
    first_manifest = module.build_position_projection_migration_inventory(first)

    with sqlite3.connect(first) as conn:
        event = _event("open-2", event_time_ms=2_000)
        conn.execute(
            "INSERT INTO trade_events VALUES (?,?,?,?,?)",
            (
                event["event_id"],
                json.dumps(event, ensure_ascii=False, sort_keys=True),
                event["event_time_ms"],
                2,
                2,
            ),
        )

    with pytest.raises(ValueError, match="stale"):
        module.apply_position_projection_migration(first, first_manifest)
    with pytest.raises(ValueError, match="stale|identity"):
        module.apply_position_projection_migration(second, first_manifest)


def test_apply_failure_rolls_back_schema_backfill_and_projection(tmp_path: Path) -> None:
    path = _legacy_store(tmp_path)
    inventory = module.build_position_projection_migration_inventory(path)

    def fail(stage: str) -> None:
        if stage == "after_backfill":
            raise RuntimeError("injected")

    with pytest.raises(RuntimeError, match="injected"):
        module.apply_position_projection_migration(path, inventory, failure_hook=fail)

    with sqlite3.connect(path) as conn:
        assert "account" not in {row[1] for row in conn.execute("PRAGMA table_info(trade_events)")}
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='position_projection_checkpoints'"
        ).fetchone() is None


def test_apply_fails_closed_on_stored_event_account_conflict(
    tmp_path: Path,
) -> None:
    path = _legacy_store(tmp_path)
    with sqlite3.connect(path) as conn:
        conn.execute("ALTER TABLE trade_events ADD COLUMN account TEXT")
        conn.execute("UPDATE trade_events SET account = 'sy'")
    inventory = module.build_position_projection_migration_inventory(path)

    with pytest.raises(ValueError, match="account conflicts with JSON"):
        module.apply_position_projection_migration(path, inventory)

    with sqlite3.connect(path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(trade_events)")}
        assert "ingest_seq" not in columns
        assert conn.execute("SELECT account FROM trade_events").fetchone()[0] == "sy"


def test_verify_detects_lot_drift(tmp_path: Path) -> None:
    path = _legacy_store(tmp_path)
    _apply(path)
    with sqlite3.connect(path) as conn:
        row = conn.execute(
            "SELECT fields_json FROM position_lots WHERE record_id='lot-1'"
        ).fetchone()
        fields = json.loads(row[0])
        fields["contracts_open"] = 99
        conn.execute(
            "UPDATE position_lots SET fields_json=? WHERE record_id='lot-1'",
            (json.dumps(fields, ensure_ascii=False, sort_keys=True),),
        )

    result = module.verify_position_projection_migration(path, shadow=True)
    assert result["status"] == "fail"
    assert "full_oracle_parity_mismatch" in result["reasons"]


def test_activation_binds_exact_store_and_deactivate_preserves_rows(tmp_path: Path) -> None:
    path = _legacy_store(tmp_path)
    _apply(path)
    shadow = module.verify_position_projection_migration(path, shadow=True)
    acceptance = _acceptance(shadow)

    activated = module.activate_position_projection_checkpoints(
        path,
        acceptance_manifest=acceptance,
        shadow_manifest=shadow,
    )
    assert activated["checkpoint_mode"] == "enabled"
    status = module.position_projection_migration_status(path)
    assert status["checkpoint_mode"] == "enabled"
    before_counts = (status["checkpoint_count"], status["head_count"])

    deactivated = module.deactivate_position_projection_checkpoints(path)
    assert deactivated["write_applied"] is True
    assert deactivated["preserved"] == [
        "trade_events",
        "position_lots",
        "heads",
        "checkpoints",
    ]
    after = module.position_projection_migration_status(path)
    assert after["checkpoint_mode"] == "disabled"
    assert (after["checkpoint_count"], after["head_count"]) == before_counts


def test_activation_rejects_incomplete_acceptance_components(tmp_path: Path) -> None:
    path = _legacy_store(tmp_path)
    _apply(path)
    shadow = module.verify_position_projection_migration(path, shadow=True)
    acceptance = _acceptance(shadow)
    acceptance.pop("manifest_hash")
    acceptance["components"]["lot_diff_publication"] = {"status": "fail"}
    acceptance = module._manifest(acceptance)

    with pytest.raises(ValueError, match="component gates"):
        module.activate_position_projection_checkpoints(
            path,
            acceptance_manifest=acceptance,
            shadow_manifest=shadow,
        )


def test_activation_rejects_malformed_component_and_reference_host_evidence(
    tmp_path: Path,
) -> None:
    path = _legacy_store(tmp_path)
    _apply(path)
    shadow = module.verify_position_projection_migration(path, shadow=True)

    malformed = _acceptance(shadow)
    malformed.pop("manifest_hash")
    malformed["components"]["lot_diff_publication"] = "pass"
    with pytest.raises(ValueError, match="component gates"):
        module.activate_position_projection_checkpoints(
            path,
            acceptance_manifest=module._manifest(malformed),
            shadow_manifest=shadow,
        )

    wrong_host = _acceptance(shadow)
    wrong_host.pop("manifest_hash")
    wrong_host["reference_host"]["expected_fingerprint"] = "c" * 64
    with pytest.raises(ValueError, match="reference host"):
        module.activate_position_projection_checkpoints(
            path,
            acceptance_manifest=module._manifest(wrong_host),
            shadow_manifest=shadow,
        )


def test_activation_rejects_loaded_source_commit_drift(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = _legacy_store(tmp_path)
    _apply(path)
    shadow = module.verify_position_projection_migration(path, shadow=True)
    acceptance = _acceptance(shadow)
    monkeypatch.setattr(module, "_source_commit", lambda: "different-source-commit")

    with pytest.raises(ValueError, match="loaded source commit"):
        module.activate_position_projection_checkpoints(
            path,
            acceptance_manifest=acceptance,
            shadow_manifest=shadow,
        )


def test_activation_rejects_stale_generation_binding(tmp_path: Path) -> None:
    path = _legacy_store(tmp_path)
    _apply(path)
    shadow = module.verify_position_projection_migration(path, shadow=True)
    acceptance = _acceptance(shadow)
    event = _event("open-2", event_time_ms=2_000)
    repo = SQLiteOptionPositionsRepository(path)
    assert repo.upsert_trade_event(event) is True

    with pytest.raises(ValueError, match="verification|stale"):
        module.activate_position_projection_checkpoints(
            path,
            acceptance_manifest=acceptance,
            shadow_manifest=shadow,
        )


@pytest.mark.parametrize(
    "mutation",
    (
        "UPDATE position_projection_source_state SET projector_schema='wrong'",
        "UPDATE position_projection_source_state "
        "SET projector_implementation_fingerprint='wrong'",
        "CREATE TABLE phase_3a_schema_drift (id INTEGER PRIMARY KEY)",
    ),
)
def test_activation_rejects_projector_implementation_and_schema_cookie_drift(
    tmp_path: Path,
    mutation: str,
) -> None:
    path = _legacy_store(tmp_path)
    _apply(path)
    shadow = module.verify_position_projection_migration(path, shadow=True)
    acceptance = _acceptance(shadow)
    with sqlite3.connect(path) as conn:
        conn.execute(mutation)

    with pytest.raises(ValueError, match="verification|stale"):
        module.activate_position_projection_checkpoints(
            path,
            acceptance_manifest=acceptance,
            shadow_manifest=shadow,
        )


def test_status_reports_generation_mismatch_and_fails_closed(tmp_path: Path) -> None:
    path = _legacy_store(tmp_path)
    _apply(path)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE position_projection_heads SET built_source_generation=-1 "
            "WHERE account='lx'"
        )

    status = module.position_projection_migration_status(path)

    assert status["readiness"] == "not_ready"
    assert "source_generation_mismatch:lx" in status["reasons"]


def test_status_reports_unmigrated_store_without_querying_missing_columns(
    tmp_path: Path,
) -> None:
    status = module.position_projection_migration_status(_legacy_store(tmp_path))

    assert status["readiness"] == "not_ready"
    assert "position_lots_account_column_missing" in status["reasons"]
    assert status["fingerprint_scope"] == {"rows": 0, "fields_json_bytes": 0}


@pytest.mark.parametrize(
    ("status_output", "expected"),
    (("", "d" * 40), (" M src/application/example.py\n", None)),
)
def test_source_commit_requires_clean_production_source(
    monkeypatch: pytest.MonkeyPatch,
    status_output: str,
    expected: str | None,
) -> None:
    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        output = status_output if command[1] == "status" else "d" * 40 + "\n"
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    assert _REAL_SOURCE_COMMIT() == expected


def test_source_commit_accepts_clean_archived_release_and_rejects_drift(
    tmp_path: Path,
) -> None:
    origin = tmp_path / "origin"
    origin.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=origin, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=origin, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=origin, check=True)
    (origin / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
    (origin / "VERSION").write_text("1.2.3\n", encoding="utf-8")
    for name in ("domain", "src", "scripts"):
        path = origin / name
        path.mkdir()
        (path / "example.py").write_text(f"NAME = {name!r}\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=origin, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=origin, check=True)
    subprocess.run(["git", "tag", "v1.2.3"], cwd=origin, check=True)
    expected = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=origin,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    install = tmp_path / "install"
    release = install / "releases" / "1.2.3"
    release.parent.mkdir(parents=True)
    shutil.copytree(origin, release, ignore=shutil.ignore_patterns(".git"))
    cache_repo = install / "_cache" / "git" / "options-monitor.git"
    cache_repo.parent.mkdir(parents=True)
    subprocess.run(["git", "clone", "-q", "--mirror", str(origin), str(cache_repo)], check=True)

    assert _REAL_SOURCE_COMMIT(release) == expected
    (release / "src" / "example.py").write_text("NAME = 'drift'\n", encoding="utf-8")
    assert _REAL_SOURCE_COMMIT(release) is None


def test_read_only_size_guard_ignores_ephemeral_shm_resize_only() -> None:
    module._assert_read_only_persistent_sizes(
        {"db_bytes": 10, "wal_bytes": 20, "shm_bytes": 65_536},
        {"db_bytes": 10, "wal_bytes": 20, "shm_bytes": 32_768},
        operation="test",
    )
    with pytest.raises(RuntimeError, match="changed persistent SQLite sizes"):
        module._assert_read_only_persistent_sizes(
            {"db_bytes": 10, "wal_bytes": 20, "shm_bytes": 65_536},
            {"db_bytes": 10, "wal_bytes": 21, "shm_bytes": 32_768},
            operation="test",
        )
