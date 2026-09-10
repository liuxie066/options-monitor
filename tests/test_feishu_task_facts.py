"""Real Feishu -> Bot -> Pi -> tool -> admission -> renderer -> durable outbox."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
from functools import partial
from pathlib import Path

import pytest

from src.application import service_deploy
from src.application.agent_tools import scheduled_tasks_impl
from src.application.inbound.feishu_ws import FeishuWsSettings, handle_feishu_ws_event
from tests.test_bot_task_report import answer, task_claim
from tests.test_inbound_feishu_ws import _message_payload
from tests.test_pi_agent_process import _chat_response, _loopback_server


@pytest.mark.parametrize('scenario,max_chars', [('complete', 8000), ('mixed', 8000), ('unavailable', 8000),
    ('mixed', 220), ('complete', 8), ('failed_only', 8000), ('duplicate', 8000), ('recovery', 8000), ('narrowed', 8000),
    ('progress_close', 8000), ('progress_conflict', 8000)])
def test_feishu_task_facts_real_runtime(monkeypatch, tmp_path, example_config_path, scenario, max_chars):
    repo = Path(__file__).resolve().parents[1]
    bundle = service_deploy.render_service_bundle(target='systemd', repo_root=repo, runtime_root=tmp_path,
        accounts=['lx'], markets=['us'], config_paths={'us': example_config_path})
    profile = json.loads(next(f for f in bundle['files'] if f['relative_path'] == 'service.profile.json')['content'])
    if scenario == 'unavailable':
        profile['service_provider'] = 'manual'
    (tmp_path / 'service.profile.json').write_text(json.dumps(profile), encoding='utf-8')
    probes = []
    progress = {}
    concurrent_update = False

    def run_os(command, **kwargs):
        nonlocal concurrent_update
        probes.append(command)
        if scenario == 'progress_conflict' and progress and not concurrent_update:
            with sqlite3.connect(tmp_path / 'audit.sqlite3') as conn:
                conn.execute("UPDATE bot_runs SET progress_json=json_set(progress_json,'$.revision',2) WHERE run_id=?", (progress['progress_ref'],))
            concurrent_update = True
        assert 0 < kwargs['timeout'] <= 1
        return subprocess.CompletedProcess(command, 0, 'enabled\n' if 'is-enabled' in command else 'active\n', '')

    monkeypatch.setattr(scheduled_tasks_impl, 'scheduled_tasks_from_profile',
        partial(service_deploy.scheduled_tasks_from_profile, run_cmd=run_os))
    monkeypatch.setenv('OM_RUNTIME_ROOT', str(tmp_path))
    monkeypatch.setenv('OM_PI_SESSION_DB', str(tmp_path / 'pi.sqlite3'))
    seen = []
    if scenario == 'narrowed':
        monkeypatch.setattr('src.application.bot.tools.MAX_OBSERVATION_TOKENS', 450)

    def submit(payload):
        observations = [json.loads(row['content']) for row in payload['messages']
            if row.get('role') == 'tool' and isinstance(row.get('content'), str) and row['content'].startswith('{')]
        seen.extend(observations)
        successful = [obs for obs in observations if obs.get('tool_name') == 'scheduled_tasks_read'
            and obs.get('ok') is True and obs.get('value', {}).get('availability') != 'unavailable'
            and obs.get('status') != 'needs_narrowing']
        claims = []
        if successful:
            claim = task_claim()
            claim['observation_ids'] = [obs['ref'] for obs in successful]
            claims = [claim]
        args = answer(claims, '\n\n启用表示允许定时触发，活动状态不证明业务已成功。')
        if progress:
            args['progress_resolution'] = progress
        return _chat_response(tool_name='submit_answer', tool_arguments=args, finish_reason='tool_calls', call_id='answer')

    first_market = 'hk' if scenario in {'failed_only', 'duplicate', 'recovery'} else 'us'
    responses = [{'body': _chat_response(tool_name='scheduled_tasks_read', tool_arguments={'config_key': first_market},
        finish_reason='tool_calls', call_id='us')}]
    if scenario in {'mixed', 'duplicate'}:
        responses.append({'body': _chat_response(tool_name='scheduled_tasks_read', tool_arguments={'config_key': 'hk'},
            finish_reason='tool_calls', call_id='hk')})
    responses.append({'body': submit})
    if scenario.startswith('progress_'):
        responses[-1] = {'status': 400, 'content_type': 'application/json', 'body': '{"error":{"message":"fixture interrupted"}}'}
    if scenario == 'recovery' or scenario.startswith('progress_'):
        responses.extend([{'body': _chat_response(tool_name='scheduled_tasks_read', tool_arguments={'config_key': 'us'},
            finish_reason='tool_calls', call_id='recover')}, {'body': submit}])
    replies = []
    queued = []

    def reply(**kwargs):
        replies.append(kwargs)
        with sqlite3.connect(tmp_path / 'audit.sqlite3') as conn:
            queued.extend(json.loads(row[0]) for row in conn.execute('SELECT payload_json FROM bot_reply_outbox'))
        return {'code': 0, 'data': {'message_id': 'reply'}}
    with _loopback_server(responses) as (url, requests):
        assistant = tmp_path / 'config.assistant.json'
        assistant.write_text(json.dumps({'assistant': {'enabled': True, 'bot': {'enabled': True},
            'llm': {'provider': 'ollama', 'model': 'om-test', 'base_url': url + '/v1',
                'context_window_tokens': 128000, 'max_attempts': 1}}}), encoding='utf-8')
        settings = FeishuWsSettings(config_key=None, config_path=str(example_config_path),
                assistant_config_path=str(assistant), allowed_senders='feishu:ou_1',
                app_id='test', app_secret='test', audit_db=str(tmp_path / 'audit.sqlite3'), max_reply_chars=max_chars)
        out = handle_feishu_ws_event(_message_payload(text='列出定时任务，并解释启用和活动状态'), settings=settings,
            reply_fn=reply,
            reaction_fn=lambda **kwargs: {'code': 0},
            execute_tool_fn=lambda *args, **kwargs: pytest.fail('unexpected Control execution'))
        if scenario == 'recovery' or scenario.startswith('progress_'):
            if scenario == 'recovery':
                assert not probes
                next_text = '现在重新查询当前授权范围的任务'
            else:
                with sqlite3.connect(tmp_path / 'audit.sqlite3') as conn:
                    original_id, raw_progress = conn.execute('SELECT run_id,progress_json FROM bot_runs').fetchone()
                original_progress = json.loads(raw_progress)
                progress.update(progress_ref=original_id, expected_revision=original_progress['revision'],
                    covered_goal=original_progress['goal'], claim_indexes=[0])
                next_text = '继续 ' + original_id
            again = _message_payload(text=next_text, message_id='msg_2')
            again['header']['event_id'] = 'evt_2'
            out = handle_feishu_ws_event(again, settings=settings, reply_fn=reply,
                reaction_fn=lambda **kwargs: {'code': 0},
                execute_tool_fn=lambda *args, **kwargs: pytest.fail('unexpected Control execution'))
    assert out['ok'], out
    assert len(requests) == len(responses), out
    assert replies, out
    with sqlite3.connect(tmp_path / 'audit.sqlite3') as conn:
        stored = conn.execute('SELECT payload_json FROM bot_reply_outbox').fetchall()
        runs = conn.execute('SELECT response_json,events_json FROM bot_runs').fetchall()
    assert len(runs) == (2 if scenario == 'recovery' or scenario.startswith('progress_') else 1)
    assert stored
    envelope = queued[-1]  # Delivery removes the durable capability payload after acknowledgement.
    result, events = map(json.loads, runs[-1])
    assert result['status'] == 'answered', result
    assert replies[-1]['content'] == envelope['transport']['content']
    text = envelope['transport']['content']['body']['elements'][0]['content']
    if max_chars == 8:
        assert '已核实' not in text
    else:
        assert '任务查询范围' in text
        if scenario == 'narrowed':
            assert '缩小查询范围' in text and '已核实' not in text
            assert all(not obs.get('value', {}).get('tasks') for obs in seen)
        elif scenario == 'unavailable':
            assert '当前无法核实' in text and '已核实 0' not in text
            assert not probes
        elif scenario in {'mixed', 'failed_only', 'duplicate'}:
            assert 'HK 当前无法核实：超出当前授权范围' in text
            assert 'HK 未配置' not in text
            assert all('hk' not in str(command) for command in probes)
            if scenario != 'mixed':
                assert not probes
                assert '已核实 0' not in text
                assert len([e for e in events if e['type'] == 'tool_result' and e['payload'].get('tool_name') == 'scheduled_tasks_read']) == 1
        else:
            tasks = next(obs['value']['tasks'] for obs in seen if obs.get('value', {}).get('tasks'))
            assert f'已核实 {len(tasks)} 项任务' in text
            assert all(row['name'].replace('.', '\\.') in result['user_response'] for row in tasks)
        assert '启用表示允许定时触发' in result['user_response']
    source = result['user_response'].replace('\r\n', '\n').replace('\r', '\n').strip()
    assert envelope['render_meta']['source_sha256'] == hashlib.sha256(source.encode()).hexdigest()
    canonical = json.dumps(envelope['transport'], ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    assert envelope['render_meta']['rendered_sha256'] == hashlib.sha256(canonical.encode()).hexdigest()
    receipt = next(event['payload'] for event in events if event['type'] == 'tool_result' and event['payload'].get('tool_name') == 'submit_answer')
    approved_text = result['user_response']
    if scenario.startswith('progress_'):
        from src.application.bot.host_store import PROGRESS_RESOLUTION_CONFLICT_NOTICE
        with sqlite3.connect(tmp_path / 'audit.sqlite3') as conn:
            current_progress = json.loads(conn.execute('SELECT progress_json FROM bot_runs WHERE run_id=?', (progress['progress_ref'],)).fetchone()[0])
        if scenario == 'progress_conflict':
            assert approved_text.endswith(PROGRESS_RESOLUTION_CONFLICT_NOTICE)
            approved_text = approved_text.removesuffix(PROGRESS_RESOLUTION_CONFLICT_NOTICE)
            assert current_progress['resolved_by'] is None
        else:
            assert current_progress['resolved_by']
    assert receipt['approved_answer_hash'] == 'sha256:' + hashlib.sha256(approved_text.encode()).hexdigest()
    assert '[[' not in envelope['fallback']['content']['text']
