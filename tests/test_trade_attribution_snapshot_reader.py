from contextlib import contextmanager
import sqlite3
from types import SimpleNamespace

import pytest

from src.application.ledger import trade_attribution as mod
from src.application.ledger.repository import SQLiteOptionPositionsRepository


@pytest.fixture(params=["DELETE", "WAL"])
def repo(tmp_path, request):
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    with sqlite3.connect(repo.db_path) as conn:
        assert conn.execute(f"PRAGMA journal_mode={request.param}").fetchone()[0].lower() == request.param.lower()
        conn.execute("CREATE TABLE observation_fixture (value INTEGER NOT NULL)")
        conn.execute("INSERT INTO observation_fixture VALUES (0)")
    return repo


def _commit(repo):
    with sqlite3.connect(repo.db_path) as conn:
        conn.execute("UPDATE observation_fixture SET value=value+1")


def _open(repo):
    return mod.open_trade_attribution_snapshot_reader(repo, account="lx", market="us")


def test_stable_reads_force_and_unrelated_commit(repo, monkeypatch):
    original = mod.read_trade_attribution_snapshot
    calls = []

    def read(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(mod, "read_trade_attribution_snapshot", read)
    with _open(repo) as reader:
        first = reader()
        assert first is not None and "trade_events" in first
        assert reader() is None
        assert reader(force=True) == first
        _commit(repo)  # No selected-table whitelist: every committed table counts.
        assert reader() == first
        assert reader() is None
    assert len(calls) == 3


def test_rollback_does_not_invalidate_and_readers_are_independent(repo):
    with _open(repo) as first, _open(repo) as second:
        assert first() == second()
        with sqlite3.connect(repo.db_path) as conn:
            conn.execute("UPDATE observation_fixture SET value=10")
            conn.rollback()
        assert first() is None and second() is None
        _commit(repo)
        assert first() is not None and first() is None
        assert second() is not None and second() is None


def test_commit_during_materialization_is_not_hidden_by_later_version(repo, monkeypatch):
    original = mod.read_trade_attribution_snapshot
    calls = []

    def concurrent(*args, **kwargs):
        rows = original(*args, **kwargs)
        if not calls:
            _commit(repo)
        calls.append(1)
        return rows

    monkeypatch.setattr(mod, "read_trade_attribution_snapshot", concurrent)
    with _open(repo) as reader:
        assert reader() is not None
        assert reader() is not None
        assert reader() is None
    assert len(calls) == 2


def test_failed_full_read_does_not_advance_observed_version(repo, monkeypatch):
    original = mod.read_trade_attribution_snapshot
    with _open(repo) as reader:
        assert reader() is not None
        _commit(repo)
        monkeypatch.setattr(mod, "read_trade_attribution_snapshot",
                            lambda *a, **kw: (_ for _ in ()).throw(ValueError("fixture failure")))
        with pytest.raises(ValueError, match="fixture failure"):
            reader()
        monkeypatch.setattr(mod, "read_trade_attribution_snapshot", original)
        assert reader() is not None
        assert reader() is None


@pytest.mark.parametrize("change", ["delete", "replace", "symlink"])
def test_bound_store_change_fails_closed(repo, tmp_path, change):
    alias = tmp_path / "alias.sqlite3"
    alias.symlink_to(repo.db_path)
    bound = SimpleNamespace(db_path=alias) if change == "symlink" else repo
    with _open(bound) as reader:
        assert reader() is not None
        if change == "delete":
            repo.db_path.unlink()
        else:
            replacement = SQLiteOptionPositionsRepository(tmp_path / "replacement.sqlite3")
            if change == "symlink":
                alias.unlink()
                alias.symlink_to(replacement.db_path)
            else:
                replacement.db_path.replace(repo.db_path)
        with pytest.raises((ValueError, FileNotFoundError)):
            reader()


@pytest.mark.parametrize("exit_error", [None, ValueError, KeyboardInterrupt])
def test_observer_is_read_only_without_batch_transaction_and_always_closes(repo, monkeypatch, exit_error):
    original = mod._read_only_connection
    connections = []

    @contextmanager
    def tracked(path):
        with original(path) as conn:
            connections.append(conn)
            yield conn

    monkeypatch.setattr(mod, "_read_only_connection", tracked)

    def operation():
        with _open(repo) as reader:
            observer = connections[0]
            assert observer.execute("PRAGMA query_only").fetchone()[0] == 1
            assert reader() is not None
            assert observer.in_transaction is False
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                observer.execute("UPDATE observation_fixture SET value=9")
            # sqlite3 begins an implicit transaction for this test-only DML
            # before query_only rejects it; the reader itself only ran PRAGMAs.
            observer.rollback()
            if exit_error:
                raise exit_error("controlled exit")

    if exit_error:
        with pytest.raises(exit_error, match="controlled exit"):
            operation()
    else:
        operation()
    for conn in connections:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            conn.execute("SELECT 1")


@pytest.mark.parametrize("version_row", [None, [], ["2"], [-1]])
def test_invalid_data_version_fails_without_materializing(repo, monkeypatch, version_row):
    original = mod._read_only_connection

    @contextmanager
    def invalid_version(path):
        with original(path):
            yield SimpleNamespace(execute=lambda sql: SimpleNamespace(fetchone=lambda: version_row))

    monkeypatch.setattr(mod, "_read_only_connection", invalid_version)
    monkeypatch.setattr(mod, "read_trade_attribution_snapshot",
                        lambda *a, **kw: pytest.fail("invalid observation cannot return a snapshot"))
    with _open(repo) as reader:
        with pytest.raises(ValueError, match="data_version is unavailable"):
            reader()


def test_canonical_policy_commit_refreshes_complete_snapshot(repo):
    with _open(repo) as reader:
        before = reader()
        assert before["attribution_policy_enablings"] == []
        with repo._writer_connection(begin_immediate=True) as conn:
            conn.execute("""INSERT INTO trade_attribution_policy_enablings
                (broker, physical_account_id, environment, account, market, policy_version,
                 effective_from_ms, created_at_ms, actor, request_id, request_hash)
                VALUES ('futu', '1001', 'REAL', 'lx', 'us', ?, 2, 1, 'fixture', 'enable', ?)""",
                (mod.ATTRIBUTION_POLICY_VERSION, "a" * 64))
        after = reader()
        assert len(after["attribution_policy_enablings"]) == 1
        assert after["trade_events"] == before["trade_events"]
        assert reader() is None


def test_store_replaced_during_full_read_is_rejected(repo, tmp_path, monkeypatch):
    replacement = SQLiteOptionPositionsRepository(tmp_path / "new-store.sqlite3")
    original = mod.read_trade_attribution_snapshot

    def replaced(*args, **kwargs):
        rows = original(*args, **kwargs)
        replacement.db_path.replace(repo.db_path)
        return rows

    monkeypatch.setattr(mod, "read_trade_attribution_snapshot", replaced)
    with _open(repo) as reader:
        with pytest.raises(ValueError, match="identity changed"):
            reader()
