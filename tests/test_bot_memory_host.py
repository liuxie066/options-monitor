from __future__ import annotations

import pytest

from src.application.bot.contracts import AppResult
from src.application.bot.host import run_contract
from src.application.bot.host_store import BotHostStore
from src.application.bot.memory_worker import MemoryWorker
from src.application.bot.memory_worker import configured_memory_scope
from src.application.bot.service import prepare_contract
from src.infrastructure.pi_agent_process import derive_pi_session_id
from tests.bot_pi_test_support import _TEST_MODEL
from tests.test_bot_phase1 import _request


def channel(tmp_path, monkeypatch, text="记住：我喜欢简短回答", *, environment="channel"):
    monkeypatch.setattr("src.application.agent_tool_config.load_runtime_config", lambda **_: (tmp_path, {"accounts": {"lx": {}}}))
    monkeypatch.setattr(MemoryWorker, "wake", lambda _: None)
    contract = prepare_contract(_request(text, environment=environment), reference_year=2026, report_now_ms=1788319188212)
    assert not isinstance(contract, AppResult)
    host = BotHostStore(tmp_path / "host.sqlite3")
    session = derive_pi_session_id("test", "test-user", "test-conversation", "key:us") if environment == "channel" else None
    return contract, host, session


def test_configured_memory_scope_accepts_runtime_account_list(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "src.application.agent_tool_config.load_runtime_config",
        lambda **_: (tmp_path, {"accounts": ["lx", "sy"]}),
    )
    contract = prepare_contract(
        _request("查看记忆", environment="channel"),
        reference_year=2026,
        report_now_ms=1788319188212,
    )

    scope = configured_memory_scope(contract)

    assert scope.allowed_accounts == frozenset({"lx", "sy"})


def test_channel_memory_runtime_config_list_is_enabled(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "src.application.agent_tool_config.load_runtime_config",
        lambda **_: (tmp_path, {"accounts": ["lx", "sy"]}),
    )
    monkeypatch.setattr(MemoryWorker, "wake", lambda _: None)
    contract = prepare_contract(
        _request("介绍一下自己", environment="channel"),
        reference_year=2026,
        report_now_ms=1788319188212,
    )
    host = BotHostStore(tmp_path / "host.sqlite3")
    session = derive_pi_session_id("test", "test-user", "test-conversation", "key:us")

    def ordinary(start, call, _):
        assert "bot_memory" in {item["name"] for item in start["tools"]}
        assert "memory and historical progress are unavailable" not in "\n".join(
            item["content"] for item in start["runtime_context"]
        )
        return submit(call, "结论：我是 Bot。")

    result = execute(monkeypatch, contract, host, session, ordinary)

    assert result.status == "answered"


def execute(monkeypatch, contract, host, session, flow):
    failures = []
    def process(start, *, on_tool_call, on_proposed, **kwargs):
        try:
            submitted = flow(start, on_tool_call, kwargs)
        except Exception as exc:
            failures.append(exc)
            raise
        if not submitted.get("approved_answer"):
            return {"ok": False, "error": {"code": "ANSWER_ADMISSION_FAILED", "stage": "answer", "message": "test rejection"}}
        proposal = {"status": "answered", "text": submitted["approved_answer"]["text"],
                    "control_request": None, "termination_reason": "stop", "usage": {}}
        winner = on_proposed(proposal)
        return {"ok": True, "result": {**proposal, "committed": winner == "commit"}}
    monkeypatch.setattr("src.application.bot.host.run_pi_agent", process)
    result = run_contract(contract, model_settings=_TEST_MODEL, host_store=host, session_key=session)
    if failures:
        raise failures[0]
    return result


def submit(call, text, ref=None):
    return call({"call_id": "answer", "tool_name": "submit_answer", "arguments": {
        "mode": "evidence" if ref else "conceptual", "status": "complete", "answer_markdown": text,
        "claims": [{"text": text, "kind": "current_fact", "required_scope": "point", "observation_ids": [ref]}] if ref else []}})


def remember(call, *, content="我喜欢简短回答"):
    listing = call({"call_id": "list", "tool_name": "bot_memory", "arguments": {"action": "list"}})
    assert listing["observation"]["ok"] is True, listing
    return call({"call_id": "remember", "tool_name": "bot_memory", "arguments": {"action": "remember",
        "kind": "preference", "content": content, "source_quote": "记住：我喜欢简短回答",
        "expected_epoch": listing["observation"]["value"]["epoch"], "expected_revision": 0, "idempotency_key": "save"}})


@pytest.mark.parametrize("repeat", range(3))
def test_channel_memory_remember_readback_and_answer_three_runs(tmp_path, monkeypatch, repeat):
    contract, host, session = channel(tmp_path, monkeypatch)
    def flow(start, call, _):
        assert "bot_memory" in {t["name"] for t in start["tools"]}
        saved = remember(call)["observation"]
        assert saved["ok"] and saved["value"]["readback"]
        assert "owner_scope" not in saved["value"]
        return submit(call, saved["value"]["acknowledgement"], saved["ref"])
    result = execute(monkeypatch, contract, host, session, flow)
    assert result.status == "answered" and "已记住" in result.user_response, result.error
    with host._connect() as conn:
        assert conn.execute("SELECT content FROM bot_memory WHERE state='active'").fetchone()[0] == "我喜欢简短回答"
        assert conn.execute("SELECT state FROM bot_memory_jobs WHERE run_id=?", (result.run_id,)).fetchone()[0] == "pending"


def test_memory_receipt_cannot_support_business_claim(tmp_path, monkeypatch):
    contract, host, session = channel(tmp_path, monkeypatch)
    def flow(start, call, _):
        saved = remember(call)["observation"]
        answer = submit(call, "现金余额为一百万。", saved["ref"])
        assert answer["observation"]["reason"] == "memory_receipt_supports_only_maintenance"
        return answer
    result = execute(monkeypatch, contract, host, session, flow)
    assert result.status == "failed"


def test_failed_memory_write_cannot_claim_conceptual_success(tmp_path, monkeypatch):
    contract, host, session = channel(tmp_path, monkeypatch)
    def flow(start, call, _):
        failed = remember(call, content="伪造的偏好")["observation"]
        assert failed["ok"] is False
        answer = submit(call, "已记住。")
        assert answer["observation"]["ok"] is False
        return answer
    result = execute(monkeypatch, contract, host, session, flow)
    assert result.status == "failed"
    with host._connect() as conn:
        assert conn.execute("SELECT count(*) FROM bot_memory WHERE state='active'").fetchone()[0] == 0


def test_cancel_late_memory_callback_and_local_identity_are_denied(tmp_path, monkeypatch):
    contract, host, session = channel(tmp_path, monkeypatch)
    def cancelled(start, call, kwargs):
        host.request_cancel(kwargs["run_id"])
        denied = call({"call_id": "late", "tool_name": "bot_memory", "arguments": {"action": "list"}})
        assert not denied.get("ok", denied.get("observation", {}).get("ok"))
        return {}
    result = execute(monkeypatch, contract, host, session, cancelled)
    assert result.status == "cancelled"
    local, host, session = channel(tmp_path, monkeypatch, environment="local")
    def unauthenticated(start, call, _):
        assert "bot_memory" not in {t["name"] for t in start["tools"]}
        denied = call({"call_id": "fake", "tool_name": "bot_memory", "arguments": {"action": "list"}})
        assert denied["observation"]["ok"] is False
        return submit(call, "可以解释问题。")
    assert execute(monkeypatch, local, host, session, unauthenticated).status == "answered"


def test_midrun_scope_change_denies_memory(tmp_path, monkeypatch):
    contract, host, session = channel(tmp_path, monkeypatch)
    def flow(start, call, _):
        monkeypatch.setattr("src.application.agent_tool_config.load_runtime_config", lambda **_: (tmp_path, {"accounts": {"sy": {}}}))
        denied = call({"call_id": "changed", "tool_name": "bot_memory", "arguments": {"action": "list"}})
        assert denied["observation"]["ok"] is False
        return submit(call, "当前记忆范围发生变化，请重新查询。")
    assert execute(monkeypatch, contract, host, session, flow).status == "answered"


def test_lease_replaced_after_start_cannot_authorize_old_callback(tmp_path, monkeypatch):
    contract, host, session = channel(tmp_path, monkeypatch)
    def flow(start, call, kwargs):
        with host._connect() as conn:
            conn.execute("UPDATE bot_runs SET lease_id='replacement' WHERE run_id=?", (kwargs['run_id'],))
        denied = call({"call_id": "write", "tool_name": "bot_memory", "arguments": {
            "action": "remember", "content": "我喜欢简短回答", "source_quote": "记住：我喜欢简短回答",
            "kind": "preference", "expected_epoch": 0, "expected_revision": 0, "idempotency_key": "late"}})
        assert denied['observation']['ok'] is False
        return submit(call, "记忆操作未确认，请重试同一幂等键或重新查询。")
    assert execute(monkeypatch, contract, host, session, flow).status == 'answered'
    with host._connect() as conn:
        assert conn.execute("SELECT count(*) FROM bot_memory WHERE state='active'").fetchone()[0] == 0


def test_new_conversation_can_view_saved_memory_with_closed_read_evidence(tmp_path, monkeypatch):
    from dataclasses import replace

    contract, host, session = channel(tmp_path, monkeypatch)
    def save(start, call, kwargs):
        saved = remember(call)['observation']
        return submit(call, saved['value']['acknowledgement'], saved['ref'])
    assert execute(monkeypatch, contract, host, session, save).status == 'answered'
    fresh = prepare_contract(_request('查看记忆', environment='channel'), reference_year=2026, report_now_ms=1788319188212)
    new_contract = replace(fresh, input={**fresh.input, 'authenticated_conversation_id': 'new-conversation'})
    new_session = derive_pi_session_id('test', 'test-user', 'new-conversation', 'key:us')
    def view(start, call, kwargs):
        assert any('我喜欢简短回答' in item['content'] for item in start['runtime_context'])
        listed = call({'call_id': 'list', 'tool_name': 'bot_memory', 'arguments': {'action': 'list'}})['observation']
        assert listed['value']['items'][0]['content'] == '我喜欢简短回答'
        return submit(call, listed['value']['acknowledgement'], listed['ref'])
    result = execute(monkeypatch, new_contract, host, new_session, view)
    assert result.status == 'answered' and '我喜欢简短回答' in result.user_response


def test_verified_model_change_revokes_old_background_lease(tmp_path, monkeypatch):
    import threading
    import time
    from dataclasses import replace
    from src.application.bot.memory_worker import get_memory_worker

    _, host, _ = channel(tmp_path, monkeypatch)
    host._ensure_schema()
    worker = get_memory_worker(host, model_settings=_TEST_MODEL, process_environ={'TEST': 'first'})
    cancel = threading.Event()
    with host._connect() as conn:
        conn.execute("""INSERT INTO bot_memory_jobs (run_id,owner_scope,source_refs_json,memory_epoch,lease_id,
            lease_until,state,updated_at) VALUES ('old','owner','[]',0,'lease',?,'running','now')""", (time.time()+30,))
    worker._active = ('old', 'lease', cancel)
    new_model = replace(_TEST_MODEL, model='updated-model')
    updated = get_memory_worker(host, model_settings=new_model, process_environ={'TEST': 'second'})
    assert updated is worker and cancel.is_set()
    assert updated.consolidate.keywords['model_settings'] == new_model
    assert updated.consolidate.keywords['environ'] == {'TEST': 'second'}
    with host._connect() as conn:
        assert conn.execute("SELECT state,lease_id,attempt FROM bot_memory_jobs").fetchone() == ('pending', None, 0)


def test_local_foreground_revokes_existing_channel_background_slot(tmp_path, monkeypatch):
    import threading
    import time
    from src.application.bot.memory_worker import get_memory_worker

    local, host, session = channel(tmp_path, monkeypatch, environment='local')
    host._ensure_schema()
    worker = get_memory_worker(host, model_settings=_TEST_MODEL)
    cancel = threading.Event()
    worker._active = ('old', 'lease', cancel)
    with host._connect() as conn:
        conn.execute("""INSERT INTO bot_memory_jobs (run_id,owner_scope,source_refs_json,memory_epoch,lease_id,
            lease_until,state,updated_at) VALUES ('old','owner','[]',0,'lease',?,'running','now')""", (time.time()+30,))
    def flow(start, call, kwargs):
        assert cancel.is_set() and worker._foreground == 1
        assert 'bot_memory' not in {tool['name'] for tool in start['tools']}
        return submit(call, '普通说明。')
    assert execute(monkeypatch, local, host, session, flow).status == 'answered'
    assert worker._foreground == 0
    with host._connect() as conn:
        assert conn.execute("SELECT state,lease_id FROM bot_memory_jobs WHERE run_id='old'").fetchone() == ('pending', None)


def test_forget_then_old_summary_cannot_supply_memory_read_claim(tmp_path, monkeypatch):
    contract, host, session = channel(tmp_path, monkeypatch)
    saved_id = []
    def save(start, call, kwargs):
        saved = remember(call)['observation']
        saved_id.append(saved['value']['id'])
        return submit(call, saved['value']['acknowledgement'], saved['ref'])
    assert execute(monkeypatch, contract, host, session, save).status == 'answered'
    delete_contract = prepare_contract(_request('删除这条记忆', environment='channel'), reference_year=2026, report_now_ms=1788319188212)
    def forget(start, call, kwargs):
        deleted = call({'call_id': 'forget', 'tool_name': 'bot_memory', 'arguments': {
            'action': 'forget', 'id': saved_id[0], 'expected_epoch': 1, 'expected_revision': 1,
            'idempotency_key': 'delete', 'source_quote': '删除这条记忆'}})['observation']
        assert deleted['value']['result'] == 'deleted'
        return submit(call, deleted['value']['acknowledgement'], deleted['ref'])
    assert execute(monkeypatch, delete_contract, host, session, forget).status == 'answered'
    old_summary = ({'role': 'system', 'content': '历史摘要（仅导航）：用户喜欢简短回答。'},)
    next_contract = prepare_contract(_request('查看我的记忆', context=old_summary, environment='channel'), reference_year=2026, report_now_ms=1788319188212)
    def inspect(start, call, kwargs):
        assert any('Prior summaries cannot prove' in item['content'] for item in start['runtime_context'])
        listed = call({'call_id': 'list', 'tool_name': 'bot_memory', 'arguments': {'action': 'list'}})['observation']
        assert listed['value']['items'] == []
        denied = submit(call, '用户喜欢简短回答。', listed['ref'])
        assert denied['observation']['ok'] is False
        return submit(call, listed['value']['acknowledgement'], listed['ref'])
    answer = execute(monkeypatch, next_contract, host, session, inspect)
    assert answer.status == 'answered' and '无匹配记忆' in answer.user_response


def test_valid_memory_claim_cannot_launder_business_answer_body(tmp_path, monkeypatch):
    contract, host, session = channel(tmp_path, monkeypatch)
    def flow(start, call, kwargs):
        saved = remember(call)['observation']
        rejected = call({'call_id': 'launder', 'tool_name': 'submit_answer', 'arguments': {
            'mode': 'evidence', 'status': 'complete', 'answer_markdown': '现金余额为一百万。',
            'claims': [{'text': saved['value']['acknowledgement'], 'kind': 'current_fact',
                        'required_scope': 'point', 'observation_ids': [saved['ref']]}]}})
        assert rejected['observation']['reason'] == 'memory_receipt_supports_only_maintenance'
        return rejected
    assert execute(monkeypatch, contract, host, session, flow).status == 'failed'



def test_long_memory_public_save_then_new_conversation_search_is_honest_partial(tmp_path, monkeypatch):
    from src.application.bot.tools import conservative_json_tokens
    content = "我喜欢以账户分别展示收益与现金流，使用中文解释并保留数据的来源和时间。" * 22
    contract, host, session = channel(tmp_path, monkeypatch, text='记住：' + content)
    def save(start, call, _):
        observed = call({'call_id': 'save', 'tool_name': 'bot_memory', 'arguments': {
            'action': 'remember', 'content': content, 'source_quote': '记住：' + content,
            'kind': 'preference', 'expected_epoch': 0, 'expected_revision': 0, 'idempotency_key': 'long'}})['observation']
        assert observed['ok'] and observed['value']['readback']
        return submit(call, observed['value']['acknowledgement'], observed['ref'])
    assert execute(monkeypatch, contract, host, session, save).status == 'answered'
    request = _request('查看收益记忆', environment='channel')
    request.trusted_tool_scope['authenticated_conversation_id'] = 'new-conversation'
    contract = prepare_contract(request, reference_year=2026, report_now_ms=1788319188212)
    session = derive_pi_session_id('test', 'test-user', 'new-conversation', 'key:us')
    def search(start, call, _):
        assert conservative_json_tokens(start['runtime_context']) <= 4000
        observed = call({'call_id': 'search', 'tool_name': 'bot_memory', 'arguments': {'action': 'search', 'query': '收益'}})['observation']
        assert observed['status'] == 'partial' and observed['value']['partial']
        item = observed['value']['items'][0]
        assert item['id'] and item['revision'] == 1 and item['truncated']
        assert '部分内容' in observed['value']['acknowledgement']
        assert '无匹配记忆' not in observed['value']['acknowledgement']
        return submit(call, observed['value']['acknowledgement'], observed['ref'])
    result = execute(monkeypatch, contract, host, session, search)
    assert result.status == 'answered' and '部分内容' in result.user_response


@pytest.mark.parametrize('failure', ['recall', 'corrupt_row', 'progress', 'explicit_progress', 'scope'])
def test_optional_memory_failure_does_not_block_ordinary_question_or_enable_writes(tmp_path, monkeypatch, failure):
    import sqlite3
    from src.application.bot.memory import BotMemoryStore, scope_from_contract
    contract, host, session = channel(tmp_path, monkeypatch, text='解释收益与现金流；参考 run_123456abcdef')
    def broken(*args, **kwargs):
        raise sqlite3.OperationalError('fixture optional memory failure')
    if failure == 'recall':
        monkeypatch.setattr(BotMemoryStore, 'recall', broken)
    elif failure == 'corrupt_row':
        host._ensure_schema()
        with host._connect() as conn:
            conn.execute("""INSERT INTO bot_memory (id,owner_scope,kind,content,source_refs_json,source_time,
                revision,state,updated_at) VALUES ('broken',?,'preference','corrupt source metadata','{','now',1,'active','now')""",
                (scope_from_contract(contract).owner_scope,))
    elif failure in {'progress', 'explicit_progress'}:
        original = host.unfinished_progress
        def read_progress(*args, **kwargs):
            if failure == 'progress' and kwargs.get('progress_ref'):
                return original(*args, **kwargs)
            return broken()
        monkeypatch.setattr(host, 'unfinished_progress', read_progress)
    else:
        monkeypatch.setattr('src.application.bot.memory_worker.configured_memory_scope', broken)
    def ordinary(start, call, _):
        assert 'bot_memory' not in {item['name'] for item in start['tools']}
        context = '\n'.join(item['content'] for item in start['runtime_context'])
        assert 'memory and historical progress are unavailable' in context
        assert 'prior chat summaries' in context
        denied = call({'call_id': 'write', 'tool_name': 'bot_memory', 'arguments': {'action': 'remember'}})
        assert denied['observation']['ok'] is False
        return submit(call, '收益反映投资回报，现金流反映资金流入与流出。当前记忆不可用。')
    result = execute(monkeypatch, contract, host, session, ordinary)
    assert result.status == 'answered' and result.ok
    assert any(event.type == 'memory_unavailable' for event in result.events)
    def no_consolidation(*args):
        raise AssertionError('unavailable run must not trigger a background model')
    worker = MemoryWorker(BotMemoryStore(host), no_consolidation)
    worker.recover_jobs()
    assert worker.run_once() is False
    with host._connect() as conn:
        assert conn.execute("SELECT count(*) FROM bot_memory WHERE state='active' AND id != 'broken'").fetchone()[0] == 0
        assert conn.execute("SELECT state FROM bot_memory_jobs WHERE run_id=?", (result.run_id,)).fetchone()[0] == 'skipped'



def test_cancelled_crash_is_not_recalled_in_new_public_host_question(tmp_path, monkeypatch):
    contract, host, session = channel(tmp_path, monkeypatch, text='解释收益与现金流')
    host.start_run('run_123456abcdef', contract=contract, session_key='old-conversation')
    host.request_cancel('run_123456abcdef')
    with host._connect() as conn:
        conn.execute("UPDATE bot_runs SET started_at='2000-01-01T00:00:00+00:00' WHERE run_id='run_123456abcdef'")
    assert host.mark_stale_runs_interrupted() == 1
    def ordinary(start, call, _):
        assert 'run_123456abcdef' not in str(start['runtime_context'])
        listed = call({'call_id': 'list', 'tool_name': 'bot_memory', 'arguments': {'action': 'list'}})['observation']
        assert listed['ok'] and listed['value']['progress'] == []
        return submit(call, '收益反映投资回报，现金流反映资金流入与流出。')
    assert execute(monkeypatch, contract, host, session, ordinary).status == 'answered'
    assert host.run_record('run_123456abcdef')['status'] == 'cancelled'


@pytest.mark.parametrize('kind', [None, 'preference'])
def test_public_host_search_filters_full_authorized_history_before_limit(tmp_path, monkeypatch, kind):
    from src.application.bot.memory import BotMemoryStore, scope_from_contract
    contract, host, session = channel(tmp_path, monkeypatch, text='搜索独特目标')
    scope = scope_from_contract(contract, ['lx'])
    host._ensure_schema()
    with host._connect() as conn:
        rows = [('old', scope.owner_scope, None, '我偏好独特目标', 'active', '2000')]
        rows += [(f'recent{i}', scope.owner_scope, None, f'常规偏好{i}', 'active', '2026') for i in range(256)]
        rows += [('account', scope.owner_scope, 'sy', '隔离资料', 'active', '2026'),
                 ('owner', 'other-owner', None, '隔离资料', 'active', '2026'),
                 ('deleted', scope.owner_scope, None, '隔离资料', 'deleted', '2026')]
        conn.executemany("""INSERT INTO bot_memory
            (id,owner_scope,account_scope,content,state,updated_at,kind,source_refs_json,source_time,revision)
            VALUES (?,?,?,?,?,?,'preference','[]','2000',1)""", rows)
    assert BotMemoryStore(host).recall(scope, '不存在的关键词')['items']
    def search(start, call, _):
        for index, query in enumerate(['独特目标', '无匹配关键词', '隔离资料']):
            arguments = {'action': 'search', 'query': query}
            if kind:
                arguments['kind'] = kind
            observed = call({'call_id': f'search{index}', 'tool_name': 'bot_memory',
                             'arguments': arguments})['observation']
            assert observed['ok'], observed
            assert [item['id'] for item in observed['value']['items']] == (['old'] if index == 0 else [])
            assert observed['value']['token_upper_bound'] <= 2000
            assert not observed['value']['partial']
        return submit(call, observed['value']['acknowledgement'], observed['ref'])
    assert execute(monkeypatch, contract, host, session, search).status == 'answered'
