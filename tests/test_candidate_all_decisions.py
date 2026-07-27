from __future__ import annotations

from pathlib import Path

import pandas as pd

from domain.domain.engine import (
    REPLACEMENT_CAPACITY_DEFERRED,
    REPLACEMENT_REJECTED_INVARIANT,
)
from src.application.scan_sell_call import run_sell_call_scan
from src.application.scan_sell_put import run_sell_put_scan


QUOTE_SNAPSHOT_ID = "a" * 64


def _write_required_data(
    root: Path,
    *,
    option_type: str,
    rows: list[dict[str, object]],
) -> None:
    parsed = root / "parsed"
    parsed.mkdir(parents=True)
    defaults: dict[str, object] = {
        "symbol": "NVDA",
        "option_type": option_type,
        "expiration": "2026-08-21",
        "contract_symbol": f"NVDA-20260821-{option_type.upper()}-100",
        "currency": "USD",
        "dte": 25,
        "strike": 100 if option_type == "put" else 120,
        "spot": 110,
        "bid": 1.0,
        "ask": 1.2,
        "last_price": 1.1,
        "mid": 1.1,
        "open_interest": 100,
        "volume": 50,
        "implied_volatility": 0.3,
        "delta": -0.2 if option_type == "put" else 0.2,
        "multiplier": 100,
    }
    pd.DataFrame([{**defaults, **row} for row in rows]).to_csv(
        parsed / "NVDA_required_data.csv",
        index=False,
    )


def test_call_capacity_only_reject_is_retained_for_replacement(
    tmp_path: Path,
) -> None:
    input_root = tmp_path / "input"
    _write_required_data(input_root, option_type="call", rows=[{}])
    captured: list[dict[str, object]] = []

    out = run_sell_call_scan(
        symbols=["NVDA"],
        input_root=input_root,
        output=tmp_path / "call.csv",
        avg_cost=90,
        shares=100,
        shares_locked=100,
        shares_available_for_cover=0,
        min_dte=7,
        max_dte=45,
        min_annualized_net_return=0,
        min_net_income=0,
        min_open_interest=0,
        min_volume=0,
        max_spread_ratio=1,
        event_risk_cfg={"enabled": False, "mode": "warn"},
        risk_policy_version="candidate-risk.v1",
        quote_snapshot_id=QUOTE_SNAPSHOT_ID,
        all_decisions_sink_fn=captured.extend,
        quiet=True,
    )

    assert out.empty
    assert len(captured) == 1
    opening = captured[0]["opening_decision"]
    invariant = captured[0]["invariant_decision"]
    replacement = captured[0]["replacement_candidate_decision"]
    assert opening["accepted"] is False
    assert [item["reason"] for item in opening["rejects"]] == [
        "hard_capacity_call"
    ]
    assert invariant["accepted"] is True
    assert replacement["replacement_eligibility"] == REPLACEMENT_CAPACITY_DEFERRED
    assert opening["normalized_input_hash"] == invariant["normalized_input_hash"]
    assert opening["risk_policy_hash"] == invariant["risk_policy_hash"]


def test_capacity_short_circuit_cannot_hide_invariant_return_reject(
    tmp_path: Path,
) -> None:
    input_root = tmp_path / "input"
    _write_required_data(input_root, option_type="call", rows=[{}])
    captured: list[dict[str, object]] = []

    run_sell_call_scan(
        symbols=["NVDA"],
        input_root=input_root,
        output=tmp_path / "call.csv",
        avg_cost=90,
        shares=100,
        shares_locked=100,
        shares_available_for_cover=0,
        min_dte=7,
        max_dte=45,
        min_annualized_net_return=0.99,
        min_net_income=0,
        min_open_interest=0,
        min_volume=0,
        max_spread_ratio=1,
        event_risk_cfg={"enabled": False, "mode": "warn"},
        risk_policy_version="candidate-risk.v1",
        quote_snapshot_id=QUOTE_SNAPSHOT_ID,
        all_decisions_sink_fn=captured.extend,
        quiet=True,
    )

    assert len(captured) == 1
    opening = captured[0]["opening_decision"]
    invariant = captured[0]["invariant_decision"]
    replacement = captured[0]["replacement_candidate_decision"]
    assert [item["reason"] for item in opening["rejects"]] == [
        "hard_capacity_call"
    ]
    assert [item["reason"] for item in invariant["rejects"]] == [
        "return_annualized"
    ]
    assert replacement["replacement_eligibility"] == REPLACEMENT_REJECTED_INVARIANT


def test_all_decisions_sidecar_does_not_change_opening_csv_or_reject_log(
    tmp_path: Path,
) -> None:
    input_root = tmp_path / "input"
    _write_required_data(
        input_root,
        option_type="put",
        rows=[
            {"contract_symbol": "ACCEPT", "strike": 100},
            {
                "contract_symbol": "REJECT_LIQUIDITY",
                "strike": 95,
                "open_interest": 0,
            },
        ],
    )
    baseline_output = tmp_path / "baseline.csv"
    sidecar_output = tmp_path / "sidecar.csv"
    captured: list[dict[str, object]] = []

    common = {
        "symbols": ["NVDA"],
        "input_root": input_root,
        "min_dte": 7,
        "max_dte": 45,
        "min_annualized_net_return": 0,
        "min_net_income": 0,
        "min_open_interest": 10,
        "min_volume": 0,
        "max_spread_ratio": 1,
        "event_risk_cfg": {"enabled": False, "mode": "warn"},
        "quiet": True,
    }
    run_sell_put_scan(output=baseline_output, **common)
    run_sell_put_scan(
        output=sidecar_output,
        risk_policy_version="candidate-risk.v1",
        quote_snapshot_id=QUOTE_SNAPSHOT_ID,
        all_decisions_sink_fn=captured.extend,
        **common,
    )

    assert baseline_output.read_bytes() == sidecar_output.read_bytes()
    assert (
        baseline_output.with_name("baseline_reject_log.csv").read_text(
            encoding="utf-8"
        ).replace("baseline", "sidecar")
        == sidecar_output.with_name("sidecar_reject_log.csv").read_text(
            encoding="utf-8"
        )
    )
    assert len(captured) == 2
