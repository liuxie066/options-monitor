from __future__ import annotations

from pathlib import Path

import pytest

from src.application.agent_tool_contracts import AgentToolError
from src.interfaces.cli.main import parse_args
from src.interfaces.cli.setup_ops import run_setup_init


def test_setup_init_requires_terminal_or_explicit_mode(tmp_path: Path) -> None:
    args = parse_args(["setup", "init", "--output-dir", str(tmp_path / "config")])
    with pytest.raises(AgentToolError, match="interactive terminal"):
        run_setup_init(args, repo_base_fn=lambda: tmp_path, input_is_tty=lambda: False)
    assert not (tmp_path / "config").exists()


def test_setup_init_preview_and_cancel_leave_target_untouched(tmp_path: Path) -> None:
    target = tmp_path / "config"
    preview_args = parse_args(["setup", "init", "--output-dir", str(target), "--market", "us", "--dry-run"])
    preview, applied = run_setup_init(preview_args, repo_base_fn=lambda: tmp_path, input_is_tty=lambda: False)
    assert not applied
    assert "config.us.json" in preview
    assert "仅预览，未写入" in preview
    assert not target.exists()

    answers = iter(("", "us", "", "123456", "no"))
    interactive_args = parse_args(["setup", "init", "--output-dir", str(target)])
    cancelled, applied = run_setup_init(
        interactive_args,
        repo_base_fn=lambda: tmp_path,
        input_is_tty=lambda: True,
        input_fn=lambda _prompt: next(answers),
    )
    assert not applied
    assert "已取消，未写入" in cancelled
    assert not target.exists()


def test_setup_init_confirmed_writes_and_reads_back_starter(tmp_path: Path, capsys) -> None:
    target = tmp_path / "config"
    args = parse_args(["setup", "init", "--output-dir", str(target), "--market", "us"])
    answers = iter(("", "", "", "123456", "yes"))

    def answer(prompt: str) -> str:
        if prompt.startswith("确认写入"):
            assert str(target / "config.yaml") in capsys.readouterr().out
            assert not target.exists()
        return next(answers)

    output, applied = run_setup_init(
        args,
        repo_base_fn=lambda: tmp_path,
        input_is_tty=lambda: True,
        input_fn=answer,
    )
    assert applied
    assert (target / "config.yaml").is_file()
    assert (target / "config.us.json").is_file()
    assert (target / "resolved" / "config.assistant.json").is_file()
    assert not (target / "config.hk.json").exists()
    assert "sy:" not in (target / "config.yaml").read_text(encoding="utf-8")
    assert "已写入并回读文件" in output
    assert "export OM_RUNTIME_ROOT=" in output
    assert "om config build --source yaml --market us" in output


def test_setup_init_rejects_existing_config_without_overwriting(tmp_path: Path) -> None:
    target = tmp_path / "config"
    target.mkdir()
    source = target / "config.yaml"
    source.write_text("existing\n", encoding="utf-8")
    args = parse_args(["setup", "init", "--output-dir", str(target), "--dry-run"])
    with pytest.raises(AgentToolError, match="already exists"):
        run_setup_init(args, repo_base_fn=lambda: tmp_path, input_is_tty=lambda: False)
    assert source.read_text(encoding="utf-8") == "existing\n"
