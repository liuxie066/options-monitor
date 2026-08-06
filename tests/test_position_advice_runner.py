from __future__ import annotations

import json
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path

from domain.domain.position_advice_authority import (
    AuthorityResolution,
    scope_for,
)
import src.application.position_advice_reader as position_advice_reader
from src.application.opend_symbol_outputs import (
    publish_required_data_quote_snapshot,
    save_outputs,
)
from src.application.position_advice_account_sources import (
    publish_account_run_sources,
)
from src.application.opening_candidate_snapshot import (
    dependency_from_hash,
    seal_opening_candidate_snapshot,
)
from src.application.position_advice_authority_service import (
    apply_authority_change,
    build_identity_binding_evidence,
)
from src.application.position_advice_runner import run_position_advice_v2
from src.application.position_advice_reader import read_position_advice_v2
from src.application.required_data_plan_identity import (
    build_required_data_expected_fetch_contract,
)
from src.application.ledger.decision_snapshot import (
    POSITION_FACT_SNAPSHOT_CONTRACT,
    decision_state_snapshot_fingerprint,
)
from src.application.ledger.lifecycle_overlay import (
    resolve_account_lifecycle_overlay,
)


NOW = datetime(2026, 7, 27, 10, 0, tzinfo=timezone.utc)


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _snapshot() -> dict[str, object]:
    snapshot = {
        "schema_version": "decision_state_snapshot.v2",
        "fingerprint_schema_version": "decision_state_fingerprint.v2",
        "position_fact_contract_version": (
            POSITION_FACT_SNAPSHOT_CONTRACT
        ),
        "normalized_account": "lx",
        "snapshot_status": "trusted",
        "actionable": True,
        "reason_codes": [],
        "decision_state_fingerprint": "",
        "source_observed_at": (NOW + timedelta(seconds=2)).isoformat(),
        "account_position_lots": [],
        "account_lifecycle_cases": [],
        "account_lifecycle_evidence": [],
        "account_lifecycle_evidence_received_at_ms_by_id": {},
        "account_lifecycle_allocations": [],
        "account_lifecycle_source_consumptions": [],
        "account_lifecycle_timing_policies": [],
        "account_lifecycle_resolution": (
            resolve_account_lifecycle_overlay(
                account="lx",
                cases=[],
                evidence=[],
                allocations=[],
                source_claims=[],
                timing_policies=[],
                position_lots=[],
            )
        ),
        "effective_void_event_ids": [],
        "account_assigned_stock_events": [],
        "account_combo_identities": [],
        "account_combo_group_memberships": [],
    }
    snapshot["decision_state_fingerprint"] = (
        decision_state_snapshot_fingerprint(snapshot)
    )
    return snapshot


def test_account_run_facade_reuses_prepared_decision_snapshot(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from src.application import position_advice_runner as mod

    snapshot = _snapshot()
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        mod,
        "open_position_ledger",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("prepared authority must not reopen the ledger")
        ),
    )

    def _run(**kwargs):
        captured["snapshot"] = kwargs["decision_snapshot_reader"]()
        return {"status": "published"}

    monkeypatch.setattr(mod, "run_position_advice_v2", _run)

    result = mod.run_position_advice_v2_from_account_run(
        base=tmp_path,
        account_run_root=tmp_path / "account",
        account_run_id="run-1",
        account="lx",
        broker="futu",
        included_markets=["US"],
        normalized_portfolio_source="futu",
        portfolio_account_identity_hash="a" * 64,
        capacity_pool_authority_id="b" * 64,
        source_receipts=[],
        data_config_path=tmp_path / "portfolio.runtime.json",
        decision_state_snapshot_override=snapshot,
    )

    assert result == {"status": "published"}
    assert captured["snapshot"] == snapshot


def _prepare_sources(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    account_root = tmp_path / "output_runs" / "run-1" / "accounts" / "lx"
    state = account_root / "state"
    quotes = tmp_path / "required_data"
    state.mkdir(parents=True)
    quotes.mkdir()
    completed_at = NOW + timedelta(seconds=1)
    fetch_plan = {
        "symbol": "NVDA",
        "spot_reference": None,
        "require_realized_volatility": False,
        "side_plans": [],
        "merged_requests": [],
        "expiration_discovery_complete": True,
        "expiration_discovery_error": None,
        "expiration_discovery": {
            "outcome": "success_empty",
            "reason_code": "no_expirations",
            "expirations": [],
            "observed_at_utc": NOW.isoformat(),
            "completed_at_utc": completed_at.isoformat(),
            "request_identity": {
                "symbol": "NVDA",
                "underlier": "US.NVDA",
                "source": "opend",
                "host": "127.0.0.1",
                "port": 11111,
                "trading_date": NOW.date().isoformat(),
            },
            "error": None,
        },
        "projection_outcome": "success_empty",
        "projected_expirations": [],
    }
    expected_contract = build_required_data_expected_fetch_contract(
        symbol="NVDA",
        fetch_plan=fetch_plan,
        source="opend",
        host="127.0.0.1",
        port=11111,
    )
    raw_path, csv_path = save_outputs(
        quotes,
        "NVDA",
        {
            "symbol": "NVDA",
            "underlier_code": "US.NVDA",
            "expirations": [],
            "expiration_count": 0,
            "rows": [],
            "meta": {
                "status": "ok",
                "source": "opend",
                "host": "127.0.0.1",
                "port": 11111,
                "trading_date": NOW.date().isoformat(),
                "source_outcome": "success_empty",
                "reason_code": "no_expirations",
                "source_observed_at": NOW.isoformat(),
                "completed_at_utc": completed_at.isoformat(),
                "snapshot_requested_codes": 0,
                "snapshot_returned_codes": 0,
                "snapshot_missing_codes": 0,
                "snapshot_unexpected_codes": 0,
                "snapshot_requested_code_set": [],
                "snapshot_returned_code_set": [],
                "snapshot_missing_code_set": [],
                "snapshot_unexpected_code_set": [],
                "snapshot_complete": True,
                "realized_volatility": {
                    "status": "not_applicable_no_contracts",
                },
            },
        },
        output_root=quotes,
    )
    quote_path, quote_receipt = publish_required_data_quote_snapshot(
        producer_root=quotes,
        producer_run_id="prefetch-1",
        symbol="NVDA",
        raw_path=raw_path,
        csv_path=csv_path,
        fetch_plan=fetch_plan,
        fetch_policy={
            "source": "opend",
            "host": "127.0.0.1",
            "port": 11111,
        },
        expected_fetch_contract=expected_contract,
        source_observed_at=NOW,
        completed_at=completed_at,
        now=completed_at,
    )
    portfolio = {
        "source_observed_at": NOW.isoformat(),
        "source_account_identifiers": ["12345"],
        "portfolio_source_name": "futu",
        "cash_by_currency": {"CNY": 100000},
        "stocks_by_symbol": {},
    }
    snapshot = _snapshot()
    _write_json(state / "portfolio_context.json", portfolio)
    _write_json(
        state / "option_positions_context.json",
        {
            "decision_snapshot_status": "trusted",
            "decision_state_fingerprint": snapshot[
                "decision_state_fingerprint"
            ],
            "cash_secured_unavailable_by_symbol": {},
            "cash_secured_total_cny": 0,
            "locked_shares_by_symbol": {},
            "locked_shares_unavailable_by_symbol": {},
        },
    )
    _write_json(
        state / "rate_cache.json",
        {
            "source": "fixture",
            "timestamp": NOW.isoformat(),
            "rates": {"USDCNY": 7.2},
        },
    )
    seal_opening_candidate_snapshot(
        base=tmp_path,
        run_id="run-1",
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
        dependencies=[
            dependency_from_hash(kind=kind, sha256=char * 64)
            for kind, char in (
                ("required_data", "c"),
                ("portfolio", "d"),
                ("ledger", "e"),
                ("fx", "f"),
                ("earnings_rv", "1"),
            )
        ],
        scan_statuses=[
            {
                "symbol": "NVDA",
                "strategy_mode": mode,
                "status": "completed",
                "reason": "no_expirations",
                "quote_snapshot_id": quote_receipt["snapshot_id"],
                "quote_receipt_relpath": quote_path.relative_to(quotes).as_posix(),
            }
            for mode in ("put", "call")
        ],
        candidate_decisions=[],
        final_candidates={"put": [], "call": []},
        sealed_at=completed_at,
    )
    result = publish_account_run_sources(
        account_run_id="run-1",
        normalized_account="lx",
        broker="futu",
        included_markets=["US"],
        account_state_dir=state,
        required_data_root=quotes,
        decision_snapshot_reader=_snapshot,
        completed_at=NOW + timedelta(seconds=3),
    )
    assert quote_receipt["snapshot_id"] in {
        item["receipt"]["snapshot_id"] for item in result["receipts"]
    }
    return account_root, result


def _publish_shadow_plan(tmp_path: Path) -> tuple[str, dict[str, object]]:
    account_root, sources = _prepare_sources(tmp_path)
    identity = str(sources["portfolio_account_identity_hash"])
    binding = build_identity_binding_evidence(
        normalized_account="lx",
        normalized_portfolio_source="futu",
        portfolio_account_identity_hash=identity,
        authoring_config_hash="a" * 64,
        market_bindings=[
            {
                "market": "US",
                "generated_config_hash": "b" * 64,
                "source_receipt_hash": "c" * 64,
                "normalized_account": "lx",
                "normalized_portfolio_source": "futu",
                "portfolio_account_identity_hash": identity,
                "source_receipt_fresh": True,
            }
        ],
    )
    apply_authority_change(
        base=tmp_path,
        normalized_account="lx",
        normalized_portfolio_source="futu",
        portfolio_account_identity_hash=identity,
        target_mode="v2_shadow",
        expected_policy_hash="absent",
        actor="operator@example",
        requested_at=NOW,
        confirm=True,
        identity_binding_evidence=binding,
    )

    result = run_position_advice_v2(
        base=tmp_path,
        account_run_id="run-1",
        account_run_root=account_root,
        normalized_account="lx",
        broker="futu",
        included_markets=["US"],
        normalized_portfolio_source="futu",
        portfolio_account_identity_hash=identity,
        capacity_pool_authority_id=str(
            sources["capacity_pool_authority_id"]
        ),
        source_receipts=[
            {
                "source_kind": item["source_kind"],
                "producer_root": item["producer_root"],
                "receipt_path": item["receipt_path"],
            }
            for item in sources["receipts"]
        ],
        decision_snapshot_reader=_snapshot,
        now=NOW + timedelta(seconds=4),
    )
    return identity, result


def test_runner_materializes_immutable_artifacts_and_switches_shadow_current(
    tmp_path: Path,
) -> None:
    identity, result = _publish_shadow_plan(tmp_path)

    assert result["status"] == "published"
    assert result["current_switched"] is True
    assert result["rows"] == 0
    assert Path(result["paths"]["json"]).is_file()
    assert Path(result["paths"]["csv"]).is_file()
    assert Path(result["paths"]["text"]).is_file()
    current_files = list(
        (tmp_path / "output_shared" / "state" / "position_advice").glob(
            "*/account_decision_current.US.v2.json"
        )
    )
    assert len(current_files) == 1

    read_result = read_position_advice_v2(
        base=tmp_path,
        normalized_account="lx",
        normalized_portfolio_source="futu",
        portfolio_account_identity_hash=identity,
        decision_snapshot_reader=_snapshot,
        now=NOW + timedelta(seconds=5),
    )
    assert read_result["availability_status"] == "available"
    assert read_result["freshness"]["status"] == "fresh"
    assert read_result["authority_mode"] == "v2_shadow"
    assert read_result["actionable_count"] == 0

    changed_snapshot = {
        **_snapshot(),
        "decision_state_fingerprint": "e" * 64,
    }
    stale_result = read_position_advice_v2(
        base=tmp_path,
        normalized_account="lx",
        normalized_portfolio_source="futu",
        portfolio_account_identity_hash=identity,
        decision_snapshot_reader=lambda: changed_snapshot,
        now=NOW + timedelta(seconds=5),
    )
    assert stale_result["freshness"]["status"] == "stale_decision_state"
    assert stale_result["actionable_count"] == 0

    expired_result = read_position_advice_v2(
        base=tmp_path,
        normalized_account="lx",
        normalized_portfolio_source="futu",
        portfolio_account_identity_hash=identity,
        decision_snapshot_reader=_snapshot,
        now=NOW + timedelta(seconds=1801),
    )
    assert expired_result["freshness"]["status"] == "stale_market_data"
    assert expired_result["actionable_count"] == 0


def test_reader_retries_once_when_snapshot_changes_then_stabilizes(
    tmp_path: Path,
) -> None:
    identity, _result = _publish_shadow_plan(tmp_path)
    changed = {
        **_snapshot(),
        "decision_state_fingerprint": "e" * 64,
    }
    snapshots = iter([_snapshot(), changed, _snapshot(), _snapshot()])

    read_result = read_position_advice_v2(
        base=tmp_path,
        normalized_account="lx",
        normalized_portfolio_source="futu",
        portfolio_account_identity_hash=identity,
        decision_snapshot_reader=lambda: next(snapshots),
        now=NOW + timedelta(seconds=5),
    )

    assert read_result["freshness"]["status"] == "fresh"
    assert read_result["actionable_count"] == 0


def test_reader_fails_closed_when_snapshot_changes_twice(
    tmp_path: Path,
) -> None:
    identity, _result = _publish_shadow_plan(tmp_path)
    changed = {
        **_snapshot(),
        "decision_state_fingerprint": "e" * 64,
    }
    snapshots = iter([_snapshot(), changed, _snapshot(), changed])

    read_result = read_position_advice_v2(
        base=tmp_path,
        normalized_account="lx",
        normalized_portfolio_source="futu",
        portfolio_account_identity_hash=identity,
        decision_snapshot_reader=lambda: next(snapshots),
        now=NOW + timedelta(seconds=5),
    )

    assert read_result["freshness"]["status"] == "stale_decision_state"
    assert read_result["freshness"]["reason_codes"] == [
        "decision_state_changed_during_read"
    ]
    assert read_result["actionable_count"] == 0


def test_reader_marks_requested_old_plan_superseded(
    tmp_path: Path,
) -> None:
    identity, result = _publish_shadow_plan(tmp_path)
    current_plan_id = str(result["portfolio_plan_id"])

    read_result = read_position_advice_v2(
        base=tmp_path,
        normalized_account="lx",
        normalized_portfolio_source="futu",
        portfolio_account_identity_hash=identity,
        decision_snapshot_reader=_snapshot,
        requested_portfolio_plan_id="f" * 64,
        now=NOW + timedelta(seconds=5),
    )

    assert read_result["portfolio_plan_id"] == current_plan_id
    assert read_result["freshness"]["status"] == "superseded_portfolio_plan"
    assert read_result["actionable_count"] == 0


def test_reader_validates_full_artifact_before_market_filter(
    tmp_path: Path,
    monkeypatch,
) -> None:
    scope_id = scope_for("lx")
    resolution = AuthorityResolution(
        portfolio_scope_id=scope_id,
        mode="v2_shadow",
        generation=1,
        policy_hash="f" * 64,
        resolution_status="resolved",
    )
    current = {
        "account": "lx",
        "portfolio_scope_id": scope_id,
        "normalized_portfolio_source": "futu",
        "portfolio_account_identity_hash": "a" * 64,
        "authority_mode": "v2_shadow",
        "authority_generation": 1,
        "authority_policy_hash": "f" * 64,
        "source_manifest_hash": "b" * 64,
        "account_run_id": "run-1",
        "decision_state_fingerprint": _snapshot()[
            "decision_state_fingerprint"
        ],
        "included_markets": ["HK", "US"],
        "current_market": "US",
        "current_manifest_hash": "c" * 64,
    }
    advice = {
        "account": "lx",
        "portfolio_scope_id": scope_id,
        "portfolio_plan_id": "e" * 64,
        "account_run_id": "run-1",
        "normalized_portfolio_source": "futu",
        "included_markets": ["HK", "US"],
        "rows": [
            {"symbol": "NVDA", "actionable": True},
            {"symbol": "0700.HK", "actionable": True},
        ],
    }
    source_manifest = {
        "source_manifest_hash": "b" * 64,
        "account_run_id": "run-1",
        "source_manifest": [],
    }
    validated_full_artifact = False

    def _validate_full_artifact(**kwargs) -> None:
        nonlocal validated_full_artifact
        assert [row["symbol"] for row in kwargs["advice"]["rows"]] == [
            "NVDA",
            "0700.HK",
        ]
        validated_full_artifact = True

    monkeypatch.setattr(
        position_advice_reader,
        "position_advice_manifest_locks",
        lambda **_kwargs: nullcontext(),
    )
    monkeypatch.setattr(
        position_advice_reader,
        "read_authority_resolution_under_lock",
        lambda **_kwargs: resolution,
    )
    monkeypatch.setattr(
        position_advice_reader,
        "validate_current_artifacts_under_lock",
        lambda **_kwargs: {
            "current": current,
            "advice": advice,
            "immutable_input": {
                "normalized_portfolio_source": "futu",
                "included_markets": ["HK", "US"],
            },
            "source_manifest": source_manifest,
        },
    )
    monkeypatch.setattr(
        position_advice_reader,
        "_validate_current_binding",
        _validate_full_artifact,
    )

    result = position_advice_reader.read_position_advice_v2(
        base=tmp_path,
        normalized_account="lx",
        normalized_portfolio_source="futu",
        portfolio_account_identity_hash="a" * 64,
        decision_snapshot_reader=lambda: _snapshot(),
        requested_market="US",
        now=NOW + timedelta(seconds=5),
    )

    assert validated_full_artifact is True
    assert result["availability_status"] == "available"
    assert result["freshness"]["status"] == "fresh"
    assert result["authority_mode"] == "v2_shadow"
    assert [row["symbol"] for row in result["rows"]] == ["NVDA"]
