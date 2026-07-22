from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any

from domain.domain.short_vol_assessment import (
    EVENT_SOURCE_OK_STATUSES,
    ShortVolAssessmentConfig,
    ShortVolPortfolioContext,
    short_vol_assessment_fields,
)

CLOSE_ADVICE_DEFAULTS = {
    "max_spread_ratio": 0.3,
    "strong_remaining_annualized_max": 0.045,
    "medium_remaining_annualized_max": 0.07,
}

DEFAULT_STRONG_REMAINING_ANNUALIZED_MAX = CLOSE_ADVICE_DEFAULTS["strong_remaining_annualized_max"]
DEFAULT_MEDIUM_REMAINING_ANNUALIZED_MAX = CLOSE_ADVICE_DEFAULTS["medium_remaining_annualized_max"]

TIER_LABELS = {
    "strong": "强烈建议平仓",
    "medium": "建议平仓",
    "weak": "可观察平仓",
    "optional": "低价买回可选",
    "not_evaluable": "无法评估",
    "none": "不提醒",
}

TIER_PRIORITY = {
    "strong": 0,
    "medium": 1,
    "optional": 2,
    "weak": 3,
    "none": 9,
}

EXIT_STATE_NOT_EVALUABLE = "not_evaluable"
EXIT_STATE_HOLD = "hold"
EXIT_STATE_PROFIT_CAPTURE = "profit_capture"
EXIT_STATE_TAKE_PROFIT = "take_profit"
EXIT_STATE_SALVAGE = "salvage"
EXIT_STATE_LET_EXPIRE = "let_expire"

EXIT_REASON_TYPE_NOT_EVALUABLE = "not_evaluable"
EXIT_REASON_TYPE_HOLD = "hold"
EXIT_REASON_TYPE_PROFIT_CAPTURE = "profit_capture"
EXIT_REASON_TYPE_TAKE_PROFIT = "take_profit"
EXIT_REASON_TYPE_SALVAGE = "salvage"
EXIT_REASON_TYPE_THESIS_EXPIRED = "thesis_expired"

FEE_USABLE_STATUSES = {"schedule_estimate", "conservative_estimate"}

HOLD_REASON_TYPE_ASSIGNMENT_ACCEPTABLE = "assignment_acceptable"
HOLD_REASON_TYPE_CALLED_AWAY_ACCEPTABLE = "called_away_acceptable"

CURRENT_CLOSE_POLICY_VERSION = "p0_current.v1"
LEGACY_CLOSE_POLICY_VERSION = "legacy_p0"

RECOMMENDATION_HOLD = "hold"
RECOMMENDATION_REVIEW = "review"
RECOMMENDATION_CLOSE = "close"
RECOMMENDATION_NOT_EVALUABLE = "not_evaluable"

DECISION_EVIDENCE_COMPLETE = "complete"
DECISION_EVIDENCE_PARTIAL = "partial"
DECISION_EVIDENCE_REVIEW_REQUIRED = "review_required"
DECISION_EVIDENCE_NOT_EVALUABLE = "not_evaluable"

POLICY_VARIANT_P0_CURRENT = "P0_current"
POLICY_VARIANT_P1_SEMANTIC_SPLIT = "P1_semantic_split"
POLICY_VARIANT_P2_PROFILE_AWARE = "P2_profile_aware"
POLICY_VARIANT_P3_OPPORTUNITY_REQUIRED = "P3_opportunity_required"

DOMAIN_CLOSE_POLICY_VARIANTS = frozenset(
    {
        POLICY_VARIANT_P0_CURRENT,
        POLICY_VARIANT_P1_SEMANTIC_SPLIT,
        POLICY_VARIANT_P2_PROFILE_AWARE,
    }
)

RETURN_FIRST_POLICY_PROFILE = "return_first"
UNDERWRITING_POLICY_PROFILES = frozenset({"insurance_underwriting", "short_vol"})

PRICING_BLOCKING_FLAGS = {
    "missing_premium",
    "invalid_premium",
    "missing_mid",
    "invalid_mid",
    "missing_dte",
    "invalid_dte",
    "missing_multiplier",
    "invalid_multiplier",
    "missing_contracts_open",
    "invalid_contracts_open",
    "invalid_spread",
    "spread_too_wide",
}

LONG_CALL_CONVEXITY_DEFAULTS = {
    "max_spread_ratio": CLOSE_ADVICE_DEFAULTS["max_spread_ratio"],
    "take_profit_value_ratio": 2.0,
    "salvage_dte_max": 7,
    "salvage_abs_delta_max": 0.10,
    "salvage_min_mid": 0.05,
}


def safe_float(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        out = float(value)
        if math.isnan(out):
            return None
        return out
    except Exception:
        return None


def safe_int(value: Any) -> int | None:
    try:
        if value in (None, ""):
            return None
        out = float(value)
        if math.isnan(out):
            return None
        return int(out)
    except Exception:
        return None


@dataclass(frozen=True)
class CloseAdviceConfig:
    max_spread_ratio: float | None = CLOSE_ADVICE_DEFAULTS["max_spread_ratio"]
    strong_remaining_annualized_max: float = DEFAULT_STRONG_REMAINING_ANNUALIZED_MAX
    medium_remaining_annualized_max: float = DEFAULT_MEDIUM_REMAINING_ANNUALIZED_MAX

    @classmethod
    def from_mapping(cls, raw: dict[str, Any] | None) -> "CloseAdviceConfig":
        src = raw or {}
        max_spread = safe_float(src.get("max_spread_ratio"))
        strong_max = safe_float(src.get("strong_remaining_annualized_max"))
        medium_max = safe_float(src.get("medium_remaining_annualized_max"))
        return cls(
            max_spread_ratio=max_spread if max_spread is not None else CLOSE_ADVICE_DEFAULTS["max_spread_ratio"],
            strong_remaining_annualized_max=(
                strong_max if strong_max is not None else DEFAULT_STRONG_REMAINING_ANNUALIZED_MAX
            ),
            medium_remaining_annualized_max=(
                medium_max if medium_max is not None else DEFAULT_MEDIUM_REMAINING_ANNUALIZED_MAX
            ),
        )


@dataclass(frozen=True)
class LongCallConvexityConfig:
    max_spread_ratio: float | None = LONG_CALL_CONVEXITY_DEFAULTS["max_spread_ratio"]
    take_profit_value_ratio: float = LONG_CALL_CONVEXITY_DEFAULTS["take_profit_value_ratio"]
    salvage_dte_max: int = LONG_CALL_CONVEXITY_DEFAULTS["salvage_dte_max"]
    salvage_abs_delta_max: float = LONG_CALL_CONVEXITY_DEFAULTS["salvage_abs_delta_max"]
    salvage_min_mid: float = LONG_CALL_CONVEXITY_DEFAULTS["salvage_min_mid"]

    @classmethod
    def from_mapping(cls, raw: dict[str, Any] | None) -> "LongCallConvexityConfig":
        src = raw or {}
        max_spread = safe_float(src.get("max_spread_ratio"))
        take_profit = safe_float(src.get("take_profit_value_ratio"))
        salvage_dte = safe_int(src.get("salvage_dte_max"))
        salvage_delta = safe_float(src.get("salvage_abs_delta_max"))
        salvage_mid = safe_float(src.get("salvage_min_mid"))
        return cls(
            max_spread_ratio=(
                max_spread
                if max_spread is not None and max_spread >= 0
                else LONG_CALL_CONVEXITY_DEFAULTS["max_spread_ratio"]
            ),
            take_profit_value_ratio=(
                take_profit
                if take_profit is not None and take_profit > 0
                else LONG_CALL_CONVEXITY_DEFAULTS["take_profit_value_ratio"]
            ),
            salvage_dte_max=(
                salvage_dte
                if salvage_dte is not None and salvage_dte >= 0
                else LONG_CALL_CONVEXITY_DEFAULTS["salvage_dte_max"]
            ),
            salvage_abs_delta_max=(
                salvage_delta
                if salvage_delta is not None and salvage_delta >= 0
                else LONG_CALL_CONVEXITY_DEFAULTS["salvage_abs_delta_max"]
            ),
            salvage_min_mid=(
                salvage_mid
                if salvage_mid is not None and salvage_mid >= 0
                else LONG_CALL_CONVEXITY_DEFAULTS["salvage_min_mid"]
            ),
        )


@dataclass(frozen=True)
class CloseDecisionFacts:
    tier: str
    exit_state: str
    side: str
    option_type: str
    strategy_family: str
    strategy_profile: str
    evaluation_status: str
    fee_calc_status: str
    estimated_pnl_if_close_net: float | None
    thesis_status: str
    continued_willingness: bool | None
    close_calibration_status: str
    combo_evidence_status: str = "not_applicable"


@dataclass(frozen=True)
class ClosePolicyResult:
    policy_version: str
    recommendation_state: str
    decision_basis: tuple[str, ...]
    decision_evidence_status: str

    def to_fields(self) -> dict[str, Any]:
        return {
            "policy_version": self.policy_version,
            "recommendation_state": self.recommendation_state,
            "decision_basis": self.decision_basis,
            "decision_evidence_status": self.decision_evidence_status,
        }


@dataclass(frozen=True)
class CloseAdviceTierRule:
    level: str
    reason: str
    min_capture: float
    min_dte: int | None = None
    max_dte: int | None = None
    remaining_annualized_attr: str | None = None

    def matches(
        self,
        *,
        capture_ratio: float,
        dte: int,
        remaining_annualized_return: float | None,
        config: CloseAdviceConfig,
    ) -> bool:
        if self.min_dte is not None and dte < self.min_dte:
            return False
        if self.max_dte is not None and dte > self.max_dte:
            return False
        if capture_ratio < self.min_capture:
            return False
        if self.remaining_annualized_attr:
            limit = safe_float(getattr(config, self.remaining_annualized_attr, None))
            if limit is None or remaining_annualized_return is None:
                return False
            if remaining_annualized_return > limit:
                return False
        return True


LONG_HOLD_REASON = "已锁定大部分收益，剩余时间仍长，继续持有的边际收益偏低"
MEDIUM_REASON = "已锁定较多收益，剩余时间仍较长，值得认真考虑买回"
OPTIONAL_REASON = "临近到期且平仓成本较低，低价买回可选"
WEAK_REASON = "已锁定部分收益且剩余时间较长，适合进入观察"

DEFAULT_TIER_RULE_SPECS: tuple[dict[str, Any], ...] = (
    {
        "level": "strong",
        "min_dte": 7,
        "max_dte": 13,
        "min_capture": 0.90,
        "remaining_annualized_attr": "strong_remaining_annualized_max",
        "reason": LONG_HOLD_REASON,
    },
    {
        "level": "strong",
        "min_dte": 14,
        "max_dte": 29,
        "min_capture": 0.85,
        "remaining_annualized_attr": "strong_remaining_annualized_max",
        "reason": LONG_HOLD_REASON,
    },
    {
        "level": "strong",
        "min_dte": 30,
        "min_capture": 0.80,
        "remaining_annualized_attr": "strong_remaining_annualized_max",
        "reason": LONG_HOLD_REASON,
    },
    {
        "level": "medium",
        "min_dte": 14,
        "min_capture": 0.70,
        "remaining_annualized_attr": "medium_remaining_annualized_max",
        "reason": MEDIUM_REASON,
    },
    {
        "level": "optional",
        "min_dte": 1,
        "max_dte": 6,
        "min_capture": 0.90,
        "reason": OPTIONAL_REASON,
    },
    {
        "level": "weak",
        "min_dte": 30,
        "min_capture": 0.50,
        "reason": WEAK_REASON,
    },
)


DEFAULT_TIER_RULES: tuple[CloseAdviceTierRule, ...] = tuple(
    CloseAdviceTierRule(**spec) for spec in DEFAULT_TIER_RULE_SPECS
)


@dataclass(frozen=True)
class CloseAdviceInput:
    account: str
    symbol: str
    option_type: str
    side: str
    expiration: str | None
    strike: float | None
    contracts_open: int | None
    premium: float | None
    close_mid: float | None
    bid: float | None = None
    ask: float | None = None
    dte: int | None = None
    multiplier: float | None = None
    spot: float | None = None
    currency: str | None = None
    delta: float | None = None


def _remaining_annualized_return(inp: CloseAdviceInput) -> float | None:
    mid = safe_float(inp.close_mid)
    dte = safe_int(inp.dte)
    if mid is None or dte is None or dte <= 0:
        return None
    if str(inp.option_type).lower() == "put":
        denominator = safe_float(inp.strike)
    elif str(inp.option_type).lower() == "call":
        denominator = safe_float(inp.spot)
    else:
        denominator = None
    if denominator is None or denominator <= 0:
        return None
    return (float(mid) / float(denominator)) * (365.0 / float(dte))


def _spread_ratio(bid: float | None, ask: float | None, mid: float | None) -> float | None:
    if bid is None or ask is None or mid is None:
        return None
    if ask < bid or mid <= 0:
        return None
    return (ask - bid) / mid


def decide_tier(
    *,
    capture_ratio: float,
    dte: int,
    remaining_annualized_return: float | None,
    config: CloseAdviceConfig,
) -> tuple[str, str]:
    for rule in DEFAULT_TIER_RULES:
        if rule.matches(
            capture_ratio=capture_ratio,
            dte=dte,
            remaining_annualized_return=remaining_annualized_return,
            config=config,
        ):
            return rule.level, rule.reason

    return "none", "未达到平仓建议阈值"


def evaluate_close_advice(inp: CloseAdviceInput, config: CloseAdviceConfig | None = None) -> dict[str, Any]:
    cfg = config or CloseAdviceConfig()
    flags: list[str] = []

    option_type = str(inp.option_type or "").strip().lower()
    side = str(inp.side or "").strip().lower()
    if side != "short" or option_type not in {"put", "call"}:
        flags.append("unsupported_position")
        return _result(
            inp,
            tier="not_evaluable",
            reason="收益捕获平仓仅支持 open short put/call",
            flags=flags,
            exit_state=EXIT_STATE_NOT_EVALUABLE,
            exit_reason_type=EXIT_REASON_TYPE_NOT_EVALUABLE,
        )

    premium = safe_float(inp.premium)
    mid = safe_float(inp.close_mid)
    dte = safe_int(inp.dte)
    multiplier = safe_float(inp.multiplier)
    contracts_open = safe_int(inp.contracts_open)

    if premium is None:
        flags.append("missing_premium")
    elif premium <= 0:
        flags.append("invalid_premium")
    if mid is None:
        flags.append("missing_mid")
    elif mid <= 0:
        flags.append("invalid_mid")
    if dte is None:
        flags.append("missing_dte")
    elif dte <= 0:
        flags.append("invalid_dte")
    if multiplier is None:
        flags.append("missing_multiplier")
    elif multiplier <= 0:
        flags.append("invalid_multiplier")
    if contracts_open is None:
        flags.append("missing_contracts_open")
    elif contracts_open <= 0:
        flags.append("invalid_contracts_open")

    spread = _spread_ratio(inp.bid, inp.ask, mid)
    if inp.bid is not None and inp.ask is not None and inp.ask < inp.bid:
        flags.append("invalid_spread")
    if cfg.max_spread_ratio is not None and spread is not None and spread > cfg.max_spread_ratio:
        flags.append("spread_too_wide")

    blocking = [f for f in flags if f in PRICING_BLOCKING_FLAGS]
    if blocking:
        return _result(
            inp,
            tier="not_evaluable",
            reason="数据不足或报价质量不足，暂不提醒",
            flags=flags,
            spread_ratio=spread,
            exit_state=EXIT_STATE_NOT_EVALUABLE,
            exit_reason_type=EXIT_REASON_TYPE_NOT_EVALUABLE,
        )

    assert premium is not None and mid is not None and dte is not None
    assert multiplier is not None and contracts_open is not None

    capture_ratio = (premium - mid) / premium
    remaining_premium = mid * multiplier * contracts_open
    realized_if_close = (premium - mid) * multiplier * contracts_open
    remaining_annualized = _remaining_annualized_return(inp)

    if mid >= premium:
        flags.append("not_profitable_to_close")
        return _result(
            inp,
            tier="none",
            reason="当前平仓价不低于开仓权利金，不属于收益型买回建议",
            flags=flags,
            capture_ratio=capture_ratio,
            remaining_premium=remaining_premium,
            realized_if_close=realized_if_close,
            remaining_annualized_return=remaining_annualized,
            spread_ratio=spread,
            exit_state=EXIT_STATE_HOLD,
            exit_reason_type=EXIT_REASON_TYPE_HOLD,
        )

    if remaining_annualized is None:
        flags.append("missing_remaining_annualized_return")

    tier, reason = decide_tier(
        capture_ratio=capture_ratio,
        dte=dte,
        remaining_annualized_return=remaining_annualized,
        config=cfg,
    )
    exit_state = EXIT_STATE_PROFIT_CAPTURE if tier != "none" else EXIT_STATE_HOLD
    exit_reason_type = EXIT_REASON_TYPE_PROFIT_CAPTURE if tier != "none" else EXIT_REASON_TYPE_HOLD
    return _result(
        inp,
        tier=tier,
        reason=reason,
        flags=flags,
        capture_ratio=capture_ratio,
        remaining_premium=remaining_premium,
        realized_if_close=realized_if_close,
        remaining_annualized_return=remaining_annualized,
        spread_ratio=spread,
        exit_state=exit_state,
        exit_reason_type=exit_reason_type,
    )


def evaluate_long_call_convexity_advice(
    inp: CloseAdviceInput,
    config: LongCallConvexityConfig | None = None,
) -> dict[str, Any]:
    cfg = config or LongCallConvexityConfig()
    flags: list[str] = []

    option_type = str(inp.option_type or "").strip().lower()
    side = str(inp.side or "").strip().lower()
    if side != "long" or option_type != "call":
        flags.append("unsupported_position")
        return _result(
            inp,
            tier="not_evaluable",
            reason="long-call convexity 评估仅支持 open long call",
            flags=flags,
            exit_state=EXIT_STATE_NOT_EVALUABLE,
            exit_reason_type=EXIT_REASON_TYPE_NOT_EVALUABLE,
        )

    premium = safe_float(inp.premium)
    mid = safe_float(inp.close_mid)
    dte = safe_int(inp.dte)
    multiplier = safe_float(inp.multiplier)
    contracts_open = safe_int(inp.contracts_open)

    if premium is None:
        flags.append("missing_premium")
    elif premium <= 0:
        flags.append("invalid_premium")
    if mid is None:
        flags.append("missing_mid")
    elif mid <= 0:
        flags.append("invalid_mid")
    if dte is None:
        flags.append("missing_dte")
    elif dte < 0:
        flags.append("invalid_dte")
    if multiplier is None:
        flags.append("missing_multiplier")
    elif multiplier <= 0:
        flags.append("invalid_multiplier")
    if contracts_open is None:
        flags.append("missing_contracts_open")
    elif contracts_open <= 0:
        flags.append("invalid_contracts_open")

    spread = _spread_ratio(inp.bid, inp.ask, mid)
    if inp.bid is not None and inp.ask is not None and inp.ask < inp.bid:
        flags.append("invalid_spread")
    if cfg.max_spread_ratio is not None and spread is not None and spread > cfg.max_spread_ratio:
        flags.append("spread_too_wide")

    blocking = [f for f in flags if f in PRICING_BLOCKING_FLAGS]
    if blocking:
        return _result(
            inp,
            tier="not_evaluable",
            reason="long call 数据不足或报价质量不足，暂不提醒",
            flags=flags,
            spread_ratio=spread,
            exit_state=EXIT_STATE_NOT_EVALUABLE,
            exit_reason_type=EXIT_REASON_TYPE_NOT_EVALUABLE,
        )

    assert premium is not None and mid is not None and dte is not None
    assert multiplier is not None and contracts_open is not None

    value_ratio = mid / premium
    current_value = mid * multiplier * contracts_open
    cost_basis = premium * multiplier * contracts_open
    realized_if_close = current_value - cost_basis
    abs_delta = safe_float(inp.delta)
    if abs_delta is not None:
        abs_delta = abs(abs_delta)

    if value_ratio >= cfg.take_profit_value_ratio:
        tier = "medium"
        reason = "long call 已显著升值，优先考虑兑现右尾收益"
        exit_state = EXIT_STATE_TAKE_PROFIT
        exit_reason_type = EXIT_REASON_TYPE_TAKE_PROFIT
    elif dte <= cfg.salvage_dte_max and (
        abs_delta is None or abs_delta <= cfg.salvage_abs_delta_max
    ):
        if mid >= cfg.salvage_min_mid:
            tier = "optional"
            reason = "long call 临近到期且右尾 thesis 转弱，仍有可回收残值"
            exit_state = EXIT_STATE_SALVAGE
            exit_reason_type = EXIT_REASON_TYPE_SALVAGE
        else:
            tier = "none"
            reason = "long call 残值过低，卖出意义不大，可允许归零"
            exit_state = EXIT_STATE_LET_EXPIRE
            exit_reason_type = EXIT_REASON_TYPE_THESIS_EXPIRED
    else:
        tier = "none"
        reason = "long call 仍保留右尾 convexity，可继续持有"
        exit_state = EXIT_STATE_HOLD
        exit_reason_type = EXIT_REASON_TYPE_HOLD

    row = _result(
        inp,
        tier=tier,
        reason=reason,
        flags=flags,
        remaining_premium=current_value,
        realized_if_close=realized_if_close,
        spread_ratio=spread,
        exit_state=exit_state,
        exit_reason_type=exit_reason_type,
    )
    row["long_call_value_ratio"] = value_ratio
    row["long_call_cost_basis"] = cost_basis
    row["long_call_current_value"] = current_value
    row["abs_delta"] = abs_delta
    return row


def evaluate_short_vol_close_advice(
    inp: CloseAdviceInput,
    *,
    short_vol_config: ShortVolAssessmentConfig,
    close_config: CloseAdviceConfig | None = None,
    quote_row: dict[str, Any] | None = None,
    mode: str | None = None,
) -> dict[str, Any]:
    """Evaluate close advice for positions opened under a short-volatility thesis.

    The existing close advice model is a return-capture model. This wrapper keeps
    the same quote-quality and profitability gates, then overlays optional
    short-vol observations. Missing or weaker IV/RV, delta, or event context does
    not create or invalidate a close recommendation by itself.
    """

    row = evaluate_close_advice(inp, close_config)
    mode_norm = "call" if str(mode or inp.option_type or "").strip().lower() == "call" else "put"
    risk_input = _short_vol_close_risk_input(inp, quote_row)
    risk_fields = short_vol_assessment_fields(
        risk_input,
        mode=mode_norm,  # type: ignore[arg-type]
        cfg=short_vol_config,
        risk_ctx=ShortVolPortfolioContext(
            nav_cny=None,
            stock_value_cny_by_symbol={},
            short_put_assignment_cny_by_symbol={},
            short_put_assignment_total_cny=0.0,
            unavailable_reasons=(),
        ),
    )
    row.update(risk_fields)
    row.update(
        _remaining_stress_observations(
            inp,
            short_vol_config=short_vol_config,
            mode=mode_norm,
            realized_volatility=risk_fields.get("realized_volatility_estimate"),
        )
    )
    row["path_stress_status"] = "ok" if risk_fields.get("path_stress_evaluable") is True else "not_evaluable"
    event_status = str(risk_fields.get("event_source_status") or "").strip().lower()
    if risk_fields.get("event_risk_flag") is True:
        event_context_status = "in_window"
    elif event_status in EVENT_SOURCE_OK_STATUSES:
        event_context_status = "clear"
    else:
        event_context_status = event_status or "unknown"
    row["event_context_status"] = event_context_status
    row["event_context_types"] = risk_fields.get("event_risk_types")
    row["event_context_dates"] = risk_fields.get("event_risk_dates")

    flags = {x for x in str(row.get("data_quality_flags") or "").split(";") if x}
    if str(row.get("tier") or "").strip().lower() == "not_evaluable" or (flags & PRICING_BLOCKING_FLAGS):
        row["short_vol_thesis_status"] = "not_evaluable"
        row["exit_state"] = EXIT_STATE_NOT_EVALUABLE
        row["exit_reason_type"] = EXIT_REASON_TYPE_NOT_EVALUABLE
        return row

    missing = []
    if risk_fields.get("realized_volatility_estimate") is None:
        missing.append("rv")
    if risk_fields.get("implied_volatility") is None:
        missing.append("iv")
    if risk_fields.get("abs_delta") is None:
        missing.append("delta")
    if missing:
        missing_reason = f"缺少 short-vol 观察数据: {', '.join(missing)}"
        if str(row.get("exit_state") or "").strip().lower() == EXIT_STATE_HOLD:
            return _short_vol_acceptance_hold(
                row,
                reason=(
                    f"{_short_vol_valid_hold_reason(mode_norm)}；{missing_reason}，"
                    "仅缺少观察项，不作为平仓依据"
                ),
                status="not_evaluable",
                hold_reason_type=_short_vol_acceptance_hold_reason_type(mode_norm),
                flag="short_vol_risk_data_missing",
            )
        return _short_vol_not_evaluable(
            row,
            reason=missing_reason,
            flag="short_vol_risk_data_missing",
            preserve_action=True,
        )

    ratio = safe_float(risk_fields.get("iv_rv_ratio"))
    spread = safe_float(risk_fields.get("iv_minus_rv"))
    ratio_bad = ratio is None or ratio < short_vol_config.min_iv_rv_ratio
    spread_bad = spread is None or spread < short_vol_config.min_iv_minus_rv
    abs_delta = safe_float(risk_fields.get("abs_delta"))
    observations = _short_vol_observation_items(
        ratio_bad=ratio_bad,
        spread_bad=spread_bad,
        abs_delta=abs_delta,
        max_abs_delta=short_vol_config.max_abs_delta,
        event_context_status=event_context_status,
    )
    thesis_status = "observe" if observations else "valid"

    row["short_vol_thesis_status"] = thesis_status
    row["short_vol_reason"] = _short_vol_thesis_reason(observations)
    if str(row.get("exit_state") or "").strip().lower() == EXIT_STATE_PROFIT_CAPTURE:
        return row
    if str(row.get("exit_state") or "").strip().lower() == EXIT_STATE_HOLD:
        return _short_vol_acceptance_hold(
            row,
            reason=_short_vol_hold_reason(mode_norm, observations),
            status=thesis_status,
            hold_reason_type=_short_vol_acceptance_hold_reason_type(mode_norm),
        )
    return row


def _short_vol_close_risk_input(inp: CloseAdviceInput, quote_row: dict[str, Any] | None) -> dict[str, Any]:
    row = dict(quote_row or {})
    row.setdefault("symbol", inp.symbol)
    row.setdefault("option_type", inp.option_type)
    row.setdefault("expiration", inp.expiration)
    row.setdefault("strike", inp.strike)
    row.setdefault("dte", inp.dte)
    row.setdefault("spot", inp.spot)
    row.setdefault("delta", inp.delta)
    row.setdefault("currency", inp.currency)
    row.setdefault("multiplier", inp.multiplier)
    row.setdefault("premium_cny", None)
    return row


def _remaining_stress_observations(
    inp: CloseAdviceInput,
    *,
    short_vol_config: ShortVolAssessmentConfig,
    mode: str,
    realized_volatility: Any,
) -> dict[str, Any]:
    spot = safe_float(inp.spot)
    strike = safe_float(inp.strike)
    mid = safe_float(inp.close_mid)
    multiplier = safe_float(inp.multiplier)
    contracts = safe_int(inp.contracts_open)
    missing = [
        name
        for name, value in (
            ("spot", spot),
            ("strike", strike),
            ("close_mid", mid),
            ("multiplier", multiplier),
            ("contracts_open", contracts),
        )
        if value is None or value <= 0
    ]
    if missing:
        return {
            "remaining_risk_status": "not_evaluable",
            "remaining_risk_unavailable_reason": ",".join(missing),
        }

    assert spot is not None and strike is not None and mid is not None
    assert multiplier is not None and contracts is not None
    remaining_reward = mid * multiplier * contracts
    scenarios: list[tuple[str, float]] = []
    if mode == "call":
        scenario_price = spot * (1.0 + max(0.0, float(short_vol_config.call_gap_up_pct)))
        scenarios.append(("call_gap_up", max(0.0, scenario_price - strike)))
    else:
        gap_price = max(0.0, spot * (1.0 - max(0.0, float(short_vol_config.gap_down_pct))))
        scenarios.append(("put_gap_down", max(0.0, strike - gap_price)))
        rv = safe_float(realized_volatility)
        dte = safe_int(inp.dte)
        if rv is not None and rv >= 0 and dte is not None and dte > 0:
            sigma_move = max(0.0, float(short_vol_config.stress_down_sigma_multiple)) * rv * math.sqrt(dte / 365.0)
            sigma_price = max(0.0, spot * (1.0 - sigma_move))
            scenarios.append(("put_sigma_down", max(0.0, strike - sigma_price)))

    scenario, intrinsic_per_share = max(scenarios, key=lambda item: item[1])
    scenario_liability = intrinsic_per_share * multiplier * contracts
    stress_loss = max(0.0, scenario_liability - remaining_reward)
    return {
        "remaining_risk_status": "ok",
        "remaining_risk_unavailable_reason": None,
        "remaining_stress_scenario": scenario,
        "remaining_stress_loss": round(stress_loss, 6),
        "remaining_reward_to_stress_loss": (
            round(remaining_reward / stress_loss, 6) if stress_loss > 0 else None
        ),
    }


def _short_vol_not_evaluable(
    row: dict[str, Any],
    *,
    reason: str,
    flag: str,
    preserve_action: bool = False,
) -> dict[str, Any]:
    out = dict(row)
    if not preserve_action:
        out["tier"] = "not_evaluable"
        out["tier_label"] = TIER_LABELS["not_evaluable"]
        out["reason"] = reason
        out["exit_state"] = EXIT_STATE_NOT_EVALUABLE
        out["exit_reason_type"] = EXIT_REASON_TYPE_NOT_EVALUABLE
    out["short_vol_thesis_status"] = "not_evaluable"
    out["short_vol_reason"] = reason
    flags = [x for x in str(out.get("data_quality_flags") or "").split(";") if x]
    flags.append(flag)
    out["data_quality_flags"] = ";".join(dict.fromkeys(flags))
    return out


def _short_vol_acceptance_hold_reason_type(mode: str) -> str:
    return HOLD_REASON_TYPE_CALLED_AWAY_ACCEPTABLE if mode == "call" else HOLD_REASON_TYPE_ASSIGNMENT_ACCEPTABLE


def _short_vol_valid_hold_reason(mode: str) -> str:
    if mode == "call":
        return "Covered Call 默认可被行权卖出正股；当前未达到收益回收阈值，继续持有等待归零或被行权"
    return "Sell Put 默认可接货；当前未达到收益回收阈值，继续持有等待归零或接货"


def _short_vol_observation_items(
    *,
    ratio_bad: bool,
    spread_bad: bool,
    abs_delta: float | None,
    max_abs_delta: float,
    event_context_status: str,
) -> list[str]:
    items: list[str] = []
    if ratio_bad and spread_bad:
        items.append("IV/RV edge 转弱，需观察承保补偿")
    elif ratio_bad:
        items.append("IV/RV ratio 转弱，需观察承保补偿")
    elif spread_bad:
        items.append("IV-RV spread 转弱，需观察承保补偿")
    if abs_delta is not None and abs_delta > max_abs_delta:
        items.append("delta 偏离承保观察区间")
    if str(event_context_status or "").strip().lower() == "in_window":
        items.append("到期前存在事件风险")
    return items


def _short_vol_thesis_reason(observations: list[str]) -> str:
    if observations:
        return f"short-vol 持仓存在观察项：{'；'.join(observations)}"
    return "IV/RV edge 和 delta 区间仍支持 short-vol 持仓"


def _short_vol_hold_reason(mode: str, observations: list[str]) -> str:
    base = _short_vol_valid_hold_reason(mode)
    if not observations:
        return base
    return f"{base}；观察项：{'；'.join(observations)}，不作为平仓提醒"


def _short_vol_acceptance_hold(
    row: dict[str, Any],
    *,
    reason: str,
    status: str,
    hold_reason_type: str,
    flag: str | None = None,
) -> dict[str, Any]:
    out = dict(row)
    out["tier"] = "none"
    out["tier_label"] = TIER_LABELS["none"]
    out["reason"] = reason
    out["short_vol_thesis_status"] = status
    out["short_vol_reason"] = reason
    out["hold_reason_type"] = hold_reason_type
    out["exit_state"] = EXIT_STATE_HOLD
    out["exit_reason_type"] = EXIT_REASON_TYPE_HOLD
    if flag:
        flags = [x for x in str(out.get("data_quality_flags") or "").split(";") if x]
        flags.append(flag)
        out["data_quality_flags"] = ";".join(dict.fromkeys(flags))
    return out


def _result(
    inp: CloseAdviceInput,
    *,
    tier: str,
    reason: str,
    flags: list[str],
    capture_ratio: float | None = None,
    remaining_premium: float | None = None,
    realized_if_close: float | None = None,
    remaining_annualized_return: float | None = None,
    spread_ratio: float | None = None,
    exit_state: str | None = None,
    exit_reason_type: str | None = None,
) -> dict[str, Any]:
    resolved_exit_state, resolved_exit_reason_type = _resolve_exit_contract(
        tier=tier,
        exit_state=exit_state,
        exit_reason_type=exit_reason_type,
    )
    return {
        "account": str(inp.account or "").strip().lower(),
        "symbol": str(inp.symbol or "").strip().upper(),
        "option_type": str(inp.option_type or "").strip().lower(),
        "expiration": inp.expiration,
        "strike": safe_float(inp.strike),
        "contracts_open": safe_int(inp.contracts_open),
        "premium": safe_float(inp.premium),
        "close_mid": safe_float(inp.close_mid),
        "bid": safe_float(inp.bid),
        "ask": safe_float(inp.ask),
        "dte": safe_int(inp.dte),
        "multiplier": safe_float(inp.multiplier),
        "capture_ratio": capture_ratio,
        "remaining_premium": remaining_premium,
        "estimated_pnl_if_close_gross": realized_if_close,
        "estimated_close_fee": None,
        "estimated_pnl_if_close_net": None,
        "realized_if_close": realized_if_close,
        "remaining_annualized_return": remaining_annualized_return,
        "spread_ratio": spread_ratio,
        "tier": tier,
        "tier_label": TIER_LABELS.get(tier, tier),
        "reason": reason,
        "exit_state": resolved_exit_state,
        "exit_reason_type": resolved_exit_reason_type,
        "data_quality_flags": ";".join(flags),
        "currency": (str(inp.currency or "").strip().upper() or None),
        "spot": safe_float(inp.spot),
    }


def _resolve_exit_contract(
    *,
    tier: str,
    exit_state: str | None,
    exit_reason_type: str | None,
) -> tuple[str, str]:
    if exit_state:
        return exit_state, exit_reason_type or exit_state
    tier_norm = str(tier or "").strip().lower()
    if tier_norm == "not_evaluable":
        return EXIT_STATE_NOT_EVALUABLE, exit_reason_type or EXIT_REASON_TYPE_NOT_EVALUABLE
    if tier_norm and tier_norm != "none":
        return EXIT_STATE_PROFIT_CAPTURE, exit_reason_type or EXIT_REASON_TYPE_PROFIT_CAPTURE
    return EXIT_STATE_HOLD, exit_reason_type or EXIT_REASON_TYPE_HOLD


def current_policy_decision_fields(
    *,
    tier: Any,
    exit_state: Any,
    policy_version: str = CURRENT_CLOSE_POLICY_VERSION,
) -> dict[str, Any]:
    """Project the existing P0 exit contract into the additive recommendation contract."""

    tier_norm = str(tier or "").strip().lower()
    exit_norm = str(exit_state or "").strip().lower()
    if exit_norm == EXIT_STATE_NOT_EVALUABLE or tier_norm == "not_evaluable":
        recommendation = RECOMMENDATION_NOT_EVALUABLE
        basis = ("evidence_not_evaluable",)
        evidence_status = DECISION_EVIDENCE_NOT_EVALUABLE
    elif exit_norm in {
        EXIT_STATE_PROFIT_CAPTURE,
        EXIT_STATE_TAKE_PROFIT,
        EXIT_STATE_SALVAGE,
    } or (not exit_norm and tier_norm not in {"", "none"}):
        recommendation = RECOMMENDATION_CLOSE
        basis = (_current_policy_close_basis(exit_state=exit_norm, tier=tier_norm),)
        evidence_status = DECISION_EVIDENCE_COMPLETE
    else:
        recommendation = RECOMMENDATION_HOLD
        basis = (_current_policy_hold_basis(exit_state=exit_norm, tier=tier_norm),)
        evidence_status = DECISION_EVIDENCE_COMPLETE
    return {
        "policy_version": str(policy_version or CURRENT_CLOSE_POLICY_VERSION),
        "recommendation_state": recommendation,
        "decision_basis": basis,
        "decision_evidence_status": evidence_status,
    }


def evaluate_close_policy(facts: CloseDecisionFacts, variant: str) -> ClosePolicyResult:
    """Evaluate a named P0/P1/P2 policy from immutable decision facts.

    P3 is intentionally excluded: it composes post-run reallocation evidence in
    the Shadow Replay application layer.
    """

    policy = _normalize_close_policy_variant(variant)
    tier = _policy_text(facts.tier)
    exit_state = _policy_text(facts.exit_state)
    evaluation = _policy_text(facts.evaluation_status)
    if (
        evaluation != "priced"
        or tier == "not_evaluable"
        or exit_state == EXIT_STATE_NOT_EVALUABLE
        or _policy_text(facts.close_calibration_status) == "not_evaluable"
    ):
        return _close_policy_result(
            policy,
            RECOMMENDATION_NOT_EVALUABLE,
            "execution_evidence_not_evaluable",
            evidence_status=DECISION_EVIDENCE_NOT_EVALUABLE,
        )

    side = _policy_text(facts.side)
    option_type = _policy_text(facts.option_type)
    if side == "long" and option_type == "call":
        projected = current_policy_decision_fields(
            tier=tier,
            exit_state=exit_state,
            policy_version=policy,
        )
        result = _close_policy_result(
            policy,
            str(projected["recommendation_state"]),
            *projected["decision_basis"],
            evidence_status=str(projected["decision_evidence_status"]),
        )
        return _apply_close_execution_gate(facts, result, long_call=True)
    if side != "short" or option_type not in {"put", "call"}:
        return _close_policy_result(
            policy,
            RECOMMENDATION_NOT_EVALUABLE,
            "unsupported_position",
            evidence_status=DECISION_EVIDENCE_NOT_EVALUABLE,
        )
    family = _policy_text(facts.strategy_family)
    if policy == POLICY_VARIANT_P2_PROFILE_AWARE and (
        (option_type == "put" and family not in {"sell_put", "combo_yield"})
        or (option_type == "call" and family not in {"sell_call", "covered_call"})
    ):
        return _close_policy_result(
            policy,
            RECOMMENDATION_NOT_EVALUABLE,
            "strategy_family_mismatch",
            evidence_status=DECISION_EVIDENCE_NOT_EVALUABLE,
        )

    signal = _profit_capture_signal(tier=tier, exit_state=exit_state)
    if signal in {"strong", "medium"}:
        execution_gate = _profit_capture_execution_gate(facts, policy=policy, signal=signal)
        if execution_gate is not None:
            return execution_gate

    if policy == POLICY_VARIANT_P0_CURRENT:
        projected = current_policy_decision_fields(
            tier=tier,
            exit_state=exit_state,
            policy_version=policy,
        )
        result = _close_policy_result(
            policy,
            str(projected["recommendation_state"]),
            *projected["decision_basis"],
            evidence_status=str(projected["decision_evidence_status"]),
        )
    elif policy == POLICY_VARIANT_P1_SEMANTIC_SPLIT:
        result = _semantic_split_policy_result(policy=policy, signal=signal, tier=tier)
    else:
        result = _profile_aware_policy_result(facts, policy=policy, signal=signal, tier=tier)

    result = _apply_close_execution_gate(facts, result, long_call=False)
    return _apply_combo_evidence_gate(facts, result)


def _normalize_close_policy_variant(value: Any) -> str:
    token = str(value or "").strip()
    aliases = {item.lower(): item for item in DOMAIN_CLOSE_POLICY_VARIANTS}
    normalized = aliases.get(token.lower())
    if normalized is None:
        supported = ", ".join(sorted(DOMAIN_CLOSE_POLICY_VARIANTS))
        raise ValueError(f"unsupported domain close policy variant: {token or '<empty>'}; expected {supported}")
    return normalized


def _policy_text(value: Any) -> str:
    return str(value or "").strip().lower()


def _close_policy_result(
    policy: str,
    recommendation: str,
    *basis: str,
    evidence_status: str = DECISION_EVIDENCE_COMPLETE,
) -> ClosePolicyResult:
    ordered_basis = tuple(
        dict.fromkeys(token for token in (_policy_text(item) for item in basis) if token)
    )
    return ClosePolicyResult(
        policy_version=policy,
        recommendation_state=recommendation,
        decision_basis=ordered_basis or ("unspecified",),
        decision_evidence_status=evidence_status,
    )


def _profit_capture_signal(*, tier: str, exit_state: str) -> str:
    if exit_state == EXIT_STATE_PROFIT_CAPTURE or not exit_state:
        return tier if tier in {"strong", "medium", "weak", "optional"} else "none"
    return "none"


def _profit_capture_execution_gate(
    facts: CloseDecisionFacts,
    *,
    policy: str,
    signal: str,
) -> ClosePolicyResult | None:
    if _policy_text(facts.fee_calc_status) not in FEE_USABLE_STATUSES:
        return _close_policy_result(
            policy,
            RECOMMENDATION_NOT_EVALUABLE,
            f"profit_capture_{signal}",
            "fee_evidence_unusable",
            evidence_status=DECISION_EVIDENCE_NOT_EVALUABLE,
        )
    net_pnl = safe_float(facts.estimated_pnl_if_close_net)
    if net_pnl is None:
        return _close_policy_result(
            policy,
            RECOMMENDATION_NOT_EVALUABLE,
            f"profit_capture_{signal}",
            "net_close_pnl_missing",
            evidence_status=DECISION_EVIDENCE_NOT_EVALUABLE,
        )
    if net_pnl <= 0:
        return _close_policy_result(
            policy,
            RECOMMENDATION_HOLD,
            f"profit_capture_{signal}",
            "net_close_pnl_non_positive",
        )
    return None


def _semantic_split_policy_result(*, policy: str, signal: str, tier: str) -> ClosePolicyResult:
    if signal == "strong":
        return _close_policy_result(policy, RECOMMENDATION_CLOSE, "profit_capture_strong")
    if signal == "medium":
        return _close_policy_result(policy, RECOMMENDATION_REVIEW, "profit_capture_medium")
    return _close_policy_result(policy, RECOMMENDATION_HOLD, _current_policy_hold_basis(exit_state="", tier=tier))


def _profile_aware_policy_result(
    facts: CloseDecisionFacts,
    *,
    policy: str,
    signal: str,
    tier: str,
) -> ClosePolicyResult:
    profile = _policy_text(facts.strategy_profile)
    if profile == RETURN_FIRST_POLICY_PROFILE:
        return _semantic_split_policy_result(policy=policy, signal=signal, tier=tier)
    if profile not in UNDERWRITING_POLICY_PROFILES:
        return _close_policy_result(
            policy,
            RECOMMENDATION_NOT_EVALUABLE,
            "strategy_profile_unsupported",
            evidence_status=DECISION_EVIDENCE_NOT_EVALUABLE,
        )
    return _underwriting_policy_result(facts, policy=policy, signal=signal, tier=tier)


def _underwriting_policy_result(
    facts: CloseDecisionFacts,
    *,
    policy: str,
    signal: str,
    tier: str,
) -> ClosePolicyResult:
    thesis = _policy_text(facts.thesis_status)
    willingness = facts.continued_willingness
    thesis_incomplete = thesis not in {"valid", "observe"}
    willingness_incomplete = willingness is None
    basis = [f"profit_capture_{signal}" if signal != "none" else _current_policy_hold_basis(exit_state="", tier=tier)]

    if willingness is False:
        return _close_policy_result(
            policy,
            RECOMMENDATION_REVIEW,
            *basis,
            "continued_willingness_revoked",
            evidence_status=DECISION_EVIDENCE_REVIEW_REQUIRED,
        )
    if signal == "strong":
        if thesis != "valid" or willingness_incomplete:
            if thesis == "observe":
                reason = "underwriting_thesis_observe"
                evidence_status = DECISION_EVIDENCE_REVIEW_REQUIRED
            else:
                reason = "thesis_evidence_incomplete" if thesis_incomplete else "continued_willingness_missing"
                evidence_status = DECISION_EVIDENCE_PARTIAL
            return _close_policy_result(
                policy,
                RECOMMENDATION_REVIEW,
                *basis,
                reason,
                evidence_status=evidence_status,
            )
        return _close_policy_result(
            policy,
            RECOMMENDATION_CLOSE,
            *basis,
            "underwriting_evidence_complete",
        )
    if signal == "medium":
        if thesis == "valid" and willingness is True:
            return _close_policy_result(
                policy,
                RECOMMENDATION_HOLD,
                *basis,
                "underwriting_thesis_valid",
                "continued_willingness_accepted",
            )
        status = DECISION_EVIDENCE_PARTIAL if thesis_incomplete or willingness_incomplete else DECISION_EVIDENCE_REVIEW_REQUIRED
        reason = (
            "thesis_evidence_incomplete"
            if thesis_incomplete
            else "continued_willingness_missing"
            if willingness_incomplete
            else "underwriting_thesis_observe"
        )
        return _close_policy_result(
            policy,
            RECOMMENDATION_REVIEW,
            *basis,
            reason,
            evidence_status=status,
        )
    if thesis == "observe":
        return _close_policy_result(
            policy,
            RECOMMENDATION_REVIEW,
            *basis,
            "underwriting_thesis_observe",
            evidence_status=DECISION_EVIDENCE_REVIEW_REQUIRED,
        )
    if willingness_incomplete and thesis == "valid":
        return _close_policy_result(
            policy,
            RECOMMENDATION_REVIEW,
            *basis,
            "continued_willingness_missing",
            evidence_status=DECISION_EVIDENCE_PARTIAL,
        )
    return _close_policy_result(
        policy,
        RECOMMENDATION_HOLD,
        *basis,
        "underwriting_thesis_valid" if thesis == "valid" else "thesis_evidence_incomplete",
        evidence_status=(DECISION_EVIDENCE_COMPLETE if thesis == "valid" else DECISION_EVIDENCE_PARTIAL),
    )


def _apply_close_execution_gate(
    facts: CloseDecisionFacts,
    result: ClosePolicyResult,
    *,
    long_call: bool,
) -> ClosePolicyResult:
    if result.recommendation_state != RECOMMENDATION_CLOSE:
        return result
    if _policy_text(facts.fee_calc_status) not in FEE_USABLE_STATUSES:
        return _close_policy_result(
            result.policy_version,
            RECOMMENDATION_NOT_EVALUABLE,
            *result.decision_basis,
            "fee_evidence_unusable",
            evidence_status=DECISION_EVIDENCE_NOT_EVALUABLE,
        )
    if long_call and _policy_text(facts.exit_state) == EXIT_STATE_SALVAGE:
        return result
    net_pnl = safe_float(facts.estimated_pnl_if_close_net)
    if net_pnl is None:
        return _close_policy_result(
            result.policy_version,
            RECOMMENDATION_NOT_EVALUABLE,
            *result.decision_basis,
            "net_close_pnl_missing",
            evidence_status=DECISION_EVIDENCE_NOT_EVALUABLE,
        )
    if net_pnl <= 0:
        return _close_policy_result(
            result.policy_version,
            RECOMMENDATION_HOLD,
            *result.decision_basis,
            "net_close_pnl_non_positive",
        )
    return result


def _apply_combo_evidence_gate(facts: CloseDecisionFacts, result: ClosePolicyResult) -> ClosePolicyResult:
    combo_status = _policy_text(facts.combo_evidence_status) or "not_applicable"
    if result.recommendation_state != RECOMMENDATION_CLOSE or combo_status == "not_applicable":
        return result
    if combo_status == "complete":
        return result
    return _close_policy_result(
        result.policy_version,
        RECOMMENDATION_REVIEW,
        *result.decision_basis,
        "combo_evidence_incomplete",
        evidence_status=DECISION_EVIDENCE_REVIEW_REQUIRED,
    )


def _current_policy_close_basis(*, exit_state: str, tier: str) -> str:
    if exit_state == EXIT_STATE_TAKE_PROFIT:
        return "long_call_take_profit"
    if exit_state == EXIT_STATE_SALVAGE:
        return "long_call_salvage"
    if exit_state == EXIT_STATE_PROFIT_CAPTURE or not exit_state:
        suffix = tier if tier in {"strong", "medium", "weak", "optional"} else "matched"
        return f"profit_capture_{suffix}"
    return "legacy_close"


def _current_policy_hold_basis(*, exit_state: str, tier: str) -> str:
    if exit_state == EXIT_STATE_LET_EXPIRE:
        return "long_call_let_expire"
    if exit_state == EXIT_STATE_HOLD or not exit_state:
        return f"hold_{tier}" if tier and tier != "none" else "hold_threshold_not_met"
    return "legacy_non_authoritative_exit_state"


def apply_fee_economic_safety(row: dict[str, Any]) -> dict[str, Any]:
    """Fail closed only when an existing close action needs unusable economics."""

    out = dict(row)
    exit_state = str(out.get("exit_state") or "").strip().lower()
    if exit_state not in {
        EXIT_STATE_PROFIT_CAPTURE,
        EXIT_STATE_TAKE_PROFIT,
        EXIT_STATE_SALVAGE,
    }:
        return out

    flags = [item for item in str(out.get("data_quality_flags") or "").split(";") if item]
    fee_status = str(out.get("fee_calc_status") or "").strip().lower()
    if fee_status not in FEE_USABLE_STATUSES:
        flags.append("fee_evidence_unavailable")
        out.update(
            {
                "tier": "not_evaluable",
                "tier_label": TIER_LABELS["not_evaluable"],
                "reason": "平仓手续费证据不可用，当前无法安全给出平仓建议",
                "exit_state": EXIT_STATE_NOT_EVALUABLE,
                "exit_reason_type": EXIT_REASON_TYPE_NOT_EVALUABLE,
                "evaluation_status": "not_evaluable",
                "data_quality_flags": ";".join(dict.fromkeys(flags)),
            }
        )
        return out

    if exit_state == EXIT_STATE_SALVAGE:
        economic_value = safe_float(out.get("net_close_proceeds"))
        failure_flag = "non_positive_net_close_proceeds"
        failure_reason = "扣除卖出平仓手续费后已无可回收净残值，继续持有观察"
    else:
        economic_value = safe_float(out.get("estimated_pnl_if_close_net"))
        failure_flag = "not_profitable_after_fee"
        failure_reason = "扣除平仓手续费后已无正收益，不建议作为收益型平仓提醒"

    if economic_value is None:
        flags.append("fee_economics_unavailable")
        out.update(
            {
                "tier": "not_evaluable",
                "tier_label": TIER_LABELS["not_evaluable"],
                "reason": "平仓手续费已估算，但净经济结果不可用，当前无法安全给出平仓建议",
                "exit_state": EXIT_STATE_NOT_EVALUABLE,
                "exit_reason_type": EXIT_REASON_TYPE_NOT_EVALUABLE,
                "evaluation_status": "not_evaluable",
                "data_quality_flags": ";".join(dict.fromkeys(flags)),
            }
        )
        return out

    if economic_value > 0:
        return out

    flags.append(failure_flag)
    out.update(
        {
            "tier": "none",
            "tier_label": TIER_LABELS["none"],
            "reason": failure_reason,
            "exit_state": EXIT_STATE_HOLD,
            "exit_reason_type": EXIT_REASON_TYPE_HOLD,
            "data_quality_flags": ";".join(dict.fromkeys(flags)),
        }
    )
    return out



def synthesize_combo_yield_group_close_advice(
    *,
    inventory_classification: Any,
    inventory_issues: list[str] | tuple[str, ...] | None,
    put_actions: list[str] | tuple[str, ...] | None,
    call_actions: list[str] | tuple[str, ...] | None,
    evidence_issues: list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Synthesize additive Combo Yield group advice after leg advice.

    The leg evaluators remain authoritative.  This helper only classifies the
    option inventory and translates already-evaluated leg actions into a
    quantity/quote-gated group view.
    """

    classification = str(inventory_classification or "").strip().lower()
    issues = sorted(
        {
            str(item or "").strip()
            for item in [*(inventory_issues or []), *(evidence_issues or [])]
            if str(item or "").strip()
        }
    )
    normalized_put_actions = sorted(
        {str(item or "").strip().lower() for item in (put_actions or []) if str(item or "").strip()}
    )
    normalized_call_actions = sorted(
        {str(item or "").strip().lower() for item in (call_actions or []) if str(item or "").strip()}
    )

    def review(*extra: str) -> dict[str, Any]:
        final_issues = sorted({*issues, *(item for item in extra if item)})
        return {
            "combo_group_classification": "review_required",
            "combo_group_status": "review_required",
            "combo_group_action": None,
            "combo_group_reason": "组合证据不完整或互相冲突，保留逐腿建议并要求人工复核",
            "combo_group_issues": final_issues,
        }

    if issues:
        return review()

    if classification == "active_combo":
        if len(normalized_put_actions) != 1:
            return review("mixed_put_leg_actions" if normalized_put_actions else "missing_put_leg_advice")
        if len(normalized_call_actions) != 1:
            return review("mixed_call_leg_actions" if normalized_call_actions else "missing_call_leg_advice")
        put_action = normalized_put_actions[0]
        call_action = normalized_call_actions[0]
        call_sell_actions = {"sell_call_take_profit", "sell_call_salvage"}
        call_hold_actions = {
            "hold_call",
            "hold_call_as_convexity",
            "hold_to_expiry_or_expire",
        }
        if call_action not in call_sell_actions | call_hold_actions:
            return review(
                "call_leg_advice_not_evaluable"
                if call_action == "not_evaluable"
                else "unsupported_call_leg_action"
            )
        if put_action == "close_put_keep_call" and call_action in call_sell_actions:
            action = "close_both"
            reason = "Put 腿满足平仓条件，Call 腿也建议卖出；组合动作与两腿建议一致"
        elif put_action == "close_put_keep_call":
            action = "close_put_keep_call"
            reason = "Put 腿满足平仓条件；Call 腿继续按其独立持有建议管理"
        elif put_action == "hold_put_keep_call" and call_action in call_sell_actions:
            action = "sell_call_keep_put"
            reason = "Put 腿继续持有，Call 腿建议卖出；组合动作与两腿建议一致"
        elif put_action == "hold_put_keep_call":
            action = "hold_active_combo"
            reason = "两腿当前均未给出卖出组合的建议"
        else:
            return review("put_leg_advice_not_evaluable" if put_action == "not_evaluable" else "unsupported_put_leg_action")
        return {
            "combo_group_classification": classification,
            "combo_group_status": "evaluable",
            "combo_group_action": action,
            "combo_group_reason": reason,
            "combo_group_issues": [],
        }

    if classification == "missing_call":
        if len(normalized_put_actions) != 1:
            return review("mixed_put_leg_actions" if normalized_put_actions else "missing_put_leg_advice")
        put_action = normalized_put_actions[0]
        if put_action == "close_put_keep_call":
            action = "close_put_unpaired"
            reason = "Call 腿缺失；仅执行 Put 腿平仓判断，不生成保留 Call 的组合建议"
        elif put_action == "hold_put_keep_call":
            action = "hold_put_unpaired"
            reason = "Call 腿缺失；仅保留 Put 腿建议并标记组合不完整"
        else:
            return review("put_leg_advice_not_evaluable" if put_action == "not_evaluable" else "unsupported_put_leg_action")
        return {
            "combo_group_classification": classification,
            "combo_group_status": "incomplete",
            "combo_group_action": action,
            "combo_group_reason": reason,
            "combo_group_issues": ["missing_call_leg"],
        }

    if classification == "residual_call":
        if len(normalized_call_actions) != 1:
            return review("mixed_call_leg_actions" if normalized_call_actions else "missing_call_leg_advice")
        call_action = normalized_call_actions[0]
        action_map = {
            "sell_call_take_profit": "sell_residual_call_take_profit",
            "sell_call_salvage": "sell_residual_call_salvage",
            "hold_to_expiry_or_expire": "hold_residual_call_to_expiry_or_expire",
            "hold_call_as_convexity": "hold_residual_call_as_convexity",
            "hold_call": "hold_residual_call",
        }
        action = action_map.get(call_action)
        if action is None:
            return review("call_leg_advice_not_evaluable" if call_action == "not_evaluable" else "unsupported_call_leg_action")
        return {
            "combo_group_classification": classification,
            "combo_group_status": "evaluable",
            "combo_group_action": action,
            "combo_group_reason": "Put 腿已不再持仓；剩余 Call 仅按当前真实行情和长期 Call 逐腿建议管理",
            "combo_group_issues": [],
        }

    if classification == "closed":
        return {
            "combo_group_classification": classification,
            "combo_group_status": "closed",
            "combo_group_action": None,
            "combo_group_reason": "组合没有未平期权腿",
            "combo_group_issues": [],
        }

    return review("unsupported_inventory_classification")

def sort_advice_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按平仓建议 tier、捕获比例和剩余权利金排序。"""
    return sorted(
        rows or [],
        key=lambda r: (
            TIER_PRIORITY.get(str(r.get("tier") or "none"), 9),
            -(safe_float(r.get("capture_ratio")) or 0.0),
            -(safe_float(r.get("remaining_premium")) or 0.0),
            str(r.get("symbol") or ""),
        ),
    )
