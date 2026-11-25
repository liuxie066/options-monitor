from decimal import Decimal

from domain.domain.assigned_stock import assigned_put_remaining_economics


def _lot():
    return {
        'shares_opened': 100, 'shares_sold': 40, 'shares_remaining': 60,
        'stock_cost_basis_total': '1010', 'option_premium_attribution_exact': '120',
        'assigned_stock_realized_pnl': '-48',
        'fee_evidence': [
            {'component': 'assignment_stock_fee', 'basis': 'actual', 'amount': '10'},
            {'component': 'put_open_option_fee', 'basis': 'actual', 'amount': '20'},
            {'component': 'put_assignment_option_fee', 'basis': 'actual', 'amount': '0'},
            {'component': 'stock_sale_fee', 'basis': 'actual', 'amount': '4'},
        ],
    }


def test_partial_sale_distributes_premium_and_stock_fees_once():
    result = assigned_put_remaining_economics(_lot(), price='8')
    for name, expected in {'stock_unrealized_pnl': '-126', 'remaining_pnl_with_put': '-66',
                           'stock_realized_pnl': '-48', 'sold_pnl_with_put': '-8'}.items():
        assert Decimal(result[name]) == Decimal(expected)
    assert Decimal(result['remaining_pnl_with_put']) + Decimal(result['sold_pnl_with_put']) == -74


def test_missing_put_fee_does_not_suppress_stock_and_conflict_does():
    lot = _lot()
    lot['fee_evidence'][1]['basis'] = 'missing'
    result = assigned_put_remaining_economics(lot, price='8')
    assert Decimal(result['stock_unrealized_pnl']) == -126
    assert result['remaining_pnl_with_put'] is None
    assert assigned_put_remaining_economics(lot, price='8', quantity_usable=False)['stock_unrealized_pnl'] is None


def test_fully_sold_needs_no_price_or_put_fee_but_conflict_takes_precedence():
    lot = _lot() | {'shares_sold': 100, 'shares_remaining': 0, 'fee_evidence': []}
    result = assigned_put_remaining_economics(lot)
    assert Decimal(result['stock_unrealized_pnl']) == 0
    assert Decimal(result['remaining_pnl_with_put']) == 0
    assert assigned_put_remaining_economics(lot, quantity_usable=False)['remaining_pnl_with_put'] is None
