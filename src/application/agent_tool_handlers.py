from __future__ import annotations

from typing import Any

from copy import deepcopy

from src.application.account_config import normalize_accounts
from src.application.agent_tool_config import load_runtime_config, repo_base, resolve_output_root, write_tools_enabled
from src.application.agent_tool_contracts import mask_path
from src.application.close_advice_runner import run_close_advice
from src.application.config_loader import load_config as load_runtime_pipeline_config, resolve_watchlist_config
from src.application.config_validator import validate_config
from domain.domain.fetch_source import resolve_symbol_fetch_source
from src.application.pipeline_context import load_option_positions_context, load_portfolio_context
from src.application.cash_headroom_query import query_sell_put_cash
from src.infrastructure.io_utils import safe_read_csv
from src.application.agent_tool_operations import (
    version_update_tool,
)
from src.application.agent_tool_runtime import (
    as_float as _as_float,
    extract_context_symbols as _extract_context_symbols,
    normalize_broker as _normalize_broker,
    resolve_data_config_ref as _resolve_data_config_ref,
    resolve_local_path as _resolve_local_path_impl,
    resolve_public_data_config_path as _resolve_public_data_config_path_impl,
    symbol_fetch_config_map as _symbol_fetch_config_map,
    validate_runtime_config as _validate_runtime_config_impl,
    write_json_atomic as _write_json_atomic,
)
from src.application.agent_tool_scan import (
    close_advice_rows_summary as _close_advice_rows_summary,
    close_advice_tool,
    get_close_advice_tool,
    get_portfolio_context_tool,
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
from src.application.version_check import update_local_version


def fetch_symbol_opend(*args: Any, **kwargs: Any) -> Any:
    from src.application.opend_symbol_fetching import fetch_symbol as _fetch_symbol_opend

    return _fetch_symbol_opend(*args, **kwargs)


def save_required_data_opend(*args: Any, **kwargs: Any) -> Any:
    from src.application.opend_symbol_outputs import save_outputs as _save_required_data_opend

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


def _mask_path_str(value: Any) -> str:
    return mask_path(value) or "..."


def _version_update_tool(payload: dict[str, Any]) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    return version_update_tool(
        payload,
        update_local_version=update_local_version,
        repo_base=repo_base,
        mask_path=_mask_path_str,
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
        mask_path=_mask_path_str,
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
    from src.application.pipeline_watchlist import run_watchlist_pipeline_default
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


TOOL_HANDLERS = {
    "version_update": _version_update_tool,
    "query_cash_headroom": _query_cash_headroom_tool,
    "get_portfolio_context": _get_portfolio_context_tool,
    "scan_opportunities": _scan_opportunities_tool,
    "prepare_close_advice_inputs": _prepare_close_advice_inputs_tool,
    "close_advice": _close_advice_tool,
    "get_close_advice": _get_close_advice_tool,
    "manage_symbols": _manage_symbols_tool,
}
