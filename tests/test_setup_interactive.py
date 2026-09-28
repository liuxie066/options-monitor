from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from src.application.agent_tool_contracts import AgentToolError
from src.application.platform_profile import current_platform_profile
from src.interfaces.cli.setup_interactive import run_interactive_setup


REPO_ROOT = Path(__file__).resolve().parents[1]


def _prompts(*answers: str):
    values = iter(answers)
    return lambda: next(values)


@pytest.mark.parametrize("system", ["Darwin", "Linux"])
def test_setup_init_creates_selected_market_only_without_services(monkeypatch, tmp_path: Path, system: str, capsys) -> None:
    profile = current_platform_profile(system=system, home=tmp_path)
    monkeypatch.setattr("src.interfaces.cli.setup_interactive.current_platform_profile", lambda: profile)
    root = tmp_path / "runtime"
    check_calls = []

    def check(**kwargs):
        check_calls.append(kwargs)
        return {"summary": {"ok": True, "error_count": 0, "warning_count": 0}}

    result = run_interactive_setup(
        repo_root=REPO_ROOT,
        prompt_fn=_prompts("manual", str(root), "", "us", "lx", "12345678", "NVDA", "yes"),
        input_is_tty=lambda: True,
        check_fn=check,
    )

    assert result["markets"] == ["us"]
    assert result["broker_verified"] is False
    assert result["service_changed"] is False
    assert result["env_file"] == str(root / "options-monitor.env")
    assert check_calls[0]["runtime_root"] == root
    assert check_calls[0]["env_file"] == root / "options-monitor.env"
    assert "--runtime-root" in result["next_steps"][0]
    assert yaml.safe_load((root / "config.yaml").read_text(encoding="utf-8"))["assistant"]["enabled"] is False
    assert json.loads((root / "config.us.json").read_text(encoding="utf-8"))["_generated"]["market"] == "us"
    assert not (root / "config.hk.json").exists()
    prompts = capsys.readouterr().err
    assert "即将创建" in prompts
    assert "Wheel" in prompts and "Close Advice" in prompts


def test_setup_init_cancel_and_non_tty_never_create_config(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    with pytest.raises(AgentToolError, match="interactive terminal"):
        run_interactive_setup(repo_root=REPO_ROOT, input_is_tty=lambda: False)
    with pytest.raises(AgentToolError) as exc:
        run_interactive_setup(
            repo_root=REPO_ROOT,
            prompt_fn=_prompts("manual", str(root), "", "us", "lx", "12345678", "NVDA", "no"),
            input_is_tty=lambda: True,
        )
    assert exc.value.code == "CANCELLED"
    assert not (root / "config.yaml").exists()
    assert not (root / "config.us.json").exists()


def test_setup_init_rejects_existing_target_before_writing(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    root.mkdir()
    source = root / "config.yaml"
    source.write_text("owned by someone else\n", encoding="utf-8")

    with pytest.raises(AgentToolError, match="already exists"):
        run_interactive_setup(
            repo_root=REPO_ROOT,
            prompt_fn=_prompts("manual", str(root), "", "us", "lx", "12345678", "NVDA"),
            input_is_tty=lambda: True,
        )

    assert source.read_text(encoding="utf-8") == "owned by someone else\n"
    assert not (root / "config.us.json").exists()


def test_setup_init_rejects_symlinked_snapshot_parent(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "resolved").symlink_to(outside, target_is_directory=True)

    with pytest.raises(AgentToolError, match="symlink"):
        run_interactive_setup(
            repo_root=REPO_ROOT,
            prompt_fn=_prompts("manual", str(root), "", "us", "lx", "12345678", "NVDA"),
            input_is_tty=lambda: True,
        )

    assert not (outside / "config.assistant.json").exists()
    assert not (root / "config.yaml").exists()


def test_setup_init_rejects_env_runtime_conflict_before_writing(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    root.mkdir()
    env_file = root / "options-monitor.env"
    env_file.write_text(f"OM_RUNTIME_ROOT={tmp_path / 'other'}\n", encoding="utf-8")

    with pytest.raises(AgentToolError, match="conflicts"):
        run_interactive_setup(
            repo_root=REPO_ROOT,
            prompt_fn=_prompts("manual", str(root), str(env_file), "us", "lx", "12345678", "NVDA"),
            input_is_tty=lambda: True,
        )

    assert not (root / "config.yaml").exists()
