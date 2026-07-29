from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from src.application.account_config import normalize_accounts
from src.application.agent_tool_contracts import AgentToolError
from src.application.config_authoring_transaction import config_source_sha256, publish_yaml_config_generation
from src.application.config_primitives import normalize_config_market, resolve_config_path
from src.application.config_yaml import (
    default_yaml_config_path,
    load_yaml_config_file,
)
from src.application.symbol_calibration import require_calibrated_symbol
from src.application.symbol_mutations import default_use_for_enabled_sides, set_path
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
    sell_put_max_strike: float | None = None,
    combo_yield_enabled: bool | None = None,
    rebuild_runtime_root: str | Path | None = None,
    apply: bool = False,
    backup: bool = True,
    expected_source_sha256: str | None = None,
) -> dict[str, Any]:
    if (
        covered_call_enabled is None
        and covered_call_min_strike is None
        and sell_put_enabled is None
        and sell_put_max_strike is None
        and combo_yield_enabled is None
    ):
        raise AgentToolError(
            code="INPUT_ERROR",
            message="at least one symbol setting is required",
            hint="Pass --covered-call-enabled, --covered-call-min-strike, --sell-put-enabled, --sell-put-max-strike, or --combo-yield-enabled.",
    )
    config_yaml_path = resolve_config_path(config_path, default=default_yaml_config_path(repo_root=repo_root))
    market_key = normalize_config_market(market)
    loaded_source_sha = config_source_sha256(config_yaml_path)
    after_doc = deepcopy(load_yaml_config_file(config_yaml_path))
    summary = _mutate_symbol_config(
        after_doc,
        market=market_key,
        symbol=symbol,
        covered_call_enabled=covered_call_enabled,
        covered_call_min_strike=covered_call_min_strike,
        sell_put_enabled=sell_put_enabled,
        sell_put_max_strike=sell_put_max_strike,
        combo_yield_enabled=combo_yield_enabled,
    )
    runtime_root = (
        Path(rebuild_runtime_root).expanduser().resolve()
        if rebuild_runtime_root is not None and str(rebuild_runtime_root).strip()
        else config_yaml_path.parent
    )
    transaction = publish_yaml_config_generation(
        repo_root=repo_root,
        config_yaml_path=config_yaml_path,
        config_doc=after_doc,
        runtime_root=runtime_root,
        markets=_markets_in_doc(after_doc),
        include_assistant=True,
        apply=bool(apply),
        backup=bool(backup),
        expected_source_sha256=expected_source_sha256 or loaded_source_sha,
    )
    validation = transaction["markets"]
    backup_path = transaction.get("backup_path")
    rebuild = transaction if apply else None
    payload = {
        "ok": True,
        "source_format": "yaml",
        "config_yaml_path": str(config_yaml_path),
        "market": market_key,
        "summary": summary,
        "validation": validation,
        "rebuild": rebuild,
        "source_revision": transaction.get("source_revision"),
        "audit_id": transaction.get("audit_id"),
    }
    return attach_write_contract(
        payload,
        dry_run=not bool(apply),
        write_applied=bool(apply),
        backup_path=backup_path,
        rollback_hint=f"restore {backup_path} to {config_yaml_path}" if backup_path else f"edit or restore {config_yaml_path}",
    )


def mutate_yaml_symbol_config(
    *,
    repo_root: Path,
    market: str,
    payload: dict[str, Any],
    config_path: str | Path | None = None,
    rebuild_runtime_root: str | Path | None = None,
    apply: bool = False,
    backup: bool = True,
    expected_source_sha256: str | None = None,
) -> dict[str, Any]:
    action = str(payload.get("action") or "").strip().lower()
    if action not in {"add", "edit", "remove"}:
        raise AgentToolError(code="INPUT_ERROR", message=f"unsupported manage_symbols action: {action}")
    market_key = normalize_config_market(market)
    config_yaml_path = resolve_config_path(config_path, default=default_yaml_config_path(repo_root=repo_root))
    loaded_source_sha = config_source_sha256(config_yaml_path)
    after_doc = deepcopy(load_yaml_config_file(config_yaml_path))
    summary = _mutate_generic_symbol_config(after_doc, market=market_key, action=action, payload=payload)
    runtime_root = (
        Path(rebuild_runtime_root).expanduser().resolve()
        if rebuild_runtime_root is not None and str(rebuild_runtime_root).strip()
        else config_yaml_path.parent
    )
    transaction = publish_yaml_config_generation(
        repo_root=repo_root,
        config_yaml_path=config_yaml_path,
        config_doc=after_doc,
        runtime_root=runtime_root,
        markets=_markets_in_doc(after_doc),
        include_assistant=True,
        apply=bool(apply),
        backup=bool(backup),
        expected_source_sha256=expected_source_sha256 or loaded_source_sha,
    )
    backup_path = transaction.get("backup_path")
    result = {
        "ok": True,
        "action": action,
        "source_format": "yaml",
        "config_yaml_path": str(config_yaml_path),
        "market": market_key,
        "summary": summary,
        "validation": transaction["markets"],
        "rebuild": transaction if apply else None,
        "source_revision": transaction.get("source_revision"),
        "audit_id": transaction.get("audit_id"),
    }
    return attach_write_contract(
        result,
        dry_run=not bool(apply),
        write_applied=bool(apply),
        backup_path=backup_path,
        rollback_hint=f"restore {backup_path} to {config_yaml_path}" if backup_path else f"edit or restore {config_yaml_path}",
    )


def _mutate_generic_symbol_config(
    config_doc: dict[str, Any],
    *,
    market: str,
    action: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    market_doc = _market_doc(config_doc, market=market)
    calibration = require_calibrated_symbol(
        str(payload.get("symbol") or ""),
        config=config_doc,
        error_factory=_input_error,
    )
    symbol = str(calibration.canonical_symbol or "")
    symbols = _symbols_list(market_doc, market=market)
    overrides = market_doc.get("overrides")
    if overrides is None:
        overrides = {}
        market_doc["overrides"] = overrides
    if not isinstance(overrides, dict):
        raise AgentToolError(code="CONFIG_ERROR", message=f"markets.{market}.overrides must be an object")
    existing = symbol in symbols

    if action == "remove":
        if not existing:
            raise AgentToolError(code="INPUT_ERROR", message=f"symbol not found: {symbol}")
        symbols.remove(symbol)
        overrides.pop(symbol, None)
        return {
            "action": "remove",
            "raw_symbol": str(payload.get("symbol") or "").strip(),
            "canonical_symbol": symbol,
            "calibration": calibration.public_payload(),
            "changed_paths": [f"markets.{market}.symbols", f"markets.{market}.overrides.{symbol}"],
        }

    if action == "add":
        if existing:
            raise AgentToolError(code="INPUT_ERROR", message=f"symbol already exists: {symbol}")
        override = _build_generic_add_override(config_doc, market=market, payload=payload)
        symbols.append(symbol)
        if override:
            overrides[symbol] = override
        return {
            "action": "add",
            "raw_symbol": str(payload.get("symbol") or "").strip(),
            "canonical_symbol": symbol,
            "calibration": calibration.public_payload(),
            "changed_paths": [f"markets.{market}.symbols[]", f"markets.{market}.overrides.{symbol}"],
            "entry": deepcopy(override),
        }

    symbol_added = False
    if not existing:
        if not bool(payload.get("upsert", False)):
            raise AgentToolError(code="INPUT_ERROR", message=f"symbol not found: {symbol}")
        symbols.append(symbol)
        symbol_added = True
    sets = payload.get("set")
    if not isinstance(sets, dict) or not sets:
        raise AgentToolError(code="INPUT_ERROR", message="edit requires non-empty set object")
    override = overrides.get(symbol)
    if override is None:
        override = {}
        overrides[symbol] = override
    if not isinstance(override, dict):
        raise AgentToolError(code="CONFIG_ERROR", message=f"markets.{market}.overrides.{symbol} must be an object")
    changed_paths: list[str] = []
    if symbol_added:
        changed_paths.append(f"markets.{market}.symbols[]")
    for raw_path, value in sets.items():
        authoring_path = _authoring_edit_path(override, str(raw_path))
        try:
            set_path(
                override,
                authoring_path,
                value,
                error_factory=lambda message: AgentToolError(code="INPUT_ERROR", message=message),
            )
        except AgentToolError:
            raise
        changed_paths.append(f"markets.{market}.overrides.{symbol}.{authoring_path}")
    if symbol_added and _enables_call_without_put(override):
        override.setdefault("sell_put", {})["enabled"] = False
        changed_paths.append(f"markets.{market}.overrides.{symbol}.sell_put.enabled")
    ensure_use = payload.get("ensure_use")
    if isinstance(ensure_use, list):
        current_use = _use_templates(override.get("use"))
        for raw_template in ensure_use:
            template = str(raw_template or "").strip()
            if template and template not in current_use:
                current_use.append(template)
        if current_use != _use_templates(override.get("use")):
            override["use"] = current_use
            changed_paths.append(f"markets.{market}.overrides.{symbol}.use")
    return {
        "action": "edit",
        "raw_symbol": str(payload.get("symbol") or "").strip(),
        "canonical_symbol": symbol,
        "calibration": calibration.public_payload(),
        "changed_paths": changed_paths,
        "entry": deepcopy(override),
    }


def _enables_call_without_put(override: dict[str, Any]) -> bool:
    call = override.get("covered_call")
    if not isinstance(call, dict):
        call = override.get("sell_call")
    return isinstance(call, dict) and call.get("enabled") is True and not isinstance(override.get("sell_put"), dict)


def _build_generic_add_override(
    config_doc: dict[str, Any],
    *,
    market: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    sell_put_enabled = bool(payload.get("sell_put_enabled", False))
    sell_call_enabled = bool(payload.get("sell_call_enabled", False))
    if sell_put_enabled:
        _require_payload_fields(payload, "sell_put_min_dte", "sell_put_max_dte")
    if sell_call_enabled:
        _require_payload_fields(payload, "sell_call_min_dte", "sell_call_max_dte")
    broker = str(payload.get("broker") or "").strip().upper()
    if broker and broker != market.upper():
        raise AgentToolError(
            code="INPUT_ERROR",
            message=f"broker {broker} conflicts with market {market}",
        )
    override: dict[str, Any] = {
        "fetch": {"limit_expirations": int(payload.get("limit_expirations") or 8)},
        "sell_put": {"enabled": sell_put_enabled},
        "covered_call": {"enabled": sell_call_enabled},
    }
    if sell_put_enabled:
        override["sell_put"].update(
            {
                "min_dte": int(payload["sell_put_min_dte"]),
                "max_dte": int(payload["sell_put_max_dte"]),
            }
        )
        for source, target in (
            ("sell_put_min_strike", "min_strike"),
            ("sell_put_max_strike", "max_strike"),
        ):
            if payload.get(source) is not None:
                override["sell_put"][target] = float(payload[source])
    if sell_call_enabled:
        override["covered_call"].update(
            {
                "min_dte": int(payload["sell_call_min_dte"]),
                "max_dte": int(payload["sell_call_max_dte"]),
            }
        )
        for source, target in (
            ("sell_call_min_strike", "min_strike"),
            ("sell_call_max_strike", "max_strike"),
        ):
            if payload.get(source) is not None:
                override["covered_call"][target] = float(payload[source])
    use = payload.get("use")
    if use is None:
        use = default_use_for_enabled_sides(
            sell_put_enabled=sell_put_enabled,
            sell_call_enabled=sell_call_enabled,
        )
    if use is not None:
        override["use"] = deepcopy(use)
    if payload.get("accounts") is not None:
        scoped_accounts = normalize_accounts(payload.get("accounts"), fallback=())
        market_accounts = set(_market_accounts(config_doc, market=market))
        unknown = [account for account in scoped_accounts if account not in market_accounts]
        if unknown:
            raise AgentToolError(
                code="INPUT_ERROR",
                message=f"symbol accounts are not enabled for market {market}: {', '.join(unknown)}",
            )
        override["accounts"] = scoped_accounts
    return override


def _require_payload_fields(payload: dict[str, Any], *fields: str) -> None:
    for field in fields:
        if payload.get(field) is None:
            raise AgentToolError(code="INPUT_ERROR", message=f"{field} is required")


def _authoring_edit_path(override: dict[str, Any], path: str) -> str:
    parts = [item.strip() for item in path.split(".")]
    if not parts or any(not item for item in parts):
        raise AgentToolError(code="INPUT_ERROR", message=f"invalid setting path: {path}")
    if parts[0] == "sell_call":
        parts[0] = "sell_call" if "sell_call" in override else "covered_call"
    elif parts[0] == "yield_enhancement":
        parts[0] = "combo_yield"
    return ".".join(parts)


def _market_accounts(config_doc: dict[str, Any], *, market: str) -> list[str]:
    market_doc = _market_doc(config_doc, market=market)
    raw = market_doc.get("accounts")
    if not isinstance(raw, list):
        raise AgentToolError(code="CONFIG_ERROR", message=f"markets.{market}.accounts must be a list")
    return normalize_accounts(raw, fallback=())


def _mutate_symbol_config(
    config_doc: dict[str, Any],
    *,
    market: str,
    symbol: str,
    covered_call_enabled: bool | None,
    covered_call_min_strike: float | None,
    sell_put_enabled: bool | None,
    sell_put_max_strike: float | None,
    combo_yield_enabled: bool | None,
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
    if sell_put_max_strike is not None:
        sell_put = override.get("sell_put")
        if not isinstance(sell_put, dict):
            sell_put = {}
            override["sell_put"] = sell_put
        if sell_put.get("max_strike") != sell_put_max_strike:
            sell_put["max_strike"] = sell_put_max_strike
            changed_paths.append(f"markets.{market}.overrides.{canonical_symbol}.sell_put.max_strike")

    if combo_yield_enabled is not None:
        combo_yield = override.get("combo_yield")
        if not isinstance(combo_yield, dict):
            combo_yield = {}
            override["combo_yield"] = combo_yield
        if combo_yield.get("enabled") is not bool(combo_yield_enabled):
            combo_yield["enabled"] = bool(combo_yield_enabled)
            changed_paths.append(f"markets.{market}.overrides.{canonical_symbol}.combo_yield.enabled")

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


def _input_error(message: str) -> AgentToolError:
    return AgentToolError(code="INPUT_ERROR", message=message)


__all__ = ["mutate_yaml_symbol_config", "set_yaml_symbol_config"]
