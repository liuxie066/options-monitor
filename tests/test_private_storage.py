from __future__ import annotations

import os
import stat
from pathlib import Path
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from domain.storage.repositories import state_repo
from src.application.ledger import repository_core
from src.application.ledger.repository import SQLiteOptionPositionsRepository
from src.application.trades.inbox import enqueue_trade_payload
from src.infrastructure import private_storage
from src.infrastructure.private_storage import (
    connect_private_sqlite,
    ensure_private_file,
    exclusive_private_file_lock,
    secure_sqlite_artifacts,
)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_sensitive_sqlite_and_audit_artifacts_ignore_permissive_umask(tmp_path: Path) -> None:
    previous_umask = os.umask(0)
    try:
        database = tmp_path / "private" / "inbound.sqlite3"
        with connect_private_sqlite(database) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("CREATE TABLE secret_payload (value TEXT NOT NULL)")
            connection.execute("INSERT INTO secret_payload(value) VALUES ('private-marker')")
            connection.commit()
            assert _mode(database) == 0o600
            assert _mode(database.parent) == 0o700
            for suffix in ("-wal", "-shm"):
                sidecar = Path(f"{database}{suffix}")
                if sidecar.exists():
                    assert _mode(sidecar) == 0o600

        audit_path = state_repo.append_shared_audit_jsonl(
            tmp_path,
            "audit_events.jsonl",
            {"event_type": "private", "action": "tested"},
        )
        assert _mode(audit_path) == 0o600
        assert _mode(audit_path.parent) == 0o700
    finally:
        os.umask(previous_umask)


def test_sqlite_factory_closes_connection_when_initial_hardening_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Connection:
        closed = False

        def close(self) -> None:
            self.closed = True

    connection = Connection()
    monkeypatch.setattr(private_storage.sqlite3, "connect", lambda *_args, **_kwargs: connection)
    monkeypatch.setattr(
        private_storage,
        "secure_sqlite_artifacts",
        lambda _path: (_ for _ in ()).throw(RuntimeError("hardening failed")),
    )

    with pytest.raises(RuntimeError, match="hardening failed"):
        connect_private_sqlite(tmp_path / "private" / "inbound.sqlite3")

    assert connection.closed is True


@pytest.mark.parametrize(
    ("failure_stage", "journal_mode", "expected_hardening_calls"),
    (("invariant", "wal", 1), ("wal", "delete", 1), ("hardening", "wal", 2)),
)
def test_repository_writer_closes_and_hardens_after_connection_initialization_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
    journal_mode: str,
    expected_hardening_calls: int,
) -> None:
    class Cursor:
        def fetchone(self) -> tuple[str]:
            return (journal_mode,)

    class Connection:
        closed = False

        def execute(self, _sql: str) -> Cursor:
            return Cursor()

        def close(self) -> None:
            self.closed = True

    connection = Connection()
    repo = object.__new__(SQLiteOptionPositionsRepository)
    repo.db_path = tmp_path / "private" / "ledger.sqlite3"
    hardening_calls = 0

    def _initialize(_connection: Connection) -> None:
        if failure_stage == "invariant":
            raise RuntimeError("connection invariant failed")

    def _secure(_path: Path) -> None:
        nonlocal hardening_calls
        hardening_calls += 1
        if failure_stage == "hardening" and hardening_calls == 1:
            raise RuntimeError("post-initialize hardening failed")

    monkeypatch.setattr(repository_core, "connect_private_sqlite", lambda _path: connection)
    monkeypatch.setattr(repository_core, "initialize_ledger_connection", _initialize)
    monkeypatch.setattr(repository_core, "secure_sqlite_artifacts", _secure)

    expected_error = {
        "invariant": "connection invariant failed",
        "wal": "SQLite WAL mode is required",
        "hardening": "post-initialize hardening failed",
    }[failure_stage]
    with pytest.raises(RuntimeError, match=expected_error):
        with repo._writer_connection():
            raise AssertionError("writer body must not start")

    assert connection.closed is True
    assert hardening_calls == expected_hardening_calls

    def _reacquire_writer_lock() -> bool:
        with exclusive_private_file_lock(Path(f"{repo.db_path}.writer.lock")):
            return True

    with ThreadPoolExecutor(max_workers=1) as executor:
        assert executor.submit(_reacquire_writer_lock).result(timeout=1) is True


def test_repository_writer_lock_covers_connect_close_and_artifact_hardening(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Cursor:
        def fetchone(self) -> tuple[str]:
            return ("wal",)

    first_body_entered = threading.Event()
    release_first_body = threading.Event()
    first_closed = threading.Event()
    final_hardening_started = threading.Event()
    release_final_hardening = threading.Event()
    second_connected = threading.Event()
    hardening_calls = 0

    class Connection:
        def __init__(self, index: int):
            self.index = index

        def execute(self, _sql: str) -> Cursor:
            return Cursor()

        def commit(self) -> None:
            pass

        def rollback(self) -> None:
            pass

        def close(self) -> None:
            if self.index == 1:
                first_closed.set()

    connections: list[Connection] = []

    def _connect(_path: Path) -> Connection:
        connection = Connection(len(connections) + 1)
        connections.append(connection)
        if connection.index == 2:
            second_connected.set()
        return connection

    def _secure(_path: Path) -> None:
        nonlocal hardening_calls
        hardening_calls += 1
        if hardening_calls == 2:
            final_hardening_started.set()
            assert release_final_hardening.wait(2)

    monkeypatch.setattr(repository_core, "connect_private_sqlite", _connect)
    monkeypatch.setattr(repository_core, "initialize_ledger_connection", lambda _conn: None)
    monkeypatch.setattr(repository_core, "secure_sqlite_artifacts", _secure)
    repo = object.__new__(SQLiteOptionPositionsRepository)
    repo.db_path = tmp_path / "private" / "ledger.sqlite3"

    def _first_writer() -> None:
        with repo._writer_connection():
            first_body_entered.set()
            assert release_first_body.wait(2)

    def _second_writer() -> None:
        with repo._writer_connection():
            pass

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(_first_writer)
        assert first_body_entered.wait(1)
        second = executor.submit(_second_writer)
        try:
            assert not second_connected.wait(0.1)
            release_first_body.set()
            assert first_closed.wait(1)
            assert final_hardening_started.wait(1)
            assert not second_connected.wait(0.1)
        finally:
            release_first_body.set()
            release_final_hardening.set()
        first.result(timeout=1)
        second.result(timeout=1)

    assert second_connected.is_set()


def test_sensitive_file_helper_rejects_symlink_target(tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("unchanged", encoding="utf-8")
    sensitive_dir = tmp_path / "private"
    sensitive_dir.mkdir()
    link = sensitive_dir / "audit.sqlite3"
    link.symlink_to(outside)

    with pytest.raises(OSError, match="must not be a symlink"):
        ensure_private_file(link)

    assert outside.read_text(encoding="utf-8") == "unchanged"


def test_exclusive_private_file_lock_serializes_contenders(tmp_path: Path) -> None:
    lock_path = tmp_path / "private" / "ledger.writer.lock"
    first_entered = threading.Event()
    release_first = threading.Event()
    second_entered = threading.Event()

    def _hold_first() -> None:
        with exclusive_private_file_lock(lock_path):
            first_entered.set()
            assert release_first.wait(2)

    def _enter_second() -> None:
        with exclusive_private_file_lock(lock_path):
            second_entered.set()

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(_hold_first)
        assert first_entered.wait(1)
        second = executor.submit(_enter_second)
        try:
            assert not second_entered.wait(0.1)
        finally:
            release_first.set()
        first.result(timeout=1)
        second.result(timeout=1)

    assert second_entered.is_set()
    assert _mode(lock_path) == 0o600


def test_exclusive_private_file_lock_is_reentrant_in_same_thread(tmp_path: Path) -> None:
    lock_path = tmp_path / "private" / "ledger.writer.lock"

    with exclusive_private_file_lock(lock_path):
        with exclusive_private_file_lock(lock_path, blocking=False):
            assert _mode(lock_path) == 0o600


def test_exclusive_private_file_lock_nonblocking_contender_fails_and_releases(
    tmp_path: Path,
) -> None:
    lock_path = tmp_path / "private" / "ledger.writer.lock"
    first_entered = threading.Event()
    release_first = threading.Event()

    def _hold_first() -> None:
        with exclusive_private_file_lock(lock_path):
            first_entered.set()
            assert release_first.wait(2)

    def _try_second() -> None:
        with pytest.raises(BlockingIOError):
            with exclusive_private_file_lock(lock_path, blocking=False):
                raise AssertionError("contended non-blocking lock must not enter")

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(_hold_first)
        assert first_entered.wait(1)
        try:
            executor.submit(_try_second).result(timeout=1)
        finally:
            release_first.set()
        first.result(timeout=1)

    with exclusive_private_file_lock(lock_path, blocking=False):
        assert _mode(lock_path) == 0o600


def test_exclusive_private_file_lock_releases_after_body_exception(tmp_path: Path) -> None:
    lock_path = tmp_path / "private" / "ledger.writer.lock"
    with pytest.raises(RuntimeError, match="body failed"):
        with exclusive_private_file_lock(lock_path, blocking=False):
            raise RuntimeError("body failed")

    with exclusive_private_file_lock(lock_path, blocking=False):
        assert _mode(lock_path) == 0o600


def test_sqlite_artifact_helper_tolerates_sidecar_disappearing_before_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = ensure_private_file(tmp_path / "private" / "inbox.sqlite3")
    journal = Path(f"{database}-journal")
    journal.write_bytes(b"transient")
    real_open = os.open

    def open_after_journal_disappears(path: str | os.PathLike[str], flags: int) -> int:
        if Path(path) == journal:
            journal.unlink()
        return real_open(path, flags)

    monkeypatch.setattr(private_storage.os, "open", open_after_journal_disappears)

    secure_sqlite_artifacts(database)

    assert _mode(database) == 0o600
    assert not journal.exists()


@pytest.mark.parametrize("artifact_kind", ["symlink", "directory"])
def test_sqlite_artifact_helper_rejects_unsafe_sidecar(tmp_path: Path, artifact_kind: str) -> None:
    database = ensure_private_file(tmp_path / "private" / "inbox.sqlite3")
    journal = Path(f"{database}-journal")
    if artifact_kind == "symlink":
        outside = tmp_path / "outside.txt"
        outside.write_text("unchanged", encoding="utf-8")
        journal.symlink_to(outside)
        expected = "must not be a symlink"
    else:
        journal.mkdir()
        expected = "is not a regular file"

    with pytest.raises(OSError, match=expected):
        secure_sqlite_artifacts(database)


def test_sqlite_artifact_helper_requires_main_database(tmp_path: Path) -> None:
    with pytest.raises(OSError, match="SQLite artifact is missing"):
        secure_sqlite_artifacts(tmp_path / "missing.sqlite3")


def test_option_ledger_ignores_permissive_umask(tmp_path: Path) -> None:
    previous_umask = os.umask(0)
    try:
        database = tmp_path / "ledger" / "option_positions.sqlite3"
        repo = SQLiteOptionPositionsRepository(database)
        assert repo.count_trade_events() == 0

        assert _mode(database.parent) == 0o700
        assert _mode(database) == 0o600
        for suffix in ("-wal", "-shm"):
            sidecar = Path(f"{database}{suffix}")
            if sidecar.exists():
                assert _mode(sidecar) == 0o600
    finally:
        os.umask(previous_umask)


def test_public_cli_entrypoints_set_private_umask() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    for entrypoint in (repo_root / "om", repo_root / "om-agent"):
        lines = entrypoint.read_text(encoding="utf-8").splitlines()
        assert "umask 077" in lines[:5]


def test_trade_inbox_ignores_permissive_umask(tmp_path: Path) -> None:
    previous_umask = os.umask(0)
    try:
        database = tmp_path / "inbox" / "trade_inbox.sqlite3"
        enqueue_trade_payload(
            database,
            payload={"deal_id": "synthetic-deal", "account": "test"},
            source="test",
            broker_deal_key="futu:test:999000000000000001:synthetic-deal",
        )

        assert _mode(database.parent) == 0o700
        assert _mode(database) == 0o600
    finally:
        os.umask(previous_umask)
