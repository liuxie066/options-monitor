from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from src.application.assistant.command_parser import parse_assistant_command
from src.application.assistant.contracts import AssistantRequest, PerceptionResult
from src.application.assistant.parser import parse_inbound_text
from src.application.assistant.perception_trace import accepted_candidate, build_perception_trace, skipped_candidate
from src.application.assistant.reasoning import resolve_reasoning


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "assistant_nlu_eval.jsonl"


def _load_cases() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for line in FIXTURE_PATH.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        cases.append(json.loads(line))
    return cases


def _parse(text: str) -> PerceptionResult:
    command_intent = parse_assistant_command(text, now_fn=lambda: date(2026, 5, 26))
    if command_intent is not None:
        return command_intent
    return parse_inbound_text(text, now_fn=lambda: date(2026, 5, 26))


def _trace_eval_case(perception: PerceptionResult) -> dict[str, Any]:
    source = "command" if perception.source == "command" else "deterministic"
    trace = build_perception_trace(
        decision=f"{source}_selected",
        selected_source=source,
        selected_perception=perception,
        candidates=[
            accepted_candidate(source, perception),
            skipped_candidate("llm", f"{source}_selected"),
        ],
    )
    return trace.public_payload()


@pytest.mark.parametrize("case", _load_cases(), ids=lambda case: str(case["id"]))
def test_assistant_nlu_eval_cases(case: dict[str, Any]) -> None:
    perception = _parse(str(case["text"]))
    trace = _trace_eval_case(perception)
    expected_source = str(case["expected_source"])
    expected_safety_class = str(case["expected_safety_class"])
    resolution = resolve_reasoning(
        perception,
        request=AssistantRequest(text=str(case["text"]), sender_id="local", config_key="us"),
    )

    assert perception.intent_name == case["expected_intent"]
    assert ("command" if perception.source == "command" else "deterministic") == expected_source
    assert resolution.safety_class == expected_safety_class
    assert trace["selected_perception"]["intent_name"] == case["expected_intent"]
    assert trace["selected_source"] == expected_source
    assert trace["candidates"][0]["intent_name"] == case["expected_intent"]
    if case.get("must_not_intent"):
        assert perception.intent_name != case["must_not_intent"]

    expected_subset = case.get("expected_args_subset")
    if isinstance(expected_subset, dict):
        _assert_subset(perception.arguments, expected_subset)


def _assert_subset(actual: Any, expected: Any) -> None:
    if isinstance(expected, dict):
        assert isinstance(actual, dict)
        for key, value in expected.items():
            assert key in actual
            _assert_subset(actual[key], value)
        return
    assert actual == expected
