from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from src.application.agent_tool_contracts import build_response
from src.application.assistant import AssistantSettings, LlmTranslatorSettings, handle_assistant_message
from src.application.assistant.action_policy import decide_tool_action_policy
from src.application.assistant.action_safety import assess_action_safety
from src.application.assistant.agent_loop import (
    TOOL_PLAN_SCHEMA_VERSION,
    LlmPlannerResult,
    LlmSynthesisResult,
    PlannerPlan,
    PlannerPlanStep,
)
from src.application.assistant.contracts import AssistantRequest, ToolCall


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "assistant_agent_eval.jsonl"


def _load_cases() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in FIXTURE_PATH.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if text:
            rows.append(json.loads(text))
    return rows


def _plan_from_payload(raw_payload: dict[str, Any]) -> PlannerPlan:
    raw = dict(raw_payload)
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


def _plan_from_case(case: dict[str, Any]) -> PlannerPlan:
    return _plan_from_payload(dict(case["plan"]))


def _write_trade_runtime_config(tmp_path: Path) -> tuple[Path, Path]:
    sqlite_path = tmp_path / "output_shared" / "state" / "option_positions.sqlite3"
    data_cfg_path = tmp_path / "portfolio.runtime.json"
    data_cfg_path.write_text(
        json.dumps({"option_positions": {"sqlite_path": str(sqlite_path)}}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    cfg_path = tmp_path / "config.hk.json"
    cfg_path.write_text(
        json.dumps(
            {
                "_generated": {
                    "schema_version": "1.0",
                    "generator": "options-monitor",
                    "source_format": "yaml",
                    "market": "hk",
                },
                "_resolved": {"source_format": "yaml", "market": "hk", "runtime_schema": "config-json-v1"},
                "accounts": ["sy"],
                "portfolio": {
                    "broker": "富途",
                    "source": "futu",
                    "account": "sy",
                    "data_config": str(data_cfg_path),
                },
                "templates": {
                    "put_base": {
                        "sell_put": {
                            "min_annualized_net_return": 0.1,
                            "min_net_income": 50,
                            "min_open_interest": 10,
                            "min_volume": 1,
                            "max_spread_ratio": 0.3,
                        }
                    }
                },
                "symbols": [
                    {
                        "symbol": "0700.HK",
                        "fetch": {"source": "futu", "limit_expirations": 8},
                        "use": ["put_base"],
                        "sell_put": {"enabled": True, "min_dte": 1, "max_dte": 45},
                        "sell_call": {"enabled": False},
                    }
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return cfg_path, sqlite_path


@pytest.mark.parametrize("case", _load_cases(), ids=lambda item: str(item["id"]))
def test_assistant_agent_eval_uses_guarded_answer_evidence(case: dict[str, Any], tmp_path: Path, monkeypatch: Any) -> None:
    mode = str(case.get("mode") or "agent_answer")
    if mode == "action_safety":
        _run_action_safety_case(case)
        return
    if mode == "planner_preview":
        _run_planner_preview_case(case, tmp_path=tmp_path, monkeypatch=monkeypatch)
        return

    plan = _plan_from_case(case)
    followup_plan = _plan_from_payload(dict(case["followup_plan"])) if isinstance(case.get("followup_plan"), dict) else None
    tool_results = [dict(item) for item in case.get("tool_results") or () if isinstance(item, dict)]
    if not tool_results:
        tool_results = [dict(case["tool_result"])]
    synthesis_responses = [str(item) for item in case.get("synthesis_responses") or ()]
    execute_calls: list[tuple[str, dict[str, Any]]] = []
    observed_synthesis_inputs: list[list[dict[str, Any]]] = []

    def _execute(tool_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        execute_calls.append((tool_name, dict(payload)))
        result_index = len(execute_calls) - 1
        assert result_index < len(tool_results)
        result = dict(tool_results[result_index])
        expected_tool_name = str(result.get("tool_name") or "").strip()
        if not expected_tool_name and result_index == 0 and plan.steps:
            expected_tool_name = plan.steps[0].tool_name
        if expected_tool_name:
            assert tool_name == expected_tool_name
        if tool_name not in {"healthcheck", "operation_timeline", "assistant_trace"}:
            assert payload.get("config_key") == "us"
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
        followup = _conversation_context.get("agent_loop_followup") if isinstance(_conversation_context, dict) else None
        selected_plan = followup_plan if isinstance(followup, dict) and followup_plan is not None else plan
        return LlmPlannerResult(
            plan=selected_plan,
            trace={
                "enabled": True,
                "attempted": True,
                "reason": "followup" if isinstance(followup, dict) else "accepted",
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
    if case.get("expect_tool_calls"):
        _assert_expected_tool_calls(execute_calls, expected_calls=case["expect_tool_calls"])
    if case.get("expect_tool_calls_used") is not None:
        assert tool_plan_data["tool_calls_used"] == int(case["expect_tool_calls_used"])
    if case.get("expect_plan_revision_count") is not None:
        assert len(tool_plan_data["plan_revisions"]) == int(case["expect_plan_revision_count"])
    if case.get("expect_followup_tool"):
        decisions = [item for item in tool_plan_data["followup_decisions"] if isinstance(item, dict)]
        assert any(
            item.get("status") == "accepted" and item.get("tool_name") == case["expect_followup_tool"]
            for item in decisions
        )
    if case.get("expect_final_response_status"):
        assert tool_plan_data["final_response"]["status"] == case["expect_final_response_status"]
    if case.get("expect_answer_guard_status"):
        assert tool_plan_data["synthesis"]["answer_guard"]["status"] == case["expect_answer_guard_status"]
    evidence_bundle = tool_plan_data["evidence_bundle"]
    if case.get("expect_diagnostic_domains"):
        domains = sorted({str(item.get("domain")) for item in evidence_bundle["diagnostics"] if item.get("domain")})
        assert domains == case["expect_diagnostic_domains"]
    if case.get("expect_diagnostic_statuses"):
        statuses = [item["status"] for item in evidence_bundle["diagnostics"]]
        for expected in case["expect_diagnostic_statuses"]:
            assert expected in statuses
    if case.get("expect_final_route"):
        session = tool_plan_data["agent_session"]
        answer_trace = session["answer_trace"]["synthesis"]
        guard = answer_trace.get("answer_guard") if isinstance(answer_trace.get("answer_guard"), dict) else {}
        if case["expect_final_route"] == "rewrite":
            assert guard.get("status") == "failed_then_rewritten"
        elif case["expect_final_route"] == "fallback":
            assert guard.get("status") == "failed_then_fallback" or answer_trace.get("fallback")
        elif case["expect_final_route"] == "pass":
            assert tool_plan_data["final_response"]["status"] in {"synthesized", "rendered"}
            assert guard.get("status") in {"passed", "", None}
    assert observed_synthesis_inputs
    evidence = next(
        (
            observations[-1]
            for observations in reversed(observed_synthesis_inputs)
            if observations and observations[-1].get("tool_name") == "assistant.answer_evidence"
        ),
        None,
    )
    expect_answer_evidence = bool(case.get("expect_answer_evidence", True))
    if expect_answer_evidence:
        assert evidence is not None
        assert evidence["tool_name"] == "assistant.answer_evidence"
        assert "fallback_renderer_text" in evidence["data"]
        assert "provenance_lines" in evidence["data"]
    elif evidence is not None:
        assert evidence["tool_name"] == "assistant.answer_evidence"
        assert "fallback_renderer_text" in evidence["data"]
        assert "provenance_lines" in evidence["data"]
    first_observation = tool_plan_data["synthesis_observations"][0]
    renderer = first_observation["output_contract"]["canonical_renderer"]
    if case.get("expect_renderer"):
        assert renderer == case["expect_renderer"]
    else:
        assert renderer


def _assert_expected_tool_calls(
    calls: list[tuple[str, dict[str, Any]]],
    *,
    expected_calls: list[dict[str, Any]],
) -> None:
    assert len(calls) == len(expected_calls)
    for (tool_name, payload), expected in zip(calls, expected_calls, strict=True):
        assert tool_name == expected["tool_name"]
        for key, value in dict(expected.get("payload_subset") or {}).items():
            assert payload.get(key) == value
        for key in expected.get("payload_required_keys") or ():
            assert str(payload.get(key) or "").strip()


def _run_action_safety_case(case: dict[str, Any]) -> None:
    request = AssistantRequest(text=str(case["question"]), sender_id="local", config_key=str(case.get("config_key") or "hk"))
    tool_name = str(case["tool_name"])
    payload = dict(case.get("payload") or {})
    policy = decide_tool_action_policy(
        call=ToolCall(tool_name=tool_name, payload=payload),
        request=request,
        task_contract=dict(case.get("task_contract") or {}),
    )
    safety = assess_action_safety(
        question=request.text,
        task_contract=dict(case.get("task_contract") or {}),
        tool_name=tool_name,
        payload=payload,
        action_policy=policy.public_payload(),
        untrusted_texts=tuple(str(item) for item in case.get("untrusted_texts") or ()),
    ).public_payload()

    assert safety["status"] == case["expect_status"]
    assert safety["code"] == case["expect_code"]
    assert safety["route"] == case["expect_route"]
    if case.get("expect_injection_evidence"):
        assert safety["injection_evidence"]


def _run_planner_preview_case(case: dict[str, Any], *, tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("OM_INBOUND_OPERATIONS_ENABLED", "1")
    monkeypatch.setenv("OM_INBOUND_TRADE_WRITE_ENABLED", "1")
    monkeypatch.setenv("OM_INBOUND_ADMIN_OPEN_IDS", "local:local")
    monkeypatch.setenv("OM_INBOUND_OPERATION_HMAC_KEY", "test-operation-hmac-key")

    def _fake_resolve(**_kwargs: object) -> tuple[int, str, dict[str, Any]]:
        return 500, "cache", {"attempted_sources": [{"source": "cache", "status": "resolved", "value": 500}]}

    monkeypatch.setattr("src.application.assistant.manual_trade_parser.resolve_multiplier_with_source_and_diagnostics", _fake_resolve)

    cfg_path, sqlite_path = _write_trade_runtime_config(tmp_path)
    plan = _plan_from_case(case)
    calls: list[tuple[str, dict[str, Any]]] = []

    def _execute(tool_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        calls.append((tool_name, payload))
        return build_response(tool_name=tool_name, ok=True, data={})

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

    out = handle_assistant_message(
        AssistantRequest(
            text=str(case["question"]),
            sender_id="local",
            channel="local",
            message_id=f"eval_{case['id']}",
            config_path=str(cfg_path),
            audit_db=str(tmp_path / "inbound.sqlite3"),
        ),
        execute_tool_fn=_execute,
        allowed_senders="local:local",
        settings=AssistantSettings(
            mode="agent_loop",
            llm=LlmTranslatorSettings(enabled=True, provider="openai", model="gpt-5.2"),
        ),
        plan_tools_fn=_plan,
        now_fn=lambda: date(2026, 6, 4),
    )

    assert out["ok"] is True
    assert calls == []
    text = str(out["data"]["response_text"])
    for expected in case.get("expect_contains") or ():
        assert str(expected) in text
    for unexpected in case.get("expect_not_contains") or ():
        assert str(unexpected) not in text
    assert out["data"]["perception"]["intent_name"] == case["expect_perception_intent"]
    permission = out["data"]["permission_request"]
    assert permission["operation_type"] == case["expect_operation_type"]
    assert permission["confirm_required"] is True
    assert permission["apply_allowed"] is False
    agent_loop = out["meta"]["assistant"]["llm"]["agent_loop"]
    step = agent_loop["steps"][0]
    assert step["action_policy"]["decision"] == "allow_preview"
    assert step["action_policy"]["apply_allowed"] is False
    assert step["action_safety"]["status"] == "allow_preview"
    assert step["precheck"]["status"] == "pass"
    assert agent_loop["preview_receipt"]["confirm_required"] is True
    assert agent_loop["preview_receipt"]["apply_allowed"] is False
    if sqlite_path.exists():
        with sqlite3.connect(sqlite_path) as conn:
            has_trade_events = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='trade_events'"
            ).fetchone()
            if has_trade_events:
                assert conn.execute("SELECT COUNT(*) FROM trade_events").fetchone()[0] == 0
