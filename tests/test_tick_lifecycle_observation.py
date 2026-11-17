from io import StringIO
import json
from pathlib import Path
import subprocess

import pytest

from src.application import tick_cron as cron
from src.application import tick_brief_preparation as brief


@pytest.mark.parametrize('failure', [None, 'preflight', 'timeout', 'start', 'child', 'cancel'])
def test_outer_observation_closes_all_owned_paths_without_changing_result(tmp_path, monkeypatch, failure):
    clock = [100.0]
    monkeypatch.setattr(cron.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(cron, 'create_run_id', lambda: 'wrapper')
    failures = []
    monkeypatch.setattr(cron, '_record_failure', lambda **kw: failures.append(kw['code']))
    error = KeyboardInterrupt()

    def preflight(**kw):
        clock[0] += 2
        if failure == 'preflight':
            raise ValueError('bad config')

    def child(command, **kw):
        assert kw['timeout'] == 8
        assert kw['env']['OM_TICK_DEADLINE_MONOTONIC'] == '110.0'
        clock[0] += 5
        if failure == 'timeout':
            clock[0] += 5
            raise subprocess.TimeoutExpired(command, 10)
        if failure == 'start':
            raise OSError('start')
        if failure == 'cancel':
            raise error
        return subprocess.CompletedProcess(command, 2 if failure == 'child' else 0)

    kwargs = dict(market='us', accounts=['lx'], lock_path=str(tmp_path/'lock'),
                  runtime_root=tmp_path, environ={}, timeout_seconds=10,
                  preflight_config_fn=preflight, run_cmd=child, stdout=StringIO(), stderr=StringIO())
    if failure == 'cancel':
        with pytest.raises(KeyboardInterrupt) as caught:
            cron.run_tick_cron(**kwargs)
        assert caught.value is error
    else:
        assert cron.run_tick_cron(**kwargs) == {None: 0, 'preflight': 1, 'timeout': 124, 'start': 1, 'child': 2}[failure]
    data = json.loads((tmp_path/'output_runs/wrapper/state/tick_process_timings.json').read_text())
    assert data['wrapper_run_id'] == 'wrapper'
    assert data['market'] == 'us' and data['accounts'] == ['lx']
    assert data['preflight_seconds'] == 2
    assert data['deadline_budget_seconds'] == 10
    assert data['observation_complete'] is True
    assert not any(k.startswith('_') for k in data)
    if failure == 'preflight':
        assert data['outer_elapsed_seconds'] == 2
        assert data['terminal_status'] == 'preflight_failed'
        assert 'child_elapsed_seconds' not in data
    else:
        assert data['outer_elapsed_seconds'] == (12 if failure == 'timeout' else 7)
        assert data['child_elapsed_seconds'] == (10 if failure == 'timeout' else 5)
        assert data['deadline_remaining_seconds'] == (-2 if failure == 'timeout' else 3)
    if failure == 'cancel':
        assert data['terminal_status'] == 'cancelled'
        assert 'return_code' not in data


@pytest.mark.parametrize('broken', ['clock', 'write'])
def test_observation_failure_preserves_business_result(tmp_path, monkeypatch, broken):
    monkeypatch.setattr(cron, 'create_run_id', lambda: 'wrapper')
    if broken == 'clock':
        monkeypatch.setattr(cron, '_observation_clock', lambda: None)
    else:
        monkeypatch.setattr(cron, 'atomic_write_json', lambda *a, **kw: (_ for _ in ()).throw(ValueError('diagnostic write')))
    assert cron.run_tick_cron(market='us', accounts=['lx'], runtime_root=tmp_path,
                             environ={}, lock_path=str(tmp_path/'lock'), preflight_config_fn=None,
                             run_cmd=lambda *a, **kw: subprocess.CompletedProcess(a, 0)) == 0
    if broken == 'clock':
        d=json.loads((tmp_path/'output_runs/wrapper/state/tick_process_timings.json').read_text())
        assert d['outer_elapsed_seconds'] is None
        assert d['deadline_remaining_seconds'] is None


@pytest.mark.parametrize('reliable', [True, False])
@pytest.mark.parametrize('fault', [None, 'persist', 'logger', 'cancel_after_persist'])
def test_scan_available_follows_durable_qualification_not_delivery(tmp_path, monkeypatch, reliable, fault):
    order=[]
    events=[]
    monkeypatch.setattr(brief, 'assemble_daily_decision_briefs', lambda **kw: {'US': {'source': 'test'}})
    monkeypatch.setattr(brief, 'daily_brief_scan_is_reliable', lambda **kw: reliable)
    def persist(**kw):
        if fault == 'persist':
            raise OSError('cannot persist')
        order.append('persist')
        return kw['brief']
    monkeypatch.setattr(brief, 'persist_daily_decision_brief_success', persist)
    monkeypatch.setattr(brief, 'persist_daily_decision_brief_failure', persist)
    monkeypatch.setattr(brief, 'write_daily_brief_failure_source', lambda **kw: order.append('failure_source'))
    def check(*args):
        if fault == 'cancel_after_persist' and 'callback' in order:
            raise TimeoutError('cancelled')
    monkeypatch.setattr(brief, 'check_fetch_control', check)
    class Log:
        def safe_event(self, step, status, **kw):
            if step != 'scan_result_available':
                return
            if fault == 'logger':
                raise RuntimeError('logger failed')
            events.append((step, status, kw['data'], list(order)))
    def callback(*args):
        order.append('callback')
    kwargs=dict(base=tmp_path,run_id='inner',config={},markets=['US'],results=[{'account':'lx','ran_scan':True}],
                ran_pipeline_accounts=['lx'],scheduled_scan_targets_by_account={'lx':'target'},
                scheduler_decisions_by_account=None,scheduler_decision=None,runlog=Log(),on_account_prepared=callback)
    if fault in {'persist','cancel_after_persist'}:
        with pytest.raises(OSError if fault == 'persist' else TimeoutError):
            brief.prepare_tick_briefs(**kwargs)
    else:
        result=brief.prepare_tick_briefs(**kwargs)
        assert result.scan_outcomes_by_account == {'lx': reliable}
    if fault != 'persist':
        assert 'callback' in order
    if fault in {'persist','logger'}:
        assert events == []
    else:
        assert len(events)==1
        _,status,data,observed_order=events[0]
        assert status == ('ok' if reliable else 'error')
        assert data == {'account':'lx','market':'US','available':reliable}
        assert observed_order == (['persist'] if reliable else ['persist','failure_source'])


def test_outer_timings_include_postprocess_and_keep_partial_checkpoint(tmp_path, monkeypatch):
    clock=[100.0]
    snapshots=[]
    monkeypatch.setattr(cron.time,'monotonic',lambda:clock[0])
    monkeypatch.setattr(cron,'create_run_id',lambda:'wrapper')
    def process(**kw):
        kw['timings'].update(business_wait_seconds=3.0,diagnostic_seconds=1.0,cleanup_seconds=1.0)
        clock[0]+=5
        return subprocess.CompletedProcess([],1)
    def failure(**kw):
        clock[0]+=4
    monkeypatch.setattr(cron,'_run_tick_process_group',process)
    monkeypatch.setattr(cron,'_record_failure',failure)
    monkeypatch.setattr(cron,'_completed_receipt',lambda *a,**kw:None)
    monkeypatch.setattr(cron,'atomic_write_json',lambda path,data:snapshots.append(dict(data)))
    assert cron.run_tick_cron(market='us',accounts=['lx'],timeout_seconds=10,runtime_root=tmp_path,
                             lock_path=str(tmp_path/'lock'),environ={},preflight_config_fn=None)==1
    partial,final=snapshots
    assert 'observation_complete' not in partial
    assert final['observation_complete'] is True
    assert final['outer_elapsed_seconds']==9
    assert final['postprocess_seconds']==4
    assert final['child_elapsed_seconds']==5
    assert final['deadline_remaining_seconds']==1
    assert final['business_wait_seconds']==3
    assert final['diagnostic_seconds']==1 and final['cleanup_seconds']==1


@pytest.mark.parametrize('path',['dryrun','locked'])
def test_no_wrapper_run_or_timing_file_for_nonexecuted_path(tmp_path,monkeypatch,path):
    def forbidden(*a,**kw):
        pytest.fail('must not allocate run or write timings')
    monkeypatch.setattr(cron,'create_run_id',forbidden)
    monkeypatch.setattr(cron,'atomic_write_json',forbidden)
    if path=='locked':
        def locked(*a):
            raise BlockingIOError()
        monkeypatch.setattr(cron.fcntl,'flock',locked)
    result=cron.run_tick_cron(market='us',accounts=['lx'],runtime_root=tmp_path,environ={},
                             lock_path=str(tmp_path/'lock'),dry_run_command=path=='dryrun',stdout=StringIO())
    assert isinstance(result,dict) if path=='dryrun' else result==0
    assert not (tmp_path/'output_runs').exists()


def test_failure_record_exception_is_not_reported_as_returned_rc(tmp_path, monkeypatch):
    error=ValueError('record failed')
    monkeypatch.setattr(cron,'create_run_id',lambda:'wrapper')
    def record(**kw):
        raise error
    monkeypatch.setattr(cron,'_record_failure',record)
    def child(*args,**kwargs):
        raise subprocess.TimeoutExpired([],10)
    with pytest.raises(ValueError) as caught:
        cron.run_tick_cron(market='us',accounts=['lx'],runtime_root=tmp_path,environ={},
                           lock_path=str(tmp_path/'lock'),preflight_config_fn=None,run_cmd=child)
    assert caught.value is error
    data=json.loads((tmp_path/'output_runs/wrapper/state/tick_process_timings.json').read_text())
    assert data['terminal_status']=='error'
    assert 'return_code' not in data
