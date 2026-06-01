from __future__ import annotations

import shutil
import tempfile
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from src.application.agent_tool_contracts import AgentToolError
from src.application.config_primitives import normalize_config_market, resolve_config_path
from src.application.config_yaml import (
    build_yaml_assistant_config_file,
    build_yaml_runtime_config_file,
    default_yaml_config_path,
    load_yaml_config_file,
    validate_yaml_runtime_config,
)
from src.application.symbol_calibration import require_calibrated_symbol
from src.application.write_contract import attach_write_contract


def set_yaml_symbol_config(
    *,
    repo_root: Path,
    market: str,
    symbol: str,
    config_path: str | Path | None = None,
    covered_call_enabled: bool | None = None,
    covered_call_min_strike: float | None = None,
    sell_put_enabled: bool | None = None,
    rebuild_runtime_root: str | Path | None = None,
    apply: bool = False,
    backup: bool = True,
) -> dict[str, Any]:
    if covered_call_enabled is None and covered_call_min_strike is None and sell_put_enabled is None:
        raise AgentToolError(
            code="INPUT_ERROR",
            message="at least one symbol setting is required",
            hint="Pass --covered-call-enabled, --covered-call-min-strike, or --sell-put-enabled.",
        )
    config_yaml_path = resolve_config_path(config_path, default=default_yaml_config_path(repo_root=repo_root))
    market_key = normalize_config_market(market)
    after_doc = deepcopy(load_yaml_config_file(config_yaml_path))
    summary = _mutate_symbol_config(
        after_doc,
        market=market_key,
        symbol=symbol,
        covered_call_enabled=covered_call_enabled,
        covered_call_min_strike=covered_call_min_strike,
        sell_put_enabled=sell_put_enabled,
    )
    validation = _validate_doc(repo_root=repo_root, config_doc=after_doc, markets=_markets_in_doc(after_doc))
    backup_path = None
    if apply:
        if backup:
            backup_path = _backup_existing_config(config_yaml_path)
        _write_yaml_config(config_yaml_path, after_doc)
    rebuild = None
    if apply and rebuild_runtime_root:
        rebuild = _rebuild_runtime_configs(
            repo_root=repo_root,
            config_yaml_path=config_yaml_path,
            runtime_root=Path(rebuild_runtime_root).expanduser(),
            markets=_markets_in_doc(after_doc),
        )
    payload = {
        "ok": True,
        "source_format": "yaml",
        "config_yaml_path": str(config_yaml_path),
        "market": market_key,
        "summary": summary,
        "validation": validation,
        "rebuild": rebuild,
    }
    return attach_write_contract(
        payload,
        dry_run=not bool(apply),
        write_applied=bool(apply),
        backup_path=backup_path,
        rollback_hint=f"restore {backup_path} to {config_yaml_path}" if backup_path else f"edit or restore {config_yaml_path}",
    )


def _mutate_symbol_config(
    config_doc: dict[str, Any],
    *,
    market: str,
    symbol: str,
    covered_call_enabled: bool | None,
    covered_call_min_strike: float | None,
    sell_put_enabled: bool | None,
) -> dict[str, Any]:
    market_doc = _market_doc(config_doc, market=market)
    calibration = require_calibrated_symbol(symbol, config=config_doc, error_factory=_input_error)
    canonical_symbol = str(calibration.canonical_symbol)
    symbols = _symbols_list(market_doc, market=market)
    symbol_added = False
    if canonical_symbol not in symbols:
        symbols.append(canonical_symbol)
        symbol_added = True

    overrides = market_doc.get("overrides")
    if overrides is None:
        overrides = {}
        market_doc["overrides"] = overrides
    if not isinstance(overrides, dict):
        raise AgentToolError(code="CONFIG_ERROR", message=f"markets.{market}.overrides must be an object")

    override = overrides.get(canonical_symbol)
    if override is None:
        override = {}
        overrides[canonical_symbol] = override
    if not isinstance(override, dict):
        raise AgentToolError(code="CONFIG_ERROR", message=f"markets.{market}.overrides.{canonical_symbol} must be an object")

    changed_paths: list[str] = []
    if symbol_added:
        changed_paths.append(f"markets.{market}.symbols[]")

    call_enabled_effective = covered_call_enabled
    if covered_call_min_strike is not None and call_enabled_effective is None:
        call_enabled_effective = True
    call_only = _should_disable_sell_put_for_call_only(
        override=override,
        symbol_added=symbol_added,
        covered_call_enabled=call_enabled_effective,
    )
    if call_only and sell_put_enabled is None:
        sell_put_enabled = False

    if sell_put_enabled is not None:
        sell_put = override.get("sell_put")
        if not isinstance(sell_put, dict):
            sell_put = {}
            override["sell_put"] = sell_put
        if sell_put.get("enabled") is not bool(sell_put_enabled):
            sell_put["enabled"] = bool(sell_put_enabled)
            changed_paths.append(f"markets.{market}.overrides.{canonical_symbol}.sell_put.enabled")

    call_key = _covered_call_authoring_key(override)
    if call_enabled_effective is not None or covered_call_min_strike is not None:
        call_cfg = override.get(call_key)
        if not isinstance(call_cfg, dict):
            call_cfg = {}
            override[call_key] = call_cfg
        if call_enabled_effective is not None and call_cfg.get("enabled") is not bool(call_enabled_effective):
            call_cfg["enabled"] = bool(call_enabled_effective)
            changed_paths.append(f"markets.{market}.overrides.{canonical_symbol}.{call_key}.enabled")
        if covered_call_min_strike is not None and call_cfg.get("min_strike") != covered_call_min_strike:
            call_cfg["min_strike"] = covered_call_min_strike
            changed_paths.append(f"markets.{market}.overrides.{canonical_symbol}.{call_key}.min_strike")

    use_changed = _sync_use_templates(
        override,
        covered_call_enabled=call_enabled_effective,
        sell_put_enabled=sell_put_enabled,
        new_symbol=symbol_added,
    )
    if use_changed:
        changed_paths.append(f"markets.{market}.overrides.{canonical_symbol}.use")

    return {
        "action": "set",
        "raw_symbol": str(symbol or "").strip(),
        "canonical_symbol": canonical_symbol,
        "calibration": calibration.public_payload(),
        "symbol_added": symbol_added,
        "changed_paths": changed_paths,
        "entry": deepcopy(override),
    }


def _market_doc(config_doc: dict[str, Any], *, market: str) -> dict[str, Any]:
    markets = config_doc.get("markets")
    if not isinstance(markets, dict):
        raise AgentToolError(code="CONFIG_ERROR", message="config.yaml markets must be an object")
    market_doc = markets.get(market)
    if not isinstance(market_doc, dict):
        raise AgentToolError(code="CONFIG_ERROR", message=f"config.yaml missing markets.{market}")
    return market_doc


def _symbols_list(market_doc: dict[str, Any], *, market: str) -> list[str]:
    raw_symbols = market_doc.get("symbols")
    if raw_symbols is None:
        raw_symbols = []
        market_doc["symbols"] = raw_symbols
    if not isinstance(raw_symbols, list):
        raise AgentToolError(code="CONFIG_ERROR", message=f"markets.{market}.symbols must be a list")
    for item in raw_symbols:
        if not isinstance(item, str):
            raise AgentToolError(code="CONFIG_ERROR", message=f"markets.{market}.symbols must contain only strings")
    return raw_symbols


def _should_disable_sell_put_for_call_only(
    *,
    override: dict[str, Any],
    symbol_added: bool,
    covered_call_enabled: bool | None,
) -> bool:
    if covered_call_enabled is not True:
        return False
    if symbol_added:
        return True
    sell_put = override.get("sell_put")
    if isinstance(sell_put, dict) and sell_put:
        return False
    use = _use_templates(override.get("use"))
    return "call_base" in use and "put_base" not in use


def _covered_call_authoring_key(override: dict[str, Any]) -> str:
    if "covered_call" in override and "sell_call" in override:
        raise AgentToolError(
            code="CONFIG_ERROR",
            message="config.yaml override cannot define both covered_call and sell_call",
            hint="Use covered_call in config.yaml; sell_call is kept for legacy configs.",
        )
    if "sell_call" in override:
        return "sell_call"
    return "covered_call"


def _sync_use_templates(
    override: dict[str, Any],
    *,
    covered_call_enabled: bool | None,
    sell_put_enabled: bool | None,
    new_symbol: bool,
) -> bool:
    if covered_call_enabled is None and sell_put_enabled is None and not new_symbol:
        return False
    use = _use_templates(override.get("use"))
    original = list(use)
    if covered_call_enabled is True and "call_base" not in use:
        use.append("call_base")
    if covered_call_enabled is False:
        use = [item for item in use if item != "call_base"]
    if sell_put_enabled is True and "put_base" not in use:
        use.append("put_base")
    if sell_put_enabled is False:
        use = [item for item in use if item != "put_base"]
    if new_symbol and not use and covered_call_enabled is True:
        use = ["call_base"]
    if use == original:
        return False
    override["use"] = use
    return True


def _use_templates(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    raise AgentToolError(code="CONFIG_ERROR", message="symbol use must be a string or list")


def _markets_in_doc(config_doc: dict[str, Any]) -> list[str]:
    markets = config_doc.get("markets")
    if not isinstance(markets, dict):
        return []
    out = []
    for market in ("us", "hk"):
        if isinstance(markets.get(market), dict):
            out.append(market)
    return out


def _validate_doc(*, repo_root: Path, config_doc: dict[str, Any], markets: list[str]) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="om-config-symbol-") as temp_dir:
        temp_path = Path(temp_dir) / "config.yaml"
        _write_yaml_config(temp_path, config_doc)
        out: dict[str, Any] = {}
        for market in markets:
            out[market] = validate_yaml_runtime_config(
                repo_root=repo_root,
                market=market,
                config_path=temp_path,
            )
        return out


def _rebuild_runtime_configs(
    *,
    repo_root: Path,
    config_yaml_path: Path,
    runtime_root: Path,
    markets: list[str],
) -> dict[str, Any]:
    runtime_root = runtime_root.resolve()
    out: dict[str, Any] = {"runtime_root": str(runtime_root), "markets": {}, "assistant": None}
    for market in markets:
        output = runtime_root / f"config.{market}.json"
        build = build_yaml_runtime_config_file(
            repo_root=repo_root,
            market=market,
            config_path=config_yaml_path,
            output_config_path=output,
            dry_run=False,
        )
        validate = validate_yaml_runtime_config(
            repo_root=repo_root,
            market=market,
            config_path=config_yaml_path,
        )
        out["markets"][market] = {
            "build": build,
            "validate": validate,
        }
    assistant_output = runtime_root / "resolved" / "config.assistant.json"
    out["assistant"] = build_yaml_assistant_config_file(
        repo_root=repo_root,
        config_path=config_yaml_path,
        output_config_path=assistant_output,
        dry_run=False,
    )
    return out


def _backup_existing_config(path: Path) -> Path | None:
    if not path.exists():
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = path.with_name(f"{path.name}.bak.{stamp}")
    shutil.copy2(path, backup_path)
    return backup_path


def _write_yaml_config(path: Path, config_doc: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump(config_doc, allow_unicode=True, sort_keys=False, default_flow_style=False)
    if not text.endswith("\n"):
        text += "\n"
    path.write_text(text, encoding="utf-8")


def _input_error(message: str) -> AgentToolError:
    return AgentToolError(code="INPUT_ERROR", message=message)


__all__ = ["set_yaml_symbol_config"]
