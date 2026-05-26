from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from src.application.assistant.command_parser import parse_assistant_command
from src.application.assistant.contracts import AssistantIntent
from src.application.assistant.intent_arbitration import accepted_candidate, build_intent_arbitration, skipped_candidate
from src.application.assistant.parser import parse_inbound_text


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "assistant_nlu_eval.jsonl"


def _load_cases() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for line in FIXTURE_PATH.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        cases.append(json.loads(line))
    return cases


def _parse(text: str) -> AssistantIntent:
    command_intent = parse_assistant_command(text, now_fn=lambda: date(2026, 5, 26))
    if command_intent is not None:
        return command_intent
    return parse_inbound_text(text, now_fn=lambda: date(2026, 5, 26))


def _arbitrate_eval_case(intent: AssistantIntent) -> dict[str, Any]:
    source = "command" if intent.parser == "command" else "deterministic"
    arbitration = build_intent_arbitration(
        decision=f"{source}_selected",
        selected_source=source,
        selected_intent=intent,
        candidates=[
            accepted_candidate(source, intent),
            skipped_candidate("llm", f"{source}_selected"),
        ],
    )
    return arbitration.public_payload()


@pytest.mark.parametrize("case", _load_cases(), ids=lambda case: str(case["id"]))
def test_assistant_nlu_eval_cases(case: dict[str, Any]) -> None:
    intent = _parse(str(case["text"]))
    arbitration = _arbitrate_eval_case(intent)

    assert intent.name == case["expected_intent"]
    assert arbitration["selected_intent"]["name"] == case["expected_intent"]
    assert arbitration["candidates"][0]["intent_name"] == case["expected_intent"]
    if case.get("must_not_intent"):
        assert intent.name != case["must_not_intent"]

    expected_subset = case.get("expected_args_subset")
    if isinstance(expected_subset, dict):
        _assert_subset(intent.arguments, expected_subset)


def _assert_subset(actual: Any, expected: Any) -> None:
    if isinstance(expected, dict):
        assert isinstance(actual, dict)
        for key, value in expected.items():
            assert key in actual
            _assert_subset(actual[key], value)
        return
    assert actual == expected
