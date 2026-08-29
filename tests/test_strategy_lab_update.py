from __future__ import annotations

import pytest


def test_strategy_lab_update_wraps_shadow_replay_without_generic_experiment_api(
    monkeypatch,
    tmp_path,
) -> None:
    import src.application.strategy_lab as strategy_lab
    import src.application.strategy_lab.update as update_module

    monkeypatch.setattr(
        update_module,
        "run_shadow_replay_data_plan",
        lambda **_kwargs: {
            "schema_version": "shadow_replay_data_plan_run.v1",
            "summary": {
                "planned_count": 1,
                "executed_count": 0,
                "skipped_count": 0,
                "deferred_count": 0,
                "error_count": 0,
            },
            "status_before": {
                "summary": {"review_queue_count": 1},
                "review_queue": [{"dataset_id": "ready-dataset"}],
            },
            "actions": [{"action": "collect_marks"}],
            "safety": {},
        },
    )

    result = update_module.run_strategy_lab_update(
        repo_root=tmp_path,
        latest=True,
        write=True,
    )

    assert result["summary"]["status"] == "planned"
    assert result["selection"]["max_datasets"] == 1
    assert result["strategy_lab"]["next_action"] == "review_ready_shadow_replay_datasets"
    assert not hasattr(strategy_lab, "run_strategy_lab_experiment")


def test_strategy_lab_cli_exposes_only_top1_and_recorder_maintenance(capsys) -> None:
    from src.interfaces.cli.main import parse_args

    with pytest.raises(SystemExit) as exc_info:
        parse_args(["research", "strategy-lab", "--help"])

    assert exc_info.value.code == 0
    help_text = capsys.readouterr().out
    assert "{top1-loop,update}" in help_text
