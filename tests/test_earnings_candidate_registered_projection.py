from __future__ import annotations

from contextlib import redirect_stdout
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from conftest import phase2_opening_row
from src.application import earnings_calendar as calendar
from src.application import scan_sell_put
from src.application.multi_tick.required_data_prefetch import _earnings_calendar_market_requests
from src.application.required_data_planning import RequiredDataFetchPlanBundle
from src.application.scan_sell_call import run_sell_call_scan
from src.application.sell_put_strategy_risk import (
    evaluate_sell_put_underwriting_row,
    resolve_sell_put_underwriting_config,
)


NOW = datetime(2026, 8, 6, 16, tzinfo=timezone.utc)


class _Gateway:
    def __init__(self, responses=()):
        self.responses = iter(responses)

    def get_earnings_calendar(self, **kwargs):
        response = next(self.responses, [])
        if isinstance(response, Exception):
            raise response
        return response


def _expiry(days, now=NOW):
    return (now.astimezone(ZoneInfo("America/New_York")).date() + timedelta(days=days)).isoformat()


def _snapshot(*, dates=None, responses=(), market="US", code="US.NVDA", now=NOW):
    return calendar.fetch_market_earnings_calendar(
        gateway=_Gateway(responses), market=market,
        scan_date=calendar.earnings_calendar_scan_date(market, now), scan_at_utc=now,
        expirations_by_underlier={code: dates or [_expiry(6, now)]}, now_fn=lambda: now,
    )


def _write_snapshot(root, snapshot):
    path = root / "earnings_calendar" / f"{snapshot.get('market', 'US')}.json"
    path.parent.mkdir(parents=True)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(snapshot, handle)


def _quote(expiration, *, now=NOW, side="put"):
    observed = (now - timedelta(seconds=1)).isoformat()
    return phase2_opening_row({
        "symbol": "NVDA", "market": "US", "option_type": side, "expiration": expiration,
        "contract_symbol": f"US.NVDA{expiration.replace('-', '')[2:]}{side[0].upper()}00100000",
        "currency": "USD", "dte": (datetime.fromisoformat(expiration).date()
            - now.astimezone(ZoneInfo("America/New_York")).date()).days,
        "strike": 100, "spot": 110, "bid": 2.0, "ask": 2.05, "multiplier": 100,
        "implied_volatility": 0.4, "open_interest": 500, "volume": 100,
        "quote_update_time": observed, "quote_observed_at_utc": observed,
        "snapshot_received_at_utc": observed, "spot_update_time": observed,
        "spot_observed_at_utc": observed, "quote_age_seconds": 1, "spot_age_seconds": 1,
    })


def _policy(row):
    cfg = resolve_sell_put_underwriting_config({
        "strategy": "insurance_underwriting", "min_dte": 1, "max_dte": 90,
        "min_annualized_net_return": 0.0, "min_net_income": 50,
    })
    # Explicit synthetic capacity/FX facts, independent of the earnings evidence.
    return evaluate_sell_put_underwriting_row(dict(
        row, net_income_cny=float(row["net_income"]) * 7, cash_required_cny=70000,
        max_new_contracts=1, option_contract_point_value_cny=700,
    ), cfg=cfg)


@pytest.mark.parametrize("mode", ["frame", "csv"])
@pytest.mark.parametrize("scenario,days,accepted,rule", [
    ("endpoint", 6, True, "candidate_engine_policy_accepted"),
    ("beyond_one_day", 7, False, "risk_earnings_unavailable"),
    ("beyond_full_window", 43, False, "risk_earnings_unavailable"),
    ("inside_unregistered", 5, False, "risk_earnings_unavailable"),
    ("registered_long", 15, True, "candidate_engine_policy_accepted"),
    ("hard_failure", 15, False, "risk_earnings_unavailable"),
    ("soft_failure", 15, True, "candidate_engine_policy_accepted"),
    ("blocking_event", 6, False, "risk_earnings_event"),
])
def test_scanner_requires_registered_earnings_projection(tmp_path, mode, scenario, days, accepted, rule):
    responses = {
        "hard_failure": [[], RuntimeError("synthetic hard failure"), []],
        "soft_failure": [RuntimeError("synthetic soft failure"), [], []],
        "blocking_event": [[{"security": "US.NVDA", "earnings_date": _expiry(5)}]],
    }.get(scenario, [])
    registered = _expiry(15) if scenario in {"registered_long", "hard_failure", "soft_failure"} else _expiry(6)
    snapshot = _snapshot(dates=[registered], responses=responses)
    _write_snapshot(tmp_path, snapshot)
    frame = pd.DataFrame([_quote(_expiry(days))])
    if mode == "csv":
        parsed = tmp_path / "parsed"
        parsed.mkdir()
        with (parsed / "NVDA_required_data.csv").open("x") as handle:
            frame.to_csv(handle, index=False)
    result = scan_sell_put.run_sell_put_scan(
        symbols=["NVDA"], input_root=tmp_path, min_annualized_net_return=0,
        quote_freshness_now_utc=NOW, required_data_frames={"NVDA": frame} if mode == "frame" else None,
    )
    assert len(result) == 1
    row = result.to_dict("records")[0]
    decision = _policy(row)
    assert (decision["accepted"], decision["rule"]) == (accepted, rule)
    if scenario in {"beyond_one_day", "beyond_full_window", "inside_unregistered"}:
        with pytest.raises(ValueError, match="stored projection mismatch"):
            calendar.project_earnings_for_expiry(snapshot, underlier_code="US.NVDA", expiration=_expiry(days))
        assert row["earnings_evidence_status"] == "data_unavailable"
        assert row["earnings_reason_code"] == "earnings_calendar_candidate_identity_invalid"
        assert row["earnings_hard_coverage_status"] == "unavailable"
        assert row["earnings_has_event"] is None
    else:
        projected = calendar.project_earnings_for_expiry(snapshot, underlier_code="US.NVDA", expiration=_expiry(days))
        assert row["earnings_hard_coverage_status"] == projected["hard_coverage_status"]
        assert row["earnings_soft_coverage_status"] == projected["soft_coverage_status"]


@pytest.mark.parametrize("market,code,symbol", [
    ("US", "US.NVDA", "NVDA"), ("US", "US.NVDA", "US.NVDA"),
    ("US", "US.NVDA", "nvda"), ("HK", "HK.00700", "0700.HK"),
    ("HK", "HK.00700", "HK.00700"),
])
def test_single_candidate_preserves_registered_identity_aliases(tmp_path, market, code, symbol):
    _write_snapshot(tmp_path, _snapshot(market=market, code=code))
    evidence = calendar.load_earnings_evidence_for_candidate(
        input_root=tmp_path, market=market, symbol=symbol, expiration=f" {_expiry(6)} ",
    )
    assert evidence["earnings_evidence_status"] == "ready"
    assert evidence["earnings_hard_coverage_status"] == "complete"
    assert evidence["earnings_has_event"] is False


@pytest.mark.parametrize("symbol,expiration", [
    ("AMZN", "2026-08-12"), ("0700.HK", "2026-08-12"),
    ("NVDA", ""), ("NVDA", "2026-08-12T00:00:00Z"), ("NVDA", "2026-08-05"),
])
def test_single_candidate_rejects_unregistered_or_invalid_identity(tmp_path, symbol, expiration):
    _write_snapshot(tmp_path, _snapshot())
    evidence = calendar.load_earnings_evidence_for_candidate(
        input_root=tmp_path, market="US", symbol=symbol, expiration=expiration,
    )
    assert evidence["earnings_evidence_status"] == "data_unavailable"
    assert evidence["earnings_reason_code"] == "earnings_calendar_candidate_identity_invalid"


@pytest.mark.parametrize("content,reason", [
    (None, "earnings_calendar_snapshot_missing"), ("{", "earnings_calendar_snapshot_invalid"),
    ("[]", "earnings_calendar_snapshot_invalid"), ("{}", "earnings_calendar_snapshot_identity_invalid"),
    ("bad_hash", "earnings_calendar_snapshot_identity_invalid"),
])
def test_invalid_snapshot_is_not_valid_empty_earnings(tmp_path, content, reason):
    if content is not None:
        path = tmp_path / "earnings_calendar/US.json"
        path.parent.mkdir()
        if content == "bad_hash":
            content = json.dumps(dict(_snapshot(), snapshot_hash="0" * 64))
        with path.open("x") as handle:
            handle.write(content)
    row = scan_sell_put.run_sell_put_scan(
        symbols=["NVDA"], input_root=tmp_path, min_annualized_net_return=0,
        quote_freshness_now_utc=NOW, required_data_frames={"NVDA": pd.DataFrame([_quote(_expiry(6))])},
    ).to_dict("records")[0]
    assert row["earnings_evidence_status"] == "data_unavailable"
    assert row["earnings_reason_code"] == reason
    assert row["earnings_has_event"] is None
    assert _policy(row)["rule"] == "risk_earnings_unavailable"


@pytest.mark.parametrize("kind", ["events", "failures"])
def test_registered_projection_nested_data_is_independently_mutable(tmp_path, kind):
    if kind == "events":
        responses = [[{"security": "US.NVDA", "earnings_date": _expiry(4)}], [],
                     [{"security": "US.NVDA", "earnings_date": _expiry(14)}]]
        fields = ("earnings_events", "earnings_blocking_events", "earnings_nonblocking_events")
    else:
        responses = [RuntimeError("synthetic soft failure"), RuntimeError("synthetic hard failure"), []]
        fields = ("earnings_hard_failed_intervals", "earnings_soft_failed_intervals")
    snapshot = _snapshot(dates=[_expiry(15)], responses=responses)
    original = deepcopy(snapshot)
    kwargs = dict(input_root=tmp_path, market="US", symbol="NVDA", expiration=_expiry(15))
    first = calendar._candidate_earnings_from_snapshot(snapshot, **kwargs)
    second = calendar._candidate_earnings_from_snapshot(snapshot, **kwargs)
    for field in fields:
        assert first[field]
        first[field][0]["mutation"] = True
        assert "mutation" not in second[field][0]
    assert snapshot == original
    _write_snapshot(tmp_path, snapshot)
    candidate = {k: v for k, v in kwargs.items() if k != "input_root"}
    rows = calendar.annotate_candidates_with_earnings_evidence(pd.DataFrame([candidate] * 2), input_root=tmp_path)
    for field in fields:
        rows.at[0, field][0]["mutation"] = True
        assert "mutation" not in rows.at[1, field][0]
        fresh = calendar.load_earnings_evidence_for_candidate(**kwargs)
        assert "mutation" not in fresh[field][0]
    assert snapshot == original


def test_registered_projection_keeps_one_validation_per_market(tmp_path):
    us = _snapshot()
    hk = _snapshot(market="HK", code="HK.00700")
    _write_snapshot(tmp_path, us)
    # The same fresh folder can contain distinct market artifacts.
    with (tmp_path / "earnings_calendar/HK.json").open("x") as handle:
        json.dump(hk, handle)
    records = [
        {"market": market, "symbol": symbol, "expiration": _expiry(6)}
        for market, symbol in [("US", "NVDA"), ("US", "NVDA"), ("HK", "0700.HK")]
    ]
    calls = []
    def profile(frame, event, arg):
        if event == "call" and frame.f_code.co_filename == calendar.__file__:
            if frame.f_code.co_name in {"validate_earnings_calendar_snapshot", "_load_candidate_earnings_snapshot"}:
                calls.append((frame.f_code.co_name, frame.f_locals.get("expected_market", frame.f_locals.get("market"))))
    previous = sys.getprofile()
    sys.setprofile(profile)
    try:
        rows = calendar.annotate_candidates_with_earnings_evidence(pd.DataFrame(records), input_root=tmp_path)
    finally:
        sys.setprofile(previous)
    assert list(rows["earnings_evidence_status"]) == ["ready"] * 3
    assert calls == [(name, market) for market in ("US", "HK") for name in (
        "_load_candidate_earnings_snapshot", "validate_earnings_calendar_snapshot")]


@pytest.mark.parametrize("days,status", [(6, "ready"), (43, "data_unavailable")])
def test_call_scanner_uses_registered_projection_boundary(tmp_path, days, status):
    _write_snapshot(tmp_path, _snapshot())
    rows = run_sell_call_scan(
        symbols=["NVDA"], input_root=tmp_path, avg_cost=90, shares=100, shares_can_sell=100, shares_locked=0,
        min_annualized_net_return=0, quote_freshness_now_utc=NOW,
        required_data_frames={"NVDA": pd.DataFrame([_quote(_expiry(days), side="call")])},
    )
    assert len(rows) == 1
    assert rows.iloc[0]["earnings_evidence_status"] == status


def test_compatibility_main_reused_directory_does_not_extend_earnings_coverage(tmp_path):
    now = datetime.now(timezone.utc)
    dates = [_expiry(days, now) for days in (6, 43)]
    _write_snapshot(tmp_path, _snapshot(dates=[dates[0]], now=now))
    parsed = tmp_path / "parsed"
    parsed.mkdir()
    with (parsed / "NVDA_required_data.csv").open("x") as handle:
        pd.DataFrame([_quote(expiration, now=now) for expiration in dates]).to_csv(handle, index=False)
    stdout = io.StringIO()
    with redirect_stdout(stdout):
        exit_code = scan_sell_put.main([
            "--symbols", "NVDA", "--input-root", str(tmp_path), "--min-annualized-net-return", "0",
        ])
    assert exit_code == 0
    rows = {row["expiration"]: row for row in json.loads(stdout.getvalue())}
    assert set(rows) == set(dates)
    assert rows[dates[0]]["earnings_evidence_status"] == "ready"
    assert _policy(rows[dates[0]])["accepted"] is True
    assert rows[dates[1]]["earnings_evidence_status"] == "data_unavailable"
    assert _policy(rows[dates[1]])["rule"] == "risk_earnings_unavailable"


def test_same_frozen_plan_registers_both_scanner_expirations(tmp_path):
    dates = [_expiry(6), _expiry(43)]
    config = {"symbol": "NVDA", "fetch": {"host": "fixture.invalid", "port": 1}}
    plan = RequiredDataFetchPlanBundle(
        symbol="NVDA", spot_reference=110, side_plans=[], merged_specs=[], projected_expirations=dates,
    )
    requests = _earnings_calendar_market_requests(
        symbol_cfgs=[config], fetch_plans_by_config_id={id(config): plan}, scan_at_utc=NOW,
    )
    registered = requests["US"]["expirations_by_underlier"]["US.NVDA"]
    assert registered == dates
    _write_snapshot(tmp_path, _snapshot(dates=registered))
    rows = scan_sell_put.run_sell_put_scan(
        symbols=["NVDA"], input_root=tmp_path, min_annualized_net_return=0,
        quote_freshness_now_utc=NOW, required_data_frames={"NVDA": pd.DataFrame([_quote(exp) for exp in dates])},
    ).to_dict("records")
    assert len(rows) == 2
    assert all(row["earnings_evidence_status"] == "ready" and _policy(row)["accepted"] for row in rows)
