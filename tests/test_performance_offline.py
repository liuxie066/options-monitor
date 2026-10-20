from copy import deepcopy
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
import json

import pytest

from domain.domain.ledger import ContractKey, TradeEvent
from domain.domain.performance import (
    OptionFeeCashEvent, PeriodRequest, calculate_option_performance,
    cashflow_input_digest, normalize_performance_period,
)
from src.application.performance.service import OptionPerformanceReadError, build_option_period_performance


def ms(day):
    return int(datetime.fromisoformat(day + 'T00:00:00+08:00').timestamp() * 1000)


def event(identity='open', *, account='lx', broker='futu', at='2026-08-20', kind='open', target=None):
    return TradeEvent(
        event_id=identity, event_type=kind, event_time_ms=ms(at),
        contract_key=ContractKey.from_values(broker=broker, account=account, underlying_symbol='NVDA',
            option_type='put', strike=100, expiration_ymd='2026-09-30'),
        contracts=1, price=Decimal('2') if kind == 'open' else Decimal('3'), currency='USD',
        source='offline-test', multiplier=100, fees=Decimal('0'),
        lot_id=identity if kind == 'open' else None, target_lot_id=target,
        raw_payload={'side': 'sell' if kind == 'open' else 'buy',
                     'close_type': 'buy_to_close' if kind == 'close' else None,
                     'fee_provenance': {'basis': 'actual', 'amount': 0, 'source': 'archive'}},
    )


def period():
    return normalize_performance_period(PeriodRequest(period='month', month='2026-09'),
                                        report_now_ms=ms('2026-10-02'))


def evidence(items, *, available=None):
    return {('trade:' + item.event_id if isinstance(item, TradeEvent) else 'fee:' + item.fee_id): {
        'available_at_ms': available if available is not None else (
            item.event_time_ms if isinstance(item, TradeEvent) else item.effective_at_ms),
        'source': 'archive://test/captured-version', 'payload_sha256': cashflow_input_digest(item),
    } for item in items}


class Repo:
    def __init__(self, events):
        self.events = events

    def list_position_lots(self):
        raise AssertionError("performance must project events instead of reading lots")

    def list_trade_events(self):
        return [item.to_dict() for item in self.events]


@pytest.mark.parametrize('broker', ['futu', 'FUTU', '富途'])
def test_offline_and_live_share_period_and_legacy_metrics(broker):
    events = [event(), event('close', at='2026-09-05', kind='close', target='open'),
              event('sy', account='sy', at='2026-09-03')]
    offline = calculate_option_performance(events, period=period(), accounts=[' LX '], broker=broker)
    live = build_option_period_performance(Repo(events), period=period(), config_key='us',
        configured_accounts=['lx', 'sy'], account='lx', broker=broker)
    assert offline.bundle['period_option_net_cashflow']['by_currency']['USD']['total']['amount'] == -300
    assert live['period_option_net_cashflow']['by_currency']['USD']['total']['amount'] == -300
    for key in ('option_net_cashflow', 'option_return', 'sell_option_win_rate', 'buy_option_win_rate'):
        assert json.loads(json.dumps(offline.bundle[key], default=float)) == live[key]
    assert offline.bundle['cashflow_source']['visibility'] == 'corrected_snapshot'


@pytest.mark.parametrize('accounts', [[], [' '], 'lx', [None], [1]])
def test_offline_requires_explicit_nonempty_scope(accounts):
    with pytest.raises(ValueError, match='accounts'):
        calculate_option_performance([], period=period(), accounts=accounts)


def test_no_matching_broker_is_valid_empty_not_alias_failure():
    out = calculate_option_performance([event()], period=period(), accounts=['lx'], broker='ibkr')
    assert out.bundle['period_option_net_cashflow']['by_currency'] == {}


def test_duplicates_have_distinct_ingestion_contracts():
    opening = event(at='2026-09-01')
    result = calculate_option_performance([opening, opening], period=period(), accounts=['lx'])
    assert result.bundle['period_option_net_cashflow']['by_currency']['USD']['total']['amount'] == 200
    assert result.bundle['cashflow_source']['input_count'] == 1
    with pytest.raises(ValueError, match='conflicting'):
        calculate_option_performance([opening, replace(opening, price=Decimal('4'))], period=period(), accounts=['lx'])
    with pytest.raises(OptionPerformanceReadError) as caught:
        build_option_period_performance(Repo([opening, opening]), period=period(), config_key='us', configured_accounts=['lx'])
    assert caught.value.reason_codes == ('ledger_tuple_invalid',)


def test_visibility_requires_bound_frozen_record_and_correct_cutoff():
    opening = event(at='2026-09-01')
    cutoff = ms('2026-10-01')
    kwargs = dict(period=period(), accounts=['lx'], knowledge_cutoff_ms=cutoff)
    with pytest.raises(ValueError, match='every input'):
        calculate_option_performance([opening], **kwargs)
    valid = evidence([opening])
    result = calculate_option_performance([opening], **kwargs, input_evidence=valid)
    assert result.bundle['cashflow_source']['visibility'] == 'point_in_time_archive'
    changed = replace(opening, price=Decimal('4'))
    with pytest.raises(ValueError, match='mismatch'):
        calculate_option_performance([changed], **kwargs, input_evidence=valid)
    with pytest.raises(ValueError, match='not available'):
        calculate_option_performance([opening], **kwargs, input_evidence=evidence([opening], available=cutoff + 1))
    with pytest.raises(ValueError, match='extends beyond'):
        calculate_option_performance([opening], period=period(), accounts=['lx'], knowledge_cutoff_ms=cutoff - 2,
                                     input_evidence=valid)


@pytest.mark.parametrize('field', ['fee', 'fx'])
def test_late_fee_or_fx_observation_cannot_hide_under_old_trade_time(field):
    opening = event(at='2026-09-01')
    raw = deepcopy(opening.raw_payload)
    if field == 'fee':
        raw['fee_provenance']['provider_observed_at_ms'] = ms('2026-10-03')
    else:
        raw['cash_conversions'] = {'option_trade_cash_gross': {'observed_at_ms': ms('2026-10-03')}}
    opening = replace(opening, raw_payload=raw)
    with pytest.raises(ValueError, match='not available'):
        calculate_option_performance([opening], period=period(), accounts=['lx'],
            knowledge_cutoff_ms=ms('2026-10-01'), input_evidence=evidence([opening]))


def test_late_fee_revision_restates_original_period_without_mutating_earlier_snapshot():
    opening = event(at='2026-09-01')
    original = deepcopy(opening.to_dict())
    before = calculate_option_performance([opening], period=period(), accounts=['lx'],
        knowledge_cutoff_ms=ms('2026-10-01'), input_evidence=evidence([opening]))
    fee = OptionFeeCashEvent(fee_id='fee-correction', source_event_id='open', effective_at_ms=opening.event_time_ms,
        currency='USD', amount=Decimal('-3'), basis='actual', source='archive://fee', replaces_attached_fee=True)
    after = calculate_option_performance([opening], period=period(), accounts=['lx'], fee_cash_events=[fee],
        knowledge_cutoff_ms=ms('2026-10-04'), input_evidence=evidence([opening, fee], available=ms('2026-10-03')))
    assert before.bundle['period_option_net_cashflow']['by_currency']['USD']['total']['amount'] == 200
    assert after.bundle['period_option_net_cashflow']['by_currency']['USD']['total']['amount'] == 197
    assert before.bundle['cashflow_source']['snapshot_hash'] != after.bundle['cashflow_source']['snapshot_hash']
    assert opening.to_dict() == original
    assert before.bundle['option_return'] == after.bundle['option_return']
    with pytest.raises(ValueError, match='not available'):
        calculate_option_performance([opening], period=period(), accounts=['lx'], fee_cash_events=[fee],
            knowledge_cutoff_ms=ms('2026-10-01'), input_evidence=evidence([opening, fee], available=ms('2026-10-03')))


def test_future_control_is_not_admitted_to_earlier_archive():
    opening = event(at='2026-09-01')
    void = replace(opening, event_id='void', event_type='void', event_time_ms=ms('2026-10-03'),
                   target_event_id=opening.event_id, lot_id=None, contracts=0)
    with pytest.raises(ValueError, match='not available'):
        calculate_option_performance([opening, void], period=period(), accounts=['lx'],
            knowledge_cutoff_ms=ms('2026-10-01'), input_evidence=evidence([opening, void]))
    later = calculate_option_performance([opening, void], period=period(), accounts=['lx'])
    assert later.bundle['period_option_net_cashflow']['by_currency'] == {}


def test_offline_calculation_does_not_read_io(monkeypatch):
    import builtins
    import socket
    import sqlite3

    def forbidden(*args, **kwargs):
        raise AssertionError('offline calculation attempted I/O')

    opening = event(at='2026-09-01')
    window = period()
    monkeypatch.setattr(builtins, 'open', forbidden)
    monkeypatch.setattr(socket, 'socket', forbidden)
    monkeypatch.setattr(sqlite3, 'connect', forbidden)
    result = calculate_option_performance([opening], period=window, accounts=['lx'])
    assert result.bundle['period_option_net_cashflow']['by_currency']['USD']['total']['amount'] == 200


def test_adjusted_effective_time_must_be_visible_at_cutoff():
    opening = event(at='2026-09-01')
    opening = replace(opening, contract_key=replace(opening.contract_key, expiration_ymd='2026-12-31'))
    for adjusted_day, rejected in [('2026-10-02', True), ('2026-09-03', False)]:
        adjustment = replace(opening, event_id='adjust', event_type='adjust',
            event_time_ms=ms('2026-09-02'), lot_id=None, target_lot_id='open', contracts=0,
            raw_payload={'patch': {'opened_at': ms(adjusted_day)}})
        inputs = [opening, adjustment]
        kwargs = dict(period=period(), accounts=['lx'], knowledge_cutoff_ms=ms('2026-10-01'),
                      input_evidence=evidence(inputs))
        if rejected:
            with pytest.raises(ValueError, match='effective cash event.*cutoff'):
                calculate_option_performance(inputs, **kwargs)
        else:
            result = calculate_option_performance(inputs, **kwargs)
            assert result.bundle['period_option_net_cashflow']['by_currency']['USD']['total']['amount'] == 200


def test_fx_quote_time_must_not_exceed_archived_availability_for_trade_or_fee():
    from domain.domain.performance.cash_conversion import cash_conversion_id, cash_conversion_identity

    opening = replace(event(at='2026-09-30'), event_time_ms=ms('2026-09-30') + 12 * 3600000)
    at = opening.event_time_ms
    for fee_input in (False, True):
        fee = OptionFeeCashEvent('charge', 'open', at, 'USD', Decimal('-2'), 'actual', 'archive')
        for future in (True, False):
            identity = cash_conversion_identity(
                cash_fact_id=fee.fact_id if fee_input else 'option_trade_cash_gross:open',
                native_amount=fee.amount if fee_input else 200, native_currency='USD', fx_rate=7,
                amount_cny=-14 if fee_input else 1400, rate_source_id='archived-fx', effective_at_ms=at,
            )
            conversion = {**identity, 'schema_version': 'cash_conversion.v1', 'status': 'observed',
                'quote_currency': 'CNY', 'method': 'booking_fx_snapshot', 'rate_source': 'fixture',
                'observed_at_ms': at, 'rate_timestamp': '2026-10-01T00:00:00+08:00' if future
                else '2026-09-30T12:00:00+08:00', 'conversion_id': cash_conversion_id(identity)}
            trade = opening if fee_input else replace(opening, raw_payload={**opening.raw_payload,
                'cash_conversions': {'option_trade_cash_gross': conversion}})
            fees = [replace(fee, cash_conversion=conversion)] if fee_input else []
            kwargs = dict(period=period(), accounts=['lx'], fee_cash_events=fees,
                          knowledge_cutoff_ms=ms('2026-10-01') - 1,
                          input_evidence=evidence([trade, *fees], available=at))
            if future:
                with pytest.raises(ValueError, match='not available'):
                    calculate_option_performance([trade], **kwargs)
            else:
                result = calculate_option_performance([trade], **kwargs)
                fact = next(f for f in result.cashflow_facts if f['fact_id'] == identity['cash_fact_id'])
                assert fact['cny_amount'] == (-14 if fee_input else 1400)
