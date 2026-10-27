"""New-tool-only batch authorization, real schema and offline dialogue acceptance."""
from contextlib import contextmanager
from dataclasses import replace
import time

import pytest

from src.application import option_candidates as app
from src.application.bot import tools
from src.application.bot.host import run_contract
from src.application.bot.scene import build_scene_manifest, load_general_scene
from src.application.bot.session import session_key_for_contract
from src.application.read_execution import bounded_call
from tests.test_bot_cross_market_read import _contract, _scope
from tests.test_bot_python_runtime import MODEL, answer, call, script


BATCH = {'queries': [
    {'symbol': 'US.NVDA', 'strategy': 'short_call', 'strike': 230},
    {'symbol': 'HK.00700', 'strategy': 'short_put', 'strike': 370},
]}


def invoke(payload=BATCH, **kwargs):
    return tools.call_read_tool('option_candidates', payload, allowed_tools=('option_candidates',), **kwargs)


def test_build_payload_does_not_apply_single_market_router(monkeypatch, tmp_path):
    _, fixed = _scope(tmp_path, monkeypatch, ['us', 'hk'])
    monkeypatch.setattr(tools, '_route_read_market', lambda *a: pytest.fail('single-market routing'))
    payload, error = tools.build_tool_payload('option_candidates', BATCH, fixed_input=fixed)
    assert error is None
    assert payload == BATCH


@pytest.mark.parametrize('missing', [None, 'read_generation', 'read_markets', 'bot_config_path', 'config_path', 'config_key'])
def test_bot_missing_trusted_scope_never_falls_back_to_cli(monkeypatch, tmp_path, missing):
    _, fixed = _scope(tmp_path, monkeypatch, ['us', 'hk'])
    if missing:
        fixed.pop(missing)
    else:
        fixed = None
    monkeypatch.setattr(tools, 'execute_tool', lambda *a: pytest.fail('untrusted execution'))
    result = invoke(trusted_input=fixed)
    assert result['error']['code'] == 'PERMISSION_DENIED'
    assert 'host_scope' not in result


@pytest.mark.parametrize('change', ['outside_market', 'generation', 'grant', 'root', 'identity', 'unreadable_root'])
def test_whole_request_denied_before_executor(monkeypatch, tmp_path, change):
    _, fixed = _scope(tmp_path, monkeypatch, ['us'] if change == 'outside_market' else ['us', 'hk'])
    if change == 'generation':
        fixed['read_generation'] = 'old'
    elif change == 'grant':
        fixed['read_markets'] = ['us']
    elif change == 'root':
        monkeypatch.setenv('OM_RUNTIME_ROOT', str(tmp_path / 'other'))
    elif change == 'unreadable_root':
        def unavailable(**kwargs):
            raise ValueError('invalid runtime root record')
        monkeypatch.setattr('src.application.runtime_paths.resolve_runtime_root', unavailable)
    elif change == 'identity':
        monkeypatch.setattr('src.application.bot.config_scope.resolve_trusted_config_scope',
                            lambda **kw: ('us', kw['config_path'], 'test'))
    monkeypatch.setattr(tools, 'execute_tool', lambda *a: pytest.fail('partial request executed'))
    result = invoke(trusted_input=fixed)
    assert result['error']['code'] == 'PERMISSION_DENIED'
    assert 'host_scope' not in result


def test_dual_market_context_propagates_deadline_paths_and_cancellation(monkeypatch, tmp_path):
    configs, fixed = _scope(tmp_path, monkeypatch, ['us', 'hk'])
    deadline = time.monotonic() + 5
    cancelled = lambda: False
    contexts = []
    original = app.option_candidates_context
    @contextmanager
    def capture(**kwargs):
        contexts.append(kwargs)
        with original(**kwargs):
            yield
    monkeypatch.setattr(app, 'option_candidates_context', capture)
    def execute(name, payload):
        assert name == 'option_candidates' and payload == BATCH
        context = bounded_call(app._context.get, deadline=deadline, cancelled=cancelled)
        assert context.config_paths == {market: str(configs[market][0].resolve()) for market in ('hk', 'us')}
        assert context.runtime_root == tmp_path.resolve()
        assert context.deadline == deadline and context.cancelled is cancelled
        return {'ok': True, 'data': {'results': []}}
    monkeypatch.setattr(tools, 'execute_tool', execute)
    result = invoke(trusted_input=fixed, deadline_monotonic=deadline, cancelled=cancelled)
    assert result['host_scope'] == {'tool': 'option_candidates', 'markets': ['hk', 'us']}
    assert len(contexts) == 1
    assert app._context.get().config_paths is None


def test_missing_config_keeps_explicit_path_for_app_unavailable(monkeypatch, tmp_path):
    configs, fixed = _scope(tmp_path, monkeypatch, ['us', 'hk'])
    configs['hk'][0].unlink()
    def execute(name, payload):
        paths = app._context.get().config_paths
        assert paths['hk'] == str(tmp_path / 'config.hk.json')
        assert paths['us'] == str(configs['us'][0])
        return {'ok': True, 'data': {'results': [{'outcome': 'unavailable'}]}}
    monkeypatch.setattr(tools, 'execute_tool', execute)
    assert invoke(trusted_input=fixed)['ok'] is True


@pytest.mark.parametrize('payload', [
    {'queries': [{'symbol': 'NVDA', 'strategy': 'put'}]},
    {'queries': [{'symbol': '腾讯', 'strategy': 'short_put'}]},
    {'queries': [{'symbol': 'NVDA', 'strategy': 'short_call', 'strike': '230'}]},
    {'queries': [{'symbol': 'NVDA', 'strategy': 'short_call', 'host': 'evil'}]},
    {**BATCH, 'runtime_root': '/tmp/evil'},
    {**BATCH, 'config_key': 'hk'},
    {'queries': [BATCH['queries'][0], {'symbol': 'NVDA', 'strategy': 'short_call', 'strike': 230.0}]},
])
def test_strict_normalization_rejects_before_executor(monkeypatch, tmp_path, payload):
    _, fixed = _scope(tmp_path, monkeypatch, ['us', 'hk'])
    monkeypatch.setattr(tools, 'execute_tool', lambda *a: pytest.fail('invalid input executed'))
    result = invoke(payload, trusted_input=fixed)
    assert result['error']['code'] == 'INPUT_ERROR'


def test_host_preserves_verified_markets_and_permission_denial(monkeypatch, tmp_path):
    _, fixed = _scope(tmp_path, monkeypatch, ['us', 'hk'])
    request = _contract(fixed)
    captured = []
    monkeypatch.setattr(tools, 'execute_tool', lambda *a: {'ok': True, 'data': {'results': []}})
    result = run_contract(request, model_settings=MODEL, session_key=session_key_for_contract(request),
                          model_request=script([call('option_candidates', BATCH), answer('查询已完成')], captured))
    observation = next(event.payload for event in result.events if event.type == 'tool_result')
    assert observation['host_scope'] == {'tool': 'option_candidates', 'markets': ['hk', 'us']}
    assert any(load_general_scene()['option_candidates_guidance'] == msg.get('content')
               for msg in captured[0]['messages'] if msg['role'] == 'system')
    _, fixed = _scope(tmp_path, monkeypatch, ['us'])
    request = _contract(fixed)
    monkeypatch.setattr(tools, 'execute_tool', lambda *a: pytest.fail('denied execution'))
    result = run_contract(request, model_settings=MODEL, session_key=session_key_for_contract(request),
                          model_request=script([call('option_candidates', BATCH), answer('市场未获授权')], []))
    observation = next(event.payload for event in result.events if event.type == 'tool_result')
    assert observation['error']['code'] == 'PERMISSION_DENIED'
    assert 'host_scope' not in observation


@pytest.mark.parametrize('message', ['腾讯370P', 'NVDA 230c', 'put', 'call?', '腾讯put', 'NVDA call', '卖put',
    '卖370P', '腾讯买370P', '卖出平仓370P',
    '腾讯卖call，370P', '腾讯卖370P，行权价380', '腾讯和NVDA卖370P和230c'])
def test_dialogue_can_clarify_with_zero_calls(monkeypatch, tmp_path, message):
    _, fixed = _scope(tmp_path, monkeypatch, ['us', 'hk'])
    request = _contract(fixed)
    request = replace(request, input={**request.input, 'user_message': message})
    captured = []
    def clarify(**kwargs):
        captured.append(kwargs)
        assert kwargs['require_tool'] is False
        guidance = load_general_scene()['option_candidates_guidance']
        assert any(msg.get('content') == guidance for msg in kwargs['messages'])
        return answer('请先澄清方向、标的或合约条件；仅支持卖出开仓。')
    monkeypatch.setattr(tools, 'call_read_tool', lambda *a, **kw: pytest.fail('unexpected read'))
    result = run_contract(request, model_settings=MODEL, session_key=session_key_for_contract(request), model_request=clarify)
    assert result.ok and captured
    assert not any(event.type == 'tool_call' for event in result.events)


@pytest.mark.parametrize('message,symbol,strategy,strike,history', [
    ('腾讯卖370P', '0700.HK', 'short_put', 370, []),
    ('腾讯卖370p', '0700.HK', 'short_put', 370, []),
    ('NVDA short 230C', 'NVDA', 'short_call', 230, []),
    ('NVDA卖230c', 'NVDA', 'short_call', 230, []),
    ('NVDA卖230.5c', 'NVDA', 'short_call', 230.5, []),
    ('370p', '0700.HK', 'short_put', 370, [
        {'role': 'user', 'content': '查询腾讯卖出开仓期权'},
        {'role': 'assistant', 'content': '需要什么行权价？'},
    ]),
])
def test_dialogue_standard_json_reaches_real_schema(monkeypatch, tmp_path, message, symbol, strategy, strike, history):
    _, fixed = _scope(tmp_path, monkeypatch, ['us', 'hk'])
    request = _contract(fixed)
    request = replace(request, input={**request.input, 'user_message': message,
        'messages': history + [{'role': 'user', 'content': message}]})
    payload = {'queries': [{'symbol': symbol, 'strategy': strategy, 'strike': strike}]}
    calls = []
    def execute(name, value):
        calls.append((name, value))
        assert value == payload and 'dte' not in value['queries'][0]
        return {'ok': True, 'data': {'results': []}}
    monkeypatch.setattr(tools, 'execute_tool', execute)
    result = run_contract(request, model_settings=MODEL, session_key=session_key_for_contract(request),
        model_request=script([call('option_candidates', payload), answer('条件查询完成')], []))
    assert result.ok and calls == [('option_candidates', payload)]


def test_scene_exposes_real_tool_schema_and_complete_dialogue_guidance(monkeypatch, tmp_path):
    from src.application.agent_tool_registry import get_tool_definition

    _, fixed = _scope(tmp_path, monkeypatch, ['us', 'hk'])
    manifest = build_scene_manifest(_contract(fixed), 'test')
    tool = next(t for t in manifest.tool_descriptions if t['name'] == 'option_candidates')
    assert set(tool['input_schema']['properties']) == {'queries'}
    assert tool['input_schema']['properties']['queries']['items']['required'] == ['symbol', 'strategy']
    assert get_tool_definition('option_candidates').description in tool['description']


def test_real_executor_returns_unavailable_for_missing_trusted_configs(monkeypatch, tmp_path):
    configs, fixed = _scope(tmp_path, monkeypatch, ['us', 'hk'])
    for path, _ in configs.values():
        path.unlink()
    monkeypatch.setattr(app, 'build_ready_futu_quote_gateway', lambda **kw: pytest.fail('unexpected provider I/O'))
    result = invoke(trusted_input=fixed, deadline_monotonic=time.monotonic() + 10)
    assert result['ok'] is True, result
    assert result['data']['completion'] == 'unavailable'
    assert len(result['data']['results']) == 2
    assert all(row['reason_codes'] == ['configuration_unavailable'] for row in result['data']['results'])
    assert result['host_scope']['markets'] == ['hk', 'us']


def test_cli_execution_does_not_require_bot_trusted_input(monkeypatch, tmp_path):
    from src.application.tool_execution import execute_tool
    monkeypatch.setenv('OM_RUNTIME_ROOT', str(tmp_path))
    monkeypatch.setattr(app, 'build_ready_futu_quote_gateway', lambda **kw: pytest.fail('unexpected provider I/O'))
    result = execute_tool('option_candidates', BATCH)
    assert result['ok'] is True, result
    assert result['data']['completion'] == 'unavailable'
    assert 'host_scope' not in result


@pytest.mark.parametrize('message', ['解释OM策略', '为什么NVDA被过滤', '解释 short put 策略',
    '查看最近报错', 'HK.00700261030P00370000', 'prefix370Psuffix', 'NVDA230c',
    '为什么腾讯370P被过滤？', '解释370P的筛选规则', 'explain call strategy',
    'INPUT', 'callback', 'NVDAcall', 'US.PUT', 'HK.00700261030P00370000'])
def test_host_keeps_first_read_required_for_general_questions_and_identifiers(monkeypatch, tmp_path, message):
    _, fixed = _scope(tmp_path, monkeypatch, ['us', 'hk'])
    request = _contract(fixed)
    request = replace(request, input={**request.input, 'user_message': message})
    captured = []
    monkeypatch.setattr(tools, 'call_read_tool', lambda *a, **kw: {'ok': True, 'data': {}})
    result = run_contract(request, model_settings=MODEL, session_key=session_key_for_contract(request),
        model_request=script([call('project_context', {'config_key': 'us'}), answer('已读取相关证据')], captured))
    assert result.ok
    assert captured[0]['require_tool'] is True
    assert any(event.type == 'tool_call' for event in result.events)


def test_runtime_does_not_infer_clarification_from_available_tool_names():
    from src.application.bot.runtime import run_agent
    description = tools.tool_descriptions(('option_candidates',))
    captured = []
    run_agent(settings=MODEL, api_key='', messages=[{'role': 'user', 'content': '解释策略'}],
        tools=description, call_tool=lambda *args: {'ok': True}, deadline=time.monotonic() + 5,
        cancelled=lambda: False, event=lambda *args: None, reserve_seconds=0,
        request=script([call('option_candidates', BATCH), answer('答案')], captured))
    assert captured[0]['require_tool'] is True


@pytest.mark.parametrize('entry', ['bot', 'cli'])
def test_stale_authoring_source_never_reaches_quotes(monkeypatch, tmp_path, entry):
    from pathlib import Path
    from src.application.bot.config_scope import BotConfigScopeError, resolve_trusted_config_scope
    from src.application.tool_execution import execute_tool

    example = Path(__file__).resolve().parents[1] / 'configs/examples/config.yaml.example'
    source = tmp_path / 'config.yaml'
    source.write_text(example.read_text())
    configs, fixed = _scope(tmp_path, monkeypatch, ['us', 'hk'], source=source)
    payload = {'queries': [{'symbol': 'NVDA', 'strategy': 'short_put'}]}
    query = app.normalize_queries(payload)[0]
    assert app._prepare(query, app.QueryContext(runtime_root=tmp_path), app._now()).endpoint
    source.write_text(source.read_text().replace('min_annualized_net_return: 0.10', 'min_annualized_net_return: 0.20'))
    with pytest.raises(BotConfigScopeError, match='config_stale'):
        resolve_trusted_config_scope(config_key=None, config_path=str(configs['us'][0]))
    monkeypatch.setattr(app, 'build_ready_futu_quote_gateway', lambda **kw: pytest.fail('stale config started quote I/O'))
    response = invoke(payload, trusted_input=fixed) if entry == 'bot' else execute_tool('option_candidates', payload)
    assert response['ok'] is True, response
    result = response['data']['results'][0]
    assert result['outcome'] == 'unable'
    assert result['reason_codes'] == ['configuration_unavailable']
