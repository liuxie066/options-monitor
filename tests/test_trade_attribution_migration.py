import sqlite3

import pytest

from src.application.ledger import repository_core
from src.application.ledger.repository import SQLiteOptionPositionsRepository
from src.application.ledger.trade_attribution_migration import (
    preview_trade_attribution_migration, apply_trade_attribution_migration,
)
from src.application.ledger.repository_schema import initialize_ledger_connection


def test_migration_requires_drain_fresh_manifest_and_verified_backup(tmp_path, monkeypatch):
    path = tmp_path / "ledger.sqlite3"
    with monkeypatch.context() as old:
        old.setattr(repository_core, "_WHEEL_EVENT_TYPES_V2", tuple(
            item for item in repository_core._WHEEL_EVENT_TYPES_V2 if not item.startswith("wheel_attribution_")))
        SQLiteOptionPositionsRepository(path)
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TABLE trade_attribution_policy_enablings")
        conn.execute("CREATE INDEX custom_wheel_index ON wheel_events(event_type)")
    with pytest.raises(RuntimeError, match="legacy"):
        SQLiteOptionPositionsRepository(path)
    manifest = preview_trade_attribution_migration(path)
    assert not manifest["wheel_schema_current"]
    assert not manifest["policy_schema_present"]
    backup = tmp_path / "before.sqlite3"
    with pytest.raises(ValueError, match="drain"):
        apply_trade_attribution_migration(path, manifest=manifest, backup_path=backup, writers_stopped=False)
    assert not backup.exists()
    with pytest.raises(ValueError, match="stale"):
        apply_trade_attribution_migration(path, manifest={**manifest, "content_hash": "wrong"},
                                         backup_path=backup, writers_stopped=True)
    assert not backup.exists()
    result = apply_trade_attribution_migration(path, manifest=manifest, backup_path=backup, writers_stopped=True)
    assert result["status"] == "applied" and result["rules_enabled"] is False
    assert preview_trade_attribution_migration(backup)["content_hash"] == manifest["content_hash"]
    assert backup.stat().st_mode & 0o777 == 0o600
    SQLiteOptionPositionsRepository(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='custom_wheel_index'").fetchone()
        assert conn.execute("SELECT COUNT(*) FROM trade_attribution_policy_enablings").fetchone()[0] == 0
        with pytest.raises(sqlite3.OperationalError, match="om_trade_attribution_writer_v1"):
            conn.execute("DELETE FROM combo_pair_inferences")
        initialize_ledger_connection(conn)
        conn.execute("DELETE FROM combo_pair_inferences")


def test_attribution_enable_public_cli_preview_apply_and_replay(tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace
    from src.application.trades import auto_intake, attribution
    from src.application.ledger.api import read_trade_attribution_policy
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    config = {"market": "us", "accounts": ["lx"], "account_settings": {"lx": {"futu": {"account_id": "1001", "trd_env": "REAL"}}}}
    monkeypatch.setattr(auto_intake, "load_config", lambda **_: config)
    monkeypatch.setattr(auto_intake, "resolve_runtime_root", lambda **_: SimpleNamespace(runtime_root=tmp_path))
    monkeypatch.setattr(attribution, "resolve_position_data_config_path", lambda **_: tmp_path / "data.json")
    monkeypatch.setattr(attribution, "resolve_ledger_store", lambda *a, **k: SimpleNamespace(sqlite_path=repo.db_path, runtime_root=tmp_path))
    monkeypatch.setattr(attribution, "ledger_store_write_guard", lambda *a, **k: {"ok": True})
    monkeypatch.setattr("time.time", lambda: 1)
    args = ["attribution-enable", "--config", str(tmp_path / "config.us.json"), "--account", "lx",
            "--actor", "operator", "--request-id", "enable-v1", "--effective-from-ms", "2000"]
    scope = {"broker": "futu", "physical_account_id": "1001", "environment": "REAL", "account": "lx", "market": "us"}
    assert auto_intake.main([*args, "--dry-run"]) == 0
    assert read_trade_attribution_policy(repo, scope=scope) is None
    assert auto_intake.main([*args, "--apply"]) == 2
    assert read_trade_attribution_policy(repo, scope=scope) is None
    assert auto_intake.main([*args, "--apply", "--confirm"]) == 0
    first = read_trade_attribution_policy(repo, scope=scope)
    assert first["effective_from_ms"] == 2000
    assert auto_intake.main([*args, "--apply", "--confirm"]) == 0
    assert read_trade_attribution_policy(repo, scope=scope) == first
    assert auto_intake.main([*args, "--once"]) == 2


def _populated_legacy_ledger(tmp_path, monkeypatch):
    from test_wheel_workflows import _wheel_repo
    with monkeypatch.context() as old:
        old.setattr(repository_core, "_WHEEL_EVENT_TYPES_V2", tuple(
            item for item in repository_core._WHEEL_EVENT_TYPES_V2 if not item.startswith("wheel_attribution_")))
        repo, _branch = _wheel_repo(tmp_path)
    with sqlite3.connect(repo.db_path) as conn:
        conn.execute("DROP TABLE trade_attribution_policy_enablings")
        for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'trg_attribution_writer_%'").fetchall():
            conn.execute(f'DROP TRIGGER "{name}"')
        assert conn.execute("SELECT COUNT(*) FROM wheel_events").fetchone()[0] > 0
        assert conn.execute("SELECT COUNT(*) FROM wheel_activation_windows").fetchone()[0] > 0
    return repo.db_path


def test_populated_migration_preserves_facts_and_fences_existing_connection(tmp_path, monkeypatch):
    path = _populated_legacy_ledger(tmp_path, monkeypatch)
    with sqlite3.connect(path) as stale:
        def facts():
            return {table: stale.execute(f"SELECT * FROM {table}").fetchall()
                    for table in ("trade_events", "wheel_events", "wheel_activation_windows")}
        before = facts()
        stale.execute("DELETE FROM combo_pair_inferences")
        stale.commit()
        manifest = preview_trade_attribution_migration(path)
        result = apply_trade_attribution_migration(path, manifest=manifest,
            backup_path=tmp_path / "before.sqlite3", writers_stopped=True)
        assert result["status"] == "applied" and facts() == before
        with pytest.raises(sqlite3.OperationalError, match="om_trade_attribution_writer_v1"):
            stale.execute("DELETE FROM combo_pair_inferences")
        stale.rollback()
    assert SQLiteOptionPositionsRepository(path).list_position_lots()


@pytest.mark.parametrize("failure", ["rebuild", "projection", "readback"])
def test_populated_migration_failure_has_verified_backup_and_defined_commit_boundary(tmp_path, monkeypatch, failure):
    from src.application.ledger import trade_attribution_migration as migration
    path = _populated_legacy_ledger(tmp_path, monkeypatch)
    manifest = preview_trade_attribution_migration(path)
    backup = tmp_path / "before.sqlite3"
    function = {"rebuild": "_create_wheel_events_v2_guards", "projection": "run_position_projection_in_transaction",
                "readback": "preview_trade_attribution_migration"}[failure]
    original = getattr(migration, function)
    def injected(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("injected failure after " + failure)
    monkeypatch.setattr(migration, function, injected)
    with pytest.raises(RuntimeError, match="injected failure"):
        apply_trade_attribution_migration(path, manifest=manifest, backup_path=backup, writers_stopped=True)
    assert preview_trade_attribution_migration(backup)["content_hash"] == manifest["content_hash"]
    after = preview_trade_attribution_migration(path)
    if failure == "readback":
        assert after["wheel_schema_current"] and after["policy_schema_present"]
        assert after["content_hash"] != manifest["content_hash"]
        SQLiteOptionPositionsRepository(path)
    else:
        assert after == manifest
