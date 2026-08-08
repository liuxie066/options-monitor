from __future__ import annotations

from pathlib import Path

import pytest

from src.application.combo_yield_candidate_snapshot import (
    COMBO_YIELD_CANDIDATE_SNAPSHOT_FILE,
    ComboYieldCandidateSnapshotError,
    load_combo_yield_candidate_snapshot,
    seal_combo_yield_candidate_snapshot,
)


def _pair(**overrides) -> dict:
    row = {
        "candidate_pair_id": "combo_yield:NVDA:P:C",
        "symbol": "NVDA",
        "put_contract_symbol": "NVDA_P100",
        "call_contract_symbol": "NVDA_C125",
        "put_strike": 100.0,
        "call_strike": 125.0,
        "net_credit_retention": 0.80,
    }
    row.update(overrides)
    return row


def test_combo_yield_snapshot_seals_and_loads_with_pairs(tmp_path: Path) -> None:
    payload = seal_combo_yield_candidate_snapshot(
        base=tmp_path,
        run_id="run-1",
        account="lx",
        market="us",
        account_config_sha256="a" * 64,
        strategy_policy_sha256="b" * 64,
        ranked_pairs=[_pair()],
    )

    assert payload["schema_version"] == "combo_yield_candidate_snapshot.v1"
    assert payload["opening_status"] == "candidates_found"
    assert len(payload["ranked_pairs"]) == 1

    loaded = load_combo_yield_candidate_snapshot(
        base=tmp_path,
        run_id="run-1",
        account="lx",
    )
    assert loaded["content_sha256"] == payload["content_sha256"]


def test_combo_yield_snapshot_empty_result_seals_no_candidate(tmp_path: Path) -> None:
    payload = seal_combo_yield_candidate_snapshot(
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


def test_combo_yield_snapshot_explicit_data_unavailable(tmp_path: Path) -> None:
    payload = seal_combo_yield_candidate_snapshot(
        base=tmp_path,
        run_id="run-1",
        account="lx",
        market="us",
        account_config_sha256="a" * 64,
        strategy_policy_sha256="b" * 64,
        ranked_pairs=[],
        opening_status="data_unavailable",
    )
    assert payload["opening_status"] == "data_unavailable"


def test_combo_yield_snapshot_rejects_tampered_payload(tmp_path: Path) -> None:
    seal_combo_yield_candidate_snapshot(
        base=tmp_path,
        run_id="run-1",
        account="lx",
        market="us",
        account_config_sha256="a" * 64,
        strategy_policy_sha256="b" * 64,
        ranked_pairs=[_pair()],
    )
    path = tmp_path / "output_runs" / "run-1" / "accounts" / "lx" / "state" / COMBO_YIELD_CANDIDATE_SNAPSHOT_FILE
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace('"net_credit_retention": 0.8', '"net_credit_retention": 0.9'), encoding="utf-8")

    with pytest.raises(ComboYieldCandidateSnapshotError, match="content hash mismatch"):
        load_combo_yield_candidate_snapshot(
            base=tmp_path,
            run_id="run-1",
            account="lx",
        )


def test_combo_yield_snapshot_rejects_missing_pair_identity(tmp_path: Path) -> None:
    with pytest.raises(ComboYieldCandidateSnapshotError, match="identity"):
        seal_combo_yield_candidate_snapshot(
            base=tmp_path,
            run_id="run-1",
            account="lx",
            market="us",
            account_config_sha256="a" * 64,
            strategy_policy_sha256="b" * 64,
            ranked_pairs=[{"symbol": "NVDA"}],
        )
