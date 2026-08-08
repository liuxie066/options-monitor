from __future__ import annotations

from pathlib import Path

import pytest

from src.application.cc_lp_candidate_snapshot import (
    CC_LP_CANDIDATE_SNAPSHOT_FILE,
    CcLpCandidateSnapshotError,
    load_cc_lp_candidate_snapshot,
    seal_cc_lp_candidate_snapshot,
)


def _pair(**overrides) -> dict:
    row = {
        "candidate_pair_id": "cc_lp:NVDA:C:P",
        "symbol": "NVDA",
        "variant": "cc_lp",
        "call_contract_symbol": "NVDA_C125",
        "put_contract_symbol": "NVDA_P90",
        "call_strike": 125.0,
        "put_strike": 90.0,
        "net_credit_retention": 0.35,
    }
    row.update(overrides)
    return row


def test_cc_lp_snapshot_seals_and_loads_with_pairs(tmp_path: Path) -> None:
    payload = seal_cc_lp_candidate_snapshot(
        base=tmp_path,
        run_id="run-1",
        account="lx",
        market="us",
        account_config_sha256="a" * 64,
        strategy_policy_sha256="b" * 64,
        ranked_pairs=[_pair()],
    )

    assert payload["schema_version"] == "cc_lp_candidate_snapshot.v1"
    assert payload["opening_status"] == "candidates_found"
    assert len(payload["ranked_pairs"]) == 1

    loaded = load_cc_lp_candidate_snapshot(
        base=tmp_path,
        run_id="run-1",
        account="lx",
    )
    assert loaded["content_sha256"] == payload["content_sha256"]


def test_cc_lp_snapshot_empty_result_seals_no_candidate(tmp_path: Path) -> None:
    payload = seal_cc_lp_candidate_snapshot(
        base=tmp_path,
        run_id="run-1",
        account="lx",
        market="hk",
        account_config_sha256="a" * 64,
        strategy_policy_sha256="b" * 64,
        ranked_pairs=[],
    )

    assert payload["opening_status"] == "no_candidate"
    assert payload["ranked_pairs"] == []


def test_cc_lp_snapshot_rejects_missing_pair_id(tmp_path: Path) -> None:
    with pytest.raises(CcLpCandidateSnapshotError):
        seal_cc_lp_candidate_snapshot(
            base=tmp_path,
            run_id="run-1",
            account="lx",
            market="us",
            account_config_sha256="a" * 64,
            strategy_policy_sha256="b" * 64,
            ranked_pairs=[{"symbol": "NVDA"}],
        )


def test_cc_lp_snapshot_explicit_not_applicable(tmp_path: Path) -> None:
    payload = seal_cc_lp_candidate_snapshot(
        base=tmp_path,
        run_id="run-1",
        account="lx",
        market="us",
        account_config_sha256="a" * 64,
        strategy_policy_sha256="b" * 64,
        ranked_pairs=[],
        opening_status="not_applicable",
    )
    assert payload["opening_status"] == "not_applicable"
