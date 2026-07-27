from __future__ import annotations

from pathlib import Path

from domain.domain.ledger import ContractKey, TradeEvent
from domain.domain.combo_yield_lifecycle import build_option_group_inventory
from domain.domain.option_lifecycle import expiration_observation_start_ms
from domain.domain.position_advice_authority import scope_for
from src.application.ledger.decision_snapshot import decision_state_snapshot
from src.application.ledger.repository import SQLiteOptionPositionsRepository
from src.application.ledger.writer import persist_trade_event_object
from src.application.positions.context_builder import build_context
from src.application.trades.lifecycle_reconciliation import (
    discover_lifecycle_cases,
    lifecycle_case_read_model,
    reconcile_lifecycle_evidence,
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


def test_discovery_freezes_case_at_observation_start_and_deadline_only_reviews(
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
    assert after_deadline["refreshed_case_ids"] == [case_id]
    reviewed = repo.get_trade_lifecycle_case(case_id)
    assert reviewed is not None
    assert reviewed["status"] == "needs_review"
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
