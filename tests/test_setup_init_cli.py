from __future__ import annotations

from pathlib import Path
import json
import os
import subprocess
import sys

import pytest

from src.application.agent_tool_contracts import AgentToolError
from src.interfaces.cli.main import parse_args
from src.interfaces.cli.setup_ops import run_setup_init
from src.application.config_yaml_init import create_starter_config


def test_setup_init_requires_terminal_or_explicit_mode(tmp_path: Path) -> None:
    args = parse_args(["setup", "init", "--output-dir", str(tmp_path / "config")])
    with pytest.raises(AgentToolError, match="interactive terminal"):
        run_setup_init(args, repo_base_fn=lambda: tmp_path, input_is_tty=lambda: False, user_home=tmp_path / "home")
    assert not (tmp_path / "config").exists()


def test_setup_init_preview_and_cancel_leave_target_untouched(tmp_path: Path) -> None:
    target = tmp_path / "config"
    preview_args = parse_args(["setup", "init", "--output-dir", str(target), "--market", "us", "--dry-run"])
    preview, applied = run_setup_init(preview_args, repo_base_fn=lambda: tmp_path, input_is_tty=lambda: False, user_home=tmp_path / "home")
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
        user_home=tmp_path / "home",
    )
    assert not applied
    assert "已取消，未写入" in cancelled
    assert not target.exists()


def test_setup_init_preview_explains_higher_priority_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OM_RUNTIME_ROOT", str(tmp_path / "other"))
    args = parse_args(["setup", "init", "--output-dir", str(tmp_path / "runtime"), "--market", "us", "--dry-run"])
    preview, applied = run_setup_init(args, repo_base_fn=lambda: tmp_path,
                                      input_is_tty=lambda: False, user_home=tmp_path / "home")
    assert not applied
    assert "仍优先于新记录" in preview
    assert "富途账户 ID 尚未填写" in preview
    assert "默认标的" in preview
    assert not (tmp_path / "home").exists()

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
        user_home=tmp_path / "home",
    )
    assert applied
    assert (target / "config.yaml").is_file()
    assert (target / "config.us.json").is_file()
    assert (target / "resolved" / "config.assistant.json").is_file()
    assert not (target / "config.hk.json").exists()
    assert "sy:" not in (target / "config.yaml").read_text(encoding="utf-8")
    assert "已写入并回读文件" in output
    assert "运行目录已记住" in output
    assert (tmp_path / "home" / ".config" / "options-monitor" / "runtime-root").read_text() == str(target) + "\n"
    assert "om config build --source yaml --market us" in output
    from src.application.agent_tool_config import load_runtime_config
    from src.application.runtime_config_readiness import evaluate_runtime_config_readiness

    path, config = load_runtime_config(config_key="us", config_path=target / "config.us.json")
    readiness = evaluate_runtime_config_readiness(config, repo_root=tmp_path,
                                                  runtime_config_path=path, explicit_market="us", config_key="us")
    assert readiness["freshness"]["ok"] is True


def test_create_starter_success_has_no_unsafe_delete_hint(tmp_path: Path) -> None:
    target = tmp_path / "runtime"
    record = tmp_path / "home" / ".config" / "options-monitor" / "runtime-root"
    result = create_starter_config(
        repo_root=Path(__file__).resolve().parents[1],
        output_config_yaml_path=target / "config.yaml",
        runtime_output_dir=target,
        assistant_output_config_path=target / "resolved" / "config.assistant.json",
        markets=["us"],
        record_path=record,
    )
    assert result["write_applied"] is True
    assert result["rollback_hint"] is None

    env = dict(os.environ)
    env.pop("OM_RUNTIME_ROOT", None)
    env.pop("OM_ENV_FILE", None)
    env["HOME"] = str(tmp_path / "home")
    checked = subprocess.run(
        [sys.executable, "-m", "src.interfaces.cli.main", "setup", "check", "--market", "us", "--no-local-env-file"],
        cwd=Path(__file__).resolve().parents[1], env=env, capture_output=True, text=True,
    )
    data = json.loads(checked.stdout)["data"]
    assert checked.returncode == 2  # The starter account ID still needs editing.
    checks = {item["name"]: item for item in data["checks"]}
    assert checks["runtime_root"]["value"]["source"] == "user_record"
    assert checks["config.us"]["value"]["config_path"] == str(target / "config.us.json")


def test_setup_init_rejects_existing_config_without_overwriting(tmp_path: Path) -> None:
    target = tmp_path / "config"
    target.mkdir()
    source = target / "config.yaml"
    source.write_text("existing\n", encoding="utf-8")
    args = parse_args(["setup", "init", "--output-dir", str(target), "--dry-run"])
    with pytest.raises(AgentToolError, match="already exists"):
        run_setup_init(args, repo_base_fn=lambda: tmp_path, input_is_tty=lambda: False, user_home=tmp_path / "home")
    assert source.read_text(encoding="utf-8") == "existing\n"


def test_setup_init_race_preserves_other_file_and_cleans_own_files(monkeypatch, tmp_path: Path) -> None:
    import src.application.config_yaml_init as starter

    target = tmp_path / "runtime"
    record = tmp_path / "home" / ".config" / "options-monitor" / "runtime-root"
    real_link = starter.os.link

    def racing_link(source, destination):
        if Path(destination) == target / "config.yaml":
            Path(destination).write_text("created elsewhere\n", encoding="utf-8")
        return real_link(source, destination)

    monkeypatch.setattr(starter.os, "link", racing_link)
    with pytest.raises(AgentToolError, match="failed to create starter config"):
        create_starter_config(repo_root=Path(__file__).resolve().parents[1],
                              output_config_yaml_path=target / "config.yaml", runtime_output_dir=target,
                              assistant_output_config_path=target / "resolved" / "config.assistant.json",
                              markets=["us"], record_path=record)
    assert (target / "config.yaml").read_text() == "created elsewhere\n"
    assert not (target / "config.us.json").exists()
    assert not (target / "resolved" / "config.assistant.json").exists()
    assert not record.exists()


def test_setup_init_failure_preserves_modified_created_file(monkeypatch, tmp_path: Path) -> None:
    import src.application.config_yaml_init as starter

    target = tmp_path / "runtime"
    record = tmp_path / "home" / ".config" / "options-monitor" / "runtime-root"
    real_link = starter.os.link
    calls = 0

    def interrupted_link(source, destination):
        nonlocal calls
        calls += 1
        if calls == 2:
            (target / "config.us.json").write_text("modified after publish\n", encoding="utf-8")
            raise OSError("simulated write failure")
        return real_link(source, destination)

    monkeypatch.setattr(starter.os, "link", interrupted_link)
    with pytest.raises(AgentToolError) as captured:
        create_starter_config(repo_root=Path(__file__).resolve().parents[1],
                              output_config_yaml_path=target / "config.yaml", runtime_output_dir=target,
                              assistant_output_config_path=target / "resolved" / "config.assistant.json",
                              markets=["us"], record_path=record)
    assert captured.value.details["preserved"] == [str(target / "config.us.json")]
    assert (target / "config.us.json").read_text() == "modified after publish\n"
    assert not (target / "config.yaml").exists()
    assert not record.exists()
