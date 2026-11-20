import fcntl
import os
import struct
from types import SimpleNamespace

import pytest

from scripts import om_root_upgrade as entry


def test_native_upgrade_guard_carries_through_inherited_descriptor(monkeypatch, tmp_path):
    monkeypatch.setattr(entry, "RUNTIME", tmp_path)
    locks = tmp_path / "locks"
    locks.mkdir()
    guard = locks / "upgrade.lock.guard"
    guard.touch()
    account = SimpleNamespace(pw_uid=os.getuid())
    with guard.open("rb") as reservation, guard.open("rb") as competitor:
        fcntl.flock(reservation.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        fd = os.dup(reservation.fileno())
        assert entry.lock_upgrade_guard(account, fd) == fd
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(competitor.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
            fcntl.flock(competitor.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)


def test_broker_requires_upgrade_session_even_for_deploy_user(monkeypatch):
    monkeypatch.setattr(entry.socket, "SO_PEERCRED", 17, raising=False)
    monkeypatch.setattr(entry.os, "getsid", lambda pid: {21: 15, 22: 16}[pid])
    account = SimpleNamespace(pw_uid=1001)
    child = SimpleNamespace(getsockopt=lambda *_args: struct.pack("3i", 21, 1001, 1001))
    outsider = SimpleNamespace(getsockopt=lambda *_args: struct.pack("3i", 22, 1001, 1001))
    entry.verify_peer(child, account, 15)
    with pytest.raises(ValueError, match="unexpected upgrade peer"):
        entry.verify_peer(outsider, account, 15)


@pytest.mark.parametrize("path", [
    "options-monitor-opend-lx.service",
    "options-monitor-opend-sy.service.d/zzzz-secret-credentials.conf",
    "../options-monitor-tick-hk.service",
    "options-monitor-tick-hk.service.d/other.conf",
    "ssh.service",
])
def test_rejects_unapproved_unit_paths(path):
    with pytest.raises(ValueError, match="scope"):
        entry.unit_path(path)


def test_broker_restarts_only_approved_service(monkeypatch):
    calls = []
    monkeypatch.setattr(entry.subprocess, "run", lambda args, **kwargs: calls.append((args, kwargs)))
    name = "options-monitor-tick-hk.service"
    entry.handle(["-n", "systemctl", "restart", name], "", {}, {name})
    with pytest.raises(ValueError, match="outside plan"):
        entry.handle(["-n", "systemctl", "restart", "ssh.service"], "", {}, {name})
    with pytest.raises(ValueError, match="external OpenD"):
        entry.handle(["-n", "systemctl", "restart", "options-monitor-opend-sy.service"], "", {},
                     {"options-monitor-opend-sy.service"})
    assert calls == [([entry.SYSTEMCTL, "restart", name], {"check": True, "timeout": 60})]


def test_broker_binds_payload_and_denies_unplanned_path(monkeypatch, tmp_path):
    monkeypatch.setattr(entry, "UNITS", tmp_path)
    actual = tmp_path / "options-monitor-tick-hk.service"
    actual.write_text("old")
    safe = []
    written = []
    monkeypatch.setattr(entry, "safe_root_path", lambda path, kind: safe.append(path))
    monkeypatch.setattr(entry, "replace_unit", lambda path, content, allowed: written.append((path, content, allowed)))
    files = {actual.name: {"before": entry.digest(b"old"), "after": entry.digest(b"new")}}
    command = ["-n", "sh", "-c", 'cat > "$1"', "sh", str(actual)]
    entry.handle(command, "new", files, set())
    with pytest.raises(KeyError):
        entry.handle([*command[:-1], str(tmp_path / "options-monitor-quality-refresh.service")], "new", files, set())
    assert safe == [tmp_path]
    assert written == [(actual, b"new", set(files[actual.name].values()))]
    assert actual == entry.unit_path(actual.name)
    assert entry.restricted(["/usr/bin/id", "-u"])[0] == "/usr/bin/setpriv"
