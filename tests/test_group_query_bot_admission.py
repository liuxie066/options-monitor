"""Fresh-process CLI and installed-distribution admission, without any real service."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

from src.application.plugin_runtime import PLUGINS, plugin_installed, require_plugin, validate_plugin_import
from src.application.secret_store import credential_spec

ROOT = Path(__file__).resolve().parents[1]


def _run(body):
    env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    env.update(OM_SECRET_BACKEND="env", PYTHONDONTWRITEBYTECODE="1")
    result = subprocess.run([sys.executable, "-B", "-c", body], cwd=ROOT, env=env,
                            text=True, capture_output=True, timeout=45)
    assert result.returncode == 0, result.stdout + result.stderr
    return result


def test_installed_group_plugin_uses_real_metadata_admission():
    assert PLUGINS["group-query-bot"] == {"distribution": "om-group-query-bot-plugin", "module": "om_group_query_bot", "source": "group-query-bot"}
    assert plugin_installed("group-query-bot")
    assert require_plugin("group-query-bot").__name__ == "om_group_query_bot"
    validate_plugin_import("group-query-bot")


def test_help_never_executes_the_plugin_package():
    _run('''
import importlib.abc, sys
class DenyPlugin(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, *args):
        if fullname.startswith("om_group_query_bot"):
            raise AssertionError("help imported optional plugin")
sys.meta_path.insert(0, DenyPlugin())
from src.interfaces.cli.main import main
for argv in [["--help"], ["group-query-bot", "--help"], ["group-query-bot", "preflight", "--help"]]:
    try:
        main(argv)
    except SystemExit as exc:
        assert exc.code == 0
assert not any(name.startswith("om_group_query_bot") for name in sys.modules)
''')


def test_missing_or_wrong_version_is_rejected_before_package_execution():
    for installed_version in (None, "999.0.0"):
        result = _run(f'''
import importlib.metadata, sys
original = importlib.metadata.version
def version(name):
    if name == "om-group-query-bot-plugin":
        value = {installed_version!r}
        if value is None:
            raise importlib.metadata.PackageNotFoundError(name)
        return value
    return original(name)
importlib.metadata.version = version
from src.interfaces.cli.main import main
assert main(["group-query-bot", "preflight", "--config", "not-read.json"]) != 0
assert not any(name.startswith("om_group_query_bot") for name in sys.modules)
''')
        assert "PLUGIN_UNAVAILABLE" in result.stdout + result.stderr


def test_real_cli_preview_has_no_ambient_config_secret_or_network_access(tmp_path):
    path = tmp_path / "preflight.json"
    data_dir = tmp_path / "unused-data"
    path.write_text(json.dumps({"instance_id": "admission", "data_dir": str(data_dir),
                               "feishu": {"app_id": "app_new",
                                          "app_secret_ref": "group_query_bot.feishu.app_secret",
                                          "allowed_chat_ids": ["chat_allowed"]}}))
    result = _run(f'''
import socket, urllib.request
from src.application import secret_store
from src.interfaces.cli import main as cli
def forbidden(*args, **kwargs):
    raise AssertionError("ambient dependency used")
secret_store.resolve_secret = forbidden
cli.command_environment = forbidden
socket.create_connection = forbidden
urllib.request.urlopen = forbidden
assert cli.main(["group-query-bot", "preflight", "--config", {str(path)!r}], discover_local=True) == 0
''')
    assert json.loads(result.stdout)["status"] == "live_not_run"
    assert not data_dir.exists()


def test_dedicated_credentials_have_no_legacy_fallback():
    expected = {
        "group_query_bot.feishu.app_secret": "om-group-query-bot-feishu-app-secret",
        "group_query_bot.llm.api_key": "om-group-query-bot-llm-api-key",
        "group_query_bot.query_api.token": "om-group-query-bot-query-api-token",
    }
    from src.infrastructure.secret_store.environment import EnvSecretProvider
    provider = EnvSecretProvider({"OM_FEISHU_BOT_APP_SECRET": "LOCAL_TEST_ONLY", "OM_LLM_API_KEY": "LOCAL_TEST_ONLY"})
    for logical_name, credential_id in expected.items():
        spec = credential_spec(logical_name)
        assert spec.systemd_credential_id == credential_id
        assert spec.legacy_env_names == ()
        assert provider.get(logical_name) is None
        assert provider.get(logical_name, legacy_env_name="OM_FEISHU_BOT_APP_SECRET") is None
        assert provider.status(logical_name).configured is False
