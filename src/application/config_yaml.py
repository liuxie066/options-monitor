from __future__ import annotations

import shlex
from copy import deepcopy
from datetime import datetime, timezone
from difflib import get_close_matches
from pathlib import Path
from typing import Any

import yaml

from domain.domain.strategy_vocab import STRATEGY_COVERED_CALL
from domain.domain.symbol_identity import symbol_market
from src.application.agent_tool_contracts import AgentToolError
from src.application.account_config import normalize_account_label
from src.application.assistant.llm_model_profiles import resolve_authoring_assistant_config
from src.application.config_primitives import (
    config_key_parts as _key_parts,
    config_path_get as _path_get,
    deep_merge_config as _deep_merge,
    file_sha256 as _file_sha256,
    normalize_config_market as _normalize_market,
    path_for_metadata as _path_for_metadata,
    resolve_config_path as _resolve_path,
)
from src.application.config_validator import (
    OPENING_STRATEGY_ALLOWED_FIELDS,
    COMBO_YIELD_ALLOWED_FIELDS,
    COMBO_YIELD_CALL_ALLOWED_FIELDS,
    validate_assistant_config,
    validate_config,
)
from src.application.config_defaults import (
    DEFAULT_CONFIG_REF,
    default_config,
    default_config_sha256,
)
from src.application.layered_config import (
    build_layered_runtime_config_from_user_config,
    default_system_config_path,
)
from src.application.runtime_config_freshness import GENERATED_KEY, GENERATED_SCHEMA_VERSION
from src.application.runtime_config_paths import write_json_atomic
from src.application.runtime_paths import resolve_runtime_root


RESOLVED_KEY = "_resolved"

PASSTHROUGH_KEYS = {
    "alert_policy",
    "close_advice",
    "notifications",
    "option_positions",
    "outputs",
    "portfolio",
    "project_guard",
    "runtime",
    "schedule",
    "symbol_defaults",
    "templates",
    "watchdog",
}
ASSISTANT_AUTHORING_KEYS = {"assistant", "inbound"}
TRADE_INTAKE_AUTHORING_KEYS = {
    "combo_reconciliation",
    "holdings_sync",
    "settlement_observation",
}
ROOT_KEYS = {
    "accounts",
    "features",
    "markets",
    "trade_intake",
    *PASSTHROUGH_KEYS,
    *ASSISTANT_AUTHORING_KEYS,
}
MARKET_KEYS = {"accounts", "features", "overrides", "symbols", *PASSTHROUGH_KEYS}
WRITE_GATE_KEYS = {"write_gates", "write_permissions", "writes", "feishu_write", "feishu_writes"}
COVERED_CALL_AUTHORING_KEY = "covered_call"
SELL_CALL_LEGACY_AUTHORING_KEY = STRATEGY_COVERED_CALL
COMBO_YIELD_AUTHORING_KEY = "combo_yield"
COMBO_YIELD_RETIRED_AUTHORING_KEY = "yield_enhancement"
OPENING_STRATEGY_AUTHORING_FIELDS = OPENING_STRATEGY_ALLOWED_FIELDS | {"dte", "strike"}
COMBO_YIELD_AUTHORING_FIELDS = {
    key for key in COMBO_YIELD_ALLOWED_FIELDS if not key.startswith("_")
}
WHEEL_AUTHORING_FIELDS = {
    "enabled",
    "accounts",
    "min_dte",
    "max_dte",
    "min_delta",
    "min_annualized_net_premium_return",
    "min_net_premium_cny",
    "max_spread_ratio",
    "min_iv_rv_ratio",
    "min_iv_minus_rv",
}


def default_yaml_config_path(*, repo_root: Path) -> Path:
    return (repo_root / "config.yaml").resolve()


def default_yaml_output_config_path(*, repo_root: Path, market: str, runtime_root: str | Path | None = None) -> Path:
    runtime = resolve_runtime_root(repo_root=repo_root, runtime_root=runtime_root)
    return (runtime.runtime_root / f"config.{market}.json").resolve()


def default_yaml_assistant_config_path(*, repo_root: Path, runtime_root: str | Path | None = None) -> Path:
    runtime = resolve_runtime_root(repo_root=repo_root, runtime_root=runtime_root)
    return (runtime.runtime_root / "resolved" / "config.assistant.json").resolve()


def _system_defaults(system_cfg: dict[str, Any]) -> dict[str, Any]:
    defaults = system_cfg.get("defaults")
    return deepcopy(defaults) if isinstance(defaults, dict) else deepcopy(system_cfg)


def load_yaml_config_file(path: str | Path) -> dict[str, Any]:
    config_path = Path(path).expanduser().resolve()
    if not config_path.exists():
        raise AgentToolError(
            code="CONFIG_ERROR",
            message=f"config.yaml not found: {config_path}",
            hint="Create config.yaml from configs/examples/config.yaml.example, or pass --config-yaml explicitly.",
        )

    text = config_path.read_text(encoding="utf-8")
    for line_no, line in enumerate(text.splitlines(), start=1):
        if "\t" in line:
            raise AgentToolError(
                code="CONFIG_ERROR",
                message=f"config.yaml must use spaces, not tabs: {config_path}:{line_no}",
                hint="Use 2-space indentation. YAML tabs are rejected to keep diffs and parser behavior predictable.",
                details={"line": line_no},
            )

    try:
        payload = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        details: dict[str, Any] = {"error": str(exc)}
        location = str(config_path)
        if mark is not None:
            line = int(getattr(mark, "line", 0)) + 1
            column = int(getattr(mark, "column", 0)) + 1
            details.update({"line": line, "column": column})
            location = f"{config_path}:{line}:{column}"
        raise AgentToolError(
            code="CONFIG_ERROR",
            message=f"failed to parse config.yaml: {location}",
            details=details,
            hint="Fix the YAML syntax first. Use 2 spaces per level and quote symbols like 0700.HK if needed.",
        ) from exc

    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise AgentToolError(code="CONFIG_ERROR", message=f"config.yaml must be a YAML object: {config_path}")
    return payload


def _reject_unknown_keys(data: dict[str, Any], *, allowed: set[str], path: str) -> None:
    for raw_key in data:
        key = str(raw_key or "").strip()
        if key == "ai_decision_advice" and path in {"config.yaml", "markets.us", "markets.hk"}:
            raise AgentToolError(
                code="CONFIG_ERROR",
                message=f"{path}.ai_decision_advice is retired and must be removed",
            )
        if key in WRITE_GATE_KEYS:
            raise AgentToolError(
                code="CONFIG_ERROR",
                message=f"{path}.{key} is not a config.yaml field",
                hint="Write permissions belong in options-monitor.env and still require command-level apply/confirm.",
            )
        if key not in allowed:
            allowed_text = ", ".join(sorted(allowed))
            raise AgentToolError(
                code="CONFIG_ERROR",
                message=f"{path}.{key} is not supported in config.yaml",
                hint=f"Use one of: {allowed_text}",
            )


def _normalize_account_label(raw: Any, *, path: str) -> str:
    try:
        return normalize_account_label(raw)
    except ValueError as exc:
        raise AgentToolError(
            code="CONFIG_ERROR",
            message=f"{path} is not a valid account label: {exc}",
        ) from exc


def _normalize_symbol(raw: Any, *, path: str) -> str:
    symbol = str(raw or "").strip().upper()
    if not symbol:
        raise AgentToolError(code="CONFIG_ERROR", message=f"{path} must be a non-empty symbol")
    return symbol


def _normalize_account_setting(raw: Any, *, account: str, path: str) -> dict[str, Any]:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise AgentToolError(code="CONFIG_ERROR", message=f"{path} must be an object")
    item = deepcopy(raw)

    shorthand_account_id = str(item.pop("futu_account_id", "") or "").strip()
    if shorthand_account_id:
        futu = item.get("futu")
        if futu is None:
            futu = {}
        if not isinstance(futu, dict):
            raise AgentToolError(code="CONFIG_ERROR", message=f"{path}.futu must be an object")
        existing = str(futu.get("account_id") or "").strip()
        if existing and existing != shorthand_account_id:
            raise AgentToolError(
                code="CONFIG_ERROR",
                message=f"{path}.futu_account_id conflicts with {path}.futu.account_id",
            )
        futu = dict(futu)
        futu["account_id"] = shorthand_account_id
        item["futu"] = futu

    account_type = str(item.get("type") or "").strip().lower()
    if not account_type:
        account_type = "external_holdings" if str(item.get("holdings_account") or "").strip() else "futu"
    item["type"] = account_type
    if account_type == "external_holdings" and not str(item.get("holdings_account") or "").strip():
        item["holdings_account"] = account
    return item


def _normalize_account_defs(raw_accounts: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(raw_accounts, dict):
        raise AgentToolError(code="CONFIG_ERROR", message="accounts must be an object keyed by account label")
    out: dict[str, dict[str, Any]] = {}
    for raw_account, raw_cfg in raw_accounts.items():
        account = _normalize_account_label(raw_account, path="accounts.<key>")
        if account in out:
            raise AgentToolError(code="CONFIG_ERROR", message=f"duplicate account after normalization: {account}")
        out[account] = _normalize_account_setting(raw_cfg, account=account, path=f"accounts.{account}")
    return out


def _normalize_market_accounts(raw_accounts: Any, *, path: str) -> list[str]:
    if not isinstance(raw_accounts, list) or not raw_accounts:
        raise AgentToolError(code="CONFIG_ERROR", message=f"{path} must be a non-empty list")
    out: list[str] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_accounts):
        account = _normalize_account_label(raw, path=f"{path}[{index}]")
        if account in seen:
            raise AgentToolError(code="CONFIG_ERROR", message=f"duplicate account in {path}: {account}")
        seen.add(account)
        out.append(account)
    return out


def _normalize_symbols(raw_symbols: Any, *, path: str, market: str) -> list[str]:
    if not isinstance(raw_symbols, list) or not raw_symbols:
        raise AgentToolError(code="CONFIG_ERROR", message=f"{path} must be a non-empty list of symbol strings")
    out: list[str] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_symbols):
        if not isinstance(raw, str):
            raise AgentToolError(
                code="CONFIG_ERROR",
                message=f"{path}[{index}] must be a symbol string",
                hint="Keep symbols as a string list and put per-symbol settings under markets.<market>.overrides.",
            )
        symbol = _normalize_symbol(raw, path=f"{path}[{index}]")
        _ensure_symbol_market(symbol, market=market, path=f"{path}[{index}]")
        if symbol in seen:
            raise AgentToolError(code="CONFIG_ERROR", message=f"duplicate symbol in {path}: {symbol}")
        seen.add(symbol)
        out.append(symbol)
    return out


def _ensure_symbol_market(symbol: str, *, market: str, path: str) -> None:
    actual_market = str(symbol_market(symbol) or "").strip().lower()
    expected_market = _normalize_market(market)
    if actual_market and actual_market != expected_market:
        raise AgentToolError(
            code="CONFIG_ERROR",
            message=(
                f"{path} resolves to {actual_market.upper()} but is configured under "
                f"markets.{expected_market}"
            ),
            hint=f"Move {symbol} to markets.{actual_market}.symbols.",
        )


def _apply_range_shorthand(item: dict[str, Any], *, key: str, min_key: str, max_key: str, path: str) -> None:
    if key not in item:
        return
    raw = item.pop(key)
    if not isinstance(raw, list) or len(raw) != 2:
        raise AgentToolError(code="CONFIG_ERROR", message=f"{path}.{key} must be a two-item list")
    item[min_key] = raw[0]
    item[max_key] = raw[1]


def _reject_unknown_authoring_keys(raw: dict[str, Any], *, allowed: set[str], path: str) -> None:
    unknown = sorted(str(key) for key in raw if str(key) not in allowed)
    if not unknown:
        return
    suggestions: list[str] = []
    choices = sorted(allowed)
    for key in unknown:
        match = get_close_matches(key, choices, n=1, cutoff=0.6)
        if match:
            suggestions.append(f"{key}->{match[0]}")
    hint = f"; did you mean: {', '.join(suggestions)}" if suggestions else ""
    raise AgentToolError(
        code="CONFIG_ERROR",
        message=f"{path} contains unsupported keys: {', '.join(unknown)}{hint}",
    )


def _normalize_strategy(
    raw: Any,
    *,
    path: str,
    allow_ranges: bool = True,
    allowed_keys: set[str] | None = None,
) -> dict[str, Any]:
    if isinstance(raw, bool):
        return {"enabled": raw}
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise AgentToolError(code="CONFIG_ERROR", message=f"{path} must be an object or boolean")
    if allowed_keys is not None:
        _reject_unknown_authoring_keys(raw, allowed=allowed_keys, path=path)
    out = deepcopy(raw)
    if allow_ranges:
        _apply_range_shorthand(out, key="dte", min_key="min_dte", max_key="max_dte", path=path)
        _apply_range_shorthand(out, key="strike", min_key="min_strike", max_key="max_strike", path=path)
    return out


def _normalize_combo_yield(raw: Any, *, path: str) -> dict[str, Any]:
    out = _normalize_strategy(
        raw,
        path=path,
        allow_ranges=False,
        allowed_keys=COMBO_YIELD_AUTHORING_FIELDS,
    )
    out["_explicit_fields"] = [key for key in out if not str(key).startswith("_")]
    call_cfg = out.get("call")
    if isinstance(call_cfg, dict):
        _reject_unknown_authoring_keys(
            call_cfg,
            allowed=COMBO_YIELD_CALL_ALLOWED_FIELDS,
            path=f"{path}.call",
        )
        out["_explicit_call_fields"] = [key for key in call_cfg if not str(key).startswith("_")]
    return out


def _canonical_strategy_authoring_key(raw_key: Any) -> str:
    key = str(raw_key or "").strip()
    if key == COVERED_CALL_AUTHORING_KEY:
        return SELL_CALL_LEGACY_AUTHORING_KEY
    if key == COMBO_YIELD_RETIRED_AUTHORING_KEY:
        raise AgentToolError(
            code="CONFIG_ERROR",
            message=f"{key} has been removed; use {COMBO_YIELD_AUTHORING_KEY}",
        )
    return key


def _runtime_key_parts_for_yaml_authoring(parts: list[str]) -> list[str]:
    return [_canonical_strategy_authoring_key(part) for part in parts]


def _normalize_strategy_authoring_container(
    raw: dict[str, Any],
    *,
    path: str,
    normalize_strategy_values: bool,
    allow_ranges: bool = True,
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for raw_key, raw_value in raw.items():
        key = str(raw_key or "").strip()
        canonical_key = _canonical_strategy_authoring_key(key)
        if canonical_key == SELL_CALL_LEGACY_AUTHORING_KEY and canonical_key in out:
            raise AgentToolError(
                code="CONFIG_ERROR",
                message=f"{path} cannot define both {COVERED_CALL_AUTHORING_KEY} and {SELL_CALL_LEGACY_AUTHORING_KEY}",
                hint="Use covered_call in config.yaml; sell_call is kept as the generated runtime/internal key.",
            )
        if normalize_strategy_values and canonical_key in {"sell_put", SELL_CALL_LEGACY_AUTHORING_KEY}:
            out[canonical_key] = _normalize_strategy(
                raw_value,
                path=f"{path}.{key}",
                allow_ranges=allow_ranges,
                allowed_keys=OPENING_STRATEGY_AUTHORING_FIELDS,
            )
        elif normalize_strategy_values and canonical_key == COMBO_YIELD_AUTHORING_KEY:
            out[canonical_key] = _normalize_combo_yield(raw_value, path=f"{path}.{key}")
        else:
            out[canonical_key] = deepcopy(raw_value)
    return out


def _normalize_symbol_override(raw: Any, *, path: str) -> dict[str, Any]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise AgentToolError(code="CONFIG_ERROR", message=f"{path} must be an object")
    out: dict[str, Any] = {}
    for raw_key, raw_value in raw.items():
        key = str(raw_key or "").strip()
        canonical_key = _canonical_strategy_authoring_key(key)
        if key == "symbol":
            raise AgentToolError(code="CONFIG_ERROR", message=f"{path}.symbol is derived from the overrides key")
        if canonical_key == SELL_CALL_LEGACY_AUTHORING_KEY and canonical_key in out:
            raise AgentToolError(
                code="CONFIG_ERROR",
                message=f"{path} cannot define both {COVERED_CALL_AUTHORING_KEY} and {SELL_CALL_LEGACY_AUTHORING_KEY}",
                hint="Use covered_call in config.yaml; sell_call is kept as the generated runtime/internal key.",
            )
        if canonical_key in {"sell_put", SELL_CALL_LEGACY_AUTHORING_KEY}:
            out[canonical_key] = _normalize_strategy(
                raw_value,
                path=f"{path}.{key}",
                allow_ranges=True,
                allowed_keys=OPENING_STRATEGY_AUTHORING_FIELDS,
            )
        elif canonical_key == COMBO_YIELD_AUTHORING_KEY:
            out[canonical_key] = _normalize_combo_yield(raw_value, path=f"{path}.{key}")
        else:
            out[key] = deepcopy(raw_value)
    return out


def _normalize_features(raw: Any, *, path: str) -> dict[str, Any]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise AgentToolError(code="CONFIG_ERROR", message=f"{path} must be an object")
    out: dict[str, Any] = {}
    for raw_key, raw_value in raw.items():
        key = str(raw_key or "").strip()
        if key == "wheel":
            if not path.startswith("markets."):
                raise AgentToolError(
                    code="CONFIG_ERROR",
                    message=f"{path}.wheel is only supported under markets.<market>.features",
                )
            out["wheel"] = _normalize_strategy(
                raw_value,
                path=f"{path}.wheel",
                allow_ranges=False,
                allowed_keys=WHEEL_AUTHORING_FIELDS,
            )
            continue
        if key == "close_advice":
            close_advice = _normalize_strategy(raw_value, path=f"{path}.close_advice", allow_ranges=False)
            out["close_advice"] = close_advice
            continue
        if key == COMBO_YIELD_AUTHORING_KEY:
            raise AgentToolError(
                code="CONFIG_ERROR",
                message=f"{path}.{key} is not a global feature switch",
                hint="Enable Combo Yield per symbol under markets.<market>.overrides.<symbol>.combo_yield.",
            )
        if "write" in key or key in WRITE_GATE_KEYS:
            raise AgentToolError(
                code="CONFIG_ERROR",
                message=f"{path}.{key} is not a config.yaml feature",
                hint="Write permissions belong in options-monitor.env and still require command-level apply/confirm.",
            )
        raise AgentToolError(code="CONFIG_ERROR", message=f"{path}.{key} is not a supported feature")
    return out


def _normalize_trade_intake_authoring(raw: Any, *, path: str) -> dict[str, Any]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise AgentToolError(code="CONFIG_ERROR", message=f"{path} must be an object")
    _reject_unknown_keys(raw, allowed=TRADE_INTAKE_AUTHORING_KEYS, path=path)
    out: dict[str, Any] = {}
    if "settlement_observation" in raw:
        settlement_observation = raw.get("settlement_observation")
        if not isinstance(settlement_observation, dict):
            raise AgentToolError(
                code="CONFIG_ERROR",
                message=(
                    f"{path}.settlement_observation must be an object"
                ),
            )
        _reject_unknown_keys(
            settlement_observation,
            allowed={"enabled"},
            path=f"{path}.settlement_observation",
        )
        enabled = settlement_observation.get("enabled", True)
        if not isinstance(enabled, bool):
            raise AgentToolError(
                code="CONFIG_ERROR",
                message=(
                    f"{path}.settlement_observation.enabled must be a boolean"
                ),
            )
        out["settlement_observation"] = {"enabled": enabled}
    if "holdings_sync" in raw:
        holdings_sync = raw.get("holdings_sync")
        if not isinstance(holdings_sync, dict):
            raise AgentToolError(
                code="CONFIG_ERROR",
                message=f"{path}.holdings_sync must be an object",
            )
        out["holdings_sync"] = deepcopy(holdings_sync)
    if "combo_reconciliation" in raw:
        combo_reconciliation = raw.get("combo_reconciliation")
        if not isinstance(combo_reconciliation, dict):
            raise AgentToolError(
                code="CONFIG_ERROR",
                message=f"{path}.combo_reconciliation must be an object",
            )
        _reject_unknown_keys(
            combo_reconciliation,
            allowed={"accounts", "default_mode"},
            path=f"{path}.combo_reconciliation",
        )
        default_raw = combo_reconciliation.get("default_mode", "off")
        default_mode = (
            "off"
            if default_raw is False
            else str(default_raw or "off").strip().lower()
        )
        accounts_raw = combo_reconciliation.get("accounts") or {}
        if not isinstance(accounts_raw, dict):
            raise AgentToolError(
                code="CONFIG_ERROR",
                message=f"{path}.combo_reconciliation.accounts must be an object",
            )
        account_modes = {
            str(account): (
                "off"
                if mode is False
                else str(mode or "").strip().lower()
            )
            for account, mode in accounts_raw.items()
        }
        out["combo_reconciliation"] = {
            "default_mode": default_mode,
            "accounts": account_modes,
        }
    return out


def _normalize_templates_authoring_keys(raw: Any, *, path: str) -> Any:
    if not isinstance(raw, dict):
        return deepcopy(raw)
    out: dict[str, Any] = {}
    for raw_profile, raw_profile_cfg in raw.items():
        profile = str(raw_profile or "").strip()
        if isinstance(raw_profile_cfg, dict):
            out[raw_profile] = _normalize_strategy_authoring_container(
                raw_profile_cfg,
                path=f"{path}.{profile}",
                normalize_strategy_values=True,
                allow_ranges=True,
            )
        else:
            out[raw_profile] = deepcopy(raw_profile_cfg)
    return out


def _normalize_passthrough_authoring_value(*, key: str, value: Any, path: str) -> Any:
    if key == "templates":
        return _normalize_templates_authoring_keys(value, path=path)
    if key == "symbol_defaults":
        if not isinstance(value, dict):
            return deepcopy(value)
        return _normalize_strategy_authoring_container(
            value,
            path=path,
            normalize_strategy_values=True,
            allow_ranges=True,
        )
    if key == "alert_policy":
        if not isinstance(value, dict):
            return deepcopy(value)
        return _normalize_strategy_authoring_container(
            value,
            path=path,
            normalize_strategy_values=False,
        )
    return deepcopy(value)


def runtime_strategy_keys_to_yaml_authoring(raw: Any) -> Any:
    if isinstance(raw, dict):
        out: dict[Any, Any] = {}
        for raw_key, raw_value in raw.items():
            key = str(raw_key or "").strip()
            if key == SELL_CALL_LEGACY_AUTHORING_KEY:
                yaml_key: Any = COVERED_CALL_AUTHORING_KEY
            elif key == COMBO_YIELD_RETIRED_AUTHORING_KEY:
                raise AgentToolError(
                    code="CONFIG_ERROR",
                    message=f"{key} has been removed; use {COMBO_YIELD_AUTHORING_KEY}",
                )
            else:
                yaml_key = raw_key
            if yaml_key in out:
                raise AgentToolError(
                    code="CONFIG_ERROR",
                    message=f"cannot convert duplicate YAML authoring key: {yaml_key}",
                    hint="Keep only the canonical authoring key in config.yaml.",
                )
            out[yaml_key] = runtime_strategy_keys_to_yaml_authoring(raw_value)
        return out
    if isinstance(raw, list):
        return [runtime_strategy_keys_to_yaml_authoring(item) for item in raw]
    return deepcopy(raw)


def _copy_passthrough(data: dict[str, Any], *, path: str) -> dict[str, Any]:
    return {
        key: _normalize_passthrough_authoring_value(key=key, value=data[key], path=f"{path}.{key}")
        for key in PASSTHROUGH_KEYS
        if key in data
    }


def yaml_to_market_user_config(raw_cfg: dict[str, Any], *, market: str) -> dict[str, Any]:
    normalized_market = _normalize_market(market)
    _reject_unknown_keys(raw_cfg, allowed=ROOT_KEYS, path="config.yaml")

    account_defs = _normalize_account_defs(raw_cfg.get("accounts"))
    markets = raw_cfg.get("markets")
    if not isinstance(markets, dict):
        raise AgentToolError(code="CONFIG_ERROR", message="markets must be an object keyed by market")
    market_cfg = markets.get(normalized_market)
    if market_cfg is None:
        raise AgentToolError(
            code="CONFIG_ERROR",
            message=f"markets.{normalized_market} is required",
            hint="Market is explicit; YAML config never falls back to us or hk.",
        )
    if not isinstance(market_cfg, dict):
        raise AgentToolError(code="CONFIG_ERROR", message=f"markets.{normalized_market} must be an object")
    _reject_unknown_keys(market_cfg, allowed=MARKET_KEYS, path=f"markets.{normalized_market}")

    accounts = _normalize_market_accounts(market_cfg.get("accounts"), path=f"markets.{normalized_market}.accounts")
    account_settings: dict[str, dict[str, Any]] = {}
    for account in accounts:
        if account not in account_defs:
            raise AgentToolError(
                code="CONFIG_ERROR",
                message=f"markets.{normalized_market}.accounts references undefined account: {account}",
                hint=f"Define accounts.{account} at the top level.",
            )
        account_settings[account] = deepcopy(account_defs[account])

    symbols = _normalize_symbols(
        market_cfg.get("symbols"),
        path=f"markets.{normalized_market}.symbols",
        market=normalized_market,
    )
    overrides_raw = market_cfg.get("overrides") or {}
    if not isinstance(overrides_raw, dict):
        raise AgentToolError(code="CONFIG_ERROR", message=f"markets.{normalized_market}.overrides must be an object")

    overrides: dict[str, dict[str, Any]] = {}
    symbol_set = set(symbols)
    for raw_symbol, raw_override in overrides_raw.items():
        symbol = _normalize_symbol(raw_symbol, path=f"markets.{normalized_market}.overrides.<key>")
        _ensure_symbol_market(
            symbol,
            market=normalized_market,
            path=f"markets.{normalized_market}.overrides.{symbol}",
        )
        if symbol not in symbol_set:
            raise AgentToolError(
                code="CONFIG_ERROR",
                message=f"markets.{normalized_market}.overrides.{symbol} must also appear in symbols",
            )
        overrides[symbol] = _normalize_symbol_override(
            raw_override,
            path=f"markets.{normalized_market}.overrides.{symbol}",
        )

    runtime_symbols: list[dict[str, Any]] = []
    for symbol in symbols:
        item = {"symbol": symbol}
        item = _deep_merge(item, overrides.get(symbol, {}))
        runtime_symbols.append(item)

    out = _copy_passthrough(raw_cfg, path="config.yaml")
    out = _deep_merge(out, _copy_passthrough(market_cfg, path=f"markets.{normalized_market}"))
    if "trade_intake" in raw_cfg:
        out["trade_intake"] = _normalize_trade_intake_authoring(
            raw_cfg.get("trade_intake"),
            path="config.yaml.trade_intake",
        )
    out = _deep_merge(out, _normalize_features(raw_cfg.get("features"), path="features"))
    out = _deep_merge(out, _normalize_features(market_cfg.get("features"), path=f"markets.{normalized_market}.features"))
    out["accounts"] = accounts
    out["account_settings"] = account_settings
    out["symbols"] = runtime_symbols
    return out


def _yaml_rebuild_command(*, config_path: Path, market: str, output_path: Path | None = None) -> str:
    command = [
        "./om",
        "config",
        "build",
        "--source",
        "yaml",
        "--market",
        market,
        "--config-yaml",
        str(config_path),
    ]
    if output_path is not None:
        command.extend(["--output", str(output_path)])
    return " ".join(shlex.quote(part) for part in command)


def _build_yaml_generated_metadata(
    *,
    repo_root: Path,
    market: str,
    yaml_path: Path,
    system_path: Path | None,
    system_ref: str,
    system_sha256: str,
    output_path: Path | None = None,
) -> dict[str, Any]:
    version_path = repo_root / "VERSION"
    version = version_path.read_text(encoding="utf-8").strip() if version_path.exists() else None
    if system_path is not None:
        system_source = {
            "role": "system",
            "loaded": True,
            "optional": False,
            "enabled": True,
            "path": _path_for_metadata(system_path, repo_root=repo_root),
            "sha256": _file_sha256(system_path),
        }
    else:
        system_source = {
            "role": "system",
            "loaded": True,
            "optional": False,
            "enabled": True,
            "inline": True,
            "ref": system_ref,
            "sha256": system_sha256,
        }
    return {
        "schema_version": GENERATED_SCHEMA_VERSION,
        "generator": "options-monitor",
        "source_format": "yaml",
        "version": version,
        "market": str(market),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sources": [
            system_source,
            {
                "role": "common_user",
                "loaded": False,
                "optional": True,
                "enabled": False,
            },
            {
                "role": "market_user",
                "loaded": True,
                "optional": False,
                "enabled": True,
                "path": _path_for_metadata(yaml_path, repo_root=repo_root),
                "sha256": _file_sha256(yaml_path),
            },
        ],
        "rebuild_command": _yaml_rebuild_command(
            config_path=yaml_path,
            market=market,
            output_path=output_path,
        ),
    }


def resolve_yaml_runtime_config(
    *,
    repo_root: Path,
    market: str,
    config_path: str | Path | None = None,
    system_config_path: str | Path | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    normalized_market = _normalize_market(market)
    yaml_path = _resolve_path(config_path, default=default_yaml_config_path(repo_root=repo_root))
    explicit_system_path = bool(system_config_path is not None and str(system_config_path).strip())
    system_path = _resolve_path(system_config_path, default=default_system_config_path(repo_root=repo_root)) if explicit_system_path else None
    system_cfg = None if system_path is not None else default_config()
    system_ref = str(system_path) if system_path is not None else DEFAULT_CONFIG_REF
    system_sha256 = _file_sha256(system_path) if system_path is not None else default_config_sha256()
    raw_cfg = load_yaml_config_file(yaml_path)
    user_cfg = yaml_to_market_user_config(raw_cfg, market=normalized_market)
    cfg, meta = build_layered_runtime_config_from_user_config(
        repo_root=repo_root,
        market=normalized_market,
        user_config=user_cfg,
        common_user_config=None,
        system_config=system_cfg,
        system_config_ref=system_ref,
        system_config_path=system_path,
        common_user_config_ref=None,
        user_config_ref=str(yaml_path),
    )
    cfg[GENERATED_KEY] = _build_yaml_generated_metadata(
        repo_root=repo_root,
        market=normalized_market,
        yaml_path=yaml_path,
        system_path=system_path,
        system_ref=system_ref,
        system_sha256=system_sha256,
    )
    cfg[RESOLVED_KEY] = {
        "source_format": "yaml",
        "market": normalized_market,
        "config_yaml_path": _path_for_metadata(yaml_path, repo_root=repo_root),
        "config_yaml_sha256": _file_sha256(yaml_path),
        "default_source": _path_for_metadata(system_path, repo_root=repo_root) if system_path is not None else system_ref,
        "default_sha256": system_sha256,
        "runtime_schema": "config-json-v1",
    }
    meta.update(
        {
            "source_format": "yaml",
            "config_yaml_path": str(yaml_path),
            "config_yaml_sha256": _file_sha256(yaml_path),
            "system_config_path": str(system_path) if system_path is not None else system_ref,
            "system_config_ref": system_ref,
            "system_config_sha256": system_sha256,
        }
    )
    return cfg, meta


def build_yaml_runtime_config_file(
    *,
    repo_root: Path,
    market: str,
    config_path: str | Path | None = None,
    system_config_path: str | Path | None = None,
    output_config_path: str | Path | None = None,
    runtime_root: str | Path | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    normalized_market = _normalize_market(market)
    cfg, meta = resolve_yaml_runtime_config(
        repo_root=repo_root,
        market=normalized_market,
        config_path=config_path,
        system_config_path=system_config_path,
    )
    output_path = _resolve_path(
        output_config_path,
        default=default_yaml_output_config_path(
            repo_root=repo_root,
            market=normalized_market,
            runtime_root=runtime_root,
        ),
    )
    cfg[GENERATED_KEY]["rebuild_command"] = _yaml_rebuild_command(
        config_path=Path(meta["config_yaml_path"]),
        market=normalized_market,
        output_path=output_path,
    )

    if not dry_run:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomic(output_path, cfg)

    return {
        "ok": True,
        **meta,
        "output_config_path": str(output_path),
        "dry_run": bool(dry_run),
        "write_applied": not bool(dry_run),
    }


def _assistant_config_from_runtime_defaults(cfg: dict[str, Any]) -> dict[str, Any]:
    assistant = cfg.get("assistant")
    if isinstance(assistant, dict):
        return deepcopy(assistant)
    return {
        "enabled": True,
        "context_window_messages": 8,
        "copilot": {
            "enabled": False,
            "toolsets": {
                "portfolio": False,
            },
        },
        "llm": {
            "provider": "",
            "base_url": "",
            "model": "",
            "api_key_env": "OM_LLM_API_KEY",
            "confidence_min": 0.75,
            "timeout_seconds": 90,
            "context_window_tokens": 24000,
            "max_output_tokens": 2048,
        },
    }


def resolve_yaml_assistant_config(
    *,
    repo_root: Path,
    config_path: str | Path | None = None,
    system_config_path: str | Path | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    yaml_path = _resolve_path(config_path, default=default_yaml_config_path(repo_root=repo_root))
    explicit_system_path = bool(system_config_path is not None and str(system_config_path).strip())
    system_path = _resolve_path(system_config_path, default=default_system_config_path(repo_root=repo_root)) if explicit_system_path else None
    system_doc = load_yaml_config_file(system_path) if system_path is not None else default_config()
    system_cfg = _system_defaults(system_doc)
    system_ref = str(system_path) if system_path is not None else DEFAULT_CONFIG_REF
    system_sha256 = _file_sha256(system_path) if system_path is not None else default_config_sha256()
    raw_cfg = load_yaml_config_file(yaml_path)

    assistant_cfg = _assistant_config_from_runtime_defaults(system_cfg)
    raw_assistant = raw_cfg.get("assistant")
    if raw_assistant is not None:
        if not isinstance(raw_assistant, dict):
            raise AgentToolError(code="CONFIG_ERROR", message="assistant must be an object")
        assistant_cfg = _deep_merge(assistant_cfg, raw_assistant)
    assistant_cfg, assistant_model_meta = resolve_authoring_assistant_config(assistant_cfg)

    inbound_cfg = deepcopy(system_cfg.get("inbound") if isinstance(system_cfg.get("inbound"), dict) else {})
    raw_inbound = raw_cfg.get("inbound")
    if raw_inbound is not None:
        if not isinstance(raw_inbound, dict):
            raise AgentToolError(code="CONFIG_ERROR", message="inbound must be an object")
        inbound_cfg = _deep_merge(inbound_cfg, raw_inbound)

    generated = {
        "schema_version": GENERATED_SCHEMA_VERSION,
        "generator": "options-monitor",
        "source_format": "yaml",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sources": [
            {
                "role": "system",
                "loaded": system_path is not None,
                "optional": False,
                "enabled": True,
                "path": _path_for_metadata(system_path, repo_root=repo_root) if system_path is not None else system_ref,
                "sha256": system_sha256,
            },
            {
                "role": "config_yaml",
                "loaded": True,
                "optional": False,
                "enabled": True,
                "path": _path_for_metadata(yaml_path, repo_root=repo_root),
                "sha256": _file_sha256(yaml_path),
            },
        ],
    }
    cfg = {
        "assistant": assistant_cfg,
        "inbound": inbound_cfg,
        GENERATED_KEY: generated,
        RESOLVED_KEY: {
            "source_format": "yaml",
            "config_yaml_path": _path_for_metadata(yaml_path, repo_root=repo_root),
            "config_yaml_sha256": _file_sha256(yaml_path),
            "default_source": _path_for_metadata(system_path, repo_root=repo_root) if system_path is not None else system_ref,
            "default_sha256": system_sha256,
            "runtime_schema": "assistant-config-json-v1",
            "assistant_models": assistant_model_meta,
        },
    }
    meta = {
        "source_format": "yaml",
        "config_yaml_path": str(yaml_path),
        "config_yaml_sha256": _file_sha256(yaml_path),
        "system_config_path": str(system_path) if system_path is not None else system_ref,
        "system_config_ref": system_ref,
        "system_config_sha256": system_sha256,
    }
    validate_assistant_config(deepcopy(cfg))
    return cfg, meta


def build_yaml_assistant_config_file(
    *,
    repo_root: Path,
    config_path: str | Path | None = None,
    system_config_path: str | Path | None = None,
    output_config_path: str | Path | None = None,
    runtime_root: str | Path | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    cfg, meta = resolve_yaml_assistant_config(
        repo_root=repo_root,
        config_path=config_path,
        system_config_path=system_config_path,
    )
    output_path = _resolve_path(
        output_config_path,
        default=default_yaml_assistant_config_path(repo_root=repo_root, runtime_root=runtime_root),
    )
    rebuild_parts = [
        "./om",
        "config",
        "build-assistant",
        "--source",
        "yaml",
        "--config-yaml",
        str(Path(meta["config_yaml_path"])),
    ]
    if str(meta.get("system_config_ref") or "") != DEFAULT_CONFIG_REF:
        rebuild_parts.extend(["--system-config", str(Path(meta["system_config_path"]))])
    rebuild_parts.extend(["--output", str(output_path)])
    cfg[GENERATED_KEY]["rebuild_command"] = " ".join(shlex.quote(part) for part in rebuild_parts)

    if not dry_run:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomic(output_path, cfg)

    return {
        "ok": True,
        **meta,
        "output_config_path": str(output_path),
        "dry_run": bool(dry_run),
        "write_applied": not bool(dry_run),
    }


def validate_yaml_runtime_config(
    *,
    repo_root: Path,
    market: str,
    config_path: str | Path | None = None,
    system_config_path: str | Path | None = None,
) -> dict[str, Any]:
    cfg, meta = resolve_yaml_runtime_config(
        repo_root=repo_root,
        market=market,
        config_path=config_path,
        system_config_path=system_config_path,
    )
    validate_config(deepcopy(cfg))
    return {"ok": True, **meta}


def explain_yaml_config_key(
    *,
    repo_root: Path,
    market: str,
    key: str,
    config_path: str | Path | None = None,
    system_config_path: str | Path | None = None,
) -> dict[str, Any]:
    normalized_market = _normalize_market(market)
    parts = _key_parts(key)
    runtime_parts = _runtime_key_parts_for_yaml_authoring(parts)
    cfg, meta = resolve_yaml_runtime_config(
        repo_root=repo_root,
        market=normalized_market,
        config_path=config_path,
        system_config_path=system_config_path,
    )
    exists, value = _path_get(cfg, runtime_parts)
    runtime_path = ".".join(runtime_parts)
    notes = [
        "config.yaml stores user overrides only; defaults are merged before runtime validation.",
        "Write permissions are not explained here because they live in options-monitor.env.",
    ]
    if runtime_path != str(key):
        notes.append("covered_call in config.yaml maps to sell_call in generated runtime config.")
    return {
        "ok": True,
        "source_format": "yaml",
        "market": normalized_market,
        "key": str(key),
        "exists": bool(exists),
        "value": value if exists else None,
        "source": "resolved_yaml" if exists else None,
        "runtime_path": runtime_path,
        "trace": [
            {
                "source": "config_yaml",
                "path": meta["config_yaml_path"],
                "sha256": meta["config_yaml_sha256"],
            },
            {
                "source": "system_defaults",
                "path": meta["system_config_ref"],
                "sha256": meta["system_config_sha256"],
            },
        ],
        "notes": notes,
        **meta,
    }


__all__ = [
    "RESOLVED_KEY",
    "build_yaml_assistant_config_file",
    "build_yaml_runtime_config_file",
    "default_yaml_assistant_config_path",
    "default_yaml_config_path",
    "default_yaml_output_config_path",
    "explain_yaml_config_key",
    "load_yaml_config_file",
    "resolve_yaml_assistant_config",
    "resolve_yaml_runtime_config",
    "runtime_strategy_keys_to_yaml_authoring",
    "validate_yaml_runtime_config",
    "yaml_to_market_user_config",
]
