from dataclasses import replace
import json

import pytest

from src.application.ledger.writer import persist_trade_event_objects_atomically
from test_order_fee_settlement import _repo, _assignments, _sync, Provider


def _replay_repo(tmp_path, *, with_source=False):
    repo = _repo(tmp_path)
    # The raw target binding is part of the lifecycle allocation writer contract.
    with repo._writer_connection(begin_immediate=True) as conn:
        for row in conn.execute("SELECT event_id, event_json FROM trade_events WHERE json_extract(event_json, '$.event_type')='assignment'").fetchall():
            event = json.loads(row['event_json'])
            event['raw_payload']['target_lot_id'] = event['target_lot_id']
            if with_source:
                event['raw_payload']['stock_settlement_source'] = dict(event['raw_payload']['stock_settlement'])
            conn.execute('UPDATE trade_events SET event_json=? WHERE event_id=?',
                         (json.dumps(event, ensure_ascii=False, sort_keys=True), row['event_id']))
    return repo


@pytest.mark.parametrize('with_source', [False, True])
def test_audited_stock_fee_enrichment_allows_original_assignment_replay(tmp_path, with_source):
    repo = _replay_repo(tmp_path, with_source=with_source)
    originals = _assignments(repo)
    assert _sync(repo, Provider(), apply=True)['migration']['status_counts'] == {'committed': 1}
    before = repo.list_trade_events()
    for event in originals:
        result = persist_trade_event_objects_atomically(repo, [event])
        assert all(not item.created for item in result)
    assert repo.list_trade_events() == before


@pytest.mark.parametrize('change', ['price', 'actual_fee', 'missing_audit', 'changed_after_audit'])
def test_enrichment_does_not_hide_conflicting_replay(tmp_path, change):
    repo = _replay_repo(tmp_path)
    event = _assignments(repo)[0]
    _sync(repo, Provider(), apply=True)
    before = repo.list_trade_events()
    raw = dict(event.raw_payload)
    stock = dict(raw['stock_settlement'])
    if change == 'price':
        stock['price'] = 441
    elif change == 'actual_fee':
        stock.update(fees='99', fee_provenance={'basis': 'actual', 'amount': '99', 'source': 'operator'})
    elif change == 'changed_after_audit':
        with repo._writer_connection(begin_immediate=True) as conn:
            row = conn.execute('SELECT event_json FROM trade_events WHERE event_id=?', (event.event_id,)).fetchone()
            changed = json.loads(row[0])
            changed['raw_payload']['unaudited_change'] = True
            conn.execute('UPDATE trade_events SET event_json=? WHERE event_id=?', (json.dumps(changed, ensure_ascii=False, sort_keys=True), event.event_id))
        before = repo.list_trade_events()
    else:
        with repo._writer_connection(begin_immediate=True) as conn:
            conn.execute('DELETE FROM broker_fee_enrichment_audit')
    raw['stock_settlement'] = stock
    with pytest.raises(ValueError):
        persist_trade_event_objects_atomically(repo, [replace(event, raw_payload=raw)])
    assert repo.list_trade_events() == before


@pytest.mark.parametrize('remove_fx_audit', [False, True])
@pytest.mark.parametrize('correct_rate', [False, True])
def test_fee_replay_after_real_fx_backfill_requires_its_audit(tmp_path, remove_fx_audit, correct_rate):
    from src.application.ledger.cash_conversion_migration import backfill_cash_conversions
    from src.infrastructure.performance_evidence_sqlite import PerformanceEvidenceSQLiteRepository
    from test_order_fee_settlement import TIME

    repo = _replay_repo(tmp_path, with_source=True)
    originals = _assignments(repo)
    _sync(repo, Provider(), apply=True)
    evidence = PerformanceEvidenceSQLiteRepository(repo.db_path)
    evidence.import_envelope({
        'schema_version': 'option_performance_evidence.v1', 'valuation_marks': [],
        'fx_rates': [{
            'base_currency': 'HKD', 'quote_currency': 'CNY', 'rate': '0.92',
            'rate_kind': 'central_parity', 'effective_at_ms': TIME - 1,
            'observed_at_ms': TIME + 200, 'source': 'pbc_central_parity',
            'source_id': 'test-hkd-rate', 'revision': 1, 'supersedes_fact_id': None,
            'quality': {'backfill': True}, 'raw': {},
        }],
    }, apply=True, migrated_at_ms=TIME + 200)
    result = backfill_cash_conversions(repo, evidence, account='lx', apply=True, migrated_at_ms=TIME + 300)
    assert result.changed_event_count > 0
    assert any(change.cash_fact_id.startswith('stock_settlement_fee_cash:') for change in result.changes)
    if correct_rate:
        from src.application.ledger.cash_conversion_migration import correct_superseded_cash_conversions
        previous = evidence.read_all().fx_rates[0]
        corrected = dict(previous.normalized_payload())
        corrected.pop('fact_id', None)
        corrected.update(rate='0.93', source='manual_correction', source_id='test-hkd-correction',
                         supersedes_fact_id=previous.fact_id, observed_at_ms=TIME + 400)
        evidence.import_envelope({
            'schema_version': 'option_performance_evidence.v1', 'valuation_marks': [],
            'fx_rates': [previous.normalized_payload(), corrected],
        }, apply=True, migrated_at_ms=TIME + 400)
        correction = correct_superseded_cash_conversions(repo, evidence, account='lx', apply=True, migrated_at_ms=TIME + 500)
        assert correction.changed_event_count > 0
    if remove_fx_audit:
        with repo._writer_connection(begin_immediate=True) as conn:
            conn.execute('DELETE FROM cash_conversion_correction_audit' if correct_rate else 'DELETE FROM cash_conversion_backfill_audit')
    before = repo.list_trade_events()
    for event in originals:
        if remove_fx_audit:
            with pytest.raises(ValueError):
                persist_trade_event_objects_atomically(repo, [event])
        else:
            assert all(not result.created for result in persist_trade_event_objects_atomically(repo, [event]))
    assert repo.list_trade_events() == before
