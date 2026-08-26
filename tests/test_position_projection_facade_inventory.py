from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
LEDGER_ROOT = REPO_ROOT / "src" / "application" / "ledger"


class _CallInventory(ast.NodeVisitor):
    def __init__(self, relative_path: str) -> None:
        self.relative_path = relative_path
        self.functions: list[str] = []
        self.calls: list[tuple[str, str, str, str | None]] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.functions.append(node.name)
        self.generic_visit(node)
        self.functions.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Call(self, node: ast.Call) -> None:
        name = (
            node.func.id
            if isinstance(node.func, ast.Name)
            else node.func.attr
            if isinstance(node.func, ast.Attribute)
            else ""
        )
        mode = next(
            (
                ast.unparse(keyword.value)
                for keyword in node.keywords
                if keyword.arg == "mode"
            ),
            None,
        )
        self.calls.append(
            (
                self.relative_path,
                ">".join(self.functions) or "<module>",
                name,
                mode,
            )
        )
        self.generic_visit(node)


def _ledger_calls() -> list[tuple[str, str, str, str | None]]:
    calls: list[tuple[str, str, str, str | None]] = []
    for path in sorted(LEDGER_ROOT.glob("*.py")):
        visitor = _CallInventory(str(path.relative_to(REPO_ROOT)))
        visitor.visit(ast.parse(path.read_text(encoding="utf-8")))
        calls.extend(visitor.calls)
    return calls


def test_trade_event_writes_and_projection_publication_have_one_owner() -> None:
    calls = _ledger_calls()
    direct_event_writes = Counter(
        (path, function)
        for path, function, name, _mode in calls
        if name == "upsert_trade_event"
    )
    lot_replacements = [
        (path, function)
        for path, function, name, _mode in calls
        if name == "replace_position_lots"
    ]

    assert direct_event_writes == Counter(
        {
            (
                "src/application/ledger/position_projection_runtime.py",
                "_run_position_projection_in_transaction_impl",
            ): 1,
        }
    )
    assert lot_replacements == []

    raw_event_dml: list[str] = []
    for path in sorted(LEDGER_ROOT.glob("*.py")):
        source = path.read_text(encoding="utf-8").upper()
        if any(
            statement in source
            for statement in (
                "INSERT INTO TRADE_EVENTS",
                "UPDATE TRADE_EVENTS",
                "DELETE FROM TRADE_EVENTS",
                "REPLACE INTO TRADE_EVENTS",
            )
        ):
            raw_event_dml.append(str(path.relative_to(REPO_ROOT)))
    assert raw_event_dml == [
        "src/application/ledger/repository_projection.py",
        "src/application/ledger/repository_trade_events.py",
        "src/application/ledger/repository_trade_schema.py",
    ]


def test_projection_runtime_facade_modes_are_fully_inventoried() -> None:
    runtime_calls = Counter(
        (path, function, mode)
        for path, function, name, mode in _ledger_calls()
        if name == "run_position_projection_in_transaction"
    )

    assert runtime_calls == Counter(
        {
            (
                "src/application/ledger/bootstrap.py",
                "materialize_bootstrap_events>_run",
                "'forced_full'",
            ): 1,
            (
                "src/application/ledger/bootstrap.py",
                "load_option_positions_repo>_recover",
                "'forced_full'",
            ): 1,
            (
                "src/application/ledger/combo_reconciliation.py",
                "adopt_post_trade_combo_pair>_run",
                "'forced_full'",
            ): 1,
            (
                "src/application/ledger/combo_reconciliation.py",
                "supersede_post_trade_combo_pair>_run",
                "'forced_full'",
            ): 1,
            (
                "src/application/ledger/interventions.py",
                "persist_manual_repair_event>_run",
                "'forced_full'",
            ): 1,
            (
                "src/application/ledger/manual_trades.py",
                "persist_manual_adjust_events>_run",
                "'fast_if_safe'",
            ): 1,
            (
                "src/application/ledger/order_fee_migration.py",
                "_apply_unit",
                "'forced_full'",
            ): 1,
            (
                "src/application/ledger/position_projection_migration.py",
                "apply_position_projection_migration",
                "'forced_full'",
            ): 1,
            (
                "src/application/ledger/position_projection_runtime.py",
                "_run_runtime",
                "mode",
            ): 1,
            (
                "src/application/ledger/writer_trade_events.py",
                "rebuild_position_lots_from_trade_events>_run",
                "'forced_full'",
            ): 1,
            (
                "src/application/ledger/writer_trade_events.py",
                "persist_trade_event_object>_run",
                "_projection_mode_for_events(storage_events)",
            ): 1,
            (
                "src/application/ledger/writer_trade_events.py",
                "persist_trade_event_with_combo_identity>_run",
                "'forced_full'",
            ): 1,
            (
                "src/application/ledger/writer_lifecycle_allocation.py",
                "apply_lifecycle_allocation_atomically>_run",
                "'forced_full'",
            ): 1,
            (
                "src/application/ledger/writer_trade_events.py",
                "persist_trade_event_objects_atomically>_run",
                "_projection_mode_for_events(storage_events, force_full=bool(case_update or allocation_rows))",
            ): 1,
        }
    )


def test_full_projection_calls_are_explicitly_classified() -> None:
    full_calls = Counter(
        (path, function, name)
        for path, function, name, _mode in _ledger_calls()
        if name in {
            "project_stored_trade_events_to_position_lots",
            "project_trade_events",
        }
    )

    assert full_calls == Counter(
        {
            (
                "src/application/ledger/bootstrap.py",
                "materialize_bootstrap_events>_run",
                "project_stored_trade_events_to_position_lots",
            ): 1,
            (
                "src/application/ledger/combo_reconciliation.py",
                "supersede_post_trade_combo_pair>_run",
                "project_stored_trade_events_to_position_lots",
            ): 1,
            (
                "src/application/ledger/combo_reconciliation.py",
                "_validate_inference_against_current_ledger",
                "project_stored_trade_events_to_position_lots",
            ): 1,
                (
                        "src/application/ledger/current_decision_oracle.py",
                    "_oracle_assigned_stock_report",
                    "project_stored_trade_events_to_position_lots",
                ): 1,
                (
                    "src/application/ledger/decision_snapshot.py",
                "decision_state_snapshot_from_rows",
                "project_stored_trade_events_to_position_lots",
            ): 1,
            (
                "src/application/ledger/migration.py",
                "shadow_replay_position_lot_snapshot",
                "project_trade_events",
            ): 1,
            (
                "src/application/ledger/position_projection_migration.py",
                "_verify_from_conn",
                "project_stored_trade_events_to_position_lots",
            ): 1,
            (
                "src/application/ledger/position_projection_runtime.py",
                "_preview_full",
                "project_stored_trade_events_to_position_lots",
            ): 2,
            (
                "src/application/ledger/position_projection_runtime.py",
                "_run_full_path",
                "project_stored_trade_events_to_position_lots",
            ): 1,
            (
                "src/application/ledger/preflight.py",
                "_preflight_trade_event_append",
                "project_trade_events",
            ): 2,
            (
                "src/application/ledger/projection_verify.py",
                "verify_position_projection",
                "project_stored_trade_events_to_position_lots",
            ): 1,
            (
                "src/application/ledger/queries.py",
                "project_trade_event_log",
                "project_stored_trade_events_to_position_lots",
            ): 1,
            (
                "src/application/ledger/writer_trade_events.py",
                "adopt_existing_combo_identity_atomically>_run",
                "project_stored_trade_events_to_position_lots",
            ): 1,
        }
    )
