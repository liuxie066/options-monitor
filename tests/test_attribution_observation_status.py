from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json

import pytest

from cash_evidence_helpers import cash_config, cash_portfolio
from src.application.trades.attribution import _attribution_observation_status
from src.application.trades.listener_status import attribution_observations, observation_status
from src.application.quality.runtime_checks import build_runtime_checks
from src.application.agent_tools import runtime_status_impl as runtime
from test_runtime_status_trade_intake_availability import _runtime_fixture


@pytest.mark.parametrize('account', ['lx', 'sy'])
@pytest.mark.parametrize('market', ['hk', 'us'])
@pytest.mark.parametrize('failure,expected', [(None, 'ok'), ('provider', 'unavailable'),
    ('history', 'partial'), ('position', 'unavailable'), ('cash', 'unavailable')])
def test_observation_uses_existing_qualification_for_both_accounts_and_markets(account, market, failure, expected):
    now = datetime.now(timezone.utc)
    config = cash_config(account, '1001')
    config['_resolved']['market'] = market
    portfolio = cash_portfolio({'capacity_authority': {'market': market}}, account=account, account_id='1001')
    # A complete zero-position/zero-cash observation is legitimate evidence.
    observation = {'portfolio': portfolio}
    if failure == 'provider':
        observation = {'error': 'AssertionError: private-canary'}
    elif failure == 'history':
        observation['ordinary_evidence'] = {'error': 'TimeoutError: private-canary'}
    elif failure == 'position':
        portfolio['position_snapshot_input']['observed_at_utc'] = (now - timedelta(minutes=5)).isoformat()
    elif failure == 'cash':
        portfolio['cash_source_observed_at'] = (now - timedelta(days=2)).isoformat()
    before = deepcopy(observation)
    result = _attribution_observation_status(observation, account=account, market=market, config=config,
                                            ordinary_requested=failure == 'history')
    assert result['status'] == expected, result
    assert observation == before
    assert 'private-canary' not in json.dumps(result)


@pytest.mark.parametrize('status,code', [('partial', 'ATTRIBUTION_OBSERVATION_UNAVAILABLE'),
    ('unavailable', 'ATTRIBUTION_OBSERVATION_UNAVAILABLE'), ('unknown', 'ATTRIBUTION_OBSERVATION_UNKNOWN')])
def test_real_status_files_reach_public_warning_without_private_fields(tmp_path, monkeypatch, status, code):
    payload, dependencies, paths = _runtime_fixture(tmp_path, monkeypatch, ['empty', 'empty'])
    for index, path in enumerate(paths):
        status_path = path.with_name('status.json')
        data = json.loads(status_path.read_text())
        entry = observation_status(status, 'provider_unavailable') if index == 0 else observation_status('ok')
        entry['private'] = 'private-canary'
        data['attribution_recovery'] = {f'demo{index}': {'us': {'errors': [], 'observation': entry}}}
        status_path.write_text(json.dumps(data))
    payload['accounts'] = ['demo0']
    public, warnings, _ = runtime.runtime_status_tool(payload, **dependencies)
    rows = public['trade_intake']['attribution_observations']
    assert {row['account'] for row in rows} == {'demo0'}
    assert code in public['summary']['warning_codes']
    assert public['summary']['ok'] is False and warnings
    assert 'private-canary' not in json.dumps(public)
    assert public['trade_intake']['summary']['listener_status'] == 'listening'


@pytest.mark.parametrize('raw', [None, [], {}, {'status': 'ok'},
    {'status': 'ok', 'observed_at_utc': 'bad-time', 'reason_codes': []},
    {'status': 'ok', 'observed_at_utc': datetime.now(timezone.utc).isoformat(), 'reason_codes': ['private-canary']}])
def test_old_or_malformed_observation_is_unknown(raw):
    rows = attribution_observations({'attribution_recovery': {'qa': {'us': {'observation': raw}}}}, accounts=['qa'])
    assert rows[0]['status'] == 'unknown'
    assert 'private-canary' not in json.dumps(rows)


def test_requested_market_absence_is_unknown_even_if_other_market_succeeded():
    data = {'attribution_recovery': {'qa': {'us': {'observation': observation_status('ok')}}}}
    rows = attribution_observations(data, accounts=['qa'], market='hk')
    assert {row['market']: row['status'] for row in rows} == {'hk': 'unknown', 'us': 'ok'}


@pytest.mark.parametrize('state,age,expected', [('ok', 0, 'pass'), ('not_required', 0, 'pass'),
    ('partial', 0, 'warn'), ('unavailable', 0, 'warn'), ('cancelled', 0, 'unknown'),
    ('ok', 601, 'unknown'), ('ok', -60, 'unknown'), ('unknown', 0, 'unknown')])
def test_quality_observation_with_fresh_heartbeat_and_same_source_in_two_markets(state, age, expected):
    now = datetime.now(timezone.utc)
    row = {'id': 'qa', 'account': 'qa', 'enabled': True, 'runtime_identity': '/synthetic/instance',
           'summary': {'listener_status': 'listening', 'last_heartbeat_utc': now.isoformat(),
                       'attribution_observations': [observation_status(state, now=now - timedelta(seconds=age))]}}
    runtimes = [{'config': {'config_key': market}, 'trade_intake': {'enabled': True, 'sources': [deepcopy(row)]}}
                for market in ('us', 'hk')]
    before = deepcopy(runtimes)
    checks = build_runtime_checks(runtime_statuses=runtimes, observed_at_utc=now.isoformat(), now=now)
    listeners = [check for check in checks if check['check_id'] == 'RT-OM-002']
    assert len(listeners) == 1 and listeners[0]['status'] == expected
    assert runtimes == before
    runtimes[1]['trade_intake']['sources'][0]['runtime_identity'] = '/synthetic/other'
    checks = build_runtime_checks(runtime_statuses=runtimes, observed_at_utc=now.isoformat(), now=now)
    assert len([check for check in checks if check['check_id'] == 'RT-OM-002']) == 2


def test_real_recovery_keeps_provider_error_visible_when_item_errors_are_empty(tmp_path, monkeypatch):
    from test_trade_attribution_reconcile import _setup
    from src.application.trades import attribution
    repo, args, *_ = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr('src.application.wheel.capacity.observe_trade_attribution_capacity',
                        lambda **kwargs: {'error': 'provider_timeout'})
    result = attribution.reconcile_trade_attribution_account(repo, **args)
    assert result['errors'] == []
    assert result['observation']['status'] == 'unavailable'
    assert result['observation']['reason_codes'] == ['provider_timeout']


@pytest.mark.parametrize('bad', [None, 'scope', 'stale', 'window', 'old_portfolio', 'receipt', 'rejected'])
def test_history_observation_uses_ledger_envelope_qualification(tmp_path, monkeypatch, bad):
    from test_ordinary_trade_attribution import _scope, NOW, _iso
    from src.application.trades import attribution
    _repo, context = _scope(tmp_path, monkeypatch)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromtimestamp(NOW / 1000, timezone.utc)

    monkeypatch.setattr(attribution, 'datetime', Clock)
    observation = context['capacity_observation']
    evidence = observation['ordinary_evidence']
    if bad == 'scope':
        evidence['scope']['account'] = 'sy'
    elif bad == 'stale':
        evidence['observed_at_ms'] = NOW - 60_001
    elif bad == 'window':
        evidence['history']['window_end_utc'] = _iso(NOW - 1000)
    elif bad == 'old_portfolio':
        observation['portfolio']['position_snapshot_input']['observed_at_utc'] = _iso(NOW - 1)
    elif bad == 'receipt':
        evidence['history_orders']['scope'] = {'account': 'sy'}
    elif bad == 'rejected':
        evidence['history']['account_results'][0]['rejected_rows'] = [1]
    result = _attribution_observation_status(observation, account='lx', market='us',
        config=context['config'], ordinary_requested=True)
    assert result['status'] == ('ok' if bad is None else 'partial'), result


@pytest.mark.parametrize('missing,expected', [(True, 'unknown'), (False, 'fail')])
@pytest.mark.parametrize('reverse', [False, True])
def test_quality_duplicate_source_preserves_worst_heartbeat(missing, expected, reverse):
    now = datetime.now(timezone.utc)
    good = {'id': 'qa', 'account': 'qa', 'enabled': True, 'runtime_identity': '/synthetic/instance',
            'summary': {'listener_status': 'listening', 'last_heartbeat_utc': now.isoformat(),
                        'attribution_observations': [observation_status('ok', now=now)]}}
    bad = deepcopy(good)
    bad['summary']['last_heartbeat_utc'] = None if missing else (now - timedelta(seconds=1600)).isoformat()
    rows = [good, bad] if not reverse else [bad, good]
    runtimes = [{'trade_intake': {'enabled': True, 'sources': [row]}} for row in rows]
    checks = build_runtime_checks(runtime_statuses=runtimes, observed_at_utc=now.isoformat(), now=now)
    listeners = [check for check in checks if check['check_id'] == 'RT-OM-002']
    assert len(listeners) == 1 and listeners[0]['status'] == expected
    assert len(listeners[0]['observed']['attribution_observations']) == 1


def test_success_with_failure_reason_is_malformed():
    data = {'attribution_recovery': {'qa': {'us': {
        'observation': observation_status('ok', 'provider_unavailable')}}}}
    assert attribution_observations(data, accounts=['qa'])[0]['status'] == 'unknown'


def test_source_reports_absent_market_without_extra_provider_work(tmp_path, monkeypatch):
    from src.application.trades import attribution
    from src.application.ledger.repository import SQLiteOptionPositionsRepository
    repo = SQLiteOptionPositionsRepository(tmp_path / 'ledger.sqlite3')
    calls = []

    def reconcile(repo, **kwargs):
        calls.append(kwargs['market'])
        return {'next_cursor': '', 'observation': observation_status('not_required')}

    monkeypatch.setattr(attribution, 'reconcile_trade_attribution_account', reconcile)
    result = attribution.reconcile_trade_attribution_source(repo, config={'market': 'us', 'accounts': ['lx']},
        accounts=['lx'], runtime_root=tmp_path, inbox_path=tmp_path / 'inbox.sqlite3',
        combo_mode='confirm', cursors={})
    assert calls == ['us']
    assert {m: row['observation']['status'] for m, row in result['lx'].items()} == {
        'hk': 'not_required', 'us': 'not_required'}
