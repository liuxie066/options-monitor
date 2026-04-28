from __future__ import annotations

from typing import Any, Callable


def normalize_symbol(value: str) -> str:
    return str(value or "").strip().upper()


def normalize_symbol_read(value: Any) -> str:
    return normalize_symbol(str(value or ""))


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
    resolve_watchlist_config: Callable[[dict[str, Any]], list[dict[str, Any]]],
) -> tuple[int | None, dict[str, Any] | None]:
    needle = normalize_symbol(symbol)
    for idx, item in enumerate(resolve_watchlist_config(cfg)):
        if normalize_symbol(str(item.get("symbol") or "")) == needle:
            return idx, item
    return None, None


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
