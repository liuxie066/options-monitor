from copy import deepcopy
from decimal import Decimal

import pytest

import domain.domain.daily_decision_brief as domain
import src.application.daily_decision_brief_repository as repo
from test_daily_decision_brief_repository_v2 import MARKET_DATE, _brief, _persist, _prepare_fixed


@pytest.mark.parametrize("scoped", [False, True])
def test_exposure_normalizes_each_revision_once_without_sharing_results(tmp_path, monkeypatch, scoped):
    _prepare_fixed(tmp_path, _persist(tmp_path))
    args = dict(base=tmp_path, account="lx", market="US")
    scope = repo.DailyBriefReadScope(**args) if scoped else None
    expected = repo.read_combo_candidate_exposures(
        **args, market_trading_date=MARKET_DATE, read_scope=scope,
    )
    original = domain._normalize_daily_decision_brief
    calls = []

    def counted(raw, **kwargs):
        calls.append((raw["account"], raw["market"], raw["market_trading_date"], raw["revision"]))
        return original(raw, **kwargs)

    monkeypatch.setattr(domain, "_normalize_daily_decision_brief", counted)
    actual = repo.read_combo_candidate_exposures(
        **args, market_trading_date=MARKET_DATE, read_scope=scope,
    )
    assert actual == expected
    assert calls == [("lx", "US", MARKET_DATE, 0)]
    actual["exposures"].append({"caller": "mutation"})
    calls.clear()
    assert repo.read_combo_candidate_exposures(
        **args, market_trading_date=MARKET_DATE, read_scope=scope,
    ) == expected
    assert calls == [("lx", "US", MARKET_DATE, 0)]


@pytest.mark.parametrize("retired", [False, True])
def test_fresh_normalized_digest_matches_original_encoding_without_extra_safe_pass(monkeypatch, retired):
    raw = _brief(run_id="numeric-digest")
    raw["positions"] = [{"nested": [Decimal("1.25"), float("nan"), float("inf"), float("-inf"), ("中文", 3)]}]
    if retired:
        raw[domain.RETIRED_DAILY_BRIEF_FIELDS[0]] = {"opaque": [Decimal("2.5"), float("nan")]}
    normalized = domain.normalize_persisted_daily_decision_brief(raw)
    assert normalized == domain.normalize_persisted_daily_decision_brief(normalized)
    payload = {key: value for key, value in normalized.items()
               if key not in {"generated_at_utc", "data_as_of_utc", "run_id"}}
    assert payload == domain._json_safe(payload)
    expected = [domain._digest(payload)]
    if retired:
        expected.append(domain._digest({**payload, domain.RETIRED_DAILY_BRIEF_FIELDS[0]: raw[domain.RETIRED_DAILY_BRIEF_FIELDS[0]]}))
    original = domain._json_safe
    digest_safe_calls = []

    def counted(value):
        if isinstance(value, dict) and "candidate_index" in value:
            digest_safe_calls.append(value)
        return original(value)

    monkeypatch.setattr(domain, "_json_safe", counted)
    actual, digests = domain.normalize_persisted_daily_decision_brief_with_digests(raw)
    assert actual == normalized
    assert digests == tuple(expected)
    # One mandatory normalize traversal; only raw legacy overlays need another.
    assert len(digest_safe_calls) == 1 + int(retired)


@pytest.mark.parametrize("field,value,message", [
    ("account", "sy", "identity mismatch"),
    ("market", "HK", "identity mismatch"),
    ("market_trading_date", "2026-07-22", "date mismatch"),
    ("revision", 1, "revision mismatch"),
    ("schema_version", "invalid", "incompatible"),
])
def test_revision_digest_reuse_preserves_repository_validation(tmp_path, monkeypatch, field, value, message):
    source = deepcopy(_persist(tmp_path)["brief"])
    source[field] = value
    monkeypatch.setattr(repo, "_history_raw", lambda **kwargs: source)
    result = repo.read_daily_decision_brief(
        base=tmp_path, account="lx", market="US", market_trading_date=MARKET_DATE, revision=0,
    )
    assert set(result) == {"available", "reason", "error", "brief", "path"}
    assert result["available"] is False
    assert result["reason"] == "state_invalid"
    assert result["brief"] is None
    assert message in result["error"]
