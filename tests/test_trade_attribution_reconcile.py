from copy import deepcopy
import json
from threading import Event

import pytest

from src.application.trades import attribution as mod
from src.application.ledger.api import read_trade_attribution_snapshot
from src.application.ledger import trade_attribution as ledger_attribution
from test_trade_attribution_view import _writable_call_scope, _call_capacity_observation


def _setup(tmp_path, monkeypatch, *, actions=False):
    # Historical fills predate policy enablement and require no automatic action.
    repo, config = _writable_call_scope(tmp_path, monkeypatch, fill_time=3000 if actions else 2000)
    observed = []
    evidence_calls = []
    views = []
    cached = []
    def evidence(*args, **kwargs):
        evidence_calls.append(kwargs['now_ms'])
        return {'complete': True, 'exposures': []}
    def observe(**kwargs):
        observed.append(1)
        return {}
    original = mod.build_trade_attribution_view
    def build(rows, **kwargs):
        result = original(rows, **kwargs)
        views.append(deepcopy(result))
        return result
    def cache(_path, *, execution_key, result):
        cached.append(deepcopy(result))
        return 0
    monkeypatch.setattr(mod, 'read_attribution_combo_evidence', evidence)
    monkeypatch.setattr('src.application.wheel.capacity.observe_trade_attribution_capacity', observe)
    monkeypatch.setattr(mod, 'build_trade_attribution_view', build)
    monkeypatch.setattr('src.application.trades.inbox.cache_trade_attribution_result', cache)
    args = dict(config=config, account='lx', market='us', runtime_root=tmp_path,
                inbox_path=tmp_path/'inbox.sqlite3', combo_mode='confirm')
    return repo, args, observed, evidence_calls, views, cached, cache


def test_unchanged_batch_builds_one_view_and_next_call_refreshes(tmp_path, monkeypatch):
    repo, args, observed, evidence, views, cached, _ = _setup(tmp_path, monkeypatch)
    snapshots = []
    original = ledger_attribution.read_trade_attribution_snapshot
    def counted(*a, **kw):
        snapshots.append(1)
        return original(*a, **kw)
    monkeypatch.setattr(ledger_attribution, "read_trade_attribution_snapshot", counted)
    first = mod.reconcile_trade_attribution_account(repo, **args)
    assert len(snapshots) == 1
    assert first['checked'] >= 2 and first['errors'] == [] and first['next_cursor'] == ''
    assert len(views) == len(observed) == len(evidence) == 1
    expected = {row['execution_key']: mod.attribution_result_payload(row) for row in views[0]['rows']}
    assert all(row == expected[row['execution_key']] for row in cached)
    monkeypatch.setattr(mod.time, 'time', lambda: 65)
    mod.reconcile_trade_attribution_account(repo, **args)
    assert len(snapshots) == 2
    assert len(views) == len(observed) == len(evidence) == 2
    assert views[1]['rows'][0]['evaluated_at_ms'] == 65000


@pytest.mark.parametrize('change', ['fee', 'new_event'])
def test_concurrent_full_snapshot_change_rebuilds_batch(tmp_path, monkeypatch, change):
    repo, args, observed, evidence, views, cached, cache = _setup(tmp_path, monkeypatch)
    before = repo.list_trade_events()
    def mutate(path, **kwargs):
        result = cache(path, **kwargs)
        if len(cached) == 1:
            event = deepcopy(next(e for e in before if e['event_type'] == 'open'))
            if change == 'fee':
                event['raw_payload']['fee_evidence'] = {'amount': 1.25, 'source': 'broker'}
                with repo._writer_connection(begin_immediate=True) as conn:
                    conn.execute('UPDATE trade_events SET event_json=? WHERE event_id=?',
                                 (json.dumps(event), event['event_id']))
            else:
                from domain.domain.ledger import ContractKey, TradeEvent
                from domain.domain.trade_execution import execution_identity_from_input
                from src.application.ledger.writer import persist_trade_event_objects_atomically
                event['event_id'] = 'concurrent-fill'
                event['lot_id'] = 'concurrent-lot'
                event['raw_payload']['execution_input']['external_execution_id'] = 'concurrent'
                event['raw_payload']['execution_id'] = execution_identity_from_input(event['raw_payload']['execution_input'])
                persist_trade_event_objects_atomically(repo, [TradeEvent(
                    event_id='concurrent-fill', event_type='open', event_time_ms=3500,
                    contract_key=ContractKey.from_values(broker='futu', account='lx', underlying_symbol='NVDA',
                        option_type='put', strike=100, expiration_ymd='2026-12-18'),
                    contracts=1, price=1, multiplier=100, currency='USD', source='test', lot_id='concurrent-lot',
                    raw_payload={'side': 'sell', 'execution_input': event['raw_payload']['execution_input'],
                        'execution_id': event['raw_payload']['execution_id'], 'multiplier_source': 'payload'})])
        return result
    monkeypatch.setattr('src.application.trades.inbox.cache_trade_attribution_result', mutate)
    result = mod.reconcile_trade_attribution_account(repo, **args)
    assert result['errors'] == []
    assert len(views) == 2 and len(evidence) == 2 and len(observed) == 1
    if change == 'fee':
        assert [e['event_id'] for e in repo.list_trade_events()] == [e['event_id'] for e in before]
    else:
        assert len(repo.list_trade_events()) == len(before) + 1


def test_real_action_rechecks_transaction_and_is_idempotent_next_batch(tmp_path, monkeypatch):
    repo, args, observed, evidence, views, cached, _ = _setup(tmp_path, monkeypatch, actions=True)
    before = repo.list_trade_events()
    first = mod.reconcile_trade_attribution_account(repo, **args)
    assert first['linked'] == 1 and first['errors'] == []
    assert len(views) > 1  # Transactional check and committed readback remain real.
    assert len(repo.list_trade_events()) == len(before) + 1
    second = mod.reconcile_trade_attribution_account(repo, **args)
    assert second['linked'] == 0 and second['errors'] == []
    assert len(repo.list_trade_events()) == len(before) + 1


def test_time_expiry_between_batch_and_action_cannot_write(tmp_path, monkeypatch):
    from src.application.wheel.capacity import trade_attribution_capacity_check
    repo, args, *_ = _setup(tmp_path, monkeypatch, actions=True)
    monkeypatch.setattr(mod, 'trade_attribution_capacity_check', trade_attribution_capacity_check)
    monkeypatch.setattr('src.application.wheel.capacity.observe_trade_attribution_capacity',
                        lambda **_: _call_capacity_observation())
    original = mod.apply_trade_attribution
    def expired(*a, **kw):
        monkeypatch.setattr(mod.time, 'time', lambda: 65)
        return original(*a, **kw)
    monkeypatch.setattr(mod, 'apply_trade_attribution', expired)
    before = read_trade_attribution_snapshot(repo, account='lx', market='us')
    result = mod.reconcile_trade_attribution_account(repo, **args)
    assert result['linked'] == 0 and result['errors']
    assert read_trade_attribution_snapshot(repo, account='lx', market='us') == before


@pytest.mark.parametrize('cancel_at', ['before', 'evidence', 'observation', 'view', 'cache'])
def test_cancellation_stops_at_phase_boundary_and_preserves_cursor(tmp_path, monkeypatch, cancel_at):
    repo, args, observed, evidence, views, cached, cache = _setup(tmp_path, monkeypatch)
    stop = Event()
    if cancel_at == 'before':
        stop.set()
    elif cancel_at == 'cache':
        def stop_cache(*a, **kw):
            result = cache(*a, **kw)
            stop.set()
            return result
        monkeypatch.setattr('src.application.trades.inbox.cache_trade_attribution_result', stop_cache)
    else:
        owner, name = (mod, 'read_attribution_combo_evidence') if cancel_at == 'evidence' else (
            mod, 'build_trade_attribution_view') if cancel_at == 'view' else (
            __import__('src.application.wheel.capacity', fromlist=['observe_trade_attribution_capacity']), 'observe_trade_attribution_capacity')
        original = getattr(owner, name)
        def stopping(*a, **kw):
            result = original(*a, **kw)
            stop.set()
            return result
        monkeypatch.setattr(owner, name, stopping)
    result = mod.reconcile_trade_attribution_account(repo, **args, stop_event=stop)
    assert result['checked'] == (1 if cancel_at == 'cache' else 0)
    if cancel_at in {'before', 'evidence'}:
        assert observed == [] and views == []
    if cancel_at == 'observation':
        assert views == []
    if cancel_at == 'cache':
        assert result['next_cursor'] == cached[0]['execution_key']


def test_cancel_during_changed_snapshot_does_not_start_evidence_refresh(tmp_path, monkeypatch):
    repo, args, observed, evidence, views, cached, _ = _setup(tmp_path, monkeypatch)
    stop = Event()
    original = ledger_attribution.read_trade_attribution_snapshot
    original_evidence = mod.read_attribution_combo_evidence
    def mutate_during_evidence(*a, **kw):
        result = original_evidence(*a, **kw)
        with repo._writer_connection(begin_immediate=True) as conn:
            conn.execute('''INSERT INTO trade_attribution_policy_enablings
                SELECT broker, 'different-account-id', environment, account, market, policy_version,
                       effective_from_ms, created_at_ms, actor, 'concurrent-policy', request_hash
                FROM trade_attribution_policy_enablings''')
        return result
    monkeypatch.setattr(mod, 'read_attribution_combo_evidence', mutate_during_evidence)
    reads = []
    def cancelling(*a, **kw):
        rows = original(*a, **kw)
        reads.append(1)
        if len(reads) == 2:
            stop.set()
        return rows
    monkeypatch.setattr(ledger_attribution, 'read_trade_attribution_snapshot', cancelling)
    result = mod.reconcile_trade_attribution_account(repo, **args, stop_event=stop)
    assert result['checked'] == 0 and result['next_cursor'] == ''
    assert len(evidence) == len(observed) == 1 and views == cached == []


def test_failed_action_invalidates_even_when_snapshot_is_unchanged(tmp_path, monkeypatch):
    repo, args, observed, evidence, views, cached, _ = _setup(tmp_path, monkeypatch)
    original = mod.build_trade_attribution_view
    injected = []
    def actionable(*a, **kw):
        view = original(*a, **kw)
        if not injected:
            first = min((r for r in view['rows'] if r['execution_key']), key=lambda r: r['execution_key'])
            first['selected_candidate_id'] = 'wheel:fixture'
            injected.append(1)
        return view
    monkeypatch.setattr(mod, 'build_trade_attribution_view', actionable)
    monkeypatch.setattr(mod, 'apply_trade_attribution', lambda *a, **kw: (_ for _ in ()).throw(RuntimeError('uncertain attempt')))
    result = mod.reconcile_trade_attribution_account(repo, **args)
    assert len(result['errors']) == 1 and result['errors'][0]['error'] == 'RuntimeError'
    assert len(views) == len(evidence) == 2 and len(observed) == 1
