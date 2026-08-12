from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

from domain.domain.candidate_defaults import CandidateLiquidityDefaults, CandidateWindowDefaults
from domain.domain.engine import (
    EARNINGS_NEAR_EXPIRY_POLICY_VERSION,
    EARNINGS_NEAR_EXPIRY_WINDOW_DAYS,
)
from src.application.combo_yield_steps import (
    enrich_combo_funding_cash,
    run_combo_yield_for_symbol_and_summarize,
    run_combo_yield_scan_and_summarize,
)
from src.application.yield_enhancement_config import derive_yield_enhancement_policy
from src.infrastructure.exchange_rates import CurrencyConverter, ExchangeRates


def _candidate(**overrides) -> dict:
    row = {
        "symbol": "NVDA",
        "expiration": "2026-08-21",
        "dte": 35,
        "contract_symbol": "NVDA260821P00100000",
        "multiplier": 100,
        "currency": "USD",
        "strike": 100.0,
        "spot": 110.0,
        "bid": 2.0,
        "ask": 2.1,
        "mid": 2.05,
        "open_interest": 500,
        "volume": 50,
        "delta": -0.2,
        "event_flag": False,
        "event_source_status": "ok",
    }
    row.update(overrides)
    return row


def _earnings_evidence(*, event_date: str) -> dict:
    market_day = date(2026, 7, 17)
    expiration_day = date(2026, 8, 21)
    hard_start = expiration_day - timedelta(
        days=EARNINGS_NEAR_EXPIRY_WINDOW_DAYS
    )
    days_before_expiration = (
        expiration_day - date.fromisoformat(event_date)
    ).days
    blocking = days_before_expiration <= EARNINGS_NEAR_EXPIRY_WINDOW_DAYS
    event = {
        "earnings_date": event_date,
        "days_before_expiration": days_before_expiration,
        "classification": "blocking" if blocking else "nonblocking",
        "blocking": blocking,
    }
    return {
        "earnings_evidence_status": "ready",
        "earnings_reason_code": None,
        "earnings_policy_version": EARNINGS_NEAR_EXPIRY_POLICY_VERSION,
        "earnings_window_days": EARNINGS_NEAR_EXPIRY_WINDOW_DAYS,
        "earnings_market_date": market_day.isoformat(),
        "earnings_hard_window_start": hard_start.isoformat(),
        "earnings_hard_window_end": expiration_day.isoformat(),
        "earnings_hard_coverage_status": "complete",
        "earnings_soft_coverage_status": "complete",
        "earnings_has_event": True,
        "earnings_blocking_has_event": blocking,
        "earnings_events": [event],
        "earnings_blocking_events": [event] if blocking else [],
        "earnings_nonblocking_events": [] if blocking else [event],
    }


def _emit_accepted_underwriting_decisions(
    frame: pd.DataFrame,
    sink,
) -> None:
    if sink is None:
        return
    sink(
        [
            {
                "normalized_input": dict(row),
                "opening_decision": {
                    "accepted": True,
                    "rejects": [],
                },
            }
            for row in frame.to_dict("records")
        ]
    )


def _accept_all_underwriting(**kwargs) -> pd.DataFrame:
    out = kwargs["df_labeled"].copy()
    _emit_accepted_underwriting_decisions(
        out,
        kwargs.get("decision_sink_fn"),
    )
    return out


def _run(
    tmp_path: Path,
    *,
    candidates: list[dict],
    find_pairs_fn,
    yield_sp: dict | None = None,
    underwriting_filter_put_candidates_fn=None,
    cash_filter_put_candidates_fn=None,
    portfolio_ctx: dict | None = None,
    is_scheduled: bool = True,
    render_alerts_fn=None,
    combo_evidence_sink_fn=None,
    select_pairs_fn=None,
    account_run_scope: bool = False,
):
    report_dir = (
        tmp_path / "output_runs" / "run-1" / "accounts" / "lx"
        if account_run_scope
        else tmp_path / "reports"
    )
    report_dir.mkdir(parents=True, exist_ok=True)
    captured: dict[str, pd.DataFrame] = {}

    def run_put_scan_fn(**kwargs):
        captured["scan_kwargs"] = kwargs
        return pd.DataFrame(candidates)

    def capture_pairs(**kwargs):
        captured["df"] = kwargs["df_candidates"].copy()
        return find_pairs_fn(**kwargs)

    def capture_cash(**kwargs):
        out = cash_filter_put_candidates_fn(**kwargs)
        captured["cash_df"] = out.copy()
        return out

    yield_cfg = {"enabled": True}
    policy = derive_yield_enhancement_policy(yield_cfg)
    _result, summary = run_combo_yield_scan_and_summarize(
        sym="NVDA",
        symbol="NVDA",
        symbol_lower="nvda",
        symbol_cfg={"symbol": "NVDA", "combo_yield": {"enabled": True}},
        yield_enhancement_cfg=yield_cfg,
        yield_sp={
            "strategy": "insurance_underwriting",
            "min_annualized_net_return": 0.10,
            **(yield_sp or {}),
        },
        yield_enhancement_policy=policy,
        required_data_dir=tmp_path / "required_data",
        report_dir=report_dir,
        yield_window=CandidateWindowDefaults(min_dte=7, max_dte=60),
        liquidity=CandidateLiquidityDefaults(),
        exchange_rate_converter=CurrencyConverter(ExchangeRates(usd_per_cny=0.14)),
        portfolio_ctx=portfolio_ctx,
        top_n=3,
        is_scheduled=is_scheduled,
        run_put_scan_fn=run_put_scan_fn,
        find_pairs_fn=capture_pairs,
        select_pairs_fn=select_pairs_fn or (lambda df: df),
        cash_filter_put_candidates_fn=(
            capture_cash if cash_filter_put_candidates_fn is not None else None
        ),
        underwriting_filter_put_candidates_fn=(
            underwriting_filter_put_candidates_fn
            or _accept_all_underwriting
        ),
        **({"render_alerts_fn": render_alerts_fn} if render_alerts_fn is not None else {}),
        **(
            {"combo_evidence_sink_fn": combo_evidence_sink_fn}
            if combo_evidence_sink_fn is not None
            else {}
        ),
    )
    trace = [
        json.loads(line)
        for line in (report_dir / "candidate_filter_trace.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert trace
    expected_evidence_path = (
        "state/combo_yield_candidate_snapshot.json"
        if account_run_scope and combo_evidence_sink_fn is not None
        else "candidate_filter_trace.jsonl"
    )
    assert {row["evidence_path"] for row in trace} == {expected_evidence_path}
    if "cash_df" in captured:
        captured["scan_kwargs"]["_cash_output"] = captured["cash_df"]
    return captured["df"], trace, captured["scan_kwargs"], summary


def test_combo_yield_trace_links_account_run_snapshot_when_capture_is_enabled(
    tmp_path: Path,
) -> None:
    evidence: list[dict] = []

    _rows, trace, _scan_kwargs, _summary = _run(
        tmp_path,
        candidates=[_candidate(annualized_net_return_on_cash_basis=0.18)],
        find_pairs_fn=lambda **_kwargs: pd.DataFrame(),
        combo_evidence_sink_fn=evidence.append,
        account_run_scope=True,
    )

    assert evidence
    assert {row["evidence_path"] for row in trace} == {
        "state/combo_yield_candidate_snapshot.json"
    }


def test_combo_yield_reuses_sell_put_underwriting_before_pairing(tmp_path: Path) -> None:
    captured_underwriting: dict[str, object] = {}

    def underwriting_gate(**kwargs):
        captured_underwriting.update(kwargs)
        out = kwargs["df_labeled"].copy()
        out["premium_edge_score"] = 0.25
        out["strike_safety_margin_pct"] = 0.12
        _emit_accepted_underwriting_decisions(
            out,
            kwargs.get("decision_sink_fn"),
        )
        return out

    captured, _trace, _scan_kwargs, _summary = _run(
        tmp_path,
        candidates=[_candidate(annualized_net_return_on_cash_basis=0.18)],
        find_pairs_fn=lambda **_kwargs: pd.DataFrame(),
        yield_sp={"strategy": "insurance_underwriting", "min_annualized_net_return": 0.15},
        underwriting_filter_put_candidates_fn=underwriting_gate,
    )

    assert captured_underwriting["sell_put_cfg"]["strategy"] == "insurance_underwriting"
    assert captured_underwriting["symbol"] == "NVDA"
    assert len(captured) == 1
    assert float(captured.iloc[0]["premium_edge_score"]) == 0.25
    assert float(captured.iloc[0]["strike_safety_margin_pct"]) == 0.12


def test_combo_yield_put_underwriting_inherits_sell_put_hard_gates(tmp_path: Path) -> None:
    captured_underwriting: dict[str, object] = {}

    def underwriting_gate(**kwargs):
        captured_underwriting.update(kwargs)
        out = kwargs["df_labeled"].copy()
        _emit_accepted_underwriting_decisions(
            out,
            kwargs.get("decision_sink_fn"),
        )
        return out

    _run(
        tmp_path,
        candidates=[_candidate(annualized_net_return_on_cash_basis=0.18)],
        find_pairs_fn=lambda **_kwargs: pd.DataFrame(),
        yield_sp={
            "strategy": "insurance_underwriting",
            "min_annualized_net_return": 0.10,
            "min_net_income": 50.0,
            "min_strike": 90.0,
            "max_strike": 105.0,
            "max_spread_ratio": 0.40,
        },
        underwriting_filter_put_candidates_fn=underwriting_gate,
    )

    sell_put_cfg = captured_underwriting["sell_put_cfg"]
    assert isinstance(sell_put_cfg, dict)
    assert sell_put_cfg["strategy"] == "insurance_underwriting"
    assert sell_put_cfg["min_net_income"] == 50.0
    assert sell_put_cfg["min_annualized_net_return"] == 0.10
    assert sell_put_cfg["min_strike"] == 90.0
    assert sell_put_cfg["max_strike"] == 105.0
    assert sell_put_cfg["max_spread_ratio"] == 0.40


def test_combo_funding_put_uses_same_six_day_earnings_window_in_memory(
    tmp_path: Path,
) -> None:
    from src.application.sell_put_strategy_risk import (
        enrich_and_filter_sell_put_underwriting,
    )

    common = {
        "annualized_net_return_on_cash_basis": 0.18,
        "period_net_return_on_cash_basis": 0.02,
        "net_income": 200.0,
        "spread_ratio": 0.05,
        "implied_volatility": 0.36,
        "term_matched_rv": 0.24,
        "max_new_contracts": 1,
    }
    near, _trace, _scan, near_summary = _run(
        tmp_path / "near",
        candidates=[
            _candidate(
                **common,
                **_earnings_evidence(event_date="2026-08-15"),
            )
        ],
        find_pairs_fn=lambda **_kwargs: pd.DataFrame(),
        underwriting_filter_put_candidates_fn=(
            enrich_and_filter_sell_put_underwriting
        ),
    )
    distant, _trace, _scan, distant_summary = _run(
        tmp_path / "distant",
        candidates=[
            _candidate(
                **common,
                **_earnings_evidence(event_date="2026-08-14"),
            )
        ],
        find_pairs_fn=lambda **_kwargs: pd.DataFrame(),
        underwriting_filter_put_candidates_fn=(
            enrich_and_filter_sell_put_underwriting
        ),
    )

    assert near.empty
    assert list(distant["contract_symbol"]) == ["NVDA260821P00100000"]
    assert near_summary["_strategy_status"] == "completed"
    assert near_summary["_strategy_reason"] == "no_candidate"
    assert distant_summary["_strategy_status"] == "completed"
    assert distant_summary["_strategy_reason"] == "no_candidate"


def test_combo_funding_put_only_hard_evidence_gap_is_data_unavailable(
    tmp_path: Path,
) -> None:
    from src.application.sell_put_strategy_risk import (
        enrich_and_filter_sell_put_underwriting,
    )

    captured, _trace, _scan, summary = _run(
        tmp_path,
        candidates=[
            _candidate(
                annualized_net_return_on_cash_basis=0.18,
                period_net_return_on_cash_basis=0.02,
                net_income=200.0,
                spread_ratio=0.05,
                implied_volatility=0.36,
                term_matched_rv=0.24,
                max_new_contracts=1,
                earnings_evidence_status="data_unavailable",
                earnings_reason_code="opend_earnings_calendar_interval_failed",
            )
        ],
        find_pairs_fn=lambda **_kwargs: pd.DataFrame(),
        underwriting_filter_put_candidates_fn=(
            enrich_and_filter_sell_put_underwriting
        ),
    )

    assert captured.empty
    assert summary["_strategy_status"] == "unavailable"
    assert summary["_strategy_reason"] == "data_unavailable"
    assert summary["_evidence_summary"]["eligibility_unresolved_count"] == 1


def test_combo_funding_put_keeps_valid_pair_with_unresolved_sibling(
    tmp_path: Path,
) -> None:
    from src.application.sell_put_strategy_risk import (
        enrich_and_filter_sell_put_underwriting,
    )

    common = {
        "annualized_net_return_on_cash_basis": 0.18,
        "period_net_return_on_cash_basis": 0.02,
        "net_income": 200.0,
        "spread_ratio": 0.05,
        "implied_volatility": 0.36,
        "term_matched_rv": 0.24,
        "max_new_contracts": 1,
    }

    def pair_valid_put(**kwargs):
        assert list(kwargs["df_candidates"]["contract_symbol"]) == [
            "NVDA260821P00100000"
        ]
        return pd.DataFrame(
            [
                {
                    "candidate_pair_id": "combo_yield:NVDA:P:C",
                    "symbol": "NVDA",
                    "put_contract_symbol": "NVDA260821P00100000",
                    "call_contract_symbol": "NVDA260821C00120000",
                    "annualized_net_credit_yield": 0.11,
                }
            ]
        )

    captured, _trace, _scan, summary = _run(
        tmp_path,
        candidates=[
            _candidate(
                **common,
                **_earnings_evidence(event_date="2026-08-14"),
            ),
            _candidate(
                contract_symbol="NVDA260821P00095000",
                strike=95.0,
                **common,
                earnings_evidence_status="data_unavailable",
                earnings_reason_code="opend_earnings_calendar_interval_failed",
            ),
        ],
        find_pairs_fn=pair_valid_put,
        underwriting_filter_put_candidates_fn=(
            enrich_and_filter_sell_put_underwriting
        ),
    )

    assert list(captured["contract_symbol"]) == ["NVDA260821P00100000"]
    assert summary["candidate_count"] == 1
    assert summary["_strategy_status"] == "completed"
    assert summary["_strategy_reason"] == "partial_data"
    assert summary["_evidence_summary"]["eligibility_unresolved_count"] == 1


def test_combo_funding_put_gap_plus_definitive_reject_is_clean_no_candidate(
    tmp_path: Path,
) -> None:
    from src.application.sell_put_strategy_risk import (
        enrich_and_filter_sell_put_underwriting,
    )

    captured, _trace, _scan, summary = _run(
        tmp_path,
        candidates=[
            _candidate(
                annualized_net_return_on_cash_basis=0.01,
                period_net_return_on_cash_basis=0.001,
                net_income=10.0,
                spread_ratio=0.05,
                implied_volatility=0.36,
                term_matched_rv=0.24,
                max_new_contracts=1,
                earnings_evidence_status="data_unavailable",
                earnings_reason_code="opend_earnings_calendar_interval_failed",
            )
        ],
        find_pairs_fn=lambda **_kwargs: pd.DataFrame(),
        underwriting_filter_put_candidates_fn=(
            enrich_and_filter_sell_put_underwriting
        ),
    )

    assert captured.empty
    assert summary["_strategy_status"] == "completed"
    assert summary["_strategy_reason"] == "no_candidate"
    assert summary["_evidence_summary"]["eligibility_unresolved_count"] == 0
    assert summary["_evidence_summary"]["diagnostic_evidence_gap_count"] == 1


def test_combo_cash_enrichment_preserves_gap_plus_capacity_reject_decision(
    tmp_path: Path,
) -> None:
    from src.application.sell_put_strategy_risk import (
        enrich_and_filter_sell_put_underwriting,
    )

    captured, _trace, _scan, summary = _run(
        tmp_path,
        candidates=[
            _candidate(
                annualized_net_return_on_cash_basis=0.18,
                period_net_return_on_cash_basis=0.02,
                net_income=200.0,
                spread_ratio=0.05,
                implied_volatility=0.36,
                term_matched_rv=0.24,
                max_new_contracts=9,
                earnings_evidence_status="data_unavailable",
                earnings_reason_code="opend_earnings_calendar_interval_failed",
            )
        ],
        find_pairs_fn=lambda **_kwargs: pd.DataFrame(),
        cash_filter_put_candidates_fn=enrich_combo_funding_cash,
        portfolio_ctx={
            "cash_by_currency": {"USD": 0.0},
            "option_ctx": {
                "cash_secured_total_by_ccy": {},
                "cash_secured_total_cny": 0.0,
                "cash_secured_by_symbol_by_ccy": {},
            },
        },
        underwriting_filter_put_candidates_fn=(
            enrich_and_filter_sell_put_underwriting
        ),
    )

    assert captured.empty
    assert summary["_strategy_status"] == "completed"
    assert summary["_strategy_reason"] == "no_candidate"
    evidence = summary["_evidence_summary"]
    assert evidence["evaluated_contract_count"] == 1
    assert evidence["eligibility_unresolved_count"] == 0
    assert evidence["diagnostic_evidence_gap_count"] == 1
    cash_audit = _scan["_cash_output"]
    assert list(cash_audit["contract_symbol"]) == ["NVDA260821P00100000"]
    assert int(cash_audit.iloc[0]["max_new_contracts"]) == 0


def test_combo_yield_facade_forces_funding_put_underwriting_when_sell_put_is_disabled(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import src.application.combo_yield_steps as steps

    captured: dict[str, object] = {}

    def fake_run(**kwargs):
        captured.update(kwargs)
        return steps._empty_result(), {
            "strategy": "combo_yield",
        }

    monkeypatch.setattr(steps, "run_combo_yield_scan_and_summarize", fake_run)

    summary = run_combo_yield_for_symbol_and_summarize(
        sym="NVDA",
        symbol="NVDA",
        symbol_lower="nvda",
        symbol_cfg={
            "symbol": "NVDA",
            "combo_yield": {"enabled": True},
            "sell_put": {"enabled": False},
        },
        sell_put_cfg={"enabled": False},
        top_n=3,
        required_data_dir=tmp_path / "required_data",
        report_dir=tmp_path / "reports",
        is_scheduled=True,
        exchange_rate_converter=CurrencyConverter(ExchangeRates()),
        portfolio_ctx=None,
    )

    assert summary == {"strategy": "combo_yield"}
    assert captured["yield_sp"] == {
        "enabled": False,
        "strategy": "insurance_underwriting",
    }
    assert captured["yield_enhancement_policy"].requires_realized_volatility is True


def test_combo_yield_writes_pair_rejection_aggregates_to_trace(tmp_path: Path) -> None:
    evidence: list[dict] = []

    def rejected_pairs(**_kwargs):
        out = pd.DataFrame()
        out.attrs["reject_counts"] = {"min_net_credit_retention": 3}
        out.attrs["pair_diagnostics"] = pd.DataFrame(
            [
                {
                    "diagnostic_scope": "pair",
                    "diagnostic_stage": "pair_filter",
                    "accepted": False,
                    "reject_reasons": "min_net_credit_retention",
                    "put_contract_symbol": "NVDA260821P00100000",
                    "call_contract_symbol": "NVDA260821C00120000",
                    "net_credit_retention": 0.72,
                }
            ]
        )
        return out

    captured, trace, _scan_kwargs, _summary = _run(
        tmp_path,
        candidates=[_candidate()],
        find_pairs_fn=rejected_pairs,
        combo_evidence_sink_fn=evidence.append,
    )

    assert len(captured) == 1
    pair_rows = [row for row in trace if row["stage"] == "combo_pair_filter"]
    assert len(pair_rows) == 1
    assert pair_rows[0]["rule"] == "min_net_credit_retention"
    assert pair_rows[0]["metric_value"] == 3
    diagnostics = pd.DataFrame(evidence[0]["pair_evaluations"])
    assert diagnostics[["put_contract_symbol", "call_contract_symbol"]].to_dict("records") == [
        {
            "put_contract_symbol": "NVDA260821P00100000",
            "call_contract_symbol": "NVDA260821C00120000",
        }
    ]
    assert diagnostics.loc[0, "reject_reasons"] == "min_net_credit_retention"
    assert {"run_id", "account"}.issubset(diagnostics.columns)
    assert len(evidence) == 1
    assert evidence[0]["schema_version"] == "combo_yield_scan_evidence.v1"
    assert evidence[0]["variant"] == "sp_lc"
    assert evidence[0]["symbol"] == "NVDA"
    assert evidence[0]["pair_evaluations"] == diagnostics.to_dict("records")
    assert evidence[0]["rank_records"] == []
    assert evidence[0]["ranked_pairs"] == []


def test_combo_yield_writes_real_diagnostics_when_call_prefilter_removes_all_pairs(tmp_path: Path) -> None:
    from src.application.sell_put_call_helper import find_sell_put_yield_enhancement_pairs

    parsed = tmp_path / "required_data" / "parsed"
    parsed.mkdir(parents=True)
    pd.DataFrame(
        [
            {
                "symbol": "NVDA",
                "option_type": "call",
                "expiration": "2026-08-21",
                "dte": 35,
                "contract_symbol": "NVDA260821C00120000",
                "strike": 120.0,
                "spot": 110.0,
                "bid": 0.9,
                "ask": 1.0,
                "mid": 0.95,
                "open_interest": 500,
                "volume": 50,
                "implied_volatility": 0.40,
                "currency": "USD",
                "delta": 0.30,
                "multiplier": 100,
            }
        ]
    ).to_csv(parsed / "NVDA_required_data.csv", index=False)

    evidence: list[dict] = []
    _run(
        tmp_path,
        candidates=[_candidate(annualized_net_return_on_cash_basis=0.18)],
        find_pairs_fn=find_sell_put_yield_enhancement_pairs,
        combo_evidence_sink_fn=evidence.append,
    )

    diagnostics = pd.DataFrame(evidence[0]["pair_evaluations"])
    call_reject = diagnostics.loc[diagnostics["diagnostic_scope"] == "call"].iloc[0]
    assert call_reject["call_contract_symbol"] == "NVDA260821C00120000"
    assert call_reject["reject_reasons"] == "call_delta_above_max"
    assert float(call_reject["policy_call_max_delta"]) == 0.20
    assert int(diagnostics["accepted"].sum()) == 0


def test_combo_yield_uses_standalone_sell_put_return_floor_and_annotations(tmp_path: Path) -> None:
    captured, trace, scan_kwargs, _summary = _run(
        tmp_path,
        candidates=[_candidate(annualized_net_return_on_cash_basis=0.18)],
        find_pairs_fn=lambda **_kwargs: pd.DataFrame(),
        yield_sp={"strategy": "insurance_underwriting", "min_annualized_net_return": 0.15},
    )

    assert scan_kwargs["min_annualized_net_return"] == 0.15
    assert scan_kwargs["min_net_income"] == 0.0
    assert len(captured) == 1
    row = captured.iloc[0]
    assert bool(row["funding_put_eligible"]) is True
    assert float(row["funding_put_min_annualized_return"]) == 0.15
    assert float(row["put_only_annualized_net_return"]) == 0.18
    assert "combo_yield_no_pair" in {item["rule"] for item in trace}


def test_combo_yield_traces_when_no_funding_put_is_eligible(tmp_path: Path) -> None:
    _captured, trace, scan_kwargs, _summary = _run(
        tmp_path,
        candidates=[],
        find_pairs_fn=lambda **_kwargs: pd.DataFrame(),
        yield_sp={"min_annualized_net_return": 0.16},
    )

    assert scan_kwargs["min_annualized_net_return"] == 0.16
    row = next(item for item in trace if item["stage"] == "post_filter")
    assert row["rule"] == "combo_yield_no_funding_put_eligible"
    assert row["threshold"] == 0.16


def test_combo_yield_emits_shadow_rank_evidence_without_changing_selection(tmp_path: Path) -> None:
    from src.application.sell_put_call_helper import (
        select_best_yield_enhancement_pairs,
    )

    pairs = pd.DataFrame(
        [
            {
                "symbol": "NVDA",
                "candidate_pair_id": "combo_yield:NVDA:NVDA_P100:NVDA_C110",
                "put_contract_symbol": "NVDA_P100",
                "call_contract_symbol": "NVDA_C110",
                "funding_accepted": True,
                "premium_funding_score": 0.9,
                "net_credit_retention": 0.80,
                "call_cost_to_put_credit": 0.20,
                "call_delta": 0.18,
                "call_spread_ratio": 0.12,
                "call_open_interest": 500,
                "call_payoff_multiple_at_1_5_sigma": 1.8,
                "call_payoff_multiple_at_2_0_sigma": 4.0,
                "put_assignment_margin_pct": 0.05,
                "put_only_annualized_net_return": 0.14,
                "combo_spread_ratio": 0.20,
                "annualized_net_credit_yield": 0.09,
                "residual_premium_ratio": 0.80,
            },
            {
                "symbol": "NVDA",
                "candidate_pair_id": "combo_yield:NVDA:NVDA_P100:NVDA_C115",
                "put_contract_symbol": "NVDA_P100",
                "call_contract_symbol": "NVDA_C115",
                "funding_accepted": True,
                "premium_funding_score": 1.1,
                "net_credit_retention": 0.88,
                "call_cost_to_put_credit": 0.12,
                "call_delta": 0.10,
                "call_spread_ratio": 0.08,
                "call_open_interest": 900,
                "call_payoff_multiple_at_1_5_sigma": 1.0,
                "call_payoff_multiple_at_2_0_sigma": 3.0,
                "put_assignment_margin_pct": 0.05,
                "put_only_annualized_net_return": 0.14,
                "combo_spread_ratio": 0.15,
                "annualized_net_credit_yield": 0.11,
                "residual_premium_ratio": 0.88,
            },
        ]
    )

    def find_pairs(**_kwargs):
        out = pairs.copy()
        out.attrs["pair_diagnostics"] = pd.DataFrame(
            [
                {
                    **row,
                    "diagnostic_scope": "pair",
                    "diagnostic_stage": "pair_filter",
                    "accepted": True,
                    "reject_reasons": "",
                }
                for row in pairs.to_dict("records")
            ]
        )
        return out

    evidence: list[dict] = []
    _captured, _trace, _scan_kwargs, _summary = _run(
        tmp_path,
        candidates=[_candidate(annualized_net_return_on_cash_basis=0.14)],
        find_pairs_fn=find_pairs,
        combo_evidence_sink_fn=evidence.append,
        select_pairs_fn=select_best_yield_enhancement_pairs,
    )

    artifact = pd.DataFrame(evidence[0]["rank_records"])
    assert artifact.loc[artifact["baseline_selected"], "call_contract_symbol"].tolist() == ["NVDA_C115"]
    assert artifact.loc[artifact["shadow_selected"], "call_contract_symbol"].tolist() == ["NVDA_C110"]
    assert artifact["rank_changed"].all()
    assert len(evidence) == 1
    pd.testing.assert_frame_equal(
        pd.DataFrame(evidence[0]["rank_records"]),
        artifact,
        check_dtype=False,
    )
    selected = evidence[0]["ranked_pairs"]
    assert [row["call_contract_symbol"] for row in selected] == ["NVDA_C115"]
    diagnostics = evidence[0]["pair_evaluations"]
    assert {
        row["candidate_pair_id"]
        for row in diagnostics
        if row["diagnostic_scope"] == "pair" and bool(row["accepted"])
    } == {
        "combo_yield:NVDA:NVDA_P100:NVDA_C110",
        "combo_yield:NVDA:NVDA_P100:NVDA_C115",
    }


def test_combo_yield_does_not_write_candidate_compatibility_csv(tmp_path: Path) -> None:
    _run(
        tmp_path,
        candidates=[_candidate()],
        find_pairs_fn=lambda **_kwargs: pd.DataFrame(),
    )

    assert not list((tmp_path / "reports").glob("*combo_yield*.csv"))


def test_combo_yield_manual_render_failure_propagates(tmp_path: Path) -> None:
    def fail_render(**_kwargs) -> str:
        raise RuntimeError("render failed")

    with pytest.raises(RuntimeError, match="render failed"):
        _run(
            tmp_path,
            candidates=[_candidate()],
            find_pairs_fn=lambda **_kwargs: pd.DataFrame(),
            is_scheduled=False,
            render_alerts_fn=fail_render,
        )


def test_empty_combo_yield_does_not_materialize_candidate_csv(tmp_path: Path) -> None:
    _run(
        tmp_path,
        candidates=[],
        find_pairs_fn=lambda **_kwargs: pd.DataFrame(),
    )

    assert not list((tmp_path / "reports").glob("*.csv"))
