from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json

import pytest

from domain.domain.ledger import ContractKey, TradeEvent
from src.application.ledger.api import preview_trade_event_repair, record_trade_event_repair, read_trade_attribution_snapshot
from src.application.ledger.repository import SQLiteOptionPositionsRepository
from src.application.ledger.writer import persist_trade_event_objects_atomically
from src.application.trades.attribution import _branch_account_ref
from src.application.trades.review import collect_futu_environment_evidence

SOURCE_MS = int(datetime(2026, 8, 12, 6, 8, 10, 425000, tzinfo=timezone.utc).timestamp()*1000)
EID = "futu:lx:1001:7717"


def scope(tmp_path, *, strike=80):
    repo=SQLiteOptionPositionsRepository(tmp_path/'ledger.sqlite3')
    key=ContractKey.from_values(broker='富途',account='lx',underlying_symbol='3690.HK',
        option_type='put',strike=strike,expiration_ymd='2026-09-29')
    raw={'futu_account_id':'1001','source_deal_id':'7717','deal_id':'7717','order_id':'order1',
        'code':'HK.MET260929P80000','side':'sell','create_time':'2026-08-12 14:08:10.425',
        'unknown_legacy_field':{'keep':True}}
    persist_trade_event_objects_atomically(repo,[TradeEvent(event_id=EID,event_type='open',
        event_time_ms=SOURCE_MS,contract_key=key,contracts=1,price='1.57',currency='HKD',
        multiplier=500,fees=0,source='opend_push',raw_payload=raw)])
    lot=repo.list_position_lots()[0]['record_id']
    persist_trade_event_objects_atomically(repo,[TradeEvent(event_id='assignment',event_type='assignment',
        event_time_ms=SOURCE_MS+1000,contract_key=key,contracts=1,price=80,currency='HKD',
        multiplier=500,fees=0,source='test',target_lot_id=lot,
        raw_payload={'stock_side':'buy','stock_qty':500,'stock_price':80,'stock_currency':'HKD'})])
    config={'market':'hk','accounts':['lx'],'account_settings':{'lx':{'futu':{
        'account_id':'1001','trd_env':'REAL','host':'127.0.0.1','port':11111}}}}
    return repo,config


def proof():
    receipt={'schema_version':'futu_history_query_receipt.v1','dataset':'executions','trd_env':'REAL',
        'observed_at_utc':datetime.now(timezone.utc).isoformat(),'coverage_status':'complete',
        'account_results':[{'futu_account_id':'1001','trd_env':'REAL','coverage_status':'complete',
        'coverage_complete':True,'pagination_complete':True,'truncated':False,'ret':0,
        'requested_start_utc':'2026-08-11T16:00:00Z','requested_end_utc':'2026-08-12T16:00:00Z',
        'covered_start_utc':'2026-08-11T16:00:00Z','covered_end_utc':'2026-08-12T16:00:00Z'}]}
    deal={'code':'HK.MET260929P80000','deal_id':'7717','order_id':'order1','qty':1.0,'price':1.57,
        'trd_side':'SELL_SHORT','create_time':'2026-08-12 14:08:10.425','environment':'REAL',
        'broker_account_id':'futu:REAL:1001','futu_account_id':'1001','trd_acc_id':'1001',
        'external_id_namespace':'futu.deal','external_order_namespace':'futu.order'}
    return {'matches':[deal],'diagnostics':receipt}


def overrides(evidence=None):
    return {'trd_env':'REAL','futu_environment_evidence':evidence or proof()}


def preview(repo, values=None):
    return preview_trade_event_repair(repo,event_id=EID,overrides=values or overrides(),reason='OpenD exact historic deal')


def apply(repo, input_hash, values=None):
    return record_trade_event_repair(repo,event_id=EID,overrides=values or overrides(),
        reason='OpenD exact historic deal',expected_input_hash=input_hash)


def storage(repo):
    with repo._connect() as c:
        return [tuple(row) for row in c.execute('SELECT event_id,event_json,trade_time_ms,created_at_ms,updated_at_ms,account,ingest_seq,market,position_effect FROM trade_events ORDER BY trade_time_ms,event_id')], repo.list_position_lots()


def test_preview_apply_retry_preserves_assignment_and_proves_physical_account(tmp_path):
    repo,_=scope(tmp_path); before=storage(repo)
    rows=read_trade_attribution_snapshot(repo,account='lx',market='hk')
    branch={'source_assignment_event_id':'assignment'}
    assert _branch_account_ref(branch,rows) is None
    plan=preview(repo)
    assert plan['mode']=='dry_run' and storage(repo)==before
    assert plan['expected_before_sha256']==hashlib.sha256(before[0][0][1].encode()).hexdigest()
    result=apply(repo,plan['expected_input_hash'])
    assert result['mode']=='applied'
    after=storage(repo)
    assert after[1]==before[1] and len(after[0])==2
    assert after[0][1]==before[0][1]
    old=json.loads(before[0][0][1]); new=json.loads(after[0][0][1])
    assert new['raw_payload'].pop('trd_env')=='REAL'
    provenance=new['raw_payload'].pop('futu_environment_provenance')
    assert provenance['input_hash']==plan['expected_input_hash']
    assert new==old
    assert _branch_account_ref(branch,read_trade_attribution_snapshot(repo,account='lx',market='hk'))=={
        'broker_id':'futu','external_account_id':'1001','environment':'REAL'}
    assert apply(repo,plan['expected_input_hash'])['mode']=='no_op'
    assert storage(repo)==after


@pytest.mark.parametrize('change,error',[
    ('account','account or coverage'),('partial','account or coverage'),('pages','account or coverage'),
    ('window','does not cover'),('stale','stale'),('future','future'),('missing','unique matching'),
    ('duplicate','unique matching'),('deal','identity mismatch'),('order','identity mismatch'),
    ('contract','identity mismatch'),('physical','identity mismatch'),('environment','identity mismatch'),
    ('price','economics mismatch'),('quantity','economics mismatch'),('time','economics mismatch'),
    ('side','economics mismatch'),('mixed','only accepts'),
    ('physical_alias','identity mismatch'),('environment_alias','identity mismatch'),
    ('cancelled','identity mismatch'),('namespace','identity mismatch'),
])
def test_unproven_or_conflicting_source_never_mutates(tmp_path,change,error):
    repo,_=scope(tmp_path); before=storage(repo); p=proof();d=p['matches'][0];a=p['diagnostics']['account_results'][0]
    if change=='account':a['futu_account_id']='2002'
    elif change=='partial':a['coverage_complete']=False
    elif change=='pages':a['pagination_complete']=False
    elif change=='window':a['covered_end_utc']='2026-08-11T16:00:00Z'
    elif change=='stale':p['diagnostics']['observed_at_utc']='2026-01-01T00:00:00Z'
    elif change=='future':p['diagnostics']['observed_at_utc']='2030-01-01T00:00:00Z'
    elif change=='missing':p['matches']=[]
    elif change=='duplicate':p['matches'].append(deepcopy(d))
    elif change=='deal':d['deal_id']='other'
    elif change=='order':d['order_id']='other'
    elif change=='contract':d['code']='HK.MET260929P75000'
    elif change=='physical':d['futu_account_id']='2002'
    elif change=='environment':d['environment']='SIMULATE'
    elif change=='price':d['price']=1.58
    elif change=='quantity':d['qty']=2
    elif change=='time':d['create_time']='2026-08-12 14:08:11.425'
    elif change=='side':d['trd_side']='BUY'
    elif change=='physical_alias':d['trd_acc_id']='2002'
    elif change=='environment_alias':d['trd_env']='SIMULATE'
    elif change=='cancelled':d['status']='CANCELLED'
    elif change=='namespace':d['external_id_namespace']='futu.order'
    values=overrides(p)
    if change=='mixed':values['price']=2
    with pytest.raises(ValueError,match=error):preview(repo,values)
    assert storage(repo)==before


def test_preview_hash_required_and_changed_source_rejected(tmp_path):
    repo,_=scope(tmp_path); plan=preview(repo); before=storage(repo)
    with pytest.raises(ValueError,match='requires --expected-input-hash'):apply(repo,None)
    with pytest.raises(ValueError,match='input hash changed'):apply(repo,'0'*64)
    with repo._connect() as c:
        c.execute("UPDATE trade_events SET event_json=json_set(event_json,'$.raw_payload.extra','changed') WHERE event_id=?",(EID,))
    changed=storage(repo)
    with pytest.raises(ValueError,match='input hash changed'):apply(repo,plan['expected_input_hash'])
    assert storage(repo)==changed and changed!=before


def test_cas_conflict_rolls_back(tmp_path,monkeypatch):
    repo,_=scope(tmp_path); plan=preview(repo); before=storage(repo)
    monkeypatch.setattr(repo,'compare_and_swap_trade_event_order_identity_json',lambda **kw:False)
    with pytest.raises(ValueError,match='CAS conflict'):apply(repo,plan['expected_input_hash'])
    assert storage(repo)==before


def test_collector_scopes_account_day_and_closes_on_failure(tmp_path,monkeypatch):
    repo,config=scope(tmp_path); calls=[]
    class Client:
        def __init__(self,**kw):calls.append(kw)
        def fetch(self,**kw):
            calls.append(kw);p=proof();return p['matches'],p['diagnostics']
        def close(self):calls.append('closed')
    monkeypatch.setattr('src.infrastructure.futu_history_deals.OpenDHistoryDealClient',Client)
    result=collect_futu_environment_evidence(repo,event_id=EID,config=config,market='hk')
    assert result['matches'][0]['deal_id']=='7717'
    assert calls[1]['futu_account_ids']==['1001'] and calls[1]['lookback_hours']==24
    assert calls[1]['now'].isoformat()=='2026-08-13T00:00:00+08:00' and calls[-1]=='closed'
    with pytest.raises(ValueError,match='mismatch'):
        collect_futu_environment_evidence(repo,event_id=EID,config=config,market='us')


def test_cli_preview_apply_and_retry(tmp_path,monkeypatch,capsys):
    import src.interfaces.cli.trade_events as cli
    repo,config=scope(tmp_path)
    monkeypatch.setattr(cli,'resolve_option_positions_repo',lambda **kw:(tmp_path/'data.json',repo))
    monkeypatch.setattr(cli,'_guard_write',lambda **kw:{'ok':True})
    monkeypatch.setattr(cli,'load_runtime_config',lambda payload:(tmp_path/'hk.json',config))
    monkeypatch.setattr(cli,'collect_futu_environment_evidence',lambda *a,**kw:proof())
    args=['repair',EID,'--trd-env','REAL','--config-key','hk','--reason','OpenD exact historic deal','--format','json']
    before=storage(repo)
    assert cli.main(args)==0
    plan=json.loads(capsys.readouterr().out)
    assert not plan['write_applied'] and storage(repo)==before
    assert cli.main([*args,'--confirm','--expected-input-hash',plan['expected_input_hash']])==0
    result=json.loads(capsys.readouterr().out)
    assert result['write_applied'] and result['operation']=='futu_environment_binding'
    after=storage(repo)
    assert cli.main([*args,'--confirm','--expected-input-hash',plan['expected_input_hash']])==0
    assert not json.loads(capsys.readouterr().out)['write_applied'] and storage(repo)==after


@pytest.mark.parametrize('kind',['stored_environment','stored_execution','provenance','void','projection','retry_change'])
def test_existing_identity_and_failed_publication_remain_safe(tmp_path,monkeypatch,kind):
    repo,_=scope(tmp_path)
    if kind=='projection':
        plan=preview(repo);before=storage(repo)
        def failed(*a,**kw):raise ValueError('projection unavailable')
        monkeypatch.setattr('src.application.ledger.interventions.run_position_projection_in_transaction',failed)
        with pytest.raises(ValueError,match='projection unavailable'):apply(repo,plan['expected_input_hash'])
        assert storage(repo)==before
        return
    if kind=='retry_change':
        plan=preview(repo);apply(repo,plan['expected_input_hash'])
    if kind=='void':
        key=ContractKey.from_values(broker='富途',account='lx',underlying_symbol='3690.HK',
            option_type='put',strike=80,expiration_ymd='2026-09-29')
        repo.upsert_trade_event(TradeEvent(event_id='void',event_type='void',event_time_ms=SOURCE_MS+2000,
            contract_key=key,contracts=0,price=0,currency='HKD',multiplier=500,fees=0,
            source='test',target_event_id=EID))
    with repo._connect() as c:
        payload=json.loads(c.execute('SELECT event_json FROM trade_events WHERE event_id=?',(EID,)).fetchone()[0])
        raw=payload['raw_payload']
        if kind=='stored_environment':raw['trd_env']='SIMULATE'
        elif kind=='stored_execution':raw['execution_input']={}
        elif kind=='provenance':raw['futu_environment_provenance']={}
        elif kind=='retry_change':raw['extra']='different'
        c.execute('UPDATE trade_events SET event_json=? WHERE event_id=?',(json.dumps(payload),EID))
    before=storage(repo)
    with pytest.raises(ValueError):preview(repo)
    assert storage(repo)==before


def test_five_branch_repair_unblocks_shared_writer_once(tmp_path,monkeypatch):
    from test_trade_attribution_full_branches import _occupied_scope,_view,_observation,NOW,EVIDENCE
    from src.application.trades import attribution
    from src.application.ledger.api import read_trade_attribution_facts
    from src.application.ledger.position_projection_runtime import run_position_projection_forced_full
    from dataclasses import replace
    import test_trade_attribution_meituan as fixture_module
    original_persist=fixture_module.persist_trade_event_objects_atomically
    source='futu:lx:1001:put-4'
    def persist_legacy_last(repo,events,**kwargs):
        converted=[]
        for event in events:
            if event.event_id=='put-4':
                raw=deepcopy(event.raw_payload);raw.pop('execution_input');raw.pop('execution_id')
                raw.update(futu_account_id='1001',source_deal_id='put-4',order_id='order4',code='HK.MET260925P75000',side='sell')
                event=replace(event,event_id=source,source='opend_push',raw_payload=raw)
            converted.append(event)
        return original_persist(repo,converted,**kwargs)
    monkeypatch.setattr(fixture_module,'persist_trade_event_objects_atomically',persist_legacy_last)
    repo,config,branches,execution,_=_occupied_scope(tmp_path,monkeypatch)
    run_position_projection_forced_full(repo)
    observation=_observation(repo)
    before_call=next(r for r in _view(repo,config,observation=observation)['rows'] if r['execution_key']==execution)
    assert before_call['candidate_ids']==['wheel:'+branches[-1]]
    assert before_call['reason_codes']==['wheel_physical_account_unproven']
    p=proof();d=p['matches'][0];d.update(deal_id='put-4',order_id='order4',code='HK.MET260925P75000',price=2,
                                       create_time='2026-09-01 09:00:04')
    p['diagnostics']['observed_at_utc']=datetime.fromtimestamp(NOW/1000,timezone.utc).isoformat()
    a=p['diagnostics']['account_results'][0]
    for prefix in ('requested','covered'):
        a[prefix+'_start_utc']='2026-08-31T16:00:00Z';a[prefix+'_end_utc']='2026-09-01T16:00:00Z'
    monkeypatch.setattr('src.application.ledger.interventions.now_ms',lambda:NOW)
    values=overrides(p)
    plan=preview_trade_event_repair(repo,event_id=source,overrides=values,reason='OpenD exact historic deal')
    original_calls=[e for e in repo.list_trade_events() if e.get('option_type')=='call' and e['event_type']=='open']
    before_count=len(repo.list_trade_events())
    repair=record_trade_event_repair(repo,event_id=source,overrides=values,reason='OpenD exact historic deal',
                                   expected_input_hash=plan['expected_input_hash'])
    assert repair['mode']=='applied' and len(repo.list_trade_events())==before_count
    after_call=next(r for r in _view(repo,config,observation=observation)['rows'] if r['execution_key']==execution)
    assert after_call['selected_candidate_id']=='wheel:'+branches[-1]
    monkeypatch.setattr(attribution,'read_attribution_combo_evidence',lambda *a,**kw:deepcopy(EVIDENCE))
    monkeypatch.setattr('src.application.wheel.capacity.observe_trade_attribution_capacity',lambda **kw:observation)
    kwargs=dict(config=config,account='lx',market='hk',runtime_root=tmp_path,inbox_path=tmp_path/'inbox.sqlite3',combo_mode='confirm')
    result=attribution.reconcile_trade_attribution_account(repo,**kwargs)
    assert result['linked']==1 and not result['errors']
    linked=next(r for r in read_trade_attribution_facts(repo,account='lx') if r['execution_key']==execution)
    assert linked['status']=='linked' and linked['wheel_branch_id']==branches[-1]
    assert len(repo.list_trade_events())==before_count+1
    assert [e for e in repo.list_trade_events() if e.get('option_type')=='call' and e['event_type']=='open']==original_calls
    assert attribution.reconcile_trade_attribution_account(repo,**kwargs)['linked']==0
    assert len(repo.list_trade_events())==before_count+1


def test_provider_failure_and_cancellation_close_without_ledger_effect(tmp_path,monkeypatch):
    repo,config=scope(tmp_path);before=storage(repo);closed=[]
    failure=RuntimeError('provider unavailable')
    class Client:
        def __init__(self,**kw):pass
        def fetch(self,**kw):raise failure
        def close(self):closed.append(True)
    monkeypatch.setattr('src.infrastructure.futu_history_deals.OpenDHistoryDealClient',Client)
    with pytest.raises(RuntimeError,match='provider unavailable'):
        collect_futu_environment_evidence(repo,event_id=EID,config=config,market='hk')
    assert closed==[True] and storage(repo)==before
    import src.interfaces.cli.trade_events as cli
    monkeypatch.setattr(cli,'resolve_option_positions_repo',lambda **kw:(tmp_path/'data.json',repo))
    monkeypatch.setattr(cli,'load_runtime_config',lambda payload:(tmp_path/'hk.json',config))
    assert cli.main(['repair',EID,'--trd-env','REAL','--config-key','hk','--reason','OpenD exact historic deal'])==2
    assert closed==[True,True] and storage(repo)==before
    failure=KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        collect_futu_environment_evidence(repo,event_id=EID,config=config,market='hk')
    assert closed==[True,True,True] and storage(repo)==before


def test_legacy_decimal_strike_representation_preserves_original_event(tmp_path):
    repo,_=scope(tmp_path,strike="80.0");before=storage(repo)
    plan=preview(repo)
    assert storage(repo)==before
    assert apply(repo,plan['expected_input_hash'])['mode']=='applied'
    after=storage(repo);new=json.loads(after[0][0][1])
    assert new['raw_payload'].pop('trd_env')=='REAL'
    new['raw_payload'].pop('futu_environment_provenance')
    assert new==json.loads(before[0][0][1]) and after[1]==before[1]
    assert apply(repo,plan['expected_input_hash'])['mode']=='no_op' and storage(repo)==after
