from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal, MutableMapping

from domain.domain.candidate_defaults import (
    DEFAULT_SELL_CALL_WINDOW,
    DEFAULT_SELL_PUT_WINDOW,
    DEFAULT_SELL_PUT_YIELD_ENHANCEMENT_WINDOW,
    CandidateWindowDefaults,
    resolve_candidate_window,
)
from src.application.opend_market_snapshot_fetching import get_underlier_spot
from src.application.opend_symbol_chain_fetching import (
    OptionExpirationDiscoveryResult,
    discover_option_expirations,
    list_option_expirations,
)
from src.application.opend_utils import get_trading_date, normalize_underlier
from src.application.yield_enhancement_config import (
    derive_yield_enhancement_policy,
    resolve_staggered_expiry_gap_days,
)
from src.application.strategy_policy import (
    SELL_CALL_FAMILY,
    SELL_PUT_FAMILY,
    assert_strategy_config_resolved,
    strategy_semantics_for_side_config,
)


OptionSide = Literal["put", "call"]
FetchPlanOutcome = Literal[
    "success_rows",
    "success_empty",
    "projection_empty",
    "provider_error",
    "parse_error",
]
ExpirationDiscoveryCacheKey = tuple[str, str, str, int, str]

DEFAULT_SELL_CALL_SPOT_FALLBACK_MIN_PCT = 0.03
DEFAULT_SELL_CALL_STRIKE_BUFFER_PCT = 0.02
DEFAULT_FETCH_NEAR_BOUND_EXPAND_PCT = 0.20
DEFAULT_COMBO_YIELD_CALL_FETCH_MAX_PCT = 0.40


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
    include_realized_volatility: bool = False
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
            "include_realized_volatility": bool(self.include_realized_volatility),
            "side_plans": [plan.to_debug_dict() for plan in self.side_plans],
            "planning_reason": self.planning_reason,
        }


@dataclass(frozen=True)
class RequiredDataFetchPlanBundle:
    symbol: str
    spot_reference: float | None
    side_plans: list[OptionSideFetchPlan]
    merged_specs: list[RequiredDataFetchSpec]
    expiration_discovery_complete: bool = True
    expiration_discovery_error: str | None = None
    expiration_discovery: OptionExpirationDiscoveryResult | None = None
    projection_outcome: FetchPlanOutcome | None = None
    projected_expirations: list[str] = field(default_factory=list)

    def to_debug_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "spot_reference": self.spot_reference,
            "side_plans": [plan.to_debug_dict() for plan in self.side_plans],
            "merged_requests": [spec.to_debug_dict() for spec in self.merged_specs],
            "expiration_discovery_complete": bool(self.expiration_discovery_complete),
            "expiration_discovery_error": self.expiration_discovery_error,
            "expiration_discovery": (
                self.expiration_discovery.to_debug_dict()
                if self.expiration_discovery is not None
                else None
            ),
            "projection_outcome": self.projection_outcome,
            "projected_expirations": list(self.projected_expirations),
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
    try:
        fresh = get_underlier_spot(
            symbol,
            host=host,
            port=port,
            base_dir=base_dir,
            snapshot_max_wait_sec=snapshot_max_wait_sec,
            snapshot_window_sec=snapshot_window_sec,
            snapshot_max_calls=snapshot_max_calls,
        )
        if fresh is not None and fresh > 0:
            return fresh
    except Exception:
        pass
    existing = _load_existing_spot(required_data_dir=required_data_dir, symbol=symbol)
    if existing is not None and existing > 0:
        return existing
    return None


def _filter_expirations_by_dte(*, symbol: str, available_expirations: list[str], min_dte: int | None, max_dte: int | None) -> list[str]:
    if not available_expirations:
        return []
    try:
        from src.application.opend_utils import get_trading_date, normalize_underlier

        today = get_trading_date(normalize_underlier(symbol).market)
    except Exception as exc:
        raise RuntimeError(f"failed to resolve trading date for {symbol}") from exc

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


def _expiration_date(value: Any) -> date | None:
    try:
        return datetime.fromisoformat(str(value)[:10]).date()
    except Exception:
        return None


def _filter_staggered_call_expirations(
    *,
    put_expirations: list[str],
    call_expirations: list[str],
    min_gap_days: int,
    max_gap_days: int,
) -> list[str]:
    put_dates = [
        parsed
        for value in put_expirations
        if (parsed := _expiration_date(value)) is not None
    ]
    if not put_dates:
        return []
    out: list[str] = []
    for value in call_expirations:
        call_date = _expiration_date(value)
        if call_date is None:
            continue
        if any(
            int(min_gap_days) <= (call_date - put_date).days <= int(max_gap_days)
            for put_date in put_dates
        ):
            out.append(str(value)[:10])
    return out


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
    expirations = filtered
    configured_min_strike = _safe_float(sell_put_cfg.get("min_strike"))
    configured_max_strike = _safe_float(sell_put_cfg.get("max_strike"))
    min_strike = configured_min_strike
    max_strike = configured_max_strike
    if spot_reference is not None and spot_reference > 0:
        max_strike = min(value for value in (configured_max_strike, spot_reference) if value is not None)
    planning_reason = f"use configured {source_prefix} near/far bounds"
    source_fields = [f"{source_prefix}.min_strike", f"{source_prefix}.max_strike", f"{source_prefix}.min_dte", f"{source_prefix}.max_dte"]
    if min_strike is None and max_strike is not None:
        min_strike = max_strike * (1.0 - DEFAULT_FETCH_NEAR_BOUND_EXPAND_PCT)
        planning_reason = f"derive {source_prefix} far bound from configured near bound -20%"
        source_fields = source_fields + [f"{source_prefix}.max_strike"]
    if spot_reference is not None and spot_reference > 0:
        source_fields = source_fields + ["spot"]
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
    has_spot = spot_reference is not None and spot_reference > 0
    if min_strike is not None or max_strike is not None or has_spot:
        base_min = min_strike
        if has_spot:
            base_min = max(value for value in (base_min, spot_reference) if value is not None)
        base_max = max_strike
        if base_min is not None and base_max is None:
            base_max = base_min * (1.0 + float(fallback_max_pct))
        if base_min is not None and base_max is not None and base_max < base_min:
            base_max = base_min
        fetch_min = base_min
        fetch_max = base_max
        if fetch_max is not None:
            fetch_max = fetch_max * (1.0 + DEFAULT_SELL_CALL_STRIKE_BUFFER_PCT)
        source = f"{source_prefix}.configured_bounds" if min_strike is not None or max_strike is not None else f"{source_prefix}.spot_derived_bounds"
        reason = f"use configured {source_prefix} near/far bounds" if min_strike is not None or max_strike is not None else f"derive {source_prefix} near/far bounds from spot reference"
        fields = [f"{source_prefix}.min_strike", f"{source_prefix}.max_strike"] if min_strike is not None or max_strike is not None else ["spot"]
        if has_spot and "spot" not in fields:
            fields = fields + ["spot"]
        return (
            StrikeWindowPlan(
                min_strike=fetch_min,
                max_strike=fetch_max,
                source=source,
                buffer_applied=(fetch_max is not None and base_max is not None and fetch_max != base_max),
                buffer_pct=DEFAULT_SELL_CALL_STRIKE_BUFFER_PCT,
                base_min_strike=base_min,
                base_max_strike=base_max,
            ),
            reason,
            fields,
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


def _resolve_call_side_plan(
    *,
    symbol: str,
    sell_call_cfg: dict,
    limit_expirations: int,
    available_expirations: list[str],
    spot_reference: float | None,
    defaults: CandidateWindowDefaults = DEFAULT_SELL_CALL_WINDOW,
    source_prefix: str = "sell_call",
    dte_source_prefix: str | None = None,
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
    expirations = filtered
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
        source_fields=source_fields + [
            f"{dte_source_prefix or source_prefix}.min_dte",
            f"{dte_source_prefix or source_prefix}.max_dte",
        ],
        spot_reference=spot_reference,
    )


def _resolve_combo_yield_call_plan(
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
    structure_mode = str(cfg.get("structure_mode") or "same_expiry_pair").strip().lower()
    if structure_mode == "staggered_expiry_pair":
        min_gap_days, max_gap_days = resolve_staggered_expiry_gap_days(cfg)
        put_window = resolve_candidate_window(
            sell_put_cfg,
            defaults=DEFAULT_SELL_PUT_WINDOW,
        )
        call_cfg.pop("min_dte", None)
        call_cfg.pop("max_dte", None)
        call_cfg["min_dte"] = int(put_window.min_dte) + int(min_gap_days)
        call_cfg["max_dte"] = int(put_window.max_dte) + int(max_gap_days)
        call_window = resolve_candidate_window(call_cfg, defaults=DEFAULT_SELL_PUT_YIELD_ENHANCEMENT_WINDOW)
        dte_source_prefix = "combo_yield"
    else:
        call_cfg.pop("min_dte", None)
        call_cfg.pop("max_dte", None)
        for key in ("min_dte", "max_dte"):
            if key in sell_put_cfg:
                call_cfg[key] = sell_put_cfg.get(key)
        call_window = resolve_candidate_window(
            sell_put_cfg,
            defaults=DEFAULT_SELL_PUT_WINDOW,
        )
        dte_source_prefix = "sell_put"
    plan = _resolve_call_side_plan(
        symbol=symbol,
        sell_call_cfg=call_cfg,
        limit_expirations=limit_expirations,
        available_expirations=available_expirations,
        spot_reference=spot_reference,
        defaults=call_window,
        source_prefix="combo_yield.call",
        dte_source_prefix=dte_source_prefix,
        fallback_min_pct=0.0,
        fallback_max_pct=DEFAULT_COMBO_YIELD_CALL_FETCH_MAX_PCT,
    )
    if structure_mode != "staggered_expiry_pair":
        return plan

    put_expirations = _filter_expirations_by_dte(
        symbol=symbol,
        available_expirations=available_expirations,
        min_dte=put_window.min_dte,
        max_dte=put_window.max_dte,
    )
    feasible_call_expirations = _filter_staggered_call_expirations(
        put_expirations=put_expirations,
        call_expirations=plan.explicit_expirations,
        min_gap_days=min_gap_days,
        max_gap_days=max_gap_days,
    )
    return replace(
        plan,
        explicit_expirations=feasible_call_expirations,
        planning_reason=(
            "derive combo_yield Call expirations from Funding Put expirations "
            "and configured expiry-gap bounds"
        ),
        source_fields=[
            field
            for field in plan.source_fields
            if field not in {"combo_yield.min_dte", "combo_yield.max_dte"}
        ]
        + [
            "sell_put.min_dte",
            "sell_put.max_dte",
            "combo_yield.min_expiry_gap_days",
            "combo_yield.max_expiry_gap_days",
        ],
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


def _expiration_discovery_cache_key(
    *,
    base: Path,
    symbol: str,
    source: str,
    host: str,
    port: int,
) -> ExpirationDiscoveryCacheKey:
    underlier = normalize_underlier(symbol, base_dir=base)
    trading_date = get_trading_date(underlier.market).isoformat()
    return (
        str(symbol or "").strip().upper(),
        str(source or "").strip().lower(),
        str(host),
        int(port),
        trading_date,
    )


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


def _position_requirement_side_plans(
    requirements: list[dict[str, Any]] | None,
) -> list[OptionSideFetchPlan]:
    grouped: dict[OptionSide, list[dict[str, Any]]] = {
        "put": [],
        "call": [],
    }
    for requirement in requirements or []:
        if not isinstance(requirement, dict):
            continue
        option_type = str(requirement.get("option_type") or "").strip().lower()
        if option_type not in {"put", "call"}:
            continue
        if str(requirement.get("planning_status") or "ready") != "ready":
            continue
        grouped[option_type].append(requirement)

    plans: list[OptionSideFetchPlan] = []
    for option_type in ("put", "call"):
        items = grouped[option_type]
        if not items:
            continue
        expirations = sorted(
            {
                str(item.get("expiration") or "").strip()
                for item in items
                if str(item.get("expiration") or "").strip()
            }
        )
        strikes = [
            value
            for item in items
            if (value := _safe_float(item.get("strike"))) is not None
        ]
        plans.append(
            OptionSideFetchPlan(
                option_type=option_type,
                min_dte=None,
                max_dte=None,
                explicit_expirations=expirations,
                strike_window=StrikeWindowPlan(
                    min_strike=(min(strikes) if strikes else None),
                    max_strike=(max(strikes) if strikes else None),
                    source="close_advice.position_requirements",
                ),
                planning_reason="cover active Close Advice position contracts",
                source_fields=["close_advice.position_requirements"],
            )
        )
    return plans


def _merge_side_plans(
    *,
    symbol: str,
    limit_expirations: int,
    host: str,
    port: int,
    side_plans: list[OptionSideFetchPlan],
    include_realized_volatility: bool = False,
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
                limit_expirations=0,
                host=host,
                port=port,
                option_types=option_types,
                explicit_expirations=list(expirations_key),
                min_dte=min((plan.min_dte for plan in plans if plan.min_dte is not None), default=None),
                max_dte=max((plan.max_dte for plan in plans if plan.max_dte is not None), default=None),
                side_strike_windows=side_strike_windows,
                include_realized_volatility=bool(include_realized_volatility),
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
    position_requirements: list[dict[str, Any]] | None = None,
    symbol_cfg: dict[str, Any] | None = None,
    fetch_host: str = "127.0.0.1",
    fetch_port: int = 11111,
    fetch_source: str = "futu",
    expiration_discovery_cache: (
        MutableMapping[
            ExpirationDiscoveryCacheKey,
            OptionExpirationDiscoveryResult,
        ]
        | None
    ) = None,
    snapshot_max_wait_sec: float = 30.0,
    snapshot_window_sec: float = 30.0,
    snapshot_max_calls: int = 60,
    expiration_max_wait_sec: float = 30.0,
    expiration_window_sec: float = 30.0,
    expiration_max_calls: int = 60,
) -> RequiredDataFetchPlanBundle:
    assert_strategy_config_resolved(symbol_cfg)
    sell_put_cfg = dict(sell_put_cfg or {})
    sell_call_cfg = dict(sell_call_cfg or {})
    resolved_yield_enhancement_cfg = dict(yield_enhancement_cfg or {})
    sell_put_semantics = strategy_semantics_for_side_config(family=SELL_PUT_FAMILY, side_cfg=sell_put_cfg)
    sell_call_semantics = strategy_semantics_for_side_config(family=SELL_CALL_FAMILY, side_cfg=sell_call_cfg)
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
        discovery_cache_key = _expiration_discovery_cache_key(
            base=base,
            symbol=symbol,
            source=fetch_source,
            host=fetch_host,
            port=fetch_port,
        )
    except Exception:
        discovery_cache_key = None
    expiration_discovery = (
        expiration_discovery_cache.get(discovery_cache_key)
        if expiration_discovery_cache is not None
        and discovery_cache_key is not None
        else None
    )
    if expiration_discovery is None:
        expiration_discovery = discover_option_expirations(
            symbol,
            source=fetch_source,
            host=fetch_host,
            port=fetch_port,
            base_dir=base,
            expiration_max_wait_sec=expiration_max_wait_sec,
            expiration_window_sec=expiration_window_sec,
            expiration_max_calls=expiration_max_calls,
            list_expirations_fn=list_option_expirations,
        )
        if (
            expiration_discovery_cache is not None
            and discovery_cache_key is not None
        ):
            expiration_discovery_cache[discovery_cache_key] = (
                expiration_discovery
            )
    available_expirations = list(expiration_discovery.expirations)

    side_plans: list[OptionSideFetchPlan] = []
    yield_enhancement_policy = derive_yield_enhancement_policy(resolved_yield_enhancement_cfg)
    combo_yield_enabled = bool(yield_enhancement_policy.enabled)
    if want_put or combo_yield_enabled:
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
    if combo_yield_enabled:
        side_plans.append(
            _resolve_combo_yield_call_plan(
                symbol=symbol,
                sell_put_cfg=sell_put_cfg,
                yield_enhancement_cfg=resolved_yield_enhancement_cfg,
                limit_expirations=limit_expirations,
                available_expirations=available_expirations,
                spot_reference=spot_reference,
            )
        )
    side_plans.extend(
        _position_requirement_side_plans(position_requirements)
    )
    side_plans = _merge_same_side_plans(side_plans)
    projected_expirations = _unique_preserve_order(
        [
            expiration
            for side_plan in side_plans
            for expiration in side_plan.explicit_expirations
        ]
    )
    if not side_plans and expiration_discovery.outcome == "success_rows":
        projected_expirations = list(expiration_discovery.expirations)
    if expiration_discovery.outcome == "success_empty":
        projection_outcome: FetchPlanOutcome = "success_empty"
    elif expiration_discovery.outcome in {"provider_error", "parse_error"}:
        projection_outcome = expiration_discovery.outcome
    elif side_plans and not projected_expirations:
        projection_outcome = "projection_empty"
    else:
        projection_outcome = "success_rows"
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
            include_realized_volatility=bool(
                (want_put and sell_put_semantics.scan_requires_rv)
                or (want_call and sell_call_semantics.scan_requires_rv)
                or (combo_yield_enabled and yield_enhancement_policy.requires_realized_volatility)
                or any(
                    bool(item.get("requires_realized_volatility"))
                    for item in (position_requirements or [])
                    if isinstance(item, dict)
                    and str(item.get("planning_status") or "ready") == "ready"
                )
            ),
        ),
        expiration_discovery_complete=expiration_discovery.complete,
        expiration_discovery_error=expiration_discovery.error,
        expiration_discovery=expiration_discovery,
        projection_outcome=projection_outcome,
        projected_expirations=projected_expirations,
    )
