from dataclasses import replace
from decimal import Decimal

import pytest

from domain.domain.ledger import ContractKey, TradeEvent, fee_fact_for_event
from domain.domain.ledger.cash_facts import cash_facts_for_trade_event
from src.application.ledger.repository import SQLiteOptionPositionsRepository
from src.application.trades.order_fee_sync import recover_order_fee_targets, sync_order_fees


TIME = 1_789_100_000_000
TARGET = ('富途', 'lx', 'stock-account', 'stock-order')


def _repo(tmp_path, *, stock_override=None):
    repo = SQLiteOptionPositionsRepository(tmp_path / 'ledger.sqlite3')
    key = ContractKey.from_values(broker='富途', account='lx', underlying_symbol='0700.HK',
                                  option_type='put', position_side='short', strike=440,
                                  expiration_ymd='2026-09-11')
    fee_zero = {'basis': 'actual', 'amount': '0', 'source': 'option_assignment_lifecycle'}
    repo.upsert_trade_event(TradeEvent(
        event_id='open', event_type='open', event_time_ms=TIME - 1, contract_key=key,
        contracts=3, price=2, currency='HKD', source='broker', multiplier=100, lot_id='lot',
        raw_payload={'fee_provenance': fee_zero},
    ))
    for number, contracts in enumerate((1, 2)):
        repo.upsert_trade_event(TradeEvent(
            event_id=f'assignment-{number}', event_type='assignment', event_time_ms=TIME + number,
            contract_key=key, contracts=contracts, price=0, currency='HKD', source='broker',
            multiplier=100, target_lot_id='lot', raw_payload={
                'fee_provenance': fee_zero, 'futu_account_id': 'stock-account', 'order_id': 'option-order',
                'stock_settlement': {'side': 'buy', 'shares': contracts * 100, 'price': 440,
                                     'currency': 'HKD', 'futu_account_id': 'stock-account',
                                     'order_id': 'stock-order', 'external_order_namespace': 'futu.order',
                                     **(stock_override or {})},
            },
        ))
    return repo


class Provider:
    quantity = '300'
    currency = 'HKD'
    fee = '77.80'

    def __init__(self):
        self.calls = []

    def fetch_terminal_orders(self, **kwargs):
        self.calls.append(kwargs)
        return {'stock-order': {'status': 'terminal_with_fill', 'dealt_qty': self.quantity,
                                'currency': self.currency}}, {}

    def fetch_order_fees(self, **kwargs):
        self.calls.append(kwargs)
        return {'stock-order': {'fee_amount': self.fee, 'fee_details': {'total': self.fee}}}, {}


def _sync(repo, provider, *, apply=False):
    return sync_order_fees(repo, account='lx', provider=provider, apply=apply,
                           observed_at_ms=TIME + 100, target_identity=TARGET)


def _assignments(repo):
    return [TradeEvent.from_dict(row) for row in repo.list_trade_events() if row['event_type'] == 'assignment']


def test_settlement_order_fee_allocates_once_and_preserves_option_cash(tmp_path):
    repo = _repo(tmp_path)
    before = repo.list_trade_events()
    provider = Provider()
    targets = recover_order_fee_targets(repo, account='lx', allowed_futu_account_ids=['stock-account'])
    assert targets['targets'] == [TARGET]
    preview = _sync(repo, provider)
    assert preview['migration']['event_count'] == 2
    assert repo.list_trade_events() == before
    result = _sync(repo, provider, apply=True)
    assert result['migration']['status_counts'] == {'committed': 1}
    events = _assignments(repo)
    assert [Decimal(event.raw_payload['stock_settlement']['fees']) for event in events] == [Decimal('25.933333'), Decimal('51.866667')]
    assert sum(next(fact.amount for fact in cash_facts_for_trade_event(event) if fact.fact_kind == 'stock_settlement_fee_cash') for event in events) == Decimal('-77.8')
    assert all(fee_fact_for_event(event).amount == 0 for event in events)
    assert all(event.raw_payload['order_id'] == 'option-order' for event in events)
    assert all(event.raw_payload['cash_conversions']['stock_settlement_fee_cash']['native_currency'] == 'HKD' for event in events)
    assert all(call['futu_account_id'] == 'stock-account' and call['order_ids'] == ['stock-order'] for call in provider.calls)
    after = repo.list_trade_events()
    assert _sync(repo, provider, apply=True)['selected_order_count'] == 0
    assert repo.list_trade_events() == after
    with repo._connect() as conn:
        assert conn.execute('SELECT count(*) FROM broker_fee_enrichment_audit').fetchone()[0] == 2


@pytest.mark.parametrize('override,reason', [
    ({'order_id': None}, 'order_identity_missing'),
    ({'futu_account_id': None}, 'order_identity_missing'),
    ({'futu_account_id': 'wrong'}, 'stock_settlement_account_conflict'),
    ({'currency': 'USD'}, 'stock_settlement_currency_conflict'),
    ({'shares': 100.5}, 'stock_settlement_quantity_invalid'),
    ({'external_order_namespace': 'other.order'}, 'unsupported_order_namespace'),
])
def test_settlement_identity_and_quantity_fail_closed(tmp_path, override, reason):
    repo = _repo(tmp_path, stock_override=override)
    before = repo.list_trade_events()
    selected = recover_order_fee_targets(repo, account='lx')
    assert selected['targets'] == []
    assert reason in {row['reason'] for row in selected['issues']}
    assert repo.list_trade_events() == before


@pytest.mark.parametrize('attribute,value,reason', [
    ('quantity', '100', 'stock_settlement_quantity_mismatch'),
    ('currency', 'USD', 'order_currency_mismatch'),
    ('fee', None, 'order_fee_invalid'),
])
def test_settlement_provider_evidence_mismatch_does_not_write(tmp_path, attribute, value, reason):
    repo = _repo(tmp_path)
    provider = Provider()
    setattr(provider, attribute, value)
    before = repo.list_trade_events()
    result = _sync(repo, provider, apply=True)
    assert reason in result['reason_counts']
    assert repo.list_trade_events() == before


def test_settlement_fee_write_unit_rolls_back_all_split_rows(tmp_path, monkeypatch):
    import src.application.ledger.order_fee_migration as migration
    repo = _repo(tmp_path)
    before = repo.list_trade_events()
    original = migration._insert_audit

    def fail_second(conn, **kwargs):
        if kwargs['change'].event_id == 'assignment-1':
            raise ValueError('injected audit failure')
        return original(conn, **kwargs)

    monkeypatch.setattr(migration, '_insert_audit', fail_second)
    result = _sync(repo, Provider(), apply=True)
    assert result['migration']['status_counts'] == {'rolled_back': 1}
    assert repo.list_trade_events() == before


def test_source_allocation_stays_valid_and_order_total_not_repeated(tmp_path):
    from domain.domain.lifecycle_allocation import allocate_stock_settlement, validate_stock_settlement_allocation_group
    repo = SQLiteOptionPositionsRepository(tmp_path / 'source.sqlite3')
    key = ContractKey.from_values(broker='富途', account='lx', underlying_symbol='0700.HK',
                                  option_type='put', position_side='short', strike=440,
                                  expiration_ymd='2026-09-11')
    # Two distinct stock fills on one order; first fill splits across two option lots.
    groups = []
    for group_index, quantities in enumerate(((1, 1), (1,))):
        source = {'side': 'buy', 'shares': 100 * sum(quantities), 'price': 440,
                  'currency': 'HKD', 'futu_account_id': 'stock-account', 'order_id': 'stock-order',
                  'source_event_id': f'stock-fill-{group_index}'}
        targets = [{'target_lot_id': f'lot-{group_index}-{index}', 'contracts_allocated': quantity,
                    'multiplier': 100} for index, quantity in enumerate(quantities)]
        settlements = allocate_stock_settlement(source, targets)
        group = []
        for index, target in enumerate(targets):
            lot = target['target_lot_id']
            repo.upsert_trade_event(TradeEvent(
                event_id=f'open-{lot}', event_type='open', event_time_ms=TIME - 1,
                contract_key=key, contracts=1, price=2, currency='HKD', source='broker', multiplier=100,
                lot_id=lot, raw_payload={'fee_provenance': {'basis': 'actual', 'amount': '0', 'source': 'test'}},
            ))
            event = TradeEvent(
                event_id=f'assignment-{lot}', event_type='assignment', event_time_ms=TIME + group_index,
                contract_key=key, contracts=1, price=0, currency='HKD', source='broker', multiplier=100,
                target_lot_id=lot, raw_payload={
                    'case_id': f'case-{group_index}', 'evidence_id': f'evidence-{group_index}',
                    'target_lot_id': lot, 'stock_settlement': settlements[lot],
                    'stock_settlement_source': source,
                    'fee_provenance': {'basis': 'actual', 'amount': '0', 'source': 'option_assignment_lifecycle'},
                },
            )
            group.append(event.event_id)
            repo.upsert_trade_event(event)
        groups.append(group)
    result = _sync(repo, Provider(), apply=True)
    assert result['migration']['status_counts'] == {'committed': 1}
    after = {event.event_id: event for event in _assignments(repo)}
    totals = []
    for group in groups:
        source = validate_stock_settlement_allocation_group([after[event_id] for event_id in group])
        totals.append(Decimal(str(source['fees'])))
    assert sum(totals) == Decimal('77.8')
    assert totals == [Decimal('51.866667'), Decimal('25.933333')]
    assert _sync(repo, Provider(), apply=True)['selected_order_count'] == 0


def test_void_after_plan_is_rechecked_inside_write_transaction(tmp_path, monkeypatch):
    import src.application.ledger.order_fee_migration as migration
    repo = _repo(tmp_path)
    before = [event.to_dict() for event in _assignments(repo)]
    original = migration.with_sqlite_repo_transaction

    def void_before_transaction(candidate, fn, **kwargs):
        event = _assignments(repo)[0]
        repo.upsert_trade_event(replace(event, event_id='void-assignment', event_type='void',
                                        target_event_id=event.event_id, raw_payload={}))
        return original(candidate, fn, **kwargs)

    monkeypatch.setattr(migration, 'with_sqlite_repo_transaction', void_before_transaction)
    result = _sync(repo, Provider(), apply=True)
    assert result['migration']['status_counts'] == {'rolled_back': 1}
    assert [event.to_dict() for event in _assignments(repo)] == before


def test_one_invalid_split_blocks_whole_stock_order(tmp_path):
    repo = _repo(tmp_path)
    events = _assignments(repo)
    bad = events[1]
    # Simulate a persisted legacy inconsistency without touching the valid first split.
    raw = {**bad.raw_payload, 'stock_settlement': {**bad.raw_payload['stock_settlement'], 'shares': 201}}
    with repo._connect() as conn:
        import json
        conn.execute('UPDATE trade_events SET event_json = ? WHERE event_id = ?',
                     (json.dumps(replace(bad, raw_payload=raw).to_dict()), bad.event_id))
        conn.commit()
    assert recover_order_fee_targets(repo, account='lx')['targets'] == []
    provider = Provider()
    provider.quantity = '100'
    result = _sync(repo, provider, apply=True)
    assert result['selected_order_count'] == 0
    assert provider.calls == []
