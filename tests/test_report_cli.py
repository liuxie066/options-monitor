from __future__ import annotations

import json
from pathlib import Path

import pytest


def _configs_and_prices(tmp_path: Path) -> tuple[Path, Path]:
    configs = tmp_path / "configs"
    configs.mkdir()
    (configs / "user.hk.json").write_text(
        json.dumps(
            {
                "symbols": [
                    {
                        "symbol": "0700.HK",
                        "use": ["put_base", "call_base"],
                        "sell_put": {"max_strike": 450},
                        "sell_call": {"enabled": True, "min_strike": 550},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (configs / "user.us.json").write_text(
        json.dumps({"symbols": [{"symbol": "NVDA", "sell_put": {"max_strike": 150}}]}),
        encoding="utf-8",
    )
    prices = tmp_path / "prices.json"
    prices.write_text(
        json.dumps({"generated_at_utc": "2026-11-12T00:00:00Z", "prices": {"0700.HK": 403.2, "NVDA": 173.73}}),
        encoding="utf-8",
    )
    return configs, prices


def test_report_cli_records_explicit_candidate_evidence(tmp_path: Path, capsys) -> None:
    from datetime import datetime, timezone
    from src.application.reports.strike_metrics import unavailable_evidence
    from src.interfaces.cli.main import parse_args
    from src.interfaces.cli.report_ops import handle_report_command

    configs, prices = _configs_and_prices(tmp_path)
    evidence = unavailable_evidence(datetime.now(timezone.utc), "fixture_remote_unavailable")
    path = tmp_path / "remote-evidence.json"
    path.write_text(json.dumps(evidence))
    args = parse_args(["report", "run", "config_review", "--runtime-root", str(tmp_path),
                       "--configs-root", str(configs), "--prices-file", str(prices),
                       "--candidate-evidence-file", str(path), "--no-deliver", "--json"])
    assert handle_report_command(args, repo_base_fn=lambda: tmp_path) == 0
    result = json.loads(capsys.readouterr().out)
    stored = json.loads((Path(result["run_dir"]) / "candidate_evidence.json").read_text())
    assert stored["errors"] == [{"reason": "fixture_remote_unavailable"}]
    assert result["delivery"]["status"] == "skipped"
    assert result["summary"]["conclusion"] == "观察"
    receipt = json.loads((Path(result["run_dir"]) / "run.json").read_text())
    assert "--candidate-evidence-file" in receipt["command"]


def test_report_cli_parser() -> None:
    from src.interfaces.cli.main import parse_args

    listing = parse_args(["report", "list", "--json"])
    assert listing.report_command == "list" and listing.json is True

    run = parse_args(["report", "run", "config_review", "--dry-run", "--prices-file", "p.json", "--candidate-evidence-file", "e.json"])
    assert run.candidate_evidence_file == "e.json"
    assert run.report_id == "config_review"
    assert run.dry_run is True
    assert run.prices_file == "p.json"
    assert run.no_deliver is False

    no_deliver = parse_args(["report", "run", "config_review", "--no-deliver"])
    assert no_deliver.no_deliver is True

    history = parse_args(["report", "history", "config_review", "--limit", "3"])
    assert history.limit == 3

    show = parse_args(["report", "show", "config_review", "20261112T000000Z-abcd"])
    assert show.run_id == "20261112T000000Z-abcd"

    schedule = parse_args(["report", "schedule", "--format", "json"])
    assert schedule.format == "json"


def test_report_cli_run_history_show_list_and_schedule(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from src.interfaces.cli.main import main

    configs, prices = _configs_and_prices(tmp_path)
    runtime = tmp_path / "runtime"
    run_args = [
        "report",
        "run",
        "config_review",
        "--runtime-root",
        str(runtime),
        "--configs-root",
        str(configs),
        "--prices-file",
        str(prices),
        "--no-deliver",
        "--json",
    ]
    assert main(run_args) == 0
    run_payload = json.loads(capsys.readouterr().out)
    assert run_payload["ok"] is True
    assert run_payload["status"] == "succeeded"
    assert run_payload["summary"]["price_available"] == 2
    run_dir = Path(run_payload["run_dir"])
    assert (run_dir / "run.json").is_file()
    assert (run_dir / "report.md").is_file()
    assert (run_dir / "report.json").is_file()
    assert (run_dir / "candidate_evidence.json").is_file()
    assert json.loads((run_dir / "candidate_evidence.json").read_text())["schema_version"] == "config_review_candidate_evidence.v1"
    run_id = run_payload["run_id"]

    assert main(["report", "history", "config_review", "--runtime-root", str(runtime), "--json"]) == 0
    history_payload = json.loads(capsys.readouterr().out)
    assert [item["run_id"] for item in history_payload["runs"]] == [run_id]
    assert history_payload["runs"][0]["status"] == "succeeded"

    assert main(["report", "show", "config_review", run_id, "--runtime-root", str(runtime)]) == 0
    shown = capsys.readouterr().out
    assert "# 周度配置清单报告" in shown
    assert "403.20" in shown

    assert main(
        ["report", "list", "--runtime-root", str(runtime), "--configs-root", str(configs), "--json"]
    ) == 0
    list_payload = json.loads(capsys.readouterr().out)
    rows = {item["report_id"]: item for item in list_payload["reports"]}
    assert rows["config_review"]["migrated"] is True
    assert rows["config_review"]["last_run"]["status"] == "succeeded"
    assert rows["daily_brief"]["migrated"] is False
    assert rows["daily_brief"]["last_run"] is None
    assert rows["code_health"]["schedules"] == ["每日 09:17 CST"]

    assert main(["report", "schedule", "--format", "json"]) == 0
    schedule_payload = json.loads(capsys.readouterr().out)
    report_ids = [item["report_id"] for item in schedule_payload["reports"]]
    assert report_ids == ["daily_brief", "close_advice", "code_health", "code_entropy", "config_review"]
    config_review = schedule_payload["reports"][-1]
    assert config_review["entries"][0]["launchd"]["label"] == "com.options-monitor.report.config-review.weekly"


def test_report_cli_dry_run_does_not_create_history(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from src.interfaces.cli.main import main

    configs, prices = _configs_and_prices(tmp_path)
    runtime = tmp_path / "runtime"
    assert main(
        [
            "report",
            "run",
            "config_review",
            "--dry-run",
            "--runtime-root",
            str(runtime),
            "--configs-root",
            str(configs),
            "--prices-file",
            str(prices),
        ]
    ) == 0
    output = capsys.readouterr().out
    assert output.startswith("# 周度配置清单报告")
    assert not (runtime / "reports" / "config_review").exists()


def test_report_cli_legacy_run_is_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from src.interfaces.cli.main import main

    runtime = tmp_path / "runtime"
    assert main(["report", "run", "daily_brief", "--runtime-root", str(runtime)]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "REPORT_NOT_MIGRATED"
    assert "daily_decision_brief_service.py" in payload["error"]["hint"]


def test_report_cli_unknown_report(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from src.interfaces.cli.main import main

    assert main(["report", "history", "nope", "--runtime-root", str(tmp_path)]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["code"] == "REPORT_NOT_FOUND"
    assert "config_review" in payload["error"]["details"]["available"]
