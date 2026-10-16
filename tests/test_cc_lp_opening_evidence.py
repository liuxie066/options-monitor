from copy import deepcopy
from datetime import timedelta

import pandas as pd
import pytest

from src.application import cc_lp_steps
from src.application import cc_lp_candidate_snapshot as snapshots
from src.application.combo_yield_steps import run_cc_lp_variant
from src.application.opening_quote_evidence import (
    normalize_option_observation,
    normalize_underlier_observation,
)
from test_cc_lp_candidate_snapshot import _dependencies
from test_cc_lp_steps import (
    DECISION_NOW,
    _available_cc_lp_context,
    _cc_lp_scan_kwargs,
    _cc_lp_symbol_cfg,
    _cc_lp_variant_kwargs,
    _put_row,
    _scan_call_row,
    _write_required_data,
)


STOCK = {"shares": 100, "can_sell_qty": 100, "shares_locked": 0, "avg_cost": 90.0}


def _scan(tmp_path, puts, *, calls=None, now=DECISION_NOW, **overrides):
    return cc_lp_steps.run_cc_lp_scan(**_cc_lp_scan_kwargs(
        tmp_path, stock=STOCK, required_data_frame=pd.DataFrame(puts),
        run_sell_call_scan_fn=lambda **_: pd.DataFrame(calls or [_scan_call_row()]),
        now_utc_fn=lambda: now, **overrides,
    ))


def _variant(tmp_path, puts, *, now=DECISION_NOW):
    captured = []
    result = run_cc_lp_variant(**_cc_lp_variant_kwargs(
        _cc_lp_symbol_cfg(), tmp_path, portfolio_ctx=_available_cc_lp_context(),
        stock=STOCK, required_data_frame=pd.DataFrame(puts),
        run_cc_lp_scan_fn=lambda **kwargs: cc_lp_steps.run_cc_lp_scan(
            **kwargs, now_utc_fn=lambda: now,
            run_sell_call_scan_fn=lambda **_: pd.DataFrame([_scan_call_row()]),
        ), combo_evidence_sink_fn=captured.append,
    ))
    assert len(captured) == 1
    assert result["candidate_count"] == len(captured[0]["ranked_pairs"])
    return result, captured[0]["ranked_pairs"]


@pytest.mark.parametrize("field", [
    "opening_contract_status", "underlier_observation_status", "snapshot_received_at_utc",
    "option_standard_type", "stock_owner", "multiplier", "chain_multiplier",
    "snapshot_multiplier", "bid", "ask", "price_tick", "contract_symbol", "mid",
])
def test_missing_put_evidence_is_unavailable_not_successful_zero(tmp_path, field):
    row = _put_row()
    row.pop(field)
    summary, pairs = _variant(tmp_path, [row])
    assert not pairs
    assert (summary["status"], summary["reason"]) == ("unavailable", "data_unavailable")


@pytest.mark.parametrize("age,accepted", [(-1, False), (0, True), (300, True), (301, False)])
def test_put_receipt_freshness_is_checked_at_decision_time(tmp_path, age, accepted):
    row = _put_row(snapshot_received_at_utc=(DECISION_NOW - timedelta(seconds=age)).isoformat())
    summary, pairs = _variant(tmp_path, [row])
    assert bool(pairs) is accepted
    assert summary["status"] == ("candidates_found" if accepted else "unavailable")


def test_underlier_not_ready_remains_unresolved(tmp_path):
    summary, pairs = _variant(tmp_path, [_put_row(underlier_observation_status="data_unavailable")])
    assert pairs == [] and summary["reason"] == "data_unavailable"


def _normalized_put(*, chain_changes=None, snapshot_changes=None):
    row = _put_row()
    underlier = normalize_underlier_observation(
        code="US.NVDA", market="US", now_utc=DECISION_NOW,
        snapshot_row={"code": "US.NVDA", "last_price": 100,
                      "update_time": DECISION_NOW.isoformat(), "sec_status": "NORMAL", "suspension": False},
        market_state_row={"code": "US.NVDA", "market_state": "MORNING"},
    )
    observation = normalize_option_observation(
        expected_owner="US.NVDA", market="US", currency="USD", underlier_observation=underlier,
        chain_row={"code": row["contract_symbol"], "lot_size": 100, "stock_type": "DRVT",
                   "stock_owner": "US.NVDA", "option_standard_type": "STANDARD", "suspension": False,
                   **(chain_changes or {})},
        snapshot_row={"code": row["contract_symbol"], "bid_price": 2, "ask_price": 2.2,
                      "price_spread": 0.01, "option_contract_size": 100, "sec_status": "NORMAL",
                      "suspension": False, **(snapshot_changes or {})},
        snapshot_requested_at_utc=DECISION_NOW, snapshot_received_at_utc=DECISION_NOW,
        now_utc=DECISION_NOW,
    )
    values = observation.to_dict()
    row.update({key: values[key] for key in (
        "bid", "ask", "price_tick", "multiplier", "chain_multiplier", "snapshot_multiplier",
        "snapshot_received_at_utc", "option_standard_type", "stock_owner",
    )})
    row.update(opening_contract_status=observation.status,
               opening_contract_reason_codes=list(observation.reason_codes))
    return row


@pytest.mark.parametrize("chain,snapshot,status", [
    ({}, {}, "candidates_found"),
    ({"option_standard_type": "NON_STANDARD"}, {}, "no_candidate"),
    ({"suspension": True}, {}, "no_candidate"),
    ({}, {"bid_price": 5}, "unavailable"),
    ({}, {"bid_price": 0}, "no_candidate"),
    ({}, {"option_contract_size": 50}, "unavailable"),
])
def test_actual_normalizer_readiness_controls_long_put(tmp_path, chain, snapshot, status):
    row = _normalized_put(chain_changes=chain, snapshot_changes=snapshot)
    if snapshot.get("option_contract_size") == 50:
        assert row["multiplier"] is None
        assert cc_lp_steps._put_leg_from_required_data(row) is None
    summary, pairs = _variant(tmp_path, [row])
    assert summary["status"] == status
    assert bool(pairs) is (status == "candidates_found")


def test_ready_normalization_can_expire_before_long_put_decision(tmp_path):
    row = _normalized_put()
    assert row["opening_contract_status"] == "ready"
    summary, pairs = _variant(tmp_path, [row], now=DECISION_NOW + timedelta(seconds=301))
    assert pairs == [] and summary["status"] == "unavailable"


def test_normalized_values_are_consumed_once_per_put_without_mutating_input(monkeypatch, tmp_path):
    good = _put_row(**{field: str(value) for field, value in {
        "bid": 2.0, "ask": 2.2, "price_tick": 0.01, "multiplier": 100.0,
        "dte": 20.0, "strike": 90.0, "spot": 100.0,
    }.items()})
    puts = [good, _put_row(contract_symbol="UNAVAILABLE_PUT", bid=None)]
    before = deepcopy(puts)
    validations, legs, clocks = [], [], []
    original_validate = cc_lp_steps.validate_opening_contract_evidence
    original_leg = cc_lp_steps._put_leg_from_required_data

    def validate(row, **kwargs):
        validations.append((row["contract_symbol"], kwargs))
        return original_validate(row, **kwargs)

    def leg(row):
        legs.append(deepcopy(row))
        return original_leg(row)

    monkeypatch.setattr(cc_lp_steps, "validate_opening_contract_evidence", validate)
    monkeypatch.setattr(cc_lp_steps, "_put_leg_from_required_data", leg)
    result = cc_lp_steps.run_cc_lp_scan(**_cc_lp_scan_kwargs(
        tmp_path, stock=STOCK, required_data_frame=pd.DataFrame(puts),
        run_sell_call_scan_fn=lambda **_: pd.DataFrame([
            _scan_call_row(), _scan_call_row(contract_symbol="SECOND_CALL", strike=115),
        ]), now_utc_fn=lambda: clocks.append("decision") or DECISION_NOW,
    ))
    assert puts == before and clocks == ["decision"]
    assert len(validations) == 2 and len(legs) == 1 and len(result) == 2
    assert all(kwargs == {"mode": "put", "now_utc": DECISION_NOW} for _, kwargs in validations)
    assert isinstance(legs[0]["dte"], int) and isinstance(legs[0]["multiplier"], int)
    assert all(isinstance(legs[0][field], float) for field in ("bid", "ask", "price_tick", "strike", "spot"))
    assert result.attrs["_evidence_summary"] == {"evaluated_contract_count": 2, "eligibility_unresolved_count": 1}
    assert set(result["generated_at_utc"]) == {DECISION_NOW.isoformat()}


def test_csv_and_frame_preserve_candidate_identity_and_economics(tmp_path):
    put = _put_row()
    root = _write_required_data(tmp_path, call_rows=[], put_rows=[put])
    frame_result = _scan(tmp_path, [put])
    csv_result = cc_lp_steps.run_cc_lp_scan(**_cc_lp_scan_kwargs(
        root, stock=STOCK, run_sell_call_scan_fn=lambda **_: pd.DataFrame([_scan_call_row()]),
    ))
    pd.testing.assert_frame_equal(frame_result, csv_result)
    assert frame_result.attrs == csv_result.attrs
    row = frame_result.iloc[0]
    assert row["candidate_pair_id"] == "cc_lp:NVDA:NVDA260828C110000:NVDA260828P90000"
    assert row["net_credit"] == pytest.approx(row["call_net_credit"] - row["put_total_cost"])
    assert row["put_total_cost"] > 2.2 * 100


@pytest.mark.parametrize("puts,status,reason,opening,count", [
    ([_put_row()], "candidates_found", "", "candidates_found", 1),
    ([_put_row(opening_contract_status="ineligible")], "no_candidate", "no_candidate", "no_candidate", 0),
    ([_put_row(delta=-0.5)], "no_candidate", "no_candidate", "no_candidate", 0),
    ([_put_row(bid=None)], "unavailable", "data_unavailable", "data_unavailable", 0),
    ([_put_row(), _put_row(contract_symbol="MISSING_PUT", bid=None)], "candidates_found", "partial_data", "partial_data", 1),
    ([_put_row(delta=-0.5), _put_row(contract_symbol="MISSING_PUT", bid=None)], "no_candidate", "partial_data", "partial_data", 0),
])
def test_variant_and_snapshot_preserve_evidence_status(monkeypatch, tmp_path, puts, status, reason, opening, count):
    summary, pairs = _variant(tmp_path, puts)
    assert (summary["status"], summary["reason"], len(pairs)) == (status, reason, count)
    storage = {}

    def write(**kwargs):
        storage[kwargs["name"]] = kwargs["payload"]
        return tmp_path / kwargs["name"]

    # Exercise snapshot assembly/readback; native publication includes forbidden cleanup.
    monkeypatch.setattr(snapshots, "write_account_run_state_bytes_once_safely", write)
    monkeypatch.setattr(snapshots, "read_account_run_state_bytes_safely", lambda **kw: storage[kw["name"]])
    terminal = "unavailable" if status == "unavailable" else "completed"
    payload = snapshots.seal_cc_lp_candidate_snapshot(
        base=tmp_path, run_id="synthetic-run", account="qa", market="us",
        account_config_sha256="a" * 64, strategy_policy_sha256="b" * 64,
        dependencies=_dependencies(), run_mode={"scan_mode": "standard", "executable": True},
        scan_statuses=[{"symbol": "NVDA", "strategy_mode": "combo_yield", "variant": "cc_lp",
                        "status": terminal, "reason": summary["reason"]}],
        ranked_pairs=pairs, sealed_at=DECISION_NOW,
    )
    assert payload["opening_status"] == opening
    assert payload["scope_results"][0]["reason_code"] == (reason or None)
    assert snapshots.project_cc_lp_candidates(payload) == pairs
