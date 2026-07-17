from __future__ import annotations

from pathlib import Path

from src.application.ledger import api as ledger_api


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
