from __future__ import annotations

import json
from pathlib import Path

import pytest


def _runtime_config(*, market: str) -> dict:
    return {
        "_generated": {
            "schema_version": "1.0",
            "generator": "options-monitor",
            "source_format": "yaml",
            "market": market,
        },
        "_resolved": {
            "source_format": "yaml",
            "market": market,
            "runtime_schema": "config-json-v1",
        },
        "accounts": ["lx"],
        "schedule_hk": {
            "enabled": True,
            "timezone": "Asia/Hong_Kong",
            "cron_interval_min": 10,
            "run_window": {"start": "09:30", "end": "16:00", "breaks": []},
            "run_points": {"start_plus_min": 10, "hourly_minute": 0, "end_minus_min": 10},
        },
    }


def test_scheduler_status_default_matches_production_runtime_and_hk_selection(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from src.application.agent_tools import config as config_tools
    from src.application.tool_execution import execute_tool

    repo_root = tmp_path / "repo"
    runtime_root = tmp_path / "runtime"
    repo_state = repo_root / "output_shared" / "state"
    runtime_state = runtime_root / "output_shared" / "state"
    repo_state.mkdir(parents=True)
    runtime_state.mkdir(parents=True)
    config_path = tmp_path / "config.hk.json"
    config_path.write_text(json.dumps(_runtime_config(market="hk")), encoding="utf-8")
    (repo_state / "scheduler_state_hk.json").write_text(
        json.dumps({"last_run_utc_by_account": {"lx": "2026-01-01T00:00:00+00:00"}}),
        encoding="utf-8",
    )
    (runtime_state / "scheduler_state.json").write_text("{}", encoding="utf-8")
    selected_state = runtime_state / "scheduler_state_hk.json"
    selected_state.write_text(
        json.dumps({"last_run_utc_by_account": {"lx": "2026-09-11T01:40:00+00:00"}}),
        encoding="utf-8",
    )
    original = selected_state.read_bytes()

    monkeypatch.setattr(config_tools, "repo_base", lambda: repo_root)
    monkeypatch.setenv("OM_RUNTIME_ROOT", str(runtime_root))
    result = execute_tool(
        "scheduler_status",
        {"config_path": str(config_path), "account": "lx"},
    )

    assert result["ok"] is True
    assert result["data"]["filters"]["market"] == "hk"
    assert result["data"]["schedule"] == {
        "key": "schedule_hk",
        "selection": "production_default",
        "status": "available",
        "enabled": True,
    }
    assert result["data"]["state"]["selection"] == "production_default"
    assert result["data"]["state"]["last_run_utc_for_account"] == "2026-09-11T01:40:00+00:00"
    assert result["meta"]["state_path"] == ".../scheduler_state_hk.json"
    assert selected_state.read_bytes() == original


def test_scheduler_status_distinguishes_state_and_account_availability(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from src.application.tool_execution import execute_tool

    config_path = tmp_path / "config.hk.json"
    config_path.write_text(json.dumps(_runtime_config(market="hk")), encoding="utf-8")
    states = tmp_path / "states"
    states.mkdir()
    corrupt = states / "corrupt.json"
    corrupt.write_text("{", encoding="utf-8")
    unreadable = states / "directory"
    unreadable.mkdir()
    empty = states / "empty.json"
    empty.write_text("{}", encoding="utf-8")
    invalid_record = states / "invalid-record.json"
    invalid_record.write_text(
        json.dumps({"last_run_utc_by_account": {"lx": "not-a-time"}}),
        encoding="utf-8",
    )

    missing_result = execute_tool(
        "scheduler_status",
        {"config_path": str(config_path), "state": str(states / "missing.json"), "account": "lx"},
    )
    corrupt_result = execute_tool(
        "scheduler_status",
        {"config_path": str(config_path), "state": str(corrupt), "account": "lx"},
    )
    unreadable_result = execute_tool(
        "scheduler_status",
        {"config_path": str(config_path), "state": str(unreadable), "account": "lx"},
    )
    empty_result = execute_tool(
        "scheduler_status",
        {"config_path": str(config_path), "state": str(empty), "account": "lx"},
    )
    no_account_result = execute_tool(
        "scheduler_status",
        {"config_path": str(config_path), "state": str(empty)},
    )
    invalid_record_result = execute_tool(
        "scheduler_status",
        {"config_path": str(config_path), "state": str(invalid_record), "account": "lx"},
    )

    assert missing_result["data"]["state"]["status"] == "missing"
    assert missing_result["data"]["state"]["account_record_status"] == "unknown"
    assert missing_result["data"]["decision"]["should_run_scan"] is None
    assert corrupt_result["data"]["state"]["status"] == "corrupt"
    assert unreadable_result["data"]["state"]["status"] == "unreadable"
    assert empty_result["data"]["state"]["status"] == "available"
    assert empty_result["data"]["state"]["empty"] is True
    assert empty_result["data"]["state"]["account_record_status"] == "absent"
    assert no_account_result["data"]["state"]["account_record_status"] == "not_selected"
    assert invalid_record_result["data"]["state"]["status"] == "corrupt"
    assert invalid_record_result["data"]["decision"]["reason"] == "state_corrupt"


def test_scheduler_status_labels_explicit_and_force_preview(
    tmp_path: Path,
) -> None:
    from src.application.tool_execution import execute_tool

    config = _runtime_config(market="hk")
    config["bad_schedule"] = []
    config_path = tmp_path / "config.hk.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    state_path = tmp_path / "state.json"
    state_path.write_text("{}", encoding="utf-8")

    missing = execute_tool(
        "scheduler_status",
        {
            "config_path": str(config_path),
            "state": str(state_path),
            "schedule_key": "missing_schedule",
        },
    )
    invalid = execute_tool(
        "scheduler_status",
        {
            "config_path": str(config_path),
            "state": str(state_path),
            "schedule_key": "bad_schedule",
        },
    )
    forced = execute_tool(
        "scheduler_status",
        {
            "config_path": str(config_path),
            "state": str(state_path),
            "schedule_key": "schedule_hk",
            "force": True,
        },
    )
    state_dir = tmp_path / "state-dir"
    state_dir.mkdir()
    (state_dir / "scheduler_state_hk.json").write_text("{}", encoding="utf-8")
    state_dir_selected = execute_tool(
        "scheduler_status",
        {"config_path": str(config_path), "state_dir": str(state_dir)},
    )

    assert missing["data"]["schedule"]["selection"] == "explicit"
    assert missing["data"]["schedule"]["status"] == "missing"
    assert missing["data"]["decision"]["status"] == "unknown"
    assert invalid["data"]["schedule"]["status"] == "invalid"
    assert invalid["data"]["decision"]["status"] == "unknown"
    assert forced["data"]["schedule"]["selection"] == "explicit"
    assert forced["data"]["decision"]["evaluation_mode"] == "force_simulation"
    assert forced["data"]["decision"]["should_run_scan"] is True
    assert state_dir_selected["data"]["state"]["selection"] == "explicit_state_dir"
    assert state_dir_selected["meta"]["state_path"] == ".../scheduler_state_hk.json"


def test_production_schedule_key_selector_keeps_market_list_semantics() -> None:
    from src.application.tick_scheduler_context import select_scheduler_schedule_key

    config = {"schedule": {}, "schedule_hk": {}}
    assert select_scheduler_schedule_key(["HK"], config) == "schedule_hk"
    assert select_scheduler_schedule_key(["US"], config) == "schedule"
    assert select_scheduler_schedule_key(["HK", "US"], config) == "schedule"
    assert select_scheduler_schedule_key(["HK"], {"schedule": {}}) == "schedule"


@pytest.mark.parametrize("unavailable", ["corrupt", "unreadable", "invalid_schedule"])
def test_scheduler_status_unavailable_decision_compacts_as_partial(
    tmp_path: Path,
    unavailable: str,
) -> None:
    from src.application.bot.tools import compact_observation
    from src.application.tool_execution import execute_tool

    config = _runtime_config(market="hk")
    if unavailable == "invalid_schedule":
        config["schedule_hk"] = "invalid"
    config_path = tmp_path / "config.hk.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    state = tmp_path / "state.json"
    if unavailable == "corrupt":
        state.write_text("{", encoding="utf-8")
    elif unavailable == "unreadable":
        state.mkdir()
    else:
        state.write_text("{}", encoding="utf-8")

    payload = {
        "config_path": str(config_path),
        "state": str(state),
        "account": "lx",
    }
    response = execute_tool("scheduler_status", payload)
    observation = compact_observation("scheduler_status", response, payload)

    assert response["ok"] is True
    assert response["data"]["decision"]["status"] == "unknown"
    assert observation["status"] == "partial"
    assert observation["missing_data"]["decision.status"] == "unknown"


def test_scheduler_status_rejects_unconfigured_account_before_state_projection(
    tmp_path: Path,
) -> None:
    from src.application.tool_execution import execute_tool

    config_path = tmp_path / "config.hk.json"
    config_path.write_text(json.dumps(_runtime_config(market="hk")), encoding="utf-8")
    state = tmp_path / "state.json"
    hidden_timestamp = "2026-09-11T01:40:00+00:00"
    state.write_text(
        json.dumps({"last_run_utc_by_account": {"sy": hidden_timestamp}}),
        encoding="utf-8",
    )

    response = execute_tool(
        "scheduler_status",
        {"config_path": str(config_path), "state": str(state), "account": "sy"},
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "INPUT_ERROR"
    assert "accounts are not configured: sy" in response["error"]["message"]
    assert hidden_timestamp not in json.dumps(response)


@pytest.mark.parametrize(
    ("schedule_key", "schedule_value"),
    [
        ("portfolio", {"risk_budget": 0.1}),
        ("operator_schedule", {"timezone": "Asia/Hong_Kong", "unexpected": True}),
        ("operator_schedule", {"enabled": "false"}),
        ("operator_schedule", {"run_window": []}),
    ],
)
def test_scheduler_status_strictly_rejects_non_schedule_or_invalid_schedule(
    tmp_path: Path,
    schedule_key: str,
    schedule_value: object,
) -> None:
    from src.application.bot.tools import compact_observation
    from src.application.tool_execution import execute_tool

    config = _runtime_config(market="hk")
    config[schedule_key] = schedule_value
    config_path = tmp_path / "config.hk.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    state = tmp_path / "state.json"
    state.write_text("{}", encoding="utf-8")
    payload = {
        "config_path": str(config_path),
        "state": str(state),
        "schedule_key": schedule_key,
        "account": "lx",
    }

    response = execute_tool("scheduler_status", payload)
    observation = compact_observation("scheduler_status", response, payload)

    assert response["ok"] is True
    assert response["data"]["schedule"]["status"] == "invalid"
    assert response["data"]["schedule"]["enabled"] is None
    assert response["data"]["decision"]["status"] == "unknown"
    assert response["data"]["decision"]["reason"] == "schedule_invalid"
    assert "now_market" not in response["data"]["decision"]
    assert observation["status"] == "partial"


@pytest.mark.parametrize(
    "state_payload",
    [
        {"last_run_utc_by_account": []},
        {"last_scan_utc_by_account": {"lx": None}},
        {"last_processed_scan_target_utc_by_account": {"lx": 123}},
        {"last_notify_utc_by_account": {"lx": "not-a-time"}},
        {
            "last_run_utc_by_account": {"lx": "2026-09-11T01:40:00+00:00"},
            "last_notify_utc_by_account": {"sy": "not-a-time"},
        },
    ],
)
def test_scheduler_status_rejects_invalid_account_maps_before_decision(
    tmp_path: Path,
    state_payload: dict,
) -> None:
    from src.application.bot.tools import compact_observation
    from src.application.tool_execution import execute_tool

    config_path = tmp_path / "config.hk.json"
    config_path.write_text(json.dumps(_runtime_config(market="hk")), encoding="utf-8")
    state = tmp_path / "state.json"
    state.write_text(json.dumps(state_payload), encoding="utf-8")
    payload = {
        "config_path": str(config_path),
        "state": str(state),
        "account": "lx",
    }

    response = execute_tool("scheduler_status", payload)
    observation = compact_observation("scheduler_status", response, payload)

    assert response["ok"] is True
    assert response["data"]["state"]["status"] == "corrupt"
    assert response["data"]["state"]["account_record_status"] == "unknown"
    assert response["data"]["decision"]["status"] == "unknown"
    assert response["data"]["decision"]["reason"] == "state_corrupt"
    assert observation["status"] == "partial"


@pytest.mark.parametrize(
    "field",
    [
        "last_run_utc",
        "last_scan_utc",
        "last_processed_scan_target_utc",
        "last_notify_utc",
    ],
)
def test_scheduler_status_rejects_invalid_legacy_timestamps(
    tmp_path: Path,
    field: str,
) -> None:
    from src.application.tool_execution import execute_tool

    config_path = tmp_path / "config.hk.json"
    config_path.write_text(json.dumps(_runtime_config(market="hk")), encoding="utf-8")
    state = tmp_path / "state.json"
    state.write_text(json.dumps({field: "not-a-time"}), encoding="utf-8")

    response = execute_tool(
        "scheduler_status",
        {"config_path": str(config_path), "state": str(state)},
    )

    assert response["data"]["state"]["status"] == "corrupt"
    assert response["data"]["decision"]["status"] == "unknown"


def test_scheduler_status_accepts_legacy_none_and_config_validator_rejects_string_enabled(
    tmp_path: Path,
) -> None:
    from src.application.config_validator import validate_config
    from src.application.tool_execution import execute_tool

    config_path = tmp_path / "config.hk.json"
    config_path.write_text(json.dumps(_runtime_config(market="hk")), encoding="utf-8")
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"last_notify_utc": None}), encoding="utf-8")

    response = execute_tool(
        "scheduler_status",
        {"config_path": str(config_path), "state": str(state)},
    )

    assert response["data"]["state"]["status"] == "available"
    assert response["data"]["decision"]["status"] == "available"
    with pytest.raises(SystemExit, match="schedule.enabled must be a boolean"):
        validate_config({"schedule": {"enabled": "false"}})
