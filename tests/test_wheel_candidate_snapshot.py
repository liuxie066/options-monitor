from __future__ import annotations

from pathlib import Path

import pytest

from src.application.wheel.candidate_snapshot import (
    WheelCandidateSnapshotError,
    load_wheel_candidate_snapshot,
    seal_wheel_candidate_snapshot,
)


def _dependencies() -> list[dict]:
    return [
        {"kind": kind, "relpath": None, "sha256": char * 64}
        for kind, char in (
            ("required_data", "a"),
            ("portfolio", "b"),
            ("ledger", "c"),
            ("fx", "d"),
            ("earnings_rv", "e"),
        )
    ]


def _candidate() -> dict:
    return {
        "candidate_id": "wheel-candidate-1",
        "final_candidate_id": "wheel-candidate-1",
        "account": "lx",
        "symbol": "NVDA",
        "stock_lot_id": "stock-1",
        "contract_symbol": "NVDA-CALL-110",
        "multiplier": 100,
        "granted_contracts": 1,
    }


def _batch(*, final: bool = True) -> dict:
    candidate = _candidate()
    return {
        "account": "lx",
        "symbol": "NVDA",
        "stock_lot_id": "stock-1",
        "batch_generation_hash": "1" * 64,
        "projection_hash": "2" * 64,
        "reason_codes": [],
        "raw_candidates": [{key: value for key, value in candidate.items() if key != "final_candidate_id"}],
        "requested_contracts": 1,
        "requested_shares": 100,
        "granted_contracts": 1 if final else 0,
        "granted_shares": 100 if final else 0,
        "capacity_before": 100,
        "capacity_after": 0 if final else 100,
        "final_candidate": candidate if final else None,
    }


def test_wheel_candidate_snapshot_seals_one_account_run_owner(tmp_path: Path) -> None:
    payload = seal_wheel_candidate_snapshot(
        base=tmp_path,
        run_id="run-1",
        account="lx",
        market="us",
        account_config_sha256="a" * 64,
        strategy_policy_sha256="b" * 64,
        dependencies=_dependencies(),
        scope_results=[
            {
                "symbol": "NVDA",
                "status": "completed",
                "reason_code": "candidates_found",
                "candidate_count": 1,
            }
        ],
        batches=[_batch()],
    )

    assert payload["candidate_owner"] == "wheel"
    assert payload["opening_status"] == "candidates_found"
    assert load_wheel_candidate_snapshot(base=tmp_path, run_id="run-1", account="lx") == payload


def test_wheel_candidate_snapshot_rejects_final_candidate_not_from_raw_top(tmp_path: Path) -> None:
    batch = _batch()
    batch["final_candidate"] = {**_candidate(), "candidate_id": "other", "final_candidate_id": "other"}
    with pytest.raises(WheelCandidateSnapshotError, match="allocation"):
        seal_wheel_candidate_snapshot(
            base=tmp_path,
            run_id="run-1",
            account="lx",
            market="us",
            account_config_sha256="a" * 64,
            strategy_policy_sha256="b" * 64,
            dependencies=_dependencies(),
            scope_results=[{"symbol": "NVDA", "status": "completed", "candidate_count": 1}],
            batches=[batch],
        )
