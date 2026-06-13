from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from src.application.agent_tool_contracts import build_response
from src.application.assistant import AssistantSettings, LlmTranslatorSettings, handle_assistant_message
from src.application.assistant.agent_loop import (
    TOOL_PLAN_SCHEMA_VERSION,
    LlmPlannerResult,
    LlmSynthesisResult,
    PlannerPlan,
    PlannerPlanStep,
)
from src.application.assistant.contracts import AssistantRequest


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "assistant_agent_eval.jsonl"


def _load_cases() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in FIXTURE_PATH.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if text:
            rows.append(json.loads(text))
    return rows


def _plan_from_case(case: dict[str, Any]) -> PlannerPlan:
    raw = dict(case["plan"])
    return PlannerPlan(
        goal=str(raw.get("goal") or ""),
        response_mode=str(raw.get("response_mode") or "synthesis"),
        required_capabilities=tuple(str(item) for item in raw.get("required_capabilities") or ()),
        steps=tuple(
            PlannerPlanStep(
                id=str(item.get("id") or f"step_{index}"),
                tool_name=str(item.get("tool_name") or ""),
                arguments=dict(item.get("arguments") or {}),
                purpose=str(item.get("purpose") or ""),
            )
            for index, item in enumerate(raw.get("steps") or (), start=1)
        ),
    )


@pytest.mark.parametrize("case", _load_cases(), ids=lambda item: str(item["id"]))
def test_assistant_agent_eval_uses_guarded_answer_evidence(case: dict[str, Any], tmp_path: Path) -> None:
    plan = _plan_from_case(case)
    synthesis_responses = [str(item) for item in case.get("synthesis_responses") or ()]
    observed_synthesis_inputs: list[list[dict[str, Any]]] = []

    def _execute(tool_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        assert tool_name == plan.steps[0].tool_name
        if tool_name != "healthcheck":
            assert payload.get("config_key") == "us"
        result = dict(case["tool_result"])
        return build_response(
            tool_name=tool_name,
            ok=bool(result.get("ok", True)),
            data=dict(result.get("data") or {}),
            warnings=[str(item) for item in result.get("warnings") or ()],
            meta=dict(result.get("meta") or {}),
        )

    def _plan(
        text: str,
        _settings: AssistantSettings,
        _conversation_context: dict[str, Any] | None,
    ) -> LlmPlannerResult:
        assert text == case["question"]
        return LlmPlannerResult(
            plan=plan,
            trace={
                "enabled": True,
                "attempted": True,
                "reason": "accepted",
                "provider": "fixture",
                "schema_version": TOOL_PLAN_SCHEMA_VERSION,
            },
        )

    def _synthesize(
        _question: str,
        _settings: AssistantSettings,
        _plan: PlannerPlan,
        observations: list[dict[str, Any]],
        _conversation_context: dict[str, Any] | None,
    ) -> LlmSynthesisResult:
        observed_synthesis_inputs.append(observations)
        index = min(len(observed_synthesis_inputs) - 1, max(len(synthesis_responses) - 1, 0))
        response_text = synthesis_responses[index] if synthesis_responses else ""
        return LlmSynthesisResult(
            response_text=response_text,
            trace={"attempted": True, "reason": "synthesized", "schema_version": "om-tool-plan-synthesis-v1"},
        )

    out = handle_assistant_message(
        AssistantRequest(
            text=str(case["question"]),
            sender_id="local",
            message_id=f"eval_{case['id']}",
            config_key="us",
            audit_db=str(tmp_path / "inbound.sqlite3"),
        ),
        execute_tool_fn=_execute,
        settings=AssistantSettings(
            mode="agent_loop",
            llm=LlmTranslatorSettings(enabled=True, provider="openai", model="gpt-5.2"),
        ),
        plan_tools_fn=_plan,
        synthesize_response_fn=_synthesize,
    )

    assert out["ok"] is True
    text = str(out["data"]["response_text"])
    for expected in case.get("expect_contains") or ():
        assert str(expected) in text
    for unexpected in case.get("expect_not_contains") or ():
        assert str(unexpected) not in text
    previous_index = -1
    for expected in case.get("expect_order") or ():
        current_index = text.index(str(expected))
        assert current_index > previous_index
        previous_index = current_index

    tool_plan_data = out["data"]["action"]["result"]["data"]
    assert tool_plan_data["synthesis"]["reason"] == case["expect_reason"]
    assert observed_synthesis_inputs
    evidence = observed_synthesis_inputs[0][-1]
    assert evidence["tool_name"] == "assistant.answer_evidence"
    assert "fallback_renderer_text" in evidence["data"]
    assert "provenance_lines" in evidence["data"]
    first_observation = tool_plan_data["synthesis_observations"][0]
    renderer = first_observation["output_contract"]["canonical_renderer"]
    if case.get("expect_renderer"):
        assert renderer == case["expect_renderer"]
    else:
        assert renderer
