from __future__ import annotations

import ast
from importlib import import_module
from pathlib import Path


PUBLIC_SYMBOLS = {
    "src.application.ledger.repository": {
        "POSITION_LOTS_COLUMN_CLASSIFICATION",
        "POSITION_PROJECTION_SCHEMA",
        "PositionLotDiff",
        "SQLiteOptionPositionsRepository",
        "TRADE_EVENTS_COLUMN_CLASSIFICATION",
        "TRADE_EVENT_PAGINATION_INDEXES",
        "TRADE_EVENT_PAGINATION_TRIGGERS",
        "TradeEventPaginationUnavailable",
        "initialize_ledger_connection",
        "require_option_positions_event_read_repo",
        "require_option_positions_event_write_repo",
        "require_option_positions_read_repo",
        "require_position_projection_publication_repo",
        "with_sqlite_repo_transaction",
    },
    "src.application.ledger.writer": {
        "accept_option_close_evidence_atomically",
        "adopt_existing_combo_identity_atomically",
        "advance_lifecycle_case_state_atomically",
        "apply_lifecycle_allocation_atomically",
        "bind_lifecycle_timing_policy_atomically",
        "discover_expired_lifecycle_cases_atomically",
        "persist_normalized_trade_events_atomically",
        "persist_trade_event",
        "persist_trade_event_object",
        "persist_trade_event_objects_atomically",
        "persist_trade_event_with_combo_identity",
        "persist_trade_event_with_wheel_intent",
        "projection_diagnostics_summary",
        "rebuild_position_lots_from_trade_events",
        "record_assigned_stock_event_atomically",
        "record_lifecycle_attempt_audit_atomically",
        "record_lifecycle_evidence_issue_atomically",
        "record_lifecycle_observation_attempt_atomically",
        "safe_int_count",
    },
    "src.application.ledger.current_decision_projection": {
        "CURRENT_ASSIGNED_STOCK_SCHEMA",
        "CURRENT_COMBO_GROUP_FACT_SCHEMA",
        "CURRENT_COMBO_SCHEMA",
        "CURRENT_DECISION_READ_SCHEMA",
        "CurrentDecisionProjectionError",
        "advance_lifecycle_case_decision_fact",
        "apply_current_decision_projection_migration",
        "arbitrate_lifecycle_case_facts",
        "build_current_combo_facts",
        "build_current_decision_projection",
        "build_current_decision_projection_migration_inventory",
        "build_current_decision_projection_payload",
        "build_initial_lifecycle_case_decision_fact",
        "build_lifecycle_case_decision_fact",
        "build_lifecycle_quality_fact",
        "capture_current_decision_projection_fence",
        "capture_trade_event_decision_projection_fence",
        "compact_assigned_stock_view",
        "current_decision_projection_migration_status",
        "current_decision_projection_row",
        "defer_current_decision_projection",
        "derive_lifecycle_quality_view",
        "empty_assigned_stock_fact",
        "encode_current_decision_projection",
        "finalize_current_decision_projection",
        "lifecycle_views_by_lot",
        "preview_current_decision_projection_oracle",
        "read_current_assigned_stock_fact",
        "read_current_decision_projection",
        "read_lifecycle_case_decision_fact",
        "update_assigned_stock_fact",
        "validate_assigned_stock_fact",
        "validate_current_decision_projection_payload",
        "verify_current_decision_projection",
        "verify_current_decision_projection_migration",
        "write_lifecycle_case_decision_fact",
    },
}

CURRENT_DECISION_RUNTIME_EXPORTS = {
    "CURRENT_DECISION_READ_SCHEMA",
    "CurrentDecisionAccountFence",
    "CurrentDecisionProjectionError",
    "CurrentDecisionProjectionFence",
    "advance_assigned_stock_fact_for_trade_events",
    "build_current_decision_projection",
    "capture_current_decision_projection_fence",
    "capture_trade_event_decision_projection_fence",
    "current_decision_projection_row",
    "defer_current_decision_projection",
    "finalize_current_decision_projection",
    "read_current_assigned_stock_fact",
    "read_current_decision_projection",
    "validate_assigned_stock_fact",
    "verify_current_decision_projection",
}

CURRENT_DECISION_EXPORT_OWNERS = {
    "src.application.ledger.current_decision_common": {
        "CURRENT_ASSIGNED_STOCK_SCHEMA",
        "CURRENT_COMBO_GROUP_FACT_SCHEMA",
        "CURRENT_COMBO_SCHEMA",
        "CURRENT_DECISION_MIGRATION_INVENTORY_SCHEMA",
        "CURRENT_DECISION_PROJECTION_SCHEMA",
        "CURRENT_DECISION_READ_SCHEMA",
        "CURRENT_LIFECYCLE_QUALITY_SCHEMA",
        "LIFECYCLE_CASE_DECISION_FACT_SCHEMA",
        "CurrentDecisionAccountFence",
        "CurrentDecisionProjectionError",
        "CurrentDecisionProjectionFence",
    },
    "src.application.ledger.current_decision_assigned_stock": {
        "advance_assigned_stock_fact_for_trade_events",
        "compact_assigned_stock_view",
        "empty_assigned_stock_fact",
        "update_assigned_stock_fact",
        "validate_assigned_stock_fact",
    },
    "src.application.ledger.current_decision_lifecycle": {
        "advance_lifecycle_case_decision_fact",
        "build_initial_lifecycle_case_decision_fact",
        "build_lifecycle_case_decision_fact",
        "validate_lifecycle_case_decision_fact",
    },
    "src.application.ledger.current_decision_combo": {
        "build_current_combo_facts",
        "validate_current_combo_facts",
    },
    "src.application.ledger.current_decision_quality": {
        "build_lifecycle_quality_fact",
        "validate_lifecycle_quality_fact",
    },
    "src.application.ledger.current_decision_payload": {
        "build_current_decision_projection",
        "build_current_decision_projection_payload",
        "current_decision_projection_row",
        "encode_current_decision_projection",
        "encode_lifecycle_case_decision_fact",
        "read_lifecycle_case_decision_fact",
        "validate_current_decision_projection_payload",
        "write_lifecycle_case_decision_fact",
    },
    "src.application.ledger.current_decision_oracle": {
        "preview_current_decision_projection_oracle",
    },
    "src.application.ledger.current_decision_migration": {
        "apply_current_decision_projection_migration",
        "build_current_decision_projection_migration_inventory",
        "current_decision_projection_migration_status",
        "verify_current_decision_projection_migration",
    },
    "src.application.ledger.current_decision_runtime": {
        "capture_current_decision_projection_fence",
        "capture_trade_event_decision_projection_fence",
        "defer_current_decision_projection",
        "finalize_current_decision_projection",
        "read_current_assigned_stock_fact",
        "read_current_decision_projection",
        "verify_current_decision_projection",
    },
}

CURRENT_DECISION_FACADE_EXPORTS = set().union(*CURRENT_DECISION_EXPORT_OWNERS.values())

LEDGER_ROOT = Path(__file__).resolve().parents[1] / "src/application/ledger"


def test_ledger_facades_keep_existing_production_imports() -> None:
    for module_name, names in PUBLIC_SYMBOLS.items():
        module = import_module(module_name)
        assert not (names - set(vars(module))), module_name


def test_current_decision_modules_keep_export_contracts() -> None:
    runtime_name = "src.application.ledger.current_decision_runtime"
    facade_name = "src.application.ledger.current_decision_projection"
    runtime = import_module(runtime_name)
    facade = import_module(facade_name)

    assert set(runtime.__all__) == CURRENT_DECISION_RUNTIME_EXPORTS
    assert set(facade.__all__) == CURRENT_DECISION_FACADE_EXPORTS

    for module_name, expected in (
        (runtime_name, CURRENT_DECISION_RUNTIME_EXPORTS),
        (facade_name, CURRENT_DECISION_FACADE_EXPORTS),
    ):
        namespace: dict[str, object] = {}
        exec(f"from {module_name} import *", namespace)
        assert set(namespace) - {"__builtins__"} == expected

    for owner_name, names in CURRENT_DECISION_EXPORT_OWNERS.items():
        owner = import_module(owner_name)
        for name in names:
            assert getattr(facade, name) is getattr(owner, name)


def test_repository_facade_keeps_core_aggregate_reads(tmp_path) -> None:
    repository = import_module("src.application.ledger.repository")
    repo = repository.SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")

    assert repo.list_trade_events() == []
    assert repo.list_position_lots() == []
    assert repo.list_assigned_stock_events() == []


def test_giant_modules_are_thin_compatibility_facades() -> None:
    expected_definitions = {
        "repository.py": [
            "SQLiteOptionPositionsRepository",
            "with_sqlite_repo_transaction",
            "require_option_positions_read_repo",
            "require_option_positions_event_read_repo",
            "require_option_positions_event_write_repo",
            "require_position_projection_publication_repo",
        ],
        "writer.py": [],
        "current_decision_projection.py": [],
    }
    for filename, expected in expected_definitions.items():
        tree = ast.parse((LEDGER_ROOT / filename).read_bytes())
        definitions = [
            node.name
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        ]
        assert definitions == expected

    repository_tree = ast.parse((LEDGER_ROOT / "repository.py").read_bytes())
    repository_class = next(
        node for node in repository_tree.body if isinstance(node, ast.ClassDef)
    )
    assert all(isinstance(node, ast.Pass) for node in repository_class.body)
