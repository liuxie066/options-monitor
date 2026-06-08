from __future__ import annotations

import json
import shutil
from copy import deepcopy
from datetime import datetime, timezone
from json import JSONDecodeError
from pathlib import Path
from typing import Any

from src.application.agent_tool_contracts import AgentToolError
from src.application.config_defaults import DEFAULT_CONFIG_REF, default_config
from src.application.config_primitives import MARKETS, deep_merge_config as _deep_merge
from src.application.config_primitives import dump_yaml as _dump_yaml
from src.application.config_primitives import resolve_config_path as _resolve_path
from src.application.config_validator import validate_config
from src.application.config_yaml import (
    MARKET_KEYS,
    PASSTHROUGH_KEYS,
    ROOT_KEYS,
    WRITE_GATE_KEYS,
    build_yaml_runtime_config_file,
    runtime_strategy_keys_to_yaml_authoring,
    validate_yaml_runtime_config,
    yaml_to_market_user_config,
)
from src.infrastructure.io_utils import atomic_write_text
from src.application.layered_config import (
    build_layered_runtime_config_from_user_config,
    default_common_user_config_path,
    default_user_config_path,
)
from src.application.runtime_config_freshness import GENERATED_KEY
from src.application.write_contract import attach_write_contract


SKIP_ROOT_KEYS = {"account_settings", "accounts", "defaults", "markets", "symbols"}
SKIP_MARKET_KEYS = {"account_settings", "accounts", "symbols"}


def _read_json_object(path: Path, *, label: str, required: bool = True) -> dict[str, Any] | None:
    if not path.exists():
        if required:
            raise AgentToolError(
                code="CONFIG_ERROR",
                message=f"{label} not found: {path}",
                hint="Pass the explicit path or create the layered JSON user config first.",
            )
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except JSONDecodeError as exc:
        raise AgentToolError(
            code="CONFIG_ERROR",
            message=f"failed to parse {label}: {path}:{exc.lineno}:{exc.colno}",
            details={"line": int(exc.lineno), "column": int(exc.colno), "error": str(exc)},
        ) from exc
    if not isinstance(payload, dict):
        raise AgentToolError(code="CONFIG_ERROR", message=f"{label} must be a JSON object: {path}")
    return payload


def _normalize_account_labels(raw: Any, *, path: str) -> list[str] | None:
    if raw is None:
        return None
    values = raw if isinstance(raw, list) else [raw]
    out: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(values):
        parts = [part for part in str(item or "").replace(",", " ").split() if part]
        if not parts:
            raise AgentToolError(code="CONFIG_ERROR", message=f"{path}[{index}] must be a non-empty account label")
        for part in parts:
            account = part.strip().lower()
            if not account:
                raise AgentToolError(code="CONFIG_ERROR", message=f"{path}[{index}] must be a non-empty account label")
            if account in seen:
                raise AgentToolError(code="CONFIG_ERROR", message=f"duplicate account in {path}: {account}")
            seen.add(account)
            out.append(account)
    if not out:
        raise AgentToolError(code="CONFIG_ERROR", message=f"{path} must include at least one account")
    return out


def _account_setting_to_yaml(raw: dict[str, Any]) -> dict[str, Any]:
    item = deepcopy(raw)
    futu = item.pop("futu", None)
    account_id = None
    if isinstance(futu, dict):
        account_id = str(futu.pop("account_id", "") or "").strip() or None

    out: dict[str, Any] = {}
    if "type" in item:
        out["type"] = item.pop("type")
    if account_id:
        out["futu_account_id"] = account_id
    if isinstance(futu, dict) and futu:
        out["futu"] = futu
    for key, value in item.items():
        out[key] = value
    return out


def _copy_supported_fields(
    raw: dict[str, Any],
    *,
    allowed: set[str],
    skip: set[str],
    path: str,
    warnings: list[str],
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for raw_key, value in raw.items():
        key = str(raw_key or "").strip()
        if key in skip:
            continue
        if key == "features":
            out[key] = deepcopy(value)
            continue
        if key in WRITE_GATE_KEYS or key == "trade_intake":
            warnings.append(f"{path}.{key} omitted: write policy belongs to env gates or command-level apply/confirm")
            continue
        if key in allowed and key in PASSTHROUGH_KEYS:
            out[key] = (
                runtime_strategy_keys_to_yaml_authoring(value)
                if key in {"alert_policy", "symbol_defaults", "templates"}
                else deepcopy(value)
            )
            continue
        if key in allowed:
            out[key] = deepcopy(value)
            continue
        warnings.append(f"{path}.{key} omitted: no config.yaml mapping")
    return out


def _normalize_legacy_agent_config(raw: dict[str, Any] | None, *, warnings: list[str]) -> dict[str, Any] | None:
    if raw is None or "agent" not in raw:
        return raw
    out = deepcopy(raw)
    legacy_agent = out.pop("agent")
    if not isinstance(legacy_agent, dict):
        warnings.append("configs/user.common.json.agent omitted: expected object")
        return out

    assistant = out.get("assistant")
    assistant_cfg = deepcopy(assistant) if isinstance(assistant, dict) else {}
    legacy_runtime = legacy_agent.get("runtime")
    runtime_cfg = legacy_runtime if isinstance(legacy_runtime, dict) else {}
    legacy_llm = legacy_agent.get("llm")
    llm_cfg = legacy_llm if isinstance(legacy_llm, dict) else {}

    legacy_llm_enabled = llm_cfg.get("enabled") if isinstance(llm_cfg.get("enabled"), bool) else None
    legacy_runtime_enabled = runtime_cfg.get("enabled") if isinstance(runtime_cfg.get("enabled"), bool) else None
    if "enabled" not in assistant_cfg and legacy_runtime_enabled is not None:
        assistant_cfg["enabled"] = bool(legacy_runtime_enabled)
    if "planner" not in assistant_cfg and legacy_llm_enabled is not None:
        assistant_cfg["planner"] = {"enabled": bool(legacy_llm_enabled)}

    if "context_window_messages" not in assistant_cfg and "context_window_messages" in runtime_cfg:
        assistant_cfg["context_window_messages"] = deepcopy(runtime_cfg["context_window_messages"])
    if "default_market_scope" not in assistant_cfg and "default_market_scope" in runtime_cfg:
        assistant_cfg["default_market_scope"] = deepcopy(runtime_cfg["default_market_scope"])

    clean_llm = {key: deepcopy(value) for key, value in llm_cfg.items() if key != "enabled"}
    if clean_llm:
        existing_llm = assistant_cfg.get("llm")
        assistant_cfg["llm"] = _deep_merge(clean_llm, existing_llm if isinstance(existing_llm, dict) else {})

    if assistant_cfg:
        out["assistant"] = assistant_cfg
    warnings.append("configs/user.common.json.agent migrated to assistant")
    return out


def _normalize_legacy_combo_yield_keys(raw: dict[str, Any] | None) -> dict[str, Any] | None:
    if raw is None:
        return None
    out = deepcopy(raw)

    def _normalize_container(container: dict[str, Any]) -> None:
        if "yield_enhancement" in container and "combo_yield" not in container:
            container["combo_yield"] = container.pop("yield_enhancement")
        for key in ("symbols",):
            symbols = container.get(key)
            if isinstance(symbols, list):
                for item in symbols:
                    if isinstance(item, dict):
                        _normalize_container(item)
        for key in ("templates", "symbol_defaults", "overrides"):
            nested = container.get(key)
            if isinstance(nested, dict):
                for item in nested.values():
                    if isinstance(item, dict):
                        _normalize_container(item)

    _normalize_container(out)
    return out


def _compact_range(raw: dict[str, Any], *, min_key: str, max_key: str, target_key: str) -> None:
    if min_key not in raw or max_key not in raw:
        return
    min_value = raw.pop(min_key)
    max_value = raw.pop(max_key)
    raw[target_key] = [min_value, max_value]


def _compact_strategy(raw: Any) -> Any:
    if not isinstance(raw, dict):
        return deepcopy(raw)
    out = deepcopy(raw)
    _compact_range(out, min_key="min_dte", max_key="max_dte", target_key="dte")
    _compact_range(out, min_key="min_strike", max_key="max_strike", target_key="strike")
    return out


def _compact_boolean_enabled(raw: Any) -> Any:
    if isinstance(raw, dict) and set(raw.keys()) == {"enabled"}:
        return bool(raw.get("enabled"))
    return deepcopy(raw)


def _symbol_item_to_yaml(raw: Any, *, path: str) -> tuple[str, dict[str, Any]]:
    if isinstance(raw, str):
        symbol = str(raw).strip().upper()
        if not symbol:
            raise AgentToolError(code="CONFIG_ERROR", message=f"{path} must be a non-empty symbol")
        return symbol, {}
    if not isinstance(raw, dict):
        raise AgentToolError(code="CONFIG_ERROR", message=f"{path} must be a symbol object")
    item = deepcopy(raw)
    symbol = str(item.pop("symbol", "") or "").strip().upper()
    if not symbol:
        raise AgentToolError(code="CONFIG_ERROR", message=f"{path}.symbol is required")

    for key in ("sell_put", "sell_call"):
        if key in item:
            item[key] = _compact_strategy(item[key])
    if "yield_enhancement" in item and "combo_yield" not in item:
        item["combo_yield"] = item.pop("yield_enhancement")
    if "combo_yield" in item:
        item["combo_yield"] = _compact_boolean_enabled(item["combo_yield"])
    return symbol, runtime_strategy_keys_to_yaml_authoring(item)


def _market_payload_from_user_config(
    raw: dict[str, Any],
    *,
    market: str,
    accounts: list[str],
    warnings: list[str],
) -> dict[str, Any]:
    out = _copy_supported_fields(
        raw,
        allowed=MARKET_KEYS,
        skip=SKIP_MARKET_KEYS,
        path=f"configs/user.{market}.json",
        warnings=warnings,
    )
    out["accounts"] = list(accounts)

    raw_symbols = raw.get("symbols")
    if not isinstance(raw_symbols, list) or not raw_symbols:
        raise AgentToolError(code="CONFIG_ERROR", message=f"configs/user.{market}.json symbols[] is required")

    symbols: list[str] = []
    overrides: dict[str, Any] = {}
    for index, raw_symbol in enumerate(raw_symbols):
        symbol, override = _symbol_item_to_yaml(raw_symbol, path=f"configs/user.{market}.json.symbols[{index}]")
        if symbol in symbols:
            raise AgentToolError(code="CONFIG_ERROR", message=f"duplicate symbol in configs/user.{market}.json: {symbol}")
        symbols.append(symbol)
        if override:
            overrides[symbol] = override

    out["symbols"] = symbols
    if overrides:
        out["overrides"] = overrides
    return out


def _runtime_without_generated(cfg: dict[str, Any]) -> dict[str, Any]:
    out = deepcopy(cfg)
    out.pop(GENERATED_KEY, None)
    return out


def _backup_existing_config(path: Path) -> Path | None:
    if not path.exists():
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    backup_path = path.with_name(f"{path.name}.bak.{stamp}")
    shutil.copy2(path, backup_path)
    return backup_path


def _post_write_validation(*, repo_root: Path, config_path: Path) -> dict[str, Any]:
    validation: dict[str, Any] = {}
    for market in MARKETS:
        validate_result = validate_yaml_runtime_config(
            repo_root=repo_root,
            market=market,
            config_path=config_path,
        )
        build_result = build_yaml_runtime_config_file(
            repo_root=repo_root,
            market=market,
            config_path=config_path,
            dry_run=True,
        )
        validation[market] = {
            "ok": True,
            "source_format": validate_result.get("source_format"),
            "config_yaml_path": validate_result.get("config_yaml_path"),
            "config_yaml_sha256": validate_result.get("config_yaml_sha256"),
            "dry_run": bool(build_result.get("dry_run")),
            "write_applied": bool(build_result.get("write_applied")),
            "output_config_path": build_result.get("output_config_path"),
        }
    return validation


def preview_config_yaml_migration(
    *,
    repo_root: Path,
    common_user_config_path: str | Path | None = None,
    include_common_user_config: bool = True,
    us_user_config_path: str | Path | None = None,
    hk_user_config_path: str | Path | None = None,
    us_accounts: list[str] | str | None = None,
    hk_accounts: list[str] | str | None = None,
    output_config_yaml_path: str | Path | None = None,
    apply: bool = False,
    backup: bool = True,
) -> dict[str, Any]:
    common_path = _resolve_path(common_user_config_path, default=default_common_user_config_path(repo_root=repo_root))
    common_cfg = (
        _read_json_object(common_path, label="common user config", required=False)
        if include_common_user_config
        else None
    )
    common_loaded = common_cfg is not None

    user_paths = {
        "us": _resolve_path(us_user_config_path, default=default_user_config_path(repo_root=repo_root, market="us")),
        "hk": _resolve_path(hk_user_config_path, default=default_user_config_path(repo_root=repo_root, market="hk")),
    }
    user_cfgs = {
        market: _read_json_object(path, label=f"{market} user config", required=True)
        for market, path in user_paths.items()
    }

    warnings: list[str] = []
    common_cfg = _normalize_legacy_combo_yield_keys(
        _normalize_legacy_agent_config(common_cfg, warnings=warnings)
    )
    user_cfgs = {
        market: _normalize_legacy_combo_yield_keys(cfg) or {}
        for market, cfg in user_cfgs.items()
    }
    old_runtime: dict[str, dict[str, Any]] = {}
    legacy_market_accounts: dict[str, list[str]] = {}
    for market in MARKETS:
        cfg, meta = build_layered_runtime_config_from_user_config(
            repo_root=repo_root,
            market=market,
            user_config=user_cfgs[market],
            common_user_config=common_cfg if common_loaded else None,
            common_user_config_ref=str(common_path) if common_loaded else None,
            user_config_ref=str(user_paths[market]),
        )
        old_runtime[market] = cfg
        legacy_market_accounts[market] = [str(item) for item in meta.get("accounts", [])]
        if "accounts" not in user_cfgs[market]:
            warnings.append(
                f"markets.{market}.accounts inferred from legacy effective config: "
                f"{', '.join(legacy_market_accounts[market])}; "
                f"pass --{market}-accounts to preview an explicit target"
            )

    account_overrides = {
        "us": _normalize_account_labels(us_accounts, path="--us-accounts"),
        "hk": _normalize_account_labels(hk_accounts, path="--hk-accounts"),
    }

    account_settings: dict[str, dict[str, Any]] = {}
    common_account_settings = common_cfg.get("account_settings") if isinstance(common_cfg, dict) else {}
    if not isinstance(common_account_settings, dict):
        common_account_settings = {}
        if common_cfg is not None:
            warnings.append("configs/user.common.json.account_settings omitted: expected object")

    market_account_settings: dict[str, dict[str, Any]] = {}
    for market in MARKETS:
        raw_market_settings = user_cfgs[market].get("account_settings")
        market_account_settings[market] = raw_market_settings if isinstance(raw_market_settings, dict) else {}

    available_accounts = set(common_account_settings)
    for market in MARKETS:
        available_accounts.update(market_account_settings[market])
        available_accounts.update(legacy_market_accounts[market])

    market_accounts: dict[str, list[str]] = {}
    for market in MARKETS:
        override = account_overrides[market]
        if override is not None:
            unknown = [account for account in override if account not in available_accounts]
            if unknown:
                raise AgentToolError(
                    code="CONFIG_ERROR",
                    message=f"--{market}-accounts references undefined accounts: {', '.join(unknown)}",
                )
            market_accounts[market] = override
            if override != legacy_market_accounts[market]:
                warnings.append(
                    f"markets.{market}.accounts overridden from "
                    f"{', '.join(legacy_market_accounts[market])} to {', '.join(override)}"
                )
        else:
            market_accounts[market] = list(legacy_market_accounts[market])

    for market in MARKETS:
        market_settings = market_account_settings[market]
        for account in market_accounts[market]:
            merged = _deep_merge(common_account_settings.get(account, {}), market_settings.get(account, {}))
            if not isinstance(merged, dict) or not merged:
                warnings.append(f"accounts.{account} inferred as empty futu account from legacy effective config")
                merged = {"type": "futu"}
            account_settings[account] = _deep_merge(account_settings.get(account, {}), merged)

    yaml_doc: dict[str, Any] = {
        "accounts": {
            account: _account_setting_to_yaml(setting)
            for account, setting in account_settings.items()
        }
    }
    if common_cfg is not None:
        yaml_doc = _deep_merge(
            yaml_doc,
            _copy_supported_fields(
                common_cfg,
                allowed=ROOT_KEYS,
                skip=SKIP_ROOT_KEYS,
                path="configs/user.common.json",
                warnings=warnings,
            ),
        )

    yaml_doc["markets"] = {}
    for market in MARKETS:
        yaml_doc["markets"][market] = _market_payload_from_user_config(
            user_cfgs[market],
            market=market,
            accounts=market_accounts[market],
            warnings=warnings,
        )

    validation: dict[str, Any] = {}
    for market in MARKETS:
        user_cfg = yaml_to_market_user_config(yaml_doc, market=market)
        new_cfg, _meta = build_layered_runtime_config_from_user_config(
            repo_root=repo_root,
            market=market,
            user_config=user_cfg,
            system_config=default_config(),
            system_config_ref=DEFAULT_CONFIG_REF,
            common_user_config=None,
            common_user_config_ref=None,
            user_config_ref="config_yaml_migration_preview",
        )
        validate_config(deepcopy(new_cfg))
        equivalent = _runtime_without_generated(old_runtime[market]) == _runtime_without_generated(new_cfg)
        if not equivalent:
            warnings.append(f"markets.{market} preview is valid but not byte-equivalent to legacy layered runtime config")
        validation[market] = {
            "ok": True,
            "equivalent_to_legacy_runtime": bool(equivalent),
            "legacy_accounts": legacy_market_accounts[market],
            "accounts": market_accounts[market],
            "symbols": [str(item.get("symbol") or "") for item in new_cfg.get("symbols", []) if isinstance(item, dict)],
        }

    output_path = _resolve_path(output_config_yaml_path, default=repo_root / "config.yaml")
    yaml_text = _dump_yaml(yaml_doc)
    should_apply = bool(apply)
    backup_path = None
    post_write_validation = None
    if should_apply:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if backup:
            backup_path = _backup_existing_config(output_path)
        atomic_write_text(output_path, yaml_text, encoding="utf-8")
        post_write_validation = _post_write_validation(repo_root=repo_root, config_path=output_path)

    return attach_write_contract(
        {
            "ok": all(bool(item.get("ok")) for item in validation.values()),
            "output_config_yaml_path": str(output_path),
            "post_write_validation": post_write_validation,
            "sources": {
                "common_user_config_path": str(common_path) if common_loaded else None,
                "common_user_config_loaded": bool(common_loaded),
                "us_user_config_path": str(user_paths["us"]),
                "hk_user_config_path": str(user_paths["hk"]),
            },
            "validation": validation,
            "warnings": warnings,
            "yaml": yaml_text,
        },
        dry_run=not should_apply,
        write_applied=should_apply,
        backup_path=backup_path,
        rollback_hint=f"restore {backup_path} to {output_path}" if backup_path else f"delete {output_path} or rerun migration",
    )


__all__ = ["preview_config_yaml_migration"]
