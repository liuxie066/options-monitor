from __future__ import annotations

import importlib
import pytest


def test_config_management_module_is_removed() -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("src.application.config_management")


def test_cli_uses_owner_modules_for_runtime_config_validation() -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("scripts.validate_config")

    cli_mod = importlib.import_module("src.interfaces.cli.main")
    app_config_mod = importlib.import_module("src.application.agent_tool_config")
    validate_mod = importlib.import_module("src.application.config_validator")

    assert cli_mod.load_runtime_config is app_config_mod.load_runtime_config
    assert cli_mod.validate_config is validate_mod.validate_config


def test_agent_plugin_tools_imports_owner_modules() -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("scripts.config_loader")
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("scripts.account_config")
    for old_module in (
        "scripts.agent_plugin",
        "scripts.agent_plugin.config",
        "scripts.agent_plugin.contracts",
        "scripts.agent_plugin.init_local",
        "scripts.agent_plugin.main",
        "scripts.agent_plugin.tools",
    ):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(old_module)

    app_tools_mod = importlib.import_module("src.application.agent_tool_handlers")
    app_config_mod = importlib.import_module("src.application.agent_tool_config")
    app_contracts_mod = importlib.import_module("src.application.agent_tool_contracts")
    config_loader_mod = importlib.import_module("src.application.config_loader")
    validate_mod = importlib.import_module("src.application.config_validator")

    assert app_tools_mod.write_tools_enabled is app_config_mod.write_tools_enabled
    assert app_tools_mod.resolve_output_root is app_config_mod.resolve_output_root
    assert app_tools_mod.repo_base is app_config_mod.repo_base
    assert app_tools_mod.load_runtime_config is app_config_mod.load_runtime_config
    assert app_tools_mod.load_runtime_pipeline_config is config_loader_mod.load_config
    assert app_tools_mod.resolve_watchlist_config is config_loader_mod.resolve_watchlist_config
    assert app_tools_mod.validate_config is validate_mod.validate_config
    assert app_contracts_mod.AgentToolError.__name__ == "AgentToolError"


def test_account_run_imports_watchlist_helpers_from_owner_module() -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("scripts.watchlist")

    account_run_mod = importlib.import_module("src.application.account_run")
    watchlist_cli_mod = importlib.import_module("src.interfaces.cli.watchlist")
    owner_mod = importlib.import_module("src.application.config_loader")

    assert account_run_mod.resolve_watchlist_config is owner_mod.resolve_watchlist_config
    assert account_run_mod.set_watchlist_config is owner_mod.set_watchlist_config
    assert watchlist_cli_mod.resolve_watchlist_config is owner_mod.resolve_watchlist_config
    assert watchlist_cli_mod.set_watchlist_config is owner_mod.set_watchlist_config


def test_pipeline_runtime_imports_config_loader_helpers_from_owner_module() -> None:
    pipeline_runtime_mod = importlib.import_module("src.application.pipeline_runtime")
    owner_mod = importlib.import_module("src.application.config_loader")

    assert pipeline_runtime_mod.load_runtime_pipeline_config is owner_mod.load_config
    assert pipeline_runtime_mod.resolve_data_config_path is owner_mod.resolve_data_config_path
    assert pipeline_runtime_mod.resolve_watchlist_config is owner_mod.resolve_watchlist_config


def test_pipeline_watchlist_imports_config_loader_helpers_from_owner_module() -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("scripts.pipeline_watchlist")

    pipeline_watchlist_mod = importlib.import_module("src.application.pipeline_watchlist")
    owner_mod = importlib.import_module("src.application.config_loader")

    assert pipeline_watchlist_mod.resolve_templates_config is owner_mod.resolve_templates_config
    assert pipeline_watchlist_mod.resolve_watchlist_config is owner_mod.resolve_watchlist_config


def test_pipeline_orchestration_helpers_use_application_owner_modules() -> None:
    for old_module in ("scripts.pipeline_runner", "scripts.pipeline_postprocess"):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(old_module)

    runner_mod = importlib.import_module("src.application.pipeline_runner")
    postprocess_mod = importlib.import_module("src.application.pipeline_postprocess")

    assert runner_mod.build_stage_plan(stage="all", stage_only=None).want("notify") is True
    assert postprocess_mod.PostprocessResult.__name__ == "PostprocessResult"


def test_pipeline_symbol_stack_imports_owner_modules() -> None:
    for old_module in (
        "scripts.pipeline_symbol",
        "scripts.exchange_rate_loader",
        "scripts.prefilters",
        "scripts.multiplier_steps",
        "scripts.required_data_steps",
        "scripts.sell_call_steps",
        "scripts.sell_put_steps",
        "scripts.pipeline_fetch_models",
        "scripts.pipeline_steps",
        "scripts.sell_put_cash",
        "scripts.scan_sell_put",
        "scripts.scan_sell_call",
        "scripts.render_sell_put_alerts",
        "scripts.render_sell_call_alerts",
        "scripts.render_yield_enhancement_alerts",
        "scripts.report_labels",
        "scripts.report_summaries",
        "scripts.sell_put_call_helper",
        "scripts.event_risk_filter",
        "scripts.sell_call_risk_bands",
        "scripts.sell_put_risk_bands",
    ):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(old_module)

    pipeline_symbol_mod = importlib.import_module("src.application.pipeline_symbol")
    required_steps_mod = importlib.import_module("src.application.required_data_steps")
    prefilters_mod = importlib.import_module("src.application.prefilters")
    sell_put_steps_mod = importlib.import_module("src.application.sell_put_steps")
    sell_call_steps_mod = importlib.import_module("src.application.sell_call_steps")
    sell_put_mod = importlib.import_module("src.application.scan_sell_put")
    sell_call_mod = importlib.import_module("src.application.scan_sell_call")
    event_risk_mod = importlib.import_module("src.application.event_risk_filter")
    put_risk_mod = importlib.import_module("domain.domain.sell_put_risk_bands")
    call_risk_mod = importlib.import_module("domain.domain.sell_call_risk_bands")

    assert pipeline_symbol_mod.ensure_required_data is required_steps_mod.ensure_required_data
    assert pipeline_symbol_mod.apply_prefilters is prefilters_mod.apply_prefilters
    assert sell_put_steps_mod.run_sell_put_scan is sell_put_mod.run_sell_put_scan
    assert sell_call_steps_mod.run_sell_call_scan is sell_call_mod.run_sell_call_scan
    assert sell_put_mod.annotate_candidates_with_event_risk is event_risk_mod.annotate_candidates_with_event_risk
    assert sell_call_mod.classify_sell_call_risk is call_risk_mod.classify_sell_call_risk
    assert importlib.import_module("src.application.report_labels").classify_sell_put_risk is put_risk_mod.classify_sell_put_risk


def test_strategy_and_spot_fallback_legacy_script_owners_are_removed() -> None:
    for old_module in ("scripts.option_candidate_strategy", "scripts.pm_bridge"):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(old_module)

    strategy_mod = importlib.import_module("domain.domain.engine.candidate_strategy")
    opend_mod = importlib.import_module("src.application.opend_symbol_fetching")

    assert callable(strategy_mod.build_strategy_config)
    assert callable(opend_mod.fetch_symbol)


def test_required_data_uses_application_opend_symbol_fetching_owner() -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("scripts.fetch_market_data_opend")

    required_data_mod = importlib.import_module("src.application.required_data_fetching")
    planning_mod = importlib.import_module("src.application.required_data_planning")
    owner_mod = importlib.import_module("src.application.opend_symbol_fetching")
    output_mod = importlib.import_module("src.application.opend_symbol_outputs")
    snapshot_mod = importlib.import_module("src.application.opend_market_snapshot_fetching")
    chain_mod = importlib.import_module("src.application.opend_symbol_chain_fetching")

    assert required_data_mod.FetchSymbolRequest is owner_mod.FetchSymbolRequest
    assert required_data_mod.fetch_symbol_request is owner_mod.fetch_symbol_request
    assert required_data_mod.save_outputs is output_mod.save_outputs
    assert planning_mod.get_underlier_spot is snapshot_mod.get_underlier_spot
    assert planning_mod.list_option_expirations is chain_mod.list_option_expirations


def test_option_positions_and_pipeline_context_import_data_config_owner_module() -> None:
    read_model_mod = importlib.import_module("src.application.ledger.read_model")
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("scripts.pipeline_context")

    pipeline_context_mod = importlib.import_module("src.application.pipeline_context")
    owner_mod = importlib.import_module("src.application.config_loader")

    assert read_model_mod.resolve_data_config_path is owner_mod.resolve_data_config_path
    assert pipeline_context_mod.resolve_data_config_path is owner_mod.resolve_data_config_path


def test_option_positions_inspection_imports_application_owner_module() -> None:
    owner_mod = importlib.import_module("src.application.positions.inspection")
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("src.application.option_positions_inspection")
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("scripts.option_positions")
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("scripts.backfill_option_positions_broker")
    cli_mod = importlib.import_module("src.interfaces.cli.option_positions")
    agent_tools_mod = importlib.import_module("src.application.agent_tool_handlers")

    assert cli_mod.build_lot_event_history is owner_mod.build_lot_event_history
    assert cli_mod.inspect_projection_state is owner_mod.inspect_projection_state
    assert agent_tools_mod.build_lot_event_history is owner_mod.build_lot_event_history
    assert agent_tools_mod.inspect_projection_state is owner_mod.inspect_projection_state


def test_portfolio_context_and_cash_query_import_application_owner_modules() -> None:
    for old_module in (
        "src.application.option_positions_context_builder",
        "scripts.query_sell_put_cash",
        "scripts.fetch_option_positions_context",
        "scripts.fetch_portfolio_context",
        "scripts.portfolio_context_service",
        "scripts.futu_portfolio_context",
    ):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(old_module)

    cash_mod = importlib.import_module("src.application.cash_headroom_query")
    option_ctx_mod = importlib.import_module("src.application.positions.context_builder")
    portfolio_ctx_mod = importlib.import_module("src.application.portfolio_context_builder")
    portfolio_service_mod = importlib.import_module("src.application.portfolio_context_service")
    futu_portfolio_mod = importlib.import_module("src.application.futu_portfolio_context")
    cli_mod = importlib.import_module("src.interfaces.cli.main")
    multi_cash_mod = importlib.import_module("src.application.multi_tick.cash_footer")
    agent_tools_mod = importlib.import_module("src.application.agent_tool_handlers")
    pipeline_context_mod = importlib.import_module("src.application.pipeline_context")

    assert cash_mod.build_option_positions_context is option_ctx_mod.build_context
    assert portfolio_service_mod.load_holdings_portfolio_context is portfolio_ctx_mod.load_holdings_portfolio_context
    assert cli_mod.query_sell_put_cash is cash_mod.query_sell_put_cash
    assert multi_cash_mod.query_sell_put_cash is cash_mod.query_sell_put_cash
    assert agent_tools_mod.query_sell_put_cash is cash_mod.query_sell_put_cash
    assert agent_tools_mod.infer_futu_portfolio_settings is futu_portfolio_mod.infer_futu_portfolio_settings
    assert pipeline_context_mod.fetch_futu_portfolio_context is futu_portfolio_mod.fetch_futu_portfolio_context
    assert pipeline_context_mod.load_account_portfolio_context is portfolio_service_mod.load_account_portfolio_context


def test_feishu_bitable_imports_infrastructure_owner_module() -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("scripts.feishu_bitable")

    infra_mod = importlib.import_module("src.infrastructure.feishu_bitable")
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("src.application.option_positions_reporting")
    bootstrap_mod = importlib.import_module("src.application.ledger.bootstrap")
    reporting_mod = importlib.import_module("src.application.positions.reporting")
    read_model_mod = importlib.import_module("src.application.ledger.read_model")
    healthcheck_mod = importlib.import_module("src.application.healthcheck_runner")

    assert bootstrap_mod.bitable_list_records is infra_mod.bitable_list_records
    assert bootstrap_mod.get_tenant_access_token is infra_mod.get_tenant_access_token
    assert reporting_mod.safe_float is infra_mod.safe_float
    assert read_model_mod.parse_note_kv is infra_mod.parse_note_kv
    assert healthcheck_mod.get_tenant_access_token is infra_mod.get_tenant_access_token


def test_exchange_rates_imports_infrastructure_owner_module() -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("scripts.exchange_rates")

    infra_mod = importlib.import_module("src.infrastructure.exchange_rates")
    read_model_mod = importlib.import_module("src.application.ledger.read_model")
    reporting_mod = importlib.import_module("src.application.positions.reporting")
    agent_tools_mod = importlib.import_module("src.application.agent_tool_handlers")
    cash_mod = importlib.import_module("src.application.sell_put_cash")
    notify_mod = importlib.import_module("src.application.notify_symbols")

    assert read_model_mod.get_exchange_rates_or_fetch_latest is infra_mod.get_exchange_rates_or_fetch_latest
    assert reporting_mod.CurrencyConverter is infra_mod.CurrencyConverter
    assert agent_tools_mod._get_cached_exchange_rates_impl is infra_mod.get_cached_exchange_rates
    assert cash_mod.CurrencyConverter is infra_mod.CurrencyConverter
    assert notify_mod.load_exchange_rate_info is infra_mod.load_exchange_rate_info


def test_multiplier_cache_imports_infrastructure_owner_module() -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("scripts.multiplier_cache")
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("scripts.parse_option_message")
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("scripts.option_intake")

    infra_mod = importlib.import_module("src.infrastructure.multiplier_cache")
    reporting_mod = importlib.import_module("src.application.positions.reporting")
    trade_normalizer_mod = importlib.import_module("src.application.trades.normalizer")
    parse_message_mod = importlib.import_module("src.application.parse_option_message")

    assert reporting_mod.resolve_multiplier is infra_mod.resolve_multiplier
    assert trade_normalizer_mod.resolve_multiplier_with_source_and_diagnostics is infra_mod.resolve_multiplier_with_source_and_diagnostics
    assert parse_message_mod.resolve_multiplier_with_source is infra_mod.resolve_multiplier_with_source


def test_option_positions_feishu_sync_imports_application_owner_module() -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("scripts.sync_option_positions_to_feishu")
    for old_module in (
        "src.application.option_positions_feishu_sync",
        "src.application.option_positions_feishu_sync_receipt",
        "src.application.option_positions_sync_config",
    ):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(old_module)

    owner_mod = importlib.import_module("src.application.positions.feishu_sync")
    workflows_mod = importlib.import_module("src.application.positions.workflows")

    assert workflows_mod.sync_single_option_position_record is owner_mod.sync_single_option_position_record


def test_trade_intake_imports_owner_modules() -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("scripts.trade_account_identity")
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("scripts.trade_account_mapping")
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("scripts.trade_event_normalizer")
    for old_module in (
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
        "scripts.auto_trade_intake",
        "scripts.futu_trade_detail_lookup",
        "scripts.trade_intake_resolver",
        "scripts.trade_intake_state",
        "scripts.trade_push_listener",
    ):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(old_module)

    identity_mod = importlib.import_module("domain.domain.trade_account_identity")
    mapping_mod = importlib.import_module("src.application.trades.account_mapping")
    normalizer_mod = importlib.import_module("src.application.trades.normalizer")
    auto_trade_mod = importlib.import_module("src.application.trades.auto_intake")
    lookup_mod = importlib.import_module("src.application.trades.futu_detail_lookup")
    resolver_mod = importlib.import_module("src.application.trades.resolver")
    workflows_mod = importlib.import_module("src.application.trades.workflows")
    validate_mod = importlib.import_module("src.application.config_validator")

    assert lookup_mod.extract_primary_account_id is identity_mod.extract_primary_account_id
    assert validate_mod.resolve_trade_intake_config is mapping_mod.resolve_trade_intake_config
    assert auto_trade_mod.resolve_trade_intake_config is mapping_mod.resolve_trade_intake_config
    assert auto_trade_mod.normalize_trade_deal is normalizer_mod.normalize_trade_deal
    assert resolver_mod.NormalizedTradeDeal is normalizer_mod.NormalizedTradeDeal
    assert workflows_mod.NormalizedTradeDeal is normalizer_mod.NormalizedTradeDeal


def test_healthcheck_and_init_local_import_validate_config_from_owner_module() -> None:
    healthcheck_mod = importlib.import_module("src.application.healthcheck_runner")
    app_init_local_mod = importlib.import_module("src.application.agent_tool_init_local")
    validate_mod = importlib.import_module("src.application.config_validator")

    assert healthcheck_mod.validate_config is validate_mod.validate_config
    assert app_init_local_mod.validate_config is validate_mod.validate_config


def test_account_config_imports_owner_module() -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("scripts.account_config")

    owner_mod = importlib.import_module("src.application.account_config")
    layered_mod = importlib.import_module("src.application.layered_config")
    multi_tick_mod = importlib.import_module("src.application.multi_account_tick")
    webui_server_mod = importlib.import_module("src.interfaces.webui.server")

    assert layered_mod.normalize_accounts is owner_mod.normalize_accounts
    assert multi_tick_mod.accounts_from_config is owner_mod.accounts_from_config
    assert webui_server_mod.list_account_config_views is owner_mod.list_account_config_views


def test_shared_infrastructure_imports_owner_modules() -> None:
    for old_module in (
        "scripts.io_utils",
        "scripts.run_log",
        "scripts.subprocess_utils",
        "scripts.logging_config",
    ):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(old_module)

    io_mod = importlib.import_module("src.infrastructure.io_utils")
    run_log_mod = importlib.import_module("src.infrastructure.run_log")
    subprocess_mod = importlib.import_module("src.infrastructure.subprocess_utils")
    logging_mod = importlib.import_module("src.infrastructure.logging_config")
    multi_tick_mod = importlib.import_module("src.application.multi_account_tick")
    pipeline_runtime_mod = importlib.import_module("src.application.pipeline_runtime")
    account_run_mod = importlib.import_module("src.application.account_run")

    assert multi_tick_mod.RunLogger is run_log_mod.RunLogger
    assert multi_tick_mod.read_json is io_mod.read_json
    assert account_run_mod.utc_now is io_mod.utc_now
    assert pipeline_runtime_mod.run_cmd is subprocess_mod.run_cmd
    assert pipeline_runtime_mod.get_logger is logging_mod.get_logger


def test_external_services_imports_infrastructure_owner_module() -> None:
    for old_module in (
        "scripts.infra.service",
        "scripts.infra.entry_external",
    ):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(old_module)

    owner_mod = importlib.import_module("src.infrastructure.external_services")
    multi_tick_mod = importlib.import_module("src.application.multi_account_tick")
    account_run_mod = importlib.import_module("src.application.account_run")
    webui_server_mod = importlib.import_module("src.interfaces.webui.server")

    assert multi_tick_mod.run_scan_scheduler_cli is owner_mod.run_scan_scheduler_cli
    assert multi_tick_mod.select_notification_delivery_adapter is owner_mod.select_notification_delivery_adapter
    assert account_run_mod.run_pipeline_script is owner_mod.run_pipeline_script
    assert webui_server_mod.send_openclaw_message is owner_mod.send_openclaw_message


def test_multi_tick_helpers_import_application_owner_modules() -> None:
    for old_module in (
        "scripts.multi_tick",
        "scripts.multi_tick.main",
        "scripts.multi_tick.misc",
        "scripts.multi_tick.notify_format",
        "scripts.multi_tick.cash_footer",
        "scripts.multi_tick.opend_guard",
        "scripts.multi_tick.project_guard",
        "scripts.multi_tick.required_data_prefetch",
        # Locks removed `scripts.domain.storage` try/except fallback.
        "scripts.domain",
        "scripts.domain.storage",
        "scripts.domain.storage.paths",
        "scripts.domain.storage.repositories",
    ):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(old_module)

    misc_mod = importlib.import_module("src.application.multi_tick.misc")
    notify_mod = importlib.import_module("src.application.multi_tick.notify_format")
    cash_footer_mod = importlib.import_module("src.application.multi_tick.cash_footer")
    opend_guard_mod = importlib.import_module("src.application.multi_tick.opend_guard")
    project_guard_mod = importlib.import_module("src.application.multi_tick.project_guard")
    prefetch_mod = importlib.import_module("src.application.multi_tick.required_data_prefetch")
    multi_tick_mod = importlib.import_module("src.application.multi_account_tick")
    account_run_mod = importlib.import_module("src.application.account_run")

    assert multi_tick_mod.AccountResult is misc_mod.AccountResult
    assert multi_tick_mod.build_account_message is notify_mod.build_account_message
    assert multi_tick_mod.query_cash_footer is cash_footer_mod.query_cash_footer
    assert multi_tick_mod.send_opend_alert is opend_guard_mod.send_opend_alert
    assert multi_tick_mod.admit_project_run is project_guard_mod.admit_project_run
    assert account_run_mod.prefetch_required_data is prefetch_mod.prefetch_required_data


def test_close_advice_imports_application_owner_module() -> None:
    for old_module in (
        "scripts.close_advice",
        "scripts.close_advice.runner",
        "scripts.close_advice.main",
    ):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(old_module)

    owner_mod = importlib.import_module("src.application.close_advice_runner")
    account_run_mod = importlib.import_module("src.application.account_run")
    agent_tools_mod = importlib.import_module("src.application.agent_tool_handlers")

    assert account_run_mod.run_close_advice is owner_mod.run_close_advice
    assert agent_tools_mod.run_close_advice is owner_mod.run_close_advice


def test_fee_calc_imports_domain_owner_module() -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("scripts.fee_calc")

    owner_mod = importlib.import_module("domain.domain.fee_calc")
    close_advice_mod = importlib.import_module("src.application.close_advice_runner")
    sell_put_mod = importlib.import_module("src.application.scan_sell_put")
    sell_call_mod = importlib.import_module("src.application.scan_sell_call")

    assert close_advice_mod.calc_futu_option_fee is owner_mod.calc_futu_option_fee
    assert sell_put_mod.calc_futu_option_fee is owner_mod.calc_futu_option_fee
    assert sell_call_mod.calc_futu_option_fee is owner_mod.calc_futu_option_fee


def test_opend_support_imports_owner_modules() -> None:
    for old_module in (
        "scripts.futu_gateway",
        "scripts.opend_utils",
        "scripts.opend_normalize",
        "scripts.required_data_validate",
        "scripts.candidate_defaults",
    ):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(old_module)

    gateway_mod = importlib.import_module("src.infrastructure.futu_gateway")
    opend_utils_mod = importlib.import_module("src.application.opend_utils")
    opend_normalize_mod = importlib.import_module("src.application.opend_normalize")
    required_validation_mod = importlib.import_module("src.application.required_data_validation")
    candidate_defaults_mod = importlib.import_module("domain.domain.candidate_defaults")
    fetching_mod = importlib.import_module("src.application.opend_symbol_fetching")
    planning_mod = importlib.import_module("src.application.required_data_planning")
    sell_put_mod = importlib.import_module("src.application.sell_put_steps")

    assert fetching_mod.build_ready_futu_gateway is gateway_mod.build_ready_futu_gateway
    assert fetching_mod.normalize_underlier is opend_utils_mod.normalize_underlier
    assert opend_normalize_mod.normalize_iv(25) == 0.25
    assert required_validation_mod.validate_required_rows([])[1].total_rows == 0
    assert planning_mod.DEFAULT_SELL_PUT_WINDOW is candidate_defaults_mod.DEFAULT_SELL_PUT_WINDOW
    assert sell_put_mod.resolve_candidate_window is candidate_defaults_mod.resolve_candidate_window


def test_report_builders_import_owner_modules() -> None:
    for old_module in (
        "scripts.report_builders",
        "scripts.summary_formatting",
        "scripts.report_formatting",
    ):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(old_module)

    report_builders_mod = importlib.import_module("src.application.report_builders")
    summary_formatting_mod = importlib.import_module("src.application.summary_formatting")
    report_formatting_mod = importlib.import_module("src.application.report_formatting")
    pipeline_runtime_mod = importlib.import_module("src.application.pipeline_runtime")

    assert report_builders_mod.format_summary_row is summary_formatting_mod.format_summary_row
    assert summary_formatting_mod.pct is report_formatting_mod.pct
    assert pipeline_runtime_mod.build_symbols_summary is report_builders_mod.build_symbols_summary


def test_alert_policy_imports_domain_owner_modules() -> None:
    for old_module in ("scripts.alert_policy", "scripts.alert_rules"):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(old_module)

    policy_mod = importlib.import_module("domain.domain.alert_policy")
    rules_mod = importlib.import_module("domain.domain.alert_rules")
    alert_engine_mod = importlib.import_module("src.application.alert_engine")
    notify_mod = importlib.import_module("src.application.notify_symbols")

    assert rules_mod.DEFAULT_ALERT_POLICY is policy_mod.DEFAULT_ALERT_POLICY
    assert alert_engine_mod.load_alert_policy is policy_mod.load_alert_policy
    assert notify_mod.SELL_PUT_NOTIFICATION_HIGH == rules_mod.SELL_PUT_NOTIFICATION_HIGH


def test_notification_modules_import_application_owner_modules() -> None:
    for old_module in ("scripts.alert_engine", "scripts.notify_symbols"):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(old_module)

    alert_engine_mod = importlib.import_module("src.application.alert_engine")
    notify_mod = importlib.import_module("src.application.notify_symbols")
    pipeline_reporting_mod = importlib.import_module("src.application.pipeline_reporting")
    agent_tools_mod = importlib.import_module("src.application.agent_tool_handlers")

    assert pipeline_reporting_mod.run_alert_engine is alert_engine_mod.run_alert_engine
    assert pipeline_reporting_mod.build_notification is notify_mod.build_notification
    assert agent_tools_mod.build_notification is notify_mod.build_notification


def test_scan_scheduler_imports_application_owner_module() -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("scripts.scan_scheduler")

    owner_mod = importlib.import_module("src.application.scan_scheduler")
    cli_mod = importlib.import_module("src.interfaces.cli.main")
    agent_tools_mod = importlib.import_module("src.application.agent_tool_handlers")
    healthcheck_mod = importlib.import_module("src.application.healthcheck_runner")

    assert cli_mod.run_scheduler is owner_mod.run_scheduler
    assert agent_tools_mod.scheduler_decide is owner_mod.decide
    assert agent_tools_mod.read_scheduler_state is owner_mod.read_state
    assert healthcheck_mod.run_scheduler is owner_mod.run_scheduler


def test_cash_secured_utils_imports_domain_owner_module() -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("scripts.cash_secured_utils")

    owner_mod = importlib.import_module("domain.domain.cash_secured_utils")
    query_mod = importlib.import_module("src.application.cash_headroom_query")
    sell_put_cash_mod = importlib.import_module("src.application.sell_put_cash")

    assert query_mod.normalize_cash_secured_by_symbol_by_ccy is owner_mod.normalize_cash_secured_by_symbol_by_ccy
    assert sell_put_cash_mod.cash_secured_symbol_cny is owner_mod.cash_secured_symbol_cny
