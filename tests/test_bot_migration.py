from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing

import pytest

from src.application.bot import migration


def _fixture(tmp_path):
    db = tmp_path / "host.sqlite3"
    with sqlite3.connect(db) as conn:
        conn.executescript("""
            CREATE TABLE copilot_sessions(session_key TEXT PRIMARY KEY, messages_json TEXT, memory_json TEXT);
            CREATE TABLE copilot_session_runs(session_key TEXT);
            CREATE TABLE copilot_runs(run_id TEXT PRIMARY KEY,status TEXT,result_json TEXT);
            CREATE TABLE copilot_reply_outbox(delivery_key TEXT PRIMARY KEY,status TEXT,payload_json TEXT,session_key TEXT,run_id TEXT);
            CREATE TABLE copilot_lane_leases(lane TEXT);
            CREATE INDEX copilot_reply_status ON copilot_reply_outbox(status);
        """)
        conn.execute("INSERT INTO copilot_sessions VALUES (?,?,?)", ("old-userless-session", '["user said Copilot"]', '{"notes":"historical Copilot"}'))
        conn.execute("INSERT INTO copilot_runs VALUES (?,?,?)", ("run1", "completed", '{"tool":"copilot_chat","user_response":"Copilot历史文字"}'))
        conn.execute("INSERT INTO copilot_reply_outbox VALUES (?,?,?,?,?)", ("stable-delivery-key", "pending", '{"tool":"copilot_chat","user_response":"Copilot历史文字"}', "old-userless-session", "run1"))
    configs = [tmp_path / "one.json", tmp_path / "two.yaml"]
    configs[0].write_bytes(b'{"assistant":{"copilot":{"enabled":true}},"accounts":["sy"]}\r\n')
    configs[1].write_text('assistant:\n  copilot:\n    enabled: true\naccounts: [sy]\n')
    return db, configs


def test_dry_run_apply_wal_backup_and_idempotent_identity_preservation(tmp_path):
    db, configs = _fixture(tmp_path)
    originals = {str(p): p.read_bytes() for p in configs}
    writer = sqlite3.connect(db)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("INSERT INTO copilot_runs VALUES ('wal-run', 'completed', '{}')")
    writer.commit()
    before = db.read_bytes()
    report = migration.migrate_bot(host_db=db, config_paths=configs)
    assert report["dry_run"] and not report["write_applied"] and db.read_bytes() == before
    assert not (tmp_path / "bot-migration.json").exists()
    with pytest.raises(ValueError, match="requires.*writers-stopped"):
        migration.migrate_bot(host_db=db, config_paths=configs, apply=True)
    with pytest.raises(ValueError, match="Legacy Copilot"):
        migration.assert_bot_ready(db)
    migration.migrate_bot(host_db=db, config_paths=configs, apply=True, writers_stopped=True)
    writer.close()
    manifest = json.loads((tmp_path / "bot-migration.json").read_text())
    assert manifest["status"] == "complete"
    for item in manifest["config_records"]:
        path = next(p for p in configs if str(p) == item["path"])
        assert item["original_sha256"] == hashlib.sha256(originals[str(path)]).hexdigest()
        assert item["new_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
        assert path.with_name(path.name + ".pre-bot").read_bytes() == originals[str(path)]
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT run_id FROM bot_runs WHERE run_id='wal-run'").fetchone()
        assert conn.execute("SELECT messages_json,memory_json FROM bot_sessions").fetchone() == ('["user said Copilot"]', '{"notes":"historical Copilot"}')
        historical = json.loads(conn.execute("SELECT result_json FROM bot_runs WHERE run_id='run1'").fetchone()[0])
        assert historical["tool"] == "copilot_chat"
        key, text, session = conn.execute("SELECT delivery_key,payload_json,session_key FROM bot_reply_outbox").fetchone()
        assert key == "stable-delivery-key" and session == "old-userless-session"
        assert json.loads(text) == {"tool": "bot_chat", "user_response": "Copilot历史文字"}
    with sqlite3.connect(manifest["backup"]) as conn:
        assert conn.execute("SELECT run_id FROM copilot_runs WHERE run_id='wal-run'").fetchone()
    migration.assert_bot_ready(db)
    repeat = migration.migrate_bot(host_db=db, config_paths=configs, apply=True, writers_stopped=True)
    assert repeat["already_complete"] and not repeat["write_applied"]


def _interrupt(monkeypatch, db, configs):
    write = migration.atomic_write_private_text
    def failing(path, content):
        if path == configs[1]:
            raise OSError("fixture interruption")
        return write(path, content)
    monkeypatch.setattr(migration, "atomic_write_private_text", failing)
    with pytest.raises(OSError, match="fixture interruption"):
        migration.migrate_bot(host_db=db, config_paths=configs, apply=True, writers_stopped=True)
    monkeypatch.setattr(migration, "atomic_write_private_text", write)
    with pytest.raises(ValueError, match="incomplete"):
        migration.assert_bot_ready(db)


def test_partial_config_apply_resumes_from_frozen_hashes(tmp_path, monkeypatch):
    db, configs = _fixture(tmp_path)
    _interrupt(monkeypatch, db, configs)
    assert "bot" in json.loads(configs[0].read_text())["assistant"]
    assert "copilot:" in configs[1].read_text()
    migration.migrate_bot(host_db=db, config_paths=configs, apply=True, writers_stopped=True)
    migration.assert_bot_ready(db)


@pytest.mark.parametrize("changed", ["original", "converted", "backup", "database"])
def test_resume_rejects_external_changes_without_overwrite(tmp_path, monkeypatch, changed):
    db, configs = _fixture(tmp_path)
    _interrupt(monkeypatch, db, configs)
    target = {"original": configs[1], "converted": configs[0], "backup": configs[0].with_name(configs[0].name + ".pre-bot")}.get(changed)
    if target:
        target.write_text(target.read_text() + "\n ")
        before = target.read_bytes()
    else:
        with sqlite3.connect(db) as conn:
            conn.execute("UPDATE bot_runs SET result_json='external change' WHERE run_id='run1'")
    for apply in (False, True):
        with pytest.raises(ValueError, match="changed"):
            migration.migrate_bot(host_db=db, config_paths=configs, apply=apply, writers_stopped=True)
    if target:
        assert target.read_bytes() == before


def test_crash_after_database_commit_before_progress_marker_resumes(tmp_path, monkeypatch):
    db, configs = _fixture(tmp_path)
    write = migration.atomic_write_private_text
    def failing(path, content):
        if path.name == "bot-migration.json" and json.loads(content).get("database_done"):
            raise OSError("after database commit")
        return write(path, content)
    monkeypatch.setattr(migration, "atomic_write_private_text", failing)
    with pytest.raises(OSError, match="after database commit"):
        migration.migrate_bot(host_db=db, config_paths=configs, apply=True, writers_stopped=True)
    monkeypatch.setattr(migration, "atomic_write_private_text", write)
    migration.migrate_bot(host_db=db, config_paths=configs, apply=True, writers_stopped=True)
    migration.assert_bot_ready(db)


def test_conflicts_active_writers_and_old_runtime_config_fail_closed(tmp_path):
    from src.application.bot.control.settings import BotSettings
    from src.application.bot.host_store import BotHostStore
    db, configs = _fixture(tmp_path)
    with pytest.raises(ValueError, match="retired"):
        BotSettings.from_runtime_config({"assistant": {"copilot": {}}})
    with pytest.raises(ValueError, match="Legacy Copilot"):
        BotHostStore(db).session_memory("old-userless-session")
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE copilot_runs SET status='running' WHERE run_id='run1'")
    with pytest.raises(ValueError, match="active Bot run"):
        migration.migrate_bot(host_db=db, config_paths=configs)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE copilot_runs SET status='completed'")
        conn.execute("CREATE TABLE bot_sessions(session_key TEXT)")
    with pytest.raises(ValueError, match="coexist"):
        migration.migrate_bot(host_db=db, config_paths=configs)


def test_unowned_config_backup_is_never_adopted(tmp_path):
    db, configs = _fixture(tmp_path)
    backup = configs[0].with_name(configs[0].name + ".pre-bot")
    backup.write_text("unrelated backup")
    with pytest.raises(ValueError, match="unowned configuration backup"):
        migration.migrate_bot(host_db=db, config_paths=configs, apply=True, writers_stopped=True)
    assert backup.read_text() == "unrelated backup"
    assert not (tmp_path / "bot-migration.json").exists()


@pytest.mark.parametrize("state", ["fresh", "host_pending", "complete"])
def test_host_migration_leaves_retired_storage_backups_and_receipts_untouched(tmp_path, monkeypatch, state):
    db, configs = _fixture(tmp_path)
    retired = [tmp_path / "pi.sqlite3", tmp_path / "pi.sqlite3.pre-migration",
               tmp_path / "pi.sqlite3.migration.json"]
    for path in retired:
        path.write_bytes(b"historical operator-owned artifact\x00")
    before = {path: path.read_bytes() for path in retired}
    historical = {"pi_databases": [str(retired[0]), str(tmp_path / "missing-pi.sqlite3")],
                  "pi_sha256": {str(retired[0]): "historical-frozen-digest"}}
    if state != "fresh":
        if state == "host_pending":
            _interrupt(monkeypatch, db, configs)
            historical = {"pi_databases": [], "pi_sha256": {}}
        else:
            migration.migrate_bot(host_db=db, config_paths=configs, apply=True, writers_stopped=True)
        manifest_path = tmp_path / "bot-migration.json"
        manifest = json.loads(manifest_path.read_text())
        manifest.update(historical)
        manifest_path.write_text(json.dumps(manifest))
    read_db = migration._read_db

    def host_only(path):
        assert path not in retired, "Host schema migration must not open retired storage"
        return read_db(path)

    monkeypatch.setattr(migration, "_read_db", host_only)
    preview = migration.migrate_bot(host_db=db, config_paths=configs)
    assert preview["dry_run"] and not preview["write_applied"]
    assert "pi_database_count" not in preview
    result = migration.migrate_bot(host_db=db, config_paths=configs, apply=True, writers_stopped=True)
    assert result["write_applied"] is (state != "complete")
    migration.assert_bot_ready(db)
    repeat = migration.migrate_bot(host_db=db, config_paths=configs, apply=True, writers_stopped=True)
    assert repeat["already_complete"] and not repeat["write_applied"]
    assert {path: path.read_bytes() for path in retired} == before
    assert not (tmp_path / "missing-pi.sqlite3").exists()
    manifest = json.loads((tmp_path / "bot-migration.json").read_text())
    if state != "fresh":
        assert {key: manifest[key] for key in historical} == historical
    else:
        assert not set(historical).intersection(manifest)


@pytest.mark.parametrize("entry", ["api", "cli"])
@pytest.mark.parametrize("apply", [False, True])
def test_pending_legacy_pi_inventory_requires_recovery_before_effects(tmp_path, monkeypatch, entry, apply):
    import argparse
    from src.interfaces.cli.bot_ops import add_bot_commands, handle_bot_command

    db, configs = _fixture(tmp_path)
    _interrupt(monkeypatch, db, configs)
    retired = tmp_path / "pi.sqlite3"
    retired.write_bytes(b"operator-owned historical storage")
    manifest_path = tmp_path / "bot-migration.json"
    manifest = json.loads(manifest_path.read_text())
    manifest.update({"pi_databases": [str(retired), str(tmp_path / "missing-pi.sqlite3")],
                     "pi_sha256": {str(retired): "historical-frozen-digest"}})
    manifest_path.write_text(json.dumps(manifest))
    before = {p: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()}

    def no_database_read(path):
        pytest.fail("unresolved legacy inventory must fail before opening any database")

    monkeypatch.setattr(migration, "_read_db", no_database_read)
    with pytest.raises(ValueError, match="legacy Pi inventory.*operator recovery"):
        if entry == "api":
            migration.migrate_bot(host_db=db, config_paths=configs, apply=apply, writers_stopped=True)
        else:
            parser = argparse.ArgumentParser()
            add_bot_commands(parser.add_subparsers(dest="command", required=True))
            command = ["bot", "migrate", "--host-db", str(db)]
            for config in configs:
                command.extend(["--config", str(config)])
            command.extend(["--apply", "--writers-stopped"] if apply else ["--dry-run"])
            handle_bot_command(parser.parse_args(command))
    with pytest.raises(ValueError, match="legacy Pi inventory.*operator recovery"):
        migration.assert_bot_ready(db)
    assert {p: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()} == before
    assert manifest["status"] == "pending"


@pytest.mark.parametrize("retired_args", [
    ["migrate-pi", "--pi-db", "pi.sqlite3", "--source-runtime", "old", "--target-runtime", "new", "--dry-run"],
    ["migrate", "--host-db", "host.sqlite3", "--pi-db", "pi.sqlite3", "--dry-run"],
])
def test_cli_rejects_retired_conversion_before_effects(tmp_path, monkeypatch, retired_args):
    import argparse
    from src.interfaces.cli.bot_ops import add_bot_commands
    monkeypatch.chdir(tmp_path)
    parser = argparse.ArgumentParser()
    add_bot_commands(parser.add_subparsers(dest="command", required=True))
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["bot", *retired_args])
    assert exc.value.code == 2
    assert list(tmp_path.iterdir()) == []


def test_cli_host_schema_migration_preview_apply_and_repeat(tmp_path):
    import argparse
    from src.interfaces.cli.bot_ops import add_bot_commands, handle_bot_command
    db, configs = _fixture(tmp_path)
    parser = argparse.ArgumentParser()
    add_bot_commands(parser.add_subparsers(dest="command", required=True))
    command = ["bot", "migrate", "--host-db", str(db)]
    for config in configs:
        command.extend(["--config", str(config)])
    preview = handle_bot_command(parser.parse_args([*command, "--dry-run"]))
    assert preview["dry_run"] and not preview["write_applied"]
    applied = handle_bot_command(parser.parse_args([*command, "--apply", "--writers-stopped"]))
    assert applied["write_applied"]
    migration.assert_bot_ready(db)
    repeated = handle_bot_command(parser.parse_args([*command, "--apply", "--writers-stopped"]))
    assert repeated["already_complete"] and not repeated["write_applied"]


def test_new_cli_only_and_protocol_rename_preserves_user_data():
    import argparse
    from src.interfaces.cli.bot_ops import add_bot_commands
    parser = argparse.ArgumentParser()
    add_bot_commands(parser.add_subparsers(dest="command", required=True))
    args = parser.parse_args(["bot", "migrate", "--host-db", "fixture.sqlite3", "--dry-run"])
    assert args.bot_command == "migrate" and args.dry_run
    with pytest.raises(SystemExit):
        parser.parse_args(["copilot", "run"])
    payload = {"tool": "copilot_chat", "text": "Copilot user text", "data": {"copilot_user_annotation": "unchanged"}}
    assert migration._rename_envelope(payload) == {"tool": "bot_chat", "text": "Copilot user text", "data": {"copilot_user_annotation": "unchanged"}}


@pytest.mark.parametrize("partial", [False, True])
def test_migrated_database_requires_manifest_even_after_sidecar_loss(tmp_path, monkeypatch, partial):
    from src.application.bot.host_store import BotHostStore
    db, configs = _fixture(tmp_path)
    if partial:
        _interrupt(monkeypatch, db, configs)
    else:
        migration.migrate_bot(host_db=db, config_paths=configs, apply=True, writers_stopped=True)
    manifest = tmp_path / "bot-migration.json"
    saved = manifest.read_bytes()
    manifest.unlink()
    before = db.read_bytes()
    for check in (lambda: migration.assert_bot_ready(db), lambda: BotHostStore(db).session_memory("fixture")):
        with pytest.raises(ValueError, match="manifest missing"):
            check()
    assert db.read_bytes() == before
    manifest.write_bytes(saved)
    if not partial:
        value = json.loads(saved); value["database_id"] = "wrong"; manifest.write_text(json.dumps(value))
        with pytest.raises(ValueError, match="identity mismatch"):
            migration.assert_bot_ready(db)


def test_genuine_fresh_bot_database_needs_no_migration_sidecar(tmp_path):
    from src.application.bot.host_store import BotHostStore
    db = tmp_path / "fresh.sqlite3"
    migration.assert_bot_ready(db)
    BotHostStore(db).session_memory("fresh-session")
    migration.assert_bot_ready(db)
    assert not (tmp_path / "bot-migration.json").exists()
    with sqlite3.connect(db) as conn:
        assert migration.MARKER not in {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


@pytest.mark.parametrize("legacy", [False, True])
def test_online_readiness_is_query_only_and_closes_connection(tmp_path, monkeypatch, legacy):
    db = tmp_path / "online.sqlite3"
    table = "copilot_runs" if legacy else "bot_runs"
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute(f"CREATE TABLE {table}(value TEXT)")
        conn.execute(f"INSERT INTO {table} VALUES ('preserved')")
    before = db.read_bytes()
    connections = []
    tables = migration._tables

    def connect():
        connection = sqlite3.connect(db)
        connections.append(connection)
        return connection

    def checked_tables(connection):
        assert connection.execute("PRAGMA query_only").fetchone() == (1,)
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute(f"DELETE FROM {table}")
        return tables(connection)

    monkeypatch.setattr(migration, "_tables", checked_tables)
    if legacy:
        with pytest.raises(ValueError, match="Legacy Copilot"):
            migration.assert_bot_ready(db, connect=connect)
    else:
        migration.assert_bot_ready(db, connect=connect)
    assert len(connections) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connections[0].execute("SELECT 1")
    assert db.read_bytes() == before


def test_missing_database_readiness_does_not_open_or_create(tmp_path):
    db = tmp_path / "missing.sqlite3"

    def unexpected_connect():
        pytest.fail("readiness must not create a missing database")

    migration.assert_bot_ready(db, connect=unexpected_connect)
    assert not db.exists()
    (tmp_path / "bot-migration.json").write_text(json.dumps({
        "version": migration.VERSION, "database": str(db),
        "backup": str(db.with_name(db.name + ".pre-bot.sqlite3")), "status": "pending",
    }))
    with pytest.raises(ValueError, match="incomplete"):
        migration.assert_bot_ready(db, connect=unexpected_connect)
    assert not db.exists()
