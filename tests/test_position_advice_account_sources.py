from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.application.opening_candidate_snapshot import (
    dependency_from_hash,
    seal_opening_candidate_snapshot,
)
from src.application.position_advice_account_sources import (
    PositionAdviceAccountSourceError,
    build_cash_capacity,
    build_share_coverage,
    publish_account_position_advice_sources,
    publish_account_run_sources,
)
from src.application.position_advice_source_receipts import (
    publish_source_receipt,
    validate_source_receipt,
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
        "decision_state_fingerprint": "b" * 64,
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
    snapshot["decision_state_fingerprint"] = (
        decision_state_snapshot_fingerprint(snapshot)
    )
    return snapshot


def _account_run_inputs(
    tmp_path: Path,
) -> tuple[Path, Path, dict, dict]:
    state = (
        tmp_path
        / "output_runs"
        / "run-1"
        / "accounts"
        / "lx"
        / "state"
    )
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
                    "can_sell_qty": 200,
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
                "reason": "all_decisions_captured",
                "quote_snapshot_id": quote["snapshot_id"],
                "quote_receipt_relpath": "quotes/NVDA/receipt.json",
            }
            for mode in ("put", "call")
        ],
        candidate_decisions=[],
        final_candidates={"put": [], "call": []},
        sealed_at=NOW,
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
                    "can_sell_qty": 200,
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


def test_share_coverage_is_incomplete_without_opend_can_sell_quantity() -> None:
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
            "locked_shares_by_symbol": {"NVDA": 0},
            "locked_shares_unavailable_by_symbol": {},
        },
    )

    row = coverage["symbols"][0]
    assert row["complete"] is False
    assert row["reason"] == "can_sell_qty_missing"
    assert row["uncommitted_covered_shares"] is None


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


def test_account_run_accepts_prepared_portfolio_context_without_legacy_filename(
    tmp_path: Path,
) -> None:
    state, quotes, _quote, snapshot = _account_run_inputs(tmp_path)
    context = json.loads((state / "portfolio_context.json").read_text())
    (state / "portfolio_context.json").unlink()
    (state / ("portfolio_context." + "a" * 64 + ".json")).write_text(
        json.dumps(context),
        encoding="utf-8",
    )

    result = publish_account_run_sources(
        account_run_id="run-1",
        normalized_account="lx",
        broker="futu",
        included_markets=["US"],
        account_state_dir=state,
        required_data_root=quotes,
        decision_snapshot_reader=lambda: snapshot,
        portfolio_context_override=context,
        completed_at=NOW + timedelta(seconds=3),
    )

    assert "portfolio" in result["source_kinds"]
    assert result["cash_capacity"]["status"] == "available"


def test_account_run_publishes_completed_zero_candidate_source_with_quote_dependency(
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


def test_account_run_facade_reuses_prepared_ledger_and_fx_authority(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from src.application import position_advice_account_sources as mod

    account_root = tmp_path / "account"
    state = account_root / "state"
    required = tmp_path / "required"
    state.mkdir(parents=True)
    required.mkdir()
    decision_snapshot = _decision_snapshot()
    fx_payload = {
        "timestamp": NOW.isoformat(),
        "source": "prepared",
        "rates": {"USDCNY": 7.2},
    }
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        mod,
        "open_position_ledger",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("prepared authority must not reopen the ledger")
        ),
    )

    def _publish(**kwargs):
        captured["decision_snapshot"] = kwargs[
            "decision_snapshot_reader"
        ]()
        captured["fx_payload"] = kwargs["fx_payload_override"]
        return {
            "schema_version": "position_advice_account_sources.v2",
            "account_run_id": "run-1",
            "account": "lx",
            "broker": "futu",
            "included_markets": ["US"],
            "portfolio_scope_id": "account:lx",
            "normalized_portfolio_source": "futu",
            "portfolio_account_identity_hash": "a" * 64,
            "capacity_pool_authority_id": "b" * 64,
            "decision_state_snapshot": decision_snapshot,
            "cash_capacity": {},
            "share_coverage": {},
            "source_kinds": [],
            "receipts": [],
        }

    monkeypatch.setattr(mod, "publish_account_run_sources", _publish)

    result = publish_account_position_advice_sources(
        account_run_root=account_root,
        account_state_dir=state,
        quote_producer_root=required,
        data_config_path=tmp_path / "portfolio.runtime.json",
        account_run_id="run-1",
        account="lx",
        broker="futu",
        included_markets=["US"],
        decision_state_snapshot_override=decision_snapshot,
        fx_payload_override=fx_payload,
    )

    assert result["decision_state_snapshot"] == decision_snapshot
    assert captured == {
        "decision_snapshot": decision_snapshot,
        "fx_payload": fx_payload,
    }
