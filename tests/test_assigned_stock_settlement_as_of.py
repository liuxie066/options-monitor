from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from domain.domain.ledger import TradeEvent
from src.application.ledger import api as ledger_api
from src.application.ledger.repository import SQLiteOptionPositionsRepository
from src.application.positions.assigned_stock_view import build_assigned_stock_view
from src.application.positions.workflows import execute_manual_assigned_stock_sale


SURFACES = ("rows", "view", "wheel")


def _open(repo, *, option_type="put", side="short", strike=100, at=1_000):
    ledger_api.record_manual_position_open(
        repo, broker="富途", account="lx", symbol="NVDA", option_type=option_type,
        side=side, contracts=1, currency="USD", strike=strike, multiplier=100,
        expiration_ymd="2026-08-21", premium_per_share=2, opened_at_ms=at,
    )


def _terminal(
    repo, *, at=2_000, settlement_at=4_000, time_field="event_time_ms",
    option_type="put", position_side="short", strike=100, lifecycle="assignment",
):
    stock_side = "buy" if (option_type, position_side) in {("put", "short"), ("call", "long")} else "sell"
    settlement = {
        "side": stock_side, "shares": 100, "price": strike, "currency": "USD",
        "fees": 0, "fee_provenance": {"basis": "actual", "source": "fixture"},
    }
    if time_field is not None:
        settlement[time_field] = settlement_at
    writer = ledger_api.record_lifecycle_assignment if lifecycle == "assignment" else ledger_api.record_lifecycle_exercise
    writer(
        repo, broker="富途", account="lx", symbol="NVDA", option_type=option_type,
        position_side=position_side, strike=strike, expiration_ymd="2026-08-21",
        contracts_to_close=1, event_time_ms=at, case_id=None, evidence_ids=[],
        stock_settlement=settlement,
    )
    return next(
        row for row in ledger_api.trade_event_log(repo)
        if row["event_type"] == lifecycle and row["event_time_ms"] == at
    )


def _repo(tmp_path: Path, **kwargs):
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    _open(repo)
    terminal = _terminal(repo, **kwargs)
    # Reopen the real SQLite store: the read side consumes persisted public writes.
    return SQLiteOptionPositionsRepository(repo.db_path), terminal


def _report(repo, at, surface):
    if surface == "view":
        return build_assigned_stock_view(repo, account="lx", as_of_ms=at)
    rows = {
        "trade_events": ledger_api.trade_event_log(repo),
        "account_assigned_stock_events": list(ledger_api.assigned_stock_event_log(repo).events),
    }
    if surface == "wheel":
        return ledger_api.build_wheel_read_model_from_rows(
            rows, account="lx", as_of_ms=at,
        )["assigned_stock_projection"]
    return ledger_api.project_assigned_stock_lifecycle_from_rows(rows, account="lx", as_of_ms=at)


def _assert_option_terminal(rows, *, at, event_id):
    published = ledger_api.project_trade_event_log([row for row in rows if row["event_time_ms"] <= at])
    assert not [item for item in published.diagnostics if item.severity == "error"]
    allocation = next(item for item in published.ledger_projection.allocations if item.close_event_id == event_id)
    assert allocation.contracts == 1
    assert allocation.closed_at_ms == at
    lot = next(item for item in published.ledger_projection.lots if item.lot_id == allocation.target_lot_id)
    assert lot.contracts_open == 0


@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize(
    ("time_field", "settlement_at"),
    [("event_time_ms", 2_500), ("event_time_ms", 3_000), ("event_time_ms", 4_000),
     ("trade_time_ms", 4_000), (None, None)],
    ids=["earlier-than-cutoff", "equal-cutoff", "later-than-cutoff", "legacy-time", "default-time"],
)
def test_public_sqlite_stock_visibility_uses_inner_settlement_time(tmp_path, surface, time_field, settlement_at):
    repo, terminal = _repo(tmp_path, time_field=time_field, settlement_at=settlement_at)
    stored = ledger_api.trade_event_log(repo)
    assert len(stored) == 2
    assert terminal["event_time_ms"] == 2_000
    persisted = terminal["raw_payload"]["stock_settlement"]
    if time_field is not None:
        assert persisted[time_field] == settlement_at
    else:
        assert persisted.get("event_time_ms") is None
        assert persisted.get("trade_time_ms") is None
    effective_at = settlement_at if settlement_at is not None else 2_000
    _assert_option_terminal(stored, at=2_000, event_id=terminal["event_id"])
    for at in (1_999, 2_000, 2_500, 2_999, 3_000, 3_999, 4_000, 5_000):
        report = _report(repo, at, surface)
        lots = report["assigned_stock_lots"]
        visible = at >= max(2_000, effective_at)
        assert len(lots) == int(visible), (at, effective_at, surface)
        assert len(report["stock_position_lots"]) == int(visible)
        assert report["assigned_stock_sale_rows"] == []
        if visible:
            assert lots[0]["assigned_at_ms"] == effective_at
            assert lots[0]["shares_remaining"] == 100
            assert lots[0]["remaining_stock_principal_basis"] == 10_000
            assert Decimal(lots[0]["stock_cost_basis_total"]) == Decimal("10000")
            assert report["stock_position_lots"][0]["opened_at_ms"] == effective_at
        else:
            assert report["assignment_lifecycle_rows"] == []
            assert report["lifecycle_efficiency_rows"] == []
            assert report["assigned_stock_review_rows"] == []
    assert ledger_api.trade_event_log(repo) == stored
    assert build_assigned_stock_view(repo, account="sy", as_of_ms=5_000)["assigned_stock_lots"] == []


@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize(
    ("lifecycle", "option_type", "position_side", "strike"),
    [("assignment", "call", "short", 110), ("exercise", "put", "long", 100)],
)
def test_inner_stock_sale_waits_for_settlement_but_option_is_already_terminal(
    tmp_path, surface, lifecycle, option_type, position_side, strike,
):
    repo, _ = _repo(tmp_path)
    _open(repo, option_type=option_type, side=position_side, strike=strike, at=5_000)
    terminal = _terminal(
        repo, at=6_000, settlement_at=8_000, option_type=option_type,
        position_side=position_side, strike=strike, lifecycle=lifecycle,
    )
    stored = ledger_api.trade_event_log(repo)
    _assert_option_terminal(stored, at=6_000, event_id=terminal["event_id"])
    for at in (5_999, 6_000, 7_999, 8_000, 8_001):
        report = _report(repo, at, surface)
        lot = report["assigned_stock_lots"][0]
        settled = at >= 8_000
        assert lot["shares_remaining"] == (0 if settled else 100)
        assert lot["remaining_stock_principal_basis"] == (0 if settled else 10_000)
        assert len(report["assigned_stock_sale_rows"]) == int(settled)
        if settled:
            sale = report["assigned_stock_sale_rows"][0]
            assert sale["event_at"] == 8_000
            assert sale["shares"] == 100
            assert sale["cash_in_gross"] == 100 * strike
        else:
            assert not any(row["status"] == "incomplete_inventory_basis" for row in report["assigned_stock_review_rows"])
    assert ledger_api.trade_event_log(repo) == stored


@pytest.mark.parametrize("surface", SURFACES)
def test_public_stock_sale_preserves_inventory_at_each_historical_boundary(tmp_path, surface):
    repo, terminal = _repo(tmp_path)
    stock_id = _report(repo, 4_000, "view")["assigned_stock_lots"][0]["stock_lot_id"]
    result = execute_manual_assigned_stock_sale(
        repo, target_lot_id=stock_id, shares=40, price=110, trade_time_ms=5_000, dry_run=False,
    )
    assert result["mode"] == "applied"
    assert result["result"]["created"] is True
    repo = SQLiteOptionPositionsRepository(repo.db_path)
    assert len(ledger_api.assigned_stock_event_log(repo).events) == 1
    for at, shares in ((3_000, None), (4_000, 100), (4_999, 100), (5_000, 60), (6_000, 60)):
        report = _report(repo, at, surface)
        if shares is None:
            assert report["assigned_stock_lots"] == []
        else:
            lot = report["assigned_stock_lots"][0]
            assert lot["source_assignment_event_id"] == terminal["event_id"]
            assert lot["shares_remaining"] == shares
            assert lot["remaining_stock_principal_basis"] == shares * 100
        assert len(report["assigned_stock_sale_rows"]) == int(at >= 5_000)


@pytest.mark.parametrize("surface", SURFACES)
def test_public_late_void_still_restates_stock_history(tmp_path, surface):
    repo, terminal = _repo(tmp_path)
    result = ledger_api.record_trade_event_void(repo, event_id=terminal["event_id"], reason="fixture correction")
    assert result["mode"] == "applied"
    stored = ledger_api.trade_event_log(repo)
    void = next(row for row in stored if row["event_type"] == "void")
    assert void["target_event_id"] == terminal["event_id"]
    assert void["event_time_ms"] > 5_000
    for at in (3_000, 4_000, 5_000):
        report = _report(repo, at, surface)
        assert report["assigned_stock_lots"] == []
        assert report["stock_position_lots"] == []
        assert report["assigned_stock_review_rows"] == []
    assert ledger_api.trade_event_log(repo) == stored


@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize("invalid", ["account", "unknown-target", "earlier-than-target"])
def test_future_settlement_does_not_bypass_control_graph_validation(tmp_path, surface, invalid):
    repo, terminal = _repo(tmp_path)
    event = TradeEvent.from_dict(terminal)
    void = replace(
        event, event_id="invalid-void", event_type="void", event_time_ms=6_000,
        contracts=0, price=0, lot_id=None, target_lot_id=None,
        target_event_id=terminal["event_id"], raw_payload={},
    )
    if invalid == "account":
        void = replace(void, contract_key=replace(void.contract_key, account="sy"))
    elif invalid == "unknown-target":
        void = replace(void, target_event_id="not-in-ledger", event_time_ms=2_500)
    else:
        void = replace(void, event_time_ms=1_500)
    # Deliberately seed a corrupt control row; normal writers reject this graph.
    repo.upsert_trade_event(void)
    with pytest.raises(ValueError, match="assigned-stock ledger projection is untrusted"):
        _report(repo, 3_000, surface)
