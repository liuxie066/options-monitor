from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import subprocess
import tarfile
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from src.application import opend_upgrade_receipt, service_deploy
from src.interfaces.cli import main as cli
from tests.test_service_opend_plan_safety import _fixture, _profile


ACCOUNTS = ("retirement", "family-hk")
UNITS = [f"options-monitor-opend-{account}.service" for account in ACCOUNTS]
OLD = "Futu_OpenD_10.12.7208_Ubuntu18.04"
NEW = "Futu_OpenD_10.12.7300_Ubuntu18.04"


@pytest.fixture
def instances(tmp_path, monkeypatch):
    from om_market_opend import upgrade

    entries = []
    for unit, port in zip(UNITS, (32117, 32462), strict=True):
        root = tmp_path / f"gateway-{port}"
        root.mkdir()
        current = root / "current"
        current.symlink_to(OLD)
        (root / "FutuOpenD.xml").write_text(
            f"<config><ip>127.0.0.1</ip><api_port>{port}</api_port></config>"
        )
        entries.append(upgrade.OpenDInstance(unit, current, port, OLD))
    monkeypatch.setattr(upgrade.platform, "system", lambda: "Linux")
    monkeypatch.setattr(upgrade.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(upgrade.os, "geteuid", lambda: 501)
    monkeypatch.setattr(upgrade.pwd, "getpwnam", lambda name: SimpleNamespace(pw_uid=501))
    monkeypatch.setattr(upgrade, "_maintenance_window", lambda: True)
    return upgrade, entries


@pytest.mark.parametrize("action", ["preview", "current", "apply", "rollback", "busy"])
def test_upgrade_preview_staging_switch_and_rollback(tmp_path, monkeypatch, instances, action):
    upgrade, entries = instances
    package = tmp_path / NEW
    package.mkdir()
    for name in ("FutuOpenD", "AppData.dat", "FutuOpenD.xml"):
        (package / name).write_text("fixture")
    latest = OLD if action == "current" else NEW
    official = f"https://softwaredownload.futunn.com/{latest}.tar.gz"
    commands = []

    def run(*args, **kwargs):
        commands.append(args)
        if args[:2] == ("systemctl", "show"):
            entry = next(item for item in entries if item.unit == args[2])
            return {"WorkingDirectory": str(entry.current), "User": "om-runner"}[args[4]]
        if "--head" in args:
            return official
        if "--output" in args:
            with tarfile.open(args[args.index("--output") + 1], "w:gz") as archive:
                archive.add(package, arcname=NEW)
        if args[:2] == ("systemctl", "list-units") and action == "busy":
            return "options-monitor-tick-us.service loaded active running"
        if args[:4] == ("sudo", "-n", "systemctl", "restart"):
            assert all((item.current.parent / NEW / "AppData.dat").is_file() for item in entries)
            if action == "rollback" and args[-1] == UNITS[1] and entries[1].current.readlink() == Path(NEW):
                raise subprocess.CalledProcessError(1, args)
        return ""

    verified = []
    monkeypatch.setattr(upgrade, "_run", run)
    monkeypatch.setattr(upgrade, "_verify", lambda item, release: verified.append((item, release)))
    result = upgrade.run_upgrade(units=UNITS, runtime_root=tmp_path, confirm=action != "preview")

    assert result["status"] == {
        "preview": "update_available", "current": "already_current", "apply": "upgraded",
        "rollback": "upgrade_failed", "busy": "blocked",
    }[action]
    assert verified[:2] == [(item, OLD) for item in entries]
    assert [item.current.readlink().name for item in entries] == [NEW if action == "apply" else OLD] * 2
    if action in {"preview", "current", "busy"}:
        assert not any("sudo" in args for args in commands)
        assert not any((item.current.parent / NEW).exists() for item in entries)
    else:
        assert all(not (item.current.parent / NEW / "FutuOpenD.xml").exists() for item in entries)
    if action == "apply":
        assert result["verified"] == UNITS
        assert verified[2:] == [(item, NEW) for item in entries]
    if action == "rollback":
        assert result["rollback_verified"] == dict.fromkeys(UNITS, True)
        assert verified[-2:] == [(item, OLD) for item in reversed(entries)]


def test_timer_is_opt_in_and_round_trips_profile(tmp_path):
    _, _, _, kwargs = _fixture(tmp_path, "yaml", accounts=ACCOUNTS)
    kwargs.update(deploy_user="om", include_opend=False)
    baseline = service_deploy.render_service_bundle(**kwargs)
    assert service_deploy.OPEND_UPGRADE_TIMER not in {item["name"] for item in _profile(baseline)["services"]}
    bundle = service_deploy.render_service_bundle(**kwargs, opend_upgrade_units=UNITS,
                                                  opend_upgrade_send_receipt=True,
                                                  include_secret_credentials=True)
    profile = _profile(bundle)
    assert profile["opend_upgrade"] == {"units": UNITS, "send_receipt": True}
    assert profile["restart"]["services"] and service_deploy.OPEND_UPGRADE_SERVICE not in profile["restart"]["services"]
    assert profile["secret_credentials"]["service_credentials"][service_deploy.OPEND_UPGRADE_SERVICE] == [
        service_deploy.FEISHU_BOT_APP_SECRET
    ]
    rebuilt = service_deploy.render_service_bundle_from_profile(
        profile, provider="systemd", repo_root=kwargs["repo_root"], runtime_root=kwargs["runtime_root"])

    def definitions(rendered):
        return {Path(item["relative_path"]).name: item["content"] for item in rendered["files"]
                if Path(item["relative_path"]).name in {
                    service_deploy.OPEND_UPGRADE_TIMER, service_deploy.OPEND_UPGRADE_SERVICE,
                }}

    units = definitions(bundle)
    assert definitions(rebuilt) == units
    assert "OnCalendar=Sun *-*-* 10:00:00 Asia/Shanghai" in units[service_deploy.OPEND_UPGRADE_TIMER]
    assert "Persistent=false" in units[service_deploy.OPEND_UPGRADE_TIMER]
    assert "--send-receipt" in units[service_deploy.OPEND_UPGRADE_SERVICE]


def test_cli_preview_has_no_receipt_and_confirm_delivers_once(tmp_path, monkeypatch, capsys):
    class Sunday(datetime):
        @classmethod
        def now(cls, tz):
            return cls(2026, 11, 22, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai")).astimezone(tz)

    monkeypatch.setattr(opend_upgrade_receipt, "datetime", Sunday)
    monkeypatch.setattr(opend_upgrade_receipt.socket, "gethostname", lambda: "host")
    path = tmp_path / "opend_upgrade_status.json"
    runs = []
    deliveries = []

    def upgrade(**kwargs):
        runs.append(kwargs["confirm"])
        return {"status": "upgraded" if kwargs["confirm"] else "update_available",
                "units": kwargs["units"], "installed": dict.fromkeys(UNITS, "10.12.7208"),
                "official": "10.12.7300", "verified": kwargs["units"] if kwargs["confirm"] else []}

    def deliver(**kwargs):
        deliveries.append(kwargs)
        assert json.loads(path.read_text())["delivery"]["status"] == "unconfirmed"
        return {"status": "delivered", "message_id": "delivered-id"}

    monkeypatch.setattr(opend_upgrade_receipt, "require_plugin", lambda key: SimpleNamespace(run_opend_upgrade=upgrade))
    monkeypatch.setattr(opend_upgrade_receipt, "deliver_report", deliver)
    args = ["service", "opend-upgrade", "--runtime-root", str(tmp_path), "--unit", *UNITS]
    assert cli.main(args) == 0
    assert json.loads(capsys.readouterr().out)["data"]["status"] == "update_available"
    assert not path.exists()
    for _ in range(2):
        assert cli.main([*args, "--confirm", "--send-receipt"]) == 0
        assert json.loads(capsys.readouterr().out)["data"]["delivery"]["message_id"] == "delivered-id"
    assert runs == [False, True]
    assert len(deliveries) == 1
    assert "10.12.7300" in deliveries[0]["markdown"]
