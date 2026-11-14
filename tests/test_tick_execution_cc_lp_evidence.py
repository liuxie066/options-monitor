"""F2 evidence from real Funding Call owners through the monitoring facade."""
from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
import socket

import pandas as pd
import pytest

from conftest import phase2_opening_row
from src.application import candidate_scanning, cc_lp_steps, combo_yield_steps
from src.application.candidate_snapshot_manifest import (
    load_candidate_snapshot_bundle, publish_candidate_snapshot_manifest,
)
from src.application.cc_lp_candidate_snapshot import seal_cc_lp_candidate_snapshot
from src.application.pipeline_watchlist import _index_owner_statuses
from src.application.scan_sell_call import run_sell_call_scan
from src.application.strategy_scan_status import publish_strategy_scan_status_index
from src.application.symbol_monitoring import run_symbol_monitoring
from test_cc_lp_candidate_snapshot import _dependencies
from test_cc_lp_steps import (
    DECISION_NOW, _available_cc_lp_context, _cc_lp_scan_kwargs, _cc_lp_symbol_cfg,
    _cc_lp_variant_kwargs, _put_row, _scan_call_row,
)
from test_symbol_monitoring_fetch_spec_merge import _deps, _inputs, _published_scopes

STOCK = {"shares": 100, "can_sell_qty": 100, "shares_locked": 0, "avg_cost": 90.0}


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("network is forbidden in producer regressions")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    earnings_available = {}
    default_evidence = {key: value for key, value in _scan_call_row().items()
                        if key.startswith("earnings_") or key in {"event_flag", "event_types", "event_dates"}}

    def annotate(df, **_kwargs):
        rows = []
        for raw in df.to_dict("records"):
            row = {**raw, **default_evidence}
            if not earnings_available.get(row["contract_symbol"], True):
                row.update(earnings_evidence_status="data_unavailable", earnings_hard_coverage_status="unavailable")
            rows.append(row)
        return pd.DataFrame(rows)

    monkeypatch.setattr(candidate_scanning, "annotate_candidates_with_earnings_evidence", annotate)
    return earnings_available


def _call(**changes):
    return phase2_opening_row({
        **_scan_call_row(), "option_type": "call",
        "spot_observed_at_utc": DECISION_NOW.isoformat(),
        "snapshot_received_at_utc": DECISION_NOW.isoformat(),
        "decision_now_utc": DECISION_NOW.isoformat(), **changes,
    })


def _scanner(captured):
    def funding_call(**kwargs):
        return run_sell_call_scan(**kwargs, quote_freshness_now_utc=DECISION_NOW)

    def scanner(**kwargs):
        df = cc_lp_steps.run_cc_lp_scan(
            **kwargs, now_utc_fn=lambda: DECISION_NOW, run_sell_call_scan_fn=funding_call,
        )
        captured.append(df)
        return df
    return scanner


def _variant(tmp_path, frame, *, cfg=None):
    captured = []
    result = combo_yield_steps.run_cc_lp_variant(**_cc_lp_variant_kwargs(
        cfg or _cc_lp_symbol_cfg(), tmp_path, portfolio_ctx=_available_cc_lp_context(),
        stock=STOCK, required_data_frame=frame, run_cc_lp_scan_fn=_scanner(captured),
    ))
    assert len(captured) == 1
    return result, captured[0]


@pytest.mark.parametrize("call,expected_status,unresolved,ineligible", [
    (_call(bid=None), "unavailable", 1, 0),
    (_call(option_standard_type="NON_STANDARD", opening_contract_status="ineligible",
           opening_contract_reason_codes="option_non_standard"), "no_candidate", 0, 1),
    (_call(earnings_evidence_status="data_unavailable", earnings_hard_coverage_status="unavailable"), "unavailable", 1, 0),
])
def test_funding_call_empty_retains_calculation_and_underwriting_evidence(
    tmp_path, call, expected_status, unresolved, ineligible, _isolated,
):
    _isolated[call["contract_symbol"]] = call.get("earnings_evidence_status") == "ready"
    result, df = _variant(tmp_path, pd.DataFrame([call, _put_row()]))
    assert df.empty
    assert result["status"] == expected_status
    assert result["candidate_count"] == 0
    evidence = df.attrs["_evidence_summary"]
    assert evidence["evaluated_contract_count"] == 1
    assert evidence["eligibility_unresolved_count"] == unresolved
    assert evidence["contract_ineligible_count"] == ineligible


def test_healthy_pair_and_missing_funding_call_are_partial(tmp_path):
    frame = pd.DataFrame([
        _call(), _call(contract_symbol="NVDA260828C115000", strike=115, bid=None), _put_row(),
    ])
    result, df = _variant(tmp_path, frame)
    assert len(df) == result["candidate_count"] == 1
    assert (result["status"], result["reason"]) == ("candidates_found", "partial_data")
    evidence = df.attrs["_evidence_summary"]
    assert evidence == {
        "evaluated_contract_count": 2,  # Rejected Call plus evaluated Put.
        "eligibility_unresolved_count": 1,
    }


def test_true_zero_and_disabled_standalone_call_policy_preserve_original_results(tmp_path):
    cfg = _cc_lp_symbol_cfg()
    cfg["sell_call"]["enabled"] = False
    result, df = _variant(tmp_path, pd.DataFrame([_call(), _put_row()]), cfg=cfg)
    assert len(df) == result["candidate_count"] == 1
    assert result["status"] == "candidates_found"
    assert df.attrs["_evidence_summary"]["evaluated_contract_count"] == 1
    result, df = _variant(tmp_path, pd.DataFrame([_call(), _put_row(delta=-0.9)]))
    assert df.empty
    assert (result["status"], result["reason"]) == ("no_candidate", "no_candidate")
    assert df.attrs["_evidence_summary"]["eligibility_unresolved_count"] == 0


def test_candidate_count_is_pairs_and_evidence_denominator_follows_contract_owners(tmp_path):
    frame = pd.DataFrame([
        _call(), _call(contract_symbol="NVDA260828C115000", strike=115),
        _put_row(), _put_row(contract_symbol="NVDA260828P95000", strike=95),
        _put_row(contract_symbol="NVDA260828P85000", strike=85, bid=None),
    ])
    result, df = _variant(tmp_path, frame)
    assert len(df) == result["candidate_count"] == 4
    assert (result["status"], result["reason"]) == ("candidates_found", "partial_data")
    evidence = df.attrs["_evidence_summary"]
    assert evidence == {
        "evaluated_contract_count": 3,  # Three terminal Puts, independent of pair fanout.
        "eligibility_unresolved_count": 1,
    }


def test_empty_put_universe_keeps_funding_call_evidence(tmp_path):
    result, df = _variant(tmp_path, pd.DataFrame([_call()]))
    assert df.empty and result["candidate_count"] == 0
    assert result["status"] == "no_candidate"
    assert df.attrs["_evidence_summary"]["evaluated_contract_count"] == 1
    assert df.attrs["_evidence_summary"]["eligibility_unresolved_count"] == 0


def test_shared_put_gap_counts_once_per_contract(tmp_path):
    calls = pd.DataFrame([_scan_call_row(contract_symbol=f"CALL-{i}") for i in range(3)])
    df = cc_lp_steps.run_cc_lp_scan(**_cc_lp_scan_kwargs(
        tmp_path, stock=STOCK, required_data_frame=pd.DataFrame([_put_row(bid=None)]),
        run_sell_call_scan_fn=lambda **_: calls,
    ))
    evidence = df.attrs["_evidence_summary"]
    assert df.empty
    assert evidence["evaluated_contract_count"] == 1  # One Put frontier shared by three funding Calls.
    assert evidence["eligibility_unresolved_count"] == 1


@pytest.mark.parametrize("case,expected_status,expected_reason", [
    ("missing_bid", "unavailable", "data_unavailable"),
    ("nonstandard", "completed", "no_candidate"),
    ("mixed", "completed", "partial_data"),
    ("zero", "completed", "no_candidate"),
])
def test_real_monitoring_status_snapshot_manifest_bundle_preserves_funding_evidence(
    tmp_path, case, expected_status, expected_reason,
):
    call = _call()
    if case == "missing_bid":
        call["bid"] = None
    elif case == "nonstandard":
        call.update(option_standard_type="NON_STANDARD", opening_contract_status="ineligible",
                    opening_contract_reason_codes="option_non_standard")
    rows = [call, _put_row(delta=-0.9) if case == "zero" else _put_row()]
    if case == "mixed":
        rows.append(_call(contract_symbol="NVDA260828C115000", strike=115, bid=None))
    frame = pd.DataFrame(rows)
    report = tmp_path / "output_runs" / "run-1" / "accounts" / "lx"
    cfg = _cc_lp_symbol_cfg()
    cfg["sell_call"]["enabled"] = False
    cfg["sell_put"] = {"enabled": False}
    scans = []
    captured = []

    def ensure(**kwargs):
        kwargs["required_data_csv_bytes_sink_fn"](frame.to_csv(index=False).encode())
        return {"snapshot_id": "quote-1", "receipt_relpath": "quotes/quote-1/receipt.json"}

    def combo(**kwargs):
        return combo_yield_steps.run_cc_lp_variant(**_cc_lp_variant_kwargs(
            kwargs["symbol_cfg"], kwargs["required_data_dir"], portfolio_ctx=kwargs["portfolio_ctx"],
            stock=kwargs["stock"], required_data_frame=kwargs["required_data_frame"],
            combo_evidence_sink_fn=kwargs["combo_evidence_sink_fn"], run_cc_lp_scan_fn=_scanner(scans),
        ))

    deps = replace(
        _deps(), ensure_required_data_fn=ensure, run_combo_yield_scan_fn=combo,
        apply_prefilters_fn=lambda **kw: SimpleNamespace(
            want_put=False, want_call=True, sp=kw["sp"], cc=kw["cc"], stock=STOCK,
        ),
    )
    run_symbol_monitoring(inputs=_inputs(
        tmp_path, report_dir=report, symbol_cfg=cfg, portfolio_ctx=_available_cc_lp_context(),
        source_producer_run_id="run-1", required_data_snapshot_manifest=tmp_path / "manifest.json",
        runtime_config={"portfolio": {"account": "lx"}}, combo_evidence_sink_fn=captured.append,
    ), deps=deps)
    scope = next(item for item in _published_scopes(report) if item["strategy_mode"] == "combo_yield")
    assert (scope["status"], scope["reason"]) == (expected_status, expected_reason)
    index = publish_strategy_scan_status_index(
        report_dir=report, run_id="run-1", account="lx", account_config_sha256="a" * 64,
        expected=[{"market": "US", "symbol": "NVDA", "strategy_family": "combo_yield",
                   "strategy_mode": "combo_yield", "candidate_owner": "cc_lp",
                   "account_config_sha256": "a" * 64}],
        run_mode={"scan_mode": "standard", "executable": True},
    )
    pairs = [row for item in captured for row in item["ranked_pairs"]]
    seal_cc_lp_candidate_snapshot(
        base=tmp_path, run_id="run-1", account="lx", market="US",
        account_config_sha256="a" * 64, strategy_policy_sha256="b" * 64,
        run_mode={"scan_mode": "standard", "executable": True}, dependencies=_dependencies(),
        scan_statuses=_index_owner_statuses(index)["cc_lp"], ranked_pairs=pairs,
    )
    publish_candidate_snapshot_manifest(
        base=tmp_path, run_id="run-1", account="lx", strategy_policy_sha256="b" * 64,
    )
    bundle = load_candidate_snapshot_bundle(base=tmp_path, run_id="run-1", account="lx")
    frozen_scope = bundle["owners"]["cc_lp"]["scope_results"][0]
    assert (frozen_scope["status"], frozen_scope["reason_code"]) == (expected_status, expected_reason)
    assert len(bundle["owners"]["cc_lp"]["ranked_pairs"]) == (1 if case == "mixed" else 0)
