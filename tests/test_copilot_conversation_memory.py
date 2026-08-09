from __future__ import annotations

import sqlite3

import pytest

from src.application.copilot import channel_facade, local_harness, tools as copilot_tools
from src.application.copilot.agent import ModelRequest, ModelTurn, ToolCall
from src.application.copilot.contracts import AppResult, CopilotRequest, new_id
from src.application.copilot.conversation_memory import prepare_contract_with_existing_memory
from src.application.copilot.host import run_contract
from src.application.copilot.host_store import CopilotHostStore
from src.application.copilot.model_client import CopilotModelSettings, build_model_runner
from src.application.copilot.service import prepare_contract


def test_conversation_memory_injects_existing_state_without_mutation(tmp_path) -> None:
    store = CopilotHostStore(tmp_path / "copilot.sqlite3")
    for index in range(9):
        store.record_session_turn(
            "wechat:chat",
            f"question {index}",
            f"answer {index}",
            max_messages=20,
            tool_uses=({"name": "runtime_status", "ok": True},),
        )
    expected_memory = {
        "version": 1,
        "compacted_turn_count": 7,
        "pinned_state": {
            "current_goal": "分析账户收益",
            "confirmed_scope": ["lx", "2026-07"],
            "user_constraints": ["只看lx"],
            "open_questions": ["风险集中度"],
        },
        "episodes": [
            {
                "goal": "分析账户收益",
                "confirmed_facts": ["7月收益为正"],
                "completed_actions": ["读取收益"],
                "tool_findings": ["runtime_status正常"],
                "user_constraints": ["只看lx"],
                "open_questions": ["风险集中度"],
                "next_step": "读取持仓",
            }
        ],
    }
    assert store.update_session_memory(
        "wechat:chat",
        expected_memory,
        expected_compacted_turn_count=0,
    )
    expected_turns = store.session_turns("wechat:chat")
    prepared = _contract("结论呢")

    result = prepare_contract_with_existing_memory(
        prepared,
        store=store,
        session_key="wechat:chat",
    )

    assert store.session_memory("wechat:chat") == expected_memory
    assert store.session_turns("wechat:chat") == expected_turns
    context = result.input["messages"][-2]
    assert context["role"] == "system"
    assert "Conversation memory from earlier turns" in context["content"]
    assert "风险集中度" in context["content"]


@pytest.mark.parametrize(
    "raw_memory",
    (
        "not-json",
        '{"version":"bad","compacted_turn_count":"bad","pinned_state":"bad","episodes":1}',
        '{"version":1e309,"compacted_turn_count":0,"pinned_state":{},"episodes":[]}',
        '{"version":1,"compacted_turn_count":Infinity,"pinned_state":{},"episodes":[]}',
    ),
)
def test_malformed_stored_memory_fails_open_without_rewrite(tmp_path, raw_memory: str) -> None:
    store = CopilotHostStore(tmp_path / "copilot.sqlite3")
    for index in range(9):
        store.record_session_turn("wechat:chat", f"q{index}", f"a{index}", max_messages=20)
    with sqlite3.connect(store.path) as conn:
        conn.execute(
            "UPDATE copilot_sessions SET memory_json = ? WHERE session_key = 'wechat:chat'",
            (raw_memory,),
        )
    expected_turns = store.session_turns("wechat:chat")
    prepared = _contract("继续")

    result = prepare_contract_with_existing_memory(
        prepared,
        store=store,
        session_key="wechat:chat",
    )

    assert result == prepared
    assert store.session_turns("wechat:chat") == expected_turns
    with sqlite3.connect(store.path) as conn:
        stored_row = conn.execute(
            "SELECT memory_json FROM copilot_sessions WHERE session_key = 'wechat:chat'"
        ).fetchone()
    assert stored_row == (raw_memory,)


def test_memory_update_rejects_nonfinite_numbers_without_rewrite(tmp_path) -> None:
    store = CopilotHostStore(tmp_path / "copilot.sqlite3")
    expected_memory = {
        "version": 1,
        "compacted_turn_count": 3,
        "pinned_state": {"current_goal": "保留现有记忆"},
        "episodes": [],
    }
    assert store.update_session_memory("feishu:chat", expected_memory)

    with pytest.raises(ValueError, match="Out of range float values"):
        store.update_session_memory(
            "feishu:chat",
            {
                "version": float("inf"),
                "compacted_turn_count": 3,
                "pinned_state": {},
                "episodes": [],
            },
        )

    assert store.session_memory("feishu:chat") == expected_memory


def test_feishu_channel_nonfinite_memory_fails_open_to_single_main_model_call(
    monkeypatch,
    tmp_path,
) -> None:
    database = tmp_path / "copilot.sqlite3"
    store = CopilotHostStore(database)
    for index in range(9):
        store.record_session_turn("feishu:chat_1", f"q{index}", f"a{index}", max_messages=20)
    raw_memory = (
        '{"version":1e309,"compacted_turn_count":0,'
        '"pinned_state":{"current_goal":"不应注入"},"episodes":[]}'
    )
    with sqlite3.connect(store.path) as conn:
        conn.execute(
            "UPDATE copilot_sessions SET memory_json = ? WHERE session_key = 'feishu:chat_1'",
            (raw_memory,),
        )
    expected_turns = store.session_turns("feishu:chat_1")
    requests: list[ModelRequest] = []

    def model(request: ModelRequest) -> ModelTurn:
        requests.append(request)
        return ModelTurn(text="结论：主模型正常回答。")

    monkeypatch.setattr(channel_facade, "_channel_model_gate", lambda _path: None)
    monkeypatch.setattr(local_harness, "_resolve_model_runner", lambda **_kwargs: (model, None))

    result = channel_facade.run_channel_request(
        user_message="继续分析",
        config_key="us",
        channel="feishu",
        sender_id="ou_1",
        conversation_id="chat_1",
        host_db_path=str(database),
    )

    assert result.status == "answered"
    assert len(requests) == 1
    assert "不应注入" not in str(requests[0].messages)
    turns = store.session_turns("feishu:chat_1")
    assert turns[:-1] == expected_turns
    assert turns[-1]["user_text"] == "继续分析"
    assert turns[-1]["assistant_final"] == "结论：主模型正常回答。"
    with sqlite3.connect(store.path) as conn:
        stored_row = conn.execute(
            "SELECT memory_json FROM copilot_sessions WHERE session_key = 'feishu:chat_1'"
        ).fetchone()
    assert stored_row == (raw_memory,)


def test_missing_stored_memory_returns_original_contract_without_creating_session(tmp_path) -> None:
    store = CopilotHostStore(tmp_path / "copilot.sqlite3")
    prepared = _contract("继续")

    result = prepare_contract_with_existing_memory(
        prepared,
        store=store,
        session_key="feishu:missing",
    )

    assert result == prepared
    with sqlite3.connect(store.path) as conn:
        session_count = conn.execute(
            "SELECT COUNT(*) FROM copilot_sessions WHERE session_key = 'feishu:missing'"
        ).fetchone()
    assert session_count == (0,)


def test_online_run_with_uncompacted_backlog_only_invokes_main_model(monkeypatch, tmp_path) -> None:
    store = CopilotHostStore(tmp_path / "copilot.sqlite3")
    for index in range(9):
        store.record_session_turn("feishu:chat", f"q{index}", f"a{index}", max_messages=20)
    requests: list[ModelRequest] = []

    def model(request: ModelRequest) -> ModelTurn:
        requests.append(request)
        return ModelTurn(text="结论：已完成主模型回答。")

    monkeypatch.setattr(local_harness, "_resolve_model_runner", lambda **_kwargs: (model, None))

    result = local_harness.run_prepared_contract(
        _contract("继续"),
        host_store=store,
        session_key="feishu:chat",
    )

    assert result.status == "answered"
    assert len(requests) == 1
    assert all(
        "Compact conversation history" not in str(item.get("content") or "")
        for item in requests[0].messages
    )
    assert store.session_memory("feishu:chat")["compacted_turn_count"] == 0
    assert len(store.session_turns("feishu:chat")) == 9


def test_successful_channel_answer_records_turn_after_run(monkeypatch, tmp_path) -> None:
    database = tmp_path / "copilot.sqlite3"
    monkeypatch.setattr(channel_facade, "_channel_model_gate", lambda _path: None)
    monkeypatch.setattr(
        channel_facade,
        "run_prepared_contract",
        lambda _prepared, **_kwargs: AppResult(status="answered", user_response="结论：运行正常。"),
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
    turns = CopilotHostStore(database).session_turns("feishu:chat_1")
    assert len(turns) == 1
    assert turns[0]["user_text"] == "检查运行状态"
    assert turns[0]["assistant_final"] == "结论：运行正常。"


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
    assert resumed_scene["scene_version"] == "v4"
    assert resumed_scene["compiled_prompt_sha256"] == failed_scene["compiled_prompt_sha256"]
    assert resumed_scene["tool_schema_sha256"] == failed_scene["tool_schema_sha256"]


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
    scene_event = next(item for item in events if item["type"] == "scene_prepared")
    assert context_event["payload"]["iteration_id"].startswith("iter_")
    assert len(context_event["payload"]["context_hash"]) == 64
    assert len(scene_event["payload"]["compiled_prompt_sha256"]) == 64
    assert scene_event["payload"]["compiled_prompt_sha256"] != context_event["payload"]["context_hash"]
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
