import pytest

from scripts import om_root_upgrade as entry


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
