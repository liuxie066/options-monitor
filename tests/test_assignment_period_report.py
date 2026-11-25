from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from src.application.agent_tool_contracts import AgentToolError
from src.application.positions.assignment_period_report import (
    _affected, _fx_summary, _resolved_observation, assignment_period, summarize_lots,
)


def ms(day):
    return int(datetime.fromisoformat(day).replace(tzinfo=ZoneInfo('Asia/Shanghai')).timestamp() * 1000)


def test_period_uses_shanghai_current_year_and_month_not_expiration():
    now = ms('2026-11-25T12:00:00')
    year = assignment_period({'assignment_year': 2026}, now)
    assert year['start_at_ms'] == ms('2026-01-01T00:00:00')
    assert year['end_exclusive_at_ms'] == now
    month = assignment_period({'assignment_month': '2026-10'}, now)
    assert month['end_exclusive_at_ms'] == ms('2026-11-01T00:00:00')
    dates = assignment_period({'assignment_start_date': '2026-10-31', 'assignment_end_date': '2026-10-31'}, now)
    assert dates['start_at_ms'] == ms('2026-10-31T00:00:00')
    assert dates['end_exclusive_at_ms'] == month['end_exclusive_at_ms']


@pytest.mark.parametrize('payload', [
    {'assignment_year': 2027}, {'assignment_month': '2026-13'},
    {'assignment_year': 2026, 'as_of_ms': 1},
    {'assignment_year': 2026, 'status': 'open'},
    {'assignment_year': 2026, 'assignment_month': '2026-01'},
    {'assignment_start_date': '2026-01-01'},
])
def test_invalid_or_historical_period_rejected(payload):
    with pytest.raises(AgentToolError):
        assignment_period(payload, ms('2026-11-25T12:00:00'))


def test_pending_sale_does_not_pollute_future_lot_or_other_account():
    row = {'assigned_at_ms': 20, 'account': 'lx', 'symbol': 'FUTU', 'broker': '富途', 'currency': 'USD'}
    assert not _affected(row, {'trade_time_ms': 10, 'account': 'lx', 'symbol': 'FUTU'})
    assert not _affected(row, {'trade_time_ms': 30, 'account': 'sy', 'symbol': 'FUTU'})
    assert _affected(row, {'trade_time_ms': None, 'account': 'lx', 'symbol': 'FUTU'})


def test_resolution_requires_account_execution_and_economic_scope():
    sale = {'source_deal_id': 'd', 'account': 'lx', 'futu_account_id': '1', 'symbol': 'FUTU',
            'broker': '富途', 'currency': 'USD', 'shares': 40, 'price': 10, 'trade_time_ms': 100,
            'target_stock_lot_id': 'lot', 'stock_event_id': 'sale', 'side': 'sell'}
    assert _resolved_observation(sale, [sale])
    assert not _resolved_observation({**sale, 'shares': 41}, [sale])
    assert not _resolved_observation({**sale, 'account': 'sy'}, [sale])
    assert not _resolved_observation({**sale, 'futu_account_id': None}, [sale])


def test_aggregation_preserves_unrounded_values_and_distinguishes_null_zero():
    rows = [{'stock_lot_id': 'a', 'currency': 'HKD', 'quantity_status': 'ledger_only',
             'stock_unrealized_pnl': '1.005', 'remaining_pnl_with_put': None, 'reasons': ['missing_fee']},
            {'stock_lot_id': 'b', 'currency': 'HKD', 'quantity_status': 'confirmed',
             'stock_unrealized_pnl': '1.005', 'remaining_pnl_with_put': '0', 'reasons': []}]
    summary = summarize_lots(rows)
    stock = summary['by_currency']['HKD']['stock_unrealized_pnl']
    assert stock['value'] == '2.01'
    assert stock['coverage'] == 'partial'
    assert stock['provisional_lot_ids'] == ['a']
    put = summary['by_currency']['HKD']['remaining_pnl_with_put']
    assert put['value'] == '0.00' and put['omitted_lot_ids'] == ['a']
    assert summarize_lots([rows[0]])['by_currency']['HKD']['remaining_pnl_with_put']['value'] is None
    assert summarize_lots([])['empty_value'] == '0.00'
    assert summarize_lots([], empty_is_zero=False)['empty_value'] is None


def test_missing_fx_keeps_native_and_never_yields_false_cny_zero():
    rows = [{'stock_lot_id': 'a', 'currency': 'USD', 'quantity_status': 'confirmed',
             'stock_unrealized_pnl': '-12', 'remaining_pnl_with_put': '-10', 'reasons': []}]
    summary = summarize_lots(rows)
    fx = _fx_summary(summary, {}, ms('2026-11-25T12:00:00'))
    assert fx['metrics']['stock_unrealized_pnl']['value'] is None
    assert fx['metrics']['stock_unrealized_pnl']['coverage'] == 'partial'
    assert summary['by_currency']['USD']['stock_unrealized_pnl']['value'] == '-12.00'


def test_cny_aggregation_rejects_mixed_source_and_keeps_decimal(monkeypatch):
    import src.application.positions.assignment_period_report as report
    at = ms('2026-11-25T12:00:00')
    def projection(snapshot, **kwargs):
        return {'pairs': snapshot}
    monkeypatch.setattr(report, 'project_exchange_rate_snapshot', projection)
    rows = [{'stock_lot_id': c, 'currency': c, 'quantity_status': 'confirmed',
             'stock_unrealized_pnl': '-1.005', 'remaining_pnl_with_put': '-1', 'reasons': []}
            for c in ('USD', 'HKD')]
    summary = summarize_lots(rows)
    pair = {'display_eligible': True, 'quote_at_utc': '2026-11-25T03:59:00+00:00', 'rate': '1', 'source': 'a'}
    good = _fx_summary(summary, {'USDCNY': pair, 'HKDCNY': pair}, at)
    assert good['metrics']['stock_unrealized_pnl']['value'] == '-2.01'
    bad = _fx_summary(summary, {'USDCNY': pair, 'HKDCNY': {**pair, 'source': 'b'}}, at)
    assert bad['metrics']['stock_unrealized_pnl']['value'] is None
    assert bad['metrics']['stock_unrealized_pnl']['coverage'] == 'partial'


def test_broker_aliases_cannot_hide_pending_sale_or_cross_quote_scope():
    from src.application.positions.assignment_period_report import _quote_scope_matches
    row = {'account': 'lx', 'broker': '富途', 'symbol': 'NVDA', 'currency': 'USD', 'assigned_at_ms': 1}
    assert _affected(row, {'account': 'lx', 'broker': 'futu', 'symbol': 'NVDA', 'trade_time_ms': 2})
    assert _quote_scope_matches({'account': 'lx', 'broker': 'futu'}, row)
    assert not _quote_scope_matches({'account': 'sy', 'broker': 'futu'}, row)
    assert not _quote_scope_matches({'account': 'lx', 'broker': 'longbridge'}, row)
