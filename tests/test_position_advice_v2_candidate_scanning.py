from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from src.application.candidate_scanning import (
    CandidateScanConfig,
    CandidateScanDependencies,
    run_candidate_scan,
)


def _scan(
    tmp_path: Path,
    *,
    annotate_event_risk_fn,
    compute_metrics_fn=None,
    put_cash_free: float = 5_000,
) -> tuple[pd.DataFrame, list[dict[str, object]]]:
    input_root = tmp_path / "input"
    parsed = input_root / "parsed"
    parsed.mkdir(parents=True)
    pd.DataFrame(
        [
            {
                "symbol": "NVDA",
                "option_type": "put",
                "expiration": "2026-09-18",
                "contract_symbol": "NVDA_CAPACITY_ONLY",
                "currency": "USD",
                "dte": 45,
                "strike": 100,
                "spot": 120,
                "bid": 1.9,
                "ask": 2.1,
                "mid": 2,
                "open_interest": 500,
                "volume": 50,
                "multiplier": 100,
            },
            {
                "symbol": "NVDA",
                "option_type": "put",
                "expiration": "2026-09-18",
                "contract_symbol": "NVDA_EVENT_BLOCKED",
                "currency": "USD",
                "dte": 45,
                "strike": 98,
                "spot": 120,
                "bid": 1.9,
                "ask": 2.1,
                "mid": 2,
                "open_interest": 500,
                "volume": 50,
                "multiplier": 100,
            },
        ]
    ).to_csv(parsed / "NVDA_required_data.csv", index=False)
    captured: list[dict[str, object]] = []
    output = tmp_path / "output" / "candidates.csv"
    result = run_candidate_scan(
        config=CandidateScanConfig(
            mode="put",
            symbols=["NVDA"],
            input_root=input_root,
            output=output,
            empty_output_columns=["symbol"],
            min_dte=14,
            max_dte=60,
            min_strike=None,
            max_strike=None,
            min_open_interest=100,
            min_volume=10,
            max_spread_ratio=0.3,
            min_annualized_net_return=0.08,
            min_net_income=50,
            quiet=True,
            risk_policy_version="candidate_policy.v2",
            quote_snapshot_id="quote-snapshot-1",
        ),
        deps=CandidateScanDependencies(
            compute_metrics_fn=compute_metrics_fn
            or (
                lambda _contract: {
                    "annualized_net_return_on_cash_basis": 0.12,
                    "net_income": 100,
                }
            ),
            build_row_fn=lambda contract, _base, metrics: {
                **contract.to_gate_payload(),
                **metrics,
            },
            build_hard_constraint_kwargs_fn=lambda _contract: {
                "put_cash_required": 10_000,
                "put_cash_free": put_cash_free,
            },
            annualized_return_value_fn=lambda metrics: metrics.get(
                "annualized_net_return_on_cash_basis"
            ),
            annotate_event_risk_fn=annotate_event_risk_fn,
            print_summary_fn=lambda _out, _output, _reject_output: None,
            all_decisions_sink_fn=captured.extend,
        ),
        event_risk_cfg={"mode": "reject"},
        base_dir=tmp_path,
    )
    return result, captured


def test_all_decisions_preserves_capacity_reject_and_replays_event_invariant(
    tmp_path: Path,
) -> None:
    def annotate(
        frame: pd.DataFrame,
        _base: Path,
        _cfg: dict[str, object] | None,
    ) -> pd.DataFrame:
        out = frame.copy()
        out["event_flag"] = out["contract_symbol"].eq("NVDA_EVENT_BLOCKED")
        return out

    opening_output, decisions = _scan(
        tmp_path,
        annotate_event_risk_fn=annotate,
    )

    assert opening_output.empty
    by_contract = {
        item["normalized_input"]["contract_symbol"]: item
        for item in decisions
    }
    capacity = by_contract["NVDA_CAPACITY_ONLY"]
    blocked = by_contract["NVDA_EVENT_BLOCKED"]
    assert capacity["replacement_candidate_decision"]["replacement_eligibility"] == (
        "capacity_deferred_to_allocator"
    )
    assert capacity["opening_decision"]["accepted"] is False
    assert capacity["invariant_decision"]["accepted"] is True
    assert blocked["replacement_candidate_decision"]["replacement_eligibility"] == (
        "rejected_invariant"
    )
    assert {
        reject["reason"]
        for reject in blocked["invariant_decision"]["rejects"]
    } == {"risk_event_reject"}


def test_all_decisions_rejects_event_annotator_cardinality_change(
    tmp_path: Path,
) -> None:
    def duplicate(
        frame: pd.DataFrame,
        _base: Path,
        _cfg: dict[str, object] | None,
    ) -> pd.DataFrame:
        return pd.concat([frame, frame.iloc[[0]]], ignore_index=True)

    with pytest.raises(ValueError, match="changed all-decisions cardinality"):
        _scan(tmp_path, annotate_event_risk_fn=duplicate)


def test_all_decisions_reuses_opening_metrics_for_capacity_accepted_rows(
    tmp_path: Path,
) -> None:
    metric_calls: list[str] = []

    def compute_metrics(contract) -> dict[str, float]:
        metric_calls.append(str(contract.contract_symbol))
        return {
            "annualized_net_return_on_cash_basis": 0.12,
            "net_income": 100,
        }

    def annotate(
        frame: pd.DataFrame,
        _base: Path,
        _cfg: dict[str, object] | None,
    ) -> pd.DataFrame:
        out = frame.copy()
        out["event_flag"] = False
        return out

    opening_output, decisions = _scan(
        tmp_path,
        annotate_event_risk_fn=annotate,
        compute_metrics_fn=compute_metrics,
        put_cash_free=20_000,
    )

    assert len(opening_output) == 2
    assert len(decisions) == 2
    assert sorted(metric_calls) == [
        "NVDA_CAPACITY_ONLY",
        "NVDA_EVENT_BLOCKED",
    ]
