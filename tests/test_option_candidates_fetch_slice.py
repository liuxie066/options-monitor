from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
import threading
import time

import pandas as pd
import pytest

from src.application import opend_symbol_fetching as fetch
from src.application import option_chain_fetching as chains
from src.application import opend_market_snapshot_fetching as snapshots
from src.application import option_candidates as candidates
from src.application.opend_fetch_config import OpenDFetchLimits
from src.application.opend_market_snapshot_fetching import fetch_option_snapshots, get_underlier_observation_opend
from src.application.opend_rate_gate import OpenDRateGate
from src.application.opening_quote_evidence import normalize_underlier_observation
from src.application.sell_put_strategy_risk import resolve_sell_put_underwriting_config
from src.application.short_vol_metrics import fetch_realized_volatility_snapshot
from src.infrastructure import futu_gateway as retry


DAY = date(2026, 6, 1)
EXPIRIES = ['2026-06-19', '2026-09-18']


def _filter(strike=100, *, side='put', low=1, high=365):
    return dict(option_type=side, min_strike=strike, max_strike=strike, min_dte=low, max_dte=high)


class Gateway:
    def __init__(self, expiries=EXPIRIES):
        self.expiries = list(expiries)
        self.chain_calls = []
        self.snapshot_calls = []
        self.expiry_calls = 0
        self.chain_rows = None

    def get_option_expiration_dates(self, _code):
        self.expiry_calls += 1
        return pd.DataFrame({'strike_time': self.expiries})

    def get_option_chain(self, **kwargs):
        self.chain_calls.append(kwargs)
        if self.chain_rows is not None:
            return pd.DataFrame(self.chain_rows)
        return pd.DataFrame([
            dict(code=f"{kwargs['code']}.{kwargs['start']}.{side}.{strike}", strike_time=kwargs['start'],
                 option_type=side, strike_price=strike, lot_size=100)
            for side in ('PUT', 'CALL') for strike in (100, 105, 110)
            if kwargs.get('option_type', side) == side
        ])

    def get_snapshot(self, codes):
        self.snapshot_calls.append(list(codes))
        return pd.DataFrame([dict(code=code, bid_price=1, ask_price=2, last_price=1.5) for code in codes])


def _request(tmp_path, gateway, **kwargs):
    values = dict(symbol='NVDA', gateway=gateway, base_dir=tmp_path, trading_date=DAY.isoformat(),
                  spot_override=120, query_cache=fetch.QueryFetchCache(), option_types='put,call',
                  query_filters=[_filter()], no_retry=True)
    values["underlier_observation"] = dict(
        schema_version="opening_underlier_observation.v1", code='US.NVDA', market='US', last_price=120,
        update_time='2026-06-01 10:00:00', observed_at_utc=datetime.now(timezone.utc).isoformat(),
        age_seconds=0, market_state='MORNING', sec_status='NORMAL', suspension=False, status='ready', reason_code=None,
    )
    values.update(kwargs)
    return fetch.FetchSymbolRequest(**values)


@pytest.fixture(autouse=True)
def offline_limiter(monkeypatch):
    # Tests below explicitly instantiate the real gate when testing waiting semantics.
    monkeypatch.setattr(chains.FileRateLimiter, 'acquire', lambda self, **kwargs: retry.check_fetch_control(**kwargs) or 0.0)


def test_exact_union_filters_side_strike_and_dte_before_quotes(tmp_path):
    gateway = Gateway()
    request = _request(tmp_path, gateway, query_filters=[_filter(100, high=30), _filter(110, side='call', low=60)])
    result = fetch.fetch_symbol_request(request)
    codes = [code for batch in gateway.snapshot_calls for code in batch]
    assert len(codes) == 2, result
    assert codes == ['US.NVDA.2026-06-19.PUT.100', 'US.NVDA.2026-09-18.CALL.110']
    assert result['meta']['query_scope_validated'] is True
    assert result['meta']['expiration_enumeration_complete'] is True
    assert result['meta']['enumeration_complete'] is True


def test_same_side_exact_strikes_do_not_quote_gap(tmp_path):
    gateway = Gateway(EXPIRIES[:1])
    result = fetch.fetch_symbol_request(_request(tmp_path, gateway, query_filters=[_filter(100), _filter(110)]))
    assert [row['strike'] for row in result['rows']] == [100, 110]
    assert all('.105' not in code for batch in gateway.snapshot_calls for code in batch)


def test_sibling_reuse_preserves_receipts_and_full_chain_satisfies_single_side(tmp_path):
    gateway = Gateway(EXPIRIES[:1])
    request = _request(tmp_path, gateway, query_filters=[_filter(100), _filter(110)])
    first = fetch.fetch_symbol_request(request)
    second = fetch.fetch_symbol_request(replace(request, query_filters=[_filter(100)], option_types='put'))
    assert (gateway.expiry_calls, len(gateway.chain_calls), len(gateway.snapshot_calls)) == (1, 1, 1)
    assert len(request.query_cache.chain_units) == 1
    assert len(request.query_cache.quote_units) == 2
    assert second['rows'][0]['snapshot_received_at_utc'] == first['rows'][0]['snapshot_received_at_utc']
    assert second['meta']['source_observed_at'] == first['meta']['source_observed_at']
    assert second['meta']['snapshot_opend_call_count'] == 0


def test_cache_identity_isolates_endpoint_and_trading_day(tmp_path):
    gateway = Gateway(EXPIRIES[:1])
    request = _request(tmp_path, gateway)
    fetch.fetch_symbol_request(request)
    fetch.fetch_symbol_request(replace(request, port=22222))
    fetch.fetch_symbol_request(replace(request, trading_date='2026-06-02'))
    assert len(gateway.chain_calls) == 3
    assert len(gateway.snapshot_calls) == 3


@pytest.mark.parametrize('expiries', [[], ['2027-01-01']])
def test_known_full_discovery_filtered_empty_never_fetches_all_chain(tmp_path, expiries):
    gateway = Gateway(expiries)
    result = fetch.fetch_symbol_request(_request(tmp_path, gateway, query_filters=[_filter(high=30)]))
    assert result['meta']['source_outcome'] == 'success_empty'
    assert result['meta']['expiration_enumeration_complete'] is True
    assert result['meta']['query_scope_validated'] is True
    assert result['meta']['requested_expirations'] == []
    assert not gateway.chain_calls and not gateway.snapshot_calls


def test_filtered_strike_empty_is_validated_success_empty(tmp_path):
    result = fetch.fetch_symbol_request(_request(tmp_path, Gateway(EXPIRIES[:1]), query_filters=[_filter(999)]))
    assert result['meta']['source_outcome'] == 'success_empty'
    assert result['meta']['query_scope_validated'] is True


def test_parse_failure_does_not_attest_empty_and_does_not_poison_sibling_cache(tmp_path):
    gateway = Gateway(EXPIRIES[:1])
    gateway.chain_rows = [dict(code='bad', option_type='PUT', strike_price='bad', strike_time=EXPIRIES[0])]
    request = _request(tmp_path, gateway)
    first = fetch.fetch_symbol_request(request)
    assert first['meta']['source_outcome'] == 'parse_error'
    assert first['meta']['expiration_statuses'][EXPIRIES[0]] == 'error'
    assert not first['meta']['enumeration_complete']
    assert first['meta']['query_scope_validated'] is True
    gateway.chain_rows = None
    second = fetch.fetch_symbol_request(request)
    assert second['rows']
    assert len(gateway.chain_calls) == 2


def test_partial_long_expiry_preserves_short_expiry_enumeration(tmp_path):
    gateway = Gateway()
    original = gateway.get_option_chain
    def chain(**kwargs):
        if kwargs['start'] == EXPIRIES[1]:
            raise RuntimeError('provider unavailable')
        return original(**kwargs)
    gateway.get_option_chain = chain
    result = fetch.fetch_symbol_request(_request(tmp_path, gateway))
    assert result['meta']['query_scope_validated'] is True
    assert result['meta']['expiration_enumeration_complete'] is True
    assert result['meta']['enumeration_complete'] is False
    assert result['meta']['expiration_statuses'] == {EXPIRIES[0]: 'fetched', EXPIRIES[1]: 'error'}
    assert result['meta']['available_expirations'] == EXPIRIES
    assert result['rows']


@pytest.mark.parametrize('peer_failure', ['provider', 'parse'])
@pytest.mark.parametrize('short_expiry', [True, False], ids=['empty_expiry', 'no_expiry'])
def test_empty_short_scope_evaluates_complete_despite_failed_long_peer(tmp_path, monkeypatch, peer_failure, short_expiry):
    now = datetime(2026, 6, 1, 14, tzinfo=timezone.utc)
    observation = normalize_underlier_observation(
        code='US.NVDA', market='US', now_utc=now,
        snapshot_row=dict(code='US.NVDA', last_price=120, update_time=now.isoformat(),
                          sec_status='NORMAL', suspension=False),
        market_state_row=dict(code='US.NVDA', market_state='MORNING'),
    )
    assert observation.status == 'ready'
    gateway = Gateway(EXPIRIES if short_expiry else EXPIRIES[1:])
    short_max_dte = 30 if short_expiry else 5

    def chain(**kwargs):
        gateway.chain_calls.append(kwargs)
        if kwargs['start'] == EXPIRIES[0]:
            return pd.DataFrame()
        if peer_failure == 'provider':
            raise RuntimeError('provider unavailable')
        return pd.DataFrame([dict(code='bad', strike_time=EXPIRIES[1], option_type='PUT', strike_price='bad')])

    monkeypatch.setattr(gateway, 'get_option_chain', chain)
    monkeypatch.setattr(candidates, 'rates_for_purpose', lambda *args, **kwargs: {'USDCNY': 7.2})
    bundle = fetch.fetch_symbol_request(_request(
        tmp_path, gateway, option_types='put', underlier_observation=observation.to_dict(),
        query_filters=[_filter(high=short_max_dte), _filter(low=60, high=180)],
    ))
    assert bundle['rows'] == []
    assert bundle['meta']['expiration_enumeration_complete'] is True
    assert bundle['meta']['available_expirations'] == gateway.expiries
    assert bundle['meta']['expiration_statuses'] == {
        **({EXPIRIES[0]: 'empty'} if short_expiry else {}), EXPIRIES[1]: 'error',
    }
    assert [call['start'] for call in gateway.chain_calls] == gateway.expiries
    assert bundle['meta']['query_scope_validated'] is True
    assert bundle['meta']['enumeration_complete'] is False
    assert not gateway.snapshot_calls

    results = []
    for index, (low, high) in enumerate(((1, short_max_dte), (60, 180))):
        config = replace(resolve_sell_put_underwriting_config({}), min_dte=low, max_dte=high,
                         min_strike=100, max_strike=100)
        prepared = candidates.PreparedQuery(
            dict(symbol='NVDA', market='US', strategy='short_put'), config,
            dict(dte=[low, high], strike=100), ('offline.invalid', 11111), DAY,
        )
        results.append(candidates._evaluate(prepared, bundle, {}, index, now))
    short, long = results
    assert short['outcome'] == 'none'
    assert short['coverage'] == dict(status='complete', enumeration_complete=True, discovered=0,
                                     evaluated=0, matched=0, excluded=0, unassessed=0)
    assert short['reason_codes'] == ['no_contracts_in_range']
    assert long['outcome'] == 'unable'
    assert long['coverage']['status'] == 'unavailable'
    assert long['coverage']['enumeration_complete'] is False
    assert long['coverage']['unassessed'] is None
    assert 'enumeration_incomplete' in long['reason_codes']


def test_global_chain_limit_counts_unique_work_before_51st_provider_call(tmp_path):
    gateway = Gateway([(DAY + timedelta(days=i)).isoformat() for i in range(1, 52)])
    request = _request(tmp_path, gateway)
    result = fetch.fetch_symbol_request(request)
    assert result['meta']['error_code'] == 'query_range_exceeded'
    assert len(gateway.chain_calls) == 50
    assert not gateway.snapshot_calls
    assert result['meta']['query_scope_validated'] is False


def test_global_quote_limit_stops_before_quotes_and_is_shared_across_symbols(tmp_path):
    gateway = Gateway(EXPIRIES[:1])
    request = _request(tmp_path, gateway)
    request.query_cache.quote_units.update({('earlier-symbol', i) for i in range(1000)})
    result = fetch.fetch_symbol_request(request)
    assert result['meta']['error_code'] == 'query_range_exceeded'
    assert not gateway.snapshot_calls
    assert len(request.query_cache.quote_units) == 1000


def test_partial_snapshots_fetch_only_missing_on_sibling_and_keep_old_receipt(tmp_path):
    gateway = Gateway()
    cache = {}
    calls = []
    def snapshot(codes):
        calls.append(codes)
        return pd.DataFrame([dict(code=code, bid_price=1, ask_price=2) for code in codes if len(calls) > 1 or code == 'A'])
    gateway.get_snapshot = snapshot
    kwargs = dict(option_codes=['A', 'B'], gateway=gateway, base_dir=tmp_path,
                  snapshot_limit=OpenDFetchLimits.from_flat_kwargs().market_snapshot,
                  snapshot_cache=cache, snapshot_fallback_max_codes=0)
    first = fetch_option_snapshots(**kwargs)
    second = fetch_option_snapshots(**kwargs)
    assert calls == [['A', 'B'], ['B']]
    assert first.missing_codes == {'B'} and second.complete
    assert second.code_receipts['A'] == first.code_receipts['A']


@pytest.mark.parametrize('fallback_mode', [None, 'empty', 'error'])
def test_each_snapshot_batch_and_fallback_preserves_its_receipt_in_cache(tmp_path, monkeypatch, fallback_mode):
    clock = [datetime(2026, 6, 1, 14, tzinfo=timezone.utc)]

    class Clock:
        @classmethod
        def now(cls, tz):
            return clock[0]

    monkeypatch.setattr(snapshots, 'datetime', Clock)
    actual_receipts = {}
    attempts = {}
    calls = []

    def snapshot(codes):
        calls.append(list(codes))
        assert len(codes) == 1
        code = codes[0]
        requested_at = clock[0].isoformat()
        clock[0] += timedelta(seconds=20)
        attempts[code] = attempts.get(code, 0) + 1
        if fallback_mode and code in {'B', 'C'} and attempts[code] == 1:
            if fallback_mode == 'error':
                raise RuntimeError('batch unavailable')
            return pd.DataFrame()
        actual_receipts[code] = (requested_at, clock[0].isoformat())
        return pd.DataFrame([dict(code=code, bid_price=1, ask_price=2)])

    # Even an old cached observation must retain its original evidence time.
    old_time = (clock[0] - timedelta(seconds=1000)).isoformat()
    cache = {'CACHED': (dict(code='CACHED', bid_price=1, ask_price=2), old_time, old_time)}
    kwargs = dict(gateway=SimpleNamespace(get_snapshot=snapshot), base_dir=tmp_path,
                  snapshot_limit=OpenDFetchLimits.from_flat_kwargs().market_snapshot,
                  snapshot_batch_size=1, snapshot_fallback_batch_size=1, snapshot_cache=cache, no_retry=True)
    first = fetch_option_snapshots(option_codes=['CACHED', 'A', 'B', 'C'], **kwargs)
    expected = {'CACHED': (old_time, old_time), **actual_receipts}
    assert first.complete
    assert first.code_receipts == expected
    assert first.fallback_filled == (2 if fallback_mode else 0)
    assert calls == [['A'], ['B'], ['C']] + ([['B'], ['C']] if fallback_mode else [])
    assert {code: value[1:] for code, value in cache.items()} == expected
    assert first.received_at_utc == old_time
    frozen_cache = dict(cache)

    clock[0] += timedelta(seconds=100)
    second = fetch_option_snapshots(option_codes=['CACHED', 'A', 'B', 'C', 'D'], **kwargs)
    assert second.complete
    assert second.opend_call_count == 1 and calls[-1] == ['D']
    assert second.code_receipts == {**expected, 'D': actual_receipts['D']}
    assert {code: cache[code] for code in frozen_cache} == frozen_cache

    clock[0] += timedelta(seconds=100)
    third = fetch_option_snapshots(option_codes=['CACHED', 'A', 'B', 'C', 'D'], **kwargs)
    assert third.opend_call_count == 0
    assert third.code_receipts == second.code_receipts


@pytest.mark.parametrize('stage', ['expiration', 'chain', 'snapshot'])
def test_late_provider_success_or_failure_stops_next_io(tmp_path, stage):
    gateway = Gateway()
    stop = threading.Event()
    name = {'expiration': 'get_option_expiration_dates', 'chain': 'get_option_chain', 'snapshot': 'get_snapshot'}[stage]
    original = getattr(gateway, name)
    def late(*args, **kwargs):
        value = original(*args, **kwargs)
        stop.set()
        return value
    setattr(gateway, name, late)
    request = _request(tmp_path, gateway, cancelled=stop.is_set, snapshot_batch_size=1)
    with pytest.raises(TimeoutError, match='cancelled'):
        fetch.fetch_symbol_request(request)
    if stage == 'expiration':
        assert not gateway.chain_calls
    if stage == 'chain':
        assert len(gateway.chain_calls) == 1 and not gateway.snapshot_calls
    if stage == 'snapshot':
        assert len(gateway.snapshot_calls) == 1


def test_underlier_cancel_stops_before_market_state(tmp_path):
    stop = threading.Event()
    def snapshot(codes):
        stop.set()
        raise RuntimeError('late error')
    gateway = SimpleNamespace(get_snapshot=snapshot)
    with pytest.raises(TimeoutError):
        get_underlier_observation_opend(gateway, 'US.NVDA', market='US', base_dir=tmp_path, cancelled=stop.is_set)


def test_expired_deadline_stops_before_gateway_call(tmp_path):
    gateway = Gateway()
    with pytest.raises(TimeoutError, match='deadline'):
        fetch.fetch_symbol_request(_request(tmp_path, gateway, deadline=time.monotonic()-1))
    assert not gateway.chain_calls and gateway.expiry_calls == 0


def test_retry_wait_clips_deadline_and_never_retries_after_cancel(monkeypatch):
    now = [10.0]
    sleeps = []
    calls = []
    monkeypatch.setattr(retry.time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(retry.random, 'uniform', lambda *_: 0)
    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds
    monkeypatch.setattr(retry.time, 'sleep', sleep)
    def failed():
        calls.append(1)
        raise retry.FutuGatewayTransientError('transport')
    with pytest.raises(TimeoutError, match='deadline'):
        retry.retry_futu_gateway_call('test', failed, deadline=10.2, quiet=True)
    assert sleeps == pytest.approx([0.2])
    assert len(calls) == 1


def test_shared_gate_deadline_and_cancellation_do_not_change_identity(tmp_path):
    gate = OpenDRateGate(max_calls=1, window_sec=30, max_wait_sec=10, label='test', state_path=tmp_path/'limiter.json')
    gate.acquire()
    with pytest.raises(TimeoutError, match='deadline'):
        gate.acquire(deadline=time.monotonic()+0.02)
    assert not gate._waiters
    assert len(gate._timestamps) == 1
    stop = threading.Event()
    timer = threading.Timer(0.02, stop.set)
    timer.start()
    try:
        with pytest.raises(TimeoutError, match='cancelled'):
            gate.acquire(cancelled=stop.is_set)
    finally:
        timer.join()
    assert not gate._waiters


def test_rv_reuses_facts_and_expands_history_for_expiry_union(tmp_path):
    from tests.test_short_vol_metrics import _Gateway, _sessions_around, _prices
    past, future = _sessions_around(DAY, before=500, after=280)
    gateway = _Gateway(calendar=past+future, prices=_prices(past))
    cache = {}
    args = dict(gateway=gateway, underlier_code='US.NVDA', trading_day=DAY, market='US',
                base_dir=tmp_path, facts_cache=cache)
    short_exp = future[19].isoformat()
    long_exp = future[250].isoformat()
    short = fetch_realized_volatility_snapshot(**args, expirations=[short_exp])
    long = fetch_realized_volatility_snapshot(**args, expirations=[short_exp, long_exp])
    assert short.status == long.status == 'ok'
    assert long.term_matched[long_exp].lookback_sessions > short.term_matched[short_exp].lookback_sessions
    assert long.term_matched[short_exp].input_hash == short.term_matched[short_exp].input_hash
    assert len(gateway.history_calls) == 2
    assert gateway.history_calls[1]['end'] == gateway.history_calls[0]['start']
    assert gateway.history_calls[1]['start'] < gateway.history_calls[0]['start']
    calendar_calls, history_calls = len(gateway.calendar_calls), len(gateway.history_calls)
    again = fetch_realized_volatility_snapshot(**args, expirations=[short_exp, long_exp])
    assert again.term_matched == long.term_matched
    assert (len(gateway.calendar_calls), len(gateway.history_calls)) == (calendar_calls, history_calls)


def test_history_cancellation_stops_pagination_and_cache_write(tmp_path):
    stop = threading.Event()
    def history(**kwargs):
        stop.set()
        return {'data': [{'time_key': '2026-05-29', 'close': 100}], 'page_req_key': 'next'}
    gateway = SimpleNamespace(request_history_kline=history)
    with pytest.raises(TimeoutError):
        fetch_realized_volatility_snapshot(gateway, underlier_code='US.NVDA', trading_day=DAY,
                                           base_dir=tmp_path, cancelled=stop.is_set)
    assert not (tmp_path/'cache').exists()


@pytest.mark.parametrize('status,reason', [('market_closed', 'market_closed'), ('data_unavailable', 'underlier_quote_stale'), ('data_unavailable', 'market_state_missing_or_invalid')])
def test_query_unassessable_underlier_stops_before_chain_history_and_quotes(tmp_path, status, reason):
    gateway = Gateway()
    request = _request(tmp_path, gateway)
    observation = {**request.underlier_observation, 'status': status, 'reason_code': reason}
    result = fetch.fetch_symbol_request(replace(request, underlier_observation=observation, include_realized_volatility=True))
    assert not gateway.chain_calls and not gateway.snapshot_calls and gateway.expiry_calls == 0
    assert result['meta']['reason_code'] == reason
    assert result['meta']['expiration_enumeration_complete'] is False
    assert result['meta']['query_scope_validated'] is False


def test_quote_constructor_cancel_after_handshake_prevents_global_state_and_closes_once(monkeypatch):
    stop = threading.Event()
    calls = []
    class Quote:
        def get_global_state(self):
            calls.append('global')
            return 0, {'program_status_type': 'READY', 'qot_logined': True}
        def close(self):
            calls.append('close')
    def build_context(**kwargs):
        assert kwargs['cancelled'] == stop.is_set
        stop.set()
        return Quote()
    monkeypatch.setattr(retry, 'build_futu_quote_context', build_context)
    with pytest.raises(TimeoutError):
        retry.build_ready_futu_quote_gateway(cancelled=stop.is_set)
    assert calls == ['close']


def test_quote_constructor_cancel_during_port_probe_never_opens_context(monkeypatch):
    stop = threading.Event()
    calls = []
    monkeypatch.setattr(retry, '_ensure_futu_api_importable', lambda: None)
    import sys
    monkeypatch.setitem(sys.modules, 'futu', SimpleNamespace(OpenQuoteContext=lambda **_: calls.append('context')))
    def probe(*_):
        stop.set()
        return True
    monkeypatch.setattr(retry, 'port_open', probe)
    with pytest.raises(TimeoutError):
        retry.build_futu_quote_context(host='offline', port=1, cancelled=stop.is_set)
    assert not calls


def test_quote_handshake_cancel_closes_context_without_next_connection_poll():
    stop = threading.Event()
    calls = []
    class Context:
        def set_sync_query_connect_timeout(self, _):
            pass
        def get_sync_conn_id(self):
            calls.append('poll')
            stop.set()
            return None
        def close(self):
            calls.append('close')
    with pytest.raises(TimeoutError):
        retry._wait_for_futu_context(Context(), host='offline', port=1, cancelled=stop.is_set)
    assert calls == ['poll', 'close']


def test_fetch_diagnostics_count_retries_phases_and_reuse(tmp_path, monkeypatch):
    gateway = Gateway(EXPIRIES[:1])
    original = gateway.get_option_chain
    attempt = []
    def transient(**kwargs):
        attempt.append(1)
        if len(attempt) == 1:
            raise retry.FutuGatewayTransientError('network transient')
        return original(**kwargs)
    monkeypatch.setattr(gateway, 'get_option_chain', transient)
    monkeypatch.setattr(retry.time, 'sleep', lambda _: None)
    request = _request(tmp_path, gateway, no_retry=False)
    first = fetch.fetch_symbol_request(request)
    assert first['meta']['fetch_diagnostics']['retry_count'] == 1
    assert first['meta']['fetch_diagnostics']['opend_calls']['option_chain'] == 2
    assert first['meta']['fetch_diagnostics']['opend_calls']['option_expiration'] == 1
    assert first['meta']['fetch_diagnostics']['opend_calls']['market_snapshot'] == 1
    assert set(first['meta']['fetch_diagnostics']['phase_ms']) >= {'underlier', 'expiration', 'chain', 'history_rv', 'snapshot'}
    second = fetch.fetch_symbol_request(request)
    assert second['meta']['fetch_diagnostics']['retry_count'] == 0
    assert all(value == 0 for value in second['meta']['fetch_diagnostics']['opend_calls'].values())
    assert second['meta']['fetch_diagnostics']['reuse_hits'] >= 3


def test_persistent_full_chain_cache_counts_once_and_reuses_for_sibling_side(tmp_path):
    gateway = Gateway(EXPIRIES[:1])
    request = _request(tmp_path, gateway, chain_cache=True)
    fetch.fetch_symbol_request(request)
    gateway.chain_calls.clear()
    shared = fetch.QueryFetchCache()
    put = fetch.fetch_symbol_request(replace(request, query_cache=shared, option_types='put'))
    call = fetch.fetch_symbol_request(replace(request, query_cache=shared, option_types='call', query_filters=[_filter(side='call')]))
    assert put['rows'] and call['rows']
    assert not gateway.chain_calls
    assert len(shared.chain_units) == 1
    assert shared.diagnostics['persistent_cache_hits'] >= 1
