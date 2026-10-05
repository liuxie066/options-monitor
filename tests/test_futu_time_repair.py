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
