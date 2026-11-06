"""Advice selection through the controlled upgrade boundary, with offline effects."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from src.application import service_upgrade as upgrade
from tests.test_pm_package_upgrade import OfflineRuntime, _upgrade, transition


ROOT = Path(__file__).resolve().parents[1]


class AdviceRuntime(OfflineRuntime):
    def __init__(self, previous: Path, installed: bool):
        super().__init__(previous, installed=False)
        self.probe_output = json.dumps([False, False, installed])
        self.advice_installs = []
        self.advice_validation_fails = False

    def __call__(self, command, **kwargs):
        if "install" in command and "./plugins/position-advice" in command:
            self.advice_installs.append(list(command))
        if "-c" in command and "require_position_advice_plugin" in command[command.index("-c") + 1]:
            self.pm_validation_fails = self.advice_validation_fails
        return super().__call__(command, **kwargs)


@pytest.fixture
def advice_transition(transition):
    previous, target, current, events = transition
    for release in (previous, target):
        shutil.copytree(ROOT / "plugins/position-advice", release / "plugins/position-advice",
                        ignore=shutil.ignore_patterns("build", "*.egg-info", "__pycache__"))
        owner = release / "domain/domain"
        owner.mkdir(parents=True)
        (owner / "close_advice.py").write_text("# built-in / compatibility facade fixture\n")
    return transition


@pytest.mark.parametrize("mode,uv_fails", [("pip", False), ("uv", False), ("auto", True)])
@pytest.mark.parametrize("installed", [False, True])
def test_advice_installation_selection_reaches_all_installers(monkeypatch, advice_transition, mode, uv_fails, installed):
    monkeypatch.setenv("OM_UPGRADE_INSTALLER", mode)
    previous, target, current, events = advice_transition
    runner = AdviceRuntime(previous, installed=installed)
    runner.uv_fails = uv_fails
    result = _upgrade(advice_transition, runner)
    assert result["ok"], result
    assert bool(runner.advice_installs) is installed
    assert result["runtime_prepare"]["dependency_context"].get("include_position_advice", False) is installed
    assert current.resolve() == target
    assert not runner.pm_installs and not runner.lb_installs


@pytest.mark.parametrize("enabled", [False, True])
def test_plugin_aware_release_respects_deliberate_removal(advice_transition, enabled):
    previous, target, current, events = advice_transition
    runtime = current.parent / "runtime"
    runtime.mkdir()
    (runtime / "config.us.json").write_text(json.dumps({"close_advice": {"enabled": enabled}}))
    runner = AdviceRuntime(previous, installed=False)
    cached = upgrade._ensure_release_runtime(
        target_dir=target, include_position_advice=True, cache_root=current.parent / "cache",
        run_cmd=runner, operations=[],
    )
    runner.advice_installs.clear()
    result = _upgrade(advice_transition, runner)
    assert result["ok"], result
    assert not runner.advice_installs
    assert result["runtime_prepare"]["dependency_hash"] != cached["dependency_hash"]
    assert Path(cached["shared_venv_path"]).is_dir()


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("profile", [False, True])
def test_legacy_builtin_migration_uses_effective_enablement(advice_transition, enabled, profile):
    previous, target, current, events = advice_transition
    (previous / "plugins/position-advice/pyproject.toml").unlink()
    runtime = current.parent / "runtime"
    runtime.mkdir()
    config = runtime / ("selected.json" if profile else "config.hk.json")
    config.write_text(json.dumps({"close_advice": {"enabled": enabled}}))
    if profile:
        (runtime / "service.profile.json").write_text(json.dumps({"service_provider": "systemd", "config_paths": {"us": str(config)}}))
    (previous / "config.us.json").write_text(json.dumps({"close_advice": {"enabled": not enabled}}))
    runner = AdviceRuntime(previous, installed=False)
    result = _upgrade(advice_transition, runner)
    assert result["ok"], result
    assert bool(runner.advice_installs) is enabled
    assert json.loads(config.read_text()) == {"close_advice": {"enabled": enabled}}


@pytest.mark.parametrize("contents", [None, "not json", "[]", '{"close_advice":{"enabled":"false"}}'])
def test_unknown_legacy_config_blocks_before_preparation(advice_transition, contents):
    previous, target, current, events = advice_transition
    (previous / "plugins/position-advice/pyproject.toml").unlink()
    runtime = current.parent / "runtime"
    runtime.mkdir()
    config = runtime / "selected.json"
    if contents is not None:
        config.write_text(contents)
    (runtime / "service.profile.json").write_text(json.dumps({"service_provider": "systemd", "config_paths": {"us": str(config)}}))
    runner = AdviceRuntime(previous, installed=False)
    result = _upgrade(advice_transition, runner)
    assert not result["ok"]
    assert "could not determine legacy Position Advice integration" in result["error"]
    assert current.resolve() == previous and events == []
    assert not (current.parent / "cache/venvs").exists()


def test_legacy_without_any_effective_config_blocks_before_preparation(advice_transition):
    previous, target, current, events = advice_transition
    (previous / "plugins/position-advice/pyproject.toml").unlink()
    # A release-local config is not evidence for the running instance.
    (previous / "config.us.json").write_text('{"close_advice":{"enabled":true}}')
    runner = AdviceRuntime(previous, installed=False)
    result = _upgrade(advice_transition, runner)
    assert not result["ok"]
    assert "could not determine legacy Position Advice integration" in result["error"]
    assert current.resolve() == previous and events == []
    assert not runner.advice_installs
    assert not (current.parent / "cache/venvs").exists()


def test_source_metadata_and_selection_are_cache_inputs(advice_transition):
    previous, target, current, events = advice_transition
    base = upgrade._dependency_hash(target, include_server=True)
    selected = upgrade._dependency_hash(target, include_server=True, include_position_advice=True)
    assert base != selected
    source = target / "plugins/position-advice/src/om_position_advice_plugin/presentation.py"
    source.write_text(source.read_text() + "\n# source change\n")
    changed = upgrade._dependency_hash(target, include_server=True, include_position_advice=True)
    assert changed == selected
    assert upgrade._dependency_hash(target, include_server=True) == base
    metadata = target / "plugins/position-advice/pyproject.toml"
    metadata.write_text(metadata.read_text().replace("0.1.0", "0.2.0"))
    assert upgrade._dependency_hash(target, include_server=True, include_position_advice=True) != changed


@pytest.mark.parametrize("reused", [False, True])
def test_import_failure_blocks_current_and_config_switch(advice_transition, reused):
    previous, target, current, events = advice_transition
    runner = AdviceRuntime(previous, installed=True)
    cached = None
    if reused:
        cached = upgrade._ensure_release_runtime(
            target_dir=target, include_position_advice=True, cache_root=current.parent / "cache",
            run_cmd=runner, operations=[],
        )
    runner.advice_validation_fails = True
    result = _upgrade(advice_transition, runner)
    assert not result["ok"] and not result["symlink_switched"]
    assert current.resolve() == previous and events == []
    if reused:
        assert Path(cached["shared_venv_path"]).is_dir()
    else:
        assert not Path(result["runtime_prepare"]["shared_venv_path"]).exists()


@pytest.mark.parametrize("expected,installed,version,broken,ok", [
    (False, False, "0.1.0", False, True), (True, True, "0.1.0", False, True),
    (True, False, "0.1.0", False, False), (False, True, "0.1.0", False, False),
    (True, True, "0.0.1", False, False), (True, True, "0.1.0", True, False),
])
def test_pre_switch_validation_executes_actual_import_and_version_check(
    advice_transition, expected, installed, version, broken, ok,
):
    previous, target, current, events = advice_transition
    shutil.copytree(ROOT / "domain", target / "domain", dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__"))
    for name in ("position_advice_plugin.py", "plugin_runtime.py"):
        shutil.copyfile(ROOT / "src/application" / name, target / "src/application" / name)
    site = current.parent / "isolated-site"
    site.mkdir()
    if installed:
        shutil.copytree(ROOT / "plugins/position-advice/src/om_position_advice_plugin",
                        site / "om_position_advice_plugin", ignore=shutil.ignore_patterns("__pycache__"))
        metadata = site / "om_position_advice_plugin-0.1.0.dist-info"
        metadata.mkdir()
        (metadata / "METADATA").write_text(f"Name: om-position-advice-plugin\nVersion: {version}\n")
        if broken:
            (site / "om_position_advice_plugin/__init__.py").write_text("raise ImportError('broken fixture')\n")

    def run_fixture_python(command, **kwargs):
        assert command[:3] == [str(target / ".venv/bin/python"), "-I", "-c"]
        code = (
            "import sys\n"
            "sys.path[:] = [p for p in sys.path if 'site-packages' not in p]\n"
            f"sys.path.insert(0, {str(site)!r})\n" + command[3]
        )
        return subprocess.run([sys.executable, "-I", "-c", code, *command[4:]], **kwargs)

    operations = []
    if ok:
        upgrade._validate_optional_runtime(
            target_dir=target, include_pm=False, include_longbridge=False, include_position_advice=expected,
            run_cmd=run_fixture_python, operations=operations,
        )
    else:
        with pytest.raises(RuntimeError):
            upgrade._validate_optional_runtime(
                target_dir=target, include_pm=False, include_longbridge=False, include_position_advice=expected,
                run_cmd=run_fixture_python, operations=operations,
            )
    assert operations[-1]["ok"] is ok


@pytest.mark.parametrize("installed", [False, True])
def test_probe_uses_metadata_without_loading_advice(advice_transition, installed):
    previous, target, current, events = advice_transition
    site = current.parent / "probe-site"
    site.mkdir()
    if installed:
        metadata = site / "om_position_advice_plugin-0.1.0.dist-info"
        metadata.mkdir()
        (metadata / "METADATA").write_text("Name: om-position-advice-plugin\nVersion: 0.1.0\n")

    def run_fixture_python(command, **kwargs):
        code = (
            "import sys\n"
            "sys.path[:] = [p for p in sys.path if 'site-packages' not in p]\n"
            f"sys.path.insert(0, {str(site)!r})\n" + command[3] +
            "assert 'om_position_advice_plugin' not in sys.modules\n"
        )
        return subprocess.run([sys.executable, "-I", "-c", code], **kwargs)

    assert upgrade._installed_optional_packages(
        previous_dir=previous, run_cmd=run_fixture_python, operations=[],
    ) == (False, False, installed)


@pytest.mark.parametrize("output", ["[false, false]", '[false, false, "false"]', "[false, false, false, false]"])
def test_incomplete_or_invalid_selection_probe_never_silently_drops_package(advice_transition, output):
    previous, target, current, events = advice_transition
    runner = AdviceRuntime(previous, installed=True)
    runner.probe_output = output
    result = _upgrade(advice_transition, runner)
    assert not result["ok"]
    assert "did not return three booleans" in result["error"]
    assert current.resolve() == previous and events == []
    assert not runner.advice_installs
