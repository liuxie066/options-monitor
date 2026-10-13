"""Recovery proof preserves its protocol and one query-only SQLite snapshot."""
from contextlib import closing
import hashlib
import sqlite3

import pytest

from src.application.ledger import api, read_only_evidence


# Minimal source tables make every contribution to the legacy proof observable.
TABLES = {
    "position_lots": ("lot_id", "account"),
    "trade_events": ("event_id", "account"),
    "assigned_stock_events": ("stock_event_id", "account"),
    "trade_lifecycle_cases": ("case_id", "account"),
    "trade_lifecycle_evidence": ("evidence_id", "case_id", "account"),
    "trade_lifecycle_source_consumptions": ("source_key", "case_id"),
    "trade_lifecycle_allocations": ("allocation_id", "case_id"),
    "trade_lifecycle_notification_outbox": ("outbox_id", "case_id"),
}
INBOX = {"inbox_id": "fixture", "payload": {"account": "lx", "memo": "旧恢复"}}
STATE = b'{"processed_deal_ids": {}}\n'
LEGACY_HASH = "edb97bb0fbb8c8d5b765fc77435e618a84dde1bc648040329e03167688f23648"


def _source(path, *, wal=False):
    with closing(sqlite3.connect(path)) as conn:
        if wal:
            conn.execute("PRAGMA journal_mode=WAL")
        for table, columns in TABLES.items():
            conn.execute(f"CREATE TABLE {table} ({', '.join(name + ' TEXT' for name in columns)}, evidence TEXT)")
            for account in ("lx", "sy"):
                values = [account if name == "account" else f"case-{account}" if name == "case_id"
                          else f"{account}-{table}" for name in columns] + ["原始"]
                conn.execute(f"INSERT INTO {table} VALUES ({', '.join('?' for _ in values)})", values)
        conn.commit()
    return path


def _snapshot(path, **kwargs):
    return api.read_trade_intake_recovery_snapshot(
        path, **{"account": "lx", "inbox_observation": INBOX, "state_bytes": STATE, **kwargs},
    )


def test_recovery_snapshot_preserves_legacy_hash_without_writable_access(tmp_path, monkeypatch):
    from src.application.ledger.repository import SQLiteOptionPositionsRepository

    path = _source(tmp_path / "ledger.sqlite3")
    before = path.read_bytes()
    connections = []
    statements = []
    original = read_only_evidence._ReadOnlyTradeReconciliationEvidenceRepository._connect

    def connect(reader):
        conn = original(reader)
        assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("CREATE TABLE unexpected_write (id INTEGER)")
        conn.set_trace_callback(statements.append)
        connections.append(conn)
        return conn

    monkeypatch.setattr(read_only_evidence._ReadOnlyTradeReconciliationEvidenceRepository, "_connect", connect)
    monkeypatch.setattr(SQLiteOptionPositionsRepository, "__init__",
                        lambda *a, **k: pytest.fail("recovery query initialized a writer"))
    result = _snapshot(path)
    assert result == {"recovery_hash": LEGACY_HASH, "ledger_row_counts": dict.fromkeys(TABLES, 1)}
    assert len(connections) == 1
    assert statements.count("BEGIN") == 1
    assert not any(sql.upper().startswith(("INSERT", "UPDATE", "DELETE", "CREATE", "ALTER")) for sql in statements)
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connections[0].execute("SELECT 1")
    assert path.read_bytes() == before
    assert set(tmp_path.iterdir()) == {path}


@pytest.mark.parametrize("table", TABLES)
def test_every_recovery_table_is_account_scoped_and_changes_the_proof(tmp_path, table):
    path = _source(tmp_path / "ledger.sqlite3")
    columns = TABLES[table]
    scope = "account" if "account" in columns else "case_id"
    with sqlite3.connect(path) as conn:
        conn.execute(f"UPDATE {table} SET evidence=? WHERE {scope}=?",
                     ("无关变更", "sy" if scope == "account" else "case-sy"))
    assert _snapshot(path)["recovery_hash"] == LEGACY_HASH
    with sqlite3.connect(path) as conn:
        conn.execute(f"UPDATE {table} SET evidence=? WHERE {scope}=?",
                     ("目标变更", "lx" if scope == "account" else "case-lx"))
    assert _snapshot(path)["recovery_hash"] != LEGACY_HASH


def test_recovery_snapshot_binds_inbox_and_exact_state_bytes(tmp_path):
    path = _source(tmp_path / "ledger.sqlite3")
    original = _snapshot(path)["recovery_hash"]
    changed_inbox = _snapshot(path, inbox_observation={**INBOX, "payload_version": 2})["recovery_hash"]
    changed_state = _snapshot(path, state_bytes=STATE.rstrip())["recovery_hash"]
    assert len({original, changed_inbox, changed_state}) == 3


def test_recovery_snapshot_does_not_mix_concurrent_commits(tmp_path, monkeypatch):
    path = _source(tmp_path / "ledger.sqlite3", wal=True)
    original_connect = read_only_evidence.sqlite3.connect
    changed = []

    class InterleavingConnection(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            cursor = super().execute(sql, *args, **kwargs)
            if sql.startswith("SELECT * FROM position_lots") and not changed:
                with closing(original_connect(path)) as writer:
                    writer.execute("BEGIN IMMEDIATE")
                    for table in TABLES:
                        writer.execute(f"UPDATE {table} SET evidence='并发更新'")
                    writer.commit()
                changed.append(True)
            return cursor

    def connect(*args, **kwargs):
        return original_connect(*args, **kwargs, factory=InterleavingConnection)

    with monkeypatch.context() as patch:
        patch.setattr(read_only_evidence.sqlite3, "connect", connect)
        observed = _snapshot(path)
    assert changed == [True]
    assert observed["recovery_hash"] == LEGACY_HASH
    assert _snapshot(path)["recovery_hash"] != LEGACY_HASH


@pytest.mark.parametrize("failure", ["missing_file", "missing_table", "missing_account"])
def test_recovery_snapshot_rejects_missing_sources_without_repair(tmp_path, failure):
    path = tmp_path / "ledger.sqlite3"
    if failure != "missing_file":
        _source(path)
    if failure == "missing_table":
        with sqlite3.connect(path) as conn:
            conn.execute("DROP TABLE trade_lifecycle_notification_outbox")
    before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in tmp_path.iterdir()}
    with pytest.raises((sqlite3.OperationalError, ValueError)):
        _snapshot(path, account="" if failure == "missing_account" else "lx")
    assert {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in tmp_path.iterdir()} == before


def test_empty_account_is_a_valid_zero_count_snapshot(tmp_path):
    path = _source(tmp_path / "ledger.sqlite3")
    result = _snapshot(path, account="unpopulated")
    assert result["ledger_row_counts"] == dict.fromkeys(TABLES, 0)
    assert result["recovery_hash"] != LEGACY_HASH
