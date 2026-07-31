from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from domain.domain.decision_state_fingerprint import canonical_sha256
from src.application.position_advice_account_sources import (
    PositionAdviceAccountSourceError,
    build_cash_capacity,
    build_share_coverage,
    publish_account_run_sources,
)
from src.application.position_advice_source_receipts import (
    publish_source_receipt,
    validate_source_receipt,
)


NOW = datetime(2026, 7, 27, 10, 0, tzinfo=timezone.utc)


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _quote_receipt(root: Path) -> dict:
    return publish_source_receipt(
        producer_root=root,
        receipt_relpath="quotes/NVDA/receipt.json",
        payload_relpath="quotes/NVDA/payload.json",
        payload_bytes=b'{"rows":[]}\n',
        source_kind="quotes",
        producer_schema_version="required_data_quote_snapshot.v1",
        producer_run_id="run-1",
        producer_scope="market",
        producer_account_run_id=None,
        broker=None,
        account=None,
        portfolio_account_identity_hash=None,
        included_markets=["US"],
        source_native_id="NVDA:quote",
        source_observed_at=NOW,
        completed_at=NOW + timedelta(seconds=1),
        producer_policy_hash="a" * 64,
    )


def _decision_snapshot() -> dict:
    return {
        "schema_version": "decision_state_snapshot.v2",
        "fingerprint_schema_version": "decision_state_fingerprint.v2",
        "snapshot_status": "trusted",
        "actionable": True,
        "reason_codes": [],
        "decision_state_fingerprint": "b" * 64,
        "source_observed_at": (NOW + timedelta(seconds=2)).isoformat(),
        "account_position_lots": [],
    }


def _account_run_inputs(
    tmp_path: Path,
) -> tuple[Path, Path, dict, dict]:
    state = tmp_path / "account" / "state"
    quotes = tmp_path / "required"
    state.mkdir(parents=True)
    quotes.mkdir()
    quote = _quote_receipt(quotes)
    snapshot = _decision_snapshot()
    _write_json(
        state / "portfolio_context.json",
        {
            "source_observed_at": NOW.isoformat(),
            "source_account_identifiers": ["12345"],
            "portfolio_source_name": "futu",
            "cash_by_currency": {"CNY": 1000, "USD": 100},
            "stocks_by_symbol": {
                "NVDA": {
                    "shares": 200,
                    "avg_cost": 100,
                    "currency": "USD",
                }
            },
        },
    )
    _write_json(
        state / "option_positions_context.json",
        {
            "decision_snapshot_status": "trusted",
            "decision_state_fingerprint": snapshot[
                "decision_state_fingerprint"
            ],
            "cash_secured_unavailable_by_symbol": {},
            "cash_secured_total_cny": 500,
            "locked_shares_by_symbol": {"NVDA": 100},
            "locked_shares_unavailable_by_symbol": {},
        },
    )
    _write_json(
        state / "rate_cache.json",
        {
            "source": "fixture",
            "timestamp": NOW.isoformat(),
            "rates": {"USDCNY": 7.2, "HKDCNY": 0.92},
        },
    )
    decision = {
        "schema_version": "candidate_all_decisions.v1",
        "candidate_id": "candidate-1",
        "strategy_mode": "put",
        "quote_snapshot_id": quote["snapshot_id"],
        "risk_policy_hash": "c" * 64,
    }
    capture = {
        "schema_version": (
            "position_advice_candidate_all_decisions_capture.v1"
        ),
        "account_run_id": "run-1",
        "account": "lx",
        "complete": True,
        "quote_receipt_relpaths": {
            "NVDA": "quotes/NVDA/receipt.json",
        },
        "candidate_decisions": [decision],
    }
    capture["capture_hash"] = canonical_sha256(capture)
    _write_json(
        state / "position_advice_candidate_all_decisions.raw.json",
        capture,
    )
    return state, quotes, quote, snapshot


def test_cash_capacity_uses_uncommitted_base_cny_not_gross_cash() -> None:
    capacity = build_cash_capacity(
        portfolio_context={
            "cash_by_currency": {"CNY": 1000, "USD": 100},
        },
        option_positions_context={
            "decision_snapshot_status": "trusted",
            "cash_secured_unavailable_by_symbol": {},
            "cash_secured_total_cny": 500,
        },
        fx_payload={"rates": {"USDCNY": 7.2}},
    )
    assert capacity["cash_available_base_cny"] == "1720"
    assert capacity["existing_short_put_collateral_base_cny"] == "500"
    assert capacity["uncommitted_cash_headroom_base_cny"] == "1220"


def test_cash_and_share_capacity_fail_closed_on_unknown_basis() -> None:
    with pytest.raises(
        PositionAdviceAccountSourceError,
        match="collateral basis",
    ):
        build_cash_capacity(
            portfolio_context={"cash_by_currency": {"CNY": 1000}},
            option_positions_context={
                "decision_snapshot_status": "trusted",
                "cash_secured_unavailable_by_symbol": {"NVDA": "missing"},
                "cash_secured_total_cny": 0,
            },
            fx_payload={"rates": {"USDCNY": 7.2}},
        )

    coverage = build_share_coverage(
        portfolio_context={
            "stocks_by_symbol": {
                "NVDA": {
                    "shares": 200,
                    "avg_cost": 100,
                    "currency": "USD",
                }
            }
        },
        option_positions_context={
            "decision_snapshot_status": "trusted",
            "locked_shares_by_symbol": {"NVDA": 100},
            "locked_shares_unavailable_by_symbol": {},
        },
    )
    assert coverage["symbols"][0]["uncommitted_covered_shares"] == 100


def test_account_run_publishes_complete_receipt_dependency_graph(
    tmp_path: Path,
) -> None:
    state, quotes, quote, snapshot = _account_run_inputs(tmp_path)

    result = publish_account_run_sources(
        account_run_id="run-1",
        normalized_account="lx",
        broker="futu",
        included_markets=["US"],
        account_state_dir=state,
        required_data_root=quotes,
        decision_snapshot_reader=lambda: snapshot,
        completed_at=NOW + timedelta(seconds=3),
    )

    assert result["source_kinds"] == [
        "candidate_decisions",
        "cash_capacity",
        "fx",
        "ledger_decision_state",
        "portfolio",
        "quotes",
        "share_coverage",
    ]
    by_kind = {
        item["source_kind"]: item
        for item in result["receipts"]
        if item["source_kind"] != "quotes"
    }
    cash = validate_source_receipt(
        by_kind["cash_capacity"]["receipt"],
        producer_root=state,
        now=NOW + timedelta(seconds=4),
    )
    assert {item["source_kind"] for item in cash["dependencies"]} == {
        "portfolio",
        "ledger_decision_state",
        "fx",
    }
    assert (
        by_kind["candidate_decisions"]["receipt"]["dependencies"][0][
            "snapshot_id"
        ]
        == quote["snapshot_id"]
    )


def test_account_run_publishes_completed_zero_candidate_source_with_quote_dependency(
    tmp_path: Path,
) -> None:
    state, quotes, quote, snapshot = _account_run_inputs(tmp_path)
    capture_path = (
        state
        / "position_advice_candidate_all_decisions.raw.json"
    )
    capture = json.loads(capture_path.read_text(encoding="utf-8"))
    capture["candidate_decisions"] = []
    capture["capture_hash"] = canonical_sha256(
        {
            key: value
            for key, value in capture.items()
            if key != "capture_hash"
        }
    )
    capture_path.write_text(json.dumps(capture), encoding="utf-8")

    result = publish_account_run_sources(
        account_run_id="run-1",
        normalized_account="lx",
        broker="futu",
        included_markets=["US"],
        account_state_dir=state,
        required_data_root=quotes,
        decision_snapshot_reader=lambda: snapshot,
        completed_at=NOW + timedelta(seconds=3),
    )

    candidate = next(
        item
        for item in result["receipts"]
        if item["source_kind"] == "candidate_decisions"
    )
    validated = validate_source_receipt(
        candidate["receipt"],
        producer_root=state,
        now=NOW + timedelta(seconds=4),
        expected_source_kind="candidate_decisions",
    )
    payload = json.loads(
        validated["payload_path"].read_text(encoding="utf-8")
    )
    assert payload["candidate_decisions"] == []
    assert payload["candidate_count"] == 0
    assert payload["quote_snapshot_ids"] == [quote["snapshot_id"]]
    assert len(validated["dependencies"]) == 1
    assert validated["dependencies"][0]["source_kind"] == "quotes"


def test_default_completion_timestamp_is_captured_after_ledger_observation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, quotes, _quote, snapshot = _account_run_inputs(tmp_path)
    reader_called = False

    class SequencedDateTime(datetime):
        @classmethod
        def now(cls, tz: timezone | None = None) -> datetime:
            value = NOW + timedelta(
                seconds=3 if reader_called else 1,
            )
            return value if tz is not None else value.replace(tzinfo=None)

    def read_snapshot() -> dict:
        nonlocal reader_called
        reader_called = True
        return snapshot

    monkeypatch.setattr(
        "src.application.position_advice_account_sources.datetime",
        SequencedDateTime,
    )

    result = publish_account_run_sources(
        account_run_id="run-1",
        normalized_account="lx",
        broker="futu",
        included_markets=["US"],
        account_state_dir=state,
        required_data_root=quotes,
        decision_snapshot_reader=read_snapshot,
    )

    ledger = next(
        item["receipt"]
        for item in result["receipts"]
        if item["source_kind"] == "ledger_decision_state"
    )
    assert reader_called is True
    assert ledger["source_observed_at"] == (
        NOW + timedelta(seconds=2)
    ).isoformat().replace("+00:00", "Z")
    assert ledger["completed_at"] == (
        NOW + timedelta(seconds=3)
    ).isoformat().replace("+00:00", "Z")
