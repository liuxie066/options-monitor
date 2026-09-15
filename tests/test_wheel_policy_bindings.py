from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import sqlite3

import pytest

from domain.domain.decision_state_fingerprint import canonical_sha256
from src.application.ledger.api import read_wheel_activation_windows_read_only
from src.application.ledger.repository import SQLiteOptionPositionsRepository
from src.application.ledger.repository_wheel_policy import TABLE, WINDOW_FIELDS


def _open(repo, *, market="us", account="lx", generation=0, policy="a"):
    with repo._writer_connection(begin_immediate=True) as conn:
        return repo.open_wheel_activation_window(
            market=market, account=account, expected_current_generation=generation,
            policy_hash=policy * 64, request_id=f"enable-{generation}", request_hash="f" * 64, conn=conn,
        )["window"]


def _close(repo):
    with repo._writer_connection(begin_immediate=True) as conn:
        return repo.close_wheel_activation_window(
            market="us", account="lx", expected_current_generation=1, policy_hash="a" * 64,
            request_id="disable", request_hash="e" * 64, conn=conn,
        )["window"]


def _request(repo, *, target="b", request_id="bind-1", market="us", account="lx"):
    window = repo.get_current_wheel_activation_window(market=market, account=account)
    stat = repo.db_path.stat()
    ledger = {"path": str(repo.db_path.resolve()), "device": stat.st_dev, "inode": stat.st_ino}
    return {
        "schema_version": "wheel_policy_rebind_request.v1", "market": market, "account": account,
        "request_id": request_id, "actor": "operator", "window": {k: window[k] for k in WINDOW_FIELDS},
        "expected_revision": window["policy_binding_revision"], "effective_policy_hash": window["effective_policy_hash"],
        "target_policy_hash": target * 64, "ledger": ledger,
        "source": {**ledger, "path": str(repo.db_path.parent / "config.yaml"), "sha256": "c" * 64},
        "runtime": {**ledger, "path": str(repo.db_path.parent / "config.us.json"), "sha256": "d" * 64},
    }


def _append(repo, request):
    with repo._writer_connection(begin_immediate=True) as conn:
        return repo.append_wheel_policy_binding(request=request, request_hash=canonical_sha256(request), conn=conn)


def test_binding_preserves_original_and_historical_window_through_close_and_reenable(tmp_path):
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.db")
    original = _open(repo)
    request = _request(repo)
    receipt = _append(repo, request)
    assert receipt["request"] == request
    assert receipt["request_hash"] == canonical_sha256(request)
    assert receipt["revision"] == 1
    assert _append(repo, request) == receipt
    assert repo.list_wheel_activation_windows(market="us", account="lx") == [original]
    assert repo.get_wheel_activation_window_for_event(market="us", account="lx", occurred_at_ms=original["activated_at_ms"]) == original
    current = repo.get_current_wheel_activation_window(market="us", account="lx")
    assert current == {**original, "effective_policy_hash": "b" * 64, "policy_binding_revision": 1}
    observed = read_wheel_activation_windows_read_only(repo.db_path, "us", "lx")
    assert observed == {"source_status": "available", "windows": [current], "policy_bindings": [receipt]}
    closed = _close(repo)
    assert repo.get_current_wheel_activation_window(market="us", account="lx") is None
    assert repo.list_wheel_policy_bindings(market="us", account="lx") == [receipt]
    with pytest.raises(ValueError, match="superseded"):
        _append(repo, request)
    second = _open(repo, generation=1, policy="c")
    assert repo.list_wheel_activation_windows(market="us", account="lx") == [closed, second]
    assert repo.get_current_wheel_activation_window(market="us", account="lx")["policy_binding_revision"] == 0
    with pytest.raises(ValueError, match="identity conflict"):
        _append(repo, _request(repo, request_id="bind-1"))


def test_revision_cas_aba_scope_and_request_identity(tmp_path):
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.db")
    _open(repo)
    for market, account in (("hk", "lx"), ("us", "sy"), ("hk", "sy")):
        _open(repo, market=market, account=account)
    first = _request(repo)
    stale = {**first, "request_id": "competitor"}
    _append(repo, first)
    with pytest.raises(ValueError, match="CAS conflict"):
        _append(repo, stale)
    _append(repo, _request(repo, target="a", request_id="reverse"))
    with pytest.raises(ValueError, match="superseded"):
        _append(repo, first)
    changed = {**first, "actor": "other"}
    with pytest.raises(ValueError, match="identity conflict"):
        _append(repo, changed)
    assert repo.get_current_wheel_activation_window(market="us", account="lx")["policy_binding_revision"] == 2
    for market, account in (("hk", "lx"), ("us", "sy"), ("hk", "sy")):
        assert repo.list_wheel_policy_bindings(market=market, account=account) == []
        assert repo.get_current_wheel_activation_window(market=market, account=account)["effective_policy_hash"] == "a" * 64


def test_rollback_and_two_writers_have_one_durable_effect(tmp_path):
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.db")
    _open(repo)
    request = _request(repo)
    with pytest.raises(RuntimeError, match="rollback"):
        with repo._writer_connection(begin_immediate=True) as conn:
            repo.append_wheel_policy_binding(request=request, request_hash=canonical_sha256(request), conn=conn)
            raise RuntimeError("rollback")
    assert repo.list_wheel_policy_bindings(market="us", account="lx") == []
    def attempt(request_id):
        try:
            return _append(repo, {**request, "request_id": request_id})
        except ValueError as exc:
            assert "CAS conflict" in str(exc)
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, ("one", "two")))
    assert sum(item is not None for item in results) == 1
    assert len(repo.list_wheel_policy_bindings(market="us", account="lx")) == 1


@pytest.mark.parametrize("mutation", ["UPDATE", "DELETE", "revision", "json", "previous_hash"])
def test_append_only_guards_and_corrupt_chain_fail_closed(tmp_path, mutation):
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.db")
    _open(repo)
    _append(repo, _request(repo))
    with repo._writer_connection(begin_immediate=True) as conn:
        if mutation in {"UPDATE", "DELETE"}:
            sql = f"UPDATE {TABLE} SET actor='other'" if mutation == "UPDATE" else f"DELETE FROM {TABLE}"
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                conn.execute(sql)
            return
        conn.execute("DROP TRIGGER trg_wheel_policy_bindings_update")
        if mutation == "revision":
            conn.execute(f"UPDATE {TABLE} SET revision=3")
        elif mutation == "json":
            conn.execute(f"UPDATE {TABLE} SET request_json='{{}}'")
        else:
            conn.execute(f"UPDATE {TABLE} SET previous_policy_hash=?", ("e" * 64,))
    assert read_wheel_activation_windows_read_only(repo.db_path, "us", "lx")["source_status"] == "unreadable"
    with pytest.raises(ValueError):
        repo.get_current_wheel_activation_window(market="us", account="lx")


def test_legacy_read_does_not_initialize_schema_and_bad_empty_schema_fails(tmp_path):
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.db")
    original = _open(repo)
    with repo._writer_connection(begin_immediate=True) as conn:
        conn.execute(f"DROP TABLE {TABLE}")
    # Keep the WAL pair open, as in the activation read-only regression test.
    # SQLite may create sidecars for a clean WAL database even with mode=ro.
    active_connection = repo._connect()
    try:
        before_names = {p.name for p in tmp_path.iterdir()}
        before = {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file() and not p.name.endswith("-shm")}
        result = read_wheel_activation_windows_read_only(repo.db_path, "us", "lx")
        assert result["windows"] == [{**original, "effective_policy_hash": "a" * 64, "policy_binding_revision": 0}]
        assert result["policy_bindings"] == []
        assert before_names == {p.name for p in tmp_path.iterdir()}
        assert before == {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file() and not p.name.endswith("-shm")}
    finally:
        active_connection.close()
    with sqlite3.connect(repo.db_path) as conn:
        conn.execute(f"CREATE TABLE {TABLE} (bogus TEXT)")
    assert read_wheel_activation_windows_read_only(repo.db_path, "us", "lx")["source_status"] == "unreadable"


def test_request_validates_hash_window_and_ledger_identity(tmp_path):
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.db")
    _open(repo)
    request = _request(repo)
    with repo._writer_connection(begin_immediate=True) as conn:
        with pytest.raises(ValueError, match="hash mismatch"):
            repo.append_wheel_policy_binding(request=request, request_hash="e" * 64, conn=conn)
    for section, key, value in (("ledger", "inode", 0), ("window", "activated_at_ms", 1)):
        changed = deepcopy(request)
        changed[section][key] = value
        with pytest.raises(ValueError, match="conflict"):
            _append(repo, changed)
    assert repo.list_wheel_policy_bindings(market="us", account="lx") == []


@pytest.mark.parametrize("failure", ["revision", "previous_hash", "request_id", "closed"])
def test_database_insert_guard_rejects_stale_or_closed_chain(tmp_path, failure):
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.db")
    _open(repo)
    receipt = _append(repo, _request(repo))
    if failure == "closed":
        _close(repo)
    with repo._writer_connection(begin_immediate=True) as conn:
        row = dict(conn.execute(f"SELECT * FROM {TABLE}").fetchone())
        row.update(revision=2, previous_policy_hash="b" * 64, policy_hash="c" * 64, request_id="new")
        if failure == "revision":
            row["revision"] = 3
        elif failure == "previous_hash":
            row["previous_policy_hash"] = "a" * 64
        elif failure == "request_id":
            row["request_id"] = receipt["request_id"]
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(f"INSERT INTO {TABLE} ({', '.join(row)}) VALUES ({', '.join('?' for _ in row)})", tuple(row.values()))
    assert len(repo.list_wheel_policy_bindings(market="us", account="lx")) == 1


def test_no_drift_and_cross_database_transaction_rejected(tmp_path):
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.db")
    other = SQLiteOptionPositionsRepository(tmp_path / "other.db")
    _open(repo)
    _open(other)
    with pytest.raises(ValueError, match="requires policy drift"):
        _append(repo, _request(repo, target="a"))
    request = _request(repo)
    with other._writer_connection(begin_immediate=True) as conn:
        with pytest.raises(ValueError, match="conflict|ledger mismatch"):
            repo.append_wheel_policy_binding(request=request, request_hash=canonical_sha256(request), conn=conn)
    assert repo.list_wheel_policy_bindings(market="us", account="lx") == []
    assert other.list_wheel_policy_bindings(market="us", account="lx") == []


@pytest.mark.parametrize("legacy_sql", [False, True])
def test_clock_regression_preserves_binding_history_on_close_and_reenable(tmp_path, monkeypatch, legacy_sql):
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.db")
    monkeypatch.setattr("src.application.ledger.repository_assigned_stock.now_ms", lambda: 1_000)
    original = _open(repo)
    monkeypatch.setattr("src.application.ledger.repository_wheel_policy.now_ms", lambda: 3_000)
    receipt = _append(repo, _request(repo))
    monkeypatch.setattr("src.application.ledger.repository_assigned_stock.now_ms", lambda: 2_000)
    if legacy_sql:
        # Old writers know the activation lower bound but not the newer binding receipt.
        with repo._writer_connection(begin_immediate=True) as conn:
            with pytest.raises(sqlite3.IntegrityError, match="close precedes policy binding"):
                conn.execute("""UPDATE wheel_activation_windows SET deactivated_at_ms=2000,
                    deactivation_request_id='legacy-close', deactivation_request_hash=?
                    WHERE market='us' AND account='lx' AND generation=1""", ("e" * 64,))
        assert repo.list_wheel_activation_windows(market="us", account="lx") == [original]
        assert repo.list_wheel_policy_bindings(market="us", account="lx") == [receipt]
    closed = _close(repo)
    assert closed["deactivated_at_ms"] == receipt["created_at_ms"] == 3_000
    assert {key: closed[key] for key in ("activated_at_ms", "policy_hash", "activation_request_id", "activation_request_hash")} == {
        key: original[key] for key in ("activated_at_ms", "policy_hash", "activation_request_id", "activation_request_hash")}
    assert repo.list_wheel_policy_bindings(market="us", account="lx") == [receipt]
    assert read_wheel_activation_windows_read_only(repo.db_path, "us", "lx")["source_status"] == "available"
    assert _close(repo) == closed
    reopened = _open(repo, generation=1, policy="c")
    assert reopened["activated_at_ms"] >= closed["deactivated_at_ms"]
    observed = read_wheel_activation_windows_read_only(repo.db_path, "us", "lx")
    assert observed["source_status"] == "available"
    assert observed["policy_bindings"] == [receipt]
    assert observed["windows"] == [closed, {**reopened, "effective_policy_hash": "c" * 64, "policy_binding_revision": 0}]
