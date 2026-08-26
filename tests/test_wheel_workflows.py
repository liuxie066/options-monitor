from __future__ import annotations

from pathlib import Path

import pytest

import src.application.ledger.manual_trades as ledger_manual_trades
from domain.domain.ledger import ContractKey, TradeEvent
from domain.domain.option_position_lots import OpenPositionCommand
from src.application.ledger.commands import record_manual_assignment
from src.application.ledger.repository import SQLiteOptionPositionsRepository
from src.application.ledger.writer import (
    persist_trade_event_objects_atomically,
    persist_trade_event_with_wheel_intent,
)
from src.application.trades.normalizer import NormalizedTradeDeal
from src.application.positions.workflows import execute_manual_assignment
from src.application.wheel import (
    build_wheel_read_model,
    cancel_wheel_call_intent,
    confirm_wheel_call_linkage,
    create_wheel_call_intent,
    end_wheel_lifecycle,
    reject_wheel_call_linkage,
)


def _assign_short_put(
    tmp_path: Path,
    *,
    wheel_start_enabled: bool,
    contracts: int = 1,
) -> tuple[SQLiteOptionPositionsRepository, str, str]:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    ledger_manual_trades.persist_manual_open_event(
        repo,
        OpenPositionCommand(
            broker="富途",
            account="lx",
            symbol="NVDA",
            option_type="put",
            side="short",
            contracts=contracts,
            currency="USD",
            strike=100,
            multiplier=100,
            expiration_ymd="2026-08-21",
            premium_per_share=2.5,
            opened_at_ms=1_000,
        ),
    )
    put_lot_id = str(repo.list_position_lots()[0]["record_id"])
    result = record_manual_assignment(
        repo,
        record_id=put_lot_id,
        contracts_to_close=contracts,
        stock_side="buy",
        stock_qty=contracts * 100,
        stock_price=100,
        as_of_ms=2_000,
        request_id="put-assignment-1",
        wheel_start_enabled=wheel_start_enabled,
    )
    assignment_event_id = str(result["result"]["event_id"])
    return repo, put_lot_id, f"assigned-stock-{assignment_event_id}"


def _create_call_intent(
    repo: SQLiteOptionPositionsRepository,
    stock_lot_id: str,
) -> tuple[dict, dict]:
    batch = build_wheel_read_model(repo, "lx", 3_000)["batches"][0]
    snapshot = {
        "account": "lx",
        "snapshot_hash": "snapshot-1",
        "batches": [
            {
                "stock_lot_id": stock_lot_id,
                "batch_generation_hash": batch["batch_generation_hash"],
                "final_candidate": {
                    "final_candidate_id": "candidate-1",
                    "symbol": "NVDA",
                    "stock_lot_id": stock_lot_id,
                    "strike": 110,
                    "expiration_ymd": "2026-08-21",
                    "granted_contracts": 1,
                    "multiplier": 100,
                },
            }
        ],
    }
    coverage = {
        "account": "lx",
        "symbol": "NVDA",
        "capacity_identity_hash": "capacity-1",
        "status": "available",
        "shares_eligible": 100,
        "shares_locked": 0,
        "shares_reserved": 0,
        "shares_available_for_cover": 100,
    }
    created = create_wheel_call_intent(
        repo,
        candidate_snapshot=snapshot,
        account="lx",
        stock_lot_id=stock_lot_id,
        final_candidate_id="candidate-1",
        expected_snapshot_hash="snapshot-1",
        expected_batch_generation_hash=batch["batch_generation_hash"],
        expires_at_ms=10_000,
        request_id="intent-create-1",
        actor="tester",
        coverage_fact=coverage,
        apply_changes=True,
        as_of_ms=4_000,
    )
    return created, coverage


def _open_unlinked_call(
    repo: SQLiteOptionPositionsRepository,
    *,
    event_time_ms: int = 3_000,
) -> str:
    call_lot_id = "unlinked-call-lot-1"
    persist_trade_event_objects_atomically(
        repo,
        [
            TradeEvent(
                event_id="unlinked-call-open-1",
                event_type="open",
                event_time_ms=event_time_ms,
                contract_key=ContractKey.from_values(
                    broker="富途",
                    account="lx",
                    underlying_symbol="NVDA",
                    option_type="call",
                    position_side="short",
                    strike=110,
                    expiration_ymd="2026-08-21",
                ),
                contracts=1,
                price=2,
                currency="USD",
                source="test",
                multiplier=100,
                lot_id=call_lot_id,
            )
        ],
    )
    return call_lot_id


def test_assignment_starts_wheel_and_manual_end_is_cas_idempotent(
    tmp_path: Path,
) -> None:
    repo, _put_lot_id, stock_lot_id = _assign_short_put(
        tmp_path,
        wheel_start_enabled=True,
    )
    batch = build_wheel_read_model(repo, "lx", 3_000)["batches"][0]

    preview = end_wheel_lifecycle(
        repo,
        account="lx",
        stock_lot_id=stock_lot_id,
        expected_batch_generation_hash=batch["batch_generation_hash"],
        request_id="end-wheel-1",
        actor="tester",
        as_of_ms=4_000,
    )
    applied = end_wheel_lifecycle(
        repo,
        account="lx",
        stock_lot_id=stock_lot_id,
        expected_batch_generation_hash=batch["batch_generation_hash"],
        request_id="end-wheel-1",
        actor="tester",
        apply_changes=True,
        as_of_ms=4_000,
    )
    replay = end_wheel_lifecycle(
        repo,
        account="lx",
        stock_lot_id=stock_lot_id,
        expected_batch_generation_hash=batch["batch_generation_hash"],
        request_id="end-wheel-1",
        actor="tester",
        apply_changes=True,
        as_of_ms=5_000,
    )

    assert preview["dry_run"] is True
    assert preview["lifecycle_status_after"] == "manual_ended"
    assert applied["write_applied"] is True
    assert replay["idempotent"] is True
    assert replay["write_applied"] is False
    assert len(repo.list_wheel_events(account="lx")) == 2
    terminal = build_wheel_read_model(repo, "lx", 5_000)["batches"][0]
    assert terminal["lifecycle_status"] == "manual_ended"
    assert terminal["phase"] is None


def test_combo_funding_put_assignment_starts_wheel_and_preserves_combo_tail(
    tmp_path: Path,
) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    group_id = "combo_yield:lx:nvda-20260821"
    for option_type, side, strike, role, opened_at_ms in (
        ("put", "short", 100, "funding_put", 1_000),
        ("call", "long", 120, "participation_call", 1_100),
    ):
        ledger_manual_trades.persist_manual_open_event(
            repo,
            OpenPositionCommand(
                broker="富途",
                account="lx",
                symbol="NVDA",
                option_type=option_type,
                side=side,
                contracts=1,
                currency="USD",
                strike=strike,
                multiplier=100,
                expiration_ymd="2026-08-21",
                premium_per_share=2.5,
                opened_at_ms=opened_at_ms,
                strategy_snapshot={
                    "strategy": "combo_yield",
                    "leg_role": role,
                    "strategy_group_id": group_id,
                },
            ),
        )

    lots = repo.list_position_lots()
    funding_put = next(row for row in lots if row["fields"]["option_type"] == "put")
    assignment = record_manual_assignment(
        repo,
        record_id=str(funding_put["record_id"]),
        contracts_to_close=1,
        stock_side="buy",
        stock_qty=100,
        stock_price=100,
        as_of_ms=2_000,
        request_id="combo-funding-put-assignment-1",
        wheel_start_enabled=True,
    )
    stock_lot_id = f'assigned-stock-{assignment["result"]["event_id"]}'
    model = build_wheel_read_model(repo, "lx", 3_000)
    assigned_stock = model["assigned_stock_projection"]["_all_assigned_stock_lots"][0]
    residual_call = next(
        row["fields"]
        for row in repo.list_position_lots()
        if row["fields"]["option_type"] == "call"
    )

    assert model["batches"][0]["stock_lot_id"] == stock_lot_id
    assert model["batches"][0]["phase"] == "ready"
    assert assigned_stock["strategy_group_id"] == group_id
    assert assigned_stock["leg_role"] == "assigned_stock"
    assert assigned_stock["source_option_leg_role"] == "funding_put"
    assert residual_call["status"] == "open"
    assert residual_call["strategy_group_id"] == group_id
    assert residual_call["leg_role"] == "participation_call"


def test_assignment_replay_does_not_backfill_wheel_start(tmp_path: Path) -> None:
    repo, put_lot_id, _stock_lot_id = _assign_short_put(
        tmp_path,
        wheel_start_enabled=False,
    )

    replay = record_manual_assignment(
        repo,
        record_id=put_lot_id,
        contracts_to_close=1,
        stock_side="buy",
        stock_qty=100,
        stock_price=100,
        as_of_ms=2_000,
        request_id="put-assignment-1",
        wheel_start_enabled=True,
    )

    assert replay["result"]["created"] is False
    assert repo.list_wheel_events(account="lx") == []


def test_manual_assignment_uses_runtime_wheel_config_to_start_lifecycle(
    tmp_path: Path,
) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    ledger_manual_trades.persist_manual_open_event(
        repo,
        OpenPositionCommand(
            broker="富途",
            account="lx",
            symbol="NVDA",
            option_type="put",
            side="short",
            contracts=1,
            currency="USD",
            strike=100,
            multiplier=100,
            expiration_ymd="2026-08-21",
            premium_per_share=2.5,
            opened_at_ms=1_000,
        ),
    )

    execute_manual_assignment(
        repo,
        record_id=str(repo.list_position_lots()[0]["record_id"]),
        contracts_to_close=1,
        stock_side="buy",
        stock_qty=100,
        stock_price=100,
        dry_run=False,
        as_of_ms=2_000,
        request_id="configured-assignment-1",
        runtime_config={"wheel": {"enabled": True, "accounts": ["lx"]}},
    )

    assert build_wheel_read_model(repo, "lx", 3_000)["batches"][0]["phase"] == "ready"


def test_intent_creation_revalidates_current_ledger_share_coverage(
    tmp_path: Path,
) -> None:
    repo, _put_lot_id, stock_lot_id = _assign_short_put(
        tmp_path,
        wheel_start_enabled=True,
    )
    batch = build_wheel_read_model(repo, "lx", 3_000)["batches"][0]
    _open_unlinked_call(repo, event_time_ms=3_500)
    snapshot = {
        "account": "lx",
        "snapshot_hash": "snapshot-stale",
        "batches": [
            {
                "stock_lot_id": stock_lot_id,
                "batch_generation_hash": batch["batch_generation_hash"],
                "final_candidate": {
                    "final_candidate_id": "candidate-stale",
                    "symbol": "NVDA",
                    "stock_lot_id": stock_lot_id,
                    "strike": 110,
                    "expiration_ymd": "2026-08-21",
                    "granted_contracts": 1,
                    "multiplier": 100,
                },
            }
        ],
    }

    with pytest.raises(
        ValueError,
        match="batch generation changed|coverage is unavailable|coverage is insufficient",
    ):
        create_wheel_call_intent(
            repo,
            candidate_snapshot=snapshot,
            account="lx",
            stock_lot_id=stock_lot_id,
            final_candidate_id="candidate-stale",
            expected_snapshot_hash="snapshot-stale",
            expected_batch_generation_hash=batch["batch_generation_hash"],
            expires_at_ms=10_000,
            request_id="intent-stale-1",
            actor="tester",
            coverage_fact={
                "account": "lx",
                "symbol": "NVDA",
                "capacity_identity_hash": "capacity-before-race",
                "status": "available",
                "shares_eligible": 100,
                "shares_locked": 0,
                "shares_reserved": 0,
                "shares_available_for_cover": 100,
            },
            apply_changes=True,
            as_of_ms=4_000,
        )
    assert not any(
        event["event_type"] == "wheel_call_intent_created"
        for event in repo.list_wheel_events(account="lx")
    )


def test_wheel_call_intent_create_and_cancel(tmp_path: Path) -> None:
    repo, _put_lot_id, stock_lot_id = _assign_short_put(
        tmp_path,
        wheel_start_enabled=True,
    )
    created, _coverage = _create_call_intent(repo, stock_lot_id)
    pending = build_wheel_read_model(repo, "lx", 5_000)["batches"][0]
    cancelled = cancel_wheel_call_intent(
        repo,
        account="lx",
        stock_lot_id=stock_lot_id,
        intent_id=created["intent_id"],
        expected_batch_generation_hash=pending["batch_generation_hash"],
        request_id="intent-cancel-1",
        actor="tester",
        broker_order_inactive_confirmed=True,
        reason="order cancelled",
        apply_changes=True,
        as_of_ms=6_000,
    )

    ready = build_wheel_read_model(repo, "lx", 7_000)["batches"][0]
    assert created["status"] == "created"
    assert pending["phase"] == "call_pending"
    assert pending["active_intent_reserved_shares"] == 100
    assert cancelled["status"] == "cancelled"
    assert ready["phase"] == "ready"
    assert ready["active_intent_ids"] == []


def test_short_call_fill_consumes_matching_intent_atomically(tmp_path: Path) -> None:
    repo, _put_lot_id, stock_lot_id = _assign_short_put(
        tmp_path,
        wheel_start_enabled=True,
    )
    created, coverage = _create_call_intent(repo, stock_lot_id)
    coverage = {**coverage, "shares_available_for_cover": 0}
    deal = NormalizedTradeDeal(
        broker="富途",
        futu_account_id="REAL_1",
        internal_account="lx",
        deal_id="call-fill-1",
        order_id="call-order-1",
        symbol="NVDA",
        option_type="call",
        side="sell",
        position_effect="open",
        contracts=1,
        price=2,
        strike=110,
        multiplier=100,
        multiplier_source="broker",
        expiration_ymd="2026-08-21",
        currency="USD",
        trade_time_ms=5_000,
        raw_payload={"deal_id": "call-fill-1"},
    )

    result = persist_trade_event_with_wheel_intent(repo, deal, coverage).to_dict()

    batch = build_wheel_read_model(repo, "lx", 6_000)["batches"][0]
    call_lot = next(
        item
        for item in repo.list_position_lots()
        if item["fields"].get("option_type") == "call"
    )
    assert result["wheel_linkage_status"] == "matched_intent"
    assert result["wheel_intent_event_id"]
    assert call_lot["fields"]["strategy"] == "wheel"
    assert call_lot["fields"]["source_stock_lot_id"] == stock_lot_id
    assert batch["phase"] == "call_open"
    assert batch["active_intent_ids"] == []
    assert created["intent_id"] not in batch["active_intent_ids"]


def test_unmatched_short_call_fill_stays_unlinked_and_is_still_recorded(
    tmp_path: Path,
) -> None:
    repo, _put_lot_id, _stock_lot_id = _assign_short_put(
        tmp_path,
        wheel_start_enabled=True,
    )
    deal = NormalizedTradeDeal(
        broker="富途",
        futu_account_id="REAL_1",
        internal_account="lx",
        deal_id="unmatched-call-fill",
        order_id="unmatched-call-order",
        symbol="NVDA",
        option_type="call",
        side="sell",
        position_effect="open",
        contracts=1,
        price=2,
        strike=110,
        multiplier=100,
        multiplier_source="broker",
        expiration_ymd="2026-08-21",
        currency="USD",
        trade_time_ms=5_000,
        raw_payload={"deal_id": "unmatched-call-fill"},
    )
    coverage = {
        "account": "lx",
        "symbol": "NVDA",
        "capacity_identity_hash": "capacity-1",
        "status": "available",
        "shares_available_for_cover": 100,
    }

    result = persist_trade_event_with_wheel_intent(repo, deal, coverage).to_dict()

    call_lot = next(
        item
        for item in repo.list_position_lots()
        if item["fields"].get("option_type") == "call"
    )
    model = build_wheel_read_model(repo, "lx", 6_000)
    assert result["created"] is True
    assert result["wheel_linkage_status"] == "no_matching_intent"
    assert call_lot["fields"].get("strategy") is None
    assert model["batches"][0]["phase"] == "linkage_unresolved"
    assert len(model["linkage_candidates"]) == 1


def test_manual_wheel_call_linkage_confirm_uses_narrow_adjust(tmp_path: Path) -> None:
    repo, _put_lot_id, stock_lot_id = _assign_short_put(
        tmp_path,
        wheel_start_enabled=True,
    )
    call_lot_id = _open_unlinked_call(repo)
    model = build_wheel_read_model(repo, "lx", 4_000)
    candidate = model["linkage_candidates"][0]

    result = confirm_wheel_call_linkage(
        repo,
        account="lx",
        call_record_id=call_lot_id,
        stock_lot_id=stock_lot_id,
        linkage_candidate_id=candidate["linkage_candidate_id"],
        expected_input_hash=candidate["input_snapshot_hash"],
        expected_batch_generation_hash=candidate["batch_generation_hash"],
        request_id="link-confirm-1",
        actor="tester",
        coverage_fact={
            "account": "lx",
            "symbol": "NVDA",
            "capacity_identity_hash": "capacity-1",
            "status": "insufficient",
            "shares_available_for_cover": 0,
        },
        apply_changes=True,
        as_of_ms=5_000,
    )

    fields = repo.get_position_lot_fields(call_lot_id)
    batch = build_wheel_read_model(repo, "lx", 6_000)["batches"][0]
    adjust = next(item for item in repo.list_trade_events() if item["event_type"] == "adjust")
    assert result["status"] == "confirmed"
    assert fields["strategy"] == "wheel"
    assert fields["source_stock_lot_id"] == stock_lot_id
    assert set(adjust["raw_payload"]["patch"]) == {
        "last_action_at",
        "strategy",
        "leg_role",
        "source_stock_lot_id",
    }
    assert batch["phase"] == "call_open"


def test_manual_linkage_consumes_unique_intent_valid_at_fill(tmp_path: Path) -> None:
    repo, _put_lot_id, stock_lot_id = _assign_short_put(
        tmp_path,
        wheel_start_enabled=True,
    )
    created, coverage = _create_call_intent(repo, stock_lot_id)
    call_lot_id = _open_unlinked_call(repo, event_time_ms=5_000)
    candidate = build_wheel_read_model(repo, "lx", 6_000)["linkage_candidates"][0]

    result = confirm_wheel_call_linkage(
        repo,
        account="lx",
        call_record_id=call_lot_id,
        stock_lot_id=stock_lot_id,
        linkage_candidate_id=candidate["linkage_candidate_id"],
        expected_input_hash=candidate["input_snapshot_hash"],
        expected_batch_generation_hash=candidate["batch_generation_hash"],
        request_id="link-confirm-with-intent",
        actor="tester",
        coverage_fact=coverage,
        apply_changes=True,
        as_of_ms=6_000,
    )

    batch = build_wheel_read_model(repo, "lx", 7_000)["batches"][0]
    assert result["intent_event_id"]
    assert created["intent_id"] not in batch["active_intent_ids"]
    assert batch["phase"] == "call_open"


def test_manual_wheel_call_linkage_rejects_only_selected_relation(
    tmp_path: Path,
) -> None:
    repo, _put_lot_id, stock_lot_id = _assign_short_put(
        tmp_path,
        wheel_start_enabled=True,
    )
    call_lot_id = _open_unlinked_call(repo)
    candidate = build_wheel_read_model(repo, "lx", 4_000)["linkage_candidates"][0]

    result = reject_wheel_call_linkage(
        repo,
        account="lx",
        call_record_id=call_lot_id,
        stock_lot_id=stock_lot_id,
        linkage_candidate_id=candidate["linkage_candidate_id"],
        expected_input_hash=candidate["input_snapshot_hash"],
        expected_batch_generation_hash=candidate["batch_generation_hash"],
        request_id="link-reject-1",
        actor="tester",
        reason="not this Wheel batch",
        apply_changes=True,
        as_of_ms=5_000,
    )

    model = build_wheel_read_model(repo, "lx", 6_000)
    assert result["status"] == "rejected"
    assert model["linkage_candidates"] == []
    assert repo.get_position_lot_fields(call_lot_id).get("strategy") is None
    assert model["batches"][0]["phase"] == "ready"


def test_partial_wheel_call_assignment_keeps_batch_active(tmp_path: Path) -> None:
    repo, _put_lot_id, stock_lot_id = _assign_short_put(
        tmp_path,
        wheel_start_enabled=True,
        contracts=2,
    )
    call_lot_id = "wheel-call-lot-partial"
    persist_trade_event_objects_atomically(
        repo,
        [
            TradeEvent(
                event_id="wheel-call-open-partial",
                event_type="open",
                event_time_ms=3_000,
                contract_key=ContractKey.from_values(
                    broker="富途",
                    account="lx",
                    underlying_symbol="NVDA",
                    option_type="call",
                    position_side="short",
                    strike=110,
                    expiration_ymd="2026-08-21",
                ),
                contracts=1,
                price=2,
                currency="USD",
                source="test",
                multiplier=100,
                lot_id=call_lot_id,
                raw_payload={
                    "strategy": "wheel",
                    "leg_role": "wheel_call",
                    "source_stock_lot_id": stock_lot_id,
                },
            )
        ],
    )

    record_manual_assignment(
        repo,
        record_id=call_lot_id,
        contracts_to_close=1,
        stock_side="sell",
        stock_qty=100,
        stock_price=110,
        as_of_ms=4_000,
    )

    batch = build_wheel_read_model(repo, "lx", 5_000)["batches"][0]
    assert [item["event_type"] for item in repo.list_wheel_events(account="lx")] == [
        "wheel_started"
    ]
    assert batch["lifecycle_status"] == "active"
    assert batch["shares_remaining"] == 100
    assert batch["phase"] == "ready"


def test_wheel_call_assignment_closes_batch_in_same_transaction(
    tmp_path: Path,
) -> None:
    repo, _put_lot_id, stock_lot_id = _assign_short_put(
        tmp_path,
        wheel_start_enabled=True,
    )
    call_lot_id = "wheel-call-lot-1"
    persist_trade_event_objects_atomically(
        repo,
        [
            TradeEvent(
                event_id="wheel-call-open-1",
                event_type="open",
                event_time_ms=3_000,
                contract_key=ContractKey.from_values(
                    broker="富途",
                    account="lx",
                    underlying_symbol="NVDA",
                    option_type="call",
                    position_side="short",
                    strike=110,
                    expiration_ymd="2026-08-21",
                ),
                contracts=1,
                price=2,
                currency="USD",
                source="test",
                multiplier=100,
                lot_id=call_lot_id,
                raw_payload={
                    "strategy": "wheel",
                    "leg_role": "wheel_call",
                    "source_stock_lot_id": stock_lot_id,
                },
            )
        ],
    )

    record_manual_assignment(
        repo,
        record_id=call_lot_id,
        contracts_to_close=1,
        stock_side="sell",
        stock_qty=100,
        stock_price=110,
        as_of_ms=4_000,
        request_id="call-assignment-1",
    )

    events = repo.list_wheel_events(account="lx")
    batch = build_wheel_read_model(repo, "lx", 5_000)["batches"][0]
    assert [item["event_type"] for item in events] == [
        "wheel_started",
        "wheel_called_away",
    ]
    assert batch["lifecycle_status"] == "called_away"
    assert batch["shares_remaining"] == 0
    assert batch["integrity_status"] == "trusted"


def test_wheel_start_failure_rolls_back_assignment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    ledger_manual_trades.persist_manual_open_event(
        repo,
        OpenPositionCommand(
            broker="富途",
            account="lx",
            symbol="NVDA",
            option_type="put",
            side="short",
            contracts=1,
            currency="USD",
            strike=100,
            multiplier=100,
            expiration_ymd="2026-08-21",
            premium_per_share=2.5,
            opened_at_ms=1_000,
        ),
    )
    put_lot_id = str(repo.list_position_lots()[0]["record_id"])

    def _fail(*_args: object, **_kwargs: object) -> bool:
        raise ValueError("forced Wheel companion failure")

    monkeypatch.setattr(repo, "append_wheel_event_once", _fail)
    with pytest.raises(ValueError, match="forced Wheel companion failure"):
        record_manual_assignment(
            repo,
            record_id=put_lot_id,
            contracts_to_close=1,
            stock_side="buy",
            stock_qty=100,
            stock_price=100,
            as_of_ms=2_000,
            wheel_start_enabled=True,
        )

    assert [item["event_type"] for item in repo.list_trade_events()] == ["open"]
    assert repo.get_position_lot_fields(put_lot_id)["status"] == "open"
