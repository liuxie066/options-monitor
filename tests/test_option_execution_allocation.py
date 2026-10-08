from copy import deepcopy
from decimal import Decimal

import pytest

from domain.domain.trade_execution import ledger_execution_event_set_is_complete
from src.application.ledger.repository import SQLiteOptionPositionsRepository
from src.application.trades.normalizer import normalize_trade_deal
from src.application.trades.resolver import resolve_trade_deal
from tests.test_trade_execution_input import execution_input


def fill(identity, *, side='buy', quantity=1, time='2026-10-01T14:00:00Z', effect=None, option_type='call', multiplier=100):
    raw = execution_input()
    raw['instrument_ref'].pop('deliverable')
    raw['instrument_ref'].update(option_type=option_type, expiration_ymd='2026-11-06', multiplier=str(multiplier))
    raw.update(external_id_namespace='futu.deal', external_order_namespace='futu.order',
               external_execution_id=identity, external_order_id='order-' + identity,
               quantity=str(quantity), side=side, price='0.62', occurred_at_utc=time)
    if effect:
        raw['position_effect'] = effect
    return normalize_trade_deal(raw, allow_opend_refresh=False)


def apply(repo, deal):
    result = resolve_trade_deal(deal, repo=repo, state={}, apply_changes=True)
    assert result.status == 'applied', result.to_dict()
    return result


@pytest.mark.parametrize('side', ['buy', 'sell'])
@pytest.mark.parametrize('opposite,quantity,expected', [(0, 1, 'open'), (2, 1, 'close'), (2, 2, 'close'), (2, 3, 'open_close')])
def test_allocation_real_sqlite(tmp_path, side, opposite, quantity, expected):
    repo = SQLiteOptionPositionsRepository(tmp_path / 'ledger.sqlite3')
    if opposite:
        apply(repo, fill('seed', side='sell' if side == 'buy' else 'buy', quantity=opposite, effect='open'))
    deal = fill('new', side=side, quantity=quantity, time='2026-10-01T14:01:00Z')
    result = apply(repo, deal)
    assert result.action == expected
    rows = [row for row in repo.list_trade_events() if (row['raw_payload'].get('execution_input') or {}).get('external_execution_id') == 'new']
    assert sum(row['contracts'] for row in rows) == quantity
    assert ledger_execution_event_set_is_complete(rows)
    assert all(row['raw_payload']['execution_input']['quantity'] == str(quantity) for row in rows)
    before = deepcopy(repo.list_trade_events())
    replay = resolve_trade_deal(deal, repo=repo, state={}, apply_changes=True)
    assert replay.reason == 'ledger_recorded'
    assert replay.action == expected
    assert repo.list_trade_events() == before
    if expected == 'open_close':
        assert len(rows) == 2
        assert not ledger_execution_event_set_is_complete(rows[:1])
        assert {row['event_type'] for row in rows} == {'open', 'close'}
        from src.application.ledger.writer_trade_events import persist_trade_event_objects_atomically
        from domain.domain.ledger import TradeEvent
        persist_trade_event_objects_atomically(repo, [TradeEvent.from_dict(row) for row in rows])
        assert repo.list_trade_events() == before
        assert sum(Decimal(row['raw_payload']['fee_provenance']['amount']) for row in rows) > 0


def test_late_fill_cannot_reclassify_after_later_close(tmp_path):
    repo = SQLiteOptionPositionsRepository(tmp_path / 'ledger.sqlite3')
    apply(repo, fill('seed', side='sell', effect='open'))
    apply(repo, fill('later', side='buy', time='2026-10-01T14:02:00Z', effect='close'))
    before = repo.list_trade_events()
    late = resolve_trade_deal(fill('late', time='2026-10-01T14:01:00Z'), repo=repo, state={}, apply_changes=True)
    assert late.status == 'unresolved'
    assert late.reason == 'unknown_position_effect:later_or_same_time_ledger_event'
    assert repo.list_trade_events() == before


def test_multi_lot_cross_zero_rollback_and_retry(tmp_path, monkeypatch):
    repo = SQLiteOptionPositionsRepository(tmp_path / 'ledger.sqlite3')
    apply(repo, fill('seed-1', side='sell', quantity=2, effect='open'))
    apply(repo, fill('seed-2', side='sell', quantity=3, effect='open', time='2026-10-01T14:01:00Z'))
    before_events, before_lots = repo.list_trade_events(), repo.list_position_lots()
    original = repo.upsert_trade_event
    calls = []
    def fail_second(event, **kwargs):
        calls.append(event.event_id)
        if len(calls) == 2:
            raise ValueError('injected second child failure')
        return original(event, **kwargs)
    monkeypatch.setattr(repo, 'upsert_trade_event', fail_second)
    deal = fill('cross', quantity=7, time='2026-10-01T14:02:00Z')
    failed = resolve_trade_deal(deal, repo=repo, state={}, apply_changes=True)
    assert failed.status == 'unresolved'
    assert 'injected second child failure' in failed.reason
    assert repo.list_trade_events() == before_events
    assert repo.list_position_lots() == before_lots
    monkeypatch.setattr(repo, 'upsert_trade_event', original)
    result = apply(repo, deal)
    assert [op.to_payload().get('contracts') for op in result.operations] == [2, 3, 2]
    assert result.action == 'open_close'
    from domain.domain.ledger import fee_fact_for_event, TradeEvent
    from domain.domain.fee_calc import estimate_futu_executed_option_fee
    rows = [r for r in repo.list_trade_events() if (r['raw_payload'].get('execution_input') or {}).get('external_execution_id') == 'cross']
    assert sum(Decimal(str(fee_fact_for_event(TradeEvent.from_dict(r)).amount)) for r in rows) == Decimal(str(estimate_futu_executed_option_fee('USD', Decimal('.62'), contracts=7, multiplier=100, is_sell=False).amount))


def test_pending_predecessor_and_equal_timestamp_are_not_opened(tmp_path):
    from src.application.trades.inbox import enqueue_trade_payload
    from src.application.trades.deal_identity import broker_deal_key
    repo = SQLiteOptionPositionsRepository(tmp_path / 'ledger.sqlite3')
    predecessor = fill('earlier', side='sell')
    enqueue_trade_payload(repo.db_path.with_name(repo.db_path.name + '.trade_intake_inbox.sqlite3'), payload=predecessor.execution_input, source='file',
                          broker_deal_key=broker_deal_key(predecessor), repo=repo)
    for time in ['2026-10-01T14:00:00Z', '2026-10-01T14:01:00Z']:
        result = resolve_trade_deal(fill('next', time=time), repo=repo, state={}, apply_changes=True)
        assert result.status == 'unresolved'
        assert result.reason == 'unknown_position_effect:earlier_or_same_time_pending_execution'
    assert not repo.list_trade_events()


def test_real_readonly_preview_and_mixed_crash_readback(tmp_path):
    from src.application.ledger.api import open_option_execution_preview_repo
    from src.application.trades.auto_intake import _readback_trade_receipt_result
    repo = SQLiteOptionPositionsRepository(tmp_path / 'ledger.sqlite3')
    apply(repo, fill('seed', side='sell', effect='open'))
    deal = fill('cross', quantity=2, time='2026-10-01T14:01:00Z')
    before = repo.db_path.read_bytes()
    preview = resolve_trade_deal(deal, repo=open_option_execution_preview_repo(repo.db_path), state={}, apply_changes=False)
    assert preview.action == 'open_close'
    assert preview.status == 'dry_run'
    assert repo.db_path.read_bytes() == before
    apply(repo, deal)
    recovered = _readback_trade_receipt_result(repo=repo, deal=deal, result={})
    assert recovered['receipt_kind'] == 'recorded', recovered
    assert recovered['action'] == 'open_close'
    assert sum(op.get('contracts_to_close') or 0 for op in recovered['operations']) == 1
    # Later fills may consume the residual open; recovery proves current projection, not historical quantity.
    apply(repo, fill('later', side='sell', time='2026-10-01T14:02:00Z', effect='close'))
    assert _readback_trade_receipt_result(repo=repo, deal=deal, result={})['receipt_kind'] == 'recorded'


def test_mixed_group_proof_rejects_conflicting_associations_and_missing_children(tmp_path):
    repo = SQLiteOptionPositionsRepository(tmp_path / 'ledger.sqlite3')
    apply(repo, fill('seed', side='sell', effect='open'))
    apply(repo, fill('cross', quantity=2, time='2026-10-01T14:01:00Z'))
    rows = [r for r in repo.list_trade_events() if (r['raw_payload'].get('execution_input') or {}).get('external_execution_id') == 'cross']
    bad = deepcopy(rows)
    bad[0]['raw_payload']['execution_input']['external_order_id'] = 'different-order'
    assert not ledger_execution_event_set_is_complete(bad)
    assert not ledger_execution_event_set_is_complete(rows[:1])
    bad = deepcopy(rows)
    bad[0]['raw_payload']['broker_deal_completion']['split_index'] = bad[1]['raw_payload']['broker_deal_completion']['split_index']
    assert not ledger_execution_event_set_is_complete(bad)


def test_optional_metadata_does_not_prevent_add_to_same_side(tmp_path):
    repo = SQLiteOptionPositionsRepository(tmp_path / 'ledger.sqlite3')
    first = fill('seed', effect='open')
    apply(repo, first)
    payload = fill('add', time='2026-10-01T14:01:00Z').execution_input
    payload.pop('external_order_id', None)
    payload.pop('external_order_namespace', None)
    payload.pop('evidence_refs', None)
    result = apply(repo, normalize_trade_deal(payload, allow_opend_refresh=False))
    assert result.action == 'open'
    assert sum(row['fields']['contracts_open'] for row in repo.list_position_lots()) == 2


def test_post_trade_quality_failure_or_concurrent_change_does_not_rewrite_ledger(tmp_path):
    from src.application.quality.service import check_post_trade_positions
    repo = SQLiteOptionPositionsRepository(tmp_path / 'ledger.sqlite3')
    apply(repo, fill('seed', effect='open'))
    before = repo.list_trade_events()
    class FailedAdapter:
        def fetch(self, **kwargs):
            raise TimeoutError('offline test')
    result = check_post_trade_positions(repo=repo, cfg={}, account='lx', market='us', adapter=FailedAdapter())
    assert result['status'] == 'unknown'
    assert repo.list_trade_events() == before
    class ConcurrentAdapter:
        def fetch(self, **kwargs):
            apply(repo, fill('next', effect='open', time='2026-10-01T14:01:00Z'))
            return None
    result = check_post_trade_positions(repo=repo, cfg={}, account='lx', market='us', adapter=ConcurrentAdapter())
    assert result == {'status': 'unknown', 'reason': 'ledger_changed_during_opend_check'}


def test_actual_order_fee_once_for_mixed_execution(tmp_path):
    from tests.test_order_fee_sync import _Provider
    from src.application.trades.order_fee_sync import sync_order_fees
    from domain.domain.ledger import TradeEvent, fee_fact_for_event
    repo = SQLiteOptionPositionsRepository(tmp_path / 'ledger.sqlite3')
    apply(repo, fill('seed', side='sell', effect='open'))
    deal = fill('cross', quantity=3, time='2026-10-01T14:01:00Z')
    apply(repo, deal)
    provider = _Provider(fee_amount='1.23', dealt_qty='3')
    result = sync_order_fees(repo, account='lx', start_ms=deal.trade_time_ms,
        end_exclusive_ms=deal.trade_time_ms + 1, provider=provider, apply=True,
        observed_at_ms=deal.trade_time_ms + 10, futu_account_id=deal.futu_account_id)
    assert result['actual_observation_count'] == 1, result
    rows = [r for r in repo.list_trade_events() if (r['raw_payload'].get('execution_input') or {}).get('external_execution_id') == 'cross']
    fees = [fee_fact_for_event(TradeEvent.from_dict(row)) for row in rows]
    assert all(fee.basis.value == 'actual' for fee in fees)
    assert sum(fee.amount for fee in fees) == Decimal('1.23')
    assert provider.fee_calls == 1


def test_intake_mixed_execution_has_one_receipt_and_full_source_quantity(tmp_path, monkeypatch):
    from tests.test_trade_receipt_claim_fence import _execution, _process, _receipt_callback
    from src.application.trades.inbox import read_trade_payload
    repo = SQLiteOptionPositionsRepository(tmp_path / 'ledger.sqlite3')
    payload = _execution()
    seed = normalize_trade_deal(payload, allow_opend_refresh=False)
    apply(repo, seed)
    payload = {**payload, 'external_execution_id': 'cross', 'side': 'buy', 'quantity': '3',
               'occurred_at_utc': '2026-09-07T02:31:00Z'}
    payload.pop('position_effect')
    sends = []
    callback = _receipt_callback(tmp_path, repo, monkeypatch, sends)
    result = _process(repo, tmp_path, 'worker', payload, callback)
    assert result['status'] == 'applied', result
    assert result['action'] == 'open_close'
    assert len(sends) == 1
    assert '先平仓后开仓' in sends[0]['message']
    assert '平仓 1 张 · 开仓 2 张' in sends[0]['message']
    assert '750.00' in sends[0]['message']
    _process(repo, tmp_path, 'worker', payload, callback)
    assert len(sends) == 1
    row = read_trade_payload(repo.db_path.with_name(repo.db_path.name + '.trade_intake_inbox.sqlite3'), inbox_id=result['inbox_id'])
    assert row['status'] == 'handled'
    assert row['receipt']['status'] == 'sent'
    assert len(repo.list_trade_events()) == 3


def test_operator_resume_retries_original_unknown_effect(tmp_path, monkeypatch):
    from tests.test_trade_receipt_claim_fence import _execution, _process, _receipt_callback
    from src.application.trades import auto_intake
    from src.application.trades.inbox import resume_trade_payload, read_trade_payload
    from src.application.trades.resolver import IntakeResolution
    repo = SQLiteOptionPositionsRepository(tmp_path / 'ledger.sqlite3')
    payload = _execution()
    payload.pop('position_effect')
    sends = []
    callback = _receipt_callback(tmp_path, repo, monkeypatch, sends)
    resolve = auto_intake.resolve_trade_deal
    monkeypatch.setattr(auto_intake, 'resolve_trade_deal', lambda deal, **kwargs: IntakeResolution(
        status='unresolved', action=None, reason='unknown_position_effect', deal_id=deal.deal_id,
        account='lx', operations=[], diagnostics={'retryable': False}))
    result = _process(repo, tmp_path, 'worker', payload, callback)
    path = repo.db_path.with_name(repo.db_path.name + '.trade_intake_inbox.sqlite3')
    assert not repo.list_trade_events()
    monkeypatch.setattr(auto_intake, 'resolve_trade_deal', resolve)
    assert _process(repo, tmp_path, 'worker', payload, callback)['status'] != 'applied'
    assert resume_trade_payload(path, inbox_id=result['inbox_id'], operator='offline-test', repo=repo)
    resumed = _process(repo, tmp_path, 'worker', payload, callback)
    assert resumed['status'] == 'applied', resumed
    assert resumed['action'] == 'open'
    assert len(repo.list_trade_events()) == 1
    assert read_trade_payload(path, inbox_id=result['inbox_id'])['status'] == 'handled'


def test_state_reconcile_preserves_every_allocation_lot(tmp_path):
    from src.application.trades.state import write_trade_intake_state, load_trade_intake_state
    from src.application.trades.state_reconcile import reconcile_trade_intake_state
    from src.application.trades.deal_identity import broker_deal_key
    repo = SQLiteOptionPositionsRepository(tmp_path / 'ledger.sqlite3')
    apply(repo, fill('seed-a', side='sell', effect='open'))
    apply(repo, fill('seed-b', side='sell', effect='open', time='2026-10-01T14:01:00Z'))
    deal = fill('cross', quantity=3, time='2026-10-01T14:02:00Z')
    result = apply(repo, deal)
    path = tmp_path / 'state.json'
    key = broker_deal_key(deal)
    write_trade_intake_state(path, {'unresolved_deal_ids': {key: {'account': 'lx', 'reason': 'unknown_position_effect'}}})
    reconcile_trade_intake_state(state_path=path, repo=repo, apply_changes=True)
    saved = load_trade_intake_state(path)['processed_deal_ids'][key]
    assert saved['action'] == 'open_close'
    assert set(saved['applied_record_ids']) == {op.lot_id for op in result.operations}


@pytest.mark.parametrize('side', ['buy', 'sell'])
@pytest.mark.parametrize('multiplier', [500, 1000])
def test_put_cross_zero_preserves_nonstandard_multiplier(tmp_path, side, multiplier):
    repo = SQLiteOptionPositionsRepository(tmp_path / 'ledger.sqlite3')
    apply(repo, fill('seed', side='sell' if side == 'buy' else 'buy', effect='open',
                     option_type='put', multiplier=multiplier))
    result = apply(repo, fill('cross', side=side, quantity=2, time='2026-10-01T14:01:00Z',
                              option_type='put', multiplier=multiplier))
    assert result.action == 'open_close'
    assert all(row['multiplier'] == multiplier for row in repo.list_trade_events())
    assert all(row['fields']['multiplier'] == multiplier for row in repo.list_position_lots())


@pytest.mark.parametrize('condition,expected', [('match', 'trusted'), ('mismatch', 'unavailable'),
                                               ('incomplete', 'unavailable'), ('stale', 'unavailable')])
def test_post_trade_quality_uses_shared_snapshot_verdict(tmp_path, condition, expected):
    from datetime import datetime, timedelta, timezone
    from src.application.quality.service import check_post_trade_positions
    from src.application.quality.opend_position_adapter import OpenDOptionSnapshot
    repo = SQLiteOptionPositionsRepository(tmp_path / 'ledger.sqlite3')
    apply(repo, fill('seed', effect='open'))
    before = repo.list_trade_events()
    observed = datetime.now(timezone.utc) - timedelta(hours=1 if condition == 'stale' else 0)
    snapshot = OpenDOptionSnapshot(account='lx', market='us', environment='REAL',
        account_fingerprint='sha256:' + 'a' * 64, observed_at_utc=observed.isoformat(),
        snapshot_id='offline', complete=condition != 'incomplete', refresh_cache=True,
        rows=[{'code': 'US.NVDA261106C100000', 'qty': 2 if condition == 'mismatch' else 1,
               'position_side': 'LONG', 'options_per_contract': 100, 'sec_type': 'DRVT'}], trading_days=[])
    class Adapter:
        def fetch(self, **kwargs):
            assert kwargs == {'cfg': {}, 'account': 'lx', 'market': 'us'}
            return snapshot
    result = check_post_trade_positions(repo=repo, cfg={}, account='lx', market='us', adapter=Adapter())
    assert result['status'] == expected, result
    if condition == 'mismatch':
        assert result['checks'][1]['observed']['observed_mismatch_count'] == 1
        assert result['checks'][1]['reason_code'] == 'POSITION_LIFECYCLE_COHERENT_READ_UNAVAILABLE'
    assert repo.list_trade_events() == before


def test_live_post_commit_check_releases_writer_and_cannot_fail_recording(tmp_path, monkeypatch):
    import threading
    from tests.test_trade_receipt_claim_fence import _execution
    from src.application.trades import auto_intake
    from src.application.ledger.api import with_sqlite_repo_writer_lock
    repo = SQLiteOptionPositionsRepository(tmp_path / 'ledger.sqlite3')
    acquired = threading.Event()
    calls = []
    def check(**kwargs):
        assert len(repo.list_trade_events()) == 1
        def acquire():
            with with_sqlite_repo_writer_lock(repo):
                acquired.set()
        thread = threading.Thread(target=acquire)
        thread.start()
        available = acquired.wait(2)
        thread.join(timeout=2)
        assert available, 'OpenD comparison must run outside the writer lock'
        calls.append(kwargs)
        return {'status': 'unknown', 'reason': 'offline-provider-unavailable'}
    monkeypatch.setattr('src.application.quality.service.check_post_trade_positions', check)
    result = auto_intake._process_payload(_execution(), repo=repo, state_path=tmp_path / 'state.json',
        audit_path=tmp_path / 'audit.jsonl', account_mapping={'123': 'lx'}, futu_account_ids=['123'],
        apply_changes=True, host='127.0.0.1', port=11111, allow_external_lookup=True,
        source='push', config={})
    assert result['status'] == 'applied', result
    assert result['position_quality_check']['status'] == 'unknown'
    assert len(calls) == 1
    assert len(repo.list_trade_events()) == 1


def test_file_batch_enqueues_before_chronological_application(tmp_path):
    import json
    from src.application.trades.file_intake import run_execution_file
    from src.application.trades.inbox import enqueue_trade_payload
    from src.application.trades.deal_identity import broker_deal_key_from_payload
    repo = SQLiteOptionPositionsRepository(tmp_path / 'ledger.sqlite3')
    earlier = fill('earlier', side='sell').execution_input
    later = fill('later', side='buy', quantity=2, time='2026-10-01T14:01:00Z').execution_input
    path = tmp_path / 'fills.jsonl'
    path.write_text(json.dumps(later) + '\n' + json.dumps(earlier) + '\n')
    prepared = []
    def prepare(payloads):
        for payload in payloads:
            enqueue_trade_payload(repo.db_path.with_name(repo.db_path.name + '.trade_intake_inbox.sqlite3'),
                payload=payload, source='file', broker_deal_key=broker_deal_key_from_payload(payload, account_mapping=None), repo=repo)
            prepared.append(payload)
    def process(payload, **kwargs):
        assert len(prepared) == 2
        return apply(repo, normalize_trade_deal(payload, allow_opend_refresh=False)).to_dict()
    result = run_execution_file(path, process_payload_fn=process,
        configured_accounts=[earlier['broker_account_ref']], prepare_payloads_fn=prepare, dry_run=False)
    assert [item['action'] for item in result['results']] == ['open_close', 'open']
    assert [item['line_number'] for item in result['results']] == [1, 2]
    assert len(repo.list_trade_events()) == 3


def test_saved_inbox_cli_preview_uses_current_holdings_without_writes(tmp_path, monkeypatch, capsys):
    from src.application.trades import auto_intake
    from src.application.trades.inbox import enqueue_trade_payload
    from src.application.trades.deal_identity import broker_deal_key
    from tests.test_trades_auto_intake_cli import _listener_source
    from src.interfaces.cli.main import main as public_main
    import json
    from pathlib import Path
    repo = SQLiteOptionPositionsRepository(tmp_path / 'ledger.sqlite3')
    apply(repo, fill('seed', side='sell', effect='open'))
    deal = fill('cross', quantity=2, time='2026-10-01T14:01:00Z')
    physical = deal.futu_account_id
    source = {**_listener_source(tmp_path, 'lx', 11111), 'account_mapping': {physical: 'lx'},
              'futu_account_ids': [physical]}
    cfg = {'enabled': True, 'mode': 'apply', 'state_path': tmp_path / 'state.json',
           'audit_path': tmp_path / 'audit.jsonl', 'status_path': tmp_path / 'status.json',
           'receipt': {'enabled': False}, 'backfill': {'enabled': False},
           'account_mapping': {physical: 'lx'}, 'futu_account_ids': [physical], 'sources': [source]}
    monkeypatch.setattr('src.application.trades.process_supervisor.run_trade_intake_process', auto_intake.main)
    monkeypatch.setattr(auto_intake, 'load_config', lambda **_: {})
    monkeypatch.setattr(auto_intake, 'resolve_trade_intake_config', lambda *_, **kwargs: {**cfg, 'mode': kwargs.get('mode_override') or 'apply'})
    monkeypatch.setattr(auto_intake, 'resolve_position_ledger_sqlite_path', lambda **_: repo.db_path)
    def unexpected(**kwargs):
        pytest.fail('preview must not open write-capable ledger or call provider')
    monkeypatch.setattr(auto_intake, 'open_position_ledger_from_runtime_config', unexpected)
    monkeypatch.setattr('src.application.quality.service.check_post_trade_positions', unexpected)
    path = repo.db_path.with_name(repo.db_path.name + '.trade_intake_inbox.sqlite3')
    row = enqueue_trade_payload(path, payload=deal.execution_input, source='push', broker_deal_key=broker_deal_key(deal), repo=repo)
    # mode=ro may create empty WAL/shared-memory coordination files on Linux.
    # Check persistent bytes and WAL content, excluding only SQLite read locks.
    shm = Path(str(repo.db_path) + '-shm')
    wal = Path(str(repo.db_path) + '-wal')
    before = {p: p.read_bytes() for p in tmp_path.rglob('*') if p.is_file() and p != shm}
    before.setdefault(wal, b'')
    assert public_main(['run', 'trade-intake', '--config', str(tmp_path / 'config.json'),
        '--runtime-root', str(tmp_path), '--inbox-id', row]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['action'] == 'open_close', result
    assert result['status'] == 'dry_run'
    after = {p: p.read_bytes() for p in tmp_path.rglob('*') if p.is_file() and p != shm}
    after.setdefault(wal, b'')
    assert after == before
