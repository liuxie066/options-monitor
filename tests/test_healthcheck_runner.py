from __future__ import annotations

import json
from pathlib import Path


def test_healthcheck_runner_returns_structured_result(monkeypatch, tmp_path: Path) -> None:
    from src.application import healthcheck_runner as runner

    config_path = tmp_path / "config.us.json"
    data_path = tmp_path / "portfolio.feishu.json"
    cron_path = tmp_path / "jobs.json"
    config_path.write_text(
        json.dumps(
            {
                "accounts": ["lx"],
                "portfolio": {"data_config": str(data_path)},
            }
        ),
        encoding="utf-8",
    )
    data_path.write_text(
        json.dumps(
            {
                "feishu": {
                    "app_id": "app",
                    "app_secret": "secret",
                    "tables": {
                        "holdings": "hold_app/hold_tbl",
                        "option_positions": "opt_app/opt_tbl",
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    cron_path.write_text(
        json.dumps({"jobs": [{"name": "options-monitor auto tick", "state": {"lastRunAtMs": 1, "lastRunStatus": "ok"}}]}),
        encoding="utf-8",
    )

    monkeypatch.setattr(runner, "validate_config", lambda _cfg: None)
    monkeypatch.setattr(runner, "get_tenant_access_token", lambda _app_id, _app_secret: "token")

    def _fields(_token: str, _app: str, table: str) -> list[dict[str, str]]:
        if table == "hold_tbl":
            names = sorted(runner.REQUIRED_HOLDINGS_FIELDS | {"broker"})
        else:
            names = sorted(runner.REQUIRED_OPTION_POSITION_FIELDS)
        return [{"field_name": name} for name in names]

    scheduler_calls: list[dict[str, Path]] = []

    def _run_scheduler(*, config: Path, state: Path, jsonl: bool, base_dir: Path) -> None:
        assert jsonl is True
        assert base_dir == tmp_path
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text("{}\n", encoding="utf-8")
        scheduler_calls.append({"config": config, "state": state})

    monkeypatch.setattr(runner, "bitable_fields", _fields)
    monkeypatch.setattr(runner, "run_scheduler", _run_scheduler)

    result = runner.run_healthcheck_runner(config=config_path, base=tmp_path, cron_path=cron_path)

    assert result["ok"] is True
    assert result["accounts"] == ["lx"]
    assert result["errors"] == []
    assert result["warnings"] == []
    assert [item["name"] for item in result["checks"]] == [
        "config_validation",
        "feishu_schema",
        "scheduler_decision",
        "cron_state",
    ]
    assert scheduler_calls
    assert scheduler_calls[0]["config"].name == "healthcheck_config.lx.json"


def test_healthcheck_report_keeps_legacy_human_sections() -> None:
    from src.application.healthcheck_runner import format_healthcheck_report

    report = format_healthcheck_report(
        {
            "utc": "2026-05-11T00:00:00+00:00",
            "errors": ["bad config"],
            "warnings": ["cron missing"],
        }
    )

    assert "# options-monitor healthcheck" in report
    assert "## CRITICAL" in report
    assert "- bad config" in report
    assert "## WARN" in report
    assert "- cron missing" in report
