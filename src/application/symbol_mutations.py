from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any

from src.application.symbol_calibration import (
    SymbolCalibrationResult,
    calibrate_symbol,
    canonical_symbol_for_write,
    require_calibrated_symbol,
)


@dataclass(frozen=True)
class SymbolMutationSummary:
    action: str
    raw_symbol: str
    canonical_symbol: str
    calibration: dict[str, Any]
    existing_symbol: str | None
    changed_paths: list[str]
    entry: dict[str, Any] | None

    def public_payload(self) -> dict[str, Any]:
        return asdict(self)


def normalize_symbol(value: str, *, config: dict[str, Any] | None = None) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    result = calibrate_symbol(raw, config=config)
    return str(result.canonical_symbol) if result.canonical_symbol else raw.upper()


def normalize_symbol_read(value: Any, *, config: dict[str, Any] | None = None) -> str:
    return normalize_symbol(str(value or ""), config=config)


def ensure_symbols_list(
    cfg: dict[str, Any],
    *,
    error_factory: Callable[[str], Exception] = ValueError,
) -> list[dict[str, Any]]:
    symbols = cfg.get("symbols")
    if symbols is None:
        cfg["symbols"] = []
        symbols = cfg["symbols"]
    if not isinstance(symbols, list):
        raise error_factory("config symbols must be a list")
    return symbols


def find_symbol_entry(
    cfg: dict[str, Any],
    symbol: str,
    *,
    resolve_watchlist_config: Callable[[dict[str, Any]], list[dict[str, Any]]] | None = None,
) -> tuple[int | None, dict[str, Any] | None]:
    needle = normalize_symbol(symbol, config=cfg)
    rows = resolve_watchlist_config(cfg) if resolve_watchlist_config is not None else _symbols_rows(cfg)
    for idx, item in enumerate(rows):
        if normalize_symbol(str(item.get("symbol") or ""), config=cfg) == needle:
            return idx, item
    return None, None


def add_symbol_entry(
    cfg: dict[str, Any],
    *,
    symbol: str,
    use: str | list[str] | tuple[str, ...] | None = None,
    limit_expirations: int = 8,
    sell_put_enabled: bool = False,
    sell_call_enabled: bool = False,
    accounts: list[str] | tuple[str, ...] | None = None,
    normalize_accounts: Callable[[Any], list[str]] | None = None,
    error_factory: Callable[[str], Exception] = ValueError,
) -> SymbolMutationSummary:
    calibration = require_calibrated_symbol(symbol, config=cfg, error_factory=error_factory)
    canonical = str(calibration.canonical_symbol)
    _, existing = find_symbol_entry(cfg, canonical)
    if existing is not None:
        raise error_factory(f"监控标的已存在：{canonical}")
    entry: dict[str, Any] = {
        "symbol": canonical,
        "fetch": {"limit_expirations": int(limit_expirations)},
        "sell_put": _strategy_defaults(enabled=bool(sell_put_enabled)),
        "sell_call": _strategy_defaults(enabled=bool(sell_call_enabled)),
    }
    if use is None:
        use = default_use_for_enabled_sides(
            sell_put_enabled=bool(sell_put_enabled),
            sell_call_enabled=bool(sell_call_enabled),
        )
    if use is not None and str(use).strip():
        entry["use"] = use
    if accounts is not None:
        entry["accounts"] = normalize_accounts(accounts) if normalize_accounts is not None else list(accounts)
    ensure_symbols_list(cfg, error_factory=error_factory).append(entry)
    return SymbolMutationSummary("add", str(symbol or "").strip(), canonical, calibration.public_payload(), None, [f"symbols[].{canonical}"], dict(entry))


def remove_symbol_entry(
    cfg: dict[str, Any],
    *,
    symbol: str,
    error_factory: Callable[[str], Exception] = ValueError,
) -> SymbolMutationSummary:
    calibration = require_calibrated_symbol(symbol, config=cfg, error_factory=error_factory)
    canonical = str(calibration.canonical_symbol)
    idx, existing = find_symbol_entry(cfg, canonical)
    if idx is None or existing is None:
        raise error_factory(f"监控标的不存在：{canonical}")
    ensure_symbols_list(cfg, error_factory=error_factory).pop(idx)
    return SymbolMutationSummary("remove", str(symbol or "").strip(), canonical, calibration.public_payload(), str(existing.get("symbol") or canonical), [f"symbols[].{canonical}"], dict(existing))


def edit_symbol_entry(
    cfg: dict[str, Any],
    *,
    symbol: str,
    sets: dict[str, Any],
    ensure_use: list[str] | tuple[str, ...] | None = None,
    error_factory: Callable[[str], Exception] = ValueError,
) -> SymbolMutationSummary:
    calibration = require_calibrated_symbol(symbol, config=cfg, error_factory=error_factory)
    canonical = str(calibration.canonical_symbol)
    idx, existing = find_symbol_entry(cfg, canonical)
    if idx is None or existing is None:
        raise error_factory(f"监控标的不存在：{canonical}")
    if not sets:
        raise error_factory("edit requires at least one field patch")
    entry = dict(existing)
    changed_paths: list[str] = []
    for key, value in sets.items():
        path = str(key).strip()
        if not path:
            raise error_factory("set path cannot be empty")
        set_path(entry, path, value, error_factory=error_factory)
        changed_paths.append(path)
    changed_paths.extend(_ensure_enabled_strategy_defaults(entry, "sell_put"))
    changed_paths.extend(_ensure_enabled_strategy_defaults(entry, "sell_call"))
    if ensure_use:
        if ensure_use_templates(entry, ensure_use, error_factory=error_factory):
            changed_paths.append("use")
    ensure_symbols_list(cfg, error_factory=error_factory)[idx] = entry
    return SymbolMutationSummary("edit", str(symbol or "").strip(), canonical, calibration.public_payload(), str(existing.get("symbol") or canonical), changed_paths, dict(entry))


def canonical_symbol_for_config_write(
    cfg: dict[str, Any],
    symbol: str,
    *,
    error_factory: Callable[[str], Exception] = ValueError,
) -> str:
    return canonical_symbol_for_write(symbol, config=cfg, error_factory=error_factory)


def calibrate_symbol_for_config(cfg: dict[str, Any], symbol: str) -> SymbolCalibrationResult:
    return calibrate_symbol(symbol, config=cfg)


def set_path(
    obj: dict[str, Any],
    path: str,
    value: Any,
    *,
    error_factory: Callable[[str], Exception] = ValueError,
) -> None:
    cur = obj
    parts = [str(x).strip() for x in str(path).split(".") if str(x).strip()]
    if not parts:
        raise error_factory("set path cannot be empty")
    for key in parts[:-1]:
        nxt = cur.get(key)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[key] = nxt
        cur = nxt
    cur[parts[-1]] = value


def ensure_use_templates(
    entry: dict[str, Any],
    templates: list[str] | tuple[str, ...],
    *,
    error_factory: Callable[[str], Exception] = ValueError,
) -> bool:
    requested = [str(item).strip() for item in templates if str(item).strip()]
    if not requested:
        return False
    current = entry.get("use")
    current_items = _use_templates(current, error_factory=error_factory)
    merged = list(current_items)
    for item in requested:
        if item not in merged:
            merged.append(item)
    if merged == current_items:
        return False
    if not current_items and len(merged) == 1:
        entry["use"] = merged[0]
    else:
        entry["use"] = merged
    return True


def _use_templates(
    value: Any,
    *,
    error_factory: Callable[[str], Exception] = ValueError,
) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    raise error_factory("symbol use must be a string or list")


def _symbols_rows(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    rows = cfg.get("symbols")
    return [item for item in rows if isinstance(item, dict)] if isinstance(rows, list) else []


def _strategy_defaults(*, enabled: bool) -> dict[str, Any]:
    if not enabled:
        return {"enabled": False}
    return {"enabled": True, "min_dte": 20, "max_dte": 45}


def _ensure_enabled_strategy_defaults(entry: dict[str, Any], key: str) -> list[str]:
    strategy = entry.get(key)
    if not isinstance(strategy, dict) or strategy.get("enabled") is not True:
        return []
    changed: list[str] = []
    for name, value in (("min_dte", 20), ("max_dte", 45)):
        if strategy.get(name) is None:
            strategy[name] = value
            changed.append(f"{key}.{name}")
    return changed


def default_use_for_enabled_sides(*, sell_put_enabled: bool, sell_call_enabled: bool) -> str | list[str] | None:
    if sell_put_enabled and sell_call_enabled:
        return ["put_base", "call_base"]
    if sell_call_enabled:
        return "call_base"
    if sell_put_enabled:
        return "put_base"
    return None
