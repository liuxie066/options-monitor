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


def test_interactive_menu_groups_first_run_and_daily_tasks(capsys, monkeypatch) -> None:
    from src.interfaces.cli import journeys
    selected: list[list[str]] = []
    daily = []
    monkeypatch.setattr(journeys, "daily_management", lambda *args, **kwargs: daily.append(True) or 0)
    answers = iter(("1", "2", "3", "0"))
    assert interactive_home(lambda args: selected.append(args) or 0, input_fn=lambda _prompt: next(answers)) == 0
    assert selected == [["setup", "init"]]
    assert daily == [True]
    output = capsys.readouterr().out
    assert "首次安装" in output and "日常管理" in output
    assert "om bot configure" in output
    assert "om holdings configure" in output
    assert "高级与完整命令" in output


def test_daily_management_without_config_returns_to_menu(capsys, monkeypatch, tmp_path) -> None:
    from src.interfaces.cli import journeys
    monkeypatch.setattr(journeys, "resolve_yaml_config_path", lambda *args, **kwargs: tmp_path / "missing.yaml")
    selected: list[list[str]] = []
    answers = iter(("2", "0"))
    assert interactive_home(lambda args: selected.append(args) or 0, input_fn=lambda _prompt: next(answers)) == 0
    assert selected == []
    assert "请先运行 om setup init" in capsys.readouterr().out


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
