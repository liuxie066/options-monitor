from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import pytest

from conftest import phase2_opening_row
from domain.domain.engine.candidate_engine import (
    REJECT_RISK_EARNINGS_UNAVAILABLE,
)
from src.application.candidate_scanning import (
    CandidateScanConfig,
    CandidateScanDependencies,
    _load_required_data_rows,
    evidence_summary_from_decisions,
    run_candidate_scan,
)
from src.application.sell_call_steps import _evidence_scan_status as call_status
from src.application.sell_put_steps import _evidence_scan_status as put_status
from src.application.scan_sell_put import run_sell_put_scan


def _decision(*, accepted: bool = False, reasons: tuple[str, ...] = ()) -> dict:
    return {
        "opening_decision": {
            "accepted": accepted,
            "rejects": [
                {
                    "reason": reason,
                    "metric_value": (
                        {"reason_code": "opend_earnings_calendar_interval_failed"}
                        if reason == REJECT_RISK_EARNINGS_UNAVAILABLE
                        else None
                    ),
                }
                for reason in reasons
            ],
        }
    }


def test_earnings_only_gap_is_an_unresolved_contract_outcome() -> None:
    summary = evidence_summary_from_decisions(
        decisions=[
            _decision(reasons=(REJECT_RISK_EARNINGS_UNAVAILABLE,))
        ],
        accepted_count=0,
    )

    assert summary["evaluated_contract_count"] == 1
    assert summary["eligibility_unresolved_count"] == 1
    assert summary["diagnostic_evidence_gap_count"] == 1
    assert summary["policy_rejected_count"] == 0
    assert summary["unavailable_by_reason"] == {
        "opend_earnings_calendar_interval_failed": 1
    }


def test_definitive_reject_keeps_earnings_gap_diagnostic_only() -> None:
    summary = evidence_summary_from_decisions(
        decisions=[
            _decision(
                reasons=(
                    REJECT_RISK_EARNINGS_UNAVAILABLE,
                    "hard_dte",
                )
            )
        ],
        accepted_count=0,
    )

    assert summary["eligibility_unresolved_count"] == 0
    assert summary["diagnostic_evidence_gap_count"] == 1
    assert summary["policy_rejected_count"] == 1
    assert put_status(evidence=summary, candidate_count=0) == (
        "completed",
        "no_candidate",
    )
    assert call_status(evidence=summary, candidate_count=0) == (
        "completed",
        "no_candidate",
    )


@pytest.mark.parametrize("project", [put_status, call_status])
def test_accepted_candidate_with_unresolved_sibling_is_partial(
    project,
) -> None:
    summary = evidence_summary_from_decisions(
        decisions=[
            _decision(accepted=True),
            _decision(reasons=(REJECT_RISK_EARNINGS_UNAVAILABLE,)),
        ],
        accepted_count=1,
    )

    assert project(evidence=summary, candidate_count=1) == (
        "completed",
        "partial_data",
    )


@pytest.mark.parametrize("project", [put_status, call_status])
def test_accepted_candidate_with_definitive_sibling_gap_is_complete(
    project,
) -> None:
    summary = evidence_summary_from_decisions(
        decisions=[
            _decision(accepted=True),
            _decision(
                reasons=(
                    REJECT_RISK_EARNINGS_UNAVAILABLE,
                    "return_annualized",
                )
            ),
        ],
        accepted_count=1,
    )

    assert summary["diagnostic_evidence_gap_count"] == 1
    assert summary["eligibility_unresolved_count"] == 0
    assert project(evidence=summary, candidate_count=1) == (
        "completed",
        None,
    )


def test_summary_rejects_accepted_count_drift() -> None:
    with pytest.raises(
        ValueError,
        match="accepted candidate count does not match",
    ):
        evidence_summary_from_decisions(
            decisions=[_decision(accepted=True)],
            accepted_count=0,
        )


def test_supplied_required_data_frame_avoids_legacy_csv_read(
    monkeypatch,
    tmp_path,
) -> None:
    frame = pd.DataFrame(
        [
            {"symbol": "NVDA", "option_type": "put"},
            {"symbol": "NVDA", "option_type": "call"},
        ]
    )
    monkeypatch.setattr(
        "src.application.candidate_scanning.pd.read_csv",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("canonical frame must avoid the legacy CSV")
        ),
    )

    result = _load_required_data_rows(
        input_root=tmp_path,
        symbol="NVDA",
        mode="put",
        frames={"NVDA": frame},
    )

    assert result.to_dict("records") == [
        {"symbol": "NVDA", "option_type": "put"}
    ]


def test_sell_put_scan_classifies_missing_canonical_status_as_unavailable(
    tmp_path,
) -> None:
    row = phase2_opening_row(
        {
            "symbol": "NVDA",
            "option_type": "put",
            "expiration": "2026-09-18",
            "contract_symbol": "US.NVDA260918P00100000",
            "currency": "USD",
            "dte": 43,
            "strike": 100.0,
            "spot": 110.0,
            "bid": 1.0,
            "ask": 1.01,
            "multiplier": 100,
            "implied_volatility": 0.30,
        }
    )
    row.pop("opening_contract_status")
    decisions: list[dict] = []

    result = run_sell_put_scan(
        symbols=["NVDA"],
        input_root=tmp_path,
        min_annualized_net_return=0.10,
        quote_freshness_now_utc=datetime(
            2026,
            4,
            1,
            15,
            0,
            tzinfo=timezone.utc,
        ),
        calculation_decision_sink_fn=decisions.extend,
        required_data_frames={"NVDA": pd.DataFrame([row])},
    )

    assert result.empty
    assert len(decisions) == 1
    reject = decisions[0]["opening_decision"]["rejects"][0]
    assert reject["reason"] == "evidence_unavailable"
    assert reject["metric_value"]["reason_code"] == "evidence_unavailable"


@pytest.mark.parametrize(
    ("overrides", "specific_reason"),
    [
        ({"option_standard_type": "NON_STANDARD"}, "option_non_standard"),
        ({"snapshot_multiplier": 50}, "option_multiplier_conflict"),
    ],
)
def test_sell_put_scan_classifies_explicit_contract_conflicts_as_ineligible(
    tmp_path,
    overrides: dict,
    specific_reason: str,
) -> None:
    row = phase2_opening_row(
        {
            "symbol": "NVDA",
            "option_type": "put",
            "expiration": "2026-09-18",
            "contract_symbol": "US.NVDA260918P00100000",
            "currency": "USD",
            "dte": 43,
            "strike": 100.0,
            "spot": 110.0,
            "bid": 1.0,
            "ask": 1.01,
            "multiplier": 100,
            "implied_volatility": 0.30,
            **overrides,
        }
    )
    decisions: list[dict] = []

    result = run_sell_put_scan(
        symbols=["NVDA"],
        input_root=tmp_path,
        min_annualized_net_return=0.10,
        quote_freshness_now_utc=datetime(
            2026,
            4,
            1,
            15,
            0,
            tzinfo=timezone.utc,
        ),
        calculation_decision_sink_fn=decisions.extend,
        required_data_frames={"NVDA": pd.DataFrame([row])},
    )

    assert result.empty
    assert len(decisions) == 1
    reject = decisions[0]["opening_decision"]["rejects"][0]
    assert reject["reason"] == "contract_ineligible"
    assert reject["metric_value"]["reason_code"] == specific_reason


@pytest.mark.parametrize(
    "specific_reason",
    [
        "option_non_standard",
        "option_type_mismatch",
        "option_multiplier_conflict",
    ],
)
def test_candidate_scan_classifies_only_explicit_contract_evidence_conflicts(
    tmp_path,
    specific_reason: str,
) -> None:
    row = phase2_opening_row(
        {
            "symbol": "NVDA",
            "option_type": "put",
            "expiration": "2026-09-18",
            "contract_symbol": "US.NVDA260918P00100000",
            "currency": "USD",
            "dte": 43,
            "strike": 100.0,
            "spot": 110.0,
            "bid": 1.0,
            "ask": 1.01,
            "multiplier": 100,
        }
    )
    decisions: list[dict] = []

    result = run_candidate_scan(
        config=CandidateScanConfig(
            mode="put",
            symbols=["NVDA"],
            input_root=tmp_path,
            min_dte=1,
            max_dte=90,
            min_strike=None,
            max_strike=None,
            min_open_interest=None,
            min_volume=None,
            max_spread_ratio=None,
            min_annualized_net_return=None,
            min_net_income=0.0,
            required_data_frames={"NVDA": pd.DataFrame([row])},
        ),
        deps=CandidateScanDependencies(
            compute_metrics_fn=lambda _contract: None,
            build_row_fn=lambda *_args: None,
            metric_reject_reason_fn=lambda _contract: {
                "rule": specific_reason,
            },
        ),
        calculation_decision_sink_fn=decisions.extend,
    )

    assert result.empty
    reject = decisions[0]["opening_decision"]["rejects"][0]
    assert reject["reason"] == "contract_ineligible"
    assert reject["metric_value"]["reason_code"] == specific_reason
