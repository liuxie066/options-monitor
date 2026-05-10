from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from src.application.account_config import ACCOUNT_TYPE_EXTERNAL_HOLDINGS, ACCOUNT_TYPE_FUTU, ACCOUNT_TYPES, normalize_accounts
from src.application.config_loader import normalize_portfolio_broker_config, set_watchlist_config
from src.application.config_validator import validate_config
from src.application.agent_tool_contracts import AgentToolError
from src.application.runtime_config_paths import write_json_atomic


MARKETS = ("us", "hk")


def default_system_config_path(*, repo_root: Path) -> Path:
    return (repo_root / "configs" / "system.json").resolve()


def default_user_config_path(*, repo_root: Path, market: str) -> Path:
    return (repo_root / "configs" / f"user.{market}.json").resolve()


def default_common_user_config_path(*, repo_root: Path) -> Path:
    return (repo_root / "configs" / "user.common.json").resolve()


def default_output_config_path(*, repo_root: Path, market: str) -> Path:
    return (repo_root / f"config.{market}.json").resolve()


def _normalize_market(value: str) -> str:
    market = str(value or "").strip().lower()
    if market not in MARKETS:
        raise AgentToolError(code="INPUT_ERROR", message="market must be us or hk")
    return market


def _resolve_path(raw: str | Path | None, *, default: Path) -> Path:
    if raw is None or not str(raw).strip():
        return default.resolve()
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = path.resolve()
    return path


def _read_json_object(path: Path, *, label: str, hint: str | None = None) -> dict[str, Any]:
    if not path.exists():
        raise AgentToolError(
            code="CONFIG_ERROR",
            message=f"{label} not found: {path}",
            hint=hint,
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise AgentToolError(
            code="CONFIG_ERROR",
            message=f"failed to parse {label}: {path}",
            details={"error": f"{type(exc).__name__}: {exc}"},
        ) from exc
    if not isinstance(payload, dict):
        raise AgentToolError(code="CONFIG_ERROR", message=f"{label} must be a JSON object: {path}")
    return payload


def _deep_merge(base: Any, override: Any) -> Any:
    if isinstance(base, dict) and isinstance(override, dict):
        out = deepcopy(base)
        for key, value in override.items():
            if key in out:
                out[key] = _deep_merge(out[key], value)
            else:
                out[key] = deepcopy(value)
        return out
    return deepcopy(override)


def _key_parts(key: str) -> list[str]:
    parts = [part.strip() for part in str(key or "").split(".")]
    if not parts or any(not part for part in parts):
        raise AgentToolError(code="INPUT_ERROR", message="config key must be a non-empty dot path")
    return parts


def _path_get(data: Any, parts: list[str]) -> tuple[bool, Any]:
    current = data
    for part in parts:
        if isinstance(current, dict):
            if part not in current:
                return False, None
            current = current[part]
            continue
        if isinstance(current, list) and part.isdigit():
            index = int(part)
            if index < 0 or index >= len(current):
                return False, None
            current = current[index]
            continue
        return False, None
    return True, current


def _path_join(prefix: str, parts: list[str]) -> str:
    return ".".join([prefix, *parts]) if parts else prefix


def _trace_value(*, source: str, path: str, data: Any, parts: list[str]) -> dict[str, Any] | None:
    exists, value = _path_get(data, parts)
    if not exists:
        return None
    return {"source": source, "path": path, "value": deepcopy(value)}


def _append_trace(
    trace: list[dict[str, Any]],
    *,
    source: str,
    path: str,
    data: Any,
    parts: list[str],
) -> None:
    entry = _trace_value(source=source, path=path, data=data, parts=parts)
    if entry is not None:
        trace.append(entry)


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

    account_settings = cfg.get("account_settings") if isinstance(cfg.get("account_settings"), dict) else {}
    source_by_account = portfolio.get("source_by_account")
    if not isinstance(source_by_account, dict):
        source_by_account = {}
    for account in accounts:
        setting = account_settings.get(account) if isinstance(account_settings.get(account), dict) else {}
        account_type = str(setting.get("type") or ACCOUNT_TYPE_FUTU).strip().lower()
        source_by_account.setdefault(account, "holdings" if account_type == ACCOUNT_TYPE_EXTERNAL_HOLDINGS else "futu")
    portfolio["source_by_account"] = source_by_account

    if not str(portfolio.get("data_config") or "").strip():
        portfolio["data_config"] = "secrets/portfolio.sqlite.json"

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

    account_settings = cfg.get("account_settings") if isinstance(cfg.get("account_settings"), dict) else {}
    for account in accounts:
        setting = account_settings.get(account) if isinstance(account_settings.get(account), dict) else {}
        if str(setting.get("type") or ACCOUNT_TYPE_FUTU).strip().lower() != ACCOUNT_TYPE_FUTU:
            continue
        futu_cfg = setting.get("futu") if isinstance(setting.get("futu"), dict) else {}
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
    system_config_path: str | Path | None = None,
    common_user_config_ref: str | None = None,
    user_config_ref: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    normalized_market = _normalize_market(market)
    system_path = _resolve_path(system_config_path, default=default_system_config_path(repo_root=repo_root))

    if not isinstance(user_config, dict):
        raise AgentToolError(code="CONFIG_ERROR", message="user config must be a JSON object")
    if common_user_config is not None and not isinstance(common_user_config, dict):
        raise AgentToolError(code="CONFIG_ERROR", message="common user config must be a JSON object")

    system_cfg = _read_json_object(system_path, label="system config")
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
    for internal_key in ("defaults", "markets", "symbol_defaults"):
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
        "system_config_path": str(system_path),
        "common_user_config_ref": str(common_user_config_ref or "<none>"),
        "common_user_config_loaded": common_user_config is not None,
        "user_config_ref": str(user_config_ref or "<memory>"),
        "accounts": accounts,
        "symbols": [str(item.get("symbol") or "") for item in cfg.get("symbols", []) if isinstance(item, dict)],
    }
    return cfg, meta


def _symbol_defaults_trace(
    *,
    system_cfg: dict[str, Any],
    market: str,
    common_user_config: dict[str, Any] | None,
    user_config: dict[str, Any],
    parts: list[str],
) -> tuple[list[dict[str, Any]], bool, Any]:
    defaults, market_payload = _system_defaults_and_market_payload(system_cfg, market=market)
    system_market = _system_market_payload(system_cfg, market=market)
    symbol_defaults = system_market.get("symbol_defaults") or {}
    if not isinstance(symbol_defaults, dict):
        raise AgentToolError(code="CONFIG_ERROR", message=f"system.markets.{market}.symbol_defaults must be an object")
    symbol_defaults = _deep_merge(
        symbol_defaults,
        _symbol_defaults_override(common_user_config, label="common user config"),
    )
    symbol_defaults = _deep_merge(
        symbol_defaults,
        _symbol_defaults_override(user_config, label="user config"),
    )

    trace: list[dict[str, Any]] = []
    _append_trace(
        trace,
        source="system.defaults",
        path=_path_join("defaults.symbol_defaults", parts),
        data=defaults,
        parts=["symbol_defaults", *parts],
    )
    _append_trace(
        trace,
        source="system.market",
        path=_path_join(f"markets.{market}.symbol_defaults", parts),
        data=market_payload,
        parts=["symbol_defaults", *parts],
    )
    if common_user_config is not None:
        _append_trace(
            trace,
            source="common_user_config",
            path=_path_join("symbol_defaults", parts),
            data=common_user_config,
            parts=["symbol_defaults", *parts],
        )
    _append_trace(
        trace,
        source="user_config",
        path=_path_join("symbol_defaults", parts),
        data=user_config,
        parts=["symbol_defaults", *parts],
    )
    exists, value = _path_get(symbol_defaults, parts)
    return trace, exists, deepcopy(value)


def _regular_key_trace(
    *,
    system_cfg: dict[str, Any],
    market: str,
    common_user_config: dict[str, Any] | None,
    user_config: dict[str, Any],
    parts: list[str],
) -> list[dict[str, Any]]:
    defaults, market_payload = _system_defaults_and_market_payload(system_cfg, market=market)
    trace: list[dict[str, Any]] = []
    _append_trace(
        trace,
        source="system.defaults",
        path=_path_join("defaults", parts),
        data=defaults,
        parts=parts,
    )
    _append_trace(
        trace,
        source="system.market",
        path=_path_join(f"markets.{market}", parts),
        data=market_payload,
        parts=parts,
    )
    if common_user_config is not None:
        _append_trace(
            trace,
            source="common_user_config",
            path=_path_join("", parts).lstrip("."),
            data=common_user_config,
            parts=parts,
        )
    _append_trace(
        trace,
        source="user_config",
        path=_path_join("", parts).lstrip("."),
        data=user_config,
        parts=parts,
    )
    return trace


def _symbol_key_trace(
    *,
    system_cfg: dict[str, Any],
    market: str,
    common_user_config: dict[str, Any] | None,
    user_config: dict[str, Any],
    parts: list[str],
) -> list[dict[str, Any]]:
    if len(parts) < 3 or parts[0] != "symbols" or not parts[1].isdigit():
        return _regular_key_trace(
            system_cfg=system_cfg,
            market=market,
            common_user_config=common_user_config,
            user_config=user_config,
            parts=parts,
        )

    symbol_index = parts[1]
    symbol_parts = parts[2:]
    trace, _exists, _value = _symbol_defaults_trace(
        system_cfg=system_cfg,
        market=market,
        common_user_config=common_user_config,
        user_config=user_config,
        parts=symbol_parts,
    )
    user_has_symbols, _user_symbols = _path_get(user_config, ["symbols"])
    if common_user_config is not None and not user_has_symbols:
        _append_trace(
            trace,
            source="common_user_config",
            path=_path_join(f"symbols.{symbol_index}", symbol_parts),
            data=common_user_config,
            parts=parts,
        )
    if user_has_symbols:
        _append_trace(
            trace,
            source="user_config",
            path=_path_join(f"symbols.{symbol_index}", symbol_parts),
            data=user_config,
            parts=parts,
        )
    return trace


def explain_layered_runtime_config_key(
    *,
    repo_root: Path,
    market: str,
    key: str,
    system_config_path: str | Path | None = None,
    common_user_config_path: str | Path | None = None,
    include_common_user_config: bool = True,
    user_config_path: str | Path | None = None,
) -> dict[str, Any]:
    normalized_market = _normalize_market(market)
    parts = _key_parts(key)
    explicit_user_path = bool(user_config_path is not None and str(user_config_path).strip())
    system_path = _resolve_path(system_config_path, default=default_system_config_path(repo_root=repo_root))
    user_path = _resolve_path(user_config_path, default=default_user_config_path(repo_root=repo_root, market=normalized_market))
    user_cfg = _read_json_object(
        user_path,
        label="user config",
        hint=f"Copy configs/examples/user.example.{normalized_market}.json to configs/user.{normalized_market}.json, then edit accounts and symbols.",
    )

    common_cfg = None
    common_path = None
    if include_common_user_config:
        if common_user_config_path is not None and str(common_user_config_path).strip():
            common_path = _resolve_path(common_user_config_path, default=default_common_user_config_path(repo_root=repo_root))
            common_cfg = _read_json_object(common_path, label="common user config")
        elif not explicit_user_path:
            common_path = default_common_user_config_path(repo_root=repo_root)
            if common_path.exists():
                common_cfg = _read_json_object(common_path, label="common user config")

    system_cfg = _read_json_object(system_path, label="system config")
    cfg, meta = build_layered_runtime_config_from_user_config(
        repo_root=repo_root,
        market=normalized_market,
        user_config=user_cfg,
        common_user_config=common_cfg,
        system_config_path=system_path,
        common_user_config_ref=str(common_path) if common_cfg is not None and common_path is not None else None,
        user_config_ref=str(user_path),
    )
    if common_path is not None:
        meta["common_user_config_path"] = str(common_path)
    meta["user_config_path"] = str(user_path)

    notes: list[str] = []
    runtime_path: str | None = str(key)
    applies_to: str | None = None
    if parts[0] == "symbol_defaults":
        symbol_parts = parts[1:]
        trace, exists, value = _symbol_defaults_trace(
            system_cfg=system_cfg,
            market=normalized_market,
            common_user_config=common_cfg,
            user_config=user_cfg,
            parts=symbol_parts,
        )
        runtime_path = None
        applies_to = _path_join("symbols[]", symbol_parts)
        notes.append("symbol_defaults is an authoring-only section; it is merged into each symbols[] item before validation.")
    else:
        exists, value = _path_get(cfg, parts)
        if parts[0] == "symbols":
            trace = _symbol_key_trace(
                system_cfg=system_cfg,
                market=normalized_market,
                common_user_config=common_cfg,
                user_config=user_cfg,
                parts=parts,
            )
            notes.append("symbols[] values may include merged symbol_defaults before per-symbol overrides.")
        else:
            trace = _regular_key_trace(
                system_cfg=system_cfg,
                market=normalized_market,
                common_user_config=common_cfg,
                user_config=user_cfg,
                parts=parts,
            )

    source = trace[-1]["source"] if trace else None
    if exists and source is None:
        source = "derived"
        notes.append("No direct authoring-layer value was found; layered_config filled this value during derivation.")

    return {
        "ok": True,
        "market": normalized_market,
        "key": str(key),
        "exists": bool(exists),
        "value": value if exists else None,
        "source": source,
        "runtime_path": runtime_path,
        "applies_to": applies_to,
        "trace": trace,
        "notes": notes,
        **meta,
    }


def build_layered_runtime_config(
    *,
    repo_root: Path,
    market: str,
    system_config_path: str | Path | None = None,
    common_user_config_path: str | Path | None = None,
    include_common_user_config: bool = True,
    user_config_path: str | Path | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    normalized_market = _normalize_market(market)
    explicit_user_path = bool(user_config_path is not None and str(user_config_path).strip())
    user_path = _resolve_path(user_config_path, default=default_user_config_path(repo_root=repo_root, market=normalized_market))
    user_cfg = _read_json_object(
        user_path,
        label="user config",
        hint=f"Copy configs/examples/user.example.{normalized_market}.json to configs/user.{normalized_market}.json, then edit accounts and symbols.",
    )

    common_cfg = None
    common_path = None
    if include_common_user_config:
        if common_user_config_path is not None and str(common_user_config_path).strip():
            common_path = _resolve_path(common_user_config_path, default=default_common_user_config_path(repo_root=repo_root))
            common_cfg = _read_json_object(common_path, label="common user config")
        elif not explicit_user_path:
            common_path = default_common_user_config_path(repo_root=repo_root)
            if common_path.exists():
                common_cfg = _read_json_object(common_path, label="common user config")

    cfg, meta = build_layered_runtime_config_from_user_config(
        repo_root=repo_root,
        market=normalized_market,
        user_config=user_cfg,
        common_user_config=common_cfg,
        system_config_path=system_config_path,
        common_user_config_ref=str(common_path) if common_cfg is not None and common_path is not None else None,
        user_config_ref=str(user_path),
    )
    if common_path is not None:
        meta["common_user_config_path"] = str(common_path)
    meta["user_config_path"] = str(user_path)
    return cfg, meta


def build_layered_runtime_config_file(
    *,
    repo_root: Path,
    market: str,
    system_config_path: str | Path | None = None,
    common_user_config_path: str | Path | None = None,
    include_common_user_config: bool = True,
    user_config_path: str | Path | None = None,
    output_config_path: str | Path | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    normalized_market = _normalize_market(market)
    cfg, meta = build_layered_runtime_config(
        repo_root=repo_root,
        market=normalized_market,
        system_config_path=system_config_path,
        common_user_config_path=common_user_config_path,
        include_common_user_config=include_common_user_config,
        user_config_path=user_config_path,
    )
    output_path = _resolve_path(
        output_config_path,
        default=default_output_config_path(repo_root=repo_root, market=normalized_market),
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
