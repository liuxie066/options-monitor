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
EXIT_STATE_RISK_EXIT = "risk_exit"
EXIT_STATE_TAKE_PROFIT = "take_profit"
EXIT_STATE_SALVAGE = "salvage"
EXIT_STATE_LET_EXPIRE = "let_expire"

EXIT_REASON_TYPE_NOT_EVALUABLE = "not_evaluable"
EXIT_REASON_TYPE_HOLD = "hold"
EXIT_REASON_TYPE_PROFIT_CAPTURE = "profit_capture"
EXIT_REASON_TYPE_RISK_EXIT = "risk_exit"
EXIT_REASON_TYPE_TAKE_PROFIT = "take_profit"
EXIT_REASON_TYPE_SALVAGE = "salvage"
EXIT_REASON_TYPE_THESIS_EXPIRED = "thesis_expired"

HOLD_REASON_TYPE_ASSIGNMENT_ACCEPTABLE = "assignment_acceptable"
HOLD_REASON_TYPE_CALLED_AWAY_ACCEPTABLE = "called_away_acceptable"

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
    otm_pct: float | None = None


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

    The existing close advice model is a return-capture model.  This wrapper keeps
    the same quote-quality and profitability gates, then overlays short-vol
    observations about the original underwriting thesis. Soft changes in IV/RV,
    delta, or event context do not create a close recommendation by themselves.
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
        return _short_vol_not_evaluable(
            row,
            reason=f"缺少 short-vol 平仓评估数据: {', '.join(missing)}",
            flag="short_vol_risk_data_missing",
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


def _short_vol_not_evaluable(row: dict[str, Any], *, reason: str, flag: str) -> dict[str, Any]:
    out = dict(row)
    out["tier"] = "not_evaluable"
    out["tier_label"] = TIER_LABELS["not_evaluable"]
    out["reason"] = reason
    out["short_vol_thesis_status"] = "not_evaluable"
    out["exit_state"] = EXIT_STATE_NOT_EVALUABLE
    out["exit_reason_type"] = EXIT_REASON_TYPE_NOT_EVALUABLE
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


CLOSE_OPTIMIZER_DEFAULTS = {
    "min_capture_for_optimizer": 0.70,
    "min_tail_risk_for_action": 0.03,
    "min_switch_value_ratio": 0.30,
    "max_effective_annualized_to_close": 0.06,
    "min_risk_adjusted_to_hold": 0.20,
}

OPTIMIZER_TIER_LABELS = {
    "optimizer_switch": "强烈建议平仓换仓",
    "optimizer_close": "建议平仓",
    "optimizer_hold": "建议继续持有",
}

OPTIMIZER_SWITCH_REASON = (
    "尾部风险暴露高且替代候选年化收益显著更高，平仓换仓更优"
)
OPTIMIZER_CLOSE_REASON = (
    "尾部风险暴露高且持有年化收益过低，即使无替代候选也建议平仓"
)
OPTIMIZER_HOLD_DEEP_OTM_REASON = "深度虚值(delta<0.05)，尾部风险极低，继续持有性价比高"
OPTIMIZER_HOLD_RISK_ADJUSTED_REASON = "风险调整后收益仍合理，继续持有"
OPTIMIZER_DEFER_LOW_CAPTURE_REASON = "未达到 optimizer 最低捕获阈值"
OPTIMIZER_DEFER_NO_DELTA_REASON = "缺少 delta 数据，无法运行 optimizer"

OPTIMIZER_TIER_PRIORITY = {
    "optimizer_switch": -2,
    "optimizer_close": -1,
    "strong": 0,
    "medium": 1,
    "optimizer_hold": 4,
    "optional": 2,
    "weak": 3,
    "none": 9,
}


@dataclass(frozen=True)
class CloseOptimizerConfig:
    min_capture_for_optimizer: float = CLOSE_OPTIMIZER_DEFAULTS["min_capture_for_optimizer"]
    min_tail_risk_for_action: float = CLOSE_OPTIMIZER_DEFAULTS["min_tail_risk_for_action"]
    min_switch_value_ratio: float = CLOSE_OPTIMIZER_DEFAULTS["min_switch_value_ratio"]
    max_effective_annualized_to_close: float = CLOSE_OPTIMIZER_DEFAULTS["max_effective_annualized_to_close"]
    min_risk_adjusted_to_hold: float = CLOSE_OPTIMIZER_DEFAULTS["min_risk_adjusted_to_hold"]

    @classmethod
    def from_mapping(cls, raw: dict[str, Any] | None) -> "CloseOptimizerConfig":
        src = raw or {}
        return cls(
            min_capture_for_optimizer=(
                safe_float(src.get("min_capture_for_optimizer"))
                or CLOSE_OPTIMIZER_DEFAULTS["min_capture_for_optimizer"]
            ),
            min_tail_risk_for_action=(
                safe_float(src.get("min_tail_risk_for_action"))
                or CLOSE_OPTIMIZER_DEFAULTS["min_tail_risk_for_action"]
            ),
            min_switch_value_ratio=(
                safe_float(src.get("min_switch_value_ratio"))
                or CLOSE_OPTIMIZER_DEFAULTS["min_switch_value_ratio"]
            ),
            max_effective_annualized_to_close=(
                safe_float(src.get("max_effective_annualized_to_close"))
                or CLOSE_OPTIMIZER_DEFAULTS["max_effective_annualized_to_close"]
            ),
            min_risk_adjusted_to_hold=(
                safe_float(src.get("min_risk_adjusted_to_hold"))
                or CLOSE_OPTIMIZER_DEFAULTS["min_risk_adjusted_to_hold"]
            ),
        )


def calc_tail_risk_score(
    capture_ratio: float,
    delta: float,
    dte: int,
) -> float:
    """尾部风险评分: (剩余价值×delta×时间窗口)。0~1，越高风险越大。

    (1-capture_ratio): 还有多少 value-at-risk
    |delta|:          价格变动敏感度
    sqrt(dte/365):    时间窗口归一化
    """
    if capture_ratio >= 1.0 or dte <= 0:
        return 0.0
    raw = (1.0 - capture_ratio) * abs(delta) * math.sqrt(max(dte, 1) / 365.0)
    return max(0.0, min(1.0, raw))


def calc_effective_annualized_return(
    remaining_premium: float,
    capital_tied_up: float,
    dte: int,
) -> float | None:
    """修正后的持有年化: 剩余权利金 / 实际资金占用 × 年化。

    与现有 _remaining_annualized_return 的差异:
    分母用 capital_tied_up (strike×multiplier×contracts) 替代 strike。
    """
    if capital_tied_up <= 0 or dte <= 0:
        return None
    return (remaining_premium / capital_tied_up) * (365.0 / float(dte))


def calc_risk_adjusted_return(
    effective_annualized: float | None,
    delta: float,
) -> float | None:
    """风险调整后收益: 高 delta 被惩罚。

    delta=0.05 深度 OTM, 调整因子=20:
      5% 年化 → 5%/0.05=100%  (很划算)
    delta=0.50 平值附近, 调整因子=2:
      5% 年化 → 5%/0.50=10%   (不太划算)
    """
    if effective_annualized is None:
        return None
    return effective_annualized / max(abs(delta), 0.02)


def calc_switch_value_ratio(
    alternative_annualized: float | None,
    effective_annualized: float | None,
) -> float | None:
    """换仓价值比: (替代年化 - 持有年化) / 持有年化。"""
    if effective_annualized is None or effective_annualized <= 0:
        return None
    if alternative_annualized is None:
        return None
    return (alternative_annualized - effective_annualized) / effective_annualized


def decide_optimizer_tier(
    *,
    capture_ratio: float,
    delta: float,
    tail_risk_score: float,
    effective_annualized: float | None,
    risk_adjusted: float | None,
    switch_value: float | None,
    config: CloseOptimizerConfig,
) -> tuple[str, str]:
    """返回 (optimizer_tier, reason)。

    决策顺序:
    1. capture 太低 → defer
    2. 深度虚值或风险调整后收益合理 → hold
    3. 有更好替代候选 + 尾部风险够高 → switch
    4. 尾部风险够高 + 持有年化过低 → close
    5. 默认 defer 回退至现有 close_advice 规则
    """
    if capture_ratio < config.min_capture_for_optimizer:
        return ("defer", OPTIMIZER_DEFER_LOW_CAPTURE_REASON)

    if abs(delta) < 0.05:
        return ("optimizer_hold", OPTIMIZER_HOLD_DEEP_OTM_REASON)

    if risk_adjusted is not None and risk_adjusted > config.min_risk_adjusted_to_hold:
        return ("optimizer_hold", OPTIMIZER_HOLD_RISK_ADJUSTED_REASON)

    if (
        tail_risk_score >= config.min_tail_risk_for_action
        and switch_value is not None
        and switch_value > config.min_switch_value_ratio
    ):
        return ("optimizer_switch", OPTIMIZER_SWITCH_REASON)

    close_threshold = config.min_tail_risk_for_action * 1.5
    if (
        tail_risk_score >= close_threshold
        and effective_annualized is not None
        and effective_annualized < config.max_effective_annualized_to_close
    ):
        return ("optimizer_close", OPTIMIZER_CLOSE_REASON)

    return ("defer", "optimizer 评估后回退到现有平仓规则")


def evaluate_close_optimizer(
    inp: CloseAdviceInput,
    optimizer_cfg: CloseOptimizerConfig,
    *,
    alternative_annualized_return: float | None = None,
) -> dict[str, Any]:
    """对已通过 close_advice 评估的持仓运行 optimizer 层。

    前置条件(由调用方保证):
    - inp.side == "short"
    - inp.close_mid < inp.premium (平仓盈利)
    - 必要数据完整 (premium, mid, dte, multiplier, contracts_open)

    返回 dict:
      optimizer_tier               "optimizer_switch"|"optimizer_close"|"optimizer_hold"|"defer"
      optimizer_reason             中文理由
      effective_annualized_return  修正后持有年化
      tail_risk_score              尾部风险评分
      risk_adjusted_return         风险调整后收益
      switch_value_ratio           换仓价值比
      alternative_annualized_return  替代候选年化(透传)
      delta                        持仓 delta
      otm_pct                      虚值幅度
    """
    side = str(inp.side or "").strip().lower()
    if side != "short":
        return {"optimizer_tier": "defer", "optimizer_reason": "仅支持 short 期权"}

    mid = safe_float(inp.close_mid)
    premium = safe_float(inp.premium)
    dte = safe_int(inp.dte)
    multiplier = safe_float(inp.multiplier) or 100.0
    contracts = safe_int(inp.contracts_open) or 1
    strike = safe_float(inp.strike)
    delta = safe_float(inp.delta)

    if mid is None or premium is None or dte is None:
        return {"optimizer_tier": "defer", "optimizer_reason": "缺少必要定价数据"}

    if delta is None:
        return {"optimizer_tier": "defer", "optimizer_reason": OPTIMIZER_DEFER_NO_DELTA_REASON}

    capture_ratio = (premium - mid) / premium

    if capture_ratio <= 0:
        return {"optimizer_tier": "defer", "optimizer_reason": "当前平仓不盈利"}

    remaining_premium = mid * multiplier * contracts

    capital_tied_up = (strike or 0.0) * multiplier * contracts
    if capital_tied_up <= 0:
        capital_tied_up = remaining_premium / max(capture_ratio, 0.01)

    effective_annualized = calc_effective_annualized_return(
        remaining_premium, capital_tied_up, dte
    )
    tail_risk = calc_tail_risk_score(capture_ratio, delta or 0.0, dte)
    risk_adjusted = calc_risk_adjusted_return(effective_annualized, delta or 0.0)
    switch_value = calc_switch_value_ratio(
        alternative_annualized_return, effective_annualized
    )

    tier, reason = decide_optimizer_tier(
        capture_ratio=capture_ratio,
        delta=delta or 0.0,
        tail_risk_score=tail_risk,
        effective_annualized=effective_annualized,
        risk_adjusted=risk_adjusted,
        switch_value=switch_value,
        config=optimizer_cfg,
    )

    return {
        "optimizer_tier": tier,
        "optimizer_reason": reason,
        "effective_annualized_return": effective_annualized,
        "tail_risk_score": tail_risk,
        "risk_adjusted_return": risk_adjusted,
        "switch_value_ratio": switch_value,
        "alternative_annualized_return": alternative_annualized_return,
        "delta": delta,
        "otm_pct": safe_float(inp.otm_pct),
    }


def sort_advice_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按优先级排序，optimizer tier 优先于现有 tier。"""
    return sorted(
        rows or [],
        key=lambda r: (
            OPTIMIZER_TIER_PRIORITY.get(str(r.get("tier") or "none"), 9),
            -(safe_float(r.get("capture_ratio")) or 0.0),
            -(safe_float(r.get("remaining_premium")) or 0.0),
            str(r.get("symbol") or ""),
        ),
    )
