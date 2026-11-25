from dataclasses import replace
from datetime import datetime
import json
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from domain.domain.ledger import ContractKey
from src.application.ledger import api
from src.application.positions import assignment_period_report as report
from tests.test_assigned_stock_query_inputs import _inbox, _sell
from tests.test_ledger_economics import _event


def ms(day):
    return int(datetime.fromisoformat(day).replace(tzinfo=ZoneInfo('Asia/Shanghai')).timestamp() * 1000)


@pytest.mark.parametrize('period', [{'assignment_year': 2026}, {'assignment_month': '2026-11'}])
@pytest.mark.parametrize('missing_state', [False, True])
@pytest.mark.parametrize('currency_fault', [False, True])
def test_real_reader_projection_partial_scope_runtime_and_metric_coverage(tmp_path, monkeypatch, period, missing_state, currency_fault):
    runtime, code = tmp_path / 'runtime', tmp_path / 'code'
    runtime.mkdir(); code.mkdir()
    now = ms('2026-11-25T12:51:00')
    cfg = {'accounts': ['lx', 'sy'], 'account_settings': {a: {
        'account_type': 'futu', 'futu': {'account_id': 'test-' + a, 'host': '127.0.0.1', 'port': 11111},
    } for a in ('lx', 'sy')}, 'trade_intake': {'account_mapping': {'futu': {'test-lx': 'lx', 'test-sy': 'sy'}}}}
    for account in ('lx', 'sy'):
        state = runtime / f'output_shared/state/trade_intake/{account}/state.json'
        state.parent.mkdir(parents=True)
        if not (account == 'lx' and missing_state):
            state.write_text(json.dumps({'unresolved_deal_ids': {}, 'failed_deal_ids': {}}))
    events = []
    for name, account, symbol, day in [('old', 'lx', 'NVDA', '2026-11-01T12:00:00'),
            ('new', 'lx', 'NVDA', '2026-11-20T12:00:00'), ('fee-gap', 'sy', 'FUTU', '2026-11-21T12:00:00'),
            ('oct', 'sy', 'AMD', '2026-10-20T12:00:00')]:
        key = ContractKey.from_values(broker='futu', account=account, underlying_symbol=symbol,
            option_type='put', strike=100, expiration_ymd='2026-12-18')
        for kind, instant, price in [('open', ms(day) - 1000, 2), ('assignment', ms(day), 0)]:
            e = _event(name + '-' + kind, kind, time_ms=instant, price=price,
                basis=None if name == 'fee-gap' else 'actual', target_lot_id=name + '-lot' if kind == 'assignment' else None,
                raw={'stock_settlement': {'side': 'buy', 'shares': 100, 'price': 100, 'fees': 0,
                     'fee_provenance': {'basis': 'actual', 'source': 'fixture'}}} if kind == 'assignment' else {})
            e = replace(e, contract_key=key, lot_id=name + '-lot' if kind == 'open' else None)
            events.append(api.trade_event_application_payload(e.to_dict()))
    data = {'trade_events': events, 'account_assigned_stock_events': [], 'diagnostics': []}
    monkeypatch.setattr(report.ledger_api, 'read_assigned_stock_query_inputs', lambda repo: data)
    db = runtime / 'ledger.sqlite3'
    inbox = str(db) + '.trade_intake_inbox.sqlite3'
    invalid = _sell()
    if currency_fault:
        invalid['currency'] = 'HKD'  # instrument_ref remains independently USD
    else:
        invalid.pop('external_id_namespace')
    invalid['occurred_at_utc'] = '2026-11-10T01:00:00Z'
    _inbox(inbox, [(invalid, 'unresolved')])
    monkeypatch.setattr(report, 'resolve_runtime_root', lambda **kw: SimpleNamespace(runtime_root=code))
    monkeypatch.setattr(report, 'assigned_stock_quote_is_usable', lambda *a, **kw: True)
    monkeypatch.setattr(report, 'project_exchange_rate_snapshot', lambda *a, **kw: {'pairs': {
        'USDCNY': {'display_eligible': True, 'quote_at_utc': '2026-11-25T04:50:00Z', 'rate': '7', 'source': 'fixture'}}})
    before = {p: p.read_bytes() for p in runtime.rglob('*') if p.is_file()}
    def forbidden(*a, **kw):
        pytest.fail('must not fetch quotes or FX')
    monkeypatch.setattr(report, 'current_exchange_rate_snapshot', forbidden)
    result = report.build_assignment_period_report(SimpleNamespace(path=db), {
        **period, 'refresh_quotes': False, 'quote_snapshots': [
            {'symbol': symbol, 'spot': 105, 'quote_time_ms': now - 1000} for symbol in ('NVDA', 'FUTU', 'AMD')]},
        cfg=cfg, repo_base=lambda: code, quote_state_base_dir=runtime,
        refresh_assigned_stock_quotes=forbidden, now_ms=lambda: now)
    assert result['row_count'] == (4 if 'assignment_year' in period else 3)
    lots = {r['source_assignment_event_id']: r for r in result['rows']}
    assert lots['old-assignment']['quantity_status'] == 'pending'
    assert lots['old-assignment']['stock_realized_pnl'] is None
    new = lots['new-assignment']
    assert new['quantity_status'] == ('pending' if missing_state else 'ledger_only')
    gap = lots['fee-gap-assignment']
    assert gap['stock_unrealized_pnl'] == '500'
    assert gap['remaining_pnl_with_put'] is None
    assert gap['confirmed']['shares_remaining'] is None
    assert gap['provisional']['shares_remaining'] == 100
    assert gap['provisional']['basis'] == 'ledger'
    assert result['source']['intake']['source_history'] == 'unknown'
    assert len(result['source']['intake']['state_sources']) == (1 if missing_state else 2)
    count = (1 if missing_state else 2) + int('assignment_year' in period)
    stock = result['summary']['by_currency']['USD']['stock_unrealized_pnl']
    assert stock['value'] == f'{count * 500:.2f}' and stock['coverage'] == 'partial'
    assert result['summary']['cny_estimate']['metrics']['stock_unrealized_pnl']['value'] == f'{count * 3500:.2f}'
    assert result['summary']['cny_estimate']['metrics']['stock_unrealized_pnl']['coverage'] == 'partial'
    assert all(p.read_bytes() == content for p, content in before.items())


def test_two_frozen_observation_instants_do_not_mix_assignment_cohorts():
    # Synthetic temporal fixture only: no prices/account evidence are replayed.
    events = []
    first, later = ms('2026-11-25T12:51:00'), ms('2026-11-25T14:00:00')
    for i in range(8):
        at = ms('2026-11-24T12:00:00') if i < 7 else ms('2026-11-25T13:47:47.855')
        count = 2 if i == 0 else 1
        settlement = {'stock_settlement': {'side': 'buy', 'shares': count * 100, 'price': 100,
            'fees': 0, 'fee_provenance': {'basis': 'actual', 'source': 'synthetic'}}}
        for kind in ('open', 'assignment'):
            e = _event(f'{i}-{kind}', kind, contracts=count, time_ms=at - (kind == 'open'),
                price=2 if kind == 'open' else 0, basis='actual', raw=settlement if kind == 'assignment' else {},
                target_lot_id=f'lot-{i}' if kind == 'assignment' else None)
            events.append(api.trade_event_application_payload(
                replace(e, lot_id=f'lot-{i}' if kind == 'open' else None,
                    contract_key=ContractKey.from_values(broker='futu', account='sample', underlying_symbol='VTI',
                        option_type='put', strike=100, expiration_ymd='2026-12-18')).to_dict()))
    data = {'trade_events': events, 'account_assigned_stock_events': []}
    for cutoff, count, shares in ((first, 7, 800), (later, 8, 900)):
        lots = api.project_assigned_stock_lifecycle_from_rows(data, as_of_ms=cutoff)['assigned_stock_lots']
        assert len(lots) == count
        assert sum(row['shares_remaining'] for row in lots) == shares


@pytest.mark.parametrize('account', [None, 'lx', 'sy'])
@pytest.mark.parametrize('period', [{'assignment_year': 2026}, {'assignment_month': '2026-11'}])
def test_public_period_replays_late_void_without_reviving_replaced_open(tmp_path, monkeypatch, account, period):
    from copy import deepcopy

    from src.application.ledger.repository import SQLiteOptionPositionsRepository
    from src.application.tool_execution import execute_tool
    from tests.test_agent_plugin_smoke import _public_cfg_with_futu

    monkeypatch.setenv('OM_RUNTIME_ROOT', str(tmp_path))
    monkeypatch.setattr('src.application.agent_tools.positions.assert_quality_allows', lambda *a, **kw: None)
    now = datetime.fromisoformat('2026-11-25T15:00:00+00:00')
    stamp = int(now.timestamp() * 1000)

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz) if tz else now.replace(tzinfo=None)

    monkeypatch.setattr(report, 'datetime', FrozenDateTime)
    db = tmp_path / 'output_shared/state/option_positions.sqlite3'
    writer = SQLiteOptionPositionsRepository(db)
    for name, owner, symbol, day in [('old', 'lx', 'NVDA', '2026-11-01T12:00:00'),
            ('new', 'lx', 'NVDA', '2026-11-22T12:00:00'),
            ('fee-gap', 'sy', 'FUTU', '2026-11-21T12:00:00')]:
        key = ContractKey.from_values(broker='futu', account=owner, underlying_symbol=symbol,
            option_type='put', strike=100, expiration_ymd='2026-12-18')
        opening = replace(_event(name + '-open', 'open', time_ms=ms(day) - 1000, price=2,
            basis=None if name == 'fee-gap' else 'actual', raw={'strategy': 'sell_put'}),
            contract_key=key, lot_id=name + '-lot')
        writer.upsert_trade_event(opening)
        if name == 'old':
            # Both opens have the same economic time/lot. The later control
            # invalidates only the replaced event, including at a past boundary.
            writer.upsert_trade_event(replace(opening, event_id='replaced-open',
                raw_payload={**opening.raw_payload, 'strategy': 'wheel'}))
            writer.upsert_trade_event(replace(opening, event_id='late-void', event_type='void',
                event_time_ms=ms('2026-11-20T12:00:00'), contracts=0, price=0,
                lot_id=None, target_event_id='replaced-open', raw_payload={}))
        assignment = _event(name + '-assignment', 'assignment', time_ms=ms(day), price=0,
            target_lot_id=name + '-lot', basis=None if name == 'fee-gap' else 'actual',
            raw={'stock_settlement': {'side': 'buy', 'shares': 100, 'price': 100, 'fees': 0,
                'fee_provenance': {'basis': 'actual', 'source': 'fixture'}}})
        writer.upsert_trade_event(replace(assignment, contract_key=key))

    data_config = tmp_path / 'portfolio.runtime.json'
    data_config.write_text('{}')
    cfg = _public_cfg_with_futu(str(data_config))
    settings = cfg['account_settings'].pop('user1')
    cfg['accounts'] = ['lx', 'sy']
    cfg['account_settings'] = {a: deepcopy(settings) for a in cfg['accounts']}
    cfg['portfolio']['account'] = 'lx'
    for owner in cfg['accounts']:
        cfg['account_settings'][owner]['futu']['account_id'] = 'test-' + owner
        state = tmp_path / f'output_shared/state/trade_intake/{owner}/state.json'
        state.parent.mkdir(parents=True)
        state.write_text(json.dumps({'unresolved_deal_ids': {}, 'failed_deal_ids': {}}))
    cfg['trade_intake']['account_mapping']['futu'] = {'test-lx': 'lx', 'test-sy': 'sy'}
    config = tmp_path / 'config.us.json'
    config.write_text(json.dumps(cfg))
    sells = []
    for owner in cfg['accounts']:
        observation = _sell(owner, 'test-' + owner, 'unknown-' + owner)
        observation['occurred_at_utc'] = '2026-11-10T01:00:00Z'
        sells.append((observation, 'unresolved'))
    _inbox(str(db) + '.trade_intake_inbox.sqlite3', sells)
    monkeypatch.setattr(report, 'assigned_stock_quote_is_usable', lambda *a, **kw: True)
    monkeypatch.setattr(report, 'project_exchange_rate_snapshot', lambda *a, **kw: {'pairs': {
        'USDCNY': {'display_eligible': True, 'quote_at_utc': '2026-11-25T14:59:00Z',
                   'rate': '7', 'source': 'fixture'}}})

    def forbidden(*a, **kw):
        pytest.fail('must not request quotes or FX')

    monkeypatch.setattr(report, 'current_exchange_rate_snapshot', forbidden)
    monkeypatch.setattr('src.application.agent_tools.positions.refresh_assigned_stock_quotes', forbidden)
    before = {p: p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    response = execute_tool('option_positions_read', {
        'config_path': str(config), 'action': 'assigned-stock', 'broker': '富途', 'status': 'all',
        **period, **({'account': account} if account else {}), 'refresh_quotes': False,
        'quote_snapshots': [{'symbol': s, 'spot': 105, 'quote_time_ms': stamp - 1000}
                            for s in ('NVDA', 'FUTU')],
    })
    assert response['ok'], response
    result = response['data']
    lots = {r['source_assignment_event_id']: r for r in result['rows']}
    assert set(lots) == ({'old-assignment', 'new-assignment'} if account == 'lx'
                        else {'fee-gap-assignment'} if account == 'sy'
                        else {'old-assignment', 'new-assignment', 'fee-gap-assignment'})
    if account != 'sy':
        assert lots['old-assignment']['quantity_status'] == 'pending'
        assert lots['old-assignment']['stock_unrealized_pnl'] is None
        assert lots['old-assignment']['provisional'] is None
        assert lots['new-assignment']['quantity_status'] == 'ledger_only'
        assert lots['new-assignment']['stock_unrealized_pnl'] == '500'
        assert lots['new-assignment']['confirmed']['shares_remaining'] is None
        assert lots['new-assignment']['provisional']['shares_remaining'] == 100
    if account != 'lx':
        gap = lots['fee-gap-assignment']
        assert gap['stock_unrealized_pnl'] == '500'
        assert gap['remaining_pnl_with_put'] is None
        assert gap['quantity_status'] == 'ledger_only'
    amount = '1000.00' if account is None else '500.00'
    stock = result['summary']['by_currency']['USD']['stock_unrealized_pnl']
    assert stock['value'] == amount and stock['coverage'] == 'partial'
    assert result['summary']['cny_estimate']['metrics']['stock_unrealized_pnl']['value'] == (
        '7000.00' if account is None else '3500.00')
    assert result['coverage']['source_history'] == 'unknown'
    # SQLite may update shared-memory reader marks in WAL mode even for a
    # mode=ro/query_only connection. The ledger/WAL, inbox and state stay intact.
    assert not [str(p.relative_to(tmp_path)) for p, content in before.items()
                if not p.name.endswith('-shm') and p.read_bytes() != content]
