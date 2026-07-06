from __future__ import annotations

import json
from pathlib import Path

from src.application.assistant.agent_loop import _copilot_tool_manifest
from src.application.assistant.context_validation import (
    CONTEXT_VALIDATION_SCHEMA_VERSION,
    validate_context_use,
)


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "assistant_context_validation.jsonl"
VALIDATOR_PATH = Path(__file__).parents[1] / "src" / "application" / "assistant" / "context_validation.py"


def test_context_validation_fixture_cases_match_expected_status() -> None:
    cases = _fixture_cases()

    assert {case["id"] for case in cases} >= {
        "validation_passes_referenced_carry",
        "validation_blocks_unreferenced_carry",
        "validation_passes_explicit_override",
        "validation_blocks_hidden_injected_args",
        "validation_asks_when_truncated_context_has_no_ref",
        "validation_blocks_clarification_with_tool_steps",
    }
    for case in cases:
        validation = validate_context_use(
            current_user_message=str(case.get("current_user_message") or ""),
            context_projection=dict(case["context_projection"]),
            plan_payload=dict(case["plan_payload"]),
            tool_manifest=_copilot_tool_manifest(),
        )
        expect = case["expect"]
        assert validation["schema_version"] == CONTEXT_VALIDATION_SCHEMA_VERSION, case["id"]
        assert validation["status"] == expect["status"], case["id"]
        assert validation["code"] == expect["code"], case["id"]
        assert validation["context_use_mode"] == expect["context_use_mode"], case["id"]
        if expect.get("violation_reason"):
            assert validation["violation"]["reason"] == expect["violation_reason"], case["id"]


def test_context_validation_is_structural_not_business_keyword_based() -> None:
    source = VALIDATOR_PATH.read_text(encoding="utf-8")

    assert "net_income" not in source
    assert "candidate_option" not in source
    assert "account_income" not in source


def test_context_validation_passes_without_projection_as_shadow_warning() -> None:
    validation = validate_context_use(
        context_projection=None,
        plan_payload={
            "context_use": {
                "mode": "none",
                "referenced_turn_ids": [],
                "referenced_evidence_refs": [],
                "inherited_slots": {},
                "current_message_slots": {},
                "override_slots": {},
                "requires_clarification": False,
                "clarification_question": None,
            },
            "steps": [
                {
                    "id": "step_1",
                    "tool_name": "analysis_query",
                    "arguments": {"sql": "select 1 as ok"},
                    "purpose": "read",
                }
            ],
        },
        tool_manifest=_copilot_tool_manifest(),
    )

    assert validation["status"] == "passed"
    assert validation["code"] == "ok"
    assert validation["warnings"][0]["code"] == "CONTEXT_PROJECTION_MISSING"


def test_context_validation_accepts_symbol_config_followup_setting_edit() -> None:
    projection = _symbol_config_projection()

    validation = validate_context_use(
        current_user_message="改为90",
        context_projection=projection,
        plan_payload={
            "context_use": {
                "mode": "carry",
                "referenced_turn_ids": ["session:s_symbol_config"],
                "referenced_evidence_refs": ["ev_001"],
                "inherited_slots": {
                    "symbol": ["FUTU"],
                    "strategy": ["sell_put"],
                    "setting_path": ["sell_put.max_strike"],
                    "setting_field": ["max_strike"],
                },
                "current_message_slots": {"setting_new_value": [90]},
                "override_slots": {},
                "requires_clarification": False,
                "clarification_question": None,
            },
            "steps": [
                {
                    "id": "step_1",
                    "tool_name": "symbol_edit",
                    "arguments": {"symbol": "FUTU", "set": {"sell_put.max_strike": 90}},
                    "purpose": "preview config change from visible setting context",
                }
            ],
        },
        tool_manifest=_copilot_tool_manifest(),
    )

    assert validation["status"] == "passed"
    assert validation["code"] == "ok"


def test_context_validation_accepts_frame_delta_using_frame_reference_only() -> None:
    projection = _symbol_config_projection()

    validation = validate_context_use(
        current_user_message="改为90",
        context_projection=projection,
        plan_payload={
            "context_use": {
                "mode": "frame_delta",
                "referenced_turn_ids": [],
                "referenced_evidence_refs": [],
                "referenced_frame_ids": ["frame_ev_001"],
                "inherited_slots": {
                    "symbol": ["FUTU"],
                    "strategy": ["sell_put"],
                    "setting_path": ["sell_put.max_strike"],
                    "setting_field": ["max_strike"],
                },
                "current_message_slots": {"setting_new_value": [90]},
                "override_slots": {},
                "delta": {"type": "set_value", "value": 90},
                "requires_clarification": False,
                "clarification_question": None,
            },
            "steps": [
                {
                    "id": "step_1",
                    "tool_name": "symbol_edit",
                    "arguments": {"symbol": "FUTU", "set": {"sell_put.max_strike": 90}},
                    "purpose": "preview config change from active setting frame",
                }
            ],
        },
        tool_manifest=_copilot_tool_manifest(),
    )

    assert validation["status"] == "passed"
    assert validation["code"] == "ok"
    assert validation["referenced_frame_ids"] == ["frame_ev_001"]


def test_context_validation_blocks_followup_setting_path_drift() -> None:
    projection = _symbol_config_projection()

    validation = validate_context_use(
        current_user_message="改为90",
        context_projection=projection,
        plan_payload={
            "context_use": {
                "mode": "carry",
                "referenced_turn_ids": ["session:s_symbol_config"],
                "referenced_evidence_refs": ["ev_001"],
                "inherited_slots": {
                    "symbol": ["FUTU"],
                    "strategy": ["sell_call"],
                    "setting_path": ["sell_call.min_strike"],
                    "setting_field": ["min_strike"],
                },
                "current_message_slots": {"setting_new_value": [90]},
                "override_slots": {},
                "requires_clarification": False,
                "clarification_question": None,
            },
            "steps": [
                {
                    "id": "step_1",
                    "tool_name": "symbol_edit",
                    "arguments": {"symbol": "FUTU", "set": {"sell_call.min_strike": 90}},
                    "purpose": "preview config change from wrong setting context",
                }
            ],
        },
        tool_manifest=_copilot_tool_manifest(),
    )

    assert validation["status"] == "blocked"
    assert validation["code"] == "CONTEXT_SLOT_NOT_AVAILABLE"
    assert validation["violation"]["slot"] == "strategy"


def test_context_validation_accepts_notification_system_event_reference_for_read_tool() -> None:
    projection = {
        "schema_version": "om-context-projection-v1",
        "recent_turns": [
            {
                "turn_id": "system:notification",
                "safe_slots": {"run_id": ["run_1"], "action": ["notification_delivery_decided"]},
                "evidence_refs": ["ev_001"],
            }
        ],
        "recent_successful_tools": [],
        "available_evidence_refs": [
            {
                "ref_id": "ev_001",
                "turn_id": "system:notification",
                "source_type": "system_event",
                "source_tool": "notification_perception",
                "safe_slots": {"run_id": ["run_1"], "action": ["notification_delivery_decided"]},
                "data_shape": {"delivery_action": "skip_no_send"},
            }
        ],
        "budget": {"truncated": False},
    }

    validation = validate_context_use(
        current_user_message="刚才那条为什么没发？",
        context_projection=projection,
        plan_payload={
            "context_use": {
                "mode": "carry",
                "referenced_turn_ids": ["system:notification"],
                "referenced_evidence_refs": ["ev_001"],
                "inherited_slots": {"run_id": ["run_1"]},
                "current_message_slots": {},
                "override_slots": {},
                "requires_clarification": False,
            },
            "steps": [
                {
                    "id": "step_1",
                    "tool_name": "notification_perception_read",
                    "arguments": {"run_id": "run_1", "limit": 3},
                    "purpose": "inspect visible notification perception evidence",
                }
            ],
        },
        tool_manifest=_copilot_tool_manifest(),
    )

    assert validation["status"] == "passed"
    assert validation["code"] == "ok"


def _fixture_cases() -> list[dict]:
    return [json.loads(line) for line in FIXTURE_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]


def _symbol_config_projection() -> dict:
    return {
        "schema_version": "om-context-projection-v1",
        "recent_turns": [
            {
                "turn_id": "session:s_symbol_config",
                "safe_slots": {
                    "symbol": ["FUTU"],
                    "strategy": ["sell_put"],
                    "setting_path": ["sell_put.max_strike"],
                    "setting_field": ["max_strike"],
                },
                "evidence_refs": ["ev_001"],
            }
        ],
        "recent_successful_tools": [],
        "available_evidence_refs": [
            {
                "ref_id": "ev_001",
                "turn_id": "session:s_symbol_config",
                "source_type": "tool_result",
                "source_tool": "symbol_config_read",
                "safe_slots": {
                    "symbol": ["FUTU"],
                    "strategy": ["sell_put"],
                    "setting_path": ["sell_put.max_strike"],
                    "setting_field": ["max_strike"],
                },
                "data_shape": {"kind": "single_symbol_setting", "setting_path": "sell_put.max_strike"},
            }
        ],
        "active_frames": [
            {
                "frame_id": "frame_ev_001",
                "type": "symbol_setting",
                "source_tool": "symbol_config_read",
                "source_ref_id": "ev_001",
                "turn_id": "session:s_symbol_config",
                "symbol": "FUTU",
                "strategy": "sell_put",
                "setting_path": "sell_put.max_strike",
                "setting_field": "max_strike",
                "current_value": 120.0,
                "allowed_deltas": ["set_value", "explain"],
            }
        ],
        "budget": {"truncated": False},
    }
