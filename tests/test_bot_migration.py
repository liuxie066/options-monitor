from __future__ import annotations

import hashlib
import json
import sqlite3

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
    from src.application.assistant.settings import AssistantSettings
    from src.application.bot.host_store import BotHostStore
    db, configs = _fixture(tmp_path)
    with pytest.raises(ValueError, match="retired"):
        AssistantSettings.from_runtime_config({"assistant": {"copilot": {}}})
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


def test_pi_store_is_read_only_and_pending_inventory_is_bound(tmp_path, monkeypatch):
    db, configs = _fixture(tmp_path)
    pi = tmp_path / "pi.sqlite3"
    with sqlite3.connect(pi) as conn:
        conn.execute("CREATE TABLE sessions(id TEXT, body TEXT)")
        conn.execute("INSERT INTO sessions VALUES ('unchanged-session-id', 'Copilot old message')")
    before = pi.read_bytes()
    write = migration.atomic_write_private_text
    def interrupted(path, content):
        if path == configs[0]:
            raise OSError("fixture interruption")
        return write(path, content)
    monkeypatch.setattr(migration, "atomic_write_private_text", interrupted)
    with pytest.raises(OSError):
        migration.migrate_bot(host_db=db, config_paths=configs, pi_paths=[pi], apply=True, writers_stopped=True)
    monkeypatch.setattr(migration, "atomic_write_private_text", write)
    assert pi.read_bytes() == before
    with pytest.raises(ValueError, match="Pi database inventory"):
        migration.migrate_bot(host_db=db, config_paths=configs)
    migration.migrate_bot(host_db=db, config_paths=configs, pi_paths=[pi], apply=True, writers_stopped=True)
    assert pi.read_bytes() == before


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
