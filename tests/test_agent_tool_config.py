from __future__ import annotations

from pathlib import Path


def test_resolve_runtime_config_path_prefers_runtime_root_env(monkeypatch, tmp_path: Path) -> None:
    from src.application.agent_tool_config import resolve_runtime_config_path

    runtime_root = tmp_path / "runtime"
    monkeypatch.setenv("OM_RUNTIME_ROOT", str(runtime_root))

    assert resolve_runtime_config_path(config_key="us") == (runtime_root / "config.us.json").resolve()
    assert resolve_runtime_config_path(config_key="hk") == (runtime_root / "config.hk.json").resolve()


def test_resolve_runtime_config_path_explicit_path_beats_runtime_root_env(monkeypatch, tmp_path: Path) -> None:
    from src.application.agent_tool_config import resolve_runtime_config_path

    explicit = tmp_path / "manual.json"
    monkeypatch.setenv("OM_RUNTIME_ROOT", str(tmp_path / "runtime"))

    assert resolve_runtime_config_path(config_key="us", config_path=explicit) == explicit.resolve()
