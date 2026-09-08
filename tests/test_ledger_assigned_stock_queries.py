from __future__ import annotations

from pathlib import Path

import pytest

from domain.domain.option_position_lots import OpenPositionCommand

from src.application.ledger import api as ledger_api
from src.application.ledger.repository import SQLiteOptionPositionsRepository
from src.application.positions.assigned_stock_view import build_assigned_stock_view
from src.application.positions.workflows import execute_manual_assigned_stock_sale


class _NoAssignedStockCapability:
    pass


class _BrokenAssignedStockRepo:
    def list_assigned_stock_events(self):
        raise RuntimeError("boom")


class _MixedAssignedStockRepo:
    def list_assigned_stock_events(self):
        return [{"stock_event_id": "sale-1"}, "bad-row"]


def test_assigned_stock_event_log_reports_absent_repository_capability() -> None:
    result = ledger_api.assigned_stock_event_log(_NoAssignedStockCapability())

    assert result.events == ()
    assert result.diagnostics == (
        {
            "context": "assigned_stock",
            "code": "assigned_stock_event_log_unavailable",
            "message": "ledger repository does not expose assigned-stock events",
        },
    )


def test_assigned_stock_event_log_fails_closed_on_read_error_and_invalid_rows() -> None:
    failed = ledger_api.assigned_stock_event_log(_BrokenAssignedStockRepo())
    mixed = ledger_api.assigned_stock_event_log(_MixedAssignedStockRepo())

    assert failed.events == ()
    assert failed.diagnostics[0]["code"] == "assigned_stock_event_log_read_failed"
    assert mixed.events == ({"stock_event_id": "sale-1"},)
    assert mixed.diagnostics[0]["code"] == "assigned_stock_event_invalid_row"
    assert mixed.diagnostics[0]["row_index"] == 1


def test_touched_assigned_stock_consumers_use_only_the_ledger_api_boundary() -> None:
    root = Path(__file__).resolve().parents[1]
    touched = [
        "src/application/performance/adapters.py",
        "src/application/positions/workflows.py",
        "src/application/ledger/read_model.py",
        "src/application/agent_tools/operations_impl.py",
        "src/application/agent_tools/materialization_impl.py",
        "src/application/trades/state_reconcile.py",
    ]

    for relative in touched:
        source = (root / relative).read_text(encoding="utf-8")
        assert "list_assigned_stock_events" not in source, relative
        assert 'getattr(repo, "list_assigned_stock_events"' not in source, relative


@pytest.mark.parametrize("projection_state", ["clean", "absent", "dirty"])
@pytest.mark.parametrize("sale_at_ms", [4_000, 5_000], ids=["same-instant", "later"])
def test_partial_call_close_rebuild_and_sale_keep_current_coverage(
    tmp_path: Path, projection_state: str, sale_at_ms: int,
) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    ledger_api.record_manual_position_open(repo, OpenPositionCommand(
        broker="富途", account="lx", symbol="NVDA", option_type="put", side="short",
        contracts=2, currency="USD", strike=100, multiplier=100, expiration_ymd="2026-08-21",
        premium_per_share=2, opened_at_ms=1_000,
    ))
    put_id = repo.list_position_lots()[0]["record_id"]
    ledger_api.record_manual_assignment(
        repo, record_id=put_id, contracts_to_close=2,
        stock_side="buy", stock_qty=200, stock_price=100, as_of_ms=2_000,
    )
    stock_id = build_assigned_stock_view(repo, account="lx", as_of_ms=2_000)["assigned_stock_lots"][0]["stock_lot_id"]
    ledger_api.record_manual_position_open(repo, OpenPositionCommand(
        broker="富途", account="lx", symbol="NVDA", option_type="call", side="short",
        contracts=2, currency="USD", strike=110, multiplier=100, expiration_ymd="2026-08-21",
        premium_per_share=2, opened_at_ms=3_000, strategy_snapshot={"source_stock_lot_id": stock_id},
    ))
    call_id = next(row["record_id"] for row in repo.list_position_lots() if row["fields"]["option_type"] == "call")
    full = build_assigned_stock_view(repo, account="lx", as_of_ms=3_000)
    compact = ledger_api.compact_assigned_stock_view(
        full, account="lx", current_position_lots=repo.list_position_lots(), as_of_ms=3_000,
    )
    assert [row["shares"] for row in compact["covered_call_allocations"]] == [200]
    if projection_state != "absent":
        with repo._connect() as conn:  # noqa: SLF001 - bootstrap pre-migration fixture
            conn.execute("""
                INSERT INTO current_decision_input_generations (
                    account, generation, case_generation, evidence_generation,
                    allocation_generation, source_consumption_generation, timing_generation,
                    combo_identity_generation, assigned_stock_generation, updated_at_ms
                ) VALUES ('lx', 0, 0, 0, 0, 0, 0, 0, 0, 3000)
            """)
        payload = ledger_api.build_current_decision_projection(
            repo, account="lx", updated_at_ms=3_000,
            assigned_stock_after=compact, all_quality_case_facts=[],
        )
        repo.upsert_current_decision_projection(ledger_api.current_decision_projection_row(payload))
    ledger_api.record_manual_position_close(
        repo, record_id=call_id, contracts_to_close=1, close_price=1,
        close_reason="BUY_BACK", as_of_ms=4_000,
    )
    full = build_assigned_stock_view(repo, account="lx", as_of_ms=sale_at_ms)
    compact = ledger_api.compact_assigned_stock_view(
        full, account="lx", current_position_lots=repo.list_position_lots(), as_of_ms=sale_at_ms,
    )
    assert sorted(row["shares"] for row in full["covered_call_allocations"]) == [100, 100]
    assert [row["shares"] for row in compact["covered_call_allocations"]] == [100]
    if projection_state != "absent":
        current = ledger_api.read_current_decision_projection(repo, account="lx", now_ms=sale_at_ms)
        assert current["status"] == "trusted"
        assert current["payload"]["assigned_stock"] == compact
    if projection_state == "dirty":
        with repo._connect() as conn:  # noqa: SLF001 - fixture represents an older projection
            conn.execute("""
                UPDATE current_decision_input_generations
                SET generation = generation + 1, timing_generation = timing_generation + 1
                WHERE account = 'lx'
            """)
    result = execute_manual_assigned_stock_sale(
        repo, target_stock_lot_id=stock_id, shares=100, price=105,
        trade_time_ms=sale_at_ms, dry_run=False,
    )
    assert result["mode"] == "applied"
    assert result["result"]["created"] is True
    assert len(repo.list_assigned_stock_events()) == 1
    after = build_assigned_stock_view(repo, account="lx", as_of_ms=sale_at_ms)
    assert after["assigned_stock_lots"][0]["shares_remaining"] == 100
    assert not any(row["status"] == "covered_call_unallocated" for row in after["assigned_stock_review_rows"])
    after_compact = ledger_api.compact_assigned_stock_view(
        after, account="lx", current_position_lots=repo.list_position_lots(), as_of_ms=sale_at_ms,
    )
    assert [row["shares"] for row in after_compact["covered_call_allocations"]] == [100]
    current = ledger_api.read_current_decision_projection(repo, account="lx", now_ms=sale_at_ms)
    if projection_state == "clean":
        assert current["status"] == "trusted"
        assert current["payload"]["assigned_stock"] == after_compact
    else:
        assert current["status"] != "trusted"
