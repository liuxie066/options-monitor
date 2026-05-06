from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from scripts.candidate_defaults import (
    DEFAULT_SELL_CALL_WINDOW,
    DEFAULT_SELL_PUT_WINDOW,
    DEFAULT_SELL_PUT_YIELD_ENHANCEMENT_WINDOW,
    CandidateWindowDefaults,
    resolve_candidate_window,
)
from src.application.opend_symbol_fetching import get_underlier_spot, list_option_expirations


OptionSide = Literal["put", "call"]

DEFAULT_SELL_CALL_SPOT_FALLBACK_MIN_PCT = 0.03
DEFAULT_SELL_CALL_STRIKE_BUFFER_PCT = 0.02
DEFAULT_FETCH_NEAR_BOUND_EXPAND_PCT = 0.20
DEFAULT_SELL_PUT_YIELD_ENHANCEMENT_MAX_CALL_OTM_PCT = 0.25


@dataclass(frozen=True)
class ExpirationPlan:
    requested: list[str]
    source: str
    min_dte: int | None
    max_dte: int | None


@dataclass(frozen=True)
class StrikeWindowPlan:
    min_strike: float | None
    max_strike: float | None
    source: str
    buffer_applied: bool = False
    buffer_pct: float = 0.0
    base_min_strike: float | None = None
    base_max_strike: float | None = None


@dataclass(frozen=True)
class OptionSideFetchPlan:
    option_type: OptionSide
    min_dte: int | None
    max_dte: int | None
    explicit_expirations: list[str]
    strike_window: StrikeWindowPlan
    planning_reason: str
    source_fields: list[str] = field(default_factory=list)
    spot_reference: float | None = None

    def to_debug_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["min_strike"] = self.strike_window.min_strike
        payload["max_strike"] = self.strike_window.max_strike
        payload["expiration_count"] = len(self.explicit_expirations)
        return payload


@dataclass(frozen=True)
class RequiredDataFetchSpec:
    symbol: str
    limit_expirations: int
    host: str
    port: int
    option_types: tuple[OptionSide, ...]
    explicit_expirations: list[str]
    min_dte: int | None
    max_dte: int | None
    side_strike_windows: dict[str, dict[str, float | None]]
    side_plans: list[OptionSideFetchPlan] = field(default_factory=list)
    planning_reason: str = ""

    def to_debug_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "limit_expirations": self.limit_expirations,
            "host": self.host,
            "port": self.port,
            "option_types": list(self.option_types),
            "explicit_expirations": list(self.explicit_expirations),
            "min_dte": self.min_dte,
            "max_dte": self.max_dte,
            "side_strike_windows": {k: dict(v) for k, v in self.side_strike_windows.items()},
            "side_plans": [plan.to_debug_dict() for plan in self.side_plans],
            "planning_reason": self.planning_reason,
        }


@dataclass(frozen=True)
class RequiredDataFetchPlanBundle:
    symbol: str
    spot_reference: float | None
    side_plans: list[OptionSideFetchPlan]
    merged_specs: list[RequiredDataFetchSpec]

    def to_debug_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "spot_reference": self.spot_reference,
            "side_plans": [plan.to_debug_dict() for plan in self.side_plans],
            "merged_requests": [spec.to_debug_dict() for spec in self.merged_specs],
        }


def _safe_float(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except Exception:
        return None


def _safe_int(value: Any) -> int | None:
    try:
        if value in (None, ""):
            return None
        return int(value)
    except Exception:
        return None


def _load_existing_spot(*, required_data_dir: Path, symbol: str) -> float | None:
    path = required_data_dir / "parsed" / f"{symbol}_required_data.csv"
    if not path.exists() or path.stat().st_size <= 0:
        return None
    try:
        import pandas as pd

        df = pd.read_csv(path, usecols=["spot"])
        spots = pd.to_numeric(df["spot"], errors="coerce").dropna()
        if spots.empty:
            return None
        return float(spots.iloc[0])
    except Exception:
        return None


def _resolve_spot_reference(
    *,
    symbol: str,
    host: str,
    port: int,
    base_dir: Path,
    required_data_dir: Path,
    snapshot_max_wait_sec: float = 30.0,
    snapshot_window_sec: float = 30.0,
    snapshot_max_calls: int = 60,
) -> float | None:
    existing = _load_existing_spot(required_data_dir=required_data_dir, symbol=symbol)
    if existing is not None and existing > 0:
        return existing
    try:
        return get_underlier_spot(
            symbol,
            host=host,
            port=port,
            base_dir=base_dir,
            snapshot_max_wait_sec=snapshot_max_wait_sec,
            snapshot_window_sec=snapshot_window_sec,
            snapshot_max_calls=snapshot_max_calls,
        )
    except Exception:
        return None


def _filter_expirations_by_dte(*, symbol: str, available_expirations: list[str], min_dte: int | None, max_dte: int | None) -> list[str]:
    if not available_expirations:
        return []
    try:
        from datetime import datetime
        from scripts.opend_utils import get_trading_date, normalize_underlier

        today = get_trading_date(normalize_underlier(symbol).market)
        out: list[str] = []
        for exp in available_expirations:
            try:
                d0 = datetime.fromisoformat(str(exp)[:10]).date()
                dte0 = int((d0 - today).days)
            except Exception:
                continue
            if min_dte is not None and dte0 < int(min_dte):
                continue
            if max_dte is not None and dte0 > int(max_dte):
                continue
            out.append(str(exp)[:10])
        return out
    except Exception:
        return list(available_expirations)


def _resolve_put_side_plan(
    *,
    symbol: str,
    sell_put_cfg: dict,
    limit_expirations: int,
    available_expirations: list[str],
    spot_reference: float | None,
    defaults: CandidateWindowDefaults = DEFAULT_SELL_PUT_WINDOW,
    source_prefix: str = "sell_put",
) -> OptionSideFetchPlan:
    window = resolve_candidate_window(sell_put_cfg, defaults=defaults)
    filtered = _filter_expirations_by_dte(
        symbol=symbol,
        available_expirations=available_expirations,
        min_dte=window.min_dte,
        max_dte=window.max_dte,
    )
    expirations = filtered[: int(limit_expirations)] if limit_expirations and filtered else filtered
    min_strike = _safe_float(sell_put_cfg.get("min_strike"))
    max_strike = _safe_float(sell_put_cfg.get("max_strike"))
    planning_reason = f"use configured {source_prefix} near/far bounds"
    source_fields = [f"{source_prefix}.min_strike", f"{source_prefix}.max_strike", f"{source_prefix}.min_dte", f"{source_prefix}.max_dte"]
    if min_strike is None and max_strike is not None:
        min_strike = max_strike * (1.0 - DEFAULT_FETCH_NEAR_BOUND_EXPAND_PCT)
        planning_reason = f"derive {source_prefix} far bound from configured near bound -20%"
        source_fields = source_fields + [f"{source_prefix}.max_strike"]
    return OptionSideFetchPlan(
        option_type="put",
        min_dte=window.min_dte,
        max_dte=window.max_dte,
        explicit_expirations=expirations,
        strike_window=StrikeWindowPlan(
            min_strike=min_strike,
            max_strike=max_strike,
            source=f"{source_prefix}.configured_bounds",
            buffer_applied=False,
            buffer_pct=0.0,
            base_min_strike=min_strike,
            base_max_strike=max_strike,
        ),
        planning_reason=planning_reason,
        source_fields=source_fields,
        spot_reference=spot_reference,
    )


def _resolve_sell_call_strike_window(
    *,
    sell_call_cfg: dict,
    spot_reference: float | None,
    source_prefix: str = "sell_call",
    fallback_min_pct: float = DEFAULT_SELL_CALL_SPOT_FALLBACK_MIN_PCT,
    fallback_max_pct: float = DEFAULT_FETCH_NEAR_BOUND_EXPAND_PCT,
) -> tuple[StrikeWindowPlan, str, list[str]]:
    min_strike = _safe_float(sell_call_cfg.get("min_strike"))
    max_strike = _safe_float(sell_call_cfg.get("max_strike"))
    if min_strike is not None or max_strike is not None:
        base_min = min_strike
        base_max = max_strike
        if base_min is not None and base_max is None:
            base_max = base_min * (1.0 + DEFAULT_FETCH_NEAR_BOUND_EXPAND_PCT)
        if base_min is not None and base_max is not None and base_max < base_min:
            base_max = base_min
        fetch_min = base_min
        fetch_max = base_max
        if fetch_min is not None and spot_reference is not None and spot_reference > 0:
            fetch_min = max(fetch_min, 0.0)
        if fetch_max is not None:
            fetch_max = fetch_max * (1.0 + DEFAULT_SELL_CALL_STRIKE_BUFFER_PCT)
        return (
            StrikeWindowPlan(
                min_strike=fetch_min,
                max_strike=fetch_max,
                source=f"{source_prefix}.configured_bounds",
                buffer_applied=(fetch_max is not None and base_max is not None and fetch_max != base_max),
                buffer_pct=DEFAULT_SELL_CALL_STRIKE_BUFFER_PCT,
                base_min_strike=base_min,
                base_max_strike=base_max,
            ),
            f"use configured {source_prefix} near/far bounds",
            [f"{source_prefix}.min_strike", f"{source_prefix}.max_strike"],
        )
    if spot_reference is None or spot_reference <= 0:
        return (
            StrikeWindowPlan(
                min_strike=None,
                max_strike=None,
                source=f"{source_prefix}.no_spot_no_bounds",
                buffer_applied=False,
                buffer_pct=0.0,
                base_min_strike=None,
                base_max_strike=None,
            ),
            "spot unavailable; no near/far bounds could be derived",
            ["spot"],
        )
    base_min = spot_reference * (1.0 + float(fallback_min_pct))
    max_pct = max(float(fallback_max_pct), float(fallback_min_pct))
    if max_pct <= float(fallback_min_pct):
        max_pct = float(fallback_min_pct) + 0.05
    base_max = spot_reference * (1.0 + max_pct)
    return (
        StrikeWindowPlan(
            min_strike=base_min,
            max_strike=base_max * (1.0 + DEFAULT_SELL_CALL_STRIKE_BUFFER_PCT),
            source=f"{source_prefix}.spot_derived_bounds",
            buffer_applied=True,
            buffer_pct=DEFAULT_SELL_CALL_STRIKE_BUFFER_PCT,
            base_min_strike=base_min,
            base_max_strike=base_max,
        ),
        f"derive {source_prefix} near/far bounds from spot reference",
        ["spot"],
    )


def _resolve_call_side_plan(
    *,
    symbol: str,
    sell_call_cfg: dict,
    limit_expirations: int,
    available_expirations: list[str],
    spot_reference: float | None,
    defaults: CandidateWindowDefaults = DEFAULT_SELL_CALL_WINDOW,
    source_prefix: str = "sell_call",
    fallback_min_pct: float = DEFAULT_SELL_CALL_SPOT_FALLBACK_MIN_PCT,
    fallback_max_pct: float = DEFAULT_FETCH_NEAR_BOUND_EXPAND_PCT,
) -> OptionSideFetchPlan:
    window = resolve_candidate_window(sell_call_cfg, defaults=defaults)
    filtered = _filter_expirations_by_dte(
        symbol=symbol,
        available_expirations=available_expirations,
        min_dte=window.min_dte,
        max_dte=window.max_dte,
    )
    expirations = filtered[: int(limit_expirations)] if limit_expirations and filtered else filtered
    strike_window, reason, source_fields = _resolve_sell_call_strike_window(
        sell_call_cfg=sell_call_cfg,
        spot_reference=spot_reference,
        source_prefix=source_prefix,
        fallback_min_pct=fallback_min_pct,
        fallback_max_pct=fallback_max_pct,
    )
    return OptionSideFetchPlan(
        option_type="call",
        min_dte=window.min_dte,
        max_dte=window.max_dte,
        explicit_expirations=expirations,
        strike_window=strike_window,
        planning_reason=reason,
        source_fields=source_fields + [f"{source_prefix}.min_dte", f"{source_prefix}.max_dte"],
        spot_reference=spot_reference,
    )


def _resolve_sell_put_yield_enhancement_call_plan(
    *,
    symbol: str,
    sell_put_cfg: dict,
    yield_enhancement_cfg: dict,
    limit_expirations: int,
    available_expirations: list[str],
    spot_reference: float | None,
) -> OptionSideFetchPlan:
    cfg = dict(yield_enhancement_cfg or {})
    call_cfg = dict(cfg.get("call") or {})
    for key in ("min_dte", "max_dte"):
        if key in cfg and key not in call_cfg:
            call_cfg[key] = cfg.get(key)
        elif key in sell_put_cfg and key not in call_cfg:
            call_cfg[key] = sell_put_cfg.get(key)
    fallback_min_pct = _safe_float(cfg.get("min_call_otm_pct"))
    fallback_max_pct = _safe_float(cfg.get("max_call_otm_pct"))
    sell_put_window = resolve_candidate_window(
        sell_put_cfg,
        defaults=DEFAULT_SELL_PUT_YIELD_ENHANCEMENT_WINDOW,
    )
    return _resolve_call_side_plan(
        symbol=symbol,
        sell_call_cfg=call_cfg,
        limit_expirations=limit_expirations,
        available_expirations=available_expirations,
        spot_reference=spot_reference,
        defaults=sell_put_window,
        source_prefix="yield_enhancement.call",
        fallback_min_pct=(
            float(fallback_min_pct)
            if fallback_min_pct is not None
            else 0.03
        ),
        fallback_max_pct=(
            float(fallback_max_pct)
            if fallback_max_pct is not None
            else DEFAULT_SELL_PUT_YIELD_ENHANCEMENT_MAX_CALL_OTM_PCT
        ),
    )


def _unique_preserve_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def _merge_same_side_plans(side_plans: list[OptionSideFetchPlan]) -> list[OptionSideFetchPlan]:
    grouped: dict[OptionSide, list[OptionSideFetchPlan]] = {"put": [], "call": []}
    for plan in side_plans:
        grouped.setdefault(plan.option_type, []).append(plan)

    merged: list[OptionSideFetchPlan] = []
    for option_type in ("put", "call"):
        plans = grouped.get(option_type) or []
        if not plans:
            continue
        if len(plans) == 1:
            merged.append(plans[0])
            continue

        expirations = _unique_preserve_order([exp for plan in plans for exp in plan.explicit_expirations])
        min_values = [plan.strike_window.min_strike for plan in plans if plan.strike_window.min_strike is not None]
        max_values = [plan.strike_window.max_strike for plan in plans if plan.strike_window.max_strike is not None]
        base_min_values = [plan.strike_window.base_min_strike for plan in plans if plan.strike_window.base_min_strike is not None]
        base_max_values = [plan.strike_window.base_max_strike for plan in plans if plan.strike_window.base_max_strike is not None]
        source_fields = _unique_preserve_order([field for plan in plans for field in plan.source_fields])
        merged.append(
            OptionSideFetchPlan(
                option_type=option_type,
                min_dte=min((plan.min_dte for plan in plans if plan.min_dte is not None), default=None),
                max_dte=max((plan.max_dte for plan in plans if plan.max_dte is not None), default=None),
                explicit_expirations=expirations,
                strike_window=StrikeWindowPlan(
                    min_strike=(min(min_values) if min_values else None),
                    max_strike=(max(max_values) if max_values else None),
                    source="+".join(_unique_preserve_order([plan.strike_window.source for plan in plans])),
                    buffer_applied=any(plan.strike_window.buffer_applied for plan in plans),
                    buffer_pct=max((plan.strike_window.buffer_pct for plan in plans), default=0.0),
                    base_min_strike=(min(base_min_values) if base_min_values else None),
                    base_max_strike=(max(base_max_values) if base_max_values else None),
                ),
                planning_reason=f"merged {option_type} requirements across enabled strategies",
                source_fields=source_fields,
                spot_reference=next((plan.spot_reference for plan in plans if plan.spot_reference is not None), None),
            )
        )
    return merged


def _merge_side_plans(
    *,
    symbol: str,
    limit_expirations: int,
    host: str,
    port: int,
    side_plans: list[OptionSideFetchPlan],
) -> list[RequiredDataFetchSpec]:
    groups: dict[tuple[str, ...], list[OptionSideFetchPlan]] = {}
    for plan in side_plans:
        key = tuple(plan.explicit_expirations)
        groups.setdefault(key, []).append(plan)
    merged: list[RequiredDataFetchSpec] = []
    for expirations_key, plans in groups.items():
        option_types = tuple(plan.option_type for plan in plans)
        side_strike_windows = {
            plan.option_type: {
                "min_strike": plan.strike_window.min_strike,
                "max_strike": plan.strike_window.max_strike,
            }
            for plan in plans
        }
        merged.append(
            RequiredDataFetchSpec(
                symbol=symbol,
                limit_expirations=limit_expirations,
                host=host,
                port=port,
                option_types=option_types,
                explicit_expirations=list(expirations_key),
                min_dte=min((plan.min_dte for plan in plans if plan.min_dte is not None), default=None),
                max_dte=max((plan.max_dte for plan in plans if plan.max_dte is not None), default=None),
                side_strike_windows=side_strike_windows,
                side_plans=list(plans),
                planning_reason=("shared expirations -> merged request" if len(plans) > 1 else "single-side request"),
            )
        )
    return merged


def build_required_data_fetch_plan(
    *,
    base: Path,
    required_data_dir: Path,
    symbol: str,
    limit_expirations: int,
    want_put: bool,
    want_call: bool,
    sell_put_cfg: dict | None = None,
    sell_call_cfg: dict | None = None,
    yield_enhancement_cfg: dict | None = None,
    fetch_host: str = "127.0.0.1",
    fetch_port: int = 11111,
    snapshot_max_wait_sec: float = 30.0,
    snapshot_window_sec: float = 30.0,
    snapshot_max_calls: int = 60,
    expiration_max_wait_sec: float = 30.0,
    expiration_window_sec: float = 30.0,
    expiration_max_calls: int = 30,
) -> RequiredDataFetchPlanBundle:
    sell_put_cfg = dict(sell_put_cfg or {})
    sell_call_cfg = dict(sell_call_cfg or {})
    resolved_yield_enhancement_cfg = dict(yield_enhancement_cfg or {})
    spot_reference = _resolve_spot_reference(
        symbol=symbol,
        host=fetch_host,
        port=fetch_port,
        base_dir=base,
        required_data_dir=required_data_dir,
        snapshot_max_wait_sec=snapshot_max_wait_sec,
        snapshot_window_sec=snapshot_window_sec,
        snapshot_max_calls=snapshot_max_calls,
    )
    try:
        available_expirations = list_option_expirations(
            symbol,
            host=fetch_host,
            port=fetch_port,
            base_dir=base,
            expiration_max_wait_sec=expiration_max_wait_sec,
            expiration_window_sec=expiration_window_sec,
            expiration_max_calls=expiration_max_calls,
        )
    except Exception:
        available_expirations = []

    side_plans: list[OptionSideFetchPlan] = []
    if want_put:
        side_plans.append(
            _resolve_put_side_plan(
                symbol=symbol,
                sell_put_cfg=sell_put_cfg,
                limit_expirations=limit_expirations,
                available_expirations=available_expirations,
                spot_reference=spot_reference,
            )
        )
    if want_call:
        side_plans.append(
            _resolve_call_side_plan(
                symbol=symbol,
                sell_call_cfg=sell_call_cfg,
                limit_expirations=limit_expirations,
                available_expirations=available_expirations,
                spot_reference=spot_reference,
            )
        )
    if want_put and bool(resolved_yield_enhancement_cfg.get("enabled", False)):
        side_plans.append(
            _resolve_sell_put_yield_enhancement_call_plan(
                symbol=symbol,
                sell_put_cfg=sell_put_cfg,
                yield_enhancement_cfg=resolved_yield_enhancement_cfg,
                limit_expirations=limit_expirations,
                available_expirations=available_expirations,
                spot_reference=spot_reference,
            )
        )
    side_plans = _merge_same_side_plans(side_plans)
    return RequiredDataFetchPlanBundle(
        symbol=symbol,
        spot_reference=spot_reference,
        side_plans=side_plans,
        merged_specs=_merge_side_plans(
            symbol=symbol,
            limit_expirations=limit_expirations,
            host=fetch_host,
            port=fetch_port,
            side_plans=side_plans,
        ),
    )
