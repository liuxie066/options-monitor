from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


def _write_upgrade_release_skeleton(path: Path, version: str) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "VERSION").write_text(f"{version}\n", encoding="utf-8")
    (path / "configs").mkdir(exist_ok=True)
    (path / "requirements").mkdir(exist_ok=True)
    (path / "constraints").mkdir(exist_ok=True)
    (path / "requirements.txt").write_text("-r requirements/runtime.txt\n", encoding="utf-8")
    (path / "constraints.txt").write_text("-c constraints/runtime.txt\n", encoding="utf-8")
    (path / "requirements" / "runtime.txt").write_text("", encoding="utf-8")
    (path / "constraints" / "runtime.txt").write_text("", encoding="utf-8")


def test_render_systemd_bundle_uses_runtime_root_and_canonical_entrypoints(tmp_path: Path) -> None:
    from src.application.service_deploy import render_service_bundle

    repo = tmp_path / "repo"
    runtime = tmp_path / "runtime"
    repo.mkdir()

    bundle = render_service_bundle(
        target="systemd",
        repo_root=repo,
        runtime_root=runtime,
        accounts=["lx"],
        markets=["us"],
    )

    files = {item["relative_path"]: item for item in bundle["files"]}
    tick = files["systemd/options-monitor-tick-us.service"]["content"]
    tick_timer = files["systemd/options-monitor-tick-us.timer"]["content"]
    intake = files["systemd/options-monitor-trade-intake.service"]["content"]
    auto_close_timer = files["systemd/options-monitor-auto-close-us.timer"]["content"]
    verify = files["systemd/options-monitor-projection-verify.service"]["content"]
    verify_timer = files["systemd/options-monitor-projection-verify.timer"]["content"]
    profile = json.loads(files["service.profile.json"]["content"])

    assert 'Environment="OM_RUNTIME_ROOT=' + str(runtime) + '"' in tick
    assert "User=" not in tick
    assert 'Environment="HOME=' not in tick
    assert str(repo / "om") + " run tick-cron --market us" in tick
    assert "--lock-path " + str(runtime / "locks" / "tick-us.lock") in tick
    assert str(repo / "om") + " option-positions auto-close-expired" in files["systemd/options-monitor-auto-close-us.service"]["content"]
    assert "--apply --yes --quiet" in files["systemd/options-monitor-auto-close-us.service"]["content"]
    assert "OnCalendar=Mon..Fri *-*-* 09..16:00/10:00 America/New_York" in tick_timer
    assert "OnUnitActiveSec=10min" not in tick_timer
    assert "OnBootSec=2min" not in tick_timer
    assert str(repo / "om") + " run trade-intake" in intake
    assert "Restart=always" in intake
    assert "OnCalendar=*-*-* 05:30:00 Asia/Shanghai" in auto_close_timer
    assert str(repo / "om") + " option-positions --data-config " + str(runtime / "portfolio.runtime.json") in verify
    assert "verify-projection --mode auto" in verify
    assert "OnCalendar=*-*-* 06:00:00 Asia/Shanghai" in verify_timer
    assert profile["service_provider"] == "systemd"
    assert profile["runtime_root"] == str(runtime)
    assert {"name": "options-monitor-tick-us.service"} in profile["services"]
    assert {"name": "options-monitor-tick-us.timer"} in profile["services"]
    assert {"name": "options-monitor-projection-verify.timer"} in profile["services"]
    assert "deploy_user" not in profile
    assert "deploy_home" not in profile


def _write_systemd_units_from_bundle(bundle: dict, systemd_root: Path, *, skip: set[str] | None = None) -> None:
    skip = skip or set()
    systemd_root.mkdir(parents=True, exist_ok=True)
    for item in bundle["files"]:
        if item.get("kind") not in {"systemd_service", "systemd_timer"}:
            continue
        name = Path(item["install_path"]).name
        if name in skip:
            continue
        (systemd_root / name).write_text(item["content"], encoding="utf-8")


def test_service_drift_detects_missing_projection_verify_timer(tmp_path: Path) -> None:
    from src.application.service_deploy import render_service_bundle
    from src.application.service_drift import service_drift

    repo = tmp_path / "repo"
    runtime = tmp_path / "runtime"
    systemd_root = tmp_path / "systemd"
    repo.mkdir()
    runtime.mkdir()
    bundle = render_service_bundle(target="systemd", repo_root=repo, runtime_root=runtime, accounts=["lx"], markets=["us"])
    profile = json.loads({item["relative_path"]: item for item in bundle["files"]}["service.profile.json"]["content"])
    profile["services"] = [
        item
        for item in profile["services"]
        if item["name"] not in {"options-monitor-projection-verify.service", "options-monitor-projection-verify.timer"}
    ]
    (runtime / "service.profile.json").write_text(json.dumps(profile, ensure_ascii=False), encoding="utf-8")
    _write_systemd_units_from_bundle(
        bundle,
        systemd_root,
        skip={"options-monitor-projection-verify.service", "options-monitor-projection-verify.timer"},
    )

    out = service_drift(repo_root=repo, runtime_root=runtime, systemd_unit_root=systemd_root)

    assert out["summary"]["status"] == "error"
    assert out["summary"]["ok"] is False
    assert out["missing_profile_units"] == [
        "options-monitor-projection-verify.service",
        "options-monitor-projection-verify.timer",
    ]
    assert out["missing_installed_units"] == [
        "options-monitor-projection-verify.service",
        "options-monitor-projection-verify.timer",
    ]
    assert out["missing_required_units"] == ["options-monitor-projection-verify.timer"]


def test_service_drift_confirm_writes_missing_timer_and_profile(tmp_path: Path) -> None:
    from src.application.service_deploy import render_service_bundle
    from src.application.service_drift import service_drift

    repo = tmp_path / "repo"
    runtime = tmp_path / "runtime"
    systemd_root = tmp_path / "systemd"
    repo.mkdir()
    runtime.mkdir()
    bundle = render_service_bundle(target="systemd", repo_root=repo, runtime_root=runtime, accounts=["lx"], markets=["us"])
    profile = json.loads({item["relative_path"]: item for item in bundle["files"]}["service.profile.json"]["content"])
    profile["services"] = [
        item
        for item in profile["services"]
        if item["name"] not in {"options-monitor-projection-verify.service", "options-monitor-projection-verify.timer"}
    ]
    (runtime / "service.profile.json").write_text(json.dumps(profile, ensure_ascii=False), encoding="utf-8")
    _write_systemd_units_from_bundle(
        bundle,
        systemd_root,
        skip={"options-monitor-projection-verify.service", "options-monitor-projection-verify.timer"},
    )
    calls: list[list[str]] = []

    def _run_cmd(command, **_kwargs):  # type: ignore[no-untyped-def]
        calls.append(list(command))
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

    out = service_drift(
        repo_root=repo,
        runtime_root=runtime,
        systemd_unit_root=systemd_root,
        confirm=True,
        run_cmd=_run_cmd,
    )

    assert out["summary"]["status"] == "ok"
    assert out["changed"] is True
    assert (systemd_root / "options-monitor-projection-verify.service").exists()
    assert (systemd_root / "options-monitor-projection-verify.timer").exists()
    refreshed = json.loads((runtime / "service.profile.json").read_text(encoding="utf-8"))
    assert {"name": "options-monitor-projection-verify.timer"} in refreshed["services"]
    assert ["systemctl", "daemon-reload"] in calls
    assert ["systemctl", "enable", "--now", "options-monitor-projection-verify.timer"] in calls
    assert ["systemctl", "enable", "--now", "options-monitor-projection-verify.service"] not in calls


def test_service_drift_confirm_uses_sudo_fallback_for_systemd_permission_errors(monkeypatch, tmp_path: Path) -> None:
    from src.application.service_deploy import render_service_bundle
    from src.application.service_drift import service_drift

    repo = tmp_path / "repo"
    runtime = tmp_path / "runtime"
    systemd_root = tmp_path / "systemd"
    repo.mkdir()
    runtime.mkdir()
    bundle = render_service_bundle(target="systemd", repo_root=repo, runtime_root=runtime, accounts=["lx"], markets=["us"])
    profile = json.loads({item["relative_path"]: item for item in bundle["files"]}["service.profile.json"]["content"])
    profile["services"] = [
        item
        for item in profile["services"]
        if item["name"] not in {"options-monitor-projection-verify.service", "options-monitor-projection-verify.timer"}
    ]
    (runtime / "service.profile.json").write_text(json.dumps(profile, ensure_ascii=False), encoding="utf-8")
    _write_systemd_units_from_bundle(
        bundle,
        systemd_root,
        skip={"options-monitor-projection-verify.service", "options-monitor-projection-verify.timer"},
    )
    original_write_text = Path.write_text

    def _write_text(path: Path, content: str, *args, **kwargs):  # type: ignore[no-untyped-def]
        if systemd_root in path.parents:
            raise PermissionError("permission denied")
        return original_write_text(path, content, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", _write_text)
    calls: list[list[str]] = []

    def _run_cmd(command, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(list(command))
        if command[:4] == ["sudo", "-n", "sh", "-c"]:
            original_write_text(Path(command[-1]), str(kwargs.get("input") or ""), encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, stdout="written\n", stderr="")
        if command[0:1] == ["systemctl"]:
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="Access denied\n")
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

    out = service_drift(
        repo_root=repo,
        runtime_root=runtime,
        systemd_unit_root=systemd_root,
        confirm=True,
        run_cmd=_run_cmd,
    )

    assert out["summary"]["status"] == "ok"
    assert any(item.get("sudo_fallback") for item in out["operations"] if item.get("operation") == "write_unit")
    assert ["sudo", "-n", "systemctl", "daemon-reload"] in calls
    assert ["sudo", "-n", "systemctl", "enable", "--now", "options-monitor-projection-verify.timer"] in calls


def test_service_drift_discovers_installed_feishu_ws_as_managed_service(tmp_path: Path) -> None:
    from src.application.service_deploy import render_service_bundle
    from src.application.service_drift import service_drift

    repo = tmp_path / "repo"
    runtime = tmp_path / "runtime"
    systemd_root = tmp_path / "systemd"
    repo.mkdir()
    runtime.mkdir()
    bundle = render_service_bundle(target="systemd", repo_root=repo, runtime_root=runtime, accounts=["lx"], markets=["us"], include_feishu_ws=True)
    profile = json.loads({item["relative_path"]: item for item in bundle["files"]}["service.profile.json"]["content"])
    profile.pop("feishu_ws", None)
    profile["services"] = [item for item in profile["services"] if item["name"] != "options-monitor-feishu-ws.service"]
    (runtime / "service.profile.json").write_text(json.dumps(profile, ensure_ascii=False), encoding="utf-8")
    _write_systemd_units_from_bundle(bundle, systemd_root)

    out = service_drift(repo_root=repo, runtime_root=runtime, systemd_unit_root=systemd_root, confirm=True)
    refreshed = json.loads((runtime / "service.profile.json").read_text(encoding="utf-8"))

    assert "options-monitor-feishu-ws.service" in out["expected_services"]
    assert {"name": "options-monitor-feishu-ws.service"} in refreshed["services"]
    assert refreshed["feishu_ws"]["enabled"] is True
    assert refreshed["restart"]["services"] == [
        "options-monitor-trade-intake.service",
        "options-monitor-feishu-ws.service",
    ]


def test_render_systemd_bundle_aligns_hk_tick_timer_to_calendar_boundaries(tmp_path: Path) -> None:
    from src.application.service_deploy import render_service_bundle

    repo = tmp_path / "repo"
    runtime = tmp_path / "runtime"
    repo.mkdir()

    bundle = render_service_bundle(
        target="systemd",
        repo_root=repo,
        runtime_root=runtime,
        accounts=["lx"],
        markets=["hk"],
    )

    files = {item["relative_path"]: item for item in bundle["files"]}
    tick = files["systemd/options-monitor-tick-hk.service"]["content"]
    tick_timer = files["systemd/options-monitor-tick-hk.timer"]["content"]

    assert str(repo / "om") + " run tick-cron --market hk" in tick
    assert "OnCalendar=Mon..Fri *-*-* 09..16:00/10:00 Asia/Hong_Kong" in tick_timer
    assert "OnUnitActiveSec=10min" not in tick_timer
    assert "OnBootSec=2min" not in tick_timer


def test_render_systemd_bundle_can_include_auto_upgrade_timer(tmp_path: Path) -> None:
    from src.application.service_deploy import render_service_bundle

    repo = tmp_path / "current"
    runtime = tmp_path / "runtime"
    repo.mkdir()

    default_bundle = render_service_bundle(target="systemd", repo_root=repo, runtime_root=runtime, markets=["us"])
    default_files = {item["relative_path"]: item for item in default_bundle["files"]}
    assert "systemd/options-monitor-upgrade.service" not in default_files

    bundle = render_service_bundle(
        target="systemd",
        repo_root=repo,
        runtime_root=runtime,
        markets=["us"],
        include_auto_upgrade=True,
    )

    files = {item["relative_path"]: item for item in bundle["files"]}
    service = files["systemd/options-monitor-upgrade.service"]["content"]
    timer = files["systemd/options-monitor-upgrade.timer"]["content"]
    profile = json.loads(files["service.profile.json"]["content"])

    assert str(repo / "om") + " update apply" in service
    assert "--repo-root " + str(repo) in service
    assert "--auto --confirm" in service
    assert "OnCalendar=*-*-* 06:10:00 Asia/Shanghai" in timer
    assert profile["auto_upgrade"]["enabled"] is True
    assert profile["config_paths"]["us"] == str(runtime / "config.us.json")


def test_render_systemd_bundle_can_include_feishu_ws_service(tmp_path: Path) -> None:
    from src.application.service_deploy import render_service_bundle

    repo = tmp_path / "repo"
    runtime = tmp_path / "runtime"
    repo.mkdir()

    bundle = render_service_bundle(
        target="systemd",
        repo_root=repo,
        runtime_root=runtime,
        markets=["us"],
        include_feishu_ws=True,
    )

    files = {item["relative_path"]: item for item in bundle["files"]}
    service = files["systemd/options-monitor-feishu-ws.service"]["content"]
    profile = json.loads(files["service.profile.json"]["content"])

    assert str(repo / "om") + " inbound feishu-ws" in service
    assert "--config-path " + str(repo / "config.us.json") in service
    assert "--audit-db " + str(runtime / "output_shared" / "state" / "inbound_control.sqlite3") in service
    assert "--lock-path " + str(runtime / "locks" / "feishu-ws.lock") in service
    assert "Restart=always" in service
    assert {"name": "options-monitor-feishu-ws.service"} in profile["services"]
    assert profile["restart"]["services"] == [
        "options-monitor-trade-intake.service",
        "options-monitor-feishu-ws.service",
    ]
    assert profile["feishu_ws"]["enabled"] is True
    assert profile["feishu_ws"]["lock_path"] == str(runtime / "locks" / "feishu-ws.lock")
    assert "systemctl enable --now options-monitor-feishu-ws.service" in bundle["commands"]["enable"]


def test_render_systemd_auto_upgrade_preserves_symlink_repo_root(tmp_path: Path) -> None:
    from src.application.service_deploy import render_service_bundle

    releases = tmp_path / "releases"
    release_dir = releases / "1.2.71"
    release_dir.mkdir(parents=True)
    current = tmp_path / "options-monitor"
    current.symlink_to(release_dir, target_is_directory=True)
    runtime = tmp_path / "runtime"

    bundle = render_service_bundle(
        target="systemd",
        repo_root=current,
        runtime_root=runtime,
        markets=["hk"],
        include_auto_upgrade=True,
    )

    files = {item["relative_path"]: item for item in bundle["files"]}
    upgrade = files["systemd/options-monitor-upgrade.service"]["content"]
    tick = files["systemd/options-monitor-tick-hk.service"]["content"]
    profile = json.loads(files["service.profile.json"]["content"])

    assert "WorkingDirectory=" + str(current) in upgrade
    assert str(current / "om") + " update apply" in upgrade
    assert "--repo-root " + str(current) in upgrade
    assert str(release_dir) not in upgrade
    assert "--config " + str(runtime / "config.hk.json") in tick
    assert profile["repo_root"] == str(current)
    assert profile["config_paths"]["hk"] == str(runtime / "config.hk.json")


def test_render_systemd_bundle_allows_deploy_identity_override(tmp_path: Path) -> None:
    from src.application.service_deploy import render_service_bundle

    repo = tmp_path / "repo"
    repo.mkdir()

    bundle = render_service_bundle(
        target="systemd",
        repo_root=repo,
        runtime_root=tmp_path / "runtime",
        accounts=["lx"],
        markets=["us"],
        deploy_user="ops",
        deploy_home="/srv/options-home",
    )

    files = {item["relative_path"]: item for item in bundle["files"]}
    tick = files["systemd/options-monitor-tick-us.service"]["content"]
    profile = json.loads(files["service.profile.json"]["content"])

    assert "User=ops" in tick
    assert 'Environment="HOME=/srv/options-home"' in tick
    assert profile["deploy_user"] == "ops"
    assert profile["deploy_home"] == "/srv/options-home"
    assert profile["restart"]["requires_sudo"] is True
    assert profile["restart"]["command_prefix"] == ["sudo", "-n", "systemctl"]
    assert profile["restart"]["services"] == ["options-monitor-trade-intake.service"]
    assert "ops ALL=(root) NOPASSWD: /bin/systemctl restart options-monitor-trade-intake.service" in profile["restart"]["sudoers"]


def test_render_systemd_feishu_ws_sudoers_cover_all_long_running_services(tmp_path: Path) -> None:
    from src.application.service_deploy import render_service_bundle

    repo = tmp_path / "repo"
    repo.mkdir()

    bundle = render_service_bundle(
        target="systemd",
        repo_root=repo,
        runtime_root=tmp_path / "runtime",
        accounts=["lx"],
        markets=["us"],
        deploy_user="ops",
        include_feishu_ws=True,
    )

    profile = json.loads({item["relative_path"]: item for item in bundle["files"]}["service.profile.json"]["content"])

    assert profile["restart"]["command_prefix"] == ["sudo", "-n", "systemctl"]
    assert profile["restart"]["services"] == [
        "options-monitor-trade-intake.service",
        "options-monitor-feishu-ws.service",
    ]
    assert "ops ALL=(root) NOPASSWD: /bin/systemctl restart options-monitor-trade-intake.service" in profile["restart"]["sudoers"]
    assert "ops ALL=(root) NOPASSWD: /bin/systemctl restart options-monitor-feishu-ws.service" in profile["restart"]["sudoers"]


def test_render_systemd_bundle_quotes_paths_with_spaces(tmp_path: Path) -> None:
    from src.application.service_deploy import render_service_bundle

    repo = tmp_path / "repo with space"
    runtime = tmp_path / "runtime with space"
    repo.mkdir()

    bundle = render_service_bundle(
        target="systemd",
        repo_root=repo,
        runtime_root=runtime,
        accounts=["lx"],
        markets=["us"],
    )

    files = {item["relative_path"]: item for item in bundle["files"]}
    tick = files["systemd/options-monitor-tick-us.service"]["content"]

    assert f'WorkingDirectory="{repo}"' in tick
    assert f'Environment="OM_RUNTIME_ROOT={runtime}"' in tick
    assert f'ExecStart="{repo / "om"}" run tick-cron' in tick
    assert f'--lock-path "{runtime / "locks" / "tick-us.lock"}"' in tick


def test_render_systemd_bundle_can_reference_environment_file(tmp_path: Path) -> None:
    from src.application.service_deploy import render_service_bundle

    repo = tmp_path / "repo"
    runtime = tmp_path / "runtime"
    env_file = tmp_path / "etc" / "options-monitor" / "options-monitor.env"
    repo.mkdir()

    bundle = render_service_bundle(
        target="systemd",
        repo_root=repo,
        runtime_root=runtime,
        accounts=["lx"],
        markets=["us"],
        env_file=env_file,
    )

    files = {item["relative_path"]: item for item in bundle["files"]}
    tick = files["systemd/options-monitor-tick-us.service"]["content"]
    intake = files["systemd/options-monitor-trade-intake.service"]["content"]
    profile = json.loads(files["service.profile.json"]["content"])

    assert f"EnvironmentFile={env_file}" in tick
    assert f"EnvironmentFile={env_file}" in intake
    assert bundle["env_file"] == str(env_file)
    assert profile["env_file"] == str(env_file)


def test_render_launchd_bundle_can_reference_environment_file(tmp_path: Path) -> None:
    from src.application.service_deploy import render_service_bundle

    repo = tmp_path / "repo"
    runtime = tmp_path / "runtime"
    env_file = tmp_path / "Library" / "Application Support" / "options-monitor" / "options-monitor.env"
    repo.mkdir()

    bundle = render_service_bundle(
        target="launchd",
        repo_root=repo,
        runtime_root=runtime,
        accounts=["lx"],
        markets=["us"],
        env_file=env_file,
    )

    files = {item["relative_path"]: item for item in bundle["files"]}
    tick = files["launchd/com.options-monitor.tick-us.plist"]["content"]
    intake = files["launchd/com.options-monitor.trade-intake.plist"]["content"]
    profile = json.loads(files["service.profile.json"]["content"])

    assert "<key>OM_ENV_FILE</key>" in tick
    assert f"<string>{env_file}</string>" in tick
    assert "<key>OM_ENV_FILE</key>" in intake
    assert bundle["env_file"] == str(env_file)
    assert profile["env_file"] == str(env_file)


def test_render_launchd_bundle_uses_launch_agents_and_logs(tmp_path: Path) -> None:
    from src.application.service_deploy import render_service_bundle

    repo = tmp_path / "repo"
    runtime = tmp_path / "runtime"
    repo.mkdir()

    bundle = render_service_bundle(
        target="launchd",
        repo_root=repo,
        runtime_root=runtime,
        accounts=["lx", "sy"],
        markets=["hk"],
    )

    files = {item["relative_path"]: item for item in bundle["files"]}
    tick = files["launchd/com.options-monitor.tick-hk.plist"]["content"]
    auto_close = files["launchd/com.options-monitor.auto-close-hk.plist"]["content"]
    verify = files["launchd/com.options-monitor.projection-verify.plist"]["content"]
    profile = json.loads(files["service.profile.json"]["content"])

    assert "<key>Label</key>" in tick
    assert "<string>com.options-monitor.tick-hk</string>" in tick
    assert str(runtime / "logs" / "com.options-monitor.tick-hk.out.log") in tick
    assert "--market" in tick
    assert "hk" in tick
    assert "<string>com.options-monitor.auto-close-hk</string>" in auto_close
    assert "<key>Hour</key>" in auto_close
    assert "<integer>5</integer>" in auto_close
    assert "<key>Minute</key>" in auto_close
    assert "<integer>30</integer>" in auto_close
    assert "<string>com.options-monitor.projection-verify</string>" in verify
    assert "<key>Hour</key>" in verify
    assert "<integer>6</integer>" in verify
    assert "<key>Minute</key>" in verify
    assert "<integer>0</integer>" in verify
    assert "verify-projection" in verify
    assert profile["service_provider"] == "launchd"
    assert {"name": "com.options-monitor.tick-hk"} in profile["services"]
    assert {"name": "com.options-monitor.projection-verify"} in profile["services"]


def test_render_launchd_bundle_can_include_auto_upgrade_timer(tmp_path: Path) -> None:
    from src.application.service_deploy import render_service_bundle

    repo = tmp_path / "current"
    runtime = tmp_path / "runtime"
    repo.mkdir()

    bundle = render_service_bundle(
        target="launchd",
        repo_root=repo,
        runtime_root=runtime,
        markets=["us"],
        include_auto_upgrade=True,
    )

    files = {item["relative_path"]: item for item in bundle["files"]}
    upgrade = files["launchd/com.options-monitor.upgrade.plist"]["content"]
    profile = json.loads(files["service.profile.json"]["content"])

    assert "<string>com.options-monitor.upgrade</string>" in upgrade
    assert "update" in upgrade
    assert "apply" in upgrade
    assert "<key>Hour</key>" in upgrade
    assert "<integer>6</integer>" in upgrade
    assert "<key>Minute</key>" in upgrade
    assert "<integer>10</integer>" in upgrade
    assert profile["auto_upgrade"]["schedule_beijing"] == "06:10"


def test_write_service_bundle_writes_relative_files(tmp_path: Path) -> None:
    from src.application.service_deploy import render_service_bundle, write_service_bundle

    bundle = render_service_bundle(
        target="systemd",
        repo_root=tmp_path / "repo",
        runtime_root=tmp_path / "runtime",
        markets=["us"],
    )

    written = write_service_bundle(bundle, tmp_path / "rendered")

    assert str(tmp_path / "rendered" / "service.profile.json") in written
    assert (tmp_path / "rendered" / "systemd" / "options-monitor-tick-us.service").exists()


def test_service_preflight_reports_runtime_output_dir_and_config_metadata(tmp_path: Path) -> None:
    from src.application.service_deploy import service_preflight

    runtime = tmp_path / "runtime"
    (runtime / "locks").mkdir(parents=True)
    (runtime / "output_accounts").mkdir()
    (runtime / "output_shared").mkdir()
    (runtime / "output").mkdir()
    cfg = tmp_path / "config.us.json"
    cfg.write_text('{"accounts":["lx"]}', encoding="utf-8")

    out = service_preflight(
        runtime_root=runtime,
        accounts=["lx"],
        config_paths={"us": cfg},
    )
    checks = {item["name"]: item for item in out["checks"]}

    assert out["summary"]["ok"] is False
    assert checks["output_symlink"]["status"] == "error"
    assert "repair-output" in checks["output_symlink"]["value"]["repair"]
    assert checks["runtime_config_us"]["status"] == "error"
    assert "generation metadata" in checks["runtime_config_us"]["message"]


def test_service_preflight_reports_json_line_and_column(tmp_path: Path) -> None:
    from src.application.service_deploy import service_preflight

    runtime = tmp_path / "runtime"
    (runtime / "locks").mkdir(parents=True)
    (runtime / "output_accounts").mkdir()
    (runtime / "output_shared").mkdir()
    (runtime / "output").symlink_to(runtime / "output_accounts" / "lx", target_is_directory=True)
    cfg = tmp_path / "config.us.json"
    cfg.write_text('{"accounts":["lx",],\n}', encoding="utf-8")

    out = service_preflight(runtime_root=runtime, accounts=["lx"], config_paths={"us": cfg})
    check = next(item for item in out["checks"] if item["name"] == "runtime_config_us")

    assert check["status"] == "error"
    assert check["value"]["line"] == 1
    assert check["value"]["column"] > 0


def test_repair_output_symlink_backs_up_and_migrates_real_output(tmp_path: Path) -> None:
    from datetime import datetime, timezone

    from src.application.service_deploy import repair_output_symlink

    runtime = tmp_path / "runtime"
    output = runtime / "output"
    (output / "reports").mkdir(parents=True)
    (output / "reports" / "symbols_notification.txt").write_text("hello", encoding="utf-8")

    dry = repair_output_symlink(runtime_root=runtime, default_account="lx")
    assert dry["changed"] is False
    assert output.is_dir()

    out = repair_output_symlink(
        runtime_root=runtime,
        default_account="lx",
        confirm=True,
        now_fn=lambda: datetime(2026, 5, 19, tzinfo=timezone.utc),
    )

    assert out["changed"] is True
    assert output.is_symlink()
    assert output.resolve() == (runtime / "output_accounts" / "lx").resolve()
    assert (runtime / "output_accounts" / "lx" / "reports" / "symbols_notification.txt").read_text(encoding="utf-8") == "hello"
    assert (runtime / "output.backup.20260519000000" / "reports" / "symbols_notification.txt").exists()


def test_service_status_from_profile_checks_provider_with_injected_runner() -> None:
    from src.application.service_deploy import service_status_from_profile

    calls: list[list[str]] = []

    def _run_cmd(command, **_kwargs):  # type: ignore[no-untyped-def]
        calls.append(list(command))
        return subprocess.CompletedProcess(command, 0, stdout="active\n", stderr="")

    out = service_status_from_profile(
        {
            "service_provider": "systemd",
            "runtime_root": "/var/lib/options-monitor",
            "services": [{"name": "options-monitor-trade-intake.service"}],
        },
        include_status=True,
        run_cmd=_run_cmd,
    )

    assert out["status_checked"] is True
    assert out["services"][0]["status"] == "ok"
    assert calls == [["systemctl", "is-active", "options-monitor-trade-intake.service"]]


def test_service_upgrade_dry_run_and_confirm_switches_current_symlink(monkeypatch, tmp_path: Path) -> None:
    from src.application.service_upgrade import service_upgrade

    monkeypatch.setenv("OM_UPGRADE_INSTALLER", "pip")
    systemd_root = tmp_path / "systemd"
    monkeypatch.setenv("OM_SYSTEMD_UNIT_ROOT", str(systemd_root))
    install = tmp_path / "opt" / "options-monitor"
    releases = install / "releases"
    v100 = releases / "1.0.0"
    v100.mkdir(parents=True)
    (v100 / "VERSION").write_text("1.0.0\n", encoding="utf-8")
    current = install / "current"
    current.symlink_to(v100, target_is_directory=True)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "service.profile.json").write_text(
        json.dumps(
            {
                "service_provider": "systemd",
                "restart": {"requires_sudo": False},
                "services": [
                    {"name": "options-monitor-tick-us.timer"},
                    {"name": "options-monitor-trade-intake.service"},
                ],
            }
        ),
        encoding="utf-8",
    )
    calls: list[list[str]] = []

    def _run_cmd(command, **_kwargs):  # type: ignore[no-untyped-def]
        calls.append(list(command))
        if command[:3] == ["git", "ls-remote", "--tags"]:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=(
                    "a refs/tags/v1.0.0\n"
                    "b refs/tags/v1.0.1\n"
                ),
                stderr="",
            )
        if command[:3] == ["git", "config", "--get"]:
            return subprocess.CompletedProcess(command, 0, stdout="https://example.invalid/repo.git\n", stderr="")
        materialized = _fake_git_cache_materialize(list(command), version="1.0.1")
        if materialized is not None:
            return materialized
        if command[:2] == ["git", "clone"]:
            target = Path(command[-1])
            target.mkdir(parents=True)
            (target / "VERSION").write_text("1.0.1\n", encoding="utf-8")
            (target / "requirements").mkdir()
            (target / "constraints").mkdir()
            (target / "requirements.txt").write_text("-r requirements/runtime.txt\n", encoding="utf-8")
            (target / "constraints.txt").write_text("-c constraints/runtime.txt\n", encoding="utf-8")
            (target / "requirements" / "runtime.txt").write_text("", encoding="utf-8")
            (target / "constraints" / "runtime.txt").write_text("", encoding="utf-8")
            (target / "requirements" / "server.txt").write_text("", encoding="utf-8")
            (target / "constraints" / "server.txt").write_text("", encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, stdout="cloned\n", stderr="")
        if command[:3] == ["python3", "-m", "venv"]:
            _create_fake_venv_python_at(Path(command[-1]))
            return subprocess.CompletedProcess(command, 0, stdout="venv\n", stderr="")
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

    dry = service_upgrade(
        repo_root=current,
        runtime_root=runtime,
        releases_root=releases,
        run_cmd=_run_cmd,
    )
    assert dry["status"] == "dry_run"
    assert dry["changed"] is False
    assert dry["repo_root_is_symlink"] is True
    assert dry["warnings"] == []
    assert current.resolve() == v100.resolve()

    out = service_upgrade(
        repo_root=current,
        runtime_root=runtime,
        releases_root=releases,
        confirm=True,
        run_cmd=_run_cmd,
    )

    assert out["status"] == "upgraded"
    assert out["changed"] is True
    assert current.resolve() == (releases / "1.0.1").resolve()
    cache_repo = install / "_cache" / "git" / "options-monitor.git"
    assert ["git", "clone", "--mirror", "https://example.invalid/repo.git", str(cache_repo)] in calls
    assert any(command[:3] == ["git", f"--git-dir={cache_repo}", "archive"] for command in calls)
    assert any(command[:2] == ["tar", "-xf"] for command in calls)
    assert not any(command[:4] == ["git", "clone", "--depth", "1"] for command in calls)
    assert any(command[:3] == ["python3", "-m", "venv"] for command in calls)
    runtime_prepare = out["runtime_prepare"]
    venv_python = str(Path(runtime_prepare["shared_venv_build_path"]) / "bin" / "python")
    assert [venv_python, "-m", "pip", "install", "-r", "requirements.txt", "-c", "constraints.txt"] in calls
    assert [
        venv_python,
        "-m",
        "pip",
        "install",
        "-r",
        "requirements/server.txt",
        "-c",
        "constraints/server.txt",
    ] in calls
    release_python = str(releases / "1.0.1" / ".venv" / "bin" / "python")
    assert any(command[:2] == [release_python, "-c"] for command in calls)
    assert ["systemctl", "restart", "options-monitor-trade-intake.service"] in calls
    assert ["systemctl", "is-active", "options-monitor-trade-intake.service"] in calls
    assert ["systemctl", "is-enabled", "options-monitor-trade-intake.service"] in calls
    assert ["systemctl", "enable", "--now", "options-monitor-projection-verify.timer"] in calls
    refreshed_profile = json.loads((runtime / "service.profile.json").read_text(encoding="utf-8"))
    assert {"name": "options-monitor-projection-verify.timer"} in refreshed_profile["services"]
    assert (systemd_root / "options-monitor-projection-verify.timer").exists()
    assert out["service_reconcile"]["summary"]["status"] == "ok"
    assert out["service_health"]["status"] == "ok"
    assert out["release_materialize"]["method"] == "git_cache_archive"
    assert out["release_materialize"]["cache_repo"] == str(cache_repo)
    assert out["release_materialize"]["cache_initialized"] is True
    assert out["runtime_prepare"]["installer"] == "pip"
    assert out["runtime_prepare"]["fallback"] is False
    assert out["runtime_prepare"]["venv_reused"] is False
    assert "duration_seconds" in out["runtime_prepare"]
    assert out["runtime_prepare"]["uv_cache_dir"] == str(install / "_cache" / "uv")
    assert out["runtime_prepare"]["pip_cache_dir"] == str(install / "_cache" / "pip")
    status = json.loads((runtime / "upgrade_status.json").read_text(encoding="utf-8"))
    assert status["release_materialize"]["method"] == "git_cache_archive"
    assert status["runtime_prepare"]["installer"] == "pip"
    assert status["target_version"] == "1.0.1"
    assert status["status"] == "upgraded"


def test_service_upgrade_restarts_feishu_ws_from_refreshed_profile_after_reconcile(monkeypatch, tmp_path: Path) -> None:
    from src.application.service_upgrade import service_upgrade

    monkeypatch.setenv("OM_UPGRADE_INSTALLER", "pip")
    systemd_root = tmp_path / "systemd"
    monkeypatch.setenv("OM_SYSTEMD_UNIT_ROOT", str(systemd_root))
    install = tmp_path / "opt" / "options-monitor"
    releases = install / "releases"
    v100 = releases / "1.0.0"
    _write_upgrade_release_skeleton(v100, "1.0.0")
    current = install / "current"
    current.symlink_to(v100, target_is_directory=True)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    env_file = runtime / "options-monitor.env"
    env_file.write_text(
        "OM_FEISHU_BOT_APP_ID=cli_1\n"
        "OM_FEISHU_BOT_APP_SECRET=secret_1\n"
        "OM_FEISHU_BOT_USER_OPEN_ID=ou_1\n",
        encoding="utf-8",
    )
    (runtime / "service.profile.json").write_text(
        json.dumps(
            {
                "service_provider": "systemd",
                "repo_root": str(current),
                "runtime_root": str(runtime),
                "env_file": str(env_file),
                "config_paths": {"us": str(runtime / "config.us.json")},
                "feishu_ws": {"enabled": True, "config_key": "us"},
                "restart": {
                    "requires_sudo": False,
                    "services": ["options-monitor-trade-intake.service"],
                },
                "services": [
                    {"name": "options-monitor-tick-us.timer"},
                    {"name": "options-monitor-trade-intake.service"},
                    {"name": "options-monitor-feishu-ws.service"},
                ],
            }
        ),
        encoding="utf-8",
    )
    calls: list[list[str]] = []

    def _run_cmd(command, **_kwargs):  # type: ignore[no-untyped-def]
        calls.append(list(command))
        if command[:3] == ["git", "ls-remote", "--tags"]:
            return subprocess.CompletedProcess(command, 0, stdout="a refs/tags/v1.0.1\n", stderr="")
        if command[:3] == ["git", "config", "--get"]:
            return subprocess.CompletedProcess(command, 0, stdout="https://example.invalid/repo.git\n", stderr="")
        materialized = _fake_git_cache_materialize(list(command), version="1.0.1")
        if materialized is not None:
            return materialized
        if command[:3] == ["python3", "-m", "venv"]:
            _create_fake_venv_python_at(Path(command[-1]))
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

    out = service_upgrade(
        repo_root=current,
        runtime_root=runtime,
        releases_root=releases,
        confirm=True,
        run_cmd=_run_cmd,
    )

    assert out["status"] == "upgraded"
    assert ["systemctl", "restart", "options-monitor-trade-intake.service"] in calls
    assert ["systemctl", "restart", "options-monitor-feishu-ws.service"] in calls
    assert ["systemctl", "is-active", "options-monitor-feishu-ws.service"] in calls
    assert ["systemctl", "is-enabled", "options-monitor-feishu-ws.service"] in calls
    assert [
        str(current / "om"),
        "inbound",
        "feishu-ws",
        "--check",
        "--config-key",
        "us",
        "--config-path",
        str(runtime / "config.us.json"),
    ] in calls
    refreshed_profile = json.loads((runtime / "service.profile.json").read_text(encoding="utf-8"))
    assert refreshed_profile["restart"]["services"] == [
        "options-monitor-trade-intake.service",
        "options-monitor-feishu-ws.service",
    ]


def test_post_upgrade_service_health_reports_feishu_ws_check_failure(tmp_path: Path) -> None:
    from src.application.service_upgrade import _post_upgrade_service_health

    repo = tmp_path / "repo"
    repo.mkdir()
    profile = {
        "service_provider": "systemd",
        "config_paths": {"us": str(tmp_path / "config.us.json")},
        "feishu_ws": {"enabled": True, "config_key": "us"},
        "services": [{"name": "options-monitor-feishu-ws.service"}],
    }
    operations: list[dict] = []

    def _run_cmd(command, **_kwargs):  # type: ignore[no-untyped-def]
        if list(command)[:3] == [str(repo / "om"), "inbound", "feishu-ws"]:
            return subprocess.CompletedProcess(command, 2, stdout="", stderr="missing Feishu app credentials\n")
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

    out = _post_upgrade_service_health(profile=profile, repo_root=repo, run_cmd=_run_cmd, operations=operations)

    assert out["ok"] is False
    assert out["status"] == "error"
    assert out["failed_checks"][0]["check"] == "feishu-ws-check"
    assert "manual_check: source the env file, then run ./om inbound feishu-ws --check" in out["remediation"]


def test_post_upgrade_feishu_ws_check_uses_profile_env_file_not_process_secret(monkeypatch, tmp_path: Path) -> None:
    from src.application.service_upgrade import _post_upgrade_service_health

    monkeypatch.setenv("OM_FEISHU_BOT_APP_ID", "stale-process-app")
    repo = tmp_path / "repo"
    runtime = tmp_path / "runtime"
    env_file = tmp_path / "options-monitor.env"
    repo.mkdir()
    runtime.mkdir()
    env_file.write_text("OM_FEISHU_BOT_APP_ID=cli_1\n", encoding="utf-8")
    profile = {
        "service_provider": "systemd",
        "runtime_root": str(runtime),
        "env_file": str(env_file),
        "config_paths": {"us": str(tmp_path / "config.us.json")},
        "feishu_ws": {"enabled": True, "config_key": "us"},
        "services": [{"name": "options-monitor-feishu-ws.service"}],
    }

    def _run_cmd(command, **kwargs):  # type: ignore[no-untyped-def]
        if list(command)[:3] == [str(repo / "om"), "inbound", "feishu-ws"]:
            env = kwargs.get("env") or {}
            assert env["OM_ENV_FILE"] == str(env_file)
            assert env["OM_RUNTIME_ROOT"] == str(runtime)
            assert "OM_FEISHU_BOT_APP_ID" not in env
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

    out = _post_upgrade_service_health(profile=profile, repo_root=repo, run_cmd=_run_cmd, operations=[])

    assert out["ok"] is True


def test_service_upgrade_restart_uses_sudo_prefix_from_deploy_profile(tmp_path: Path) -> None:
    from src.application.service_upgrade import _restart_services_from_profile

    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "service.profile.json").write_text(
        json.dumps(
            {
                "service_provider": "systemd",
                "deploy_user": "liuxie",
                "restart": {
                    "requires_sudo": True,
                    "command_prefix": ["sudo", "-n", "systemctl"],
                },
                "services": [
                    {"name": "options-monitor-trade-intake.service"},
                    {"name": "options-monitor-feishu-ws.service"},
                ],
            }
        ),
        encoding="utf-8",
    )
    calls: list[list[str]] = []

    def _run_cmd(command, **_kwargs):  # type: ignore[no-untyped-def]
        calls.append(list(command))
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

    restarted = _restart_services_from_profile(runtime_root=runtime, run_cmd=_run_cmd, operations=[])

    assert restarted == ["options-monitor-trade-intake.service", "options-monitor-feishu-ws.service"]
    assert calls == [
        ["sudo", "-n", "systemctl", "restart", "options-monitor-trade-intake.service"],
        ["sudo", "-n", "systemctl", "restart", "options-monitor-feishu-ws.service"],
    ]


def test_service_upgrade_restart_uses_sudo_fallback_for_legacy_non_root_systemd_profile(monkeypatch, tmp_path: Path) -> None:
    from src.application.service_upgrade import _restart_services_from_profile

    monkeypatch.setattr(os, "geteuid", lambda: 501, raising=False)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "service.profile.json").write_text(
        json.dumps(
            {
                "service_provider": "systemd",
                "services": [{"name": "options-monitor-trade-intake.service"}],
            }
        ),
        encoding="utf-8",
    )
    calls: list[list[str]] = []
    operations: list[dict] = []

    def _run_cmd(command, **_kwargs):  # type: ignore[no-untyped-def]
        calls.append(list(command))
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

    restarted = _restart_services_from_profile(runtime_root=runtime, run_cmd=_run_cmd, operations=operations)

    assert restarted == ["options-monitor-trade-intake.service"]
    assert calls == [["sudo", "-n", "systemctl", "restart", "options-monitor-trade-intake.service"]]
    assert operations[0]["command_source"] == "non_root_sudo_fallback"


def test_service_upgrade_restart_honors_explicit_non_sudo_profile(monkeypatch, tmp_path: Path) -> None:
    from src.application.service_upgrade import _restart_services_from_profile

    monkeypatch.setattr(os, "geteuid", lambda: 501, raising=False)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "service.profile.json").write_text(
        json.dumps(
            {
                "service_provider": "systemd",
                "restart": {"requires_sudo": False},
                "services": [{"name": "options-monitor-trade-intake.service"}],
            }
        ),
        encoding="utf-8",
    )
    calls: list[list[str]] = []
    operations: list[dict] = []

    def _run_cmd(command, **_kwargs):  # type: ignore[no-untyped-def]
        calls.append(list(command))
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

    restarted = _restart_services_from_profile(runtime_root=runtime, run_cmd=_run_cmd, operations=operations)

    assert restarted == ["options-monitor-trade-intake.service"]
    assert calls == [["systemctl", "restart", "options-monitor-trade-intake.service"]]
    assert operations[0]["command_source"] == "profile.requires_sudo_false"


def _write_runtime_target_with_server_deps(path: Path) -> None:
    _write_upgrade_release_skeleton(path, "1.0.1")
    (path / "requirements" / "server.txt").write_text("", encoding="utf-8")
    (path / "constraints" / "server.txt").write_text("", encoding="utf-8")


def _create_fake_venv_python(target: Path) -> None:
    _create_fake_venv_python_at(target / ".venv")


def _create_fake_venv_python_at(venv_dir: Path) -> None:
    venv_python = venv_dir / "bin" / "python"
    venv_python.parent.mkdir(parents=True, exist_ok=True)
    venv_python.write_text("#!/bin/sh\n", encoding="utf-8")
    venv_python.chmod(0o755)


def _fake_git_cache_materialize(command: list[str], *, version: str = "1.0.1") -> subprocess.CompletedProcess | None:
    if command[:3] == ["git", "clone", "--mirror"]:
        Path(command[-1]).mkdir(parents=True, exist_ok=True)
        return subprocess.CompletedProcess(command, 0, stdout="mirrored\n", stderr="")
    if len(command) >= 3 and command[0] == "git" and str(command[1]).startswith("--git-dir=") and command[2] == "fetch":
        return subprocess.CompletedProcess(command, 0, stdout="fetched\n", stderr="")
    if len(command) >= 4 and command[0] == "git" and str(command[1]).startswith("--git-dir=") and command[2] == "archive":
        tar_path = Path(command[command.index("-o") + 1])
        tar_path.parent.mkdir(parents=True, exist_ok=True)
        tar_path.write_text("fake tar\n", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="archived\n", stderr="")
    if command[:2] == ["tar", "-xf"]:
        target = Path(command[command.index("-C") + 1])
        _write_upgrade_release_skeleton(target, version)
        (target / "requirements" / "server.txt").write_text("", encoding="utf-8")
        (target / "constraints" / "server.txt").write_text("", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="extracted\n", stderr="")
    return None


def test_service_upgrade_materialize_uses_existing_git_cache_fetch(tmp_path: Path) -> None:
    from src.application.service_upgrade import _materialize_release_from_git_cache

    cache_root = tmp_path / "_cache"
    cache_repo = cache_root / "git" / "options-monitor.git"
    cache_repo.mkdir(parents=True)
    target = tmp_path / "releases" / "1.0.1"
    calls: list[list[str]] = []

    def _run_cmd(command, **_kwargs):  # type: ignore[no-untyped-def]
        calls.append(list(command))
        materialized = _fake_git_cache_materialize(list(command), version="1.0.1")
        if materialized is not None:
            return materialized
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

    out = _materialize_release_from_git_cache(
        remote_url="https://example.invalid/repo.git",
        tag="v1.0.1",
        target_dir=target,
        cache_root=cache_root,
        run_cmd=_run_cmd,
        operations=[],
    )

    assert out["method"] == "git_cache_archive"
    assert out["cache_initialized"] is False
    assert out["fetched"] is True
    assert target.exists()
    assert ["git", f"--git-dir={cache_repo}", "fetch", "--tags", "--prune", "origin"] in calls
    assert ["git", "clone", "--mirror", "https://example.invalid/repo.git", str(cache_repo)] not in calls


def test_service_upgrade_materialize_reuses_existing_target_without_git(tmp_path: Path) -> None:
    from src.application.service_upgrade import _materialize_release_from_git_cache

    target = tmp_path / "releases" / "1.0.1"
    _write_upgrade_release_skeleton(target, "1.0.1")
    calls: list[list[str]] = []

    out = _materialize_release_from_git_cache(
        remote_url="https://example.invalid/repo.git",
        tag="v1.0.1",
        target_dir=target,
        cache_root=tmp_path / "_cache",
        run_cmd=lambda command, **_kwargs: calls.append(list(command))
        or subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr=""),
        operations=[],
    )

    assert out["method"] == "reuse_existing_release"
    assert calls == []


def test_service_upgrade_check_falls_back_to_git_cache_when_current_release_has_no_git(tmp_path: Path) -> None:
    from src.application.service_upgrade import service_upgrade_check

    repo = tmp_path / "releases" / "1.0.0"
    _write_upgrade_release_skeleton(repo, "1.0.0")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    cache_root = tmp_path / "_cache"
    cache_repo = cache_root / "git" / "options-monitor.git"
    cache_repo.mkdir(parents=True)
    calls: list[tuple[list[str], str | None]] = []

    def _run_cmd(command, **kwargs):  # type: ignore[no-untyped-def]
        calls.append((list(command), kwargs.get("cwd")))
        if command[:3] == ["git", "ls-remote", "--tags"]:
            raise subprocess.CalledProcessError(128, command, stderr="fatal: not a git repository")
        if command[:3] == ["git", f"--git-dir={cache_repo}", "ls-remote"]:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=(
                    "a refs/tags/v1.0.0\n"
                    "b refs/tags/v1.0.1\n"
                ),
                stderr="",
            )
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

    out = service_upgrade_check(repo_root=repo, runtime_root=runtime, cache_root=cache_root, run_cmd=_run_cmd)

    assert out["ok"] is True
    assert out["latest_version"] == "1.0.1"
    assert out["version_check"]["source"] == "upgrade_cache"
    assert out["version_check"]["fallback_from"] == "current_release"
    assert any(command[:3] == ["git", f"--git-dir={cache_repo}", "ls-remote"] for command, _cwd in calls)


def test_service_upgrade_confirm_uses_cached_remote_when_current_release_has_no_git(tmp_path: Path) -> None:
    from src.application.service_upgrade import service_upgrade

    install = tmp_path / "opt" / "options-monitor"
    releases = install / "releases"
    v100 = releases / "1.0.0"
    _write_upgrade_release_skeleton(v100, "1.0.0")
    current = install / "current"
    current.parent.mkdir(parents=True, exist_ok=True)
    current.symlink_to(v100, target_is_directory=True)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    cache_root = install / "_cache"
    cache_repo = cache_root / "git" / "options-monitor.git"
    cache_repo.mkdir(parents=True)
    calls: list[list[str]] = []

    def _run_cmd(command, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(list(command))
        if command[:3] == ["git", "ls-remote", "--tags"]:
            raise subprocess.CalledProcessError(128, command, stderr="fatal: not a git repository")
        if command[:3] == ["git", f"--git-dir={cache_repo}", "ls-remote"]:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=(
                    "a refs/tags/v1.0.0\n"
                    "b refs/tags/v1.0.1\n"
                ),
                stderr="",
            )
        if command[:3] == ["git", "config", "--get"]:
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="fatal: not a git repository")
        if command[:3] == ["git", f"--git-dir={cache_repo}", "config"]:
            return subprocess.CompletedProcess(command, 0, stdout="https://example.invalid/repo.git\n", stderr="")
        materialized = _fake_git_cache_materialize(list(command), version="1.0.1")
        if materialized is not None:
            return materialized
        if command[:3] == ["python3", "-m", "venv"]:
            _create_fake_venv_python_at(Path(command[-1]))
            return subprocess.CompletedProcess(command, 0, stdout="venv\n", stderr="")
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

    out = service_upgrade(
        repo_root=current,
        runtime_root=runtime,
        releases_root=releases,
        cache_root=cache_root,
        confirm=True,
        restart_services=False,
        run_cmd=_run_cmd,
    )

    assert out["status"] == "upgraded"
    assert current.resolve() == (releases / "1.0.1").resolve()
    assert ["git", f"--git-dir={cache_repo}", "config", "--get", "remote.origin.url"] in calls
    assert ["git", f"--git-dir={cache_repo}", "fetch", "--tags", "--prune", "origin"] in calls
    assert not (releases / "1.0.1" / ".git").exists()


def test_service_upgrade_runtime_prepare_auto_uses_pip_when_uv_missing(monkeypatch, tmp_path: Path) -> None:
    from src.application.service_upgrade import _ensure_release_runtime

    monkeypatch.delenv("OM_UPGRADE_INSTALLER", raising=False)
    target = tmp_path / "release"
    _write_runtime_target_with_server_deps(target)
    calls: list[list[str]] = []
    operations: list[dict] = []

    def _run_cmd(command, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(list(command))
        if command == ["sh", "-lc", "command -v uv"]:
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="")
        if command[:3] == ["python3", "-m", "venv"]:
            _create_fake_venv_python_at(Path(command[-1]))
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

    out = _ensure_release_runtime(target_dir=target, run_cmd=_run_cmd, operations=operations)

    build_python = str(Path(out["shared_venv_build_path"]) / "bin" / "python")
    assert out["installer"] == "pip"
    assert out["fallback"] is False
    assert ["uv", "pip", "install", "-p", build_python, "-r", "requirements.txt", "-c", "constraints.txt"] not in calls
    assert [build_python, "-m", "pip", "install", "-r", "requirements.txt", "-c", "constraints.txt"] in calls
    assert [build_python, "-m", "pip", "install", "-r", "requirements/server.txt", "-c", "constraints/server.txt"] in calls
    assert (target / ".venv").is_symlink()
    assert Path(out["shared_venv_path"]).exists()
    assert not Path(out["shared_venv_build_path"]).exists()
    assert operations and all("duration_seconds" in item for item in operations)


def test_service_upgrade_runtime_prepare_auto_uses_uv_and_maps_pip_index(monkeypatch, tmp_path: Path) -> None:
    from src.application.service_upgrade import _ensure_release_runtime

    monkeypatch.delenv("OM_UPGRADE_INSTALLER", raising=False)
    monkeypatch.setenv("PIP_INDEX_URL", "https://mirrors.aliyun.com/pypi/simple/")
    monkeypatch.delenv("UV_INDEX_URL", raising=False)
    target = tmp_path / "release"
    _write_runtime_target_with_server_deps(target)
    calls: list[list[str]] = []
    uv_envs: list[dict[str, str]] = []

    def _run_cmd(command, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(list(command))
        if command == ["sh", "-lc", "command -v uv"]:
            return subprocess.CompletedProcess(command, 0, stdout="/usr/bin/uv\n", stderr="")
        if command[:4] == ["uv", "venv", "--python", "python3"]:
            _create_fake_venv_python_at(Path(command[-1]))
        if command[:3] == ["uv", "pip", "install"]:
            uv_envs.append(dict(kwargs["env"]))
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

    out = _ensure_release_runtime(target_dir=target, run_cmd=_run_cmd, operations=[])

    build_venv = Path(out["shared_venv_build_path"])
    build_python = str(build_venv / "bin" / "python")
    assert out["installer"] == "uv"
    assert out["fallback"] is False
    assert ["uv", "venv", "--python", "python3", str(build_venv)] in calls
    assert ["uv", "pip", "install", "-p", build_python, "-r", "requirements.txt", "-c", "constraints.txt"] in calls
    assert ["uv", "pip", "install", "-p", build_python, "-r", "requirements/server.txt", "-c", "constraints/server.txt"] in calls
    assert uv_envs and uv_envs[0]["UV_INDEX_URL"] == "https://mirrors.aliyun.com/pypi/simple/"
    assert uv_envs[0]["UV_CACHE_DIR"] == str(tmp_path / "_cache" / "uv")
    assert uv_envs[0]["PIP_CACHE_DIR"] == str(tmp_path / "_cache" / "pip")
    assert out["python_spec"] == "python3"
    assert out["uv_cache_dir"] == str(tmp_path / "_cache" / "uv")


def test_service_upgrade_runtime_prepare_pip_mode_skips_uv(monkeypatch, tmp_path: Path) -> None:
    from src.application.service_upgrade import _ensure_release_runtime

    monkeypatch.setenv("OM_UPGRADE_INSTALLER", "pip")
    target = tmp_path / "release"
    _write_runtime_target_with_server_deps(target)
    calls: list[list[str]] = []

    def _run_cmd(command, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(list(command))
        if command[:3] == ["python3", "-m", "venv"]:
            _create_fake_venv_python_at(Path(command[-1]))
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

    out = _ensure_release_runtime(target_dir=target, run_cmd=_run_cmd, operations=[])

    assert out["installer"] == "pip"
    assert ["sh", "-lc", "command -v uv"] not in calls


def test_service_upgrade_runtime_prepare_reuses_dependency_cached_venv(monkeypatch, tmp_path: Path) -> None:
    from src.application.service_upgrade import _ensure_release_runtime

    monkeypatch.setenv("OM_UPGRADE_INSTALLER", "pip")
    cache_root = tmp_path / "_cache"
    first = tmp_path / "release-a"
    second = tmp_path / "release-b"
    _write_runtime_target_with_server_deps(first)
    _write_runtime_target_with_server_deps(second)
    calls: list[list[str]] = []

    def _run_cmd(command, **_kwargs):  # type: ignore[no-untyped-def]
        calls.append(list(command))
        if command[:3] == ["python3", "-m", "venv"]:
            _create_fake_venv_python_at(Path(command[-1]))
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

    first_out = _ensure_release_runtime(target_dir=first, cache_root=cache_root, run_cmd=_run_cmd, operations=[])
    first_call_count = len(calls)
    second_out = _ensure_release_runtime(target_dir=second, cache_root=cache_root, run_cmd=_run_cmd, operations=[])
    second_calls = calls[first_call_count:]

    assert first_out["installer"] == "pip"
    assert second_out["installer"] == "cache"
    assert second_out["venv_reused"] is True
    assert second_out["dependency_hash"] == first_out["dependency_hash"]
    assert second_out["dependency_context"]["python_spec"] == "python3"
    assert "duration_seconds" in second_out
    assert Path(first_out["shared_venv_path"]) == Path(second_out["shared_venv_path"])
    assert (second / ".venv").is_symlink()
    assert not Path(second_out["shared_venv_build_path"]).exists()
    assert not any(command[:3] == ["python3", "-m", "venv"] for command in second_calls)
    assert not any(command[1:4] == ["-m", "pip", "install"] for command in second_calls)


def test_service_upgrade_dependency_hash_changes_with_dependency_files(tmp_path: Path) -> None:
    from src.application.service_upgrade import _dependency_hash

    target = tmp_path / "release"
    _write_runtime_target_with_server_deps(target)

    before = _dependency_hash(target, include_server=True)
    (target / "constraints" / "server.txt").write_text("lark-oapi==1.6.6\n", encoding="utf-8")
    after = _dependency_hash(target, include_server=True)

    assert before != after


def test_service_upgrade_runtime_prepare_removes_temp_venv_on_install_failure(monkeypatch, tmp_path: Path) -> None:
    from src.application.service_upgrade import RuntimePrepareError, _ensure_release_runtime

    monkeypatch.setenv("OM_UPGRADE_INSTALLER", "pip")
    target = tmp_path / "release"
    _write_runtime_target_with_server_deps(target)

    def _run_cmd(command, **_kwargs):  # type: ignore[no-untyped-def]
        if command[:3] == ["python3", "-m", "venv"]:
            _create_fake_venv_python_at(Path(command[-1]))
            return subprocess.CompletedProcess(command, 0, stdout="venv\n", stderr="")
        if command[1:4] == ["-m", "pip", "install"] and "-r" in command:
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="install failed\n")
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

    try:
        _ensure_release_runtime(target_dir=target, run_cmd=_run_cmd, operations=[])
    except RuntimePrepareError as exc:
        assert "install failed" in str(exc)
        assert "duration_seconds" in exc.runtime_prepare
        assert not Path(exc.runtime_prepare["shared_venv_build_path"]).exists()
        assert not Path(exc.runtime_prepare["shared_venv_path"]).exists()
    else:  # pragma: no cover - defensive assertion branch
        raise AssertionError("expected RuntimePrepareError")


def test_service_upgrade_runtime_prepare_uv_mode_failure_does_not_fallback(monkeypatch, tmp_path: Path) -> None:
    from src.application.service_upgrade import RuntimePrepareError, _ensure_release_runtime

    monkeypatch.setenv("OM_UPGRADE_INSTALLER", "uv")
    target = tmp_path / "release"
    _write_runtime_target_with_server_deps(target)
    calls: list[list[str]] = []

    def _run_cmd(command, **_kwargs):  # type: ignore[no-untyped-def]
        calls.append(list(command))
        if command[:4] == ["uv", "venv", "--python", "python3"]:
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="uv failed\n")
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

    try:
        _ensure_release_runtime(target_dir=target, run_cmd=_run_cmd, operations=[])
    except RuntimePrepareError as exc:
        assert exc.runtime_prepare["installer"] == "uv"
        assert exc.runtime_prepare["fallback"] is False
        assert "uv failed" in str(exc.runtime_prepare["uv_error"])
    else:  # pragma: no cover - defensive assertion branch
        raise AssertionError("expected RuntimePrepareError")

    assert not any(command[:3] == ["python3", "-m", "venv"] for command in calls)


def test_service_upgrade_runtime_prepare_auto_falls_back_to_pip_after_uv_failure(monkeypatch, tmp_path: Path) -> None:
    from src.application.service_upgrade import _ensure_release_runtime

    monkeypatch.delenv("OM_UPGRADE_INSTALLER", raising=False)
    target = tmp_path / "release"
    _write_runtime_target_with_server_deps(target)
    calls: list[list[str]] = []

    def _run_cmd(command, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(list(command))
        if command == ["sh", "-lc", "command -v uv"]:
            return subprocess.CompletedProcess(command, 0, stdout="/usr/bin/uv\n", stderr="")
        if command[:4] == ["uv", "venv", "--python", "python3"]:
            _create_fake_venv_python_at(Path(command[-1]))
            return subprocess.CompletedProcess(command, 0, stdout="uv venv\n", stderr="")
        if command[:3] == ["uv", "pip", "install"]:
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="uv install failed\n")
        if command[:3] == ["python3", "-m", "venv"]:
            _create_fake_venv_python_at(Path(command[-1]))
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

    out = _ensure_release_runtime(target_dir=target, run_cmd=_run_cmd, operations=[])

    assert out["installer"] == "pip"
    assert out["fallback"] is True
    assert out["fallback_from"] == "uv"
    assert "uv install failed" in str(out["uv_error"])
    assert any(command[:3] == ["python3", "-m", "venv"] for command in calls)


def test_service_upgrade_restart_denied_includes_remediation(tmp_path: Path) -> None:
    from src.application.service_upgrade import ServiceRestartError, _restart_services_from_profile

    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "service.profile.json").write_text(
        json.dumps(
            {
                "service_provider": "systemd",
                "deploy_user": "liuxie",
                "restart": {
                    "requires_sudo": True,
                    "command_prefix": ["sudo", "-n", "systemctl"],
                },
                "services": [{"name": "options-monitor-trade-intake.service"}],
            }
        ),
        encoding="utf-8",
    )
    operations: list[dict] = []

    def _run_cmd(command, **_kwargs):  # type: ignore[no-untyped-def]
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="Failed to restart: Access denied\n")

    try:
        _restart_services_from_profile(runtime_root=runtime, run_cmd=_run_cmd, operations=operations)
    except ServiceRestartError as exc:
        remediation = exc.remediation
    else:  # pragma: no cover - defensive assertion branch
        raise AssertionError("expected ServiceRestartError")

    assert operations[-1]["returncode"] == 1
    assert "manual_restart: sudo systemctl restart options-monitor-trade-intake.service" in remediation
    assert "liuxie ALL=(root) NOPASSWD: /bin/systemctl restart options-monitor-trade-intake.service" in remediation


def test_service_upgrade_partial_success_when_restart_denied_after_switch(monkeypatch, tmp_path: Path) -> None:
    from src.application.service_upgrade import service_upgrade

    monkeypatch.setenv("OM_SYSTEMD_UNIT_ROOT", str(tmp_path / "systemd"))
    install = tmp_path / "opt" / "options-monitor"
    releases = install / "releases"
    v100 = releases / "1.0.0"
    v100.mkdir(parents=True)
    (v100 / "VERSION").write_text("1.0.0\n", encoding="utf-8")
    current = install / "current"
    current.symlink_to(v100, target_is_directory=True)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "service.profile.json").write_text(
        json.dumps(
            {
                "service_provider": "systemd",
                "deploy_user": "liuxie",
                "restart": {
                    "requires_sudo": True,
                    "command_prefix": ["sudo", "-n", "systemctl"],
                    "services": [
                        "options-monitor-trade-intake.service",
                        "options-monitor-feishu-ws.service",
                    ],
                },
                "services": [
                    {"name": "options-monitor-trade-intake.service"},
                    {"name": "options-monitor-feishu-ws.service"},
                ],
                "feishu_ws": {"enabled": True, "config_key": "us"},
            }
        ),
        encoding="utf-8",
    )

    def _run_cmd(command, **_kwargs):  # type: ignore[no-untyped-def]
        if command[:3] == ["git", "ls-remote", "--tags"]:
            return subprocess.CompletedProcess(command, 0, stdout="a refs/tags/v1.0.1\n", stderr="")
        if command[:3] == ["git", "config", "--get"]:
            return subprocess.CompletedProcess(command, 0, stdout="https://example.invalid/repo.git\n", stderr="")
        materialized = _fake_git_cache_materialize(list(command), version="1.0.1")
        if materialized is not None:
            return materialized
        if command[:2] == ["git", "clone"]:
            target = Path(command[-1])
            target.mkdir(parents=True)
            (target / "VERSION").write_text("1.0.1\n", encoding="utf-8")
            (target / "requirements").mkdir()
            (target / "constraints").mkdir()
            (target / "requirements.txt").write_text("-r requirements/runtime.txt\n", encoding="utf-8")
            (target / "constraints.txt").write_text("-c constraints/runtime.txt\n", encoding="utf-8")
            (target / "requirements" / "runtime.txt").write_text("", encoding="utf-8")
            (target / "constraints" / "runtime.txt").write_text("", encoding="utf-8")
            (target / "requirements" / "server.txt").write_text("", encoding="utf-8")
            (target / "constraints" / "server.txt").write_text("", encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, stdout="cloned\n", stderr="")
        if command[:3] == ["python3", "-m", "venv"]:
            _create_fake_venv_python_at(Path(command[-1]))
            return subprocess.CompletedProcess(command, 0, stdout="venv\n", stderr="")
        if command[:4] == ["sudo", "-n", "systemctl", "restart"]:
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="Access denied\n")
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

    out = service_upgrade(
        repo_root=current,
        runtime_root=runtime,
        releases_root=releases,
        confirm=True,
        auto=True,
        run_cmd=_run_cmd,
    )

    assert out["ok"] is True
    assert out["status"] == "upgraded_restart_failed"
    assert out["changed"] is True
    assert out["symlink_switched"] is True
    assert current.resolve() == (releases / "1.0.1").resolve()
    assert out["restart_failed_services"] == [
        "options-monitor-trade-intake.service",
        "options-monitor-feishu-ws.service",
    ]
    assert "manual_restart: sudo systemctl restart options-monitor-feishu-ws.service" in out["manual_remediation"]
    status = json.loads((runtime / "upgrade_status.json").read_text(encoding="utf-8"))
    assert status["status"] == "upgraded_restart_failed"
    assert status["restart_failed_services"] == out["restart_failed_services"]


def test_service_upgrade_restart_uses_explicit_restart_services_from_profile(tmp_path: Path) -> None:
    from src.application.service_upgrade import _restart_services_from_profile

    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "service.profile.json").write_text(
        json.dumps(
            {
                "service_provider": "systemd",
                "restart": {
                    "command_prefix": ["sudo", "-n", "systemctl"],
                    "services": [
                        "options-monitor-trade-intake.service",
                        "options-monitor-feishu-ws.service",
                        "options-monitor-custom-worker.service",
                    ],
                },
                "services": [
                    {"name": "options-monitor-tick-us.service"},
                    {"name": "options-monitor-trade-intake.service"},
                    {"name": "options-monitor-feishu-ws.service"},
                ],
            }
        ),
        encoding="utf-8",
    )
    calls: list[list[str]] = []

    def _run_cmd(command, **_kwargs):  # type: ignore[no-untyped-def]
        calls.append(list(command))
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

    restarted = _restart_services_from_profile(runtime_root=runtime, run_cmd=_run_cmd, operations=[])

    assert restarted == [
        "options-monitor-trade-intake.service",
        "options-monitor-feishu-ws.service",
        "options-monitor-custom-worker.service",
    ]
    assert calls == [
        ["sudo", "-n", "systemctl", "restart", "options-monitor-trade-intake.service"],
        ["sudo", "-n", "systemctl", "restart", "options-monitor-feishu-ws.service"],
        ["sudo", "-n", "systemctl", "restart", "options-monitor-custom-worker.service"],
    ]


def test_service_upgrade_restart_supports_restart_command_string(tmp_path: Path) -> None:
    from src.application.service_upgrade import _restart_services_from_profile

    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "service.profile.json").write_text(
        json.dumps(
            {
                "service_provider": "systemd",
                "restart": {
                    "restart_command": "sudo -n systemctl restart",
                    "services": ["options-monitor-trade-intake.service"],
                },
            }
        ),
        encoding="utf-8",
    )
    calls: list[list[str]] = []

    def _run_cmd(command, **_kwargs):  # type: ignore[no-untyped-def]
        calls.append(list(command))
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

    restarted = _restart_services_from_profile(runtime_root=runtime, run_cmd=_run_cmd, operations=[])

    assert restarted == ["options-monitor-trade-intake.service"]
    assert calls == [["sudo", "-n", "systemctl", "restart", "options-monitor-trade-intake.service"]]


def test_service_upgrade_restart_no_profile_is_noop(tmp_path: Path) -> None:
    from src.application.service_upgrade import _restart_services_from_profile

    runtime = tmp_path / "runtime"
    runtime.mkdir()
    calls: list[list[str]] = []

    def _run_cmd(command, **_kwargs):  # type: ignore[no-untyped-def]
        calls.append(list(command))
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

    restarted = _restart_services_from_profile(runtime_root=runtime, run_cmd=_run_cmd, operations=[])

    assert restarted == []
    assert calls == []


def test_service_upgrade_migrates_user_configs_and_rebuilds_runtime_configs_before_switch(monkeypatch, tmp_path: Path) -> None:
    from src.application.service_upgrade import service_upgrade

    monkeypatch.setenv("OM_SYSTEMD_UNIT_ROOT", str(tmp_path / "systemd"))
    install = tmp_path / "opt" / "options-monitor"
    releases = install / "releases"
    v100 = releases / "1.0.0"
    v100.mkdir(parents=True)
    (v100 / "VERSION").write_text("1.0.0\n", encoding="utf-8")
    (v100 / "configs").mkdir()
    for name in ("user.common.json", "user.hk.json", "user.us.json"):
        (v100 / "configs" / name).write_text(json.dumps({"name": name}), encoding="utf-8")
    current = install / "current"
    current.symlink_to(v100, target_is_directory=True)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    hk_runtime = runtime / "config.hk.json"
    us_runtime = runtime / "config.us.json"
    hk_runtime.write_text(
        json.dumps(
            {
                "_generated": {
                    "sources": [
                        {"role": "common_user", "loaded": True, "path": "configs/user.common.json"},
                        {"role": "market_user", "loaded": True, "path": "configs/user.hk.json"},
                    ]
                },
                "inbound": {"feishu_ws": {"ack_reaction": "SMILE"}},
            }
        ),
        encoding="utf-8",
    )
    us_runtime.write_text(
        json.dumps(
            {
                "_generated": {
                    "sources": [
                        {"role": "common_user", "loaded": True, "path": "configs/user.common.json"},
                        {"role": "market_user", "loaded": True, "path": "configs/user.us.json"},
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    (runtime / "service.profile.json").write_text(
        json.dumps(
            {
                "service_provider": "systemd",
                "markets": ["hk", "us"],
                "config_paths": {"hk": str(hk_runtime), "us": str(us_runtime)},
                "restart": {"requires_sudo": False},
                "services": [{"name": "options-monitor-trade-intake.service"}],
            }
        ),
        encoding="utf-8",
    )
    calls: list[list[str]] = []

    def _run_cmd(command, **_kwargs):  # type: ignore[no-untyped-def]
        calls.append(list(command))
        if command[:3] == ["git", "ls-remote", "--tags"]:
            return subprocess.CompletedProcess(command, 0, stdout="b refs/tags/v1.0.1\n", stderr="")
        if command[:3] == ["git", "config", "--get"]:
            return subprocess.CompletedProcess(command, 0, stdout="https://example.invalid/repo.git\n", stderr="")
        materialized = _fake_git_cache_materialize(list(command), version="1.0.1")
        if materialized is not None:
            return materialized
        if command[:2] == ["git", "clone"]:
            target = Path(command[-1])
            target.mkdir(parents=True)
            (target / "VERSION").write_text("1.0.1\n", encoding="utf-8")
            (target / "configs").mkdir()
            (target / "configs" / "system.json").write_text("{}", encoding="utf-8")
            (target / "requirements").mkdir()
            (target / "constraints").mkdir()
            (target / "requirements.txt").write_text("-r requirements/runtime.txt\n", encoding="utf-8")
            (target / "constraints.txt").write_text("-c constraints/runtime.txt\n", encoding="utf-8")
            (target / "requirements" / "runtime.txt").write_text("", encoding="utf-8")
            (target / "constraints" / "runtime.txt").write_text("", encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, stdout="cloned\n", stderr="")
        if command[:3] == ["python3", "-m", "venv"]:
            _create_fake_venv_python_at(Path(command[-1]))
            return subprocess.CompletedProcess(command, 0, stdout="venv\n", stderr="")
        if command[:4] == ["./om", "config", "build", "--market"]:
            Path(command[-1]).write_text('{"ok": true}\n', encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, stdout="built\n", stderr="")
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

    out = service_upgrade(
        repo_root=current,
        runtime_root=runtime,
        releases_root=releases,
        confirm=True,
        run_cmd=_run_cmd,
    )

    target = releases / "1.0.1"
    assert out["status"] == "upgraded"
    assert current.resolve() == target.resolve()
    assert (target / "configs" / "user.common.json").exists()
    assert (target / "configs" / "user.hk.json").exists()
    assert (target / "configs" / "user.us.json").exists()
    common_overlay = json.loads((target / "configs" / "user.common.json").read_text(encoding="utf-8"))
    assert common_overlay["inbound"]["feishu_ws"]["ack_reaction"] == "SMILE"
    assert out["runtime_config_prepare"]["preserved_hotfixes"][0]["path"] == "inbound.feishu_ws.ack_reaction"
    assert ["./om", "config", "build", "--market", "hk", "--output", str(hk_runtime)] in calls
    assert ["./om", "config", "validate", "--config-path", str(hk_runtime), "--market", "hk"] in calls
    assert ["./om", "config", "build", "--market", "us", "--output", str(us_runtime)] in calls
    restart_index = calls.index(["systemctl", "restart", "options-monitor-trade-intake.service"])
    validate_index = calls.index(["./om", "config", "validate", "--config-path", str(us_runtime), "--market", "us"])
    assert validate_index < restart_index
    assert out["runtime_config_prepare"]["status"] == "prepared"


def test_service_upgrade_missing_user_config_fails_before_switch_with_remediation(tmp_path: Path) -> None:
    from src.application.service_upgrade import service_upgrade

    install = tmp_path / "opt" / "options-monitor"
    releases = install / "releases"
    v100 = releases / "1.0.0"
    v100.mkdir(parents=True)
    (v100 / "VERSION").write_text("1.0.0\n", encoding="utf-8")
    (v100 / "configs").mkdir()
    (v100 / "configs" / "user.common.json").write_text("{}", encoding="utf-8")
    current = install / "current"
    current.symlink_to(v100, target_is_directory=True)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "service.profile.json").write_text(
        json.dumps(
            {
                "service_provider": "systemd",
                "markets": ["hk"],
                "config_paths": {"hk": str(runtime / "config.hk.json")},
                "services": [{"name": "options-monitor-trade-intake.service"}],
            }
        ),
        encoding="utf-8",
    )
    calls: list[list[str]] = []

    def _run_cmd(command, **_kwargs):  # type: ignore[no-untyped-def]
        calls.append(list(command))
        if command[:3] == ["git", "ls-remote", "--tags"]:
            return subprocess.CompletedProcess(command, 0, stdout="b refs/tags/v1.0.1\n", stderr="")
        if command[:3] == ["git", "config", "--get"]:
            return subprocess.CompletedProcess(command, 0, stdout="https://example.invalid/repo.git\n", stderr="")
        materialized = _fake_git_cache_materialize(list(command), version="1.0.1")
        if materialized is not None:
            return materialized
        if command[:2] == ["git", "clone"]:
            target = Path(command[-1])
            target.mkdir(parents=True)
            (target / "VERSION").write_text("1.0.1\n", encoding="utf-8")
            (target / "configs").mkdir()
            (target / "requirements").mkdir()
            (target / "constraints").mkdir()
            (target / "requirements.txt").write_text("-r requirements/runtime.txt\n", encoding="utf-8")
            (target / "constraints.txt").write_text("-c constraints/runtime.txt\n", encoding="utf-8")
            (target / "requirements" / "runtime.txt").write_text("", encoding="utf-8")
            (target / "constraints" / "runtime.txt").write_text("", encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, stdout="cloned\n", stderr="")
        if command[:3] == ["python3", "-m", "venv"]:
            _create_fake_venv_python_at(Path(command[-1]))
            return subprocess.CompletedProcess(command, 0, stdout="venv\n", stderr="")
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

    out = service_upgrade(
        repo_root=current,
        runtime_root=runtime,
        releases_root=releases,
        confirm=True,
        run_cmd=_run_cmd,
    )

    assert out["status"] == "failed"
    assert out["changed"] is False
    assert out["symlink_switched"] is False
    assert current.resolve() == v100.resolve()
    assert out["remediation"][0].startswith("restore_user_overlays: copy ")
    assert not any(command[:4] == ["./om", "config", "build", "--market"] for command in calls)
    assert ["systemctl", "restart", "options-monitor-trade-intake.service"] not in calls


def test_service_upgrade_recovers_user_configs_from_older_complete_release(monkeypatch, tmp_path: Path) -> None:
    from src.application.service_upgrade import service_upgrade

    monkeypatch.setenv("OM_SYSTEMD_UNIT_ROOT", str(tmp_path / "systemd"))
    install = tmp_path / "opt" / "options-monitor"
    releases = install / "releases"
    v090 = releases / "0.9.0"
    _write_upgrade_release_skeleton(v090, "0.9.0")
    for name in ("user.common.json", "user.hk.json", "user.us.json"):
        (v090 / "configs" / name).write_text(json.dumps({"source": "0.9.0", "name": name}), encoding="utf-8")
    v100 = releases / "1.0.0"
    _write_upgrade_release_skeleton(v100, "1.0.0")
    current = install / "current"
    current.symlink_to(v100, target_is_directory=True)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    hk_runtime = runtime / "config.hk.json"
    us_runtime = runtime / "config.us.json"
    (runtime / "service.profile.json").write_text(
        json.dumps(
            {
                "service_provider": "systemd",
                "markets": ["hk", "us"],
                "config_paths": {"hk": str(hk_runtime), "us": str(us_runtime)},
                "services": [{"name": "options-monitor-trade-intake.service"}],
            }
        ),
        encoding="utf-8",
    )
    calls: list[dict[str, object]] = []

    def _run_cmd(command, **kwargs):  # type: ignore[no-untyped-def]
        calls.append({"command": list(command), "cwd": kwargs.get("cwd")})
        if command[:3] == ["git", "ls-remote", "--tags"]:
            return subprocess.CompletedProcess(command, 0, stdout="b refs/tags/v1.0.1\n", stderr="")
        if command[:3] == ["git", "config", "--get"]:
            return subprocess.CompletedProcess(command, 0, stdout="https://example.invalid/repo.git\n", stderr="")
        materialized = _fake_git_cache_materialize(list(command), version="1.0.1")
        if materialized is not None:
            return materialized
        if command[:2] == ["git", "clone"]:
            _write_upgrade_release_skeleton(Path(command[-1]), "1.0.1")
            return subprocess.CompletedProcess(command, 0, stdout="cloned\n", stderr="")
        if command[:3] == ["python3", "-m", "venv"]:
            _create_fake_venv_python_at(Path(command[-1]))
            return subprocess.CompletedProcess(command, 0, stdout="venv\n", stderr="")
        if command[:4] == ["./om", "config", "build", "--market"]:
            Path(command[-1]).write_text('{"ok": true}\n', encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, stdout="built\n", stderr="")
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

    out = service_upgrade(
        repo_root=current,
        runtime_root=runtime,
        releases_root=releases,
        confirm=True,
        run_cmd=_run_cmd,
    )

    target = releases / "1.0.1"
    assert out["status"] == "upgraded"
    assert current.resolve() == target.resolve()
    for name in ("user.common.json", "user.hk.json", "user.us.json"):
        assert json.loads((target / "configs" / name).read_text(encoding="utf-8"))["source"] == "0.9.0"
    assert any(
        call["command"] == ["./om", "config", "validate", "--config-path", str(hk_runtime), "--market", "hk"]
        and call["cwd"] == str(current)
        for call in calls
    )
    assert out["post_switch_runtime_config_validate"][0]["phase"] == "post_switch"


def test_service_upgrade_uses_runtime_overlay_dir_before_older_release(tmp_path: Path) -> None:
    from src.application.service_upgrade import service_upgrade

    install = tmp_path / "opt" / "options-monitor"
    releases = install / "releases"
    v090 = releases / "0.9.0"
    _write_upgrade_release_skeleton(v090, "0.9.0")
    for name in ("user.common.json", "user.hk.json"):
        (v090 / "configs" / name).write_text(json.dumps({"source": "older", "name": name}), encoding="utf-8")
    v100 = releases / "1.0.0"
    _write_upgrade_release_skeleton(v100, "1.0.0")
    current = install / "current"
    current.symlink_to(v100, target_is_directory=True)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    runtime_configs = runtime / "configs"
    runtime_configs.mkdir()
    for name in ("user.common.json", "user.hk.json"):
        (runtime_configs / name).write_text(json.dumps({"source": "runtime", "name": name}), encoding="utf-8")
    hk_runtime = runtime / "config.hk.json"
    (runtime / "service.profile.json").write_text(
        json.dumps(
            {
                "service_provider": "systemd",
                "markets": ["hk"],
                "config_paths": {"hk": str(hk_runtime)},
                "services": [],
            }
        ),
        encoding="utf-8",
    )

    def _run_cmd(command, **kwargs):  # type: ignore[no-untyped-def]
        if command[:3] == ["git", "ls-remote", "--tags"]:
            return subprocess.CompletedProcess(command, 0, stdout="b refs/tags/v1.0.1\n", stderr="")
        if command[:3] == ["git", "config", "--get"]:
            return subprocess.CompletedProcess(command, 0, stdout="https://example.invalid/repo.git\n", stderr="")
        materialized = _fake_git_cache_materialize(list(command), version="1.0.1")
        if materialized is not None:
            return materialized
        if command[:2] == ["git", "clone"]:
            _write_upgrade_release_skeleton(Path(command[-1]), "1.0.1")
            return subprocess.CompletedProcess(command, 0, stdout="cloned\n", stderr="")
        if command[:3] == ["python3", "-m", "venv"]:
            _create_fake_venv_python_at(Path(command[-1]))
            return subprocess.CompletedProcess(command, 0, stdout="venv\n", stderr="")
        if command[:4] == ["./om", "config", "build", "--market"]:
            Path(command[-1]).write_text('{"ok": true}\n', encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, stdout="built\n", stderr="")
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

    out = service_upgrade(
        repo_root=current,
        runtime_root=runtime,
        releases_root=releases,
        confirm=True,
        restart_services=False,
        run_cmd=_run_cmd,
    )

    target = releases / "1.0.1"
    assert out["status"] == "upgraded"
    for name in ("user.common.json", "user.hk.json"):
        assert json.loads((target / "configs" / name).read_text(encoding="utf-8"))["source"] == "runtime"


def test_service_upgrade_uses_runtime_config_metadata_overlay_source(tmp_path: Path) -> None:
    from src.application.service_upgrade import service_upgrade

    install = tmp_path / "opt" / "options-monitor"
    releases = install / "releases"
    v100 = releases / "1.0.0"
    _write_upgrade_release_skeleton(v100, "1.0.0")
    current = install / "current"
    current.symlink_to(v100, target_is_directory=True)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    authoring = tmp_path / "authoring"
    authoring.mkdir()
    common_source = authoring / "user.common.json"
    market_source = authoring / "user.hk.json"
    common_source.write_text(json.dumps({"source": "metadata", "name": "user.common.json"}), encoding="utf-8")
    market_source.write_text(json.dumps({"source": "metadata", "name": "user.hk.json"}), encoding="utf-8")
    hk_runtime = runtime / "config.hk.json"
    hk_runtime.write_text(
        json.dumps(
            {
                "_generated": {
                    "sources": [
                        {"role": "common_user", "loaded": True, "path": str(common_source)},
                        {"role": "market_user", "loaded": True, "path": str(market_source)},
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    (runtime / "service.profile.json").write_text(
        json.dumps(
            {
                "service_provider": "systemd",
                "markets": ["hk"],
                "config_paths": {"hk": str(hk_runtime)},
                "services": [],
            }
        ),
        encoding="utf-8",
    )

    def _run_cmd(command, **kwargs):  # type: ignore[no-untyped-def]
        if command[:3] == ["git", "ls-remote", "--tags"]:
            return subprocess.CompletedProcess(command, 0, stdout="b refs/tags/v1.0.1\n", stderr="")
        if command[:3] == ["git", "config", "--get"]:
            return subprocess.CompletedProcess(command, 0, stdout="https://example.invalid/repo.git\n", stderr="")
        materialized = _fake_git_cache_materialize(list(command), version="1.0.1")
        if materialized is not None:
            return materialized
        if command[:2] == ["git", "clone"]:
            _write_upgrade_release_skeleton(Path(command[-1]), "1.0.1")
            return subprocess.CompletedProcess(command, 0, stdout="cloned\n", stderr="")
        if command[:3] == ["python3", "-m", "venv"]:
            _create_fake_venv_python_at(Path(command[-1]))
            return subprocess.CompletedProcess(command, 0, stdout="venv\n", stderr="")
        if command[:4] == ["./om", "config", "build", "--market"]:
            Path(command[-1]).write_text('{"ok": true}\n', encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, stdout="built\n", stderr="")
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

    out = service_upgrade(
        repo_root=current,
        runtime_root=runtime,
        releases_root=releases,
        confirm=True,
        restart_services=False,
        run_cmd=_run_cmd,
    )

    target = releases / "1.0.1"
    assert out["status"] == "upgraded"
    for name in ("user.common.json", "user.hk.json"):
        assert json.loads((target / "configs" / name).read_text(encoding="utf-8"))["source"] == "metadata"


def test_service_upgrade_rebuild_failure_fails_before_switch_with_remediation(tmp_path: Path) -> None:
    from src.application.service_upgrade import service_upgrade

    install = tmp_path / "opt" / "options-monitor"
    releases = install / "releases"
    v100 = releases / "1.0.0"
    _write_upgrade_release_skeleton(v100, "1.0.0")
    for name in ("user.common.json", "user.hk.json"):
        (v100 / "configs" / name).write_text(json.dumps({"name": name}), encoding="utf-8")
    current = install / "current"
    current.symlink_to(v100, target_is_directory=True)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    hk_runtime = runtime / "config.hk.json"
    (runtime / "service.profile.json").write_text(
        json.dumps(
            {
                "service_provider": "systemd",
                "markets": ["hk"],
                "config_paths": {"hk": str(hk_runtime)},
                "services": [{"name": "options-monitor-trade-intake.service"}],
            }
        ),
        encoding="utf-8",
    )
    calls: list[list[str]] = []

    def _run_cmd(command, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(list(command))
        if command[:3] == ["git", "ls-remote", "--tags"]:
            return subprocess.CompletedProcess(command, 0, stdout="b refs/tags/v1.0.1\n", stderr="")
        if command[:3] == ["git", "config", "--get"]:
            return subprocess.CompletedProcess(command, 0, stdout="https://example.invalid/repo.git\n", stderr="")
        materialized = _fake_git_cache_materialize(list(command), version="1.0.1")
        if materialized is not None:
            return materialized
        if command[:2] == ["git", "clone"]:
            _write_upgrade_release_skeleton(Path(command[-1]), "1.0.1")
            return subprocess.CompletedProcess(command, 0, stdout="cloned\n", stderr="")
        if command[:3] == ["python3", "-m", "venv"]:
            _create_fake_venv_python_at(Path(command[-1]))
            return subprocess.CompletedProcess(command, 0, stdout="venv\n", stderr="")
        if command[:4] == ["./om", "config", "build", "--market"]:
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="build failed")
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

    out = service_upgrade(
        repo_root=current,
        runtime_root=runtime,
        releases_root=releases,
        confirm=True,
        run_cmd=_run_cmd,
    )

    assert out["status"] == "failed"
    assert out["changed"] is False
    assert out["symlink_switched"] is False
    assert current.resolve() == v100.resolve()
    assert any(item.startswith("manual_rebuild: ") for item in out["remediation"])
    assert ["systemctl", "restart", "options-monitor-trade-intake.service"] not in calls


def test_service_upgrade_blocks_major_by_default(tmp_path: Path) -> None:
    from src.application.service_upgrade import service_upgrade

    repo = tmp_path / "repo"
    runtime = tmp_path / "runtime"
    repo.mkdir()
    runtime.mkdir()
    (repo / "VERSION").write_text("1.0.0\n", encoding="utf-8")

    out = service_upgrade(
        repo_root=repo,
        runtime_root=runtime,
        target_version="2.0.0",
        run_cmd=lambda command, **_kwargs: subprocess.CompletedProcess(command, 0, stdout="", stderr=""),
    )

    assert out["status"] == "blocked_major_upgrade"
    assert out["changed"] is False


def test_service_upgrade_dry_run_warns_when_repo_root_is_not_symlink(tmp_path: Path) -> None:
    from src.application.service_upgrade import service_upgrade

    repo = tmp_path / "repo"
    runtime = tmp_path / "runtime"
    repo.mkdir()
    runtime.mkdir()
    (repo / "VERSION").write_text("1.0.0\n", encoding="utf-8")

    def _run_cmd(command, **_kwargs):  # type: ignore[no-untyped-def]
        if command[:3] == ["git", "ls-remote", "--tags"]:
            return subprocess.CompletedProcess(command, 0, stdout="b refs/tags/v1.0.1\n", stderr="")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    out = service_upgrade(
        repo_root=repo,
        runtime_root=runtime,
        target_version="1.0.1",
        run_cmd=_run_cmd,
    )

    assert out["status"] == "dry_run"
    assert out["repo_root_is_symlink"] is False
    assert out["warnings"] == ["confirmed upgrade requires repo_root to be a current symlink"]


def test_service_upgrade_confirm_fails_fast_when_repo_root_is_not_symlink(tmp_path: Path) -> None:
    from src.application.service_upgrade import service_upgrade

    repo = tmp_path / "repo"
    runtime = tmp_path / "runtime"
    releases = tmp_path / "releases"
    repo.mkdir()
    runtime.mkdir()
    (repo / "VERSION").write_text("1.0.0\n", encoding="utf-8")
    calls: list[list[str]] = []

    def _run_cmd(command, **_kwargs):  # type: ignore[no-untyped-def]
        calls.append(list(command))
        if command[:3] == ["git", "ls-remote", "--tags"]:
            return subprocess.CompletedProcess(command, 0, stdout="b refs/tags/v1.0.1\n", stderr="")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    out = service_upgrade(
        repo_root=repo,
        runtime_root=runtime,
        releases_root=releases,
        target_version="1.0.1",
        confirm=True,
        run_cmd=_run_cmd,
    )

    assert out["status"] == "repo_root_not_symlink"
    assert out["changed"] is False
    assert not releases.exists()
    assert not any(command[:2] == ["git", "clone"] for command in calls)
    status = json.loads((runtime / "upgrade_status.json").read_text(encoding="utf-8"))
    assert status["status"] == "repo_root_not_symlink"


def test_service_upgrade_coerces_release_entity_repo_root_to_current_symlink(monkeypatch, tmp_path: Path) -> None:
    from src.application.service_upgrade import service_upgrade

    monkeypatch.setenv("OM_UPGRADE_INSTALLER", "pip")
    monkeypatch.setenv("OM_SYSTEMD_UNIT_ROOT", str(tmp_path / "systemd"))
    install = tmp_path / "opt" / "options-monitor"
    releases = install / "releases"
    v100 = releases / "1.0.0"
    _write_upgrade_release_skeleton(v100, "1.0.0")
    current = install / "current"
    current.symlink_to(v100, target_is_directory=True)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "service.profile.json").write_text(
        json.dumps(
            {
                "service_provider": "systemd",
                "repo_root": str(current),
                "runtime_root": str(runtime),
                "restart": {"requires_sudo": False},
                "services": [{"name": "options-monitor-trade-intake.service"}],
            }
        ),
        encoding="utf-8",
    )

    def _run_cmd(command, **kwargs):  # type: ignore[no-untyped-def]
        if command[:3] == ["git", "ls-remote", "--tags"]:
            return subprocess.CompletedProcess(command, 0, stdout="b refs/tags/v1.0.1\n", stderr="")
        if command[:3] == ["git", "config", "--get"]:
            return subprocess.CompletedProcess(command, 0, stdout="https://example.invalid/repo.git\n", stderr="")
        materialized = _fake_git_cache_materialize(list(command), version="1.0.1")
        if materialized is not None:
            return materialized
        if command[:3] == ["python3", "-m", "venv"]:
            _create_fake_venv_python_at(Path(command[-1]))
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

    out = service_upgrade(
        repo_root=v100,
        runtime_root=runtime,
        releases_root=releases,
        target_version="1.0.1",
        confirm=True,
        run_cmd=_run_cmd,
    )

    assert out["status"] == "upgraded"
    assert out["repo_root"] == str(current)
    assert out["repo_root_is_symlink"] is True
    assert out["repo_root_resolution"]["coerced"] is True
    assert current.resolve() == (releases / "1.0.1").resolve()


def test_service_upgrade_cleanup_after_success_deletes_older_releases(tmp_path: Path) -> None:
    from src.application.service_upgrade import service_upgrade

    install = tmp_path / "opt" / "options-monitor"
    releases = install / "releases"
    v080 = releases / "0.8.0"
    v090 = releases / "0.9.0"
    v100 = releases / "1.0.0"
    for release in (v080, v090, v100):
        _write_upgrade_release_skeleton(release, release.name)
    for name in ("user.common.json", "user.hk.json"):
        (v100 / "configs" / name).write_text(json.dumps({"name": name}), encoding="utf-8")
    current = install / "current"
    current.symlink_to(v100, target_is_directory=True)
    downloads = install / "_downloads"
    downloads.mkdir()
    (downloads / "old.tar.gz").write_text("cache", encoding="utf-8")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    hk_runtime = runtime / "config.hk.json"
    (runtime / "service.profile.json").write_text(
        json.dumps(
            {
                "service_provider": "systemd",
                "markets": ["hk"],
                "config_paths": {"hk": str(hk_runtime)},
                "services": [],
            }
        ),
        encoding="utf-8",
    )

    def _run_cmd(command, **kwargs):  # type: ignore[no-untyped-def]
        if command[:3] == ["git", "ls-remote", "--tags"]:
            return subprocess.CompletedProcess(command, 0, stdout="b refs/tags/v1.0.1\n", stderr="")
        if command[:3] == ["git", "config", "--get"]:
            return subprocess.CompletedProcess(command, 0, stdout="https://example.invalid/repo.git\n", stderr="")
        materialized = _fake_git_cache_materialize(list(command), version="1.0.1")
        if materialized is not None:
            return materialized
        if command[:2] == ["git", "clone"]:
            _write_upgrade_release_skeleton(Path(command[-1]), "1.0.1")
            return subprocess.CompletedProcess(command, 0, stdout="cloned\n", stderr="")
        if command[:3] == ["python3", "-m", "venv"]:
            _create_fake_venv_python_at(Path(command[-1]))
            return subprocess.CompletedProcess(command, 0, stdout="venv\n", stderr="")
        if command[:4] == ["./om", "config", "build", "--market"]:
            Path(command[-1]).write_text('{"ok": true}\n', encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, stdout="built\n", stderr="")
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

    out = service_upgrade(
        repo_root=current,
        runtime_root=runtime,
        releases_root=releases,
        target_version="1.0.1",
        confirm=True,
        restart_services=False,
        cleanup_after_upgrade=True,
        cleanup_keep_releases=2,
        run_cmd=_run_cmd,
    )

    assert out["status"] == "upgraded"
    assert out["symlink_switched"] is True
    assert current.resolve() == (releases / "1.0.1").resolve()
    cleanup = out["post_upgrade_cleanup"]
    assert cleanup["status"] == "cleaned"
    assert {Path(item["path"]).name for item in cleanup["kept_releases"]} == {"1.0.1", "1.0.0"}
    assert (releases / "1.0.1").exists()
    assert v100.exists()
    assert not v090.exists()
    assert not v080.exists()
    assert not downloads.exists()
    status = json.loads((runtime / "upgrade_status.json").read_text(encoding="utf-8"))
    assert status["post_upgrade_cleanup"]["status"] == "cleaned"


def test_service_rollback_switches_current_symlink(tmp_path: Path) -> None:
    from src.application.service_upgrade import service_rollback, write_upgrade_status

    install = tmp_path / "opt" / "options-monitor"
    releases = install / "releases"
    v100 = releases / "1.0.0"
    v101 = releases / "1.0.1"
    v100.mkdir(parents=True)
    v101.mkdir()
    (v100 / "VERSION").write_text("1.0.0\n", encoding="utf-8")
    (v101 / "VERSION").write_text("1.0.1\n", encoding="utf-8")
    current = install / "current"
    current.symlink_to(v101, target_is_directory=True)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    write_upgrade_status(
        runtime_root=runtime,
        payload={"status": "upgraded", "current_version": "1.0.0", "target_version": "1.0.1"},
    )

    dry = service_rollback(repo_root=current, runtime_root=runtime, releases_root=releases)
    assert dry["status"] == "dry_run"
    assert current.resolve() == v101.resolve()

    out = service_rollback(
        repo_root=current,
        runtime_root=runtime,
        releases_root=releases,
        confirm=True,
        restart_services=False,
    )
    assert out["status"] == "rolled_back"
    assert current.resolve() == v100.resolve()


def test_runtime_status_loads_service_profile_paths(monkeypatch, tmp_path: Path) -> None:
    from src.application.tool_execution import execute_tool
    from src.application.service_deploy import render_service_bundle

    cfg_path = tmp_path / "config.us.json"
    data_config = tmp_path / "portfolio.runtime.json"
    data_config.parent.mkdir(parents=True, exist_ok=True)
    data_config.write_text("{}", encoding="utf-8")
    cfg_path.write_text(
        json.dumps(
            {
                "accounts": ["lx"],
                "portfolio": {"data_config": str(data_config)},
                "notifications": {"provider": "openclaw", "target": "route"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    systemd_root = tmp_path / "systemd"
    monkeypatch.setenv("OM_SYSTEMD_UNIT_ROOT", str(systemd_root))
    bundle = render_service_bundle(
        target="systemd",
        repo_root=tmp_path,
        runtime_root=tmp_path,
        accounts=["lx"],
        markets=["us"],
        config_paths={"us": cfg_path},
    )
    _write_systemd_units_from_bundle(bundle, systemd_root)
    profile_path = tmp_path / "service.profile.json"
    profile = json.loads({item["relative_path"]: item for item in bundle["files"]}["service.profile.json"]["content"])
    profile["paths"] = {
        "report_dir": str(tmp_path / "output" / "reports"),
        "state_dir": str(tmp_path / "output" / "state"),
        "shared_state_dir": str(tmp_path / "output_shared" / "state"),
        "accounts_root": str(tmp_path / "output_accounts"),
        "runs_root": str(tmp_path / "output_runs"),
    }
    profile_path.write_text(json.dumps(profile, ensure_ascii=False), encoding="utf-8")

    out = execute_tool("runtime_status", {"profile_path": str(profile_path)})

    assert out["ok"] is True
    assert out["data"]["config"]["accounts"] == ["lx"]
    assert out["data"]["service_profile"]["loaded"] is True
    assert out["data"]["service_profile"]["provider"] == "systemd"
    assert out["data"]["service_drift"]["summary"]["status"] == "ok"


def test_runtime_status_warns_when_required_service_timer_is_missing(monkeypatch, tmp_path: Path) -> None:
    from src.application.tool_execution import execute_tool
    from src.application.service_deploy import render_service_bundle

    cfg_path = tmp_path / "config.us.json"
    data_config = tmp_path / "portfolio.runtime.json"
    data_config.write_text("{}", encoding="utf-8")
    cfg_path.write_text(
        json.dumps(
            {
                "accounts": ["lx"],
                "portfolio": {"data_config": str(data_config)},
                "notifications": {"provider": "openclaw", "target": "route"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    systemd_root = tmp_path / "systemd"
    monkeypatch.setenv("OM_SYSTEMD_UNIT_ROOT", str(systemd_root))
    bundle = render_service_bundle(
        target="systemd",
        repo_root=tmp_path,
        runtime_root=tmp_path,
        accounts=["lx"],
        markets=["us"],
        config_paths={"us": cfg_path},
    )
    _write_systemd_units_from_bundle(
        bundle,
        systemd_root,
        skip={"options-monitor-projection-verify.service", "options-monitor-projection-verify.timer"},
    )
    profile = json.loads({item["relative_path"]: item for item in bundle["files"]}["service.profile.json"]["content"])
    profile["services"] = [
        item
        for item in profile["services"]
        if item["name"] not in {"options-monitor-projection-verify.service", "options-monitor-projection-verify.timer"}
    ]
    profile_path = tmp_path / "service.profile.json"
    profile_path.write_text(json.dumps(profile, ensure_ascii=False), encoding="utf-8")

    out = execute_tool("runtime_status", {"profile_path": str(profile_path)})

    assert out["ok"] is True
    assert out["data"]["summary"]["ok"] is False
    assert "SERVICE_DRIFT_REQUIRED_UNIT_MISSING" in out["data"]["summary"]["warning_codes"]
    assert out["data"]["service_drift"]["missing_required_units"] == ["options-monitor-projection-verify.timer"]


def test_cli_service_render_returns_json(capsys, tmp_path: Path) -> None:
    from src.interfaces.cli.main import main

    rc = main([
        "service",
        "render",
        "--target",
        "systemd",
        "--repo-root",
        str(tmp_path / "repo"),
        "--runtime-root",
        str(tmp_path / "runtime"),
        "--markets",
        "us",
        "--env-file",
        str(tmp_path / "options-monitor.env"),
        "--no-content",
    ])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["data"]["summary"]["service_provider"] == "systemd"
    assert payload["data"]["env_file"] == str(tmp_path / "options-monitor.env")
    assert payload["data"]["files"][0].get("content") is None


def test_cli_service_render_no_content_still_writes_files(capsys, tmp_path: Path) -> None:
    from src.interfaces.cli.main import main

    output_dir = tmp_path / "rendered"
    rc = main([
        "service",
        "render",
        "--target",
        "systemd",
        "--repo-root",
        str(tmp_path / "repo"),
        "--runtime-root",
        str(tmp_path / "runtime"),
        "--markets",
        "us",
        "--output-dir",
        str(output_dir),
        "--no-content",
    ])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["data"]["files"][0].get("content") is None
    assert "ExecStart=" in (output_dir / "systemd" / "options-monitor-tick-us.service").read_text(encoding="utf-8")


def test_cli_service_drift_reports_missing_units(monkeypatch, capsys, tmp_path: Path) -> None:
    from src.application.service_deploy import render_service_bundle
    from src.interfaces.cli.main import main

    repo = tmp_path / "repo"
    runtime = tmp_path / "runtime"
    systemd_root = tmp_path / "systemd"
    repo.mkdir()
    runtime.mkdir()
    monkeypatch.setenv("OM_SYSTEMD_UNIT_ROOT", str(systemd_root))
    bundle = render_service_bundle(target="systemd", repo_root=repo, runtime_root=runtime, accounts=["lx"], markets=["us"])
    profile = json.loads({item["relative_path"]: item for item in bundle["files"]}["service.profile.json"]["content"])
    profile["services"] = [
        item
        for item in profile["services"]
        if item["name"] not in {"options-monitor-projection-verify.service", "options-monitor-projection-verify.timer"}
    ]
    (runtime / "service.profile.json").write_text(json.dumps(profile, ensure_ascii=False), encoding="utf-8")
    _write_systemd_units_from_bundle(
        bundle,
        systemd_root,
        skip={"options-monitor-projection-verify.service", "options-monitor-projection-verify.timer"},
    )

    rc = main(["service", "drift", "--repo-root", str(repo), "--runtime-root", str(runtime)])

    assert rc == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["data"]["missing_required_units"] == ["options-monitor-projection-verify.timer"]


def test_cli_service_upgrade_delegates_to_application(monkeypatch, capsys, tmp_path: Path) -> None:
    import src.interfaces.cli.main as cli_main

    calls: list[dict[str, object]] = []

    def _fake_upgrade(**kwargs):  # type: ignore[no-untyped-def]
        calls.append(dict(kwargs))
        return {"ok": True, "status": "dry_run", "changed": False}

    monkeypatch.setattr(cli_main, "service_upgrade", _fake_upgrade)

    rc = cli_main.main(
        [
            "service",
            "upgrade",
            "--repo-root",
            str(tmp_path / "current"),
            "--runtime-root",
            str(tmp_path / "runtime"),
            "--cache-root",
            str(tmp_path / "_cache"),
            "--target-version",
            "1.2.99",
            "--auto",
            "--cleanup-after-upgrade",
        ]
    )

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert calls[0]["cache_root"] == str(tmp_path / "_cache")
    assert calls[0]["target_version"] == "1.2.99"
    assert calls[0]["auto"] is True
    assert calls[0]["confirm"] is False
    assert calls[0]["cleanup_after_upgrade"] is True
    assert calls[0]["cleanup_keep_releases"] == 2


def test_cli_update_check_delegates_cache_root_to_application(monkeypatch, capsys, tmp_path: Path) -> None:
    import src.interfaces.cli.main as cli_main

    calls: list[dict[str, object]] = []

    def _fake_check(**kwargs):  # type: ignore[no-untyped-def]
        calls.append(dict(kwargs))
        return {"ok": True, "status": "checked"}

    monkeypatch.setattr(cli_main, "service_upgrade_check", _fake_check)

    rc = cli_main.main(
        [
            "update",
            "check",
            "--repo-root",
            str(tmp_path / "current"),
            "--runtime-root",
            str(tmp_path / "runtime"),
            "--cache-root",
            str(tmp_path / "_cache"),
        ]
    )

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert calls[0]["cache_root"] == str(tmp_path / "_cache")


def test_cli_service_cleanup_delegates_to_application(monkeypatch, capsys, tmp_path: Path) -> None:
    import src.interfaces.cli.main as cli_main

    calls: list[dict[str, object]] = []

    def _fake_cleanup(**kwargs):  # type: ignore[no-untyped-def]
        calls.append(dict(kwargs))
        return {"ok": True, "status": "dry_run", "changed": False}

    monkeypatch.setattr(cli_main, "service_cleanup", _fake_cleanup)

    rc = cli_main.main(
        [
            "service",
            "cleanup",
            "--repo-root",
            str(tmp_path / "current"),
            "--releases-root",
            str(tmp_path / "releases"),
            "--keep-releases",
            "3",
            "--cleanup-downloads",
            "--cleanup-pip-cache",
        ]
    )

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["tool_name"] == "service.cleanup"
    assert payload["ok"] is True
    assert calls[0]["repo_root"] == str(tmp_path / "current")
    assert calls[0]["releases_root"] == str(tmp_path / "releases")
    assert calls[0]["keep_releases"] == 3
    assert calls[0]["cleanup_downloads"] is True
    assert calls[0]["cleanup_pip_cache"] is True
    assert calls[0]["confirm"] is False


def test_service_cleanup_dry_run_reports_releases_and_caches(tmp_path: Path) -> None:
    from src.application.service_cleanup import service_cleanup

    apps = tmp_path / "apps"
    releases = apps / "releases"
    v100 = releases / "1.0.0"
    v101 = releases / "1.0.1"
    v102 = releases / "1.0.2"
    for release in (v100, v101, v102):
        _write_upgrade_release_skeleton(release, release.name)
        (release / "payload.txt").write_text(release.name, encoding="utf-8")
    current = apps / "current"
    current.symlink_to(v102, target_is_directory=True)
    downloads = apps / "_downloads"
    downloads.mkdir()
    (downloads / "release.tar.gz").write_text("download-cache", encoding="utf-8")

    out = service_cleanup(
        repo_root=current,
        releases_root=releases,
        cleanup_downloads=True,
    )

    assert out["ok"] is True
    assert out["status"] == "dry_run"
    assert out["changed"] is False
    assert out["active_release"] == str(v102.resolve())
    assert [item["version"] for item in out["kept_releases"]] == ["1.0.2", "1.0.1"]
    assert [Path(item["path"]).name for item in out["delete_releases"]] == ["1.0.0"]
    assert out["cache_dirs"][0]["path"] == str(downloads)
    assert out["estimated_freed_bytes"] > 0
    assert out["freed_bytes"] == 0
    assert out["deleted_paths"] == []
    assert v100.exists()
    assert downloads.exists()


def test_service_cleanup_confirm_deletes_only_old_releases_and_selected_caches(tmp_path: Path) -> None:
    from src.application.service_cleanup import service_cleanup

    apps = tmp_path / "apps"
    releases = apps / "releases"
    v100 = releases / "1.0.0"
    v101 = releases / "1.0.1"
    v102 = releases / "1.0.2"
    for release in (v100, v101, v102):
        _write_upgrade_release_skeleton(release, release.name)
    current = apps / "current"
    current.symlink_to(v102, target_is_directory=True)
    downloads = apps / "_downloads"
    downloads.mkdir()
    (downloads / "release.tar.gz").write_text("download-cache", encoding="utf-8")

    out = service_cleanup(
        repo_root=current,
        releases_root=releases,
        cleanup_downloads=True,
        confirm=True,
    )

    assert out["ok"] is True
    assert out["status"] == "cleaned"
    assert out["changed"] is True
    assert v102.exists()
    assert v101.exists()
    assert not v100.exists()
    assert not downloads.exists()
    assert str(v100) in out["deleted_paths"]
    assert str(downloads) in out["deleted_paths"]
    assert out["freed_bytes"] == out["estimated_freed_bytes"]


def test_service_cleanup_keeps_active_release_even_when_it_is_not_newest(tmp_path: Path) -> None:
    from src.application.service_cleanup import service_cleanup

    apps = tmp_path / "apps"
    releases = apps / "releases"
    v100 = releases / "1.0.0"
    v101 = releases / "1.0.1"
    v102 = releases / "1.0.2"
    for release in (v100, v101, v102):
        _write_upgrade_release_skeleton(release, release.name)
    current = apps / "current"
    current.symlink_to(v100, target_is_directory=True)

    out = service_cleanup(
        repo_root=current,
        releases_root=releases,
        keep_releases=2,
        confirm=True,
    )

    kept = {Path(item["path"]).name for item in out["kept_releases"]}
    assert kept == {"1.0.0", "1.0.2"}
    assert v100.exists()
    assert v102.exists()
    assert not v101.exists()


def test_service_cleanup_requires_repo_root_symlink(tmp_path: Path) -> None:
    from src.application.service_cleanup import service_cleanup

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "VERSION").write_text("1.0.0\n", encoding="utf-8")

    out = service_cleanup(repo_root=repo, releases_root=tmp_path / "releases", confirm=True)

    assert out["ok"] is False
    assert out["status"] == "repo_root_not_symlink"
    assert out["changed"] is False


def test_cli_run_trade_intake_delegates_to_application(monkeypatch) -> None:
    import src.application.trades.auto_intake as auto_intake
    from src.interfaces.cli.main import main

    calls: list[list[str]] = []
    monkeypatch.setattr(auto_intake, "main", lambda argv: calls.append(list(argv)) or 0)

    rc = main([
        "run",
        "trade-intake",
        "--config",
        "config.us.json",
        "--mode",
        "apply",
        "--once",
    ])

    assert rc == 0
    assert calls == [["--config", "config.us.json", "--mode", "apply", "--host", "127.0.0.1", "--port", "11111", "--once"]]
