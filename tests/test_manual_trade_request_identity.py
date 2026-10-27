from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier

import pytest

from src.application.ledger import api, manual_trades
from src.application.ledger.commands import persist_manual_adjust_event_with_ledger
from src.application.ledger.repository import SQLiteOptionPositionsRepository
from src.application.positions import workflows


@pytest.fixture
def repo(tmp_path):
    return SQLiteOptionPositionsRepository(tmp_path / 'ledger.sqlite3')


def _open(repo, account='account-a'):
    return api.record_manual_position_open(
        repo, broker='futu', account=account, symbol='NVDA', option_type='put',
        side='short', contracts=2, currency='USD', strike=100, multiplier=100,
        expiration_ymd='2030-06-21', premium_per_share=2.5, opened_at_ms=1000,
    ).result.lot_id


def _adjust(repo, lot_id, **patch):
    return api.record_manual_position_adjust(repo, lot_id=lot_id, **patch).result


def _close(lot_id, **patch):
    return dict(lot_id=lot_id, contracts_to_close=1, close_price=0.5,
                close_reason='manual_buy_to_close', **patch)


def _reopen(repo):
    return SQLiteOptionPositionsRepository(repo.db_path)


def _assert_replay(repo, lot_id, **expected):
    from src.application.ledger.writer import rebuild_position_lots_from_trade_events

    before = repo.get_record_fields(lot_id)
    rebuild_position_lots_from_trade_events(repo)
    assert repo.get_record_fields(lot_id) == before
    for key, value in expected.items():
        assert before[key] == value


def test_adjust_public_roundtrip_retry_restart_and_account_isolation(repo):
    lot_id = _open(repo)
    other = _open(repo, 'account-b')
    first = _adjust(repo, lot_id, premium_per_share=3.1)
    retry = _adjust(repo, lot_id, premium_per_share=3.1)
    assert first.created and not retry.created
    assert retry.event_id == first.event_id
    assert repo.get_record_fields(lot_id)['premium_open'] == '3.1'
    assert _adjust(repo, lot_id, premium_per_share=2.5).created
    repo = _reopen(repo)
    repeated = _adjust(repo, lot_id, premium_per_share=3.1)
    assert repeated.created
    assert repeated.event_id != first.event_id
    assert repo.get_record_fields(lot_id)['premium_open'] == '3.1'
    assert not _adjust(repo, lot_id, premium_per_share=3.1).created
    assert repo.get_record_fields(other)['premium_open'] == '2.5'
    assert _adjust(repo, other, premium_per_share=3.1).created
    assert repo.count_trade_events() == 6
    _assert_replay(repo, lot_id, premium_open='3.1')


@pytest.mark.parametrize('batch', [False, True], ids=['single', 'batch'])
@pytest.mark.parametrize('field,initial,target,stored', [
    ('premium_per_share', 2.5, 3.1, 'premium_open'),
    ('contracts', 2, 3, 'contracts_opened'),
    ('strike', 100, 110, 'strike'),
    ('expiration_ymd', '2030-06-21', '2030-07-19', 'expiration_ymd'),
    ('multiplier', 100, 200, 'multiplier'),
    ('opened_at_ms', 1000, 1500, 'opened_at_ms'),
])
def test_adjust_all_economic_fields_roundtrip_with_increasing_time(repo, batch, field, initial, target, stored):
    lot_id = _open(repo)

    def apply(value, timestamp):
        patch = {field: value, 'as_of_ms': timestamp}
        if batch:
            return api.record_manual_position_adjustments(repo, [{'record_id': lot_id, **patch}])[0].result
        return persist_manual_adjust_event_with_ledger(repo, lot_id=lot_id, **patch).result

    first = apply(target, 2000)
    assert first.created
    retry = apply(target, 3000)
    assert not retry.created and retry.event_id == first.event_id
    assert apply(initial, 4000).created
    final = apply(target, 5000)
    assert final.created and final.event_id != first.event_id
    assert not apply(target, 6000).created
    fields = repo.get_record_fields(lot_id)
    actual = fields['contract_key'][stored] if stored in {'strike', 'expiration_ymd'} else fields[stored]
    if isinstance(target, (int, float)):
        assert Decimal(str(actual)) == Decimal(str(target))
    else:
        assert actual == target
    assert repo.count_trade_events() == 4
    _assert_replay(repo, lot_id)


def test_adjust_concurrent_identical_transition_is_applied_once(repo):
    lot_id = _open(repo)
    _adjust(repo, lot_id, premium_per_share=3.1)
    _adjust(repo, lot_id, premium_per_share=2.5)
    barrier = Barrier(2)

    def apply(_):
        local = _reopen(repo)
        barrier.wait(timeout=10)
        return _adjust(local, lot_id, premium_per_share=3.1)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(apply, range(2)))
    assert sum(result.created for result in results) == 1
    assert len({result.event_id for result in results}) == 1
    assert repo.count_trade_events() == 4
    _assert_replay(repo, lot_id, premium_open='3.1')


@pytest.mark.parametrize('entry', ['preview', 'apply', 'writer', 'workflow-preview', 'workflow-apply'])
def test_voided_close_is_rejected_by_every_entry_after_restart(repo, entry):
    lot_id = _open(repo)
    other = _open(repo, 'account-b')
    close = _close(lot_id)
    first = api.record_manual_position_close(repo, **close)
    retry = api.record_manual_position_close(repo, **close)
    assert first.result.created and not retry.result.created
    assert first.result.event_id == retry.result.event_id
    assert repo.get_record_fields(lot_id)['contracts_open'] == 1
    api.record_trade_event_void(repo, event_id=first.result.event_id, reason='incorrect close')
    repo = _reopen(repo)
    assert repo.get_record_fields(lot_id)['contracts_open'] == 2
    before = repo.list_trade_events()
    with pytest.raises(ValueError, match='manual close request.*voided'):
        if entry == 'preview':
            api.preview_manual_position_close(repo, **close)
        elif entry == 'apply':
            api.record_manual_position_close(repo, **close)
        elif entry == 'writer':
            manual_trades.persist_manual_close_event(repo, fields=repo.get_record_fields(lot_id), **close)
        else:
            workflows.execute_manual_close(repo, dry_run=entry.endswith('preview'), **close)
    assert repo.list_trade_events() == before
    assert api.record_manual_position_close(repo, **_close(other)).result.created
    assert repo.get_record_fields(lot_id)['contracts_open'] == 2
    different = api.record_manual_position_close(repo, **{**close, 'close_price': 0.6})
    assert different.result.created
    assert different.result.event_id != first.result.event_id
    _assert_replay(repo, lot_id, contracts_open=1)


def test_voided_close_concurrent_requests_cannot_report_applied(repo):
    lot_id = _open(repo)
    first = api.record_manual_position_close(repo, **_close(lot_id))
    api.record_trade_event_void(repo, event_id=first.result.event_id, reason='incorrect close')
    barrier = Barrier(2)

    def apply(_):
        local = _reopen(repo)
        barrier.wait(timeout=10)
        with pytest.raises(ValueError, match='manual close request.*voided'):
            workflows.execute_manual_close(local, dry_run=False, **_close(lot_id))

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(apply, range(2)))
    assert repo.count_trade_events() == 3
    _assert_replay(repo, lot_id, contracts_open=2)


@pytest.fixture
def cli(repo, tmp_path, monkeypatch):
    from src.interfaces.cli import option_positions

    data_config = tmp_path / 'data.json'
    data_config.write_text('{}')
    monkeypatch.setattr(option_positions, 'resolve_option_positions_repo', lambda **_: (data_config, repo))
    monkeypatch.setattr(option_positions, 'ledger_store_payload', lambda *_: {'sqlite_path': str(repo.db_path)})
    return lambda *args: option_positions.main(['--data-config', str(data_config), *args])


def test_adjust_cli_roundtrip_reports_only_applied_target(repo, cli, capsys):
    lot_id = _open(repo)
    for premium in ('3.1', '2.5', '3.1', '3.1'):
        assert cli('adjust-lot', '--record-id', lot_id, '--premium-per-share', premium, '--apply', '--confirm') == 0
        assert '[DONE] adjusted' in capsys.readouterr().out
        assert repo.get_record_fields(lot_id)['premium_open'] == premium
    assert repo.count_trade_events() == 4


@pytest.mark.parametrize('apply', [False, True], ids=['preview', 'apply'])
def test_voided_close_cli_never_prints_success(repo, cli, capsys, apply):
    lot_id = _open(repo)
    first = api.record_manual_position_close(repo, **_close(lot_id))
    api.record_trade_event_void(repo, event_id=first.result.event_id, reason='incorrect close')
    args = ['buy-close', '--record-id', lot_id, '--contracts', '1', '--close-price', '0.5']
    args += ['--apply', '--confirm'] if apply else ['--dry-run']
    with pytest.raises(SystemExit, match='manual close request.*voided'):
        cli(*args)
    assert '[DONE]' not in capsys.readouterr().out
    assert repo.count_trade_events() == 3
    assert repo.get_record_fields(lot_id)['contracts_open'] == 2


def test_legacy_adjustment_retry_preserves_event_and_actual_patch(repo):
    from domain.domain.ledger import ContractKey, TradeEvent
    from src.application.ledger.writer import persist_trade_event_object

    lot_id = _open(repo)
    fields = repo.get_record_fields(lot_id)
    # Frozen pre-fix identity from the account-a / NVDA fixture, not a new revision hash.
    legacy = TradeEvent(
        event_id='manual-adjust-27eadbfb147e52dc', event_type='adjust', event_time_ms=2000,
        contract_key=ContractKey.from_values(**fields['contract_key']), contracts=0, price=0,
        multiplier=100, currency='USD', source='cli_manual_adjust', target_lot_id=lot_id,
        raw_payload={'mode': 'manual_adjust', 'record_id': lot_id,
                     'patch': {'premium': 3.1, 'last_action_at': 2000}},
    )
    persist_trade_event_object(repo, legacy)
    before = repo.list_trade_events()
    retry = api.record_manual_position_adjust(_reopen(repo), lot_id=lot_id, premium_per_share=3.1)
    assert not retry.result.created and retry.result.event_id == legacy.event_id
    assert retry.patch.to_dict() == legacy.raw_payload['patch']
    assert repo.list_trade_events() == before
    _adjust(repo, lot_id, premium_per_share=2.5)
    assert _adjust(repo, lot_id, premium_per_share=3.1).created
    _assert_replay(repo, lot_id, premium_open='3.1')


def test_adjust_void_restores_revision_but_does_not_absorb_new_transition(repo):
    lot_id = _open(repo)
    first = _adjust(repo, lot_id, premium_per_share=3.1)
    api.record_trade_event_void(repo, event_id=first.event_id, reason='incorrect adjustment')
    assert repo.get_record_fields(lot_id)['premium_open'] == '2.5'
    repeated = _adjust(_reopen(repo), lot_id, premium_per_share=3.1)
    assert repeated.created and repeated.event_id != first.event_id
    assert not _adjust(repo, lot_id, premium_per_share=3.1).created
    _assert_replay(repo, lot_id, premium_open='3.1')


def test_adjust_partial_close_retry_and_compound_metadata_roundtrip(repo):
    from src.application.ledger.read_model import load_position_lot_records

    lot_id = _open(repo)
    first = _adjust(repo, lot_id, premium_per_share=3.1)
    api.record_manual_position_close(repo, **_close(lot_id))
    retry = _adjust(repo, lot_id, premium_per_share=3.1)
    assert not retry.created and retry.event_id == first.event_id
    assert repo.get_record_fields(lot_id)['contracts_closed'] == 1
    patches = [
        {'premium_per_share': 4, 'contracts': 3, 'strategy': 'sell_put',
         'strategy_snapshot': {'strategy_family': 'sell_put', 'strategy_profile': 'return_first'}},
        {'premium_per_share': 2.5, 'contracts': 2, 'strategy': 'sell_put',
         'strategy_snapshot': {'strategy_family': 'sell_put', 'strategy_profile': 'quality_first'}},
    ]
    for patch in (patches[0], patches[1], patches[0]):
        result = _adjust(repo, lot_id, **patch)
        assert result.created
        assert not _adjust(repo, lot_id, **patch).created
        stored = load_position_lot_records(repo)[0]['fields']
        assert stored['strategy_snapshot'] == {'strategy': 'sell_put', **patch['strategy_snapshot']}
        fields = repo.get_record_fields(lot_id)
        assert fields['contracts_open'] == patch['contracts'] - 1
        assert fields['contracts_closed'] == 1
    _assert_replay(repo, lot_id, premium_open='4', contracts_open=2, contracts_closed=1)


@pytest.mark.parametrize('batch', [False, True], ids=['single', 'batch'])
@pytest.mark.parametrize('targets', [(3.1, 4.2), (4.2, 3.1), (5, 6)])
def test_adjust_different_targets_with_same_preflight_time_follow_commit_order(repo, monkeypatch, batch, targets):
    from threading import Event
    from src.application.ledger import commands

    lot_id = _open(repo)
    barrier = Barrier(2)
    first_committed = Event()
    owner_name = 'persist_manual_adjust_events' if batch else 'persist_manual_adjust_event'
    original = getattr(commands, owner_name)
    preflight_times = []

    def interleave(local, *args, **kwargs):
        patch = args[0][0] if batch else kwargs
        value = patch['premium_per_share']
        preflight_times.append(patch['as_of_ms'])
        barrier.wait(timeout=10)
        if value == targets[1]:
            assert first_committed.wait(timeout=10)
        try:
            return original(local, *args, **kwargs)
        finally:
            if value == targets[0]:
                first_committed.set()

    monkeypatch.setattr(commands, owner_name, interleave)
    monkeypatch.setattr('src.application.ledger.preflight.now_ms', lambda: 2_000_000_000_000)

    def apply(value):
        local = _reopen(repo)
        if batch:
            try:
                return api.record_manual_position_adjustments(local, [
                    {'record_id': lot_id, 'premium_per_share': value},
                ])[0]
            except ValueError as exc:
                return exc
        return api.record_manual_position_adjust(local, lot_id=lot_id, premium_per_share=value)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(apply, targets))
    assert preflight_times == [2_000_000_000_000] * 2
    if batch:
        # Preserve the batch's whole-snapshot CAS; a fresh preview can retry safely.
        assert isinstance(results[1], ValueError)
        assert 'target fields changed since preflight' in str(results[1])
        assert repo.get_record_fields(lot_id)['premium_open'] == str(targets[0])
        assert repo.count_trade_events() == 2
        monkeypatch.setattr(commands, owner_name, original)
        results[1] = apply(targets[1])
    assert all(result.result.created for result in results)
    assert repo.get_record_fields(lot_id)['premium_open'] == str(targets[1])
    events = {row['event_id']: row for row in repo.list_trade_events()}
    times = [events[result.result.event_id]['event_time_ms'] for result in results]
    assert times[0] < times[1]
    for result, timestamp in zip(results, times):
        assert result.patch.to_dict()['last_action_at'] == timestamp
        assert result.ledger_preflight.event_time_ms == timestamp
    _assert_replay(repo, lot_id, premium_open=str(targets[1]))
