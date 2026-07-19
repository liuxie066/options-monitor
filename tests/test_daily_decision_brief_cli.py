from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path


def test_daily_brief_cli_parser_supports_latest_day_revision_and_json() -> None:
    from src.interfaces.cli.main import parse_args

    latest = parse_args(["daily-brief", "latest", "--account", "lx"])
    assert latest.daily_brief_command == "latest"
    assert latest.market == "US"
    assert latest.json is False

    revision = parse_args(
        [
            "daily-brief",
            "day",
            "--account",
            "lx",
            "--market",
            "US",
            "--date",
            "2026-07-19",
            "--revision",
            "2",
            "--json",
        ]
    )
    assert revision.daily_brief_command == "day"
    assert revision.market_trading_date == "2026-07-19"
    assert revision.revision == 2
    assert revision.json is True


def test_daily_brief_cli_outputs_markdown_and_json(monkeypatch, capsys, tmp_path: Path) -> None:
    from src.interfaces.cli import daily_brief_ops

    data = {
        "schema_version": "daily_decision_brief_read.output.v1",
        "available": True,
        "rendered_markdown": "# 每日决策简报\n- ok",
    }
    monkeypatch.setattr(daily_brief_ops, "read_daily_brief_view", lambda **_kwargs: data)

    markdown_args = Namespace(
        daily_brief_command="latest",
        account="lx",
        market="US",
        json=False,
    )
    assert daily_brief_ops.handle_daily_brief_command(markdown_args, repo_base_fn=lambda: tmp_path) == 0
    assert capsys.readouterr().out == "# 每日决策简报\n- ok\n"

    json_args = Namespace(**{**vars(markdown_args), "json": True})
    assert daily_brief_ops.handle_daily_brief_command(json_args, repo_base_fn=lambda: tmp_path) == 0
    assert json.loads(capsys.readouterr().out)["available"] is True


def test_daily_brief_cli_rejects_negative_revision(tmp_path: Path) -> None:
    from src.application.agent_tool_contracts import AgentToolError
    from src.interfaces.cli import daily_brief_ops

    args = Namespace(
        daily_brief_command="day",
        account="lx",
        market="US",
        market_trading_date="2026-07-19",
        revision=-1,
        json=True,
    )
    try:
        daily_brief_ops.handle_daily_brief_command(args, repo_base_fn=lambda: tmp_path)
        raise AssertionError("expected AgentToolError")
    except AgentToolError as exc:
        assert exc.code == "INPUT_ERROR"
        assert "revision must be non-negative" in str(exc)


def test_daily_brief_main_renders_input_errors_without_traceback(capsys) -> None:
    from src.interfaces.cli.main import main

    exit_code = main(
        [
            "daily-brief",
            "day",
            "--account",
            "lx",
            "--market",
            "US",
            "--date",
            "2026-07-19",
            "--revision",
            "-1",
        ]
    )
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert exit_code == 2
    assert payload["ok"] is False
    assert payload["error"]["code"] == "INPUT_ERROR"
    assert "revision must be non-negative" in payload["error"]["message"]
    assert captured.err == ""
