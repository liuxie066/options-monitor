"""Model parameters stay lightweight; configuration readers retain their contracts."""
from __future__ import annotations

from pathlib import Path
import subprocess
import sys

import pytest

from src.application.bot import model_config


def test_model_settings_do_not_load_runtime_configuration(tmp_path):
    root = Path(__file__).resolve().parents[1]
    code = '''
import importlib.abc, sys
sys.path.insert(0, sys.argv[1])
class DenyConfiguration(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, *args):
        if fullname in {"src.application.config_validator", "src.application.config_yaml", "pandas"}:
            raise AssertionError("model parameters loaded runtime configuration: " + fullname)
sys.meta_path.insert(0, DenyConfiguration())
from src.application.bot.model_config import ModelSettings
settings = ModelSettings.from_config({
    "provider": "deepseek", "model": "test-model", "context_window_tokens": 64000,
    "max_output_tokens": 2048,
})
assert settings.process_payload()["max_output_tokens"] == 2048
assert settings.output_reservation_tokens == 2048
assert "pandas" not in sys.modules
'''
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", code, str(root)], cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin", "OM_SECRET_BACKEND": "env"},
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("content,required,error", [
    (None, False, None),
    (None, True, "bot_config_not_found"),
    ("{", True, "invalid_bot_config"),
    ("[]", True, "invalid_bot_config"),
    ('{"bot":{"enabled":true,"read_markets":["us"]}}', True, None),
])
def test_explicit_config_read_results(tmp_path, content, required, error):
    path = tmp_path / "config.bot.json"
    if content is not None:
        path.write_text(content)
    payload, actual = model_config._load_bot_config(
        config_path=path, repo_root=tmp_path, require_config=required,
    )
    assert actual == error
    if content is not None and error is None:
        assert payload == {"bot": {"enabled": True, "read_markets": ["us"]}}
    else:
        assert payload is None


def test_default_config_path_keeps_canonical_resolver(tmp_path, monkeypatch):
    from src.application import config_yaml

    selected = tmp_path / "isolated-runtime" / "config.bot.json"
    calls = []

    def resolve(*, repo_root):
        calls.append(repo_root)
        return selected

    monkeypatch.setattr(config_yaml, "default_yaml_bot_config_path", resolve)
    assert model_config._bot_config_path(config_path=None, repo_root=tmp_path) == selected
    assert calls == [tmp_path]
    local = tmp_path / "config.bot.json"
    local.write_text("{}")
    assert model_config._bot_config_path(config_path=None, repo_root=tmp_path) == local
    explicit = tmp_path / "explicit.json"
    assert model_config._bot_config_path(config_path=explicit, repo_root=tmp_path) == explicit
    assert calls == [tmp_path]
