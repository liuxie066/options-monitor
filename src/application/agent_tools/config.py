from __future__ import annotations

from typing import Any

from src.application.agent_tool_contracts import AgentToolError
from src.application.agent_tools.operations_impl import config_validate_tool, scheduler_status_tool
from src.application.agent_tools.symbols_impl import find_symbol_entry, manage_symbols_tool
from src.application.agent_tools.base import AgentTool, AgentToolContext, build_agent_tool
from src.application.symbol_calibration import calibrate_symbol
from src.application.yield_enhancement_config import resolve_yield_enhancement_cfg


_CONFIG_VALIDATE_OUTPUT_CONTRACT: dict[str, Any] = {
    "schema_version": "config_validate.output.v1",
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
    "source_label": "OM runtime symbol config",
    "primary_rows": "strategies",
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
    ],
}

_SYMBOL_RESOLVE_OUTPUT_CONTRACT: dict[str, Any] = {
    "schema_version": "symbol_resolve.output.v1",
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


def _mask_path_str(ctx: AgentToolContext, value: Any) -> str:
    return ctx.mask_path(value) or "..."


def _config_validate_tool(
    ctx: AgentToolContext,
    payload: dict[str, Any],
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    return config_validate_tool(
        payload,
        load_runtime_config=ctx.load_runtime_config,
        validate_runtime_config=ctx.validate_runtime_config,
        accounts_from_config=ctx.accounts_from_config,
        resolve_watchlist_config=ctx.resolve_watchlist_config,
        mask_path=lambda value: _mask_path_str(ctx, value),
    )


def _scheduler_status_tool(
    ctx: AgentToolContext,
    payload: dict[str, Any],
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    return scheduler_status_tool(
        payload,
        load_runtime_config=ctx.load_runtime_config,
        read_state=ctx.read_scheduler_state,
        decide=ctx.scheduler_decide,
        repo_base=ctx.repo_base,
        mask_path=lambda value: _mask_path_str(ctx, value),
    )


def _manage_symbols_tool(
    ctx: AgentToolContext,
    payload: dict[str, Any],
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    return manage_symbols_tool(
        payload,
        load_runtime_config=ctx.load_runtime_config,
        deepcopy_fn=ctx.deepcopy_value,
        write_tools_enabled=ctx.write_tools_enabled,
        apply_symbol_mutation_fn=lambda cfg, tool_payload: ctx.apply_symbol_mutation(
            cfg,
            tool_payload,
            normalize_accounts=ctx.normalize_accounts,
            resolve_watchlist_config=ctx.resolve_watchlist_config,
        ),
        validate_runtime_config=ctx.validate_runtime_config,
        list_symbol_rows_fn=lambda cfg: ctx.list_symbol_rows(
            cfg,
            resolve_watchlist_config=ctx.resolve_watchlist_config,
            normalize_accounts=ctx.normalize_accounts,
        ),
        write_json_atomic=ctx.write_json_atomic,
        mask_path=ctx.mask_path,
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
    "yield enhancement": "combo_yield",
    "yield_enhancement": "combo_yield",
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
    ctx: AgentToolContext,
    payload: dict[str, Any],
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    config_path, cfg = ctx.load_runtime_config(config_key=payload.get("config_key"), config_path=payload.get("config_path"))
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

    canonical = str(calibration.canonical_symbol)
    _idx, entry = find_symbol_entry(cfg, canonical, resolve_watchlist_config=ctx.resolve_watchlist_config)
    base: dict[str, Any] = {
        "schema_version": "symbol_config_read.v1",
        "symbol": raw_symbol,
        "canonical_symbol": canonical,
        "calibration": calibration.public_payload(),
        "config_path": ctx.mask_path(config_path),
        "found": False,
    }
    if entry is None:
        return {
            **base,
            "missing_reason": "symbol_not_configured",
            "message": f"监控标的未配置：{canonical}",
        }, [], {"config_path": ctx.mask_path(config_path)}

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
            }, [], {"config_path": ctx.mask_path(config_path)}
        if field:
            exists, value = _read_nested_field(strategy_cfg, field)
            data.update({"field": field, "path": f"{strategy}.{field}"})
            if not exists:
                return {
                    **data,
                    "found": False,
                    "missing_reason": "field_not_configured",
                    "message": f"{canonical} {strategy}.{field} 当前没有配置。",
                }, [], {"config_path": ctx.mask_path(config_path)}
            data["value"] = ctx.deepcopy_value(value)
        return data, [], {"config_path": ctx.mask_path(config_path)}

    if field:
        matches = []
        for name, strategy_cfg in strategies.items():
            exists, value = _read_nested_field(strategy_cfg, field)
            if exists:
                matches.append({"strategy": name, "field": field, "path": f"{name}.{field}", "value": ctx.deepcopy_value(value)})
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
            return data, [], {"config_path": ctx.mask_path(config_path)}
        return {
            **data,
            "found": False,
            "missing_reason": "strategy_required" if len(matches) > 1 else "field_not_configured",
            "message": (
                f"{canonical} 的 {field} 存在于多个策略，请指定 sell_put / covered_call。"
                if len(matches) > 1
                else f"{canonical} 当前没有配置字段：{field}。"
            ),
        }, [], {"config_path": ctx.mask_path(config_path)}

    return data, [], {"config_path": ctx.mask_path(config_path)}


def _symbol_resolve_tool(
    ctx: AgentToolContext,
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
        config_path, cfg = ctx.load_runtime_config(config_key=payload.get("config_key"), config_path=payload.get("config_path"))

    calibration = calibrate_symbol(raw_symbol, config=cfg)
    data = {
        "schema_version": "symbol_resolve.v1",
        "symbol": raw_symbol,
        "resolved": calibration.status == "ok" and bool(calibration.canonical_symbol),
        **calibration.public_payload(),
    }
    meta: dict[str, Any] = {}
    if config_path is not None:
        masked = ctx.mask_path(config_path)
        data["config_path"] = masked
        meta["config_path"] = masked
    warnings = [] if data["resolved"] else ["symbol_unresolved"]
    return data, warnings, meta


def _manage_symbols_write_requested(payload: dict[str, Any]) -> bool:
    action = str(payload.get("action") or "list").strip().lower()
    return action != "list" and not bool(payload.get("dry_run", False))


CONFIG_VALIDATE_TOOL = build_agent_tool(
    name="config_validate",
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
    description="List or mutate symbols[] entries. Write actions require OM_AGENT_ENABLE_WRITE_TOOLS=true and confirm=true.",
    requires=("runtime_config",),
    capabilities=("config_write",),
    side_effects=("writes_runtime_config",),
    input_schema={
        "config_key": "us|hk",
        "config_path": "optional explicit config path",
        "action": "list|add|edit|remove",
        "symbol": "required for add/edit/remove",
        "set": _MANAGE_SYMBOLS_SET_SCHEMA,
        "dry_run": "optional bool",
        "confirm": "required true for non-dry-run writes",
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
        {"input": {"config_key": "us", "action": "add", "symbol": "NVDA", "dry_run": True}},
    ),
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
    combo_yield = resolve_yield_enhancement_cfg(entry)
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
    combo_yield = resolve_yield_enhancement_cfg(entry)
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
