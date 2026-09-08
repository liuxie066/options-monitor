from __future__ import annotations

from pathlib import Path
from dataclasses import replace
from unittest.mock import patch

import pytest

import src.application.ledger.manual_trades as ledger_manual_trades
import src.application.wheel.workflows as wheel_workflows
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


def test_put_intent_preview_revalidates_capacity_inside_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = {
        "account_wheel_events": [],
        "trade_events": [],
        "account_position_lots": [],
    }
    position_lots = [{"record_id": "existing-put"}]

    class Repo:
        def read_lifecycle_account_rows(self, *, account, conn):
            assert account == "lx"
            assert conn is not None
            return rows

        def list_position_lots(self, *, conn):
            assert conn is not None
            return position_lots

        def get_current_wheel_activation_window(self, *, market, account, conn):
            assert (market, account) == ("us", "lx")
            assert conn is not None
            return {
                "market": "us",
                "account": "lx",
                "generation": 1,
                "activated_at_ms": 500,
                "deactivated_at_ms": None,
                "policy_sha256": "a" * 64,
            }

    repo = Repo()
    branch = {
        "account": "lx",
        "symbol": "NVDA",
        "wheel_branch_id": "wheel-put-1",
        "direction": "put",
        "lifecycle_status": "active",
        "integrity_status": "trusted",
        "phase": "ready",
        "active_option_lot_ids": [],
        "active_intent_ids": [],
        "branch_generation_hash": "generation-1",
    }
    candidate = {
        "final_candidate_id": "candidate-1",
        "account": "lx",
        "symbol": "NVDA",
        "wheel_branch_id": "wheel-put-1",
        "direction": "put",
        "branch_generation_hash": "generation-1",
        "capacity_identity_hash": "capacity-1",
        "granted_contracts": 1,
        "multiplier": 100,
        "strike": 100,
        "currency": "USD",
        "cash_reservation_currency": "USD",
        "expiration_ymd": "2027-01-15",
    }
    snapshot = {
        "account": "lx",
        "snapshot_hash": "snapshot-1",
        "rows": [
            {
                "wheel_branch_id": "wheel-put-1",
                "direction": "put",
                "branch_generation_hash": "generation-1",
                "final_candidate": candidate,
            }
        ],
        "opening_put_candidates": [{"symbol": "MSFT"}],
    }
    allocation = {
        "account": "lx",
        "allocation_status": "allocated",
        "granted_contracts": 1,
        "capacity_identity_hash": "capacity-1",
        "cash_reservation_amount": 10_000,
        "cash_reservation_currency": "USD",
    }
    revalidations = []
    monkeypatch.setattr(
        wheel_workflows,
        "with_sqlite_repo_transaction",
        lambda active_repo, call, **_kwargs: call(active_repo, object()),
    )
    monkeypatch.setattr(wheel_workflows, "_wheel_branch", lambda *_args, **_kwargs: branch)
    monkeypatch.setattr(wheel_workflows, "project_wheel_intents", lambda *_args, **_kwargs: [])

    def _revalidate(**kwargs):
        revalidations.append(kwargs)
        return allocation

    monkeypatch.setattr(
        wheel_workflows,
        "revalidate_selected_wheel_put_candidate_from_rows",
        _revalidate,
    )

    result = wheel_workflows.create_wheel_intent(
        repo,
        candidate_snapshot=snapshot,
        account="lx",
        wheel_branch_id="wheel-put-1",
        direction="put",
        final_candidate_id="candidate-1",
        expected_snapshot_hash="snapshot-1",
        expected_branch_generation_hash="generation-1",
        expires_at_ms=2_000,
        request_id="request-1",
        actor="tester",
        capacity_fact={
            "cash_authority": {"status": "available"},
            "cash_authority_hash": "authority-1",
            "cash_by_currency": {"USD": 20_000},
            "fx_snapshot": {"rates": {}},
        },
        new_intent_enabled=True,
        market="us",
        activation_descriptor={
            "market": "us",
            "account": "lx",
            "generation": 1,
            "activated_at_ms": 500,
            "deactivated_at_ms": None,
            "policy_hash": "a" * 64,
        },
        policy_sha256="a" * 64,
        apply_changes=False,
        as_of_ms=1_000,
    )

    assert result["schema_version"] == "wheel_intent_result.v1"
    assert result["direction"] == "put"
    assert result["wheel_branch_id"] == "wheel-put-1"
    assert result["dry_run"] is True
    assert result["write_applied"] is False
    assert revalidations[0]["lifecycle_rows"] is rows
    assert revalidations[0]["position_lots"] == position_lots
    assert revalidations[0]["opening_put_candidates"] == [{"symbol": "MSFT"}]


def test_put_linkage_rejection_preview_uses_canonical_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = {
        "account_wheel_events": [],
        "account_position_lots": [],
        "trade_events": [],
    }

    class Repo:
        def read_lifecycle_account_rows(self, *, account, conn):
            assert account == "lx"
            assert conn is not None
            return rows

    repo = Repo()
    branch = {
        "account": "lx",
        "wheel_branch_id": "wheel-put-1",
        "direction": "put",
        "branch_generation_hash": "generation-1",
    }
    candidate = {
        "direction": "put",
        "wheel_branch_id": "wheel-put-1",
        "option_record_id": "put-lot-1",
        "option_open_event_id": "put-open-1",
        "linkage_candidate_id": "candidate-1",
        "input_snapshot_hash": "input-1",
        "branch_generation_hash": "generation-1",
    }
    monkeypatch.setattr(
        wheel_workflows,
        "with_sqlite_repo_transaction",
        lambda active_repo, call, **_kwargs: call(active_repo, object()),
    )
    monkeypatch.setattr(
        wheel_workflows,
        "build_wheel_read_model_from_rows",
        lambda *_args, **_kwargs: {"wheel_branches": [branch]},
    )
    monkeypatch.setattr(
        wheel_workflows,
        "project_wheel_linkage_candidates",
        lambda *_args, **_kwargs: [candidate],
    )

    result = wheel_workflows.reject_wheel_linkage(
        repo,
        account="lx",
        option_record_id="put-lot-1",
        wheel_branch_id="wheel-put-1",
        direction="put",
        linkage_candidate_id="candidate-1",
        expected_input_hash="input-1",
        expected_branch_generation_hash="generation-1",
        request_id="request-1",
        actor="tester",
        reason="not this cycle",
        market="us",
        apply_changes=False,
        as_of_ms=1_000,
    )

    assert result["schema_version"] == "wheel_linkage_result.v1"
    assert result["direction"] == "put"
    assert result["wheel_branch_id"] == "wheel-put-1"
    assert result["option_record_id"] == "put-lot-1"
    assert result["status"] == "planned"
    assert result["dry_run"] is True


def _partial_call_fill() -> NormalizedTradeDeal:
    return NormalizedTradeDeal(
        broker="富途", futu_account_id="REAL_1", internal_account="lx",
        deal_id="partial-call-1", order_id="bound-call-order", symbol="NVDA",
        option_type="call", side="sell", position_effect="open", contracts=1,
        price=2, strike=110, multiplier=100, multiplier_source="broker",
        expiration_ymd="2026-08-21", currency="USD", trade_time_ms=5_000,
        raw_payload={},
    )


def test_two_partial_fills_consume_one_wheel_intent_without_changing_trade_amounts(tmp_path):
    repo, _, stock_lot_id = _assign_short_put(tmp_path, wheel_start_enabled=True, contracts=2)
    created, coverage = _create_call_intent(repo, stock_lot_id, contracts=2, broker_order_id="bound-call-order")
    first_deal = _partial_call_fill()
    first = persist_trade_event_with_wheel_intent(repo, first_deal, coverage).to_dict()
    partial = build_wheel_read_model(repo, "lx", 5_000)["batches"][0]
    assert first["wheel_linkage_status"] == "matched_intent"
    assert partial["active_intent_ids"] == [created["intent_id"]]
    assert partial["active_intent_reserved_shares"] == 100
    second = persist_trade_event_with_wheel_intent(repo, replace(first_deal, deal_id="partial-call-2", trade_time_ms=6_000), coverage).to_dict()
    completed = build_wheel_read_model(repo, "lx", 6_000)["batches"][0]
    assert second["wheel_linkage_status"] == "matched_intent"
    assert completed["active_intent_ids"] == []
    assert completed["active_intent_reserved_shares"] == 0
    consumed = [event for event in repo.list_wheel_events(account="lx") if event["event_type"] == "wheel_call_intent_consumed"]
    assert [event["payload"]["contracts"] for event in consumed] == [1, 1]
    fills = [event for event in repo.list_trade_events() if event["event_type"] == "open" and event["option_type"] == "call"]
    assert sum(event["contracts"] * event["price"] * event["multiplier"] for event in fills) == 400
    assert all(event["raw_payload"]["source_stock_lot_id"] == stock_lot_id for event in fills)


@pytest.mark.parametrize("order_id", [None, "another-order"])
def test_bound_wheel_order_does_not_consume_another_fill(tmp_path, order_id):
    repo, _, stock_lot_id = _assign_short_put(tmp_path, wheel_start_enabled=True)
    created, coverage = _create_call_intent(repo, stock_lot_id, broker_order_id="bound-call-order")
    result = persist_trade_event_with_wheel_intent(repo, replace(_partial_call_fill(), order_id=order_id), coverage).to_dict()
    assert result["wheel_linkage_status"] == "no_matching_intent"
    assert not [event for event in repo.list_wheel_events(account="lx") if event["event_type"] == "wheel_call_intent_consumed"]
    assert created["intent_id"] in build_wheel_read_model(repo, "lx", 5_000)["batches"][0]["active_intent_ids"]
    assert any(event["event_id"] == result["event_id"] for event in repo.list_trade_events())


def test_wheel_intent_replay_uses_stable_request_and_preserves_accepted_capacity(tmp_path):
    repo, _, stock_lot_id = _assign_short_put(tmp_path, wheel_start_enabled=True)
    created, coverage = _create_call_intent(repo, stock_lot_id)
    before = repo.list_wheel_events(account="lx")
    original = next(event["payload"] for event in before if event["event_type"] == "wheel_call_intent_created")
    replay = create_wheel_call_intent(
        repo, candidate_snapshot={}, account="lx", stock_lot_id=stock_lot_id,
        final_candidate_id=original["final_candidate_id"], expected_snapshot_hash=original["snapshot_hash"],
        expected_batch_generation_hash=original["batch_generation_hash"],
        expires_at_ms=original["expires_at_ms"], request_id=original["request_id"], actor=original["actor"],
        coverage_fact={**coverage, "capacity_identity_hash": "refreshed-capacity", "shares_available_for_cover": 0},
        new_intent_enabled=True, market="us", activation_descriptor=None,
        policy_sha256="", apply_changes=True, as_of_ms=6_000,
    )
    assert replay["status"] == "idempotent"
    assert replay["event_id"] == created["event_id"]
    assert repo.list_wheel_events(account="lx") == before


def _assign_short_put(
    tmp_path: Path,
    *,
    wheel_start_enabled: bool,
    contracts: int = 1,
) -> tuple[SQLiteOptionPositionsRepository, str, str]:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    put_lot_id = "wheel-source-put-lot"
    persist_trade_event_objects_atomically(
        repo,
        [
            TradeEvent(
                event_id="wheel-source-put-open",
                event_type="open",
                event_time_ms=1_000,
                contract_key=ContractKey.from_values(
                    broker="富途",
                    account="lx",
                    underlying_symbol="NVDA",
                    option_type="put",
                    position_side="short",
                    strike=100,
                    expiration_ymd="2026-08-21",
                ),
                contracts=contracts,
                price=2.5,
                currency="USD",
                source="test",
                multiplier=100,
                lot_id=put_lot_id,
                raw_payload=_trusted_multiplier_payload("wheel-source-put-open"),
            )
        ],
    )
    if wheel_start_enabled:
        _open_test_activation(repo)
    assignment_event_id = "wheel-source-put-assignment"
    persist_trade_event_objects_atomically(
        repo,
        [
            TradeEvent(
                event_id=assignment_event_id,
                event_type="assignment",
                event_time_ms=2_000,
                contract_key=ContractKey.from_values(
                    broker="富途",
                    account="lx",
                    underlying_symbol="NVDA",
                    option_type="put",
                    position_side="short",
                    strike=100,
                    expiration_ymd="2026-08-21",
                ),
                contracts=contracts,
                price=0,
                currency="USD",
                source="test",
                multiplier=100,
                target_lot_id=put_lot_id,
                raw_payload={
                    "target_lot_id": put_lot_id,
                    "stock_settlement": {
                        "side": "buy",
                        "shares": contracts * 100,
                        "price": 100,
                        "fees": 0,
                        "currency": "USD",
                        "fee_provenance": {"basis": "actual", "source": "test"},
                    }
                },
            )
        ],
    )
    return repo, put_lot_id, f"assigned-stock-{assignment_event_id}"


def _open_test_activation(repo: SQLiteOptionPositionsRepository) -> None:
    with patch(
        "src.application.ledger.repository_assigned_stock.now_ms",
        return_value=500,
    ), repo._writer_connection(begin_immediate=True) as conn:
        repo.open_wheel_activation_window(
            market="us",
            account="lx",
            expected_current_generation=0,
            policy_hash="a" * 64,
            request_id="test-wheel-activation",
            request_hash="b" * 64,
            conn=conn,
        )


def _trusted_multiplier_payload(event_id: str, **extra: object) -> dict[str, object]:
    return {
        "source_type": "broker_trade_event",
        "source_deal_id": event_id,
        "external_event_key": f"futu:{event_id}",
        "multiplier_source": "payload",
        **extra,
    }


def _create_call_intent(
    repo: SQLiteOptionPositionsRepository,
    stock_lot_id: str,
    *,
    new_intent_enabled: bool = True,
    contracts: int = 1,
    broker_order_id: str | None = None,
    market: str = "us",
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
                    "granted_contracts": contracts,
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
        "shares_eligible": contracts * 100,
        "shares_locked": 0,
        "shares_reserved": 0,
        "shares_available_for_cover": contracts * 100,
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
        new_intent_enabled=new_intent_enabled,
        market=market,
        activation_descriptor={
            "market": market,
            "account": "lx",
            "generation": 1,
            "activated_at_ms": 500,
            "deactivated_at_ms": None,
            "policy_hash": "a" * 64,
        },
        policy_sha256="a" * 64,
        broker_order_id=broker_order_id,
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
        market="us",
        as_of_ms=4_000,
    )
    applied = end_wheel_lifecycle(
        repo,
        account="lx",
        stock_lot_id=stock_lot_id,
        expected_batch_generation_hash=batch["batch_generation_hash"],
        request_id="end-wheel-1",
        actor="tester",
        market="us",
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
        market="us",
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


@pytest.mark.parametrize("apply_changes", [False, True])
def test_manual_end_rejects_cross_market_without_effects(
    tmp_path: Path,
    apply_changes: bool,
) -> None:
    repo, _put_lot_id, stock_lot_id = _assign_short_put(
        tmp_path,
        wheel_start_enabled=True,
    )
    batch = build_wheel_read_model(repo, "lx", 3_000, market="us")["batches"][0]
    before = repo.list_wheel_events(account="lx")

    with pytest.raises(ValueError, match="resolve uniquely"):
        end_wheel_lifecycle(
            repo,
            account="lx",
            stock_lot_id=stock_lot_id,
            expected_batch_generation_hash=batch["batch_generation_hash"],
            request_id=f"cross-market-end-{apply_changes}",
            actor="tester",
            market="hk",
            apply_changes=apply_changes,
            as_of_ms=4_000,
        )

    assert repo.list_wheel_events(account="lx") == before


def test_combo_funding_put_assignment_does_not_bootstrap_wheel_and_preserves_combo_tail(
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
    record_manual_assignment(
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
    model = build_wheel_read_model(repo, "lx", 3_000)
    assigned_stock = model["assigned_stock_projection"]["_all_assigned_stock_lots"][0]
    residual_call = next(
        row["fields"]
        for row in repo.list_position_lots()
        if row["fields"]["option_type"] == "call"
    )

    assert model["batches"] == []
    assert model["wheel_branches"] == []
    assert assigned_stock["strategy_group_id"] == group_id
    assert assigned_stock["leg_role"] == "assigned_stock"
    assert assigned_stock["source_option_leg_role"] == "funding_put"
    assert residual_call["status"] == "open"
    assert residual_call["strategy_group_id"] == group_id
    assert residual_call["leg_role"] == "participation_call"


def test_assignment_replay_does_not_backfill_wheel_start(tmp_path: Path) -> None:
    repo, _put_lot_id, _stock_lot_id = _assign_short_put(
        tmp_path,
        wheel_start_enabled=False,
    )

    assignment = next(
        TradeEvent.from_dict(item)
        for item in repo.list_trade_events()
        if item["event_type"] == "assignment"
    )
    _open_test_activation(repo)
    replay = persist_trade_event_objects_atomically(repo, [assignment])

    assert replay[0].created is False
    assert repo.list_wheel_events(account="lx") == []


def test_manual_assignment_runtime_boolean_does_not_authorize_wheel_lifecycle(
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
            request_id="manual-open-for-wheel-rollback",
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

    assert build_wheel_read_model(repo, "lx", 3_000)["wheel_branches"] == []


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
        match=(
            "batch generation changed|coverage is unavailable|coverage is insufficient|"
            "not ready"
        ),
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
            new_intent_enabled=True,
            market="us",
            activation_descriptor={
                "market": "us",
                "account": "lx",
                "generation": 1,
                "activated_at_ms": 500,
                "deactivated_at_ms": None,
                "policy_hash": "a" * 64,
            },
            policy_sha256="a" * 64,
            apply_changes=True,
            as_of_ms=4_000,
        )
    assert not any(
        event["event_type"] == "wheel_call_intent_created"
        for event in repo.list_wheel_events(account="lx")
    )


def test_intent_creation_rejects_disabled_wheel(tmp_path: Path) -> None:
    repo, _put_lot_id, stock_lot_id = _assign_short_put(
        tmp_path,
        wheel_start_enabled=True,
    )

    with pytest.raises(ValueError, match="wheel_disabled"):
        _create_call_intent(repo, stock_lot_id, new_intent_enabled=False)

    assert not any(
        event["event_type"] == "wheel_call_intent_created"
        for event in repo.list_wheel_events(account="lx")
    )


def test_intent_creation_rejects_closed_activation_without_effects(tmp_path: Path) -> None:
    repo, _put_lot_id, stock_lot_id = _assign_short_put(
        tmp_path,
        wheel_start_enabled=True,
    )
    with patch(
        "src.application.ledger.repository_assigned_stock.now_ms",
        return_value=3_500,
    ), repo._writer_connection(begin_immediate=True) as conn:
        repo.close_wheel_activation_window(
            market="us",
            account="lx",
            expected_current_generation=1,
            policy_hash="a" * 64,
            request_id="test-wheel-deactivation",
            request_hash="c" * 64,
            conn=conn,
        )
    before = repo.list_wheel_events(account="lx")

    with pytest.raises(ValueError, match="wheel_disabled"):
        _create_call_intent(repo, stock_lot_id)

    assert repo.list_wheel_events(account="lx") == before


def test_intent_creation_rejects_cross_market_branch_without_effects(tmp_path: Path) -> None:
    repo, _put_lot_id, stock_lot_id = _assign_short_put(
        tmp_path,
        wheel_start_enabled=True,
    )
    before = repo.list_wheel_events(account="lx")

    with pytest.raises(ValueError, match="wheel_disabled"):
        _create_call_intent(repo, stock_lot_id, market="hk")

    assert repo.list_wheel_events(account="lx") == before


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
        market="us",
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
    already_inactive = cancel_wheel_call_intent(
        repo,
        account="lx",
        stock_lot_id=stock_lot_id,
        intent_id=created["intent_id"],
        expected_batch_generation_hash=ready["batch_generation_hash"],
        request_id="intent-cancel-2",
        actor="tester",
        broker_order_inactive_confirmed=True,
        reason="already cancelled",
        market="us",
        apply_changes=True,
        as_of_ms=7_000,
    )
    assert already_inactive["status"] == "already_inactive"
    assert already_inactive["market"] == "us"


@pytest.mark.parametrize("apply_changes", [False, True])
def test_call_intent_cancel_rejects_cross_market_without_effects(
    tmp_path: Path,
    apply_changes: bool,
) -> None:
    repo, _put_lot_id, stock_lot_id = _assign_short_put(
        tmp_path,
        wheel_start_enabled=True,
    )
    created, _coverage = _create_call_intent(repo, stock_lot_id)
    pending = build_wheel_read_model(repo, "lx", 5_000, market="us")["batches"][0]
    before = repo.list_wheel_events(account="lx")

    with pytest.raises(ValueError, match="resolve uniquely"):
        cancel_wheel_call_intent(
            repo,
            account="lx",
            stock_lot_id=stock_lot_id,
            intent_id=created["intent_id"],
            expected_batch_generation_hash=pending["batch_generation_hash"],
            request_id=f"cross-market-cancel-{apply_changes}",
            actor="tester",
            broker_order_inactive_confirmed=True,
            reason="wrong market",
            market="hk",
            apply_changes=apply_changes,
            as_of_ms=6_000,
        )

    assert repo.list_wheel_events(account="lx") == before


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
        market="us",
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
        "source_wheel_branch_id",
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
        market="us",
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
        market="us",
        apply_changes=True,
        as_of_ms=5_000,
    )

    model = build_wheel_read_model(repo, "lx", 6_000)
    assert result["status"] == "rejected"
    assert model["linkage_candidates"] == []
    assert repo.get_position_lot_fields(call_lot_id).get("strategy") is None
    assert model["batches"][0]["phase"] == "ready"


@pytest.mark.parametrize("apply_changes", [False, True])
@pytest.mark.parametrize("action", ["confirm", "reject"])
def test_call_linkage_rejects_cross_market_without_effects(
    tmp_path: Path,
    action: str,
    apply_changes: bool,
) -> None:
    repo, _put_lot_id, stock_lot_id = _assign_short_put(
        tmp_path,
        wheel_start_enabled=True,
    )
    call_lot_id = _open_unlinked_call(repo)
    candidate = build_wheel_read_model(repo, "lx", 4_000, market="us")[
        "linkage_candidates"
    ][0]
    before_events = repo.list_wheel_events(account="lx")
    before_trades = repo.list_trade_events()
    common = {
        "account": "lx",
        "call_record_id": call_lot_id,
        "stock_lot_id": stock_lot_id,
        "linkage_candidate_id": candidate["linkage_candidate_id"],
        "expected_input_hash": candidate["input_snapshot_hash"],
        "expected_batch_generation_hash": candidate["batch_generation_hash"],
        "request_id": f"cross-market-linkage-{action}-{apply_changes}",
        "actor": "tester",
        "market": "hk",
        "apply_changes": apply_changes,
        "as_of_ms": 5_000,
    }

    with pytest.raises(ValueError, match="stale or unavailable"):
        if action == "confirm":
            confirm_wheel_call_linkage(
                repo,
                **common,
                coverage_fact={
                    "account": "lx",
                    "symbol": "NVDA",
                    "capacity_identity_hash": "capacity-1",
                },
            )
        else:
            reject_wheel_call_linkage(
                repo,
                **common,
                reason="wrong market",
            )

    assert repo.list_wheel_events(account="lx") == before_events
    assert repo.list_trade_events() == before_trades


def test_partial_wheel_call_assignment_keeps_batch_active(tmp_path: Path) -> None:
    repo, _put_lot_id, stock_lot_id = _assign_short_put(
        tmp_path,
        wheel_start_enabled=True,
        contracts=2,
    )
    call_lot_id = "wheel-call-lot-partial"
    call_key = ContractKey.from_values(
        broker="富途",
        account="lx",
        underlying_symbol="NVDA",
        option_type="call",
        position_side="short",
        strike=110,
        expiration_ymd="2026-08-21",
    )
    persist_trade_event_objects_atomically(
        repo,
        [
            TradeEvent(
                event_id="wheel-call-open-partial",
                event_type="open",
                event_time_ms=3_000,
                contract_key=call_key,
                contracts=1,
                price=2,
                currency="USD",
                source="test",
                multiplier=100,
                lot_id=call_lot_id,
                raw_payload=_trusted_multiplier_payload(
                    "wheel-call-open-partial",
                    strategy="wheel",
                    leg_role="wheel_call",
                    source_stock_lot_id=stock_lot_id,
                ),
            )
        ],
    )

    persist_trade_event_objects_atomically(
        repo,
        [
            TradeEvent(
                event_id="wheel-call-assignment-partial",
                event_type="assignment",
                event_time_ms=4_000,
                contract_key=call_key,
                contracts=1,
                price=0,
                currency="USD",
                source="test",
                multiplier=100,
                target_lot_id=call_lot_id,
                raw_payload={
                    "target_lot_id": call_lot_id,
                    "stock_settlement": {
                        "side": "sell",
                        "shares": 100,
                        "price": 110,
                        "fees": 0,
                        "currency": "USD",
                        "fee_provenance": {"basis": "actual", "source": "test"},
                    },
                },
            )
        ],
    )

    batch = build_wheel_read_model(repo, "lx", 5_000)["batches"][0]
    branches = build_wheel_read_model(repo, "lx", 5_000)["wheel_branches"]
    child = next(item for item in branches if item["direction"] == "put")
    assert batch["lifecycle_status"] == "active"
    assert batch["remaining_contracts"] == 1
    assert batch["shares_remaining"] == 100
    assert batch["phase"] == "ready"
    assert child["lifecycle_status"] == "pending_decision"


def test_wheel_call_assignment_closes_batch_in_same_transaction(
    tmp_path: Path,
) -> None:
    repo, _put_lot_id, stock_lot_id = _assign_short_put(
        tmp_path,
        wheel_start_enabled=True,
    )
    call_lot_id = "wheel-call-lot-1"
    call_key = ContractKey.from_values(
        broker="富途",
        account="lx",
        underlying_symbol="NVDA",
        option_type="call",
        position_side="short",
        strike=110,
        expiration_ymd="2026-08-21",
    )
    persist_trade_event_objects_atomically(
        repo,
        [
            TradeEvent(
                event_id="wheel-call-open-1",
                event_type="open",
                event_time_ms=3_000,
                contract_key=call_key,
                contracts=1,
                price=2,
                currency="USD",
                source="test",
                multiplier=100,
                lot_id=call_lot_id,
                raw_payload=_trusted_multiplier_payload(
                    "wheel-call-open-1",
                    strategy="wheel",
                    leg_role="wheel_call",
                    source_stock_lot_id=stock_lot_id,
                ),
            )
        ],
    )

    persist_trade_event_objects_atomically(
        repo,
        [
            TradeEvent(
                event_id="call-assignment-1",
                event_type="assignment",
                event_time_ms=4_000,
                contract_key=call_key,
                contracts=1,
                price=0,
                currency="USD",
                source="test",
                multiplier=100,
                target_lot_id=call_lot_id,
                raw_payload={
                    "target_lot_id": call_lot_id,
                    "stock_settlement": {
                        "side": "sell",
                        "shares": 100,
                        "price": 110,
                        "fees": 0,
                        "currency": "USD",
                        "fee_provenance": {"basis": "actual", "source": "test"},
                    },
                },
            )
        ],
    )

    branches = build_wheel_read_model(repo, "lx", 5_000)["wheel_branches"]
    parent = next(item for item in branches if item["direction"] == "call")
    child = next(item for item in branches if item["direction"] == "put")
    assert parent["lifecycle_status"] == "converted"
    assert parent["shares_remaining"] == 0
    assert parent["integrity_status"] == "trusted"
    assert child["lifecycle_status"] == "pending_decision"


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
            request_id="manual-open-for-wheel-rollback",
        ),
    )
    put_lot_id = str(repo.list_position_lots()[0]["record_id"])
    _open_test_activation(repo)

    def _fail(*_args: object, **_kwargs: object) -> bool:
        raise ValueError("forced Wheel companion failure")

    monkeypatch.setattr(repo, "append_wheel_event_once", _fail)
    with pytest.raises(ValueError, match="forced Wheel companion failure"):
        persist_trade_event_objects_atomically(
            repo,
            [
                TradeEvent(
                    event_id="put-assignment-rollback",
                    event_type="assignment",
                    event_time_ms=2_000,
                    contract_key=ContractKey.from_values(
                        broker="富途",
                        account="lx",
                        underlying_symbol="NVDA",
                        option_type="put",
                        position_side="short",
                        strike=100,
                        expiration_ymd="2026-08-21",
                    ),
                    contracts=1,
                    price=0,
                    currency="USD",
                    source="test",
                    multiplier=100,
                    target_lot_id=put_lot_id,
                    raw_payload={
                        "target_lot_id": put_lot_id,
                        "stock_settlement": {
                            "side": "buy",
                            "shares": 100,
                            "price": 100,
                            "fees": 0,
                            "currency": "USD",
                            "fee_provenance": {
                                "basis": "actual",
                                "source": "test",
                            },
                        },
                    },
                )
            ],
        )

    assert [item["event_type"] for item in repo.list_trade_events()] == ["open"]
    assert repo.get_position_lot_fields(put_lot_id)["status"] == "open"


@pytest.mark.parametrize("namespace", ["futu.order", "external-file.order"])
def test_bound_wheel_order_requires_proven_order_namespace(tmp_path, namespace):
    from src.application.ledger.api import record_trade_event_with_wheel_intent
    from src.application.trades.normalizer import normalize_trade_deal

    repo, _, stock_lot_id = _assign_short_put(tmp_path, wheel_start_enabled=True)
    created, coverage = _create_call_intent(repo, stock_lot_id, broker_order_id="bound-call-order")
    deal = normalize_trade_deal({
        "schema_version": "trade_execution.v1",
        "broker_account_ref": {"broker_id": "futu", "external_account_id": "REAL_1",
                               "environment": "REAL", "broker_account_id": "futu:REAL:REAL_1",
                               "account_label": "lx"},
        "instrument_ref": {"asset_type": "option", "market": "US", "symbol": "NVDA",
                           "currency": "USD", "option_type": "call", "strike": "110",
                           "expiration_ymd": "2026-08-21", "multiplier": "100"},
        "external_id_namespace": "futu.deal", "external_execution_id": "scope-fill",
        "external_order_namespace": namespace, "external_order_id": "bound-call-order",
        "side": "sell", "position_effect": "open", "quantity": "1", "price": "2",
        "currency": "USD", "occurred_at_utc": "1970-01-01T00:00:05Z",
    })
    result = record_trade_event_with_wheel_intent(repo, deal, coverage).to_dict()
    matched = namespace == "futu.order"
    assert result["wheel_linkage_status"] == ("matched_intent" if matched else "no_matching_intent")
    consumed = [event for event in repo.list_wheel_events(account="lx")
                if event["event_type"] == "wheel_call_intent_consumed"]
    assert len(consumed) == int(matched)
    event = next(event for event in repo.list_trade_events() if event["event_id"] == result["event_id"])
    assert event["contracts"] * event["price"] * event["multiplier"] == 200
    assert bool(event["raw_payload"].get("source_stock_lot_id")) is matched
    assert (created["intent_id"] in build_wheel_read_model(repo, "lx", 5_000)["batches"][0]["active_intent_ids"]) is not matched
