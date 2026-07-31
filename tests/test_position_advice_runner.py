from __future__ import annotations

import json
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path

from domain.domain.decision_state_fingerprint import canonical_sha256
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
from src.application.position_advice_authority_service import (
    apply_authority_change,
    build_identity_binding_evidence,
)
from src.application.position_advice_runner import run_position_advice_v2
from src.application.position_advice_reader import read_position_advice_v2


NOW = datetime(2026, 7, 27, 10, 0, tzinfo=timezone.utc)


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _snapshot() -> dict[str, object]:
    return {
        "schema_version": "decision_state_snapshot.v2",
        "fingerprint_schema_version": "decision_state_fingerprint.v2",
        "snapshot_status": "trusted",
        "actionable": True,
        "reason_codes": [],
        "decision_state_fingerprint": "d" * 64,
        "source_observed_at": (NOW + timedelta(seconds=2)).isoformat(),
        "account_position_lots": [],
        "account_lifecycle_cases": [],
        "account_lifecycle_evidence": [],
        "account_lifecycle_allocations": [],
        "account_assigned_stock_events": [],
        "account_combo_identities": [],
    }


def _prepare_sources(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    account_root = tmp_path / "output_runs" / "run-1" / "accounts" / "lx"
    state = account_root / "state"
    quotes = tmp_path / "required_data"
    state.mkdir(parents=True)
    quotes.mkdir()
    raw_path, csv_path = save_outputs(
        quotes,
        "NVDA",
        {
            "symbol": "NVDA",
            "rows": [],
            "meta": {
                "status": "ok",
                "source": "opend",
                "source_outcome": "success_empty",
                "reason_code": "no_expirations",
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
        fetch_plan={"symbol": "NVDA", "sides": ["put", "call"]},
        fetch_policy={"source": "opend"},
        source_observed_at=NOW,
        completed_at=NOW + timedelta(seconds=1),
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
    capture = {
        "schema_version": (
            "position_advice_candidate_all_decisions_capture.v1"
        ),
        "account_run_id": "run-1",
        "account": "lx",
        "complete": True,
        "quote_receipt_relpaths": {
            "NVDA": quote_path.relative_to(quotes).as_posix(),
        },
        "candidate_decisions": [],
    }
    capture["capture_hash"] = canonical_sha256(capture)
    _write_json(
        state / "position_advice_candidate_all_decisions.raw.json",
        capture,
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
        "decision_state_fingerprint": "d" * 64,
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
