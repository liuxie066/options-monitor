from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path


def _brief(*, run_id: str) -> dict:
    return {
        "market": "US",
        "market_trading_date": "2026-07-19",
        "account": "lx",
        "revision": 999,
        "run_id": run_id,
        "generated_at_utc": "2026-07-19T13:40:00+00:00",
        "data_as_of_utc": "2026-07-19T13:39:00+00:00",
        "valid_until_utc": "2026-07-20T20:00:00+00:00",
        "status": "ready",
        "actionability": "live_actionable",
        "strategy_summary": run_id,
        "actions": [],
        "positions": [],
        "capacity": {},
        "candidates": {"sell_put": [], "covered_call": [], "combo_yield": []},
        "rejections": {},
        "events": [],
        "data_gaps": [],
        "source_artifacts": [],
    }


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

    inspect = parse_args(["daily-brief", "delivery-inspect", "--account", "lx", "--market", "HK"])
    assert inspect.daily_brief_command == "delivery-inspect"

    migrate = parse_args(["daily-brief", "delivery-migrate", "--account", "lx", "--market", "HK"])
    assert migrate.confirm is False
    assert migrate.dry_run is False

    confirmed = parse_args(
        ["daily-brief", "delivery-migrate", "--account", "lx", "--market", "HK", "--confirm"]
    )
    assert confirmed.confirm is True


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


def test_daily_brief_cli_reads_env_runtime_root_then_repo_fallback(monkeypatch, capsys, tmp_path: Path) -> None:
    from src.application.daily_decision_brief_repository import prepare_daily_decision_brief
    from src.interfaces.cli import daily_brief_ops

    repo_root = tmp_path / "repo"
    runtime_root = tmp_path / "runtime"
    prepare_daily_decision_brief(base=repo_root, brief=_brief(run_id="repo-r0"))
    prepare_daily_decision_brief(base=runtime_root, brief=_brief(run_id="runtime-r0"))
    runtime_r1 = prepare_daily_decision_brief(base=runtime_root, brief=_brief(run_id="runtime-r1"))

    args = Namespace(
        daily_brief_command="day",
        account="lx",
        market="US",
        market_trading_date="2026-07-19",
        revision=runtime_r1["brief"]["revision"],
        json=True,
    )
    monkeypatch.setenv("OM_RUNTIME_ROOT", str(runtime_root))
    monkeypatch.delenv("OM_ENV_FILE", raising=False)

    assert daily_brief_ops.handle_daily_brief_command(args, repo_base_fn=lambda: repo_root) == 0
    runtime_payload = json.loads(capsys.readouterr().out)
    assert runtime_payload["brief"]["revision"] == 1
    assert runtime_payload["brief"]["run_id"] == "runtime-r1"

    monkeypatch.delenv("OM_RUNTIME_ROOT")
    args.revision = 0
    assert daily_brief_ops.handle_daily_brief_command(args, repo_base_fn=lambda: repo_root) == 0
    repo_payload = json.loads(capsys.readouterr().out)
    assert repo_payload["brief"]["revision"] == 0
    assert repo_payload["brief"]["run_id"] == "repo-r0"


def test_daily_brief_delivery_cli_inspects_and_migrates_v1_pointer(
    monkeypatch,
    capsys,
    tmp_path: Path,
) -> None:
    from datetime import datetime, timezone

    from src.application.daily_decision_brief_repository import (
        confirm_daily_decision_brief_delivery,
        prepare_daily_decision_brief,
    )
    from src.interfaces.cli.main import main

    lifecycle = prepare_daily_decision_brief(base=tmp_path, brief=_brief(run_id="legacy-run"))
    brief = lifecycle["brief"]
    confirm_daily_decision_brief_delivery(
        base=tmp_path,
        market="US",
        market_trading_date=brief["market_trading_date"],
        account="lx",
        revision=brief["revision"],
        delivery_kind=lifecycle["delivery_kind"],
        delivery_key=lifecycle["delivery_key"],
        brief_digest=lifecycle["current_brief_digest"],
        confirmed_at_utc=datetime(2026, 7, 19, 13, 41, tzinfo=timezone.utc),
    )
    monkeypatch.setenv("OM_RUNTIME_ROOT", str(tmp_path))
    monkeypatch.delenv("OM_ENV_FILE", raising=False)

    assert main(["daily-brief", "delivery-inspect", "--account", "lx", "--market", "US"]) == 0
    inspected = json.loads(capsys.readouterr().out)
    assert inspected["reason"] == "migration_available"
    assert inspected["migration"]["target_schema_version"] == "daily_decision_brief_delivery.v2"

    assert main(["daily-brief", "delivery-migrate", "--account", "lx", "--market", "US"]) == 0
    dry_run = json.loads(capsys.readouterr().out)
    assert dry_run["dry_run"] is True
    assert dry_run["write_applied"] is False

    assert main(
        ["daily-brief", "delivery-migrate", "--account", "lx", "--market", "US", "--confirm"]
    ) == 0
    migrated = json.loads(capsys.readouterr().out)
    assert migrated["write_applied"] is True
    assert migrated["backup_path"].endswith("Z")


def test_daily_brief_delivery_cli_reports_invalid_state_without_traceback(
    monkeypatch,
    capsys,
    tmp_path: Path,
) -> None:
    from src.interfaces.cli.main import main

    state_dir = tmp_path / "output_accounts" / "lx" / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "daily_decision_brief.US.delivery.json").write_text('{"bad": true}', encoding="utf-8")
    monkeypatch.setenv("OM_RUNTIME_ROOT", str(tmp_path))
    monkeypatch.delenv("OM_ENV_FILE", raising=False)

    assert main(["daily-brief", "delivery-inspect", "--account", "lx", "--market", "US"]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["code"] == "STATE_INVALID"
