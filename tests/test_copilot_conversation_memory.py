from __future__ import annotations

import json
import sqlite3
import threading
from argparse import Namespace

import pytest

from src.application.copilot import channel_facade, local_harness, tools as copilot_tools
from src.application.copilot.contracts import AppResult, CopilotRequest, CopilotScope, new_id
from tests.copilot_pi_test_support import (
    _TEST_MODEL,
    ModelRequest,
    ModelTurn,
    ToolCall,
    fake_pi_agent,
    run_contract,
)
from src.application.copilot.host_store import CopilotHostStore
from src.application.copilot.service import prepare_contract
from src.interfaces.cli.copilot_ops import _successful_observations, handle_copilot_command
from src.infrastructure.pi_agent_process import derive_pi_session_id


def test_successful_channel_answer_uses_opaque_pi_session_without_legacy_write(
    monkeypatch, tmp_path
) -> None:
    database = tmp_path / "copilot.sqlite3"
    captured: dict[str, object] = {}
    monkeypatch.setattr(channel_facade, "_channel_model_gate", lambda _path: None)

    def fake_run(_prepared, **kwargs):  # type: ignore[no-untyped-def]
        captured.update(kwargs)
        return AppResult(status="answered", user_response="结论：运行正常。")

    monkeypatch.setattr(
        channel_facade,
        "run_prepared_contract",
        fake_run,
    )

    result = channel_facade.run_channel_request(
        user_message="检查运行状态",
        config_key="us",
        channel="feishu",
        sender_id="ou_1",
        conversation_id="chat_1",
        host_db_path=str(database),
    )

    assert result.status == "answered"
    assert captured["session_key"] == derive_pi_session_id(
        "feishu", "ou_1", "chat_1", "key:us"
    )
    assert CopilotHostStore(database).session_turns("feishu:chat_1") == ()


def test_channel_path_scope_is_canonical_and_sender_scoped(tmp_path) -> None:
    first = tmp_path / "config-a.json"
    second = tmp_path / "config-b.json"
    alias = tmp_path / "config-alias.json"
    first.write_text("{}", encoding="utf-8")
    second.write_text("{}", encoding="utf-8")
    alias.symlink_to(first)

    _, canonical, scope = channel_facade._resolve_authority_scope(
        config_key=None, config_path=str(alias)
    )
    _, _, same_scope = channel_facade._resolve_authority_scope(
        config_key=None, config_path=str(first)
    )
    _, _, other_scope = channel_facade._resolve_authority_scope(
        config_key=None, config_path=str(second)
    )

    assert canonical == str(first.resolve())
    assert scope == same_scope
    assert scope != other_scope
    first_session = channel_facade._channel_session_key(
        channel="feishu",
        sender_id="ou_1",
        conversation_id="group_1",
        authority_scope=scope,
    )
    assert first_session != channel_facade._channel_session_key(
        channel="feishu",
        sender_id="ou_2",
        conversation_id="group_1",
        authority_scope=scope,
    )
    assert first_session != channel_facade._channel_session_key(
        channel="feishu",
        sender_id="ou_1",
        conversation_id="group_1",
        authority_scope=other_scope,
    )
    assert channel_facade._channel_session_key(
        channel="feishu",
        sender_id="ou_1",
        conversation_id=None,
        authority_scope=scope,
    ) == derive_pi_session_id("feishu", "ou_1", "sender:ou_1", scope)


def test_invalid_channel_identity_or_scope_fails_before_model_gate(monkeypatch, tmp_path) -> None:
    invoked = False
    directory = tmp_path / "config-directory"
    directory.mkdir()

    def unexpected_gate(_path):
        nonlocal invoked
        invoked = True
        return None

    monkeypatch.setattr(channel_facade, "_channel_model_gate", unexpected_gate)
    cases = (
        {"config_key": "us", "sender_id": ""},
        {"config_key": "us", "config_path": str(tmp_path / "missing.json")},
        {"config_key": None, "config_path": str(tmp_path / "missing.json")},
        {"config_key": None, "config_path": str(directory)},
    )

    for case in cases:
        request = {
            "user_message": "检查运行状态",
            "channel": "feishu",
            "sender_id": "ou_1",
            "conversation_id": "group_1",
            **case,
        }
        result = channel_facade.run_channel_request(**request)
        assert result.error == {
            "code": "CHANNEL_NOT_READY",
            "reason": "channel_identity_or_scope_invalid",
        }
    assert invoked is False


def test_channel_config_path_stays_out_of_model_visible_context(monkeypatch, tmp_path) -> None:
    config_path = tmp_path / "private" / "config.us.json"
    config_path.parent.mkdir()
    config_path.write_text("{}", encoding="utf-8")
    captured: dict[str, object] = {}
    monkeypatch.setattr(channel_facade, "_channel_model_gate", lambda _path: None)

    def fake_run(prepared, **kwargs):  # type: ignore[no-untyped-def]
        captured["prepared"] = prepared
        captured.update(kwargs)
        return AppResult(status="answered", user_response="结论：运行正常。")

    monkeypatch.setattr(channel_facade, "run_prepared_contract", fake_run)
    result = channel_facade.run_channel_request(
        user_message="检查运行状态",
        config_key=None,
        config_path=str(config_path),
        channel="feishu",
        sender_id="ou_1",
        conversation_id="group_1",
        host_db_path=str(tmp_path / "audit.sqlite3"),
    )

    prepared = captured["prepared"]
    canonical = str(config_path.resolve())
    assert result.status == "answered"
    assert prepared.input["config_path"] == canonical
    assert canonical not in json.dumps(prepared.input["messages"], ensure_ascii=False)
    assert canonical not in str(captured["session_key"])


def test_host_store_migrates_existing_session_schema(tmp_path) -> None:
    path = tmp_path / "copilot.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE copilot_sessions (session_key TEXT PRIMARY KEY, messages_json TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO copilot_sessions VALUES ('legacy', '[{\"role\":\"user\",\"content\":\"hello\"}]', 'now')"
        )

    store = CopilotHostStore(path)

    assert store.session_turns("legacy") == ()
    with sqlite3.connect(path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(copilot_sessions)")}
        messages_json = conn.execute(
            "SELECT messages_json FROM copilot_sessions WHERE session_key = 'legacy'"
        ).fetchone()
    assert {"messages_json", "turns_json", "memory_json"}.issubset(columns)
    assert messages_json == ('[{"role":"user","content":"hello"}]',)


def test_failed_read_only_run_resumes_with_recovered_observation(monkeypatch, tmp_path) -> None:
    store = CopilotHostStore(tmp_path / "copilot.sqlite3")
    turns = iter(
        (
            ModelTurn(
                tool_calls=(ToolCall("call_1", "runtime_status", {"config_key": "us"}),),
            ),
            RuntimeError("provider unavailable"),
        )
    )
    monkeypatch.setattr(
        copilot_tools,
        "call_read_tool",
        lambda _name, _payload, *, allowed_tools: {"ok": True, "data": {"status": "healthy"}},
    )

    def failing_model(_request):
        item = next(turns)
        if isinstance(item, Exception):
            raise item
        return item

    failed = run_contract(
        _contract("运行状态"),
        model_runner=failing_model,
        host_store=store,
        session_key="wechat:chat",
    )
    source = store.resume_source(failed.run_id)
    assert source is not None
    contract, events, session_key = source
    recovered = tuple(
        dict(item["payload"])
        for item in events
        if item.get("type") == "tool_result" and item.get("payload", {}).get("ok")
    )

    resumed = run_contract(
        contract,
        model_runner=lambda request: ModelTurn(
            text=(
                "结论：恢复后确认运行正常。"
                if any("<om-recovered-observations>" in str(item.get("content") or "") for item in request.messages)
                else ""
            )
        ),
        host_store=store,
        session_key=session_key,
        resumed_from=failed.run_id,
        recovered_observations=recovered,
    )

    assert resumed.status == "answered"
    assert "恢复后" in resumed.user_response
    assert store.run_record(resumed.run_id)["resumed_from"] == failed.run_id
    failed_scene = next(
        item["payload"]
        for item in store.run_events(failed.run_id)
        if item["type"] == "scene_prepared"
    )
    resumed_scene = next(
        item["payload"]
        for item in store.run_events(resumed.run_id)
        if item["type"] == "scene_prepared"
    )
    assert resumed_scene["scene_version"] == "v5"
    assert resumed_scene["compiled_prompt_sha256"] == failed_scene["compiled_prompt_sha256"]
    assert resumed_scene["tool_schema_sha256"] == failed_scene["tool_schema_sha256"]


def test_cancel_request_only_updates_active_run(tmp_path) -> None:
    store = CopilotHostStore(tmp_path / "copilot.sqlite3")
    contract = _contract("运行状态")
    store.start_run("run_active", contract=contract, session_key="wechat:chat")

    assert store.request_cancel("run_active") is True
    assert store.is_cancel_requested("run_active") is True
    assert store.request_cancel("missing") is False


def test_cancel_command_reports_closed_admission_boundary(tmp_path) -> None:
    payload = handle_copilot_command(
        Namespace(
            copilot_command="cancel",
            host_db=str(tmp_path / "copilot.sqlite3"),
            run_id="missing",
        )
    )

    assert payload["status"] == "not_ready"
    assert payload["user_response"] == "该运行不存在、已作出准入决定或已进入终态。"


def test_recovery_observations_are_success_only_bounded_and_redacted() -> None:
    events = [
        {
            "type": "tool_result",
            "payload": {"ok": True, "ref": f"obs_{index}", "api_key": f"secret-{index}"},
        }
        for index in range(10)
    ]
    events.extend(
        (
            {"type": "tool_result", "payload": {"ok": False, "ref": "failed", "secret": "no"}},
            {"type": "tool_call", "payload": {"ok": True, "ref": "not-a-result"}},
        )
    )

    recovered = _successful_observations(events)

    assert [item["ref"] for item in recovered] == [f"obs_{index}" for index in range(2, 10)]
    assert "secret-" not in json.dumps(recovered, ensure_ascii=False)


def test_cancel_and_commit_compare_and_set_have_exactly_one_winner(tmp_path) -> None:
    path = tmp_path / "copilot.sqlite3"
    starter = CopilotHostStore(path)
    cancel_store = CopilotHostStore(path)
    decision_store = CopilotHostStore(path)

    for index in range(12):
        run_id = f"run_race_{index}"
        starter.start_run(run_id, contract=_contract("运行状态"), session_key="wechat:chat")
        barrier = threading.Barrier(3)
        outcome: dict[str, object] = {}

        def cancel() -> None:
            barrier.wait()
            outcome["cancel"] = cancel_store.request_cancel(run_id)

        def commit() -> None:
            barrier.wait()
            outcome["decision"] = decision_store.claim_admission_decision(run_id, "commit")

        workers = [threading.Thread(target=cancel), threading.Thread(target=commit)]
        for worker in workers:
            worker.start()
        barrier.wait()
        for worker in workers:
            worker.join(timeout=2)
            assert not worker.is_alive()

        state = str(starter.run_record(run_id)["admission_state"])
        assert state in {"cancel", "commit"}
        if state == "cancel":
            assert outcome == {"cancel": True, "decision": "cancel"}
        else:
            assert outcome == {"cancel": False, "decision": "commit"}


def test_host_admission_persists_commit_and_discard(tmp_path) -> None:
    store = CopilotHostStore(tmp_path / "copilot.sqlite3")

    accepted = run_contract(
        _contract("运行状态"),
        model_runner=lambda _request: ModelTurn(text="结论：运行正常。"),
        host_store=store,
    )
    rejected = run_contract(
        _contract("运行状态"),
        model_runner=lambda _request: ModelTurn(text="```markdown\n未闭合"),
        host_store=store,
    )

    assert accepted.status == "answered"
    assert store.run_record(accepted.run_id)["admission_state"] == "commit"
    assert rejected.status == "failed"
    assert rejected.error["code"] == "RESULT_REJECTED"
    assert store.run_record(rejected.run_id)["admission_state"] == "discard"


def test_unknown_session_commit_is_private_safe_and_not_resumable(monkeypatch, tmp_path) -> None:
    store = CopilotHostStore(tmp_path / "copilot.sqlite3")
    secret = "provider-secret-that-must-not-escape"

    def exits_after_commit(_start, *, on_proposed, **_kwargs):
        decision = on_proposed(
            {
                "status": "answered",
                "text": "结论：运行正常。",
                "control_request": None,
                "termination_reason": "stop",
                "usage": {},
            }
        )
        assert decision == "commit"
        return {
            "ok": False,
            "error": {
                "code": "PI_PROCESS_EXITED",
                "stage": "process",
                "message": secret,
                "retryable": True,
            },
        }

    monkeypatch.setattr("src.application.copilot.host.run_pi_agent", exits_after_commit)
    result = run_contract(_contract("运行状态"), model_settings=_TEST_MODEL, host_store=store)
    events = store.run_events(result.run_id)

    assert result.status == "failed"
    assert result.error == {"code": "MODEL_ERROR"}
    assert secret not in json.dumps(result.decision_trace, ensure_ascii=False)
    assert any(
        item["payload"].get("session_commit_outcome") == "unknown"
        for item in events
        if item["type"] == "model_error"
    )
    assert store.run_record(result.run_id)["admission_state"] == "commit"
    assert store.resume_source(result.run_id) is None


def test_process_adapter_exception_finishes_host_run_once(monkeypatch, tmp_path) -> None:
    store = CopilotHostStore(tmp_path / "copilot.sqlite3")

    def broken_process(*_args, **_kwargs):
        raise RuntimeError("provider-secret-that-must-not-escape")

    monkeypatch.setattr("src.application.copilot.host.run_pi_agent", broken_process)
    result = run_contract(_contract("运行状态"), model_settings=_TEST_MODEL, host_store=store)

    record = store.run_record(result.run_id)
    assert result.status == "failed"
    assert result.error == {"code": "INTERNAL_ERROR"}
    assert record["status"] == "failed"
    assert sum(item["type"] == "final_result" for item in store.run_events(result.run_id)) == 1
    assert "provider-secret" not in json.dumps(result.decision_trace, ensure_ascii=False)


def test_cancel_winner_overrides_concurrent_process_failure(monkeypatch, tmp_path) -> None:
    store = CopilotHostStore(tmp_path / "copilot.sqlite3")

    def failed_after_cancel(_start, *, run_id, **_kwargs):
        assert store.request_cancel(run_id) is True
        return {
            "ok": False,
            "error": {
                "code": "MODEL_ERROR",
                "stage": "model",
                "message": "provider failed",
                "retryable": True,
            },
        }

    monkeypatch.setattr("src.application.copilot.host.run_pi_agent", failed_after_cancel)
    result = run_contract(_contract("运行状态"), model_settings=_TEST_MODEL, host_store=store)

    assert result.status == "cancelled"
    assert result.error == {"code": "CANCELLED"}
    assert store.run_record(result.run_id)["admission_state"] == "cancel"
    assert any(item["type"] == "run_cancelled" for item in store.run_events(result.run_id))


def test_cancelled_final_cannot_overwrite_a_committed_admission(monkeypatch, tmp_path) -> None:
    store = CopilotHostStore(tmp_path / "copilot.sqlite3")

    def contradictory_child(_start, *, on_proposed, **_kwargs):
        decision = on_proposed(
            {
                "status": "answered",
                "text": "结论：运行正常。",
                "control_request": None,
                "termination_reason": "stop",
                "usage": {},
            }
        )
        assert decision == "commit"
        return {
            "ok": True,
            "result": {
                "status": "cancelled",
                "text": "",
                "control_request": None,
                "termination_reason": "aborted",
                "usage": {},
                "committed": False,
            },
        }

    monkeypatch.setattr("src.application.copilot.host.run_pi_agent", contradictory_child)
    result = run_contract(_contract("运行状态"), model_settings=_TEST_MODEL, host_store=store)

    assert result.status == "failed"
    assert result.error == {"code": "INTERNAL_ERROR"}
    assert result.decision_trace["pi_process"]["session_commit_outcome"] == "unknown"
    assert store.run_record(result.run_id)["admission_state"] == "commit"


def test_late_tool_callback_cannot_mutate_finished_host_run(monkeypatch, tmp_path) -> None:
    store = CopilotHostStore(tmp_path / "copilot.sqlite3")
    release = threading.Event()
    done = threading.Event()
    late_result: list[dict] = []
    executed = False

    def read_tool(*_args, **_kwargs):
        nonlocal executed
        executed = True
        return {"ok": True, "data": {}}

    def process(_start, *, on_tool_call, **_kwargs):
        def late_call() -> None:
            release.wait(timeout=2)
            late_result.append(
                on_tool_call(
                    {"call_id": "late_1", "tool_name": "runtime_status", "arguments": {}}
                )
            )
            done.set()

        threading.Thread(target=late_call, daemon=True).start()
        return {
            "ok": False,
            "error": {
                "code": "PI_PROCESS_TIMEOUT",
                "stage": "deadline",
                "message": "timed out",
                "retryable": True,
            },
        }

    monkeypatch.setattr(copilot_tools, "call_read_tool", read_tool)
    monkeypatch.setattr("src.application.copilot.host.run_pi_agent", process)
    result = run_contract(_contract("运行状态"), model_settings=_TEST_MODEL, host_store=store)
    before = store.run_events(result.run_id)
    release.set()
    assert done.wait(timeout=2)
    after = store.run_events(result.run_id)

    assert result.status == "failed"
    assert late_result[0]["error"] == "CANCELLED"
    assert executed is False
    assert after == before


def test_tool_boundary_stops_run_when_cancelled_during_read(monkeypatch) -> None:
    cancelled = False
    turns = iter((ModelTurn(tool_calls=(ToolCall("call_1", "runtime_status", {}),)),))

    def read_tool(_name, _payload, *, allowed_tools):
        nonlocal cancelled
        cancelled = True
        return {"ok": True, "data": {"status": "healthy"}}

    monkeypatch.setattr(copilot_tools, "call_read_tool", read_tool)
    result = run_contract(
        _contract("运行状态"),
        model_runner=lambda _request: next(turns),
        is_cancelled=lambda: cancelled,
    )

    assert result.status == "cancelled"
    assert result.error["code"] == "CANCELLED"


def test_stale_run_is_interrupted_and_resume_attempts_are_bounded(tmp_path) -> None:
    store = CopilotHostStore(tmp_path / "copilot.sqlite3")
    contract = _contract("运行状态")
    store.start_run("run_stale", contract=contract, session_key="wechat:chat")
    with store._connect() as conn:
        conn.execute(
            "UPDATE copilot_runs SET started_at = '2000-01-01T00:00:00+00:00' WHERE run_id = 'run_stale'"
        )

    assert store.mark_stale_runs_interrupted(older_than_seconds=1) == 1
    record = store.run_record("run_stale")
    assert record["status"] == "interrupted"
    assert record["termination_reason"] == "host_restart_or_stale_run"
    assert store.resume_source("run_stale", max_attempts=2) is not None
    assert store.resume_source("run_stale", max_attempts=2) is not None
    assert store.resume_source("run_stale", max_attempts=2) is None


def test_stale_run_claimed_commit_is_not_resumable(tmp_path) -> None:
    store = CopilotHostStore(tmp_path / "copilot.sqlite3")
    store.start_run("run_stale_commit", contract=_contract("运行状态"), session_key="wechat:chat")
    assert store.claim_admission_decision("run_stale_commit", "commit") == "commit"
    with store._connect() as conn:
        conn.execute(
            "UPDATE copilot_runs SET started_at = '2000-01-01T00:00:00+00:00' "
            "WHERE run_id = 'run_stale_commit'"
        )

    assert store.mark_stale_runs_interrupted(older_than_seconds=1) == 1
    assert store.resume_source("run_stale_commit") is None


def test_stale_run_claimed_cancel_is_not_resumable(tmp_path) -> None:
    store = CopilotHostStore(tmp_path / "copilot.sqlite3")
    store.start_run("run_stale_cancel", contract=_contract("运行状态"), session_key="wechat:chat")
    assert store.request_cancel("run_stale_cancel") is True
    with store._connect() as conn:
        conn.execute(
            "UPDATE copilot_runs SET started_at = '2000-01-01T00:00:00+00:00' "
            "WHERE run_id = 'run_stale_cancel'"
        )

    assert store.mark_stale_runs_interrupted(older_than_seconds=1) == 1
    assert store.resume_source("run_stale_cancel") is None


def test_run_trace_persists_iteration_usage_and_termination(tmp_path) -> None:
    store = CopilotHostStore(tmp_path / "copilot.sqlite3")
    result = run_contract(
        _contract("运行状态"),
        model_runner=lambda _request: ModelTurn(
            text="结论：运行正常。",
            finish_reason="stop",
            usage={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        ),
        host_store=store,
        session_key="wechat:chat",
    )

    record = store.run_record(result.run_id)
    metrics = __import__("json").loads(record["metrics_json"])
    events = store.run_events(result.run_id)
    started_event = next(item for item in events if item["type"] == "model_turn_started")
    completed_event = next(item for item in events if item["type"] == "model_turn_completed")
    scene_event = next(item for item in events if item["type"] == "scene_prepared")
    assert started_event["payload"] == {"iteration": 1}
    assert completed_event["payload"]["usage_total"]["totalTokens"] == 15
    assert len(scene_event["payload"]["compiled_prompt_sha256"]) == 64
    assert metrics["model_turn_count"] == 1
    assert metrics["usage"]["totalTokens"] == 15
    assert record["termination_reason"] == "completed"


def test_committed_compaction_usage_survives_later_process_failure(monkeypatch, tmp_path) -> None:
    store = CopilotHostStore(tmp_path / "copilot.sqlite3")
    usage = {"input": 7, "output": 3, "cacheRead": 0, "cacheWrite": 0, "totalTokens": 10}

    def process(_start, *, on_event, **_kwargs):
        on_event(
            {
                "event_type": "context_compaction_committed",
                "data": {"compaction_count": 1, "usage_total": usage},
            }
        )
        return {
            "ok": False,
            "error": {
                "code": "PI_PROCESS_EXITED",
                "stage": "process",
                "message": "child exited",
                "retryable": True,
            },
        }

    monkeypatch.setattr("src.application.copilot.host.run_pi_agent", process)
    result = run_contract(_contract("运行状态"), model_settings=_TEST_MODEL, host_store=store)

    assert result.status == "failed"
    assert json.loads(store.run_record(result.run_id)["metrics_json"])["usage"] == usage


def test_run_progress_exposes_only_coarse_public_labels(monkeypatch, tmp_path) -> None:
    store = CopilotHostStore(tmp_path / "copilot.sqlite3")
    turns = iter(
        (
            ModelTurn(tool_calls=(ToolCall("call_status", "runtime_status", {"config_key": "us"}),)),
            ModelTurn(text="结论：运行正常。"),
        )
    )
    monkeypatch.setattr(
        copilot_tools,
        "call_read_tool",
        lambda _name, _payload, *, allowed_tools: {"ok": True, "data": {"secret_detail": "hidden"}},
    )

    result = run_contract(
        _contract("运行状态"),
        model_runner=lambda _request: next(turns),
        host_store=store,
        session_key="wechat:chat",
    )

    progress = store.run_progress(result.run_id)
    labels = [item["label"] for item in progress]
    assert "正在读取数据" in labels
    assert "正在整理结论" in labels
    assert labels[-1] == "执行完成"
    assert all(set(item) == {"event_id", "type", "label", "timestamp"} for item in progress)
    assert "hidden" not in str(progress)
    assert "运行正常" not in str(progress)
    assert "scene_prepared" not in {item["type"] for item in progress}


def test_reply_outbox_is_idempotent_retryable_and_deliverable(tmp_path) -> None:
    store = CopilotHostStore(tmp_path / "copilot.sqlite3")
    first = store.enqueue_reply(
        delivery_key="wechat:command-1",
        channel="wechat",
        session_key="wechat:chat",
        run_id="run_1",
        payload={"text": "结论", "context_token": "reusable-private-capability"},
    )
    second = store.enqueue_reply(
        delivery_key="wechat:command-1",
        channel="wechat",
        payload={"text": "不应覆盖"},
    )
    assert first["payload_json"] == second["payload_json"]

    claimed = store.claim_reply(delivery_key="wechat:command-1")
    assert claimed["status"] == "delivering"
    assert claimed["attempt_count"] == 1
    assert store.mark_reply_failed(
        "wechat:command-1",
        error="temporary",
        retryable=True,
        retry_after_seconds=1,
    )
    assert store.claim_reply(delivery_key="wechat:command-1", before="9999-01-01T00:00:00+00:00")
    assert store.mark_reply_delivered("wechat:command-1")
    assert store.claim_reply(delivery_key="wechat:command-1", before="9999-01-01T00:00:00+00:00") is None
    delivered = store.list_replies()[0]
    assert delivered["status"] == "delivered"
    assert delivered["payload_json"] == "{}"
    assert "reusable-private-capability" not in str(delivered)


def test_reply_outbox_scrubs_capability_after_terminal_failure(tmp_path) -> None:
    store = CopilotHostStore(tmp_path / "copilot.sqlite3")
    store.enqueue_reply(
        delivery_key="wechat:terminal",
        channel="wechat",
        payload={"text": "结论", "context_token": "terminal-private-capability"},
    )
    assert store.claim_reply(delivery_key="wechat:terminal") is not None
    assert store.mark_reply_failed("wechat:terminal", error="provider reflected a secret", retryable=False)

    terminal = store.list_replies()[0]
    assert terminal["status"] == "terminal_failed"
    assert terminal["payload_json"] == "{}"
    assert terminal["last_error"] == "terminal_delivery_error"
    assert "terminal-private-capability" not in str(terminal)
    assert "provider reflected a secret" not in str(terminal)


def test_reply_outbox_recovers_expired_delivery_claim(tmp_path) -> None:
    store = CopilotHostStore(tmp_path / "copilot.sqlite3")
    store.enqueue_reply(
        delivery_key="wechat:command-crashed",
        channel="wechat",
        payload={"text": "结论"},
    )
    assert store.claim_reply(delivery_key="wechat:command-crashed") is not None
    with store._connect() as conn:
        conn.execute(
            "UPDATE copilot_reply_outbox SET updated_at = '2000-01-01T00:00:00+00:00' WHERE delivery_key = ?",
            ("wechat:command-crashed",),
        )

    recovered = store.claim_reply(delivery_key="wechat:command-crashed")

    assert recovered is not None
    assert recovered["status"] == "delivering"
    assert recovered["attempt_count"] == 2


def test_lane_limit_and_expired_lease_recovery(tmp_path) -> None:
    store = CopilotHostStore(tmp_path / "copilot.sqlite3")
    assert store.acquire_lane("chat_read", "lease_1", limit=1, ttl_seconds=60)
    assert not store.acquire_lane("chat_read", "lease_2", limit=1, ttl_seconds=60)
    store.release_lane("chat_read", "lease_1")
    assert store.acquire_lane("chat_read", "lease_2", limit=1, ttl_seconds=60)
    with store._connect() as conn:
        conn.execute(
            "UPDATE copilot_lane_leases SET expires_at = '2000-01-01T00:00:00+00:00' WHERE lease_id = 'lease_2'"
        )
    assert store.acquire_lane("chat_read", "lease_3", limit=1, ttl_seconds=60)


def test_channel_capacity_exhaustion_does_not_invoke_model_runtime(monkeypatch, tmp_path) -> None:
    database = tmp_path / "copilot.sqlite3"
    store = CopilotHostStore(database)
    assert store.acquire_lane("chat_read", "occupied_1", limit=2, ttl_seconds=60)
    assert store.acquire_lane("chat_read", "occupied_2", limit=2, ttl_seconds=60)
    invoked = False

    def unexpected_run(*_args, **_kwargs):
        nonlocal invoked
        invoked = True
        raise AssertionError("model runtime must not run when the lane is full")

    monkeypatch.setattr(channel_facade, "_channel_model_gate", lambda _path: None)
    monkeypatch.setattr(channel_facade, "run_prepared_contract", unexpected_run)

    result = channel_facade.run_channel_request(
        user_message="7月收益",
        config_key="us",
        channel="wechat",
        sender_id="user_1",
        conversation_id="chat_1",
        host_db_path=str(database),
    )

    assert result.status == "not_ready"
    assert result.error == {"code": "CHANNEL_NOT_READY", "reason": "channel_capacity_exhausted"}
    assert invoked is False


def test_malformed_tool_arguments_are_traceable_and_recoverable() -> None:
    turns = iter(
        (
            ModelTurn(
                tool_calls=(
                    ToolCall(
                        "call_bad_json",
                        "runtime_status",
                        {"__invalid_arguments__": "{" + "x" * 800},
                    ),
                ),
            ),
            ModelTurn(text="结论：工具参数错误，未读取到运行状态。"),
        )
    )

    result = run_contract(_contract("运行状态"), model_runner=lambda _request: next(turns))

    assert result.status == "answered"
    observation = next(event for event in result.events if event.type == "tool_result")
    assert observation.payload["tool_call_id"] == "call_bad_json"
    assert observation.payload["error"] == "INPUT_ERROR"
    assert "x" * 100 not in str(observation.payload)


def _contract(text: str):
    prepared = prepare_contract(
        CopilotRequest(
            request_id=new_id("req"),
            source_entry="test",
            user_message=text,
            explicit_scope=CopilotScope(config_key="us"),
            execution_environment="local",
        ),
        reference_year=2026,
    )
    assert not isinstance(prepared, AppResult)
    return prepared
