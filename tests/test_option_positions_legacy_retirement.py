from __future__ import annotations

import ast
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def _python_text_offenders(
    roots: list[Path],
    banned: tuple[str, ...],
    *,
    exclude: tuple[Path, ...] = (),
) -> list[str]:
    offenders = []
    for root in roots:
        for path in root.rglob("*.py"):
            if path in exclude:
                continue
            text = path.read_text(encoding="utf-8")
            if any(fragment in text for fragment in banned):
                offenders.append(str(path.relative_to(REPO_ROOT)))
    return offenders


def test_option_positions_v2_code_is_physically_retired() -> None:
    retired_paths = [
        REPO_ROOT / "domain" / "domain" / "option_position_ledger.py",
        REPO_ROOT / "domain" / "domain" / "option_positions_v2.py",
        REPO_ROOT / "domain" / "storage" / "repositories" / "option_positions_v2_repo.py",
        REPO_ROOT / "src" / "application" / "option_positions_v2_service.py",
        REPO_ROOT / "src" / "application" / "option_positions_service.py",
        REPO_ROOT / "src" / "application" / "option_positions_facade.py",
        REPO_ROOT / "src" / "application" / "option_positions_auto_close.py",
        REPO_ROOT / "src" / "application" / "option_positions_context_builder.py",
        REPO_ROOT / "src" / "application" / "option_positions_feishu_sync.py",
        REPO_ROOT / "src" / "application" / "option_positions_feishu_sync_receipt.py",
        REPO_ROOT / "src" / "application" / "option_positions_inspection.py",
        REPO_ROOT / "src" / "application" / "option_positions_reporting.py",
        REPO_ROOT / "src" / "application" / "option_positions_sync_config.py",
        REPO_ROOT / "src" / "application" / "positions" / "feishu_sync.py",
        REPO_ROOT / "src" / "application" / "positions" / "feishu_sync_receipt.py",
        REPO_ROOT / "src" / "application" / "positions" / "sync_config.py",
        REPO_ROOT / "src" / "application" / "ledger" / "sync_metadata.py",
        REPO_ROOT / "src" / "application" / "position_maintenance.py",
        REPO_ROOT / "src" / "application" / "position_maintenance_receipt.py",
        REPO_ROOT / "tests" / "test_option_positions_legacy_v2.py",
        REPO_ROOT / "tests" / "test_option_positions_service.py",
        REPO_ROOT / "tests" / "test_option_positions_sqlite_service.py",
    ]
    assert [str(path.relative_to(REPO_ROOT)) for path in retired_paths if path.exists()] == []


def test_legacy_position_trade_test_filenames_are_retired() -> None:
    retired_paths = [
        REPO_ROOT / "tests" / "test_option_positions_auto_close.py",
        REPO_ROOT / "tests" / "test_option_positions_context_partial_close.py",
        REPO_ROOT / "tests" / "test_option_positions_feishu_sync_receipt.py",
        REPO_ROOT / "tests" / "test_option_positions_reporting.py",
        REPO_ROOT / "tests" / "test_positions_feishu_sync.py",
        REPO_ROOT / "tests" / "test_positions_feishu_sync_receipt.py",
        REPO_ROOT / "tests" / "test_position_maintenance.py",
        REPO_ROOT / "tests" / "test_position_maintenance_receipt.py",
        REPO_ROOT / "tests" / "test_position_workflows_auto_sync.py",
        REPO_ROOT / "tests" / "test_positions_workflows_auto_sync.py",
        REPO_ROOT / "tests" / "test_sync_option_positions_to_feishu.py",
        REPO_ROOT / "tests" / "test_auto_trade_intake_audit.py",
        REPO_ROOT / "tests" / "test_auto_trade_intake_cli.py",
        REPO_ROOT / "tests" / "test_futu_trade_detail_lookup.py",
        REPO_ROOT / "tests" / "test_trade_account_mapping.py",
        REPO_ROOT / "tests" / "test_trade_event_normalizer.py",
        REPO_ROOT / "tests" / "test_trade_intake_receipt.py",
        REPO_ROOT / "tests" / "test_trade_intake_resolver_close.py",
        REPO_ROOT / "tests" / "test_trade_intake_resolver_open.py",
        REPO_ROOT / "tests" / "test_trade_intake_state.py",
        REPO_ROOT / "tests" / "test_trade_intent.py",
        REPO_ROOT / "tests" / "test_trade_push_listener.py",
    ]
    assert [str(path.relative_to(REPO_ROOT)) for path in retired_paths if path.exists()] == []


def test_option_positions_v2_imports_do_not_return_to_runtime_code() -> None:
    roots = [REPO_ROOT / "domain", REPO_ROOT / "src"]
    banned = (
        "domain.domain.option_positions_v2",
        "option_positions_v2_service",
        "option_positions_v2_repo",
        "load_option_positions_v2_records",
        "refresh_option_positions_v2_state",
    )
    assert _python_text_offenders(roots, banned) == []


def test_legacy_option_position_lots_is_compatibility_reexport() -> None:
    legacy_path = REPO_ROOT / "domain" / "domain" / "option_position_lots.py"
    text = legacy_path.read_text(encoding="utf-8")

    assert "from domain.domain.ledger.position_fields import *" in text
    assert "class OpenPositionCommand" not in text
    assert "def build_open_fields(" not in text
    assert "def build_close_patch(" not in text


def test_core_position_trade_runtime_imports_ledger_position_fields() -> None:
    roots = [
        REPO_ROOT / "src" / "application" / "ledger",
        REPO_ROOT / "src" / "application" / "positions",
        REPO_ROOT / "src" / "application" / "trades",
        REPO_ROOT / "src" / "interfaces" / "cli",
    ]
    assert (REPO_ROOT / "domain" / "domain" / "ledger" / "position_fields.py").exists()
    assert _python_text_offenders(roots, ("domain.domain.option_position_lots",)) == []


def test_runtime_code_does_not_import_legacy_position_lots_reexport() -> None:
    roots = [REPO_ROOT / "src", REPO_ROOT / "domain" / "domain"]
    assert _python_text_offenders(
        roots,
        ("from domain.domain.option_position_lots import",),
        exclude=(REPO_ROOT / "domain" / "domain" / "option_position_lots.py",),
    ) == []


def test_ledger_write_paths_use_position_field_contract_builders() -> None:
    checked = [
        REPO_ROOT / "src" / "application" / "ledger" / "commands.py",
        REPO_ROOT / "src" / "application" / "ledger" / "manual_trades.py",
        REPO_ROOT / "src" / "application" / "ledger" / "maintenance.py",
        REPO_ROOT / "src" / "application" / "ledger" / "preflight.py",
        REPO_ROOT / "src" / "application" / "ledger" / "publisher.py",
    ]
    banned_imports = {
        "build_open_fields",
        "build_close_patch",
        "build_open_adjustment_patch",
        "build_expire_auto_close_patch",
    }
    offenders: list[str] = []
    for path in checked:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            if node.module != "domain.domain.ledger.position_fields":
                continue
            for alias in node.names:
                if alias.name in banned_imports:
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{alias.name}")

    assert offenders == []


def test_position_lot_projection_uses_position_patch_decoder() -> None:
    lots_text = (REPO_ROOT / "domain" / "domain" / "ledger" / "lots.py").read_text(encoding="utf-8")
    fields_text = (REPO_ROOT / "domain" / "domain" / "ledger" / "position_fields.py").read_text(encoding="utf-8")

    assert "def decode_position_lot_patch(" in fields_text
    assert "decode_position_lot_patch(event.raw_payload.get(\"patch\"))" in lots_text
    assert "patch.get(" not in lots_text


def test_position_lot_sync_metadata_is_retired() -> None:
    repository_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(
            (REPO_ROOT / "src" / "application" / "ledger").glob(
                "repository*.py"
            )
        )
    )
    commands_text = (REPO_ROOT / "src" / "application" / "ledger" / "commands.py").read_text(encoding="utf-8")

    assert not (REPO_ROOT / "src" / "application" / "ledger" / "sync_metadata.py").exists()
    assert "PositionLotSyncMetadataPatch" not in repository_text
    assert "PositionLotSyncMetadataPatch" not in commands_text
    assert "def update_position_lot_sync_metadata(" not in repository_text
    assert "def update_position_lot_fields(" not in repository_text
    assert "update_position_lot_fields" not in commands_text


def test_position_lot_projection_write_path_uses_explicit_record_contract() -> None:
    record_text = (REPO_ROOT / "src" / "application" / "ledger" / "position_records.py").read_text(encoding="utf-8")
    publisher_text = (REPO_ROOT / "src" / "application" / "ledger" / "publisher.py").read_text(encoding="utf-8")
    repository_text = "\n".join(
        (
            REPO_ROOT / "src" / "application" / "ledger" / filename
        ).read_text(encoding="utf-8")
        for filename in ("repository_common.py", "repository_projection_tail.py")
    )
    writer_text = (
        REPO_ROOT / "src" / "application" / "ledger" / "writer_trade_events.py"
    ).read_text(encoding="utf-8")

    assert "class PositionLotRecord" in record_text
    assert "lots: list[PositionLotRecord]" in publisher_text
    assert "records: Sequence[PositionLotRecord]" in repository_text
    assert "replace_position_lots requires PositionLotRecord records" in repository_text
    assert "merge_preserved_position_lot_metadata" not in writer_text
    assert "replace_position_lots(self, records: list[dict[str, Any]]" not in repository_text


def test_runtime_code_does_not_import_legacy_option_position_ledger() -> None:
    roots = [REPO_ROOT / "src", REPO_ROOT / "scripts"]
    assert _python_text_offenders(roots, ("domain.domain.option_position_ledger",)) == []


def test_option_positions_v2_is_not_exported_from_lazy_packages() -> None:
    import domain.domain as domain_pkg
    import domain.storage.repositories as repository_pkg

    assert "option_positions_v2" not in dir(domain_pkg)
    assert "option_positions_v2_repo" not in dir(repository_pkg)


def test_default_position_read_paths_do_not_fallback_to_legacy_records() -> None:
    checked = [
        REPO_ROOT / "src" / "application" / "ledger" / "read_model.py",
        REPO_ROOT / "src" / "application" / "positions" / "workflows.py",
        REPO_ROOT / "src" / "application" / "trades" / "resolver.py",
    ]
    offenders: list[str] = []
    for path in checked:
        text = path.read_text(encoding="utf-8")
        if "list_records(page_size=500)" in text or 'getattr(repo, "list_records"' in text:
            offenders.append(str(path.relative_to(REPO_ROOT)))
    assert offenders == []


def test_runtime_code_uses_ledger_read_model_instead_of_option_positions_facade() -> None:
    roots = [REPO_ROOT / "src" / "application", REPO_ROOT / "src" / "interfaces", REPO_ROOT / "scripts"]
    assert _python_text_offenders(
        roots,
        ("option_positions_facade import", "import src.application.option_positions_facade"),
        exclude=(REPO_ROOT / "src" / "application" / "option_positions_facade.py",),
    ) == []


def test_runtime_code_routes_position_service_through_ledger_service() -> None:
    roots = [REPO_ROOT / "src" / "application", REPO_ROOT / "src" / "interfaces", REPO_ROOT / "scripts"]
    assert _python_text_offenders(roots, ("src.application.option_positions_service",)) == []


def test_ledger_preflight_has_dedicated_owner() -> None:
    preflight_path = REPO_ROOT / "src" / "application" / "ledger" / "preflight.py"
    service_path = REPO_ROOT / "src" / "application" / "ledger" / "service.py"

    assert preflight_path.exists()
    assert not service_path.exists()
    preflight_text = preflight_path.read_text(encoding="utf-8")
    assert "def _preflight_open_event(" in preflight_text
    assert "def _preflight_lot_close(" in preflight_text
    assert "def _preflight_lot_adjust(" in preflight_text


def test_manual_ledger_command_results_use_explicit_contracts() -> None:
    results_text = (REPO_ROOT / "src" / "application" / "ledger" / "results.py").read_text(encoding="utf-8")
    preflight_text = (REPO_ROOT / "src" / "application" / "ledger" / "preflight.py").read_text(encoding="utf-8")
    commands_text = (REPO_ROOT / "src" / "application" / "ledger" / "commands.py").read_text(encoding="utf-8")
    service_path = REPO_ROOT / "src" / "application" / "ledger" / "service.py"

    assert "class LedgerPreflightResult" in results_text
    assert "class LedgerWriteResult" in results_text
    assert "class ManualOpenPreviewResult" in results_text
    assert "class ManualClosePreviewResult" in results_text
    assert "class ManualAdjustPreviewResult" in results_text
    assert not service_path.exists()
    assert "def preflight_manual_open(\n" in preflight_text
    assert ") -> LedgerPreflightResult:" in preflight_text
    assert ") -> ManualOpenPreviewResult:" in commands_text
    assert ") -> ManualClosePreviewResult:" in commands_text
    assert ") -> ManualAdjustPreviewResult:" in commands_text
    assert ") -> OpenLedgerResult:" in commands_text
    assert ") -> ManualCloseLedgerResult:" in commands_text
    assert ") -> ManualAdjustLedgerResult:" in commands_text


def test_auto_close_maintenance_results_use_explicit_contracts() -> None:
    results_text = (REPO_ROOT / "src" / "application" / "ledger" / "results.py").read_text(encoding="utf-8")
    maintenance_text = (REPO_ROOT / "src" / "application" / "ledger" / "maintenance.py").read_text(
        encoding="utf-8"
    )
    commands_text = (REPO_ROOT / "src" / "application" / "ledger" / "commands.py").read_text(encoding="utf-8")
    position_maintenance_text = (REPO_ROOT / "src" / "application" / "positions" / "maintenance.py").read_text(
        encoding="utf-8"
    )

    assert "class ExpiredCloseDecision" in results_text
    assert "class ExpiredCloseApplyResult" in results_text
    assert "class ExpiredCloseRunResult" in results_text
    assert ") -> list[ExpiredCloseDecision]:" in maintenance_text
    assert ") -> ExpiredCloseRunResult:" in maintenance_text
    assert ") -> list[ExpiredCloseDecision]:" in commands_text
    assert ") -> ExpiredCloseRunResult:" in commands_text
    assert "def _expired_close_run_payloads(" in position_maintenance_text
    assert "record_expired_position_closes(" in position_maintenance_text
    assert "decisions, applied, errors = record_expired_position_closes(" not in position_maintenance_text


def test_broker_trade_operations_use_explicit_contracts() -> None:
    results_text = (REPO_ROOT / "src" / "application" / "ledger" / "results.py").read_text(encoding="utf-8")
    commands_text = (REPO_ROOT / "src" / "application" / "ledger" / "commands.py").read_text(encoding="utf-8")
    workflows_text = (REPO_ROOT / "src" / "application" / "trades" / "workflows.py").read_text(encoding="utf-8")
    resolver_text = (REPO_ROOT / "src" / "application" / "trades" / "resolver.py").read_text(encoding="utf-8")

    assert "class BrokerTradeOpenPreviewResult" in results_text
    assert "class BrokerTradeOperation" in results_text
    assert ") -> BrokerTradeOpenPreviewResult:" in commands_text
    assert ") -> BrokerTradeOperation:" in commands_text
    assert ") -> list[BrokerTradeOperation]:" in commands_text
    assert ") -> BrokerTradeOpenPreviewResult:" in workflows_text
    assert ") -> BrokerTradeOperation:" in workflows_text
    assert "operations: list[BrokerTradeOperation]" in resolver_text
    assert "operations: list[dict[str, Any]]" not in resolver_text


def test_trade_event_interventions_use_explicit_preview_contracts() -> None:
    results_text = (REPO_ROOT / "src" / "application" / "ledger" / "results.py").read_text(encoding="utf-8")
    interventions_text = (REPO_ROOT / "src" / "application" / "ledger" / "interventions.py").read_text(
        encoding="utf-8"
    )
    commands_text = (REPO_ROOT / "src" / "application" / "ledger" / "commands.py").read_text(encoding="utf-8")

    assert "class TradeEventInterventionPreview" in results_text
    assert "preview: TradeEventInterventionPreview | dict[str, Any]" in results_text
    assert ") -> TradeEventInterventionPreview:" in interventions_text
    assert ") -> LedgerWriteResult:" in interventions_text
    assert ") -> TradeEventInterventionLedgerResult:" in commands_text
    assert "TradeEventInterventionLedgerResult(" in commands_text


def test_close_lot_resolver_has_single_application_owner() -> None:
    resolver_path = REPO_ROOT / "src" / "application" / "ledger" / "lot_resolver.py"
    position_workflows_text = (REPO_ROOT / "src" / "application" / "positions" / "workflows.py").read_text(encoding="utf-8")
    trade_intake_text = (REPO_ROOT / "src" / "application" / "trades" / "resolver.py").read_text(encoding="utf-8")

    assert resolver_path.exists()
    assert "src.application.ledger.api" in position_workflows_text
    assert "src.application.ledger.api" in trade_intake_text
    assert "src.application.ledger.lot_resolver" not in position_workflows_text
    assert "src.application.ledger.lot_resolver" not in trade_intake_text
    assert "def _manual_close_candidate_summary(" not in position_workflows_text
    assert "def _iter_open_candidates(" not in trade_intake_text


def test_trade_and_position_workflows_have_separate_application_owners() -> None:
    position_workflows_text = (REPO_ROOT / "src" / "application" / "positions" / "workflows.py").read_text(encoding="utf-8")
    trade_intake_text = (REPO_ROOT / "src" / "application" / "trades" / "resolver.py").read_text(encoding="utf-8")
    trade_workflows_text = (REPO_ROOT / "src" / "application" / "trades" / "workflows.py").read_text(encoding="utf-8")

    assert "from src.application.trades.workflows import" in trade_intake_text
    assert "from src.application.positions.workflows import" not in trade_intake_text
    assert "def preview_trade_open(" in trade_workflows_text
    assert "def apply_trade_close_with(" in trade_workflows_text
    banned_position_trade_helpers = (
        "NormalizedTradeDeal",
        "persist_trade_open_event_with_ledger",
        "persist_trade_close_events_with_ledger",
        "def _build_trade_open_command(",
        "def preview_trade_open(",
        "def apply_trade_open_with(",
        "def preview_trade_close(",
        "def apply_trade_close_with(",
    )
    assert [item for item in banned_position_trade_helpers if item in position_workflows_text] == []


def test_position_maintenance_lives_under_positions_namespace() -> None:
    roots = [REPO_ROOT / "src", REPO_ROOT / "scripts"]
    banned_imports = (
        "src.application.option_positions_auto_close",
        "src.application.position_maintenance",
        "src.application.position_maintenance_receipt",
        "from src.application import option_positions_auto_close",
        "from src.application import position_maintenance",
    )
    assert (REPO_ROOT / "src" / "application" / "positions" / "auto_close.py").exists()
    assert (REPO_ROOT / "src" / "application" / "positions" / "maintenance.py").exists()
    assert (REPO_ROOT / "src" / "application" / "positions" / "maintenance_receipt.py").exists()
    assert _python_text_offenders(roots, banned_imports) == []


def test_position_feishu_sync_is_fully_retired() -> None:
    roots = [REPO_ROOT / "src", REPO_ROOT / "scripts"]
    banned_imports = (
        "src.application.option_positions_feishu_sync",
        "src.application.option_positions_feishu_sync_receipt",
        "src.application.option_positions_sync_config",
        "src.application.positions.feishu_sync",
        "src.application.positions.feishu_sync_receipt",
        "src.application.positions.sync_config",
        "src.application.ledger.sync_metadata",
    )
    assert not (REPO_ROOT / "src" / "application" / "positions" / "feishu_sync.py").exists()
    assert not (REPO_ROOT / "src" / "application" / "positions" / "feishu_sync_receipt.py").exists()
    assert not (REPO_ROOT / "src" / "application" / "positions" / "sync_config.py").exists()
    assert _python_text_offenders(roots, banned_imports) == []


def test_position_read_reporting_lives_under_positions_namespace() -> None:
    roots = [REPO_ROOT / "src", REPO_ROOT / "scripts"]
    banned_imports = (
        "src.application.option_positions_context_builder",
        "src.application.option_positions_inspection",
        "src.application.option_positions_reporting",
    )
    assert (REPO_ROOT / "src" / "application" / "positions" / "context_builder.py").exists()
    assert (REPO_ROOT / "src" / "application" / "positions" / "inspection.py").exists()
    assert not (REPO_ROOT / "src" / "application" / "positions" / "reporting.py").exists()
    assert (REPO_ROOT / "src" / "application" / "positions" / "assigned_stock_view.py").exists()
    assert _python_text_offenders(roots, banned_imports) == []


def test_trade_intake_lives_under_trades_namespace() -> None:
    retired_paths = [
        REPO_ROOT / "src" / "application" / "auto_trade_intake.py",
        REPO_ROOT / "src" / "application" / "futu_trade_detail_lookup.py",
        REPO_ROOT / "src" / "application" / "trade_account_mapping.py",
        REPO_ROOT / "src" / "application" / "trade_event_normalizer.py",
        REPO_ROOT / "src" / "application" / "trade_intake.py",
        REPO_ROOT / "src" / "application" / "trade_intake_receipt.py",
        REPO_ROOT / "src" / "application" / "trade_intake_resolver.py",
        REPO_ROOT / "src" / "application" / "trade_intake_state.py",
        REPO_ROOT / "src" / "application" / "trade_intent.py",
        REPO_ROOT / "src" / "application" / "trade_push_listener.py",
    ]
    assert [str(path.relative_to(REPO_ROOT)) for path in retired_paths if path.exists()] == []

    roots = [REPO_ROOT / "src", REPO_ROOT / "scripts"]
    banned_imports = (
        "src.application.auto_trade_intake",
        "src.application.futu_trade_detail_lookup",
        "src.application.trade_account_mapping",
        "src.application.trade_event_normalizer",
        "src.application.trade_intake",
        "src.application.trade_intake_receipt",
        "src.application.trade_intake_resolver",
        "src.application.trade_intake_state",
        "src.application.trade_intent",
        "src.application.trade_push_listener",
    )
    assert (REPO_ROOT / "src" / "application" / "trades" / "auto_intake.py").exists()
    assert (REPO_ROOT / "src" / "application" / "trades" / "resolver.py").exists()
    assert (REPO_ROOT / "src" / "application" / "trades" / "normalizer.py").exists()
    assert _python_text_offenders(roots, banned_imports) == []


def test_repository_config_and_guards_live_under_ledger_repository() -> None:
    repository_text = "\n".join(
        (
            REPO_ROOT / "src" / "application" / "ledger" / filename
        ).read_text(encoding="utf-8")
        for filename in ("repository.py", "repository_common.py")
    )
    moved_defs = (
        "class SQLiteOptionPositionsRepository",
        "def option_positions_bootstrap_from_feishu_enabled(",
        "def resolve_option_positions_sqlite_path(",
        "def require_option_positions_read_repo(",
        "def require_option_positions_event_write_repo(",
        "def with_sqlite_repo_transaction(",
    )
    assert [item for item in moved_defs if item not in repository_text] == []


def test_bootstrap_flow_lives_under_ledger_bootstrap() -> None:
    bootstrap_text = (REPO_ROOT / "src" / "application" / "ledger" / "bootstrap.py").read_text(encoding="utf-8")
    moved_defs = (
        "def _normalize_bootstrap_records(",
        "def _bootstrap_trade_events(",
        "def materialize_bootstrap_events(",
        "def apply_bootstrap_snapshot(",
        "def load_option_positions_repo(",
    )
    assert [item for item in moved_defs if item not in bootstrap_text] == []


def test_event_write_projection_lives_under_ledger_writer() -> None:
    results_text = (REPO_ROOT / "src" / "application" / "ledger" / "results.py").read_text(encoding="utf-8")
    writer_text = "\n".join(
        (
            REPO_ROOT / "src" / "application" / "ledger" / filename
        ).read_text(encoding="utf-8")
        for filename in ("writer_common.py", "writer_trade_events.py")
    )
    moved_defs = (
        "def projection_diagnostics_summary(",
        "def rebuild_position_lots_from_trade_events(",
        "def persist_trade_event_object(",
        "def persist_trade_event(",
    )
    assert [item for item in moved_defs if item not in writer_text] == []
    assert "class LedgerWriteResult" in results_text
    assert "class ProjectionRefreshResult" in results_text
    assert "def rebuild_position_lots_from_trade_events(repo: Any) -> ProjectionRefreshResult:" in writer_text
    assert "def persist_trade_event_object(repo: Any, event: Any) -> LedgerWriteResult:" in writer_text
    assert "def persist_trade_event(repo: Any, deal: Any) -> LedgerWriteResult:" in writer_text


def test_trade_event_codec_has_dedicated_storage_boundary() -> None:
    codec_text = (REPO_ROOT / "src" / "application" / "ledger" / "event_codec.py").read_text(encoding="utf-8")
    repository_text = "\n".join(
        (
            REPO_ROOT / "src" / "application" / "ledger" / filename
        ).read_text(encoding="utf-8")
        for filename in (
            "repository_trade_events.py",
            "repository_trade_schema.py",
        )
    )
    publisher_text = (REPO_ROOT / "src" / "application" / "ledger" / "publisher.py").read_text(encoding="utf-8")

    assert "def encode_trade_event_for_storage(" in codec_text
    assert "def import_stored_trade_events(" in codec_text
    assert "encode_trade_event_for_storage" in repository_text
    assert "import_stored_trade_events" in publisher_text


def test_position_target_matching_lives_under_ledger_targets() -> None:
    targets_text = (REPO_ROOT / "src" / "application" / "ledger" / "targets.py").read_text(encoding="utf-8")
    moved_defs = (
        "def assert_position_lot_target_matches_current_state(",
    )
    assert [item for item in moved_defs if item not in targets_text] == []


def test_auto_close_maintenance_lives_under_ledger_maintenance() -> None:
    maintenance_text = (REPO_ROOT / "src" / "application" / "ledger" / "maintenance.py").read_text(encoding="utf-8")
    moved_defs = (
        "def persist_expire_auto_close_event(",
        "def build_expired_close_decisions(",
        "def auto_close_expired_positions(",
    )
    assert [item for item in moved_defs if item not in maintenance_text] == []


def test_manual_trade_writes_live_under_ledger_manual_trades() -> None:
    manual_text = (REPO_ROOT / "src" / "application" / "ledger" / "manual_trades.py").read_text(encoding="utf-8")
    moved_defs = (
        "def _manual_open_event_id(",
        "def _manual_close_event_id(",
        "def existing_manual_close_event_result(",
        "def persist_manual_open_event(",
        "def persist_manual_close_event(",
        "def persist_manual_adjust_event(",
    )
    assert [item for item in moved_defs if item not in manual_text] == []


def test_trade_event_interventions_live_under_ledger_interventions() -> None:
    interventions_text = (REPO_ROOT / "src" / "application" / "ledger" / "interventions.py").read_text(encoding="utf-8")
    moved_defs = (
        "def persist_manual_void_event(",
        "def build_manual_void_preview(",
        "def build_manual_repair_preview(",
        "def persist_manual_repair_event(",
    )
    assert [item for item in moved_defs if item not in interventions_text] == []


def test_position_and_trade_modules_depend_on_ledger_public_api_only() -> None:
    roots = [
        REPO_ROOT / "src" / "application" / "positions",
        REPO_ROOT / "src" / "application" / "trades",
    ]
    offenders: list[str] = []
    for root in roots:
        for path in root.rglob("*.py"):
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                stripped = line.strip()
                if not (
                    stripped.startswith("from src.application.ledger.")
                    or stripped.startswith("import src.application.ledger.")
                ):
                    continue
                if "src.application.ledger.api" in stripped:
                    continue
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}:{stripped}")

    assert offenders == []


def test_non_ledger_runtime_depends_on_ledger_public_api_only() -> None:
    roots = [
        REPO_ROOT / "src" / "application",
        REPO_ROOT / "src" / "interfaces",
        REPO_ROOT / "scripts",
    ]
    offenders: list[str] = []
    for root in roots:
        for path in root.rglob("*.py"):
            if REPO_ROOT / "src" / "application" / "ledger" in path.parents:
                continue
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                stripped = line.strip()
                if not (
                    stripped.startswith("from src.application.ledger.")
                    or stripped.startswith("import src.application.ledger.")
                ):
                    continue
                if "src.application.ledger.api" in stripped:
                    continue
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}:{stripped}")

    assert offenders == []


def test_ledger_public_api_does_not_import_workflow_modules_at_module_load() -> None:
    api_path = REPO_ROOT / "src" / "application" / "ledger" / "api.py"
    tree = ast.parse(api_path.read_text(encoding="utf-8"))
    offenders: list[str] = []
    for node in tree.body:
        if not isinstance(node, ast.ImportFrom):
            continue
        module = str(node.module or "")
        if module.startswith("src.application.positions") or module.startswith("src.application.trades"):
            offenders.append(module)
        if module == "src.application.ledger.read_model":
            offenders.append(module)

    assert offenders == []


def test_ledger_public_api_is_thin_command_query_facade() -> None:
    api_path = REPO_ROOT / "src" / "application" / "ledger" / "api.py"
    tree = ast.parse(api_path.read_text(encoding="utf-8"))
    executable_defs = [
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    ]

    assert (REPO_ROOT / "src" / "application" / "ledger" / "commands.py").exists()
    assert (REPO_ROOT / "src" / "application" / "ledger" / "queries.py").exists()
    assert executable_defs == []


def test_position_risk_context_uses_typed_ledger_view() -> None:
    context_text = (REPO_ROOT / "src" / "application" / "positions" / "context_builder.py").read_text(
        encoding="utf-8"
    )

    assert "RiskPositionView" in context_text
    assert "position_lot_risk_view" in context_text
    assert "position_lot_context_view" not in context_text
    assert "normalize_position_lot_snapshot" not in context_text


def test_core_workflows_use_semantic_ledger_api_names() -> None:
    checked_paths = sorted((REPO_ROOT / "src" / "application" / "positions").glob("*.py"))
    checked_paths.extend(sorted((REPO_ROOT / "src" / "application" / "trades").glob("*.py")))
    banned_exact = {
        "LotCloseSelector",
        "auto_close_expired_positions",
        "build_manual_repair_preview",
        "build_manual_void_preview",
        "build_expired_close_decisions",
        "build_position_lot_view",
        "canonicalize_position_lot_fields",
        "canonicalize_position_lot_record",
        "list_close_lot_candidate_records",
        "list_writable_position_lots",
        "load_canonical_position_lot_records",
        "load_close_candidate_records",
        "load_option_positions_repo",
        "load_position_lot_records",
        "load_projection_verify_state",
        "project_stored_trade_events_to_position_lots",
        "rebuild_position_lots_from_trade_events",
        "require_option_positions_event_write_repo",
        "require_option_positions_read_repo",
        "resolve_fifo_close_lots",
        "resolve_position_lot_records",
        "resolve_position_repo",
        "resolve_position_repo_from_config",
        "resolve_unique_close_lot",
    }
    banned_prefixes = ("persist_", "preflight_")
    offenders: list[str] = []
    for path in checked_paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            if node.module != "src.application.ledger.api":
                continue
            for alias in node.names:
                name = alias.name
                if name in banned_exact or name.startswith(banned_prefixes):
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{name}")

    assert offenders == []


def test_ledger_public_api_exports_semantic_surface_only() -> None:
    api_path = REPO_ROOT / "src" / "application" / "ledger" / "api.py"
    tree = ast.parse(api_path.read_text(encoding="utf-8"))
    exported: set[str] = set()
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(target, ast.Name) and target.id == "__all__" for target in node.targets):
            continue
        if isinstance(node.value, ast.List):
            exported.update(item.value for item in node.value.elts if isinstance(item, ast.Constant) and isinstance(item.value, str))
    banned_exact = {
        "LotCloseSelector",
        "auto_close_expired_positions",
        "build_expired_close_decisions",
        "build_manual_repair_preview",
        "build_manual_void_preview",
        "build_position_lot_view",
        "canonicalize_position_lot_fields",
        "canonicalize_position_lot_record",
        "list_close_lot_candidate_records",
        "list_writable_position_lots",
        "load_canonical_position_lot_records",
        "load_close_candidate_records",
        "load_option_positions_repo",
        "load_position_lot_records",
        "load_projection_verify_state",
        "project_stored_trade_events_to_position_lots",
        "rebuild_position_lots_from_trade_events",
        "require_option_positions_event_write_repo",
        "require_option_positions_read_repo",
        "resolve_fifo_close_lots",
        "resolve_position_lot_records",
        "resolve_position_repo",
        "resolve_position_repo_from_config",
        "resolve_unique_close_lot",
        "supports_ledger_close_preflight",
        "supports_ledger_open_preflight",
    }
    banned_prefixes = ("persist_", "preflight_")
    offenders = sorted(name for name in exported if name in banned_exact or name.startswith(banned_prefixes))
    assert offenders == []


def test_trade_event_projection_mainline_has_no_legacy_passthrough() -> None:
    codec_text = (REPO_ROOT / "src" / "application" / "ledger" / "event_codec.py").read_text(encoding="utf-8")
    publisher_text = (REPO_ROOT / "src" / "application" / "ledger" / "publisher.py").read_text(encoding="utf-8")

    assert "legacy_trade_event_to_ledger_event" not in codec_text
    assert "_encode_legacy_bootstrap_passthrough" not in codec_text
    assert "_bootstrap_passthrough_records" not in publisher_text
    assert "_resolve_legacy_close_targets" not in publisher_text


def test_option_lifecycle_layer_does_not_mutate_position_lots_directly() -> None:
    checked = [
        REPO_ROOT / "src" / "application" / "trades" / "lifecycle.py",
        REPO_ROOT / "src" / "application" / "ledger" / "lifecycle.py",
    ]
    banned_fragments = (
        ".update_record(",
        ".replace_position_lots(",
        "update_position_lot_fields(",
        "replace_position_lots(",
    )
    offenders: list[str] = []
    for path in checked:
        text = path.read_text(encoding="utf-8")
        for fragment in banned_fragments:
            if fragment in text:
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{fragment}")

    ledger_lifecycle_text = (REPO_ROOT / "src" / "application" / "ledger" / "lifecycle.py").read_text(
        encoding="utf-8"
    )
    assert "persist_trade_event_objects_atomically(" in ledger_lifecycle_text
    assert offenders == []
