from copy import deepcopy
from datetime import datetime
from dataclasses import replace
from decimal import Decimal

import pandas as pd
import pytest

from src.application.opening_quote_evidence import normalize_underlier_observation
from src.application.positions import assigned_stock_quotes as quotes


NOW = datetime.fromisoformat('2026-11-25T13:30:00+00:00')
CASES = [('0700.HK', 'HK.00700', 'HK', '2026-11-25', 337.4),
         ('3690.HK', 'HK.03690', 'HK', '2026-11-25', 61.45),
         ('9992.HK', 'HK.09992', 'HK', '2026-11-25', 137.7),
         ('FUTU', 'US.FUTU', 'US', '2026-11-24', 88.09),
         ('PDD', 'US.PDD', 'US', '2026-11-24', 87.22)]


class Gateway:
    missing = None
    def __init__(self):
        self.calls = []
        self.closed = False

    def get_trading_days_with_receipt(self, **kwargs):
        self.calls.append(('calendar', kwargs))
        days = ('2026-11-24', '2026-11-25', '2026-11-26', '2026-11-27') if kwargs['market'] == 'HK' else (
            '2026-11-24', '2026-11-25', '2026-11-27')
        return {'retcode': 0, 'coverage_complete': True, 'pagination_complete': True, 'page_count': 1,
                'rows': [{'time': day, 'trade_date_type': 'MORNING' if kwargs['market'] == 'US'
                          and day == '2026-11-27' else 'WHOLE'} for day in days]}

    def request_history_kline(self, **kwargs):
        self.calls.append(('daily', kwargs))
        if kwargs['code'] == self.missing:
            return {'data': pd.DataFrame(), 'page_req_key': None}
        case = next(c for c in CASES if c[1] == kwargs['code'])
        return {'data': pd.DataFrame([{'code': case[1], 'time_key': case[3] + ' 00:00:00',
                                       'close': case[4]}]), 'page_req_key': None}

    def close(self):
        self.closed = True


def refresh(monkeypatch, tmp_path, *, clock=NOW, preserve_evidence=True):
    gateway = Gateway()
    monkeypatch.setattr(quotes, 'build_ready_futu_quote_gateway', lambda **kw: gateway)

    def observe(_gateway, codes, **kwargs):
        code = codes[0]
        case = next(c for c in CASES if c[1] == code)
        value = normalize_underlier_observation(
            code=code, market=case[2], snapshot_row={
                'code': code, 'last_price': case[4] + 1, 'update_time': case[3] + ' 16:00:00',
                'sec_status': 'NORMAL', 'suspension': False},
            market_state_row={'code': code, 'market_state': 'CLOSED'}, now_utc=clock)
        return {code: value}

    monkeypatch.setattr(quotes, 'get_underlier_observations', observe)
    stamp = int(clock.timestamp() * 1000)
    result = quotes.refresh_assigned_stock_quote_snapshots(
        [{'symbol': c[0], 'shares_remaining': 100} for c in CASES], cfg={},
        host='isolated', port=11111, base_dir=tmp_path, state_base_dir=tmp_path,
        preserve_evidence=preserve_evidence, now_ms=lambda: stamp)
    return result, gateway, stamp


def test_five_symbol_closed_quotes_use_completed_unadjusted_close(monkeypatch, tmp_path):
    result, gateway, stamp = refresh(monkeypatch, tmp_path)
    assert len(result.quote_snapshots) == 5
    assert all(quotes.assigned_stock_quote_is_usable(q, stamp) for q in result.quote_snapshots)
    for quote, case in zip(result.quote_snapshots, CASES):
        assert quote['spot'] == case[4]
        local_close = datetime.fromisoformat(case[3] + ('T16:00:00+08:00' if case[2] == 'HK'
                                                      else 'T16:00:00-05:00'))
        assert quote['quote_time_ms'] == int(local_close.timestamp() * 1000)
        assert quote['quote_time_ms'] < quote['quote_observed_at_ms'] == stamp
        assert quote['quote_status'] == 'closed_market_carry'
    assert len([c for c in gateway.calls if c[0] == 'calendar']) == 2
    assert all(c[1]['ktype'] == 'K_DAY' and c[1]['autype'] == 'NONE'
               and c[1]['start'] == c[1]['end'] for c in gateway.calls if c[0] == 'daily')
    assert result.diagnostics['received_symbols'] == [c[0] for c in CASES]
    assert result.diagnostics['qualified_symbols'] == [c[0] for c in CASES]
    assert gateway.closed


def test_closed_observation_is_not_reported_as_qualified_when_close_is_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(Gateway, 'request_history_kline', lambda self, **kw: {
        'data': pd.DataFrame(), 'page_req_key': None})
    result, gateway, stamp = refresh(monkeypatch, tmp_path)
    assert not any(quotes.assigned_stock_quote_is_usable(q, stamp) for q in result.quote_snapshots)
    assert result.diagnostics['status'] == 'missing_quote'
    assert result.diagnostics['received_symbols'] == [c[0] for c in CASES]
    assert result.diagnostics['qualified_symbols'] == []
    assert result.diagnostics['missing_symbols'] == [c[0] for c in CASES]
    assert len(result.diagnostics['errors']) == 5
    assert gateway.closed


@pytest.mark.parametrize('fault', ['adjusted', 'pagination', 'duplicate_bar', 'wrong_code', 'currency',
    'future_bar', 'wrong_date', 'price_changed', 'stamp_changed', 'received_in_future',
    'calendar_incomplete', 'calendar_future', 'missing_request', 'malformed_bar', 'observation_future',
    'suspended', 'auction', 'calendar_market', 'same_date_post_close', 'state_received_future', 'calendar_time_missing'])
def test_completed_close_revalidates_original_receipts(monkeypatch, tmp_path, fault):
    result, _, stamp = refresh(monkeypatch, tmp_path)
    quote = deepcopy(result.quote_snapshots[0])
    carry = quote['market_evidence']['closed_market_carry']
    receipt = carry['daily_close']
    bar = receipt['rows'][0]
    if fault == 'adjusted':
        receipt['request']['autype'] = 'QFQ'
    elif fault == 'pagination':
        receipt['pagination_complete'] = False
    elif fault == 'duplicate_bar':
        receipt['rows'].append(dict(bar))
    elif fault == 'wrong_code':
        bar['code'] = 'HK.03690'
    elif fault == 'currency':
        bar['currency'] = 'USD'
    elif fault == 'future_bar':
        bar['time_key'] = '2026-11-25 23:59:00'
    elif fault == 'wrong_date':
        bar['time_key'] = '2026-11-24 00:00:00'
    elif fault == 'price_changed':
        quote['spot'] += 1
    elif fault == 'stamp_changed':
        quote['quote_time_ms'] = stamp
    elif fault == 'received_in_future':
        receipt['received_at_ms'] = stamp + 1
    elif fault == 'calendar_incomplete':
        carry['calendars']['HK']['receipt']['coverage_complete'] = False
    elif fault == 'calendar_future':
        carry['calendars']['HK']['source_observed_at_utc'] = '2026-11-26T00:00:00Z'
    elif fault == 'missing_request':
        receipt.pop('request')
    elif fault == 'malformed_bar':
        receipt['rows'][0] = None
    elif fault == 'observation_future':
        carry['session_observation']['observed_at_utc'] = '2026-11-26T00:00:00Z'
    elif fault == 'suspended':
        carry['session_observation']['suspension'] = True
    elif fault == 'auction':
        carry['session_observation']['market_state'] = 'HK_CAS'
    elif fault == 'calendar_market':
        carry['calendars'] = {'US': carry['calendars']['HK']}
    elif fault == 'same_date_post_close':
        bar['time_key'] = '2026-11-25 18:00:00'
    elif fault == 'state_received_future':
        carry['session_observation']['received_at_ms'] = stamp + 1
    elif fault == 'calendar_time_missing':
        carry['calendars']['HK']['source_observed_at_utc'] = None
    assert not quotes.assigned_stock_quote_is_usable(quote, stamp)


def test_close_expires_at_next_open_and_cannot_value_before_receipt(monkeypatch, tmp_path):
    result, _, stamp = refresh(monkeypatch, tmp_path)
    for quote in result.quote_snapshots:
        until = quote['market_evidence']['stocks'][0]['valid_until_ms']
        assert quotes.assigned_stock_quote_is_usable(quote, until - 1)
        assert not quotes.assigned_stock_quote_is_usable(quote, until)
        assert not quotes.assigned_stock_quote_is_usable(quote, stamp - 1)


def test_hk_preopen_auction_uses_previous_completed_day_but_closing_auction_does_not(monkeypatch, tmp_path):
    from domain.domain.performance.strategy_evaluation import completed_daily_close_market
    result, _, _ = refresh(monkeypatch, tmp_path)
    carry = deepcopy(result.quote_snapshots[0]['market_evidence']['closed_market_carry'])
    preopen = int(datetime.fromisoformat('2026-11-25T01:15:00+00:00').timestamp() * 1000)
    carry['daily_close']['request'].update(start='2026-11-24', end='2026-11-24')
    carry['daily_close']['rows'][0]['time_key'] = '2026-11-24 00:00:00'
    carry['daily_close']['received_at_ms'] = preopen
    carry['session_observation'].update(market_state='AUCTION', observed_at_utc='2026-11-24T08:00:00+00:00')
    carry['calendars']['HK']['source_observed_at_utc'] = '2026-11-25T01:15:00+00:00'
    market = completed_daily_close_market(symbol='0700.HK', calendar=carry['calendars']['HK'],
        receipt=carry['daily_close'], observation=carry['session_observation'],
        observation_received_at_ms=preopen, at_ms=preopen)
    assert market['stocks'][0]['effective_at_ms'] < preopen < market['stocks'][0]['valid_until_ms']
    quote = deepcopy(result.quote_snapshots[0])
    quote['market_evidence']['closed_market_carry']['session_observation']['market_state'] = 'AUCTION'
    assert not quotes.assigned_stock_quote_is_usable(quote, int(NOW.timestamp() * 1000))


@pytest.mark.parametrize('market,at,day,kind,close', [
    ('HK', '2026-11-25T05:00:00+00:00', '2026-11-25', 'MORNING', '2026-11-25T04:00:00+00:00'),
    ('US', '2026-11-27T19:00:00+00:00', '2026-11-27', 'MORNING', '2026-11-27T18:00:00+00:00'),
    ('US', '2026-11-02T13:00:00+00:00', '2026-10-30', 'WHOLE', '2026-10-30T20:00:00+00:00'),
])
def test_completed_close_session_policy_handles_half_days_and_dst(market, at, day, kind, close):
    from domain.domain.performance.strategy_evaluation import completed_stock_close_reference
    calendar = {'start': '2026-10-20', 'end': '2026-12-01',
        'source_observed_at_utc': at, 'receipt': {'retcode': 0, 'coverage_complete': True,
        'pagination_complete': True, 'page_count': 1, 'rows': [
            {'time': day, 'trade_date_type': kind}, {'time': '2026-12-01', 'trade_date_type': 'WHOLE'}]}}
    # Include the next normal US trading date to prove the DST opening bound.
    if '11-02' in at:
        calendar['receipt']['rows'].insert(1, {'time': '2026-11-02', 'trade_date_type': 'WHOLE'})
    result = completed_stock_close_reference(symbol='FUTU' if market == 'US' else '0700.HK',
        calendar=calendar, at_ms=int(datetime.fromisoformat(at).timestamp() * 1000))
    assert result['effective_at_ms'] == int(datetime.fromisoformat(close).timestamp() * 1000)
    if '11-02' in at:
        assert result['valid_until_ms'] == int(datetime.fromisoformat('2026-11-02T14:30:00+00:00').timestamp() * 1000)


@pytest.mark.parametrize('at', ['2026-11-25T02:00:00+00:00', '2026-11-25T04:30:00+00:00'])
def test_no_daily_close_carry_during_regular_session_or_hk_lunch(monkeypatch, tmp_path, at):
    result, gateway, _ = refresh(monkeypatch, tmp_path, clock=datetime.fromisoformat(at))
    assert not any(q['symbol'].endswith('.HK') for q in result.quote_snapshots)
    assert not any(c[1]['code'].startswith('HK.') for c in gateway.calls if c[0] == 'daily')


@pytest.mark.parametrize('period', [{'assignment_year': 2026}, {'assignment_month': '2026-11'}])
@pytest.mark.parametrize('account', [None, 'lx', 'sy'])
@pytest.mark.parametrize('missing_pdd', [False, True])
@pytest.mark.parametrize('history_complete', [False, True])
@pytest.mark.parametrize('sale_shares', [40, 100])
def test_public_ytd_completed_close_preserves_ledger_basis_and_metric_gaps(
    monkeypatch, tmp_path, account, missing_pdd, history_complete, period, sale_shares,
):
    """Synthetic lots with production-shaped symbols/times; no real ledger facts."""
    import json
    from src.application.ledger import api
    from src.application.ledger.repository import SQLiteOptionPositionsRepository
    from src.application.positions import assignment_period_report as report
    from src.application.tool_execution import execute_tool
    from tests.test_agent_plugin_smoke import _public_cfg_with_futu
    from tests.test_assigned_stock_query_inputs import _inbox
    from tests.test_ledger_economics import _event
    from domain.domain.ledger import ContractKey

    monkeypatch.setenv('OM_RUNTIME_ROOT', str(tmp_path))
    monkeypatch.setattr('src.application.agent_tools.positions.assert_quality_allows', lambda *a, **kw: None)
    stamp = int(NOW.timestamp() * 1000)

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)

    monkeypatch.setattr(report, 'datetime', FrozenDateTime)
    monkeypatch.setattr(quotes, '_now_ms', lambda: stamp)
    # The public adapter's clock default is bound at definition time.
    from src.application.agent_tools import positions
    original_refresh = positions.refresh_assigned_stock_quotes
    monkeypatch.setattr(positions, 'refresh_assigned_stock_quotes', lambda *a, **kw:
                        original_refresh(*a, **kw, now_ms=lambda: stamp))
    if history_complete:
        # Synthetic attestation for the complete-history branch only. The real
        # reader still runs; this is not an assertion about production history.
        reader = report.read_stock_sell_observations

        def with_attestation(*a, **kw):
            result = reader(*a, **kw)
            return {**result, 'source_history': 'complete'}

        monkeypatch.setattr(report, 'read_stock_sell_observations', with_attestation)
    db = tmp_path / 'output_shared/state/option_positions.sqlite3'
    writer = SQLiteOptionPositionsRepository(db)
    setups = [('tencent', 'lx', '0700.HK', 400), ('meituan', 'lx', '3690.HK', 70),
              ('pop', 'sy', '9992.HK', 140), ('futu', 'lx', 'FUTU', 100),
              ('pdd', 'sy', 'PDD', 100), ('sold', 'lx', 'FUTU', 100)]
    for name, owner, symbol, strike in setups:
        key = ContractKey.from_values(broker='futu', account=owner, underlying_symbol=symbol,
            option_type='put', strike=strike, expiration_ymd='2026-12-18')
        currency = 'HKD' if symbol.endswith('.HK') else 'USD'
        for kind in ('open', 'assignment'):
            event = _event(name + '-' + kind, kind, time_ms=stamp - (200_000 if kind == 'open' else 100_000),
                price=2 if kind == 'open' else 0, fees=0 if name == 'pdd' else 2 if kind == 'open' else 3,
                currency=currency, basis=None if name == 'pdd' else 'actual',
                target_lot_id=name + '-lot' if kind == 'assignment' else None,
                raw={'stock_settlement': {'side': 'buy', 'shares': 100, 'price': strike,
                    'fees': 1, 'fee_provenance': {'basis': 'actual', 'source': 'synthetic'}}}
                    if kind == 'assignment' else {})
            writer.upsert_trade_event(replace(event, contract_key=key,
                lot_id=name + '-lot' if kind == 'open' else None))
    writer.upsert_assigned_stock_event({
        'stock_event_id': 'sold-stock', 'event_type': 'sale', 'account': 'lx', 'broker': '富途',
        'symbol': 'FUTU', 'currency': 'USD', 'side': 'sell', 'shares': sale_shares, 'price': 105,
        'fees': 1, 'fee_provenance': {'basis': 'actual', 'source': 'synthetic'},
        'target_stock_lot_id': 'assigned-stock-sold-assignment', 'trade_time_ms': stamp - 50_000})
    _inbox(str(db) + '.trade_intake_inbox.sqlite3', [])
    config_data = tmp_path / 'portfolio.runtime.json'
    config_data.write_text('{}')
    cfg = _public_cfg_with_futu(str(config_data))
    settings = cfg['account_settings'].pop('user1')
    cfg['accounts'] = ['lx', 'sy']
    cfg['account_settings'] = {a: deepcopy(settings) for a in cfg['accounts']}
    cfg['portfolio']['account'] = 'lx'
    cfg['trade_intake']['account_mapping']['futu'] = {'test-lx': 'lx', 'test-sy': 'sy'}
    for owner in cfg['accounts']:
        cfg['account_settings'][owner]['futu']['account_id'] = 'test-' + owner
        state = tmp_path / f'output_shared/state/trade_intake/{owner}/state.json'
        state.parent.mkdir(parents=True)
        state.write_text(json.dumps({'unresolved_deal_ids': {}, 'failed_deal_ids': {}}))
    config = tmp_path / 'config.us.json'
    config.write_text(json.dumps(cfg))
    gateway = Gateway()
    gateway.missing = 'US.PDD' if missing_pdd else None
    monkeypatch.setattr(quotes, 'build_ready_futu_quote_gateway', lambda **kw: gateway)

    def observe(_gateway, codes, **kwargs):
        code = codes[0]
        case = next(c for c in CASES if c[1] == code)
        return {code: normalize_underlier_observation(code=code, market=case[2],
            snapshot_row={'code': code, 'last_price': case[4] + 1, 'update_time': case[3] + ' 16:00:00',
                          'sec_status': 'NORMAL', 'suspension': False},
            market_state_row={'code': code, 'market_state': 'CLOSED'}, now_utc=NOW)}

    monkeypatch.setattr(quotes, 'get_underlier_observations', observe)
    monkeypatch.setattr(report, 'current_exchange_rate_snapshot', lambda **kw: {})
    monkeypatch.setattr(report, 'project_exchange_rate_snapshot', lambda *a, **kw: {'pairs': {
        c + 'CNY': {'display_eligible': True, 'quote_at_utc': '2026-11-25T13:29:00Z',
                   'rate': rate, 'source': 'synthetic'} for c, rate in [('USD', '7'), ('HKD', '0.85')]}})
    before = api.read_assigned_stock_query_inputs(writer)
    response = execute_tool('option_positions_read', {'config_path': str(config), 'action': 'assigned-stock',
        **period, 'status': 'all', 'refresh_quotes': True,
        **({'account': account} if account else {})})
    assert response['ok'], response
    data = response['data']
    from tests.test_assignment_period_entry_contract import assert_stock_only_period
    assert_stock_only_period(response)
    sold_remaining_pnl = '-715.2' if sale_shares == 40 else '0'
    realized_pnl = '198.6' if sale_shares == 40 else '498'
    expected = {'tencent': '-6261', 'meituan': '-856', 'pop': '-231', 'futu': '-1192',
                'pdd': None if missing_pdd else '-1279', 'sold': sold_remaining_pnl}
    for row in data['rows']:
        name = row['source_assignment_event_id'].removesuffix('-assignment')
        if expected[name] is None:
            assert row['stock_unrealized_pnl'] is None
        else:
            assert Decimal(row['stock_unrealized_pnl']) == Decimal(expected[name])
        assert row['quantity_status'] == ('confirmed' if history_complete else 'ledger_only')
        if history_complete:
            assert row['confirmed']['shares_remaining'] == (100 - sale_shares if name == 'sold' else 100)
            assert row['provisional'] is None
        else:
            assert row['confirmed']['shares_remaining'] is None
            assert row['provisional']['shares_remaining'] == (100 - sale_shares if name == 'sold' else 100)
        assert Decimal(row['stock_realized_pnl']) == Decimal(realized_pnl if name == 'sold' else '0')
        assert {f['component'] for f in row['fee_evidence']} == (
            {'assignment_stock_fee', 'stock_sale_fee'} if name == 'sold' else {'assignment_stock_fee'})
        assert row['quote_status'] == ('not_required' if name == 'sold' and sale_shares == 100 else
                                      'missing_quote' if name == 'pdd' and missing_pdd else 'closed_market_carry')
        if name == 'pdd':
            assert 'remaining_pnl_with_put' not in row
    assert data['coverage']['source_history'] == ('complete' if history_complete else 'unknown')
    assert data['coverage']['status'] == ('partial' if not history_complete or missing_pdd and account != 'lx' else 'complete')
    if account is None:
        by_currency = data['summary']['by_currency']
        assert by_currency['HKD']['stock_unrealized_pnl']['value'] == '-7348.00'
        assert Decimal(by_currency['USD']['stock_unrealized_pnl']['value']) == (
            Decimal('-1192.00' if missing_pdd else '-2471.00') + Decimal(sold_remaining_pnl))
        assert Decimal(data['summary']['cny_estimate']['metrics']['stock_unrealized_pnl']['value']) == (
            Decimal('-14589.80' if missing_pdd else '-23542.80') + Decimal(sold_remaining_pnl) * 7)
    expected_realized = Decimal(0 if account == 'sy' else realized_pnl)
    assert Decimal(data['summary']['by_currency']['USD']['stock_realized_pnl']['value']) == expected_realized
    assert Decimal(data['summary']['cny_estimate']['metrics']['stock_realized_pnl']['value']) == expected_realized * 7
    for account_row in data['accounts']:
        amount = Decimal(realized_pnl if account_row['account'] == 'lx' else '0')
        assert Decimal(account_row['summary']['by_currency']['USD']['stock_realized_pnl']['value']) == amount
        assert Decimal(account_row['summary']['cny_estimate']['metrics']['stock_realized_pnl']['value']) == amount * 7
        for symbol_row in account_row['symbols']:
            currency = 'HKD' if symbol_row['symbol'].endswith('.HK') else 'USD'
            expected_symbol = amount if symbol_row['symbol'] == 'FUTU' else Decimal(0)
            assert Decimal(symbol_row['summary']['by_currency'][currency]['stock_realized_pnl']['value']) == expected_symbol
    expected_symbols = {s for _, a, s, _ in setups if not account or a == account}
    qualified = expected_symbols - ({'PDD'} if missing_pdd else set())
    assert set(data['quote_refresh']['received_symbols']) == expected_symbols
    assert set(data['quote_refresh']['qualified_symbols']) == qualified
    assert set(data['quote_refresh']['missing_symbols']) == expected_symbols - qualified
    assert data['quote_refresh']['status'] == ('partial' if missing_pdd and account != 'lx' else 'ok')
    if missing_pdd and account != 'lx':
        assert data['quote_refresh']['qualification_gaps']['PDD'] == ['completed_close_receipt_incomplete_or_mismatched']
    after = api.read_assigned_stock_query_inputs(writer)
    assert before['trade_events'] == after['trade_events']
    assert before['account_assigned_stock_events'] == after['account_assigned_stock_events']
    assert gateway.closed


def test_final_valuation_diagnostics_exclude_quote_that_expired_after_acquisition(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from src.application.positions import assignment_period_report as report
    from src.application.ledger import api
    from tests.test_performance_assignment import _trade, _assign_put

    clock = datetime.fromisoformat('2026-07-24T15:00:00+00:00')
    stamp = int(clock.timestamp() * 1000)
    source = {'trade_events': [api.trade_event_application_payload(e.to_dict()) for e in (
        _trade(event_time_ms=stamp - 200_000), _assign_put(event_time_ms=stamp - 100_000))],
        'account_assigned_stock_events': [], 'diagnostics': []}
    monkeypatch.setattr(report.ledger_api, 'read_assigned_stock_query_inputs', lambda repo: source)
    monkeypatch.setattr(report, 'read_stock_sell_observations', lambda *a, **kw: {
        'observations': [], 'coverage': 'complete', 'source_history': 'unknown'})
    observation = normalize_underlier_observation(code='US.NVDA', market='US', snapshot_row={
        'code': 'US.NVDA', 'last_price': 100, 'update_time': '2026-07-24 10:59:30',
        'sec_status': 'NORMAL', 'suspension': False},
        market_state_row={'code': 'US.NVDA', 'market_state': 'MORNING'}, now_utc=clock).to_dict()
    quote = {'symbol': 'NVDA', 'spot': 100, 'quote_time_ms': stamp - 30_000, 'quote_observation': observation}
    monkeypatch.setattr(report, 'current_exchange_rate_snapshot', lambda **kw: {})
    moments = iter([stamp, stamp + 600_000])
    result = report.build_assignment_period_report(SimpleNamespace(path=tmp_path / 'ledger.sqlite3'),
        {'assignment_year': 2026}, cfg={'accounts': ['lx'], 'account_settings': {
            'lx': {'type': 'futu', 'futu': {'account_id': 'test-lx', 'host': 'localhost', 'port': 11111}}}},
        repo_base=lambda: tmp_path, now_ms=lambda: next(moments),
        refresh_assigned_stock_quotes=lambda *a, **kw: SimpleNamespace(quote_snapshots=[quote], diagnostics={
            'enabled': True, 'status': 'ok', 'received_symbols': ['NVDA'],
            'refreshed_symbols': ['NVDA'], 'qualified_symbols': ['NVDA']}))
    assert result['quote_refresh']['received_symbols'] == ['NVDA']
    assert result['quote_refresh']['qualified_symbols'] == []
    assert result['quote_refresh']['status'] == 'missing_quote'
    assert result['quote_refresh']['qualification_gaps'] == {'NVDA': ['underlier_quote_stale']}
    assert result['rows'][0]['stock_unrealized_pnl'] is None


@pytest.mark.parametrize('terminal', [None, '', b''])
def test_shared_close_quote_and_default_refresh_accept_terminal_history_pages(monkeypatch, tmp_path, terminal):
    original = Gateway.request_history_kline
    def history(self, **kwargs):
        return {**original(self, **kwargs), 'page_req_key': terminal}
    monkeypatch.setattr(Gateway, 'request_history_kline', history)
    result, _, stamp = refresh(monkeypatch, tmp_path, preserve_evidence=False)
    assert len(result.quote_snapshots) == 5
    assert all(quotes.assigned_stock_quote_is_usable(q, stamp) for q in result.quote_snapshots)


@pytest.mark.parametrize('case', CASES)
def test_common_current_spot_and_performance_collector_use_completed_close(monkeypatch, tmp_path, case):
    from src.application import opend_market_snapshot_fetching as shared
    from src.application.performance.evidence_collection import collect_current_performance_evidence
    from domain.domain.performance.models import StockInstrumentKey

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW
    gateway = Gateway()
    raw = normalize_underlier_observation(
        code=case[1], market=case[2], snapshot_row={
            'code': case[1], 'last_price': case[4] + 1, 'update_time': case[3] + ' 16:00:00',
            'sec_status': 'NORMAL', 'suspension': False},
        market_state_row={'code': case[1], 'market_state': 'CLOSED'}, now_utc=NOW)
    monkeypatch.setattr(shared, 'datetime', Clock)
    monkeypatch.setattr(shared, 'build_ready_futu_quote_gateway', lambda **kw: gateway)
    monkeypatch.setattr(shared, 'get_underlier_observations_opend', lambda *a, **kw: {case[1]: raw})
    quote = shared.get_underlier_spot_quote(case[0], base_dir=tmp_path)
    assert quote['price'] == case[4]
    assert quote['quote_status'] == 'closed_market_carry'
    assert quote['effective_at_ms'] == quote['quote_time_ms'] < quote['quote_observed_at_ms']
    assert quote['quote_observation']['status'] == raw.status == 'market_closed'
    assert quote['price_field'] == 'close'
    instrument = StockInstrumentKey(symbol=case[0], currency='HKD' if case[2] == 'HK' else 'USD')
    result = collect_current_performance_evidence(
        period_status='partial_current', refresh_quotes=True, option_positions=(),
        stock_instruments=(instrument,), now_ms=int(NOW.timestamp() * 1000), base_dir=tmp_path,
        fx_payload_fetcher=lambda: None)
    assert len(result.valuation_marks) == 1
    mark = result.valuation_marks[0]
    assert mark.price == Decimal(str(case[4]))
    assert (mark.mark_kind, mark.source) == ('close', 'opend_unadjusted_daily_close')
    assert mark.effective_at_ms == quote['quote_time_ms']
    assert mark.observed_at_ms == quote['quote_observed_at_ms']
    assert not mark.quality.get('timestamp_fallback')
    assert mark.raw['market_evidence'] == quote['market_evidence']


@pytest.mark.parametrize('stop_after', ['before', 'calendar', 'history', 'deadline'])
def test_shared_close_acquisition_respects_cancellation_and_deadline(monkeypatch, tmp_path, stop_after):
    from time import monotonic
    from src.application import opend_market_snapshot_fetching as shared
    from src.application.opend_fetch_config import OpenDFetchLimits
    from src.application.opend_utils import normalize_underlier
    stopped = [stop_after == 'before']
    class ControlledGateway(Gateway):
        def get_trading_days_with_receipt(self, **kw):
            result = super().get_trading_days_with_receipt(**kw)
            stopped[0] = stop_after == 'calendar'
            return result
        def request_history_kline(self, **kw):
            result = super().request_history_kline(**kw)
            stopped[0] = stop_after == 'history'
            return result
    gateway = ControlledGateway()
    raw = normalize_underlier_observation(
        code='HK.00700', market='HK', snapshot_row={
            'code': 'HK.00700', 'last_price': 338.4, 'update_time': '2026-11-25 16:00:00',
            'sec_status': 'NORMAL', 'suspension': False},
        market_state_row={'code': 'HK.00700', 'market_state': 'CLOSED'}, now_utc=NOW)
    with pytest.raises(TimeoutError, match='cancelled|deadline'):
        shared.get_stock_valuation_quote(
            gateway, symbol='0700.HK', underlier=normalize_underlier('0700.HK', base_dir=tmp_path),
            observation=raw, base_dir=tmp_path, calendar_cache={},
            history_limit=OpenDFetchLimits.from_flat_kwargs().history_kline,
            now_ms=lambda: int(NOW.timestamp() * 1000), cancelled=lambda: stopped[0],
            deadline=monotonic() - 1 if stop_after == 'deadline' else None)
    assert len(gateway.calls) == {'before': 0, 'deadline': 0, 'calendar': 1, 'history': 2}[stop_after]
