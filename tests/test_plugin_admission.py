"""Fresh-process admission through the host's real optional capability entries."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest

from src.application.plugin_runtime import PLUGINS


ROOT = Path(__file__).resolve().parents[1]


def _probe(tmp_path, plugin_id, body, *, version="0.0.0", package_code=""):
    item = PLUGINS[plugin_id]
    package = tmp_path / item["module"]
    package.mkdir()
    (package / "__init__.py").write_text(
        "import builtins\nbuiltins.plugin_executions.append(__name__)\n" + package_code
    )
    code = f'''
import builtins, importlib, importlib.metadata, sys
sys.path.insert(0, {str(tmp_path)!r})
from src.application import plugin_runtime
builtins.plugin_executions = []
original_version = importlib.metadata.version
def version(name):
    if name == {item["distribution"]!r}:
        if {version!r} is None:
            raise importlib.metadata.PackageNotFoundError(name)
        return {version!r}
    return original_version(name)
importlib.metadata.version = version
{body}
'''
    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, text=True,
                            capture_output=True, timeout=60,
                            env={**os.environ, "OM_SECRET_BACKEND": "env"})
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("plugin_id", PLUGINS)
@pytest.mark.parametrize("version", ["0.0.0", None])
def test_host_direct_import_rejects_unqualified_package_before_execution(tmp_path, plugin_id, version):
    module = PLUGINS[plugin_id]["module"]
    _probe(tmp_path, plugin_id, f'''
assert not plugin_runtime.plugin_installed({plugin_id!r})
try:
    importlib.import_module({module!r})
except ImportError:
    pass
else:
    raise AssertionError("unqualified plugin imported")
assert builtins.plugin_executions == []
''', version=version)


@pytest.mark.parametrize("plugin_id", PLUGINS)
def test_qualified_environment_package_stays_lazy_until_loaded(tmp_path, plugin_id):
    module = PLUGINS[plugin_id]["module"]
    _probe(tmp_path, plugin_id, f'''
assert plugin_runtime.plugin_installed({plugin_id!r})
assert builtins.plugin_executions == []
assert plugin_runtime.require_plugin({plugin_id!r}).__name__ == {module!r}
assert builtins.plugin_executions == [{module!r}]
''', version="0.1.0")


@pytest.mark.parametrize("plugin_id,entry,error", [
    ("feishu", 'from src.application.channels.plugins import load_channel_plugin\nload = lambda: load_channel_plugin("feishu")', "AgentToolError"),
    ("wechat", 'from src.application.channels.plugins import load_channel_plugin\nload = lambda: load_channel_plugin("wechat_clawbot")', "AgentToolError"),
    ("pm", 'from src.application.portfolio_management import load_portfolio_management_client_class as load', "PortfolioManagementUnavailableError"),
    ("position-advice", 'from src.application.position_advice_plugin import require_position_advice_plugin as load', "PositionAdviceUnavailableError"),
])
def test_public_loaders_reject_incompatible_package(tmp_path, plugin_id, entry, error):
    _probe(tmp_path, plugin_id, f'''
{entry}
try:
    load()
except Exception as exc:
    assert type(exc).__name__ == {error!r}, repr(exc)
else:
    raise AssertionError("public loader accepted incompatible plugin")
assert builtins.plugin_executions == []
''', package_code="class PortfolioManagementClient: pass\ndef evaluate_close_advice(*a): pass\ndef format_close_advice_reason(*a): pass\n")


@pytest.mark.parametrize("channel,plugin_id", [("feishu", "feishu"), ("wechat_clawbot", "wechat")])
def test_qualified_channel_dependency_failure_is_not_package_absence(tmp_path, channel, plugin_id):
    _probe(tmp_path, plugin_id, f'''
from src.application.channels.plugins import channel_plugin_installed, load_channel_plugin
assert channel_plugin_installed({channel!r})
assert builtins.plugin_executions == []
try:
    load_channel_plugin({channel!r})
except ImportError as exc:
    assert str(exc) == "fixture dependency broken"
else:
    raise AssertionError("broken dependency accepted")
assert builtins.plugin_executions == [{PLUGINS[plugin_id]["module"]!r}]
''', version="0.1.0", package_code="raise ImportError('fixture dependency broken')\n")


def test_advice_status_rejects_incompatible_package_without_import(tmp_path):
    _probe(tmp_path, "position-advice", '''
from src.application.position_advice_plugin import position_advice_plugin_status
assert position_advice_plugin_status({"close_advice": {"enabled": True}}) == "unavailable"
assert builtins.plugin_executions == []
''')


@pytest.mark.parametrize("plugin_id,entry", [
    ("pm", "from src.application.portfolio_management import load_portfolio_management_client_class as load"),
    ("position-advice", "from src.application.position_advice_plugin import require_position_advice_plugin as load"),
    ("position-advice", "from src.application.position_advice_plugin import evaluate_close_advice\nload = lambda: evaluate_close_advice(None)"),
    ("feishu", 'from src.application.channels.plugins import load_channel_plugin\nload = lambda: load_channel_plugin("feishu")'),
    ("wechat", 'from src.application.channels.plugins import load_channel_plugin\nload = lambda: load_channel_plugin("wechat")'),
])
def test_cached_package_cannot_bypass_host_admission(tmp_path, plugin_id, entry):
    module = PLUGINS[plugin_id]["module"]
    _probe(tmp_path, plugin_id, f'''
from types import ModuleType
cached = ModuleType({module!r})
cached.PortfolioManagementClient = lambda: "cached PM"
cached.evaluate_close_advice = lambda *args: "cached advice"
cached.format_close_advice_reason = lambda *args: "cached reason"
sys.modules[{module!r}] = cached
{entry}
try:
    load()
except Exception as exc:
    assert type(exc).__name__ in {{"PortfolioManagementUnavailableError", "PositionAdviceUnavailableError", "AgentToolError"}}
else:
    raise AssertionError("cached incompatible plugin accepted")
assert builtins.plugin_executions == []
''')


def test_genuinely_absent_official_packages_keep_none_discovery():
    # -S removes environment owners; the repository contains no top-level plugin packages.
    code = '''
import importlib.util
from src.application.plugin_runtime import PLUGINS
for item in PLUGINS.values():
    assert importlib.util.find_spec(item["module"]) is None, item
'''
    result = subprocess.run([sys.executable, "-S", "-c", code], cwd=ROOT,
                            text=True, capture_output=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("source_layout", [True, False])
def test_list_and_runtime_share_environment_version_expectation(tmp_path, monkeypatch, source_layout):
    from src.application import plugin_distribution, plugin_runtime

    (tmp_path / "VERSION").write_text("9.9.9")
    if source_layout:
        project = tmp_path / "plugins/portfolio-management/pyproject.toml"
        project.parent.mkdir(parents=True)
        project.write_text('[project]\nversion="0.1.0"\n')
    expected = "0.1.0" if source_layout else "9.9.9"
    monkeypatch.setattr(plugin_runtime, "code_root", lambda: tmp_path)
    monkeypatch.setattr(plugin_runtime.importlib.util, "find_spec", lambda _: object())
    monkeypatch.setattr(plugin_runtime.importlib.metadata, "version", lambda _: expected)
    row = next(row for row in plugin_distribution.list_plugins(repo_root=tmp_path)["plugins"] if row["id"] == "pm")
    assert row["compatible"] is True and row["status"] == "installed"
    assert plugin_runtime.plugin_installed("pm")
