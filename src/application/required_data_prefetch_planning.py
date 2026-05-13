from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from domain.domain.candidate_defaults import (
    DEFAULT_SELL_CALL_WINDOW,
    DEFAULT_SELL_PUT_WINDOW,
    DEFAULT_SELL_PUT_YIELD_ENHANCEMENT_WINDOW,
    resolve_candidate_window,
)
from domain.domain.fetch_source import resolve_symbol_fetch_source
from src.application.yield_enhancement_config import resolve_yield_enhancement_cfg


DEFAULT_STRIKE_EXPAND_PCT = 0.20
DEFAULT_CALL_STRIKE_BUFFER_PCT = 0.02


@dataclass(frozen=True)
class PrefetchSymbolPlan:
    symbol_cfgs: list[dict[str, Any]]
    requested_symbols: list[str]
    deduped_groups: list[dict[str, Any]]

    @property
    def requested_count(self) -> int:
        return len(self.requested_symbols)

    @property
    def unique_count(self) -> int:
        return len(self.symbol_cfgs)

    @property
    def deduped_count(self) -> int:
        return max(0, self.requested_count - self.unique_count)

    def summary(self) -> dict[str, Any]:
        return {
            "requested_count": self.requested_count,
            "unique_count": self.unique_count,
            "deduped_count": self.deduped_count,
            "deduped_groups": [dict(group) for group in self.deduped_groups],
        }


def build_prefetch_symbol_plan(symbol_cfgs: list[dict[str, Any]]) -> PrefetchSymbolPlan:
    requested_symbols = [
        str(item.get("symbol") or "").strip()
        for item in symbol_cfgs
        if isinstance(item, dict) and str(item.get("symbol") or "").strip()
    ]
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for idx, cfg in enumerate(symbol_cfgs):
        key = _dedupe_key(cfg, idx=idx)
        groups.setdefault(key, []).append(cfg)

    merged_cfgs: list[dict[str, Any]] = []
    deduped_groups: list[dict[str, Any]] = []
    for items in groups.values():
        merged = merge_prefetch_symbol_configs(items)
        merged_cfgs.append(merged)
        if len(items) > 1:
            deduped_groups.append(
                {
                    "symbol": str(merged.get("symbol") or "").strip(),
                    "requested_count": len(items),
                    "symbols": [
                        str(item.get("symbol") or "").strip()
                        for item in items
                        if isinstance(item, dict) and str(item.get("symbol") or "").strip()
                    ],
                }
            )

    return PrefetchSymbolPlan(
        symbol_cfgs=merged_cfgs,
        requested_symbols=requested_symbols,
        deduped_groups=deduped_groups,
    )


def merge_prefetch_symbol_configs(symbol_cfgs: list[dict[str, Any]]) -> dict[str, Any]:
    items = [cfg for cfg in symbol_cfgs if isinstance(cfg, dict)]
    if not items:
        return {}
    merged = deepcopy(items[0])
    fetch_cfg = dict(_as_dict(merged.get("fetch")))
    fetch_cfg["limit_expirations"] = max((_limit_expirations(item) for item in items), default=8)
    merged["fetch"] = fetch_cfg
    merged["_prefetch_strategy_kwargs"] = _merge_strategy_prefetch_kwargs(
        [strategy_prefetch_kwargs(item, enabled=True) for item in items]
    )
    merged["_prefetch_requested_count"] = len(items)
    return merged


def strategy_prefetch_kwargs(symbol_cfg: dict[str, Any], *, enabled: bool) -> dict[str, Any]:
    precomputed = symbol_cfg.get("_prefetch_strategy_kwargs") if isinstance(symbol_cfg, dict) else None
    if enabled and isinstance(precomputed, dict):
        return _clone_strategy_kwargs(precomputed)
    if not enabled:
        return {"option_types": "put,call"}

    sp = _as_dict(symbol_cfg.get("sell_put"))
    cc = _as_dict(symbol_cfg.get("sell_call"))
    ye = resolve_yield_enhancement_cfg(symbol_cfg)
    want_put = bool(sp.get("enabled", False))
    want_direct_call = bool(cc.get("enabled", False))
    want_yield_call = bool(want_put and ye.get("enabled", False))
    want_call = bool(want_direct_call or want_yield_call)

    option_types: list[str] = []
    min_dtes: list[int] = []
    max_dtes: list[int] = []
    side_strike_windows: dict[str, dict[str, float | None]] = {}

    if want_put:
        min_dte, max_dte = _window_values(sp, defaults=DEFAULT_SELL_PUT_WINDOW)
        min_dtes.append(min_dte)
        max_dtes.append(max_dte)
        option_types.append("put")
        side_strike_windows["put"] = _put_strike_window(sp)

    if want_direct_call:
        min_dte, max_dte = _window_values(cc, defaults=DEFAULT_SELL_CALL_WINDOW)
        min_dtes.append(min_dte)
        max_dtes.append(max_dte)
        option_types.append("call")
        side_strike_windows["call"] = _call_strike_window(cc)

    if want_yield_call:
        call_cfg = dict(_as_dict(ye.get("call")))
        for key in ("min_dte", "max_dte"):
            if key in ye and key not in call_cfg:
                call_cfg[key] = ye.get(key)
            elif key in sp and key not in call_cfg:
                call_cfg[key] = sp.get(key)
        min_dte, max_dte = _window_values(call_cfg, defaults=DEFAULT_SELL_PUT_YIELD_ENHANCEMENT_WINDOW)
        min_dtes.append(min_dte)
        max_dtes.append(max_dte)
        if "call" not in option_types:
            option_types.append("call")
        yield_window = _call_strike_window(call_cfg)
        existing_call = side_strike_windows.get("call")
        if existing_call is None:
            side_strike_windows["call"] = yield_window
        else:
            side_strike_windows["call"] = _merge_strike_windows(existing_call, yield_window)

    if not option_types:
        option_types = ["put", "call"]

    return _strategy_payload(
        option_types=option_types,
        min_dtes=min_dtes,
        max_dtes=max_dtes,
        side_strike_windows=side_strike_windows,
    )


def _merge_strategy_prefetch_kwargs(items: list[dict[str, Any]]) -> dict[str, Any]:
    option_types: list[str] = []
    min_dtes: list[int] = []
    max_dtes: list[int] = []
    side_strike_windows: dict[str, dict[str, float | None]] = {}

    for item in items:
        for option_type in _parse_option_types(item.get("option_types")):
            if option_type not in option_types:
                option_types.append(option_type)
        if item.get("min_dte") is not None:
            min_dtes.append(int(item["min_dte"]))
        if item.get("max_dte") is not None:
            max_dtes.append(int(item["max_dte"]))
        raw_windows = item.get("side_strike_windows")
        if not isinstance(raw_windows, dict):
            continue
        for side in ("put", "call"):
            raw_window = raw_windows.get(side)
            if not isinstance(raw_window, dict):
                continue
            incoming = {
                "min_strike": _to_float(raw_window.get("min_strike")),
                "max_strike": _to_float(raw_window.get("max_strike")),
            }
            existing = side_strike_windows.get(side)
            side_strike_windows[side] = incoming if existing is None else _merge_strike_windows(existing, incoming)

    if not option_types:
        option_types = ["put", "call"]
    ordered_option_types = [side for side in ("put", "call") if side in set(option_types)]
    return _strategy_payload(
        option_types=ordered_option_types,
        min_dtes=min_dtes,
        max_dtes=max_dtes,
        side_strike_windows=side_strike_windows,
    )


def _strategy_payload(
    *,
    option_types: list[str],
    min_dtes: list[int],
    max_dtes: list[int],
    side_strike_windows: dict[str, dict[str, float | None]],
) -> dict[str, Any]:
    all_mins = [
        float(window.get("min_strike"))
        for window in side_strike_windows.values()
        if window.get("min_strike") is not None
    ]
    all_maxs = [
        float(window.get("max_strike"))
        for window in side_strike_windows.values()
        if window.get("max_strike") is not None
    ]
    return {
        "option_types": ",".join(dict.fromkeys(option_types)),
        "min_dte": min(min_dtes) if min_dtes else None,
        "max_dte": max(max_dtes) if max_dtes else None,
        "min_strike": min(all_mins) if all_mins else None,
        "max_strike": max(all_maxs) if all_maxs else None,
        "side_strike_windows": side_strike_windows,
    }


def _dedupe_key(symbol_cfg: dict[str, Any], *, idx: int) -> tuple[Any, ...]:
    symbol = str((symbol_cfg or {}).get("symbol") or "").strip()
    if not symbol:
        return ("empty", idx)
    fetch_cfg = _as_dict((symbol_cfg or {}).get("fetch"))
    source, _decision = resolve_symbol_fetch_source(fetch_cfg)
    host = str(fetch_cfg.get("host") or "127.0.0.1").strip()
    port = _to_int(fetch_cfg.get("port") or 11111, 11111)
    return ("symbol", _symbol_key(symbol), source, host, int(port))


def _symbol_key(symbol: str) -> str:
    raw = str(symbol or "").strip()
    if not raw:
        return ""
    return raw.upper()


def _limit_expirations(symbol_cfg: dict[str, Any]) -> int:
    fetch_cfg = _as_dict((symbol_cfg or {}).get("fetch"))
    return max(1, _to_int(fetch_cfg.get("limit_expirations") or 8, 8))


def _window_values(raw: dict[str, Any], *, defaults: Any) -> tuple[int, int]:
    window = resolve_candidate_window(raw, defaults=defaults)
    return int(window.min_dte), int(window.max_dte)


def _put_strike_window(sp: dict[str, Any]) -> dict[str, float | None]:
    min_strike = _to_float(sp.get("min_strike"))
    max_strike = _to_float(sp.get("max_strike"))
    if min_strike is None and max_strike is not None:
        min_strike = max_strike * (1.0 - DEFAULT_STRIKE_EXPAND_PCT)
    return {"min_strike": min_strike, "max_strike": max_strike}


def _call_strike_window(cc: dict[str, Any]) -> dict[str, float | None]:
    min_strike = _to_float(cc.get("min_strike"))
    max_strike = _to_float(cc.get("max_strike"))
    if min_strike is not None and max_strike is None:
        max_strike = min_strike * (1.0 + DEFAULT_STRIKE_EXPAND_PCT)
    if min_strike is not None and max_strike is not None and max_strike < min_strike:
        max_strike = min_strike
    if max_strike is not None:
        max_strike = max_strike * (1.0 + DEFAULT_CALL_STRIKE_BUFFER_PCT)
    return {"min_strike": min_strike, "max_strike": max_strike}


def _merge_strike_windows(
    left: dict[str, float | None],
    right: dict[str, float | None],
) -> dict[str, float | None]:
    mins = [v for v in (_to_float(left.get("min_strike")), _to_float(right.get("min_strike"))) if v is not None]
    maxs = [v for v in (_to_float(left.get("max_strike")), _to_float(right.get("max_strike"))) if v is not None]
    return {
        "min_strike": min(mins) if mins else None,
        "max_strike": max(maxs) if maxs else None,
    }


def _parse_option_types(value: Any) -> list[str]:
    out: list[str] = []
    for item in str(value or "").split(","):
        raw = str(item or "").strip().lower()
        if raw in {"put", "call"} and raw not in out:
            out.append(raw)
    return out


def _clone_strategy_kwargs(value: dict[str, Any]) -> dict[str, Any]:
    cloned = dict(value)
    raw_windows = cloned.get("side_strike_windows")
    if isinstance(raw_windows, dict):
        cloned["side_strike_windows"] = {
            str(side): dict(window)
            for side, window in raw_windows.items()
            if isinstance(window, dict)
        }
    return cloned


def _to_int(v: Any, default: int) -> int:
    try:
        return int(v)
    except Exception:
        return int(default)


def _to_float(v: Any) -> float | None:
    try:
        if v in (None, ""):
            return None
        return float(v)
    except Exception:
        return None


def _as_dict(v: Any) -> dict[str, Any]:
    return v if isinstance(v, dict) else {}
