from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from conftest import phase2_opening_row


def test_sell_put_accepts_but_ignores_legacy_liquidity_gate_parameters(tmp_path: Path) -> None:
    from src.application.scan_sell_put import run_sell_put_scan

    td = tmp_path
    root = Path(td)
    parsed_dir = root / "parsed"
    parsed_dir.mkdir(parents=True, exist_ok=True)

    pd.DataFrame(
        [
            phase2_opening_row({
                "symbol": "0700.HK",
                "market": "hk",
                "option_type": "put",
                "expiration": "2026-05-01",
                "dte": 14,
                "contract_symbol": "TSTP",
                "strike": 90.0,
                "spot": 100.0,
                "bid": 1.9,
                "ask": 2.1,
                "mid": 2.0,
                "quote_update_time": "2026-04-17 10:00:00",
                "snapshot_received_at_utc": "2026-04-17T02:00:00Z",
                "open_interest": 0,
                "volume": 0,
                "implied_volatility": 0.2,
                "delta": -0.2,
                "multiplier": 100,
                "currency": "HKD",
            })
        ]
    ).to_csv(parsed_dir / "0700.HK_required_data.csv", index=False)

    out = run_sell_put_scan(
            symbols=["0700.HK"],
            input_root=root,
        min_open_interest=999_999,
        min_volume=999_999,
        min_net_income=0,
        min_annualized_net_return=0,
        quote_freshness_now_utc=datetime(2026, 4, 17, 2, 1, tzinfo=timezone.utc),
    )

    assert len(out) == 1


def test_sell_put_scan_emits_calculation_reject_without_csv_authority(tmp_path: Path) -> None:
    from src.application.scan_sell_put import run_sell_put_scan

    td = tmp_path
    root = Path(td)
    parsed_dir = root / "parsed"
    parsed_dir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(
        [
            phase2_opening_row(
                {
                    "symbol": "NVDA",
                    "option_type": "put",
                    "expiration": "2026-05-01",
                    "dte": 14,
                    "contract_symbol": "BAD_MULTIPLIER",
                    "strike": 90.0,
                    "spot": 100.0,
                    "bid": 1.9,
                    "ask": 2.1,
                    "implied_volatility": 0.30,
                    "multiplier": 100,
                    "snapshot_multiplier": 50,
                    "currency": "USD",
                }
            )
        ]
    ).to_csv(parsed_dir / "NVDA_required_data.csv", index=False)
    captured: list[dict] = []

    out = run_sell_put_scan(
            symbols=["NVDA"],
            input_root=root,
        min_net_income=0,
        min_annualized_net_return=0,
        calculation_decision_sink_fn=captured.extend,
        quote_freshness_now_utc=datetime(2026, 4, 1, 15, 0, tzinfo=timezone.utc),
    )

    assert out.empty
    assert len(captured) == 1
    decision = captured[0]["opening_decision"]
    assert decision["accepted"] is False
    assert decision["rejects"][0]["reason"] == "input_invalid"
    assert decision["rejects"][0]["metric_value"]["reason_code"] == (
        "option_multiplier_conflict"
    )

    from src.application.candidate_scanning import evidence_summary_from_decisions

    evidence = evidence_summary_from_decisions(
        decisions=captured,
        accepted_count=0,
    )
    assert evidence["eligibility_unresolved_count"] == 1
    assert evidence["policy_rejected_count"] == 0


def test_us_sell_put_non_positive_net_premium_is_a_definitive_reject(tmp_path: Path) -> None:
    from domain.domain.engine import validate_candidate_decision_payload
    from src.application.candidate_scanning import (
        evidence_summary_from_decisions,
        project_evidence_scan_status,
    )
    from src.application.scan_sell_put import run_sell_put_scan

    td = tmp_path
    root = Path(td)
    parsed_dir = root / "parsed"
    parsed_dir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(
        [
            phase2_opening_row(
                {
                    "symbol": "NVDA",
                    "option_type": "put",
                    "expiration": "2026-05-01",
                    "dte": 14,
                    "contract_symbol": "TINY_PREMIUM_PUT",
                    "strike": 90.0,
                    "spot": 100.0,
                    "bid": 0.01,
                    "ask": 0.01,
                    "implied_volatility": 0.30,
                    "multiplier": 100,
                    "currency": "USD",
                }
            )
        ]
    ).to_csv(parsed_dir / "NVDA_required_data.csv", index=False)
    captured: list[dict] = []

    out = run_sell_put_scan(
        symbols=["NVDA"],
        input_root=root,
        min_net_income=0,
        min_annualized_net_return=0,
        calculation_decision_sink_fn=captured.extend,
        quote_freshness_now_utc=datetime(2026, 4, 1, 15, 0, tzinfo=timezone.utc),
    )

    assert out.empty
    assert len(captured) == 1
    decision = captured[0]["opening_decision"]
    assert validate_candidate_decision_payload(decision) == decision
    assert decision["rejects"][0]["reason"] == "policy_rejected"
    assert decision["rejects"][0]["metric_value"]["reason_code"] == (
        "net_premium_non_positive"
    )
    evidence = evidence_summary_from_decisions(
        decisions=captured,
        accepted_count=0,
    )
    assert evidence["policy_rejected_count"] == 1
    assert evidence["eligibility_unresolved_count"] == 0
    assert project_evidence_scan_status(
        evidence=evidence,
        candidate_count=0,
    ) == ("completed", "no_candidate")


def test_hk_covered_call_non_positive_net_premium_is_a_definitive_reject(tmp_path: Path) -> None:
    from domain.domain.engine import validate_candidate_decision_payload
    from src.application.candidate_scanning import (
        evidence_summary_from_decisions,
        project_evidence_scan_status,
    )
    from src.application.scan_sell_call import run_sell_call_scan

    td = tmp_path
    root = Path(td)
    parsed_dir = root / "parsed"
    parsed_dir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(
        [
            phase2_opening_row(
                {
                    "symbol": "0700.HK",
                    "market": "HK",
                    "option_type": "call",
                    "expiration": "2026-05-01",
                    "dte": 14,
                    "contract_symbol": "TINY_PREMIUM_CALL",
                    "strike": 110.0,
                    "spot": 100.0,
                    "bid": 0.01,
                    "ask": 0.01,
                    "implied_volatility": 0.30,
                    "multiplier": 100,
                    "currency": "HKD",
                }
            )
        ]
    ).to_csv(parsed_dir / "0700.HK_required_data.csv", index=False)
    captured: list[dict] = []

    out = run_sell_call_scan(
        symbols=["0700.HK"],
        input_root=root,
        avg_cost=90.0,
        shares=100,
        min_net_income=0,
        min_annualized_net_return=0,
        calculation_decision_sink_fn=captured.extend,
        quote_freshness_now_utc=datetime(2026, 4, 1, 15, 0, tzinfo=timezone.utc),
    )

    assert out.empty
    assert len(captured) == 1
    decision = captured[0]["opening_decision"]
    assert validate_candidate_decision_payload(decision) == decision
    assert decision["rejects"][0]["reason"] == "policy_rejected"
    assert decision["rejects"][0]["metric_value"]["reason_code"] == (
        "net_premium_non_positive"
    )
    evidence = evidence_summary_from_decisions(
        decisions=captured,
        accepted_count=0,
    )
    assert evidence["policy_rejected_count"] == 1
    assert evidence["eligibility_unresolved_count"] == 0
    assert project_evidence_scan_status(
        evidence=evidence,
        candidate_count=0,
    ) == ("completed", "no_candidate")


def test_non_positive_net_premium_requires_explicit_ready_opening_status() -> None:
    from src.application.candidate_scanning import (
        CandidateScanConfig,
        _calculation_decision_record,
        evidence_summary_from_decisions,
    )
    from src.application.candidate_models import CandidateContractInput

    contract = CandidateContractInput.from_row(
        pd.Series(
            phase2_opening_row(
                {
                    "symbol": "NVDA",
                    "option_type": "put",
                    "expiration": "2026-05-01",
                    "contract_symbol": "MISSING_OPENING_STATUS",
                    "strike": 90.0,
                    "spot": 100.0,
                    "bid": 0.01,
                    "ask": 0.01,
                    "implied_volatility": 0.30,
                    "multiplier": 100,
                    "currency": "USD",
                    "opening_contract_status": "",
                }
            )
        ),
        mode="put",
    )
    decision = _calculation_decision_record(
        contract=contract,
        config=CandidateScanConfig(
            mode="put",
            symbols=["NVDA"],
            input_root=Path("."),
            min_dte=0,
            max_dte=0,
            min_strike=None,
            max_strike=None,
            min_open_interest=None,
            min_volume=None,
            max_spread_ratio=None,
            min_annualized_net_return=None,
            min_net_income=0,
        ),
        reason={"rule": "net_premium_non_positive"},
    )

    reject = decision["opening_decision"]["rejects"][0]
    assert reject["reason"] == "input_invalid"
    assert reject["metric_value"]["reason_code"] == "net_premium_non_positive"
    evidence = evidence_summary_from_decisions(
        decisions=[decision],
        accepted_count=0,
    )
    assert evidence["eligibility_unresolved_count"] == 1
    assert evidence["policy_rejected_count"] == 0


def test_zero_bid_only_scope_projects_no_candidate_not_partial_data() -> None:
    from src.application.candidate_scanning import evidence_summary_from_decisions
    from src.application.sell_put_steps import _evidence_scan_status

    decisions = [
        {
            "opening_decision": {
                "accepted": False,
                "rejects": [
                    {
                        "reason": "contract_ineligible",
                        "metric_value": {
                            "reason_codes": ["option_no_current_bid"],
                            "status": "ineligible",
                        },
                    }
                ],
            }
        }
    ]
    evidence = evidence_summary_from_decisions(
        decisions=decisions,
        accepted_count=0,
    )

    assert evidence["evidence_unavailable_count"] == 0
    assert evidence["contract_ineligible_count"] == 1
    assert _evidence_scan_status(
        evidence=evidence,
        candidate_count=0,
    ) == ("completed", "no_candidate")


def test_input_invalid_only_scope_projects_data_unavailable_not_no_candidate() -> None:
    from src.application.candidate_scanning import evidence_summary_from_decisions
    from src.application.sell_put_steps import _evidence_scan_status

    decisions = [
        {
            "opening_decision": {
                "accepted": False,
                "rejects": [
                    {
                        "reason": "input_invalid",
                        "metric_value": {
                            "reason_code": "term_matched_rv_unavailable",
                        },
                    }
                ],
            }
        }
    ]
    evidence = evidence_summary_from_decisions(
        decisions=decisions,
        accepted_count=0,
    )

    assert evidence["evidence_unavailable_count"] == 1
    assert evidence["policy_rejected_count"] == 0
    assert evidence["unavailable_by_reason"] == {
        "term_matched_rv_unavailable": 1,
    }
    assert _evidence_scan_status(
        evidence=evidence,
        candidate_count=0,
    ) == ("unavailable", "data_unavailable")


def test_mixed_input_invalid_scope_projects_partial_data_not_no_candidate() -> None:
    from src.application.candidate_scanning import evidence_summary_from_decisions
    from src.application.sell_call_steps import _evidence_scan_status

    decisions = [
        {
            "opening_decision": {
                "accepted": False,
                "rejects": [{"reason": "input_missing"}],
            }
        },
        {
            "opening_decision": {
                "accepted": False,
                "rejects": [{"reason": "contract_ineligible"}],
            }
        },
    ]
    evidence = evidence_summary_from_decisions(
        decisions=decisions,
        accepted_count=0,
    )

    assert evidence["evidence_unavailable_count"] == 1
    assert evidence["contract_ineligible_count"] == 1
    assert _evidence_scan_status(
        evidence=evidence,
        candidate_count=0,
    ) == ("completed", "partial_data")
