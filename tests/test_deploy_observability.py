from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

from scripts.deploy_observability import append_event, classify_lag, load_state


def test_append_event_success_sync() -> None:
    with TemporaryDirectory() as td:
        state_path = Path(td) / "deploy_observability.json"
        ev = append_event(
            {
                "timestamp_utc": "2026-04-14T01:00:00+00:00",
                "operation": "deploy",
                "status": "success",
                "dev_commit": "abc1234",
                "prod_commit_before": "def5678",
                "prod_commit_after": "abc1234",
                "merged_to_target": False,
            },
            state_path=state_path,
        )
        assert ev["status"] == "success"
        assert ev["should_alert"] is True
        state = load_state(state_path)
        assert state["last_success"]["dev_commit"] == "abc1234"
        assert state["last_event"]["status"] == "success"


def test_append_event_publish_not_merged() -> None:
    with TemporaryDirectory() as td:
        state_path = Path(td) / "deploy_observability.json"
        ev = append_event(
            {
                "timestamp_utc": "2026-04-14T01:10:00+00:00",
                "operation": "publish",
                "status": "success",
                "dev_commit": "abc1234",
                "prod_commit_before": "aaa1111",
                "prod_commit_after": "bbb2222",
                "target_branch": "main",
                "merged_to_target": False,
            },
            state_path=state_path,
        )
        assert ev["operation"] == "publish"
        assert ev["merged_to_target"] is False
        state = load_state(state_path)
        assert state["last_event"]["merged_to_target"] is False


def test_append_event_failure_record_and_cooldown_suppress() -> None:
    with TemporaryDirectory() as td:
        state_path = Path(td) / "deploy_observability.json"
        first = append_event(
            {
                "timestamp_utc": "2026-04-14T01:20:00+00:00",
                "operation": "publish",
                "status": "failed",
                "dev_commit": "abc1234",
                "prod_commit_before": "aaa1111",
                "prod_commit_after": "aaa1111",
                "failure_reason": "git merge conflict on scripts/deploy_to_prod.py",
            },
            state_path=state_path,
            cooldown_seconds=1800,
        )
        second = append_event(
            {
                "timestamp_utc": "2026-04-14T01:25:00+00:00",
                "operation": "publish",
                "status": "failed",
                "dev_commit": "abc1234",
                "prod_commit_before": "aaa1111",
                "prod_commit_after": "aaa1111",
                "failure_reason": "git merge conflict on scripts/deploy_to_prod.py",
            },
            state_path=state_path,
            cooldown_seconds=1800,
        )
        assert first["should_alert"] is True
        assert second["should_alert"] is False
        assert second["suppressed"] is True
        state = load_state(state_path)
        fk = first["failure_key"]
        assert state["failure_aggregate"][fk]["count"] == 2
        assert state["last_failure"]["failure_reason"].startswith("git merge conflict")


def test_classify_lag_detects_prod_behind() -> None:
    lag = classify_lag(
        dev_head="abc1234",
        prod_head="def5678",
        prod_is_ancestor_of_dev=True,
        dev_is_ancestor_of_prod=False,
        ahead_by=3,
        behind_by=0,
    )
    assert lag["is_lagging"] is True
    assert lag["is_diverged"] is False
    assert lag["ahead_by"] == 3
