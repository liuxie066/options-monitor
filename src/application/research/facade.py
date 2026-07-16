from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from src.application.agent_tool_config import repo_base
from src.application.agent_tool_contracts import AgentToolError, build_error_payload, build_response, mask_path


def run_research_collect(
    payload: dict[str, Any],
    *,
    repo_base_fn: Callable[[], Path] = repo_base,
) -> dict[str, Any]:
    if _truthy(payload.get("write_outputs")) and not bool(payload.get("confirm")):
        err = AgentToolError(
            code="CONFIRMATION_REQUIRED",
            message="--confirm is required when --write-outputs is used",
            hint="Run without --write-outputs first, then retry with --write-outputs --confirm only when writing local Research reports is intended.",
        )
        return build_response(tool_name="research.collect", ok=False, error=build_error_payload(err))

    try:
        data, warnings, meta = _run_research_collect(payload, repo_base_fn=repo_base_fn)
        return build_response(
            tool_name="research.collect",
            ok=True,
            data=data,
            warnings=warnings,
            meta=meta,
        )
    except AgentToolError as err:
        return build_response(tool_name="research.collect", ok=False, error=build_error_payload(err))
    except Exception as exc:
        err = AgentToolError(code="INTERNAL_ERROR", message=f"{type(exc).__name__}: {exc}")
        return build_response(tool_name="research.collect", ok=False, error=build_error_payload(err))


def _run_research_collect(
    payload: dict[str, Any],
    *,
    repo_base_fn: Callable[[], Path],
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    from src.application.account_config import accounts_from_config, list_account_config_views, normalize_accounts
    from src.application.agent_tool_config import load_runtime_config, write_tools_enabled
    from src.application.agent_tools.healthcheck_impl import run_healthcheck_tool
    from src.application.agent_tools.runtime_helpers import (
        healthcheck_symbols_for_futu,
        mask_account_id,
        read_json_object_or_empty as _read_json_object_or_empty,
        resolve_data_config_ref,
        resolve_public_data_config_path,
        run_futu_doctor,
        validate_runtime_config,
    )
    from src.application.agent_tools.runtime_status_impl import runtime_status_tool
    from src.application.config_sections import resolve_watchlist_config
    from src.application.config_validator import validate_config
    from src.application.futu_portfolio_context import infer_futu_portfolio_settings
    from src.application.ledger.api import open_position_ledger
    from src.application.research.service import research_tool

    def _runtime_status_tool(payload: dict[str, Any]) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
        return runtime_status_tool(
            payload,
            load_runtime_config=load_runtime_config,
            normalize_accounts=normalize_accounts,
            accounts_from_config=accounts_from_config,
            read_json_object_or_empty=_read_json_object_or_empty,
            repo_base=repo_base_fn,
            mask_path=mask_path,
        )

    def _healthcheck_tool(payload: dict[str, Any]) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
        return run_healthcheck_tool(
            payload,
            load_runtime_config=load_runtime_config,
            validate_runtime_config=lambda cfg, allow_empty_symbols=False: validate_runtime_config(
                cfg,
                allow_empty_symbols=allow_empty_symbols,
                resolve_watchlist_config=resolve_watchlist_config,
                validate_config=validate_config,
            ),
            normalize_accounts=normalize_accounts,
            accounts_from_config=accounts_from_config,
            resolve_data_config_ref=resolve_data_config_ref,
            resolve_public_data_config_path=lambda payload, portfolio_cfg: resolve_public_data_config_path(
                payload,
                portfolio_cfg,
                repo_base=repo_base_fn,
            ),
            read_json_object_or_empty=_read_json_object_or_empty,
            mask_path=lambda value: mask_path(value) or "...",
            list_account_config_views=list_account_config_views,
            mask_account_id=mask_account_id,
            infer_futu_portfolio_settings=infer_futu_portfolio_settings,
            load_option_positions_repo=open_position_ledger,
            run_futu_doctor=lambda **kwargs: run_futu_doctor(**kwargs, repo_base=repo_base_fn),
            healthcheck_symbols_for_futu=lambda cfg: healthcheck_symbols_for_futu(
                cfg,
                resolve_watchlist_config=resolve_watchlist_config,
            ),
            write_tools_enabled=write_tools_enabled,
        )

    return research_tool(
        payload,
        runtime_status_tool_fn=_runtime_status_tool,
        healthcheck_tool_fn=_healthcheck_tool,
        load_runtime_config=load_runtime_config,
        repo_base=repo_base_fn,
        mask_path=mask_path,
    )


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


__all__ = ["run_research_collect"]
