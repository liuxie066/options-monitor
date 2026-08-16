from __future__ import annotations

import pandas as pd
import pytest

from domain.domain.engine.candidate_engine import (
    REJECT_RISK_EARNINGS_UNAVAILABLE,
)
from src.application.candidate_scanning import (
    _load_required_data_rows,
    evidence_summary_from_decisions,
)
from src.application.sell_call_steps import _evidence_scan_status as call_status
from src.application.sell_put_steps import _evidence_scan_status as put_status


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
