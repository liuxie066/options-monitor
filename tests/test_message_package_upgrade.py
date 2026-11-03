"""Message package preservation through the real upgrade boundary; all effects offline."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from src.application import service_upgrade as upgrade
from src.application import runtime_bundle
from tests.test_pm_package_upgrade import OfflineRuntime, _bundled_runtime, _upgrade, transition


ROOT = Path(__file__).resolve().parents[1]
PACKAGES = (("feishu-channel", "om_feishu_channel"), ("wechat-channel", "om_wechat_channel"))


class MessageRuntime(OfflineRuntime):
    def __init__(self, previous, feishu=False, wechat=False):
        super().__init__(previous, installed=False)
        self.messages = [feishu, wechat]
        self.message_probe_output = json.dumps(self.messages)
        self.message_installs = []
        self.message_validation_fails = False

    def __call__(self, command, **kwargs):
        code = command[command.index("-c") + 1] if "-c" in command else ""
        if command[0] == str(upgrade._release_python(self.previous)) and "om-feishu-channel-plugin" in code:
            self.commands.append(list(command))
            return subprocess.CompletedProcess(command, 0, self.message_probe_output, "")
        if "install" in command and any(f"./plugins/{p}" in command for p, _ in PACKAGES):
            self.message_installs.append(list(command))
        if "load_channel_plugin" in code:
            self.commands.append(list(command))
            status = int(self.message_validation_fails)
            return subprocess.CompletedProcess(command, status, "", "broken message package" if status else "")
        return super().__call__(command, **kwargs)


@pytest.fixture
def message_transition(transition):
    previous, target, current, events = transition
    for release in (previous, target):
        for package, _ in PACKAGES:
            shutil.copytree(ROOT / "plugins" / package, release / "plugins" / package,
                            ignore=shutil.ignore_patterns("build", "*.egg-info", "__pycache__"))
        for name in (
            "src/application/channels/plugins.py", "src/application/agent_tool_contracts.py",
            "src/infrastructure/feishu_bot.py", "src/infrastructure/feishu_bitable.py",
            "src/application/channels/wechat_clawbot/ilink_client.py",
        ):
            path = release / name
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / name, path)
    return transition


def _legacy(previous):
    (previous / "src/application/channels/plugins.py").unlink()
    for package, _ in PACKAGES:
        shutil.rmtree(previous / "plugins" / package)


def _runtime_config(current, config, profile=None):
    runtime = current.parent / "runtime"
    runtime.mkdir(exist_ok=True)
    path = runtime / "effective.json"
    path.write_text(json.dumps(config))
    profile = {"service_provider": "systemd", "config_paths": {"us": str(path)}, **(profile or {})}
    (runtime / "service.profile.json").write_text(json.dumps(profile))
    return path


@pytest.mark.parametrize("mode,uv_fails", [("pip", False), ("uv", False), ("auto", True)])
@pytest.mark.parametrize("feishu,wechat", [(False, False), (True, False), (False, True), (True, True)])
def test_installed_message_selection_reaches_installers_without_enabling(
    monkeypatch, message_transition, mode, uv_fails, feishu, wechat,
):
    monkeypatch.setenv("OM_UPGRADE_INSTALLER", mode)
    previous, target, current, events = message_transition
    config = _runtime_config(current, {"channels": {}, "notifications": {"enabled": False}})
    before = config.read_bytes()
    runner = MessageRuntime(previous, feishu, wechat)
    runner.uv_fails = uv_fails
    result = _upgrade(message_transition, runner)
    assert result["ok"], result.get("error", result)
    assert current.resolve() == target
    for package, selected, key in (("feishu-channel", feishu, "include_feishu"),
                                   ("wechat-channel", wechat, "include_wechat")):
        assert any(f"./plugins/{package}" in cmd for cmd in runner.message_installs) is selected
        assert result["runtime_prepare"]["dependency_context"].get(key, False) is selected
    assert config.read_bytes() == before


@pytest.mark.parametrize("feishu,wechat", [(True, False), (False, True), (True, True)])
def test_explicit_removal_does_not_restore_from_enabled_config_or_selected_cache(message_transition, feishu, wechat):
    previous, target, current, events = message_transition
    _runtime_config(current, {"channels": {
        channel: {"enabled": True, "receive_enabled": True, "notify_enabled": True}
        for channel in ("feishu", "wechat_clawbot")
    }})
    runner = MessageRuntime(previous)
    cached = upgrade._ensure_release_runtime(
        target_dir=target, include_feishu=feishu, include_wechat=wechat,
        cache_root=current.parent / "cache", run_cmd=runner, operations=[],
    )
    runner.message_installs.clear()
    result = _upgrade(message_transition, runner)
    assert result["ok"], result.get("error", result)
    assert not runner.message_installs
    assert result["runtime_prepare"]["dependency_hash"] != cached["dependency_hash"]
    assert Path(cached["shared_venv_path"]).is_dir()


@pytest.mark.parametrize("channel,provider", [("feishu", "feishu_app"), ("wechat_clawbot", "wechat_clawbot")])
@pytest.mark.parametrize("enabled", [False, True, None])
def test_legacy_outbound_uses_effective_config_not_repo_decoy(message_transition, channel, provider, enabled):
    previous, target, current, events = message_transition
    _legacy(previous)
    notification_config = {"provider": provider}
    if enabled is not None:
        notification_config["enabled"] = enabled
    config = _runtime_config(current, {"notifications": notification_config})
    (previous / "config.us.json").write_text(json.dumps({"notifications": {"enabled": not enabled, "provider": provider}}))
    runner = MessageRuntime(previous)
    result = _upgrade(message_transition, runner)
    assert result["ok"], result.get("error", result)
    package = "feishu-channel" if channel == "feishu" else "wechat-channel"
    assert any(f"./plugins/{package}" in cmd for cmd in runner.message_installs) is (enabled is not False)
    assert json.loads(config.read_text())["notifications"] == notification_config


@pytest.mark.parametrize("channel,profile_key", [("feishu", "feishu_ws"), ("wechat_clawbot", "wechat_clawbot")])
@pytest.mark.parametrize("controls,expected", [
    (None, True), ({}, False), ({"enabled": True}, False),
    ({"enabled": True, "receive_enabled": True}, True),
    ({"enabled": False, "receive_enabled": True}, False),
    ({"enabled": True, "receive_enabled": False, "notify_enabled": True}, False),
])
def test_legacy_inbound_profile_obeys_explicit_channel_controls(message_transition, channel, profile_key, controls, expected):
    previous, target, current, events = message_transition
    _legacy(previous)
    config = {"notifications": {"enabled": False}, "feishu_app_id": "retained-not-activation"}
    if controls is not None:
        config["channels"] = {channel: controls}
    _runtime_config(current, config, {profile_key: {"enabled": True, "config_key": "us"}})
    runner = MessageRuntime(previous)
    result = _upgrade(message_transition, runner)
    assert result["ok"], result.get("error", result)
    package = "feishu-channel" if channel == "feishu" else "wechat-channel"
    assert any(f"./plugins/{package}" in cmd for cmd in runner.message_installs) is expected


def test_legacy_credentials_and_retained_state_alone_never_install_messages(message_transition):
    previous, target, current, events = message_transition
    _legacy(previous)
    _runtime_config(current, {"feishu": {"app_id": "retained"}, "inbound": {"wechat_clawbot": {"token": "retained"}}})
    runner = MessageRuntime(previous)
    result = _upgrade(message_transition, runner)
    assert result["ok"], result.get("error", result)
    assert not runner.message_installs


@pytest.mark.parametrize("contents", [None, "not json", "[]", '{"notifications":{"enabled":"false"}}'])
def test_unknown_legacy_effective_config_blocks_before_preparing_runtime(message_transition, contents):
    previous, target, current, events = message_transition
    _legacy(previous)
    path = _runtime_config(current, {})
    if contents is None:
        path.unlink()
    else:
        path.write_text(contents)
    runner = MessageRuntime(previous)
    result = _upgrade(message_transition, runner)
    assert not result["ok"] and "could not determine legacy message channels" in result["error"]
    assert current.resolve() == previous and events == []
    assert not (current.parent / "cache/venvs").exists()


def test_legacy_service_profile_is_activation_evidence_without_credentials(message_transition):
    previous, target, current, events = message_transition
    _legacy(previous)
    runtime = current.parent / "runtime"
    runtime.mkdir()
    (runtime / "service.profile.json").write_text(json.dumps({
        "service_provider": "systemd", "feishu_ws": {"enabled": True},
    }))
    runner = MessageRuntime(previous)
    result = _upgrade(message_transition, runner)
    assert result["ok"], result.get("error", result)
    assert any("./plugins/feishu-channel" in cmd for cmd in runner.message_installs)
    assert not any("./plugins/wechat-channel" in cmd for cmd in runner.message_installs)


@pytest.mark.parametrize("channel,package", [("feishu", "feishu-channel"), ("wechat_clawbot", "wechat-channel")])
def test_legacy_explicit_receive_only_config_preserves_manually_invoked_channel(message_transition, channel, package):
    previous, target, current, events = message_transition
    _legacy(previous)
    _runtime_config(current, {"channels": {channel: {"enabled": True, "receive_enabled": True}},
                              "notifications": {"enabled": False}})
    runner = MessageRuntime(previous)
    result = _upgrade(message_transition, runner)
    assert result["ok"], result.get("error", result)
    assert any(f"./plugins/{package}" in cmd for cmd in runner.message_installs)


@pytest.mark.parametrize("output", ["not json", "null", "[false]", '[false,"false"]', "[false,false,false]"])
def test_invalid_message_probe_blocks_without_compensation_or_state_changes(message_transition, monkeypatch, output):
    previous, target, current, events = message_transition
    path = _runtime_config(current, {"channels": {}})
    before = path.read_bytes()
    service_calls = []
    monkeypatch.setattr(upgrade, "_compensate_service_transition", lambda **_: service_calls.append("compensate"))
    monkeypatch.setattr(upgrade, "_reconcile_services_from_current_release", lambda **_: service_calls.append("reconcile"))
    runner = MessageRuntime(previous, True, True)
    runner.message_probe_output = output
    result = _upgrade(message_transition, runner)
    assert not result["ok"] and not result["symlink_switched"] and not result["changed"]
    assert current.resolve() == previous and path.read_bytes() == before
    assert events == [] and service_calls == [] and result["compensation"] == {}
    assert not (current.parent / "cache/venvs").exists()
    assert not runner.message_installs


@pytest.mark.parametrize("reused", [False, True])
def test_invalid_message_import_blocks_before_configs_services_and_switch(message_transition, monkeypatch, reused):
    previous, target, current, events = message_transition
    path = _runtime_config(current, {"channels": {}})
    before = path.read_bytes()
    runner = MessageRuntime(previous, True, True)
    cached = None
    if reused:
        cached = upgrade._ensure_release_runtime(
            target_dir=target, include_feishu=True, include_wechat=True, cache_root=current.parent / "cache",
            run_cmd=runner, operations=[],
        )
    service_calls = []
    monkeypatch.setattr(upgrade, "_compensate_service_transition", lambda **_: service_calls.append("compensate"))
    monkeypatch.setattr(upgrade, "_reconcile_services_from_current_release", lambda **_: service_calls.append("reconcile"))
    runner.message_validation_fails = True
    result = _upgrade(message_transition, runner)
    assert not result["ok"] and not result["symlink_switched"] and not result["changed"]
    assert current.resolve() == previous and path.read_bytes() == before
    assert events == [] and service_calls == [] and result["compensation"] == {}
    assert Path(result["runtime_prepare"]["shared_venv_path"]).exists() is reused
    if cached:
        assert result["runtime_prepare"]["dependency_hash"] == cached["dependency_hash"]


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("feishu,wechat", [(True, False), (False, True), (True, True)])
def test_bundle_rejects_messages_before_switch_without_silent_loss(message_transition, monkeypatch, feishu, wechat, legacy):
    previous, target, current, events = message_transition
    path = _runtime_config(current, {"notifications": {"enabled": False}})
    if legacy:
        _legacy(previous)
        profile = path.parent / "service.profile.json"
        document = json.loads(profile.read_text())
        document.update(feishu_ws={"enabled": feishu}, wechat_clawbot={"enabled": wechat})
        profile.write_text(json.dumps(document))
    before = path.read_bytes()
    for release in (previous, target):
        _bundled_runtime(release)
    monkeypatch.setattr(upgrade, "_materialize_runtime_bundle", lambda **_: {"method": "fixture"})
    calls = []
    monkeypatch.setattr(runtime_bundle, "validate_runtime", lambda root: calls.append("validate"))
    monkeypatch.setattr(upgrade, "_compensate_service_transition", lambda **_: calls.append("compensate"))
    runner = MessageRuntime(previous, feishu and not legacy, wechat and not legacy)

    def bundled_runner(command, **kwargs):
        if command[0] == str(previous / runtime_bundle.PYTHON) and "om-position-advice-plugin" in command[3]:
            return subprocess.CompletedProcess(command, 0, "[false,false,false]", "")
        return runner(command, **kwargs)

    result = _upgrade(message_transition, bundled_runner)
    assert not result["ok"] and not result["symlink_switched"] and not result["changed"]
    assert "runtime bundle cannot preserve optional packages" in result["error"]
    assert result["runtime_prepare"]["unsupported_optional_packages"] == [
        name for selected, name in ((feishu, "om-feishu-channel-plugin"), (wechat, "om-wechat-channel-plugin")) if selected
    ]
    assert current.resolve() == previous and path.read_bytes() == before
    assert events == [] and calls == [] and result["compensation"] == {}
    assert not runner.message_installs
    runtime_bundle.load_manifest(target, verify_files=True)


@pytest.mark.parametrize("feishu,wechat", [(False, False), (True, False), (False, True), (True, True)])
def test_message_probe_reads_only_current_runtime_metadata(message_transition, feishu, wechat):
    previous, target, current, events = message_transition
    site = current.parent / "probe-site"
    site.mkdir()
    for selected, (package, module) in zip((feishu, wechat), PACKAGES):
        if selected:
            metadata = site / (module + "_plugin-0.1.0.dist-info")
            metadata.mkdir()
            (metadata / "METADATA").write_text(f"Name: om-{package}-plugin\nVersion: 0.1.0\n")

    def runner(command, **kwargs):
        assert command[:3] == [str(previous / ".venv/bin/python"), "-I", "-c"]
        code = (
            "import sys\n"
            "sys.path[:] = [p for p in sys.path if 'site-packages' not in p]\n"
            f"sys.path.insert(0, {str(site)!r})\n" + command[3] +
            "assert 'om_feishu_channel' not in sys.modules and 'om_wechat_channel' not in sys.modules\n"
            "assert 'lark_oapi' not in sys.modules\n"
        )
        return subprocess.run([sys.executable, "-I", "-c", code], **kwargs)

    assert upgrade._message_package_selection(
        previous_dir=previous, runtime_root=current.parent / "runtime", run_cmd=runner, operations=[],
    ) == (feishu, wechat)


@pytest.mark.parametrize("key,package", [("include_feishu", "feishu-channel"), ("include_wechat", "wechat-channel")])
def test_message_source_dependencies_and_selection_are_cache_inputs(message_transition, key, package):
    previous, target, current, events = message_transition
    base = upgrade._dependency_hash(target, include_server=True)
    selected = upgrade._dependency_hash(target, include_server=True, **{key: True})
    assert base != selected
    source = next((target / "plugins" / package / "src").rglob("*.py"))
    source.write_text(source.read_text() + "\n# changed source\n")
    changed = upgrade._dependency_hash(target, include_server=True, **{key: True})
    assert selected != changed
    spec = target / "plugins" / package / "pyproject.toml"
    spec.write_text(spec.read_text().replace('dependencies = [', 'dependencies = ["fixture-dependency>=1", '))
    assert changed != upgrade._dependency_hash(target, include_server=True, **{key: True})
    assert base == upgrade._dependency_hash(target, include_server=True)


@pytest.mark.parametrize("channel,package,module", [
    ("feishu", "feishu-channel", "om_feishu_channel"),
    ("wechat_clawbot", "wechat-channel", "om_wechat_channel"),
])
@pytest.mark.parametrize("expected,installed,version,broken,ok", [
    (False, False, "0.1.0", False, True), (True, True, "0.1.0", False, True),
    (True, False, "0.1.0", False, False), (False, True, "0.1.0", False, False),
    (True, True, "0.0.1", False, False), (True, True, "0.1.0", True, False),
])
def test_actual_message_version_and_import_validation(
    message_transition, channel, package, module, expected, installed, version, broken, ok,
):
    previous, target, current, events = message_transition
    shutil.copyfile(ROOT / "src/application/plugin_runtime.py", target / "src/application/plugin_runtime.py")
    site = current.parent / "isolated-site"
    site.mkdir()
    if installed:
        shutil.copytree(ROOT / "plugins" / package / "src" / module, site / module,
                        ignore=shutil.ignore_patterns("__pycache__"))
        metadata = site / (module + "_plugin-0.1.0.dist-info")
        metadata.mkdir()
        (metadata / "METADATA").write_text(f"Name: om-{package}-plugin\nVersion: {version}\n")
        if broken:
            (site / module / "__init__.py").write_text("raise ImportError('broken package fixture')\n")
    (site / "lark_oapi.py").write_text("# isolated SDK import, no connection\n")

    def runner(command, **kwargs):
        assert command[:3] == [str(target / ".venv/bin/python"), "-I", "-c"]
        code = (
            "import sys\n"
            "sys.path[:] = [p for p in sys.path if 'site-packages' not in p]\n"
            f"sys.path.insert(0, {str(site)!r})\n" + command[3]
        )
        return subprocess.run([sys.executable, "-I", "-c", code, *command[4:]], **kwargs)

    options = {"include_feishu": channel == "feishu" and expected,
               "include_wechat": channel == "wechat_clawbot" and expected}
    if ok:
        upgrade._validate_message_runtime(target_dir=target, run_cmd=runner, operations=[], **options)
    else:
        with pytest.raises(RuntimeError):
            upgrade._validate_message_runtime(target_dir=target, run_cmd=runner, operations=[], **options)
