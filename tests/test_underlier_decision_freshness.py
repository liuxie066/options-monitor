"""Decision-time source age, distinct from option receipt and frozen fetch age."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from candidate_evidence_helpers import earnings_evidence
from conftest import phase2_opening_row
from domain.domain.engine import candidate_engine as engine
from src.application import candidate_scanning as scanning
from src.application import opening_candidate_snapshot as snapshots
from src.application.opening_quote_evidence import normalize_underlier_observation
from src.application.scan_sell_call import run_sell_call_scan
from src.application.scan_sell_put import run_sell_put_scan


NOW = datetime(2026, 4, 1, 15, tzinfo=timezone.utc)


def _row(mode="put", **overrides):
    return phase2_opening_row({
        "symbol": "NVDA", "option_type": mode, "expiration": "2026-05-06", "dte": 35,
        "contract_symbol": f"NVDA-{mode}", "multiplier": 100, "currency": "USD",
        "strike": 95 if mode == "put" else 110, "spot": 100,
        "bid": 3.0, "ask": 3.01, "delta": -0.3 if mode == "put" else 0.3,
        "open_interest": 1000, "volume": 100, "implied_volatility": 0.3,
        "snapshot_received_at_utc": NOW.isoformat(),
        "spot_observed_at_utc": NOW.isoformat(), **overrides,
    })


@pytest.mark.parametrize("mode", ["put", "call"])
@pytest.mark.parametrize("value", [None, "", "bad-time", "2026-04-01T15:00:00", pd.NA, pd.NaT, float("nan")])
def test_core_missing_or_invalid_source_time_fails_before_fee(monkeypatch, mode, value):
    def fee_must_not_run(*args, **kwargs):
        pytest.fail("economic calculation ran without source-time evidence")

    monkeypatch.setattr(engine, "estimate_futu_option_sell_fee", fee_must_not_run)
    with pytest.raises(engine.CandidateCalculationError) as caught:
        engine.calculate_opening_candidate_metrics(
            _row(mode, spot_observed_at_utc=value), mode=mode, now_utc=NOW,
        )
    assert caught.value.reason == "evidence_unavailable"
    assert set(caught.value.metric_value) == {"spot_observed_at_utc"}
    assert caught.value.threshold == "present timezone-aware source time"


@pytest.mark.parametrize("mode", ["put", "call"])
def test_missing_source_key_has_no_receipt_or_raw_time_fallback(mode):
    row = _row(mode, spot_update_time=NOW.isoformat(), spot_age_seconds=0)
    row.pop("spot_observed_at_utc")
    with pytest.raises(engine.CandidateCalculationError) as caught:
        engine.validate_opening_contract_evidence(row, mode=mode, now_utc=NOW)
    assert caught.value.reason == "evidence_unavailable"
    assert caught.value.metric_value == {"spot_observed_at_utc": None}


@pytest.mark.parametrize("mode", ["put", "call"])
@pytest.mark.parametrize("age", [-1, 0, 299, 300, 301])
def test_source_age_boundary_ignores_frozen_age_and_preserves_economics(monkeypatch, mode, age):
    row = _row(mode, spot_observed_at_utc=(NOW - timedelta(seconds=age)).isoformat(),
               spot_age_seconds=0 if age == 301 else 9999)
    before = deepcopy(row)
    if age < 0 or age > 300:
        monkeypatch.setattr(engine, "estimate_futu_option_sell_fee",
                            lambda *args, **kwargs: pytest.fail("fee ran before source-age validation"))
        with pytest.raises(engine.CandidateCalculationError) as caught:
            engine.calculate_opening_candidate_metrics(row, mode=mode, now_utc=NOW)
        assert caught.value.reason == "evidence_unavailable"
        assert caught.value.metric_value == {"spot_age_seconds": age}
        assert caught.value.threshold == (">= 0" if age < 0 else 300)
    else:
        normalized = engine.validate_opening_contract_evidence(row, mode=mode, now_utc=NOW)
        assert normalized == {"bid": 3.0, "ask": 3.01, "price_tick": 0.01,
                              "multiplier": 100, "dte": 35, "strike": row["strike"], "spot": 100.0}
        assert engine.calculate_opening_candidate_metrics(row, mode=mode, now_utc=NOW) == (
            engine.calculate_opening_candidate_metrics(_row(mode), mode=mode, now_utc=NOW)
        )
    assert row == before


@pytest.mark.parametrize("mode", ["put", "call"])
@pytest.mark.parametrize("source_time", [
    NOW, NOW.isoformat(), "2026-04-01T11:00:00-04:00", "2026-04-01T23:00:00+08:00",
])
def test_timezone_aware_source_and_historical_replay_clock(mode, source_time):
    row = _row(mode, spot_observed_at_utc=source_time)
    assert engine.validate_opening_contract_evidence(
        row, mode=mode, now_utc="2026-04-01T11:00:00-04:00",
    )["spot"] == 100


@pytest.mark.parametrize("mode", ["put", "call"])
def test_default_clock_is_captured_once_for_both_ages(monkeypatch, mode):
    calls = []

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            calls.append(tz)
            return NOW

    monkeypatch.setattr(engine, "datetime", Clock)
    row = _row(mode, spot_observed_at_utc=(NOW - timedelta(seconds=300)).isoformat())
    assert engine.validate_opening_contract_evidence(row, mode=mode)["spot"] == 100
    assert calls == [timezone.utc]


@pytest.mark.parametrize("mode", ["put", "call"])
@pytest.mark.parametrize("limit,option_age,spot_age,field", [
    (600, 400, 301, "spot_age_seconds"),
    (60, 60, 120, None),
    (60, 61, 120, "snapshot_age_seconds"),
    (300, -1, 301, "snapshot_age_seconds"),
    (300, 301, 301, "snapshot_age_seconds"),
])
def test_option_limit_remains_independent_and_keeps_error_precedence(mode, limit, option_age, spot_age, field):
    row = _row(mode, snapshot_received_at_utc=(NOW - timedelta(seconds=option_age)).isoformat(),
               spot_observed_at_utc=(NOW - timedelta(seconds=spot_age)).isoformat())
    if field is None:
        assert engine.validate_opening_contract_evidence(
            row, mode=mode, now_utc=NOW, max_snapshot_age_seconds=limit,
        )["spot"] == 100
    else:
        with pytest.raises(engine.CandidateCalculationError) as caught:
            engine.validate_opening_contract_evidence(row, mode=mode, now_utc=NOW, max_snapshot_age_seconds=limit)
        assert caught.value.reason == "evidence_unavailable"
        assert set(caught.value.metric_value) == {field}


@pytest.mark.parametrize("mode", ["put", "call"])
def test_missing_option_receipt_precedes_invalid_spot(mode):
    with pytest.raises(engine.CandidateCalculationError) as caught:
        engine.validate_opening_contract_evidence(
            _row(mode, snapshot_received_at_utc=None, spot_observed_at_utc=pd.NA), mode=mode, now_utc=NOW,
        )
    assert caught.value.reason == "evidence_unavailable"
    assert caught.value.metric_value == {"snapshot_received_at_utc": None}


def _normalized_row(mode, *, age_at_fetch, contract):
    observation = normalize_underlier_observation(
        code="US.NVDA", market="US", now_utc=NOW,
        snapshot_row={"code": "US.NVDA", "last_price": 100,
                      "update_time": (NOW - timedelta(seconds=age_at_fetch)).isoformat(),
                      "sec_status": "NORMAL", "suspension": False},
        market_state_row={"code": "US.NVDA", "market_state": "MORNING"},
    )
    assert observation.status == "ready"
    return _row(mode, contract_symbol=contract, spot_observed_at_utc=observation.observed_at_utc,
                spot_age_seconds=observation.age_seconds, underlier_observation_status=observation.status)


@pytest.mark.parametrize("mode", ["put", "call"])
@pytest.mark.parametrize("input_kind", ["frame", "csv"])
@pytest.mark.parametrize("ages,status,reason,count", [
    ([298], "completed", None, 1),
    ([299], "unavailable", "data_unavailable", 0),
    ([298, 299], "completed", "partial_data", 1),
])
def test_fetch_ready_age_is_rechecked_through_scan_policy_and_snapshot(
    monkeypatch, tmp_path, mode, input_kind, ages, status, reason, count,
):
    rows = [_normalized_row(mode, age_at_fetch=age, contract=f"NVDA-{mode}-{age}") for age in ages]
    before = deepcopy(rows)
    decision = NOW + timedelta(seconds=2)
    earnings = earnings_evidence(expiration="2026-05-06", market_date="2026-04-01")
    monkeypatch.setattr(scanning, "annotate_candidates_with_earnings_evidence",
                        lambda frame, **_: pd.DataFrame([{**row, **earnings} for row in frame.to_dict("records")]))
    source = pd.DataFrame(rows)
    if input_kind == "csv":
        (tmp_path / "parsed").mkdir()
        source.to_csv(tmp_path / "parsed" / "NVDA_required_data.csv", index=False)
        frames = None
    else:
        frames = {"NVDA": source}
    calculations = []
    common = dict(symbols=["NVDA"], input_root=tmp_path, required_data_frames=frames,
                  min_annualized_net_return=0.1, quote_freshness_now_utc=decision,
                  calculation_decision_sink_fn=calculations.extend)
    if mode == "put":
        candidates = run_sell_put_scan(**common)
    else:
        candidates = run_sell_call_scan(**common, avg_cost=90, shares=200, shares_can_sell=200,
                                        shares_locked=0, capacity_facts={"capacity_authority_status": "available",
                                                                        "capacity_identity_hash": "7" * 64})
    final = []
    evaluations = list(calculations)
    for candidate in candidates.to_dict("records"):
        candidate.update(net_income_cny=candidate["net_income"] / 0.14, max_new_contracts=1,
                         policy_min_dte=14, policy_max_dte=60, policy_min_annualized_return=0.1)
        policy = engine.evaluate_opening_candidate_policy(candidate, mode=mode)
        assert policy["accepted"]
        final.append(candidate)
        evaluations.append({"normalized_input": candidate, "opening_decision": policy})
    evidence = scanning.evidence_summary_from_decisions(decisions=evaluations, accepted_count=len(final))
    terminal = scanning.project_evidence_scan_status(evidence=evidence, candidate_count=len(final))
    assert terminal == (status, reason)
    assert len(candidates) == len(final) == count
    assert len(calculations) == ages.count(299)
    for record in calculations:
        assert record["opening_decision"]["rejects"][0]["reason"] == "evidence_unavailable"
    memory = {}

    def write(**kwargs):
        memory[kwargs["name"]] = kwargs["payload"]
        return tmp_path / kwargs["name"]

    # Only storage is replaced: assembly, validation, readback and projection are real.
    monkeypatch.setattr(snapshots, "write_account_run_state_bytes_once_safely", write)
    monkeypatch.setattr(snapshots, "read_account_run_state_bytes_safely", lambda **kw: memory[kw["name"]])
    (tmp_path / "output_runs" / "synthetic-622" / "accounts" / "qa").mkdir(parents=True)
    sealed = snapshots.seal_opening_candidate_snapshot(
        base=tmp_path, run_id="synthetic-622", account="qa", market="US",
        physical_account={"status": "available", "logical_account": "qa", "futu_account_id": "synthetic-622",
                          "trd_env": "SIMULATE", "market": "US", "source": "opend"},
        account_config_sha256="a" * 64, strategy_policy_sha256="b" * 64,
        dependencies=[{"kind": kind, "relpath": None, "sha256": str(index) * 64} for index, kind in enumerate(
            ["required_data", "portfolio", "ledger", "fx", "earnings_rv"], 1)],
        scan_statuses=[{"symbol": "NVDA", "strategy_mode": mode, "status": status, "reason": reason}],
        final_candidates={mode: final}, candidate_evaluations=None if final else {mode: evaluations},
        run_mode={"scan_mode": "standard", "executable": True}, sealed_at=decision,
    )
    assert len(snapshots.ranked_opening_candidates(sealed, mode=mode)) == count
    assert sealed["opening_status"] == ("candidates_found" if count else "data_unavailable")
    scope = next(row for row in sealed["scope_results"] if row["scope"] == "strategy")
    assert scope["reason_code"] == reason
    assert rows == before


@pytest.mark.parametrize("mode", ["put", "call"])
@pytest.mark.parametrize("source_time", [None, float("nan")])
def test_missing_normal_producer_and_csv_time_reaches_canonical_rejection(tmp_path, mode, source_time):
    from src.application.scan_sell_call import explain_metrics_rejection as explain_call
    from src.application.scan_sell_put import explain_metrics_rejection as explain_put

    path = tmp_path / "synthetic.csv"
    pd.DataFrame([_row(mode, spot_observed_at_utc=source_time)]).to_csv(path, index=False)
    row = pd.read_csv(path).iloc[0]
    rejection = (explain_put(row, now_utc=NOW) if mode == "put"
                 else explain_call(row, avg_cost=90, now_utc=NOW))
    assert rejection["rule"] == "evidence_unavailable"
    assert rejection["metric_value"] == {"spot_observed_at_utc": None}


@pytest.mark.parametrize("leg", ["put", "call"])
@pytest.mark.parametrize("age,count", [(300, 1), (301, 0)])
def test_sp_lc_checks_source_time_of_both_funding_and_long_legs(tmp_path, leg, age, count):
    from test_combo_yield_steps import _candidate, _call_candidate, _run, _write_combo_calls
    from src.application.sell_put_call_helper import find_sell_put_combo_yield_pairs

    decision = datetime(2026, 7, 17, 14, tzinfo=timezone.utc)
    put = _candidate(annualized_net_return_on_cash_basis=0.18, funding_put_eligible=True)
    call = _call_candidate()
    (put if leg == "put" else call)["spot_observed_at_utc"] = (decision - timedelta(seconds=age)).isoformat()
    _write_combo_calls(tmp_path, [call], put_rows=[put])
    _, _, _, summary = _run(tmp_path, candidates=[put], find_pairs_fn=find_sell_put_combo_yield_pairs,
                             decision_now_utc=decision)
    assert summary["candidate_count"] == count
    assert summary["_strategy_status"] == ("completed" if count else "unavailable")
    assert summary["_strategy_reason"] == (None if count else "data_unavailable")


@pytest.mark.parametrize("ages,status,reason,count", [
    ([300], "candidates_found", "", 1),
    ([301], "unavailable", "data_unavailable", 0),
    ([300, 301], "candidates_found", "partial_data", 1),
])
def test_cc_lp_accepted_619_consumer_preserves_source_gap_status(tmp_path, ages, status, reason, count):
    from test_cc_lp_opening_evidence import _variant
    from test_cc_lp_steps import DECISION_NOW, _put_row

    puts = [_put_row(contract_symbol=f"NVDA-P-{age}",
                     spot_observed_at_utc=(DECISION_NOW - timedelta(seconds=age)).isoformat()) for age in ages]
    summary, pairs = _variant(tmp_path, puts)
    assert (summary["status"], summary["reason"], len(pairs)) == (status, reason, count)
