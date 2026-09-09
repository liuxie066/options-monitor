from __future__ import annotations

import os
import sqlite3
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


@pytest.mark.parametrize("stage", ["stat", "chmod", "readback"])
def test_sqlite_artifact_helper_tolerates_sidecar_disappearing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    database = ensure_private_file(tmp_path / "private" / "inbox.sqlite3")
    journal = Path(f"{database}-journal")
    journal.write_bytes(b"transient")
    real_lstat = Path.lstat
    real_chmod = os.chmod
    stats = 0

    def stat_after_journal_disappears(path: Path):
        nonlocal stats
        if path == journal:
            stats += 1
        if path == journal and (stage == "stat" or (stage == "readback" and stats == 2)):
            journal.unlink()
        return real_lstat(path)

    def chmod_after_journal_disappears(path, mode, **kwargs):
        if path == journal and stage == "chmod":
            journal.unlink()
        return real_chmod(path, mode, **kwargs)

    monkeypatch.setattr(Path, "lstat", stat_after_journal_disappears)
    monkeypatch.setattr(private_storage.os, "chmod", chmod_after_journal_disappears)

    secure_sqlite_artifacts(database)

    assert _mode(database) == 0o600
    assert not journal.exists()


@pytest.mark.parametrize("artifact_kind", ["symlink", "directory", "fifo"])
def test_sqlite_artifact_helper_rejects_unsafe_sidecar(tmp_path: Path, artifact_kind: str) -> None:
    database = ensure_private_file(tmp_path / "private" / "inbox.sqlite3")
    journal = Path(f"{database}-journal")
    if artifact_kind == "symlink":
        outside = tmp_path / "outside.txt"
        outside.write_text("unchanged", encoding="utf-8")
        journal.symlink_to(outside)
        expected = "must not be a symlink"
    elif artifact_kind == "directory":
        journal.mkdir()
        expected = "is not a regular file"
    else:
        os.mkfifo(journal)
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


@pytest.mark.parametrize("artifact_kind", ["symlink", "directory", "fifo"])
def test_sqlite_factory_rejects_unsafe_database(tmp_path: Path, artifact_kind: str) -> None:
    database = tmp_path / "private" / "database.sqlite3"
    database.parent.mkdir()
    outside = tmp_path / "outside"
    outside.write_bytes(b"unchanged")
    outside.chmod(0o644)
    if artifact_kind == "symlink":
        database.symlink_to(outside)
    elif artifact_kind == "directory":
        database.mkdir()
    else:
        os.mkfifo(database)
    with pytest.raises(OSError, match="symlink|regular file"):
        connect_private_sqlite(database)
    assert outside.read_bytes() == b"unchanged"
    assert _mode(outside) == 0o644


@pytest.mark.parametrize("helper", [connect_private_sqlite, secure_sqlite_artifacts])
def test_sqlite_helpers_reject_symlink_parent(tmp_path: Path, helper) -> None:
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o755)
    (outside / "database.sqlite3").touch(mode=0o644)
    link = tmp_path / "link"
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError, match="symlink"):
        helper(link / "database.sqlite3")
    assert _mode(outside) == 0o755
    assert _mode(outside / "database.sqlite3") == 0o644


def test_sqlite_helpers_do_not_open_existing_artifacts(tmp_path: Path, monkeypatch) -> None:
    database = ensure_private_file(tmp_path / "private" / "database.sqlite3")
    for suffix in ("", "-wal", "-shm", "-journal"):
        artifact = Path(f"{database}{suffix}")
        artifact.touch()
        artifact.chmod(0o666)

    def unexpected_open(*args, **kwargs):
        raise AssertionError("permission maintenance must not open existing artifacts")

    monkeypatch.setattr(private_storage.os, "open", unexpected_open)
    with connect_private_sqlite(database) as connection:
        secure_sqlite_artifacts(database)
        assert connection.execute("SELECT 1").fetchone() == (1,)
    for suffix in ("", "-wal", "-shm", "-journal"):
        artifact = Path(f"{database}{suffix}")
        if artifact.exists():
            assert _mode(artifact) == 0o600


@pytest.mark.parametrize("replacement", ["symlink", "regular", "directory"])
def test_sqlite_metadata_rejects_path_replacement(tmp_path: Path, monkeypatch, replacement: str) -> None:
    database = ensure_private_file(tmp_path / "private" / "database.sqlite3")
    outside = tmp_path / "outside"
    outside.write_bytes(b"unchanged")
    outside.chmod(0o644)
    real_chmod = os.chmod

    def replace_before_chmod(path, mode, **kwargs):
        if path == database:
            path.rename(path.with_suffix(".old"))
            if replacement == "symlink":
                path.symlink_to(outside)
            elif replacement == "directory":
                path.mkdir()
            else:
                path.touch()
        assert kwargs == {"follow_symlinks": False}
        return real_chmod(path, mode, **kwargs)

    monkeypatch.setattr(private_storage.os, "chmod", replace_before_chmod)
    with pytest.raises((OSError, NotImplementedError), match="changed|not implemented|not supported|unavailable"):
        secure_sqlite_artifacts(database)
    assert outside.read_bytes() == b"unchanged"
    assert _mode(outside) == 0o644


@pytest.mark.parametrize("error", [PermissionError("denied"), NotImplementedError("no no-follow support")])
def test_sqlite_metadata_errors_are_not_silenced(tmp_path: Path, monkeypatch, error) -> None:
    database = ensure_private_file(tmp_path / "private" / "database.sqlite3")
    journal = Path(f"{database}-journal")
    journal.touch()
    real_chmod = os.chmod

    def failed_chmod(path, mode, **kwargs):
        if path == journal:
            raise error
        return real_chmod(path, mode, **kwargs)

    monkeypatch.setattr(private_storage.os, "chmod", failed_chmod)
    with pytest.raises(type(error), match=str(error)):
        secure_sqlite_artifacts(database)


def test_sqlite_creation_publishes_closed_inode_and_preserves_competing_database(tmp_path: Path, monkeypatch) -> None:
    database = tmp_path / "private" / "database.sqlite3"
    real_mkstemp = private_storage.tempfile.mkstemp
    real_link = os.link
    descriptor = None
    competitor = None

    def tracked_mkstemp(**kwargs):
        nonlocal descriptor
        descriptor, name = real_mkstemp(**kwargs)
        return descriptor, name

    def competing_link(source, target, **kwargs):
        nonlocal competitor
        with pytest.raises(OSError):
            os.fstat(descriptor)
        assert _mode(Path(source)) == 0o600
        assert not target.exists()
        with pytest.raises(sqlite3.OperationalError):
            sqlite3.connect(f"{target.as_uri()}?mode=ro", uri=True)
        # A competing writer publishes its own DB while this creator is preparing.
        competitor = sqlite3.connect(target)
        competitor.execute("CREATE TABLE winner (value TEXT)")
        competitor.execute("INSERT INTO winner VALUES ('preserved')")
        competitor.commit()
        with sqlite3.connect(f"{target.as_uri()}?mode=ro", uri=True) as reader:
            assert reader.execute("SELECT value FROM winner").fetchone() == ("preserved",)
        return real_link(source, target, **kwargs)

    monkeypatch.setattr(private_storage.tempfile, "mkstemp", tracked_mkstemp)
    monkeypatch.setattr(private_storage.os, "link", competing_link)
    try:
        connection = connect_private_sqlite(database)
        try:
            assert connection.execute("SELECT value FROM winner").fetchone() == ("preserved",)
        finally:
            connection.close()
    finally:
        if competitor is not None:
            competitor.close()
    assert not list(database.parent.glob(".*.tmp"))
    assert _mode(database) == 0o600


@pytest.mark.parametrize("stage", ["fchmod", "link"])
def test_sqlite_creation_failure_cleans_temporary_inode(tmp_path: Path, monkeypatch, stage: str) -> None:
    database = tmp_path / "private" / "database.sqlite3"
    real_mkstemp = private_storage.tempfile.mkstemp
    descriptor = None

    def tracked_mkstemp(**kwargs):
        nonlocal descriptor
        descriptor, name = real_mkstemp(**kwargs)
        return descriptor, name

    def fail(*args, **kwargs):
        raise PermissionError("creation failed")

    monkeypatch.setattr(private_storage.tempfile, "mkstemp", tracked_mkstemp)
    monkeypatch.setattr(private_storage.os, stage, fail)
    with pytest.raises(PermissionError, match="creation failed"):
        connect_private_sqlite(database)
    with pytest.raises(OSError):
        os.fstat(descriptor)
    assert not database.exists()
    assert not list(database.parent.glob(".*.tmp"))


def test_sqlite_directory_replacement_does_not_chmod_symlink_destination(tmp_path: Path, monkeypatch) -> None:
    database = ensure_private_file(tmp_path / "private" / "database.sqlite3")
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o755)
    real_chmod = os.chmod

    def replace_before_chmod(path, mode, **kwargs):
        if path == database.parent:
            path.rename(tmp_path / "old-private")
            path.symlink_to(outside, target_is_directory=True)
        assert kwargs == {"follow_symlinks": False}
        return real_chmod(path, mode, **kwargs)

    monkeypatch.setattr(private_storage.os, "chmod", replace_before_chmod)
    with pytest.raises((OSError, NotImplementedError), match="changed|not implemented|not supported|unavailable"):
        secure_sqlite_artifacts(database)
    assert _mode(outside) == 0o755


def test_sqlite_concurrent_creators_use_one_published_inode(tmp_path: Path, monkeypatch) -> None:
    database = tmp_path / "private" / "database.sqlite3"
    barrier = threading.Barrier(4)
    real_link = os.link
    published = []

    def synchronized_link(source, target, **kwargs):
        barrier.wait(timeout=5)
        real_link(source, target, **kwargs)
        published.append(target.stat().st_ino)

    def connect():
        connection = connect_private_sqlite(database)
        try:
            assert connection.execute("SELECT 1").fetchone() == (1,)
            return database.stat().st_ino
        finally:
            connection.close()

    monkeypatch.setattr(private_storage.os, "link", synchronized_link)
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda _: connect(), range(4)))
    assert len(published) == 1
    assert results == published * 4
    assert not list(database.parent.glob(".*.tmp"))
