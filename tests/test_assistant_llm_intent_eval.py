from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from src.application.assistant.contracts import AssistantRequest
from src.application.assistant.llm_translator import parse_llm_translation_payload
from src.application.assistant.reasoning import resolve_reasoning
from src.application.assistant.settings import LlmTranslatorSettings


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "assistant_llm_intent_eval.jsonl"


def _load_cases() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for line in FIXTURE_PATH.read_text(encoding="utf-8").splitlines():
        if line.strip():
            cases.append(json.loads(line))
    return cases


@pytest.mark.parametrize("case", _load_cases(), ids=lambda case: str(case["id"]))
def test_assistant_llm_intent_eval_cases(case: dict[str, Any]) -> None:
    result = parse_llm_translation_payload(
        dict(case["payload"]),
        settings=LlmTranslatorSettings(enabled=True, confidence_min=0.75),
    )

    expected_error = case.get("expected_error")
    if expected_error:
        assert result.intent is None
        assert result.error is not None
        assert result.error.code == expected_error
        if case.get("expected_rejected_reason"):
            assert result.error.details is not None
            assert result.error.details["llm_rejected_reason"] == case["expected_rejected_reason"]
        return

    assert result.error is None
    assert result.intent is not None
    assert result.intent.intent_name == case["expected_intent"]
    _assert_subset(result.intent.arguments, case.get("expected_args_subset") or {})

    resolution = resolve_reasoning(
        result.intent,
        request=AssistantRequest(text=str(case["text"]), sender_id="local", config_key="us"),
    )

    assert resolution.safety_class == case["expected_safety_class"]
    assert resolution.tool_call is not None
    assert resolution.tool_call.tool_name == case["expected_tool"]
    _assert_subset(resolution.tool_call.payload, case.get("expected_tool_payload_subset") or {})


def _assert_subset(actual: Any, expected: Any) -> None:
    if isinstance(expected, dict):
        assert isinstance(actual, dict)
        for key, value in expected.items():
            assert key in actual
            _assert_subset(actual[key], value)
        return
    assert actual == expected
