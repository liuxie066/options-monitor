from __future__ import annotations

import json
from pathlib import Path

from src.application.assistant.agent_loop import _planner_tool_manifest
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
            planner_manifest=_planner_tool_manifest(),
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
        planner_manifest=_planner_tool_manifest(),
    )

    assert validation["status"] == "passed"
    assert validation["code"] == "ok"
    assert validation["warnings"][0]["code"] == "CONTEXT_PROJECTION_MISSING"


def _fixture_cases() -> list[dict]:
    return [json.loads(line) for line in FIXTURE_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]
