from __future__ import annotations

import json

from src.application.ledger import api
from src.application.positions.assignment_period_report import _affected
from src.application.trades.inbox import read_stock_sell_observations
from tests.test_assigned_stock_query_inputs import _inbox, _sell
from tests.test_performance_assignment import _assign_put, _trade


def rows(sales):
    return {'trade_events': [_trade().to_dict(), _assign_put().to_dict()],
            'account_assigned_stock_events': sales}


def sale(instant, shares=100, **overrides):
    return {'stock_event_id': 'confirmed-sale', 'event_type': 'sale', 'account': 'lx',
            'broker': '富途', 'symbol': 'NVDA', 'currency': 'USD', 'side': 'sell',
            'target_stock_lot_id': 'assigned-stock-assign-put', 'shares': shares,
            'price': 100, 'fees': 0, 'trade_time_ms': instant,
            'fee_provenance': {'basis': 'actual', 'source': 'fixture'}, **overrides}


def test_candidates_use_canonical_inventory_at_disposal_not_current_closed_state():
    data = rows([sale(4000)])
    pending = {'account': 'lx', 'broker': '富途', 'symbol': 'NVDA', 'currency': 'USD',
               'shares': 40, 'price': 100, 'trade_time_ms': 5000}
    # Both queries see the same currently closed batch. Only the earlier sale
    # could have consumed this batch's inventory.
    before, after = api.qualify_assigned_stock_sell_observations(
        data, [{**pending, 'trade_time_ms': 3000}, pending], as_of_ms=6000,
    )
    current = api.project_assigned_stock_lifecycle_from_rows(data, as_of_ms=6000)['assigned_stock_lots'][0]
    assert current['shares_remaining'] == 0
    assert before['candidate_stock_lot_ids'] == ['assigned-stock-assign-put']
    assert after['candidate_stock_lot_ids'] == []
    assert _affected(current, before)
    assert not _affected(current, after)
    from domain.domain.assigned_stock import assigned_put_remaining_economics
    assert assigned_put_remaining_economics(current, quantity_usable=not _affected(current, after))['stock_unrealized_pnl'] == '0'
    assert assigned_put_remaining_economics(current, quantity_usable=not _affected(current, before))['stock_unrealized_pnl'] is None


def test_sparse_stale_state_resolves_only_via_identical_full_accepted_source(tmp_path):
    path = tmp_path / 'inbox.sqlite3'
    _inbox(path, [(_sell(), 'applied')])
    state = tmp_path / 'output_shared/state/trade_intake/lx/state.json'
    state.parent.mkdir(parents=True)
    initial = read_stock_sell_observations(path, futu_account_mapping={'test-lx': 'lx'}, accounts=['lx'])['observations'][0]
    state.write_text(json.dumps({'unresolved_deal_ids': {'old': {
        'account': 'lx', 'action': 'assigned_stock_sale', 'status': 'unresolved',
        'source_deal_id': initial['source_deal_id'], 'futu_account_id': initial['futu_account_id'],
        'broker_deal_key': initial['execution_id'],
        'diagnostics': {'candidates': [{'symbol': 'NVDA', 'stock_lot_id': 'assigned-stock-assign-put'}]},
    }}, 'failed_deal_ids': {}}))
    observed = read_stock_sell_observations(path, futu_account_mapping={'test-lx': 'lx'},
                                           accounts=['lx'], runtime_root=tmp_path)['observations']
    sold = sale(initial['trade_time_ms'], shares=40, source_deal_id=initial['source_deal_id'],
                futu_account_id=initial['futu_account_id'], execution_id=initial['execution_id'])
    now = initial['trade_time_ms'] + 1000
    qualified = api.qualify_assigned_stock_sell_observations(rows([sold]), observed, as_of_ms=now)
    assert all(item['resolved'] for item in qualified)
    assert qualified[1]['resolution_source_ids'] == [initial['source_id']]
    # An economics revision sharing the identity cannot clear the sparse record.
    conflicted = api.qualify_assigned_stock_sell_observations(
        rows([sold]), [*observed, {**observed[0], 'price': 101}], as_of_ms=now,
    )
    assert conflicted[0]['resolved']
    assert not conflicted[1]['resolved']
    assert not conflicted[2]['resolved']
    # A same-deal-ID observation in another namespace is a different execution.
    different = [{**observed[0], 'execution_id': 'execution:v1:other-namespace'}, observed[1]]
    assert not any(item['resolved'] for item in api.qualify_assigned_stock_sell_observations(
        rows([sold]), different, as_of_ms=now,
    ))


def test_structurally_valid_sale_rejected_by_projection_does_not_resolve():
    observation = {'account': 'lx', 'futu_account_id': 'test-lx', 'source_deal_id': 'd',
                   'broker': '富途', 'symbol': 'NVDA', 'currency': 'USD',
                   'shares': 101, 'price': 100, 'trade_time_ms': 3000}
    rejected = sale(3000, shares=101, source_deal_id='d', futu_account_id='test-lx')
    result = api.qualify_assigned_stock_sell_observations(rows([rejected]), [observation], as_of_ms=4000)
    assert not result[0]['resolved']


def test_effective_sale_resolution_keeps_account_identity_and_economics_isolated():
    recorded = sale(3000, shares=40, source_deal_id='d', futu_account_id='test-lx')
    source = {**recorded, 'source_kind': 'inbox'}
    cases = [source, {**source, 'shares': 41}, {**source, 'account': 'sy'},
             {**source, 'futu_account_id': None}, {**source, 'price': 101}]
    qualified = api.qualify_assigned_stock_sell_observations(rows([recorded]), cases, as_of_ms=4000)
    assert [r['resolved'] for r in qualified] == [True, False, False, False, False]


def test_sparse_state_with_conflicting_source_revision_hash_stays_unresolved():
    recorded = sale(3000, shares=40, source_deal_id='d', futu_account_id='test-lx')
    complete = {**recorded, 'source_kind': 'inbox', 'source_id': 'full',
                'economic_payload_hash': 'new'}
    sparse = {'source_kind': 'state', 'account': 'lx', 'futu_account_id': 'test-lx',
              'source_deal_id': 'd', 'broker': '富途', 'economic_payload_hash': 'old'}
    result = api.qualify_assigned_stock_sell_observations(rows([recorded]), [complete, sparse], as_of_ms=4000)
    assert result[0]['resolved']
    assert not result[1]['resolved']


def test_same_execution_conflict_keeps_closed_sale_targets_affected():
    recorded = sale(4000, source_deal_id='d', futu_account_id='test-lx')
    data = rows([recorded])
    current = api.project_assigned_stock_lifecycle_from_rows(data, as_of_ms=6000)['assigned_stock_lots'][0]
    assert current['shares_remaining'] == 0
    variants = [{**recorded, 'shares': 40}, {**recorded, 'price': 101},
                {**recorded, 'trade_time_ms': 5000}]
    qualified = api.qualify_assigned_stock_sell_observations(data, variants, as_of_ms=6000)
    from domain.domain.assigned_stock import assigned_put_remaining_economics
    for conflict in qualified:
        assert not conflict['resolved']
        assert conflict['candidate_stock_lot_ids'] == [current['stock_lot_id']]
        assert _affected(current, conflict)
        assert assigned_put_remaining_economics(current, quantity_usable=False)['remaining_pnl_with_put'] is None
    # A later disposal with a distinct execution cannot consume old closed inventory.
    different = {**recorded, 'source_deal_id': 'other', 'shares': 40, 'trade_time_ms': 5000}
    for unrelated in (different, {**variants[2], 'account': 'sy'},
                      {**variants[2], 'futu_account_id': 'other-physical'}):
        q = api.qualify_assigned_stock_sell_observations(data, [unrelated], as_of_ms=6000)[0]
        assert q['candidate_stock_lot_ids'] == []
        assert not _affected(current, q)


def test_same_millisecond_disposal_does_not_assume_after_full_sale():
    for sale_at in (2000, 4000):
        data = rows([sale(sale_at, source_deal_id='known', futu_account_id='test-lx')])
        current = api.project_assigned_stock_lifecycle_from_rows(data, as_of_ms=6000)['assigned_stock_lots'][0]
        assert current['shares_remaining'] == 0
        unknown = {'account': 'lx', 'futu_account_id': 'test-lx', 'source_deal_id': 'other',
                   'broker': '富途', 'symbol': 'NVDA', 'currency': 'USD',
                   'shares': 40, 'price': 100, 'trade_time_ms': sale_at}
        q = api.qualify_assigned_stock_sell_observations(data, [unknown], as_of_ms=6000)[0]
        assert q['candidate_stock_lot_ids'] == [current['stock_lot_id']]
        assert _affected(current, q)


def test_future_revision_is_conflict_evidence_not_future_economic_application():
    recorded = sale(4000, source_deal_id='d', futu_account_id='test-lx')
    data = rows([recorded])
    current = api.project_assigned_stock_lifecycle_from_rows(data, as_of_ms=6000)['assigned_stock_lots'][0]
    for stamp in (None, 7000):
        revised = {**recorded, 'shares': 40, 'symbol': 'FUTU', 'trade_time_ms': stamp}
        conflict = api.qualify_assigned_stock_sell_observations(data, [revised], as_of_ms=6000)[0]
        assert conflict['accepted_sale_conflict']
        assert conflict['accepted_sale_conflict_lot_ids'] == [current['stock_lot_id']]
        assert not conflict['resolved']
        assert _affected(current, conflict)
        assert current['shares_remaining'] == 0  # no future sale is applied
    distinct = {**recorded, 'source_deal_id': 'other', 'trade_time_ms': 7000}
    future = api.qualify_assigned_stock_sell_observations(data, [distinct], as_of_ms=6000)[0]
    assert not future['accepted_sale_conflict']


def test_report_requested_symbol_does_not_hide_identity_linked_revision(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from src.application.positions import assignment_period_report as report

    recorded = sale(4000, source_deal_id='d', futu_account_id='test-lx')
    data = rows([recorded])
    data['trade_events'] = [api.trade_event_application_payload(event) for event in data['trade_events']]
    monkeypatch.setattr(report.ledger_api, 'read_assigned_stock_query_inputs', lambda repo: data)
    captured = []
    revision = {**recorded, 'symbol': 'FUTU', 'shares': 40, 'trade_time_ms': 7000}

    def observations(*args, **kwargs):
        captured.append(kwargs['symbols'])
        return {'observations': [revision], 'coverage': 'complete', 'source_history': 'unknown'}

    monkeypatch.setattr(report, 'read_stock_sell_observations', observations)
    result = report.build_assignment_period_report(
        SimpleNamespace(path=tmp_path/'ledger.sqlite3'),
        {'assignment_year': 1970, 'symbol': 'NVDA', 'account': 'lx', 'refresh_quotes': False},
        cfg={'accounts': ['lx']}, repo_base=lambda: tmp_path, now_ms=lambda: 6000,
        refresh_assigned_stock_quotes=lambda *a, **k: None,
    )
    assert captured == [None]
    assert result['rows'][0]['quantity_status'] == 'pending'
    assert result['rows'][0]['remaining_pnl_with_put'] is None
    assert result['unresolved_stock_sales'][0]['symbol'] == 'FUTU'
