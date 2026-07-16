from __future__ import annotations

import json
from copy import deepcopy
from json import JSONDecodeError
from pathlib import Path
from typing import Any

from src.application.account_config import ACCOUNT_TYPE_EXTERNAL_HOLDINGS, ACCOUNT_TYPE_FUTU, ACCOUNT_TYPES, normalize_accounts
from src.application.config_loader import normalize_portfolio_broker_config
from src.application.config_sections import set_watchlist_config
from src.application.agent_tool_contracts import AgentToolError
from src.application.config_primitives import (
    deep_merge_config as _deep_merge,
    normalize_config_market as _normalize_market,
    resolve_config_path as _resolve_path,
)
from src.application.config_validator import validate_config


def default_system_config_path(*, repo_root: Path) -> Path:
    return (repo_root / "configs" / "system.json").resolve()


def default_user_config_path(*, repo_root: Path, market: str) -> Path:
    return (repo_root / "configs" / f"user.{market}.json").resolve()


def default_common_user_config_path(*, repo_root: Path) -> Path:
    return (repo_root / "configs" / "user.common.json").resolve()


def _read_json_object(path: Path, *, label: str, hint: str | None = None) -> dict[str, Any]:
    if not path.exists():
        raise AgentToolError(
            code="CONFIG_ERROR",
            message=f"{label} not found: {path}",
            hint=hint,
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except JSONDecodeError as exc:
        raise AgentToolError(
            code="CONFIG_ERROR",
            message=f"failed to parse {label}: {path}:{exc.lineno}:{exc.colno}",
            details={
                "error": str(exc),
                "line": int(exc.lineno),
                "column": int(exc.colno),
                "position": int(exc.pos),
            },
            hint="Fix the JSON syntax first. A common cause is a trailing comma in ignored or local runtime config.",
        ) from exc
    except Exception as exc:
        raise AgentToolError(
            code="CONFIG_ERROR",
            message=f"failed to parse {label}: {path}",
            details={"error": f"{type(exc).__name__}: {exc}"},
        ) from exc
    if not isinstance(payload, dict):
        raise AgentToolError(code="CONFIG_ERROR", message=f"{label} must be a JSON object: {path}")
    return payload


def _symbol_defaults_override(raw: dict[str, Any] | None, *, label: str) -> dict[str, Any]:
    if raw is None:
        return {}
    value = raw.get("symbol_defaults")
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise AgentToolError(code="CONFIG_ERROR", message=f"{label}.symbol_defaults must be an object")
    return deepcopy(value)


def _system_defaults_and_market_payload(system_cfg: dict[str, Any], *, market: str) -> tuple[dict[str, Any], dict[str, Any]]:
    defaults = system_cfg.get("defaults")
    if defaults is None:
        defaults = {}
    if not isinstance(defaults, dict):
        raise AgentToolError(code="CONFIG_ERROR", message="system.defaults must be an object")

    markets = system_cfg.get("markets")
    if markets is None:
        markets = {}
    if not isinstance(markets, dict):
        raise AgentToolError(code="CONFIG_ERROR", message="system.markets must be an object")
    market_payload = markets.get(market)
    if market_payload is None:
        market_payload = {}
    if not isinstance(market_payload, dict):
        raise AgentToolError(code="CONFIG_ERROR", message=f"system.markets.{market} must be an object")
    return defaults, market_payload


def _system_market_payload(system_cfg: dict[str, Any], *, market: str) -> dict[str, Any]:
    defaults, market_payload = _system_defaults_and_market_payload(system_cfg, market=market)
    return _deep_merge(defaults, market_payload)


def _normalized_account_settings(raw: Any) -> dict[str, dict[str, Any]]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise AgentToolError(code="CONFIG_ERROR", message="account_settings must be an object")

    out: dict[str, dict[str, Any]] = {}
    for raw_key, raw_value in raw.items():
        account = str(raw_key or "").strip().lower()
        if not account:
            raise AgentToolError(code="CONFIG_ERROR", message="account_settings contains empty account key")
        if not isinstance(raw_value, dict):
            raise AgentToolError(code="CONFIG_ERROR", message=f"account_settings.{account} must be an object")
        item = deepcopy(raw_value)
        account_type = str(item.get("type") or ACCOUNT_TYPE_FUTU).strip().lower()
        if account_type not in ACCOUNT_TYPES:
            raise AgentToolError(
                code="CONFIG_ERROR",
                message=f"account_settings.{account}.type must be one of: {', '.join(ACCOUNT_TYPES)}",
            )
        item["type"] = account_type
        if account_type == ACCOUNT_TYPE_EXTERNAL_HOLDINGS and not str(item.get("holdings_account") or "").strip():
            item["holdings_account"] = account
        out[account] = item
    return out


def _derive_accounts(cfg: dict[str, Any]) -> list[str]:
    raw_accounts = cfg.get("accounts")
    settings = _normalized_account_settings(cfg.get("account_settings"))
    if raw_accounts is not None:
        accounts = normalize_accounts(raw_accounts, fallback=())
    else:
        accounts = list(settings.keys())

    if not accounts:
        raise AgentToolError(
            code="CONFIG_ERROR",
            message="user config must define accounts or account_settings",
            hint="Keep the user file small, but include at least one account label.",
        )

    cfg["accounts"] = accounts
    for account in accounts:
        if account not in settings:
            settings[account] = {"type": ACCOUNT_TYPE_FUTU}
    cfg["account_settings"] = settings
    return accounts


def _derive_portfolio(cfg: dict[str, Any], *, accounts: list[str]) -> None:
    portfolio = cfg.get("portfolio")
    if not isinstance(portfolio, dict):
        portfolio = {}
    else:
        portfolio = deepcopy(portfolio)

    portfolio.setdefault("broker", "富途")
    portfolio.setdefault("source", "futu")
    portfolio.setdefault("base_currency", "CNY")
    portfolio["account"] = str(portfolio.get("account") or accounts[0]).strip().lower()

    raw_account_settings = cfg.get("account_settings")
    account_settings = raw_account_settings if isinstance(raw_account_settings, dict) else {}
    source_by_account = portfolio.get("source_by_account")
    if not isinstance(source_by_account, dict):
        source_by_account = {}
    for account in accounts:
        raw_setting = account_settings.get(account)
        setting = raw_setting if isinstance(raw_setting, dict) else {}
        account_type = str(setting.get("type") or ACCOUNT_TYPE_FUTU).strip().lower()
        source_by_account.setdefault(account, "holdings" if account_type == ACCOUNT_TYPE_EXTERNAL_HOLDINGS else "futu")
    portfolio["source_by_account"] = source_by_account

    cfg["portfolio"] = portfolio


def _derive_trade_intake(cfg: dict[str, Any], *, accounts: list[str]) -> None:
    trade_intake = cfg.get("trade_intake")
    if not isinstance(trade_intake, dict):
        trade_intake = {}
    else:
        trade_intake = deepcopy(trade_intake)
    trade_intake.setdefault("enabled", True)
    trade_intake.setdefault("mode", "dry-run")

    account_mapping = trade_intake.get("account_mapping")
    if not isinstance(account_mapping, dict):
        account_mapping = {}
    else:
        account_mapping = deepcopy(account_mapping)
    futu_mapping = account_mapping.get("futu")
    if not isinstance(futu_mapping, dict):
        futu_mapping = {}
    else:
        futu_mapping = {str(k): str(v).strip().lower() for k, v in futu_mapping.items()}

    raw_account_settings = cfg.get("account_settings")
    account_settings = raw_account_settings if isinstance(raw_account_settings, dict) else {}
    for account in accounts:
        raw_setting = account_settings.get(account)
        setting = raw_setting if isinstance(raw_setting, dict) else {}
        if str(setting.get("type") or ACCOUNT_TYPE_FUTU).strip().lower() != ACCOUNT_TYPE_FUTU:
            continue
        if setting.get("trade_intake_enabled") is False:
            continue
        raw_futu_cfg = setting.get("futu")
        futu_cfg = raw_futu_cfg if isinstance(raw_futu_cfg, dict) else {}
        account_id = str(futu_cfg.get("account_id") or "").strip()
        if account_id:
            futu_mapping.setdefault(account_id, account)

    account_mapping["futu"] = futu_mapping
    trade_intake["account_mapping"] = account_mapping
    cfg["trade_intake"] = trade_intake


def _apply_symbol_defaults(cfg: dict[str, Any], *, symbol_defaults: dict[str, Any]) -> None:
    raw_symbols = cfg.get("symbols")
    if not isinstance(raw_symbols, list) or not raw_symbols:
        raise AgentToolError(
            code="CONFIG_ERROR",
            message="user config symbols[] is required and cannot be empty",
        )

    symbols: list[dict[str, Any]] = []
    for index, raw_item in enumerate(raw_symbols):
        if not isinstance(raw_item, dict):
            raise AgentToolError(code="CONFIG_ERROR", message=f"symbols[{index}] must be an object")
        symbols.append(_deep_merge(symbol_defaults, raw_item))
    cfg["symbols"] = symbols


def build_layered_runtime_config_from_user_config(
    *,
    repo_root: Path,
    market: str,
    user_config: dict[str, Any],
    common_user_config: dict[str, Any] | None = None,
    system_config: dict[str, Any] | None = None,
    system_config_ref: str | None = None,
    system_config_path: str | Path | None = None,
    common_user_config_ref: str | None = None,
    user_config_ref: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    normalized_market = _normalize_market(market)

    if not isinstance(user_config, dict):
        raise AgentToolError(code="CONFIG_ERROR", message="user config must be a JSON object")
    if common_user_config is not None and not isinstance(common_user_config, dict):
        raise AgentToolError(code="CONFIG_ERROR", message="common user config must be a JSON object")
    if system_config is not None and system_config_path is not None:
        raise AgentToolError(code="CONFIG_ERROR", message="system_config and system_config_path cannot both be set")
    if system_config is not None and not isinstance(system_config, dict):
        raise AgentToolError(code="CONFIG_ERROR", message="system config must be a JSON object")

    system_path: Path | None = None
    if system_config is None:
        system_path = _resolve_path(system_config_path, default=default_system_config_path(repo_root=repo_root))
        system_cfg = _read_json_object(system_path, label="system config")
        system_ref = str(system_path)
    else:
        system_cfg = deepcopy(system_config)
        system_ref = str(system_config_ref or "<memory>")
    system_market = _system_market_payload(system_cfg, market=normalized_market)

    symbol_defaults = system_market.pop("symbol_defaults", {})
    if not isinstance(symbol_defaults, dict):
        raise AgentToolError(code="CONFIG_ERROR", message=f"system.markets.{normalized_market}.symbol_defaults must be an object")
    symbol_defaults = _deep_merge(
        symbol_defaults,
        _symbol_defaults_override(common_user_config, label="common user config"),
    )
    symbol_defaults = _deep_merge(
        symbol_defaults,
        _symbol_defaults_override(user_config, label="user config"),
    )

    cfg = _deep_merge(system_market, common_user_config or {})
    cfg = _deep_merge(cfg, user_config)
    for internal_key in ("defaults", "markets", "symbol_defaults", "assistant", "inbound"):
        cfg.pop(internal_key, None)

    _apply_symbol_defaults(cfg, symbol_defaults=symbol_defaults)
    set_watchlist_config(cfg, cfg["symbols"])
    cfg = normalize_portfolio_broker_config(cfg)
    accounts = _derive_accounts(cfg)
    _derive_portfolio(cfg, accounts=accounts)
    _derive_trade_intake(cfg, accounts=accounts)

    try:
        validate_config(deepcopy(cfg))
    except SystemExit as exc:
        raise AgentToolError(code="CONFIG_ERROR", message=str(exc)) from exc

    meta = {
        "market": normalized_market,
        "system_config_path": str(system_path) if system_path is not None else system_ref,
        "system_config_ref": system_ref,
        "common_user_config_ref": str(common_user_config_ref or "<none>"),
        "common_user_config_loaded": common_user_config is not None,
        "user_config_ref": str(user_config_ref or "<memory>"),
        "accounts": accounts,
        "symbols": [str(item.get("symbol") or "") for item in cfg.get("symbols", []) if isinstance(item, dict)],
    }
    return cfg, meta
