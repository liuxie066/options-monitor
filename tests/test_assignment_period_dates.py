"""Period dates use the query timezone while lifecycle dates keep market semantics."""
from __future__ import annotations

import json
from datetime import datetime

import pytest

from src.application.ledger import api
from src.application.ledger.manual_trades import persist_manual_open_event
from src.application.ledger.repository import SQLiteOptionPositionsRepository
from src.application.positions import assignment_period_report as report
from src.application.tool_execution import execute_tool
from tests.test_agent_plugin_smoke import _public_cfg_with_futu


@pytest.mark.parametrize('symbol,instant,period,excluded,expected,market_date', [
    ('FUTU', '2026-06-13T01:30:00+00:00', {'assignment_year': 2026}, None, '2026-06-13', '2026-06-12'),
    ('FUTU', '2026-01-17T02:00:00+00:00', {'assignment_year': 2026}, None, '2026-01-17', '2026-01-16'),
    ('FUTU', '2026-06-30T16:00:00+00:00', {'assignment_month': '2026-07'}, {'assignment_month': '2026-06'}, '2026-07-01', '2026-06-30'),
    ('FUTU', '2026-12-31T16:00:00+00:00', {'assignment_year': 2027}, {'assignment_year': 2026}, '2027-01-01', '2026-12-31'),
    ('0700.HK', '2026-06-13T01:30:00+00:00', {'assignment_year': 2026}, None, '2026-06-13', '2026-06-13'),
])
def test_public_period_date_matches_shanghai_filter_without_rewriting_market_date(
    tmp_path, monkeypatch, symbol, instant, period, excluded, expected, market_date,
):
    monkeypatch.setenv('OM_RUNTIME_ROOT', str(tmp_path))
    monkeypatch.setattr('src.application.agent_tools.positions.assert_quality_allows', lambda *a, **kw: None)
    clock = datetime.fromisoformat('2027-01-03T15:00:00+00:00')
    at = int(datetime.fromisoformat(instant).timestamp() * 1000)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock.astimezone(tz) if tz else clock.replace(tzinfo=None)

    monkeypatch.setattr(report, 'datetime', Clock)
    monkeypatch.setattr(report, 'read_stock_sell_observations', lambda *a, **kw: {
        'observations': [], 'diagnostics': [], 'coverage': 'complete', 'source_history': 'unknown'})
    db = tmp_path / 'output_shared/state/option_positions.sqlite3'
    writer = SQLiteOptionPositionsRepository(db)
    currency = 'HKD' if symbol.endswith('.HK') else 'USD'
    persist_manual_open_event(writer, broker='富途', account='sy', symbol=symbol,
        option_type='put', side='short', contracts=1, currency=currency, strike=100,
        multiplier=100, expiration_ymd='2027-12-17', premium_per_share=2.5, opened_at_ms=at-1000)
    api.record_manual_assignment(writer, lot_id=writer.list_position_lots()[0]['record_id'],
        contracts_to_close=1, stock_side='buy', stock_qty=100, stock_price=100, as_of_ms=at)
    before = api.read_assigned_stock_query_inputs(writer)
    legacy = api.project_assigned_stock_lifecycle_from_rows(before,
        account='sy', as_of_ms=int(clock.timestamp()*1000))['assigned_stock_lots'][0]
    assert legacy['assigned_date'] == market_date

    data_config = tmp_path / 'portfolio.runtime.json'
    data_config.write_text('{}')
    cfg = _public_cfg_with_futu(str(data_config))
    cfg['accounts'] = ['sy']
    cfg['account_settings']['sy'] = cfg['account_settings'].pop('user1')
    cfg['portfolio']['account'] = 'sy'
    cfg['trade_intake']['account_mapping']['futu'] = {'999000000000000001': 'sy'}
    config = tmp_path / 'config.us.json'
    config.write_text(json.dumps(cfg))
    request = {'config_path': str(config), 'action': 'assigned-stock', 'account': 'sy',
               'broker': '富途', 'status': 'all', 'refresh_quotes': False}
    response = execute_tool('option_positions_read', {**request, **period})
    assert response['ok'], response
    data = response['data']
    assert data['query']['timezone'] == 'Asia/Shanghai'
    assert data['row_count'] == 1
    row = data['rows'][0]
    assert row['assigned_date'] == expected
    assert row['assigned_at_ms'] == at == legacy['assigned_at_ms']
    assert row['stock_lot_id'] == legacy['stock_lot_id']
    assert row['shares_opened'] == row['book_shares_remaining'] == 100
    assert data['accounts'][0]['symbols'][0]['lots'][0]['assigned_date'] == expected
    if excluded:
        empty = execute_tool('option_positions_read', {**request, **excluded})
        assert empty['ok'], empty
        assert empty['data']['row_count'] == 0
    after = api.read_assigned_stock_query_inputs(writer)
    assert before['trade_events'] == after['trade_events']
    assert before['account_assigned_stock_events'] == after['account_assigned_stock_events']
