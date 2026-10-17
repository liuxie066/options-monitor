from __future__ import annotations

import errno
import multiprocessing
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

import pytest

from src.application import file_locks, service_upgrade as upgrade
from src.application.agent_tool_contracts import AgentToolError
from src.application.agent_tools.runtime_status_impl import _upgrade_lock_info


class LockCancelled(BaseException):
    pass


def _identity(path: Path) -> tuple[int, int]:
    info = path.lstat()
    return info.st_dev, info.st_ino


def _assert_closed(fd: int) -> None:
    with pytest.raises(OSError) as caught:
        os.fstat(fd)
    assert caught.value.errno == errno.EBADF


def _assert_busy(path: Path) -> None:
    with pytest.raises(RuntimeError) as caught:
        with upgrade._UpgradeLock(path):
            pytest.fail("a contender entered the protected body")
    assert str(caught.value) == f"upgrade lock already exists: {path}"


def test_unpublished_pid_does_not_allow_a_second_owner(monkeypatch, tmp_path):
    path = tmp_path / "locks" / "upgrade.lock"
    created = threading.Event()
    publish = threading.Event()
    entered = threading.Event()
    release = threading.Event()
    original_open = upgrade._UpgradeLock._open_lock

    def paused_open(lock):
        fd = original_open(lock)
        created.set()
        if not publish.wait(10):
            os.close(fd)
            raise AssertionError("PID publication barrier timed out")
        return fd

    def hold():
        with upgrade._UpgradeLock(path):
            entered.set()
            assert release.wait(10)

    monkeypatch.setattr(upgrade._UpgradeLock, "_open_lock", paused_open)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(hold)
        try:
            assert created.wait(10)
            identity = _identity(path)
            assert path.read_bytes() == b""
            _assert_busy(path)
            assert _identity(path) == identity
            assert path.read_bytes() == b""
            publish.set()
            assert entered.wait(10)
            _assert_busy(path)
            assert path.read_text() == str(os.getpid())
        finally:
            publish.set()
            release.set()
        future.result(timeout=10)
    assert not path.exists()
    with upgrade._UpgradeLock(path):
        assert path.read_text() == str(os.getpid())


@pytest.mark.parametrize("stale_pid", ["", "not-a-pid", "999999999"])
def test_two_stale_reclaimers_cannot_both_enter(monkeypatch, tmp_path, stale_pid):
    path = tmp_path / "upgrade.lock"
    path.write_text(stale_pid)
    inspected = threading.Event()
    reclaim = threading.Event()
    entered = threading.Event()
    release = threading.Event()
    original_stale = upgrade._upgrade_lock_is_stale
    main_thread = threading.get_ident()

    def paused_stale(candidate):
        result = original_stale(candidate)
        if threading.get_ident() != main_thread:
            inspected.set()
            assert reclaim.wait(10)
        return result

    def hold():
        with upgrade._UpgradeLock(path):
            entered.set()
            assert release.wait(10)

    monkeypatch.setattr(upgrade, "_upgrade_lock_is_stale", paused_stale)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(hold)
        try:
            assert inspected.wait(10)
            identity = _identity(path)
            _assert_busy(path)
            assert _identity(path) == identity
            assert path.read_text() == stale_pid
            reclaim.set()
            assert entered.wait(10)
            _assert_busy(path)
            assert path.read_text() == str(os.getpid())
        finally:
            reclaim.set()
            release.set()
        future.result(timeout=10)
    assert not path.exists()


@pytest.mark.parametrize("replacement", ["file", "symlink", "missing"])
def test_exit_preserves_replacement_path_identity(tmp_path, replacement):
    path = tmp_path / "upgrade.lock"
    owner = upgrade._UpgradeLock(path)
    detached = tmp_path / "detached-pid"
    with owner:
        fd = owner.fd
        path.rename(detached)
        if replacement == "file":
            path.write_text("successor-pid")
        elif replacement == "symlink":
            path.symlink_to(detached)
        expected_identity = _identity(path) if replacement != "missing" else None
        expected_bytes = path.read_bytes() if replacement != "missing" else None
    _assert_closed(fd)
    assert detached.read_text() == str(os.getpid())
    if replacement == "missing":
        assert not path.exists()
    else:
        assert _identity(path) == expected_identity
        assert path.read_bytes() == expected_bytes
        assert path.is_symlink() is (replacement == "symlink")
    with file_locks.single_instance_lock(path.with_name(path.name + ".guard"), busy_message="busy"):
        pass


def test_guard_stays_held_until_pid_cleanup_finishes(monkeypatch, tmp_path):
    path = tmp_path / "upgrade.lock"
    cleaning = threading.Event()
    release = threading.Event()
    original_unlink = Path.unlink

    def paused_unlink(candidate, *args, **kwargs):
        if candidate == path:
            cleaning.set()
            assert release.wait(10)
        return original_unlink(candidate, *args, **kwargs)

    def hold_and_exit():
        with upgrade._UpgradeLock(path):
            pass

    monkeypatch.setattr(Path, "unlink", paused_unlink)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(hold_and_exit)
        try:
            assert cleaning.wait(10)
            assert path.read_text() == str(os.getpid())
            with pytest.raises(AgentToolError) as caught:
                with file_locks.single_instance_lock(path.with_name(path.name + ".guard"), busy_message="busy"):
                    pytest.fail("guard released before PID cleanup")
            assert caught.value.code == "RESOURCE_BUSY"
        finally:
            release.set()
        future.result(timeout=10)
    assert not path.exists()


def test_repeated_and_non_owner_exit_cannot_release_a_successor(tmp_path):
    path = tmp_path / "upgrade.lock"
    former = upgrade._UpgradeLock(path)
    with former:
        pass
    with upgrade._UpgradeLock(path):
        identity, content = _identity(path), path.read_bytes()
        former.__exit__(None, None, None)
        upgrade._UpgradeLock(path).__exit__(None, None, None)
        assert _identity(path) == identity
        assert path.read_bytes() == content
        _assert_busy(path)


def test_same_object_failed_reentry_keeps_outer_resources(tmp_path):
    path = tmp_path / "upgrade.lock"
    owner = upgrade._UpgradeLock(path)
    with owner:
        fd = owner.fd
        identity, content = _identity(path), path.read_bytes()
        with pytest.raises(RuntimeError, match="upgrade lock already exists"):
            owner.__enter__()
        assert owner.fd == fd
        assert os.fstat(fd).st_ino == identity[1]
        assert _identity(path) == identity
        assert path.read_bytes() == content
        _assert_busy(path)
    _assert_closed(fd)
    with owner:
        assert path.read_text() == str(os.getpid())


def _hold_lock_in_child(path: str, connection) -> None:
    try:
        with upgrade._UpgradeLock(Path(path)):
            connection.send(os.getpid())
            if not connection.poll(20):
                raise AssertionError("child release command timed out")
            assert connection.recv() == "release"
    finally:
        connection.close()


@pytest.mark.parametrize("abrupt", [False, True], ids=["normal-exit", "process-death"])
def test_real_process_exclusion_and_exit_recovery(tmp_path, abrupt):
    path = tmp_path / "upgrade.lock"
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    process = context.Process(target=_hold_lock_in_child, args=(str(path), child))
    process.start()
    child.close()
    try:
        assert parent.poll(20), "child never acquired lock"
        pid = parent.recv()
        assert pid == process.pid
        assert path.read_text() == str(pid)
        guard_identity = _identity(path.with_name(path.name + ".guard"))
        _assert_busy(path)
        if abrupt:
            process.terminate()
        else:
            parent.send("release")
        process.join(10)
        assert not process.is_alive()
        assert (process.exitcode != 0) if abrupt else (process.exitcode == 0)
        if abrupt:
            assert path.read_text() == str(pid)
        else:
            assert not path.exists()
        with upgrade._UpgradeLock(path):
            assert path.read_text() == str(os.getpid())
            assert _identity(path.with_name(path.name + ".guard")) == guard_identity
    finally:
        if process.is_alive():
            process.terminate()
            process.join(5)
        if process.is_alive():
            process.kill()
            process.join(5)
        parent.close()
        assert not process.is_alive()
        process.close()


def test_different_runtime_roots_do_not_contend(tmp_path):
    first = tmp_path / "first" / "locks" / "upgrade.lock"
    second = tmp_path / "second" / "locks" / "upgrade.lock"
    with upgrade._UpgradeLock(first), upgrade._UpgradeLock(second):
        assert first.read_text() == second.read_text() == str(os.getpid())
        _assert_busy(first)
        _assert_busy(second)


@pytest.mark.parametrize("failure", [OSError("PID write failed"), LockCancelled("cancelled"), "zero"])
def test_failed_pid_publication_closes_fd_and_guard(monkeypatch, tmp_path, failure):
    path = tmp_path / "upgrade.lock"
    owner = upgrade._UpgradeLock(path)
    captured = []
    original_write = os.write

    def fail_write(fd, content):
        if fd == owner.fd:
            captured.append(fd)
            if failure == "zero":
                return 0
            raise failure
        return original_write(fd, content)

    with monkeypatch.context() as patch:
        patch.setattr(upgrade.os, "write", fail_write)
        expected = OSError if failure == "zero" else type(failure)
        with pytest.raises(expected):
            with owner:
                pytest.fail("failed publication entered the body")
    assert len(captured) == 1
    _assert_closed(captured[0])
    assert not path.exists()
    with owner:
        assert path.read_text() == str(os.getpid())


def test_short_pid_writes_publish_complete_pid(monkeypatch, tmp_path):
    path = tmp_path / "upgrade.lock"
    owner = upgrade._UpgradeLock(path)
    original_write = os.write
    chunks = []

    def short_write(fd, content):
        if fd == owner.fd:
            chunks.append(content[:1])
            return original_write(fd, content[:1])
        return original_write(fd, content)

    monkeypatch.setattr(upgrade.os, "write", short_write)
    with owner:
        assert path.read_text() == str(os.getpid())
        assert b"".join(chunks) == str(os.getpid()).encode()
        _assert_busy(path)
    assert not path.exists()


@pytest.mark.parametrize("stage", ["guard-open", "guard-flock", "pid-open"])
def test_acquisition_io_failure_preserves_pid_and_releases_guard(monkeypatch, tmp_path, stage):
    path = tmp_path / "upgrade.lock"
    path.write_text("999999999")
    guard = path.with_name(path.name + ".guard")
    identity = _identity(path)
    captured = []
    original_open = Path.open
    original_os_open = os.open
    original_flock = file_locks.fcntl.flock

    def fail_guard_open(candidate, *args, **kwargs):
        if candidate == guard:
            raise OSError("guard open failed")
        return original_open(candidate, *args, **kwargs)

    def fail_flock(fd, operation):
        if operation == file_locks.fcntl.LOCK_EX | file_locks.fcntl.LOCK_NB:
            captured.append(fd)
            raise OSError("guard flock failed")
        return original_flock(fd, operation)

    def fail_pid_open(candidate, *args, **kwargs):
        if os.fspath(candidate) == str(path):
            raise OSError("PID open failed")
        return original_os_open(candidate, *args, **kwargs)

    with monkeypatch.context() as patch:
        if stage == "guard-open":
            patch.setattr(Path, "open", fail_guard_open)
        elif stage == "guard-flock":
            patch.setattr(file_locks.fcntl, "flock", fail_flock)
        else:
            patch.setattr(upgrade.os, "open", fail_pid_open)
        with pytest.raises(OSError, match="failed"):
            with upgrade._UpgradeLock(path):
                pytest.fail("failed acquisition entered the body")
    for fd in captured:
        _assert_closed(fd)
    assert _identity(path) == identity
    assert path.read_text() == "999999999"
    with upgrade._UpgradeLock(path):
        assert path.read_text() == str(os.getpid())


def test_non_busy_guard_error_is_not_translated(monkeypatch, tmp_path):
    error = AgentToolError(code="INPUT_ERROR", message="guard rejected")

    @contextmanager
    def rejected_guard(*args, **kwargs):
        raise error
        yield

    monkeypatch.setattr(upgrade, "single_instance_lock", rejected_guard)
    path = tmp_path / "upgrade.lock"
    with pytest.raises(AgentToolError) as caught:
        with upgrade._UpgradeLock(path):
            pytest.fail("guard error entered body")
    assert caught.value is error
    assert not path.exists()


@pytest.mark.parametrize("failure", [
    RuntimeError("body failed"), LockCancelled("cancelled"),
    AgentToolError(code="RESOURCE_BUSY", message="body failure, not guard contention"),
])
def test_body_failure_releases_owned_resources(tmp_path, failure):
    path = tmp_path / "upgrade.lock"
    with pytest.raises(type(failure)) as caught:
        with upgrade._UpgradeLock(path) as owner:
            fd = owner.fd
            raise failure
    assert caught.value is failure
    _assert_closed(fd)
    assert not path.exists()
    with upgrade._UpgradeLock(path):
        pass


@pytest.mark.parametrize("stage", ["lstat", "unlink", "close"])
def test_pid_cleanup_error_still_closes_fd_and_releases_guard(monkeypatch, tmp_path, stage):
    path = tmp_path / "upgrade.lock"
    owner = upgrade._UpgradeLock(path)
    owner.__enter__()
    fd = owner.fd
    original_lstat, original_unlink, original_close = Path.lstat, Path.unlink, os.close

    def fail_lstat(candidate, *args, **kwargs):
        if candidate == path:
            raise OSError("PID lstat failed")
        return original_lstat(candidate, *args, **kwargs)

    def fail_unlink(candidate, *args, **kwargs):
        if candidate == path:
            raise OSError("PID unlink failed")
        return original_unlink(candidate, *args, **kwargs)

    def fail_close(descriptor):
        original_close(descriptor)
        if descriptor == fd:
            raise OSError("PID close failed")

    with monkeypatch.context() as patch:
        if stage == "lstat":
            patch.setattr(Path, "lstat", fail_lstat)
        elif stage == "unlink":
            patch.setattr(Path, "unlink", fail_unlink)
        else:
            patch.setattr(upgrade.os, "close", fail_close)
        with pytest.raises(OSError, match="PID .* failed"):
            owner.__exit__(None, None, None)
    _assert_closed(fd)
    with file_locks.single_instance_lock(path.with_name(path.name + ".guard"), busy_message="busy"):
        pass
    path.write_text("999999999")
    with upgrade._UpgradeLock(path):
        pass


@pytest.mark.parametrize("stale_pid", ["", "not-a-pid", "0", "-1", "999999999"])
def test_stale_pid_recovery_preserves_observer_and_guard_identity(tmp_path, stale_pid):
    path = tmp_path / "locks" / "upgrade.lock"
    path.parent.mkdir(mode=0o750)
    directory_mode = path.parent.stat().st_mode
    path.write_text(stale_pid)
    with upgrade._UpgradeLock(path):
        assert _upgrade_lock_info(path) == {
            "exists": True, "pid": os.getpid(), "active": True, "stale": False,
        }
        guard = path.with_name(path.name + ".guard")
        guard_identity = _identity(guard)
    assert _upgrade_lock_info(path) == {
        "exists": False, "pid": None, "active": False, "stale": False,
    }
    assert _identity(guard) == guard_identity
    assert path.parent.stat().st_mode == directory_mode
    with upgrade._UpgradeLock(path):
        assert _identity(guard) == guard_identity
    assert not path.exists()


@pytest.mark.parametrize("permission_denied", [False, True])
def test_active_legacy_pid_is_preserved_without_guard_ownership(monkeypatch, tmp_path, permission_denied):
    path = tmp_path / "upgrade.lock"
    content = f"{os.getpid()}\n"
    path.write_text(content)
    identity = _identity(path)
    original_kill = os.kill

    def denied(pid, signal):
        if pid == os.getpid() and signal == 0:
            raise PermissionError("legacy process belongs to another user")
        return original_kill(pid, signal)

    if permission_denied:
        monkeypatch.setattr(upgrade.os, "kill", denied)
    _assert_busy(path)
    assert _identity(path) == identity
    assert path.read_text() == content
    with file_locks.single_instance_lock(path.with_name(path.name + ".guard"), busy_message="busy"):
        pass
