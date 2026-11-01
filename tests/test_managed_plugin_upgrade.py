"""Managed wheels survive the real upgrade facade; all transport/services are offline."""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import shutil
import shlex
import subprocess
import sys
from types import SimpleNamespace
import venv

import pytest

from src.application import plugin_distribution as distribution, runtime_bundle, service_upgrade as upgrade
from src.application.plugin_runtime import PLUGINS, plugin_store, read_managed_plugin
from tests.plugin_release_helpers import wheel_files, write_wheel
from tests.test_pm_package_upgrade import OfflineRuntime, _bundled_runtime, transition


ROOT = Path(__file__).resolve().parents[1]


def _publication(version: str, *, broken: str | None = None):
    """Real, dependency-free fixture wheels with verified METADATA/RECORD/digests."""
    catalog = {"schema_version": 1, "repository": runtime_bundle.REPOSITORY,
               "om_version": version, "tag": f"v{version}", "commit": "a" * 40, "plugins": []}
    assets = {}
    for plugin_id, item in PLUGINS.items():
        module, name = item["module"], item["distribution"].replace("-", "_")
        source = ROOT / "plugins" / item["source"] / "src"
        files = wheel_files(item["distribution"], module, version)
        files.update({p.relative_to(source).as_posix(): p.read_bytes() for p in (source / module).rglob("*.py")})
        if plugin_id == broken:
            files[f"{module}/__init__.py"] = b"raise ImportError('broken target wheel')\n"
        buffer = io.BytesIO()
        write_wheel(buffer, files)
        asset = f"{name}-{version}-py3-none-any.whl"
        assets[asset] = buffer.getvalue()
        catalog["plugins"].append({"id": plugin_id, "distribution": item["distribution"], "module": module,
                                   "version": version, "requires_python": ">=3.12", "asset": asset,
                                   "sha256": hashlib.sha256(assets[asset]).hexdigest(), "size": len(assets[asset])})
    assets["om-plugins.json"] = json.dumps(catalog).encode()
    base = f"https://github.com/{runtime_bundle.REPOSITORY}/releases/download/v{version}/"
    release = {"tag_name": f"v{version}", "draft": False, "prerelease": False,
               "assets": [{"name": name, "state": "uploaded", "size": len(data),
                           "digest": "sha256:" + hashlib.sha256(data).hexdigest(),
                           "browser_download_url": base + name} for name, data in assets.items()]}
    return release, {base + name: data for name, data in assets.items()}


def _release_code(release: Path):
    paths = ["src/__init__.py", "src/application/__init__.py", "src/application/plugin_runtime.py",
             "src/application/portfolio_management.py", "src/application/portfolio_management_contract.py",
             "domain/__init__.py", "domain/domain/__init__.py"]
    paths += [f"domain/domain/{name}.py" for name in (
        "close_advice", "option_position_identity", "expiration_dates", "symbol_identity", "trade_contract_identity", "money",
    )]
    for name in paths:
        destination = release / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, destination)
    for item in PLUGINS.values():
        destination = release / "plugins" / item["source"] / "pyproject.toml"
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / "plugins" / item["source"] / "pyproject.toml", destination)


def _inventory(path: Path):
    return {p.relative_to(path).as_posix(): p.read_bytes()
            for p in path.rglob("*") if p.is_file() and "__pycache__" not in p.parts}


def _python_proxy(executable: Path, runtime_python: Path):
    executable.write_text(f'#!/bin/sh\nexec {shlex.quote(str(runtime_python))} "$@"\n')
    executable.chmod(0o755)


@pytest.fixture(scope="module")
def managed_python(tmp_path_factory):
    # CI installs development plugins into its controller environment. Targets
    # model fresh runtime environments, so their real subprocesses must not
    # inherit that package ownership from sys.executable.
    environment = tmp_path_factory.mktemp("managed-target-python")
    venv.EnvBuilder(with_pip=False).create(environment)
    return environment / "bin/python"


@pytest.fixture(params=["source", "bundle"])
def managed_transition(monkeypatch, transition, request, managed_python):
    previous, target, current, events = transition
    for release in (previous, target):
        _release_code(release)
    bundled = request.param == "bundle"
    if bundled:
        for release in (previous, target):
            # Published bundles contain core/runtime files, never plugin source packages.
            shutil.rmtree(release / "plugins")
            _bundled_runtime(release)
            _python_proxy(release / runtime_bundle.PYTHON, managed_python)
            manifest_path = release / runtime_bundle.MANIFEST
            manifest = json.loads(manifest_path.read_text())
            manifest["files"][runtime_bundle.PYTHON] = runtime_bundle.sha256(release / runtime_bundle.PYTHON)
            manifest_path.write_text(json.dumps(manifest))
        monkeypatch.setattr(upgrade, "_materialize_runtime_bundle", lambda **_: {"method": "fixture"})
        monkeypatch.setattr(runtime_bundle, "validate_runtime", lambda root: {"ok": True})
    releases, downloads = {}, {}
    for version in ("1.0.0", "1.0.1"):
        release, data = _publication(version)
        releases["v" + version] = release
        downloads.update(data)
    requests = []

    class Response(io.BytesIO):
        def __init__(self, url, data):
            super().__init__(data)
            self.url = url

        def geturl(self):
            return self.url

    def urlopen(request, **kwargs):
        url = request if isinstance(request, str) else request.full_url
        requests.append(url)
        if url.startswith(f"https://api.github.com/repos/{runtime_bundle.REPOSITORY}/releases/tags/"):
            return Response(url, json.dumps(releases[url.rsplit("/", 1)[1]]).encode())
        assert url in downloads, f"unexpected network request: {url}"
        return Response(url, downloads[url])

    monkeypatch.setattr(runtime_bundle, "urlopen", urlopen)
    monkeypatch.setattr(distribution, "_environment_version", lambda _: None)
    run_state = {"upgrading": False}
    staged_commands = []

    def staged_import(command, **kwargs):
        if run_state["upgrading"]:
            assert command[0] == str(upgrade._release_python(target)), "staged validation must use target Python"
            assert command[4] == str(target)
            staged_commands.append(command)
            events.append("managed_staged_validate")
        return subprocess.run(command, **kwargs)

    monkeypatch.setattr(distribution, "subprocess", SimpleNamespace(run=staged_import, TimeoutExpired=subprocess.TimeoutExpired))
    runtime = current.parent / "runtime"
    runtime.mkdir()
    config = runtime / "config.us.json"
    config.write_text(json.dumps({"portfolio_management": {"enabled": False}, "close_advice": {"enabled": False}}))
    profile = runtime / "service.profile.json"
    profile.write_text(json.dumps({"service_provider": "systemd", "config_paths": {"us": str(config)}}))
    for name, event, result in (
        ("_reconcile_services_from_current_release", "services", {"summary": {"ok": True}}),
        ("_restart_services_from_loaded_profile", "restart", []),
        ("_post_upgrade_service_health", "health", {"ok": True}),
    ):
        monkeypatch.setattr(upgrade, name, lambda _event=event, _result=result, **_: events.append(_event) or _result)
    old_switch = upgrade._switch_current_symlink

    def switch(**kwargs):
        events.append("switch")
        return old_switch(**kwargs)

    monkeypatch.setattr(upgrade, "_switch_current_symlink", switch)
    offline = OfflineRuntime(previous, installed=False)
    validation_commands = []

    def runner(command, **kwargs):
        if "-c" in command and "read_managed_plugin" in command[command.index("-c") + 1]:
            assert command[0] == str(upgrade._release_python(target))
            validation_commands.append(command)
            events.append("managed_validate")
            # Run the actual isolated target import check, with the validated test interpreter.
            return subprocess.run(command, **kwargs)
        if bundled and command[0] == str(previous / runtime_bundle.PYTHON):
            return subprocess.CompletedProcess(command, 0, offline.probe_output, "")
        result = offline(command, **kwargs)
        if command[:3] == [sys.executable, "-m", "venv"] or command[:2] == ["uv", "venv"]:
            _python_proxy(Path(command[-1]) / "bin/python", managed_python)
        return result

    def run(*, confirm=True):
        run_state["upgrading"] = True
        try:
            return upgrade.service_upgrade(
                repo_root=current, runtime_root=runtime, releases_root=target.parent, cache_root=current.parent / "cache",
                target_version="1.0.1", confirm=confirm, run_cmd=runner,
            )
        finally:
            run_state["upgrading"] = False

    def install(plugin_id, root=previous):
        return distribution.install_plugin(plugin_id, repo_root=root, confirm=True)

    return SimpleNamespace(previous=previous, target=target, current=current, events=events, runtime=runtime,
                           config=config, profile=profile, requests=requests, releases=releases, downloads=downloads,
                           offline=offline, validation_commands=validation_commands, run=run, install=install,
                           bundled=bundled, staged_commands=staged_commands)


@pytest.mark.parametrize("selected", [(), ("pm",), ("position-advice",), tuple(PLUGINS)])
@pytest.mark.parametrize("enabled", [False, True])
def test_managed_selection_rotates_wheels_independently_of_config(managed_transition, selected, enabled):
    ctx = managed_transition
    for plugin_id in selected:
        ctx.install(plugin_id)
    ctx.config.write_text(json.dumps({"portfolio_management": {"enabled": enabled}, "close_advice": {"enabled": enabled}}))
    old_store = _inventory(plugin_store(ctx.previous))
    old_bundle = _inventory(ctx.target) if ctx.bundled else None
    config_before, profile_before = ctx.config.read_bytes(), ctx.profile.read_bytes()
    ctx.requests.clear()

    result = ctx.run()

    assert result["ok"], result
    assert ctx.current.resolve() == ctx.target
    assert _inventory(plugin_store(ctx.previous)) == old_store
    assert ctx.config.read_bytes() == config_before and ctx.profile.read_bytes() == profile_before
    for plugin_id in PLUGINS:
        receipt = read_managed_plugin(ctx.target, plugin_id)
        if plugin_id in selected:
            assert receipt["om_version"] == receipt["version"] == "1.0.1"
            assert receipt["wheel_sha256"] != read_managed_plugin(ctx.previous, plugin_id)["wheel_sha256"]
            assert receipt["repo_root"] == str(ctx.target.resolve())
        else:
            assert receipt is None
    if selected:
        assert ctx.validation_commands
        assert len(ctx.staged_commands) == len(selected)
        assert ctx.events.index("managed_staged_validate") < ctx.events.index("managed_validate")
        assert ctx.events.index("managed_validate") < ctx.events.index("configs") < ctx.events.index("switch")
        assert ctx.events.index("switch") < ctx.events.index("services") < ctx.events.index("restart")
        assert all("/v1.0.1/" in url or url.endswith("/v1.0.1") for url in ctx.requests)
        assert len([url for url in ctx.requests if url.endswith(".whl")]) == len(selected)
    else:
        assert ctx.requests == []
    assert not ctx.offline.pm_installs
    assert not any("./plugins/position-advice" in cmd for cmd in ctx.offline.commands)
    if ctx.bundled:
        assert _inventory(ctx.target) == old_bundle
        runtime_bundle.load_manifest(ctx.target, verify_files=True)
    else:
        expected = upgrade._dependency_hash(ctx.target, include_server=True, installer_mode="pip")
        assert result["runtime_prepare"]["dependency_hash"] == expected
        assert not result["runtime_prepare"]["dependency_context"].get("include_pm")
        assert not result["runtime_prepare"]["dependency_context"].get("include_position_advice")


@pytest.mark.parametrize("failure", ["receipt", "missing_wheel", "digest", "import", "target_loader", "target_version"])
def test_managed_failures_leave_previous_store_config_and_services_untouched(managed_transition, failure):
    ctx = managed_transition
    ctx.install("pm")
    if failure == "receipt":
        (plugin_store(ctx.previous) / "pm/receipt.json").write_text("{invalid")
    elif failure == "missing_wheel":
        ctx.releases["v1.0.1"]["assets"] = [a for a in ctx.releases["v1.0.1"]["assets"] if not a["name"].startswith("om_pm_")]
    elif failure == "digest":
        url = next(url for url in ctx.downloads if "/v1.0.1/om_pm_" in url)
        ctx.downloads[url] += b"corrupted download"
    elif failure == "import":
        release, data = _publication("1.0.1", broken="pm")
        ctx.releases["v1.0.1"] = release
        ctx.downloads.update(data)
    elif failure == "target_loader":
        (ctx.target / "src/__init__.py").write_text("# target fails to register its managed finder\n")
        if ctx.bundled:
            path = ctx.target / runtime_bundle.MANIFEST
            manifest = json.loads(path.read_text())
            manifest["files"]["src/__init__.py"] = runtime_bundle.sha256(ctx.target / "src/__init__.py")
            path.write_text(json.dumps(manifest))
    elif failure == "target_version":
        ctx.install("pm", ctx.target)
        path = plugin_store(ctx.target) / "pm/receipt.json"
        receipt = json.loads(path.read_text())
        receipt["version"] = "0.1.0"  # valid file inventory, incompatible host/wheel version
        path.write_text(json.dumps(receipt))
    before = _inventory(plugin_store(ctx.previous))
    config_before, profile_before = ctx.config.read_bytes(), ctx.profile.read_bytes()

    result = ctx.run()

    assert not result["ok"], result
    assert not result["symlink_switched"] and not result["changed"]
    assert ctx.current.resolve() == ctx.previous
    assert _inventory(plugin_store(ctx.previous)) == before
    assert ctx.config.read_bytes() == config_before and ctx.profile.read_bytes() == profile_before
    assert all(event not in ctx.events for event in ("configs", "switch", "services", "restart", "health"))
    status = upgrade.load_upgrade_status(runtime_root=ctx.runtime)
    assert status["ok"] is False and status["symlink_switched"] is False


@pytest.mark.parametrize("plugin_id", list(PLUGINS))
def test_explicit_managed_uninstall_does_not_resurrect_target_residue(managed_transition, plugin_id):
    ctx = managed_transition
    ctx.install(plugin_id)
    ctx.install(plugin_id, ctx.target)  # left by a previous interrupted attempt
    distribution.uninstall_plugin(plugin_id, repo_root=ctx.previous, confirm=True)
    before = _inventory(plugin_store(ctx.target))

    result = ctx.run()

    assert not result["ok"], result
    assert ctx.current.resolve() == ctx.previous
    assert _inventory(plugin_store(ctx.target)) == before
    assert read_managed_plugin(ctx.previous, plugin_id) is None
    assert "configs" not in ctx.events and "switch" not in ctx.events


def test_managed_upgrade_preview_never_installs(managed_transition):
    ctx = managed_transition
    ctx.install("pm")
    ctx.requests.clear()
    before = _inventory(plugin_store(ctx.previous))

    result = ctx.run(confirm=False)

    assert result["ok"] and result["status"] == "dry_run", result
    assert ctx.current.resolve() == ctx.previous
    assert read_managed_plugin(ctx.target, "pm") is None
    assert _inventory(plugin_store(ctx.previous)) == before
    assert ctx.requests == [] and ctx.events == []


def test_controller_pm_package_does_not_block_target_managed_upgrade(managed_transition, monkeypatch):
    ctx = managed_transition
    ctx.install("pm")
    before = _inventory(plugin_store(ctx.previous))
    monkeypatch.setattr(distribution, "_environment_version", lambda plugin_id: "0.1.0" if plugin_id == "pm" else None)
    assert distribution._environment_version("pm") == "0.1.0"

    result = ctx.run()

    assert result["ok"], result
    assert ctx.current.resolve() == ctx.target
    assert read_managed_plugin(ctx.target, "pm")["version"] == "1.0.1"
    assert _inventory(plugin_store(ctx.previous)) == before
    assert not ctx.offline.pm_installs
    assert len(ctx.staged_commands) == 1
    assert ctx.staged_commands[0][0] == str(upgrade._release_python(ctx.target))
    assert ctx.events.index("managed_staged_validate") < ctx.events.index("managed_validate") < ctx.events.index("switch")


def test_previous_plugin_code_is_never_imported_by_updater(managed_transition):
    ctx = managed_transition
    for plugin_id in PLUGINS:
        ctx.install(plugin_id)
        directory = plugin_store(ctx.previous) / plugin_id
        entry = PLUGINS[plugin_id]["module"] + "/__init__.py"
        module = directory / "site" / entry
        module.write_text("raise RuntimeError('previous release plugin must never be imported')\n")
        receipt_path = directory / "receipt.json"
        receipt = json.loads(receipt_path.read_text())
        receipt["files"][entry] = runtime_bundle.sha256(module)
        receipt_path.write_text(json.dumps(receipt))
        assert read_managed_plugin(ctx.previous, plugin_id) is not None
    before = _inventory(plugin_store(ctx.previous))

    result = ctx.run()

    assert result["ok"], result
    assert ctx.current.resolve() == ctx.target
    assert _inventory(plugin_store(ctx.previous)) == before
    assert len(ctx.validation_commands) == 1


def test_partial_target_install_can_retry_without_touching_previous_store(managed_transition):
    ctx = managed_transition
    for plugin_id in PLUGINS:
        ctx.install(plugin_id)
    before = _inventory(plugin_store(ctx.previous))
    assets = ctx.releases["v1.0.1"]["assets"]
    ctx.releases["v1.0.1"]["assets"] = [a for a in assets if not a["name"].startswith("om_position_advice_")]

    first = ctx.run()

    assert not first["ok"]
    assert ctx.current.resolve() == ctx.previous
    assert "configs" not in ctx.events
    target_pm = read_managed_plugin(ctx.target, "pm")
    assert target_pm is not None
    assert read_managed_plugin(ctx.target, "position-advice") is None
    assert _inventory(plugin_store(ctx.previous)) == before
    ctx.releases["v1.0.1"]["assets"] = assets
    ctx.events.clear()
    ctx.requests.clear()

    second = ctx.run()

    assert second["ok"], second
    assert ctx.current.resolve() == ctx.target
    assert read_managed_plugin(ctx.target, "pm") == target_pm
    assert read_managed_plugin(ctx.target, "position-advice")["version"] == "1.0.1"
    assert _inventory(plugin_store(ctx.previous)) == before
    assert not any("/om_pm_plugin-" in url for url in ctx.requests)


@pytest.fixture
def managed_rollback(managed_transition, monkeypatch, managed_python):
    ctx = managed_transition
    commands = []
    if not ctx.bundled:
        _python_proxy(upgrade._release_python(ctx.previous), managed_python)

    def runner(command, **kwargs):
        if "-c" in command and "read_managed_plugin" in command[command.index("-c") + 1]:
            assert command[0] == str(upgrade._release_python(ctx.previous))
            assert command[4] == str(ctx.previous)
            commands.append(command)
            ctx.events.append("managed_validate")
            return subprocess.run(command, **kwargs)
        return ctx.offline(command, **kwargs)

    def rollback(*, confirm=True, restart_services=False):
        # Rollback must inspect what its own target has, never install or migrate.
        monkeypatch.setattr(distribution, "install_plugin", lambda *a, **kw: pytest.fail("rollback installed a plugin"))
        return upgrade.service_rollback(
            repo_root=ctx.current, runtime_root=ctx.runtime, releases_root=ctx.target.parent,
            to_version="1.0.0", confirm=confirm, restart_services=restart_services, run_cmd=runner,
        )

    return SimpleNamespace(ctx=ctx, run=rollback, commands=commands)


@pytest.mark.parametrize("plugin_id", list(PLUGINS))
@pytest.mark.parametrize("failure", ["receipt", "import"])
@pytest.mark.parametrize("restart_services", [False, True])
def test_managed_rollback_failure_stops_before_configs_and_switch(managed_rollback, plugin_id, failure, restart_services):
    rollback, ctx = managed_rollback, managed_rollback.ctx
    ctx.install(plugin_id)
    assert ctx.run()["ok"]
    directory = plugin_store(ctx.previous) / plugin_id
    receipt_path = directory / "receipt.json"
    if failure == "receipt":
        receipt_path.write_text("{invalid")
    else:
        entry = PLUGINS[plugin_id]["module"] + "/__init__.py"
        module = directory / "site" / entry
        module.write_text("raise ImportError('broken rollback plugin')\n")
        receipt = json.loads(receipt_path.read_text())
        receipt["files"][entry] = runtime_bundle.sha256(module)
        receipt_path.write_text(json.dumps(receipt))
        assert read_managed_plugin(ctx.previous, plugin_id) is not None
    old_store, current_store = _inventory(plugin_store(ctx.previous)), _inventory(plugin_store(ctx.target))
    config_before, profile_before = ctx.config.read_bytes(), ctx.profile.read_bytes()
    ctx.events.clear()
    ctx.requests.clear()

    result = rollback.run(restart_services=restart_services)

    assert not result["ok"], result
    assert result["status"] == ("managed_plugin_invalid" if failure == "receipt" else "rollback_failed")
    assert not result["changed"] and not result["symlink_switched"]
    assert ctx.current.resolve() == ctx.target
    assert _inventory(plugin_store(ctx.previous)) == old_store
    assert _inventory(plugin_store(ctx.target)) == current_store
    assert ctx.config.read_bytes() == config_before and ctx.profile.read_bytes() == profile_before
    assert all(event not in ctx.events for event in ("configs", "switch", "services", "restart", "health"))
    assert bool(rollback.commands) is (failure == "import")
    assert ctx.requests == []
    status = upgrade.load_upgrade_status(runtime_root=ctx.runtime)
    assert status["ok"] is False and status["symlink_switched"] is False


@pytest.mark.parametrize("plugin_id", list(PLUGINS))
def test_managed_rollback_preview_rejects_bad_target_receipt_without_loading(managed_rollback, plugin_id):
    rollback, ctx = managed_rollback, managed_rollback.ctx
    ctx.install(plugin_id)
    assert ctx.run()["ok"]
    (plugin_store(ctx.previous) / plugin_id / "receipt.json").write_text("{invalid")
    ctx.events.clear()
    ctx.requests.clear()
    status_before = upgrade.upgrade_status_path(ctx.runtime).read_bytes()

    result = rollback.run(confirm=False)

    assert not result["ok"] and result["status"] == "managed_plugin_invalid", result
    assert ctx.current.resolve() == ctx.target
    assert ctx.events == [] and rollback.commands == [] and ctx.requests == []
    assert upgrade.upgrade_status_path(ctx.runtime).read_bytes() == status_before


@pytest.mark.parametrize("selected", [(), ("pm",), ("position-advice",), tuple(PLUGINS)])
@pytest.mark.parametrize("restart_services", [False, True])
def test_managed_rollback_validates_only_target_selection_without_migrating(managed_rollback, selected, restart_services):
    rollback, ctx = managed_rollback, managed_rollback.ctx
    for plugin_id in selected:
        ctx.install(plugin_id)
    assert ctx.run()["ok"]
    # Make current's selection different; rollback must keep the old target's choice.
    for plugin_id in PLUGINS:
        if plugin_id in selected:
            distribution.uninstall_plugin(plugin_id, repo_root=ctx.target, confirm=True)
        else:
            ctx.install(plugin_id, ctx.target)
    old_store, current_store = _inventory(plugin_store(ctx.previous)), _inventory(plugin_store(ctx.target))
    config_before, profile_before = ctx.config.read_bytes(), ctx.profile.read_bytes()
    ctx.events.clear()
    ctx.requests.clear()

    result = rollback.run(restart_services=restart_services)

    assert result["ok"] and result["status"] == "rolled_back", result
    assert ctx.current.resolve() == ctx.previous
    assert _inventory(plugin_store(ctx.previous)) == old_store
    assert _inventory(plugin_store(ctx.target)) == current_store
    assert ctx.config.read_bytes() == config_before and ctx.profile.read_bytes() == profile_before
    assert ctx.requests == []
    assert len(rollback.commands) == bool(selected)
    if selected:
        assert json.loads(rollback.commands[0][-1]) == list(selected)
        assert ctx.events.index("managed_validate") < ctx.events.index("configs") < ctx.events.index("switch")
    if not restart_services:
        assert "restart" not in ctx.events and "health" not in ctx.events
