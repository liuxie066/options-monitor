from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from src.application.agent_tool_contracts import AgentToolError
from src.application.agent_tools.operations_impl import config_validate_tool, scheduler_status_tool
from src.application.agent_tools.symbols_impl import find_symbol_entry, manage_symbols_tool
from src.application.agent_tools.base import AgentTool, build_agent_tool
from src.application.account_config import accounts_from_config
from src.application.agent_tools.symbols_impl import apply_symbol_mutation
from copy import deepcopy as deepcopy_value
from src.application.agent_tools.symbols_impl import list_symbol_rows
from src.application.agent_tool_config import load_runtime_config
from src.application.agent_tool_contracts import mask_path
from src.application.account_config import normalize_accounts
from src.application.scan_scheduler import read_state as read_scheduler_state
from src.application.agent_tool_config import repo_base
from src.application.config_sections import resolve_watchlist_config
from src.application.scan_scheduler import decide as scheduler_decide
from src.application.agent_tools.runtime_helpers import validate_runtime_config
from src.application.agent_tool_config import write_tools_enabled
from src.application.config_yaml_symbols import mutate_yaml_symbol_config
from src.application.runtime_config_freshness import infer_runtime_config_market
from src.application.symbol_calibration import calibrate_symbol
from src.application.combo_yield_config import resolve_combo_yield_cfg


_CONFIG_VALIDATE_OUTPUT_CONTRACT: dict[str, Any] = {
    "schema_version": "config_validate.output.v1",
    "evidence_type": "diagnostic", "bounded_projection": "contract_fields", "coverage": "primary_rows", "freshness": "not_applicable", "pagination": {"mode": "none"},
    "source_label": "OM runtime config validator",
    "primary_rows": "warnings",
    "fact_fields": [
        "config_key",
        "config_path",
        "account_count",
        "accounts[]",
        "symbol_count",
        "warnings[]",
    ],
}

_SYMBOL_CONFIG_OUTPUT_CONTRACT: dict[str, Any] = {
    "schema_version": "symbol_config_read.output.v1",
    "evidence_type": "point", "bounded_projection": "contract_fields", "coverage": "point", "freshness": "source_declared", "pagination": {"mode": "none"},
    "source_label": "OM runtime symbol config",
    "fact_fields": [
        "symbol",
        "canonical_symbol",
        "found",
        "missing_reason",
        "strategy",
        "field",
        "path",
        "value",
        "strategy_config",
        "strategies",
        "freshness",
    ],
    "model_preview_fields": [
        "symbol",
        "canonical_symbol",
        "found",
        "missing_reason",
        "message",
        "strategy",
        "field",
        "path",
        "value",
        "strategy_config",
        "strategies",
        "freshness",
    ],
}

_SYMBOL_RESOLVE_OUTPUT_CONTRACT: dict[str, Any] = {
    "schema_version": "symbol_resolve.output.v1",
    "evidence_type": "point", "bounded_projection": "contract_fields", "coverage": "point", "freshness": "not_applicable", "pagination": {"mode": "none"},
    "source_label": "OM symbol identity resolver",
    "result_shape": "scalar",
    "fact_fields": [
        "symbol",
        "raw_input",
        "canonical_symbol",
        "market",
        "currency",
        "futu_code",
        "source_kind",
        "status",
        "message",
    ],
    "missing_data_fields": [
        "canonical_symbol",
    ],
    "model_preview_fields": [
        "symbol",
        "raw_input",
        "canonical_symbol",
        "market",
        "currency",
        "futu_code",
        "source_kind",
        "status",
        "message",
    ],
}

_SCHEDULER_STATUS_OUTPUT_CONTRACT: dict[str, Any] = {
    "schema_version": "scheduler_status.output.v1",
    "evidence_type": "diagnostic", "bounded_projection": "contract_fields", "coverage": "point", "freshness": "source_declared", "pagination": {"mode": "none"},
    "source_label": "OM scheduler config and local scheduler state",
    "result_shape": "scalar",
    "fact_fields": [
        "decision.reason",
        "decision.is_scan_window_open",
        "decision.is_notify_window_open",
        "decision.should_notify",
        "decision.schedule_enabled",
        "state.last_run_utc_for_account",
        "state.last_notify_utc",
        "state.last_notify_utc_for_account",
        "filters.account",
        "filters.schedule_key",
        "filters.force",
    ],
    "freshness_fields": [
        "state.last_run_utc_for_account",
        "state.last_notify_utc",
        "state.last_notify_utc_for_account",
    ],
}

_SCALAR_SETTING_VALUE_SCHEMA: dict[str, Any] = {"type": ["string", "number", "integer", "boolean", "null"]}
_MANAGE_SYMBOLS_SET_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": "edit-only flat dot-path map, for example {'sell_put.max_strike': 90}; nested objects are not accepted",
    "additionalProperties": _SCALAR_SETTING_VALUE_SCHEMA,
    "propertyNames": {"pattern": r"^[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+$"},
    "minProperties": 1,
}


def _mask_path_str(value: Any) -> str:
    return mask_path(value) or "..."


def _config_validate_tool(
    payload: dict[str, Any],
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    return config_validate_tool(
        payload,
        load_runtime_config=load_runtime_config,
        validate_runtime_config=validate_runtime_config,
        accounts_from_config=accounts_from_config,
        resolve_watchlist_config=resolve_watchlist_config,
        mask_path=lambda value: _mask_path_str(value),
    )


def _scheduler_status_tool(
    payload: dict[str, Any],
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    return scheduler_status_tool(
        payload,
        load_runtime_config=load_runtime_config,
        read_state=read_scheduler_state,
        decide=scheduler_decide,
        repo_base=repo_base,
        mask_path=lambda value: _mask_path_str(value),
    )


def _manage_symbols_tool(
    payload: dict[str, Any],
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    return manage_symbols_tool(
        payload,
        load_runtime_config=load_runtime_config,
        deepcopy_fn=deepcopy_value,
        write_tools_enabled=write_tools_enabled,
        apply_symbol_mutation_fn=lambda cfg, tool_payload: apply_symbol_mutation(
            cfg,
            tool_payload,
            normalize_accounts=normalize_accounts,
            resolve_watchlist_config=resolve_watchlist_config,
        ),
        validate_runtime_config=validate_runtime_config,
        list_symbol_rows_fn=lambda cfg: list_symbol_rows(
            cfg,
            resolve_watchlist_config=resolve_watchlist_config,
            normalize_accounts=normalize_accounts,
        ),
        mask_path=mask_path,
        repo_base_fn=repo_base,
        infer_runtime_config_market_fn=infer_runtime_config_market,
        mutate_yaml_symbol_config_fn=mutate_yaml_symbol_config,
    )


_STRATEGY_ALIASES = {
    "sell put": "sell_put",
    "sell_put": "sell_put",
    "put": "sell_put",
    "sp": "sell_put",
    "cash secured put": "sell_put",
    "cash_secured_put": "sell_put",
    "现金担保": "sell_put",
    "卖沽": "sell_put",
    "sell call": "sell_call",
    "sell_call": "sell_call",
    "covered call": "sell_call",
    "covered_call": "sell_call",
    "call": "sell_call",
    "cc": "sell_call",
    "备兑": "sell_call",
    "combo yield": "combo_yield",
    "combo_yield": "combo_yield",
    "收益增强": "combo_yield",
}
_FIELD_ALIASES = {
    "enabled": "enabled",
    "开关": "enabled",
    "启用": "enabled",
    "min strike": "min_strike",
    "min_strike": "min_strike",
    "minimum strike": "min_strike",
    "最低行权价": "min_strike",
    "最小行权价": "min_strike",
    "max strike": "max_strike",
    "max_strike": "max_strike",
    "maximum strike": "max_strike",
    "最高行权价": "max_strike",
    "最大行权价": "max_strike",
    "min dte": "min_dte",
    "min_dte": "min_dte",
    "max dte": "max_dte",
    "max_dte": "max_dte",
}


def _symbol_config_read_tool(
    payload: dict[str, Any],
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    config_path, cfg = load_runtime_config(config_key=payload.get("config_key"), config_path=payload.get("config_path"))
    raw_symbol = str(payload.get("symbol") or "").strip()
    if not raw_symbol:
        raise AgentToolError(
            code="NEEDS_CLARIFICATION",
            message="symbol_config_read requires symbol",
            hint="例如：泡泡玛特 sell put max strike 是多少。",
        )

    calibration = calibrate_symbol(raw_symbol, config=cfg)
    if calibration.status != "ok" or not calibration.canonical_symbol:
        raise AgentToolError(
            code="NEEDS_CLARIFICATION",
            message=calibration.message,
            hint="请提供标准标的代码或已配置的别名，例如 9992.HK、0700.HK、NVDA。",
            details={"symbol": raw_symbol},
        )

    symbol_market = str(calibration.market or "").strip().lower()
    config_market = infer_runtime_config_market(
        config_key=payload.get("config_key"),
        config_path=config_path,
        config=cfg,
    )
    if symbol_market in {"us", "hk"} and symbol_market != config_market:
        sibling_path = config_path.with_name(f"config.{symbol_market}.json") if payload.get("config_path") else None
        config_path, cfg = load_runtime_config(config_key=symbol_market, config_path=sibling_path)

    canonical = str(calibration.canonical_symbol)
    _idx, entry = find_symbol_entry(cfg, canonical, resolve_watchlist_config=resolve_watchlist_config)
    base: dict[str, Any] = {
        "schema_version": "symbol_config_read.v1",
        "symbol": raw_symbol,
        "canonical_symbol": canonical,
        "calibration": calibration.public_payload(),
        "config_path": mask_path(config_path),
        "found": False,
        "freshness": {
            "status": "fresh",
            "as_of": datetime.now(timezone.utc).isoformat(),
            "kind": "runtime_config_read",
        },
    }
    if entry is None:
        return {
            **base,
            "missing_reason": "symbol_not_configured",
            "message": f"监控标的未配置：{canonical}",
        }, [], {"config_path": mask_path(config_path)}

    strategies = _symbol_strategy_configs(entry)
    strategy, field = _normalize_strategy_and_field(payload.get("strategy"), payload.get("field"))
    data = {
        **base,
        "found": True,
        "symbol_config": _public_symbol_config(entry),
        "strategies": strategies,
    }

    if strategy:
        strategy_cfg = strategies.get(strategy)
        data["strategy"] = strategy
        data["strategy_config"] = dict(strategy_cfg or {})
        if strategy_cfg is None:
            return {
                **data,
                "found": False,
                "missing_reason": "strategy_not_supported",
                "message": f"{canonical} 不支持策略配置：{strategy}",
            }, [], {"config_path": mask_path(config_path)}
        if field:
            exists, value = _read_nested_field(strategy_cfg, field)
            data.update({"field": field, "path": f"{strategy}.{field}"})
            if not exists:
                return {
                    **data,
                    "found": False,
                    "missing_reason": "field_not_configured",
                    "message": f"{canonical} {strategy}.{field} 当前没有配置。",
                }, [], {"config_path": mask_path(config_path)}
            data["value"] = deepcopy_value(value)
        return data, [], {"config_path": mask_path(config_path)}

    if field:
        matches = []
        for name, strategy_cfg in strategies.items():
            exists, value = _read_nested_field(strategy_cfg, field)
            if exists:
                matches.append({"strategy": name, "field": field, "path": f"{name}.{field}", "value": deepcopy_value(value)})
        data["field"] = field
        data["matches"] = matches
        if len(matches) == 1:
            match = matches[0]
            data.update(
                {
                    "strategy": match["strategy"],
                    "path": match["path"],
                    "value": match["value"],
                    "strategy_config": dict(strategies.get(str(match["strategy"])) or {}),
                }
            )
            return data, [], {"config_path": mask_path(config_path)}
        return {
            **data,
            "found": False,
            "missing_reason": "strategy_required" if len(matches) > 1 else "field_not_configured",
            "message": (
                f"{canonical} 的 {field} 存在于多个策略，请指定 sell_put / covered_call。"
                if len(matches) > 1
                else f"{canonical} 当前没有配置字段：{field}。"
            ),
        }, [], {"config_path": mask_path(config_path)}

    return data, [], {"config_path": mask_path(config_path)}


def _symbol_resolve_tool(
    payload: dict[str, Any],
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    raw_symbol = str(payload.get("symbol") or "").strip()
    if not raw_symbol:
        raise AgentToolError(
            code="NEEDS_CLARIFICATION",
            message="symbol_resolve requires symbol",
            hint="请提供标的代码、中文名或已配置别名，例如 泡泡玛特、9992.HK、NVDA。",
        )

    cfg: dict[str, Any] | None = None
    config_path = None
    if str(payload.get("config_key") or "").strip() or str(payload.get("config_path") or "").strip():
        config_path, cfg = load_runtime_config(config_key=payload.get("config_key"), config_path=payload.get("config_path"))

    calibration = calibrate_symbol(raw_symbol, config=cfg)
    data = {
        "schema_version": "symbol_resolve.v1",
        "symbol": raw_symbol,
        "resolved": calibration.status == "ok" and bool(calibration.canonical_symbol),
        **calibration.public_payload(),
    }
    meta: dict[str, Any] = {}
    if config_path is not None:
        masked = mask_path(config_path)
        data["config_path"] = masked
        meta["config_path"] = masked
    warnings = [] if data["resolved"] else ["symbol_unresolved"]
    return data, warnings, meta


def _manage_symbols_write_requested(payload: dict[str, Any]) -> bool:
    action = str(payload.get("action") or "list").strip().lower()
    return action != "list" and not bool(payload.get("dry_run", False))


CONFIG_VALIDATE_TOOL = build_agent_tool(
    name="config_validate",
    catalog_summary="校验市场运行配置并返回诊断结果。",
    description="Validate runtime config only, without OpenD checks or pipeline execution.",
    requires=("runtime_config",),
    capabilities=("config_validate", "read_only"),
    input_schema={
        "config_key": "us|hk (optional when config_path is set)",
        "config_path": "absolute or relative JSON config path",
        "allow_empty_symbols": "optional bool for first-time config scaffolds",
    },
    handler=_config_validate_tool,
    pure_read=True,
    safe_default_input={},
    examples=({"input": {"config_key": "us"}},),
    output_contract=_CONFIG_VALIDATE_OUTPUT_CONTRACT,
    copilot_input_fields=("config_key",),
)

SCHEDULER_STATUS_TOOL = build_agent_tool(
    name="scheduler_status",
    catalog_summary="读取调度器与任务激活状态。",
    description="Return scheduler decision and existing scheduler state without marking scan/notify state or running pipelines.",
    requires=("runtime_config",),
    capabilities=("scheduler_status", "read_only"),
    input_schema={
        "config_key": "us|hk",
        "config_path": "optional explicit config path",
        "state_dir": "optional state dir; defaults to output_shared/state",
        "state": "optional explicit scheduler state file",
        "schedule_key": "optional schedule key; defaults to schedule",
        "account": "optional account label",
        "force": "optional bool to preview force-mode scheduler decision",
    },
    handler=_scheduler_status_tool,
    pure_read=True,
    safe_default_input={},
    examples=({"input": {"config_key": "us", "account": "lx"}},),
    output_contract=_SCHEDULER_STATUS_OUTPUT_CONTRACT,
    copilot_input_fields=("config_key", "schedule_key", "account", "force"),
)

SYMBOL_CONFIG_READ_TOOL = build_agent_tool(
    name="symbol_config_read",
    catalog_summary="读取标的配置与账户覆盖设置。",
    description="Read the current monitored-symbol strategy config for a symbol, strategy, or field.",
    requires=("runtime_config",),
    capabilities=("symbol_config_read", "config_read", "read_only"),
    input_schema={
        "config_key": "us|hk",
        "config_path": "optional explicit config path",
        "symbol": {
            "type": "string",
            "minLength": 1,
            "description": "Symbol, canonical code, or configured alias such as 泡泡玛特",
            "required": True,
        },
        "strategy": "optional sell_put|sell_call|covered_call|combo_yield",
        "field": "optional field or dot path, for example max_strike or sell_put.max_strike",
    },
    handler=_symbol_config_read_tool,
    pure_read=True,
    safe_default_input={},
    examples=(
        {"input": {"config_key": "hk", "symbol": "泡泡玛特", "strategy": "sell_put", "field": "max_strike"}},
        {"input": {"config_key": "us", "symbol": "NVDA", "strategy": "sell_call"}},
    ),
    output_contract=_SYMBOL_CONFIG_OUTPUT_CONTRACT,
    copilot_input_fields=("config_key", "symbol", "strategy", "field"),
)

SYMBOL_RESOLVE_TOOL = build_agent_tool(
    name="symbol_resolve",
    catalog_summary="解析并规范化市场标的身份。",
    description="Resolve a user-provided symbol, Chinese name, alias, or Futu code to canonical OM symbol identity.",
    requires=("symbol_identity",),
    capabilities=("symbol_resolve", "symbol_identity", "read_only"),
    input_schema={
        "config_key": "optional us|hk; when present, include runtime-config symbol aliases",
        "config_path": "optional explicit config path",
        "symbol": {
            "type": "string",
            "minLength": 1,
            "description": "Symbol, company name, option root, Futu code, or configured alias such as 泡泡玛特",
            "required": True,
        },
    },
    handler=_symbol_resolve_tool,
    pure_read=True,
    safe_default_input={},
    examples=(
        {"input": {"symbol": "泡泡玛特"}},
        {"input": {"symbol": "HK.09992"}},
        {"input": {"symbol": "NVDA"}},
    ),
    output_contract=_SYMBOL_RESOLVE_OUTPUT_CONTRACT,
    copilot_input_fields=("config_key", "symbol"),
)

MANAGE_SYMBOLS_TOOL = build_agent_tool(
    name="manage_symbols",
    description="List runtime symbols or mutate authoritative config.yaml and publish generated snapshots. Writes require OM_AGENT_ENABLE_WRITE_TOOLS=true and confirm=true.",
    requires=("runtime_config",),
    capabilities=("config_write",),
    side_effects=("writes_config_yaml", "publishes_generated_runtime_configs"),
    input_schema={
        "config_key": "us|hk",
        "config_path": "optional explicit config path",
        "action": "list|add|edit|remove",
        "symbol": "required for add/edit/remove",
        "set": _MANAGE_SYMBOLS_SET_SCHEMA,
        "limit_expirations": {"type": "integer", "minimum": 1, "description": "add-only option expiration fetch limit"},
        "sell_put_enabled": {"type": "boolean", "description": "add-only Sell Put enable switch"},
        "sell_put_min_dte": {"type": "integer", "minimum": 0, "description": "required when sell_put_enabled=true"},
        "sell_put_max_dte": {"type": "integer", "minimum": 0, "description": "required when sell_put_enabled=true"},
        "sell_put_min_strike": {"type": "number", "minimum": 0, "description": "optional add-only minimum put strike"},
        "sell_put_max_strike": {"type": "number", "minimum": 0, "description": "optional add-only maximum put strike"},
        "sell_call_enabled": {"type": "boolean", "description": "add-only Covered Call enable switch"},
        "sell_call_min_dte": {"type": "integer", "minimum": 0, "description": "required when sell_call_enabled=true"},
        "sell_call_max_dte": {"type": "integer", "minimum": 0, "description": "required when sell_call_enabled=true"},
        "sell_call_min_strike": {"type": "number", "minimum": 0, "description": "optional add-only minimum call strike"},
        "sell_call_max_strike": {"type": "number", "minimum": 0, "description": "optional add-only maximum call strike"},
        "broker": {"type": "string", "minLength": 1, "description": "optional add-only broker override"},
        "use": {
            "type": ["string", "array"],
            "items": {"type": "string", "minLength": 1},
            "description": "optional add-only template reference or ordered template list",
        },
        "accounts": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "description": "optional add-only account scope",
        },
        "dry_run": "optional bool",
        "confirm": "required true for non-dry-run writes",
        "yes": {"type": "boolean", "description": "alias for confirm on non-dry-run writes"},
    },
    handler=_manage_symbols_tool,
    read_only=False,
    risk_level="local_write",
    requires_confirm=True,
    requires_env=("OM_AGENT_ENABLE_WRITE_TOOLS=true for non-dry-run writes",),
    safe_default_input={"action": "list"},
    write_request_predicate=_manage_symbols_write_requested,
    examples=(
        {"input": {"config_key": "us", "action": "list"}},
        {
            "input": {
                "config_key": "us",
                "action": "add",
                "symbol": "NVDA",
                "sell_put_enabled": True,
                "sell_put_min_dte": 20,
                "sell_put_max_dte": 45,
                "use": "put_base",
                "dry_run": True,
            }
        },
    ),
    allow_additional_input=False,
)

TOOLS: tuple[AgentTool, ...] = (
    CONFIG_VALIDATE_TOOL,
    SCHEDULER_STATUS_TOOL,
    SYMBOL_RESOLVE_TOOL,
    SYMBOL_CONFIG_READ_TOOL,
    MANAGE_SYMBOLS_TOOL,
)


def _symbol_strategy_configs(entry: dict[str, Any]) -> dict[str, dict[str, Any]]:
    sell_put = entry.get("sell_put") if isinstance(entry.get("sell_put"), dict) else {}
    sell_call = entry.get("sell_call") if isinstance(entry.get("sell_call"), dict) else {}
    combo_yield = resolve_combo_yield_cfg(entry)
    return {
        "sell_put": dict(sell_put),
        "sell_call": dict(sell_call),
        "combo_yield": dict(combo_yield),
    }


def _public_symbol_config(entry: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in ("symbol", "broker", "accounts", "use", "fetch"):
        value = entry.get(key)
        if value is not None:
            out[key] = value
    for key in ("sell_put", "sell_call"):
        value = entry.get(key)
        if isinstance(value, dict):
            out[key] = dict(value)
    combo_yield = resolve_combo_yield_cfg(entry)
    if combo_yield:
        out["combo_yield"] = dict(combo_yield)
    return out


def _normalize_strategy_and_field(raw_strategy: Any, raw_field: Any) -> tuple[str | None, str | None]:
    strategy = _normalize_strategy(raw_strategy)
    field_text = str(raw_field or "").strip()
    if field_text:
        prefix_strategy, field = _split_strategy_field(field_text)
        return strategy or prefix_strategy, _normalize_field(field)
    return strategy, None


def _normalize_strategy(raw: Any) -> str | None:
    value = str(raw or "").strip()
    if not value:
        return None
    key = value.replace("-", "_").lower()
    if key in {"yield enhancement", "yield_enhancement"}:
        raise AgentToolError(
            code="INPUT_ERROR",
            message="yield_enhancement has been removed; use combo_yield",
        )
    return _STRATEGY_ALIASES.get(value) or _STRATEGY_ALIASES.get(key) or _STRATEGY_ALIASES.get(key.replace("_", " "))


def _split_strategy_field(raw: str) -> tuple[str | None, str]:
    text = str(raw or "").strip()
    if "." not in text:
        return None, text
    prefix, rest = text.split(".", 1)
    strategy = _normalize_strategy(prefix)
    if strategy:
        return strategy, rest
    return None, text


def _normalize_field(raw: Any) -> str | None:
    value = str(raw or "").strip()
    if not value:
        return None
    key = value.replace("-", "_").lower()
    return _FIELD_ALIASES.get(value) or _FIELD_ALIASES.get(key) or _FIELD_ALIASES.get(key.replace("_", " ")) or key


def _read_nested_field(mapping: dict[str, Any], field: str) -> tuple[bool, Any]:
    current: Any = mapping
    for part in [item for item in str(field or "").split(".") if item]:
        if not isinstance(current, dict) or part not in current:
            return False, None
        current = current[part]
    return True, current


__all__ = [
    "CONFIG_VALIDATE_TOOL",
    "MANAGE_SYMBOLS_TOOL",
    "SCHEDULER_STATUS_TOOL",
    "SYMBOL_CONFIG_READ_TOOL",
    "SYMBOL_RESOLVE_TOOL",
    "TOOLS",
]
