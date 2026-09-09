from __future__ import annotations

import json
import threading
import time
from dataclasses import replace

import pytest

from src.application.bot.contracts import ExecutionContract, contract_from_payload
from src.application.bot.host_store import BotHostStore
from src.application.bot.memory import BotMemoryStore, scope_from_contract
from src.application.bot.memory_worker import MemoryWorker


def setup(tmp_path):
    host = BotHostStore(tmp_path / "host.sqlite3")
    host._ensure_schema()
    return host, BotMemoryStore(host)


def run(host, run_id="r1", text="记住：我喜欢简短回答", *, sender="alice", status="running", environment="channel"):
    contract = ExecutionContract("c" + run_id, "q" + run_id, "om_chat", environment,
        {"authenticated_channel": "feishu", "authenticated_sender_id": sender,
         "authenticated_conversation_id": run_id, "config_key": "us", "user_message": text}, {}, {})
    host.start_run(run_id, contract=contract, session_key="session" + run_id)
    with host._connect() as conn:
        conn.execute("UPDATE bot_runs SET lease_id=?,status=? WHERE run_id=?", ("lease" + run_id, status, run_id))
    return contract


def act(memory, scope, action="remember", run_id="r1", **overrides):
    args = {"idempotency_key": "op1", "expected_revision": 0,
            "expected_epoch": memory.recall(scope)["epoch"], "content": "我喜欢简短回答",
            "kind": "preference", "source_quote": "记住：我喜欢简短回答"}
    args.update(overrides)
    return memory.act(run_id=run_id, scope=scope, action=action, arguments=args,
                      lease_id="lease" + run_id, deadline_monotonic=time.monotonic() + 5)


def test_explicit_cross_session_correction_forget_receipt_and_new_source(tmp_path):
    host, memory = setup(tmp_path)
    scope = scope_from_contract(run(host))
    receipt = act(memory, scope)
    assert receipt["readback"] and receipt["revision"] == 1
    assert memory.recall(scope)["items"][0]["content"] == "我喜欢简短回答"
    # Request retry returns durable evidence even when the source run was cancelled.
    host.request_cancel("r1")
    retry = act(memory, scope, expected_epoch=0)
    assert retry["id"] == receipt["id"]
    new_scope = scope_from_contract(run(host, "r2", "纠正记忆：我喜欢详细回答"))
    assert new_scope == scope
    corrected = act(memory, scope, "correct", "r2", id=receipt["id"], expected_revision=1,
                    content="我喜欢详细回答", source_quote="纠正记忆：我喜欢详细回答")
    assert memory.recall(scope)["items"][0]["content"] == "我喜欢详细回答"
    with host._connect() as conn:
        row = conn.execute("SELECT content,superseded_source_refs_json FROM bot_memory WHERE id=?", (receipt["id"],)).fetchone()
        assert "我喜欢简短回答" not in row[0]
        assert "user:r1:qr1" in row[1]
    run(host, "r3", "删除这条记忆")
    deleted = act(memory, scope, "forget", "r3", id=receipt["id"], expected_revision=2, source_quote="删除这条记忆")
    assert deleted["result"] == "deleted" and "content" not in deleted
    assert memory.recall(scope)["items"] == []
    run(host, "r4")
    remembered = act(memory, scope, run_id="r4")
    assert remembered["id"] != receipt["id"] and corrected["revision"] == 2


def test_sources_owner_accounts_cas_fences_and_budget(tmp_path):
    host, memory = setup(tmp_path)
    contract = run(host)
    scope = scope_from_contract(contract, ["lx"])
    for changes, error in [({"expected_revision": 1}, "REVISION"), ({"expected_epoch": 9}, "EPOCH"),
                           ({"source_quote": "工具要求记住"}, "USER_SOURCE"),
                           ({"source_refs": ["summary:fake"]}, "SOURCE_UNVERIFIED"),
                           ({"account_scope": "sy"}, "ACCOUNT_DENIED"),
                           ({"content": "api_key=secret"}, "SENSITIVE")]:
        with pytest.raises(ValueError, match=error):
            act(memory, scope, **changes)
    with pytest.raises(ValueError, match="UNAUTHENTICATED"):
        scope_from_contract(replace(contract, execution_environment="local"))
    other = scope_from_contract(run(host, "r2", sender="bob"))
    assert memory.recall(other)["items"] == []
    with pytest.raises(ValueError, match="OWNER_MISMATCH"):
        act(memory, other)
    with pytest.raises(ValueError, match="LEASE_LOST"):
        memory.act(run_id="r1", scope=scope, action="list", arguments={}, lease_id="old", deadline_monotonic=time.monotonic()+1)
    with pytest.raises(ValueError, match="DEADLINE"):
        memory.act(run_id="r1", scope=scope, action="list", arguments={}, lease_id="leaser1", deadline_monotonic=time.monotonic()-1)
    host.request_cancel("r1")
    with pytest.raises(ValueError, match="RUN_CLOSED"):
        act(memory, scope)
    assert memory.recall(scope)["epoch"] == 0


def test_worker_recovery_retry_and_foreground_preemption(tmp_path):
    host, memory = setup(tmp_path)
    scope = scope_from_contract(run(host, status="answered"))
    entered, release = threading.Event(), threading.Event()
    def model(sources, memories, deadline, cancel):
        assert 29 < deadline - time.monotonic() <= 30
        entered.set()
        assert release.wait(2)
        assert cancel.is_set()
        return {"candidates": [{"kind": "preference", "content": "我喜欢简短回答", "source_refs": ["user:r1:qr1"]}]}
    worker = MemoryWorker(memory, model)
    assert worker.recover_jobs() == 1 and worker.recover_jobs() == 0
    thread = threading.Thread(target=worker.run_once)
    thread.start()
    assert entered.wait(2)
    before = time.monotonic()
    worker.foreground_enter()
    assert time.monotonic() - before < .5
    release.set()
    thread.join(2)
    assert not thread.is_alive() and memory.recall(scope)["items"] == []
    with host._connect() as conn:
        assert conn.execute("SELECT state,attempt,lease_id FROM bot_memory_jobs").fetchone() == ("pending", 0, None)
    failing = MemoryWorker(memory, lambda *args: (_ for _ in ()).throw(ValueError("bad output")))
    assert failing.run_once() and failing.run_once() and not failing.run_once()
    with host._connect() as conn:
        assert conn.execute("SELECT state,attempt FROM bot_memory_jobs").fetchone() == ("failed", 2)


def test_new_epoch_job_cannot_rebuild_corrected_original_source(tmp_path):
    host, memory = setup(tmp_path)
    scope = scope_from_contract(run(host))
    saved = act(memory, scope)
    run(host, "r2", "纠正：我喜欢详细回答")
    act(memory, scope, "correct", "r2", id=saved["id"], expected_revision=1,
        content="我喜欢详细回答", source_quote="纠正：我喜欢详细回答")
    with host._connect() as conn:
        conn.execute("UPDATE bot_runs SET status='answered' WHERE run_id='r1'")
    calls = []
    def model(*args):
        calls.append(args)
        return {"candidates": [{"content": "我喜欢简短回答", "source_refs": ["user:r1:qr1"]}]}
    worker = MemoryWorker(memory, model)
    assert worker.enqueue("r1") and worker.run_once()
    assert calls == []  # The original source is suppressed even before model input.
    assert [i["content"] for i in memory.recall(scope)["items"]] == ["我喜欢详细回答"]


def test_write_lock_cancel_winner_prevents_effect(tmp_path):
    host, memory = setup(tmp_path)
    scope = scope_from_contract(run(host))
    with host._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE bot_runs SET cancel_requested=1,admission_state='cancel' WHERE run_id='r1'")
    with pytest.raises(ValueError, match="RUN_CLOSED"):
        act(memory, scope)
    assert memory.recall(scope)["items"] == []
    with host._connect() as conn:
        assert not json.loads(conn.execute("SELECT events_json FROM bot_runs").fetchone()[0])


def test_original_evidence_recovery_account_filter_and_correction(tmp_path):
    from src.application.bot.memory_worker import verified_sources_from_run

    host, memory = setup(tmp_path)
    scope = scope_from_contract(run(host, text="记住这次经验", status="answered"), ["lx"])
    event = {"type": "tool_result", "timestamp": "2026-09-09T00:00:00+00:00", "payload": {
        "tool_name": "receipt_read", "ref": "obv1", "ok": True, "status": "complete",
        "coverage": {"status": "complete", "scope": {"account": "lx"}},
        "content_hash": "sha256:fixture", "value": {"account": "lx", "result": "数据库锁已释放"},
        "tool_input": {"account": "lx"}}}
    with host._connect() as conn:
        conn.execute("UPDATE bot_runs SET events_json=? WHERE run_id='r1'", (json.dumps([event]),))
    sources = verified_sources_from_run(host.run_record("r1"), scope=scope)
    assert sources["evidence:r1:obv1"]["account_scope"] == "lx"
    assert verified_sources_from_run(host.run_record("r1"), scope=scope_from_contract(
        contract_from_payload(
            json.loads(host.run_record("r1")["contract_json"])))) == {}
    worker = MemoryWorker(memory, lambda *args: {"candidates": [{"kind": "experience",
        "account_scope": "lx", "content": "数据库锁已释放", "source_refs": ["evidence:r1:obv1"]}]},
        scope_for_run=lambda _: scope)
    assert worker.recover_jobs() == 1
    with host._connect() as conn:
        assert "evidence:r1:obv1" in json.loads(conn.execute("SELECT source_refs_json FROM bot_memory_jobs").fetchone()[0])
    assert worker.run_once()
    experience = memory.recall(scope)["items"][0]
    assert experience["kind"] == "experience"
    run(host, "r2", "纠正：数据库锁尚未释放")
    act(memory, scope, "correct", "r2", id=experience["id"], expected_revision=1, kind="experience",
        account_scope="lx", content="数据库锁尚未释放", source_quote="纠正：数据库锁尚未释放")
    assert memory.recall(scope)["items"][0]["content"] == "数据库锁尚未释放"
    # A second job with a new epoch and candidate id cannot reuse the suppressed original evidence.
    with host._connect() as conn:
        conn.execute("UPDATE bot_memory_jobs SET state='pending',memory_epoch=999")
    assert worker.run_once()
    assert len(memory.recall(scope)["items"]) == 1


def test_pi_consolidation_payload_uses_shared_deadline_and_no_capabilities(monkeypatch):
    from src.application.bot.memory_worker import consolidate_with_pi
    from src.application.bot.model_config import PiModelSettings
    from src.infrastructure import pi_agent_process

    seen = []
    def bridge(payload, **kwargs):
        pi_agent_process._validate_start_payload(payload)
        seen.append((payload, kwargs))
        assert kwargs["on_proposed"]({}) == "commit"
        kwargs["on_event"]({"event_type": "model_turn_completed", "data": {
            "usage_total": {"input": 23, "output": 7, "totalTokens": 30}, "model_retry_count": 0}})
        return {"ok": True, "result": {"committed": True, "text": '{"candidates":[]}'}}
    monkeypatch.setattr(pi_agent_process, "run_pi_agent", bridge)
    model = PiModelSettings("openai", "openai-responses", "test", "http://127.0.0.1:1/v1", "", "", 90, 64000, 1000, 2)
    deadline = time.monotonic() + 25
    result = consolidate_with_pi({}, {}, deadline, threading.Event(), model_settings=model)
    assert result["candidates"] == []
    assert result["cost"]["usage_total"]["totalTokens"] == 30
    payload, kwargs = seen[0]
    assert kwargs["deadline_monotonic"] == deadline
    assert 24000 < payload["remaining_budget_ms"] <= 25000
    assert payload["tools"] == payload["runtime_context"] == payload["recovered_observations"] == []
    assert payload["session_id"] is None and payload["model"]["max_attempts"] == 1


def test_recall_has_finite_byte_upper_bound_and_many_candidates(tmp_path):
    host, memory = setup(tmp_path)
    scope = scope_from_contract(run(host))
    for index in range(12):
        run_id = "extra" + str(index)
        run(host, run_id, f"记住：第{index}条偏好")
        act(memory, scope, run_id=run_id, content=f"第{index}条偏好", source_quote=f"记住：第{index}条偏好")
    recall = memory.recall(scope)
    assert 0 < len(recall["items"]) <= 8 and recall["token_upper_bound"] <= 2000
    assert recall["partial"]
    assert memory.recall(scope, "第1条")["items"][0]["content"] == "第1条偏好"


def test_crashed_job_lease_recovery_does_not_allow_unlimited_attempts(tmp_path):
    host, memory = setup(tmp_path)
    run(host, status="answered")
    worker = MemoryWorker(memory, lambda *args: {"candidates": []})
    assert worker.enqueue("r1")
    with host._connect() as conn:
        conn.execute("UPDATE bot_memory_jobs SET state='running',lease_id='dead',lease_until=?,attempt=1", (time.time()-1,))
    assert worker.run_once()
    with host._connect() as conn:
        assert conn.execute("SELECT state,attempt FROM bot_memory_jobs").fetchone() == ("failed", 2)
    assert not worker.run_once()


def test_deadline_rechecked_after_sqlite_writer_wait(tmp_path):
    host, memory = setup(tmp_path)
    scope = scope_from_contract(run(host))
    deadline = time.monotonic() + .03
    errors = []
    started = threading.Event()
    def write():
        started.set()
        try:
            memory.act(run_id='r1', scope=scope, action='remember', arguments={
                'content': '我喜欢简短回答', 'kind': 'preference', 'source_quote': '记住：我喜欢简短回答',
                'expected_epoch': 0, 'expected_revision': 0, 'idempotency_key': 'wait'},
                lease_id='leaser1', deadline_monotonic=deadline)
        except Exception as exc:
            errors.append(exc)
    with host._connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        thread = threading.Thread(target=write)
        thread.start()
        assert started.wait(1)
        time.sleep(.05)
    thread.join(1)
    assert not thread.is_alive() and len(errors) == 1 and 'DEADLINE' in str(errors[0])
    assert memory.recall(scope)['items'] == []


def test_combined_memory_progress_context_is_bounded_without_truncating_bound_goal():
    from src.application.bot.memory import bounded_memory_context
    from src.application.bot.tools import conservative_json_tokens

    progress = [{'progress_ref': f'run_{index}', 'revision': 1, 'goal': '原始目标' * 500,
                 'accounts': ['lx'], 'evidence_refs': [{'ref': f'evidence_{ref}', 'content_hash': 'a'*64} for ref in range(12)]}
                for index in range(8)]
    memory = {'items': [{'id': 'memory_1', 'content': '偏好内容' * 50}], 'epoch': 2}
    context = bounded_memory_context(memory, progress, progress[0])
    assert conservative_json_tokens(context) <= 3500
    assert context['bound_progress']['goal'] == progress[0]['goal']
    assert all(item['detail_required'] and len(item['goal']) <= 160 for item in context['unfinished_progress'])


@pytest.mark.parametrize('result_kind', ['valid', 'invalid_json', 'model_failure'])
def test_consolidation_bridge_usage_is_durable_on_success_and_failure(tmp_path, monkeypatch, result_kind):
    from functools import partial
    from src.application.bot.memory_worker import consolidate_with_pi
    from src.infrastructure import pi_agent_process
    from tests.bot_pi_test_support import _TEST_MODEL

    host, memory = setup(tmp_path)
    scope = scope_from_contract(run(host, text='我习惯看简短结论', status='answered'))
    def bridge(payload, **kwargs):
        pi_agent_process._validate_start_payload(payload)
        sources = json.loads(payload['user_message'])['sources']
        assert sources['user:r1:qr1']['text'] == '我习惯看简短结论'
        kwargs['on_event']({'event_type': 'model_turn_completed', 'data': {
            'usage_total': {'input': 51, 'output': 13, 'totalTokens': 64}, 'model_retry_count': 0}})
        if result_kind == 'model_failure':
            return {'ok': False, 'error': {'code': 'MODEL_ERROR'}}
        content = json.dumps({'candidates': [{'kind': 'preference', 'content': '我习惯看简短结论',
                    'source_refs': ['user:r1:qr1'], 'account_scope': None}]}) if result_kind == 'valid' else 'not-json'
        return {'ok': True, 'result': {'committed': True, 'text': content}}
    monkeypatch.setattr(pi_agent_process, 'run_pi_agent', bridge)
    worker = MemoryWorker(memory, partial(consolidate_with_pi, model_settings=_TEST_MODEL))
    assert worker.enqueue('r1') and worker.run_once()
    with host._connect() as conn:
        state, cost_json, attempt = conn.execute('SELECT state,cost_json,attempt FROM bot_memory_jobs').fetchone()
    assert json.loads(cost_json)['usage_total']['totalTokens'] == 64
    if result_kind == 'valid':
        assert state == 'done' and attempt == 0
        assert memory.recall(scope)['items'][0]['content'] == '我习惯看简短结论'
    else:
        assert state == 'pending' and attempt == 1 and memory.recall(scope)['items'] == []


@pytest.mark.parametrize("content", [
    "我喜欢以账户分别展示收益与现金流，使用中文解释并保留数据的来源和时间。" * 22,
    "a" * 2000,
])
def test_long_legal_memory_has_bounded_manageable_preview(tmp_path, content):
    host, memory = setup(tmp_path)
    scope = scope_from_contract(run(host, text="记住：" + content))
    saved = act(memory, scope, content=content, source_quote="记住：" + content)
    with host._connect() as conn:
        assert conn.execute("SELECT content FROM bot_memory WHERE id=?", (saved['id'],)).fetchone()[0] == content
    for query in ('', content[:10]):
        listing = memory.recall(scope, query)
        item = listing['items'][0]
        assert listing['partial'] and item['truncated']
        assert (item['id'], item['revision']) == (saved['id'], 1)
        assert item['content_chars'] == len(content)
        assert content.startswith(item['content']) and 0 < len(item['content']) < len(content)
        assert listing['token_upper_bound'] <= 2000
        assert len(json.dumps(listing['items'], ensure_ascii=False, separators=(',', ':')).encode()) <= 2002
    run(host, 'r2', '纠正记忆：我喜欢详细回答')
    corrected = act(memory, scope, 'correct', 'r2', id=item['id'], expected_revision=item['revision'],
                    content='我喜欢详细回答', source_quote='纠正记忆：我喜欢详细回答')
    assert corrected['readback'] and memory.recall(scope)['items'][0]['content'] == '我喜欢详细回答'
    run(host, 'r3', '删除这条记忆')
    deleted = act(memory, scope, 'forget', 'r3', id=item['id'], expected_revision=corrected['revision'], source_quote='删除这条记忆')
    assert deleted['readback'] and memory.recall(scope)['items'] == []


def test_cancel_winner_survives_stale_recovery_and_is_only_explicit_navigation(tmp_path):
    host, _ = setup(tmp_path)
    scope = scope_from_contract(run(host))
    assert host.request_cancel('r1')
    with host._connect() as conn:
        conn.execute("UPDATE bot_runs SET started_at='2000-01-01T00:00:00+00:00' WHERE run_id='r1'")
    assert host.mark_stale_runs_interrupted() == 1
    row = host.run_record('r1')
    assert (row['status'], row['admission_state'], row['termination_reason']) == ('cancelled', 'cancel', 'CANCELLED')
    assert host.unfinished_progress(scope) == []
    explicit = host.unfinished_progress(scope, include_cancelled=True, progress_ref='r1')
    assert len(explicit) == 1 and explicit[0]['termination_reason'] == 'CANCELLED'
    assert host.resume_source('r1') is None
    # Defend historical rows already recovered with the old interrupted status.
    with host._connect() as conn:
        conn.execute("UPDATE bot_runs SET status='interrupted' WHERE run_id='r1'")
    assert host.unfinished_progress(scope) == []
    assert host.resume_source('r1') is None
