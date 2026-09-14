from __future__ import annotations

import json
import threading
import time
from dataclasses import replace

import pytest

from src.application.bot.contracts import ExecutionContract, contract_from_payload
from src.application.bot.host_store import BotHostStore
from src.application.bot.memory import BotMemoryStore, scope_from_contract


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
