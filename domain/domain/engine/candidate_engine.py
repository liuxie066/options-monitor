from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING
from typing import Any, Literal

from domain.domain.fee_calc import estimate_futu_option_sell_fee


CANDIDATE_ENGINE_SCHEMA_VERSION = "1.0"
SCHEMA_KIND_CANDIDATE_DECISION = "candidate_decision"
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
REJECT_CONTRACT_INELIGIBLE = "contract_ineligible"
REJECT_EVIDENCE_UNAVAILABLE = "evidence_unavailable"
REJECT_POLICY_REJECTED = "policy_rejected"
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
    REJECT_CONTRACT_INELIGIBLE,
    REJECT_EVIDENCE_UNAVAILABLE,
    REJECT_POLICY_REJECTED,
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

@dataclass(frozen=True)
class CandidateReject:
    stage: str
    reason: str
    message: str = ""
    metric_value: Any = None
    threshold: Any = None

    def to_payload(self) -> dict[str, Any]:
        return normalize_candidate_reject(self)


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


def _opening_not_ready_reason(status: str, reason_codes: set[str]) -> str:
    """Classify a non-ready opening contract into the three reject families.

    ``ineligible`` means the contract identity or state is provably not
    applicable; ``data_unavailable`` means required evidence cannot be proven;
    anything else (including market-closed observations) is treated as
    evidence-unavailable rather than a policy rejection.
    """

    if status == "ineligible":
        return REJECT_CONTRACT_INELIGIBLE
    if status == "data_unavailable":
        return REJECT_EVIDENCE_UNAVAILABLE
    if status == "market_closed":
        return REJECT_EVIDENCE_UNAVAILABLE
    return REJECT_EVIDENCE_UNAVAILABLE if reason_codes else REJECT_CONTRACT_INELIGIBLE


def _parse_decision_time(value: Any) -> datetime | None:
    """Parse a UTC receipt timestamp recorded by OM.

    Decision-time freshness evidence must carry an explicit timezone; naive
    values are rejected rather than guessed against a market clock.
    """

    if isinstance(value, datetime):
        parsed = value
    else:
        raw = str(value or "").strip()
        if not raw:
            return None
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _decision_now(value: Any) -> datetime:
    parsed = _parse_decision_time(value) if value is not None else None
    if parsed is not None:
        return parsed
    return datetime.now(timezone.utc)


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


def _first_text(raw: dict[str, Any], *fields: str) -> str:
    """Return the first usable text without evaluating nullable scalars as bools."""

    for field in fields:
        value = raw.get(field)
        if value is None:
            continue
        try:
            text = str(value).strip()
        except Exception:
            continue
        if text and text.lower() not in {"<na>", "nan", "nat", "none"}:
            return text
    return ""


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
    now_utc: Any = None,
    max_snapshot_age_seconds: int = 300,
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
        reason_codes = raw.get("opening_contract_reason_codes")
        code_set = (
            {str(item) for item in reason_codes if item}
            if isinstance(reason_codes, (list, tuple, set))
            else set()
        )
        raise CandidateCalculationError(
            _opening_not_ready_reason(opening_status, code_set),
            "normalized OpenD opening contract is not ready",
            metric_value={
                "status": opening_status or None,
                "reason_codes": reason_codes,
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

    # The 300-second acquisition-freshness window is owned by the decision
    # moment, not the fetch moment: re-evaluate it here against the real
    # decision clock instead of trusting the fetch-time ``ready`` snapshot.
    snapshot_received = _parse_decision_time(raw.get("snapshot_received_at_utc"))
    if snapshot_received is None:
        raise CandidateCalculationError(
            "evidence_unavailable",
            "OpenD option snapshot receipt timestamp is missing or invalid",
            metric_value={"snapshot_received_at_utc": raw.get("snapshot_received_at_utc")},
            threshold="present UTC receipt",
        )
    decision_now = _decision_now(now_utc)
    snapshot_age = (decision_now - snapshot_received).total_seconds()
    if snapshot_age < 0:
        raise CandidateCalculationError(
            "evidence_unavailable",
            "OpenD option snapshot receipt is in the future",
            metric_value={"snapshot_age_seconds": snapshot_age},
            threshold=">= 0",
        )
    if snapshot_age > int(max_snapshot_age_seconds):
        raise CandidateCalculationError(
            "evidence_unavailable",
            "OpenD option snapshot is stale relative to the decision moment",
            metric_value={"snapshot_age_seconds": snapshot_age},
            threshold=int(max_snapshot_age_seconds),
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
    if rv_status != "ok":
        raise CandidateCalculationError(
            "term_matched_rv_unavailable",
            "term-matched realized volatility is not available",
            metric_value={
                "status": rv_status or None,
                "reason": raw.get("term_matched_rv_reason"),
            },
            threshold="ok",
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
    min_dte: int | None = None,
    max_dte: int | None = None,
    min_strike: float | None = None,
    max_strike: float | None = None,
    min_annualized_return: float | None = None,
    min_net_premium_cny: float | None = None,
    min_iv_rv_ratio: float | None = None,
    min_iv_minus_rv: float | None = None,
    max_spread_ratio: float | None = None,
    require_earnings_evidence: bool = True,
    reject_known_earnings: bool = True,
) -> dict[str, Any]:
    """Evaluate the common formal opening gates owned by Candidate Engine."""

    mode_norm = normalize_strategy_mode(mode)
    src = dict(raw) if isinstance(raw, dict) else {}
    rejects: list[dict[str, Any]] = []

    resolved_min_dte = _first_float({"value": min_dte}, "value")
    if resolved_min_dte is None:
        resolved_min_dte = _first_float(src, "policy_min_dte")
    resolved_max_dte = _first_float({"value": max_dte}, "value")
    if resolved_max_dte is None:
        resolved_max_dte = _first_float(src, "policy_max_dte")
    resolved_min_strike = _first_float({"value": min_strike}, "value")
    if resolved_min_strike is None:
        resolved_min_strike = _first_float(src, "policy_min_strike")
    resolved_max_strike = _first_float({"value": max_strike}, "value")
    if resolved_max_strike is None:
        resolved_max_strike = _first_float(src, "policy_max_strike")
    resolved_min_annualized_return = _resolved_policy_float(
        min_annualized_return,
        src,
        "policy_min_annualized_return",
        OPENING_CANDIDATE_MIN_ANNUALIZED_RETURN,
    )
    resolved_min_net_premium_cny = _resolved_policy_float(
        min_net_premium_cny,
        src,
        "policy_min_net_premium_cny",
        OPENING_CANDIDATE_MIN_NET_PREMIUM_CNY,
    )
    resolved_min_iv_rv_ratio = _resolved_policy_float(
        min_iv_rv_ratio,
        src,
        "policy_min_iv_rv_ratio",
        OPENING_CANDIDATE_MIN_IV_RV_RATIO,
    )
    resolved_min_iv_minus_rv = _resolved_policy_float(
        min_iv_minus_rv,
        src,
        "policy_min_iv_minus_rv",
        OPENING_CANDIDATE_MIN_IV_MINUS_RV,
    )
    resolved_max_spread_ratio = _resolved_policy_float(
        max_spread_ratio,
        src,
        "policy_max_spread_ratio",
        OPENING_CANDIDATE_MAX_SPREAD_RATIO,
    )
    dte = _first_float(src, "dte")
    if resolved_min_dte is not None and (dte is None or dte < resolved_min_dte):
        _reject(
            rejects,
            stage=STAGE_HARD_CONSTRAINTS,
            reason=REJECT_HARD_DTE,
            message="dte below minimum or unavailable",
            metric_value=dte,
            threshold=resolved_min_dte,
        )
    if resolved_max_dte is not None and (dte is None or dte > resolved_max_dte):
        _reject(
            rejects,
            stage=STAGE_HARD_CONSTRAINTS,
            reason=REJECT_HARD_DTE,
            message="dte above maximum or unavailable",
            metric_value=dte,
            threshold=resolved_max_dte,
        )

    strike = _first_float(src, "strike")
    spot = _first_float(src, "spot")
    effective_min_strike = resolved_min_strike
    effective_max_strike = resolved_max_strike
    if spot is not None and spot > 0:
        if mode_norm == "put":
            effective_max_strike = min(
                value
                for value in (resolved_max_strike, spot)
                if value is not None
            )
            effective_min_strike = max(
                value
                for value in (resolved_min_strike, effective_max_strike * 0.80)
                if value is not None
            )
        else:
            effective_min_strike = max(
                value for value in (resolved_min_strike, spot) if value is not None
            )
            effective_max_strike = min(
                value
                for value in (resolved_max_strike, effective_min_strike * 1.20)
                if value is not None
            )
    if effective_min_strike is not None and (
        strike is None or strike < effective_min_strike
    ):
        _reject(
            rejects,
            stage=STAGE_HARD_CONSTRAINTS,
            reason=REJECT_HARD_STRIKE,
            message="strike below formal recall window or unavailable",
            metric_value=strike,
            threshold=effective_min_strike,
        )
    if effective_max_strike is not None and (
        strike is None or strike > effective_max_strike
    ):
        _reject(
            rejects,
            stage=STAGE_HARD_CONSTRAINTS,
            reason=REJECT_HARD_STRIKE,
            message="strike above formal recall window or unavailable",
            metric_value=strike,
            threshold=effective_max_strike,
        )

    if mode_norm == "put":
        max_new_contracts = _first_float(src, "max_new_contracts")
        capacity_reason = _first_text(
            src,
            "cash_secured_unavailable_reason",
            "cash_requirement_unavailable_reason",
            "cash_fx_status",
        )
        if max_new_contracts is None or max_new_contracts < 1:
            _reject(
                rejects,
                stage=STAGE_HARD_CONSTRAINTS,
                reason=REJECT_HARD_CAPACITY_PUT,
                message=(
                    f"sell put capacity below one contract: {capacity_reason}"
                    if capacity_reason
                    else "sell put capacity below one contract or unavailable"
                ),
                metric_value=max_new_contracts,
                threshold=1,
            )
    else:
        covered_contracts = _first_float(
            src,
            "covered_contracts_available",
            "max_new_contracts",
        )
        if covered_contracts is None or covered_contracts < 1:
            _reject(
                rejects,
                stage=STAGE_HARD_CONSTRAINTS,
                reason=REJECT_HARD_CAPACITY_CALL,
                message="covered call capacity below one contract or unavailable",
                metric_value=covered_contracts,
                threshold=1,
            )

    annualized = _first_float(
        src,
        "annualized_net_return_on_cash_basis"
        if mode_norm == "put"
        else "annualized_net_premium_return",
    )
    if annualized is None or annualized < resolved_min_annualized_return:
        _reject(
            rejects,
            stage=STAGE_RETURN_FLOOR,
            reason=REJECT_RETURN_ANNUALIZED,
            message="annualized net return below formal minimum or unavailable",
            metric_value=annualized,
            threshold=resolved_min_annualized_return,
        )

    net_premium_cny = _first_float(src, "net_premium_cny", "net_income_cny")
    if (
        net_premium_cny is None
        or net_premium_cny < resolved_min_net_premium_cny
    ):
        _reject(
            rejects,
            stage=STAGE_RETURN_FLOOR,
            reason=REJECT_RETURN_NET_PREMIUM_CNY,
            message="one-contract net premium in CNY below minimum or unavailable",
            metric_value=net_premium_cny,
            threshold=resolved_min_net_premium_cny,
        )

    spread_ratio = _first_float(src, "spread_ratio")
    if spread_ratio is None or spread_ratio > resolved_max_spread_ratio:
        _reject(
            rejects,
            stage=STAGE_RISK_FILTER,
            reason=REJECT_RISK_SPREAD,
            message="raw quote spread ratio above maximum or unavailable",
            metric_value=spread_ratio,
            threshold=resolved_max_spread_ratio,
        )

    iv_rv_ratio = _first_float(src, "iv_rv_ratio")
    if iv_rv_ratio is None or iv_rv_ratio < resolved_min_iv_rv_ratio:
        _reject(
            rejects,
            stage=STAGE_RISK_FILTER,
            reason=REJECT_RISK_IV_RV_RATIO,
            message="IV to term-matched RV ratio below minimum or unavailable",
            metric_value=iv_rv_ratio,
            threshold=resolved_min_iv_rv_ratio,
        )

    iv_minus_rv = _first_float(src, "iv_minus_rv")
    if iv_minus_rv is None or iv_minus_rv < resolved_min_iv_minus_rv:
        _reject(
            rejects,
            stage=STAGE_RISK_FILTER,
            reason=REJECT_RISK_IV_MINUS_RV,
            message="IV minus term-matched RV below minimum or unavailable",
            metric_value=iv_minus_rv,
            threshold=resolved_min_iv_minus_rv,
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


def _resolved_policy_float(
    explicit: Any,
    row: dict[str, Any],
    field: str,
    default: float,
) -> float:
    resolved = _first_float({"value": explicit}, "value")
    if resolved is None:
        resolved = _first_float(row, field)
    return float(default if resolved is None else resolved)


def _first_float(src: dict[str, Any], *names: str) -> float | None:
    for name in names:
        value = _coerce_float(src.get(name))
        if value is not None:
            return value
    return None


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
    spread = _first_float(src, "spread_ratio")
    open_interest = _first_float(src, "open_interest")
    concentration_sort = (
        _sell_put_concentration_sort(src)
        if mode == "put"
        else _covered_call_remaining_concentration_sort(src)
    )
    return (
        annual_missing,
        -float(primary_return or 0.0),
        -_candidate_tie_break_margin(src, mode=mode),
        *concentration_sort,
        float("inf") if spread is None else float(spread),
        -float(open_interest or 0.0),
        -float(net_income or 0.0),
        str(src.get("symbol") or "").strip().upper(),
        str(src.get("contract_symbol") or src.get("option_symbol") or ""),
    )


_RANK_DRIVER_LABELS: dict[str, str] = {
    "period_net_return_on_cash_basis": "持有期净收益",
    "period_net_premium_return": "持有期净权利金收益",
}


def explain_candidate_rank(
    row: dict[str, Any] | Any,
    *,
    mode: StrategyMode | str,
) -> dict[str, Any]:
    mode_norm = normalize_strategy_mode(mode)
    src = row if isinstance(row, dict) else {}
    rank_key = build_candidate_rank_key(src, mode=mode_norm)
    primary_drivers = [
        "period_net_return_on_cash_basis"
        if mode_norm == "put"
        else "period_net_premium_return"
    ]
    return {
        "mode": mode_norm,
        "ranking_policy": "candidate_engine",
        "symbol": str(src.get("symbol") or "").strip().upper() or None,
        "contract_symbol": str(src.get("contract_symbol") or src.get("option_symbol") or "").strip() or None,
        "option_type": str(src.get("option_type") or ("put" if mode_norm == "put" else "call")).strip().lower() or None,
        "expiration": str(src.get("expiration") or "").strip() or None,
        "strike": _first_float(src, "strike"),
        "annualized_return": rank_key.get("annualized_return"),
        "period_net_return": rank_key.get("period_net_return"),
        "net_income": rank_key.get("net_income"),
        "primary_drivers": primary_drivers,
        "primary_driver_labels": [
            _RANK_DRIVER_LABELS.get(item, item) for item in primary_drivers
        ],
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




def build_candidate_rank_key(
    row: dict[str, Any] | Any,
    *,
    mode: StrategyMode | str,
) -> dict[str, Any]:
    mode_norm = normalize_strategy_mode(mode)
    src = row if isinstance(row, dict) else {}
    annual = _first_float(
        src,
        "annualized_net_return_on_cash_basis"
        if mode_norm == "put"
        else "annualized_net_premium_return",
    )
    net = _first_float(src, "net_income")
    return {
        "annualized_return": annual,
        "period_net_return": _candidate_period_return(src, mode=mode_norm),
        "net_income": net,
        "sort_tuple": _candidate_recommendation_sort_tuple(
            src,
            mode=mode_norm,
            annualized_return=annual,
            net_income=net,
        ),
    }


def rank_candidate_rows(
    rows: list[dict[str, Any]],
    *,
    mode: StrategyMode | str,
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
) -> list[dict[str, Any]]:
    """Return one highest-ranked hard-gate-passing contract per underlying."""
    ranked = rank_candidate_rows(rows, mode=mode)
    seen: set[str] = set()
    selected: list[dict[str, Any]] = []
    for row in ranked:
        symbol = str(row.get("symbol") or "").strip().upper()
        key = symbol or str(row.get("contract_symbol") or row.get("option_symbol") or "")
        if key not in seen:
            seen.add(key)
            selected.append(row)
    return selected
