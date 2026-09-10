from __future__ import annotations

import errno
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path

import pytest

from src.application.bot import pi_migration as migration
from src.application.bot.pi_migration import (
    assert_pi_storage_ready,
    migrate_pi,
    read_pi_migration_receipt,
)
from src.infrastructure.pi_agent_process import run_pi_migration_bridge
from tests.bot_pi_test_support import seed_actual_legacy_pi_store
from tests.test_pi_agent_process import _run_session, _start_payload


def _file_inventory(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def _store_format(fixture) -> str:
    return run_pi_migration_bridge(
        "probe",
        fixture.target_runtime,
        {"expected-version": "0.85.1", "database": fixture.database},
    )["format"]


def _clone_runtime(source: Path, destination: Path) -> Path:
    def link_or_copy(input_path, output_path):
        try:
            return os.link(input_path, output_path)
        except OSError as exc:
            if exc.errno != errno.EXDEV:
                raise
            return shutil.copy2(input_path, output_path)

    return Path(shutil.copytree(source, destination, copy_function=link_or_copy, symlinks=True))


def _add_abandoned_legacy_tail(database: Path, session_id: str, count: int) -> None:
    with closing(sqlite3.connect(database)) as connection:
        parent_id = connection.execute(
            "SELECT leaf_id FROM lanes WHERE session_id = ? AND lane = 'main'",
            (session_id,),
        ).fetchone()[0]
        sequence = connection.execute(
            "SELECT MAX(seq) + 1 FROM entries WHERE session_id = ?", (session_id,),
        ).fetchone()[0]
        payload = connection.execute(
            "SELECT payload FROM entries WHERE session_id = ? AND type = 'message' ORDER BY seq LIMIT 1",
            (session_id,),
        ).fetchone()[0]
        rows = []
        for index in range(count):
            entry_id = f"abandoned-{index}"
            rows.append((session_id, sequence + index, entry_id, parent_id, 1_700_100_000_000 + index, payload))
            parent_id = entry_id
        connection.executemany(
            "INSERT INTO entries (session_id, seq, id, parent_id, type, timestamp, payload) "
            "VALUES (?, ?, ?, ?, 'message', ?, ?)",
            rows,
        )
        connection.commit()


def _add_unreachable_committed_branch(database: Path, session_id: str) -> None:
    with closing(sqlite3.connect(database)) as connection:
        sequence = connection.execute(
            "SELECT MAX(seq) + 1 FROM entries WHERE session_id = ?", (session_id,),
        ).fetchone()[0]
        message_payload = connection.execute(
            "SELECT payload FROM entries WHERE session_id = ? AND type = 'message' ORDER BY seq LIMIT 1",
            (session_id,),
        ).fetchone()[0]
        marker_payload = connection.execute(
            "SELECT payload FROM entries WHERE session_id = ? AND type = 'custom' ORDER BY seq LIMIT 1",
            (session_id,),
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO entries (session_id, seq, id, parent_id, type, timestamp, payload) "
            "VALUES (?, ?, 'branch-message', 'old_commit', 'message', ?, ?)",
            (session_id, sequence, 1_700_200_000_000, message_payload),
        )
        connection.execute(
            "INSERT INTO entries (session_id, seq, id, parent_id, type, timestamp, payload) "
            "VALUES (?, ?, 'branch-commit', 'branch-message', 'custom', ?, ?)",
            (session_id, sequence + 1, 1_700_200_000_001, marker_payload),
        )
        connection.commit()


def test_actual_forward_admitted_turn_reverse_preserves_current_history(tmp_path):
    fixture = seed_actual_legacy_pi_store(tmp_path / "pi_sessions.sqlite3")
    database, old, new = fixture.database, fixture.source_runtime, fixture.target_runtime
    assert assert_pi_storage_ready(database, old)["ok"] is True
    with pytest.raises(ValueError, match="incompatible"):
        assert_pi_storage_ready(database, new)

    forward = migrate_pi(pi_db=database, source_runtime=old, target_runtime=new,
                         apply=True, writers_stopped=True)
    assert forward["ok"] is True
    receipt = read_pi_migration_receipt(database)
    assert receipt["phase"] == "published"
    retained_backups = {
        Path(receipt["backup"]["path"]): Path(receipt["backup"]["path"]).read_bytes(),
    }
    with pytest.raises(ValueError):
        assert_pi_storage_ready(database, old)
    result = _run_session(
        database, fixture.session_id,
        _start_payload(session_id=fixture.session_id, user_message="post-migration question",
                       debug={"fixture_response": "post-migration answer", "delay_ms": 0,
                              "expected_history": ["old summary", "retained question", "retained answer"]}),
        run_id="post_migration_turn",
    )
    assert result["ok"] is True, result
    assert result["result"]["committed"] is True
    assert assert_pi_storage_ready(database, new)["ok"] is True
    assert migrate_pi(pi_db=database, source_runtime=old, target_runtime=new,
                      apply=True, writers_stopped=True)["already_applied"] is True

    before_path = tmp_path / "current-target.json"
    run_pi_migration_bridge("export", new, {
        "expected-version": "0.85.1", "database": database, "output": before_path,
    })
    current = json.loads(before_path.read_text())
    reverse = migrate_pi(pi_db=database, source_runtime=new, target_runtime=old,
                         apply=True, writers_stopped=True)
    assert reverse["ok"] is True
    assert assert_pi_storage_ready(database, old)["ok"] is True
    receipt = read_pi_migration_receipt(database)
    assert receipt["prior"]["target"]["runtime"]["version"] == "0.85.1"
    reverse_backup = Path(receipt["backup"]["path"])
    retained_backups[reverse_backup] = reverse_backup.read_bytes()
    after_path = tmp_path / "current-legacy.json"
    run_pi_migration_bridge("export", old, {
        "expected-version": "0.84.2", "database": database, "output": after_path,
    })
    restored = json.loads(after_path.read_text())
    assert restored == current
    entries = restored["sessions"][0]["entries"]
    assert any(entry.get("data", {}).get("run_id") == "post_migration_turn" for entry in entries)
    assert any(entry.get("type") == "compaction" for entry in entries)

    assert migrate_pi(pi_db=database, source_runtime=old, target_runtime=new,
                      apply=True, writers_stopped=True)["write_applied"] is True
    receipt = read_pi_migration_receipt(database)
    second_forward_backup = Path(receipt["backup"]["path"])
    retained_backups[second_forward_backup] = second_forward_backup.read_bytes()
    second_turn = _run_session(
        database, fixture.session_id,
        _start_payload(session_id=fixture.session_id, user_message="second upgrade question",
                       debug={"fixture_response": "second upgrade answer", "delay_ms": 0}),
        run_id="second_post_migration_turn",
    )
    assert second_turn["ok"] is True, second_turn
    second_current_path = tmp_path / "second-current-target.json"
    run_pi_migration_bridge("export", new, {
        "expected-version": "0.85.1", "database": database, "output": second_current_path,
    })
    second_current = json.loads(second_current_path.read_text())

    assert migrate_pi(pi_db=database, source_runtime=new, target_runtime=old,
                      apply=True, writers_stopped=True)["write_applied"] is True
    receipt = read_pi_migration_receipt(database)
    second_reverse_backup = Path(receipt["backup"]["path"])
    retained_backups[second_reverse_backup] = second_reverse_backup.read_bytes()
    final_path = tmp_path / "second-current-legacy.json"
    run_pi_migration_bridge("export", old, {
        "expected-version": "0.84.2", "database": database, "output": final_path,
    })
    assert json.loads(final_path.read_text()) == second_current

    chain = []
    current_receipt = receipt
    while current_receipt is not None:
        chain.append(current_receipt)
        current_receipt = current_receipt.get("prior")
    assert len(chain) == 4
    assert {Path(item["backup"]["path"]) for item in chain} == set(retained_backups)
    assert all(path.read_bytes() == contents for path, contents in retained_backups.items())


def test_preview_preserves_exact_file_inventory_including_sidecars(tmp_path):
    fixture = seed_actual_legacy_pi_store(tmp_path / "pi_sessions.sqlite3")
    Path(str(fixture.database) + "-wal").touch(mode=0o600)
    Path(str(fixture.database) + "-shm").write_bytes(b"preview-sidecar-sentinel")
    before = _file_inventory(tmp_path)

    result = migrate_pi(
        pi_db=fixture.database,
        source_runtime=fixture.source_runtime,
        target_runtime=fixture.target_runtime,
    )

    assert result["mode"] == "preview"
    assert result["write_applied"] is False
    assert _file_inventory(tmp_path) == before


def test_empty_stores_convert_in_both_directions(tmp_path):
    fixture = seed_actual_legacy_pi_store(tmp_path / "runtime-seed.sqlite3")
    empty_payload = tmp_path / "empty.json"
    empty_payload.write_text(json.dumps({"format": "om-pi-export.v1", "sessions": []}))
    directions = (
        (fixture.source_runtime, "0.84.2", "legacy", fixture.target_runtime, "target"),
        (fixture.target_runtime, "0.85.1", "target", fixture.source_runtime, "legacy"),
    )

    for index, (source, source_version, source_format, target, target_format) in enumerate(directions):
        database = tmp_path / f"empty-{index}.sqlite3"
        run_pi_migration_bridge("import", source, {
            "expected-version": source_version, "database": database, "input": empty_payload,
        })
        assert run_pi_migration_bridge("probe", fixture.target_runtime, {
            "expected-version": "0.85.1", "database": database,
        })["format"] == source_format

        preview = migrate_pi(pi_db=database, source_runtime=source, target_runtime=target)
        assert preview["session_ids"] == []
        applied = migrate_pi(
            pi_db=database, source_runtime=source, target_runtime=target,
            apply=True, writers_stopped=True,
        )
        assert applied["write_applied"] is True
        assert assert_pi_storage_ready(database, target)["database_format"] == target_format
        exported = tmp_path / f"empty-{index}.json"
        report = run_pi_migration_bridge("export", target, {
            "expected-version": "0.85.1" if target_format == "target" else "0.84.2",
            "database": database,
            "output": exported,
        })
        assert report["sessionCount"] == 0
        assert json.loads(exported.read_text())["sessions"] == []


def test_prepared_receipt_failure_preserves_orphan_and_retries_with_new_generation(tmp_path, monkeypatch):
    fixture = seed_actual_legacy_pi_store(tmp_path / "pi_sessions.sqlite3")
    original_write_receipt = migration._write_receipt

    def fail_first_prepared_receipt(database, receipt):
        if receipt["phase"] == "prepared":
            raise RuntimeError("prepared receipt fault")
        original_write_receipt(database, receipt)

    monkeypatch.setattr(migration, "_write_receipt", fail_first_prepared_receipt)
    with pytest.raises(RuntimeError, match="prepared receipt fault"):
        migrate_pi(
            pi_db=fixture.database,
            source_runtime=fixture.source_runtime,
            target_runtime=fixture.target_runtime,
            apply=True,
            writers_stopped=True,
        )
    assert read_pi_migration_receipt(fixture.database) is None
    orphaned = list(tmp_path.glob("pi_sessions.sqlite3.om-pi-recovery-0.84.2-to-0.85.1-*.sqlite3"))
    assert len(orphaned) == 1
    orphan_bytes = orphaned[0].read_bytes()

    monkeypatch.setattr(migration, "_write_receipt", original_write_receipt)
    retried = migrate_pi(
        pi_db=fixture.database,
        source_runtime=fixture.source_runtime,
        target_runtime=fixture.target_runtime,
        apply=True,
        writers_stopped=True,
    )

    receipt = read_pi_migration_receipt(fixture.database)
    assert retried["write_applied"] is True
    assert Path(receipt["backup"]["path"]) != orphaned[0]
    assert orphaned[0].read_bytes() == orphan_bytes
    assert assert_pi_storage_ready(fixture.database, fixture.target_runtime)["ok"] is True


def test_exporter_death_leaves_prepared_backup_and_retry_recovers(tmp_path, monkeypatch):
    fixture = seed_actual_legacy_pi_store(tmp_path / "pi_sessions.sqlite3")
    abandoned_count = 15_000
    _add_abandoned_legacy_tail(fixture.database, fixture.session_id, abandoned_count)
    original_bridge = migration._bridge
    node = shutil.which("node")
    assert node is not None
    bridge_entry = Path(__file__).resolve().parents[1] / "agent-runtime" / "pi_migration.mjs"
    ready = tmp_path / "exporter-opened"
    preload = tmp_path / "pause-exporter.mjs"
    preload.write_text(f'''
import {{ writeFileSync }} from "node:fs";
import {{ pathToFileURL }} from "node:url";
const runtime = process.argv[process.argv.indexOf("--runtime") + 1];
const sdk = await import(import.meta.resolve("@earendil-works/pi-session-backend-sqlite-node", pathToFileURL(runtime + "/package.json").href));
const prototype = sdk.SqliteSessionRepository.prototype;
const original = prototype.open;
prototype.open = async function (...args) {{
  const session = await original.apply(this, args);
  writeFileSync({json.dumps(str(ready))}, String(process.pid));
  process.kill(process.pid, "SIGSTOP");
  return session;
}};
''')
    exporter_opened_store = False

    def kill_real_legacy_export(command, runtime, **arguments):
        nonlocal exporter_opened_store
        if command != "export" or Path(runtime) != fixture.source_runtime:
            return original_bridge(command, runtime, **arguments)
        maintenance_descriptors = arguments.pop("maintenance_descriptors", ())
        argv = [node, "--experimental-import-meta-resolve", "--no-warnings", "--import", str(preload), str(bridge_entry),
                command, "--runtime", str(runtime)]
        for name, value in arguments.items():
            argv.extend(("--" + name.replace("_", "-"), str(value)))
        process = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={"PATH": os.environ.get("PATH", "")},
            pass_fds=maintenance_descriptors,
        )
        deadline = time.monotonic() + 10
        try:
            while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.01)
            exporter_opened_store = ready.exists()
        finally:
            if process.poll() is None:
                process.kill()
            stdout, stderr = process.communicate(timeout=5)
        assert exporter_opened_store, f"legacy exporter did not reach repository open: {stdout}\n{stderr}"
        raise RuntimeError("exporter killed")

    monkeypatch.setattr(migration, "_bridge", kill_real_legacy_export)
    with pytest.raises(RuntimeError, match="exporter killed"):
        migrate_pi(
            pi_db=fixture.database,
            source_runtime=fixture.source_runtime,
            target_runtime=fixture.target_runtime,
            apply=True,
            writers_stopped=True,
        )
    receipt = read_pi_migration_receipt(fixture.database)
    assert receipt["phase"] == "prepared"
    backup = Path(receipt["backup"]["path"])
    backup_bytes = backup.read_bytes()
    assert exporter_opened_store is True
    assert not any(path.exists() for path in migration._sidecars(backup))
    assert _store_format(fixture) == "legacy"
    with pytest.raises(ValueError, match="unresolved"):
        assert_pi_storage_ready(fixture.database, fixture.source_runtime)

    monkeypatch.setattr(migration, "_bridge", original_bridge)
    retried = migrate_pi(
        pi_db=fixture.database,
        source_runtime=fixture.source_runtime,
        target_runtime=fixture.target_runtime,
        apply=True,
        writers_stopped=True,
    )

    assert retried["write_applied"] is True
    assert backup.read_bytes() == backup_bytes
    assert not any(path.exists() for path in migration._sidecars(backup))
    published = read_pi_migration_receipt(fixture.database)
    assert published["phase"] == "published"
    assert published["conversion"]["excludedTailCount"] == abandoned_count
    with closing(sqlite3.connect(f"file:{backup}?mode=ro", uri=True)) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM entries WHERE id LIKE 'abandoned-%'",
        ).fetchone()[0] == abandoned_count


def test_validated_migration_retries_without_reimport(tmp_path, monkeypatch):
    fixture = seed_actual_legacy_pi_store(tmp_path / "pi_sessions.sqlite3")
    original_write_receipt = migration._write_receipt

    def fail_after_validated(database, receipt):
        original_write_receipt(database, receipt)
        if receipt["phase"] == "validated":
            raise RuntimeError("validated fault")

    monkeypatch.setattr(migration, "_write_receipt", fail_after_validated)
    with pytest.raises(RuntimeError, match="validated fault"):
        migrate_pi(
            pi_db=fixture.database,
            source_runtime=fixture.source_runtime,
            target_runtime=fixture.target_runtime,
            apply=True,
            writers_stopped=True,
        )
    receipt = read_pi_migration_receipt(fixture.database)
    stage = Path(receipt["backup"]["path"] + ".staging.sqlite3")
    assert receipt["phase"] == "validated"
    assert stage.is_file()
    stage_bytes = stage.read_bytes()
    assert _store_format(fixture) == "legacy"
    with pytest.raises(ValueError, match="unresolved"):
        assert_pi_storage_ready(fixture.database, fixture.source_runtime)

    monkeypatch.setattr(migration, "_write_receipt", original_write_receipt)
    original_bridge = migration._bridge

    def forbid_reimport(command, *args, **kwargs):
        if command == "import":
            raise AssertionError("validated retry must reuse staging")
        return original_bridge(command, *args, **kwargs)

    monkeypatch.setattr(migration, "_bridge", forbid_reimport)
    retried = migrate_pi(
        pi_db=fixture.database,
        source_runtime=fixture.source_runtime,
        target_runtime=fixture.target_runtime,
        apply=True,
        writers_stopped=True,
    )

    assert retried["write_applied"] is True
    assert not stage.exists()
    assert fixture.database.read_bytes() == stage_bytes
    assert assert_pi_storage_ready(fixture.database, fixture.target_runtime)["ok"] is True


def test_retry_reconciles_target_renamed_before_published_receipt(tmp_path, monkeypatch):
    fixture = seed_actual_legacy_pi_store(tmp_path / "pi_sessions.sqlite3")
    original_write_receipt = migration._write_receipt

    def fail_before_published_receipt(database, receipt):
        if receipt["phase"] == "published":
            raise RuntimeError("published receipt fault")
        original_write_receipt(database, receipt)

    monkeypatch.setattr(migration, "_write_receipt", fail_before_published_receipt)
    with pytest.raises(RuntimeError, match="published receipt fault"):
        migrate_pi(
            pi_db=fixture.database,
            source_runtime=fixture.source_runtime,
            target_runtime=fixture.target_runtime,
            apply=True,
            writers_stopped=True,
        )
    assert read_pi_migration_receipt(fixture.database)["phase"] == "validated"
    assert _store_format(fixture) == "target"
    with pytest.raises(ValueError, match="unresolved"):
        assert_pi_storage_ready(fixture.database, fixture.target_runtime)

    monkeypatch.setattr(migration, "_write_receipt", original_write_receipt)
    original_bridge = migration._bridge

    def forbid_reimport(command, *args, **kwargs):
        if command == "import":
            raise AssertionError("publication reconciliation must not import")
        return original_bridge(command, *args, **kwargs)

    monkeypatch.setattr(migration, "_bridge", forbid_reimport)
    reconciled = migrate_pi(
        pi_db=fixture.database,
        source_runtime=fixture.source_runtime,
        target_runtime=fixture.target_runtime,
        apply=True,
        writers_stopped=True,
    )

    assert reconciled["already_applied"] is True
    assert reconciled["write_applied"] is False
    assert read_pi_migration_receipt(fixture.database)["phase"] == "published"


def test_apply_captures_committed_wal_in_sealed_backup(tmp_path):
    fixture = seed_actual_legacy_pi_store(tmp_path / "pi_sessions.sqlite3")
    wal_cwd = "/tmp/committed-in-wal"
    subprocess.run(
        [sys.executable, "-c", """
import os, sqlite3, sys
connection = sqlite3.connect(sys.argv[1])
connection.execute("PRAGMA journal_mode=WAL")
connection.execute("PRAGMA wal_autocheckpoint=0")
connection.execute("UPDATE sessions SET cwd = ? WHERE id = ?", (sys.argv[2], sys.argv[3]))
connection.commit()
os._exit(0)
""", str(fixture.database), wal_cwd, fixture.session_id],
        check=True,
    )
    assert Path(str(fixture.database) + "-wal").stat().st_size > 0

    result = migrate_pi(
        pi_db=fixture.database,
        source_runtime=fixture.source_runtime,
        target_runtime=fixture.target_runtime,
        apply=True,
        writers_stopped=True,
    )

    receipt = read_pi_migration_receipt(fixture.database)
    with closing(sqlite3.connect(f"file:{receipt['backup']['path']}?mode=ro", uri=True)) as backup:
        assert backup.execute("SELECT cwd FROM sessions WHERE id = ?", (fixture.session_id,)).fetchone() == (wal_cwd,)
    assert result["write_applied"] is True
    assert assert_pi_storage_ready(fixture.database, fixture.target_runtime)["ok"] is True


def test_invalid_store_and_runtime_inputs_fail_closed(tmp_path):
    fixture = seed_actual_legacy_pi_store(tmp_path / "valid.sqlite3")
    valid_before = fixture.database.read_bytes()
    with pytest.raises(ValueError, match="only exact"):
        migrate_pi(
            pi_db=fixture.database,
            source_runtime=fixture.source_runtime,
            target_runtime=fixture.source_runtime,
            apply=True,
            writers_stopped=True,
        )
    assert fixture.database.read_bytes() == valid_before

    invalid = tmp_path / "invalid.sqlite3"
    invalid.write_bytes(b"not a sqlite database")
    invalid_before = invalid.read_bytes()
    with pytest.raises(ValueError, match="does not match"):
        migrate_pi(
            pi_db=invalid,
            source_runtime=fixture.source_runtime,
            target_runtime=fixture.target_runtime,
        )
    with pytest.raises(ValueError, match="does not match"):
        migrate_pi(
            pi_db=invalid,
            source_runtime=fixture.source_runtime,
            target_runtime=fixture.target_runtime,
            apply=True,
            writers_stopped=True,
        )
    assert invalid.read_bytes() == invalid_before
    assert read_pi_migration_receipt(invalid) is None
    assert not list(tmp_path.glob("invalid.sqlite3.om-pi-*"))


def test_invalid_staging_format_is_rejected_before_validation_or_publication(tmp_path, monkeypatch):
    fixture = seed_actual_legacy_pi_store(tmp_path / "pi_sessions.sqlite3")
    original_checkpoint = migration._checkpoint_source

    def checkpoint_then_corrupt_target(database):
        original_checkpoint(database)
        if str(database).endswith(".staging.sqlite3"):
            with sqlite3.connect(database) as connection:
                connection.execute("DROP TRIGGER trg_entries_validate")

    monkeypatch.setattr(migration, "_checkpoint_source", checkpoint_then_corrupt_target)
    with pytest.raises(ValueError, match="staging store format"):
        migrate_pi(
            pi_db=fixture.database,
            source_runtime=fixture.source_runtime,
            target_runtime=fixture.target_runtime,
            apply=True,
            writers_stopped=True,
        )

    receipt = read_pi_migration_receipt(fixture.database)
    assert receipt["phase"] == "prepared"
    assert _store_format(fixture) == "legacy"
    backup = Path(receipt["backup"]["path"])
    backup_bytes = backup.read_bytes()

    monkeypatch.setattr(migration, "_checkpoint_source", original_checkpoint)
    assert migrate_pi(
        pi_db=fixture.database,
        source_runtime=fixture.source_runtime,
        target_runtime=fixture.target_runtime,
        apply=True,
        writers_stopped=True,
    )["write_applied"] is True
    assert backup.read_bytes() == backup_bytes


def test_unreachable_committed_legacy_branch_is_not_silently_dropped(tmp_path):
    fixture = seed_actual_legacy_pi_store(tmp_path / "pi_sessions.sqlite3")
    _add_unreachable_committed_branch(fixture.database, fixture.session_id)

    with pytest.raises(ValueError, match="unsupported legacy session branches"):
        migrate_pi(
            pi_db=fixture.database,
            source_runtime=fixture.source_runtime,
            target_runtime=fixture.target_runtime,
            apply=True,
            writers_stopped=True,
        )

    assert _store_format(fixture) == "legacy"
    receipt = read_pi_migration_receipt(fixture.database)
    assert receipt["phase"] == "prepared"
    with closing(sqlite3.connect(f"file:{receipt['backup']['path']}?mode=ro", uri=True)) as backup:
        assert backup.execute(
            "SELECT id FROM entries WHERE id IN ('branch-message', 'branch-commit') ORDER BY seq",
        ).fetchall() == [("branch-message",), ("branch-commit",)]


def test_corrupt_legacy_payload_does_not_leak_content_through_facade(tmp_path):
    fixture = seed_actual_legacy_pi_store(tmp_path / "pi_sessions.sqlite3")
    sentinel = "PRIVATE_CONVERSATION_SENTINEL"
    with closing(sqlite3.connect(fixture.database)) as connection:
        connection.execute(
            "UPDATE entries SET payload = ? WHERE session_id = ? AND id = 'old_user'",
            (f'{{"message":"{sentinel}"', fixture.session_id),
        )
        connection.commit()

    with pytest.raises(ValueError) as rejected:
        migrate_pi(
            pi_db=fixture.database,
            source_runtime=fixture.source_runtime,
            target_runtime=fixture.target_runtime,
            apply=True,
            writers_stopped=True,
        )

    assert sentinel not in str(rejected.value)
    assert _store_format(fixture) == "legacy"


def test_repeated_same_run_compactions_roundtrip(tmp_path):
    fixture = seed_actual_legacy_pi_store(tmp_path / "initial.sqlite3")
    exported = tmp_path / "initial.json"
    run_pi_migration_bridge(
        "export",
        fixture.source_runtime,
        {"expected-version": "0.84.2", "database": fixture.database, "output": exported},
    )
    payload = json.loads(exported.read_text(encoding="utf-8"))
    entries = payload["sessions"][0]["entries"]
    first_compaction = next(entry for entry in entries if entry["type"] == "compaction")
    first_marker = entries[-1]
    second_compaction = {
        **first_compaction,
        "id": "second-compaction",
        "parentId": first_marker["id"],
        "timestamp": first_marker["timestamp"] + 10,
        "summary": "second summary",
    }
    second_marker = {
        **first_marker,
        "id": "second-compaction-commit",
        "parentId": second_compaction["id"],
        "timestamp": second_compaction["timestamp"] + 10,
    }
    entries.extend((second_compaction, second_marker))
    source_seed = tmp_path / "repeated-seed.json"
    source_seed.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    repeated = tmp_path / "repeated.sqlite3"
    run_pi_migration_bridge(
        "import",
        fixture.source_runtime,
        {"expected-version": "0.84.2", "database": repeated, "input": source_seed},
    )

    forward = migrate_pi(
        pi_db=repeated,
        source_runtime=fixture.source_runtime,
        target_runtime=fixture.target_runtime,
        apply=True,
        writers_stopped=True,
    )
    assert forward["write_applied"] is True
    target_export = tmp_path / "target.json"
    run_pi_migration_bridge(
        "export",
        fixture.target_runtime,
        {"expected-version": "0.85.1", "database": repeated, "output": target_export},
    )
    current = json.loads(target_export.read_text(encoding="utf-8"))
    compaction_markers = [
        entry for entry in current["sessions"][0]["entries"]
        if entry.get("data", {}).get("kind") == "compaction"
    ]
    assert [entry["data"]["run_id"] for entry in compaction_markers] == [
        first_marker["data"]["run_id"], first_marker["data"]["run_id"],
    ]

    reverse = migrate_pi(
        pi_db=repeated,
        source_runtime=fixture.target_runtime,
        target_runtime=fixture.source_runtime,
        apply=True,
        writers_stopped=True,
    )
    assert reverse["write_applied"] is True
    restored_export = tmp_path / "restored.json"
    run_pi_migration_bridge(
        "export",
        fixture.source_runtime,
        {"expected-version": "0.84.2", "database": repeated, "output": restored_export},
    )
    assert json.loads(restored_export.read_text(encoding="utf-8")) == current


def test_readiness_accepts_relocated_target_but_rejects_same_version_drift(tmp_path):
    fixture = seed_actual_legacy_pi_store(tmp_path / "pi_sessions.sqlite3")
    migrate_pi(
        pi_db=fixture.database,
        source_runtime=fixture.source_runtime,
        target_runtime=fixture.target_runtime,
        apply=True,
        writers_stopped=True,
    )
    relocated = _clone_runtime(fixture.target_runtime, tmp_path / "relocated-target")
    assert assert_pi_storage_ready(fixture.database, relocated)["ok"] is True

    preview = migrate_pi(pi_db=fixture.database, source_runtime=relocated,
                         target_runtime=fixture.source_runtime)
    assert preview["receipt_phase"] == "published"

    lock_path = relocated / "package-lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    lock["omTestDrift"] = True
    replacement = relocated / "package-lock.changed.json"
    replacement.write_text(json.dumps(lock, sort_keys=True), encoding="utf-8")
    replacement.replace(lock_path)

    with pytest.raises(ValueError, match="target runtime identity changed"):
        assert_pi_storage_ready(fixture.database, relocated)
    before = {path.name: path.read_bytes() for path in tmp_path.glob("pi_sessions.sqlite3*")}
    for apply in (False, True):
        with pytest.raises(ValueError, match="published Pi migration receipt does not match this source"):
            migrate_pi(pi_db=fixture.database, source_runtime=relocated,
                       target_runtime=fixture.source_runtime, apply=apply, writers_stopped=apply)
        assert {path.name: path.read_bytes() for path in tmp_path.glob("pi_sessions.sqlite3*")} == before


def test_readiness_rejects_missing_recovery_backup(tmp_path):
    fixture = seed_actual_legacy_pi_store(tmp_path / "pi_sessions.sqlite3")
    migrate_pi(
        pi_db=fixture.database,
        source_runtime=fixture.source_runtime,
        target_runtime=fixture.target_runtime,
        apply=True,
        writers_stopped=True,
    )
    receipt = read_pi_migration_receipt(fixture.database)
    backup = Path(receipt["backup"]["path"])
    backup.rename(backup.with_name(backup.name + ".missing"))

    with pytest.raises(ValueError, match="recovery backup is unavailable"):
        assert_pi_storage_ready(fixture.database, fixture.target_runtime)


def test_readiness_recursively_requires_prior_source_runtime(tmp_path):
    fixture = seed_actual_legacy_pi_store(tmp_path / "pi_sessions.sqlite3")
    retained_old = _clone_runtime(fixture.source_runtime, tmp_path / "retained-old")
    migrate_pi(
        pi_db=fixture.database,
        source_runtime=retained_old,
        target_runtime=fixture.target_runtime,
        apply=True,
        writers_stopped=True,
    )
    migrate_pi(
        pi_db=fixture.database,
        source_runtime=fixture.target_runtime,
        target_runtime=fixture.source_runtime,
        apply=True,
        writers_stopped=True,
    )
    retained_old.rename(tmp_path / "retained-old.missing")

    with pytest.raises(ValueError, match="source runtime is unavailable"):
        assert_pi_storage_ready(fixture.database, fixture.source_runtime)


@pytest.mark.parametrize(("phase", "damage"), [
    ("prepared", "missing"),
    ("prepared", "writable"),
    ("prepared", "sidecar"),
    ("validated-source", "missing"),
    ("validated-target", "missing"),
    ("published", "missing"),
    ("prior", "missing"),
])
def test_migration_preflight_rejects_broken_recovery_without_writes(tmp_path, monkeypatch, phase, damage):
    fixture = seed_actual_legacy_pi_store(tmp_path / "pi_sessions.sqlite3")
    source, target = fixture.source_runtime, fixture.target_runtime
    original_write = migration._write_receipt

    def interrupt_phase(database, receipt):
        if phase == "validated-target" and receipt["phase"] == "published":
            raise RuntimeError("phase fault")
        original_write(database, receipt)
        if receipt["phase"] == phase or (phase == "validated-source" and receipt["phase"] == "validated"):
            raise RuntimeError("phase fault")

    if phase in {"published", "prior"}:
        migrate_pi(pi_db=fixture.database, source_runtime=source, target_runtime=target,
                   apply=True, writers_stopped=True)
        source, target = target, source
        if phase == "prior":
            migrate_pi(pi_db=fixture.database, source_runtime=source, target_runtime=target,
                       apply=True, writers_stopped=True)
            source, target = target, source
    else:
        with monkeypatch.context() as patch:
            patch.setattr(migration, "_write_receipt", interrupt_phase)
            with pytest.raises(RuntimeError, match="phase fault"):
                migrate_pi(pi_db=fixture.database, source_runtime=source, target_runtime=target,
                           apply=True, writers_stopped=True)
    receipt = read_pi_migration_receipt(fixture.database)
    backup_receipt = receipt["prior"] if phase == "prior" else receipt
    backup = Path(backup_receipt["backup"]["path"])
    if damage == "missing":
        backup.unlink()
    elif damage == "writable":
        backup.chmod(0o600)
    else:
        Path(str(backup) + "-wal").write_bytes(b"unexpected sidecar")
    before = _file_inventory(tmp_path)
    before_modes = {str(path): path.stat().st_mode for path in tmp_path.rglob("*")}
    for apply in (False, True):
        with pytest.raises(ValueError, match="retained Pi recovery backup"):
            migrate_pi(pi_db=fixture.database, source_runtime=source, target_runtime=target,
                       apply=apply, writers_stopped=apply)
        assert _file_inventory(tmp_path) == before
        expected_modes = dict(before_modes)
        if apply:
            # The existing maintenance lock tightens active-file permissions.
            expected_modes[str(fixture.database)] = (before_modes[str(fixture.database)] & ~0o777) | 0o600
        assert {str(path): path.stat().st_mode for path in tmp_path.rglob("*")} == expected_modes
