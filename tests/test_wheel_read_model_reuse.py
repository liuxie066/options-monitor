from copy import deepcopy
from dataclasses import replace

import pytest

from domain.domain.wheel import build_legacy_wheel_event
from src.application.ledger import assigned_stock_projection as assigned
from src.application.wheel import read_model as model
from tests.test_performance_assignment import _assign_put, _trade


def _mixed_rows():
    events, wheel_events = [], []
    for account, symbol in [('lx', 'NVDA'), ('lx', '0700.HK'), ('sy', 'NVDA')]:
        key = f'{account}-{symbol}'
        contract = replace(_trade().contract_key, account=account, underlying_symbol=symbol)
        currency = 'HKD' if symbol.endswith('.HK') else 'USD'
        opened = _trade(event_id=f'open-{key}', lot_id=f'lot-{key}', contract_key=contract, currency=currency)
        assigned_event = _assign_put(event_id=f'assign-{key}', target_lot_id=f'lot-{key}',
                                     contract_key=contract, currency=currency)
        call = _trade(event_id=f'call-{key}', lot_id=f'call-lot-{key}', event_time_ms=3000,
                      contract_key=replace(contract, option_type='call'), currency=currency)
        events.extend({**item.to_dict(), "position_side": "short"} for item in (opened, assigned_event, call))
        if account == 'lx':
            wheel_events.append(build_legacy_wheel_event(
                event_id=f'wheel-{key}',
                account=account, lot_id=f'assigned-stock-assign-{key}', event_type='wheel_started',
                occurred_at_ms=2000, recorded_at_ms=2001, source_trade_event_id=f'assign-{key}',
                payload={'request_id': f'assignment:assign-{key}'},
            ))
    return {'trade_events': events, 'account_wheel_events': wheel_events}


@pytest.mark.parametrize('market', [None, 'us', 'hk'])
@pytest.mark.parametrize('readiness', [None, {'monitoring_gate': 'enabled'},
                                      {'monitoring_gate': 'config_mismatch', 'reason_code': 'policy_drift'}])
def test_pair_matches_separate_views_and_shares_base(monkeypatch, market, readiness):
    rows = _mixed_rows()
    original = deepcopy(rows)
    kwargs = dict(account='lx', as_of_ms=4000)
    expected = (model.build_wheel_read_model_from_rows(rows, **kwargs, market=market,
                                                      monitoring_readiness=readiness),
                model.build_wheel_read_model_from_rows(rows, **kwargs))
    project, branches = assigned.project_trade_event_log, model.project_wheel_branches
    calls = {'project': 0, 'branches': 0}
    def counted_project(events):
        calls['project'] += 1
        return project(events)
    def counted_branches(*args):
        calls['branches'] += 1
        return branches(*args)
    monkeypatch.setattr(assigned, 'project_trade_event_log', counted_project)
    monkeypatch.setattr(model, 'project_wheel_branches', counted_branches)
    actual = model.build_wheel_read_model_with_capacity_from_rows(
        rows, **kwargs, market=market, monitoring_readiness=readiness)
    assert actual == expected
    assert calls == {'project': 1, 'branches': 1}
    assert rows == original
    assert {row['symbol'] for row in actual[1]['wheel_branches']} == {'NVDA', '0700.HK'}
    assert all(row['account'] == 'lx' for row in actual[1]['wheel_branches'])
    assert all(row['phase'] == 'linkage_unresolved' for row in actual[1]['wheel_branches'])


@pytest.mark.parametrize('side', [0, 1])
def test_pair_nested_mutation_does_not_escape_and_next_observation_is_fresh(side):
    rows = _mixed_rows()
    original = deepcopy(rows)
    pair = model.build_wheel_read_model_with_capacity_from_rows(rows, account='lx', as_of_ms=4000, market='us')
    other = deepcopy(pair[1 - side])
    expected = deepcopy(pair)
    pair[side]['wheel_branches'][0]['coverage']['test_mutation'] = True
    pair[side]['batches'][0]['active_option_contracts'].append({'lot_id': 'mutated'})
    pair[side]['assigned_stock_projection']['assigned_stock_lots'][0]['shares_remaining'] = -1
    assert pair[1 - side] == other
    assert rows == original
    assert model.build_wheel_read_model_with_capacity_from_rows(
        rows, account='lx', as_of_ms=4000, market='us') == expected
    earlier = model.build_wheel_read_model_with_capacity_from_rows(rows, account='lx', as_of_ms=1500, market='us')
    assert earlier[1]['wheel_branches'] == []
    rows['account_wheel_events'] = []
    changed = model.build_wheel_read_model_with_capacity_from_rows(rows, account='lx', as_of_ms=4000, market='us')
    assert changed != expected


@pytest.mark.parametrize('rows,account,instant,market', [
    ({}, '', 4000, 'us'), ({}, 'lx', 0, 'us'), ({}, 'lx', 'invalid', 'us'),
    ({}, 'lx', 4000, 'invalid'),
    ({'trade_events': [{'event_id': 'bad', 'event_type': 'open', 'event_time_ms': 'bad-time'}]},
     'lx', 4000, 'invalid'),
])
def test_pair_preserves_single_view_first_error(rows, account, instant, market):
    with pytest.raises((ValueError, TypeError)) as expected:
        model.build_wheel_read_model_from_rows(rows, account=account, as_of_ms=instant, market=market)
    with pytest.raises(type(expected.value)) as actual:
        model.build_wheel_read_model_with_capacity_from_rows(rows, account=account, as_of_ms=instant, market=market)
    assert str(actual.value) == str(expected.value)


def test_pair_preserves_selection_fallback(monkeypatch):
    row = _trade().to_dict()
    row.update(event_time_ms=0, trade_time_ms=6000)
    rows = {'trade_events': [row]}
    expected = model.build_wheel_read_model_from_rows(rows, account='lx', as_of_ms=4000)
    calls = []
    original = assigned.project_trade_event_log
    monkeypatch.setattr(assigned, 'project_trade_event_log',
                        lambda events: (calls.append(len(events)), original(events))[1])
    pair = model.build_wheel_read_model_with_capacity_from_rows(rows, account='lx', as_of_ms=4000, market=None)
    assert pair == (expected, expected)
    assert calls == [1, 0]


@pytest.mark.parametrize("paired", [False, True])
def test_historical_wheel_keeps_later_void_of_repaired_terminal(paired):
    opened = _trade()
    expired = _trade(event_id="old-expiry", event_type="expire_close", event_time_ms=2000,
                     price=0, lot_id=None, target_lot_id="lot-put", raw_payload={})
    assignment = _assign_put(event_time_ms=2500)
    void = _trade(event_id="void-old-expiry", event_type="void", event_time_ms=5000,
                  contracts=0, price=0, lot_id=None, target_event_id="old-expiry", raw_payload={})
    rows = {"trade_events": [item.to_dict() for item in (opened, expired, assignment, void)]}
    before = deepcopy(rows)
    read = (model.build_wheel_read_model_with_capacity_from_rows if paired
            else model.build_wheel_read_model_from_rows)
    result = read(rows, account="lx", as_of_ms=4000, market="us")
    views = result if paired else (result,)
    for view in views:
        assert [lot["shares_remaining"] for lot in view["assigned_stock_projection"]["assigned_stock_lots"]] == [100]
    assert rows == before


def test_historical_wheel_does_not_include_void_of_future_event():
    opened = _trade(event_time_ms=6000)
    void = _trade(event_id="void-future-open", event_type="void", event_time_ms=7000,
                  contracts=0, price=0, lot_id=None, target_event_id=opened.event_id, raw_payload={})
    out = model.build_wheel_read_model_from_rows(
        {"trade_events": [opened.to_dict(), void.to_dict()]}, account="lx", as_of_ms=4000)
    assert out["assigned_stock_projection"]["assigned_stock_lots"] == []


def test_historical_wheel_retained_void_still_checks_account_identity():
    opened = _trade()
    assigned = _assign_put()
    void = _trade(event_id="invalid-void", event_type="void", event_time_ms=5000,
                  contracts=0, price=0, lot_id=None, target_event_id=assigned.event_id,
                  contract_key=replace(assigned.contract_key, account="sy"), raw_payload={})
    with pytest.raises(ValueError, match="target_event_contract_mismatch"):
        model.build_wheel_read_model_from_rows(
            {"trade_events": [item.to_dict() for item in (opened, assigned, void)]},
            account="lx", as_of_ms=4000)
