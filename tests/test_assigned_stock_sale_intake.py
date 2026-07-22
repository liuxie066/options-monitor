from __future__ import annotations

from pathlib import Path

import src.application.ledger.manual_trades as ledger_manual_trades
import src.application.ledger.repository as ledger_repository

from domain.domain.option_position_lots import OpenPositionCommand
from src.application.ledger.commands import record_manual_assignment
from src.application.positions.assigned_stock_view import build_assigned_stock_view
from src.application.positions.workflows import execute_manual_assigned_stock_sale
from src.application.trades.normalizer import NormalizedTradeDeal
from src.application.trades.resolver import resolve_trade_deal


def _stock_sale_deal(**overrides: object) -> NormalizedTradeDeal:
    base = {
        "broker": "富途",
        "futu_account_id": "REAL_1",
        "internal_account": "lx",
        "deal_id": "stock-sale-1",
        "order_id": "order-stock-sale-1",
        "symbol": "NVDA",
        "option_type": None,
        "side": "sell",
        "position_effect": None,
        "contracts": 100,
        "price": 105.0,
        "strike": None,
        "multiplier": None,
        "multiplier_source": None,
        "expiration_ymd": None,
        "currency": "USD",
        "trade_time_ms": 3000,
        "raw_payload": {"deal_id": "stock-sale-1", "code": "US.NVDA"},
    }
    base.update(overrides)
    return NormalizedTradeDeal(**base)


def _repo_with_assigned_stock(tmp_path: Path, *, opened_at_ms: int = 1000, assigned_at_ms: int = 2000):
    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
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
            strike=100.0,
            multiplier=100,
            expiration_ymd="2026-06-19",
            premium_per_share=2.5,
            opened_at_ms=opened_at_ms,
        ),
    )
    lot = repo.list_position_lots()[0]
    record_manual_assignment(
        repo,
        record_id=lot["record_id"],
        contracts_to_close=1,
        stock_side="buy",
        stock_qty=100,
        stock_price=100.0,
        as_of_ms=assigned_at_ms,
    )
    assignment_event = [item for item in repo.list_trade_events() if item.get("event_type") == "assignment"][0]
    return repo, f"assigned-stock-{assignment_event['event_id']}"


def _assigned_stock_lifecycle(repo, stock_lot_id: str) -> dict:
    report = build_assigned_stock_view(repo)
    return [row for row in report["assigned_stock_lots"] if row["stock_lot_id"] == stock_lot_id][0]


def test_resolve_trade_previews_broker_assigned_stock_sale(tmp_path: Path) -> None:
    repo, stock_lot_id = _repo_with_assigned_stock(tmp_path)

    result = resolve_trade_deal(_stock_sale_deal(), repo=repo, state={}, apply_changes=False)

    assert result.status == "dry_run"
    assert result.action == "assigned_stock_sale"
    assert result.reason == "preview_assigned_stock_sale"
    assert repo.list_assigned_stock_events() == []
    operation = result.operations[0].to_payload()
    assert operation["record_id"] == stock_lot_id
    assert operation["event_id"] == "assigned-stock-sale-stock-sale-1"
    assert operation["fields"]["source"] == "broker"
    assert operation["fields"]["target_stock_lot_id"] == stock_lot_id
    assert operation["fields"]["fees"] == 2.5261
    assert operation["fields"]["fee_provenance"]["basis"] == "estimated"
    assert result.diagnostics["assigned_stock_sale"]["stock_lot_after"]["assigned_stock_realized_pnl"] == 497.4739


def test_resolve_trade_applies_broker_assigned_stock_sale(tmp_path: Path) -> None:
    repo, stock_lot_id = _repo_with_assigned_stock(tmp_path)

    result = resolve_trade_deal(_stock_sale_deal(), repo=repo, state={}, apply_changes=True)

    assert result.status == "applied"
    assert result.action == "assigned_stock_sale"
    assert result.reason == "applied_assigned_stock_sale"
    events = repo.list_assigned_stock_events()
    assert len(events) == 1
    assert events[0]["stock_event_id"] == "assigned-stock-sale-stock-sale-1"
    assert events[0]["source"] == "broker"
    assert events[0]["source_deal_id"] == "stock-sale-1"
    assert events[0]["fees"] == 2.5261
    assert events[0]["fee_provenance"]["basis"] == "estimated"
    lifecycle = _assigned_stock_lifecycle(repo, stock_lot_id)
    assert lifecycle["status"] == "closed"
    assert lifecycle["assigned_stock_realized_pnl"] == 497.4739
    assert lifecycle["option_premium_attribution"] == 250.0
    assert lifecycle["assignment_lifecycle_pnl"] == 747.4739


def test_resolve_trade_does_not_reopen_closed_assigned_stock_lot_after_stock_buy(tmp_path: Path) -> None:
    repo, stock_lot_id = _repo_with_assigned_stock(tmp_path)

    sale = resolve_trade_deal(_stock_sale_deal(), repo=repo, state={}, apply_changes=True)
    buy = resolve_trade_deal(
        _stock_sale_deal(deal_id="stock-buy-1", side="buy", price=95.0, trade_time_ms=4000),
        repo=repo,
        state={},
        apply_changes=True,
    )
    later_sale = resolve_trade_deal(
        _stock_sale_deal(deal_id="stock-sale-2", price=110.0, trade_time_ms=5000),
        repo=repo,
        state={},
        apply_changes=True,
    )

    assert sale.status == "applied"
    assert buy.status == "skipped"
    assert buy.reason == "not_option_deal"
    assert later_sale.status == "skipped"
    assert later_sale.reason == "not_option_deal"
    assert len(repo.list_assigned_stock_events()) == 1
    lifecycle = _assigned_stock_lifecycle(repo, stock_lot_id)
    assert lifecycle["status"] == "closed"
    assert lifecycle["shares_remaining"] == 0
    assert lifecycle["assigned_stock_realized_pnl"] == 497.4739


def test_broker_assigned_stock_sale_preserves_actual_fee_evidence(tmp_path: Path) -> None:
    repo, stock_lot_id = _repo_with_assigned_stock(tmp_path)

    result = resolve_trade_deal(
        _stock_sale_deal(raw_payload={"commission": -0.99, "platform_fee": -1.0}),
        repo=repo,
        state={},
        apply_changes=True,
    )

    assert result.status == "applied"
    event = repo.list_assigned_stock_events()[0]
    assert event["fees"] == 1.99
    assert event["fee_provenance"] == {
        "basis": "actual",
        "source": "raw_payload.components",
        "reason": "broker_reported_fee",
        "components": ["commission", "platform_fee"],
    }
    assert _assigned_stock_lifecycle(repo, stock_lot_id)["assigned_stock_realized_pnl"] == 498.01


def test_manual_assigned_stock_sale_explicit_zero_is_actual(tmp_path: Path) -> None:
    repo, stock_lot_id = _repo_with_assigned_stock(tmp_path)

    execute_manual_assigned_stock_sale(
        repo,
        target_stock_lot_id=stock_lot_id,
        shares=100,
        price=105.0,
        fees=0.0,
        trade_time_ms=3000,
        dry_run=False,
    )

    event = repo.list_assigned_stock_events()[0]
    assert event["fees"] == 0.0
    assert event["fee_provenance"]["basis"] == "actual"
    assert _assigned_stock_lifecycle(repo, stock_lot_id)["assigned_stock_realized_pnl"] == 500.0


def test_resolve_trade_does_not_reopen_closed_assigned_stock_lot_after_stock_buy(tmp_path: Path) -> None:
    repo, stock_lot_id = _repo_with_assigned_stock(tmp_path)

    sale = resolve_trade_deal(_stock_sale_deal(), repo=repo, state={}, apply_changes=True)
    buy = resolve_trade_deal(
        _stock_sale_deal(deal_id="stock-buy-1", side="buy", price=95.0, trade_time_ms=4000),
        repo=repo,
        state={},
        apply_changes=True,
    )
    later_sale = resolve_trade_deal(
        _stock_sale_deal(deal_id="stock-sale-2", price=110.0, trade_time_ms=5000),
        repo=repo,
        state={},
        apply_changes=True,
    )

    assert sale.status == "applied"
    assert buy.status == "skipped"
    assert buy.reason == "not_option_deal"
    assert later_sale.status == "skipped"
    assert later_sale.reason == "not_option_deal"
    assert len(repo.list_assigned_stock_events()) == 1
    lifecycle = _assigned_stock_lifecycle(repo, stock_lot_id)
    assert lifecycle["status"] == "closed"
    assert lifecycle["shares_remaining"] == 0
    assert lifecycle["assigned_stock_realized_pnl"] == 497.4739


def test_resolve_trade_broker_assigned_stock_sale_duplicate_is_idempotent(tmp_path: Path) -> None:
    repo, _stock_lot_id = _repo_with_assigned_stock(tmp_path)

    first = resolve_trade_deal(_stock_sale_deal(), repo=repo, state={}, apply_changes=True)
    duplicate = resolve_trade_deal(_stock_sale_deal(), repo=repo, state={}, apply_changes=True)

    assert first.status == "applied"
    assert duplicate.status == "applied"
    assert duplicate.operations[0].to_payload()["result"]["created"] is False
    assert duplicate.diagnostics["assigned_stock_sale"]["idempotent_duplicate"] is True
    assert len(repo.list_assigned_stock_events()) == 1


def test_resolve_trade_keeps_unmatched_stock_sale_as_non_option(tmp_path: Path) -> None:
    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")

    result = resolve_trade_deal(_stock_sale_deal(symbol="TIGR"), repo=repo, state={}, apply_changes=True)

    assert result.status == "skipped"
    assert result.action is None
    assert result.reason == "not_option_deal"
    assert repo.list_assigned_stock_events() == []


def test_resolve_trade_broker_assigned_stock_sale_ambiguous_lot_is_unresolved(tmp_path: Path) -> None:
    repo, _first_stock_lot_id = _repo_with_assigned_stock(tmp_path, opened_at_ms=1000, assigned_at_ms=2000)
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
            strike=100.0,
            multiplier=100,
            expiration_ymd="2026-07-17",
            premium_per_share=2.5,
            opened_at_ms=1100,
        ),
    )
    second_lot = [item for item in repo.list_position_lots() if item["fields"]["status"] == "open"][0]
    record_manual_assignment(
        repo,
        record_id=second_lot["record_id"],
        contracts_to_close=1,
        stock_side="buy",
        stock_qty=100,
        stock_price=100.0,
        as_of_ms=2100,
    )

    result = resolve_trade_deal(_stock_sale_deal(contracts=100, trade_time_ms=3000), repo=repo, state={}, apply_changes=True)

    assert result.status == "unresolved"
    assert result.action == "assigned_stock_sale"
    assert result.reason == "ambiguous_assigned_stock_sale"
    assert result.diagnostics["viable_count"] == 2
    assert {item["stock_cost_per_share"] for item in result.diagnostics["candidates"]} == {100.0}
    assert all(item.get("source_assignment_event_id") for item in result.diagnostics["candidates"])
    assert repo.list_assigned_stock_events() == []
