from __future__ import annotations

import json

from src.interfaces.cli import main as cli
from src.interfaces.cli.home import interactive_home, render_credential_readiness


def test_noninteractive_home_shows_global_command_and_advanced_entry(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
    assert cli.main([]) == 0
    output = capsys.readouterr().out
    assert "首次安装" in output
    assert "日常管理" in output
    assert "om setup check --format text" in output
    assert "富途 OpenAPI/OpenD" in output
    assert "通知通道" in output
    assert "Bot LLM" in output
    assert "om symbols list" in output
    assert "om-agent spec" in output
    assert "om help all" in output
    assert "om config init" not in output
    assert "om service --help" in output
    assert "./om" not in output


def test_root_help_is_task_based_and_full_help_remains_available(capsys) -> None:
    assert cli.main(["--help"]) == 0
    guide = capsys.readouterr().out
    assert "首次安装" in guide and "日常管理" in guide
    assert "{healthcheck,doctor" not in guide

    assert cli.main(["help", "all"]) == 0
    full = capsys.readouterr().out
    assert full.startswith("usage: om ")
    assert "{healthcheck,doctor" in full
    assert "trade-events" in full


def test_interactive_menu_groups_first_run_and_daily_tasks(capsys) -> None:
    selected: list[list[str]] = []
    answers = iter(("1", "1", "2", "3", "4", "5", "6", "7", "0", "2", "1", "us", "2", "3", "hk", "4", "hk", "0", "3", "0"))
    assert interactive_home(lambda args: selected.append(args) or 0, input_fn=lambda _prompt: next(answers)) == 0
    assert selected == [
        ["setup", "init"],
        ["settings", "doctor", "--format", "text"],
        ["secrets", "status", "--format", "text"],
        ["setup", "check", "--format", "text"],
        ["status", "--config-key", "us"],
        ["daily-brief", "latest"],
        ["doctor", "--config-key", "hk"],
        ["symbols", "list", "--market", "hk"],
    ]
    output = capsys.readouterr().out
    assert "首次安装" in output and "日常管理" in output
    assert "OM 不负责启动 OpenD" in output
    assert "它不证明消息已送达" in output
    assert "检查不调用模型" in output
    assert "高级与完整命令" in output


def test_interactive_symbol_list_requires_explicit_market(capsys) -> None:
    selected: list[list[str]] = []
    answers = iter(("2", "4", "invalid", "0", "0"))
    assert interactive_home(lambda args: selected.append(args) or 0, input_fn=lambda _prompt: next(answers)) == 0
    assert selected == []
    assert "请输入 us 或 hk" in capsys.readouterr().out


def test_setup_text_shows_next_steps_without_changing_json_default(monkeypatch, capsys) -> None:
    report = {
        "summary": {"ok": False, "error_count": 1, "warning_count": 0},
        "checks": [{"name": "config.us", "status": "error", "message": "missing", "hint": "om config init --dry-run"}],
        "next_steps": ["om config init --dry-run"],
    }
    monkeypatch.setattr(cli, "run_setup_check", lambda **_kwargs: report)
    assert cli.main(["setup", "check", "--format", "text"]) == 2
    output = capsys.readouterr().out
    assert "config.us" in output
    assert "om config init --dry-run" in output
    assert "下一步" in output

    assert cli.main(["setup", "check"]) == 2
    assert json.loads(capsys.readouterr().out)["data"] == report


def test_settings_and_secret_text_are_redacted(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        cli,
        "diagnose_effective_settings",
        lambda **_kwargs: {
            "summary": {"ok": True, "error_count": 0, "warning_count": 1},
            "checks": [{"name": "env_file", "status": "warn", "message": "missing"}],
        },
    )
    assert cli.main(["settings", "doctor", "--format", "text"]) == 0
    assert "env_file" in capsys.readouterr().out

    monkeypatch.setattr(
        cli,
        "read_credential_readiness",
        lambda _args: {
            "summary": {"backend": "keychain"},
            "credentials": [{"logical_name": "llm.deepseek.api_key", "configured": True, "source": "keychain"}],
        },
    )
    def forbidden_write_path(_args):
        raise AssertionError("write path called")

    monkeypatch.setattr(cli, "run_store_command", forbidden_write_path)
    assert cli.main(["secrets", "status", "--format", "text"]) == 0
    output = capsys.readouterr().out
    assert "llm.deepseek.api_key" in output
    assert "不显示值" in output
    assert "om secrets set" in output


def test_secret_text_ignores_unexpected_value_field() -> None:
    output = render_credential_readiness(
        {
            "summary": {"backend": "keychain"},
            "credentials": [{"logical_name": "llm.deepseek.api_key", "configured": True, "value": "do-not-print"}],
        }
    )
    assert "do-not-print" not in output
