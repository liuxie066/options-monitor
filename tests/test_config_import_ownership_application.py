from __future__ import annotations

import importlib
import pytest


def test_config_management_module_is_removed() -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("src.application.config_management")


def test_cli_uses_owner_modules_for_runtime_config_validation() -> None:
    cli_mod = importlib.import_module("src.interfaces.cli.main")
    agent_config_mod = importlib.import_module("scripts.agent_plugin.config")
    validate_mod = importlib.import_module("scripts.validate_config")

    assert cli_mod.load_runtime_config is agent_config_mod.load_runtime_config
    assert cli_mod.validate_config is validate_mod.validate_config


def test_agent_plugin_tools_imports_owner_modules() -> None:
    tools_mod = importlib.import_module("scripts.agent_plugin.tools")
    app_tools_mod = importlib.import_module("src.application.agent_tool_handlers")
    agent_config_mod = importlib.import_module("scripts.agent_plugin.config")
    contracts_mod = importlib.import_module("scripts.agent_plugin.contracts")
    app_contracts_mod = importlib.import_module("src.application.agent_tool_contracts")
    config_loader_mod = importlib.import_module("scripts.config_loader")
    validate_mod = importlib.import_module("scripts.validate_config")

    assert tools_mod is app_tools_mod
    assert tools_mod.write_tools_enabled is agent_config_mod.write_tools_enabled
    assert tools_mod.resolve_output_root is agent_config_mod.resolve_output_root
    assert tools_mod.repo_base is agent_config_mod.repo_base
    assert tools_mod.load_runtime_config is agent_config_mod.load_runtime_config
    assert tools_mod.load_runtime_pipeline_config is config_loader_mod.load_config
    assert tools_mod.resolve_watchlist_config is config_loader_mod.resolve_watchlist_config
    assert tools_mod.validate_config is validate_mod.validate_config
    assert contracts_mod.AgentToolError is app_contracts_mod.AgentToolError


def test_account_run_imports_watchlist_helpers_from_owner_module() -> None:
    account_run_mod = importlib.import_module("src.application.account_run")
    owner_mod = importlib.import_module("scripts.config_loader")

    assert account_run_mod.resolve_watchlist_config is owner_mod.resolve_watchlist_config
    assert account_run_mod.set_watchlist_config is owner_mod.set_watchlist_config


def test_pipeline_runtime_imports_config_loader_helpers_from_owner_module() -> None:
    pipeline_runtime_mod = importlib.import_module("src.application.pipeline_runtime")
    owner_mod = importlib.import_module("scripts.config_loader")

    assert pipeline_runtime_mod.load_runtime_pipeline_config is owner_mod.load_config
    assert pipeline_runtime_mod.resolve_data_config_path is owner_mod.resolve_data_config_path
    assert pipeline_runtime_mod.resolve_watchlist_config is owner_mod.resolve_watchlist_config


def test_pipeline_watchlist_imports_config_loader_helpers_from_owner_module() -> None:
    pipeline_watchlist_mod = importlib.import_module("scripts.pipeline_watchlist")
    owner_mod = importlib.import_module("scripts.config_loader")

    assert pipeline_watchlist_mod.resolve_templates_config is owner_mod.resolve_templates_config
    assert pipeline_watchlist_mod.resolve_watchlist_config is owner_mod.resolve_watchlist_config


def test_required_data_uses_application_opend_symbol_fetching_owner() -> None:
    required_data_mod = importlib.import_module("src.application.required_data_fetching")
    planning_mod = importlib.import_module("src.application.required_data_planning")
    owner_mod = importlib.import_module("src.application.opend_symbol_fetching")

    assert required_data_mod.FetchSymbolRequest is owner_mod.FetchSymbolRequest
    assert required_data_mod.fetch_symbol_request is owner_mod.fetch_symbol_request
    assert required_data_mod.save_outputs is owner_mod.save_outputs
    assert planning_mod.get_underlier_spot is owner_mod.get_underlier_spot
    assert planning_mod.list_option_expirations is owner_mod.list_option_expirations


def test_option_positions_and_pipeline_context_import_data_config_owner_module() -> None:
    option_positions_mod = importlib.import_module("src.application.option_positions_facade")
    pipeline_context_mod = importlib.import_module("scripts.pipeline_context")
    owner_mod = importlib.import_module("scripts.config_loader")

    assert option_positions_mod.resolve_data_config_path is owner_mod.resolve_data_config_path
    assert pipeline_context_mod.resolve_data_config_path is owner_mod.resolve_data_config_path


def test_healthcheck_and_init_local_import_validate_config_from_owner_module() -> None:
    healthcheck_mod = importlib.import_module("scripts.healthcheck")
    init_local_mod = importlib.import_module("scripts.agent_plugin.init_local")
    validate_mod = importlib.import_module("scripts.validate_config")

    assert healthcheck_mod.validate_config is validate_mod.validate_config
    assert init_local_mod.validate_config is validate_mod.validate_config
