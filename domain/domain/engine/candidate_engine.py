from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING
from typing import Any, Literal

from domain.domain.decision_state_fingerprint import canonical_sha256
from domain.domain.fee_calc import estimate_futu_option_sell_fee


CANDIDATE_ENGINE_SCHEMA_VERSION = "1.0"
SCHEMA_KIND_CANDIDATE_DECISION = "candidate_decision"
REPLACEMENT_CANDIDATE_SCHEMA_VERSION = "replacement_candidate_decision.v1"
REPLACEMENT_ACCEPTED_OPENING = "accepted_opening"
REPLACEMENT_CAPACITY_DEFERRED = "capacity_deferred_to_allocator"
REPLACEMENT_REJECTED_INVARIANT = "rejected_invariant"

StrategyMode = Literal["put", "call"]
OPENING_CANDIDATE_NEAR_RETURN_THRESHOLD = 0.002
OPENING_CANDIDATE_MIN_ANNUALIZED_RETURN = 0.10
OPENING_CANDIDATE_MIN_NET_PREMIUM_CNY = 50.0
OPENING_CANDIDATE_MIN_IV_RV_RATIO = 1.10
OPENING_CANDIDATE_MIN_IV_MINUS_RV = 0.05
OPENING_CANDIDATE_MAX_SPREAD_RATIO = 0.40
SELL_PUT_NEAR_RETURN_THRESHOLD = OPENING_CANDIDATE_NEAR_RETURN_THRESHOLD

STAGE_INPUT_NORMALIZATION = "stage0_input_normalization"
STAGE_HARD_CONSTRAINTS = "stage1_hard_constraints"
STAGE_RETURN_FLOOR = "stage2_return_floor"
STAGE_RISK_FILTER = "stage3_risk_filter"
STAGE_RANKING = "stage4_ranking"

CANDIDATE_STAGE_ORDER: tuple[str, ...] = (
    STAGE_INPUT_NORMALIZATION,
    STAGE_HARD_CONSTRAINTS,
    STAGE_RETURN_FLOOR,
    STAGE_RISK_FILTER,
    STAGE_RANKING,
)

REJECT_INPUT_MISSING = "input_missing"
REJECT_INPUT_INVALID = "input_invalid"
REJECT_HARD_DTE = "hard_dte"
REJECT_HARD_STRIKE = "hard_strike"
REJECT_HARD_CAPACITY_PUT = "hard_capacity_put"
REJECT_HARD_CAPACITY_CALL = "hard_capacity_call"
REJECT_RETURN_ANNUALIZED = "return_annualized"
REJECT_RETURN_NET_INCOME = "return_net_income"
REJECT_RETURN_NET_PREMIUM_CNY = "return_net_premium_cny"
REJECT_RISK_OPEN_INTEREST = "risk_open_interest"
REJECT_RISK_VOLUME = "risk_volume"
REJECT_RISK_SPREAD = "risk_spread"
REJECT_RISK_IV_RV_RATIO = "risk_iv_rv_ratio"
REJECT_RISK_IV_MINUS_RV = "risk_iv_minus_rv"
REJECT_RISK_EARNINGS_UNAVAILABLE = "risk_earnings_unavailable"
REJECT_RISK_EARNINGS_EVENT = "risk_earnings_event"
REJECT_RISK_EVENT_WARN = "risk_event_warn"
REJECT_RISK_EVENT_REJECT = "risk_event_reject"
REJECT_RISK_INSURANCE_UNDERWRITING = "risk_insurance_underwriting"

CANDIDATE_REJECT_REASONS: tuple[str, ...] = (
    REJECT_INPUT_MISSING,
    REJECT_INPUT_INVALID,
    REJECT_HARD_DTE,
    REJECT_HARD_STRIKE,
    REJECT_HARD_CAPACITY_PUT,
    REJECT_HARD_CAPACITY_CALL,
    REJECT_RETURN_ANNUALIZED,
    REJECT_RETURN_NET_INCOME,
    REJECT_RETURN_NET_PREMIUM_CNY,
    REJECT_RISK_OPEN_INTEREST,
    REJECT_RISK_VOLUME,
    REJECT_RISK_SPREAD,
    REJECT_RISK_IV_RV_RATIO,
    REJECT_RISK_IV_MINUS_RV,
    REJECT_RISK_EARNINGS_UNAVAILABLE,
    REJECT_RISK_EARNINGS_EVENT,
    REJECT_RISK_EVENT_WARN,
    REJECT_RISK_EVENT_REJECT,
    REJECT_RISK_INSURANCE_UNDERWRITING,
)

LEGACY_REJECT_RULE_REASON_MAP: dict[str, str] = {
    "input_missing": REJECT_INPUT_MISSING,
    "input_invalid": REJECT_INPUT_INVALID,
    "dte": REJECT_HARD_DTE,
    "strike": REJECT_HARD_STRIKE,
    "put_cash_capacity": REJECT_HARD_CAPACITY_PUT,
    "call_cover_capacity": REJECT_HARD_CAPACITY_CALL,
    "min_annualized_return": REJECT_RETURN_ANNUALIZED,
    "min_net_income": REJECT_RETURN_NET_INCOME,
    "min_open_interest": REJECT_RISK_OPEN_INTEREST,
    "min_volume": REJECT_RISK_VOLUME,
    "max_spread_ratio": REJECT_RISK_SPREAD,
    "event_risk_warn": REJECT_RISK_EVENT_WARN,
    "event_risk_reject": REJECT_RISK_EVENT_REJECT,
}

LEGACY_REJECT_RULE_STAGE_MAP: dict[str, str] = {
    "input_missing": STAGE_INPUT_NORMALIZATION,
    "input_invalid": STAGE_INPUT_NORMALIZATION,
    "dte": STAGE_HARD_CONSTRAINTS,
    "strike": STAGE_HARD_CONSTRAINTS,
    "put_cash_capacity": STAGE_HARD_CONSTRAINTS,
    "call_cover_capacity": STAGE_HARD_CONSTRAINTS,
    "min_annualized_return": STAGE_RETURN_FLOOR,
    "min_net_income": STAGE_RETURN_FLOOR,
    "min_open_interest": STAGE_RISK_FILTER,
    "min_volume": STAGE_RISK_FILTER,
    "max_spread_ratio": STAGE_RISK_FILTER,
    "event_risk_warn": STAGE_RISK_FILTER,
    "event_risk_reject": STAGE_RISK_FILTER,
}

CANDIDATE_REJECT_REASON_RULE_MAP: dict[str, str] = {
    reason: rule for rule, reason in LEGACY_REJECT_RULE_REASON_MAP.items()
}

COMMON_CRITICAL_FIELDS: tuple[str, ...] = (
    "symbol",
    "option_type",
    "expiration",
    "dte",
    "spot",
    "strike",
    "mid",
    "multiplier",
)

NUMERIC_INPUT_FIELDS: tuple[str, ...] = (
    "dte",
    "spot",
    "strike",
    "bid",
    "ask",
    "last_price",
    "mid",
    "open_interest",
    "volume",
    "implied_volatility",
    "delta",
    "multiplier",
)


@dataclass(frozen=True)
class CandidateReject:
    stage: str
    reason: str
    message: str = ""
    metric_value: Any = None
    threshold: Any = None

    def to_payload(self) -> dict[str, Any]:
        return normalize_candidate_reject(self)


@dataclass(frozen=True)
class CandidateScoreWeights:
    annualized_return: float = 1.0
    net_income: float = 1e-6
    liquidity: float = 0.0
    risk_distance: float = 0.0
    vol_edge: float = 0.0
    delta_target: float = 0.0
    concentration: float = 0.0
    path_risk: float = 0.0


@dataclass(frozen=True)
class CandidateStrategyScore:
    total: float
    components: dict[str, float]
    warnings: tuple[str, ...] = ()


class CandidateCalculationError(ValueError):
    """Stable fail-closed reason from the canonical candidate calculation."""

    def __init__(
        self,
        reason: str,
        message: str,
        *,
        metric_value: Any = None,
        threshold: Any = None,
    ) -> None:
        super().__init__(message)
        self.reason = str(reason)
        self.message = str(message)
        self.metric_value = metric_value
        self.threshold = threshold

    def to_payload(self) -> dict[str, Any]:
        return {
            "rule": self.reason,
            "message": self.message,
            "metric_value": self.metric_value,
            "threshold": self.threshold,
        }


def normalize_strategy_mode(mode: Any) -> StrategyMode:
    mode_norm = str(mode or "").strip().lower()
    if mode_norm not in {"put", "call"}:
        raise ValueError(f"unsupported candidate strategy mode: {mode}")
    return mode_norm  # type: ignore[return-value]


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and not value.strip():
        return True
    try:
        return bool(value != value)
    except Exception:
        return False


def _coerce_float(value: Any) -> float | None:
    if _is_missing(value):
        return None
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except Exception:
        return None
    return parsed if math.isfinite(parsed) else None


def _required_positive_float(raw: dict[str, Any], field: str) -> float:
    value = _coerce_float(raw.get(field))
    if value is None:
        raise CandidateCalculationError(
            f"{field}_missing_or_invalid",
            f"{field} must be a finite positive number",
            metric_value=raw.get(field),
            threshold="> 0",
        )
    if value <= 0:
        raise CandidateCalculationError(
            f"{field}_non_positive",
            f"{field} must be positive",
            metric_value=value,
            threshold=0,
        )
    return value


def _required_positive_int(raw: dict[str, Any], field: str) -> int:
    value = _required_positive_float(raw, field)
    integer = int(value)
    if float(integer) != value:
        raise CandidateCalculationError(
            f"{field}_not_integer",
            f"{field} must be a positive integer",
            metric_value=value,
            threshold="positive integer",
        )
    return integer


def _tick_ceiling(value: float, tick: float) -> float:
    value_decimal = Decimal(str(value))
    tick_decimal = Decimal(str(tick))
    units = (value_decimal / tick_decimal).to_integral_value(rounding=ROUND_CEILING)
    return float(units * tick_decimal)


def calculate_opening_candidate_metrics(
    raw: dict[str, Any] | Any,
    *,
    mode: StrategyMode | str,
    avg_cost: float | None = None,
    cny_per_currency_unit: float | None = None,
) -> dict[str, Any]:
    """Calculate the sole formal opening price, fee and return contract.

    The input is the normalized OpenD row produced by Phase 1. The function is
    deliberately strict: it never substitutes last for bid/ask, never defaults
    a multiplier, never falls back from term-matched RV, and always prices the
    candidate at the tick-rounded limit used for a patient sell order.
    """

    mode_norm = normalize_strategy_mode(mode)
    if not isinstance(raw, dict):
        raise CandidateCalculationError(
            "candidate_input_invalid",
            "candidate input must be an object",
            metric_value=type(raw).__name__,
            threshold="object",
        )
    opening_status = str(raw.get("opening_contract_status") or "").strip().lower()
    if opening_status != "ready":
        raise CandidateCalculationError(
            "opening_contract_not_ready",
            "normalized OpenD opening contract is not ready",
            metric_value={
                "status": opening_status or None,
                "reason_codes": raw.get("opening_contract_reason_codes"),
            },
            threshold="ready",
        )
    underlier_status = str(raw.get("underlier_observation_status") or "").strip().lower()
    if underlier_status != "ready":
        raise CandidateCalculationError(
            "opening_underlier_not_ready",
            "normalized OpenD underlier observation is not ready",
            metric_value={
                "status": underlier_status or None,
                "reason_code": raw.get("underlier_observation_reason_code"),
            },
            threshold="ready",
        )

    option_type = str(raw.get("option_type") or "").strip().lower()
    if option_type != mode_norm:
        raise CandidateCalculationError(
            "option_type_mismatch",
            "option type does not match strategy mode",
            metric_value=option_type or None,
            threshold=mode_norm,
        )
    if str(raw.get("option_standard_type") or "").strip().upper() != "STANDARD":
        raise CandidateCalculationError(
            "option_non_standard",
            "only OpenD STANDARD option contracts are eligible",
            metric_value=raw.get("option_standard_type"),
            threshold="STANDARD",
        )
    if not str(raw.get("stock_owner") or "").strip():
        raise CandidateCalculationError(
            "option_stock_owner_missing",
            "OpenD stock_owner binding is required",
            threshold="non-empty",
        )

    bid = _required_positive_float(raw, "bid")
    ask = _required_positive_float(raw, "ask")
    if ask < bid:
        raise CandidateCalculationError(
            "option_ask_below_bid",
            "ask must be greater than or equal to bid",
            metric_value={"bid": bid, "ask": ask},
            threshold="ask >= bid",
        )
    price_tick = _required_positive_float(raw, "price_tick")
    multiplier = _required_positive_int(raw, "multiplier")
    chain_multiplier = _required_positive_int(raw, "chain_multiplier")
    snapshot_multiplier = _required_positive_int(raw, "snapshot_multiplier")
    if len({multiplier, chain_multiplier, snapshot_multiplier}) != 1:
        raise CandidateCalculationError(
            "option_multiplier_conflict",
            "chain and snapshot multiplier bindings disagree",
            metric_value={
                "multiplier": multiplier,
                "chain_multiplier": chain_multiplier,
                "snapshot_multiplier": snapshot_multiplier,
            },
            threshold="all equal",
        )

    dte = _required_positive_int(raw, "dte")
    strike = _required_positive_float(raw, "strike")
    spot = _required_positive_float(raw, "spot")
    implied_volatility = _required_positive_float(raw, "implied_volatility")
    rv_status = str(raw.get("term_matched_rv_status") or "").strip().lower()
    if rv_status != "ready":
        raise CandidateCalculationError(
            "term_matched_rv_unavailable",
            "term-matched realized volatility is not ready",
            metric_value={
                "status": rv_status or None,
                "reason": raw.get("term_matched_rv_reason"),
            },
            threshold="ready",
        )
    term_matched_rv = _required_positive_float(raw, "term_matched_rv")

    raw_mid = (bid + ask) / 2.0
    raw_spread = ask - bid
    spread_ratio = raw_spread / raw_mid
    sell_limit = _tick_ceiling(raw_mid, price_tick)
    try:
        fee_estimate = estimate_futu_option_sell_fee(
            raw.get("currency"),
            sell_limit,
            contracts=1,
            multiplier=multiplier,
        )
    except ValueError as exc:
        raise CandidateCalculationError(
            "option_fee_estimate_unavailable",
            "versioned option sell-fee estimate is unavailable",
            metric_value=raw.get("currency"),
            threshold="supported versioned fee schedule",
        ) from exc
    gross_premium = sell_limit * multiplier
    net_premium = gross_premium - fee_estimate.amount
    if net_premium <= 0:
        raise CandidateCalculationError(
            "net_premium_non_positive",
            "estimated net premium must be positive",
            metric_value=net_premium,
            threshold=0,
        )

    iv_rv_ratio = implied_volatility / term_matched_rv
    iv_minus_rv = implied_volatility - term_matched_rv
    out: dict[str, Any] = {
        "raw_mid": round(raw_mid, 10),
        "mid": round(raw_mid, 10),
        "raw_spread": round(raw_spread, 10),
        "spread": round(raw_spread, 10),
        "spread_ratio": round(spread_ratio, 10),
        "price_tick": price_tick,
        "sell_limit": sell_limit,
        "gross_premium": round(gross_premium, 6),
        "gross_income": round(gross_premium, 6),
        "estimated_full_sell_fees": round(fee_estimate.amount, 6),
        "futu_fee": round(fee_estimate.amount, 6),
        "fee_schedule_version": fee_estimate.fee_schedule_version,
        "fee_basis": fee_estimate.fee_basis,
        "fee_schedule_url": fee_estimate.fee_schedule_url,
        "net_premium": round(net_premium, 6),
        "net_income": round(net_premium, 6),
        "implied_volatility": implied_volatility,
        "term_matched_rv": term_matched_rv,
        "iv_rv_ratio": round(iv_rv_ratio, 6),
        "iv_minus_rv": round(iv_minus_rv, 6),
    }
    cny_rate = _coerce_float(cny_per_currency_unit)
    if cny_rate is not None and cny_rate > 0:
        net_premium_cny = net_premium * cny_rate
        out["net_premium_cny"] = round(net_premium_cny, 6)
        out["net_income_cny"] = round(net_premium_cny, 6)

    if mode_norm == "put":
        assignment_notional = strike * multiplier
        net_cash_basis = assignment_notional - net_premium
        if net_cash_basis <= 0:
            raise CandidateCalculationError(
                "net_cash_basis_non_positive",
                "sell put net cash basis must be positive",
                metric_value=net_cash_basis,
                threshold=0,
            )
        period_return = net_premium / net_cash_basis
        breakeven = strike - (net_premium / multiplier)
        out.update(
            {
                "assignment_notional": round(assignment_notional, 6),
                "put_cash_required": round(assignment_notional, 6),
                "net_cash_basis": round(net_cash_basis, 6),
                "cash_basis": round(net_cash_basis, 6),
                "period_net_return": round(period_return, 10),
                "period_net_return_on_cash_basis": round(period_return, 10),
                "annualized_net_return": round(period_return * 365.0 / dte, 10),
                "annualized_net_return_on_cash_basis": round(period_return * 365.0 / dte, 10),
                "breakeven": round(breakeven, 6),
                "net_assignment_discount_pct": round((spot - breakeven) / spot, 10),
                "otm_pct": round((spot - strike) / spot, 10),
            }
        )
        return out

    current_market_value = spot * multiplier
    period_return = net_premium / current_market_value
    out.update(
        {
            "current_market_value": round(current_market_value, 6),
            "period_net_premium_return": round(period_return, 10),
            "period_net_return": round(period_return, 10),
            "annualized_net_premium_return": round(period_return * 365.0 / dte, 10),
            "strike_above_spot_pct": round((strike - spot) / spot, 10),
            "strike_upside_margin_pct": round((strike - spot) / spot, 10),
        }
    )
    avg_cost_value = _coerce_float(avg_cost)
    if avg_cost_value is not None and avg_cost_value > 0:
        out["strike_above_cost_pct"] = round((strike - avg_cost_value) / avg_cost_value, 10)
        out["if_exercised_total_return"] = round(
            (((strike - avg_cost_value) * multiplier) + net_premium)
            / (avg_cost_value * multiplier),
            10,
        )
    return out


def evaluate_opening_candidate_policy(
    raw: dict[str, Any] | Any,
    *,
    mode: StrategyMode | str,
    min_annualized_return: float = OPENING_CANDIDATE_MIN_ANNUALIZED_RETURN,
    min_net_premium_cny: float = OPENING_CANDIDATE_MIN_NET_PREMIUM_CNY,
    min_iv_rv_ratio: float = OPENING_CANDIDATE_MIN_IV_RV_RATIO,
    min_iv_minus_rv: float = OPENING_CANDIDATE_MIN_IV_MINUS_RV,
    max_spread_ratio: float = OPENING_CANDIDATE_MAX_SPREAD_RATIO,
    require_earnings_evidence: bool = True,
    reject_known_earnings: bool = True,
) -> dict[str, Any]:
    """Evaluate the common formal opening gates owned by Candidate Engine."""

    mode_norm = normalize_strategy_mode(mode)
    src = dict(raw) if isinstance(raw, dict) else {}
    rejects: list[dict[str, Any]] = []

    annualized = _first_float(
        src,
        "annualized_net_return_on_cash_basis"
        if mode_norm == "put"
        else "annualized_net_premium_return",
    )
    if annualized is None or annualized < float(min_annualized_return):
        _reject(
            rejects,
            stage=STAGE_RETURN_FLOOR,
            reason=REJECT_RETURN_ANNUALIZED,
            message="annualized net return below formal minimum or unavailable",
            metric_value=annualized,
            threshold=float(min_annualized_return),
        )

    net_premium_cny = _first_float(src, "net_premium_cny", "net_income_cny")
    if net_premium_cny is None or net_premium_cny < float(min_net_premium_cny):
        _reject(
            rejects,
            stage=STAGE_RETURN_FLOOR,
            reason=REJECT_RETURN_NET_PREMIUM_CNY,
            message="one-contract net premium in CNY below minimum or unavailable",
            metric_value=net_premium_cny,
            threshold=float(min_net_premium_cny),
        )

    spread_ratio = _first_float(src, "spread_ratio")
    if spread_ratio is None or spread_ratio > float(max_spread_ratio):
        _reject(
            rejects,
            stage=STAGE_RISK_FILTER,
            reason=REJECT_RISK_SPREAD,
            message="raw quote spread ratio above maximum or unavailable",
            metric_value=spread_ratio,
            threshold=float(max_spread_ratio),
        )

    iv_rv_ratio = _first_float(src, "iv_rv_ratio")
    if iv_rv_ratio is None or iv_rv_ratio < float(min_iv_rv_ratio):
        _reject(
            rejects,
            stage=STAGE_RISK_FILTER,
            reason=REJECT_RISK_IV_RV_RATIO,
            message="IV to term-matched RV ratio below minimum or unavailable",
            metric_value=iv_rv_ratio,
            threshold=float(min_iv_rv_ratio),
        )

    iv_minus_rv = _first_float(src, "iv_minus_rv")
    if iv_minus_rv is None or iv_minus_rv < float(min_iv_minus_rv):
        _reject(
            rejects,
            stage=STAGE_RISK_FILTER,
            reason=REJECT_RISK_IV_MINUS_RV,
            message="IV minus term-matched RV below minimum or unavailable",
            metric_value=iv_minus_rv,
            threshold=float(min_iv_minus_rv),
        )

    if require_earnings_evidence:
        earnings_status = str(src.get("earnings_evidence_status") or "").strip().lower()
        if earnings_status != "ready":
            _reject(
                rejects,
                stage=STAGE_RISK_FILTER,
                reason=REJECT_RISK_EARNINGS_UNAVAILABLE,
                message="OpenD earnings coverage is unavailable for the holding period",
                metric_value={
                    "status": earnings_status or None,
                    "reason": src.get("earnings_reason_code"),
                },
                threshold="ready",
            )
        elif reject_known_earnings and bool(src.get("earnings_has_event")):
            _reject(
                rejects,
                stage=STAGE_RISK_FILTER,
                reason=REJECT_RISK_EARNINGS_EVENT,
                message="known earnings event falls within the holding period",
                metric_value=src.get("earnings_event_dates"),
                threshold="no event through expiration",
            )

    return build_candidate_decision(
        mode=mode_norm,
        symbol=str(src.get("symbol") or ""),
        contract_symbol=str(src.get("contract_symbol") or src.get("option_symbol") or ""),
        accepted=not rejects,
        rejects=rejects,
        normalized_input=src,
    )


def _non_finite_numeric_fields(raw: dict[str, Any]) -> list[str]:
    fields: list[str] = []
    for field in NUMERIC_INPUT_FIELDS:
        if field not in raw or _is_missing(raw.get(field)) or isinstance(raw.get(field), bool):
            continue
        try:
            parsed = float(raw.get(field))
        except Exception:
            continue
        if not math.isfinite(parsed):
            fields.append(field)
    return fields


def _bounded(value: float, *, low: float = 0.0, high: float = 1.0) -> float:
    return max(float(low), min(float(high), float(value)))


def _score_average(parts: list[float]) -> float:
    values = [float(p) for p in parts if p is not None]
    if not values:
        return 0.0
    return sum(values) / len(values)


def _liquidity_quality(
    *,
    spread_ratio: float | None = None,
    open_interest: float | None = None,
    volume: float | None = None,
) -> float:
    parts: list[float] = []
    if spread_ratio is not None:
        parts.append(_bounded(1.0 - max(float(spread_ratio), 0.0)))
    if open_interest is not None:
        parts.append(_bounded(float(open_interest) / 100.0))
    if volume is not None:
        parts.append(_bounded(float(volume) / 10.0))
    return _score_average(parts)


def _risk_distance_quality(
    *,
    mode: StrategyMode,
    delta: float | None = None,
    otm_pct: float | None = None,
    dte: float | None = None,
) -> float:
    del mode
    parts: list[float] = []
    if delta is not None:
        parts.append(_bounded(1.0 - abs(float(delta))))
    if otm_pct is not None:
        parts.append(_bounded(float(otm_pct) / 0.20))
    if dte is not None:
        parts.append(_bounded(float(dte) / 45.0))
    return _score_average(parts)


def _path_risk_quality(src: dict[str, Any], *, mode: StrategyMode) -> float | None:
    explicit = _first_float(src, "path_risk_score")
    if explicit is not None:
        return _bounded(explicit)

    nav_pct_cap = 0.03
    if mode == "call":
        opportunity_cost = _first_float(src, "call_gap_up_opportunity_cost_nav_pct")
        if opportunity_cost is None:
            return None
        return _bounded(1.0 - (max(float(opportunity_cost), 0.0) / nav_pct_cap))

    losses = [
        value
        for value in (
            _first_float(src, "put_stress_down_loss_nav_pct"),
            _first_float(src, "put_gap_down_loss_nav_pct"),
        )
        if value is not None
    ]
    if not losses:
        return None
    return _bounded(1.0 - (max(float(value) for value in losses) / nav_pct_cap))


def compute_candidate_strategy_score(
    *,
    mode: StrategyMode | str,
    annualized_return: float | None = None,
    net_income: float | None = None,
    spread_ratio: float | None = None,
    open_interest: float | None = None,
    volume: float | None = None,
    delta: float | None = None,
    otm_pct: float | None = None,
    dte: float | None = None,
    vol_edge_score: float | None = None,
    delta_target_score: float | None = None,
    concentration_score: float | None = None,
    path_risk_score: float | None = None,
    weights: CandidateScoreWeights | None = None,
) -> CandidateStrategyScore:
    mode_norm = normalize_strategy_mode(mode)
    score_weights = weights or CandidateScoreWeights()
    components = {
        "annualized_return": (_coerce_float(annualized_return) or 0.0) * float(score_weights.annualized_return),
        "net_income": (_coerce_float(net_income) or 0.0) * float(score_weights.net_income),
        "liquidity": _liquidity_quality(
            spread_ratio=_coerce_float(spread_ratio),
            open_interest=_coerce_float(open_interest),
            volume=_coerce_float(volume),
        )
        * float(score_weights.liquidity),
        "risk_distance": _risk_distance_quality(
            mode=mode_norm,
            delta=_coerce_float(delta),
            otm_pct=_coerce_float(otm_pct),
            dte=_coerce_float(dte),
        )
        * float(score_weights.risk_distance),
        "vol_edge": (_coerce_float(vol_edge_score) or 0.0) * float(score_weights.vol_edge),
        "delta_target": (_coerce_float(delta_target_score) or 0.0) * float(score_weights.delta_target),
        "concentration": (_coerce_float(concentration_score) or 0.0) * float(score_weights.concentration),
        "path_risk": (_coerce_float(path_risk_score) or 0.0) * float(score_weights.path_risk),
    }
    warnings: list[str] = []
    spread_value = _coerce_float(spread_ratio)
    if spread_value is not None and spread_value >= 0.30:
        warnings.append("wide_spread")
    total = sum(components.values())
    return CandidateStrategyScore(total=float(total), components=components, warnings=tuple(warnings))


def _first_float(src: dict[str, Any], *names: str) -> float | None:
    for name in names:
        value = _coerce_float(src.get(name))
        if value is not None:
            return value
    return None


def _candidate_score_inputs(src: dict[str, Any], *, mode: StrategyMode) -> dict[str, float | None]:
    if mode == "put":
        annualized_return = _first_float(src, "annualized_net_return_on_cash_basis")
        period_return = _sell_put_period_return(src)
        otm_pct = _first_float(src, "otm_pct")
    else:
        annualized_return = _first_float(src, "annualized_net_premium_return")
        period_return = _covered_call_period_return(src)
        otm_pct = _first_float(src, "otm_pct", "strike_above_spot_pct")
    return {
        "annualized_return": annualized_return,
        "period_return": period_return,
        "net_income": _first_float(src, "net_income"),
        "spread_ratio": _first_float(src, "spread_ratio"),
        "open_interest": _first_float(src, "open_interest"),
        "volume": _first_float(src, "volume"),
        "delta": _first_float(src, "delta"),
        "otm_pct": otm_pct,
        "dte": _first_float(src, "dte"),
        "vol_edge_score": _first_float(src, "vol_edge_score"),
        "iv_rv_ratio": _first_float(src, "iv_rv_ratio"),
        "iv_minus_rv": _first_float(src, "iv_minus_rv"),
        "delta_target_score": _first_float(src, "delta_target_score"),
        "concentration_score": _first_float(src, "concentration_score"),
        "path_risk_score": _path_risk_quality(src, mode=mode),
    }


def _candidate_tie_break_margin(src: dict[str, Any], *, mode: StrategyMode) -> float:
    if mode == "put":
        explicit = _first_float(src, "net_assignment_discount_pct")
        if explicit is not None:
            return float(explicit)
        spot = _first_float(src, "spot")
        breakeven = _first_float(src, "breakeven")
        if spot is not None and spot > 0 and breakeven is not None:
            return float((spot - breakeven) / spot)
        return 0.0
    return float(
        _first_float(
            src,
            "strike_upside_margin_pct",
            "strike_above_spot_pct",
            "otm_pct",
        )
        or 0.0
    )


def _candidate_concentration_tie_break(src: dict[str, Any]) -> float:
    score = _first_float(src, "concentration_score")
    if score is not None:
        return float(score)
    exposures = [
        value
        for value in (
            _first_float(src, "single_trade_concentration"),
            _first_float(src, "symbol_concentration_after"),
            _first_float(src, "total_short_put_concentration_after"),
        )
        if value is not None
    ]
    return -max(exposures) if exposures else 0.0


def _sell_put_period_return(src: dict[str, Any]) -> float | None:
    explicit = _first_float(
        src,
        "period_net_return_on_cash_basis",
        "period_net_return",
    )
    if explicit is not None:
        return explicit
    net_income = _first_float(src, "net_income")
    cash_basis = _first_float(src, "cash_basis")
    if net_income is not None and cash_basis is not None and cash_basis > 0:
        return net_income / cash_basis
    annualized = _first_float(src, "annualized_net_return_on_cash_basis")
    dte = _first_float(src, "dte")
    if annualized is not None and dte is not None and dte > 0:
        return annualized * dte / 365.0
    return None


def _covered_call_period_return(src: dict[str, Any]) -> float | None:
    explicit = _first_float(
        src,
        "period_net_premium_return",
        "period_net_return",
    )
    if explicit is not None:
        return explicit
    net_premium = _first_float(src, "net_premium", "net_income")
    current_market_value = _first_float(src, "current_market_value")
    if net_premium is not None and current_market_value is not None and current_market_value > 0:
        return net_premium / current_market_value
    annualized = _first_float(src, "annualized_net_premium_return")
    dte = _first_float(src, "dte")
    if annualized is not None and dte is not None and dte > 0:
        return annualized * dte / 365.0
    # Compatibility for historical diagnostic rows. Formal Phase-2 candidate
    # rows always carry the period return directly.
    if annualized is not None:
        return annualized
    return None


def _candidate_period_return(src: dict[str, Any], *, mode: StrategyMode) -> float | None:
    return _sell_put_period_return(src) if mode == "put" else _covered_call_period_return(src)


def _known_low_sort(value: float | None) -> tuple[bool, float]:
    return (value is None, float("inf") if value is None else float(value))


def _known_high_sort(value: float | None) -> tuple[bool, float]:
    return (value is None, 0.0 if value is None else -float(value))


def _sell_put_within_symbol_tie_key(src: dict[str, Any]) -> tuple[Any, ...]:
    return (
        -_candidate_tie_break_margin(src, mode="put"),
        *_known_low_sort(_first_float(src, "spread_ratio")),
        *_known_high_sort(_first_float(src, "open_interest")),
        -float(_first_float(src, "net_income_cny", "net_income") or 0.0),
        str(src.get("contract_symbol") or src.get("option_symbol") or ""),
    )


def _sell_put_concentration_sort(src: dict[str, Any]) -> tuple[bool, float]:
    explicit = _first_float(src, "symbol_concentration_after")
    if explicit is not None:
        return False, explicit
    quality = _first_float(src, "concentration_score")
    if quality is not None:
        return False, -quality
    return True, float("inf")


def _sell_put_cross_symbol_tie_key(src: dict[str, Any]) -> tuple[Any, ...]:
    return (
        *_sell_put_concentration_sort(src),
        -_candidate_tie_break_margin(src, mode="put"),
        *_known_low_sort(_first_float(src, "spread_ratio")),
        *_known_high_sort(_first_float(src, "open_interest")),
        -float(_first_float(src, "net_income_cny", "net_income") or 0.0),
        str(src.get("symbol") or "").strip().upper(),
        str(src.get("contract_symbol") or src.get("option_symbol") or ""),
    )


def _rank_return_bands(
    rows: list[dict[str, Any]],
    *,
    period_return_fn: Any,
    tie_key: Any,
) -> list[dict[str, Any]]:
    remaining = list(enumerate(rows))
    ranked: list[dict[str, Any]] = []
    while remaining:
        usable = [
            value
            for _index, row in remaining
            if (value := period_return_fn(row)) is not None
        ]
        if not usable:
            ranked.extend(
                row
                for _index, row in sorted(
                    remaining,
                    key=lambda item: (tie_key(item[1]), item[0]),
                )
            )
            break
        band_max = max(usable)
        floor = band_max - OPENING_CANDIDATE_NEAR_RETURN_THRESHOLD
        band = [
            (index, row)
            for index, row in remaining
            if (value := period_return_fn(row)) is not None
            and value >= floor
        ]
        ranked.extend(
            row
            for _index, row in sorted(
                band,
                key=lambda item: (tie_key(item[1]), item[0]),
            )
        )
        band_indices = {index for index, _row in band}
        remaining = [item for item in remaining if item[0] not in band_indices]
    return ranked


def _covered_call_within_symbol_tie_key(src: dict[str, Any]) -> tuple[Any, ...]:
    return (
        -float(_first_float(src, "strike") or 0.0),
        *_known_low_sort(_first_float(src, "spread_ratio")),
        *_known_high_sort(_first_float(src, "open_interest")),
        -float(_first_float(src, "net_premium_cny", "net_premium", "net_income") or 0.0),
        str(src.get("contract_symbol") or src.get("option_symbol") or ""),
    )


def _covered_call_remaining_concentration_sort(src: dict[str, Any]) -> tuple[bool, float]:
    explicit = _first_float(
        src,
        "remaining_symbol_concentration_after",
        "symbol_concentration_after_call",
        "symbol_concentration_after",
    )
    if explicit is not None:
        return False, explicit
    quality = _first_float(src, "concentration_score")
    if quality is not None:
        return False, -quality
    return True, float("inf")


def _covered_call_cross_symbol_tie_key(src: dict[str, Any]) -> tuple[Any, ...]:
    return (
        *_covered_call_remaining_concentration_sort(src),
        -_candidate_tie_break_margin(src, mode="call"),
        *_known_low_sort(_first_float(src, "spread_ratio")),
        *_known_high_sort(_first_float(src, "open_interest")),
        -float(_first_float(src, "net_premium_cny", "net_premium", "net_income") or 0.0),
        str(src.get("symbol") or "").strip().upper(),
        str(src.get("contract_symbol") or src.get("option_symbol") or ""),
    )


def _candidate_recommendation_sort_tuple(
    src: dict[str, Any],
    *,
    mode: StrategyMode,
    annualized_return: float | None,
    net_income: float | None,
) -> tuple[Any, ...]:
    period_return = _candidate_period_return(src, mode=mode)
    primary_return = period_return
    annual_missing = primary_return is None
    concentration = _candidate_concentration_tie_break(src)
    spread = _first_float(src, "spread_ratio")
    open_interest = _first_float(src, "open_interest")
    return (
        annual_missing,
        -float(primary_return or 0.0),
        -_candidate_tie_break_margin(src, mode=mode),
        -float(concentration),
        float("inf") if spread is None else float(spread),
        -float(open_interest or 0.0),
        -float(net_income or 0.0),
        str(src.get("symbol") or "").strip().upper(),
        str(src.get("contract_symbol") or src.get("option_symbol") or ""),
    )


_SCORE_COMPONENT_LABELS: dict[str, str] = {
    "annualized_return": "年化收益",
    "period_net_return_on_cash_basis": "持有期净收益",
    "period_net_premium_return": "持有期净权利金收益",
    "net_income": "净收入",
    "liquidity": "流动性",
    "risk_distance": "风险距离",
    "vol_edge": "波动率优势",
    "delta_target": "Delta目标",
    "concentration": "集中度",
    "path_risk": "路径风险",
}

_SCORE_WARNING_LABELS: dict[str, str] = {
    "wide_spread": "价差偏宽",
}


def explain_candidate_rank(
    row: dict[str, Any] | Any,
    *,
    mode: StrategyMode | str,
    score_weights: CandidateScoreWeights | None = None,
) -> dict[str, Any]:
    mode_norm = normalize_strategy_mode(mode)
    src = row if isinstance(row, dict) else {}
    rank_key = build_candidate_rank_key(src, mode=mode_norm, score_weights=score_weights)
    components = {
        str(name): float(value)
        for name, value in (rank_key.get("score_components") or {}).items()
        if _coerce_float(value) is not None
    }
    warnings = [str(item) for item in (rank_key.get("score_warnings") or []) if str(item).strip()]
    primary_drivers = [
        "period_net_return_on_cash_basis"
        if mode_norm == "put"
        else "period_net_premium_return"
    ]
    score_inputs = _candidate_score_inputs(src, mode=mode_norm)
    return {
        "mode": mode_norm,
        "ranking_policy": "candidate_engine",
        "symbol": str(src.get("symbol") or "").strip().upper() or None,
        "contract_symbol": str(src.get("contract_symbol") or src.get("option_symbol") or "").strip() or None,
        "option_type": str(src.get("option_type") or ("put" if mode_norm == "put" else "call")).strip().lower() or None,
        "expiration": str(src.get("expiration") or "").strip() or None,
        "strike": _first_float(src, "strike"),
        "strategy_score": float(rank_key.get("strategy_score") or 0.0),
        "strategy_score_role": "diagnostic_only",
        "annualized_return": rank_key.get("annualized_return"),
        "period_net_return": rank_key.get("period_net_return"),
        "net_income": rank_key.get("net_income"),
        "score_components": components,
        "score_component_labels": {name: _SCORE_COMPONENT_LABELS.get(name, name) for name in components},
        "score_inputs": score_inputs,
        "score_warnings": warnings,
        "risk_notes": [_SCORE_WARNING_LABELS.get(item, item) for item in warnings],
        "primary_drivers": primary_drivers,
        "primary_driver_labels": [_SCORE_COMPONENT_LABELS.get(item, item) for item in primary_drivers],
        "rank_reason": (
            (
                "硬门槛通过后按持有期净收益分带；同标的带内依次比较净接货折价、"
                "价差、OI 和净收入；跨标的代表合约带内先比较接货后集中度，再比较"
                "净接货折价、价差、OI 和净收入"
                if mode_norm == "put"
                else (
                    "硬门槛通过后按持有期净权利金收益分带；同标的带内依次比较更高 strike、"
                    "价差、OI 和净权利金；跨标的代表合约带内先比较被叫走后剩余集中度，"
                    "再比较 strike 上行距离、价差、OI 和净权利金"
                )
            )
        ),
    }


def _reject(
    sink: list[dict[str, Any]],
    *,
    stage: str,
    reason: str,
    message: str,
    metric_value: Any = None,
    threshold: Any = None,
) -> None:
    sink.append(
        build_candidate_reject(
            stage=stage,
            reason=reason,
            message=message,
            metric_value=metric_value,
            threshold=threshold,
        )
    )


def _normalize_candidate_input_row(
    raw: dict[str, Any],
    *,
    mode: StrategyMode,
) -> dict[str, Any]:
    out = dict(raw)
    out["mode"] = mode
    out["symbol"] = str(raw.get("symbol") or "").strip().upper()
    out["option_type"] = str(raw.get("option_type") or "").strip().lower()
    out["contract_symbol"] = str(raw.get("contract_symbol") or "").strip()
    out["expiration"] = str(raw.get("expiration") or "").strip()
    out["currency"] = str(raw.get("currency") or "").strip().upper()

    for field in NUMERIC_INPUT_FIELDS:
        if field not in raw:
            continue
        v = _coerce_float(raw.get(field))
        if field == "dte" and v is not None:
            try:
                out[field] = int(v)
            except Exception:
                out[field] = None
        else:
            out[field] = v
    return out


def normalize_candidate_reject(raw: CandidateReject | dict[str, Any] | Any) -> dict[str, Any]:
    if isinstance(raw, CandidateReject):
        src = {
            "stage": raw.stage,
            "reason": raw.reason,
            "message": raw.message,
            "metric_value": raw.metric_value,
            "threshold": raw.threshold,
        }
    elif isinstance(raw, dict):
        src = raw
    else:
        src = {}

    stage = str(src.get("stage") or "").strip()
    if stage not in CANDIDATE_STAGE_ORDER:
        raise ValueError(f"unsupported candidate reject stage: {stage}")

    reason = str(src.get("reason") or "").strip()
    if reason not in CANDIDATE_REJECT_REASONS:
        raise ValueError(f"unsupported candidate reject reason: {reason}")

    out = {
        "stage": stage,
        "reason": reason,
        "message": str(src.get("message") or ""),
    }
    if "metric_value" in src:
        out["metric_value"] = src.get("metric_value")
    if "threshold" in src:
        out["threshold"] = src.get("threshold")
    return out


def build_candidate_reject(
    *,
    stage: str,
    reason: str,
    message: str = "",
    metric_value: Any = None,
    threshold: Any = None,
) -> dict[str, Any]:
    return normalize_candidate_reject(
        CandidateReject(
            stage=stage,
            reason=reason,
            message=message,
            metric_value=metric_value,
            threshold=threshold,
        )
    )


def map_legacy_reject_rule(rule: str) -> dict[str, str]:
    rule_norm = str(rule or "").strip()
    reason = LEGACY_REJECT_RULE_REASON_MAP.get(rule_norm)
    stage = LEGACY_REJECT_RULE_STAGE_MAP.get(rule_norm)
    if not reason or not stage:
        raise ValueError(f"unsupported legacy reject rule: {rule}")
    return {"rule": rule_norm, "stage": stage, "reason": reason}


def normalize_legacy_reject_log_row(row: dict[str, Any] | Any) -> dict[str, Any]:
    """Convert existing scanner reject-log rows to Engine reject reason rows."""
    src = row if isinstance(row, dict) else {}
    mapped = map_legacy_reject_rule(str(src.get("reject_rule") or ""))
    reject = build_candidate_reject(
        stage=mapped["stage"],
        reason=mapped["reason"],
        message=str(src.get("reject_rule") or ""),
        metric_value=src.get("metric_value"),
        threshold=src.get("threshold"),
    )
    out = {
        **reject,
        "legacy_reject_stage": str(src.get("reject_stage") or ""),
        "legacy_reject_rule": mapped["rule"],
        "symbol": str(src.get("symbol") or "").strip().upper(),
        "contract_symbol": str(src.get("contract_symbol") or "").strip(),
        "expiration": str(src.get("expiration") or "").strip(),
        "strike": src.get("strike"),
        "mode": str(src.get("mode") or "").strip().lower(),
    }
    return out


def normalize_legacy_reject_log_rows(rows: list[dict[str, Any]] | Any) -> list[dict[str, Any]]:
    if rows is None:
        return []
    if not isinstance(rows, list):
        raise ValueError("legacy reject log rows must be a list")
    return [normalize_legacy_reject_log_row(row) for row in rows]


def validate_candidate_decision_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("candidate decision payload must be a dict")
    if str(payload.get("schema_kind") or "") != SCHEMA_KIND_CANDIDATE_DECISION:
        raise ValueError(f"schema_kind must be {SCHEMA_KIND_CANDIDATE_DECISION}")
    if str(payload.get("schema_version") or "") != CANDIDATE_ENGINE_SCHEMA_VERSION:
        raise ValueError(f"unsupported candidate decision schema_version: {payload.get('schema_version')}")
    normalize_strategy_mode(payload.get("mode"))
    if not isinstance(payload.get("accepted"), bool):
        raise ValueError("candidate decision accepted must be bool")
    rejects = payload.get("rejects")
    if not isinstance(rejects, list):
        raise ValueError("candidate decision rejects must be a list")
    payload["rejects"] = [normalize_candidate_reject(item) for item in rejects]
    return payload


def build_candidate_decision(
    *,
    mode: StrategyMode | str,
    symbol: str,
    contract_symbol: str | None = None,
    accepted: bool,
    rejects: list[dict[str, Any] | CandidateReject] | None = None,
    score: float | None = None,
    rank_key: dict[str, Any] | None = None,
    normalized_input: dict[str, Any] | None = None,
) -> dict[str, Any]:
    reject_payloads = [normalize_candidate_reject(item) for item in (rejects or [])]
    out: dict[str, Any] = {
        "schema_kind": SCHEMA_KIND_CANDIDATE_DECISION,
        "schema_version": CANDIDATE_ENGINE_SCHEMA_VERSION,
        "mode": normalize_strategy_mode(mode),
        "symbol": str(symbol or "").strip().upper(),
        "contract_symbol": str(contract_symbol or "").strip(),
        "accepted": bool(accepted),
        "rejects": reject_payloads,
    }
    if score is not None:
        out["score"] = float(score)
    if isinstance(rank_key, dict):
        out["rank_key"] = dict(rank_key)
    if isinstance(normalized_input, dict):
        out["normalized_input"] = dict(normalized_input)
    return validate_candidate_decision_payload(out)


def evaluate_candidate_input(
    raw: dict[str, Any] | Any,
    *,
    mode: StrategyMode | str,
    extra_required_fields: tuple[str, ...] | list[str] | None = None,
) -> dict[str, Any]:
    """Stage 0 candidate input gate.

    This function intentionally avoids DTE, strike, return, liquidity, and event
    policy decisions. It only normalizes the input row and rejects rows that lack
    the critical fields needed by later stages.
    """
    mode_norm = normalize_strategy_mode(mode)
    src = raw if isinstance(raw, dict) else {}
    normalized = _normalize_candidate_input_row(src, mode=mode_norm)
    invalid_numeric_fields = _non_finite_numeric_fields(src)

    required = list(COMMON_CRITICAL_FIELDS)
    if extra_required_fields:
        for field in extra_required_fields:
            f = str(field or "").strip()
            if f and f not in required:
                required.append(f)

    missing = [
        field
        for field in required
        if field not in invalid_numeric_fields and _is_missing(normalized.get(field))
    ]
    rejects: list[dict[str, Any]] = []
    if invalid_numeric_fields:
        rejects.append(
            build_candidate_reject(
                stage=STAGE_INPUT_NORMALIZATION,
                reason=REJECT_INPUT_INVALID,
                message=f"non-finite numeric fields: {', '.join(invalid_numeric_fields)}",
                threshold=invalid_numeric_fields,
            )
        )

    bid = _coerce_float(src.get("bid"))
    ask = _coerce_float(src.get("ask"))
    if bid is not None and ask is not None and ask < bid:
        rejects.append(
            build_candidate_reject(
                stage=STAGE_INPUT_NORMALIZATION,
                reason=REJECT_INPUT_INVALID,
                message="ask below bid",
                metric_value={"bid": bid, "ask": ask},
                threshold="ask >= bid",
            )
        )
    if missing:
        rejects.append(
            build_candidate_reject(
                stage=STAGE_INPUT_NORMALIZATION,
                reason=REJECT_INPUT_MISSING,
                message=f"missing critical fields: {', '.join(missing)}",
                threshold=missing,
            )
        )

    option_type = str(normalized.get("option_type") or "").strip().lower()
    if option_type and option_type != mode_norm:
        rejects.append(
            build_candidate_reject(
                stage=STAGE_INPUT_NORMALIZATION,
                reason=REJECT_INPUT_MISSING,
                message=f"option_type mismatch: expected {mode_norm}, got {option_type}",
                metric_value=option_type,
                threshold=mode_norm,
            )
        )

    return build_candidate_decision(
        mode=mode_norm,
        symbol=str(normalized.get("symbol") or ""),
        contract_symbol=str(normalized.get("contract_symbol") or ""),
        accepted=(len(rejects) == 0),
        rejects=rejects,
        normalized_input=normalized,
    )


def evaluate_candidate_hard_constraints(
    raw: dict[str, Any] | Any,
    *,
    mode: StrategyMode | str,
    min_dte: int | float | None = None,
    max_dte: int | float | None = None,
    min_strike: int | float | None = None,
    max_strike: int | float | None = None,
    put_cash_required: int | float | None = None,
    put_cash_free: int | float | None = None,
    put_cash_capacity_available: bool | None = None,
    put_cash_capacity_reason: str | None = None,
    call_covered_contracts_available: int | float | None = None,
    extra_required_fields: tuple[str, ...] | list[str] | None = None,
) -> dict[str, Any]:
    """Stage 1 hard constraints gate.

    This preserves Stage 0 as the first gate. If input normalization rejects the
    row, Stage 1 does not add derived-policy rejects that would be based on
    incomplete inputs.
    """
    mode_norm = normalize_strategy_mode(mode)
    stage0 = evaluate_candidate_input(
        raw,
        mode=mode_norm,
        extra_required_fields=extra_required_fields,
    )
    normalized = dict(stage0.get("normalized_input") or {})
    rejects = list(stage0.get("rejects") or [])
    if not bool(stage0.get("accepted")):
        return build_candidate_decision(
            mode=mode_norm,
            symbol=str(normalized.get("symbol") or ""),
            contract_symbol=str(normalized.get("contract_symbol") or ""),
            accepted=False,
            rejects=rejects,
            normalized_input=normalized,
        )

    dte = _coerce_float(normalized.get("dte"))
    strike = _coerce_float(normalized.get("strike"))
    spot = _coerce_float(normalized.get("spot"))

    min_dte_v = _coerce_float(min_dte)
    if min_dte_v is not None and dte is not None and dte < min_dte_v:
        _reject(
            rejects,
            stage=STAGE_HARD_CONSTRAINTS,
            reason=REJECT_HARD_DTE,
            message="dte below minimum",
            metric_value=dte,
            threshold=min_dte_v,
        )

    max_dte_v = _coerce_float(max_dte)
    if max_dte_v is not None and dte is not None and dte > max_dte_v:
        _reject(
            rejects,
            stage=STAGE_HARD_CONSTRAINTS,
            reason=REJECT_HARD_DTE,
            message="dte above maximum",
            metric_value=dte,
            threshold=max_dte_v,
        )

    configured_min_strike = _coerce_float(min_strike)
    configured_max_strike = _coerce_float(max_strike)
    effective_min_strike = configured_min_strike
    effective_max_strike = configured_max_strike
    if mode_norm == "put" and spot is not None:
        effective_max_strike = min(
            value for value in (configured_max_strike, spot) if value is not None
        )
        effective_min_strike = max(
            value
            for value in (configured_min_strike, effective_max_strike * 0.80)
            if value is not None
        )
    if mode_norm == "call" and spot is not None:
        effective_min_strike = max(
            value for value in (configured_min_strike, spot) if value is not None
        )
        effective_max_strike = min(
            value
            for value in (configured_max_strike, effective_min_strike * 1.20)
            if value is not None
        )

    if effective_min_strike is not None and strike is not None and strike < effective_min_strike:
        _reject(
            rejects,
            stage=STAGE_HARD_CONSTRAINTS,
            reason=REJECT_HARD_STRIKE,
            message="strike below minimum",
            metric_value=strike,
            threshold=(
                {
                    "min_strike": configured_min_strike,
                    "spot": spot,
                    "effective_min_strike": effective_min_strike,
                }
                if mode_norm == "call"
                else {
                    "min_strike": configured_min_strike,
                    "effective_max_strike": effective_max_strike,
                    "effective_min_strike": effective_min_strike,
                }
            ),
        )

    if effective_max_strike is not None and strike is not None and strike > effective_max_strike:
        _reject(
            rejects,
            stage=STAGE_HARD_CONSTRAINTS,
            reason=REJECT_HARD_STRIKE,
            message="strike above maximum",
            metric_value=strike,
            threshold=(
                {
                    "max_strike": configured_max_strike,
                    "spot": spot,
                    "effective_max_strike": effective_max_strike,
                }
                if mode_norm == "put"
                else {
                    "max_strike": configured_max_strike,
                    "effective_min_strike": effective_min_strike,
                    "effective_max_strike": effective_max_strike,
                }
            ),
        )

    put_required_v = _coerce_float(put_cash_required)
    put_free_v = _coerce_float(put_cash_free)
    put_capacity_reason = str(put_cash_capacity_reason or "").strip()
    if mode_norm == "put" and put_cash_capacity_available is False:
        _reject(
            rejects,
            stage=STAGE_HARD_CONSTRAINTS,
            reason=REJECT_HARD_CAPACITY_PUT,
            message=(
                f"put cash capacity evidence is unavailable: {put_capacity_reason}"
                if put_capacity_reason
                else "put cash capacity evidence is unavailable"
            ),
            metric_value=put_required_v,
            threshold=put_free_v,
        )
    elif mode_norm == "put" and put_required_v is not None and put_free_v is not None and put_required_v > put_free_v:
        _reject(
            rejects,
            stage=STAGE_HARD_CONSTRAINTS,
            reason=REJECT_HARD_CAPACITY_PUT,
            message=(
                f"put cash requirement exceeds free cash: {put_capacity_reason}"
                if put_capacity_reason
                else "put cash requirement exceeds free cash"
            ),
            metric_value=put_required_v,
            threshold=put_free_v,
        )

    call_cover_v = _coerce_float(call_covered_contracts_available)
    if mode_norm == "call" and call_cover_v is not None and call_cover_v < 1:
        _reject(
            rejects,
            stage=STAGE_HARD_CONSTRAINTS,
            reason=REJECT_HARD_CAPACITY_CALL,
            message="covered contracts available below one",
            metric_value=call_cover_v,
            threshold=1,
        )

    return build_candidate_decision(
        mode=mode_norm,
        symbol=str(normalized.get("symbol") or ""),
        contract_symbol=str(normalized.get("contract_symbol") or ""),
        accepted=(len(rejects) == 0),
        rejects=rejects,
        normalized_input=normalized,
    )


def evaluate_candidate_non_resource_hard_constraints(
    raw: dict[str, Any] | Any,
    *,
    mode: StrategyMode | str,
    min_dte: int | float | None = None,
    max_dte: int | float | None = None,
    min_strike: int | float | None = None,
    max_strike: int | float | None = None,
    extra_required_fields: tuple[str, ...] | list[str] | None = None,
) -> dict[str, Any]:
    """Run the exact Stage 0/1 opening gates without cash/share availability."""

    return evaluate_candidate_hard_constraints(
        raw,
        mode=mode,
        min_dte=min_dte,
        max_dte=max_dte,
        min_strike=min_strike,
        max_strike=max_strike,
        put_cash_required=None,
        put_cash_free=None,
        put_cash_capacity_available=None,
        put_cash_capacity_reason=None,
        call_covered_contracts_available=None,
        extra_required_fields=extra_required_fields,
    )


def evaluate_candidate_return_floor(
    candidate_decision: dict[str, Any] | Any,
    *,
    min_annualized_return: int | float | None = None,
    min_net_income: int | float | None = None,
    annualized_return: int | float | None = None,
    net_income: int | float | None = None,
) -> dict[str, Any]:
    """Stage 2 return floor gate.

    Accepts a previous candidate decision DTO and appends only return-floor
    rejects when prior stages have accepted the row.
    """
    prev = validate_candidate_decision_payload(dict(candidate_decision or {}))
    mode_norm = normalize_strategy_mode(prev.get("mode"))
    normalized = dict(prev.get("normalized_input") or {})
    rejects = list(prev.get("rejects") or [])
    if not bool(prev.get("accepted")):
        return build_candidate_decision(
            mode=mode_norm,
            symbol=str(prev.get("symbol") or ""),
            contract_symbol=str(prev.get("contract_symbol") or ""),
            accepted=False,
            rejects=rejects,
            normalized_input=normalized,
        )

    annual_v = _coerce_float(annualized_return if annualized_return is not None else normalized.get("annualized_return"))
    min_annual_v = _coerce_float(min_annualized_return)
    if min_annual_v is not None and (annual_v is None or annual_v < min_annual_v):
        _reject(
            rejects,
            stage=STAGE_RETURN_FLOOR,
            reason=REJECT_RETURN_ANNUALIZED,
            message="annualized return below minimum",
            metric_value=annual_v,
            threshold=min_annual_v,
        )

    net_v = _coerce_float(net_income if net_income is not None else normalized.get("net_income"))
    min_net_v = _coerce_float(min_net_income)
    if min_net_v is not None and (net_v is None or net_v < min_net_v):
        _reject(
            rejects,
            stage=STAGE_RETURN_FLOOR,
            reason=REJECT_RETURN_NET_INCOME,
            message="net income below minimum",
            metric_value=net_v,
            threshold=min_net_v,
        )

    return build_candidate_decision(
        mode=mode_norm,
        symbol=str(prev.get("symbol") or ""),
        contract_symbol=str(prev.get("contract_symbol") or ""),
        accepted=(len(rejects) == 0),
        rejects=rejects,
        normalized_input=normalized,
    )


def evaluate_candidate_risk_filter(
    candidate_decision: dict[str, Any] | Any,
    *,
    min_open_interest: int | float | None = None,
    min_volume: int | float | None = None,
    max_spread_ratio: int | float | None = None,
    event_flag: bool = False,
    event_mode: str = "warn",
    open_interest: int | float | None = None,
    volume: int | float | None = None,
    spread_ratio: int | float | None = None,
) -> dict[str, Any]:
    """Stage 3 risk and execution quality gate."""
    prev = validate_candidate_decision_payload(dict(candidate_decision or {}))
    mode_norm = normalize_strategy_mode(prev.get("mode"))
    normalized = dict(prev.get("normalized_input") or {})
    rejects = list(prev.get("rejects") or [])
    if not bool(prev.get("accepted")):
        return build_candidate_decision(
            mode=mode_norm,
            symbol=str(prev.get("symbol") or ""),
            contract_symbol=str(prev.get("contract_symbol") or ""),
            accepted=False,
            rejects=rejects,
            normalized_input=normalized,
        )

    oi_v = _coerce_float(open_interest if open_interest is not None else normalized.get("open_interest"))
    min_oi_v = _coerce_float(min_open_interest)
    if min_oi_v is not None and oi_v is not None and oi_v < min_oi_v:
        _reject(
            rejects,
            stage=STAGE_RISK_FILTER,
            reason=REJECT_RISK_OPEN_INTEREST,
            message="open interest below minimum",
            metric_value=oi_v,
            threshold=min_oi_v,
        )

    vol_v = _coerce_float(volume if volume is not None else normalized.get("volume"))
    min_vol_v = _coerce_float(min_volume)
    if min_vol_v is not None and vol_v is not None and vol_v < min_vol_v:
        _reject(
            rejects,
            stage=STAGE_RISK_FILTER,
            reason=REJECT_RISK_VOLUME,
            message="volume below minimum",
            metric_value=vol_v,
            threshold=min_vol_v,
        )

    spread_v = _coerce_float(spread_ratio if spread_ratio is not None else normalized.get("spread_ratio"))
    max_spread_v = _coerce_float(max_spread_ratio)
    if max_spread_v is not None:
        if spread_v is None:
            _reject(
                rejects,
                stage=STAGE_RISK_FILTER,
                reason=REJECT_RISK_SPREAD,
                message="spread ratio unavailable",
                metric_value=spread_v,
                threshold=max_spread_v,
            )
        elif spread_v > max_spread_v:
            _reject(
                rejects,
                stage=STAGE_RISK_FILTER,
                reason=REJECT_RISK_SPREAD,
                message="spread ratio above maximum",
                metric_value=spread_v,
                threshold=max_spread_v,
            )

    event_mode_norm = str(event_mode or "warn").strip().lower() or "warn"
    if bool(event_flag):
        if event_mode_norm == "reject":
            _reject(
                rejects,
                stage=STAGE_RISK_FILTER,
                reason=REJECT_RISK_EVENT_REJECT,
                message="key event hit in reject mode",
                metric_value=True,
                threshold=event_mode_norm,
            )
        else:
            _reject(
                rejects,
                stage=STAGE_RISK_FILTER,
                reason=REJECT_RISK_EVENT_WARN,
                message="key event hit in warn mode",
                metric_value=True,
                threshold=event_mode_norm,
            )

    return build_candidate_decision(
        mode=mode_norm,
        symbol=str(prev.get("symbol") or ""),
        contract_symbol=str(prev.get("contract_symbol") or ""),
        accepted=not any(r.get("reason") != REJECT_RISK_EVENT_WARN for r in rejects),
        rejects=rejects,
        normalized_input=normalized,
    )


def evaluate_candidate_invariants(
    raw: dict[str, Any] | Any,
    *,
    mode: StrategyMode | str,
    risk_policy_version: str,
    quote_snapshot_id: str,
    min_dte: int | float | None = None,
    max_dte: int | float | None = None,
    min_strike: int | float | None = None,
    max_strike: int | float | None = None,
    min_annualized_return: int | float | None = None,
    min_net_income: int | float | None = None,
    annualized_return: int | float | None = None,
    net_income: int | float | None = None,
    min_open_interest: int | float | None = None,
    min_volume: int | float | None = None,
    max_spread_ratio: int | float | None = None,
    event_flag: bool = False,
    event_mode: str = "warn",
    open_interest: int | float | None = None,
    volume: int | float | None = None,
    spread_ratio: int | float | None = None,
    extra_required_fields: tuple[str, ...] | list[str] | None = None,
) -> dict[str, Any]:
    """Replay all Candidate Engine gates that do not consume portfolio resources."""

    policy_version = str(risk_policy_version or "").strip()
    snapshot_id = str(quote_snapshot_id or "").strip()
    if not policy_version or not snapshot_id:
        raise ValueError("risk policy version and quote snapshot id are required")
    mode_norm = normalize_strategy_mode(mode)
    stage1 = evaluate_candidate_non_resource_hard_constraints(
        raw,
        mode=mode_norm,
        min_dte=min_dte,
        max_dte=max_dte,
        min_strike=min_strike,
        max_strike=max_strike,
        extra_required_fields=extra_required_fields,
    )
    stage2 = evaluate_candidate_return_floor(
        stage1,
        min_annualized_return=min_annualized_return,
        min_net_income=min_net_income,
        annualized_return=annualized_return,
        net_income=net_income,
    )
    stage3 = evaluate_candidate_risk_filter(
        stage2,
        min_open_interest=min_open_interest,
        min_volume=min_volume,
        max_spread_ratio=max_spread_ratio,
        event_flag=event_flag,
        event_mode=event_mode,
        open_interest=open_interest,
        volume=volume,
        spread_ratio=spread_ratio,
    )
    normalized_input = dict(stage3.get("normalized_input") or {})
    policy = {
        "mode": mode_norm,
        "min_dte": _coerce_float(min_dte),
        "max_dte": _coerce_float(max_dte),
        "min_strike": _coerce_float(min_strike),
        "max_strike": _coerce_float(max_strike),
        "min_annualized_return": _coerce_float(min_annualized_return),
        "min_net_income": _coerce_float(min_net_income),
        "min_open_interest": _coerce_float(min_open_interest),
        "min_volume": _coerce_float(min_volume),
        "max_spread_ratio": _coerce_float(max_spread_ratio),
        "event_mode": str(event_mode or "warn").strip().lower(),
        "extra_required_fields": sorted(
            {
                str(item or "").strip()
                for item in (extra_required_fields or [])
                if str(item or "").strip()
            }
        ),
    }
    out = dict(stage3)
    out.update(
        {
            "risk_policy_version": policy_version,
            "risk_policy": policy,
            "risk_policy_hash": canonical_sha256(policy),
            "quote_snapshot_id": snapshot_id,
            "normalized_input_hash": canonical_sha256(normalized_input),
            "stage_decision_hashes": {
                "stage1_non_resource": canonical_sha256(stage1),
                "stage2_return_floor": canonical_sha256(stage2),
                "stage3_risk_filter": canonical_sha256(stage3),
            },
        }
    )
    out["decision_hash"] = canonical_sha256(out)
    return out


def attach_opening_decision_provenance(
    opening_decision: dict[str, Any],
    *,
    risk_policy_version: str,
    risk_policy_hash: str,
    quote_snapshot_id: str,
    normalized_input: dict[str, Any],
) -> dict[str, Any]:
    """Bind an actual opening decision to the input/policy used to produce it."""

    decision = validate_candidate_decision_payload(dict(opening_decision or {}))
    policy_version = str(risk_policy_version or "").strip()
    policy_hash = str(risk_policy_hash or "").strip()
    snapshot_id = str(quote_snapshot_id or "").strip()
    if not policy_version or not policy_hash or not snapshot_id:
        raise ValueError("opening decision provenance is incomplete")
    out = dict(decision)
    out.update(
        {
            "risk_policy_version": policy_version,
            "risk_policy_hash": policy_hash,
            "quote_snapshot_id": snapshot_id,
            "normalized_input": dict(normalized_input or {}),
            "normalized_input_hash": canonical_sha256(dict(normalized_input or {})),
        }
    )
    out["decision_hash"] = canonical_sha256(out)
    return out


def candidate_blocking_reject_reasons(decision: dict[str, Any]) -> tuple[str, ...]:
    payload = validate_candidate_decision_payload(dict(decision or {}))
    return tuple(
        sorted(
            {
                str(item.get("reason") or "")
                for item in payload.get("rejects", [])
                if str(item.get("reason") or "") != REJECT_RISK_EVENT_WARN
            }
        )
    )


def build_replacement_candidate_decision(
    *,
    candidate_id: str,
    opening_decision: dict[str, Any],
    invariant_decision: dict[str, Any],
) -> dict[str, Any]:
    opening = validate_candidate_decision_payload(dict(opening_decision or {}))
    invariant = validate_candidate_decision_payload(dict(invariant_decision or {}))
    mode = normalize_strategy_mode(opening.get("mode"))
    candidate = str(candidate_id or "").strip()
    if not candidate:
        raise ValueError("candidate_id is required")
    opening_hash = str(opening.get("decision_hash") or "")
    invariant_hash = str(invariant.get("decision_hash") or "")
    opening_without_hash = {
        key: value for key, value in opening.items() if key != "decision_hash"
    }
    invariant_without_hash = {
        key: value for key, value in invariant.items() if key != "decision_hash"
    }
    decision_hashes_valid = (
        opening_hash == canonical_sha256(opening_without_hash)
        and invariant_hash == canonical_sha256(invariant_without_hash)
    )
    opening_blocking = candidate_blocking_reject_reasons(opening)
    invariant_blocking = candidate_blocking_reject_reasons(invariant)
    expected_resource_reject = (
        REJECT_HARD_CAPACITY_PUT if mode == "put" else REJECT_HARD_CAPACITY_CALL
    )
    parity = decision_hashes_valid and all(
        (
            opening.get("normalized_input_hash"),
            invariant.get("normalized_input_hash"),
            opening.get("risk_policy_version"),
            invariant.get("risk_policy_version"),
            opening.get("risk_policy_hash"),
            invariant.get("risk_policy_hash"),
            opening.get("quote_snapshot_id"),
            invariant.get("quote_snapshot_id"),
        )
    ) and (
        opening.get("normalized_input_hash") == invariant.get("normalized_input_hash")
        and opening.get("risk_policy_version") == invariant.get("risk_policy_version")
        and opening.get("risk_policy_hash") == invariant.get("risk_policy_hash")
        and opening.get("quote_snapshot_id") == invariant.get("quote_snapshot_id")
    )
    eligibility = REPLACEMENT_REJECTED_INVARIANT
    resource_reject: str | None = None
    if parity and bool(invariant.get("accepted")) and not invariant_blocking:
        if bool(opening.get("accepted")) and not opening_blocking:
            eligibility = REPLACEMENT_ACCEPTED_OPENING
        elif opening_blocking == (expected_resource_reject,):
            eligibility = REPLACEMENT_CAPACITY_DEFERRED
            resource_reject = expected_resource_reject
    payload = {
        "schema_version": REPLACEMENT_CANDIDATE_SCHEMA_VERSION,
        "candidate_id": candidate,
        "strategy_mode": mode,
        "opening_decision_hash": opening_hash,
        "opening_accepted": bool(opening.get("accepted")),
        "opening_rejects": list(opening.get("rejects") or []),
        "blocking_reject_reasons": list(opening_blocking),
        "invariant_decision_hash": invariant_hash,
        "invariant_rejects": list(invariant.get("rejects") or []),
        "invariant_policy_parity": parity,
        "resource_relative_reject": resource_reject,
        "replacement_eligibility": eligibility,
        "risk_policy_version": str(invariant.get("risk_policy_version") or ""),
        "quote_snapshot_id": str(invariant.get("quote_snapshot_id") or ""),
    }
    payload["replacement_decision_hash"] = canonical_sha256(payload)
    return payload


def build_candidate_rank_key(
    row: dict[str, Any] | Any,
    *,
    mode: StrategyMode | str,
    score_weights: CandidateScoreWeights | None = None,
) -> dict[str, Any]:
    mode_norm = normalize_strategy_mode(mode)
    src = row if isinstance(row, dict) else {}
    if mode_norm == "put":
        score_inputs = _candidate_score_inputs(src, mode=mode_norm)
        annual = score_inputs["annualized_return"]
        net = score_inputs["net_income"]
        score = compute_candidate_strategy_score(
            mode=mode_norm,
            annualized_return=annual,
            net_income=net,
            spread_ratio=score_inputs["spread_ratio"],
            open_interest=score_inputs["open_interest"],
            volume=score_inputs["volume"],
            delta=score_inputs["delta"],
            otm_pct=score_inputs["otm_pct"],
            dte=score_inputs["dte"],
            vol_edge_score=score_inputs["vol_edge_score"],
            delta_target_score=score_inputs["delta_target_score"],
            concentration_score=score_inputs["concentration_score"],
            path_risk_score=score_inputs["path_risk_score"],
            weights=score_weights,
        )
        out: dict[str, Any] = {
            "strategy_score": score.total,
            "annualized_return": annual,
            "period_net_return": score_inputs["period_return"],
            "net_income": net,
            "score_components": dict(score.components),
            "score_warnings": list(score.warnings),
            "sort_tuple": _candidate_recommendation_sort_tuple(
                src,
                mode=mode_norm,
                annualized_return=annual,
                net_income=net,
            ),
        }
        return out

    score_inputs = _candidate_score_inputs(src, mode=mode_norm)
    annual = score_inputs["annualized_return"]
    net = score_inputs["net_income"]
    score = compute_candidate_strategy_score(
        mode=mode_norm,
        annualized_return=annual,
        net_income=net,
        spread_ratio=score_inputs["spread_ratio"],
        open_interest=score_inputs["open_interest"],
        volume=score_inputs["volume"],
        delta=score_inputs["delta"],
        otm_pct=score_inputs["otm_pct"],
        dte=score_inputs["dte"],
        vol_edge_score=score_inputs["vol_edge_score"],
        delta_target_score=score_inputs["delta_target_score"],
        concentration_score=score_inputs["concentration_score"],
        path_risk_score=score_inputs["path_risk_score"],
        weights=score_weights,
    )
    out = {
        "strategy_score": score.total,
        "annualized_return": annual,
        "period_net_return": score_inputs["period_return"],
        "net_income": net,
        "score_components": dict(score.components),
        "score_warnings": list(score.warnings),
        "sort_tuple": _candidate_recommendation_sort_tuple(
            src,
            mode=mode_norm,
            annualized_return=annual,
            net_income=net,
        ),
    }
    return out


def rank_candidate_rows(
    rows: list[dict[str, Any]],
    *,
    mode: StrategyMode | str,
    score_weights: CandidateScoreWeights | None = None,
) -> list[dict[str, Any]]:
    mode_norm = normalize_strategy_mode(mode)
    normalized_rows = [r for r in rows if isinstance(r, dict)]
    grouped: dict[str, list[dict[str, Any]]] = {}
    group_order: list[str] = []
    for row in normalized_rows:
        symbol = str(row.get("symbol") or "").strip().upper()
        group_key = symbol or str(
            row.get("contract_symbol") or row.get("option_symbol") or ""
        )
        if group_key not in grouped:
            grouped[group_key] = []
            group_order.append(group_key)
        grouped[group_key].append(row)

    period_return_fn = (
        _sell_put_period_return if mode_norm == "put" else _covered_call_period_return
    )
    within_tie_key = (
        _sell_put_within_symbol_tie_key
        if mode_norm == "put"
        else _covered_call_within_symbol_tie_key
    )
    cross_tie_key = (
        _sell_put_cross_symbol_tie_key
        if mode_norm == "put"
        else _covered_call_cross_symbol_tie_key
    )
    within_ranked = {
        key: _rank_return_bands(
            grouped[key],
            period_return_fn=period_return_fn,
            tie_key=within_tie_key,
        )
        for key in group_order
    }
    representatives = [within_ranked[key][0] for key in group_order]
    ranked_representatives = _rank_return_bands(
        representatives,
        period_return_fn=period_return_fn,
        tie_key=cross_tie_key,
    )
    remainder = [
        row
        for key in group_order
        for row in within_ranked[key][1:]
    ]
    return [*ranked_representatives, *remainder]


def select_best_candidate_per_symbol(
    rows: list[dict[str, Any]],
    *,
    mode: StrategyMode | str,
    score_weights: CandidateScoreWeights | None = None,
) -> list[dict[str, Any]]:
    """Return one highest-ranked hard-gate-passing contract per underlying."""
    ranked = rank_candidate_rows(rows, mode=mode, score_weights=score_weights)
    seen: set[str] = set()
    selected: list[dict[str, Any]] = []
    for row in ranked:
        symbol = str(row.get("symbol") or "").strip().upper()
        key = symbol or str(row.get("contract_symbol") or row.get("option_symbol") or "")
        if key not in seen:
            seen.add(key)
            selected.append(row)
    return selected
