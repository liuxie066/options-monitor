"""Isolated launchd service lifecycle regressions."""

import json
import os
import subprocess
from pathlib import Path

import pytest


def _bundle(monkeypatch: pytest.MonkeyPatch, module: object, profile: dict, files: dict[str, str]) -> None:
    rendered = {
        "files": [
            {"kind": "launchd_plist", "install_path": f"~/Library/LaunchAgents/{name}.plist", "content": content}
            for name, content in files.items()
        ] + [{"kind": "service_profile", "content": json.dumps(profile)}]
    }
    monkeypatch.setattr(module, "_expected_bundle_from_profile", lambda *_args, **_kwargs: rendered)


def test_launchd_drift_installs_owned_job_and_is_idempotent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import src.application.service_drift as drift_module

    monkeypatch.setenv("HOME", str(tmp_path))
    label = "com.options-monitor.tick-us"
    plist = tmp_path / "Library" / "LaunchAgents" / f"{label}.plist"
    profile = {
        "service_provider": "launchd",
        "repo_root": str(tmp_path / "current"),
        "runtime_root": str(tmp_path / "runtime"),
        "markets": ["us"],
        "accounts": ["lx"],
        "services": [{"name": label}],
    }
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    profile_path = runtime / "service.profile.json"
    profile_path.write_text(json.dumps(profile), encoding="utf-8")
    bundle = {
        "files": [
            {
                "kind": "launchd_plist",
                "install_path": f"~/Library/LaunchAgents/{label}.plist",
                "content": "<plist>tick-us</plist>\n",
            },
            {"kind": "service_profile", "content": json.dumps(profile)},
        ]
    }
    monkeypatch.setattr(drift_module, "_expected_bundle_from_profile", lambda *_args, **_kwargs: bundle)
    loaded: set[str] = set()
    mutations: list[list[str]] = []

    def run_cmd(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        args = list(command)
        if args[:2] == ["launchctl", "print-disabled"]:
            return subprocess.CompletedProcess(args, 0, f'{{"{label}" => false}}', "")
        if args[:2] == ["launchctl", "print"]:
            return subprocess.CompletedProcess(args, 0 if label in loaded else 113, "", "")
        mutations.append(args)
        if args[:2] == ["launchctl", "bootstrap"]:
            loaded.add(label)
        return subprocess.CompletedProcess(args, 0, "", "")

    kwargs = {
        "repo_root": tmp_path / "current",
        "runtime_root": runtime,
        "profile_path": profile_path,
        "run_cmd": run_cmd,
    }
    preview = drift_module.service_drift(**kwargs)
    assert preview["changed"] is False
    assert label in preview["missing_installed_units"]
    assert mutations == []

    applied = drift_module.service_drift(**kwargs, confirm=True)
    assert applied["apply_errors"] == []
    assert plist.read_text(encoding="utf-8") == "<plist>tick-us</plist>\n"
    assert label in loaded
    assert applied["summary"]["status"] == "ok"

    again = drift_module.service_drift(**kwargs, confirm=True)
    assert again["apply_errors"] == []
    assert again["changed"] is False


def test_launchd_drift_retires_live_only_old_job_and_preserves_unknown_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import src.application.service_drift as drift_module

    monkeypatch.setenv("HOME", str(tmp_path))
    old = "com.options-monitor.old-task"
    new = "com.options-monitor.tick-us"
    root = tmp_path / "Library" / "LaunchAgents"
    root.mkdir(parents=True)
    unknown = root / "com.options-monitor.user-owned.plist"
    unknown.write_text("user", encoding="utf-8")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    previous = {"service_provider": "launchd", "services": [{"name": old}], "markets": ["us"]}
    desired = {**previous, "services": [{"name": new}]}
    profile_path = runtime / "service.profile.json"
    profile_path.write_text(json.dumps(previous), encoding="utf-8")
    _bundle(monkeypatch, drift_module, desired, {new: "new plist"})
    loaded = {old}
    commands: list[list[str]] = []

    def run_cmd(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        args = list(command)
        commands.append(args)
        if args[1] == "print-disabled":
            return subprocess.CompletedProcess(args, 0, "{}", "")
        if args[1] == "print":
            return subprocess.CompletedProcess(args, 0 if args[2].split("/")[-1] in loaded else 113, "", "")
        if args[1] == "bootout":
            loaded.discard(args[2].split("/")[-1])
        if args[1] == "bootstrap":
            loaded.add(Path(args[3]).stem)
        return subprocess.CompletedProcess(args, 0, "", "")

    out = drift_module.service_drift(
        repo_root=tmp_path / "current", runtime_root=runtime,
        profile_path=profile_path, confirm=True, run_cmd=run_cmd,
    )
    assert out["apply_errors"] == []
    assert old not in loaded and new in loaded
    assert out["applied"]["retired_units"] == [old]
    assert out["summary"]["ok"] is True
    assert unknown.read_text(encoding="utf-8") == "user"
    assert str(unknown) in out["unknown_launchd_plists"]
    assert any(args[1] == "bootout" and args[2].endswith(old) for args in commands)


def test_launchd_drift_restores_running_job_after_bootstrap_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import src.application.service_drift as drift_module

    monkeypatch.setenv("HOME", str(tmp_path))
    label = "com.options-monitor.feishu-ws"
    root = tmp_path / "Library" / "LaunchAgents"
    root.mkdir(parents=True)
    plist = root / f"{label}.plist"
    plist.write_text("old", encoding="utf-8")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    profile = {"service_provider": "launchd", "services": [{"name": label}], "markets": ["us"]}
    profile_path = runtime / "service.profile.json"
    profile_path.write_text(json.dumps(profile), encoding="utf-8")
    _bundle(monkeypatch, drift_module, profile, {label: "new"})
    loaded = {label}
    bootstrap_calls = 0

    def run_cmd(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal bootstrap_calls
        args = list(command)
        if args[1] == "print-disabled":
            return subprocess.CompletedProcess(args, 0, "{}", "")
        if args[1] == "print":
            return subprocess.CompletedProcess(args, 0 if label in loaded else 113, "", "")
        if args[1] == "bootout":
            loaded.clear()
        if args[1] == "bootstrap":
            bootstrap_calls += 1
            if bootstrap_calls == 1:
                return subprocess.CompletedProcess(args, 5, "", "fixture bootstrap failed")
            loaded.add(label)
        return subprocess.CompletedProcess(args, 0, "", "")

    out = drift_module.service_drift(
        repo_root=tmp_path / "current", runtime_root=runtime,
        profile_path=profile_path, confirm=True, run_cmd=run_cmd,
    )
    assert out["apply_errors"]
    assert plist.read_text(encoding="utf-8") == "old"
    assert label in loaded
    assert any(item["operation"] == "restore_launchd_job" and item["ok"] for item in out["operations"])


def test_launchd_drift_fails_closed_on_unknown_disabled_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import src.application.service_drift as drift_module

    monkeypatch.setenv("HOME", str(tmp_path))
    label = "com.options-monitor.tick-us"
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    profile = {"service_provider": "launchd", "services": [{"name": label}], "markets": ["us"]}
    profile_path = runtime / "service.profile.json"
    profile_path.write_text(json.dumps(profile), encoding="utf-8")
    _bundle(monkeypatch, drift_module, profile, {label: "plist"})
    commands: list[list[str]] = []

    def run_cmd(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(list(command))
        return subprocess.CompletedProcess(command, 0, f'{{"{label}" => maybe}}', "")

    out = drift_module.service_drift(
        repo_root=tmp_path / "current", runtime_root=runtime,
        profile_path=profile_path, confirm=True, run_cmd=run_cmd,
    )
    assert out["apply_errors"]
    assert out["summary"]["status"] == "error"
    assert not (tmp_path / "Library" / "LaunchAgents" / f"{label}.plist").exists()
    assert all(command[1] == "print-disabled" for command in commands)


def test_launchd_drift_reports_enable_only_as_a_write(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import src.application.service_drift as drift_module

    monkeypatch.setenv("HOME", str(tmp_path))
    label = "com.options-monitor.tick-us"
    root = tmp_path / "Library" / "LaunchAgents"
    root.mkdir(parents=True)
    (root / f"{label}.plist").write_text("same", encoding="utf-8")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    profile = {"service_provider": "launchd", "services": [{"name": label}], "markets": ["us"]}
    profile_path = runtime / "service.profile.json"
    profile_path.write_text(json.dumps(profile), encoding="utf-8")
    _bundle(monkeypatch, drift_module, profile, {label: "same"})
    disabled = True

    def run_cmd(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal disabled
        args = list(command)
        if args[1] == "print-disabled":
            return subprocess.CompletedProcess(args, 0, f'{{"{label}" => {str(disabled).lower()}}}', "")
        if args[1] == "enable":
            disabled = False
        return subprocess.CompletedProcess(args, 0, "", "")

    out = drift_module.service_drift(
        repo_root=tmp_path / "current", runtime_root=runtime,
        profile_path=profile_path, confirm=True, run_cmd=run_cmd,
    )
    assert out["apply_errors"] == []
    assert out["changed"] is True
    assert out["applied"]["enabled_units"] == [label]


@pytest.mark.parametrize("preserve", [True, False])
@pytest.mark.parametrize("label", ["com.options-monitor.feishu-ws", "com.options-monitor.opend.us"])
def test_launchd_drift_does_not_reload_paused_or_no_restart_job(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, preserve: bool, label: str
) -> None:
    import src.application.service_drift as drift_module

    monkeypatch.setenv("HOME", str(tmp_path))
    root = tmp_path / "Library" / "LaunchAgents"
    root.mkdir(parents=True)
    (root / f"{label}.plist").write_text("old", encoding="utf-8")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    profile = {"service_provider": "launchd", "services": [{"name": label}], "markets": ["us"]}
    profile_path = runtime / "service.profile.json"
    profile_path.write_text(json.dumps(profile), encoding="utf-8")
    _bundle(monkeypatch, drift_module, profile, {label: "new"})
    commands: list[list[str]] = []

    def run_cmd(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        args = list(command)
        commands.append(args)
        if args[1] == "print-disabled":
            return subprocess.CompletedProcess(args, 0, f'{{"{label}" => true}}' if preserve else "{}", "")
        if args[1] == "print":
            return subprocess.CompletedProcess(args, 113 if preserve else 0, "", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    out = drift_module.service_drift(
        repo_root=tmp_path / "current", runtime_root=runtime,
        profile_path=profile_path, confirm=True, run_cmd=run_cmd,
        activation_policy="preserve-existing" if preserve else "ensure-active",
        preserved_activation_states={label: {"activation_state": "disabled", "active_state": "inactive"}} if preserve else None,
        restart_services=False,
    )
    assert out["apply_errors"] == []
    assert out["summary"]["ok"] is True
    assert (root / f"{label}.plist").read_text(encoding="utf-8") == "new"
    assert not any(args[1] in {"bootout", "bootstrap", "enable"} for args in commands)
    if preserve:
        assert label in out["preserved_activation_units"]
    else:
        assert out["applied"]["deferred_reload_units"] == [label]


def test_launchd_drift_defers_live_self_upgrade_job(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import src.application.service_drift as drift_module

    monkeypatch.setenv("HOME", str(tmp_path))
    label = "com.options-monitor.upgrade"
    root = tmp_path / "Library" / "LaunchAgents"
    root.mkdir(parents=True)
    plist = root / f"{label}.plist"
    plist.write_text("old", encoding="utf-8")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    profile = {"service_provider": "launchd", "services": [{"name": label}], "markets": ["us"]}
    profile_path = runtime / "service.profile.json"
    profile_path.write_text(json.dumps(profile), encoding="utf-8")
    _bundle(monkeypatch, drift_module, profile, {label: "new"})
    commands: list[list[str]] = []

    def run_cmd(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        args = list(command)
        commands.append(args)
        return subprocess.CompletedProcess(args, 0, "{}" if args[1] == "print-disabled" else "", "")

    out = drift_module.service_drift(
        repo_root=tmp_path / "current", runtime_root=runtime,
        profile_path=profile_path, confirm=True, run_cmd=run_cmd,
    )
    assert out["summary"]["ok"] is True
    assert plist.read_text(encoding="utf-8") == "new"
    assert out["applied"]["deferred_reload_units"] == [label]
    assert not any(args[1] in {"bootout", "bootstrap"} for args in commands)


def test_launchd_upgrade_helpers_restart_and_health_with_pid(tmp_path: Path) -> None:
    from src.application.service_upgrade import (
        _post_upgrade_service_health, _restart_services_from_loaded_profile,
    )

    label = "com.options-monitor.trade-intake"
    profile = {"service_provider": "launchd", "services": [{"name": label}]}
    commands: list[list[str]] = []

    def run_cmd(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        args = list(command)
        commands.append(args)
        return subprocess.CompletedProcess(args, 0, "pid = 123\n", "")

    assert _restart_services_from_loaded_profile(profile=profile, run_cmd=run_cmd, operations=[]) == [label]
    assert ["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{label}"] in commands
    result = _post_upgrade_service_health(profile=profile, repo_root=tmp_path, run_cmd=run_cmd, operations=[])
    assert result["ok"] and result["status"] == "ok"
    assert any(check["check"] == "launchd-running-pid" for check in result["checks"])


def test_launchd_upgrade_helpers_preserve_paused_and_fail_without_pid(tmp_path: Path) -> None:
    from src.application.service_upgrade import (
        _post_upgrade_service_health, _restart_services_from_loaded_profile,
    )

    label = "com.options-monitor.trade-intake"
    profile = {"service_provider": "launchd", "services": [{"name": label}]}
    commands: list[list[str]] = []

    def run_cmd(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        args = list(command)
        commands.append(args)
        return subprocess.CompletedProcess(args, 0, "service registered\n", "")

    paused = {label: {"activation_state": "disabled", "active_state": "inactive"}}
    assert _restart_services_from_loaded_profile(
        profile=profile, run_cmd=run_cmd, operations=[], preserved_activation_states=paused,
    ) == []
    assert commands == []
    result = _post_upgrade_service_health(
        profile=profile, repo_root=tmp_path, run_cmd=run_cmd, operations=[],
        monotonic_fn=lambda: 100.0, sleep_fn=lambda _: None,
    )
    assert not result["ok"]
    assert result["failed_checks"][0]["check"] == "launchd-running-pid"


def test_launchd_child_reconcile_requires_successful_exit(tmp_path: Path) -> None:
    from src.application.service_upgrade import (
        ServiceTransitionError, _reconcile_services_from_current_release,
    )

    def run_cmd(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 2, json.dumps({"checked": True, "supported": True, "summary": {"status": "ok"}}), "failed")

    with pytest.raises(ServiceTransitionError):
        _reconcile_services_from_current_release(
            repo_link=tmp_path / "current", target_dir=tmp_path / "release",
            runtime=tmp_path / "runtime", activation_policy="ensure-active", run_cmd=run_cmd,
        )


def test_launchd_child_reconcile_forwards_no_restart(tmp_path: Path) -> None:
    from src.application.service_upgrade import _reconcile_services_from_current_release

    calls: list[list[str]] = []

    def run_cmd(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(list(command))
        return subprocess.CompletedProcess(command, 0, json.dumps({"summary": {"status": "ok"}}), "")

    _reconcile_services_from_current_release(
        repo_link=tmp_path / "current", target_dir=tmp_path / "release",
        runtime=tmp_path / "runtime", activation_policy="ensure-active",
        restart_services=False, run_cmd=run_cmd,
    )
    assert "--no-restart-services" in calls[0]


def test_launchd_reconcile_rejects_unconfirmed_or_unconverged_response() -> None:
    from src.application.service_upgrade import _service_reconcile_failed

    healthy = {"provider": "launchd", "checked": True, "supported": True,
               "confirmed": True, "summary": {"status": "ok", "ok": True}}
    assert not _service_reconcile_failed(healthy)
    assert _service_reconcile_failed({**healthy, "confirmed": False})
    assert _service_reconcile_failed({**healthy, "extra_installed_units": ["com.options-monitor.old"]})
    assert _service_reconcile_failed({**healthy, "summary": {"status": "skipped"}})
    assert _service_reconcile_failed({}, expected_provider="launchd")


def test_launchd_activation_snapshot_captures_paused_job(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import src.application.service_upgrade as module

    label = "com.options-monitor.trade-intake"
    monkeypatch.setattr(module, "service_drift", lambda **_kwargs: {
        "checked": True, "supported": True, "expected_services": [label],
        "installed_units": [label], "activation_states": {label: "disabled"},
        "active_states": {label: "inactive"},
    })
    snapshot = module.capture_preserved_timer_activation_states(
        repo_root=tmp_path, runtime_root=tmp_path,
        profile={"service_provider": "launchd"}, run_cmd=lambda *_args, **_kwargs: None,
    )
    assert snapshot == {label: {"activation_state": "disabled", "active_state": "inactive"}}


def test_mac_dual_market_render_cli_accepts_explicit_feishu_market(tmp_path: Path) -> None:
    repo = Path(__file__).resolve().parents[1]
    config = {"accounts": ["lx", "sy"], "account_settings": {
        "lx": {"type": "futu", "futu": {"host": "127.0.0.1", "port": 11111}},
        "sy": {"type": "futu", "futu": {"host": "127.0.0.1", "port": 11111}},
    }}
    us = tmp_path / "config.us.json"
    hk = tmp_path / "config.hk.json"
    us.write_text(json.dumps(config), encoding="utf-8")
    hk.write_text(json.dumps(config), encoding="utf-8")
    yaml = tmp_path / "config.yaml"
    yaml.write_text((repo / "configs/examples/config.yaml.example").read_text(encoding="utf-8"), encoding="utf-8")
    command = [str(repo / "om"), "service", "render", "--target", "launchd",
               "--repo-root", str(repo), "--runtime-root", str(tmp_path / "runtime"),
               "--markets", "us", "hk", "--accounts", "lx",
               "--config-yaml", str(yaml),
               "--config-us", str(us), "--config-hk", str(hk),
               "--include-feishu-ws", "--feishu-ws-config-key", "us"]
    proc = subprocess.run(command, cwd=repo, capture_output=True, text=True, timeout=30, check=False)
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    profile_file = next(item for item in payload["data"]["files"] if item["kind"] == "service_profile")
    assert json.loads(profile_file["content"])["feishu_ws"]["config_key"] == "us"


@pytest.mark.parametrize(("child_fails", "pid_fails_once"), [(False, False), (True, False), (False, True)])
def test_launchd_release_transition_and_compensation_use_fixed_provider(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, child_fails: bool, pid_fails_once: bool
) -> None:
    import src.application.service_upgrade as module
    from tests.service_deploy_test_support import _write_upgrade_release_skeleton

    releases = tmp_path / "releases"
    old = releases / "1.0.0"
    new = releases / "1.0.1"
    _write_upgrade_release_skeleton(old, "1.0.0")
    _write_upgrade_release_skeleton(new, "1.0.1")
    current = tmp_path / "current"
    current.symlink_to(old, target_is_directory=True)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    label = "com.options-monitor.trade-intake"
    profile = {"service_provider": "launchd", "services": [{"name": label}]}
    (runtime / "service.profile.json").write_text(json.dumps(profile), encoding="utf-8")
    monkeypatch.setattr(module, "service_upgrade_check", lambda **_kwargs: {
        "ok": True, "latest_version": "1.0.1", "release_tag": "v1.0.1",
    })
    monkeypatch.setattr(module, "_materialize_release_from_git_cache", lambda **_kwargs: {"status": "reused"})
    monkeypatch.setattr(module, "_ensure_release_runtime", lambda **_kwargs: {"status": "ready"})
    monkeypatch.setattr(module, "_run_required", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(module, "_pi_storage_readiness", lambda **_kwargs: {"ok": True, "status": "ready"})
    monkeypatch.setattr(module, "_prepare_runtime_configs_for_release", lambda **_kwargs: {"status": "prepared"})
    monkeypatch.setattr(module, "_commit_prepared_runtime_configs", lambda **_kwargs: {"status": "committed"})
    monkeypatch.setattr(module, "_validate_committed_runtime_configs", lambda **_kwargs: [])
    monkeypatch.setattr(module, "_restore_committed_runtime_configs", lambda **_kwargs: {"ok": True, "errors": []})
    compensation_calls: list[dict] = []

    def drift(**kwargs: object) -> dict:
        compensation_calls.append(dict(kwargs))
        return {"provider": "launchd", "checked": True, "supported": True,
                "confirmed": True, "summary": {"status": "ok", "ok": True}, "apply_errors": []}

    monkeypatch.setattr(module, "service_drift", drift)
    commands: list[list[str]] = []
    pid_checks = 0

    def run_cmd(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal pid_checks
        args = list(command)
        commands.append(args)
        if args[:3] == [str(new / "om"), "service", "drift"]:
            data = {"provider": "launchd", "checked": True, "supported": True,
                    "confirmed": True, "summary": {"status": "ok", "ok": True}, "apply_errors": []}
            return subprocess.CompletedProcess(args, 1 if child_fails else 0, json.dumps(data), "child failed" if child_fails else "")
        if args[:2] == ["launchctl", "list"]:
            pid_checks += 1
            if pid_fails_once and pid_checks <= 5:
                return subprocess.CompletedProcess(args, 0, "registered without PID\n", "")
            return subprocess.CompletedProcess(args, 0, '"PID" = 123;\n', "")
        return subprocess.CompletedProcess(args, 0, "", "")

    out = module.service_upgrade(
        repo_root=current, runtime_root=runtime, releases_root=releases,
        confirm=True, restart_services=True, run_cmd=run_cmd,
    )
    if child_fails or pid_fails_once:
        assert out["status"] == "upgrade_failed_rolled_back"
        assert out["rolled_back"] is True
        assert current.resolve() == old.resolve()
        assert len(compensation_calls) == 1
        assert compensation_calls[0]["restart_services"] is True
        if pid_fails_once:
            assert out["failure_status"] == "upgraded_service_health_failed"
    else:
        assert out["status"] == "upgraded"
        assert current.resolve() == new.resolve()
        assert out["restarted_services"] == [label]
        assert out["service_health"]["ok"] is True
        assert not compensation_calls
        rolled_back = module.service_rollback(
            repo_root=current, runtime_root=runtime, releases_root=releases,
            to_version="1.0.0", confirm=True, restart_services=True, run_cmd=run_cmd,
        )
        assert rolled_back["status"] == "rolled_back"
        assert current.resolve() == old.resolve()
        assert rolled_back["restarted_services"] == [label]
        assert len(compensation_calls) == 1
