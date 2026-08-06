from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from typing import Any

import pytest

from domain.domain.decision_state_fingerprint import canonical_sha256
from src.application.ledger.decision_snapshot import (
    POSITION_FACT_SNAPSHOT_CONTRACT,
    decision_state_snapshot_fingerprint,
)
from src.application.ledger.lifecycle_overlay import (
    resolve_account_lifecycle_overlay,
)
from src.application.position_advice_source_producers import (
    publish_opening_candidate_snapshot_receipt,
    publish_cash_capacity_snapshot,
    publish_fx_source_snapshot,
    publish_ledger_source_snapshot,
    publish_portfolio_source_snapshot,
)
from src.application.position_advice_source_receipts import (
    publish_source_receipt,
    source_dependency_from_receipt,
    validate_source_receipt,
)


NOW = datetime(2026, 7, 27, 10, 0, tzinfo=timezone.utc)
IDENTITY = "a" * 64
POLICY = "b" * 64


def _raw_source(
    root: Path,
    *,
    kind: str,
    account_scope: bool,
    before_receipt_commit: Callable[[Mapping[str, Any]], None] | None = None,
) -> tuple[Path, dict[str, object]]:
    path = root / f"{kind}.receipt.json"
    receipt = publish_source_receipt(
        producer_root=root,
        receipt_relpath=path.name,
        payload_relpath=f"{kind}.payload.json",
        payload_bytes=b"{}\n",
        source_kind=kind,
        producer_schema_version=f"{kind}.v1",
        producer_run_id="run-1",
        producer_scope="account" if account_scope else "global",
        producer_account_run_id="run-1" if account_scope else None,
        broker="futu" if account_scope else None,
        account="lx" if account_scope else None,
        portfolio_account_identity_hash=IDENTITY if account_scope else None,
        included_markets=["US"],
        source_native_id=f"{kind}-1",
        source_observed_at=NOW,
        completed_at=NOW + timedelta(seconds=1),
        producer_policy_hash=POLICY,
        before_receipt_commit=before_receipt_commit,
    )
    return path, receipt


def test_source_receipt_commit_validator_sees_complete_receipt_once(
    tmp_path: Path,
) -> None:
    payload_path = tmp_path / "quotes.payload.json"
    receipt_path = tmp_path / "quotes.receipt.json"
    calls: list[dict[str, Any]] = []

    def _validate(receipt: Mapping[str, Any]) -> None:
        calls.append(dict(receipt))
        assert receipt["schema_version"] == "position_advice_source_receipt.v1"
        assert receipt["source_kind"] == "quotes"
        assert receipt["payload_relpath"] == "quotes.payload.json"
        assert receipt["completed"] is True
        assert len(str(receipt["payload_sha256"])) == 64
        assert payload_path.read_bytes() == b"{}\n"
        assert not receipt_path.exists()

    committed_path, committed = _raw_source(
        tmp_path,
        kind="quotes",
        account_scope=False,
        before_receipt_commit=_validate,
    )

    assert committed_path == receipt_path
    assert receipt_path.is_file()
    assert calls == [committed]


def test_source_receipt_commit_validator_failure_leaves_only_orphan_payload(
    tmp_path: Path,
) -> None:
    payload_path = tmp_path / "quotes.payload.json"
    receipt_path = tmp_path / "quotes.receipt.json"
    calls = 0

    def _reject(receipt: Mapping[str, Any]) -> None:
        nonlocal calls
        calls += 1
        assert receipt["completed"] is True
        assert payload_path.is_file()
        assert not receipt_path.exists()
        raise RuntimeError("commit-time freshness expired")

    with pytest.raises(RuntimeError, match="commit-time freshness expired"):
        _raw_source(
            tmp_path,
            kind="quotes",
            account_scope=False,
            before_receipt_commit=_reject,
        )

    assert calls == 1
    assert payload_path.read_bytes() == b"{}\n"
    assert not receipt_path.exists()


def test_portfolio_and_ledger_producers_preserve_native_observation(
    tmp_path: Path,
) -> None:
    portfolio_path, portfolio = publish_portfolio_source_snapshot(
        producer_root=tmp_path,
        account_run_id="run-1",
        account="lx",
        broker="futu",
        normalized_portfolio_source="futu",
        portfolio_account_identity_hash=IDENTITY,
        included_markets=["US"],
        portfolio_context={
            "source_observed_at": NOW.isoformat(),
            "source_account_identifiers": ["123"],
            "cash_by_currency": {"USD": 1000},
        },
        completed_at=NOW + timedelta(seconds=1),
    )
    ledger_snapshot = {
        "schema_version": "decision_state_snapshot.v2",
        "position_fact_contract_version": (
            POSITION_FACT_SNAPSHOT_CONTRACT
        ),
        "normalized_account": "lx",
        "snapshot_status": "trusted",
        "actionable": True,
        "decision_state_fingerprint": "",
        "fingerprint_schema_version": "decision_state_fingerprint.v2",
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
        "account_combo_identities": [],
        "account_combo_group_memberships": [],
    }
    ledger_snapshot["decision_state_fingerprint"] = (
        decision_state_snapshot_fingerprint(ledger_snapshot)
    )
    ledger_path, ledger = publish_ledger_source_snapshot(
        producer_root=tmp_path,
        account_run_id="run-1",
        account="lx",
        broker="futu",
        portfolio_account_identity_hash=IDENTITY,
        included_markets=["US"],
        decision_state_snapshot=ledger_snapshot,
        completed_at=NOW + timedelta(seconds=3),
    )

    assert portfolio_path.is_file()
    assert ledger_path.is_file()
    assert portfolio["source_observed_at"] == "2026-07-27T10:00:00Z"
    assert ledger["source_observed_at"] == "2026-07-27T10:00:02Z"


def test_portfolio_producer_rejects_unknown_business_observation(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="observation is not trusted"):
        publish_portfolio_source_snapshot(
            producer_root=tmp_path,
            account_run_id="run-unknown",
            account="lx",
            broker="futu",
            normalized_portfolio_source="holdings",
            portfolio_account_identity_hash=IDENTITY,
            included_markets=["US"],
            portfolio_context={
                "retrieved_at_utc": NOW.isoformat(),
                "source_observed_at": None,
                "source_observation_status": "unknown",
                "source_account_identifiers": ["lx"],
                "cash_by_currency": {"USD": 1000},
            },
            completed_at=NOW + timedelta(seconds=1),
        )


def test_candidate_and_capacity_dependencies_are_closed(
    tmp_path: Path,
) -> None:
    quote_path, quote = _raw_source(
        tmp_path,
        kind="quotes",
        account_scope=False,
    )
    portfolio_path, _portfolio = _raw_source(
        tmp_path,
        kind="portfolio",
        account_scope=True,
    )
    ledger_path, _ledger = _raw_source(
        tmp_path,
        kind="ledger_decision_state",
        account_scope=True,
    )
    fx_path, _fx = _raw_source(
        tmp_path,
        kind="fx",
        account_scope=False,
    )
    quote_dep = source_dependency_from_receipt(
        receipt_path=quote_path,
        producer_root=tmp_path,
        now=NOW + timedelta(seconds=2),
    )
    opening_snapshot = {
        "schema_version": "opening_candidate_snapshot.v1",
        "content_sha256": "c" * 64,
        "strategy_policy_sha256": "d" * 64,
        "ranked_candidates": [
            {"candidate_id": "candidate-1", "quote_snapshot_id": quote["snapshot_id"]}
        ],
    }
    (tmp_path / "opening_candidate_snapshot.json").write_text(
        json.dumps(opening_snapshot),
        encoding="utf-8",
    )
    candidate_path, candidate = publish_opening_candidate_snapshot_receipt(
        producer_root=tmp_path,
        account_run_id="run-1",
        account="lx",
        broker="futu",
        portfolio_account_identity_hash=IDENTITY,
        included_markets=["US"],
        snapshot=opening_snapshot,
        quote_dependencies=[quote_dep],
        source_observed_at=NOW,
        completed_at=NOW + timedelta(seconds=2),
    )
    candidate_validated = validate_source_receipt(
        candidate,
        producer_root=tmp_path,
        now=NOW + timedelta(seconds=3),
    )
    assert candidate_path.is_file()
    assert candidate_validated["dependencies"] == [quote_dep]

    deps = [
        source_dependency_from_receipt(
            receipt_path=path,
            producer_root=tmp_path,
            now=NOW + timedelta(seconds=2),
        )
        for path in (portfolio_path, ledger_path, fx_path)
    ]
    authority_id = canonical_sha256({"pool": "cash"})
    capacity_path, capacity = publish_cash_capacity_snapshot(
        producer_root=tmp_path,
        account_run_id="run-1",
        account="lx",
        broker="futu",
        portfolio_account_identity_hash=IDENTITY,
        included_markets=["US"],
        capacity_pool_authority_id=authority_id,
        cash_capacity={
            "cash_capacity_semantics": "cash_headroom.v2",
            "available_base_cny": 1000,
        },
        dependencies=deps,
        source_observed_at=NOW + timedelta(seconds=2),
        completed_at=NOW + timedelta(seconds=3),
    )
    validated = validate_source_receipt(
        capacity,
        producer_root=tmp_path,
        now=NOW + timedelta(seconds=4),
    )
    assert capacity_path.is_file()
    assert {item["source_kind"] for item in validated["dependencies"]} == {
        "portfolio",
        "ledger_decision_state",
        "fx",
    }


def test_same_global_fact_can_be_republished_by_another_run(
    tmp_path: Path,
) -> None:
    first_path, first = publish_fx_source_snapshot(
        producer_root=tmp_path,
        producer_run_id="run-1",
        included_markets=["US"],
        fx_payload={
            "source": "fixture",
            "timestamp": NOW.isoformat(),
            "rates": {"USDCNY": "7.20"},
        },
        source_observed_at=NOW.isoformat(),
        provider="fixture",
        completed_at=NOW + timedelta(seconds=1),
    )
    second_path, second = publish_fx_source_snapshot(
        producer_root=tmp_path,
        producer_run_id="run-2",
        included_markets=["US"],
        fx_payload={
            "source": "fixture",
            "timestamp": NOW.isoformat(),
            "rates": {"USDCNY": "7.20"},
        },
        source_observed_at=NOW.isoformat(),
        provider="fixture",
        completed_at=NOW + timedelta(seconds=2),
    )

    assert first_path != second_path
    assert first_path.is_file()
    assert second_path.is_file()
    assert first["producer_run_id"] == "run-1"
    assert second["producer_run_id"] == "run-2"
    assert first["snapshot_id"] == second["snapshot_id"]
