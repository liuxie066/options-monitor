from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from domain.domain.decision_state_fingerprint import canonical_sha256
from domain.domain.engine import (
    attach_opening_decision_provenance,
    build_candidate_decision,
    evaluate_candidate_invariants,
)
from src.application.opening_candidate_snapshot import (
    OPENING_CANDIDATE_SNAPSHOT_FILE,
    OpeningCandidateSnapshotError,
    dependency_from_file,
    dependency_from_hash,
    load_latest_opening_candidate_snapshot,
    load_opening_candidate_snapshot,
    ranked_opening_candidate_decisions,
    ranked_opening_candidates,
    seal_opening_candidate_snapshot,
)


NOW = datetime(2026, 8, 6, 9, 30, tzinfo=timezone.utc)


def _decision(*, contract_symbol: str, period_return: float) -> dict:
    normalized = {
        "symbol": "NVDA",
        "contract_symbol": contract_symbol,
        "expiration": "2026-09-18",
        "option_type": "put",
        "strike": 90.0,
        "spot": 100.0,
        "dte": 43,
        "bid": 2.9,
        "ask": 3.1,
        "mid": 3.0,
        "multiplier": 100,
        "currency": "USD",
        "open_interest": 500,
        "volume": 50,
        "spread_ratio": 0.0667,
        "period_net_return_on_cash_basis": period_return,
        "annualized_net_return_on_cash_basis": period_return * 365 / 43,
        "net_income": 295.0,
        "net_income_cny": 2124.0,
        "implied_volatility": 0.42,
        "term_matched_rv": 0.30,
        "iv_rv_ratio": 1.4,
        "iv_minus_rv": 0.12,
        "event_source_status": "ok",
        "event_earnings_coverage_status": "complete",
        "event_flag": False,
    }
    invariant = evaluate_candidate_invariants(
        normalized,
        mode="put",
        risk_policy_version="candidate_pipeline_policy.v2",
        quote_snapshot_id="c" * 64,
        min_dte=21,
        max_dte=60,
        min_strike=None,
        max_strike=100,
        min_annualized_return=0.10,
        min_net_income=50,
        annualized_return=normalized["annualized_net_return_on_cash_basis"],
        net_income=295,
        min_open_interest=None,
        min_volume=None,
        max_spread_ratio=0.3,
        event_flag=False,
        event_mode="reject",
        open_interest=500,
        volume=50,
        spread_ratio=0.0667,
        extra_required_fields=(),
    )
    opening = attach_opening_decision_provenance(
        build_candidate_decision(
            mode="put",
            symbol="NVDA",
            contract_symbol=contract_symbol,
            accepted=True,
            normalized_input=dict(invariant["normalized_input"]),
        ),
        risk_policy_version="candidate_pipeline_policy.v2",
        risk_policy_hash=str(invariant["risk_policy_hash"]),
        quote_snapshot_id="c" * 64,
        normalized_input=dict(invariant["normalized_input"]),
    )
    return {
        "schema_version": "candidate_all_decisions.v1",
        "candidate_id": canonical_sha256({"contract": contract_symbol}),
        "strategy_mode": "put",
        "normalized_input": invariant["normalized_input"],
        "normalized_input_hash": invariant["normalized_input_hash"],
        "risk_policy_version": invariant["risk_policy_version"],
        "risk_policy_hash": invariant["risk_policy_hash"],
        "quote_snapshot_id": invariant["quote_snapshot_id"],
        "opening_decision": opening,
        "invariant_decision": invariant,
    }


def _dependencies(base: Path, run_id: str, account: str) -> list[dict]:
    run_state = base / "output_runs" / run_id / "state"
    account_state = base / "output_runs" / run_id / "accounts" / account / "state"
    run_state.mkdir(parents=True, exist_ok=True)
    account_state.mkdir(parents=True, exist_ok=True)
    required = run_state / "required_data_snapshot_manifest.json"
    portfolio = account_state / "prepared_portfolio_context_manifest.json"
    ledger = account_state / "prepared_option_positions_context_manifest.json"
    required.write_text('{"sealed":true}\n', encoding="utf-8")
    portfolio.write_text('{"portfolio":true}\n', encoding="utf-8")
    ledger.write_text('{"ledger":true}\n', encoding="utf-8")
    required_dependency = dependency_from_file(
        kind="required_data",
        path=required,
        base=base,
    )
    return [
        required_dependency,
        dependency_from_file(kind="portfolio", path=portfolio, base=base),
        dependency_from_file(kind="ledger", path=ledger, base=base),
        dependency_from_hash(kind="fx", sha256="d" * 64),
        dependency_from_hash(
            kind="earnings_rv",
            sha256=required_dependency["sha256"],
        ),
    ]


def _seal(base: Path, *, run_id: str = "run-1", account: str = "lx") -> dict:
    high = _decision(
        contract_symbol="NVDA260918P00090000",
        period_return=0.04,
    )
    low = _decision(
        contract_symbol="NVDA260918P00085000",
        period_return=0.03,
    )
    return seal_opening_candidate_snapshot(
        base=base,
        run_id=run_id,
        account=account,
        market="US",
        physical_account={
            "status": "available",
            "logical_account": account,
            "futu_account_id": "12345",
            "trd_env": "REAL",
            "market": "us",
            "source": "opend",
        },
        account_config_sha256="a" * 64,
        strategy_policy_sha256="b" * 64,
        dependencies=_dependencies(base, run_id, account),
        scan_statuses=[
            {
                "symbol": "NVDA",
                "strategy_mode": "put",
                "status": "completed",
                "reason": "all_decisions_captured",
                "quote_snapshot_id": "c" * 64,
                "quote_receipt_relpath": "quotes/NVDA/receipt.json",
            }
        ],
        candidate_decisions=[low, high],
        final_candidates={
            "put": [low["normalized_input"], high["normalized_input"]],
        },
        sealed_at=NOW,
    )


def test_snapshot_seals_final_candidate_order_and_account_binding(tmp_path: Path) -> None:
    payload = _seal(tmp_path)

    assert payload["opening_status"] == "candidates_found"
    assert [row["rank"] for row in payload["ranked_candidates"]] == [1, 2]
    assert [
        row["facts"]["contract_symbol"] for row in payload["ranked_candidates"]
    ] == ["NVDA260918P00090000", "NVDA260918P00085000"]
    assert all(
        row["candidate_id"] in {
            decision["candidate_id"] for decision in payload["candidate_decisions"]
        }
        for row in payload["ranked_candidates"]
    )
    assert [
        row["facts"]["contract_symbol"]
        for row in ranked_opening_candidates(payload, mode="put")
    ] == ["NVDA260918P00090000", "NVDA260918P00085000"]
    assert [
        row["normalized_input"]["contract_symbol"]
        for row in ranked_opening_candidate_decisions(payload)
    ] == ["NVDA260918P00090000", "NVDA260918P00085000"]
    assert payload == load_opening_candidate_snapshot(
        base=tmp_path,
        run_id="run-1",
        account="lx",
    )


def test_agent_filter_explains_sealed_scope_without_refiltering(
    tmp_path: Path,
) -> None:
    from src.application.agent_tools.candidate_filter_impl import (
        candidate_filter_explain_tool,
    )

    _seal(tmp_path)
    data, warnings, meta = candidate_filter_explain_tool(
        {
            "runtime_root": str(tmp_path),
            "run_id": "run-1",
            "account": "lx",
            "symbol": "NVDA",
            "function": "sell_put",
        },
        repo_base=lambda: tmp_path,
        mask_path=lambda path: str(path) if path else None,
    )

    assert warnings == []
    assert data["trace_count"] == 3
    assert data["functions"][0]["status"] == "accepted"
    assert all(
        event["run_id"] == "run-1" and event["account"] == "lx"
        for event in data["functions"][0]["events"]
    )
    assert meta["source_files"][0]["content_sha256"]


def test_empty_result_is_a_sealed_no_candidate_snapshot(tmp_path: Path) -> None:
    payload = seal_opening_candidate_snapshot(
        base=tmp_path,
        run_id="run-empty",
        account="lx",
        market="US",
        physical_account={
            "status": "available",
            "logical_account": "lx",
            "futu_account_id": "12345",
            "trd_env": "REAL",
            "market": "US",
            "source": "opend",
        },
        account_config_sha256="a" * 64,
        strategy_policy_sha256="b" * 64,
        dependencies=_dependencies(tmp_path, "run-empty", "lx"),
        scan_statuses=[
            {
                "symbol": "NVDA",
                "strategy_mode": "put",
                "status": "completed",
            }
        ],
        candidate_decisions=[],
        final_candidates={"put": []},
        sealed_at=NOW,
    )

    assert payload["opening_status"] == "no_candidate"
    assert payload["ranked_candidates"] == []
    assert (
        tmp_path
        / "output_runs"
        / "run-empty"
        / "accounts"
        / "lx"
        / "state"
        / OPENING_CANDIDATE_SNAPSHOT_FILE
    ).is_file()


def test_partial_scope_is_explicit_but_optional_metrics_do_not_make_partial(
    tmp_path: Path,
) -> None:
    decision = _decision(
        contract_symbol="NVDA260918P00090000",
        period_return=0.04,
    )
    decision["normalized_input"]["open_interest"] = None
    decision["normalized_input"]["volume"] = None
    payload = seal_opening_candidate_snapshot(
        base=tmp_path,
        run_id="run-partial",
        account="lx",
        market="US",
        physical_account={
            "status": "available",
            "logical_account": "lx",
            "futu_account_id": "12345",
            "trd_env": "REAL",
            "market": "US",
            "source": "opend",
        },
        account_config_sha256="a" * 64,
        strategy_policy_sha256="b" * 64,
        dependencies=_dependencies(tmp_path, "run-partial", "lx"),
        scan_statuses=[
            {"symbol": "NVDA", "strategy_mode": "put", "status": "completed"},
            {"symbol": "NVDA", "strategy_mode": "call", "status": "failed"},
        ],
        candidate_decisions=[decision],
        final_candidates={"put": [decision["normalized_input"]], "call": []},
        sealed_at=NOW,
    )

    assert payload["opening_status"] == "partial_data"
    result_by_mode = {
        row["strategy_mode"]: row for row in payload["strategy_results"]
    }
    assert result_by_mode["put"]["strategy_status"] == "candidates_found"


def test_tamper_wrong_scope_and_missing_dependency_fail_closed(tmp_path: Path) -> None:
    _seal(tmp_path)
    snapshot_path = (
        tmp_path
        / "output_runs"
        / "run-1"
        / "accounts"
        / "lx"
        / "state"
        / OPENING_CANDIDATE_SNAPSHOT_FILE
    )
    payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    payload["opening_status"] = "no_candidate"
    snapshot_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(OpeningCandidateSnapshotError, match="content hash mismatch"):
        load_opening_candidate_snapshot(base=tmp_path, run_id="run-1", account="lx")
    with pytest.raises(OpeningCandidateSnapshotError, match="unavailable"):
        load_opening_candidate_snapshot(base=tmp_path, run_id="run-1", account="sy")

    other = tmp_path / "missing-dependency"
    _seal(other)
    dependency = (
        other
        / "output_runs"
        / "run-1"
        / "state"
        / "required_data_snapshot_manifest.json"
    )
    dependency.unlink()
    with pytest.raises(OpeningCandidateSnapshotError, match="dependency is missing"):
        load_opening_candidate_snapshot(base=other, run_id="run-1", account="lx")


def test_immutable_conflict_and_latest_pointer_fail_closed(tmp_path: Path) -> None:
    _seal(tmp_path)
    _seal(tmp_path)
    pointer = tmp_path / "output_shared" / "state" / "last_run_dir.txt"
    pointer.parent.mkdir(parents=True)
    pointer.write_text("output_runs/run-1\n", encoding="utf-8")
    assert load_latest_opening_candidate_snapshot(base=tmp_path, account="lx")[
        "run_id"
    ] == "run-1"

    pointer.write_text("output_runs/missing-run\n", encoding="utf-8")
    with pytest.raises(OpeningCandidateSnapshotError, match="unavailable"):
        load_latest_opening_candidate_snapshot(base=tmp_path, account="lx")

    with pytest.raises(OpeningCandidateSnapshotError, match="conflicts"):
        seal_opening_candidate_snapshot(
            **{
                "base": tmp_path,
                "run_id": "run-1",
                "account": "lx",
                "market": "US",
                "physical_account": {
                    "status": "available",
                    "logical_account": "lx",
                    "futu_account_id": "12345",
                    "trd_env": "REAL",
                    "market": "US",
                    "source": "opend",
                },
                "account_config_sha256": "a" * 64,
                "strategy_policy_sha256": "b" * 64,
                "dependencies": _dependencies(tmp_path, "run-1", "lx"),
                "scan_statuses": [
                    {
                        "symbol": "NVDA",
                        "strategy_mode": "put",
                        "status": "completed",
                    }
                ],
                "candidate_decisions": [],
                "final_candidates": {"put": []},
                "sealed_at": NOW,
            }
        )
