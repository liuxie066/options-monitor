from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest

from src.application.ledger import api
from src.application.ledger import read_only_evidence as evidence
from src.application.trades.inbox import read_stock_sell_observations
from domain.domain.trade_execution import EXECUTION_INPUT_VERSION


def _ledger(path):
    with sqlite3.connect(path) as conn:
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('CREATE TABLE trade_events (event_id TEXT, trade_time_ms INTEGER, event_json TEXT)')
        conn.execute('CREATE TABLE assigned_stock_events (stock_event_id TEXT, trade_time_ms INTEGER, event_json TEXT)')
        conn.execute('INSERT INTO trade_events VALUES (?, ?, ?)', ('before', 1, json.dumps({'event_id': 'before'})))
        conn.execute('INSERT INTO assigned_stock_events VALUES (?, ?, ?)', ('old-sale', 2, json.dumps({'stock_event_id': 'old-sale'})))


def test_snapshot_keeps_both_sources_before_intervening_commit(tmp_path, monkeypatch):
    path = tmp_path / 'ledger.sqlite3'
    _ledger(path)
    original = evidence._ReadOnlyTradeReconciliationEvidenceRepository._read_trade_events

    def read_then_commit(self, conn, **kwargs):
        rows = original(self, conn, **kwargs)
        with sqlite3.connect(path) as writer:
            writer.execute("UPDATE trade_events SET event_json=?", (json.dumps({'event_id': 'after'}),))
            writer.execute("UPDATE assigned_stock_events SET event_json=?", (json.dumps({'stock_event_id': 'new-sale'}),))
        return rows

    monkeypatch.setattr(evidence._ReadOnlyTradeReconciliationEvidenceRepository, '_read_trade_events', read_then_commit)
    result = api.read_assigned_stock_query_inputs(SimpleNamespace(db_path=path))
    assert result['trade_events'][0]['event_id'] == 'before'
    assert result['account_assigned_stock_events'][0]['stock_event_id'] == 'old-sale'
    assert result['source']['coverage'] == 'complete'
    with sqlite3.connect(path) as conn:
        assert json.loads(conn.execute('SELECT event_json FROM assigned_stock_events').fetchone()[0])['stock_event_id'] == 'new-sale'


@pytest.mark.parametrize('bad', ['{', '[]'])
def test_invalid_stock_source_is_not_empty(tmp_path, bad):
    path = tmp_path / 'ledger.sqlite3'
    _ledger(path)
    with sqlite3.connect(path) as conn:
        conn.execute('UPDATE assigned_stock_events SET event_json=?', (bad,))
    with pytest.raises(ValueError):
        api.read_assigned_stock_query_inputs(SimpleNamespace(path=path))


def test_missing_source_table_is_not_zero(tmp_path):
    path = tmp_path / 'ledger.sqlite3'
    _ledger(path)
    with sqlite3.connect(path) as conn:
        conn.execute('DROP TABLE trade_events')
    with pytest.raises(sqlite3.DatabaseError):
        api.read_assigned_stock_query_inputs(SimpleNamespace(path=path))


def _sell(account='lx', physical='test-lx', deal_id='sale-1'):
    return {
        'schema_version': EXECUTION_INPUT_VERSION,
        'broker_account_ref': {'broker_account_id': 'test-account', 'broker_id': 'futu',
                               'external_account_id': physical, 'environment': 'REAL', 'account_label': account},
        'instrument_ref': {'asset_type': 'stock', 'symbol': 'NVDA', 'market': 'US', 'currency': 'USD'},
        'external_id_namespace': 'futu-us-deals', 'external_execution_id': deal_id,
        'external_order_namespace': 'futu-orders', 'external_order_id': 'test-order',
        'side': 'sell', 'quantity': '40', 'price': '100', 'currency': 'USD',
        'occurred_at_utc': '2026-09-07T01:02:03Z', 'evidence_refs': ['synthetic-test-evidence'],
    }


def _inbox(path, rows):
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE trade_inbox (inbox_id TEXT, payload_json TEXT, result_json TEXT, received_at_ms INTEGER, result_status TEXT, result_reason TEXT)')
        for idx, (payload, status) in enumerate(rows):
            conn.execute('INSERT INTO trade_inbox VALUES (?, ?, ?, ?, ?, ?)',
                         (str(idx), json.dumps(payload), '{}', idx, status, 'test'))


def test_sell_discovery_includes_unresolved_and_handled_without_writes(tmp_path, monkeypatch):
    path = tmp_path / 'inbox.sqlite3'
    _inbox(path, [(_sell(), 'unresolved'), (_sell(deal_id='sale-2'), 'skipped'),
                  (_sell('sy', 'test-sy', 'sale-3'), 'applied')])
    from src.application.trades import normalizer
    original = normalizer.normalize_trade_deal
    calls = []

    def offline(payload, **kwargs):
        calls.append(kwargs['allow_opend_refresh'])
        return original(payload, **kwargs)

    monkeypatch.setattr(normalizer, 'normalize_trade_deal', offline)
    before = path.read_bytes()
    result = read_stock_sell_observations(path, futu_account_mapping={'test-lx': 'lx', 'test-sy': 'sy'}, accounts=['lx'])
    assert [r['source_deal_id'] for r in result['observations']] == ['sale-1', 'sale-2']
    assert all(r['broker'] == '富途' for r in result['observations'])
    assert result['coverage'] == 'complete'
    assert result['source_history'] == 'unknown'
    assert calls == [False, False, False]
    assert path.read_bytes() == before


def test_missing_inbox_is_unknown_without_creation(tmp_path):
    path = tmp_path / 'absent.sqlite3'
    result = read_stock_sell_observations(path, futu_account_mapping={})
    assert result['coverage'] == 'unknown'
    assert not path.exists()


def test_pending_state_outside_inbox_remains_visible(tmp_path):
    path = tmp_path / 'inbox.sqlite3'
    _inbox(path, [])
    state_path = tmp_path / 'output_shared/state/trade_intake/lx/state.json'
    state_path.parent.mkdir(parents=True)
    state_path.write_text(json.dumps({
        'unresolved_deal_ids': {'old-deal': {
            'action': 'assigned_stock_sale', 'account': 'lx', 'status': 'unresolved',
            'source_deal_id': 'old-deal', 'futu_account_id': 'test-lx',
            'diagnostics': {'candidates': [{'symbol': 'NVDA', 'stock_lot_id': 'old-lot'}]},
        }}, 'failed_deal_ids': {},
    }))
    before = state_path.read_bytes()
    result = read_stock_sell_observations(path, futu_account_mapping={'test-lx': 'lx'},
                                          accounts=['lx'], runtime_root=tmp_path)
    assert result['coverage'] == 'complete'
    assert result['observations'][0]['source_deal_id'] == 'old-deal'
    assert result['observations'][0]['symbol'] == 'NVDA'
    assert result['observations'][0]['trade_time_ms'] is None
    assert result['source_history'] == 'unknown'
    assert state_path.read_bytes() == before


def test_incomplete_state_is_not_complete_source(tmp_path):
    path = tmp_path / 'inbox.sqlite3'
    _inbox(path, [])
    state_path = tmp_path / 'output_shared/state/trade_intake/lx/state.json'
    state_path.parent.mkdir(parents=True)
    state_path.write_text('{}')
    result = read_stock_sell_observations(path, futu_account_mapping={'test-lx': 'lx'},
                                          accounts=['lx'], runtime_root=tmp_path)
    assert result['coverage'] == 'unknown'
    assert all(row['account'] == 'lx' for row in result['diagnostics'])


def test_standard_stock_sell_reaches_canonical_batch_pending_scope(tmp_path):
    from src.application.positions.assignment_period_report import _affected

    path = tmp_path / 'inbox.sqlite3'
    _inbox(path, [(_sell(), 'unresolved')])
    observations = read_stock_sell_observations(
        path, futu_account_mapping={'test-lx': 'lx'}, accounts=['lx'],
    )['observations']
    assert len(observations) == 1
    observation = observations[0]
    lot = {'assigned_at_ms': observation['trade_time_ms'] - 1, 'account': 'lx',
           'broker': '富途', 'symbol': 'NVDA', 'currency': 'USD'}
    assert _affected(lot, observation)


@pytest.mark.parametrize('override', [None, 'custom/state.json'])
def test_configured_single_account_state_path_is_authoritative(tmp_path, override):
    from src.application.trades.account_mapping import resolve_trade_intake_config

    path = tmp_path / 'inbox.sqlite3'
    _inbox(path, [])
    cfg = {'accounts': ['lx'], 'account_settings': {'lx': {
        'account_type': 'futu', 'futu': {'account_id': 'test-lx', 'host': '127.0.0.1', 'port': 11111},
    }}, 'trade_intake': {'account_mapping': {'futu': {'test-lx': 'lx'}}}}
    if override:
        cfg['trade_intake']['state_path'] = override
    source = resolve_trade_intake_config(cfg)['sources'][0]
    assert source['account'] == 'lx'
    assert str(source['state_path']) == (override or 'output_shared/state/auto_trade_intake_state.json')
    canonical = tmp_path / source['state_path']
    canonical.parent.mkdir(parents=True, exist_ok=True)
    canonical.write_text(json.dumps({'unresolved_deal_ids': {'pending': {
        'account': 'lx', 'action': 'assigned_stock_sale', 'status': 'unresolved',
        'source_deal_id': 'pending', 'futu_account_id': 'test-lx',
        'diagnostics': {'candidates': [{'symbol': 'NVDA'}]},
    }}, 'failed_deal_ids': {}}))
    misleading = tmp_path / 'output_shared/state/trade_intake/lx/state.json'
    misleading.parent.mkdir(parents=True, exist_ok=True)
    misleading.write_text(json.dumps({'unresolved_deal_ids': {}, 'failed_deal_ids': {}}))
    before = canonical.read_bytes()
    result = read_stock_sell_observations(path, futu_account_mapping={'test-lx': 'lx'}, config=cfg,
                                          accounts=['lx'], runtime_root=tmp_path)
    assert result['coverage'] == 'complete'
    assert [r['source_deal_id'] for r in result['observations']] == ['pending']
    assert canonical.read_bytes() == before


def test_shared_legacy_state_does_not_stamp_scope_onto_unbound_entry(tmp_path):
    from src.application.trades.account_mapping import resolve_trade_intake_config

    path = tmp_path / 'inbox.sqlite3'
    _inbox(path, [])
    cfg = {'accounts': ['lx', 'sy'], 'trade_intake': {
        'account_mapping': {'futu': {'test-lx': 'lx', 'test-sy': 'sy'}},
    }}
    source = resolve_trade_intake_config(cfg)['sources'][0]
    assert source['account'] is None
    state = tmp_path / source['state_path']
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps({'unresolved_deal_ids': {
        'unbound': {'action': 'assigned_stock_sale', 'source_deal_id': 'private-unknown-id'},
        'other': {'action': 'assigned_stock_sale', 'account': 'sy', 'futu_account_id': 'test-sy',
                  'source_deal_id': 'private-other-id'},
        'bound': {'action': 'assigned_stock_sale', 'account': 'lx', 'futu_account_id': 'test-lx',
                  'source_deal_id': 'visible-id'},
    }, 'failed_deal_ids': {}}))
    result = read_stock_sell_observations(
        path, futu_account_mapping={'test-lx': 'lx', 'test-sy': 'sy'},
        config=cfg, accounts=['lx'], runtime_root=tmp_path,
    )
    assert [r['source_deal_id'] for r in result['observations']] == ['visible-id']
    assert result['coverage'] == 'unknown'
    assert all(r['account'] == 'lx' for r in result['diagnostics'])
    assert 'private-' not in json.dumps(result)


@pytest.mark.parametrize('requested, expected_coverage', [('lx', 'unknown'), ('sy', 'unknown'), ('zz', 'complete')])
def test_shared_state_conflicting_label_and_physical_scope_is_not_silently_skipped(tmp_path, requested, expected_coverage):
    from src.application.trades.account_mapping import resolve_trade_intake_config

    mapping = {'test-lx': 'lx', 'test-sy': 'sy', 'test-zz': 'zz'}
    cfg = {'accounts': ['lx', 'sy', 'zz'], 'trade_intake': {'account_mapping': {'futu': mapping}}}
    source = resolve_trade_intake_config(cfg)['sources'][0]
    assert source['account'] is None
    path = tmp_path / 'inbox.sqlite3'
    _inbox(path, [])
    state = tmp_path / source['state_path']
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps({'unresolved_deal_ids': {'conflict': {
        'action': 'assigned_stock_sale', 'account': 'lx', 'futu_account_id': 'test-sy',
        'source_deal_id': 'private-conflicting-source',
    }}, 'failed_deal_ids': {}}))
    result = read_stock_sell_observations(path, futu_account_mapping=mapping, config=cfg,
                                          accounts=[requested], runtime_root=tmp_path)
    assert result['coverage'] == expected_coverage
    assert result['observations'] == []
    assert all(r['account'] == requested for r in result['diagnostics'])
    assert 'private-conflicting-source' not in json.dumps(result)
