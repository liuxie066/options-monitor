from __future__ import annotations

import hashlib
import json

import pytest

from src.application.bot import tools as bot_tools
from src.application.bot.host import run_contract
from src.application.bot.result_admission import admit_submit_answer
from src.application.bot.task_report import TASK_MARKER, TASK_TOOL
from src.application.channels.feishu_reply_renderer import render_feishu_conversation_reply
from tests.bot_pi_test_support import _TEST_MODEL
from tests.test_bot_phase1 import _contract


def task_observation(*, partial=False, rows=1):
    return {
        'tool_name': TASK_TOOL, 'ref': 'obv_tasks', 'ok': True,
        'status': 'partial' if partial else 'complete',
        'value': {'tasks': [{'name': f'om-us-{i}.timer', 'configured': True,
            'enabled': 'enabled', 'active': 'unknown' if partial else 'active',
            'reasons': ['probe_timeout'] if partial else []} for i in range(rows)],
            'availability': 'partial' if partial else 'available'},
        'coverage': {'status': 'partial' if partial else 'complete', 'complete_for': 'point'},
        'freshness': {'status': 'current', 'as_of': '2026-09-11T00:00:00Z'},
    }


def task_claim(ref='obv_tasks'):
    return {'text': TASK_MARKER, 'kind': 'current_fact', 'required_scope': 'point', 'observation_ids': [ref]}


def answer(claims, extra=''):
    return {'mode': 'evidence' if claims else 'conceptual', 'status': 'complete',
        'answer_markdown': TASK_MARKER + extra, 'claims': claims}


def evidence(obs):
    return {obs['ref']: {**obs, 'authorized_read': True, 'observation_status': obs['status']}}


@pytest.mark.parametrize('partial,failed,status', [(False, False, 'complete'), (True, False, 'partial'), (False, True, 'partial')])
def test_fixed_report_keeps_scope_counts_and_failed_market(partial, failed, status):
    obs = task_observation(partial=partial)
    reads = {'us': {'market': 'us', 'observation': obs}}
    if failed:
        reads['hk'] = {'market': 'hk', 'observation': {'ok': False, 'diagnostic': 'scope_conflict'}}
    result = admit_submit_answer(answer([task_claim()], '\n启用表示允许定时触发。'), evidence(obs), task_reads=reads)
    approved = result['approved_answer']
    assert approved['status'] == status
    assert '已核实 1 项任务' in approved['text']
    assert '不代表整台机器' in approved['text']
    assert '启用表示允许定时触发。' in approved['text']
    if failed:
        assert approved['text'].index('HK 当前无法核实：超出当前授权范围') < approved['text'].index('已核实 1 项任务')
        assert 'HK 未配置' not in approved['text']
    assert approved['text_sha256'] == 'sha256:' + hashlib.sha256(approved['text'].encode()).hexdigest()


def test_failed_and_narrowed_reads_never_support_task_fact():
    reads = {'hk': {'market': 'hk', 'observation': {'ok': False, 'diagnostic': 'scope_conflict'}}}
    result = admit_submit_answer(answer([], '\n定时器活动与业务成功是不同信息。'), {}, task_reads=reads)
    assert result['approved_answer']['status'] == 'insufficient_evidence'
    assert '没有任务' not in result['approved_answer']['text']
    assert admit_submit_answer(answer([task_claim()]), {}, task_reads=reads)['observation']['ok'] is False
    reads['us'] = {'market': 'us', 'observation': {'ok': True, 'status': 'needs_narrowing'}}
    result = admit_submit_answer(answer([]), {}, task_reads=reads)
    assert result['approved_answer']['status'] == 'needs_narrowing'
    assert 'HK 当前无法核实' in result['approved_answer']['text']


@pytest.mark.parametrize('mutation', ['missing_marker', 'twice', 'not_first', 'wrong_claim', 'extra_claim', 'old_ref'])
def test_task_protocol_cannot_escape_binding(mutation):
    obs = task_observation()
    args = answer([task_claim()])
    if mutation == 'missing_marker':
        args['answer_markdown'] = '只有一个定时任务'
    elif mutation == 'twice':
        args['answer_markdown'] *= 2
    elif mutation == 'not_first':
        args['answer_markdown'] = '说明\n' + TASK_MARKER
    elif mutation == 'wrong_claim':
        args['claims'][0]['text'] = '整机只有一个任务'
    elif mutation == 'extra_claim':
        args['claims'].append({**task_claim(), 'text': '其他事实'})
    else:
        args['claims'] = [task_claim('obv_old')]
    assert admit_submit_answer(args, evidence(obs), task_reads={'us': {'market': 'us', 'observation': obs}})['observation']['ok'] is False


@pytest.mark.parametrize('max_chars,extra,expected', [(8000, '', '已核实 8 项'), (230, '', '缩小查询范围'), (8, '', '…'), (250, '\n\n' + '概念解释。' * 200, '缩小查询范围')])
def test_host_freezes_channel_projection_before_answer_receipt(monkeypatch, max_chars, extra, expected):
    previews = []
    accepted = []
    obs = task_observation(rows=8)
    monkeypatch.setattr(bot_tools, 'call_read_tool', lambda *a, **kw: {'ok': True})
    monkeypatch.setattr(bot_tools, 'compact_observation', lambda *a, **kw: dict(obs))

    def builder(result):
        reply = {'channel': 'feishu', 'delivery_key': 'test', 'payload': render_feishu_conversation_reply(
            message_id='test', text=result.user_response, max_chars=max_chars, reply_in_thread=False, render_route='bot')}
        previews.append(reply)
        return reply

    def process(start, *, on_tool_call, on_proposed, **kwargs):
        read = on_tool_call({'tool_name': TASK_TOOL, 'arguments': {}})
        result = on_tool_call({'tool_name': 'submit_answer', 'arguments': answer([task_claim(read['ref'])], extra)})
        assert result['observation']['ok'] is True, result
        assert previews  # Preflight happened before this successful receipt.
        approved = result['approved_answer']
        accepted.append(approved)
        proposal = {'status': 'answered', 'text': approved['text'], 'control_request': None, 'termination_reason': 'stop', 'usage': {}}
        decision = on_proposed(proposal)
        return {'ok': True, 'result': {**proposal, 'committed': decision == 'commit'}}

    monkeypatch.setattr('src.application.bot.host.run_pi_agent', process)
    result = run_contract(_contract('列任务'), model_settings=_TEST_MODEL, reply_builder=builder)
    assert result.ok, result
    assert expected in result.user_response
    assert accepted[0]['status'] == ('complete' if max_chars == 8000 else 'needs_narrowing')
    final = previews[-1]['payload']
    assert final['render_meta']['source_sha256'] == hashlib.sha256(result.user_response.strip().encode()).hexdigest()
    canonical = json.dumps(final['transport'], ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    assert final['render_meta']['rendered_sha256'] == hashlib.sha256(canonical.encode()).hexdigest()


@pytest.mark.parametrize('conflict', [False, True])
def test_progress_transaction_selects_preflighted_reply(monkeypatch, tmp_path, conflict):
    from src.application.bot.contracts import AppEvent, AppResult, utc_now_iso
    from src.application.bot.host_store import PROGRESS_RESOLUTION_CONFLICT_NOTICE
    from tests.test_bot_memory_host import channel

    original = 'run_123456789abc'
    contract, store, session = channel(tmp_path, monkeypatch, '列任务')
    store.start_run(original, contract=contract, session_key=session)
    store.append_event(AppEvent(event_id='initial', run_id=original, type='tool_result', timestamp=utc_now_iso(),
        payload={'ok': True, 'tool_name': TASK_TOOL, 'ref': 'obv_previous', 'content_hash': 'sha256:previous', 'tool_input': {'config_key': 'us'},
            'coverage': {'status': 'complete', 'complete_for': 'point'}}))
    store.finish_run(AppResult(run_id=original, status='failed', ok=False, user_response='未完成'))
    contract, _, _ = channel(tmp_path, monkeypatch, '继续 ' + original)
    monkeypatch.setattr(bot_tools, 'call_read_tool', lambda *a, **kw: {'ok': True})
    monkeypatch.setattr(bot_tools, 'compact_observation', lambda *a, **kw: task_observation())
    previews = {}
    approved = []

    def builder(result):
        assert result.user_response not in previews  # finish_run must reuse, not render again.
        reply = {'delivery_key': 'progress', 'channel': 'feishu', 'payload': render_feishu_conversation_reply(
            message_id='progress', text=result.user_response, max_chars=8000, reply_in_thread=False, render_route='bot')}
        previews[result.user_response] = reply
        return reply

    def process(start, *, on_tool_call, on_proposed, **kwargs):
        read = on_tool_call({'tool_name': TASK_TOOL, 'arguments': {}})
        args = answer([task_claim(read['ref'])])
        args['progress_resolution'] = {'progress_ref': original, 'expected_revision': 1, 'covered_goal': '列任务', 'claim_indexes': [0]}
        admitted = on_tool_call({'tool_name': 'submit_answer', 'arguments': args})
        assert admitted['observation']['ok'], admitted
        text = admitted['approved_answer']['text']
        approved.append(text)
        assert set(previews) == {text, text + PROGRESS_RESOLUTION_CONFLICT_NOTICE}
        if conflict:
            with store._connect() as conn:
                conn.execute("UPDATE bot_runs SET progress_json=json_set(progress_json,'$.revision',2) WHERE run_id=?", (original,))
        proposal = {'status': 'answered', 'text': text, 'control_request': None, 'termination_reason': 'stop', 'usage': {}}
        return {'ok': True, 'result': {**proposal, 'committed': on_proposed(proposal) == 'commit'}}

    monkeypatch.setattr('src.application.bot.host.run_pi_agent', process)
    result = run_contract(contract, model_settings=_TEST_MODEL, host_store=store, session_key=session, reply_builder=builder)
    assert result.ok, result
    assert result.user_response == approved[0] + (PROGRESS_RESOLUTION_CONFLICT_NOTICE if conflict else '')
    assert store.reply_payload('progress') == previews[result.user_response]['payload']
    progress = json.loads(store.run_record(original)['progress_json'])
    assert bool(progress['resolved_by']) is not conflict


@pytest.mark.parametrize('action', ['remember', 'list'])
def test_failed_task_report_preserves_exact_durable_memory_receipt(monkeypatch, tmp_path, action):
    from tests.test_bot_memory_host import channel, execute, remember

    contract, store, session = channel(tmp_path, monkeypatch)

    def flow(start, call, kwargs):
        observed = (remember(call) if action == 'remember' else call({
            'tool_name': 'bot_memory', 'arguments': {'action': 'list'}}))['observation']
        assert observed['ok']
        failed = call({'tool_name': TASK_TOOL, 'arguments': {'config_key': 'hk'}})
        assert not failed['ok']
        ack = observed['value']['acknowledgement']
        args = answer([{'text': ack, 'kind': 'current_fact', 'required_scope': 'point',
            'observation_ids': [observed['ref']]}], '\n' + ack)
        rejected = call({'tool_name': 'submit_answer', 'arguments': {
            **args, 'answer_markdown': args['answer_markdown'] + '\n现金有一百万。'}})
        assert rejected['observation']['reason'] == 'memory_receipt_supports_only_maintenance'
        if action == 'remember':
            unbacked = call({'tool_name': 'submit_answer', 'arguments': answer([], '\n已记住。')})
            assert unbacked['observation']['reason'] == 'memory_operation_receipt_required'
        accepted = call({'tool_name': 'submit_answer', 'arguments': args})
        assert accepted['approved_answer']['status'] == 'insufficient_evidence'
        assert ack in accepted['approved_answer']['text']
        return accepted

    result = execute(monkeypatch, contract, store, session, flow)
    assert result.status == 'answered', result
    assert 'HK 当前无法核实：超出当前授权范围' in result.user_response
    assert '现金有一百万' not in result.user_response
    with store._connect() as conn:
        assert conn.execute("SELECT count(*) FROM bot_memory WHERE state='active'").fetchone()[0] == (action == 'remember')
        response = json.loads(conn.execute('SELECT response_json FROM bot_runs WHERE run_id=?', (result.run_id,)).fetchone()[0])
    assert response['user_response'] == result.user_response
