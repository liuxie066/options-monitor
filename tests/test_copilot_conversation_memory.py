from __future__ import annotations

import sqlite3

from src.application.copilot import tools as copilot_tools
from src.application.copilot import channel_facade
from src.application.copilot.agent import ModelRequest, ModelTurn, ToolCall
from src.application.copilot.contracts import AppResult, CopilotRequest, new_id
from src.application.copilot.conversation_memory import prepare_contract_with_memory
from src.application.copilot.host import run_contract
from src.application.copilot.host_store import CopilotHostStore
from src.application.copilot.model_client import CopilotModelSettings, build_model_runner
from src.application.copilot.service import prepare_contract


def test_conversation_memory_compacts_old_turns_and_injects_pinned_state(tmp_path) -> None:
    store = CopilotHostStore(tmp_path / "copilot.sqlite3")
    for index in range(9):
        store.record_session_turn(
            "wechat:chat",
            f"question {index}",
            f"answer {index}",
            max_messages=20,
            tool_uses=({"name": "runtime_status", "ok": True},),
        )
    prepared = _contract("结论呢")

    result = prepare_contract_with_memory(
        prepared,
        store=store,
        session_key="wechat:chat",
        model_runner=lambda _request: ModelTurn(
            text=(
                '{"episode_summary":{"goal":"分析账户收益","confirmed_facts":["7月收益为正"],'
                '"completed_actions":["读取收益"],"tool_findings":["runtime_status正常"],'
                '"user_constraints":["只看lx"],"open_questions":["风险集中度"],'
                '"next_step":"读取持仓"},"pinned_state":{"current_goal":"分析账户收益",'
                '"confirmed_scope":["lx","2026-07"],"user_constraints":["只看lx"],'
                '"open_questions":["风险集中度"]}}'
            )
        ),
    )

    memory = store.session_memory("wechat:chat")
    assert memory["compacted_turn_count"] == 7
    assert memory["pinned_state"]["confirmed_scope"] == ["lx", "2026-07"]
    assert memory["episodes"][0]["confirmed_facts"] == ["7月收益为正"]
    context = result.input["messages"][-2]
    assert context["role"] == "system"
    assert "Conversation memory from earlier turns" in context["content"]
    assert "风险集中度" in context["content"]


def test_invalid_memory_compaction_preserves_raw_turns(tmp_path) -> None:
    store = CopilotHostStore(tmp_path / "copilot.sqlite3")
    for index in range(9):
        store.record_session_turn("wechat:chat", f"q{index}", f"a{index}", max_messages=20)
    prepared = _contract("继续")

    result = prepare_contract_with_memory(
        prepared,
        store=store,
        session_key="wechat:chat",
        model_runner=lambda _request: ModelTurn(text="not-json"),
    )

    assert store.session_memory("wechat:chat")["compacted_turn_count"] == 0
    assert len(store.session_turns("wechat:chat")) == 9
    assert result == prepared


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

    assert store.session_messages("legacy")[0]["content"] == "hello"
    assert store.session_turns("legacy") == ()
    assert store.session_memory("legacy") == {
        "version": 1,
        "compacted_turn_count": 0,
        "pinned_state": {},
        "episodes": [],
    }


def test_session_turn_keeps_multiple_tool_results_by_call_id(tmp_path) -> None:
    store = CopilotHostStore(tmp_path / "copilot.sqlite3")
    store.record_session_turn(
        "wechat:chat",
        "总结收益和风险",
        "结论：收益为正，风险集中。",
        max_messages=20,
        tool_uses=(
            {"name": "option_performance_report", "ok": True},
            {"name": "option_positions_read", "ok": True},
        ),
    )

    assert [item["name"] for item in store.session_turns("wechat:chat")[0]["tool_uses"]] == [
        "option_performance_report",
        "option_positions_read",
    ]


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
            text="结论：恢复后确认运行正常。" if "Recovered read-only observations" in request.messages[-1]["content"] else ""
        ),
        host_store=store,
        session_key=session_key,
        resumed_from=failed.run_id,
        recovered_observations=recovered,
    )

    assert resumed.status == "answered"
    assert "恢复后" in resumed.user_response
    assert store.run_record(resumed.run_id)["resumed_from"] == failed.run_id


def test_cancel_request_only_updates_active_run(tmp_path) -> None:
    store = CopilotHostStore(tmp_path / "copilot.sqlite3")
    contract = _contract("运行状态")
    store.start_run("run_active", contract=contract, session_key="wechat:chat")

    assert store.request_cancel("run_active") is True
    assert store.is_cancel_requested("run_active") is True
    assert store.request_cancel("missing") is False


def test_model_runner_honors_cancellation_before_provider_call() -> None:
    called = False

    def provider(**_kwargs):
        nonlocal called
        called = True
        return {}

    runner = build_model_runner(
        CopilotModelSettings(provider="deepseek", model="deepseek-chat", api_key_env="TEST_KEY"),
        environ={"TEST_KEY": "secret"},
        create_chat_completion_fn=provider,
    )

    try:
        runner(
            ModelRequest(
                messages=({"role": "user", "content": "status"},),
                tools=(),
                is_cancelled=lambda: True,
            )
        )
    except RuntimeError as exc:
        assert getattr(exc, "cancelled", False) is True
    else:
        raise AssertionError("expected cancellation")
    assert called is False


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


def test_memory_update_rejects_stale_compaction_write(tmp_path) -> None:
    store = CopilotHostStore(tmp_path / "copilot.sqlite3")
    assert store.update_session_memory(
        "wechat:chat",
        {"version": 1, "compacted_turn_count": 3, "pinned_state": {}, "episodes": []},
        expected_compacted_turn_count=0,
    )

    assert not store.update_session_memory(
        "wechat:chat",
        {"version": 1, "compacted_turn_count": 2, "pinned_state": {}, "episodes": []},
        expected_compacted_turn_count=0,
    )
    assert store.session_memory("wechat:chat")["compacted_turn_count"] == 3


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
    context_event = next(item for item in events if item["type"] == "iteration_context_snapshot")
    assert context_event["payload"]["iteration_id"].startswith("iter_")
    assert len(context_event["payload"]["context_hash"]) == 64
    assert metrics["model_turn_count"] == 1
    assert metrics["usage"]["total_tokens"] == 15
    assert record["termination_reason"] == "final_answer"


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


def test_reply_outbox_is_idempotent_retryable_and_deliverable(tmp_path) -> None:
    store = CopilotHostStore(tmp_path / "copilot.sqlite3")
    first = store.enqueue_reply(
        delivery_key="wechat:command-1",
        channel="wechat",
        session_key="wechat:chat",
        run_id="run_1",
        payload={"text": "结论"},
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
    assert store.list_replies()[0]["status"] == "delivered"


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

    protocol = next(event for event in result.events if event.type == "tool_protocol_error")
    assert result.status == "answered"
    assert protocol.payload["iteration_id"].startswith("iter_")
    assert protocol.payload["tool_call_id"] == "call_bad_json"
    assert protocol.payload["error_category"] == "malformed_arguments"
    assert protocol.payload["partial_tool_name"] == "runtime_status"
    assert len(protocol.payload["partial_arguments"]) <= 500
    observation = next(event for event in result.events if event.type == "tool_result")
    assert observation.payload["error"] == "INPUT_ERROR"
    assert "valid JSON" in observation.payload["hint"]


def _contract(text: str):
    prepared = prepare_contract(
        CopilotRequest(
            request_id=new_id("req"),
            source_entry="test",
            user_message=text,
            execution_environment="channel",
        ),
        reference_year=2026,
    )
    assert not isinstance(prepared, AppResult)
    return prepared
