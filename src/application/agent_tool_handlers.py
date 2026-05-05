from __future__ import annotations

import json
from typing import Any

from copy import deepcopy

from scripts.account_config import accounts_from_config, list_account_config_views, normalize_accounts
from src.application.agent_tool_config import load_runtime_config, repo_base, resolve_output_root, write_tools_enabled
from src.application.agent_tool_contracts import AgentToolError, mask_path
from scripts.close_advice import run_close_advice
from scripts.config_loader import load_config as load_runtime_pipeline_config, resolve_watchlist_config
from scripts.validate_config import validate_config
from domain.domain.fetch_source import resolve_symbol_fetch_source
from scripts.futu_portfolio_context import infer_futu_portfolio_settings
from scripts.notify_symbols import build_notification
from scripts.option_positions import build_lot_event_history, inspect_projection_state
from scripts.option_positions_core.domain import normalize_account as _normalize_account
from scripts.option_positions_core.service import load_option_positions_repo
from scripts.option_positions_core.reporting import build_monthly_income_report
from scripts.pipeline_context import load_option_positions_context, load_portfolio_context
from scripts.query_sell_put_cash import query_sell_put_cash
from scripts.scan_scheduler import decide as scheduler_decide, read_state as read_scheduler_state
from scripts.exchange_rates import get_cached_exchange_rates as _get_cached_exchange_rates_impl
from scripts.io_utils import safe_read_csv
from src.application.option_positions_facade import resolve_option_positions_repo
from src.application.option_positions_facade import list_position_rows as _list_position_rows
from src.application.agent_tool_healthcheck import run_healthcheck_tool
from src.application.agent_tool_notifications import preview_notification_tool
from src.application.agent_tool_openclaw import (
    openclaw_readiness_tool,
    runtime_status_tool,
)
from src.application.agent_tool_operations import (
    config_validate_tool,
    option_positions_read_tool,
    scheduler_status_tool,
    version_check_tool,
)
from src.application.agent_tool_runtime import (
    as_float as _as_float,
    extract_context_symbols as _extract_context_symbols,
    healthcheck_symbols_for_futu as _healthcheck_symbols_for_futu_impl,
    mask_account_id as _mask_account_id_impl,
    normalize_broker as _normalize_broker,
    read_json_object_or_empty as _read_json_object_or_empty_impl,
    resolve_data_config_ref as _resolve_data_config_ref,
    resolve_local_path as _resolve_local_path_impl,
    resolve_public_data_config_path as _resolve_public_data_config_path_impl,
    run_futu_doctor as _run_futu_doctor_impl,
    symbol_fetch_config_map as _symbol_fetch_config_map,
    validate_runtime_config as _validate_runtime_config_impl,
    write_json_atomic as _write_json_atomic,
)
from src.application.agent_tool_scan import (
    close_advice_rows_summary as _close_advice_rows_summary,
    close_advice_tool,
    get_close_advice_tool,
    get_portfolio_context_tool,
    monthly_income_report_tool,
    prepare_close_advice_inputs_tool,
    query_cash_headroom_tool,
    scan_opportunities_tool,
    scan_summary_rows as _scan_summary_rows,
)
from src.application.agent_tool_symbols import (
    apply_symbol_mutation as _apply_symbol_mutation,
    find_symbol_entry as _find_symbol_entry,
    list_symbol_rows as _list_symbol_rows,
    manage_symbols_tool,
    set_path as _set_path,
)
from src.application.version_check import check_version_update


def fetch_symbol_opend(*args: Any, **kwargs: Any) -> Any:
    from scripts.fetch_market_data_opend import fetch_symbol as _fetch_symbol_opend

    return _fetch_symbol_opend(*args, **kwargs)


def save_required_data_opend(*args: Any, **kwargs: Any) -> Any:
    from scripts.fetch_market_data_opend import save_outputs as _save_required_data_opend

    return _save_required_data_opend(*args, **kwargs)


def _validate_runtime_config(cfg: dict[str, Any], *, allow_empty_symbols: bool = False) -> list[str]:
    return _validate_runtime_config_impl(
        cfg,
        allow_empty_symbols=allow_empty_symbols,
        resolve_watchlist_config=resolve_watchlist_config,
        validate_config=validate_config,
    )


def _resolve_public_data_config_path(payload: dict[str, Any], portfolio_cfg: dict[str, Any]):
    return _resolve_public_data_config_path_impl(payload, portfolio_cfg, repo_base=repo_base)


def _resolve_local_path(value: Any, *, default):
    return _resolve_local_path_impl(value, default=default, repo_base=repo_base)


def _read_json_object_or_empty(path):
    return _read_json_object_or_empty_impl(path)


def _mask_account_id(value: Any) -> str:
    return _mask_account_id_impl(value)


def _run_futu_doctor(*, host: str, port: int, symbols: list[str], timeout_sec: int) -> dict[str, Any]:
    return _run_futu_doctor_impl(host=host, port=port, symbols=symbols, timeout_sec=timeout_sec, repo_base=repo_base)


def _healthcheck_symbols_for_futu(cfg: dict[str, Any]) -> list[str]:
    return _healthcheck_symbols_for_futu_impl(cfg, resolve_watchlist_config=resolve_watchlist_config)


def _healthcheck_tool(payload: dict[str, Any]) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    return run_healthcheck_tool(
        payload,
        load_runtime_config=load_runtime_config,
        validate_runtime_config=_validate_runtime_config,
        normalize_accounts=normalize_accounts,
        accounts_from_config=accounts_from_config,
        resolve_data_config_ref=_resolve_data_config_ref,
        resolve_public_data_config_path=_resolve_public_data_config_path,
        read_json_object_or_empty=_read_json_object_or_empty,
        mask_path=mask_path,
        list_account_config_views=list_account_config_views,
        mask_account_id=_mask_account_id,
        infer_futu_portfolio_settings=infer_futu_portfolio_settings,
        load_option_positions_repo=load_option_positions_repo,
        run_futu_doctor=_run_futu_doctor,
        healthcheck_symbols_for_futu=_healthcheck_symbols_for_futu,
        write_tools_enabled=write_tools_enabled,
    )


def _version_check_tool(payload: dict[str, Any]) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    return version_check_tool(
        payload,
        check_version_update=check_version_update,
        repo_base=repo_base,
        mask_path=mask_path,
    )


def _config_validate_tool(payload: dict[str, Any]) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    return config_validate_tool(
        payload,
        load_runtime_config=load_runtime_config,
        validate_runtime_config=_validate_runtime_config,
        accounts_from_config=accounts_from_config,
        resolve_watchlist_config=resolve_watchlist_config,
        mask_path=mask_path,
    )


def _scheduler_status_tool(payload: dict[str, Any]) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    return scheduler_status_tool(
        payload,
        load_runtime_config=load_runtime_config,
        read_state=read_scheduler_state,
        decide=scheduler_decide,
        repo_base=repo_base,
        mask_path=mask_path,
    )


def _query_cash_headroom_tool(payload: dict[str, Any]) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    return query_cash_headroom_tool(
        payload,
        load_runtime_config=load_runtime_config,
        resolve_public_data_config_path=_resolve_public_data_config_path,
        normalize_broker=_normalize_broker,
        resolve_output_root=resolve_output_root,
        query_sell_put_cash=query_sell_put_cash,
        repo_base=repo_base,
        mask_path=mask_path,
    )


def _get_cached_exchange_rates(*, cache_path):
    return _get_cached_exchange_rates_impl(cache_path=cache_path)


def _monthly_income_report_tool(payload: dict[str, Any]) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    return monthly_income_report_tool(
        payload,
        load_runtime_config=load_runtime_config,
        resolve_public_data_config_path=_resolve_public_data_config_path,
        normalize_broker=_normalize_broker,
        resolve_option_positions_repo=resolve_option_positions_repo,
        build_monthly_income_report=build_monthly_income_report,
        get_cached_exchange_rates=_get_cached_exchange_rates,
        repo_base=repo_base,
        mask_path=mask_path,
    )


def _option_positions_read_tool(payload: dict[str, Any]) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    return option_positions_read_tool(
        payload,
        load_runtime_config=load_runtime_config,
        resolve_public_data_config_path=_resolve_public_data_config_path,
        normalize_broker=_normalize_broker,
        normalize_account=_normalize_account,
        resolve_option_positions_repo=resolve_option_positions_repo,
        list_position_rows=_list_position_rows,
        build_lot_event_history=build_lot_event_history,
        inspect_projection_state=inspect_projection_state,
        repo_base=repo_base,
        mask_path=mask_path,
    )


def _get_portfolio_context_tool(payload: dict[str, Any]) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    return get_portfolio_context_tool(
        payload,
        load_runtime_config=load_runtime_config,
        resolve_public_data_config_path=_resolve_public_data_config_path,
        normalize_broker=_normalize_broker,
        resolve_output_root=resolve_output_root,
        load_portfolio_context=load_portfolio_context,
        repo_base=repo_base,
        mask_path=mask_path,
    )


def _scan_opportunities_tool(payload: dict[str, Any]) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    from scripts.pipeline_watchlist import run_watchlist_pipeline_default
    return scan_opportunities_tool(
        payload,
        load_runtime_config=load_runtime_config,
        resolve_data_config_ref=_resolve_data_config_ref,
        resolve_output_root=resolve_output_root,
        repo_base=repo_base,
        load_config=load_runtime_pipeline_config,
        run_watchlist_pipeline_default=run_watchlist_pipeline_default,
        scan_summary_rows_fn=lambda rows: _scan_summary_rows(rows, as_float=_as_float),
    )


def _prepare_close_advice_inputs_tool(payload: dict[str, Any]) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    return prepare_close_advice_inputs_tool(
        payload,
        load_runtime_config=load_runtime_config,
        resolve_public_data_config_path=_resolve_public_data_config_path,
        normalize_broker=_normalize_broker,
        resolve_output_root=resolve_output_root,
        load_option_positions_context=load_option_positions_context,
        symbol_fetch_config_map_fn=lambda cfg: _symbol_fetch_config_map(cfg, resolve_watchlist_config=resolve_watchlist_config),
        extract_context_symbols_fn=_extract_context_symbols,
        resolve_symbol_fetch_source=resolve_symbol_fetch_source,
        fetch_symbol_opend=fetch_symbol_opend,
        save_required_data_opend=save_required_data_opend,
        repo_base=repo_base,
        mask_path=mask_path,
    )


def _close_advice_tool(payload: dict[str, Any]) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    return close_advice_tool(
        payload,
        load_runtime_config=load_runtime_config,
        resolve_output_root=resolve_output_root,
        resolve_local_path=lambda value, *, default: _resolve_local_path(value, default=default),
        run_close_advice=run_close_advice,
        close_advice_rows_summary_fn=lambda csv_path, text_path: _close_advice_rows_summary(csv_path, text_path, safe_read_csv=safe_read_csv, as_float=_as_float),
        repo_base=repo_base,
        mask_path=mask_path,
    )


def _get_close_advice_tool(payload: dict[str, Any]) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    return get_close_advice_tool(
        payload,
        prepare_close_advice_inputs_tool_fn=_prepare_close_advice_inputs_tool,
        close_advice_tool_fn=_close_advice_tool,
    )


def _manage_symbols_tool(payload: dict[str, Any]) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    return manage_symbols_tool(
        payload,
        load_runtime_config=load_runtime_config,
        deepcopy_fn=deepcopy,
        write_tools_enabled=write_tools_enabled,
        apply_symbol_mutation_fn=lambda cfg, payload: _apply_symbol_mutation(cfg, payload, normalize_accounts=normalize_accounts, resolve_watchlist_config=resolve_watchlist_config),
        validate_runtime_config=_validate_runtime_config,
        list_symbol_rows_fn=lambda cfg: _list_symbol_rows(cfg, resolve_watchlist_config=resolve_watchlist_config, normalize_accounts=normalize_accounts),
        write_json_atomic=_write_json_atomic,
        mask_path=mask_path,
    )


def _preview_notification_tool(payload: dict[str, Any]) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    return preview_notification_tool(payload, build_notification=build_notification)


def _runtime_status_tool(payload: dict[str, Any]) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    return runtime_status_tool(
        payload,
        load_runtime_config=load_runtime_config,
        normalize_accounts=normalize_accounts,
        accounts_from_config=accounts_from_config,
        read_json_object_or_empty=_read_json_object_or_empty,
        repo_base=repo_base,
        mask_path=mask_path,
    )


def _openclaw_readiness_tool(payload: dict[str, Any]) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    return openclaw_readiness_tool(
        payload,
        runtime_status_tool_fn=_runtime_status_tool,
        healthcheck_tool_fn=_healthcheck_tool,
    )


TOOL_HANDLERS = {
    "healthcheck": _healthcheck_tool,
    "version_check": _version_check_tool,
    "config_validate": _config_validate_tool,
    "scheduler_status": _scheduler_status_tool,
    "query_cash_headroom": _query_cash_headroom_tool,
    "monthly_income_report": _monthly_income_report_tool,
    "option_positions_read": _option_positions_read_tool,
    "get_portfolio_context": _get_portfolio_context_tool,
    "scan_opportunities": _scan_opportunities_tool,
    "prepare_close_advice_inputs": _prepare_close_advice_inputs_tool,
    "close_advice": _close_advice_tool,
    "get_close_advice": _get_close_advice_tool,
    "manage_symbols": _manage_symbols_tool,
    "preview_notification": _preview_notification_tool,
    "runtime_status": _runtime_status_tool,
    "openclaw_readiness": _openclaw_readiness_tool,
}
