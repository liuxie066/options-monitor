from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from domain.domain.decision_state_fingerprint import canonical_sha256
from domain.domain.engine import build_candidate_decision
from src.application.combo_yield_candidate_snapshot import (
    COMBO_YIELD_CANDIDATE_SNAPSHOT_FILE,
    ComboYieldCandidateSnapshotError,
    load_combo_yield_candidate_snapshot,
    project_combo_yield_candidates,
    project_combo_yield_funding_put_decisions,
    project_combo_yield_pair_diagnostics,
    project_combo_yield_rank_evidence,
    project_combo_yield_rejections,
    seal_combo_yield_candidate_snapshot,
    validate_combo_yield_candidate_snapshot,
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
        "variant": "sp_lc",
        "status": status,
        "reason": reason,
        "quote_snapshot_id": "quote-1",
        "quote_receipt_relpath": "quotes/quote-1/receipt.json",
    }


def _pair(**overrides) -> dict:
    row = {
        "candidate_pair_id": "combo_yield:NVDA:NVDA_P100:NVDA_C125",
        "symbol": "NVDA",
        "put_contract_symbol": "NVDA_P100",
        "call_contract_symbol": "NVDA_C125",
        "put_strike": 100.0,
        "call_strike": 125.0,
        "net_credit_retention": 0.80,
    }
    row.update(overrides)
    return row


def _pair_evaluation() -> dict:
    return {
        **_pair(),
        "diagnostic_scope": "pair",
        "diagnostic_stage": "pair_filter",
        "accepted": True,
        "reject_reasons": "",
    }


def _rank_record() -> dict:
    return {
        **_pair(),
        "baseline_rank": 1,
        "shadow_rank": 1,
        "baseline_selected": True,
        "shadow_selected": True,
        "rank_changed": False,
    }


def _funding_put_decision() -> dict:
    normalized = {
        "symbol": "NVDA",
        "contract_symbol": "NVDA_P100",
        "expiration": "2026-08-21",
        "strike": 100.0,
    }
    return {
        "normalized_input": normalized,
        "opening_decision": build_candidate_decision(
            mode="put",
            symbol="NVDA",
            contract_symbol="NVDA_P100",
            accepted=True,
            rejects=[],
            normalized_input=normalized,
        ),
    }


def _seal(tmp_path: Path, *, pairs: bool = True, status: str = "completed") -> dict:
    return seal_combo_yield_candidate_snapshot(
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
                reason="source_unavailable" if status != "completed" else None,
            )
        ],
        funding_put_decisions=[],
        pair_evaluations=[_pair_evaluation()] if pairs else [],
        rank_records=[_rank_record()] if pairs else [],
        ranked_pairs=[_pair()] if pairs else [],
    )


def test_combo_yield_snapshot_seals_full_evidence_and_loads(tmp_path: Path) -> None:
    payload = _seal(tmp_path)

    assert payload["schema_version"] == "combo_yield_candidate_snapshot.v2"
    assert payload["candidate_owner"] == "sp_lc"
    assert payload["opening_status"] == "candidates_found"
    assert payload["pair_evaluations"][0]["eligibility_status"] == "eligible"
    assert payload["pair_evaluations"][0]["selection_state"] == "selected"
    assert len(payload["ranked_pairs"]) == 1

    loaded = load_combo_yield_candidate_snapshot(
        base=tmp_path,
        run_id="run-1",
        account="lx",
    )
    assert loaded["content_sha256"] == payload["content_sha256"]


def test_combo_yield_snapshot_empty_result_seals_no_candidate(tmp_path: Path) -> None:
    payload = _seal(tmp_path, pairs=False)

    assert payload["opening_status"] == "no_candidate"
    assert payload["ranked_pairs"] == []


def test_combo_yield_snapshot_derives_data_unavailable_from_scope(tmp_path: Path) -> None:
    payload = _seal(tmp_path, pairs=False, status="unavailable")
    assert payload["opening_status"] == "data_unavailable"


def test_combo_yield_snapshot_rejects_tampered_payload(tmp_path: Path) -> None:
    _seal(tmp_path)
    path = (
        tmp_path
        / "output_runs"
        / "run-1"
        / "accounts"
        / "lx"
        / "state"
        / COMBO_YIELD_CANDIDATE_SNAPSHOT_FILE
    )
    text = path.read_text(encoding="utf-8")
    path.write_text(
        text.replace('"net_credit_retention": 0.8', '"net_credit_retention": 0.9', 1),
        encoding="utf-8",
    )

    with pytest.raises(ComboYieldCandidateSnapshotError, match="content hash mismatch"):
        load_combo_yield_candidate_snapshot(
            base=tmp_path,
            run_id="run-1",
            account="lx",
        )


def test_combo_yield_snapshot_rejects_rehashed_wrong_owner(tmp_path: Path) -> None:
    payload = dict(_seal(tmp_path))
    payload["candidate_owner"] = "cc_lp"
    payload["content_sha256"] = canonical_sha256(
        {key: value for key, value in payload.items() if key != "content_sha256"}
    )

    with pytest.raises(ComboYieldCandidateSnapshotError, match="owner mismatch"):
        validate_combo_yield_candidate_snapshot(
            payload,
            expected_run_id="run-1",
            expected_account="lx",
        )


def test_combo_yield_snapshot_rejects_selected_pair_without_evidence(
    tmp_path: Path,
) -> None:
    with pytest.raises(ComboYieldCandidateSnapshotError, match="not eligible"):
        seal_combo_yield_candidate_snapshot(
            base=tmp_path,
            run_id="run-1",
            account="lx",
            market="us",
            account_config_sha256="a" * 64,
            strategy_policy_sha256="b" * 64,
            dependencies=_dependencies(),
            scan_statuses=[_scope()],
            ranked_pairs=[_pair()],
        )


def test_combo_yield_snapshot_rejects_non_finite_evidence(tmp_path: Path) -> None:
    with pytest.raises(ComboYieldCandidateSnapshotError, match="non-finite"):
        seal_combo_yield_candidate_snapshot(
            base=tmp_path,
            run_id="run-1",
            account="lx",
            market="us",
            account_config_sha256="a" * 64,
            strategy_policy_sha256="b" * 64,
            dependencies=_dependencies(),
            scan_statuses=[_scope()],
            pair_evaluations=[{**_pair_evaluation(), "put_delta": float("inf")}],
            rank_records=[_rank_record()],
            ranked_pairs=[_pair()],
        )


def test_combo_yield_snapshot_normalizes_pandas_missing_values(tmp_path: Path) -> None:
    payload = seal_combo_yield_candidate_snapshot(
        base=tmp_path,
        run_id="run-1",
        account="lx",
        market="us",
        account_config_sha256="a" * 64,
        strategy_policy_sha256="b" * 64,
        dependencies=_dependencies(),
        scan_statuses=[_scope()],
        pair_evaluations=[
            {
                **_pair_evaluation(),
                "spot": pd.NA,
                "expiration": pd.NaT,
            }
        ],
        rank_records=[_rank_record()],
        ranked_pairs=[_pair()],
    )

    assert payload["pair_evaluations"][0]["spot"] is None
    assert payload["pair_evaluations"][0]["expiration"] is None


def test_combo_yield_snapshot_rejects_unknown_evidence_type(tmp_path: Path) -> None:
    with pytest.raises(ComboYieldCandidateSnapshotError, match="unsupported type"):
        seal_combo_yield_candidate_snapshot(
            base=tmp_path,
            run_id="run-1",
            account="lx",
            market="us",
            account_config_sha256="a" * 64,
            strategy_policy_sha256="b" * 64,
            dependencies=_dependencies(),
            scan_statuses=[_scope()],
            pair_evaluations=[{**_pair_evaluation(), "spot": object()}],
            rank_records=[_rank_record()],
            ranked_pairs=[_pair()],
        )


def test_combo_yield_snapshot_validates_funding_put_candidate_decision(
    tmp_path: Path,
) -> None:
    payload = seal_combo_yield_candidate_snapshot(
        base=tmp_path,
        run_id="run-1",
        account="lx",
        market="us",
        account_config_sha256="a" * 64,
        strategy_policy_sha256="b" * 64,
        dependencies=_dependencies(),
        scan_statuses=[_scope()],
        funding_put_decisions=[_funding_put_decision()],
        pair_evaluations=[],
        rank_records=[],
        ranked_pairs=[],
    )

    assert payload["funding_put_decisions"][0]["opening_decision"]["mode"] == "put"


def test_combo_yield_snapshot_rejects_mismatched_funding_put_decision(
    tmp_path: Path,
) -> None:
    decision = _funding_put_decision()
    decision["normalized_input"] = {
        **decision["normalized_input"],
        "contract_symbol": "OTHER",
    }

    with pytest.raises(ComboYieldCandidateSnapshotError, match="input mismatch"):
        seal_combo_yield_candidate_snapshot(
            base=tmp_path,
            run_id="run-1",
            account="lx",
            market="us",
            account_config_sha256="a" * 64,
            strategy_policy_sha256="b" * 64,
            dependencies=_dependencies(),
            scan_statuses=[_scope()],
            funding_put_decisions=[decision],
            pair_evaluations=[],
            rank_records=[],
            ranked_pairs=[],
        )


def test_combo_yield_snapshot_rejects_funding_put_outside_scope(
    tmp_path: Path,
) -> None:
    normalized = {
        "symbol": "AAPL",
        "contract_symbol": "AAPL_P100",
        "expiration": "2026-08-21",
        "strike": 100.0,
    }
    decision = {
        "normalized_input": normalized,
        "opening_decision": build_candidate_decision(
            mode="put",
            symbol="AAPL",
            contract_symbol="AAPL_P100",
            accepted=False,
            rejects=[],
            normalized_input=normalized,
        ),
    }

    with pytest.raises(ComboYieldCandidateSnapshotError, match="escapes snapshot scope"):
        seal_combo_yield_candidate_snapshot(
            base=tmp_path,
            run_id="run-1",
            account="lx",
            market="us",
            account_config_sha256="a" * 64,
            strategy_policy_sha256="b" * 64,
            dependencies=_dependencies(),
            scan_statuses=[_scope()],
            funding_put_decisions=[decision],
        )


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        (
            {
                "symbol": "AAPL",
                "candidate_pair_id": "combo_yield:AAPL:AAPL_P100:AAPL_C125",
                "put_contract_symbol": "AAPL_P100",
                "call_contract_symbol": "AAPL_C125",
            },
            "escapes snapshot scope",
        ),
        ({"account": "sy"}, "account identity mismatch"),
        ({"run_id": "run-other"}, "run identity mismatch"),
        ({"candidate_pair_id": "combo_yield:NVDA:WRONG:PAIR"}, "pair identity mismatch"),
    ],
)
def test_combo_yield_snapshot_rejects_pair_evidence_identity_mismatch(
    tmp_path: Path,
    overrides: dict,
    error: str,
) -> None:
    evaluation = {
        **_pair(),
        "diagnostic_scope": "pair",
        "diagnostic_stage": "pair_filter",
        "accepted": False,
        "reject_reasons": "test_rejection",
        **overrides,
    }

    with pytest.raises(ComboYieldCandidateSnapshotError, match=error):
        seal_combo_yield_candidate_snapshot(
            base=tmp_path,
            run_id="run-1",
            account="lx",
            market="us",
            account_config_sha256="a" * 64,
            strategy_policy_sha256="b" * 64,
            dependencies=_dependencies(),
            scan_statuses=[_scope()],
            pair_evaluations=[evaluation],
        )


def test_combo_yield_snapshot_projections_preserve_sealed_facts(
    tmp_path: Path,
) -> None:
    rejected = {
        **_pair(candidate_pair_id="combo_yield:NVDA:NVDA_P100:NVDA_C130"),
        "call_contract_symbol": "NVDA_C130",
        "diagnostic_scope": "pair",
        "diagnostic_stage": "pair_filter",
        "accepted": False,
        "reject_reasons": "min_net_credit_retention",
    }
    payload = seal_combo_yield_candidate_snapshot(
        base=tmp_path,
        run_id="run-1",
        account="lx",
        market="us",
        account_config_sha256="a" * 64,
        strategy_policy_sha256="b" * 64,
        dependencies=_dependencies(),
        scan_statuses=[_scope()],
        funding_put_decisions=[_funding_put_decision()],
        pair_evaluations=[_pair_evaluation(), rejected],
        rank_records=[_rank_record()],
        ranked_pairs=[_pair()],
    )

    assert project_combo_yield_candidates(payload) == payload["ranked_pairs"]
    assert project_combo_yield_funding_put_decisions(payload) == payload[
        "funding_put_decisions"
    ]
    assert project_combo_yield_pair_diagnostics(payload) == payload[
        "pair_evaluations"
    ]
    assert project_combo_yield_rank_evidence(payload) == payload["rank_records"]
    assert project_combo_yield_rejections(payload) == [
        item
        for item in payload["pair_evaluations"]
        if item["eligibility_status"] == "rejected"
    ]
