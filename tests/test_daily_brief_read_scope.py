from copy import deepcopy
import json

import pytest
from decision_history_fixtures import replace_history_payload, delete_history_revision

import src.application.daily_decision_brief_repository as repo
from test_daily_decision_brief_repository_v2 import _brief, _persist, _prepare_fixed, MARKET_DATE, TARGET_1000


def test_multiple_dates_share_one_validation_with_identical_results(tmp_path, monkeypatch):
    dates = [MARKET_DATE, '2026-07-22', '2026-07-23']
    _prepare_fixed(tmp_path, _persist(tmp_path))
    for date in dates[1:]:
        repo.persist_daily_decision_brief_success(base=tmp_path, brief=_brief(run_id=date, market_date=date))
    args = dict(base=tmp_path, account='lx', market='US')
    expected = [repo.read_combo_candidate_exposures(**args, market_trading_date=date) for date in dates]
    original = repo._normalize_delivery_state
    calls = []
    def counted(*a, **kw):
        calls.append(kw['account'])
        return original(*a, **kw)
    monkeypatch.setattr(repo, '_normalize_delivery_state', counted)
    scope = repo.DailyBriefReadScope(**args)
    actual = [repo.read_combo_candidate_exposures(**args, market_trading_date=date, read_scope=scope) for date in dates]
    assert actual == expected
    assert calls == ['lx']


def test_scope_copies_results_binds_identity_and_refreshes_next_request(tmp_path):
    args = dict(base=tmp_path, account='lx', market='US')
    scope = repo.DailyBriefReadScope(**args)
    assert repo.read_daily_decision_brief_delivery_state(**args, read_scope=scope)['reason'] == 'not_found'
    _prepare_fixed(tmp_path, _persist(tmp_path))
    # One observation is stable, even a previously missing one. A new request sees the write.
    assert repo.read_daily_decision_brief_delivery_state(**args, read_scope=scope)['reason'] == 'not_found'
    fresh = repo.DailyBriefReadScope(**args)
    result = repo.read_daily_decision_brief_delivery_state(**args, read_scope=fresh)
    expected = deepcopy(result)
    result['state']['days'].clear()
    assert repo.read_daily_decision_brief_delivery_state(**args, read_scope=fresh) == expected
    for override in ({'account': 'sy'}, {'market': 'HK'}, {'base': tmp_path / 'other'}):
        with pytest.raises(ValueError, match='identity mismatch'):
            repo.read_daily_decision_brief_delivery_state(**{**args, **override}, read_scope=fresh)
    with pytest.raises(ValueError, match='bounded'):
        repo.read_daily_decision_brief_delivery_state(**args, read_scope=fresh, bounded=True)


def test_fixed_recovery_shares_validation_and_preserves_invalid_state_error(tmp_path, monkeypatch):
    persisted = _persist(tmp_path)
    args = dict(base=tmp_path, account='lx', market='US')
    repo.record_daily_decision_brief_fixed_recovery(**args, market_trading_date=MARKET_DATE,
        scheduled_target_market=TARGET_1000, revision=persisted['current_revision'],
        brief_digest=persisted['current_brief_digest'], candidate_identities=[])
    _prepare_fixed(tmp_path, persisted)
    original = repo._normalize_delivery_state
    calls = []
    def counted(*a, **kw):
        calls.append(1)
        return original(*a, **kw)
    monkeypatch.setattr(repo, '_normalize_delivery_state', counted)
    scope = repo.DailyBriefReadScope(**args)
    retry = repo.read_retryable_daily_decision_brief_delivery(**args, market_trading_date=MARKET_DATE, read_scope=scope)
    assert retry['envelope']['status'] == 'pending'
    assert repo.read_daily_decision_brief_fixed_recovery(**args, market_trading_date=MARKET_DATE, read_scope=scope)['reason'] == 'none'
    assert len(calls) == 1
    path = retry['path']
    raw = json.loads(path.read_text())
    raw['schema_version'] = 'invalid'
    path.write_text(json.dumps(raw))
    invalid_scope = repo.DailyBriefReadScope(**args)
    assert not repo.read_daily_decision_brief_delivery_state(**args, read_scope=invalid_scope)['available']
    with pytest.raises(repo.DailyDecisionBriefStateError):
        repo.read_daily_decision_brief_fixed_recovery(**args, market_trading_date=MARKET_DATE, read_scope=invalid_scope)


@pytest.mark.parametrize('damage', ['digest', 'missing'])
def test_scoped_exposures_keep_broken_source_unavailable(tmp_path, damage):
    _prepare_fixed(tmp_path, _persist(tmp_path))
    args = dict(base=tmp_path, account='lx', market='US')
    revision = repo.read_daily_decision_brief(**args, market_trading_date=MARKET_DATE)['path']
    if damage == 'missing':
        delete_history_revision(tmp_path)
    else:
        raw = json.loads(revision.read_text())
        raw['strategy_summary'] = 'tampered'
        replace_history_payload(tmp_path, raw)
    expected = repo.read_combo_candidate_exposures(**args, market_trading_date=MARKET_DATE)
    actual = repo.read_combo_candidate_exposures(**args, market_trading_date=MARKET_DATE,
        read_scope=repo.DailyBriefReadScope(**args))
    assert actual == expected
    assert actual['complete'] is False


def test_exposure_uses_one_revision_read_after_delivery_observation(tmp_path, monkeypatch):
    _prepare_fixed(tmp_path, _persist(tmp_path))
    args = dict(base=tmp_path, account='lx', market='US')
    scope = repo.DailyBriefReadScope(**args)
    repo.read_daily_decision_brief_delivery_state(**args, read_scope=scope)
    original = repo._history_raw
    reads = []
    def counted(**kwargs):
        reads.append(kwargs["revision"])
        return original(**kwargs)
    monkeypatch.setattr(repo, "_history_raw", counted)
    repo.read_combo_candidate_exposures(**args, market_trading_date=MARKET_DATE, read_scope=scope)
    assert reads == [0]


def test_modern_digest_skips_identical_legacy_hash_but_keeps_legacy_fields(monkeypatch):
    import domain.domain.daily_decision_brief as domain
    original = domain._digest
    calls = []
    def counted(value):
        calls.append(value)
        return original(value)
    monkeypatch.setattr(domain, '_digest', counted)
    current = domain.daily_brief_compatible_digests(_brief(run_id='modern'))
    # Ignore normalization's action IDs; the payload digests have market_trading_date.
    assert len([v for v in calls if isinstance(v, dict) and 'candidate_index' in v]) == 1
    legacy = _brief(run_id='legacy')
    legacy[next(iter(domain.RETIRED_DAILY_BRIEF_FIELDS))] = {'original': 'value'}
    both = domain.daily_brief_compatible_digests(legacy)
    assert len(both) == 2 and both[0] == current[0]
