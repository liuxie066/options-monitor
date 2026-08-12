from __future__ import annotations

from pathlib import Path

import pytest

from src.application.cc_lp_candidate_snapshot import (
    CcLpCandidateSnapshotError,
    load_cc_lp_candidate_snapshot,
    project_cc_lp_candidates,
    seal_cc_lp_candidate_snapshot,
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


def _scope(*, status: str = "completed", reason: str | None = None) -> dict:
    return {
        "symbol": "NVDA",
        "strategy_mode": "combo_yield",
        "variant": "cc_lp",
        "status": status,
        "reason": reason,
        "quote_snapshot_id": "quote-1",
        "quote_receipt_relpath": "quotes/quote-1/receipt.json",
    }


def _pair(**overrides) -> dict:
    row = {
        "candidate_pair_id": "cc_lp:NVDA:NVDA_C125:NVDA_P90",
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


def _seal(tmp_path: Path, *, pairs: bool = True, status: str = "completed") -> dict:
    return seal_cc_lp_candidate_snapshot(
        base=tmp_path,
        run_id="run-1",
        account="lx",
        market="us",
        account_config_sha256="a" * 64,
        strategy_policy_sha256="b" * 64,
        dependencies=_dependencies(),
        scan_statuses=[
            _scope(
                status=status,
                reason="no_covered_stock" if status == "not_applicable" else None,
            )
        ],
        ranked_pairs=[_pair()] if pairs else [],
    )


def test_cc_lp_snapshot_seals_and_loads_with_pairs(tmp_path: Path) -> None:
    payload = _seal(tmp_path)

    assert payload["schema_version"] == "cc_lp_candidate_snapshot.v2"
    assert payload["candidate_owner"] == "cc_lp"
    assert payload["opening_status"] == "candidates_found"
    assert len(payload["ranked_pairs"]) == 1

    loaded = load_cc_lp_candidate_snapshot(
        base=tmp_path,
        run_id="run-1",
        account="lx",
    )
    assert loaded["content_sha256"] == payload["content_sha256"]
    assert project_cc_lp_candidates(payload) == payload["ranked_pairs"]


def test_cc_lp_snapshot_empty_result_seals_no_candidate(tmp_path: Path) -> None:
    payload = _seal(tmp_path, pairs=False)

    assert payload["opening_status"] == "no_candidate"
    assert payload["ranked_pairs"] == []


def test_cc_lp_snapshot_rejects_missing_pair_id(tmp_path: Path) -> None:
    with pytest.raises(CcLpCandidateSnapshotError, match="identity"):
        seal_cc_lp_candidate_snapshot(
            base=tmp_path,
            run_id="run-1",
            account="lx",
            market="us",
            account_config_sha256="a" * 64,
            strategy_policy_sha256="b" * 64,
            dependencies=_dependencies(),
            scan_statuses=[_scope()],
            ranked_pairs=[{"symbol": "NVDA"}],
        )


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"symbol": "AAPL"}, "escapes snapshot scope"),
        ({"run_id": "run-2"}, "run identity mismatch"),
        ({"account": "sy"}, "account identity mismatch"),
        ({"candidate_pair_id": "cc_lp:NVDA:WRONG:PAIR"}, "pair identity mismatch"),
    ],
)
def test_cc_lp_snapshot_binds_ranked_pair_identity_to_scope(
    tmp_path: Path,
    overrides: dict,
    message: str,
) -> None:
    with pytest.raises(CcLpCandidateSnapshotError, match=message):
        seal_cc_lp_candidate_snapshot(
            base=tmp_path,
            run_id="run-1",
            account="lx",
            market="us",
            account_config_sha256="a" * 64,
            strategy_policy_sha256="b" * 64,
            dependencies=_dependencies(),
            scan_statuses=[_scope()],
            ranked_pairs=[_pair(**overrides)],
        )


def test_cc_lp_snapshot_derives_not_applicable_from_scope(tmp_path: Path) -> None:
    payload = _seal(tmp_path, pairs=False, status="not_applicable")
    assert payload["opening_status"] == "not_applicable"
