"""Public CLI -> official assets -> staged installation -> real lazy adapter.

Only HTTPS transport is replaced; package/receipt/import and CLI paths are real.
"""
import base64
import csv
import hashlib
import importlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import venv
import zipfile

import pytest

from src.application import plugin_distribution as distribution, plugin_runtime as runtime
from src.interfaces.cli import main as cli
from tests.plugin_release_helpers import copy_channel_core

SOURCE = Path(__file__).resolve().parents[1]
VERSION = "9.9.9"


def test_feishu_dependency_validator_is_available_in_core_source_runtime():
    from packaging.requirements import Requirement
    requirements = (SOURCE / "requirements/runtime.txt").read_text().splitlines()
    assert "packaging" in {Requirement(line).name for line in requirements if line and not line.startswith("#")}


def write_test_sdk(site, *, requirement=""):
    """Contract-only SDK fixture; real wheel acceptance is a separate offline QA."""
    content = {
        "lark_oapi/__init__.py": "from . import ws\nclass EventDispatcherHandler:\n    @staticmethod\n    def builder(*args): raise AssertionError('must not construct a dispatcher')\n",
        "lark_oapi/ws/__init__.py": "class Client:\n    def __init__(self,*args,**kwargs): raise AssertionError('must not connect')\n",
        "lark_oapi/core/__init__.py": "",
        "lark_oapi/core/enum.py": "class LogLevel:\n    INFO=1\n",
        "lark_oapi-1.6.5.dist-info/METADATA": "Metadata-Version: 2.4\nName: lark-oapi\nVersion: 1.6.5\n" + requirement,
    }
    for name, text in content.items():
        path = site / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


def make_wheel(folder, plugin_id, *, extra=None, code_override=None):
    item = runtime.PLUGINS[plugin_id]
    module = item["module"]
    distribution_name = item["distribution"].replace("-", "_")
    metadata = f"{distribution_name}-{VERSION}.dist-info"
    files = {path.relative_to(SOURCE / "plugins" / item["source"] / "src").as_posix(): path.read_bytes()
             for path in (SOURCE / "plugins" / item["source"] / "src" / module).rglob("*.py")}
    if code_override is not None:
        files[f"{module}/__init__.py"] = code_override.encode()
    requirement = "Requires-Dist: lark-oapi\n" if plugin_id == "feishu" else ""
    files[f"{metadata}/METADATA"] = f"Metadata-Version: 2.4\nName: {item['distribution']}\nVersion: {VERSION}\nRequires-Python: >=3.12\n{requirement}".encode()
    files[f"{metadata}/WHEEL"] = b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    files.update(extra or {})
    record = io.StringIO()
    writer = csv.writer(record, lineterminator="\n")
    for name, data in files.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
        writer.writerow([name, "sha256=" + digest, len(data)])
    writer.writerow([f"{metadata}/RECORD", "", ""])
    files[f"{metadata}/RECORD"] = record.getvalue().encode()
    asset = f"{distribution_name}-{VERSION}-py3-none-any.whl"
    path = folder / asset
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return {"id": plugin_id, "distribution": item["distribution"], "module": module,
            "version": VERSION, "requires_python": ">=3.12", "asset": asset,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "size": path.stat().st_size}


@pytest.fixture(scope="module")
def plugin_python(tmp_path_factory):
    environment = tmp_path_factory.mktemp("plugin-target-python")
    venv.EnvBuilder(with_pip=False).create(environment)
    site = next((environment / "lib").glob("python*/site-packages"))
    shutil.copytree(Path(importlib.util.find_spec("packaging").origin).parent, site / "packaging")
    return environment / "bin/python"


@pytest.fixture
def official(tmp_path, monkeypatch, plugin_python):
    root = tmp_path / "release"
    root.mkdir()
    (root / "VERSION").write_text(VERSION)
    for rel in ["src/__init__.py", "src/application/__init__.py", "src/application/plugin_runtime.py",
                "src/application/portfolio_management_contract.py"]:
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(SOURCE / rel, target)
    copy_channel_core(SOURCE, root)
    (root / "domain").symlink_to(SOURCE / "domain", target_is_directory=True)
    for item in runtime.PLUGINS.values():
        target = root / "plugins" / item["source"]
        target.mkdir(parents=True)
        shutil.copy2(SOURCE / "plugins" / item["source"] / "pyproject.toml", target / "pyproject.toml")
    assets = tmp_path / "assets"
    assets.mkdir()
    catalog = {"schema_version": 1, "repository": "liuxie066/options-monitor", "om_version": VERSION,
               "tag": "v" + VERSION, "commit": "a" * 40,
               "plugins": [make_wheel(assets, key) for key in runtime.PLUGINS]}
    downloads = []
    def publication():
        (assets / "om-plugins.json").write_text(json.dumps(catalog))
        return {"tag_name": "v" + VERSION, "draft": False, "prerelease": False,
                "assets": [{"name": path.name, "state": "uploaded", "size": path.stat().st_size,
                            "digest": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(),
                            "browser_download_url": f"https://github.com/liuxie066/options-monitor/releases/download/v{VERSION}/{path.name}"}
                           for path in assets.iterdir()]}
    class Response(io.BytesIO):
        def __init__(self, value, url):
            super().__init__(value)
            self.url = url
        def geturl(self):
            return self.url
    def fetch(request, **kwargs):
        url = request.full_url
        downloads.append(url)
        release = publication()
        if url == f"https://api.github.com/repos/liuxie066/options-monitor/releases/tags/v{VERSION}":
            return Response(json.dumps(release).encode(), url)
        prefix = f"https://github.com/liuxie066/options-monitor/releases/download/v{VERSION}/"
        assert url.startswith(prefix), f"unexpected network request: {url}"
        return Response((assets / url[len(prefix):]).read_bytes(), url)
    monkeypatch.setattr(distribution.runtime_bundle, "urlopen", fetch)
    monkeypatch.setattr(distribution, "code_root", lambda: root)
    monkeypatch.setattr(runtime, "code_root", lambda: root)
    monkeypatch.setattr(distribution, "_environment_version", lambda _: None)
    # Only dependency acquisition is replaced. Probes/imports use fresh real Python processes.
    from types import SimpleNamespace
    original_run = subprocess.run
    def offline_pip(command, **kwargs):
        if "--target" in command and "lark-oapi" in command:
            write_test_sdk(Path(command[command.index("--target") + 1]))
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[1:3] == ["-I", "-c"]:
            command = [str(plugin_python), *command[1:]]
        return original_run(command, **kwargs)
    monkeypatch.setattr(distribution, "subprocess", SimpleNamespace(run=offline_pip, TimeoutExpired=subprocess.TimeoutExpired))
    monkeypatch.setattr(cli, "repo_base", lambda: root)
    monkeypatch.setenv("OM_RUNTIME_ROOT", str(tmp_path / "isolated-runtime"))
    for key in ("OM_ENV_FILE",):
        monkeypatch.delenv(key, raising=False)
    yield root, assets, catalog, downloads
    for name in list(sys.modules):
        if any(name == item["module"] or name.startswith(item["module"] + ".") for item in runtime.PLUGINS.values()):
            sys.modules.pop(name)


def test_cli_list_offline_and_install_preview_has_no_persistent_effect(official, capsys):
    root, _, _, downloads = official
    assert cli.main(["plugins", "list", "--format", "json"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert all(row["status"] == "not_installed" for row in output["plugins"])
    assert not downloads
    assert cli.main(["plugins", "install", "pm", "--format", "json"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "preview" and output["enabled_changed"] is False
    assert not runtime.plugin_store(root).exists()
    assert len(downloads) == 2


@pytest.mark.parametrize("plugin_id", runtime.PLUGINS)
def test_cli_install_real_adapter_receipt_idempotency_and_uninstall(official, capsys, plugin_id):
    root, _, _, downloads = official
    before = {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    command = ["plugins", "install", plugin_id, "--confirm", "--format", "json"]
    assert cli.main(command) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "installed" and output["restart_required"] is True
    assert len(downloads) == 3  # API + catalog + selected wheel, not the other plugin.
    receipt = runtime.read_managed_plugin(root, plugin_id)
    assert receipt["version"] == VERSION
    assert cli.main(command) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "already_installed"
    assert len(downloads) == 3
    # Real PM/advice package loads through the same finder used by scheduled jobs.
    module = importlib.import_module(runtime.PLUGINS[plugin_id]["module"])
    assert str(runtime.plugin_store(root)) in module.__file__
    if plugin_id == "pm":
        from src.application.portfolio_management import load_portfolio_management_client_class
        assert load_portfolio_management_client_class() is module.PortfolioManagementClient
    elif plugin_id == "position-advice":
        from domain.domain.close_advice import require_position_advice_plugin
        assert require_position_advice_plugin() is module
    else:
        from src.application.channels.plugins import load_channel_plugin
        assert load_channel_plugin(plugin_id) is module
    assert cli.main(["plugins", "uninstall", plugin_id, "--format", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "preview"
    assert runtime.read_managed_plugin(root, plugin_id)
    assert cli.main(["plugins", "uninstall", plugin_id, "--confirm", "--format", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "uninstalled"
    assert runtime.read_managed_plugin(root, plugin_id) is None
    assert before == {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file() and p.suffix != ".pyc"}


@pytest.mark.parametrize("fault", ["version", "missing_asset", "unsafe_wheel", "import_failure"])
def test_failed_install_keeps_code_config_and_store_unchanged(official, capsys, fault):
    root, assets, catalog, _ = official
    if fault == "version":
        catalog["om_version"] = "8.0.0"
    elif fault == "missing_asset":
        (assets / catalog["plugins"][0]["asset"]).unlink()
    else:
        entry = make_wheel(assets, "pm", **({"extra": {"../escape.py": b"bad"}} if fault == "unsafe_wheel" else
                                           {"code_override": "raise RuntimeError('broken optional plugin')"}))
        catalog["plugins"][0] = entry
    assert cli.main(["plugins", "install", "pm", "--confirm", "--format", "json"]) == 2
    output = json.loads(capsys.readouterr().out)
    assert output["error"]["code"] == "PLUGIN_ERROR"
    assert not (runtime.plugin_store(root) / "pm").exists()
    assert not (root / "config.yaml").exists()
    assert not (root.parent / "escape.py").exists()


def test_corruption_is_not_hidden_by_python_environment_fallback(official, capsys):
    root, _, _, _ = official
    distribution.install_plugin("pm", confirm=True)
    installed = runtime.plugin_store(root) / "pm/site/om_pm_plugin/__init__.py"
    installed.write_text("raise RuntimeError('corrupt')")
    assert cli.main(["plugins", "list", "--format", "json"]) == 2
    assert json.loads(capsys.readouterr().out)["plugins"][0]["status"] == "invalid"
    with pytest.raises(ImportError):
        importlib.util.find_spec("om_pm_plugin")


def test_other_code_release_and_runtime_root_do_not_change_install_selection(official, tmp_path, monkeypatch):
    root, _, _, _ = official
    distribution.install_plugin("pm", confirm=True)
    other = tmp_path / "other-release"
    other.mkdir()
    (other / "VERSION").write_text(VERSION)
    assert runtime.read_managed_plugin(other, "pm") is None
    monkeypatch.setenv("OM_RUNTIME_ROOT", str(tmp_path / "different-account-runtime"))
    assert runtime.read_managed_plugin(root, "pm") is not None


def test_official_wheels_load_in_fresh_direct_python_process_without_pythonpath(official):
    root, _, _, _ = official
    for plugin_id in runtime.PLUGINS:
        distribution.install_plugin(plugin_id, confirm=True)
    code = "import sys;sys.path.insert(0,sys.argv[1]);import src;from domain.domain.close_advice import require_position_advice_plugin;assert callable(require_position_advice_plugin().evaluate_close_advice);from om_pm_plugin import PortfolioManagementClient;assert callable(PortfolioManagementClient)"
    run = subprocess.run([sys.executable, "-I", "-c", code, str(root)], capture_output=True, text=True)
    assert run.returncode == 0, run.stderr


def test_bundle_without_plugin_sources_lists_and_installs_outside_inventory(official, capsys):
    from tests.test_pm_package_upgrade import _bundled_runtime

    root, _, _, downloads = official
    shutil.rmtree(root / "plugins")
    # Replace the fixture's domain link before sealing an immutable bundle.
    (root / "domain").unlink()
    shutil.copytree(SOURCE / "domain", root / "domain", ignore=shutil.ignore_patterns("__pycache__"))
    _bundled_runtime(root)
    manifest = distribution.runtime_bundle.load_manifest(root, verify_files=True)
    before = (root / "om.runtime.json").read_bytes()
    assert cli.main(["plugins", "list", "--format", "json"]) == 0
    assert all(row["status"] == "not_installed" for row in json.loads(capsys.readouterr().out)["plugins"])
    assert not downloads
    distribution.install_plugin("pm", confirm=True)
    assert distribution.list_plugins()["plugins"][0]["status"] == "installed"
    assert distribution.runtime_bundle.load_manifest(root, verify_files=True) == manifest
    assert (root / "om.runtime.json").read_bytes() == before


def test_import_timeout_leaves_no_loadable_installation(official, monkeypatch):
    root, _, _, _ = official
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("isolated-plugin-import", 30)
    monkeypatch.setattr(distribution.subprocess, "run", timeout)
    with pytest.raises(ValueError, match="timed out"):
        distribution.install_plugin("pm", confirm=True)
    assert runtime.read_managed_plugin(root, "pm") is None


def test_damaged_installation_can_be_explicitly_removed(official):
    root, _, _, downloads = official
    distribution.install_plugin("pm", confirm=True)
    target = runtime.plugin_store(root) / "pm"
    (target / "receipt.json").write_text("{broken")
    downloads.clear()
    assert distribution.uninstall_plugin("pm")["status"] == "preview"
    assert target.exists()
    assert distribution.uninstall_plugin("pm", confirm=True)["status"] == "uninstalled"
    assert not target.exists() and not downloads


@pytest.mark.parametrize("action", ["install", "uninstall"])
def test_python_environment_packages_keep_their_owner(official, monkeypatch, action):
    root, _, _, downloads = official
    monkeypatch.setattr(distribution, "_environment_version", lambda _: "0.1.0")
    with pytest.raises(ValueError, match="Python environment"):
        getattr(distribution, action + "_plugin")("pm", confirm=True)
    assert not downloads and not runtime.plugin_store(root).exists()


def test_uninstall_does_not_report_success_for_non_directory(official):
    root, _, _, _ = official
    target = runtime.plugin_store(root) / "pm"
    target.parent.mkdir(parents=True)
    target.write_text("unexpected file")
    with pytest.raises(ValueError, match="not a directory"):
        distribution.uninstall_plugin("pm", confirm=True)
    assert target.read_text() == "unexpected file"


def test_uninstall_cleanup_failure_is_explicit_without_partial_visible_install(official, monkeypatch):
    root, _, _, _ = official
    distribution.install_plugin("pm", confirm=True)
    target = runtime.plugin_store(root) / "pm"
    def partial_cleanup(path):
        (path / "site/om_pm_plugin/__init__.py").unlink()
        raise PermissionError("fixture: protected metadata")
    monkeypatch.setattr(distribution.shutil, "rmtree", partial_cleanup)
    result = distribution.uninstall_plugin("pm", confirm=True)
    assert result["status"] == "uninstalled" and result["changed"]
    assert result["cleanup_pending"] is True
    assert Path(result["retained_directory"]).is_dir()
    assert not target.exists() and runtime.read_managed_plugin(root, "pm") is None
    assert distribution.list_plugins()["plugins"][0]["status"] == "not_installed"


def test_uninstall_rename_failure_keeps_valid_installation(official, monkeypatch):
    root, _, _, _ = official
    distribution.install_plugin("pm", confirm=True)
    target = runtime.plugin_store(root) / "pm"
    before = runtime.read_managed_plugin(root, "pm")
    rename = Path.rename
    def fail_selected(path, destination):
        if path == target:
            raise PermissionError("fixture: directory not writable")
        return rename(path, destination)
    monkeypatch.setattr(Path, "rename", fail_selected)
    with pytest.raises(PermissionError):
        distribution.uninstall_plugin("pm", confirm=True)
    assert runtime.read_managed_plugin(root, "pm") == before


@pytest.mark.parametrize("plugin_id", runtime.PLUGINS)
@pytest.mark.parametrize("confirm", [False, True])
def test_mixed_environment_package_requires_owner_cleanup_before_managed_uninstall(official, tmp_path, monkeypatch, plugin_id, confirm):
    root, _, _, _ = official
    distribution.install_plugin(plugin_id, confirm=True)
    before = runtime.read_managed_plugin(root, plugin_id)
    item = runtime.PLUGINS[plugin_id]
    environment = tmp_path / "python-environment"
    shutil.copytree(SOURCE / "plugins" / item["source"] / "src" / item["module"], environment / item["module"])
    metadata = environment / (item["module"] + "-0.1.0.dist-info")
    metadata.mkdir()
    (metadata / "METADATA").write_text(f"Metadata-Version: 2.4\nName: {item['distribution']}\nVersion: 0.1.0\n")
    monkeypatch.syspath_prepend(str(environment))
    monkeypatch.setattr(distribution, "_environment_version", lambda key: importlib.metadata.version(runtime.PLUGINS[key]["distribution"]))
    assert distribution._environment_version(plugin_id) == "0.1.0"
    with pytest.raises(ValueError, match="Python environment"):
        distribution.uninstall_plugin(plugin_id, confirm=confirm)
    assert runtime.read_managed_plugin(root, plugin_id) == before


def test_running_process_keeps_physical_code_owner_after_current_symlink_switch(official, tmp_path):
    root, _, _, _ = official
    other = tmp_path / "next-release"
    shutil.copytree(root, other, symlinks=True)
    distribution.install_plugin("pm", repo_root=root, confirm=True)
    distribution.install_plugin("pm", repo_root=other, confirm=True)
    current = tmp_path / "current"
    current.symlink_to(root, target_is_directory=True)
    script = '''
import sys
from pathlib import Path
current, previous, target = map(Path, sys.argv[1:])
sys.path.insert(0, str(current))
import src
from src.application.plugin_runtime import code_root, plugin_store
assert code_root() == previous.resolve()
current.unlink()
current.symlink_to(target, target_is_directory=True)
assert code_root() == previous.resolve(), "running process rebound to new code release"
from om_pm_plugin import PortfolioManagementClient
assert Path(sys.modules['om_pm_plugin'].__file__).is_relative_to(plugin_store(previous))
'''
    result = subprocess.run([sys.executable, "-I", "-c", script, str(current), str(root), str(other)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_feishu_preview_does_not_prepare_sdk_or_change_activation(official, monkeypatch):
    root, _, _, _ = official
    def forbidden(*args, **kwargs):
        pytest.fail("preview must not acquire dependencies or import SDK")
    monkeypatch.setattr(distribution, "_prepare_feishu_sdk", forbidden)
    result = distribution.install_plugin("feishu")
    assert result["dependencies"] == ["lark-oapi"] and result["enabled_changed"] is False
    assert not runtime.plugin_store(root).exists()


def test_managed_sdk_visible_to_fresh_readiness_and_corruption_rejected(official, plugin_python):
    root, _, _, _ = official
    distribution.install_plugin("feishu", confirm=True)
    receipt = runtime.read_managed_plugin(root, "feishu")
    assert "lark_oapi/__init__.py" in receipt["dependency_files"]
    code = "import sys;sys.path.insert(0,sys.argv[1]);from src.infrastructure.feishu_ws_client import is_feishu_ws_sdk_available;assert is_feishu_ws_sdk_available()"
    result = subprocess.run([str(plugin_python), "-I", "-c", code, str(root)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    sdk = runtime.plugin_store(root) / "feishu/dependencies/lark_oapi/__init__.py"
    sdk.write_text("raise RuntimeError('corrupt SDK')\n")
    with pytest.raises(ValueError, match="inventory"):
        runtime.read_managed_plugin(root, "feishu")
    assert distribution.list_plugins()["plugins"][2]["status"] == "invalid"
    assert distribution.uninstall_plugin("feishu", confirm=True)["status"] == "uninstalled"
    assert not sdk.exists()


@pytest.mark.parametrize("failure", ["install", "import", "requirements", "timeout"])
def test_sdk_failure_has_no_active_plugin_and_leaves_existing_environment_untouched(official, monkeypatch, failure):
    root, _, _, _ = official
    original = distribution.subprocess.run
    def broken(command, **kwargs):
        if "--target" in command:
            if failure == "timeout":
                raise subprocess.TimeoutExpired(command, 300)
            if failure == "install":
                return subprocess.CompletedProcess(command, 1, "", "offline install failed")
            site = Path(command[command.index("--target") + 1])
            write_test_sdk(site, requirement="Requires-Dist: om-impossible-sdk-dependency>=999\n" if failure == "requirements" else "")
            if failure == "import":
                (site / "lark_oapi/core/enum.py").unlink()
            return subprocess.CompletedProcess(command, 0, "", "")
        return original(command, **kwargs)
    monkeypatch.setattr(distribution.subprocess, "run", broken)
    before = {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    with pytest.raises(ValueError):
        distribution.install_plugin("feishu", confirm=True)
    assert runtime.read_managed_plugin(root, "feishu") is None
    assert before == {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file() and p.suffix != ".pyc"}


def test_missing_sdk_never_mutates_or_extends_runtime_bundle(official, monkeypatch):
    root, _, _, _ = official
    # This explicit bundle marker is enough to enforce the no-pip boundary.
    (root / "om.runtime.json").write_text("{}")
    monkeypatch.setattr(distribution, "_catalog", lambda *args: (_ for _ in ()).throw(AssertionError("not used")))
    staged = root.parent / "staged"
    staged.mkdir()
    with pytest.raises(ValueError, match="bundled Feishu SDK"):
        distribution._prepare_feishu_sdk(root, staged)
    assert list(staged.iterdir()) == []


@pytest.mark.parametrize("uv_available", [False, True])
def test_sdk_installer_targets_private_stage_and_exact_python(official, monkeypatch, plugin_python, uv_available):
    root, _, _, _ = official
    staged = root.parent / "sdk-staging"
    staged.mkdir()
    monkeypatch.setattr(distribution.shutil, "which", lambda name: "/tools/uv" if uv_available else None)
    monkeypatch.setenv("UV_PREFIX", "/must-not-write")
    monkeypatch.setenv("UV_NO_DEPS", "1")
    monkeypatch.setenv("PIP_TARGET", "/must-not-write")
    calls = []
    original = distribution.subprocess.run
    def acquire(command, **kwargs):
        if "--target" in command:
            calls.append((command, kwargs))
        return original(command, **kwargs)
    monkeypatch.setattr(distribution.subprocess, "run", acquire)
    before = {p.relative_to(plugin_python.parent.parent): p.read_bytes()
              for p in plugin_python.parent.parent.rglob("*") if p.is_file()}
    files = distribution._prepare_feishu_sdk(root, staged, python_executable=plugin_python)
    assert "lark_oapi/__init__.py" in files
    assert len(calls) == 1
    command, kwargs = calls[0]
    if uv_available:
        assert command[:6] == ["/tools/uv", "--no-config", "--no-cache", "--no-python-downloads", "pip", "install"]
        assert command[command.index("--python") + 1] == str(plugin_python)
        assert command[command.index("--link-mode") + 1] == "copy"
    else:
        assert command[:6] == [str(plugin_python), "-I", "-m", "pip", "--isolated", "install"]
    assert command[command.index("--target") + 1] == str(staged / "dependencies")
    assert command[command.index("-c") + 1] == str(root / "constraints/runtime-bundle.txt")
    assert not any(name.startswith(("UV_", "PIP_")) for name in kwargs["env"])
    assert before == {p.relative_to(plugin_python.parent.parent): p.read_bytes()
                      for p in plugin_python.parent.parent.rglob("*") if p.is_file()}
