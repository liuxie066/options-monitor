from src.application import query_cursor
"""Python Bot facade: tools, grounded input, continuity, bounded completion and cancellation."""
import json
import time
from dataclasses import replace

import pytest

from src.application.bot.contracts import BotRequest, BotScope
from src.application.bot.host import run_contract
from src.application.bot.host_store import BotHostStore
from src.application.bot.model_config import ModelSettings
from src.application.bot.runtime import RunStopped, compact_messages, run_agent, token_estimate
from src.application.bot.service import prepare_contract

MODEL = ModelSettings.from_config({'provider': 'deepseek', 'model': 'test-model', 'context_window_tokens': 24000, 'max_output_tokens': 2048})
TOOL = {'name': 'read', 'description': 'read', 'input_schema': {'type': 'object'}}


def answer(text='答案', reason='stop'):
    return {'message': {'role': 'assistant', 'content': text}, 'finish_reason': reason}


def call(name='read', args=None, id='call1'):
    return {'message': {'role': 'assistant', 'content': '', 'tool_calls': [{'id': id, 'function': {'name': name, 'arguments': json.dumps(args or {})}}]}, 'finish_reason': 'tool_calls'}


def script(replies, captured):
    def request(**kw):
        captured.append(kw)
        return replies.pop(0)
    return request


def run(replies, captured, **kw):
    return run_agent(settings=MODEL, api_key='', messages=[{'role': 'system', 'content': 'read first'}, {'role': 'user', 'content': '解释策略'}],
                     tools=[TOOL], deadline=time.monotonic()+30, reserve_seconds=0, cancelled=lambda: False, event=lambda *a: None,
                     request=script(replies, captured), **kw)


def _channel_contract(question, *, request_id, sender='alice', conversation='one'):
    return prepare_contract(BotRequest(
        request_id=request_id, source_entry='test', user_message=question,
        execution_environment='channel', explicit_scope=BotScope(config_key='us'),
        trusted_tool_scope={'authenticated_channel': 'test', 'authenticated_sender_id': sender,
                            'authenticated_conversation_id': conversation}))


def test_tools_then_final_without_submission():
    captured = []
    text = run([call(), answer('过滤条件来自已读结果。')], captured, call_tool=lambda *a: {'ok': True, 'data': {'reason': 'delta_too_high', 'account': 'lx'}})
    assert text == '过滤条件来自已读结果。'
    assert 'delta_too_high' in captured[-1]['messages'][-1]['content']


def test_tool_limit_reserves_plain_answer():
    captured = []
    assert run([call(), answer()], captured, tool_limit=1, call_tool=lambda *a: {'ok': True}) == '答案'
    assert captured[-1]['tools'] == []


def test_failure_is_visible_for_answer():
    captured=[]
    run([call(), answer('记录不可用，不能判断过滤原因。')], captured, call_tool=lambda *a: {'ok': False, 'error': {'code': 'UNAVAILABLE'}})
    assert 'UNAVAILABLE' in captured[-1]['messages'][-1]['content']


def test_unknown_tool_never_executes():
    captured=[]
    run([call('shell'), answer('无法执行')], captured, call_tool=lambda *a: pytest.fail('unauthorized tool'))
    assert 'INPUT_ERROR' in captured[-1]['messages'][-1]['content']


def _tool_batch(calls):
    return {'message': {'role': 'assistant', 'content': '', 'tool_calls': calls}, 'finish_reason': 'tool_calls'}


@pytest.fixture
def failed_batch_scene(tmp_path, monkeypatch):
    from src.application.bot import scene

    original = scene._SCENE_PATH
    definition = json.loads(original.read_text())
    for relative in definition['prompt_fragments']:
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((original.parent / relative).read_bytes())

    def configure(**limits):
        definition['runtime'].update(limits)
        target = tmp_path / 'scene.json'
        target.write_text(json.dumps(definition))
        monkeypatch.setattr(scene, '_SCENE_PATH', target)
        scene.load_general_scene.cache_clear()

    yield configure
    scene.load_general_scene.cache_clear()


def _host_batches(monkeypatch, batches):
    """Real Host/scene/runtime; only model and read leaf are synthetic."""
    from src.application.bot import tools

    captured, reads = [], []

    def read(_name, payload, **_kwargs):
        name = payload['relative_name']
        reads.append(name)
        if name.startswith('X'):
            raise RuntimeError('synthetic read failure')
        return {'ok': name.startswith('S'), 'data': {'text': name},
                'error': {'code': 'SYNTHETIC_UNAVAILABLE'} if not name.startswith('S') else None}

    def request(**kwargs):
        index = len(captured)
        captured.append(json.loads(json.dumps({'tools': kwargs['tools'], 'messages': kwargs['messages']})))
        if index >= len(batches) or not kwargs['tools']:
            return answer('合成读取已结束；缺失证据保持未知。')
        calls = []
        for offset, kind in enumerate(batches[index]):
            ident = f'{index}-{offset}'
            item = call('project_files', {'action': 'read', 'relative_name': f'{kind}-{ident}.md',
                                         'start_line': 1, 'max_lines': 1}, id=ident)['message']['tool_calls'][0]
            if kind == 'U':
                item['function']['name'] = 'unavailable_tool'
            elif kind == 'J':
                item['function']['arguments'] = '{'
            elif kind == 'I':
                item['function']['arguments'] = json.dumps({'action': 'read', 'relative_name': f'I-{ident}.md',
                                                          'max_lines': 'invalid'})
            calls.append(item)
        return _tool_batch(calls)

    monkeypatch.setattr(tools, 'call_read_tool', read)
    result = run_contract(contract('解释合成读取证据'),
                          model_settings=replace(MODEL, context_window_tokens=200000),
                          model_request=request)
    assert result.ok, result
    observations = [json.loads(item['content']) for item in captured[-1]['messages'] if item['role'] == 'tool']
    return captured, reads, observations


def test_runtime_default_failure_limit_counts_batches():
    captured, reads = [], []
    calls = [call(args={'index': index}, id=str(index))['message']['tool_calls'][0] for index in range(3)]

    def read(_name, args):
        reads.append(args)
        return {'ok': args.get('recovery', False)}

    assert run([_tool_batch(calls), call(args={'recovery': True}, id='recovery'), answer()],
               captured, call_tool=read) == '答案'
    assert len(reads) == 4 and captured[1]['tools'] == [TOOL]


@pytest.mark.parametrize('batches,expected_tools', [
    (['FFF', 'S'], [True, True, True]),
    (['FF', 'FF', 'S'], [True, True, True, True]),
    (['F', 'F', 'F'], [True, True, True, False]),
])
def test_host_failure_limit_counts_whole_batches(monkeypatch, batches, expected_tools):
    captured, reads, observations = _host_batches(monkeypatch, batches)
    assert [bool(item['tools']) for item in captured] == expected_tools
    assert len(reads) == sum(map(len, batches))
    assert [bool(item['ok']) for item in observations] == [kind == 'S' for batch in batches for kind in batch]


@pytest.mark.parametrize('success_position', range(4))
def test_host_partial_success_resets_batch_streak_in_any_order(monkeypatch, success_position):
    partial = 'F' * success_position + 'S' + 'F' * (3 - success_position)
    captured, reads, observations = _host_batches(monkeypatch, ['F', 'F', partial, 'F', 'S'])
    assert [bool(item['tools']) for item in captured] == [True] * 6
    assert len(reads) == 8
    assert sum(bool(item['ok']) for item in observations) == 2
    assert observations[-1]['ok'] is True


@pytest.mark.parametrize('configured,effective', [(1, 1), (2, 2), (4, 4), (0, 3)])
def test_host_honors_scene_failed_batch_limit(monkeypatch, failed_batch_scene, configured, effective):
    failed_batch_scene(max_consecutive_failed_tool_batches=configured)
    captured, reads, _ = _host_batches(monkeypatch, ['F'] * effective)
    assert [bool(item['tools']) for item in captured] == [True] * effective + [False]
    assert len(reads) == effective


@pytest.mark.parametrize('kind,code', [('U', 'INPUT_ERROR'), ('J', 'INPUT_ERROR'),
                                      ('I', 'TOOL_ERROR'), ('X', 'TOOL_ERROR')])
def test_host_generated_and_read_errors_share_batch_limit(monkeypatch, kind, code):
    captured, reads, observations = _host_batches(monkeypatch, [kind * 3, 'S'])
    assert [bool(item['tools']) for item in captured] == [True, True, True]
    assert len(reads) == (4 if kind == 'X' else 1)
    assert [item['error']['code'] for item in observations[:3]] == [code] * 3
    assert observations[-1]['ok'] is True


@pytest.mark.parametrize('kind', ['U', 'J', 'I', 'X'])
def test_host_success_with_generated_errors_resets_streak(monkeypatch, kind):
    captured, reads, observations = _host_batches(monkeypatch, ['F', 'F', 'S' + kind * 2, 'F', 'S'])
    assert [bool(item['tools']) for item in captured] == [True] * 6
    assert len(reads) == (7 if kind == 'X' else 5)
    assert [bool(item['ok']) for item in observations] == [False, False, True, False, False, False, True]


@pytest.mark.parametrize('repeated', [False, True])
def test_partial_success_cannot_reopen_budget_final(repeated):
    captured, reads = [], []
    calls = [call(args={'index': 0 if repeated else index}, id=str(index))['message']['tool_calls'][0]
             for index in range(3)]

    def read(_name, args):
        reads.append(args)
        return {'ok': True}

    run([_tool_batch(calls), answer()], captured, call_tool=read, tool_limit=12 if repeated else 1)
    assert len(reads) == (2 if repeated else 1)
    assert captured[-1]['tools'] == []
    observations = [json.loads(item['content']) for item in captured[-1]['messages'] if item['role'] == 'tool']
    assert observations[0]['ok'] is True
    assert observations[-1]['error']['code'] == 'BUDGET_EXHAUSTED'


@pytest.mark.parametrize('same_batch', [False, True])
def test_duplicate_tool_ids_keep_batch_validation(same_batch):
    reads = []
    duplicate = call()['message']['tool_calls'][0]
    replies = [_tool_batch([duplicate, duplicate])] if same_batch else [call(), call()]
    with pytest.raises(RunStopped, match='duplicate_tool_id'):
        run(replies, [], call_tool=lambda *args: reads.append(args) or {'ok': False})
    assert len(reads) == (0 if same_batch else 1)


def test_truncation_has_one_continuation():
    captured=[]
    assert run([answer('前半', 'length'), answer('后半')], captured, call_tool=lambda *a: {}) == '前半后半'
    assert not captured[-1]['tools']
    with pytest.raises(RunStopped, match='output_length'):
        run([answer('半', 'length'), answer('半', 'length')], [], call_tool=lambda *a: {})


def test_current_turn_compacts_paired_results_without_saved_history():
    messages=[{'role':'system','content':'rules'}, {'role':'user','content':'question'}]
    for i in range(6):
        messages.extend([call(id=str(i))['message'], {'role':'tool','tool_call_id':str(i),'content':'x'*1500}])
    compacted=compact_messages(messages, 2500, [TOOL])
    assert token_estimate([compacted,[TOOL]]) <= 2500
    assert compacted[0] == messages[0] and messages[1] in compacted
    assert compacted[-1] == messages[-1]
    ids={c['id'] for m in compacted for c in m.get('tool_calls',[])}
    assert ids == {m['tool_call_id'] for m in compacted if m['role']=='tool'}
    assert any('partial history' in m.get('content','') for m in compacted)


def contract(question):
    return prepare_contract(BotRequest(request_id='req-test', source_entry='local', user_message=question, explicit_scope=BotScope()))


def test_host_context_and_answer_commit_together(tmp_path):
    store=BotHostStore(tmp_path/'host.db')
    first=run_contract(contract('解释 covered call'), model_settings=MODEL, host_store=store, session_key='local-test', model_request=script([answer('备兑看涨需要持有对应股票。')], []))
    assert first.ok, first
    captured=[]
    second=run_contract(contract('那风险呢'), model_settings=MODEL, host_store=store, session_key='local-test', model_request=script([answer('收益上限受到限制。')], captured))
    assert second.ok, second
    assert any(m.get('content')=='解释 covered call' for m in captured[0]['messages'])
    assert len(store.chat_messages('local-test')) == 4
    assert store.chat_messages('other-user') == []
    assert json.loads(store.run_record(second.run_id)['response_json'])['user_response'] == second.user_response


def test_cancel_during_model_does_not_persist_answer(tmp_path):
    store=BotHostStore(tmp_path/'host.db')
    started=time.monotonic()
    def slow(**kw):
        time.sleep(.15)
        return answer('too late')
    result=run_contract(contract('read'), model_settings=MODEL, host_store=store, session_key='cancel', model_request=slow, is_cancelled=lambda: time.monotonic()-started>.04)
    assert result.status=='cancelled'
    assert store.chat_messages('cancel') == []


def test_project_source_through_public_tool_owner(tmp_path, monkeypatch):
    from src.application.agent_tools import project, project_reader
    (tmp_path/'README.md').write_text('Covered call requires owned shares.\n')
    monkeypatch.setattr(project, 'repo_base', lambda: tmp_path)
    monkeypatch.setattr(query_cursor, '_key', lambda: 'isolated-test-key')
    captured=[]
    result=run_contract(contract('解释策略'), model_settings=MODEL,
        model_request=script([call('project_files', {'action':'read','relative_name':'README.md','start_line':1,'max_lines':1}), answer('策略要求持有股票。')], captured))
    assert result.ok, result
    content='\n'.join(m.get('content','') for m in captured[-1]['messages'] if m['role']=='tool')
    assert 'Covered call requires owned shares.' in content
    assert 'tool_directory' not in {t['name'] for t in captured[0]['tools']}


def test_compaction_preserves_current_followup_and_tool_pair():
    messages=[{'role':'system','content':'rules'}, {'role':'user','content':'old'*3000},
              {'role':'assistant','content':'old answer'*1000}, {'role':'user','content':'current question'},
              call()['message'], {'role':'tool','tool_call_id':'call1','content':'current evidence'},
              {'role':'system','content':'answer now'}]
    compacted=compact_messages(messages, 1600, [TOOL])
    assert any(m.get('content')=='current question' for m in compacted)
    assert any(m.get('content')=='current evidence' for m in compacted)


@pytest.mark.parametrize('api', ['openai-completions','openai-responses'])
def test_provider_wire_format_and_fractional_timeout(monkeypatch, api):
    from src.application.bot.runtime import request_model
    from src.infrastructure import openai_chat_completions, openai_responses
    seen=[]
    def post(url, payload, **kwargs):
        seen.append((url,payload,kwargs))
        if api=='openai-completions':
            return {'choices':[{'message':{'role':'assistant','content':'ok'},'finish_reason':'stop'}]}
        return {'status':'completed','output':[{'type':'message','content':[{'type':'output_text','text':'ok'}]}]}
    monkeypatch.setattr(openai_chat_completions,'_post_json',post)
    monkeypatch.setattr(openai_responses,'_post_json',post)
    settings=replace(MODEL,api_kind=api)
    messages=[{'role':'system','content':'rules'}, {'role':'user','content':'question'}, call()['message'],
              {'role':'tool','tool_call_id':'call1','content':'evidence'}]
    result=request_model(settings=settings,api_key='test',messages=messages,tools=[TOOL],timeout=.25,require_tool=True)
    assert result['message']['content']=='ok'
    assert seen[0][1]['tool_choice']=='required'
    assert seen[0][2]['timeout']==.25
    if api=='openai-responses':
        assert seen[0][1]['input'][-2]['type']=='function_call'
        assert seen[0][1]['input'][-1]['call_id']=='call1'
        assert seen[0][1]['store'] is False
    else:
        assert seen[0][1]['messages'][-1]['tool_call_id']=='call1'


def test_memory_remember_then_cross_conversation_recall_and_sender_isolation(tmp_path, monkeypatch):
    from src.application.bot.memory import BotMemoryStore, scope_from_contract
    from src.application.bot.session import session_key_for_contract
    monkeypatch.setattr('src.application.agent_tool_config.load_runtime_config',lambda **kw:(tmp_path,{'accounts':['lx']}))
    store=BotHostStore(tmp_path/'host.db')
    def channel(question, sender='alice', conversation='one'):
        return _channel_contract(question, request_id='memory-test', sender=sender, conversation=conversation)
    c=channel('记住：我喜欢简短回答')
    captured=[]
    result=run_contract(c,model_settings=MODEL,host_store=store,session_key=session_key_for_contract(c),model_request=script([
        call('bot_memory',{'action':'remember','idempotency_key':'one','expected_revision':0,'expected_epoch':0,
                          'content':'我喜欢简短回答','kind':'preference','source_quote':'记住：我喜欢简短回答'}),answer('已记住。')],captured))
    assert result.ok, result
    assert json.loads(captured[-1]['messages'][-1]['content'])['ok']
    assert BotMemoryStore(store).recall(scope_from_contract(c))['items'][0]['content']=='我喜欢简短回答'
    other=[]
    assert run_contract(channel('我喜欢怎样的回答',conversation='two'),model_settings=MODEL,host_store=store,session_key=session_key_for_contract(channel('question',conversation='two')),model_request=script([answer('简短回答。')],other)).ok
    assert '我喜欢简短回答' in json.dumps(other[0]['messages'],ensure_ascii=False)
    stranger=[]
    assert run_contract(channel('我喜欢怎样的回答','bob'),model_settings=MODEL,host_store=store,session_key=session_key_for_contract(channel('question','bob')),model_request=script([answer('尚无偏好记忆。')],stranger)).ok
    assert '我喜欢简短回答' not in json.dumps(stranger[0]['messages'],ensure_ascii=False)


def test_outbox_failure_rolls_back_answer_and_context(tmp_path):
    store=BotHostStore(tmp_path/'host.db')
    def fail(result):
        raise ValueError('cannot prepare reply')
    result=run_contract(contract('question'), model_settings=MODEL,host_store=store,session_key='s',
                        reply_builder=fail,model_request=script([answer()],[]))
    assert not result.ok
    assert store.chat_messages('s')==[]
    assert store.run_record(result.run_id)['response_json'] is None


def test_recorded_filter_reason_through_host(tmp_path, example_config_path, monkeypatch):
    from tests.candidate_evidence_helpers import seal_opening_candidate_fixture
    from src.application.agent_tools import project_reader
    monkeypatch.setenv('OM_RUNTIME_ROOT', str(tmp_path))
    monkeypatch.setattr(query_cursor, '_key', lambda: 'isolated-filter-cursor')
    seal_opening_candidate_fixture(tmp_path, run_id='test-run', rejected_rows=[
        {'symbol':'NVDA','contract_symbol':'NVDA-test','rule':'risk_spread'}])
    c=prepare_contract(BotRequest(request_id='filter',source_entry='test',user_message='NVDA 为什么被过滤',
                                  explicit_scope=BotScope(config_path=str(example_config_path))))
    captured=[]
    result=run_contract(c,model_settings=MODEL,model_request=script([
        call('candidate_filter_explain',{'symbol':'NVDA','account':'lx','run_id':'test-run','limit':1}),
        answer('记录显示价差不合格。')],captured))
    assert result.ok, result
    observation=json.loads(captured[-1]['messages'][-1]['content'])
    assert observation['ok'], observation
    assert observation['data']['account']=='lx'
    assert 'risk_spread' in json.dumps(observation)


def test_actual_error_writer_to_host_answer(tmp_path, example_config_path, monkeypatch):
    from tests.test_runtime_scoped_diagnostics import build_runtime_diagnostic_fixture
    from src.application.agent_tools import project_reader
    fixture=build_runtime_diagnostic_fixture(tmp_path/'runtime')
    monkeypatch.setenv('OM_RUNTIME_ROOT',str(fixture['root']))
    monkeypatch.setattr(query_cursor,'_key',lambda:'isolated-error-cursor')
    c=prepare_contract(BotRequest(request_id='error',source_entry='test',user_message='这次扫描为什么失败',
                                  explicit_scope=BotScope(config_path=str(example_config_path))))
    captured=[]
    result=run_contract(c,model_settings=MODEL,model_request=script([
        call('runtime_logs',{'account':'lx','run_id':fixture['run_id']}),answer('记录显示账户配置哈希不一致。')],captured))
    assert result.ok, result
    observation=json.loads(captured[-1]['messages'][-1]['content'])
    assert observation['ok'], observation
    assert 'account_config_hash_mismatch' in json.dumps(observation)
    assert 'private stderr' not in json.dumps(observation)


def test_failed_provider_after_read_gets_one_final_attempt():
    replies=iter([call(), RuntimeError('provider failed'), answer('已有记录表明过滤原因是价差。')])
    seen=[]
    def request(**kw):
        seen.append(kw)
        response=next(replies)
        if isinstance(response,Exception):
            raise response
        return response
    result=run_agent(settings=MODEL,api_key='',messages=[{'role':'user','content':'why'}],tools=[TOOL],
        call_tool=lambda *a:{'ok':True,'data':'risk_spread'},deadline=time.monotonic()+30,reserve_seconds=0,
        cancelled=lambda:False,event=lambda *a:None,request=request)
    assert '价差' in result and not seen[-1]['tools']


def test_wrong_sender_session_never_invokes_model(tmp_path):
    from src.application.bot.session import session_key_for_contract
    c=_channel_contract('question', request_id='identity', conversation='chat')
    other=replace(c,input={**c.input,'authenticated_sender_id':'bob'})
    result=run_contract(c,model_settings=MODEL,host_store=BotHostStore(tmp_path/'host.db'),
        session_key=session_key_for_contract(other),model_request=lambda **kw:pytest.fail('wrong identity reached model'))
    assert not result.ok


def test_replaced_run_lease_cannot_write_memory(tmp_path, monkeypatch):
    from src.application.bot.memory import BotMemoryStore, scope_from_contract
    from src.application.bot.session import session_key_for_contract
    monkeypatch.setattr('src.application.agent_tool_config.load_runtime_config',lambda **kw:(tmp_path,{'accounts':['lx']}))
    store=BotHostStore(tmp_path/'host.db')
    c=_channel_contract('记住：简短回答', request_id='memory-lease')
    replies=iter([call('bot_memory',{'action':'remember','idempotency_key':'lease','expected_revision':0,'expected_epoch':0,
        'content':'简短回答','kind':'preference','source_quote':'记住：简短回答'}),answer('已记住')])
    def request(**kw):
        with store._connect() as conn:
            conn.execute("UPDATE bot_runs SET lease_id='replacement'")
        return next(replies)
    result=run_contract(c,model_settings=MODEL,host_store=store,session_key=session_key_for_contract(c),model_request=request)
    assert result.error['code']=='MEMORY_UNCONFIRMED'
    assert BotMemoryStore(store).recall(scope_from_contract(c))['items']==[]


def test_native_observation_can_back_explicit_experience(tmp_path, monkeypatch):
    from src.application.bot.memory import BotMemoryStore, scope_from_contract
    from src.application.bot.session import session_key_for_contract
    monkeypatch.setattr('src.application.agent_tool_config.load_runtime_config',lambda **kw:(tmp_path,{'accounts':['lx']}))
    monkeypatch.setattr('src.application.bot.tools.call_read_tool',lambda *a,**kw:{'ok':True,'data':{
        'scope':{'account':'lx'},'coverage':{'status':'complete'},'diagnostics':[{'reason':'account_config_hash_mismatch'}]}})
    c=_channel_contract('请读取报错，并记住这次 lx 的错误原因', request_id='experience')
    store=BotHostStore(tmp_path/'host.db')
    count=0
    def request(**kw):
        nonlocal count
        count+=1
        if count==1:
            return call('runtime_logs',{'account':'lx','run_id':'synthetic-error'})
        if count==2:
            observation=json.loads(kw['messages'][-1]['content'])
            assert observation['ok'],observation
            return call('bot_memory',{'action':'remember','idempotency_key':'exp','expected_revision':0,'expected_epoch':0,
                'kind':'experience','account_scope':'lx','content':'account_config_hash_mismatch',
                'source_refs':[observation['memory_source_ref']],'source_quote':'请读取报错，并记住这次 lx 的错误原因'},id='memory')
        assert json.loads(kw['messages'][-1]['content'])['ok'],kw['messages'][-1]
        return answer('已记录这次报错。')
    result=run_contract(c,model_settings=MODEL,host_store=store,session_key=session_key_for_contract(c),model_request=request)
    assert result.ok,result
    assert BotMemoryStore(store).recall(scope_from_contract(c,['lx']))['items'][0]['content']=='account_config_hash_mismatch'


@pytest.mark.parametrize('reason', ['max_output_tokens','content_filter'])
def test_responses_continues_only_output_limit(monkeypatch,reason):
    from src.application.bot.runtime import request_model
    monkeypatch.setattr('src.application.bot.runtime.create_response',lambda **kw:{
        'status':'incomplete','incomplete_details':{'reason':reason},'output':[]})
    settings=replace(MODEL,api_kind='openai-responses')
    kwargs=dict(settings=settings,api_key='',messages=[{'role':'user','content':'question'}],tools=[],timeout=1)
    if reason=='max_output_tokens':
        assert request_model(**kwargs)['finish_reason']=='length'
    else:
        with pytest.raises(RunStopped,match='incomplete_response'):
            request_model(**kwargs)


def test_provider_usage_keeps_current_evidence_and_tools_available():
    captured=[]
    settings=replace(MODEL,context_window_tokens=4096)
    bulky_tool={**TOOL,'description':'x'*2000}
    first=call()
    first['usage']={'prompt_tokens':500}
    text=run_agent(settings=settings,api_key='',messages=[{'role':'user','content':'question'}],tools=[bulky_tool],
        deadline=time.monotonic()+30,reserve_seconds=0,cancelled=lambda:False,event=lambda *a:None,
        request=script([first,answer('保留证据形成回答')],captured),call_tool=lambda *a:{'ok':True,'data':{'text':'FACT'*450}})
    assert text=='保留证据形成回答'
    assert captured[0]['require_tool'] is True
    assert captured[-1]['tools'] == [bulky_tool]
    assert any('FACT'*450 in message.get('content','') for message in captured[-1]['messages'])


def test_followup_can_answer_from_context_without_forced_tool():
    captured=[]
    messages=[{'role':'system','content':'rules'}, {'role':'user','content':'解释策略'},
              {'role':'assistant','content':'策略说明'}, {'role':'user','content':'那风险呢'}]
    text=run_agent(settings=MODEL,api_key='',messages=messages,tools=[TOOL],
        deadline=time.monotonic()+30,reserve_seconds=0,cancelled=lambda:False,event=lambda *a:None,
        request=script([answer('风险说明')],captured),call_tool=lambda *a:pytest.fail('unexpected tool'))
    assert text=='风险说明'
    assert captured[0]['require_tool'] is False


def test_source_lines_are_numbered_without_mutating_original():
    from src.application.bot.tools import model_observation
    source={'ok':True,'data':{'action':'read','config_revision':'a'*64,'source_hash':'b'*64,
        'source':{'content_hash':'c'*64},'body_range':{'start_line':661,'start_char':0},
        'text':'def policy():\n    return True\n'}}
    result=model_observation('project_files',source)
    assert result['data']['text']=='661|def policy():\n662|    return True\n663|'
    assert not {'config_revision','source_hash'} & result['data'].keys()
    assert 'content_hash' not in result['data']['source']
    assert source['data']['text']=='def policy():\n    return True\n'


def test_opening_policy_fits_one_host_observation():
    import inspect
    from domain.domain.engine.candidate_engine import evaluate_opening_candidate_policy
    from src.application.bot.tools import call_read_tool, conservative_json_tokens, model_observation
    lines, start_line = inspect.getsourcelines(evaluate_opening_candidate_policy)
    payload={'action':'read','resource':'project',
             'relative_name':'domain/domain/engine/candidate_engine.py',
             'start_line':start_line,'max_lines':300}
    response=call_read_tool('project_files',payload,allowed_tools=('project_files',))
    observation=model_observation('project_files',response)
    assert observation['ok'], observation
    assert 'known earnings event falls within six calendar days' in observation['data']['text']
    assert observation['data']['body_range']['end_line'] >= start_line + len(lines) - 1
    assert conservative_json_tokens(observation) <= 8000
