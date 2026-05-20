from __future__ import annotations

import hashlib
import shlex
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from src.application.agent_tool_contracts import AgentToolError
from src.application.config_validator import validate_config
from src.application.config_defaults import (
    DEFAULT_CONFIG_REF,
    default_config,
    default_config_sha256,
)
from src.application.layered_config import (
    MARKETS,
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
    "inbound",
    "notifications",
    "option_positions",
    "outputs",
    "portfolio",
    "runtime",
    "symbol_defaults",
    "templates",
    "watchdog",
}
ROOT_KEYS = {"accounts", "features", "markets", *PASSTHROUGH_KEYS}
MARKET_KEYS = {"accounts", "features", "overrides", "symbols", *PASSTHROUGH_KEYS}
WRITE_GATE_KEYS = {"write_gates", "write_permissions", "writes", "feishu_write", "feishu_writes"}


def default_yaml_config_path(*, repo_root: Path) -> Path:
    return (repo_root / "config.yaml").resolve()


def default_yaml_output_config_path(*, repo_root: Path, market: str, runtime_root: str | Path | None = None) -> Path:
    runtime = resolve_runtime_root(repo_root=repo_root, runtime_root=runtime_root)
    return (runtime.runtime_root / "resolved" / f"config.{market}.json").resolve()


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


def _deep_merge(base: Any, override: Any) -> Any:
    if isinstance(base, dict) and isinstance(override, dict):
        out = deepcopy(base)
        for key, value in override.items():
            out[key] = _deep_merge(out[key], value) if key in out else deepcopy(value)
        return out
    return deepcopy(override)


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _path_for_metadata(path: Path, *, repo_root: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return str(resolved)


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
    account = str(raw or "").strip().lower()
    if not account:
        raise AgentToolError(code="CONFIG_ERROR", message=f"{path} must be a non-empty account label")
    return account


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


def _normalize_symbols(raw_symbols: Any, *, path: str) -> list[str]:
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
        if symbol in seen:
            raise AgentToolError(code="CONFIG_ERROR", message=f"duplicate symbol in {path}: {symbol}")
        seen.add(symbol)
        out.append(symbol)
    return out


def _apply_range_shorthand(item: dict[str, Any], *, key: str, min_key: str, max_key: str, path: str) -> None:
    if key not in item:
        return
    raw = item.pop(key)
    if not isinstance(raw, list) or len(raw) != 2:
        raise AgentToolError(code="CONFIG_ERROR", message=f"{path}.{key} must be a two-item list")
    item[min_key] = raw[0]
    item[max_key] = raw[1]


def _normalize_strategy(raw: Any, *, path: str, allow_ranges: bool = True) -> dict[str, Any]:
    if isinstance(raw, bool):
        return {"enabled": raw}
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise AgentToolError(code="CONFIG_ERROR", message=f"{path} must be an object or boolean")
    out = deepcopy(raw)
    if allow_ranges:
        _apply_range_shorthand(out, key="dte", min_key="min_dte", max_key="max_dte", path=path)
        _apply_range_shorthand(out, key="strike", min_key="min_strike", max_key="max_strike", path=path)
    return out


def _normalize_symbol_override(raw: Any, *, path: str) -> dict[str, Any]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise AgentToolError(code="CONFIG_ERROR", message=f"{path} must be an object")
    out: dict[str, Any] = {}
    for raw_key, raw_value in raw.items():
        key = str(raw_key or "").strip()
        if key == "symbol":
            raise AgentToolError(code="CONFIG_ERROR", message=f"{path}.symbol is derived from the overrides key")
        if key in {"sell_put", "sell_call"}:
            out[key] = _normalize_strategy(raw_value, path=f"{path}.{key}", allow_ranges=True)
        elif key == "yield_enhancement":
            out[key] = _normalize_strategy(raw_value, path=f"{path}.{key}", allow_ranges=False)
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
        if key == "close_advice":
            close_advice = _normalize_strategy(raw_value, path=f"{path}.close_advice", allow_ranges=False)
            out["close_advice"] = close_advice
            continue
        if key == "yield_enhancement":
            raise AgentToolError(
                code="CONFIG_ERROR",
                message=f"{path}.yield_enhancement is not a global feature switch",
                hint="Enable yield enhancement per symbol under markets.<market>.overrides.<symbol>.yield_enhancement.",
            )
        if "write" in key or key in WRITE_GATE_KEYS:
            raise AgentToolError(
                code="CONFIG_ERROR",
                message=f"{path}.{key} is not a config.yaml feature",
                hint="Write permissions belong in options-monitor.env and still require command-level apply/confirm.",
            )
        raise AgentToolError(code="CONFIG_ERROR", message=f"{path}.{key} is not a supported feature")
    return out


def _copy_passthrough(data: dict[str, Any]) -> dict[str, Any]:
    return {key: deepcopy(data[key]) for key in PASSTHROUGH_KEYS if key in data}


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

    symbols = _normalize_symbols(market_cfg.get("symbols"), path=f"markets.{normalized_market}.symbols")
    overrides_raw = market_cfg.get("overrides") or {}
    if not isinstance(overrides_raw, dict):
        raise AgentToolError(code="CONFIG_ERROR", message=f"markets.{normalized_market}.overrides must be an object")

    overrides: dict[str, dict[str, Any]] = {}
    symbol_set = set(symbols)
    for raw_symbol, raw_override in overrides_raw.items():
        symbol = _normalize_symbol(raw_symbol, path=f"markets.{normalized_market}.overrides.<key>")
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

    out = _copy_passthrough(raw_cfg)
    out = _deep_merge(out, _copy_passthrough(market_cfg))
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
    cfg, meta = resolve_yaml_runtime_config(
        repo_root=repo_root,
        market=normalized_market,
        config_path=config_path,
        system_config_path=system_config_path,
    )
    exists, value = _path_get(cfg, parts)
    return {
        "ok": True,
        "source_format": "yaml",
        "market": normalized_market,
        "key": str(key),
        "exists": bool(exists),
        "value": value if exists else None,
        "source": "resolved_yaml" if exists else None,
        "runtime_path": str(key),
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
        "notes": [
            "config.yaml stores user overrides only; defaults are merged before runtime validation.",
            "Write permissions are not explained here because they live in options-monitor.env.",
        ],
        **meta,
    }


__all__ = [
    "RESOLVED_KEY",
    "build_yaml_runtime_config_file",
    "default_yaml_config_path",
    "default_yaml_output_config_path",
    "explain_yaml_config_key",
    "load_yaml_config_file",
    "resolve_yaml_runtime_config",
    "validate_yaml_runtime_config",
    "yaml_to_market_user_config",
]
