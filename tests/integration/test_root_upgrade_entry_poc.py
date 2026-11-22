from __future__ import annotations

import hashlib
import json
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from src.application import service_drift, service_upgrade
from tests.service_deploy_test_support import (
    _drift_roots,
    _fake_systemd_query_runner,
    _render_bundle,
    _write_systemd_units_from_bundle,
)

EXTERNAL = tuple(f"options-monitor-opend-{account}.service" for account in ("lx", "sy"))


def qualify(preview):
    if set(preview.get("external_opend_units") or []) != set(EXTERNAL):
        raise ValueError("release does not recognize both external OpenD units")
    touched = (
        preview["extra_managed_units"] + preview["mismatched_units"] + preview["missing_installed_units"]
        + preview["extra_managed_files"] + preview["missing_managed_files"]
        + preview["mismatched_managed_files"] + preview["mode_mismatched_managed_files"]
    )
    if any(unit in name for name in touched for unit in EXTERNAL):
        raise ValueError("release plans an external OpenD mutation")


CLIENT = """
import json, socket, sys
with socket.socket(fileno=int(sys.argv[1])) as stream:
    stream.sendall(sys.stdin.buffer.read())
    stream.shutdown(socket.SHUT_WR)
    response = json.load(stream.makefile("rb"))
    sys.exit(response["rc"])
"""


class LocalPrivilegeSession:
    """Root-side contract fixture. Its destinations and payloads come from the test operator."""

    def __init__(self, units, approved, systemctl, pids):
        self.units = units
        self.approved = approved
        self.systemctl = systemctl
        self.pids = pids
        self.calls = []

    def __call__(self, command, **options):
        if command[0] == "systemctl" and command[1] in {"is-enabled", "is-active", "show"}:
            return _fake_systemd_query_runner()(command, **options)
        parent, child = socket.socketpair()
        with parent, child:
            parent.settimeout(8)
            proc = subprocess.Popen(
                [sys.executable, "-I", "-c", CLIENT, str(child.fileno())],
                stdin=subprocess.PIPE,
                pass_fds=(child.fileno(),),
            )
            with proc:
                child.close()
                assert proc.stdin is not None
                proc.stdin.write(json.dumps([command, options.get("input")]).encode())
                proc.stdin.close()
                with parent.makefile("rb") as reader:
                    args, payload = json.load(reader)
                reply = self.handle(args, payload)
                parent.sendall(json.dumps({"rc": reply}).encode())
                parent.shutdown(socket.SHUT_WR)
                assert proc.wait(timeout=8) == reply
        return subprocess.CompletedProcess(command, reply, "", "permission denied" if reply else "")

    def handle(self, command, content):
        self.calls.append(tuple(command))
        if any(external in token for token in command for external in EXTERNAL):
            return 1
        if command == ["sudo", "-n", "install", "-d", str(self.units)]:
            return 0
        if command[:6] == ["sudo", "-n", "sh", "-c", 'cat > "$1"', "sh"] and len(command) == 7:
            path = Path(command[6])
            if path.parent != self.units or path.is_symlink() or not isinstance(content, str):
                return 1
            source = content.encode()
            if hashlib.sha256(source).hexdigest() not in self.approved.get(path.name, set()):
                return 1
            path.write_bytes(source)
            return 0
        if tuple(command) not in self.systemctl:
            return 1
        if command[-2] == "restart":
            self.pids[command[-1]] += 1
        return 0


def test_root_entry_forward_and_compensated_failure(tmp_path, monkeypatch):
    repo, runtime, unit_root = _drift_roots(tmp_path)
    bundle = _render_bundle(repo, runtime, accounts=["lx", "sy"], markets=["hk"])
    profile = json.loads(next(row["content"] for row in bundle["files"] if row["kind"] == "service_profile"))
    profile["external_opend_units"] = list(EXTERNAL)
    profile_path = runtime / "service.profile.json"
    profile_path.write_text(json.dumps(profile))
    _write_systemd_units_from_bundle(bundle, unit_root)
    opend = {}
    for index, name in enumerate(EXTERNAL):
        unit = unit_root / name
        unit.write_text(f"[Service]\nExecStart=/fixture/opend-{index}\n")
        dropin = unit_root / (name + ".d") / "zzzz-secret-credentials.conf"
        dropin.parent.mkdir()
        dropin.write_text("[Service]\nEnvironment=FIXTURE=1\n")
        opend.update({unit: unit.read_bytes(), dropin: dropin.read_bytes()})
    pids = dict(zip(EXTERNAL, (701, 702), strict=True))

    unit = unit_root / "options-monitor-tick-hk.service"
    pids[unit.name] = 700
    new = unit.read_bytes()
    old = new + b"\n# Previous release fixture\n"
    unit.write_bytes(old)
    broker = LocalPrivilegeSession(
        unit_root,
        {unit.name: {hashlib.sha256(x).hexdigest() for x in (new, old)}},
        {
            ("systemctl", "daemon-reload"),
            ("sudo", "-n", "systemctl", "daemon-reload"),
            ("sudo", "-n", "systemctl", "restart", unit.name),
        },
        pids,
    )
    real_write = Path.write_text

    def deploy_write(path, *args, **kwargs):
        if path.is_relative_to(unit_root):
            raise PermissionError("deployment process cannot write systemd files")
        return real_write(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", deploy_write)
    forwarded = service_drift.service_drift(
        repo_root=repo, runtime_root=runtime, systemd_unit_root=unit_root, run_cmd=broker,
    )
    qualify(forwarded)
    out = service_drift.service_drift(
        repo_root=repo, runtime_root=runtime, systemd_unit_root=unit_root, run_cmd=broker, confirm=True,
    )
    assert out["summary"]["ok"], out["apply_errors"]
    assert out["applied"]["written_units"] == [unit.name]
    assert unit.read_bytes() == new
    assert out["applied"]["retired_units"] == []
    assert not any(name in arg for call in broker.calls for arg in call for name in EXTERNAL)

    paths = {}
    for name in ("old", "new"):
        release = tmp_path / name
        (release / "src/application/bot").mkdir(parents=True)
        (release / "src/application/bot/runtime.py").touch()
        paths[name] = release
    current = tmp_path / "current"
    current.symlink_to(paths["new"])
    live_config = runtime / "generated.json"
    live_config.write_text("new")
    backup = runtime / "backup.json"
    backup.write_text("old")

    def restore_services(**kwargs):
        assert current.resolve() == paths["old"]
        assert live_config.read_text() == "old"
        assert kwargs["activation_policy"] == "preserve-existing"
        assert broker(
            ["sudo", "-n", "sh", "-c", 'cat > "$1"', "sh", str(unit)], input=old.decode()
        ).returncode == 0
        assert broker(["sudo", "-n", "systemctl", "restart", unit.name]).returncode == 0
        return {"ok": True, "restarted_services": [unit.name], "service_health": {"ok": True}}

    monkeypatch.setattr(service_upgrade, "_restore_services_from_release", restore_services)
    result = service_upgrade._compensate_service_transition(
        repo_link=current, previous_dir=paths["old"], runtime_root=runtime, previous_profile=profile,
        config_commit={"artifacts": [{"live_path": str(live_config), "backup_path": str(backup),
                                      "existed_before": True}]},
        restart_services=True, activation_policy="preserve-existing",
        preserved_activation_states={}, run_cmd=broker, operations=[],
    )
    assert result["ok"] and result["symlink_restored"]
    assert unit.read_bytes() == old
    assert pids[unit.name] == 701
    for name in EXTERNAL:
        for args in (["restart", name], ["disable", "--now", name]):
            assert broker(["sudo", "-n", "systemctl", *args]).returncode == 1
    for path in opend:
        assert broker(["sudo", "-n", "rm", "--", str(path)]).returncode == 1
        assert broker(["sudo", "-n", "sh", "-c", 'cat > "$1"', "sh", str(path)], input="replaced").returncode == 1
    assert broker(["sudo", "-n", "sh", "-c", 'cat > "$1"', "sh", str(unit)], input="unapproved").returncode == 1
    assert {p: p.read_bytes() for p in opend} == opend
    assert {name: pids[name] for name in EXTERNAL} == {EXTERNAL[0]: 701, EXTERNAL[1]: 702}


def test_legacy_release_is_rejected_before_switch():
    with pytest.raises(ValueError, match="does not recognize"):
        qualify({"extra_installed_units": list(EXTERNAL)})
