from __future__ import annotations

import json
from pathlib import Path


def test_code_health_probe_reads_last_run(tmp_path: Path) -> None:
    from src.application.reports.legacy import observe_legacy_last_run
    from src.application.reports.registry import report_definition

    state = tmp_path / "Library" / "Application Support" / "options-monitor" / "code-health-review"
    state.mkdir(parents=True)
    (state / "last-run.json").write_text(
        json.dumps(
            {
                "started_at": "2026-11-11T08:05:03+00:00",
                "finished_at": "2026-11-11T08:06:18+00:00",
                "status": "smoke_passed",
                "mode": "smoke",
            }
        ),
        encoding="utf-8",
    )
    observation = observe_legacy_last_run(report_definition("code_health"), runtime_root=tmp_path, home=tmp_path)
    assert observation["at_utc"] == "2026-11-11T08:06:18+00:00"
    assert observation["status"] == "smoke_passed"
    assert observation["detail"] == "smoke"


def test_code_entropy_probe_reads_last_scorecard_line(tmp_path: Path) -> None:
    from src.application.reports.legacy import observe_legacy_last_run
    from src.application.reports.registry import report_definition

    state = tmp_path / "Library" / "Application Support" / "options-monitor" / "code-entropy-review"
    state.mkdir(parents=True)
    lines = [
        {"run_id": "a", "started_at": "2026-11-10T00:00:00Z", "status": "complete", "mode": "daily"},
        {"run_id": "b", "started_at": "2026-11-11T00:00:00Z", "status": "failed", "mode": "weekly", "main_sha": "abc"},
        "{truncated",
    ]
    (state / "scorecards.jsonl").write_text("\n".join(json.dumps(item) if isinstance(item, dict) else item for item in lines) + "\n", encoding="utf-8")
    observation = observe_legacy_last_run(report_definition("code_entropy"), runtime_root=tmp_path, home=tmp_path)
    assert observation["at_utc"] == "2026-11-11T00:00:00Z"
    assert observation["status"] == "failed"
    assert "weekly" in observation["detail"]


def test_daily_brief_probe_picks_newest_current_brief(tmp_path: Path) -> None:
    from src.application.reports.legacy import observe_legacy_last_run
    from src.application.reports.registry import report_definition

    lx = tmp_path / "output_accounts" / "lx" / "state"
    sy = tmp_path / "output_accounts" / "sy" / "state"
    lx.mkdir(parents=True)
    sy.mkdir(parents=True)
    (lx / "daily_decision_brief.HK.current.json").write_text(
        json.dumps({"market": "HK", "account": "lx", "generated_at_utc": "2026-11-11T07:55:03Z", "status": "ready"}),
        encoding="utf-8",
    )
    (sy / "daily_decision_brief.US.current.json").write_text(
        json.dumps({"market": "US", "account": "sy", "generated_at_utc": "2026-11-11T17:35:47Z", "status": "degraded"}),
        encoding="utf-8",
    )
    observation = observe_legacy_last_run(report_definition("daily_brief"), runtime_root=tmp_path, home=tmp_path)
    assert observation["at_utc"] == "2026-11-11T17:35:47Z"
    assert observation["status"] == "degraded"
    assert "market=US" in observation["detail"]


def test_close_advice_probe_reads_newest_manifest(tmp_path: Path) -> None:
    from src.application.reports.legacy import observe_legacy_last_run
    from src.application.reports.registry import report_definition

    lx = tmp_path / "output_accounts" / "lx" / "reports"
    sy = tmp_path / "output_accounts" / "sy" / "reports"
    lx.mkdir(parents=True)
    sy.mkdir(parents=True)
    (lx / "close_advice.manifest.json").write_text(
        json.dumps({"generated_at_utc": "2026-11-11T07:59:59Z", "status": "success", "row_count": 3, "accounts": ["lx"]}),
        encoding="utf-8",
    )
    (sy / "close_advice.manifest.json").write_text(
        json.dumps({"generated_at_utc": "2026-11-11T08:10:00Z", "status": "failed", "row_count": 1, "accounts": ["sy"]}),
        encoding="utf-8",
    )
    observation = observe_legacy_last_run(report_definition("close_advice"), runtime_root=tmp_path, home=tmp_path)
    assert observation["at_utc"] == "2026-11-11T08:10:00Z"
    assert observation["status"] == "failed"
    assert "accounts=sy" in observation["detail"]


def test_missing_legacy_state_returns_none(tmp_path: Path) -> None:
    from src.application.reports.legacy import observe_legacy_last_run
    from src.application.reports.registry import report_definition

    assert observe_legacy_last_run(report_definition("code_health"), runtime_root=tmp_path, home=tmp_path) is None
    assert observe_legacy_last_run(report_definition("code_entropy"), runtime_root=tmp_path, home=tmp_path) is None
    assert observe_legacy_last_run(report_definition("daily_brief"), runtime_root=tmp_path, home=tmp_path) is None
    assert observe_legacy_last_run(report_definition("close_advice"), runtime_root=tmp_path, home=tmp_path) is None
