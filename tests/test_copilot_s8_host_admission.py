from __future__ import annotations

from src.application.copilot import tools as copilot_tools
from src.application.copilot.host import run_contract
from tests.copilot_pi_test_support import _TEST_MODEL
from tests.test_copilot_phase1 import _contract


def _run_answered_host(monkeypatch, prompt: str, tool_flow) -> object:
    def process(_start, *, on_tool_call, on_proposed, **_kwargs):
        text = tool_flow(on_tool_call)
        proposal = {
            "status": "answered",
            "text": text,
            "control_request": None,
            "termination_reason": "stop",
            "usage": {},
        }
        decision = on_proposed(proposal)
        return {"ok": True, "result": {**proposal, "committed": decision == "commit"}}

    monkeypatch.setattr("src.application.copilot.host.run_pi_agent", process)
    return run_contract(_contract(prompt), model_settings=_TEST_MODEL)


def test_host_registers_successful_read_for_submit_answer(monkeypatch) -> None:
    monkeypatch.setattr(
        copilot_tools,
        "call_read_tool",
        lambda *_args, **_kwargs: {"ok": True, "data": {"summary": {"ok": True}}},
    )
    monkeypatch.setattr(
        copilot_tools,
        "compact_observation",
        lambda *_args, **_kwargs: {
            "tool_name": "runtime_status",
            "ok": True,
            "status": "complete",
            "value": {"summary": {"ok": True}},
            "coverage": {
                "status": "complete",
                "complete_for": "point",
                "scope": {"config_key": "us"},
            },
            "freshness": {
                "status": "current",
                "as_of": "2026-08-22T09:30:00+08:00",
            },
        },
    )

    def tool_flow(on_tool_call):
        observation = on_tool_call(
            {
                "call_id": "read_1",
                "tool_name": "runtime_status",
                "arguments": {"config_key": "us"},
            }
        )
        admitted = on_tool_call(
            {
                "call_id": "answer_1",
                "tool_name": "submit_answer",
                "arguments": {
                    "mode": "evidence",
                    "status": "complete",
                    "answer_markdown": "结论：当前运行状态正常。",
                    "claims": [
                        {
                            "text": "当前运行状态正常",
                            "kind": "current_fact",
                            "observation_ids": [observation["ref"]],
                            "required_scope": "point",
                        }
                    ],
                },
            }
        )
        assert admitted["observation"] == {"ok": True, "status": "answer_accepted"}
        return admitted["approved_answer"]["text"]

    result = _run_answered_host(monkeypatch, "检查当前运行状态", tool_flow)

    assert result.ok is True
    assert result.status == "answered"
    assert result.user_response.startswith("结论：当前运行状态正常。")
    assert "> 数据时间：2026-08-22T09:30:00+08:00。" in result.user_response


def test_host_registers_evidence_budget_narrowing_as_diagnostic(monkeypatch) -> None:
    monkeypatch.setattr(
        copilot_tools,
        "call_read_tool",
        lambda *_args, **_kwargs: {"ok": True, "data": {"summary": {"ok": True}}},
    )
    monkeypatch.setattr(
        copilot_tools,
        "compact_observation",
        lambda *_args, **_kwargs: {
            "tool_name": "runtime_status",
            "ok": True,
            "status": "complete",
            "value": {"summary": {"ok": True}},
            "coverage": {"status": "complete", "complete_for": "point"},
            "freshness": {
                "status": "current",
                "as_of": "2026-08-22T09:30:00+08:00",
            },
        },
    )
    monkeypatch.setattr(copilot_tools, "conservative_json_tokens", lambda _value: 20_001)

    def tool_flow(on_tool_call):
        observation = on_tool_call(
            {
                "call_id": "read_large",
                "tool_name": "runtime_status",
                "arguments": {"config_key": "us"},
            }
        )
        assert observation["status"] == "needs_narrowing"
        admitted = on_tool_call(
            {
                "call_id": "answer_narrow",
                "tool_name": "submit_answer",
                "arguments": {
                    "mode": "evidence",
                    "status": "needs_narrowing",
                    "answer_markdown": "当前结果范围过大。",
                    "claims": [
                        {
                            "text": "当前结果范围过大",
                            "kind": "judgment",
                            "observation_ids": [observation["ref"]],
                            "required_scope": "point",
                        }
                    ],
                },
            }
        )
        assert admitted["observation"] == {"ok": True, "status": "answer_accepted"}
        return admitted["approved_answer"]["text"]

    result = _run_answered_host(monkeypatch, "检查当前运行状态", tool_flow)

    assert result.ok is True
    assert "需要缩小范围" in result.user_response


def test_host_discards_plain_answer_without_submit_answer(monkeypatch) -> None:
    def process(_start, *, on_proposed, **_kwargs):
        proposal = {
            "status": "answered",
            "text": "绕过结构化准入的回答",
            "control_request": None,
            "termination_reason": "stop",
            "usage": {},
        }
        decision = on_proposed(proposal)
        return {"ok": True, "result": {**proposal, "committed": decision == "commit"}}

    monkeypatch.setattr("src.application.copilot.host.run_pi_agent", process)

    result = run_contract(_contract("解释运行机制"), model_settings=_TEST_MODEL)

    assert result.ok is False
    assert result.status == "failed"
    assert result.error == {
        "code": "RESULT_REJECTED",
        "reason": "answer_not_approved",
    }


def test_failed_read_is_audited_but_not_registered_as_evidence(monkeypatch) -> None:
    monkeypatch.setattr(
        copilot_tools,
        "call_read_tool",
        lambda *_args, **_kwargs: {
            "ok": False,
            "error": {"code": "READ_ERROR", "message": "unavailable"},
        },
    )

    def tool_flow(on_tool_call):
        failed = on_tool_call(
            {
                "call_id": "read_failed",
                "tool_name": "runtime_status",
                "arguments": {"config_key": "us"},
            }
        )
        rejected = on_tool_call(
            {
                "call_id": "answer_failed_read",
                "tool_name": "submit_answer",
                "arguments": {
                    "mode": "evidence",
                    "status": "complete",
                    "answer_markdown": "错误地引用失败读取。",
                    "claims": [
                        {
                            "text": "读取成功",
                            "kind": "current_fact",
                            "observation_ids": [failed["ref"]],
                            "required_scope": "point",
                        }
                    ],
                },
            }
        )
        assert rejected["observation"]["reason"] == "observation_outside_request"
        admitted = on_tool_call(
            {
                "call_id": "answer_diagnostic",
                "tool_name": "submit_answer",
                "arguments": {
                    "mode": "conceptual",
                    "status": "insufficient_evidence",
                    "answer_markdown": "读取失败，无法形成事实结论。",
                    "claims": [],
                },
            }
        )
        return admitted["approved_answer"]["text"]

    result = _run_answered_host(monkeypatch, "检查当前运行状态", tool_flow)

    failed_events = [
        event
        for event in result.events
        if event.type == "tool_result"
        and event.payload.get("tool_call_id") == "read_failed"
    ]
    assert result.ok is True
    assert len(failed_events) == 1
    assert failed_events[0].payload["ok"] is False


def test_evidence_content_hash_binds_coverage_metadata(monkeypatch) -> None:
    projection_count = 0

    monkeypatch.setattr(
        copilot_tools,
        "call_read_tool",
        lambda *_args, **_kwargs: {"ok": True, "data": {"value": 1}},
    )

    def compact(*_args, **_kwargs):
        nonlocal projection_count
        projection_count += 1
        return {
            "tool_name": "runtime_status",
            "ok": True,
            "status": "complete",
            "value": {"value": 1},
            "coverage": {
                "status": "complete",
                "complete_for": "point" if projection_count == 1 else "requested_page",
            },
            "freshness": {"status": "not_applicable"},
        }

    monkeypatch.setattr(copilot_tools, "compact_observation", compact)

    def tool_flow(on_tool_call):
        first = on_tool_call(
            {
                "call_id": "hash_1",
                "tool_name": "runtime_status",
                "arguments": {"config_key": "us"},
            }
        )
        second = on_tool_call(
            {
                "call_id": "hash_2",
                "tool_name": "runtime_status",
                "arguments": {"config_key": "us"},
            }
        )
        assert first["value"] == second["value"]
        assert first["content_hash"] != second["content_hash"]
        admitted = on_tool_call(
            {
                "call_id": "answer_hash",
                "tool_name": "submit_answer",
                "arguments": {
                    "mode": "conceptual",
                    "status": "complete",
                    "answer_markdown": "哈希覆盖完整证据包。",
                    "claims": [],
                },
            }
        )
        return admitted["approved_answer"]["text"]

    result = _run_answered_host(monkeypatch, "检查证据哈希", tool_flow)

    assert result.ok is True


def _near_limit_ascii_blob(factory) -> str:
    low, high = 0, 30_000
    best = ""
    while low <= high:
        midpoint = (low + high) // 2
        candidate = "x" * midpoint
        tokens = copilot_tools.conservative_json_tokens(factory(candidate))
        if tokens <= 3_998:
            best = candidate
            low = midpoint + 1
        else:
            high = midpoint - 1
    assert 3_980 <= copilot_tools.conservative_json_tokens(factory(best)) <= 3_998
    return best


def test_host_final_success_observation_is_bounded_after_protocol_metadata(monkeypatch) -> None:
    template = lambda blob: {
        "tool_name": "runtime_status",
        "ok": True,
        "status": "complete",
        "value": {"blob": blob},
        "coverage": {"status": "complete", "complete_for": "point"},
        "freshness": {"status": "not_applicable"},
    }
    projected = template(_near_limit_ascii_blob(template))
    captured = {}
    monkeypatch.setattr(
        copilot_tools,
        "call_read_tool",
        lambda *_args, **_kwargs: {"ok": True, "data": {}},
    )
    monkeypatch.setattr(
        copilot_tools,
        "compact_observation",
        lambda *_args, **_kwargs: projected,
    )

    def process(_start, *, on_tool_call, **_kwargs):
        captured.update(on_tool_call({
            "call_id": "read_near_limit",
            "tool_name": "runtime_status",
            "arguments": {"config_key": "us"},
        }))
        return {
            "ok": False,
            "error": {
                "code": "MODEL_ERROR",
                "stage": "model",
                "message": "test completed",
                "retryable": False,
            },
        }

    monkeypatch.setattr("src.application.copilot.host.run_pi_agent", process)
    run_contract(_contract("检查最终证据预算"), model_settings=_TEST_MODEL)

    assert captured["status"] == "needs_narrowing"
    assert "tool_call_id" not in captured
    assert copilot_tools.conservative_json_tokens(captured) <= 4_000


def test_host_final_failed_observation_is_bounded_after_ref(monkeypatch) -> None:
    template = lambda blob: {
        "tool_name": "runtime_status",
        "ok": False,
        "status": "failed",
        "error": "READ_ERROR",
        "code": "READ_ERROR",
        "message": "unavailable",
        "retryable": False,
        "details": {"blob": blob},
    }
    projected = template(_near_limit_ascii_blob(template))
    captured = {}
    monkeypatch.setattr(
        copilot_tools,
        "call_read_tool",
        lambda *_args, **_kwargs: {"ok": False, "error": {}},
    )
    monkeypatch.setattr(
        copilot_tools,
        "compact_observation",
        lambda *_args, **_kwargs: projected,
    )

    def process(_start, *, on_tool_call, **_kwargs):
        captured.update(on_tool_call({
            "call_id": "read_failed_near_limit",
            "tool_name": "runtime_status",
            "arguments": {"config_key": "us"},
        }))
        return {
            "ok": False,
            "error": {
                "code": "MODEL_ERROR",
                "stage": "model",
                "message": "test completed",
                "retryable": False,
            },
        }

    monkeypatch.setattr("src.application.copilot.host.run_pi_agent", process)
    run_contract(_contract("检查失败证据预算"), model_settings=_TEST_MODEL)

    assert captured["status"] == "failed"
    assert captured["details"] == {"truncated": True}
    assert "tool_call_id" not in captured
    assert copilot_tools.conservative_json_tokens(captured) <= 4_000


def test_host_does_not_copy_provider_call_id_into_model_observation(monkeypatch) -> None:
    captured = {}
    monkeypatch.setattr(
        copilot_tools,
        "call_read_tool",
        lambda *_args, **_kwargs: {"ok": True, "data": {}},
    )
    monkeypatch.setattr(
        copilot_tools,
        "compact_observation",
        lambda *_args, **_kwargs: {
            "tool_name": "runtime_status",
            "ok": True,
            "status": "complete",
            "value": {"healthy": True},
            "coverage": {"status": "complete", "complete_for": "point"},
            "freshness": {"status": "not_applicable"},
        },
    )

    def process(_start, *, on_tool_call, **_kwargs):
        captured.update(on_tool_call({
            "call_id": "c" * 100_000,
            "tool_name": "runtime_status",
            "arguments": {"config_key": "us"},
        }))
        return {
            "ok": False,
            "error": {
                "code": "MODEL_ERROR",
                "stage": "model",
                "message": "test completed",
                "retryable": False,
            },
        }

    monkeypatch.setattr("src.application.copilot.host.run_pi_agent", process)
    run_contract(_contract("检查 call id 边界"), model_settings=_TEST_MODEL)

    assert captured["status"] == "complete"
    assert "tool_call_id" not in captured
    assert copilot_tools.conservative_json_tokens(captured) <= 4_000
