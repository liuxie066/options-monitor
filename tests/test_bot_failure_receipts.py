"""Failed real reads stay diagnostics through Host persistence and receipts."""
from __future__ import annotations

from dataclasses import replace
import json

from src.application.agent_tools import candidate
from src.application.assistant.capability_catalog import preview_operation_capabilities
from src.application.bot.control_handoff import CONTROL_PREVIEW_TOOL
from src.application.bot.event_store import incomplete_progress_response, safe_failure_cause
from src.application.bot.host import run_contract
from src.application.bot.host_store import BotHostStore
from tests.bot_pi_test_support import _TEST_MODEL
from tests.test_bot_memory_host import channel
from tests.test_bot_phase1 import _contract


def _failed_run(monkeypatch, contract, store, flow, **kwargs):
    failures = []

    def process(_start, *, on_tool_call, on_event, **_kwargs):
        try:
            flow(on_tool_call)
        except Exception as exc:
            failures.append(exc)
            raise
        on_event({"event_type": "forced_final_activated", "data": {"reason": "tool_call_limit"}})
        return {"ok": False, "error": {"code": "BUDGET_EXHAUSTED", "stage": "model", "message": "fixture stop"}}

    monkeypatch.setattr("src.application.bot.host.run_pi_agent", process)
    result = run_contract(contract, model_settings=_TEST_MODEL, host_store=store, **kwargs)
    if failures:
        raise failures[0]
    assert not result.ok
    row = store.run_record(result.run_id)
    progress = json.loads(row["progress_json"])
    assert json.loads(row["response_json"])["user_response"] == result.user_response
    return result, progress


def test_real_missing_candidate_reads_and_rejected_submission_reach_durable_receipt(tmp_path, monkeypatch):
    monkeypatch.setattr(candidate, "repo_base", lambda: tmp_path)
    monkeypatch.setattr(candidate, "load_runtime_config", lambda **_: (tmp_path / "config.us.json", {}))
    store = BotHostStore(tmp_path / "host.db")
    observations = []

    def flow(call):
        for tool, arguments in (
            ("candidate_filter_explain", {"symbol": "PDD"}),
            ("candidate_rank_explain", {"mode": "put", "top_n": 1}),
        ):
            observation = call({"call_id": tool, "tool_name": tool,
                                "arguments": {"account": "lx", "run_id": "missing-run", **arguments}})
            observations.append(observation)
            assert observation["ok"] is False
            assert observation["code"] == "DEPENDENCY_MISSING"
            assert observation["details"]["account"] == "lx"
            assert observation["details"]["run_id"] == "missing-run"
            assert observation["retryable"] is False
        rejected = call({"call_id": "answer", "tool_name": "submit_answer", "arguments": {
            "mode": "evidence", "status": "complete", "answer_markdown": "PDD 已全部过滤。",
            "claims": [{"text": "PDD 已全部过滤", "kind": "historical_fact", "required_scope": "point",
                        "observation_ids": [observations[0]["ref"]]}],
        }})
        assert rejected["observation"]["ok"] is False
        assert rejected["observation"]["reason"] == "observation_outside_request"

    result, progress = _failed_run(monkeypatch, _contract("解释 PDD 的历史筛选"), store, flow)
    checks = progress["completed_checks"]
    assert {key: checks[key] for key in ("failed_count", "failed_read_count", "failed_submission_count", "failed_internal_count", "read_count")} == {
        "failed_count": 3, "failed_read_count": 2, "failed_submission_count": 1,
        "failed_internal_count": 0, "read_count": 0,
    }
    assert progress["evidence_refs"] == [] and progress["revision"] == 1
    assert len(checks["failure_causes"]) == 3
    for cause in checks["failure_causes"][:2]:
        assert cause["category"] == "read" and cause["account"] == "lx"
        assert cause["run_id"] == "missing-run" and cause["retryable"] is False
        assert cause["code"] == "DEPENDENCY_MISSING" and cause["hint"]
    assert checks["failure_causes"][2]["category"] == "submission"
    assert "2 次读取失败，1 次答案提交失败" in result.user_response
    assert "3 次读取失败" not in result.user_response
    assert "missing-run" in result.user_response and "PDD 已全部过滤" not in result.user_response
    assert "未安排自动重试" in result.user_response and "当前条件下不宜原样重试" in result.user_response
    assert f"继续 {result.run_id}" in result.user_response
    # Finished-result replay must retain the receipt and never recompute progress.
    before = store.run_record(result.run_id)
    replay = store.finish_run(replace(result, user_response="forged retry text"))
    assert replay.user_response == result.user_response
    assert store.run_record(result.run_id) == before


def test_directory_control_memory_and_unknown_failures_are_internal(tmp_path, monkeypatch):
    contract, store, session = channel(tmp_path, monkeypatch, "查询记忆并解释当前工具失败")

    def flow(call):
        for index, (name, arguments) in enumerate((
            ("tool_directory", {"catalog_hash": "wrong", "tool_names": ["runtime_status"]}),
            (CONTROL_PREVIEW_TOOL, {"intent_name": "not_a_control_operation", "arguments": {}}),
            ("bot_memory", {"action": "not_a_memory_action"}),
            ("historical_unknown_tool", {}),
        )):
            reply = call({"call_id": f"internal-{index}", "tool_name": name, "arguments": arguments})
            assert reply.get("observation", reply)["ok"] is False

    result, progress = _failed_run(monkeypatch, contract, store, flow,
                                   session_key=session, control_preview_specs=preview_operation_capabilities())
    checks = progress["completed_checks"]
    assert checks["failed_read_count"] == checks["failed_submission_count"] == 0
    assert checks["failed_internal_count"] == 4
    assert checks["failed_count"] == 3  # Preserve the legacy mixed count, which excluded memory events.
    assert checks["read_count"] == 0 and progress["evidence_refs"] == []
    assert len(checks["failure_causes"]) == 3
    assert {cause["tool_name"] for cause in checks["failure_causes"]} == {"tool_directory", CONTROL_PREVIEW_TOOL, "bot_memory"}
    assert all(cause["category"] == "internal" for cause in checks["failure_causes"])
    assert any(event.type == "memory_tool_result" and event.payload["ok"] is False for event in result.events)
    assert "4 次内部工具失败" in result.user_response and "次读取失败" not in result.user_response


def test_legacy_stored_mixed_count_renders_generic_without_rewriting(tmp_path):
    store = BotHostStore(tmp_path / "host.db")
    contract = _contract("解释历史失败")
    store.start_run("legacy-run", contract=contract, session_key=None)
    legacy = {"progress_ref": "legacy-run", "revision": 7, "goal": "解释历史失败",
              "completed_checks": {"read_count": 0, "partial_count": 0, "failed_count": 3}}
    with store._connect() as conn:
        conn.execute("UPDATE bot_runs SET progress_json=? WHERE run_id=?", (json.dumps(legacy), "legacy-run"))
    before = store.run_record("legacy-run")
    response = incomplete_progress_response("未完成", json.loads(before["progress_json"]))
    assert "3 次工具失败（历史记录未分类）" in response
    assert "3 次读取失败" not in response
    assert store.run_record("legacy-run") == before


def test_failure_causes_are_bounded_allowlisted_and_redacted():
    cause = safe_failure_cause({
        "tool_name": "candidate_rank_explain", "code": "DEPENDENCY_MISSING",
        "message": "raw balance=999999", "hint": "token=FIXTURE_SECRET " + "next " * 100,
        "details": {"account": "lx", "run_id": "missing-run", "reason": "snapshot_unavailable",
                    "retryable": False, "balance": 999999, "path": "/private/secret", "exception": "raw"},
    }, "read")
    assert cause["account"] == "lx" and cause["run_id"] == "missing-run" and cause["retryable"] is False
    assert len(cause["hint"]) <= 240
    encoded = json.dumps(cause)
    assert all(value not in encoded for value in ("999999", "/private/secret", "FIXTURE_SECRET", "exception"))
    assert set(cause) <= {"category", "tool_name", "code", "reason", "account", "run_id", "retryable", "hint"}


def test_failed_candidate_diagnostics_follow_current_account_permissions(tmp_path, monkeypatch):
    from src.application.bot.memory import scope_from_contract
    contract, store, session = channel(tmp_path, monkeypatch, "解释 PDD")
    monkeypatch.setattr(candidate, "repo_base", lambda: tmp_path)
    monkeypatch.setattr(candidate, "load_runtime_config", lambda **_: (tmp_path / "config.us.json", {}))
    def flow(call):
        obs = call({"call_id": "read", "tool_name": "candidate_rank_explain", "arguments": {
            "account": "lx", "run_id": "missing-run", "mode": "put", "top_n": 1}})
        assert obs["ok"] is False
    _, progress = _failed_run(monkeypatch, contract, store, flow, session_key=session)
    assert progress["accounts"] == ["lx"]
    assert len(store.unfinished_progress(scope_from_contract(contract, ["lx"]))) == 1
    assert store.unfinished_progress(scope_from_contract(contract, ["sy"])) == []
