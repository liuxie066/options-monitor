from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from domain.domain.decision_state_fingerprint import canonical_sha256
from src.application.position_advice_source_producers import (
    publish_candidate_decisions_snapshot,
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
    )
    return path, receipt


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
    ledger_path, ledger = publish_ledger_source_snapshot(
        producer_root=tmp_path,
        account_run_id="run-1",
        account="lx",
        broker="futu",
        portfolio_account_identity_hash=IDENTITY,
        included_markets=["US"],
        decision_state_snapshot={
            "snapshot_status": "trusted",
            "actionable": True,
            "decision_state_fingerprint": "c" * 64,
            "fingerprint_schema_version": "decision_state_fingerprint.v2",
            "source_observed_at": (NOW + timedelta(seconds=2)).isoformat(),
        },
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
    candidate_path, candidate = publish_candidate_decisions_snapshot(
        producer_root=tmp_path,
        account_run_id="run-1",
        account="lx",
        broker="futu",
        portfolio_account_identity_hash=IDENTITY,
        included_markets=["US"],
        decisions=[
            {
                "schema_version": "candidate_all_decisions.v1",
                "candidate_id": "candidate-1",
                "strategy_mode": "put",
                "quote_snapshot_id": quote["snapshot_id"],
                "risk_policy_hash": "d" * 64,
            }
        ],
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
