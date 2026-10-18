"""Recorded scan clocks cross the live adapter, immutable archive and offline core."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json

import pandas as pd
import pytest

from conftest import phase2_opening_row
from domain.domain.decision_state_fingerprint import canonical_sha256
from domain.domain.engine import CandidateCalculationError
from om_strategy_core import calculate_metrics
from src.application import candidate_scanning, scan_sell_call, scan_sell_put
from src.application.opening_candidate_snapshot import (
    OpeningCandidateSnapshotError, load_opening_candidate_snapshot, validate_opening_candidate_snapshot,
)
from test_opening_candidate_snapshot import _candidate, _scan_status, _seal_snapshot, _snapshot_path

NOW = datetime(2026, 8, 6, 9, 30, 0, 123456, tzinfo=timezone.utc)


def quote(mode="put", **changes):
    raw = phase2_opening_row({
        **_candidate(contract_symbol=f"US.NVDA260918{mode[0].upper()}90000", period_return=.03),
        "option_type": mode, "opening_contract_status": "ready",
        "snapshot_received_at_utc": NOW.isoformat(), "spot_observed_at_utc": NOW.isoformat(),
    })
    return {**raw, **changes}


def scan(mode, root, frame, decisions, **kwargs):
    common = dict(symbols=["NVDA"], input_root=root, min_annualized_net_return=0,
                  required_data_frames={"NVDA": frame}, calculation_decision_sink_fn=decisions.extend)
    if mode == "call":
        return scan_sell_call.run_sell_call_scan(**common, avg_cost=80, shares=300,
                                                shares_can_sell=200, shares_locked=100, **kwargs)
    return scan_sell_put.run_sell_put_scan(**common, **kwargs)


@pytest.fixture(autouse=True)
def no_calendar_io(monkeypatch):
    monkeypatch.setattr(candidate_scanning, "annotate_candidates_with_earnings_evidence", lambda rows, **kw: rows)


@pytest.mark.parametrize("mode", ["put", "call"])
def test_actual_capture_once_success_and_rejection(mode, tmp_path, monkeypatch):
    adapter = scan_sell_call if mode == "call" else scan_sell_put
    calls = []

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            calls.append(tz)
            return NOW + timedelta(days=len(calls) - 1)

    monkeypatch.setattr(adapter, "datetime", Clock)
    rows = [quote(mode, decision_now_utc="2000-01-01T00:00:00Z"),
            quote(mode, contract_symbol="US.NVDA260918P91000", opening_contract_status="ineligible")]
    source = pd.DataFrame(rows)
    original = source.copy(deep=True)
    rejects = []
    result = scan(mode, tmp_path, source, rejects)
    assert calls == [timezone.utc]
    assert len(result) == len(rejects) == 1
    assert result.iloc[0]["decision_now_utc"] == NOW.isoformat()
    assert rejects[0]["normalized_input"]["decision_now_utc"] == NOW.isoformat()
    assert rejects[0]["opening_decision"]["normalized_input"]["decision_now_utc"] == NOW.isoformat()
    pd.testing.assert_frame_equal(source, original)
    stored = result.iloc[0].to_dict()
    assert calculate_metrics(stored, mode=mode, avg_cost=80 if mode == "call" else None) == calculate_metrics(
        stored, mode=mode, avg_cost=80 if mode == "call" else None, now_utc=NOW)


@pytest.mark.parametrize("mode", ["put", "call"])
def test_separate_calls_preserve_their_own_time(mode, tmp_path):
    source = pd.DataFrame([quote(mode)])
    times = [NOW, NOW + timedelta(microseconds=1)]
    actual = [scan(mode, tmp_path, source, [], quote_freshness_now_utc=t).iloc[0]["decision_now_utc"] for t in times]
    assert actual == [t.isoformat() for t in times]


@pytest.mark.parametrize("bad", ["invalid", "2026-08-06T09:30:00", "", None, 123, True])
def test_recorded_clock_must_be_valid_even_with_explicit_time(bad):
    with pytest.raises(ValueError, match="decision_now_utc"):
        calculate_metrics(quote(decision_now_utc=bad), mode="put", now_utc=NOW)


def test_missing_conflicting_and_equivalent_clock():
    row = quote()
    with pytest.raises(ValueError, match="missing decision_now_utc"):
        calculate_metrics(row, mode="put")
    expected = calculate_metrics(row, mode="put", now_utc=NOW)
    row["decision_now_utc"] = NOW.isoformat()
    with pytest.raises(ValueError, match="conflicts"):
        calculate_metrics(row, mode="put", now_utc=NOW + timedelta(microseconds=1))
    offset = NOW.astimezone(timezone(timedelta(hours=8)))
    assert calculate_metrics(row, mode="put", now_utc=offset) == expected
    row["decision_now_utc"] = offset.isoformat()
    assert calculate_metrics(row, mode="put") == expected


@pytest.mark.parametrize("mode", ["put", "call"])
@pytest.mark.parametrize("age", [300, 300.000001, -0.000001])
@pytest.mark.parametrize("field", ["snapshot_received_at_utc", "spot_observed_at_utc"])
def test_replay_preserves_freshness_boundary(mode, age, field):
    row = quote(mode, decision_now_utc=NOW.isoformat(),
                **{field: (NOW - timedelta(seconds=age)).isoformat()})
    kwargs = dict(mode=mode, avg_cost=80 if mode == "call" else None)
    if age == 300:
        assert calculate_metrics(row, **kwargs) == calculate_metrics(row, now_utc=NOW, **kwargs)
    else:
        with pytest.raises(CandidateCalculationError) as recorded:
            calculate_metrics(row, **kwargs)
        with pytest.raises(CandidateCalculationError) as explicit:
            calculate_metrics(row, now_utc=NOW, **kwargs)
        assert recorded.value.to_payload() == explicit.value.to_payload()


def sealed_scan(tmp_path, *, account="lx", now=NOW):
    raw = quote()
    rejected = quote(contract_symbol="US.NVDA260918P91000", opening_contract_status="ineligible")
    rejects = []
    result = scan("put", tmp_path, pd.DataFrame([raw, rejected]), rejects, quote_freshness_now_utc=now)
    candidate = {**raw, **result.iloc[0].to_dict()}
    from domain.domain.engine import evaluate_opening_candidate_policy
    opening = evaluate_opening_candidate_policy(candidate, mode="put")
    assert opening["accepted"]
    decisions = [{"normalized_input": opening["normalized_input"], "opening_decision": opening}, *rejects]
    snapshot = _seal_snapshot(
        tmp_path, run_id="run-clock", account=account,
        scan_statuses=[_scan_status("NVDA", "put", "completed", snapshot_id="c" * 64, receipt="quotes/NVDA/receipt.json")],
        final_candidates={"put": [candidate]}, candidate_evaluations={"put": decisions},
    )
    return snapshot


def test_real_scan_disk_roundtrip_account_isolation_and_replay(tmp_path):
    for account, now in [("lx", NOW), ("sy", NOW + timedelta(microseconds=1))]:
        expected = sealed_scan(tmp_path, account=account, now=now)
        loaded = load_opening_candidate_snapshot(base=tmp_path, run_id="run-clock", account=account)
        assert loaded == expected
        assert json.loads(_snapshot_path(tmp_path, "run-clock", account).read_bytes()) == expected
        assert {d["normalized_input"]["decision_now_utc"] for d in loaded["candidate_decisions"]} == {now.isoformat()}
        accepted = next(d for d in loaded["candidate_decisions"] if d["opening_decision"]["accepted"])
        row = accepted["normalized_input"]
        assert calculate_metrics(row, mode="put") == calculate_metrics(row, mode="put", now_utc=now)


@pytest.mark.parametrize("bad", [None, "", "invalid", "2026-08-06T09:30:00", False])
def test_rehashed_invalid_clock_is_rejected_on_load(tmp_path, bad):
    payload = deepcopy(sealed_scan(tmp_path))
    payload["candidate_decisions"][0]["normalized_input"]["decision_now_utc"] = bad
    payload["content_sha256"] = canonical_sha256({k: v for k, v in payload.items() if k != "content_sha256"})
    with pytest.raises(OpeningCandidateSnapshotError, match="decision_now_utc"):
        validate_opening_candidate_snapshot(payload, expected_run_id="run-clock", expected_account="lx")


def test_clock_tampering_without_rehash_is_detected(tmp_path):
    payload = deepcopy(sealed_scan(tmp_path))
    payload["candidate_decisions"][0]["normalized_input"]["decision_now_utc"] = (NOW + timedelta(seconds=1)).isoformat()
    with pytest.raises(OpeningCandidateSnapshotError, match="hash mismatch"):
        validate_opening_candidate_snapshot(payload, expected_run_id="run-clock", expected_account="lx")


def test_legacy_archive_remains_readable_but_missing_clock_is_explicit(tmp_path):
    candidate = _candidate(contract_symbol="US.NVDA260918P90000", period_return=.03)
    _seal_snapshot(tmp_path, run_id="legacy", final_candidates={"put": [candidate]},
                   scan_statuses=[_scan_status("NVDA", "put", "completed", snapshot_id="c" * 64, receipt="q.json")])
    snapshot = load_opening_candidate_snapshot(base=tmp_path, run_id="legacy", account="lx")
    row = snapshot["candidate_decisions"][0]["normalized_input"]
    assert "decision_now_utc" not in row
    with pytest.raises(ValueError, match="missing decision_now_utc"):
        calculate_metrics(row, mode="put")


@pytest.mark.parametrize("mode", ["put", "call"])
@pytest.mark.parametrize("bad", [False, "2026-08-06T09:30:00Z", NOW.replace(tzinfo=None)])
def test_scan_rejects_invalid_override_without_falling_back(mode, bad, tmp_path):
    with pytest.raises(ValueError, match="timezone-aware"):
        scan(mode, tmp_path, pd.DataFrame([quote(mode)]), [], quote_freshness_now_utc=bad)
