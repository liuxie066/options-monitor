"""The JSON entry reaches config, fetch normalization and policy with an offline gateway."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json

import pandas as pd
import pytest

from src.application import option_candidates as app, opend_symbol_fetching as fetch
from src.application.short_vol_metrics import RealizedVolatilitySnapshot, TermMatchedRVObservation
from src.interfaces.agent import cli


@pytest.fixture(autouse=True)
def fake_config_freshness(monkeypatch):
    # These fixtures focus on profile/fetch integration; real config freshness is
    # covered through both public entry paths in test_bot_option_candidates.
    monkeypatch.setattr(app, 'ensure_runtime_config_freshness', lambda *a, **kw: None)


@pytest.fixture
def configured(monkeypatch, tmp_path):
    config = {'_generated': {'market': 'us'}, 'templates': {'base': {
        'fetch': {'source': 'futu', 'host': 'fixture.invalid', 'port': 11111},
        'sell_put': {'enabled': False, 'min_dte': 7, 'max_dte': 45, 'min_strike': 95, 'max_strike': 100},
    }}, 'symbols': [{'symbol': 'NVDA', 'use': 'base'}]}
    original = deepcopy(config)
    monkeypatch.setattr(app, 'load_runtime_config', lambda **kw: (tmp_path / 'config.us.json', config))
    yield config
    assert config == original


def test_prepare_inheritance_overrides_defaults_and_known_route(configured, tmp_path):
    context = app.QueryContext(runtime_root=tmp_path)
    now = datetime.now(timezone.utc)
    query = app.normalize_queries({'queries': [{'symbol': 'US.NVDA', 'strategy': 'short_put'}]})[0]
    item = app._prepare(query, context, now)
    assert item.filters == {'strike': None, 'min_strike': 95, 'max_strike': 100, 'dte': [7, 45],
                            'strike_source': 'config', 'dte_source': 'config'}
    explicit = app._prepare({**query, 'strike': 120, 'dte': [1, 90]}, context, now)
    assert explicit.filters['min_strike'] == explicit.filters['max_strike'] == 120
    assert explicit.filters['dte'] == [1, 90]
    assert explicit.config.min_iv_rv_ratio == item.config.min_iv_rv_ratio
    other = app._prepare({**query, 'symbol': 'MSFT'}, context, now)
    assert other.endpoint == item.endpoint
    assert other.filters['dte'] == [7, 60]
    assert other.filters['min_strike'] is None


def test_cli_facade_reaches_real_fetch_policy_and_bounded_envelope(configured, monkeypatch, tmp_path, capsys):
    now = datetime.now(timezone.utc)
    day = app.earnings_calendar_scan_date('US', now)
    expiry = (day + timedelta(days=30)).isoformat()
    code = f"US.NVDA{day + timedelta(days=30):%y%m%d}P100000"
    calls = []

    class Gateway:
        def get_snapshot(self, codes):
            calls.append(('snapshot', codes))
            if codes == ['US.NVDA']:
                return pd.DataFrame([{'code': 'US.NVDA', 'last_price': 110, 'update_time': now.isoformat(),
                                      'sec_status': 'NORMAL', 'suspension': False}])
            return pd.DataFrame([{'code': value, 'bid_price': 3, 'ask_price': 3.1, 'last_price': 3,
                                  'update_time': now.isoformat(), 'sec_status': 'NORMAL', 'suspension': False,
                                  'price_spread': .01, 'option_contract_size': 100,
                                  'option_implied_volatility': 30, 'option_delta': -.3} for value in codes])

        def get_market_state(self, codes):
            return pd.DataFrame([{'code': value, 'market_state': 'MORNING'} for value in codes])

        def get_option_expiration_dates(self, underlier):
            return pd.DataFrame([{'strike_time': expiry}])

        def get_option_chain(self, **kwargs):
            calls.append(('chain', kwargs))
            return pd.DataFrame([{'code': code, 'strike_time': expiry, 'strike_price': 100, 'option_type': 'PUT',
                                  'lot_size': 100, 'option_standard_type': 'STANDARD', 'stock_owner': 'US.NVDA',
                                  'stock_type': 'DRVT', 'suspension': False}])

        def get_earnings_calendar(self, **kwargs):
            return pd.DataFrame(columns=['security', 'earnings_date'])

        def close(self):
            calls.append(('close', None))

    term = TermMatchedRVObservation('term_matched_rv.v1', expiry, 'ok', None, .20, 22, 22,
                                   (day - timedelta(days=45)).isoformat(), day.isoformat(), 23, 22, 'a' * 64)
    monkeypatch.setattr(app, 'build_ready_futu_quote_gateway', lambda **kw: Gateway())
    monkeypatch.setattr(fetch, 'fetch_realized_volatility_snapshot', lambda *a, **kw:
                        RealizedVolatilitySnapshot(status='ok', term_matched={expiry: term}))
    monkeypatch.setattr(app, 'current_exchange_rate_snapshot', lambda **kw: {'pairs': {'USDCNY': {
        'rate': 7.2, 'source': 'tencent_quote', 'quote_at_utc': now.isoformat(), 'observed_at_utc': now.isoformat()}}})
    monkeypatch.setenv('OM_RUNTIME_ROOT', str(tmp_path))
    payload = {'queries': [{'symbol': 'NVDA', 'strategy': 'short_put', 'strike': 100}]}
    with app.option_candidates_context(runtime_root=tmp_path):
        assert cli.main(['run', '--tool', 'option_candidates', '--input-json', json.dumps(payload), '--no-local-env-file']) == 0
    response = json.loads(capsys.readouterr().out)
    assert response['ok'], response
    result = response['data']['results'][0]
    assert result['outcome'] == 'found', result
    assert result['coverage']['status'] == 'complete'
    candidate = result['candidates'][0]
    assert candidate['contract'] == code and candidate['multiplier'] == 100
    assert candidate['evidence']['iv'] == .30
    assert candidate['obligation']['price'] < 100
    assert result['earnings_coverage'] == 'complete'
    assert [entry[0] for entry in calls] == ['snapshot', 'chain', 'snapshot', 'close']
    assert not (tmp_path / 'output_shared/state/rate_cache.json').exists()


@pytest.mark.parametrize('strategy', ['short_put', 'short_call'])
@pytest.mark.parametrize('global_spread', [.10, .05])
def test_query_preserves_global_spread_gate(monkeypatch, tmp_path, strategy, global_spread):
    from test_option_candidates_contract import NOW, _bundle, _row

    side = 'sell_put' if strategy == 'short_put' else 'sell_call'
    policy = {'max_spread_ratio': .30}
    config = {'_generated': {'market': 'us'}, 'templates': {'base': {
        'fetch': {'source': 'futu', 'host': 'fixture.invalid', 'port': 11111},
        side: {'max_spread_ratio': global_spread}}},
        'symbols': [{'symbol': 'NVDA', 'use': 'base', side: policy}]}
    monkeypatch.setattr(app, 'load_runtime_config', lambda **kw: (tmp_path / 'config.us.json', config))
    monkeypatch.setattr(app, 'rates_for_purpose', lambda *a, **kw: {'USDCNY': 7.2})
    query = app.normalize_queries({'queries': [{'symbol': 'NVDA', 'strategy': strategy}]})[0]
    prepared = app._prepare(query, app.QueryContext(runtime_root=tmp_path), NOW)
    assert prepared.config.max_spread_ratio == global_spread
    row = _row(mode=strategy.removeprefix('short_'), bid=3, ask=3.7)
    result = app._evaluate(prepared, _bundle([row]), {}, 0, NOW)
    assert result['outcome'] == 'none'
    assert result['coverage']['excluded'] == 1
    assert any('spread' in item['code'] for item in result['exclusion_summary'])


def test_configured_dte_over_query_limit_is_unavailable_without_io(monkeypatch, tmp_path):
    config = {'_generated': {'market': 'us'}, 'symbols': [{
        'symbol': 'NVDA', 'fetch': {'source': 'futu'}, 'sell_put': {'max_dte': 366}}]}
    monkeypatch.setattr(app, 'load_runtime_config', lambda **kw: (tmp_path / 'config.us.json', config))
    monkeypatch.setattr(app, 'build_ready_futu_quote_gateway', lambda **kw: pytest.fail('unexpected quote I/O'))
    with app.option_candidates_context(runtime_root=tmp_path):
        data, _, _ = app.option_candidates_tool({'queries': [{'symbol': 'NVDA', 'strategy': 'short_put'}]})
    assert data['results'][0]['outcome'] == 'unable'
    assert data['results'][0]['reason_codes'] == ['query_range_exceeded']


def test_query_does_not_reenable_retired_pricing_config(monkeypatch, tmp_path):
    config = {'_generated': {'market': 'us'}, 'symbols': [{
        'symbol': 'NVDA', 'fetch': {'source': 'futu'},
        'sell_put': {'pricing': {'max_spread_ratio': .20}}}]}
    monkeypatch.setattr(app, 'load_runtime_config', lambda **kw: (tmp_path / 'config.us.json', config))
    monkeypatch.setattr(app, 'build_ready_futu_quote_gateway', lambda **kw: pytest.fail('unexpected quote I/O'))
    with app.option_candidates_context(runtime_root=tmp_path):
        data, _, _ = app.option_candidates_tool({'queries': [{'symbol': 'NVDA', 'strategy': 'short_put'}]})
    assert data['results'][0]['outcome'] == 'unable'
    assert data['results'][0]['reason_codes'] == ['configuration_unavailable']
