from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from domain.domain.ledger import ContractKey, TradeEvent
from domain.domain.combo_yield_lifecycle import build_option_group_inventory
from domain.domain.option_lifecycle import expiration_observation_start_ms
from domain.domain.position_advice_authority import scope_for
from src.application.ledger.decision_snapshot import (
    decision_state_snapshot,
    decision_state_snapshot_fingerprint,
    validate_position_fact_snapshot_contract,
)
from src.application.ledger.lifecycle_overlay import (
    resolve_account_lifecycle_overlay,
)
from src.application.ledger.api import (
    advance_lifecycle_case_state,
    record_trade_event_void,
)
from src.application.ledger.repository import SQLiteOptionPositionsRepository
from src.application.ledger.writer import persist_trade_event_object
from src.application.ledger.source_consumption import (
    build_source_consumption_claim,
)
from src.application.positions.context_builder import build_context
from src.application.trades.lifecycle_reconciliation import (
    discover_lifecycle_cases,
    lifecycle_case_read_model,
    reconcile_lifecycle_evidence,
)
from src.application.trades.close_reason_evidence import (
    build_lifecycle_timing_policy,
)
from src.application.trades.close_reason_reconciliation import (
    reconcile_due_lifecycle_cases,
)


EXPIRATION_YMD = "2026-08-21"


def _open_event(
    *,
    event_id: str,
    lot_id: str,
    contracts: int = 2,
) -> TradeEvent:
    contract_key = ContractKey.from_values(
        broker="futu",
        account="lx",
        underlying_symbol="NVDA",
        option_type="put",
        position_side="short",
        strike=100,
        expiration_ymd=EXPIRATION_YMD,
    )
    return TradeEvent(
        event_id=event_id,
        event_type="open",
        event_time_ms=1_700_000_000_000,
        contract_key=contract_key,
        contracts=contracts,
        price=2,
        currency="USD",
        source="test",
        multiplier=100,
        lot_id=lot_id,
        raw_payload={
            "fields": {
                "broker": "futu",
                "account": "lx",
                "symbol": "NVDA",
                "option_type": "put",
                "side": "short",
                "contracts": contracts,
                "contracts_open": contracts,
                "contracts_closed": 0,
                "currency": "USD",
                "strike": 100,
                "expiration_ymd": EXPIRATION_YMD,
                "multiplier": 100,
            }
        },
    )


def _assignment_evidence(
    *,
    evidence_id: str,
    contracts: int,
    event_time_ms: int,
    target_lot_id: str | None,
) -> dict:
    return {
        "evidence_id": evidence_id,
        "case_id": None,
        "source_type": "broker_settlement",
        "source_event_id": f"settlement-{evidence_id}",
        "evidence_type": "assignment",
        "account": "lx",
        "symbol": "NVDA",
        "option_type": "put",
        "position_side": "short",
        "strike": 100,
        "expiration_ymd": EXPIRATION_YMD,
        "contracts": contracts,
        "event_time_ms": event_time_ms,
        "currency": "USD",
        "target_lot_id": target_lot_id,
        "stock_settlement": {
            "source_event_id": (
                f"futu:lx:1001:stock-{evidence_id}"
            ),
            "symbol": "NVDA",
            "side": "buy",
            "shares": 100 * contracts,
            "event_time_ms": event_time_ms,
        },
    }


def _expire_close_evidence(
    *,
    evidence_id: str,
    contracts: int,
    event_time_ms: int,
    target_lot_id: str,
) -> dict:
    return {
        "evidence_id": evidence_id,
        "case_id": None,
        "source_type": "manual_lifecycle",
        "source_event_id": f"expiry-{evidence_id}",
        "evidence_type": "expire_close",
        "account": "lx",
        "symbol": "NVDA",
        "option_type": "put",
        "position_side": "short",
        "strike": 100,
        "expiration_ymd": EXPIRATION_YMD,
        "contracts": contracts,
        "event_time_ms": event_time_ms,
        "currency": "USD",
        "target_lot_id": target_lot_id,
    }


def _zero_price_close_evidence(
    *,
    evidence_id: str,
    case_id: str,
    contracts: int,
    event_time_ms: int,
    target_lot_id: str,
) -> dict:
    return {
        "evidence_id": evidence_id,
        "case_id": case_id,
        "source_type": "futu_trade_push",
        "source_event_id": f"futu:lx:1001:{evidence_id}",
        "evidence_type": "option_zero_price_close",
        "account": "lx",
        "futu_account_id": "1001",
        "symbol": "NVDA",
        "option_type": "put",
        "position_side": "short",
        "strike": 100,
        "expiration_ymd": EXPIRATION_YMD,
        "contracts": contracts,
        "price": 0,
        "event_time_ms": event_time_ms,
        "received_at_ms": event_time_ms + 1,
        "target_lot_id": target_lot_id,
    }


def _record_zero_price_close_anchor(
    repo: SQLiteOptionPositionsRepository,
    *,
    evidence: dict,
) -> None:
    case_id = str(evidence["case_id"])
    assert repo.bind_trade_lifecycle_case_futu_account_once(
        case_id=case_id,
        futu_account_id="1001",
    )
    assert repo.insert_trade_lifecycle_evidence_once(evidence)
    assert repo.insert_trade_lifecycle_source_consumption_once(
        build_source_consumption_claim(
            source_key=str(evidence["source_event_id"]),
            case_id=case_id,
            owner_evidence_id=str(evidence["evidence_id"]),
            source_role="option_anchor",
            economic_payload=evidence,
        )
    )


def test_discovery_is_create_only_and_due_owner_reviews_missing_evidence_at_deadline(
    tmp_path: Path,
) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    persist_trade_event_object(
        repo,
        _open_event(event_id="open-1", lot_id="lot-1"),
    )
    observation_start = expiration_observation_start_ms(EXPIRATION_YMD, "US")
    assert observation_start is not None

    before = discover_lifecycle_cases(
        repo,
        account="lx",
        observed_at_ms=observation_start - 1,
    )
    assert before["created_case_ids"] == []
    assert repo.list_trade_lifecycle_cases() == []

    discovered = discover_lifecycle_cases(
        repo,
        account="lx",
        observed_at_ms=observation_start,
    )
    assert len(discovered["created_case_ids"]) == 1
    case_id = discovered["created_case_ids"][0]
    lifecycle_case = repo.get_trade_lifecycle_case(case_id)
    assert lifecycle_case is not None
    assert lifecycle_case["target_contracts_by_lot"] == {"lot-1": 2}
    assert lifecycle_case["observation_start_ms"] == observation_start
    assert lifecycle_case["pending_until_ms"] == observation_start + 72 * 60 * 60 * 1000

    replay = discover_lifecycle_cases(
        repo,
        account="lx",
        observed_at_ms=observation_start + 1,
    )
    assert replay["created_case_ids"] == []
    assert replay["skipped_targeted_lot_ids"] == ["lot-1"]
    assert len(repo.list_trade_lifecycle_cases()) == 1
    assert lifecycle_case_read_model(
        repo,
        case_id=case_id,
        now_ms=observation_start + 1,
    )["lifecycle_state"] == "settlement_pending"

    after_deadline = discover_lifecycle_cases(
        repo,
        account="lx",
        observed_at_ms=observation_start + 72 * 60 * 60 * 1000,
    )
    assert after_deadline["refreshed_case_ids"] == []
    assert after_deadline["would_refresh_case_ids"] == []
    assert repo.get_trade_lifecycle_case(case_id) == lifecycle_case

    deadline_ms = observation_start + 72 * 60 * 60 * 1000
    dry_run = reconcile_due_lifecycle_cases(
        repo,
        account="lx",
        now_ms=deadline_ms,
        apply_changes=False,
        observation_collector=lambda *_args: (_ for _ in ()).throw(
            AssertionError("missing anchor must not trigger provider collection")
        ),
    )
    assert dry_run["case_count"] == 1
    assert dry_run["results"][0]["decision"] == {
        "status": "needs_review",
        "close_reason": None,
        "contracts_resolved": 0,
        "evidence_ids": [],
        "reason_codes": ["settlement_evidence_deadline_elapsed"],
        "public_transition": None,
    }
    assert repo.get_trade_lifecycle_case(case_id) == lifecycle_case
    assert repo.list_trade_lifecycle_notifications() == []

    applied = reconcile_due_lifecycle_cases(
        repo,
        account="lx",
        now_ms=deadline_ms,
        apply_changes=True,
        observation_collector=lambda *_args: (_ for _ in ()).throw(
            AssertionError("missing anchor must not trigger provider collection")
        ),
    )
    assert applied["case_count"] == 1
    assert applied["results"][0]["write_result"]["status"] == "needs_review"
    assert applied["results"][0]["write_result"]["business_state_changed"] is True
    reviewed = repo.get_trade_lifecycle_case(case_id)
    assert reviewed is not None
    assert reviewed["status"] == "needs_review"
    assert reviewed["derived_summary"]["reason_state"] == "needs_review"
    assert reviewed["derived_summary"]["lifecycle_reason_codes"] == [
        "settlement_evidence_deadline_elapsed"
    ]
    assert repo.list_trade_lifecycle_notifications() == []

    replayed = reconcile_due_lifecycle_cases(
        repo,
        account="lx",
        now_ms=deadline_ms,
        apply_changes=True,
        observation_collector=lambda *_args: (_ for _ in ()).throw(
            AssertionError("missing anchor must not trigger provider collection")
        ),
    )
    assert replayed["results"][0]["write_result"]["business_state_changed"] is False
    assert replayed["results"][0]["write_result"]["resolution_revision"] == (
        reviewed["derived_summary"]["resolution_revision"]
    )
    assert repo.list_trade_lifecycle_notifications() == []
    assert repo.list_trade_events() == [
        item for item in repo.list_trade_events() if item["event_type"] == "open"
    ]


def test_partial_then_complete_assignment_and_duplicate_reconciliation_are_idempotent(
    tmp_path: Path,
) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    persist_trade_event_object(
        repo,
        _open_event(event_id="open-1", lot_id="lot-1"),
    )
    observation_start = expiration_observation_start_ms(EXPIRATION_YMD, "US")
    assert observation_start is not None
    case_id = discover_lifecycle_cases(
        repo,
        account="lx",
        observed_at_ms=observation_start,
    )["created_case_ids"][0]

    first_evidence = _assignment_evidence(
        evidence_id="assignment-1",
        contracts=1,
        event_time_ms=observation_start + 1,
        target_lot_id="lot-1",
    )
    first = reconcile_lifecycle_evidence(
        repo,
        evidence=first_evidence,
        case_id=case_id,
        apply_changes=True,
        now_ms=observation_start + 1,
    )
    assert first.status == "applied"
    assert first.lifecycle_read_model is not None
    assert first.lifecycle_read_model["lifecycle_state"] == "partially_resolved"
    assert first.lifecycle_read_model["remaining_contracts_by_lot"] == {"lot-1": 1}
    assert first.lifecycle_read_model["actionable"] is False

    replay = reconcile_lifecycle_evidence(
        repo,
        evidence=first_evidence,
        case_id=case_id,
        apply_changes=True,
        now_ms=observation_start + 2,
    )
    assert replay.status == "idempotent"
    assert replay.ledger_result is not None
    assert replay.ledger_result["terminal_events_created"] == [False]
    assert replay.ledger_result["allocations_created"] == [False]
    assert repo.get_position_lot_fields("lot-1")["contracts_open"] == 1

    completed = reconcile_lifecycle_evidence(
        repo,
        evidence=_assignment_evidence(
            evidence_id="assignment-2",
            contracts=1,
            event_time_ms=observation_start + 3,
            target_lot_id="lot-1",
        ),
        case_id=case_id,
        apply_changes=True,
        now_ms=observation_start + 3,
    )
    assert completed.status == "applied"
    assert completed.lifecycle_read_model is not None
    assert completed.lifecycle_read_model["lifecycle_state"] == "assigned"
    assert completed.lifecycle_read_model["remaining_contracts_by_lot"] == {
        "lot-1": 0
    }
    assert len(completed.lifecycle_read_model["terminal_event_ids"]) == 2
    assert repo.get_position_lot_fields("lot-1")["contracts_open"] == 0


def test_zero_price_close_reserves_lot_without_changing_projection(
    tmp_path: Path,
) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    persist_trade_event_object(
        repo,
        _open_event(event_id="open-1", lot_id="lot-1"),
    )
    observation_start = expiration_observation_start_ms(EXPIRATION_YMD, "US")
    assert observation_start is not None
    case_id = discover_lifecycle_cases(
        repo,
        account="lx",
        observed_at_ms=observation_start,
    )["created_case_ids"][0]
    _record_zero_price_close_anchor(
        repo,
        evidence=_zero_price_close_evidence(
            evidence_id="zero-close-1",
            case_id=case_id,
            contracts=2,
            event_time_ms=observation_start + 1,
            target_lot_id="lot-1",
        )
    )

    read_model = lifecycle_case_read_model(
        repo,
        case_id=case_id,
        now_ms=observation_start + 100 * 60 * 60 * 1000,
    )
    assert read_model["schema_version"] == "option_lifecycle_read_model.v3"
    assert read_model["lifecycle_state"] == "settlement_pending"
    assert read_model["closure_fact"] == "option_leg_closed"
    assert read_model["reason_state"] == "cause_pending"
    assert read_model["reserved_contracts_by_lot"] == {"lot-1": 2}
    assert read_model["remaining_contracts_by_lot"] == {"lot-1": 2}
    assert read_model["reservation_evidence_ids"] == ["zero-close-1"]
    assert read_model["actionable"] is False
    assert repo.get_position_lot_fields("lot-1")["contracts_open"] == 2

    snapshot = decision_state_snapshot(
        repo,
        account="lx",
        portfolio_scope_id=scope_for("lx"),
    )
    assert validate_position_fact_snapshot_contract(snapshot) == ()
    assert snapshot[
        "account_lifecycle_evidence_received_at_ms_by_id"
    ].keys() == {"zero-close-1"}

    omitted_resolution = deepcopy(snapshot)
    omitted_resolution["account_lifecycle_resolution"] = (
        resolve_account_lifecycle_overlay(
            account="lx",
            cases=[],
            evidence=[],
            allocations=[],
            source_claims=[],
            timing_policies=[],
            position_lots=omitted_resolution["account_position_lots"],
        )
    )
    omitted_resolution["decision_state_fingerprint"] = (
        decision_state_snapshot_fingerprint(omitted_resolution)
    )
    assert "position_fact_lifecycle_resolution_facts_mismatch" in (
        validate_position_fact_snapshot_contract(omitted_resolution)
    )

    conflicted_facts = deepcopy(snapshot)
    conflicted_facts["account_lifecycle_source_consumptions"][0][
        "source_payload_hash"
    ] = "0" * 64
    conflicted_facts["decision_state_fingerprint"] = (
        decision_state_snapshot_fingerprint(conflicted_facts)
    )
    assert "position_fact_lifecycle_resolution_facts_mismatch" in (
        validate_position_fact_snapshot_contract(conflicted_facts)
    )

    missing_receive_times = deepcopy(snapshot)
    missing_receive_times.pop(
        "account_lifecycle_evidence_received_at_ms_by_id"
    )
    missing_receive_times["decision_state_fingerprint"] = (
        decision_state_snapshot_fingerprint(missing_receive_times)
    )
    assert "position_fact_lifecycle_evidence_receive_times_invalid" in (
        validate_position_fact_snapshot_contract(missing_receive_times)
    )

    context = build_context(
        repo.list_position_lots(),
        broker="富途",
        account="lx",
        rates={"USDCNY": 7.2},
        decision_snapshot=snapshot,
        lifecycle_now_ms=observation_start + 100 * 60 * 60 * 1000,
    )
    lifecycle = context["position_lifecycle_by_lot"]["lot-1"]
    assert lifecycle["closure_fact"] == "option_leg_closed"
    assert lifecycle["reason_state"] == "cause_pending"
    assert lifecycle["reserved_contracts_by_lot"] == {"lot-1": 2}
    assert lifecycle["remaining_contracts_by_lot"] == {"lot-1": 2}
    assert lifecycle["actionable"] is False


def test_voided_terminal_allocation_becomes_reservation_again(
    tmp_path: Path,
) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    persist_trade_event_object(
        repo,
        _open_event(event_id="open-1", lot_id="lot-1", contracts=1),
    )
    observation_start = expiration_observation_start_ms(EXPIRATION_YMD, "US")
    assert observation_start is not None
    case_id = discover_lifecycle_cases(
        repo,
        account="lx",
        observed_at_ms=observation_start,
    )["created_case_ids"][0]
    _record_zero_price_close_anchor(
        repo,
        evidence=_zero_price_close_evidence(
            evidence_id="zero-close-1",
            case_id=case_id,
            contracts=1,
            event_time_ms=observation_start + 1,
            target_lot_id="lot-1",
        )
    )
    resolved = reconcile_lifecycle_evidence(
        repo,
        evidence=_assignment_evidence(
            evidence_id="assignment-1",
            contracts=1,
            event_time_ms=observation_start + 2,
            target_lot_id="lot-1",
        ),
        case_id=case_id,
        apply_changes=True,
        now_ms=observation_start + 2,
    )
    assert resolved.lifecycle_read_model is not None
    terminal_event_id = resolved.lifecycle_read_model["terminal_event_ids"][0]
    assert resolved.lifecycle_read_model["reason_state"] == "resolved"
    assert resolved.lifecycle_read_model["reserved_contracts_by_lot"] == {
        "lot-1": 0
    }

    record_trade_event_void(
        repo,
        event_id=terminal_event_id,
        reason="incorrect settlement classification",
    )

    read_model = lifecycle_case_read_model(
        repo,
        case_id=case_id,
        now_ms=observation_start + 3,
    )
    assert read_model["terminal_event_ids"] == []
    assert read_model["voided_terminal_event_ids"] == [terminal_event_id]
    assert read_model["allocation_ids"] == []
    assert read_model["lifecycle_state"] == "settlement_pending"
    assert read_model["closure_fact"] == "option_leg_closed"
    assert read_model["reason_state"] == "cause_pending"
    assert read_model["reserved_contracts_by_lot"] == {"lot-1": 1}
    assert read_model["remaining_contracts_by_lot"] == {"lot-1": 1}
    assert repo.get_position_lot_fields("lot-1")["contracts_open"] == 1

    snapshot = decision_state_snapshot(
        repo,
        account="lx",
        portfolio_scope_id=scope_for("lx"),
    )
    assert snapshot["effective_void_event_ids"] == [terminal_event_id]
    context = build_context(
        repo.list_position_lots(),
        broker="富途",
        account="lx",
        rates={"USDCNY": 7.2},
        decision_snapshot=snapshot,
        lifecycle_now_ms=observation_start + 3,
    )
    assert context["position_lifecycle_by_lot"]["lot-1"]["reason_state"] == (
        "cause_pending"
    )


def test_lifecycle_writer_rejects_stale_related_generation_before_state_write(
    tmp_path: Path,
) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    persist_trade_event_object(
        repo,
        _open_event(event_id="open-1", lot_id="lot-1", contracts=1),
    )
    observation_start = expiration_observation_start_ms(
        EXPIRATION_YMD,
        "US",
    )
    assert observation_start is not None
    case_id = discover_lifecycle_cases(
        repo,
        account="lx",
        observed_at_ms=observation_start,
    )["created_case_ids"][0]
    prepared = lifecycle_case_read_model(
        repo,
        case_id=case_id,
        now_ms=observation_start,
    )
    expected_token = prepared["lifecycle_generation_token"]
    before = repo.get_trade_lifecycle_case(case_id)
    assert before is not None
    assert repo.insert_trade_lifecycle_case_once(
        {
            **before,
            "case_id": "competing-case",
            "case_key": "competing-case",
        }
    )

    with pytest.raises(
        ValueError,
        match="lifecycle generation compare-and-set failed",
    ):
        advance_lifecycle_case_state(
            repo,
            case_id=case_id,
            status="waiting_settlement_evidence",
            derived_summary={"reason_state": "cause_pending"},
            public_transition=None,
            expected_lifecycle_generation_token=expected_token,
        )

    assert repo.get_trade_lifecycle_case(case_id) == before
    assert repo.list_trade_lifecycle_notifications() == []


def test_ambiguous_quantity_binding_records_conflict_without_closing_lots(
    tmp_path: Path,
) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    persist_trade_event_object(
        repo,
        _open_event(event_id="open-1", lot_id="lot-1", contracts=1),
    )
    persist_trade_event_object(
        repo,
        _open_event(event_id="open-2", lot_id="lot-2", contracts=1),
    )
    observation_start = expiration_observation_start_ms(EXPIRATION_YMD, "US")
    assert observation_start is not None
    case_id = discover_lifecycle_cases(
        repo,
        account="lx",
        observed_at_ms=observation_start,
    )["created_case_ids"][0]

    result = reconcile_lifecycle_evidence(
        repo,
        evidence=_assignment_evidence(
            evidence_id="ambiguous",
            contracts=1,
            event_time_ms=observation_start + 1,
            target_lot_id=None,
        ),
        case_id=case_id,
        apply_changes=True,
        now_ms=observation_start + 1,
    )
    assert result.status == "conflict"
    assert result.reason_codes == ("ambiguous_quantity_binding",)
    assert result.ledger_result is not None
    assert result.ledger_result["terminal_event_ids"] == []
    assert repo.get_position_lot_fields("lot-1")["contracts_open"] == 1
    assert repo.get_position_lot_fields("lot-2")["contracts_open"] == 1
    assert repo.get_trade_lifecycle_case(case_id)["status"] == "conflict"


def test_late_assignment_after_expire_close_records_conflict_without_second_close(
    tmp_path: Path,
) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    persist_trade_event_object(
        repo,
        _open_event(event_id="open-1", lot_id="lot-1", contracts=1),
    )
    observation_start = expiration_observation_start_ms(EXPIRATION_YMD, "US")
    assert observation_start is not None
    case_id = discover_lifecycle_cases(
        repo,
        account="lx",
        observed_at_ms=observation_start,
    )["created_case_ids"][0]

    expiry = reconcile_lifecycle_evidence(
        repo,
        evidence=_expire_close_evidence(
            evidence_id="expiry-1",
            contracts=1,
            event_time_ms=observation_start + 1,
            target_lot_id="lot-1",
        ),
        case_id=case_id,
        apply_changes=True,
        now_ms=observation_start + 1,
    )
    assert expiry.status == "applied"
    assert expiry.lifecycle_read_model is not None
    assert expiry.lifecycle_read_model["lifecycle_state"] == "expired_unassigned"
    terminal_event_count = len(
        [
            item
            for item in repo.list_trade_events()
            if item["event_type"] != "open"
        ]
    )

    late = reconcile_lifecycle_evidence(
        repo,
        evidence=_assignment_evidence(
            evidence_id="assignment-late",
            contracts=1,
            event_time_ms=observation_start + 2,
            target_lot_id="lot-1",
        ),
        case_id=case_id,
        apply_changes=True,
        now_ms=observation_start + 2,
    )
    assert late.status == "conflict"
    assert late.reason_codes == (
        "late_settlement_conflicts_with_expire_close",
    )
    assert len(
        [
            item
            for item in repo.list_trade_events()
            if item["event_type"] != "open"
        ]
    ) == terminal_event_count
    assert repo.get_position_lot_fields("lot-1")["contracts_open"] == 0
    assert repo.get_trade_lifecycle_case(case_id)["status"] == "conflict"
    assert late.lifecycle_read_model is not None
    assert late.lifecycle_read_model["lifecycle_state"] == "conflict"


def test_position_context_consumes_coherent_lifecycle_snapshot_and_combo_is_invalidated(
    tmp_path: Path,
) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    persist_trade_event_object(
        repo,
        _open_event(event_id="open-1", lot_id="lot-1"),
    )
    observation_start = expiration_observation_start_ms(EXPIRATION_YMD, "US")
    assert observation_start is not None
    case_id = discover_lifecycle_cases(
        repo,
        account="lx",
        observed_at_ms=observation_start,
    )["created_case_ids"][0]
    reconcile_lifecycle_evidence(
        repo,
        evidence=_assignment_evidence(
            evidence_id="assignment-1",
            contracts=1,
            event_time_ms=observation_start + 1,
            target_lot_id="lot-1",
        ),
        case_id=case_id,
        apply_changes=True,
        now_ms=observation_start + 1,
    )
    snapshot = decision_state_snapshot(
        repo,
        account="lx",
        portfolio_scope_id=scope_for("lx"),
    )
    context = build_context(
        repo.list_position_lots(),
        broker="富途",
        account="lx",
        rates={"USDCNY": 7.2},
        decision_snapshot=snapshot,
        lifecycle_now_ms=observation_start + 1,
    )
    lifecycle = context["position_lifecycle_by_lot"]["lot-1"]
    assert lifecycle["lifecycle_state"] == "partially_resolved"
    assert lifecycle["remaining_contracts_by_lot"] == {"lot-1": 1}
    assert lifecycle["actionable"] is False
    assert context["open_positions_min"][0]["lifecycle_state"] == "partially_resolved"
    assert context["decision_state_fingerprint"] == snapshot[
        "decision_state_fingerprint"
    ]

    groups = build_option_group_inventory(
        [
            {
                "record_id": "lot-put",
                "account": "lx",
                "symbol": "NVDA",
                "option_type": "put",
                "side": "short",
                "contracts": 1,
                "contracts_open": 1,
                "contracts_closed": 0,
                "expiration_ymd": "2026-08-21",
                "strategy": "combo_yield",
                "strategy_group_id": "combo_yield:lx:1",
                "leg_role": "funding_put",
                "lifecycle_state": "partially_resolved",
                "resolved_contracts_by_lot": {"lot-put": 1},
            },
            {
                "record_id": "lot-call",
                "account": "lx",
                "symbol": "NVDA",
                "option_type": "call",
                "side": "long",
                "contracts": 1,
                "contracts_open": 1,
                "contracts_closed": 0,
                "expiration_ymd": "2026-08-21",
                "strategy": "combo_yield",
                "strategy_group_id": "combo_yield:lx:1",
                "leg_role": "participation_call",
            },
        ]
    )
    assert groups[0]["summary_classification"] == "review_required"
    assert "lifecycle_terminal_allocation" in groups[0]["inventory_issues"]


def _broker_pair_evidence(
    *,
    evidence_id: str,
    event_time_ms: int,
    option_event_time_ms: int,
) -> dict:
    return {
        "evidence_id": evidence_id,
        "source_type": "broker_settlement_pair",
        "source_event_id": (
            f"futu:lx:1001:option-{evidence_id}|"
            f"futu:lx:1001:stock-{evidence_id}"
        ),
        "evidence_type": "assignment",
        "account": "lx",
        "symbol": "NVDA",
        "option_type": "put",
        "position_side": "short",
        "strike": 100,
        "expiration_ymd": EXPIRATION_YMD,
        "contracts": 1,
        "event_time_ms": event_time_ms,
        "option_event_time_ms": option_event_time_ms,
        "stock_settlement": {
            "source_event_id": (
                f"futu:lx:1001:stock-{evidence_id}"
            ),
            "futu_account_id": "1001",
            "symbol": "NVDA",
            "side": "buy",
            "shares": 100,
            "price": 100,
            "event_time_ms": event_time_ms,
        },
    }


def test_broker_pair_writer_rejects_global_case_ambiguity(
    tmp_path: Path,
) -> None:
    repo = SQLiteOptionPositionsRepository(
        tmp_path / "ledger.sqlite3"
    )
    persist_trade_event_object(
        repo,
        _open_event(event_id="open-1", lot_id="lot-1"),
    )
    observation_start = expiration_observation_start_ms(
        EXPIRATION_YMD,
        "US",
    )
    assert observation_start is not None
    case_id = discover_lifecycle_cases(
        repo,
        account="lx",
        observed_at_ms=observation_start,
    )["created_case_ids"][0]
    assert repo.bind_trade_lifecycle_case_futu_account_once(
        case_id=case_id,
        futu_account_id="1001",
    )
    anchor_source = "futu:lx:1001:deadline-anchor"
    anchor = {
        "evidence_id": "deadline-anchor",
        "case_id": case_id,
        "source_type": "futu_broker_deal",
        "source_event_id": anchor_source,
        "evidence_type": "option_zero_price_close",
        "account": "lx",
        "futu_account_id": "1001",
        "symbol": "NVDA",
        "option_type": "put",
        "position_side": "short",
        "strike": 100,
        "expiration_ymd": EXPIRATION_YMD,
        "contracts": 1,
        "price": 0,
        "event_time_ms": observation_start,
    }
    assert repo.insert_trade_lifecycle_evidence_once(anchor)
    assert repo.insert_trade_lifecycle_source_consumption_once(
        build_source_consumption_claim(
            source_key=anchor_source,
            case_id=case_id,
            owner_evidence_id="deadline-anchor",
            source_role="option_anchor",
            economic_payload=anchor,
        )
    )
    original = repo.get_trade_lifecycle_case(case_id)
    assert original is not None
    duplicate = {
        **original,
        "case_id": "duplicate-case",
        "case_key": "duplicate-case",
    }
    assert repo.insert_trade_lifecycle_case_once(duplicate)

    result = reconcile_lifecycle_evidence(
        repo,
        evidence=_broker_pair_evidence(
            evidence_id="ambiguous-pair",
            event_time_ms=observation_start + 2,
            option_event_time_ms=observation_start + 1,
        ),
        apply_changes=True,
        now_ms=observation_start + 2,
    )

    assert result.status == "conflict"
    assert result.reason_codes == (
        "ambiguous_lifecycle_case_match",
    )


def test_broker_pair_writer_enforces_settlement_deadline(
    tmp_path: Path,
) -> None:
    repo = SQLiteOptionPositionsRepository(
        tmp_path / "ledger.sqlite3"
    )
    persist_trade_event_object(
        repo,
        _open_event(event_id="open-1", lot_id="lot-1"),
    )
    observation_start = expiration_observation_start_ms(
        EXPIRATION_YMD,
        "US",
    )
    assert observation_start is not None
    case_id = discover_lifecycle_cases(
        repo,
        account="lx",
        observed_at_ms=observation_start,
    )["created_case_ids"][0]
    assert repo.bind_trade_lifecycle_case_futu_account_once(
        case_id=case_id,
        futu_account_id="1001",
    )
    anchor_source = "futu:lx:1001:deadline-anchor"
    anchor = {
        "evidence_id": "deadline-anchor",
        "case_id": case_id,
        "source_type": "futu_broker_deal",
        "source_event_id": anchor_source,
        "evidence_type": "option_zero_price_close",
        "account": "lx",
        "futu_account_id": "1001",
        "symbol": "NVDA",
        "option_type": "put",
        "position_side": "short",
        "strike": 100,
        "expiration_ymd": EXPIRATION_YMD,
        "contracts": 1,
        "price": 0,
        "event_time_ms": observation_start,
    }
    assert repo.insert_trade_lifecycle_evidence_once(anchor)
    assert repo.insert_trade_lifecycle_source_consumption_once(
        build_source_consumption_claim(
            source_key=anchor_source,
            case_id=case_id,
            owner_evidence_id="deadline-anchor",
            source_role="option_anchor",
            economic_payload=anchor,
        )
    )
    policy = build_lifecycle_timing_policy(
        case_id=case_id,
        market="US",
        expiration_ymd=EXPIRATION_YMD,
        contract_metadata={
            "settlement_style": "physical",
            "underlying_security_type": "equity",
            "last_trade_cutoff_ms": observation_start - 1,
            "last_trade_cutoff_source": (
                "instrument_policy_registry"
            ),
        },
        trading_days=[
            {"date": "2026-08-21", "type": "TRADING"},
            {"date": "2026-08-24", "type": "TRADING"},
            {"date": "2026-08-25", "type": "TRADING"},
        ],
        calendar_source="test_calendar",
        calendar_observed_at_ms=observation_start,
    )
    assert repo.insert_trade_lifecycle_timing_policy_once(policy)
    deadline = int(policy["settlement_deadline_ms"])

    result = reconcile_lifecycle_evidence(
        repo,
        evidence=_broker_pair_evidence(
            evidence_id="after-deadline",
            event_time_ms=deadline + 1,
            option_event_time_ms=observation_start,
        ),
        apply_changes=True,
        now_ms=deadline + 1,
    )

    assert result.status == "conflict"
    assert "stock_settlement_after_deadline" in result.reason_codes
