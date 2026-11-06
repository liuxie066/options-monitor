"""PM package preservation through the real upgrade boundary, without installers/services."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from src.application import service_upgrade as upgrade
from src.application import runtime_bundle
from tests.service_deploy_test_support import (
    _create_fake_venv_python_at,
    _write_runtime_target_with_server_deps,
)


def _package_source(target: Path) -> None:
    package = target / "plugins/portfolio-management"
    (package / "src/om_pm_plugin").mkdir(parents=True)
    (package / "pyproject.toml").write_text('[project]\nname="om-pm-plugin"\nversion="0.1.0"\n')
    (package / "src/om_pm_plugin/__init__.py").write_text("# package fixture\n")


class OfflineRuntime:
    def __init__(self, previous: Path, installed: bool, longbridge: bool = False):
        self.previous = previous
        self.installed = installed
        self.commands = []
        self.pm_installs = []
        self.lb_installs = []
        self.pm_validation_fails = False
        self.lb_validation_fails = False
        self.probe_output = json.dumps([installed, longbridge, False])
        self.uv_fails = False

    def __call__(self, command, **kwargs):
        self.commands.append(list(command))
        stdout, status = "ok\n", 0
        if command[0] == str(self.previous / ".venv/bin/python"):
            stdout = self.probe_output
        elif command[:3] == [sys.executable, "-m", "venv"] or command[:2] == ["uv", "venv"]:
            _create_fake_venv_python_at(Path(command[-1]))
        elif "install" in command:
            if "./plugins/portfolio-management" in command:
                self.pm_installs.append(list(command))
            if "requirements/longbridge.txt" in command:
                self.lb_installs.append(list(command))
            if command[0] == "uv" and self.uv_fails:
                status = 1
            if status == 0 and "--target" in command:
                import tomllib
                source = next(part for part in command if part.startswith("./plugins/"))
                project = Path(kwargs["cwd"]) / source
                spec = tomllib.loads((project / "pyproject.toml").read_text())["project"]
                site = Path(command[command.index("--target") + 1])
                shutil.copytree(project / "src", site)
                name = spec.get("name", "om-" + project.name)
                metadata = site / (name.replace("-", "_") + ".dist-info")
                metadata.mkdir()
                (metadata / "METADATA").write_text(f"Name: {name}\nVersion: {spec['version']}\n")
        elif command[1:3] == ["-m", "ensurepip"]:
            pass
        elif command == ["sh", "-lc", "command -v uv"]:
            stdout = "/offline/uv\n"
        elif "-c" in command:
            if "load_portfolio_management_client_class" in command[command.index("-c") + 1]:
                status = int(self.pm_validation_fails or self.lb_validation_fails)
        elif command == ["./om-agent", "spec"] or command[1:2] == ["scripts/release_check.py"]:
            pass
        else:
            pytest.fail(f"Unexpected external command: {command}")
        return subprocess.CompletedProcess(command, status, stdout=stdout, stderr="offline failure" if status else "")


@pytest.fixture
def transition(monkeypatch, tmp_path):
    previous, target = tmp_path / "releases/1.0.0", tmp_path / "releases/1.0.1"
    for release in (previous, target):
        _write_runtime_target_with_server_deps(release)
    (previous / "VERSION").write_text("1.0.0\n")
    _create_fake_venv_python_at(previous / ".venv")
    _package_source(previous)
    _package_source(target)
    (target / "requirements/longbridge.txt").write_text("longbridge==5.3.0\n")
    current = tmp_path / "current"
    current.symlink_to(previous, target_is_directory=True)
    monkeypatch.setenv("OM_UPGRADE_INSTALLER", "pip")
    monkeypatch.setattr(upgrade, "service_upgrade_check", lambda **_: {
        "ok": True, "latest_version": "1.0.1", "release_tag": "v1.0.1",
    })
    monkeypatch.setattr(upgrade, "_materialize_release_from_git_cache", lambda **_: {"method": "fixture"})
    events = []
    monkeypatch.setattr(upgrade, "_prepare_runtime_configs_for_release", lambda **_: events.append("configs") or {})
    monkeypatch.setattr(upgrade, "_commit_prepared_runtime_configs", lambda **_: {})
    monkeypatch.setattr(upgrade, "_validate_committed_runtime_configs", lambda **_: [])
    monkeypatch.setattr(upgrade, "_reconcile_services_from_current_release", lambda **_: {"summary": {"ok": True}})
    return previous, target, current, events


def _upgrade(transition, runner):
    previous, target, current, events = transition
    return upgrade.service_upgrade(
        repo_root=current, runtime_root=current.parent / "runtime", releases_root=target.parent,
        cache_root=current.parent / "cache", target_version="1.0.1", confirm=True,
        restart_services=False, run_cmd=runner,
    )


def _bundled_runtime(release: Path) -> None:
    python = release / runtime_bundle.PYTHON
    python.parent.mkdir(parents=True)
    python.write_text("#!/bin/sh\nexit 0\n")
    python.chmod(0o755)
    for name in ("om", "om-agent"):
        (release / name).write_text("#!/bin/sh\nexit 0\n")
    version = (release / "VERSION").read_text().strip()
    platform_name = runtime_bundle.current_platform()
    manifest = {
        "schema_version": 1, "kind": "options-monitor-runtime", "version": version,
        "tag": f"v{version}", "commit": "a" * 40, "repository": runtime_bundle.REPOSITORY,
        "platform": platform_name, "minimum_system": runtime_bundle.MINIMUM_SYSTEMS[platform_name],
        "python": {
            "executable": runtime_bundle.PYTHON, "version": "3.12.12", "sha256": "b" * 64,
            "source_url": "https://github.com/astral-sh/python-build-standalone/releases/download/test/python.tar.gz",
        },
        "dependencies": {"fixture": "1.0.0"},
        "files": {path.relative_to(release).as_posix(): runtime_bundle.sha256(path)
                  for path in release.rglob("*") if path.is_file()},
    }
    (release / runtime_bundle.MANIFEST).write_text(json.dumps(manifest))


@pytest.mark.parametrize("advice", [False, True])
@pytest.mark.parametrize("pm,lb", [(False, False), (True, False), (False, True), (True, True)])
def test_bundle_optional_selection_blocks_before_switch(monkeypatch, transition, pm, lb, advice):
    previous, target, current, events = transition
    for release in (previous, target):
        _bundled_runtime(release)
    monkeypatch.setattr(upgrade, "_materialize_runtime_bundle", lambda **_: {"method": "fixture"})
    validations = []
    monkeypatch.setattr(runtime_bundle, "validate_runtime", lambda root: validations.append(root) or {"ok": True})
    runner = OfflineRuntime(previous, installed=pm, longbridge=lb)
    probes = []

    def bundled_runner(command, **kwargs):
        if command[:3] == [str(previous / runtime_bundle.PYTHON), "-I", "-c"]:
            probes.append(command)
            return subprocess.CompletedProcess(command, 0, json.dumps([pm, lb, advice]), "")
        return runner(command, **kwargs)

    result = _upgrade(transition, bundled_runner)

    assert len(probes) == 1, json.dumps(result, indent=2)
    assert not runner.pm_installs and not runner.lb_installs
    if pm or lb or advice:
        assert not result["ok"], result
        assert "RuntimePrepareError" in result["error"]
        assert "runtime bundle cannot preserve optional packages" in result["error"]
        for selected, name in ((pm, "om-pm-plugin"), (lb, "longbridge"), (advice, "om-position-advice-plugin")):
            assert (name in result["error"]) is selected
        assert result["runtime_prepare"]["installer"] == "runtime_bundle"
        assert not result["symlink_switched"] and not result["changed"]
        assert current.resolve() == previous
        assert events == []
        assert validations == []
        receipt = upgrade.load_upgrade_status(runtime_root=current.parent / "runtime")
        assert receipt["ok"] is False and receipt["symlink_switched"] is False
    else:
        assert result["ok"], result
        assert current.resolve() == target
        assert validations == [target]
        assert result["runtime_prepare"]["installer"] == "runtime_bundle"
    runtime_bundle.load_manifest(target, verify_files=True)
    assert not any("install" in command for command in runner.commands)


@pytest.mark.parametrize("enabled", [False, True])
def test_upgrade_preserves_installed_pm_independently_of_enabled(transition, enabled):
    previous, target, current, events = transition
    (previous / "config.us.json").write_text(json.dumps({"portfolio_management": {"enabled": enabled}}))
    runner = OfflineRuntime(previous, installed=True)

    result = _upgrade(transition, runner)

    assert result["ok"], result
    assert runner.pm_installs, "upgrade dropped the already installed PM extension"
    assert current.resolve() == target


@pytest.mark.parametrize("enabled", [False, True])
def test_upgrade_does_not_install_absent_pm_even_when_enabled(transition, enabled):
    previous, target, current, events = transition
    (previous / "config.us.json").write_text(json.dumps({"portfolio_management": {"enabled": enabled}}))
    runner = OfflineRuntime(previous, installed=False)

    result = _upgrade(transition, runner)

    assert result["ok"], result
    assert not runner.pm_installs
    assert not result["runtime_prepare"]["dependency_context"].get("include_pm", False)


@pytest.mark.parametrize("mode,uv_fails", [("pip", False), ("uv", False), ("auto", True)])
def test_installed_pm_reaches_each_installer_and_fallback(monkeypatch, transition, mode, uv_fails):
    monkeypatch.setenv("OM_UPGRADE_INSTALLER", mode)
    previous, target, current, events = transition
    runner = OfflineRuntime(previous, installed=True)
    runner.uv_fails = uv_fails

    result = _upgrade(transition, runner)

    assert result["ok"], result
    assert runner.pm_installs
    assert all("./plugins/portfolio-management" in command for command in runner.pm_installs)
    assert result["runtime_prepare"]["installer"] == ("uv" if mode == "uv" else "pip")
    assert result["runtime_prepare"]["fallback"] is uv_fails


def test_explicit_uninstall_does_not_reuse_pm_environment(transition):
    previous, target, current, events = transition
    runner = OfflineRuntime(previous, installed=False)
    cached = upgrade._ensure_release_runtime(
        target_dir=target, include_pm=True, cache_root=current.parent / "cache", run_cmd=runner, operations=[],
    )
    runner.pm_installs.clear()

    result = _upgrade(transition, runner)

    assert result["ok"], result
    assert not runner.pm_installs
    assert result["runtime_prepare"]["dependency_hash"] != cached["dependency_hash"]
    assert Path(cached["shared_venv_path"]).is_dir()


def test_pm_source_and_selection_are_dependency_cache_inputs(transition):
    previous, target, current, events = transition
    base = upgrade._dependency_hash(target, include_server=True)
    installed = upgrade._dependency_hash(target, include_server=True, include_pm=True)
    assert installed != base
    package = target / "plugins/portfolio-management"
    source = package / "src/om_pm_plugin/__init__.py"
    source.write_text("# changed package\n")
    changed = upgrade._dependency_hash(target, include_server=True, include_pm=True)
    assert changed == installed
    assert upgrade._dependency_hash(target, include_server=True) == base
    metadata = package / "pyproject.toml"
    metadata.write_text(metadata.read_text().replace("0.1.0", "0.2.0"))
    assert upgrade._dependency_hash(target, include_server=True, include_pm=True) != changed


@pytest.mark.parametrize("reused", [False, True])
def test_pm_import_failure_blocks_config_and_current_switch(transition, reused):
    previous, target, current, events = transition
    runner = OfflineRuntime(previous, installed=True)
    cached = None
    if reused:
        cached = upgrade._ensure_release_runtime(
            target_dir=target, include_pm=True, cache_root=current.parent / "cache", run_cmd=runner, operations=[],
        )
    runner.pm_validation_fails = True

    result = _upgrade(transition, runner)

    assert not result["ok"]
    assert result["status"] == "failed"
    assert "RuntimePrepareError" in result["error"]
    assert not result["symlink_switched"]
    assert current.resolve() == previous
    assert events == []
    if reused:
        assert Path(cached["shared_venv_path"]).is_dir(), "failure must preserve the reused environment"
    else:
        assert not Path(result["runtime_prepare"]["shared_venv_path"]).exists()


@pytest.mark.parametrize("output", ["not json", "null", '"false"'])
def test_unreadable_current_installation_is_not_treated_as_uninstalled(transition, output):
    previous, target, current, events = transition
    runner = OfflineRuntime(previous, installed=True)
    runner.probe_output = output

    result = _upgrade(transition, runner)

    assert not result["ok"]
    assert current.resolve() == previous
    assert events == []
    assert not runner.pm_installs
    assert not (current.parent / "cache/venvs").exists()


@pytest.mark.parametrize("expected,installed,version,broken,ok", [
    (False, False, "0.1.0", False, True),
    (True, True, "0.1.0", False, True),
    (True, False, "0.1.0", False, False),
    (False, True, "0.1.0", False, False),
    (True, True, "0.0.1", False, False),
    (True, True, "0.1.0", True, False),
])
def test_validation_executes_installed_package_against_target_core(
    transition, expected, installed, version, broken, ok,
):
    previous, target, current, events = transition
    root = Path(__file__).resolve().parents[1]
    for name in ("portfolio_management.py", "portfolio_management_contract.py", "plugin_runtime.py"):
        shutil.copyfile(root / "src/application" / name, target / "src/application" / name)
    site = current.parent / "isolated-site"
    site.mkdir()
    if installed:
        shutil.copytree(root / "plugins/portfolio-management/src/om_pm_plugin", site / "om_pm_plugin")
        metadata = site / "om_pm_plugin-0.1.0.dist-info"
        metadata.mkdir()
        (metadata / "METADATA").write_text(f"Name: om-pm-plugin\nVersion: {version}\n")
        if broken:
            (site / "om_pm_plugin/__init__.py").write_text("raise ImportError('broken fixture')\n")

    def run_fixture_python(command, **kwargs):
        assert command[:3] == [str(target / ".venv/bin/python"), "-I", "-c"]
        # Execute the actual check with fixture distributions, never the host's packages.
        isolate = (
            "import sys\n"
            "sys.path[:] = [p for p in sys.path if 'site-packages' not in p]\n"
            f"sys.path.insert(0, {str(site)!r})\n"
        )
        return subprocess.run([sys.executable, "-I", "-c", isolate + command[3], *command[4:]], **kwargs)

    operations = []
    if ok:
        upgrade._validate_optional_runtime(
            target_dir=target, include_pm=expected, include_longbridge=False,
            run_cmd=run_fixture_python, operations=operations,
        )
    else:
        with pytest.raises(RuntimeError):
            upgrade._validate_optional_runtime(
                target_dir=target, include_pm=expected, include_longbridge=False,
                run_cmd=run_fixture_python, operations=operations,
            )
    assert operations[-1]["ok"] is ok


@pytest.mark.parametrize("enabled,alias", [(True, False), (True, True), (False, False)])
def test_legacy_builtin_pm_migrates_from_effective_profile_config(transition, enabled, alias):
    previous, target, current, events = transition
    (previous / "plugins/portfolio-management/pyproject.toml").unlink()
    client = previous / "src/infrastructure/portfolio_management_client.py"
    client.parent.mkdir()
    client.write_text("# old built-in transport\n")
    runtime = current.parent / "runtime"
    runtime.mkdir()
    config = runtime / "selected.json"
    document = ({"trade_intake": {"holdings_sync": {"enabled": enabled}}} if alias else
                {"portfolio_management": {"enabled": enabled}})
    config.write_text(json.dumps(document))
    (runtime / "service.profile.json").write_text(json.dumps({
        "service_provider": "systemd", "markets": ["us"], "config_paths": {"us": str(config)},
    }))
    # A repo-local decoy must not override the effective service profile.
    (previous / "config.us.json").write_text(json.dumps({"portfolio_management": {"enabled": not enabled}}))
    runner = OfflineRuntime(previous, installed=False)

    result = _upgrade(transition, runner)

    assert result["ok"], result
    assert bool(runner.pm_installs) is enabled


def test_plugin_release_enabled_config_does_not_undo_explicit_uninstall(transition):
    previous, target, current, events = transition
    runtime = current.parent / "runtime"
    runtime.mkdir()
    (runtime / "config.us.json").write_text('{"portfolio_management":{"enabled":true}}')
    client = previous / "src/infrastructure/portfolio_management_client.py"
    client.parent.mkdir()
    client.write_text("# new compatibility constructor, not built-in transport\n")
    runner = OfflineRuntime(previous, installed=False)

    result = _upgrade(transition, runner)

    assert result["ok"], result
    assert not runner.pm_installs


def test_legacy_builtin_pm_uses_runtime_root_without_profile(transition):
    previous, target, current, events = transition
    (previous / "plugins/portfolio-management/pyproject.toml").unlink()
    client = previous / "src/infrastructure/portfolio_management_client.py"
    client.parent.mkdir()
    client.write_text("# old built-in transport\n")
    runtime = current.parent / "runtime"
    runtime.mkdir()
    (runtime / "config.hk.json").write_text('{"portfolio_management":{"enabled":true}}')
    runner = OfflineRuntime(previous, installed=False)

    result = _upgrade(transition, runner)

    assert result["ok"], result
    assert runner.pm_installs


@pytest.mark.parametrize("contents", [None, "not json", '[]', '{"portfolio_management":{"enabled":"false"}}'])
def test_legacy_pm_unknown_config_blocks_before_runtime_preparation(transition, contents):
    previous, target, current, events = transition
    (previous / "plugins/portfolio-management/pyproject.toml").unlink()
    client = previous / "src/infrastructure/portfolio_management_client.py"
    client.parent.mkdir()
    client.write_text("# old built-in transport\n")
    runtime = current.parent / "runtime"
    runtime.mkdir()
    config = runtime / "selected.json"
    if contents is not None:
        config.write_text(contents)
    (runtime / "service.profile.json").write_text(json.dumps({"config_paths": {"us": str(config)}}))
    runner = OfflineRuntime(previous, installed=False)

    result = _upgrade(transition, runner)

    assert not result["ok"]
    assert "could not determine legacy PM integration" in result["error"]
    assert current.resolve() == previous
    assert events == []
    assert not (current.parent / "cache/venvs").exists()


@pytest.mark.parametrize("installed", [False, True])
@pytest.mark.parametrize("mode", ["pip", "uv", "auto"])
def test_longbridge_installation_is_preserved_without_enabling_or_loading_credentials(
    monkeypatch, transition, installed, mode,
):
    monkeypatch.setenv("OM_UPGRADE_INSTALLER", mode)
    previous, target, current, events = transition
    runner = OfflineRuntime(previous, installed=False, longbridge=installed)
    runner.uv_fails = mode == "auto"

    result = _upgrade(transition, runner)

    assert result["ok"], result
    assert bool(runner.lb_installs) is installed
    assert not runner.pm_installs
    assert result["runtime_prepare"]["dependency_context"].get("include_longbridge", False) is installed


def test_longbridge_selection_and_requirement_pin_are_cache_inputs(transition):
    previous, target, current, events = transition
    base = upgrade._dependency_hash(target, include_server=True)
    selected = upgrade._dependency_hash(target, include_server=True, include_longbridge=True)
    assert selected != base
    (target / "requirements/longbridge.txt").write_text("longbridge==5.4.0\n")
    assert upgrade._dependency_hash(target, include_server=True, include_longbridge=True) != selected
    assert upgrade._dependency_hash(target, include_server=True) == base


def test_longbridge_explicit_removal_does_not_reuse_installed_sdk_cache(transition):
    previous, target, current, events = transition
    runner = OfflineRuntime(previous, installed=False)
    cached = upgrade._ensure_release_runtime(
        target_dir=target, include_longbridge=True,
        cache_root=current.parent / "cache", run_cmd=runner, operations=[],
    )
    runner.lb_installs.clear()

    result = _upgrade(transition, runner)

    assert result["ok"], result
    assert not runner.lb_installs
    assert result["runtime_prepare"]["dependency_hash"] != cached["dependency_hash"]
    assert Path(cached["shared_venv_path"]).is_dir()


def test_longbridge_import_failure_preserves_current(transition):
    previous, target, current, events = transition
    runner = OfflineRuntime(previous, installed=False, longbridge=True)
    runner.lb_validation_fails = True

    result = _upgrade(transition, runner)

    assert not result["ok"]
    assert current.resolve() == previous
    assert not result["symlink_switched"]
    assert events == []


def test_unchanged_selected_packages_reuse_cache_and_still_validate(transition):
    previous, target, current, events = transition
    runner = OfflineRuntime(previous, installed=True, longbridge=True)
    first = upgrade._ensure_release_runtime(
        target_dir=target, include_pm=True, include_longbridge=True,
        cache_root=current.parent / "cache", run_cmd=runner, operations=[],
    )
    runner.commands.clear()
    runner.pm_installs.clear()
    runner.lb_installs.clear()

    result = _upgrade(transition, runner)

    assert result["ok"], result
    assert result["runtime_prepare"]["venv_reused"]
    assert result["runtime_prepare"]["dependency_hash"] == first["dependency_hash"]
    assert not runner.pm_installs and not runner.lb_installs
    assert any("load_portfolio_management_client_class" in " ".join(command) for command in runner.commands)


@pytest.mark.parametrize("advice", [False, True])
@pytest.mark.parametrize("pm,lb", [(False, False), (True, False), (False, True), (True, True)])
def test_current_runtime_probe_reads_metadata_without_importing_packages(transition, pm, lb, advice):
    previous, target, current, events = transition
    site = current.parent / "probe-site"
    site.mkdir()
    for name, installed in (("om-pm-plugin", pm), ("longbridge", lb), ("om-position-advice-plugin", advice)):
        if installed:
            metadata = site / (name.replace("-", "_") + "-0.1.0.dist-info")
            metadata.mkdir()
            (metadata / "METADATA").write_text(f"Name: {name}\nVersion: 0.1.0\n")

    def run_fixture_python(command, **kwargs):
        assert command[:3] == [str(previous / ".venv/bin/python"), "-I", "-c"]
        code = (
            "import sys\n"
            "sys.path[:] = [p for p in sys.path if 'site-packages' not in p]\n"
            f"sys.path.insert(0, {str(site)!r})\n" + command[3] +
            "assert 'om_pm_plugin' not in sys.modules and 'longbridge' not in sys.modules\n"
            "assert 'om_position_advice_plugin' not in sys.modules\n"
        )
        return subprocess.run([sys.executable, "-I", "-c", code], **kwargs)

    assert upgrade._installed_optional_packages(previous_dir=previous, run_cmd=run_fixture_python, operations=[]) == (pm, lb, advice)


@pytest.mark.parametrize("expected,installed,broken,ok", [
    (True, True, False, True), (False, False, False, True),
    (True, False, False, False), (False, True, False, False), (True, True, True, False),
])
def test_longbridge_pre_switch_check_uses_only_offline_imports(transition, expected, installed, broken, ok):
    previous, target, current, events = transition
    site = current.parent / "isolated-site"
    site.mkdir()
    if installed:
        package = site / "longbridge"
        package.mkdir()
        (package / "__init__.py").write_text("")
        (package / "openapi.py").write_text(
            "raise ImportError('broken SDK fixture')\n" if broken else
            "class Config:\n    def __init__(self): raise AssertionError('no credential construction')\n"
            "class TradeContext:\n    def __init__(self): raise AssertionError('no API calls')\n"
        )
        metadata = site / "longbridge-5.3.0.dist-info"
        metadata.mkdir()
        (metadata / "METADATA").write_text("Name: longbridge\nVersion: 5.3.0\n")

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
            target_dir=target, include_pm=False, include_longbridge=expected,
            run_cmd=run_fixture_python, operations=operations,
        )
    else:
        with pytest.raises(RuntimeError):
            upgrade._validate_optional_runtime(
                target_dir=target, include_pm=False, include_longbridge=expected,
                run_cmd=run_fixture_python, operations=operations,
            )
    assert operations[-1]["ok"] is ok
