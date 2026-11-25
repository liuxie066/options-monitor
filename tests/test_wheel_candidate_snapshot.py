from __future__ import annotations

import json
from pathlib import Path

import pytest

from domain.domain.decision_state_fingerprint import canonical_sha256
from src.application.wheel.candidate_snapshot import (
    WHEEL_CANDIDATE_SNAPSHOT_FILE_V1,
    WHEEL_CANDIDATE_SNAPSHOT_SCHEMA_V1,
    WHEEL_CANDIDATE_SNAPSHOT_SCHEMA_V2,
    WHEEL_CANDIDATE_SNAPSHOT_SCHEMA_V3,
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


def _scope(*, direction: str = "call", candidate_count: int = 1) -> dict:
    return {"symbol": "NVDA", "direction": direction, "status": "completed", "candidate_count": candidate_count}


def _seal(base: Path, run_id: str = "run-1", **overrides) -> dict:
    return seal_wheel_candidate_snapshot(
        base=base,
        run_id=run_id,
        account="lx",
        market="us",
        account_config_sha256="a" * 64,
        strategy_policy_sha256="b" * 64,
        dependencies=_dependencies(),
        run_mode={"scan_mode": "standard", "executable": True},
        **overrides,
    )


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


@pytest.mark.parametrize("reason,expected", [
    ("wheel_capacity_fully_committed_or_reserved", "no_candidate"),
    ("wheel_disabled", "no_candidate"),
    ("wheel_config_mismatch", "partial_data"),
    ("coverage_quantity_unavailable", "partial_data"),
])
def test_snapshot_normal_skips_and_legacy_status_validation(tmp_path, reason, expected):
    from src.application.wheel.candidate_snapshot import validate_wheel_candidate_snapshot
    scopes = [_scope(candidate_count=0), {"symbol": "AMD", "direction": "call",
              "status": "not_applicable", "reason_code": reason, "candidate_count": 0}]
    payload = _seal(tmp_path, scope_results=scopes, batches=[])
    assert payload["opening_status"] == expected
    assert load_wheel_candidate_snapshot(base=tmp_path, run_id="run-1", account="lx") == payload
    legacy = {k: v for k, v in payload.items() if k not in {"status_policy_version", "content_sha256"}}
    legacy["opening_status"] = "partial_data"
    legacy["content_sha256"] = canonical_sha256(legacy)
    validate_wheel_candidate_snapshot(legacy, expected_run_id="run-1", expected_account="lx")


@pytest.mark.parametrize("field,value,reason,label", [
    ("snapshot_received_at_utc", None, "option_snapshot_receipt_missing", "期权报价接收时间缺失或无效"),
    ("snapshot_received_at_utc", "2026-04-01T15:00:01+00:00", "option_snapshot_receipt_in_future", "期权报价接收时间晚于决策时间"),
    ("snapshot_received_at_utc", "2026-04-01T14:54:59+00:00", "option_snapshot_stale", "期权报价过期"),
    ("spot_observed_at_utc", None, "underlier_update_time_missing_or_invalid", "正股行情时间缺失或无效"),
    ("spot_observed_at_utc", "2026-04-01T15:00:01+00:00", "underlier_update_time_in_future", "正股行情时间晚于决策时间"),
    ("spot_observed_at_utc", "2026-04-01T14:54:59+00:00", "underlier_quote_stale", "正股行情过期"),
])
def test_decision_freshness_reason_survives_snapshot_and_brief(tmp_path, field, value, reason, label):
    from conftest import phase2_opening_row
    from domain.domain.engine.candidate_engine import CandidateCalculationError, validate_opening_contract_evidence
    from src.application.candidate_scanning import calculation_rejection, candidate_diagnostics_from_decisions
    from src.application.daily_decision_brief_renderer import render_fixed_report_text

    now = "2026-04-01T15:00:00+00:00"
    row = phase2_opening_row({"symbol": "NVDA", "option_type": "put", "multiplier": 100,
        "contract_symbol": "NVDA-PUT-100", "expiration": "2026-05-06",
        "snapshot_received_at_utc": now, "spot_observed_at_utc": now, field: value})
    with pytest.raises(CandidateCalculationError) as caught:
        validate_opening_contract_evidence(row, mode="put", now_utc=now)
    assert caught.value.reason == "evidence_unavailable"
    assert caught.value.metric_value["reason_code"] == reason
    decision = {"accepted": False, "normalized_input": row,
                "rejects": [calculation_rejection(row, caught.value.to_payload())]}
    diagnostics = candidate_diagnostics_from_decisions([decision, decision])
    assert diagnostics == [{"outcome": "unavailable", "reason_code": reason,
                            "expiration": "2026-05-06", "contract_count": 1}]
    batch = {**_batch(), "candidate_diagnostics": diagnostics, "candidate_evidence_reason": "partial_data"}
    _seal(tmp_path, scope_results=[_scope()], batches=[batch])
    loaded = load_wheel_candidate_snapshot(base=tmp_path, run_id="run-1", account="lx")
    assert loaded["batches"][0]["candidate_diagnostics"] == diagnostics
    message = render_fixed_report_text({"account": "lx", "market": "US", "wheel_batches": loaded["batches"]})
    assert label in message
    assert "evidence_unavailable" not in message


def test_snapshot_rejects_invalid_diagnostic_count(tmp_path):
    batch = _batch()
    batch["candidate_diagnostics"] = [{"outcome": "unavailable", "reason_code": "ask_missing_or_invalid",
                                      "expiration": "2026-05-06", "contract_count": True}]
    with pytest.raises(WheelCandidateSnapshotError, match="diagnostics"):
        _seal(tmp_path, scope_results=[_scope()], batches=[batch])


def test_normal_waiting_batch_raw_candidates_do_not_create_opening_opportunity(tmp_path):
    batch = {**_batch(final=False), "candidate_status": "not_applicable", "reason_code": "wheel_call_open"}
    payload = _seal(tmp_path, scope_results=[
        {**_scope(candidate_count=0), "status": "not_applicable", "reason_code": "wheel_call_open"},
        {**_scope(candidate_count=0), "symbol": "AMD", "reason_code": "no_candidate"},
    ], batches=[batch])
    assert batch["raw_candidates"]
    assert payload["opening_status"] == "no_candidate"


@pytest.mark.parametrize("detail", [None, "Required-data scan retained covered expiration rows."])
@pytest.mark.parametrize("has_candidate", [True, False])
def test_wheel_status_detail_roundtrips_without_changing_candidate_decisions(
    tmp_path: Path, detail: str | None, has_candidate: bool,
) -> None:
    for base in (tmp_path / "without-detail", tmp_path / "observed"):
        base.mkdir()
    scope = _scope(candidate_count=int(has_candidate))
    batches = [_batch()] if has_candidate else []
    baseline = _seal(tmp_path / "without-detail", scope_results=[scope], batches=batches)
    payload = _seal(
        tmp_path / "observed",
        scope_results=[{**scope, **({"detail": detail} if detail is not None else {})}],
        batches=batches,
    )

    assert payload["opening_status"] == ("candidates_found" if has_candidate else "no_candidate")
    assert payload["scope_results"][0]["status"] == "completed"
    if detail is None:
        assert "reason_detail" not in payload["scope_results"][0]
    else:
        assert payload["scope_results"][0]["reason_detail"] == detail
    assert payload == load_wheel_candidate_snapshot(
        base=tmp_path / "observed", run_id="run-1", account="lx",
    )
    assert payload["batches"] == baseline["batches"]
    assert payload["capacity_allocations"] == baseline["capacity_allocations"]
    assert [
        {key: value for key, value in row.items() if key != "reason_detail"}
        for row in payload["scope_results"]
    ] == baseline["scope_results"]


def test_wheel_candidate_snapshot_seals_one_account_run_owner(tmp_path: Path) -> None:
    payload = _seal(
        tmp_path,
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
    assert payload["schema_version"] == WHEEL_CANDIDATE_SNAPSHOT_SCHEMA_V3
    assert payload["opening_status"] == "candidates_found"
    assert load_wheel_candidate_snapshot(base=tmp_path, run_id="run-1", account="lx") == payload


def test_wheel_candidate_snapshot_rejects_final_candidate_not_from_raw_top(tmp_path: Path) -> None:
    batch = _batch()
    batch["final_candidate"] = {**_candidate(), "candidate_id": "other", "final_candidate_id": "other"}
    with pytest.raises(WheelCandidateSnapshotError, match="allocation"):
        _seal(
            tmp_path,
            scope_results=[_scope()],
            batches=[batch],
        )


def test_wheel_candidate_snapshot_rejects_positive_grant_without_final_candidate(
    tmp_path: Path,
) -> None:
    batch = _batch(final=False)
    batch["granted_contracts"] = 1

    with pytest.raises(WheelCandidateSnapshotError, match="grant requires final candidate"):
        _seal(
            tmp_path,
            scope_results=[_scope()],
            batches=[batch],
        )


def test_wheel_candidate_snapshot_accepts_rejected_candidate_with_zero_grant(
    tmp_path: Path,
) -> None:
    batch = _batch(final=False)
    batch["reason_code"] = "wheel_capacity_grant_candidate_rejected"

    payload = _seal(
        tmp_path,
        scope_results=[_scope()],
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
    payload = _seal(
        tmp_path,
        scope_results=[_scope(candidate_count=0), _scope(direction="put", candidate_count=0)],
        batches=[call_batch, put_batch],
    )

    assert [(row["symbol"], row["direction"]) for row in payload["scope_results"]] == [
        ("NVDA", "call"),
        ("NVDA", "put"),
    ]


def test_wheel_candidate_snapshot_loader_adapts_legacy_file_location(
    tmp_path: Path,
) -> None:
    payload = _seal(
        tmp_path,
        run_id="run-v2",
        scope_results=[_scope(candidate_count=0)],
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
                if key not in {"direction", "wheel_branch_id", "batch_generation_hash"}
            },
            "batch_generation_hash": row["batch_generation_hash"],
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

    with pytest.raises(WheelCandidateSnapshotError, match="artifact_version_mismatch"):
        load_wheel_candidate_snapshot(
            base=tmp_path,
            run_id="run-v1",
            account="lx",
        )


def test_wheel_candidate_snapshot_rejects_mixed_v1_v2_files(tmp_path: Path) -> None:
    _seal(
        tmp_path,
        run_id="run-mixed",
        scope_results=[_scope(candidate_count=0)],
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
    payload = _seal(
        tmp_path,
        run_id="run-put",
        scope_results=[_scope(direction="put")],
        batches=[
            {
                "account": "lx",
                "symbol": "NVDA",
                "wheel_branch_id": "branch-put-1",
                "direction": "put",
                "batch_generation_hash": "1" * 64,
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
    payload = _seal(
        tmp_path,
        run_id="run-ordinary-cc",
        scope_results=[_scope(candidate_count=0)],
        batches=[_batch(final=False)],
        capacity_allocations=[ordinary],
    )

    assert payload["capacity_allocations"] == [ordinary]


def test_confirmation_reads_original_hash_bound_cash_and_ledger_fx(tmp_path, monkeypatch):
    from hashlib import sha256
    from cash_evidence_helpers import cash_config, cash_portfolio
    from src.application.tick_run_workspace import publish_account_run_config
    from src.application.wheel.candidate_snapshot import load_wheel_candidate_cash_fact
    import src.application.futu_portfolio_context as futu

    monkeypatch.setattr(futu, "fetch_futu_portfolio_context", lambda **kw: pytest.fail("confirmation must not fetch"))
    config = cash_config()
    config["portfolio"]["account"] = "lx"
    authority = publish_account_run_config(base=tmp_path, run_id="run-1", account="lx", config=config)
    state = tmp_path / "output_runs/run-1/accounts/lx/state"
    portfolio = cash_portfolio({"cash_by_currency": {"USD": 123},
        "source_observed_at": "2026-01-01T00:00:00+00:00", "exchange_rates": {"USD": 999}})
    deps = _dependencies()
    for kind, name, payload in (("portfolio", "portfolio_context.json", portfolio),
                               ("ledger", "option_positions_context.json", {"exchange_rates": {"USD": 7}})):
        path = state / name
        path.write_text(json.dumps(payload))
        deps = [{"kind": kind, "relpath": str(path.relative_to(tmp_path)),
                 "sha256": sha256(path.read_bytes()).hexdigest()} if row["kind"] == kind else row for row in deps]
    snapshot = {"run_id": "run-1", "account": "lx", "account_config_sha256": authority.account_config_sha256,
                "dependencies": deps}
    fact = load_wheel_candidate_cash_fact(base=tmp_path, snapshot=snapshot)
    assert fact["cash_by_currency"] == {"USD": 123}
    assert fact["cash_snapshot"] == portfolio["cash_snapshot"]
    assert fact["fx_snapshot"] == {"USD": 7}
    assert fact["cash_evidence"]["cash_source_observed_at"] == "2026-01-01T00:00:00+00:00"
    assert "cash_evidence_error" in load_wheel_candidate_cash_fact(base=tmp_path / "other-runtime", snapshot=snapshot)
    (state / "portfolio_context.json").write_text("{}")
    assert "hash" in load_wheel_candidate_cash_fact(base=tmp_path, snapshot=snapshot)["cash_evidence_error"]
