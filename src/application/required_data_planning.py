from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, timezone
import math
from pathlib import Path
from typing import Any, Literal, MutableMapping

from domain.domain.candidate_defaults import (
    DEFAULT_SELL_CALL_WINDOW,
    DEFAULT_SELL_PUT_WINDOW,
    CandidateWindowDefaults,
    resolve_candidate_window,
)
from src.application import opend_utils
from src.application.opend_market_snapshot_fetching import (
    get_underlier_observation as get_underlier_spot,
)
from src.application.opening_quote_evidence import OpeningUnderlierObservation
from src.application.opend_symbol_chain_fetching import (
    OptionExpirationDiscoveryResult,
    discover_option_expirations,
    list_option_expirations,
)
from src.application.yield_enhancement_config import (
    derive_yield_enhancement_policy,
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
SpotObservationCacheKey = tuple[str, str, str, int, str]

DEFAULT_SELL_CALL_SPOT_FALLBACK_MIN_PCT = 0.03
DEFAULT_FETCH_NEAR_BOUND_EXPAND_PCT = 0.20
DEFAULT_COMBO_YIELD_CALL_FETCH_MAX_PCT = 0.40
DEFAULT_COMBO_YIELD_CALL_STRIKE_BUFFER_PCT = 0.02


class RequiredDataPlanningError(RuntimeError):
    """Raised when required-data demand cannot be projected without loss."""

    def __init__(
        self,
        *,
        symbol: str,
        requirement_index: int,
        field_name: str,
        reason_code: str = "invalid_ready_position_requirement",
    ) -> None:
        self.symbol = str(symbol or "").strip().upper()
        self.reason_code = str(reason_code or "").strip()
        self.requirement_index = int(requirement_index)
        self.field_name = str(field_name or "").strip()
        super().__init__(
            f"{self.symbol or 'UNKNOWN'}: {self.reason_code}: "
            f"position_requirements[{self.requirement_index}].{self.field_name}"
        )


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
    required_exact_strikes_by_expiration: dict[str, list[float]] = field(
        default_factory=dict
    )
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
    trading_date: str | None = None

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
            "include_realized_volatility": self.include_realized_volatility,
            "side_plans": [plan.to_debug_dict() for plan in self.side_plans],
            "planning_reason": self.planning_reason,
            "trading_date": self.trading_date,
        }


@dataclass(frozen=True)
class RequiredDataFetchPlanBundle:
    symbol: str
    spot_reference: float | None
    side_plans: list[OptionSideFetchPlan]
    merged_specs: list[RequiredDataFetchSpec]
    underlier_observation: OpeningUnderlierObservation | None = None
    expiration_discovery_complete: bool = True
    expiration_discovery_error: str | None = None
    expiration_discovery: OptionExpirationDiscoveryResult | None = None
    projection_outcome: FetchPlanOutcome | None = None
    projected_expirations: list[str] = field(default_factory=list)
    require_realized_volatility: bool = False
    spot_observation_complete: bool = False
    spot_observation_error: str | None = None

    def to_debug_dict(self) -> dict[str, Any]:
        payload = {
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
            "require_realized_volatility": self.require_realized_volatility,
            "spot_observation_complete": bool(self.spot_observation_complete),
            "spot_observation_error": self.spot_observation_error,
        }
        if self.underlier_observation is not None:
            payload["underlier_observation"] = self.underlier_observation.to_dict()
        return payload


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


def _resolve_underlier_observation(
    *,
    symbol: str,
    host: str,
    port: int,
    base_dir: Path,
    snapshot_max_wait_sec: float = 30.0,
    snapshot_window_sec: float = 30.0,
    snapshot_max_calls: int = 60,
) -> OpeningUnderlierObservation | None:
    try:
        observed = get_underlier_spot(
            symbol,
            host=host,
            port=port,
            base_dir=base_dir,
            snapshot_max_wait_sec=snapshot_max_wait_sec,
            snapshot_window_sec=snapshot_window_sec,
            snapshot_max_calls=snapshot_max_calls,
        )
        if isinstance(observed, OpeningUnderlierObservation):
            return observed
        legacy_spot = _safe_float(observed)
        if legacy_spot is None or not math.isfinite(legacy_spot) or legacy_spot <= 0:
            return None
        underlier = opend_utils.normalize_underlier(symbol, base_dir=base_dir)
        return OpeningUnderlierObservation(
            schema_version="opening_underlier_observation.v1",
            code=underlier.code,
            market=underlier.market,
            last_price=legacy_spot,
            update_time=None,
            observed_at_utc=None,
            age_seconds=None,
            market_state="MORNING",
            sec_status="NORMAL",
            suspension=False,
            status="ready",
            reason_code="legacy_test_observation",
        )
    except Exception:
        pass
    return None


def _filter_expirations_by_dte(
    *,
    symbol: str,
    available_expirations: list[str],
    trading_date: date | None,
    min_dte: int | None,
    max_dte: int | None,
) -> list[str]:
    if not available_expirations:
        return []
    if trading_date is None:
        raise RuntimeError(f"failed to resolve trading date for {symbol}")

    out: list[str] = []
    for exp in available_expirations:
        try:
            d0 = datetime.fromisoformat(str(exp)[:10]).date()
            dte0 = int((d0 - trading_date).days)
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


def _strict_iso_expiration(value: Any) -> str | None:
    if not isinstance(value, str) or not value or value != value.strip():
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return None
    return value if parsed.isoformat() == value else None


def _strict_positive_finite_strike(value: Any) -> float | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(parsed) or parsed <= 0:
        return None
    return parsed


def _resolve_put_side_plan(
    *,
    symbol: str,
    sell_put_cfg: dict,
    limit_expirations: int,
    available_expirations: list[str],
    trading_date: date | None,
    spot_reference: float | None,
    defaults: CandidateWindowDefaults = DEFAULT_SELL_PUT_WINDOW,
    source_prefix: str = "sell_put",
) -> OptionSideFetchPlan:
    window = resolve_candidate_window(sell_put_cfg, defaults=defaults)
    filtered = _filter_expirations_by_dte(
        symbol=symbol,
        available_expirations=available_expirations,
        trading_date=trading_date,
        min_dte=window.min_dte,
        max_dte=window.max_dte,
    )
    expirations = filtered
    configured_min_strike = _safe_float(sell_put_cfg.get("min_strike"))
    configured_max_strike = _safe_float(sell_put_cfg.get("max_strike"))
    if not expirations:
        return OptionSideFetchPlan(
            option_type="put",
            min_dte=window.min_dte,
            max_dte=window.max_dte,
            explicit_expirations=[],
            strike_window=StrikeWindowPlan(
                min_strike=None,
                max_strike=None,
                source=f"{source_prefix}.no_expirations",
                buffer_applied=False,
                buffer_pct=0.0,
                base_min_strike=None,
                base_max_strike=None,
            ),
            planning_reason="no eligible expirations; spot not required",
            source_fields=[
                f"{source_prefix}.min_dte",
                f"{source_prefix}.max_dte",
            ],
            spot_reference=spot_reference,
        )
    if spot_reference is None or spot_reference <= 0:
        raise RuntimeError(f"OpenD spot unavailable for {symbol} sell-put recall")
    max_strike = min(
        value
        for value in (configured_max_strike, spot_reference)
        if value is not None
    )
    derived_min_strike = max_strike * (1.0 - DEFAULT_FETCH_NEAR_BOUND_EXPAND_PCT)
    min_strike = max(
        value
        for value in (configured_min_strike, derived_min_strike)
        if value is not None
    )
    planning_reason = f"use configured {source_prefix} near/far bounds"
    source_fields = [f"{source_prefix}.min_strike", f"{source_prefix}.max_strike", f"{source_prefix}.min_dte", f"{source_prefix}.max_dte"]
    planning_reason = (
        f"derive {source_prefix} recall window from min(configured max, OpenD spot) and -20%"
    )
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
    strike_buffer_pct: float = 0.0,
) -> tuple[StrikeWindowPlan, str, list[str]]:
    del fallback_min_pct
    min_strike = _safe_float(sell_call_cfg.get("min_strike"))
    max_strike = _safe_float(sell_call_cfg.get("max_strike"))
    has_spot = spot_reference is not None and spot_reference > 0
    if has_spot:
        base_min = max(
            value for value in (min_strike, spot_reference) if value is not None
        )
        derived_max = base_min * (1.0 + float(fallback_max_pct))
        base_max = min(max_strike, derived_max) if max_strike is not None else derived_max
        buffer_pct = max(float(strike_buffer_pct), 0.0)
        fetch_max = (
            base_max
            if base_max < base_min
            else base_max * (1.0 + buffer_pct)
        )
        source = f"{source_prefix}.configured_bounds" if min_strike is not None or max_strike is not None else f"{source_prefix}.spot_derived_bounds"
        if buffer_pct > 0:
            reason = (
                f"use configured {source_prefix} bounds with {buffer_pct:.0%} fetch buffer"
                if min_strike is not None or max_strike is not None
                else f"derive {source_prefix} bounds from spot with {buffer_pct:.0%} fetch buffer"
            )
        else:
            reason = (
                f"use configured {source_prefix} bounds with exact spot-based 20% cap"
                if min_strike is not None or max_strike is not None
                else f"derive {source_prefix} exact 20% bounds from spot reference"
            )
        if base_max < base_min:
            reason = f"{source_prefix} has no feasible strike window because configured max is below recall min"
        fields = [f"{source_prefix}.min_strike", f"{source_prefix}.max_strike"] if min_strike is not None or max_strike is not None else ["spot"]
        if has_spot and "spot" not in fields:
            fields = fields + ["spot"]
        return (
            StrikeWindowPlan(
                min_strike=base_min,
                max_strike=fetch_max,
                source=source,
                buffer_applied=buffer_pct > 0,
                buffer_pct=buffer_pct,
                base_min_strike=base_min,
                base_max_strike=base_max,
            ),
            reason,
            fields,
        )
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
    trading_date: date | None,
    spot_reference: float | None,
    defaults: CandidateWindowDefaults = DEFAULT_SELL_CALL_WINDOW,
    source_prefix: str = "sell_call",
    dte_source_prefix: str | None = None,
    fallback_min_pct: float = DEFAULT_SELL_CALL_SPOT_FALLBACK_MIN_PCT,
    fallback_max_pct: float = DEFAULT_FETCH_NEAR_BOUND_EXPAND_PCT,
    strike_buffer_pct: float = 0.0,
) -> OptionSideFetchPlan:
    window = resolve_candidate_window(sell_call_cfg, defaults=defaults)
    filtered = _filter_expirations_by_dte(
        symbol=symbol,
        available_expirations=available_expirations,
        trading_date=trading_date,
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
        strike_buffer_pct=strike_buffer_pct,
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
    trading_date: date | None = None,
    spot_reference: float | None,
) -> OptionSideFetchPlan:
    cfg = dict(yield_enhancement_cfg or {})
    call_cfg = dict(cfg.get("call") or {})
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
        trading_date=trading_date,
        spot_reference=spot_reference,
        defaults=call_window,
        source_prefix="combo_yield.call",
        dte_source_prefix=dte_source_prefix,
        fallback_min_pct=0.0,
        fallback_max_pct=DEFAULT_COMBO_YIELD_CALL_FETCH_MAX_PCT,
        strike_buffer_pct=DEFAULT_COMBO_YIELD_CALL_STRIKE_BUFFER_PCT,
    )
    return plan


def _unique_preserve_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def _physical_host(value: Any) -> str:
    return str(value or "").strip().lower()


def _expiration_discovery_cache_key(
    *,
    symbol: str,
    source: str,
    host: str,
    port: int,
    trading_date: str,
) -> ExpirationDiscoveryCacheKey:
    return (
        str(symbol or "").strip().upper(),
        str(source or "").strip().lower(),
        _physical_host(host),
        int(port),
        trading_date,
    )


def _spot_observation_cache_key(
    *,
    symbol: str,
    source: str,
    host: str,
    port: int,
    trading_date: str,
) -> SpotObservationCacheKey:
    return _expiration_discovery_cache_key(
        symbol=symbol,
        source=source,
        host=host,
        port=port,
        trading_date=trading_date,
    )


def _cached_spot_observation(
    *,
    cache: MutableMapping[
        SpotObservationCacheKey,
        OpeningUnderlierObservation | None,
    ],
    cache_key: SpotObservationCacheKey,
) -> OpeningUnderlierObservation | None:
    cached = cache[cache_key]
    if cached is None:
        return None
    if not isinstance(cached, OpeningUnderlierObservation):
        raise RuntimeError("spot-observation cache contains an invalid value")
    spot = _safe_float(cached.last_price)
    if spot is None or not math.isfinite(spot) or spot <= 0:
        raise RuntimeError("spot-observation cache contains an invalid value")
    return cached


def _expiration_discovery_cache_entries(
    cache: MutableMapping[
        ExpirationDiscoveryCacheKey,
        OptionExpirationDiscoveryResult,
    ],
    *,
    symbol: str,
    source: str,
    host: str,
    port: int,
) -> list[tuple[ExpirationDiscoveryCacheKey, Any]]:
    prefix = (
        str(symbol or "").strip().upper(),
        str(source or "").strip().lower(),
        _physical_host(host),
        int(port),
    )
    return [
        (key, value)
        for key, value in cache.items()
        if isinstance(key, tuple)
        and len(key) == 5
        and key[:4] == prefix
    ]


def _expiration_discovery_cache_identity_matches(
    *,
    cache_key: ExpirationDiscoveryCacheKey,
    result: Any,
) -> bool:
    if not isinstance(result, OptionExpirationDiscoveryResult):
        return False
    identity = result.request_identity
    if not isinstance(identity, dict):
        return False
    expected_symbol, expected_source, expected_host, expected_port, expected_date = (
        cache_key
    )
    return (
        identity.get("symbol") == expected_symbol
        and identity.get("source") == expected_source
        and _physical_host(identity.get("host")) == expected_host
        and identity.get("port") == expected_port
        and identity.get("trading_date") == expected_date
        and _strict_iso_expiration(expected_date) == expected_date
    )


def _expiration_discovery_cache_failure(
    *,
    symbol: str,
    source: str,
    host: str,
    port: int,
    trading_date: str | None,
    reason_code: str,
    error: str,
) -> OptionExpirationDiscoveryResult:
    return OptionExpirationDiscoveryResult(
        outcome="parse_error",
        reason_code=reason_code,
        expirations=[],
        observed_at_utc=None,
        completed_at_utc=(
            datetime.now(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        ),
        request_identity={
            "symbol": str(symbol or "").strip().upper(),
            "underlier": None,
            "source": str(source or "").strip().lower(),
            "host": _physical_host(host),
            "port": int(port),
            "trading_date": trading_date,
        },
        error=error,
    )


def _freeze_expiration_discovery_trading_date(
    *,
    base: Path,
    symbol: str,
) -> str:
    underlier = opend_utils.normalize_underlier(symbol, base_dir=base)
    raw_trading_date = opend_utils.get_trading_date(
        underlier.market
    ).isoformat()
    trading_date = _strict_iso_expiration(raw_trading_date)
    if trading_date is None:
        raise ValueError(
            f"invalid expiration-discovery trading date for {symbol}"
        )
    return trading_date


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
        min_dte_values = [
            plan.min_dte for plan in plans if plan.min_dte is not None
        ]
        max_dte_values = [
            plan.max_dte for plan in plans if plan.max_dte is not None
        ]
        merged_min_dte = (
            None
            if any(plan.min_dte is None for plan in plans)
            else min(min_dte_values)
        )
        merged_max_dte = (
            None
            if any(plan.max_dte is None for plan in plans)
            else max(max_dte_values)
        )
        min_values = [
            plan.strike_window.min_strike
            for plan in plans
            if plan.strike_window.min_strike is not None
        ]
        max_values = [
            plan.strike_window.max_strike
            for plan in plans
            if plan.strike_window.max_strike is not None
        ]
        merged_min_strike = (
            None
            if any(
                plan.strike_window.min_strike is None for plan in plans
            )
            else min(min_values)
        )
        merged_max_strike = (
            None
            if any(
                plan.strike_window.max_strike is None for plan in plans
            )
            else max(max_values)
        )
        base_min_values = [plan.strike_window.base_min_strike for plan in plans if plan.strike_window.base_min_strike is not None]
        base_max_values = [plan.strike_window.base_max_strike for plan in plans if plan.strike_window.base_max_strike is not None]
        source_fields = _unique_preserve_order([field for plan in plans for field in plan.source_fields])
        exact_strikes_by_expiration: dict[str, set[float]] = {}
        for plan in plans:
            for expiration, strikes in (
                plan.required_exact_strikes_by_expiration.items()
            ):
                exact_strikes_by_expiration.setdefault(expiration, set()).update(
                    float(strike) for strike in strikes
                )
        merged.append(
            OptionSideFetchPlan(
                option_type=option_type,
                min_dte=merged_min_dte,
                max_dte=merged_max_dte,
                explicit_expirations=expirations,
                strike_window=StrikeWindowPlan(
                    min_strike=merged_min_strike,
                    max_strike=merged_max_strike,
                    source="+".join(_unique_preserve_order([plan.strike_window.source for plan in plans])),
                    buffer_applied=any(plan.strike_window.buffer_applied for plan in plans),
                    buffer_pct=max((plan.strike_window.buffer_pct for plan in plans), default=0.0),
                    base_min_strike=(min(base_min_values) if base_min_values else None),
                    base_max_strike=(max(base_max_values) if base_max_values else None),
                ),
                planning_reason=f"merged {option_type} requirements across enabled strategies",
                required_exact_strikes_by_expiration={
                    expiration: sorted(strikes)
                    for expiration, strikes in sorted(
                        exact_strikes_by_expiration.items()
                    )
                },
                source_fields=source_fields,
                spot_reference=next((plan.spot_reference for plan in plans if plan.spot_reference is not None), None),
            )
        )
    return merged


def _validate_ready_position_requirements(
    requirements: list[dict[str, Any]] | None,
    *,
    symbol: str,
) -> list[dict[str, Any]]:
    ready: list[dict[str, Any]] = []
    for index, requirement in enumerate(requirements or []):
        if not isinstance(requirement, dict):
            raise RequiredDataPlanningError(
                symbol=symbol,
                requirement_index=index,
                field_name="requirement",
            )
        if str(requirement.get("planning_status") or "ready") != "ready":
            continue
        option_type = requirement.get("option_type")
        if not isinstance(option_type, str) or option_type not in {"put", "call"}:
            raise RequiredDataPlanningError(
                symbol=symbol,
                requirement_index=index,
                field_name="option_type",
            )
        expiration = _strict_iso_expiration(requirement.get("expiration"))
        if expiration is None:
            raise RequiredDataPlanningError(
                symbol=symbol,
                requirement_index=index,
                field_name="expiration",
            )
        strike = _strict_positive_finite_strike(requirement.get("strike"))
        if strike is None:
            raise RequiredDataPlanningError(
                symbol=symbol,
                requirement_index=index,
                field_name="strike",
            )
        ready.append(
            {
                **requirement,
                "option_type": option_type,
                "expiration": expiration,
                "strike": strike,
            }
        )
    return ready


def _position_requirement_side_plans(
    requirements: list[dict[str, Any]],
    *,
    trading_date: date | None,
    symbol: str,
) -> list[OptionSideFetchPlan]:
    grouped: dict[OptionSide, list[dict[str, Any]]] = {
        "put": [],
        "call": [],
    }
    for requirement in requirements:
        option_type = requirement["option_type"]
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
        strikes = [float(item["strike"]) for item in items]
        dtes: list[int] = []
        if trading_date is not None:
            for requirement_index, item in enumerate(items):
                expiration_date = date.fromisoformat(item["expiration"])
                dte = (expiration_date - trading_date).days
                if dte < 0:
                    raise RequiredDataPlanningError(
                        symbol=symbol,
                        requirement_index=requirement_index,
                        field_name="expiration",
                        reason_code=(
                            "position_expiration_before_trading_date"
                        ),
                    )
                dtes.append(dte)
        exact_strikes_by_expiration = {
            expiration: sorted(
                {
                    float(item["strike"])
                    for item in items
                    if item["expiration"] == expiration
                }
            )
            for expiration in expirations
        }
        plans.append(
            OptionSideFetchPlan(
                option_type=option_type,
                min_dte=(min(dtes) if dtes else None),
                max_dte=(max(dtes) if dtes else None),
                explicit_expirations=expirations,
                strike_window=StrikeWindowPlan(
                    min_strike=(min(strikes) if strikes else None),
                    max_strike=(max(strikes) if strikes else None),
                    source="close_advice.position_requirements",
                ),
                planning_reason="cover active Close Advice position contracts",
                required_exact_strikes_by_expiration=(
                    exact_strikes_by_expiration
                ),
                source_fields=["close_advice.position_requirements"],
            )
        )
    return plans


def _expiration_discovery_trading_date(
    *,
    expiration_discovery: OptionExpirationDiscoveryResult,
    symbol: str,
    has_ready_requirements: bool,
    expected_trading_date: str | None,
) -> date | None:
    raw_trading_date = expiration_discovery.request_identity.get(
        "trading_date"
    )
    normalized = _strict_iso_expiration(raw_trading_date)
    if normalized is not None:
        if (
            expected_trading_date is not None
            and normalized != expected_trading_date
        ):
            raise RequiredDataPlanningError(
                symbol=symbol,
                requirement_index=0,
                field_name="expiration_discovery.trading_date",
                reason_code="expiration_discovery_trading_date_mismatch",
            )
        return date.fromisoformat(normalized)
    if expiration_discovery.complete:
        raise RequiredDataPlanningError(
            symbol=symbol,
            requirement_index=0,
            field_name="expiration_discovery.trading_date",
            reason_code=(
                "position_dte_unavailable"
                if has_ready_requirements
                else "strategy_dte_unavailable"
            ),
        )
    return None


def _merge_side_plans(
    *,
    symbol: str,
    limit_expirations: int,
    host: str,
    port: int,
    side_plans: list[OptionSideFetchPlan],
    trading_date: date | None = None,
    include_realized_volatility: bool = False,
) -> list[RequiredDataFetchSpec]:
    if not isinstance(include_realized_volatility, bool):
        raise TypeError("required-data RV authority must be a bool")
    groups: dict[tuple[str, ...], list[OptionSideFetchPlan]] = {}
    for plan in side_plans:
        if not plan.explicit_expirations:
            continue
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
                include_realized_volatility=include_realized_volatility,
                side_plans=list(plans),
                planning_reason=("shared expirations -> merged request" if len(plans) > 1 else "single-side request"),
                trading_date=(
                    trading_date.isoformat()
                    if trading_date is not None
                    else None
                ),
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
    spot_observation_cache: (
        MutableMapping[
            SpotObservationCacheKey,
            OpeningUnderlierObservation | None,
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
    ready_position_requirements = _validate_ready_position_requirements(
        position_requirements,
        symbol=symbol,
    )
    sell_put_cfg = dict(sell_put_cfg or {})
    sell_call_cfg = dict(sell_call_cfg or {})
    resolved_yield_enhancement_cfg = dict(yield_enhancement_cfg or {})
    sell_put_semantics = strategy_semantics_for_side_config(family=SELL_PUT_FAMILY, side_cfg=sell_put_cfg)
    sell_call_semantics = strategy_semantics_for_side_config(family=SELL_CALL_FAMILY, side_cfg=sell_call_cfg)
    expiration_discovery: OptionExpirationDiscoveryResult | None = None
    discovery_cache_key: ExpirationDiscoveryCacheKey | None = None
    frozen_trading_date: str | None = None
    trading_date_resolution_error: Exception | None = None
    cache_entries = (
        _expiration_discovery_cache_entries(
            expiration_discovery_cache,
            symbol=symbol,
            source=fetch_source,
            host=fetch_host,
            port=fetch_port,
        )
        if expiration_discovery_cache is not None
        else []
    )
    if len(cache_entries) > 1:
        expiration_discovery = _expiration_discovery_cache_failure(
            symbol=symbol,
            source=fetch_source,
            host=fetch_host,
            port=fetch_port,
            trading_date=None,
            reason_code="expiration_discovery_cache_ambiguous",
            error=(
                "multiple expiration-discovery cache dates exist for one "
                "physical binding"
            ),
        )
    elif cache_entries:
        candidate_key, candidate_result = cache_entries[0]
        candidate_date = _strict_iso_expiration(candidate_key[4])
        if (
            candidate_date is None
            or not _expiration_discovery_cache_identity_matches(
                cache_key=candidate_key,
                result=candidate_result,
            )
        ):
            expiration_discovery = _expiration_discovery_cache_failure(
                symbol=symbol,
                source=fetch_source,
                host=fetch_host,
                port=fetch_port,
                trading_date=None,
                reason_code=(
                    "expiration_discovery_cache_identity_invalid"
                ),
                error=(
                    "expiration-discovery cache identity does not match "
                    "its physical binding and trading-date key"
                ),
            )
        else:
            frozen_trading_date = candidate_date
            discovery_cache_key = candidate_key
            expiration_discovery = candidate_result
    else:
        try:
            frozen_trading_date = (
                _freeze_expiration_discovery_trading_date(
                    base=base,
                    symbol=symbol,
                )
            )
        except Exception as exc:
            trading_date_resolution_error = exc
    underlier_observation: OpeningUnderlierObservation | None = None
    spot_reference: float | None = None
    if frozen_trading_date is not None:
        spot_cache_key = _spot_observation_cache_key(
            symbol=symbol,
            source=fetch_source,
            host=fetch_host,
            port=fetch_port,
            trading_date=frozen_trading_date,
        )
        if (
            spot_observation_cache is not None
            and spot_cache_key in spot_observation_cache
        ):
            underlier_observation = _cached_spot_observation(
                cache=spot_observation_cache,
                cache_key=spot_cache_key,
            )
        else:
            underlier_observation = _resolve_underlier_observation(
                symbol=symbol,
                host=fetch_host,
                port=fetch_port,
                base_dir=base,
                snapshot_max_wait_sec=snapshot_max_wait_sec,
                snapshot_window_sec=snapshot_window_sec,
                snapshot_max_calls=snapshot_max_calls,
            )
            if spot_observation_cache is not None:
                spot_observation_cache[spot_cache_key] = underlier_observation
    if underlier_observation is not None:
        spot_reference = _safe_float(underlier_observation.last_price)
    spot_observation_complete = (
        underlier_observation is not None
        and underlier_observation.status == "ready"
        and spot_reference is not None
        and math.isfinite(float(spot_reference))
        and float(spot_reference) > 0
    )
    if (
        discovery_cache_key is None
        and frozen_trading_date is not None
        and expiration_discovery is None
    ):
        try:
            discovery_cache_key = _expiration_discovery_cache_key(
                symbol=symbol,
                source=fetch_source,
                host=fetch_host,
                port=fetch_port,
                trading_date=frozen_trading_date,
            )
        except Exception:
            discovery_cache_key = None
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
            trading_date=(frozen_trading_date or ""),
        )
        if (
            trading_date_resolution_error is not None
            and expiration_discovery.reason_code
            == "request_identity_invalid"
        ):
            expiration_discovery = replace(
                expiration_discovery,
                error=(
                    f"{type(trading_date_resolution_error).__name__}: "
                    f"{trading_date_resolution_error}"
                ),
            )
        if (
            expiration_discovery_cache is not None
            and discovery_cache_key is not None
            and _expiration_discovery_cache_identity_matches(
                cache_key=discovery_cache_key,
                result=expiration_discovery,
            )
        ):
            expiration_discovery_cache[discovery_cache_key] = (
                expiration_discovery
            )
    trading_date = _expiration_discovery_trading_date(
        expiration_discovery=expiration_discovery,
        symbol=symbol,
        has_ready_requirements=bool(ready_position_requirements),
        expected_trading_date=frozen_trading_date,
    )
    position_side_plans = _position_requirement_side_plans(
        ready_position_requirements,
        trading_date=trading_date,
        symbol=symbol,
    )
    available_expirations = list(expiration_discovery.expirations)

    side_plans: list[OptionSideFetchPlan] = []
    spot_observation_error: str | None = None
    yield_enhancement_policy = derive_yield_enhancement_policy(resolved_yield_enhancement_cfg)
    combo_yield_enabled = bool(yield_enhancement_policy.enabled)
    if want_put or combo_yield_enabled:
        try:
            side_plans.append(
                _resolve_put_side_plan(
                    symbol=symbol,
                    sell_put_cfg=sell_put_cfg,
                    limit_expirations=limit_expirations,
                    available_expirations=available_expirations,
                    trading_date=trading_date,
                    spot_reference=spot_reference,
                )
            )
        except RuntimeError as exc:
            spot_observation_error = str(exc)
    if want_call:
        side_plans.append(
            _resolve_call_side_plan(
                symbol=symbol,
                sell_call_cfg=sell_call_cfg,
                limit_expirations=limit_expirations,
                available_expirations=available_expirations,
                trading_date=trading_date,
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
                trading_date=trading_date,
                spot_reference=spot_reference,
            )
        )
    side_plans.extend(position_side_plans)
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
    elif spot_observation_error is not None:
        projection_outcome = "provider_error"
    elif side_plans and not projected_expirations:
        projection_outcome = "projection_empty"
    else:
        projection_outcome = "success_rows"
    require_realized_volatility = bool(
        (want_put and sell_put_semantics.scan_requires_rv)
        or (want_call and sell_call_semantics.scan_requires_rv)
        or (
            combo_yield_enabled
            and yield_enhancement_policy.requires_realized_volatility
        )
        or any(
            bool(item.get("requires_realized_volatility"))
            for item in ready_position_requirements
        )
    )
    return RequiredDataFetchPlanBundle(
        symbol=symbol,
        spot_reference=spot_reference,
        underlier_observation=underlier_observation,
        side_plans=side_plans,
        merged_specs=_merge_side_plans(
            symbol=symbol,
            limit_expirations=limit_expirations,
            host=fetch_host,
            port=fetch_port,
            side_plans=(
                side_plans if projection_outcome == "success_rows" else []
            ),
            trading_date=trading_date,
            include_realized_volatility=require_realized_volatility,
        ),
        expiration_discovery_complete=expiration_discovery.complete,
        expiration_discovery_error=expiration_discovery.error,
        expiration_discovery=expiration_discovery,
        projection_outcome=projection_outcome,
        projected_expirations=projected_expirations,
        require_realized_volatility=require_realized_volatility,
        spot_observation_complete=spot_observation_complete,
        spot_observation_error=spot_observation_error,
    )
