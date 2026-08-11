from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

import pandas as pd

from conftest import phase2_opening_row


def test_sell_put_accepts_but_ignores_legacy_liquidity_gate_parameters() -> None:
    from src.application.scan_sell_put import run_sell_put_scan

    with TemporaryDirectory() as td:
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
            output=root / "sell_put_candidates.csv",
            min_open_interest=999_999,
            min_volume=999_999,
            min_net_income=0,
            min_annualized_net_return=0,
            quiet=True,
            quote_freshness_now_utc=datetime(2026, 4, 17, 2, 1, tzinfo=timezone.utc),
        )

        assert len(out) == 1


def test_sell_put_scan_emits_calculation_reject_without_csv_authority() -> None:
    from src.application.scan_sell_put import run_sell_put_scan

    with TemporaryDirectory() as td:
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
            output=None,
            min_net_income=0,
            min_annualized_net_return=0,
            quiet=True,
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
