from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from domain.domain.decision_state_fingerprint import canonical_sha256
from domain.domain.engine import (
    EARNINGS_NEAR_EXPIRY_POLICY_VERSION,
    EARNINGS_NEAR_EXPIRY_WINDOW_DAYS,
)
from src.application.opening_candidate_snapshot import (
    OPENING_CANDIDATE_SNAPSHOT_FILE,
    OpeningCandidateSnapshotError,
    candidate_universe_summary,
    dependency_from_file,
    dependency_from_hash,
    load_opening_candidate_snapshot,
    ranked_opening_candidate_decisions,
    ranked_opening_candidates,
    rejected_opening_candidate_decisions,
    seal_opening_candidate_snapshot,
    validate_opening_candidate_snapshot,
)
from src.application.candidate_snapshot_manifest import (
    CandidateSnapshotManifestError,
    load_latest_candidate_snapshot_bundle,
    publish_candidate_snapshot_manifest,
)
from src.application.opend_symbol_outputs import SUCCESS_EMPTY_REASON_CODES
from src.application.strategy_scan_status import (
    publish_strategy_scan_status,
    publish_strategy_scan_status_index_v2,
)


NOW = datetime(2026, 8, 6, 9, 30, tzinfo=timezone.utc)


def _candidate(*, contract_symbol: str, period_return: float) -> dict:
    return {
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
        "earnings_evidence_status": "ready",
        "earnings_reason_code": None,
        "earnings_policy_version": EARNINGS_NEAR_EXPIRY_POLICY_VERSION,
        "earnings_window_days": EARNINGS_NEAR_EXPIRY_WINDOW_DAYS,
        "earnings_market_date": "2026-08-06",
        "earnings_hard_window_start": "2026-09-12",
        "earnings_hard_window_end": "2026-09-18",
        "earnings_hard_coverage_status": "complete",
        "earnings_soft_coverage_status": "complete",
        "earnings_has_event": False,
        "earnings_blocking_has_event": False,
        "earnings_events": [],
        "earnings_blocking_events": [],
        "earnings_nonblocking_events": [],
        "max_new_contracts": 1,
        "policy_min_dte": 21,
        "policy_max_dte": 60,
        "policy_max_strike": 100.0,
        "policy_max_spread_ratio": 0.30,
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
    high = _candidate(
        contract_symbol="NVDA260918P00090000",
        period_return=0.04,
    )
    low = _candidate(
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
                "quote_snapshot_id": "c" * 64,
                "quote_receipt_relpath": "quotes/NVDA/receipt.json",
            }
        ],
        final_candidates={"put": [low, high]},
        sealed_at=NOW,
    )


def _publish_opening_manifest(base: Path, payload: dict) -> dict:
    account_dir = (
        base
        / "output_runs"
        / str(payload["run_id"])
        / "accounts"
        / str(payload["account"])
    )
    expected: list[dict[str, str]] = []
    ranked = [dict(row) for row in payload.get("ranked_candidates") or []]
    for raw in payload.get("scope_results") or []:
        scope = dict(raw)
        if scope.get("scope") != "strategy":
            continue
        mode = str(scope["strategy_mode"])
        family = "sell_put" if mode == "put" else "covered_call"
        symbol = str(scope["symbol"])
        reason = str(scope.get("reason_code") or "") or None
        status = str(scope["status"])
        candidate_count = sum(
            str(row.get("strategy_mode")) == mode
            and str((row.get("facts") or {}).get("symbol") or "").upper()
            == symbol.upper()
            for row in ranked
        )
        status_kwargs: dict = {
            "report_dir": account_dir,
            "run_id": str(payload["run_id"]),
            "account": str(payload["account"]),
            "market": str(payload["market"]),
            "symbol": symbol,
            "strategy_family": family,
            "status": status,
            "snapshot_id": scope.get("quote_snapshot_id"),
            "receipt_relpath": scope.get("quote_receipt_relpath"),
        }
        if status == "completed":
            status_kwargs["candidate_count"] = candidate_count
            if reason in SUCCESS_EMPTY_REASON_CODES:
                status_kwargs.update(
                    source_outcome="success_empty",
                    reason_code=reason,
                )
            else:
                status_kwargs["reason"] = reason
        else:
            status_kwargs["reason"] = reason or "fixture_unavailable"
        publish_strategy_scan_status(**status_kwargs)
        expected.append(
            {
                "market": str(payload["market"]),
                "symbol": symbol,
                "strategy_family": family,
                "strategy_mode": mode,
                "candidate_owner": "opening",
                "account_config_sha256": str(payload["account_config_sha256"]),
            }
        )
    publish_strategy_scan_status_index_v2(
        report_dir=account_dir,
        run_id=str(payload["run_id"]),
        account=str(payload["account"]),
        account_config_sha256=str(payload["account_config_sha256"]),
        expected=expected,
    )
    return publish_candidate_snapshot_manifest(
        base=base,
        run_id=str(payload["run_id"]),
        account=str(payload["account"]),
        strategy_policy_sha256=str(payload["strategy_policy_sha256"]),
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


def test_same_snapshot_replay_is_byte_hash_and_order_stable(tmp_path: Path) -> None:
    first = _seal(tmp_path)
    snapshot_path = (
        tmp_path
        / "output_runs"
        / "run-1"
        / "accounts"
        / "lx"
        / "state"
        / OPENING_CANDIDATE_SNAPSHOT_FILE
    )
    first_bytes = snapshot_path.read_bytes()

    second = _seal(tmp_path)

    assert second["content_sha256"] == first["content_sha256"]
    assert second["ranked_candidates"] == first["ranked_candidates"]
    assert snapshot_path.read_bytes() == first_bytes


@pytest.mark.parametrize(
    "collection",
    (
        "strategy_results",
        "candidate_decisions",
        "ranked_candidates",
        "scope_results",
    ),
)
def test_snapshot_collection_scalar_items_fail_with_domain_error(
    tmp_path: Path,
    collection: str,
) -> None:
    payload = _seal(tmp_path)
    malformed = {**payload, collection: [None]}
    malformed["content_sha256"] = canonical_sha256(
        {
            key: value
            for key, value in malformed.items()
            if key != "content_sha256"
        }
    )

    with pytest.raises(
        OpeningCandidateSnapshotError,
        match="opening candidate (strategy results|snapshot collections) are invalid",
    ):
        validate_opening_candidate_snapshot(
            malformed,
            expected_run_id="run-1",
            expected_account="lx",
        )


def test_account_execution_order_does_not_change_market_facts(tmp_path: Path) -> None:
    forward_root = tmp_path / "forward"
    reverse_root = tmp_path / "reverse"
    forward = {
        account: _seal(forward_root, account=account)
        for account in ("sy", "lx")
    }
    reverse = {
        account: _seal(reverse_root, account=account)
        for account in ("lx", "sy")
    }

    for account in ("lx", "sy"):
        assert forward[account]["content_sha256"] == reverse[account][
            "content_sha256"
        ]
        assert forward[account]["ranked_candidates"] == reverse[account][
            "ranked_candidates"
        ]

    lx_decisions = {
        row["normalized_input"]["contract_symbol"]: row
        for row in forward["lx"]["candidate_decisions"]
    }
    sy_decisions = {
        row["normalized_input"]["contract_symbol"]: row
        for row in forward["sy"]["candidate_decisions"]
    }
    contract_symbol = "NVDA260918P00090000"
    lx_decision = lx_decisions[contract_symbol]
    sy_decision = sy_decisions[contract_symbol]
    assert lx_decision["normalized_input_hash"] == sy_decision[
        "normalized_input_hash"
    ]
    assert forward["lx"]["ranked_candidates"][0]["facts"] == forward["sy"][
        "ranked_candidates"
    ][0]["facts"]
    assert lx_decision["candidate_id"] != sy_decision["candidate_id"]


def test_agent_filter_explains_sealed_scope_without_refiltering(
    tmp_path: Path,
) -> None:
    from src.application.agent_tools.candidate_filter_impl import (
        candidate_filter_explain_tool,
    )

    payload = _seal(tmp_path)
    manifest = _publish_opening_manifest(tmp_path, payload)
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
    assert meta["source_files"][0]["manifest_content_sha256"] == manifest[
        "content_sha256"
    ]
    assert meta["source_files"][0]["authority"] == (
        "terminal_manifest_bound_opening_candidate_snapshot"
    )


def test_agent_explain_rejects_uncommitted_opening_owner(tmp_path: Path) -> None:
    from src.application.agent_tool_contracts import AgentToolError
    from src.application.agent_tools.candidate_filter_impl import (
        candidate_filter_explain_tool,
    )

    _seal(tmp_path)

    with pytest.raises(AgentToolError, match="manifest is unavailable"):
        candidate_filter_explain_tool(
            {
                "runtime_root": str(tmp_path),
                "run_id": "run-1",
                "account": "lx",
                "symbol": "NVDA",
            },
            repo_base=lambda: tmp_path,
            mask_path=lambda path: str(path) if path else None,
        )


def test_agent_rank_rejects_uncommitted_opening_owner(tmp_path: Path) -> None:
    from src.application.agent_tool_contracts import AgentToolError
    from src.application.agent_tools.candidate_rank_impl import (
        candidate_rank_explain_tool,
    )

    _seal(tmp_path)

    with pytest.raises(AgentToolError, match="manifest is unavailable"):
        candidate_rank_explain_tool(
            {
                "runtime_root": str(tmp_path),
                "run_id": "run-1",
                "account": "lx",
                "mode": "put",
            },
            repo_base=lambda: tmp_path,
            resolve_output_root=lambda _value: tmp_path,
            mask_path=lambda path: str(path) if path else None,
        )


def test_validator_rejects_decision_outside_declared_strategy_scope(
    tmp_path: Path,
) -> None:
    payload = _seal(tmp_path)
    tampered = dict(payload)
    tampered["scope_results"] = [
        (
            {**dict(row), "symbol": "AAPL"}
            if row.get("scope") == "strategy"
            else dict(row)
        )
        for row in payload["scope_results"]
    ]
    tampered["content_sha256"] = canonical_sha256(
        {key: value for key, value in tampered.items() if key != "content_sha256"}
    )

    with pytest.raises(OpeningCandidateSnapshotError, match="escapes scan scope"):
        validate_opening_candidate_snapshot(
            tampered,
            expected_run_id="run-1",
            expected_account="lx",
        )


def test_terminal_manifest_rejects_minimal_legacy_opening_contract(
    tmp_path: Path,
) -> None:
    payload = _seal(tmp_path)
    legacy = {
        **payload,
        "candidate_decisions": [
            {"candidate_id": row["candidate_id"]}
            for row in payload["candidate_decisions"]
        ],
    }
    legacy["content_sha256"] = canonical_sha256(
        {key: value for key, value in legacy.items() if key != "content_sha256"}
    )
    snapshot_path = (
        tmp_path
        / "output_runs"
        / "run-1"
        / "accounts"
        / "lx"
        / "state"
        / OPENING_CANDIDATE_SNAPSHOT_FILE
    )
    snapshot_path.write_text(
        json.dumps(legacy, ensure_ascii=False),
        encoding="utf-8",
    )

    with pytest.raises(CandidateSnapshotManifestError, match="snapshot is invalid"):
        _publish_opening_manifest(tmp_path, legacy)


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
                "reason": "no_expirations",
                "quote_snapshot_id": "empty-quote",
                "quote_receipt_relpath": "quotes/NVDA/receipt.json",
            }
        ],
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
    from src.application.agent_tools.candidate_filter_impl import (
        candidate_filter_explain_tool,
    )

    _publish_opening_manifest(tmp_path, payload)

    data, warnings, _meta = candidate_filter_explain_tool(
        {
            "runtime_root": str(tmp_path),
            "run_id": "run-empty",
            "account": "lx",
            "symbol": "NVDA",
            "function": "sell_put",
        },
        repo_base=lambda: tmp_path,
        mask_path=lambda path: str(path) if path else None,
    )
    function = data["functions"][0]
    assert warnings == []
    assert function["status"] == "completed"
    assert function["rejection_reason_counts"] == {}
    assert function["events"][0]["rule"] == "no_expirations"
    assert function["events"][0]["is_rejection"] is False


@pytest.mark.parametrize(
    ("run_id", "call_statuses"),
    [
        (
            "run-shared-call-all-unheld",
            [
                {
                    "symbol": "3690.HK",
                    "strategy_mode": "call",
                    "status": "not_applicable",
                    "reason": "covered_call_underlying_not_held",
                }
            ],
        ),
        (
            "run-shared-call-mixed",
            [
                {
                    "symbol": "0700.HK",
                    "strategy_mode": "call",
                    "status": "completed",
                    "reason": "no_candidate",
                },
                {
                    "symbol": "3690.HK",
                    "strategy_mode": "call",
                    "status": "not_applicable",
                    "reason": "covered_call_underlying_not_held",
                },
                {
                    "symbol": "9992.HK",
                    "strategy_mode": "call",
                    "status": "completed",
                    "reason": "no_candidate",
                },
            ],
        ),
    ],
)
def test_shared_config_call_without_account_holding_is_a_legal_zero_candidate(
    tmp_path: Path,
    run_id: str,
    call_statuses: list[dict],
) -> None:
    payload = seal_opening_candidate_snapshot(
        base=tmp_path,
        run_id=run_id,
        account="sy",
        market="HK",
        physical_account={
            "status": "available",
            "logical_account": "sy",
            "futu_account_id": "12345",
            "trd_env": "REAL",
            "market": "HK",
            "source": "opend",
        },
        account_config_sha256="a" * 64,
        strategy_policy_sha256="b" * 64,
        dependencies=_dependencies(tmp_path, run_id, "sy"),
        scan_statuses=[
            {
                "symbol": "0700.HK",
                "strategy_mode": "put",
                "status": "completed",
                "reason": "no_candidate",
            },
            *call_statuses,
        ],
        final_candidates={"put": [], "call": []},
        sealed_at=NOW,
    )

    assert payload["opening_status"] == "no_candidate"
    assert {
        row["strategy_mode"]: row["strategy_status"]
        for row in payload["strategy_results"]
    } == {"call": "no_candidate", "put": "no_candidate"}
    unheld_scope = next(
        row
        for row in payload["scope_results"]
        if row["symbol"] == "3690.HK" and row["strategy_mode"] == "call"
    )
    assert unheld_scope["status"] == "not_applicable"
    assert unheld_scope["reason_code"] == "covered_call_underlying_not_held"
    assert candidate_universe_summary(payload) == {
        "status": "complete",
        "affected_scopes": [],
    }


def test_missing_call_portfolio_context_remains_data_unavailable(
    tmp_path: Path,
) -> None:
    run_id = "run-shared-call-context-unavailable"
    payload = seal_opening_candidate_snapshot(
        base=tmp_path,
        run_id=run_id,
        account="sy",
        market="HK",
        physical_account={
            "status": "available",
            "logical_account": "sy",
            "futu_account_id": "12345",
            "trd_env": "REAL",
            "market": "HK",
            "source": "opend",
        },
        account_config_sha256="a" * 64,
        strategy_policy_sha256="b" * 64,
        dependencies=_dependencies(tmp_path, run_id, "sy"),
        scan_statuses=[
            {
                "symbol": "0700.HK",
                "strategy_mode": "put",
                "status": "completed",
                "reason": "no_candidate",
            },
            {
                "symbol": "0700.HK",
                "strategy_mode": "call",
                "status": "completed",
                "reason": "no_candidate",
            },
            {
                "symbol": "3690.HK",
                "strategy_mode": "call",
                "status": "not_applicable",
                "reason": "covered_call_portfolio_context_unavailable",
            },
        ],
        final_candidates={"put": [], "call": []},
        sealed_at=NOW,
    )

    results = {
        row["strategy_mode"]: row["strategy_status"]
        for row in payload["strategy_results"]
    }
    assert payload["opening_status"] == "partial_data"
    assert results == {"call": "data_unavailable", "put": "no_candidate"}
    assert candidate_universe_summary(payload) == {
        "status": "partial",
        "affected_scopes": [
            {
                "symbol": "3690.HK",
                "strategy_mode": "call",
                "reason_code": "covered_call_portfolio_context_unavailable",
            }
        ],
    }


def test_input_invalid_decision_cannot_seal_as_clean_no_candidate(
    tmp_path: Path,
) -> None:
    from domain.domain.engine import (
        STAGE_INPUT_NORMALIZATION,
        build_candidate_decision,
    )

    candidate = _candidate(
        contract_symbol="NVDA260918P00090000",
        period_return=0.01,
    )
    opening_decision = build_candidate_decision(
        mode="put",
        symbol="NVDA",
        contract_symbol=candidate["contract_symbol"],
        accepted=False,
        rejects=[
            {
                "stage": STAGE_INPUT_NORMALIZATION,
                "reason": "input_invalid",
                "message": "term-matched realized volatility is unavailable",
                "metric_value": {
                    "reason_code": "term_matched_rv_unavailable",
                },
                "threshold": "ok",
            }
        ],
        normalized_input=candidate,
    )

    payload = seal_opening_candidate_snapshot(
        base=tmp_path,
        run_id="run-input-invalid",
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
        dependencies=_dependencies(tmp_path, "run-input-invalid", "lx"),
        scan_statuses=[
            {
                "symbol": "NVDA",
                "strategy_mode": "put",
                "status": "completed",
                "reason": "no_candidate",
            }
        ],
        final_candidates={"put": []},
        candidate_evaluations={
            "put": [
                {
                    "normalized_input": candidate,
                    "opening_decision": opening_decision,
                }
            ]
        },
        sealed_at=NOW,
    )

    assert payload["opening_status"] == "data_unavailable"
    assert payload["strategy_results"] == [
        {
            "strategy_mode": "put",
            "strategy_status": "data_unavailable",
            "capacity_status": "available",
            "candidate_count": 0,
            "scope_count": 1,
        }
    ]
    assert candidate_universe_summary(payload) == {
        "status": "partial",
        "affected_scopes": [
            {
                "symbol": "NVDA",
                "strategy_mode": "put",
                "reason_code": "input_invalid",
            }
        ],
    }


@pytest.mark.parametrize("scan_reason", ["partial_data", "no_candidate"])
def test_mixed_input_unavailable_decisions_seal_as_partial_data(
    tmp_path: Path,
    scan_reason: str,
) -> None:
    from domain.domain.engine import (
        STAGE_INPUT_NORMALIZATION,
        build_candidate_decision,
        evaluate_opening_candidate_policy,
    )

    unavailable_candidate = _candidate(
        contract_symbol="NVDA260918P00090000",
        period_return=0.01,
    )
    unavailable_decision = build_candidate_decision(
        mode="put",
        symbol="NVDA",
        contract_symbol=unavailable_candidate["contract_symbol"],
        accepted=False,
        rejects=[
            {
                "stage": STAGE_INPUT_NORMALIZATION,
                "reason": "input_missing",
                "message": "term-matched realized volatility is missing",
                "metric_value": {
                    "reason_code": "term_matched_rv_unavailable",
                },
                "threshold": "ok",
            }
        ],
        normalized_input=unavailable_candidate,
    )
    policy_rejected_candidate = _candidate(
        contract_symbol="NVDA260918P00085000",
        period_return=0.01,
    )
    policy_rejected_decision = evaluate_opening_candidate_policy(
        policy_rejected_candidate,
        mode="put",
    )
    assert policy_rejected_decision["accepted"] is False

    run_id = f"run-mixed-{scan_reason}"
    payload = seal_opening_candidate_snapshot(
        base=tmp_path,
        run_id=run_id,
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
        dependencies=_dependencies(tmp_path, run_id, "lx"),
        scan_statuses=[
            {
                "symbol": "NVDA",
                "strategy_mode": "put",
                "status": "completed",
                "reason": scan_reason,
            }
        ],
        final_candidates={"put": []},
        candidate_evaluations={
            "put": [
                {
                    "normalized_input": unavailable_candidate,
                    "opening_decision": unavailable_decision,
                },
                {
                    "normalized_input": policy_rejected_candidate,
                    "opening_decision": policy_rejected_decision,
                },
            ]
        },
        sealed_at=NOW,
    )

    assert payload["opening_status"] == "partial_data"
    assert payload["strategy_results"] == [
        {
            "strategy_mode": "put",
            "strategy_status": "partial_data",
            "capacity_status": "available",
            "candidate_count": 0,
            "scope_count": 1,
        }
    ]
    universe = candidate_universe_summary(payload)
    assert universe["status"] == "partial"
    assert universe["affected_scopes"][0]["symbol"] == "NVDA"
    assert universe["affected_scopes"][0]["strategy_mode"] == "put"


def test_rejected_contract_is_sealed_and_agent_reports_recorded_reason(
    tmp_path: Path,
) -> None:
    from domain.domain.engine import evaluate_opening_candidate_policy
    from src.application.agent_tools.candidate_filter_impl import (
        candidate_filter_explain_tool,
    )

    candidate = _candidate(
        contract_symbol="NVDA260918P00090000",
        period_return=0.01,
    )
    opening_decision = evaluate_opening_candidate_policy(candidate, mode="put")
    assert opening_decision["accepted"] is False

    payload = seal_opening_candidate_snapshot(
        base=tmp_path,
        run_id="run-rejected",
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
        dependencies=_dependencies(tmp_path, "run-rejected", "lx"),
        scan_statuses=[
            {
                "symbol": "NVDA",
                "strategy_mode": "put",
                "status": "completed",
            }
        ],
        final_candidates={"put": []},
        candidate_evaluations={
            "put": [
                {
                    "normalized_input": candidate,
                    "opening_decision": opening_decision,
                }
            ],
            "call": [],
        },
        sealed_at=NOW,
    )

    contract_scope = next(
        row for row in payload["scope_results"] if row["scope"] == "contract"
    )
    assert contract_scope["status"] == "rejected"
    assert contract_scope["reason_codes"] == ["return_annualized"]
    assert contract_scope["rejects"][0]["metric_value"] == opening_decision[
        "rejects"
    ][0]["metric_value"]
    projected_rejections = rejected_opening_candidate_decisions(
        payload,
        mode="put",
    )
    assert len(projected_rejections) == 1
    assert projected_rejections[0]["candidate_id"] == contract_scope["candidate_id"]
    assert projected_rejections[0]["opening_decision"]["accepted"] is False

    _publish_opening_manifest(tmp_path, payload)

    data, warnings, _meta = candidate_filter_explain_tool(
        {
            "runtime_root": str(tmp_path),
            "run_id": "run-rejected",
            "account": "lx",
            "symbol": "NVDA",
            "function": "sell_put",
        },
        repo_base=lambda: tmp_path,
        mask_path=lambda path: str(path) if path else None,
    )

    function = data["functions"][0]
    event = next(item for item in function["events"] if item["is_rejection"])
    assert warnings == []
    assert function["status"] == "rejected"
    assert function["rejection_reason_counts"] == {"return_annualized": 1}
    assert event["metric_value"] == opening_decision["rejects"][0]["metric_value"]
    assert event["threshold"] == 0.10
    assert event["message"] == "annualized net return below formal minimum or unavailable"


def test_rejected_decision_cannot_escape_scanned_symbol_scope(
    tmp_path: Path,
) -> None:
    from domain.domain.engine import evaluate_opening_candidate_policy

    candidate = _candidate(
        contract_symbol="AAPL260918P00090000",
        period_return=0.01,
    )
    candidate["symbol"] = "AAPL"
    opening_decision = evaluate_opening_candidate_policy(candidate, mode="put")

    with pytest.raises(OpeningCandidateSnapshotError, match="escapes scan scope"):
        seal_opening_candidate_snapshot(
            base=tmp_path,
            run_id="run-cross-symbol",
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
            dependencies=_dependencies(tmp_path, "run-cross-symbol", "lx"),
            scan_statuses=[
                {
                    "symbol": "NVDA",
                    "strategy_mode": "put",
                    "status": "completed",
                }
            ],
            final_candidates={"put": []},
            candidate_evaluations={
                "put": [
                    {
                        "normalized_input": candidate,
                        "opening_decision": opening_decision,
                    }
                ]
            },
            sealed_at=NOW,
        )


def test_snapshot_reuses_resolved_policy_fields_instead_of_default_thresholds(
    tmp_path: Path,
) -> None:
    candidate = _candidate(
        contract_symbol="NVDA260918P00090000",
        period_return=0.01,
    )
    candidate["annualized_net_return_on_cash_basis"] = 0.09
    candidate["policy_min_annualized_return"] = 0.08
    candidate["policy_min_net_premium_cny"] = 50.0
    candidate["policy_min_iv_rv_ratio"] = 1.10
    candidate["policy_min_iv_minus_rv"] = 0.05

    payload = seal_opening_candidate_snapshot(
        base=tmp_path,
        run_id="run-custom-policy",
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
        dependencies=_dependencies(tmp_path, "run-custom-policy", "lx"),
        scan_statuses=[
            {
                "symbol": "NVDA",
                "strategy_mode": "put",
                "status": "completed",
            }
        ],
        final_candidates={"put": [candidate]},
        sealed_at=NOW,
    )

    assert payload["opening_status"] == "candidates_found"
    assert payload["candidate_decisions"][0]["opening_decision"]["accepted"] is True


def test_market_closed_is_sealed_as_explicit_unavailable_state(
    tmp_path: Path,
) -> None:
    payload = seal_opening_candidate_snapshot(
        base=tmp_path,
        run_id="run-closed",
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
        dependencies=_dependencies(tmp_path, "run-closed", "lx"),
        scan_statuses=[
            {
                "symbol": "NVDA",
                "strategy_mode": "put",
                "status": "unavailable",
                "reason": "market_closed",
            },
            {
                "symbol": "NVDA",
                "strategy_mode": "call",
                "status": "not_applicable",
            },
        ],
        final_candidates={"put": [], "call": []},
        sealed_at=NOW,
    )

    assert payload["opening_status"] == "market_closed"
    assert payload["ranked_candidates"] == []
    assert {
        row["strategy_mode"]: row["strategy_status"]
        for row in payload["strategy_results"]
    } == {"call": "not_applicable", "put": "data_unavailable"}


def test_partial_scope_is_explicit_but_optional_metrics_do_not_make_partial(
    tmp_path: Path,
) -> None:
    candidate = _candidate(
        contract_symbol="NVDA260918P00090000",
        period_return=0.04,
    )
    candidate["open_interest"] = None
    candidate["volume"] = None
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
        final_candidates={"put": [candidate], "call": []},
        sealed_at=NOW,
    )

    assert payload["opening_status"] == "candidates_found"
    result_by_mode = {
        row["strategy_mode"]: row for row in payload["strategy_results"]
    }
    assert result_by_mode["put"]["strategy_status"] == "candidates_found"
    assert candidate_universe_summary(payload) == {
        "status": "partial",
        "affected_scopes": [
            {
                "symbol": "NVDA",
                "strategy_mode": "call",
                "reason_code": "failed",
            }
        ],
    }


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


def test_immutable_conflict_and_terminal_latest_pointer_fail_closed(
    tmp_path: Path,
) -> None:
    payload = _seal(tmp_path)
    _seal(tmp_path)
    _publish_opening_manifest(tmp_path, payload)
    pointer = tmp_path / "output_shared" / "state" / "last_run_dir.txt"
    pointer.parent.mkdir(parents=True)
    pointer.write_text("output_runs/run-1\n", encoding="utf-8")
    bundle = load_latest_candidate_snapshot_bundle(base=tmp_path, account="lx")
    assert bundle["manifest"]["run_id"] == "run-1"
    assert bundle["owners"]["opening"]["run_id"] == "run-1"

    pointer.write_text("output_runs/missing-run\n", encoding="utf-8")
    with pytest.raises(CandidateSnapshotManifestError, match="unavailable"):
        load_latest_candidate_snapshot_bundle(base=tmp_path, account="lx")

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
                "final_candidates": {"put": []},
                "sealed_at": NOW,
            }
        )
