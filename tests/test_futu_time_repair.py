from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
import hashlib
import json
import sqlite3

import pytest

from domain.domain.performance.models import EvidenceEnvelope, FXRateFact
from domain.domain.trade_execution import _futu_execution_input, execution_identity_from_input
from src.application.ledger.api import refresh_position_lot_projection
from src.application.ledger.repository import SQLiteOptionPositionsRepository
from src.application.trades import inbox
from src.application.trades.review import preview_futu_time_repair
from src.infrastructure.performance_evidence_sqlite import PerformanceEvidenceSQLiteRepository
from tests.test_futu_trade_time_preview import _event, OLD_MS, NEW_MS


def fixture(tmp_path, *, standard=False, with_fx=True):
    repo = SQLiteOptionPositionsRepository(tmp_path / 'ledger.sqlite3')
    raw = {**_event().raw_payload, 'internal_account': 'lx', 'trd_env': 'REAL', 'environment': 'REAL',
           'broker_account_id': 'futu:REAL:123', 'external_id_namespace': 'futu.deal',
           'external_order_namespace': 'futu.order', 'multiplier': 100, 'price': 2,
           'fee_provenance': {'basis': 'actual', 'amount': '0', 'source': 'opend.order_fee_query'}}
    if standard:
        ex = _futu_execution_input(raw)
        ex['occurred_at_utc'] = '2026-09-08T02:43:37.674Z'
        ex['source_timezone'] = 'Asia/Shanghai'
        raw['execution_input'] = ex
        raw['execution_id'] = execution_identity_from_input(ex)
    repo.upsert_trade_event(replace(_event(), raw_payload=raw))
    refresh_position_lot_projection(repo)
    fx = FXRateFact(None, 'USD', 'CNY', Decimal('6.8'), 'central_parity', OLD_MS, NEW_MS,
                    'pbc_central_parity', 'test-day')
    PerformanceEvidenceSQLiteRepository(repo.db_path).import_envelope(
        EvidenceEnvelope(fx_rates=(fx,) if with_fx else ()), apply=True, migrated_at_ms=NEW_MS)
    path = repo.db_path.with_name(repo.db_path.name + '.trade_intake_inbox.sqlite3')
    source = {k: v for k, v in raw.items() if k not in {'execution_input', 'execution_id', 'cash_conversions', 'fee_provenance'}}
    key = execution_identity_from_input(_futu_execution_input(source))
    iid = inbox.enqueue_trade_payload(path, payload=source, source='file', broker_deal_key=key, repo=repo)
    with inbox._connect(path) as conn:
        conn.execute("UPDATE trade_inbox SET status='handled', economic_payload_hash='old-parser-hash' WHERE inbox_id=?", (iid,))
    with sqlite3.connect(repo.db_path) as conn:
        eid, text = conn.execute('SELECT event_id,event_json FROM trade_events').fetchone()
    request = {'schema_version': 'futu_trade_time_repair_request.v1', 'batch_id': 'test-time-batch',
               'reason': 'verified source timezone', 'prepared_at_ms': NEW_MS + 1000,
               'targets': [{'event_id': eid, 'before_sha256': hashlib.sha256(text.encode()).hexdigest(), 'after_trade_time_ms': NEW_MS}]}
    return repo, path, request


def dumps(repo, inbox_path):
    result = []
    for path in (repo.db_path, inbox_path):
        with inbox._connect(path) as conn:
            result.append(list(conn.iterdump()))
    return result


@pytest.mark.parametrize('standard', [False, True])
def test_preview_binds_both_stores_without_mutation(tmp_path, standard):
    repo, path, request = fixture(tmp_path, standard=standard)
    before = dumps(repo, path)
    result = preview_futu_time_repair(repo, request=request)
    assert result == preview_futu_time_repair(repo, request=request)
    assert dumps(repo, path) == before
    assert len(result['events']) == 1 and len(result['inbox']) == 1
    change = result['events'][0]
    assert change['after_trade_time_ms'] == NEW_MS
    raw = change['after_payload']['raw_payload']
    assert raw['create_time'] == '2026-09-08 10:43:37.674'
    assert all(c['status'] == 'observed' for c in raw['cash_conversions'].values())
    assert raw['cash_conversions']['option_trade_cash_gross']['native_amount'] == '200'
    if standard:
        assert raw['execution_input']['occurred_at_utc'] == '2026-09-08T14:43:37.674Z'
        assert raw['execution_id'] == change['before_payload']['raw_payload']['execution_id']
    patch = result['inbox'][0]
    assert patch['before_row']['payload_json'] == patch['after_row']['payload_json']
    assert patch['after_row']['status'] == 'handled'
    assert patch['after_row']['economic_payload_hash'] != 'old-parser-hash'
    assert result['invariants']['position_lot_count'] == 1


@pytest.mark.parametrize('mutation,error', [
    ('missing_fx', 'FX unavailable'), ('hash', 'snapshot changed'), ('duplicate', 'unique'),
    ('requested_time', 'raw source time'), ('active_claim', 'quiescent'), ('wrong_account', 'identity conflict'),
])
def test_preview_failures_leave_databases_unchanged(tmp_path, mutation, error):
    repo, path, request = fixture(tmp_path, with_fx=mutation != 'missing_fx')
    if mutation == 'hash':
        request['targets'][0]['before_sha256'] = '0' * 64
    elif mutation == 'duplicate':
        request['targets'].append(deepcopy(request['targets'][0]))
    elif mutation == 'requested_time':
        request['targets'][0]['after_trade_time_ms'] += 1
    elif mutation == 'active_claim':
        with inbox._connect(path) as conn:
            conn.execute("UPDATE trade_inbox SET claim_id='leased'")
    elif mutation == 'wrong_account':
        with inbox._connect(path) as conn:
            conn.execute("UPDATE trade_inbox SET payload_json=json_set(payload_json,'$.internal_account','sy')")
    before = dumps(repo, path)
    with pytest.raises(ValueError, match=error):
        preview_futu_time_repair(repo, request=request)
    assert dumps(repo, path) == before


def test_cli_batch_preview(tmp_path, monkeypatch, capsys):
    import src.interfaces.cli.trade_events as cli
    repo, path, request = fixture(tmp_path)
    monkeypatch.setattr(cli, 'open_futu_time_repair_store', lambda **kwargs: (repo, {}))
    file = tmp_path / 'request.json'
    file.write_text(json.dumps(request))
    before = dumps(repo, path)
    assert cli.main(['repair-futu-times', '--request', str(file)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['write_applied'] is False
    assert result['input_hash']
    assert dumps(repo, path) == before


@pytest.mark.parametrize('event_type', ['close', 'expire_close'])
def test_close_batch_preserves_native_allocations(tmp_path, event_type):
    from domain.domain.ledger.events import lot_id_for_open_event
    repo, path, request = fixture(tmp_path)
    opening = _event()
    with sqlite3.connect(repo.db_path) as conn:
        raw = json.loads(conn.execute('SELECT event_json FROM trade_events').fetchone()[0])['raw_payload']
    raw.update(create_time='2026-09-08 10:44:37.674', deal_id='closing', side='buy', price=1 if event_type == 'close' else 0)
    closing = replace(opening, event_id='futu:lx:123:closing', event_type=event_type,
                      event_time_ms=OLD_MS + 60000, price=raw['price'], raw_payload=raw,
                      target_lot_id=lot_id_for_open_event(opening))
    repo.upsert_trade_event(closing)
    refresh_position_lot_projection(repo)
    with sqlite3.connect(repo.db_path) as conn:
        text = conn.execute('SELECT event_json FROM trade_events WHERE event_id=?', (closing.event_id,)).fetchone()[0]
    request['targets'].append({'event_id': closing.event_id, 'before_sha256': hashlib.sha256(text.encode()).hexdigest(), 'after_trade_time_ms': NEW_MS + 60000})
    before = dumps(repo, path)
    result = preview_futu_time_repair(repo, request=request)
    assert len(result['events']) == 2
    assert result['invariants']['allocation_count'] == 1
    assert result['invariants']['native_economics_unchanged']
    assert dumps(repo, path) == before


def test_mismatched_physical_execution_rejected(tmp_path):
    repo, path, request = fixture(tmp_path)
    with inbox._connect(path) as conn:
        conn.execute("UPDATE trade_inbox SET payload_json=json_set(payload_json,'$.deal_id','different-fill')")
    with pytest.raises(ValueError, match='physical execution'):
        preview_futu_time_repair(repo, request=request)


def test_unchanged_evidence_drift_invalidates_input_hash(tmp_path):
    repo, path, request = fixture(tmp_path)
    before = preview_futu_time_repair(repo, request=request)
    with inbox._connect(path) as conn:
        conn.execute('UPDATE trade_inbox SET received_at_ms=received_at_ms+1')
    after = preview_futu_time_repair(repo, request=request)
    assert before['input_hash'] != after['input_hash']


def test_incomplete_split_evidence_rejected(tmp_path):
    repo, path, request = fixture(tmp_path)
    with repo._connect() as conn:
        eid, raw = conn.execute('SELECT event_id,event_json FROM trade_events').fetchone()
        event = json.loads(raw)
        event['raw_payload']['broker_deal_completion'] = {'split_count': 2, 'split_index': 1, 'expected_contracts': 2, 'allocated_contracts': 1}
        raw = json.dumps(event)
        conn.execute('UPDATE trade_events SET event_json=? WHERE event_id=?', (raw, eid))
    request['targets'][0]['before_sha256'] = hashlib.sha256(raw.encode()).hexdigest()
    with pytest.raises(ValueError, match='incomplete split execution evidence'):
        preview_futu_time_repair(repo, request=request)


def test_real_cli_preview_keeps_journal_mode_and_schema(tmp_path, capsys):
    import src.interfaces.cli.trade_events as cli
    from pathlib import Path
    root = tmp_path / 'runtime'
    location = root / 'output_shared' / 'state'
    location.mkdir(parents=True)
    repo, path, request = fixture(location)
    # Use the supported runtime path without monkeypatching the CLI facade.
    import gc
    gc.collect()  # Fixture owners use context-managed SQLite transactions; release idle handles.
    destination = location / 'option_positions.sqlite3'
    for old, new in ((repo.db_path, destination), (path, Path(str(destination) + '.trade_intake_inbox.sqlite3'))):
        with sqlite3.connect(old) as conn:
            conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            conn.execute('PRAGMA journal_mode=DELETE')
        old.rename(new)
    file = tmp_path / 'request.json'
    file.write_text(json.dumps(request))
    before = {str(p): p.read_bytes() for p in location.iterdir() if p.is_file()}
    assert cli.main(['repair-futu-times', '--request', str(file), '--runtime-root', str(root)]) == 0
    assert json.loads(capsys.readouterr().out)['write_applied'] is False
    assert before == {str(p): p.read_bytes() for p in location.iterdir() if p.is_file()}


@pytest.mark.parametrize("field", ["physical", "internal", "environment", "deal"])
def test_raw_normalized_identity_conflict_rejected(tmp_path, field):
    repo, path, request = fixture(tmp_path, standard=True)
    with repo._connect() as conn:
        eid, raw = conn.execute('SELECT event_id,event_json FROM trade_events').fetchone()
        event = json.loads(raw)
        ex = event['raw_payload']['execution_input']
        if field == 'physical':
            ex['broker_account_ref']['external_account_id'] = '999'
            ex['broker_account_ref']['broker_account_id'] = 'futu:REAL:999'
        elif field == 'internal':
            ex['broker_account_ref']['account_label'] = 'sy'
        elif field == 'environment':
            ex['broker_account_ref']['environment'] = 'SIMULATE'
        else:
            ex['external_execution_id'] = 'other-deal'
        event['raw_payload']['execution_id'] = execution_identity_from_input(ex)
        raw = json.dumps(event)
        conn.execute('UPDATE trade_events SET event_json=? WHERE event_id=?', (raw, eid))
    request['targets'][0]['before_sha256'] = hashlib.sha256(raw.encode()).hexdigest()
    with pytest.raises(ValueError, match='conflict'):
        preview_futu_time_repair(repo, request=request)


@pytest.mark.parametrize("field", ["raw_physical", "nested_physical", "internal"])
def test_inbox_raw_normalized_identity_conflict_rejected(tmp_path, field):
    repo, path, request = fixture(tmp_path)
    with inbox._connect(path) as conn:
        raw = json.loads(conn.execute('SELECT payload_json FROM trade_inbox').fetchone()[0])
        raw['execution_input'] = _futu_execution_input(raw)
        if field == 'raw_physical':
            raw['futu_account_id'] = '999'
        elif field == 'nested_physical':
            raw['execution_input']['broker_account_ref']['external_account_id'] = '999'
        else:
            raw['execution_input']['broker_account_ref']['account_label'] = 'sy'
        conn.execute('UPDATE trade_inbox SET payload_json=?', (json.dumps(raw),))
    with pytest.raises(ValueError, match='identity conflict'):
        preview_futu_time_repair(repo, request=request)


def apply_fixture(repo, request, plan, backup):
    import gc
    from src.application.trades.review import repair_futu_time_batch
    gc.collect()
    return repair_futu_time_batch(repo, request=request, expected_input_hash=plan['input_hash'], backup_dir=str(backup))


def test_apply_joint_transaction_receipt_backup_retry(tmp_path):
    repo, path, request = fixture(tmp_path, standard=True)
    plan = preview_futu_time_repair(repo, request=request)
    before = dumps(repo, path)
    result = apply_fixture(repo, request, plan, tmp_path / 'backup')
    assert result['mode'] == 'applied' and result['durable_readback']
    assert result['journal_restored'] == {'main': True, 'repair_inbox': True}
    assert result['event_count'] == result['inbox_count'] == 1
    for index, backup in enumerate(result['backups']):
        from pathlib import Path
        target = Path(backup['path'])
        assert hashlib.sha256(target.read_bytes()).hexdigest() == backup['sha256']
        with sqlite3.connect(target) as conn:
            assert list(conn.iterdump()) == before[index]
    after = dumps(repo, path)
    retry = apply_fixture(repo, request, plan, tmp_path / 'unused')
    assert retry['mode'] == 'no_op' and retry['write_applied'] is False
    assert dumps(repo, path) == after
    assert not (tmp_path / 'unused').exists()
    with inbox._connect(repo.db_path) as conn:
        assert conn.execute('SELECT COUNT(*) FROM futu_trade_time_repair_audit').fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError, match='append-only'):
            conn.execute("DELETE FROM futu_trade_time_repair_audit")



@pytest.mark.parametrize('failure', ['hash', 'drift', 'cas', 'inbox', 'projection', 'interrupt'])
def test_apply_failures_rollback_both_stores(tmp_path, monkeypatch, failure):
    import src.application.ledger.position_projection_runtime as runtime
    repo, path, request = fixture(tmp_path)
    plan = preview_futu_time_repair(repo, request=request)
    if failure == 'hash':
        plan['input_hash'] = '0' * 64
    elif failure == 'drift':
        with inbox._connect(path) as conn:
            conn.execute('UPDATE trade_inbox SET received_at_ms=received_at_ms+1')
    elif failure == 'cas':
        monkeypatch.setattr(repo, 'compare_and_swap_trade_event_time', lambda **kwargs: False)
    elif failure in {'inbox', 'interrupt'}:
        original = inbox.apply_futu_time_repair
        def fail(*args):
            original(*args)
            if failure == 'interrupt':
                raise KeyboardInterrupt()
            raise ValueError('injected inbox error')
        monkeypatch.setattr(inbox, 'apply_futu_time_repair', fail)
    else:
        monkeypatch.setattr(runtime, 'run_position_projection_in_transaction', lambda *a, **kw: (_ for _ in ()).throw(ValueError('injected projector error')))
    before = dumps(repo, path)
    with pytest.raises((ValueError, KeyboardInterrupt)):
        apply_fixture(repo, request, plan, tmp_path / 'backup')
    assert dumps(repo, path) == before



@pytest.mark.parametrize('fault', ['event', 'inbox', 'after_commit'])
def test_process_crash_recovery_and_lost_response(tmp_path, fault):
    import subprocess, sys
    repo, path, request = fixture(tmp_path)
    plan = preview_futu_time_repair(repo, request=request)
    file = tmp_path / 'request.json'
    file.write_text(json.dumps(request))
    before = dumps(repo, path)
    script = r'''import json,os,sys
from pathlib import Path
from src.application.ledger.repository import SQLiteOptionPositionsRepository
from src.application.trades import inbox
from src.application.trades.review import repair_futu_time_batch
repo=SQLiteOptionPositionsRepository(Path(sys.argv[1]),initialize=False)
fault=sys.argv[5]
if fault=='event':
 original=repo.compare_and_swap_trade_event_time
 def stop(**kw):
  original(**kw)
  os._exit(71)
 repo.compare_and_swap_trade_event_time=stop
elif fault=='inbox':
 original=inbox.apply_futu_time_repair
 def stop(*args):
  original(*args)
  os._exit(72)
 inbox.apply_futu_time_repair=stop
repair_futu_time_batch(repo,request=json.loads(Path(sys.argv[2]).read_text()),expected_input_hash=sys.argv[3],backup_dir=sys.argv[4])
os._exit(73)
'''
    import gc
    gc.collect()
    result = subprocess.run([sys.executable, '-c', script, str(repo.db_path), str(file), plan['input_hash'], str(tmp_path / 'backup'), fault], capture_output=True, text=True)
    assert result.returncode == {'event': 71, 'inbox': 72, 'after_commit': 73}[fault], result.stderr
    if fault != 'after_commit':
        assert dumps(repo, path) == before
    else:
        retry = apply_fixture(repo, request, plan, tmp_path / 'unused')
        assert retry['mode'] == 'no_op' and retry['durable_readback']


def test_raw_time_sql_without_matching_audit_rejected(tmp_path):
    repo, path, request = fixture(tmp_path)
    with repo._connect() as conn:
        with pytest.raises(sqlite3.IntegrityError, match='immutable'):
            conn.execute("UPDATE trade_events SET trade_time_ms=?,event_json=json_set(event_json,'$.event_time_ms',?,'$.raw_payload.trade_time_correction_provenance.schema_version','futu_raw_trade_time_correction.v1')", (NEW_MS, NEW_MS))


def test_apply_requires_hash_backup_and_rejects_batch_conflict(tmp_path):
    from src.application.trades.review import repair_futu_time_batch
    repo, path, request = fixture(tmp_path)
    plan = preview_futu_time_repair(repo, request=request)
    for digest, backup in [(None, str(tmp_path/'backup')), (plan['input_hash'], None)]:
        with pytest.raises(ValueError, match='requires'):
            repair_futu_time_batch(repo, request=request, expected_input_hash=digest, backup_dir=backup)
    apply_fixture(repo, request, plan, tmp_path/'backup')
    changed = {**request, 'reason': 'another reason'}
    with pytest.raises(ValueError, match='already used'):
        apply_fixture(repo, changed, plan, tmp_path/'unused')
    with inbox._connect(path) as conn:
        conn.execute("UPDATE trade_inbox SET updated_at_ms=updated_at_ms+1")
    with pytest.raises(ValueError, match='readback mismatch'):
        apply_fixture(repo, request, plan, tmp_path/'unused')


def test_reader_blocks_journal_change_without_business_writes(tmp_path):
    import gc
    repo, path, request = fixture(tmp_path)
    plan = preview_futu_time_repair(repo, request=request)
    before = dumps(repo, path)
    gc.collect()
    with sqlite3.connect(repo.db_path) as reader:
        reader.execute('BEGIN')
        reader.execute('SELECT * FROM trade_events').fetchall()
        with pytest.raises(sqlite3.OperationalError, match='locked'):
            apply_fixture(repo, request, plan, tmp_path/'backup')
    assert dumps(repo, path) == before
    assert not (tmp_path/'backup').exists()


def test_cli_confirm_apply_and_same_request_noop(tmp_path, monkeypatch, capsys):
    import gc
    import src.interfaces.cli.trade_events as cli
    repo, path, request = fixture(tmp_path)
    monkeypatch.setattr(cli, 'open_futu_time_repair_store', lambda **kwargs: (repo, {}))
    monkeypatch.setattr(cli, '_guard_write', lambda **kwargs: {'ok': True})
    file = tmp_path/'request.json';file.write_text(json.dumps(request))
    assert cli.main(['repair-futu-times','--request',str(file)]) == 0
    plan = json.loads(capsys.readouterr().out)
    args = ['repair-futu-times','--request',str(file),'--confirm','--expected-input-hash',plan['input_hash'],'--backup-dir',str(tmp_path/'backup')]
    gc.collect()
    assert cli.main(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['mode'] == 'applied' and result['write_applied']
    assert cli.main(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['mode'] == 'no_op' and result['write_applied'] is False


def test_committed_readback_failure_does_not_report_unwritten(tmp_path, monkeypatch):
    import src.application.ledger.futu_time_repair as repair
    repo, path, request = fixture(tmp_path)
    plan = preview_futu_time_repair(repo, request=request)
    original = repair._prior_receipt
    calls = 0
    def fail_readback(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise sqlite3.OperationalError('injected readback error')
        return original(*args)
    monkeypatch.setattr(repair, '_prior_receipt', fail_readback)
    result = apply_fixture(repo, request, plan, tmp_path/'backup')
    assert result['mode'] == 'committed_readback_unavailable' and result['write_applied']
    assert result['durable_readback'] is False
    assert apply_fixture(repo, request, plan, tmp_path/'unused')['mode'] == 'no_op'


def test_unenriched_push_identity_binds_to_unique_ledger_account(tmp_path):
    repo, path, request = fixture(tmp_path)
    with inbox._connect(path) as conn:
        conn.execute("UPDATE trade_inbox SET payload_json=json_remove(payload_json,'$.internal_account')")
    result = preview_futu_time_repair(repo, request=request)
    assert len(result['inbox']) == 1
    raw = json.loads(result['inbox'][0]['after_row']['payload_json'])
    assert 'internal_account' not in raw


def test_unbound_legacy_receipt_is_preserved_without_identity_inference(tmp_path):
    repo, path, request = fixture(tmp_path)
    with inbox._connect(path) as conn:
        conn.execute("UPDATE trade_inbox SET broker_deal_key=NULL,economic_payload_hash=NULL,payload_json=json_remove(payload_json,'$.internal_account','$.environment','$.trd_env')")
    before = dumps(repo, path)[1]
    plan = preview_futu_time_repair(repo, request=request)
    assert plan['inbox'] == []
    apply_fixture(repo, request, plan, tmp_path/'backup')
    assert dumps(repo, path)[1] == before



@pytest.mark.parametrize('kind', ['hash', 'nested', 'top_level'])
def test_unbound_rows_with_saved_derived_content_are_not_skipped(tmp_path, kind):
    repo, path, request = fixture(tmp_path)
    with inbox._connect(path) as conn:
        raw = json.loads(conn.execute('SELECT payload_json FROM trade_inbox').fetchone()[0])
        if kind == 'nested':
            raw['execution_input'] = _futu_execution_input(raw)
        elif kind == 'top_level':
            raw['occurred_at_utc'] = '2026-09-08T02:43:37.674Z'
        conn.execute('UPDATE trade_inbox SET broker_deal_key=NULL,economic_payload_hash=?,payload_json=?', ('saved' if kind=='hash' else None,json.dumps(raw)))
    with pytest.raises(ValueError):
        preview_futu_time_repair(repo, request=request)



@pytest.mark.parametrize('failure', ['before_commit', 'after_commit', 'unreadable', 'close', 'rollback'])
def test_cli_commit_ambiguity_is_resolved_or_explicitly_unknown(tmp_path, monkeypatch, capsys, failure):
    import gc
    import src.interfaces.cli.trade_events as cli
    import src.application.ledger.futu_time_repair as repair
    repo, path, request = fixture(tmp_path)
    plan = preview_futu_time_repair(repo, request=request)
    original_connect = sqlite3.connect
    class CommitFault(sqlite3.Connection):
        def commit(self):
            if failure not in {'before_commit', 'rollback'}:
                super().commit()
            if failure != 'close':
                raise sqlite3.OperationalError('lost COMMIT acknowledgement')
        def rollback(self):
            super().rollback()
            if failure == 'rollback':
                raise sqlite3.OperationalError('lost rollback acknowledgement')
        def close(self):
            super().close()
            if failure == 'close':
                raise sqlite3.OperationalError('close acknowledgement lost')
    def connect(*args, **kwargs):
        if kwargs.get('isolation_level', 'default') is None:
            kwargs['factory'] = CommitFault
        return original_connect(*args, **kwargs)
    monkeypatch.setattr(repair.sqlite3, 'connect', connect)
    original_receipt = repair._prior_receipt
    calls = 0
    def read_receipt(*args):
        nonlocal calls
        calls += 1
        if calls == 2 and failure == 'unreadable':
            raise sqlite3.OperationalError('read unavailable')
        return original_receipt(*args)
    monkeypatch.setattr(repair, '_prior_receipt', read_receipt)
    monkeypatch.setattr(cli, 'open_futu_time_repair_store', lambda **kw: (repo, {}))
    monkeypatch.setattr(cli, '_guard_write', lambda **kw: {'ok':True})
    file=tmp_path/'request.json';file.write_text(json.dumps(request));gc.collect()
    code=cli.main(['repair-futu-times','--request',str(file),'--confirm','--expected-input-hash',plan['input_hash'],'--backup-dir',str(tmp_path/'backup')])
    result=json.loads(capsys.readouterr().out)
    assert result['mode']=={'before_commit':'not_applied','after_commit':'applied','unreadable':'commit_outcome_unknown','close':'applied','rollback':'not_applied'}[failure]
    assert result['write_applied'] is {'before_commit':False,'after_commit':True,'unreadable':None,'close':True,'rollback':False}[failure]
    assert code==(0 if failure in {'after_commit','close'} else 2)
    with original_connect(repo.db_path) as conn:
        count=conn.execute('SELECT COUNT(*) FROM futu_trade_time_repair_audit').fetchone()[0]
        assert count==(0 if failure in {'before_commit','rollback'} else 1)
