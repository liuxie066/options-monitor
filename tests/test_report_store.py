from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.application.reports.store import (
    ReportArtifact,
    create_run,
    finalize_run,
    get_run,
    list_runs,
    read_run_artifact,
    report_state_dir,
    write_artifacts,
)


def _now(offset_minutes: int = 0) -> datetime:
    return datetime(2026, 11, 12, 1, 0, tzinfo=timezone.utc) + timedelta(minutes=offset_minutes)


def test_run_store_roundtrip_and_history_order(tmp_path: Path) -> None:
    first = create_run(
        runtime_root=tmp_path,
        report_id="config_review",
        now=_now(0),
        command=["om", "report", "run", "config_review"],
    )
    records = write_artifacts(first, [ReportArtifact("report.md", "# first\n"), ReportArtifact("report.json", "{}\n")])
    finalize_run(first, status="succeeded", artifacts=records, summary={"symbol_count": 1}, finished_at=_now(1))

    second = create_run(
        runtime_root=tmp_path,
        report_id="config_review",
        now=_now(10),
        command=["om", "report", "run", "config_review"],
    )
    second_records = write_artifacts(second, [ReportArtifact("report.md", "# second\n")])
    finalize_run(second, status="succeeded", artifacts=second_records, summary={}, finished_at=_now(11))

    runs = list_runs(tmp_path, "config_review")
    assert [run["run_id"] for run in runs] == [second.run_id, first.run_id]
    assert all(run["status"] == "succeeded" for run in runs)
    assert str(report_state_dir(tmp_path, "config_review")) in runs[0]["run_dir"]

    loaded = get_run(tmp_path, "config_review", first.run_id)
    artifact = read_run_artifact(loaded)
    assert artifact.name == "report.md"
    assert artifact.content == "# first\n"
    assert artifact.record["sha256"]

    json_artifact = read_run_artifact(loaded, artifact_name="report.json")
    assert json_artifact.content == "{}\n"


def test_list_runs_skips_invalid_manifests_and_limits(tmp_path: Path) -> None:
    handle = create_run(runtime_root=tmp_path, report_id="config_review", now=_now(), command=["om"])
    finalize_run(handle, status="succeeded", artifacts=[], summary={}, finished_at=_now(1))
    broken = report_state_dir(tmp_path, "config_review") / "99999999T999999Z-broken"
    broken.mkdir()
    (broken / "run.json").write_text("{not json", encoding="utf-8")
    wrong_owner = report_state_dir(tmp_path, "config_review") / "99999999T999999Z-owner"
    wrong_owner.mkdir()
    (wrong_owner / "run.json").write_text('{"report_id": "daily_brief", "run_id": "x"}', encoding="utf-8")

    runs = list_runs(tmp_path, "config_review")
    assert [run["run_id"] for run in runs] == [handle.run_id]
    assert len(list_runs(tmp_path, "config_review", limit=1)) == 1


def test_missing_run_and_artifact_are_typed_errors(tmp_path: Path) -> None:
    from src.application.agent_tool_contracts import AgentToolError

    with pytest.raises(AgentToolError) as run_exc:
        get_run(tmp_path, "config_review", "20261112T010000Z-missing")
    assert run_exc.value.code == "REPORT_RUN_NOT_FOUND"

    handle = create_run(runtime_root=tmp_path, report_id="config_review", now=_now(), command=["om"])
    finalize_run(handle, status="succeeded", artifacts=[], summary={}, finished_at=_now(1))
    loaded = get_run(tmp_path, "config_review", handle.run_id)
    with pytest.raises(AgentToolError) as artifact_exc:
        read_run_artifact(loaded)
    assert artifact_exc.value.code == "REPORT_ARTIFACT_NOT_FOUND"


def test_failed_run_is_recorded_with_error(tmp_path: Path) -> None:
    handle = create_run(runtime_root=tmp_path, report_id="config_review", now=_now(), command=["om"])
    finalize_run(
        handle,
        status="failed",
        artifacts=[],
        summary=None,
        error={"code": "REPORT_CONFIG_INVALID", "message": "bad"},
        finished_at=_now(1),
    )
    loaded = get_run(tmp_path, "config_review", handle.run_id)
    assert loaded["status"] == "failed"
    assert loaded["error"] == {"code": "REPORT_CONFIG_INVALID", "message": "bad"}
    assert loaded["trigger"] == "manual"
    assert loaded["command"] == ["om"]
