"""Broker-bound valuation regressions; every provider boundary is isolated."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from domain.domain.portfolio_assignment_scenario import project_assignment_scenario
from src.application import portfolio_assignment_scenario as app
from test_portfolio_assignment_scenario import _evidence, _option, _quote


@pytest.fixture
def broker_inputs(monkeypatch):
    now = datetime(2026, 11, 1, 1, tzinfo=timezone.utc)
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now
    monkeypatch.setattr(app, 'datetime', Clock)
    context = {
        'portfolio_source_name': 'longbridge', 'portfolio_source_account': 'lx',
        'capacity_authority': {
            'schema_version': 'physical_account_capacity_authority.v2',
            'source': 'longbridge', 'status': 'available', 'broker_id': 'longbridge',
            'broker_account_id': 'LB-123', 'logical_account': 'lx', 'trd_env': 'REAL', 'market': 'us',
        },
        'cash_snapshot': {'status': 'fresh', 'reason_codes': [], 'max_age_sec': 300,
                          'source_observed_at': now.isoformat(), 'evaluated_at': now.isoformat()},
        'cash_by_currency': {'USD': 1000}, 'cash_source': 'longbridge.available_cash', 'source_observed_at': now.isoformat(),
        'exchange_rates': {'rates': {'USDCNY': 7}},
        'position_snapshot_input': {
            'schema_version': 'position_snapshot.v1', 'snapshot_id': 'lb-stock-1',
            'source_id': 'longbridge.positions', 'observed_at_utc': now.isoformat(),
            'source_as_of_utc': now.isoformat(), 'completeness': 'complete',
            'quality': {'status': 'ready'}, 'errors': [], 'evidence_refs': [],
            'broker_account_ref': {'broker_id': 'longbridge', 'broker_account_id': 'longbridge:REAL:LB-123',
                                   'external_account_id': 'LB-123', 'environment': 'REAL', 'account_label': 'lx'},
            'scope': {'markets': ['US'], 'asset_types': ['stock'], 'filtered': False},
            'rows': [{'instrument_ref': {'asset_type': 'stock', 'symbol': 'NVDA', 'market': 'US', 'currency': 'USD'},
                      'quantity': '10', 'position_side': 'long'}],
        },
    }
    config = {'_resolved': {'market': 'us'}, 'accounts': ['lx'], 'account_settings': {'lx': {
        'type': 'longbridge', 'longbridge': {'account_id': 'LB-123', 'credentials_env_prefix': 'TEST_LB'}}}}
    fx = {'rates': {'USDCNY': 7}, 'source': 'fixture', 'timestamp': now.isoformat()}
    quote = _quote('NVDA', currency='USD', price=10, cny_price=70, fx=7, source='shared_quote')
    monkeypatch.setattr(app, '_read_futu_quotes', lambda *a, **kw: ([quote], []))
    # Any unmocked service read is a test failure.
    monkeypatch.setattr(app, 'read_portfolio_valuation_evidence', lambda **kw: pytest.fail('unexpected PM read'))
    from src.infrastructure.longbridge_gateway import credential_binding_fingerprint
    from src.application.portfolio_context_service import evaluate_account_cash_snapshot
    settings = config['account_settings']['lx']['longbridge']
    settings.update(region='ap', trd_env='REAL')
    env = {f'TEST_LB_{key}': f'fixture-{key.lower()}' for key in ('APP_KEY', 'APP_SECRET', 'ACCESS_TOKEN')}
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    fingerprint = credential_binding_fingerprint(settings, environ=env)
    settings['credential_binding_sha256'] = fingerprint
    context['capacity_authority'].update(credential_binding_sha256=fingerprint,
        binding_method='user_confirmed_credentials_sha256', broker_echoed=False)
    context.update(filters={'account': 'lx'}, source_account_identifiers=['LB-123'],
        cash_source_observed_at=now.isoformat(), cash_balance_reliable=True,
        cash_balance_unavailable_by_row={}, cash_source_observation_status='trusted')
    context['cash_snapshot'] = evaluate_account_cash_snapshot(context, config=config, account='lx', evaluated_at=now)
    assert context['cash_snapshot']['status'] == 'fresh', context['cash_snapshot']
    return context, config, fx


def collect(inputs, **kwargs):
    context, config, fx = inputs
    return app.collect_assignment_portfolio_evidence(accounts=['lx'], runtime_config=config,
        futu_contexts={'lx': context}, option_positions=[], fx_observation=fx, capacity_fx=fx, **kwargs)


def test_direct_longbridge_valuation_keeps_broker_and_real_source(broker_inputs):
    result = collect(broker_inputs)
    assert result['status'] == 'partial', result
    assert any('total cash assets are unverified' in warning for warning in result['warnings'])
    assert {row['broker'] for row in result['holdings']} == {'长桥'}
    assert {row['source'] for row in result['holdings']} == {'longbridge_available_cash', 'longbridge_positions'}
    assert result['holdings_sources'] == ['longbridge']
    assert result['direct_broker_bindings']['lx']['broker_echoed'] is False
    assert not any('futu' in name for name in result['freshness']['dataset_ids'])
    output = project_assignment_scenario(accounts=['lx'], portfolio_evidence=result, option_positions=[], snapshot={})
    assert output['cash_coverage']['available_cash_and_mmf_cny'] == '7000.00'
    assert output['distribution']['net_assets_cny'] is None
    assert output['distribution']['status'] == 'partial'
    assert next(row for row in output['distribution']['rows'] if row['category'] == 'cash')['value_cny'] is None
    assert output['position_sizing']['net_assets_cny'] is None


@pytest.mark.parametrize('problem', ['schema', 'source', 'broker', 'account', 'physical', 'partial', 'stale', 'cash', 'fingerprint', 'binding', 'broker_echoed'])
def test_invalid_direct_evidence_never_reaches_quotes(broker_inputs, monkeypatch, problem):
    context, _, _ = broker_inputs
    if problem in {'schema', 'source', 'broker'}:
        key = {'schema': 'schema_version', 'source': 'source', 'broker': 'broker_id'}[problem]
        context['capacity_authority'][key] = 'futu'
    elif problem in {'fingerprint', 'binding', 'broker_echoed'}:
        key = {'fingerprint': 'credential_binding_sha256', 'binding': 'binding_method', 'broker_echoed': 'broker_echoed'}[problem]
        context['capacity_authority'][key] = 'unverified'
    elif problem == 'account':
        context['capacity_authority']['logical_account'] = 'sy'
    elif problem == 'physical':
        context['position_snapshot_input']['broker_account_ref']['external_account_id'] = 'other'
    elif problem == 'partial':
        context['position_snapshot_input']['completeness'] = 'partial'
    elif problem == 'stale':
        context['position_snapshot_input']['observed_at_utc'] = (app.datetime.now(timezone.utc) - timedelta(seconds=301)).isoformat()
    else:
        context['cash_snapshot']['status'] = 'unknown'
    monkeypatch.setattr(app, '_read_futu_quotes', lambda *a, **kw: pytest.fail('invalid evidence reached quote I/O'))
    result = collect(broker_inputs)
    assert result['status'] == 'unavailable'
    assert result['holdings'] == []


def test_public_query_selects_configured_broker(broker_inputs, monkeypatch, tmp_path):
    context, config, fx = broker_inputs
    monkeypatch.setattr(app, '_load_runtime_and_positions', lambda a: ([], str(tmp_path / 'config.us.json'), config))
    monkeypatch.setattr(app, 'current_exchange_rate_snapshot', lambda **kw: fx)
    monkeypatch.setattr(app, 'project_exchange_rate_snapshot', lambda *a, **kw: fx)
    calls = []
    def load(**kw):
        calls.append(kw)
        return context
    monkeypatch.setattr(app, 'load_account_portfolio_context', load)
    result = app.query_portfolio_assignment_scenario(['lx'])
    assert calls[0]['portfolio_source'] == 'longbridge'
    assert calls[0]['write_cache'] is False
    assert result['distribution']['net_assets_cny'] is None
    assert result['cash_coverage']['available_cash_and_mmf_cny'] == '7000.00'
    assert '仅富途' not in app.render_assignment_scenario_text(result)


@pytest.mark.parametrize('kind,shares,gross', [('put', '110', '1400.00'), ('call', '-90', '12600.00')])
def test_direct_longbridge_assignment_uses_baseline_without_futu_fees(broker_inputs, kind, shares, gross):
    evidence = collect(broker_inputs)
    option = {**_option('lb-lot', option_type=kind, symbol='NVDA', strike=8, currency='USD'), 'broker': 'longbridge'}
    result = project_assignment_scenario(accounts=['lx'], portfolio_evidence=evidence, option_positions=[option], snapshot={})
    assert result['position_changes'][0]['ending_shares'] == shares
    assert result['cash_coverage']['ending_cash_gross_cny'] == gross
    assert result['cash_coverage']['ending_cash_net_estimated_cny'] is None
    fee = result['fee_summary']['items'][0]
    assert fee['reason'] == 'unsupported_broker_fee_schedule'
    assert fee['calculator'] is None and fee['source'] is None
    assert result['fee_summary']['total_fees_cny'] is None
    assert result['account_breakdown'][0]['ending_cash_gross_cny'] == gross
    assert result['expiration_ladder'][0]['projected_ending_cash_net_cny'] is None
    assert result['status'] == 'partial'


@pytest.mark.parametrize('partial', [False, True])
def test_pm_deduplication_is_explicit_account_and_broker_scoped(broker_inputs, monkeypatch, partial):
    from cash_evidence_helpers import cash_portfolio
    context, config, fx = broker_inputs
    config['portfolio'] = {'holdings': {'enabled': True}}
    other = cash_portfolio({'cash_by_currency': {'CNY': 0}, 'source_observed_at': app.datetime.now(timezone.utc).isoformat()}, account='sy')
    duplicate = {'account': ' LX ', 'broker': '長橋證券', 'code': 'NVDA', 'asset_type': 'stock',
                 'currency': 'USD', 'quantity': '10', 'market_value_cny': '700', 'record_id': 'duplicate'}
    pm = _evidence(holdings=[duplicate,
        {**duplicate, 'account': 'lx', 'broker': 'IB', 'record_id': 'other-broker'},
        {**duplicate, 'account': 'sy', 'record_id': 'other-account'},
        {**duplicate, 'account': '', 'record_id': 'missing-account'},
        {**duplicate, 'broker': '', 'record_id': 'missing-broker'}], status='partial' if partial else 'complete')
    pm['scope'].update(accounts=['lx', 'sy'], holdings_scope='non_futu',
        broker_inventory={a: {'brokers': []} for a in ['lx', 'sy']}, holding_counts={a: {} for a in ['lx', 'sy']})
    pm['warnings'] = ['PM source partial'] if partial else []
    monkeypatch.setattr(app, 'read_portfolio_valuation_evidence', lambda **kw: pm)
    result = app.collect_assignment_portfolio_evidence(accounts=['lx', 'sy'], runtime_config=config,
        futu_contexts={'lx': context, 'sy': other}, option_positions=[], fx_observation=fx, capacity_fx=fx)
    pm_rows = [row for row in result['holdings'] if row.get('record_id')]
    assert {row['record_id'] for row in pm_rows} == {'other-broker', 'other-account', 'missing-account', 'missing-broker'}
    assert result['pm_supplement']['excluded_direct_broker_rows'] == 1
    assert result['pm_supplement']['included_rows'] == 4
    assert result['status'] == 'partial'
    assert any('identity is missing' in warning for warning in result['warnings'])
    if partial:
        assert 'PM source partial' in result['warnings']
        assert result['pm_supplement']['status'] == 'partial'


def test_pm_all_duplicate_rows_does_not_erase_partial_source(broker_inputs, monkeypatch):
    context, config, _ = broker_inputs
    config['portfolio'] = {'holdings': {'enabled': True}}
    pm = _evidence(holdings=[{'account': 'lx', 'broker': 'Longbridge', 'code': 'NVDA'}], status='partial')
    pm['scope'].update(holdings_scope='non_futu', broker_inventory={'lx': {'brokers': []}}, holding_counts={'lx': {}})
    monkeypatch.setattr(app, 'read_portfolio_valuation_evidence', lambda **kw: pm)
    result = collect(broker_inputs)
    assert result['pm_supplement']['included_rows'] == 0
    assert result['pm_supplement']['status'] == result['status'] == 'partial'
    assert result['freshness']['trust_status'] == 'partial'
    assert len(result['holdings']) == 2


def test_missing_requested_account_fails_closed(broker_inputs):
    _, config, fx = broker_inputs
    result = app.collect_assignment_portfolio_evidence(accounts=['lx'], runtime_config=config,
        futu_contexts={}, option_positions=[], fx_observation=fx)
    assert result['status'] == 'unavailable'
    assert result['holdings'] == []


def test_longbridge_pm_cash_does_not_prove_direct_stock_baseline():
    evidence = _evidence(holdings=[{'account': 'lx', 'broker': 'longbridge', 'code': 'USD',
        'asset_type': 'cash', 'currency': 'USD', 'quantity': 1000, 'market_value_cny': 7000}],
        quotes=[_quote('NVDA', currency='USD', price=10, cny_price=70, fx=7)])
    option = {**_option('lb-lot', option_type='call', symbol='NVDA', strike=8, currency='USD'), 'broker': 'longbridge'}
    result = project_assignment_scenario(accounts=['lx'], portfolio_evidence=evidence, option_positions=[option], snapshot={})
    assert result['position_changes'][0]['ending_shares'] is None
    assert result['position_sizing']['net_assets_cny'] is None
    assert any('terminal baseline' in warning for warning in result['warnings'])


@pytest.mark.parametrize('failure', ['price', 'fx', 'capacity_fx'])
def test_longbridge_missing_valuation_inputs_are_unknown(broker_inputs, monkeypatch, failure):
    _, _, fx = broker_inputs
    if failure == 'price':
        monkeypatch.setattr(app, '_read_futu_quotes', lambda *a, **kw: ([], ['NVDA: quote unavailable']))
    elif failure == 'fx':
        broker_inputs[0]['exchange_rates'] = {'rates': {}}
        fx['rates'] = {}
    result = collect(broker_inputs)
    if failure == 'capacity_fx':
        result['capacity_fx_rates_to_cny'] = {}
    output = project_assignment_scenario(accounts=['lx'], portfolio_evidence=result, option_positions=[], snapshot={})
    if failure == 'capacity_fx':
        assert output['cash_coverage']['fx_status'] == 'unavailable'
        assert output['cash_coverage']['available_cash_and_mmf_cny'] is None
    else:
        assert output['position_sizing']['net_assets_cny'] is None
        assert output['distribution']['net_assets_cny'] is None


def test_longbridge_short_stock_remains_signed(broker_inputs):
    broker_inputs[0]['position_snapshot_input']['rows'][0]['position_side'] = 'short'
    result = collect(broker_inputs)
    stock = next(row for row in result['holdings'] if row['asset_type'] == 'stock')
    assert stock['quantity'] == '-10' and Decimal(stock['market_value_cny']) == -700
    output = project_assignment_scenario(accounts=['lx'], portfolio_evidence=result, option_positions=[], snapshot={})
    assert output['distribution']['liabilities_cny'] is None
    assert output['distribution']['liabilities'][0]['liability_cny'] == '700.00'
    assert output['position_sizing']['net_assets_cny'] is None


def test_longbridge_option_quote_planning_uses_ledger_underlier(broker_inputs, monkeypatch):
    context, config, fx = broker_inputs
    context['position_snapshot_input']['rows'] = []
    seen = []
    def quotes(codes, **kwargs):
        seen.extend(codes)
        return [_quote('NVDA', currency='USD', price=10, cny_price=70, fx=7)], []
    monkeypatch.setattr(app, '_read_futu_quotes', quotes)
    option = {**_option('lb-lot', option_type='put', symbol='NVDA', strike=8, currency='USD'), 'broker': '长桥'}
    result = app.collect_assignment_portfolio_evidence(accounts=['lx'], runtime_config=config,
        futu_contexts={'lx': context}, option_positions=[option], fx_observation=fx, capacity_fx=fx)
    assert seen == ['NVDA']
    output = project_assignment_scenario(accounts=['lx'], portfolio_evidence=result, option_positions=[option], snapshot={})
    assert output['position_changes'][0]['ending_shares'] == '100'
    assert output['position_sizing']['net_assets_cny'] is None
    assert output['fee_summary']['items'][0]['source'] is None


def test_same_run_quote_recovery_cannot_erase_missing_total_cash_assets(broker_inputs, monkeypatch):
    from src.application.required_data_snapshot import FrozenRequiredDataBatch
    context, config, _ = broker_inputs
    now = app.datetime.now(timezone.utc)
    old = _quote('NVDA', currency='USD', price=10, cny_price=70, fx=7, source='futu_opend_market_snapshot')
    old.update(is_stale=True, valuation_quality='stale', observed_at=(now - timedelta(seconds=310)).isoformat())
    warning = app._dated_quote_warning('NVDA', old['observed_at'])
    monkeypatch.setattr(app, '_read_futu_quotes', lambda *a, **kw: ([old], [warning]))
    context['position_sizing_evidence'] = collect(broker_inputs)
    batch = FrozenRequiredDataBatch(manifest={'run_id': 'run-lb'}, manifest_bytes=b'', entries={}, unavailable={})
    before = deepcopy(context)
    updated = app.recover_position_sizing_quotes(context=context, runtime_config=config, account='lx', run_id='run-lb', quotes=batch)
    assert updated is context
    assert context == before
    assert any('total cash assets are unverified' in warning for warning in updated['position_sizing_evidence']['warnings'])


@pytest.mark.parametrize('kind,net', [(None, '18700.00'), ('put', '20100.00'), ('call', '17300.00')])
def test_verified_total_cash_values_assets_separately_from_free_cash_capacity(broker_inputs, kind, net):
    context, _, fx = broker_inputs
    context['cash_valuation'] = {'status': 'available', 'amount': 20000, 'currency': 'HKD',
        'source': 'longbridge.account_balance.total_cash', 'source_observed_at': context['cash_source_observed_at'],
        'scope': 'physical_account_aggregate', 'additive_with_cash_by_currency': False, 'errors': []}
    fx['rates']['HKDCNY'] = 0.9
    context['exchange_rates']['rates']['HKDCNY'] = 0.9
    evidence = collect(broker_inputs)
    assert evidence['status'] == 'complete', evidence
    cash_assets = [row for row in evidence['holdings'] if row['asset_type'] == 'cash']
    assert len(cash_assets) == 1 and cash_assets[0]['currency'] == 'HKD'
    assert cash_assets[0]['source'] == 'longbridge.account_balance.total_cash'
    assert evidence['capacity_cash_holdings'][0]['currency'] == 'USD'
    options = [{**_option('lb-lot', option_type=kind, symbol='NVDA', strike=8, currency='USD'), 'broker': '长桥'}] if kind else []
    output = project_assignment_scenario(accounts=['lx'], portfolio_evidence=evidence, option_positions=options, snapshot={})
    assert output['position_sizing']['net_assets_cny'] == net
    assert output['cash_coverage']['available_cash_and_mmf_cny'] == '7000.00'
    if kind:
        assert output['fee_summary']['total_fees_cny'] is None
        assert output['cash_coverage']['ending_cash_net_estimated_cny'] is None
    else:
        assert output['distribution']['net_assets_cny'] == net
        assert output['status'] == 'complete'


@pytest.mark.parametrize('problem', ['missing', 'amount', 'stale', 'currency', 'source', 'scope', 'additive', 'errors'])
def test_unverified_total_cash_never_becomes_complete_assets(broker_inputs, problem):
    context, _, _ = broker_inputs
    fact = {'status': 'available', 'amount': 2000, 'currency': 'USD',
        'source': 'longbridge.account_balance.total_cash', 'source_observed_at': context['cash_source_observed_at'],
        'scope': 'physical_account_aggregate', 'additive_with_cash_by_currency': False, 'errors': []}
    if problem != 'missing':
        key, value = {'amount': ('amount', True), 'stale': ('source_observed_at', '2020-01-01T00:00:00Z'),
            'currency': ('currency', ''), 'source': ('source', 'available_cash'), 'scope': ('scope', 'unknown'),
            'additive': ('additive_with_cash_by_currency', True), 'errors': ('errors', ['partial'])}[problem]
        fact[key] = value
        context['cash_valuation'] = fact
    result = collect(broker_inputs)
    assert result['status'] == 'partial'
    output = project_assignment_scenario(accounts=['lx'], portfolio_evidence=result, option_positions=[], snapshot={})
    assert output['position_sizing']['net_assets_cny'] is None
    assert output['distribution']['net_assets_cny'] is None
    assert output['cash_coverage']['available_cash_and_mmf_cny'] == '7000.00'


@pytest.mark.parametrize('mixed_market', [False, True])
def test_bound_adapter_to_valuation_preserves_total_and_available_cash(broker_inputs, monkeypatch, mixed_market):
    from src.application import longbridge_portfolio_context as adapter
    from src.application.portfolio_context_service import evaluate_account_cash_snapshot
    from test_longbridge_portfolio_context import Client, binding_env, bound_config, stock
    config = bound_config()
    for key, value in binding_env().items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(adapter, 'datetime', app.datetime)
    fx = {**broker_inputs[2], 'rates': {'HKDCNY': 0.9, 'USDCNY': 7}}
    client = Client()
    if mixed_market:
        client.rows.append(stock('700.HK', quantity='100', available='100'))
    context = adapter.fetch_longbridge_portfolio_context(cfg=config, account='demo', client=client,
        environ=binding_env(), exchange_rate_observation=fx)
    context['cash_snapshot'] = evaluate_account_cash_snapshot(context, config=config, account='demo',
        evaluated_at=app.datetime.now(timezone.utc))
    assert context['cash_snapshot']['status'] == 'fresh'
    def quotes(codes, **kwargs):
        assert set(codes) == ({'AAPL', '0700.HK'} if mixed_market else {'AAPL'})
        rows = [_quote('AAPL', currency='USD', price=10, cny_price=70, fx=7)]
        if mixed_market:
            rows.append(_quote('0700.HK', currency='HKD', price=10, cny_price=9, fx=0.9))
        return rows, []
    monkeypatch.setattr(app, '_read_futu_quotes', quotes)
    evidence = app.collect_assignment_portfolio_evidence(accounts=['demo'], runtime_config=config,
        futu_contexts={'demo': context}, option_positions=[], fx_observation=fx, capacity_fx=fx)
    assert client.calls == ['balance', 'positions']
    assert evidence['status'] == 'complete', evidence
    result = project_assignment_scenario(accounts=['demo'], portfolio_evidence=evidence, option_positions=[], snapshot={})
    expected_assets = '901634.10' if mixed_market else '900734.10'
    assert result['distribution']['net_assets_cny'] == expected_assets
    assert result['position_sizing']['net_assets_cny'] == expected_assets
    assert result['cash_coverage']['available_cash_and_mmf_cny'] == '-1753.50'
    assert result['distribution']['net_assets_cny'] != result['cash_coverage']['available_cash_and_mmf_cny']
