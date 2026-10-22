from __future__ import annotations

import hashlib
import json
from copy import deepcopy
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
    load_candidate_snapshot_bundle,
    load_latest_candidate_snapshot_bundle,
    publish_candidate_snapshot_manifest,
)
from src.application.opend_symbol_outputs import SUCCESS_EMPTY_REASON_CODES
from src.application.strategy_scan_status import (
    publish_strategy_scan_status,
    publish_strategy_scan_status_index,
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
        "delta": -0.3,
        "delta_source": "opend_market_snapshot.option_delta",
        "delta_unit": "fraction",
        "delta_reason_code": None,
        "snapshot_received_at_utc": NOW.isoformat(),
        "decision_now_utc": NOW.isoformat(),
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


def _physical_account(account: str, market: str) -> dict:
    return {
        "status": "available",
        "logical_account": account,
        "futu_account_id": "12345",
        "trd_env": "REAL",
        "market": market,
        "source": "opend",
    }


def _scan_status(
    symbol: str,
    strategy_mode: str,
    status: str,
    *,
    reason: str | None = None,
    snapshot_id: str | None = None,
    receipt: str | None = None,
) -> dict:
    return {
        "symbol": symbol,
        "strategy_mode": strategy_mode,
        "status": status,
        "reason": reason,
        "quote_snapshot_id": snapshot_id,
        "quote_receipt_relpath": receipt,
    }


def _seal_snapshot(
    base: Path,
    *,
    run_id: str,
    scan_statuses: list[dict],
    final_candidates: dict,
    account: str = "lx",
    market: str = "US",
    **extra: object,
) -> dict:
    dependencies = _dependencies(base, run_id, account)
    if isinstance(extra.get("capacity_fx_evidence"), dict):
        source = _capacity_fx_source()
        source_hash = canonical_sha256(source)
        next(d for d in dependencies if d["kind"] == "fx")["sha256"] = source_hash
        (base / "output_runs" / run_id / "state" / "current_exchange_rates.v1.json").write_text(json.dumps({
            "schema_version": 1, "run_id": run_id, "snapshot": source, "snapshot_sha256": source_hash,
        }))
    return seal_opening_candidate_snapshot(
        base=base,
        run_id=run_id,
        account=account,
        market=market,
        physical_account=_physical_account(account, market),
        account_config_sha256="a" * 64,
        strategy_policy_sha256="b" * 64,
        dependencies=dependencies,
        scan_statuses=scan_statuses,
        final_candidates=final_candidates,
        run_mode={"scan_mode": "standard", "executable": True},
        sealed_at=NOW,
        **extra,
    )


def _account_dir(base: Path, run_id: str, account: str) -> Path:
    return base / "output_runs" / run_id / "accounts" / account


def _snapshot_path(base: Path, run_id: str = "run-1", account: str = "lx") -> Path:
    return _account_dir(base, run_id, account) / "state" / OPENING_CANDIDATE_SNAPSHOT_FILE


def _compact_bytes(payload: dict) -> bytes:
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return (text + "\n").encode("utf-8")


def _pretty_bytes(payload: dict) -> bytes:
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
    return (text + "\n").encode("utf-8")


def _explain_candidates(tmp_path: Path, *, run_id: str, **extra: object) -> tuple[dict, list, dict]:
    from src.application.agent_tools.candidate_filter_impl import candidate_filter_explain_tool

    return candidate_filter_explain_tool(
        {"runtime_root": str(tmp_path), "run_id": run_id, "account": "lx", "symbol": "NVDA", **extra},
        repo_base=lambda: tmp_path,
        mask_path=lambda path: str(path) if path else None,
    )


def _seal(base: Path, *, run_id: str = "run-1", account: str = "lx") -> dict:
    return _seal_snapshot(
        base,
        run_id=run_id,
        account=account,
        scan_statuses=[
            _scan_status(
                "NVDA",
                "put",
                "completed",
                snapshot_id="c" * 64,
                receipt="quotes/NVDA/receipt.json",
            )
        ],
        final_candidates={
            "put": [
                _candidate(contract_symbol="NVDA260918P00085000", period_return=0.03),
                _candidate(contract_symbol="NVDA260918P00090000", period_return=0.04),
            ]
        },
    )


def _publish_opening_manifest(base: Path, payload: dict) -> dict:
    account_dir = _account_dir(base, str(payload["run_id"]), str(payload["account"]))
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
    publish_strategy_scan_status_index(
        report_dir=account_dir,
        run_id=str(payload["run_id"]),
        account=str(payload["account"]),
        account_config_sha256=str(payload["account_config_sha256"]),
        expected=expected,
        run_mode={"scan_mode": "standard", "executable": True},
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
    snapshot_path = _snapshot_path(tmp_path)
    first_bytes = snapshot_path.read_bytes()
    expected_compact_bytes = _compact_bytes(first)
    pretty_bytes = _pretty_bytes(first)

    second = _seal(tmp_path)

    assert first_bytes == expected_compact_bytes
    assert len(first_bytes) < len(pretty_bytes)
    assert second["content_sha256"] == first["content_sha256"]
    assert second["ranked_candidates"] == first["ranked_candidates"]
    assert snapshot_path.read_bytes() == first_bytes


def test_pretty_snapshot_with_matching_manifest_remains_loadable(
    tmp_path: Path,
) -> None:
    payload = _seal(tmp_path)
    snapshot_path = _snapshot_path(tmp_path)
    compact_bytes = snapshot_path.read_bytes()
    pretty_bytes = _pretty_bytes(payload)
    assert pretty_bytes != compact_bytes
    snapshot_path.write_bytes(pretty_bytes)

    manifest = _publish_opening_manifest(tmp_path, payload)
    bundle = load_candidate_snapshot_bundle(
        base=tmp_path,
        run_id="run-1",
        account="lx",
    )

    opening_entry = next(
        item
        for item in manifest["owner_snapshots"]
        if item["candidate_owner"] == "opening"
    )
    assert opening_entry["sha256"] == hashlib.sha256(pretty_bytes).hexdigest()
    assert opening_entry["content_sha256"] == payload["content_sha256"]
    assert bundle["owners"]["opening"] == payload


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
    payload = _seal(tmp_path)
    manifest = _publish_opening_manifest(tmp_path, payload)
    data, warnings, meta = _explain_candidates(tmp_path, run_id="run-1", function="sell_put")

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

    _seal(tmp_path)

    with pytest.raises(AgentToolError, match="manifest is unavailable"):
        _explain_candidates(tmp_path, run_id="run-1")


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
    snapshot_path = _snapshot_path(tmp_path)
    snapshot_path.write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(CandidateSnapshotManifestError, match="snapshot is invalid"):
        _publish_opening_manifest(tmp_path, legacy)


def test_empty_result_is_a_sealed_no_candidate_snapshot(tmp_path: Path) -> None:
    payload = _seal_snapshot(
        tmp_path,
        run_id="run-empty",
        scan_statuses=[
            _scan_status("NVDA", "put", "completed", reason="no_expirations",
                         snapshot_id="empty-quote", receipt="quotes/NVDA/receipt.json")
        ],
        final_candidates={"put": []},
    )

    assert payload["opening_status"] == "no_candidate"
    assert payload["ranked_candidates"] == []
    assert _snapshot_path(tmp_path, "run-empty").is_file()
    _publish_opening_manifest(tmp_path, payload)

    data, warnings, _meta = _explain_candidates(tmp_path, run_id="run-empty", function="sell_put")
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
            [_scan_status("3690.HK", "call", "not_applicable", reason="covered_call_underlying_not_held")],
        ),
        (
            "run-shared-call-mixed",
            [
                _scan_status("0700.HK", "call", "completed", reason="no_candidate"),
                _scan_status("3690.HK", "call", "not_applicable", reason="covered_call_underlying_not_held"),
                _scan_status("9992.HK", "call", "completed", reason="no_candidate"),
            ],
        ),
    ],
)
def test_shared_config_call_without_account_holding_is_a_legal_zero_candidate(
    tmp_path: Path,
    run_id: str,
    call_statuses: list[dict],
) -> None:
    payload = _seal_snapshot(
        tmp_path,
        run_id=run_id,
        account="sy",
        market="HK",
        scan_statuses=[_scan_status("0700.HK", "put", "completed", reason="no_candidate"), *call_statuses],
        final_candidates={"put": [], "call": []},
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
    payload = _seal_snapshot(
        tmp_path,
        run_id=run_id,
        account="sy",
        market="HK",
        scan_statuses=[
            _scan_status("0700.HK", "put", "completed", reason="no_candidate"),
            _scan_status("0700.HK", "call", "completed", reason="no_candidate"),
            _scan_status("3690.HK", "call", "not_applicable", reason="covered_call_portfolio_context_unavailable"),
        ],
        final_candidates={"put": [], "call": []},
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

    payload = _seal_snapshot(
        tmp_path,
        run_id="run-input-invalid",
        scan_statuses=[_scan_status("NVDA", "put", "completed", reason="no_candidate")],
        final_candidates={"put": []},
        candidate_evaluations={"put": [{"normalized_input": candidate, "opening_decision": opening_decision}]},
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
                "reason_code": "term_matched_rv_unavailable",
                "scope": "contract",
                "contract_symbol": "NVDA260918P00090000",
                "expiration": "2026-09-18",
            }
        ],
    }


@pytest.mark.parametrize("reverse_rejects", [False, True])
def test_specific_contract_gap_replaces_only_generic_strategy_reason(
    reverse_rejects: bool,
) -> None:
    nvda_rejects = [
        {
            "reason": "input_missing",
        },
        {
            "reason": "input_invalid",
            "metric_value": {
                "reason_code": "term_matched_rv_unavailable",
            },
        },
    ]
    if reverse_rejects:
        nvda_rejects.reverse()
    snapshot = {
        "scope_results": [
            {
                "scope": "strategy",
                "symbol": "NVDA",
                "strategy_mode": "put",
                "status": "completed",
                "reason_code": "partial_data",
            },
            {
                "scope": "contract",
                "symbol": "NVDA",
                "strategy_mode": "put",
                "status": "rejected",
                "reason_codes": ["input_invalid", "input_missing"],
                "rejects": nvda_rejects,
            },
            {
                "scope": "strategy",
                "symbol": "AAPL",
                "strategy_mode": "call",
                "status": "unavailable",
                "reason_code": "covered_call_portfolio_context_unavailable",
            },
            {
                "scope": "contract",
                "symbol": "AAPL",
                "strategy_mode": "call",
                "status": "rejected",
                "reason_codes": ["input_missing"],
                "rejects": [
                    {
                        "reason": "input_missing",
                        "metric_value": {
                            "reason_code": "term_matched_rv_unavailable",
                        },
                    }
                ],
            },
        ]
    }

    assert candidate_universe_summary(snapshot) == {
        "status": "partial",
        "affected_scopes": [
            {
                "symbol": "AAPL",
                "strategy_mode": "call",
                "reason_code": "covered_call_portfolio_context_unavailable",
            },
            {
                "symbol": "NVDA",
                "strategy_mode": "put",
                "reason_code": "term_matched_rv_unavailable",
            },
        ],
    }


@pytest.mark.parametrize("reverse_contracts", [False, True])
def test_contract_gap_reason_is_deterministic_across_contract_scopes(
    reverse_contracts: bool,
) -> None:
    contracts = [
        {
            "scope": "contract",
            "symbol": "NVDA",
            "strategy_mode": "put",
            "status": "rejected",
            "reason_codes": ["input_invalid"],
            "rejects": [
                {
                    "reason": "input_invalid",
                    "metric_value": {
                        "reason_code": "term_matched_rv_unavailable",
                    },
                }
            ],
        },
        {
            "scope": "contract",
            "symbol": "NVDA",
            "strategy_mode": "put",
            "status": "rejected",
            "reason_codes": ["input_invalid"],
            "rejects": [
                {
                    "reason": "input_invalid",
                    "metric_value": {
                        "reason_code": "option_multiplier_conflict",
                    },
                }
            ],
        },
    ]
    if reverse_contracts:
        contracts.reverse()
    snapshot = {
        "scope_results": [
            {
                "scope": "strategy",
                "symbol": "NVDA",
                "strategy_mode": "put",
                "status": "completed",
                "reason_code": "partial_data",
            },
            *contracts,
        ]
    }

    assert candidate_universe_summary(snapshot) == {
        "status": "partial",
        "affected_scopes": [
            {
                "symbol": "NVDA",
                "strategy_mode": "put",
                "reason_code": "option_multiplier_conflict",
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
    payload = _seal_snapshot(
        tmp_path,
        run_id=run_id,
        scan_statuses=[_scan_status("NVDA", "put", "completed", reason=scan_reason)],
        final_candidates={"put": []},
        candidate_evaluations={
            "put": [
                {"normalized_input": unavailable_candidate, "opening_decision": unavailable_decision},
                {"normalized_input": policy_rejected_candidate, "opening_decision": policy_rejected_decision},
            ]
        },
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

    candidate = _candidate(
        contract_symbol="NVDA260918P00090000",
        period_return=0.01,
    )
    opening_decision = evaluate_opening_candidate_policy(candidate, mode="put")
    assert opening_decision["accepted"] is False

    payload = _seal_snapshot(
        tmp_path,
        run_id="run-rejected",
        scan_statuses=[_scan_status("NVDA", "put", "completed")],
        final_candidates={"put": []},
        candidate_evaluations={
            "put": [{"normalized_input": candidate, "opening_decision": opening_decision}],
            "call": [],
        },
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

    data, warnings, _meta = _explain_candidates(tmp_path, run_id="run-rejected", function="sell_put")

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
        _seal_snapshot(
            tmp_path,
            run_id="run-cross-symbol",
            scan_statuses=[_scan_status("NVDA", "put", "completed")],
            final_candidates={"put": []},
            candidate_evaluations={"put": [{"normalized_input": candidate, "opening_decision": opening_decision}]},
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

    payload = _seal_snapshot(
        tmp_path,
        run_id="run-custom-policy",
        scan_statuses=[_scan_status("NVDA", "put", "completed")],
        final_candidates={"put": [candidate]},
    )

    assert payload["opening_status"] == "candidates_found"
    assert payload["candidate_decisions"][0]["opening_decision"]["accepted"] is True


def test_market_closed_is_sealed_as_explicit_unavailable_state(
    tmp_path: Path,
) -> None:
    payload = _seal_snapshot(
        tmp_path,
        run_id="run-closed",
        scan_statuses=[
            _scan_status("NVDA", "put", "unavailable", reason="market_closed"),
            _scan_status("NVDA", "call", "not_applicable"),
        ],
        final_candidates={"put": [], "call": []},
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
    payload = _seal_snapshot(
        tmp_path,
        run_id="run-partial",
        scan_statuses=[
            _scan_status("NVDA", "put", "completed"),
            _scan_status("NVDA", "call", "failed"),
        ],
        final_candidates={"put": [candidate], "call": []},
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
    snapshot_path = _snapshot_path(tmp_path)
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
        _seal_snapshot(
            tmp_path,
            run_id="run-1",
            scan_statuses=[_scan_status("NVDA", "put", "completed")],
            final_candidates={"put": []},
        )


def test_missing_cny_premium_survives_seal_and_brief_as_data_gap(tmp_path):
    from domain.domain.engine import evaluate_opening_candidate_policy
    from src.application.daily_decision_brief_service import _load_opening_candidate_families
    from src.application.daily_decision_brief_renderer import _strategy_data_gap_reminders

    row = _candidate(contract_symbol="NVDA-CNY-MISSING", period_return=0.02)
    row["net_income_cny"] = None
    decision = evaluate_opening_candidate_policy(row, mode="put")
    payload = _seal_snapshot(tmp_path, run_id="run-cny-missing",
        scan_statuses=[_scan_status("NVDA", "put", "completed", reason="no_candidate")],
        final_candidates={"put": []}, candidate_evaluations={"put": [
            {"normalized_input": row, "opening_decision": decision}]})
    assert payload["opening_status"] == "data_unavailable"
    validate_opening_candidate_snapshot(payload, expected_run_id="run-cny-missing", expected_account="lx", require_current_contract=True)
    summary = candidate_universe_summary(payload)
    assert summary["affected_scopes"][0]["reason_code"] == "net_premium_cny_unavailable"
    gaps = []
    _load_opening_candidate_families(base=tmp_path, run_id="run-cny-missing", account="lx", market="US",
        source_artifacts=[], data_gaps=gaps, snapshot=payload)
    assert any(row.get("reason_code") == "net_premium_cny_unavailable" for row in gaps)
    assert any("人民币净权利金无法计算" in line for line in _strategy_data_gap_reminders({"data_gaps": gaps}))


@pytest.mark.parametrize("blocked_status", ["unavailable", "failed", "incomplete"])
@pytest.mark.parametrize("reason", ["option_close_settlement_pending", "quote_unavailable"])
@pytest.mark.parametrize("has_candidate", [False, True])
def test_one_blocked_call_scope_preserves_other_call_results(
    tmp_path, blocked_status, reason, has_candidate,
):
    from src.application.daily_decision_brief_service import _load_opening_candidate_families

    candidate = _candidate(contract_symbol="NVDA260918C00100000", period_return=0.04)
    candidate.update(
        option_type="call", strike=100, delta=0.3,
        period_net_premium_return=0.04,
        annualized_net_premium_return=0.04 * 365 / 43,
    )
    payload = _seal_snapshot(
        tmp_path, run_id="run-scoped-call", account="sy",
        scan_statuses=[
            _scan_status("FUTU", "call", blocked_status, reason=reason),
            _scan_status("NVDA", "call", "completed", reason=None if has_candidate else "no_candidate"),
            _scan_status("TCOM", "call", "not_applicable", reason="covered_call_underlying_not_held"),
        ],
        final_candidates={"call": [candidate] if has_candidate else []},
    )
    assert payload["strategy_results"][0]["strategy_status"] == (
        "candidates_found" if has_candidate else "partial_data"
    )
    assert candidate_universe_summary(payload) == {
        "status": "partial",
        "affected_scopes": [{"symbol": "FUTU", "strategy_mode": "call", "reason_code": reason}],
    }
    gaps = []
    _puts, _put_available, calls, call_available, accepted = _load_opening_candidate_families(
        base=tmp_path, run_id="run-scoped-call", account="sy", market="US",
        data_gaps=gaps, source_artifacts=[], snapshot=payload, applicable_modes=("call",),
    )
    assert accepted is not None
    assert call_available is True
    assert len(calls) == int(has_candidate)
    assert [gap["symbol"] for gap in gaps] == ["FUTU"]
    assert all(gap["reason"] == "opening_candidate_strategy_partial_data" for gap in gaps)


def test_all_call_scopes_blocked_remain_unavailable(tmp_path):
    payload = _seal_snapshot(
        tmp_path, run_id="run-all-call-blocked",
        scan_statuses=[
            _scan_status("FUTU", "call", "unavailable", reason="option_close_settlement_pending"),
            _scan_status("NVDA", "call", "unavailable", reason="quote_unavailable"),
        ],
        final_candidates={"call": []},
    )
    assert payload["strategy_results"][0]["strategy_status"] == "data_unavailable"


def test_blocked_call_with_only_unheld_siblings_is_still_symbol_scoped(tmp_path):
    payload = _seal_snapshot(
        tmp_path, run_id="run-call-unheld-siblings",
        scan_statuses=[
            _scan_status("FUTU", "call", "unavailable", reason="option_close_settlement_pending"),
            _scan_status("NVDA", "call", "not_applicable", reason="covered_call_underlying_not_held"),
        ],
        final_candidates={"call": []},
    )
    assert payload["strategy_results"][0]["strategy_status"] == "partial_data"
    assert candidate_universe_summary(payload)["affected_scopes"] == [
        {"symbol": "FUTU", "strategy_mode": "call", "reason_code": "option_close_settlement_pending"}
    ]


def _capacity_fx_source():
    return {"schema_version": 2, "pairs": {
        pair: {"rate": rate, "source": "tencent_quote",
               "quote_at_utc": "2026-09-30T06:00:00+00:00",
               "observed_at_utc": "2026-09-30T06:00:00+00:00"}
        for pair, rate in (("USDCNY", 7.2), ("HKDCNY", 0.92))
    }}


def _capacity_fx_evidence(*, marker=..., stale=False):
    from src.infrastructure.exchange_rates import project_exchange_rate_snapshot

    evaluated_at = datetime(2026, 10, 8 if stale else 2, 1, 43, tzinfo=timezone.utc)
    observation = project_exchange_rate_snapshot(
        _capacity_fx_source(), purpose="capacity", now=evaluated_at,
    )
    evidence = {
        "schema_version": 1, "evaluated_at_utc": evaluated_at.isoformat(),
        "observation_status": "unavailable_stale" if stale else "ready",
        "observation": observation, "observation_sha256": canonical_sha256(observation),
        "usd_per_cny": None if stale else 1 / 7.2, "cny_per_hkd": None if stale else 0.92,
        "run_fx_snapshot_sha256": canonical_sha256(_capacity_fx_source()), "marker_present": marker is not ...,
    }
    if marker is not ...:
        evidence["marker_value"] = marker
    return evidence


def _validate_changed_evidence(payload):
    payload["content_sha256"] = canonical_sha256({k: v for k, v in payload.items() if k != "content_sha256"})
    validate_opening_candidate_snapshot(payload, expected_run_id=payload["run_id"], expected_account=payload["account"])


def test_ranking_inputs_record_actual_preaggregate_order(tmp_path, monkeypatch):
    from src.application import opening_candidate_snapshot as owner

    rows = [
        {**_candidate(contract_symbol=s + str(k), period_return=0.03 + k / 1000), "symbol": s}
        for s in ("NVDA", "AMD") for k in (1, 2)
    ]
    seen = []
    original = owner.rank_candidates

    def rank(source_rows, **kwargs):
        seen.append(deepcopy(source_rows))
        return original(source_rows, **kwargs)

    monkeypatch.setattr(owner, "rank_candidates", rank)
    payload = _seal_snapshot(
        tmp_path, run_id="ordered", final_candidates={"put": rows},
        scan_statuses=[_scan_status(s, "put", "completed") for s in ("NVDA", "AMD")],
    )
    inputs = payload["ranking_inputs"]["put"]
    by_id = {d["candidate_id"]: d["normalized_input"] for d in payload["candidate_decisions"]}
    assert [by_id[cid] for cid in inputs["candidate_ids"]] == seen[0]
    assert [row["contract_symbol"] for row in seen[0]] == [r["contract_symbol"] for r in rows]
    assert inputs["profile"] == "current_tie_break"
    assert [r["facts"]["contract_symbol"] for r in payload["ranked_candidates"]] != [r["contract_symbol"] for r in rows]


@pytest.mark.parametrize("invalid", ["null", "mode", "duplicate", "missing", "foreign", "profile", "shape"])
def test_ranking_inputs_invalid_presence_never_falls_back(tmp_path, invalid):
    payload = _seal(tmp_path)
    inputs = payload["ranking_inputs"]
    if invalid == "null":
        payload["ranking_inputs"] = None
    elif invalid == "mode":
        inputs["call"] = inputs.pop("put")
    elif invalid == "duplicate":
        inputs["put"]["candidate_ids"][1] = inputs["put"]["candidate_ids"][0]
    elif invalid == "missing":
        inputs["put"]["candidate_ids"].pop()
    elif invalid == "foreign":
        inputs["put"]["candidate_ids"][0] = "other-account-candidate"
    elif invalid == "profile":
        inputs["put"]["profile"] = "concentration_first"
    else:
        inputs["put"]["candidate_ids"] = "not-a-list"
    with pytest.raises(OpeningCandidateSnapshotError, match="ranking"):
        _validate_changed_evidence(payload)


def test_optional_evidence_legacy_absence_remains_readable(tmp_path):
    payload = _seal(tmp_path)
    del payload["ranking_inputs"]
    assert "capacity_fx_evidence" not in payload
    _validate_changed_evidence(payload)


@pytest.mark.parametrize("stale,marker", [(False, ...), (False, None), (False, "ready"), (True, "unavailable_stale")])
def test_capacity_fx_seals_and_replays_without_current_time(tmp_path, monkeypatch, stale, marker):
    from src.infrastructure import exchange_rates as fx
    from src.application.opening_candidate_snapshot import validate_capacity_fx_evidence

    evidence = _capacity_fx_evidence(marker=marker, stale=stale)
    payload = _seal_snapshot(
        tmp_path, run_id="fx", scan_statuses=[_scan_status("NVDA", "put", "completed")],
        final_candidates={"put": []}, capacity_fx_evidence=evidence,
    )
    assert payload["capacity_fx_evidence"] == evidence
    assert payload["ranking_inputs"] == {"put": {"candidate_ids": [], "profile": "current_tie_break"}}
    monkeypatch.setattr(fx, "_utc_now", lambda: pytest.fail("sealed FX validation must not read current time"))
    monkeypatch.setattr(fx, "fetch_market_exchange_rates", lambda: pytest.fail("no provider"))
    validate_capacity_fx_evidence(evidence, expected_run_fx_snapshot_sha256=evidence["run_fx_snapshot_sha256"])
    assert load_opening_candidate_snapshot(base=tmp_path, run_id="fx", account="lx") == payload


@pytest.mark.parametrize("field,value", [
    ("schema_version", True), ("evaluated_at_utc", "2026-10-02T01:43:00"),
    ("evaluated_at_utc", None), ("observation", []), ("observation_sha256", "e" * 64),
    ("run_fx_snapshot_sha256", "e" * 64), ("usd_per_cny", True), ("usd_per_cny", -0.1),
    ("usd_per_cny", float("inf")), ("usd_per_cny", 0.2), ("cny_per_hkd", None),
    ("observation_status", "unavailable"), ("marker_present", 1), ("marker_value", "ready"),
])
def test_capacity_fx_validator_rejects_malformed_evidence(field, value):
    from src.application.opening_candidate_snapshot import validate_capacity_fx_evidence

    evidence = _capacity_fx_evidence()
    evidence[field] = value
    with pytest.raises(OpeningCandidateSnapshotError):
        validate_capacity_fx_evidence(evidence, expected_run_fx_snapshot_sha256=canonical_sha256(_capacity_fx_source()))


@pytest.mark.parametrize("invalid", ["null", "marker_missing", "marker_type", "time", "rates", "digest"])
def test_capacity_fx_invalid_presence_is_integrity_error(tmp_path, invalid):
    payload = _seal(tmp_path)
    evidence = _capacity_fx_evidence()
    payload["capacity_fx_evidence"] = evidence
    if invalid == "null":
        payload["capacity_fx_evidence"] = None
    elif invalid == "marker_missing":
        evidence["marker_present"] = True
    elif invalid == "marker_type":
        evidence.update(marker_present=True, marker_value=0)
    elif invalid == "time":
        evidence["observation"]["evaluated_at_utc"] = "2026-10-02T02:43:00+00:00"
    elif invalid == "rates":
        evidence["observation"]["rates"]["USDCNY"] = 8
        evidence["usd_per_cny"] = 1 / 8
    else:
        evidence["observation"]["rates"]["USDCNY"] = 8
    if invalid in {"time", "rates"}:
        evidence["observation_sha256"] = canonical_sha256(evidence["observation"])
    with pytest.raises(OpeningCandidateSnapshotError):
        _validate_changed_evidence(payload)


def test_capacity_fx_evidence_tamper_breaks_snapshot_hash(tmp_path):
    payload = _seal_snapshot(
        tmp_path, run_id="fx", scan_statuses=[_scan_status("NVDA", "put", "completed")],
        final_candidates={"put": []}, capacity_fx_evidence=_capacity_fx_evidence(),
    )
    payload["capacity_fx_evidence"]["marker_present"] = True
    with pytest.raises(OpeningCandidateSnapshotError, match="content hash"):
        validate_opening_candidate_snapshot(payload, expected_run_id="fx", expected_account="lx")


def test_new_seal_rejects_explicit_null_capacity_fx_evidence(tmp_path):
    with pytest.raises(OpeningCandidateSnapshotError, match="capacity FX evidence shape"):
        _seal_snapshot(
            tmp_path, run_id="fx-null", scan_statuses=[_scan_status("NVDA", "put", "completed")],
            final_candidates={"put": []}, capacity_fx_evidence=None,
        )


def test_capacity_fx_snapshot_dependency_generation_is_semantically_bound(tmp_path):
    payload = _seal(tmp_path)
    payload["capacity_fx_evidence"] = _capacity_fx_evidence()
    next(d for d in payload["dependencies"] if d["kind"] == "fx")["sha256"] = "e" * 64
    with pytest.raises(OpeningCandidateSnapshotError, match="capacity FX run dependency mismatch"):
        _validate_changed_evidence(payload)


def test_ranking_inputs_reject_valid_candidate_from_other_account(tmp_path):
    payload = _seal(tmp_path, account="lx")
    foreign = _seal(tmp_path, account="sy")
    payload["ranking_inputs"]["put"]["candidate_ids"][0] = foreign["ranking_inputs"]["put"]["candidate_ids"][0]
    with pytest.raises(OpeningCandidateSnapshotError, match="ranking inputs accepted set mismatch"):
        _validate_changed_evidence(payload)


@pytest.mark.parametrize("damage", ["missing", "corrupt", "wrong_run", "wrong_digest", "different_source"])
def test_capacity_fx_loader_requires_actual_bound_run_source(tmp_path, damage):
    _seal_snapshot(
        tmp_path, run_id="fx", scan_statuses=[_scan_status("NVDA", "put", "completed")],
        final_candidates={"put": []}, capacity_fx_evidence=_capacity_fx_evidence(),
    )
    source_path = tmp_path / "output_runs/fx/state/current_exchange_rates.v1.json"
    envelope = json.loads(source_path.read_text())
    if damage == "missing":
        source_path.unlink()
    elif damage == "corrupt":
        source_path.write_text("{")
    else:
        if damage == "wrong_run":
            envelope["run_id"] = "other"
        elif damage == "wrong_digest":
            envelope["snapshot_sha256"] = "e" * 64
        else:
            envelope["snapshot"]["pairs"]["USDCNY"]["rate"] = 8.0
            envelope["snapshot_sha256"] = canonical_sha256(envelope["snapshot"])
        source_path.write_text(json.dumps(envelope))
    with pytest.raises(OpeningCandidateSnapshotError, match="capacity FX run source"):
        load_opening_candidate_snapshot(base=tmp_path, run_id="fx", account="lx")


@pytest.mark.parametrize("change", ["rate", "metadata"])
def test_capacity_fx_actual_projection_rejects_self_consistent_forgery(tmp_path, change):
    from src.infrastructure.exchange_rates import project_exchange_rate_snapshot

    evidence = _capacity_fx_evidence()
    source = _capacity_fx_source()
    if change == "rate":
        source["pairs"]["USDCNY"]["rate"] = 8.0
        evidence["usd_per_cny"] = 1 / 8
    else:
        source["pairs"]["USDCNY"]["observed_at_utc"] = "2026-09-30T06:01:00+00:00"
    evidence["observation"] = project_exchange_rate_snapshot(
        source, purpose="capacity", now=datetime.fromisoformat(evidence["evaluated_at_utc"]),
    )
    evidence["observation_sha256"] = canonical_sha256(evidence["observation"])
    payload = _seal_snapshot(
        tmp_path, run_id="fx", scan_statuses=[_scan_status("NVDA", "put", "completed")],
        final_candidates={"put": []}, capacity_fx_evidence=_capacity_fx_evidence(),
    )
    payload["capacity_fx_evidence"] = evidence
    # Structural validation alone deliberately performs no filesystem lookup.
    _validate_changed_evidence(payload)
    _snapshot_path(tmp_path, "fx").write_bytes(_compact_bytes(payload))
    with pytest.raises(OpeningCandidateSnapshotError, match="observation run source mismatch"):
        load_opening_candidate_snapshot(base=tmp_path, run_id="fx", account="lx")
    with pytest.raises(OpeningCandidateSnapshotError, match="observation run source mismatch"):
        _seal_snapshot(
            tmp_path / "seal", run_id="fx", scan_statuses=[_scan_status("NVDA", "put", "completed")],
            final_candidates={"put": []}, capacity_fx_evidence=evidence,
        )


def test_capacity_fx_memory_validation_never_loads_run_source(tmp_path, monkeypatch):
    from src.application import current_fx_run

    payload = _seal_snapshot(
        tmp_path, run_id="fx", scan_statuses=[_scan_status("NVDA", "put", "completed")],
        final_candidates={"put": []}, capacity_fx_evidence=_capacity_fx_evidence(),
    )
    monkeypatch.setattr(current_fx_run, "load_run_fx_snapshot", lambda **_kw: pytest.fail("memory validation performed IO"))
    _validate_changed_evidence(payload)


@pytest.mark.parametrize("mode,sign", [("put", -1), ("call", 1)])
@pytest.mark.parametrize("unverifiable", [False, True])
def test_delta_rejection_sealed_and_explained_without_rescan(tmp_path, monkeypatch, mode, sign, unverifiable):
    from domain.domain.engine import evaluate_opening_candidate_policy
    from src.application.tool_execution import execute_tool

    candidate = _candidate(contract_symbol=f"NVDA260918{'P' if mode == 'put' else 'C'}00100000", period_return=0.04)
    candidate.update(option_type=mode, strike=100, delta=sign * 0.4001,
                     period_net_premium_return=0.04, annualized_net_premium_return=0.34,
                     policy_max_abs_delta=0.4)
    if unverifiable:
        candidate["delta_source"] = None
    decision = evaluate_opening_candidate_policy(candidate, mode=mode)
    reason = "risk_delta_unavailable" if unverifiable else "risk_delta"
    assert [item["reason"] for item in decision["rejects"]] == [reason]
    payload = _seal_snapshot(
        tmp_path, run_id="delta-rejected", scan_statuses=[_scan_status("NVDA", mode, "completed")],
        final_candidates={mode: []}, candidate_evaluations={mode: [{"normalized_input": candidate, "opening_decision": decision}]},
    )
    assert payload["opening_status"] == ("data_unavailable" if unverifiable else "no_candidate")
    recorded = payload["candidate_decisions"][0]
    assert recorded["normalized_input"]["delta"] == sign * 0.4001
    assert recorded["normalized_input"]["policy_max_abs_delta"] == 0.4
    assert recorded["normalized_input"]["snapshot_received_at_utc"] == NOW.isoformat()
    _publish_opening_manifest(tmp_path, payload)

    # Public explanation must read the sealed rejection without recomputing policy.
    def forbidden(*args, **kwargs):
        raise AssertionError("explanation must not rescan")
    monkeypatch.setattr("src.application.opening_candidate_snapshot.evaluate_opening_candidate_policy", forbidden)
    response = execute_tool("candidate_filter_explain", {
        "runtime_root": str(tmp_path), "run_id": "delta-rejected", "account": "lx", "symbol": "NVDA", "rule": reason,
    })
    assert response["ok"] is True
    events = [event for function in response["data"]["functions"] for event in function["events"] if event["is_rejection"]]
    assert len(events) == 1
    assert events[0]["threshold"] == 0.4
    assert events[0]["metric_value"]["delta"] == sign * 0.4001
    assert events[0]["metric_value"]["reason_code"] == ("delta_source_unverified" if unverifiable else None)
