from decimal import Decimal

import pytest

from domain.domain.assigned_stock import assigned_put_remaining_economics


def _lot():
    return {
        'shares_opened': 100, 'shares_sold': 40, 'shares_remaining': 60,
        'stock_cost_basis_total': '1010', 'put_open_premium_exact': '120',
        'assigned_stock_realized_pnl': '-48',
        'source_option_fee_allocations': [
            {'component': 'option_open', 'basis': 'actual', 'amount': '20'},
            {'component': 'option_assignment', 'basis': 'actual', 'amount': '0'},
        ],
        'fee_evidence': [
            {'component': 'assignment_stock_fee', 'basis': 'actual', 'amount': '10'},
            {'component': 'put_open_option_fee', 'basis': 'actual', 'amount': '20'},
            {'component': 'put_assignment_option_fee', 'basis': 'actual', 'amount': '0'},
            {'component': 'stock_sale_fee', 'basis': 'actual', 'amount': '4'},
        ],
    }


def test_partial_sale_includes_only_stock_and_stock_fees_once():
    result = assigned_put_remaining_economics(_lot(), price='8')
    for name, expected in {'stock_unrealized_pnl': '-126', 'stock_realized_pnl': '-48'}.items():
        assert Decimal(result[name]) == Decimal(expected)
    assert Decimal(result['stock_unrealized_pnl']) + Decimal(result['stock_realized_pnl']) == -174
    assert not any('put' in key or 'premium' in key for key in result)


def test_missing_put_fee_does_not_suppress_stock_and_conflict_does():
    lot = _lot()
    lot['source_option_fee_allocations'][0]['basis'] = 'missing'
    result = assigned_put_remaining_economics(lot, price='8')
    assert Decimal(result['stock_unrealized_pnl']) == -126
    assert result == assigned_put_remaining_economics(_lot(), price='8')
    assert result['reasons'] == []
    assert assigned_put_remaining_economics(lot, price='8', quantity_usable=False)['stock_unrealized_pnl'] is None


def test_fully_sold_needs_no_price_or_put_fee_but_conflict_takes_precedence():
    lot = _lot() | {'shares_sold': 100, 'shares_remaining': 0, 'fee_evidence': []}
    result = assigned_put_remaining_economics(lot)
    assert Decimal(result['stock_unrealized_pnl']) == 0
    assert result['stock_realized_pnl'] is None
    assert set(result['reasons']) == {'actual_stock_settlement_fee_missing', 'actual_stock_sale_fee_missing'}
    assert assigned_put_remaining_economics(lot, quantity_usable=False)['stock_unrealized_pnl'] is None


def test_real_projection_uses_final_canonical_fee_remainder():
    from domain.domain.ledger import project_trade_events
    from domain.domain.assigned_stock import (
        assigned_stock_allocation_row, assigned_stock_trade_event_row,
        assigned_stock_position_lot_row, project_assigned_stock_lifecycle,
    )
    from tests.test_ledger_economics import _event

    settlement = {'close_type': 'assignment', 'stock_settlement': {
        'side': 'buy', 'shares': 100, 'price': 100, 'fees': 0,
        'fee_provenance': {'basis': 'actual', 'source': 'test'},
    }}
    for prior_normal_close in (False, True):
        closes = [
            _event(event_id, 'close' if prior_normal_close and i == 0 else 'assignment',
                contracts=1, price=0, time_ms=i + 2, fees=0, basis='actual',
                target_lot_id='lot-1', raw={} if prior_normal_close and i == 0 else settlement)
            for i, event_id in enumerate('abc')
        ]
        events = [_event('open', 'open', contracts=3, price=2, fees=1, basis='actual'), *closes]
        canonical = project_trade_events(events)
        view = project_assigned_stock_lifecycle(
            [assigned_stock_trade_event_row(e) for e in events],
            assignment_option_rows=[assigned_stock_allocation_row(a) for a in canonical.allocations],
            option_open_lots=[assigned_stock_position_lot_row(l, current_fields={}, valuation_marks=[], at_ms=5)
                              for l in canonical.lots],
            assigned_stock_events=[], as_of_ms=5, account_norm=None, broker_norm=None, month=None,
        )
        allocated = {a.close_event_id: a for a in canonical.allocations}
        for lot in view['assigned_stock_lots']:
            source = allocated[lot['source_assignment_event_id']]
            # Lifecycle facts still own the original premium and exact fee
            # remainder, although the stock-only period report omits them.
            net = Decimal(lot['put_open_premium_exact']) - sum(
                Decimal(f['amount']) for f in lot['source_option_fee_allocations'])
            assert net == source.realized_pnl_net
        assert net == Decimal('199.666666')


def test_unknown_quantity_never_claims_sold_net_or_zero_realized():
    result = assigned_put_remaining_economics(_lot(), price='8', quantity_usable=False)
    assert result['stock_realized_pnl'] is None
    assert result['stock_unrealized_pnl'] is None


@pytest.mark.parametrize('component', ['assignment_stock_fee', 'stock_sale_fee'])
@pytest.mark.parametrize('basis', ['estimated', 'missing'])
def test_missing_actual_stock_fees_suppress_only_dependent_amounts(component, basis):
    lot = _lot()
    next(f for f in lot['fee_evidence'] if f['component'] == component)['basis'] = basis
    result = assigned_put_remaining_economics(lot, price='8')
    assert result['stock_realized_pnl'] is None
    if component == 'stock_sale_fee':
        assert result['stock_unrealized_pnl'] == '-126'
        assert result['reasons'] == ['actual_stock_sale_fee_missing']
    else:
        assert result['stock_unrealized_pnl'] is None
        assert result['reasons'] == ['actual_stock_settlement_fee_missing']


def test_missing_quote_preserves_realized_and_missing_put_facts_change_nothing():
    lot = _lot()
    result = assigned_put_remaining_economics(lot)
    assert result['stock_realized_pnl'] == '-48'
    assert result['stock_unrealized_pnl'] is None
    assert result['reasons'] == ['qualified_quote_missing']
    lot.pop('put_open_premium_exact')
    lot.pop('source_option_fee_allocations')
    assert assigned_put_remaining_economics(lot) == result
