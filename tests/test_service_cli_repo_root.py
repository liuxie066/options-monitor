from __future__ import annotations

import json
import os
import pwd
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from src.application.service_deploy import render_service_bundle
from src.application.service_drift import service_drift
from src.application.service_lifecycle import service_lifecycle
from src.interfaces.cli import main as cli
from src.interfaces.cli import service_ops
from tests.service_deploy_test_support import (
    _fake_systemd_query_runner,
    _synthetic_drift_context,
    _write_systemd_units_from_bundle,
)
from tests.test_service_lifecycle import Manager, _preview_probe, apply, context, instance


REPO = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("target", ["systemd", "launchd"])
@pytest.mark.parametrize("empty_services", [False, True])
@pytest.mark.parametrize("inbound, metadata, error", [
    ("feishu_ws", {}, "feishu_ws_config_key"),
    ("wechat_clawbot", {"allowed_senders": "wechat:fixture"}, "wechat_clawbot_config_key"),
    ("wechat_clawbot", {"config_key": "us"}, "wechat_clawbot_allowed_senders"),
])
def test_cli_drift_incomplete_inbound_is_config_error_without_effects(
    tmp_path, monkeypatch, capsys, target, empty_services, inbound, metadata, error,
):
    from src.application import service_drift as owner

    fixture = _synthetic_drift_context(monkeypatch, tmp_path, target=target)
    profile = fixture["profile"]
    profile["markets"] = ["us", "hk"]
    profile[inbound] = {"enabled": True, **metadata}
    name = inbound.replace("_", "-")
    unit_name = f"options-monitor-{name}.service" if target == "systemd" else f"com.options-monitor.{name}"
    unit = (fixture["units"] / unit_name if target == "systemd" else
            fixture["home"] / "Library/LaunchAgents" / f"{unit_name}.plist")
    assert unit.is_relative_to(tmp_path)
    unit.write_text("installed inbound definition\n", encoding="utf-8")
    profile["services"] = [] if empty_services else [*profile["services"], {"name": unit_name}]
    fixture["profile_path"].write_text(json.dumps(profile), encoding="utf-8")
    monkeypatch.setattr(owner, "_apply_service_drift", fixture["fail"]("apply"))
    monkeypatch.setattr(cli, "service_drift", lambda **kwargs: service_drift(
        **kwargs, systemd_unit_root=fixture["units"], run_cmd=fixture["fail"]("runner"),
    ))
    before = fixture["inventory"]()
    for extra in ([], ["--confirm"]):
        assert cli.main([
            "service", "drift", "--repo-root", str(fixture["repo"]),
            "--runtime-root", str(fixture["runtime"]), *extra,
        ]) == 2
        response = json.loads(capsys.readouterr().out)
        assert response["ok"] is False
        assert response["error"]["code"] == "CONFIG_ERROR"
        assert error in response["error"]["message"]
        assert fixture["inventory"]() == before
    assert fixture["forbidden"] == []


@pytest.fixture
def linked_entry(monkeypatch, tmp_path):
    current = tmp_path / "current"
    current.symlink_to(REPO, target_is_directory=True)
    neutral = tmp_path / "neutral"
    neutral.mkdir()
    monkeypatch.chdir(neutral)
    monkeypatch.setattr(service_ops, "__file__", str(current / "src/interfaces/cli/service_ops.py"))
    return current


@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize("argv,consumer", [
    (["service", "install"], "service_lifecycle"),
    (["service", "start"], "service_lifecycle"),
    (["service", "stop"], "service_lifecycle"),
    (["service", "drift"], "service_drift"),
    (["service", "credentials-migrate", "--secret-credential-delivery", "runtime-files"], "migrate_service_credentials"),
    (["service", "cleanup"], "service_cleanup"),
    (["update", "check"], "service_upgrade_check"),
    (["update", "verify", "--no-check-latest"], "service_upgrade_verify"),
    (["update", "apply", "--target-version", "1.0.1"], "service_upgrade"),
    (["update", "rollback", "--to-version", "1.0.0"], "service_rollback"),
])
def test_main_service_defaults_and_explicit_override(linked_entry, monkeypatch, tmp_path, capsys, argv, consumer, explicit):
    captured = []

    def capture(*args, **kwargs):
        captured.append(kwargs)
        return {"ok": True, "summary": {"ok": True}}

    original_handler = cli.handle_service_update_command

    def handle(args, **kwargs):
        kwargs[consumer + "_fn"] = capture
        return original_handler(args, **kwargs)

    monkeypatch.setattr(cli, "handle_service_update_command", handle)
    requested = tmp_path / "explicit-repo" if explicit else linked_entry
    options = ["--runtime-root", str(tmp_path / "runtime")]
    if explicit:
        options += ["--repo-root", str(requested)]
    assert cli.main(argv + options) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    assert len(captured) == 1
    assert Path(captured[0]["repo_root"]) == requested
    assert cli.repo_base() == REPO


@pytest.fixture(scope="module")
def releases(tmp_path_factory):
    base = tmp_path_factory.mktemp("service-cli-releases")
    for version in ("1.0.0", "1.0.1"):
        release = base / version
        release.mkdir()
        for name in ("src", "domain", "om_strategy_core", "scripts", "services", "configs", "contracts"):
            shutil.copytree(REPO / name, release / name, ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copy2(REPO / "om", release / "om")
        (release / "VERSION").write_text(version + "\n")
    return base


@pytest.mark.parametrize("target", ["systemd", "launchd"])
def test_real_wrapper_render_keeps_current_across_release_switch(releases, tmp_path, target):
    current = tmp_path / "current"
    neutral, runtime = tmp_path / "neutral", tmp_path / "runtime"
    neutral.mkdir()
    runtime.mkdir()
    source = runtime / "config.yaml"
    source.write_text("accounts: [lx]\n")
    env = {"PATH": os.environ["PATH"], "OM_PYTHON": sys.executable, "OM_RUNTIME_ROOT": str(runtime)}

    def render(*extra):
        result = subprocess.run([
            str(current / "om"), "service", "render", "--target", target,
            "--runtime-root", str(runtime), "--config-yaml", str(source),
            "--accounts", "lx", "--markets", "us", *extra,
        ], cwd=neutral, env=env, capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr + result.stdout
        data = json.loads(result.stdout)["data"]
        profile = json.loads(next(item["content"] for item in data["files"] if item["relative_path"] == "service.profile.json"))
        assert not (runtime / "service.profile.json").exists()
        return data, profile

    for version in ("1.0.0", "1.0.1"):
        if current.is_symlink():
            current.unlink()
        current.symlink_to(releases / version, target_is_directory=True)
        data, profile = render()
        assert profile["repo_root"] == str(current)
        assert str(current / "om") in str(data)
        assert str(releases / version) not in str(data)
    explicit = releases / "1.0.0"
    _, profile = render("--repo-root", str(explicit))
    assert profile["repo_root"] == str(explicit)


def test_default_cli_drift_and_stop_share_installed_link_scope(linked_entry, monkeypatch, tmp_path, capsys):
    runtime, units = tmp_path / "runtime", tmp_path / "units"
    runtime.mkdir()
    source = runtime / "config.yaml"
    source.write_text("accounts: [lx]\n")
    bundle = render_service_bundle(target="systemd", repo_root=linked_entry, runtime_root=runtime,
                                   config_yaml=source, accounts=["lx"], markets=["us"],
                                   config_paths={"us": runtime / "config.us.json"},
                                   deploy_user=pwd.getpwuid(os.getuid()).pw_name)
    _write_systemd_units_from_bundle(bundle, units)
    profile = runtime / "service.profile.json"
    profile.write_text(next(item["content"] for item in bundle["files"] if item["relative_path"] == "service.profile.json"))
    before = {path.name: path.read_bytes() for path in units.iterdir()}
    profile_before = profile.read_bytes()
    runner = _fake_systemd_query_runner()
    monkeypatch.setattr(cli, "service_drift", lambda **kwargs: service_drift(**kwargs,
        systemd_unit_root=units, managed_root_uid=os.getuid(), managed_root_gid=os.getgid(), run_cmd=runner))
    for extra in ([], ["--confirm"], ["--confirm"]):
        assert cli.main(["service", "drift", "--runtime-root", str(runtime), *extra]) == 0
        data = json.loads(capsys.readouterr().out)["data"]
        assert data["mismatched_units"] == []
        assert data["profile_content_changed"] is False
        assert data["changed"] is False
    assert profile.read_bytes() == profile_before
    assert {path.name: path.read_bytes() for path in units.iterdir()} == before
    # The direct adapter's default must be the same as main's explicit callback.
    out = service_ops.handle_service_update_command(cli.parse_args(["service", "drift", "--runtime-root", str(runtime)]),
        service_drift_fn=lambda **kwargs: service_drift(**kwargs, systemd_unit_root=units,
            managed_root_uid=os.getuid(), managed_root_gid=os.getgid(), run_cmd=runner))
    assert out["ok"] is True
    assert out["data"]["repo_root"] == str(linked_entry)

    manager = Manager(units, "systemd")
    original_handler = cli.handle_service_update_command
    monkeypatch.setattr(cli, "handle_service_update_command", lambda args, **kwargs: original_handler(args,
        service_lifecycle_fn=lambda action, **options: service_lifecycle(action, **options,
            unit_root=units, system="Linux", run_cmd=manager), **kwargs))
    assert cli.main(["service", "stop", "--runtime-root", str(runtime)]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    assert not any(call[1] in {"stop", "disable", "enable", "daemon-reload"} for call in manager.calls)
    assert cli.main(["service", "stop", "--runtime-root", str(runtime), "--repo-root", str(REPO)]) == 2
    error = json.loads(capsys.readouterr().out)["error"]
    assert error["code"] == "SERVICE_INSTANCE_CONFLICT"
    assert error["details"]["field"] == "repo_root"


@pytest.mark.parametrize('env_line', [
    'Environment="HOME=/fictional" \\\n # ignored \\\n ; ignored too\n "API_KEY={secret}"', 'Environment="API_KEY={secret}',
    'ExecStartPost=/bin/true \\\n\xa0# ordinary\nEnvironment=API_KEY={secret}', 'Description=ordinary\x00Environment=API_KEY={secret}',
    'Environment="API_KEY={secret}" \\',
    'Environment="HOME=/fictional" \\\n "API_KEY={secret}" \\',
    'Environment="HOME=/fictional" \\\n\r "API_KEY={secret}"',
])
def test_cli_install_preview_redacts_installed_environment(instance, tmp_path, monkeypatch, capsys, env_line):
    ctx, manager = context(instance, tmp_path, 'systemd')
    apply('install', ctx)
    unit = ctx['unit_root'] / 'options-monitor-tick-us.service'
    sentinel = 'fake-cli-secret-526'
    original = unit.read_text()
    installed = (original.split('[Install]', 1)[0] + env_line.format(secret=sentinel)
                 if env_line.endswith('\\') else original.replace('[Service]', '[Service]\n' + env_line.format(secret=sentinel)))
    unit.write_text(installed)
    before = {path: path.read_bytes() for path in tmp_path.rglob('*') if path.is_file()}
    manager.calls.clear()
    handler = cli.handle_service_update_command
    monkeypatch.setattr(cli, 'handle_service_update_command', lambda args, **kwargs: handler(args,
        service_lifecycle_fn=lambda action, **options: service_lifecycle(action, **options,
            unit_root=ctx['unit_root'], system='Linux', run_cmd=_preview_probe(ctx, manager, sentinel),
            credential_store_root=tmp_path / 'credentials'), **kwargs))
    assert cli.main(['service', 'install', '--repo-root', str(REPO), '--runtime-root', str(instance),
                     '--target', 'systemd']) == 0
    captured = capsys.readouterr()
    assert sentinel not in captured.out + captured.err
    data = json.loads(captured.out)['data']
    assert data['ok'] and not data['confirmed'] and not data['files_changed']
    assert '<redacted>' in str(data['files'])
    assert before == {path: path.read_bytes() for path in tmp_path.rglob('*') if path.is_file()}
    assert manager.calls and all(call[1] == 'show' for call in manager.calls)


def test_cli_cleanup_real_owner_busy_then_retry(tmp_path, capsys):
    from src.application.service_upgrade import _UpgradeLock

    releases = tmp_path / 'releases'
    for version in ('1.0.0', '1.1.0', '1.2.0'):
        path = releases / version
        path.mkdir(parents=True)
        (path / 'VERSION').write_text(version)
        (path / "src/application/bot").mkdir(parents=True, exist_ok=True)
        (path / "src/application/bot/runtime.py").write_text("# Python Bot fixture\n", encoding="utf-8")
    current = tmp_path / 'current'
    current.symlink_to(releases / '1.2.0')
    runtime = tmp_path / 'runtime'
    argv = ['service', 'cleanup', '--repo-root', str(current), '--releases-root', str(releases),
            '--runtime-root', str(runtime), '--confirm']
    with _UpgradeLock(runtime / 'locks/upgrade.lock'):
        assert cli.main(argv) != 0
        result = json.loads(capsys.readouterr().out)
        assert result['ok'] is False and result['data']['status'] == 'upgrade_locked'
        assert result['data']['changed'] is False
        assert len(list(releases.iterdir())) == 3
    assert cli.main(argv) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['ok'] and result['data']['status'] == 'cleaned'
    assert current.resolve().is_dir() and len(list(releases.iterdir())) == 2
