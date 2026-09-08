from __future__ import annotations

import json
from pathlib import Path

import pytest

from domain.domain.decision_state_fingerprint import canonical_sha256
from src.application.wheel.candidate_snapshot import (
    WHEEL_CANDIDATE_SNAPSHOT_FILE_V1,
    WHEEL_CANDIDATE_SNAPSHOT_SCHEMA_V1,
    WHEEL_CANDIDATE_SNAPSHOT_SCHEMA_V2,
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
        "wheel_branch_id": "branch-call-1",
        "direction": "call",
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
        "wheel_branch_id": "branch-call-1",
        "direction": "call",
        "branch_generation_hash": "1" * 64,
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
                "direction": "call",
                "status": "completed",
                "reason_code": "candidates_found",
                "candidate_count": 1,
            }
        ],
        batches=[_batch()],
    )

    assert payload["candidate_owner"] == "wheel"
    assert payload["schema_version"] == WHEEL_CANDIDATE_SNAPSHOT_SCHEMA_V2
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
            scope_results=[{"symbol": "NVDA", "direction": "call", "status": "completed", "candidate_count": 1}],
            batches=[batch],
        )


def test_wheel_candidate_snapshot_rejects_positive_grant_without_final_candidate(
    tmp_path: Path,
) -> None:
    batch = _batch(final=False)
    batch["granted_contracts"] = 1

    with pytest.raises(WheelCandidateSnapshotError, match="grant requires final candidate"):
        seal_wheel_candidate_snapshot(
            base=tmp_path,
            run_id="run-1",
            account="lx",
            market="us",
            account_config_sha256="a" * 64,
            strategy_policy_sha256="b" * 64,
            dependencies=_dependencies(),
            scope_results=[{"symbol": "NVDA", "direction": "call", "status": "completed", "candidate_count": 1}],
            batches=[batch],
        )


def test_wheel_candidate_snapshot_accepts_rejected_candidate_with_zero_grant(
    tmp_path: Path,
) -> None:
    batch = _batch(final=False)
    batch["reason_code"] = "wheel_capacity_grant_candidate_rejected"

    payload = seal_wheel_candidate_snapshot(
        base=tmp_path,
        run_id="run-1",
        account="lx",
        market="us",
        account_config_sha256="a" * 64,
        strategy_policy_sha256="b" * 64,
        dependencies=_dependencies(),
        scope_results=[{"symbol": "NVDA", "direction": "call", "status": "completed", "candidate_count": 1}],
        batches=[batch],
    )

    assert payload["batches"][0]["granted_contracts"] == 0
    assert payload["batches"][0]["final_candidate"] is None


def test_wheel_candidate_snapshot_allows_same_symbol_across_directions(
    tmp_path: Path,
) -> None:
    call_batch = _batch(final=False)
    put_batch = {
        **_batch(final=False),
        "stock_lot_id": None,
        "wheel_branch_id": "branch-put-1",
        "direction": "put",
    }
    payload = seal_wheel_candidate_snapshot(
        base=tmp_path,
        run_id="run-1",
        account="lx",
        market="us",
        account_config_sha256="a" * 64,
        strategy_policy_sha256="b" * 64,
        dependencies=_dependencies(),
        scope_results=[
            {"symbol": "NVDA", "direction": "call", "status": "completed", "candidate_count": 0},
            {"symbol": "NVDA", "direction": "put", "status": "completed", "candidate_count": 0},
        ],
        batches=[call_batch, put_batch],
    )

    assert [(row["symbol"], row["direction"]) for row in payload["scope_results"]] == [
        ("NVDA", "call"),
        ("NVDA", "put"),
    ]


def test_wheel_candidate_snapshot_loader_adapts_legacy_file_location(
    tmp_path: Path,
) -> None:
    payload = seal_wheel_candidate_snapshot(
        base=tmp_path,
        run_id="run-v2",
        account="lx",
        market="us",
        account_config_sha256="a" * 64,
        strategy_policy_sha256="b" * 64,
        dependencies=_dependencies(),
        scope_results=[
            {"symbol": "NVDA", "direction": "call", "status": "completed", "candidate_count": 0}
        ],
        batches=[_batch(final=False)],
    )
    legacy = dict(payload)
    legacy["schema_version"] = WHEEL_CANDIDATE_SNAPSHOT_SCHEMA_V1
    legacy["scope_results"] = [
        {key: value for key, value in row.items() if key != "direction"}
        for row in legacy["scope_results"]
    ]
    legacy["batches"] = [
        {
            **{
                key: value
                for key, value in row.items()
                if key not in {"direction", "wheel_branch_id", "branch_generation_hash"}
            },
            "batch_generation_hash": row["branch_generation_hash"],
        }
        for row in legacy["batches"]
    ]
    binding = {
        "run_id": legacy["run_id"],
        "account": legacy["account"],
        "account_config_sha256": legacy["account_config_sha256"],
        "strategy_policy_sha256": legacy["strategy_policy_sha256"],
        "required_data_manifest_sha256": legacy["required_data_manifest_sha256"],
        "scope_results": legacy["scope_results"],
        "batches": legacy["batches"],
        "capacity_allocations": legacy["capacity_allocations"],
    }
    legacy["snapshot_hash"] = canonical_sha256(binding)
    legacy["content_sha256"] = canonical_sha256(
        {key: value for key, value in legacy.items() if key != "content_sha256"}
    )
    target = tmp_path / "output_runs" / "run-v1" / "accounts" / "lx" / "state"
    target.mkdir(parents=True)
    legacy["run_id"] = "run-v1"
    binding["run_id"] = "run-v1"
    legacy["snapshot_hash"] = canonical_sha256(binding)
    legacy["content_sha256"] = canonical_sha256(
        {key: value for key, value in legacy.items() if key != "content_sha256"}
    )
    (target / WHEEL_CANDIDATE_SNAPSHOT_FILE_V1).write_text(
        json.dumps(legacy),
        encoding="utf-8",
    )

    loaded = load_wheel_candidate_snapshot(
        base=tmp_path,
        run_id="run-v1",
        account="lx",
    )
    assert loaded["schema_version"] == WHEEL_CANDIDATE_SNAPSHOT_SCHEMA_V1


def test_wheel_candidate_snapshot_rejects_mixed_v1_v2_files(tmp_path: Path) -> None:
    seal_wheel_candidate_snapshot(
        base=tmp_path,
        run_id="run-mixed",
        account="lx",
        market="us",
        account_config_sha256="a" * 64,
        strategy_policy_sha256="b" * 64,
        dependencies=_dependencies(),
        scope_results=[
            {
                "symbol": "NVDA",
                "direction": "call",
                "status": "completed",
                "candidate_count": 0,
            }
        ],
        batches=[_batch(final=False)],
        capacity_allocations=[],
        sealed_at="2026-09-09T00:00:00Z",
    )
    target = tmp_path / "output_runs" / "run-mixed" / "accounts" / "lx" / "state"
    (target / WHEEL_CANDIDATE_SNAPSHOT_FILE_V1).write_text("{}", encoding="utf-8")

    with pytest.raises(WheelCandidateSnapshotError, match="artifact_version_mismatch"):
        load_wheel_candidate_snapshot(
            base=tmp_path,
            run_id="run-mixed",
            account="lx",
        )


def test_wheel_candidate_snapshot_v2_binds_put_cash_allocation(
    tmp_path: Path,
) -> None:
    candidate = {
        "candidate_id": "wheel-put-candidate-1",
        "final_candidate_id": "wheel-put-candidate-1",
        "account": "lx",
        "symbol": "NVDA",
        "wheel_branch_id": "branch-put-1",
        "direction": "put",
        "contract_symbol": "NVDA-PUT-99",
        "multiplier": 100,
        "granted_contracts": 1,
        "capacity_identity_hash": "3" * 64,
        "allocation_input_hash": "4" * 64,
        "cash_reservation_amount": 9_900,
        "cash_reservation_currency": "USD",
    }
    allocation = {
        "claim_id": "wheel:put:branch-put-1",
        "strategy_family": "wheel",
        "wheel_branch_id": "branch-put-1",
        "direction": "put",
        "capacity_identity_hash": "3" * 64,
        "allocation_input_hash": "4" * 64,
        "granted_contracts": 1,
        "cash_reservation_amount": 9_900,
        "cash_reservation_currency": "USD",
    }
    payload = seal_wheel_candidate_snapshot(
        base=tmp_path,
        run_id="run-put",
        account="lx",
        market="us",
        account_config_sha256="a" * 64,
        strategy_policy_sha256="b" * 64,
        dependencies=_dependencies(),
        scope_results=[
            {
                "symbol": "NVDA",
                "direction": "put",
                "status": "completed",
                "candidate_count": 1,
            }
        ],
        batches=[
            {
                "account": "lx",
                "symbol": "NVDA",
                "wheel_branch_id": "branch-put-1",
                "direction": "put",
                "branch_generation_hash": "1" * 64,
                "projection_hash": "2" * 64,
                "raw_candidates": [
                    {key: value for key, value in candidate.items() if key != "final_candidate_id"}
                ],
                "granted_contracts": 1,
                "final_candidate": candidate,
            }
        ],
        capacity_allocations=[allocation],
    )

    assert payload["batches"][0]["final_candidate"]["cash_reservation_amount"] == 9_900


def test_wheel_candidate_snapshot_v2_preserves_ordinary_cc_allocation(
    tmp_path: Path,
) -> None:
    ordinary = {
        "claim_id": "covered_call:NVDA",
        "strategy_family": "covered_call",
        "account": "lx",
        "symbol": "NVDA",
        "granted_contracts": 1,
    }
    payload = seal_wheel_candidate_snapshot(
        base=tmp_path,
        run_id="run-ordinary-cc",
        account="lx",
        market="us",
        account_config_sha256="a" * 64,
        strategy_policy_sha256="b" * 64,
        dependencies=_dependencies(),
        scope_results=[
            {
                "symbol": "NVDA",
                "direction": "call",
                "status": "completed",
                "candidate_count": 0,
            }
        ],
        batches=[_batch(final=False)],
        capacity_allocations=[ordinary],
    )

    assert payload["capacity_allocations"] == [ordinary]
