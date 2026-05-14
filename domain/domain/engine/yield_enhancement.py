from __future__ import annotations

from dataclasses import dataclass
from math import sqrt
from typing import Any


@dataclass(frozen=True)
class YieldEnhancementLeg:
    symbol: str
    option_type: str
    expiration: str
    contract_symbol: str
    currency: str
    dte: int
    strike: float
    spot: float
    bid: float
    ask: float
    mid: float
    multiplier: float
    open_interest: float | None = None
    volume: float | None = None
    implied_volatility: float | None = None
    delta: float | None = None
    spread: float | None = None
    spread_ratio: float | None = None


@dataclass(frozen=True)
class YieldEnhancementMetrics:
    net_credit: float
    net_debit: float
    funding_ratio: float | None
    cash_required: float
    downside_breakeven: float
    upside_breakeven: float
    max_loss_if_zero: float
    put_otm_pct: float
    call_otm_pct: float
    gap_width_pct: float
    upside_breakeven_pct_above_spot: float
    combo_spread_ratio: float | None
    expected_move_iv: float | None = None
    expected_move: float | None = None
    scenario_score: float | None = None
    annualized_scenario_score: float | None = None


@dataclass(frozen=True)
class YieldEnhancementFundingDecision:
    accepted: bool
    reject_reasons: tuple[str, ...]
    put_net_credit: float
    call_total_cost: float
    combo_net_credit: float
    call_cost_ratio: float | None
    upside_scenario_price: float | None
    upside_lift: float | None
    upside_net_lift: float | None
    upside_lift_to_call_cost: float | None
    upside_lift_to_put_credit: float | None
    premium_funding_score: float
    combo_spread_ratio: float | None
    score_components: dict[str, float]


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    try:
        out = float(value)
    except Exception:
        return None
    try:
        if out != out:
            return None
    except Exception:
        return None
    return out


def _positive(value: Any) -> float | None:
    out = _safe_float(value)
    if out is None or out <= 0:
        return None
    return out


def _pct_distance(numerator: float, denominator: float) -> float:
    if denominator <= 0:
        return 0.0
    return numerator / denominator


def _normalize_weights(weights: tuple[float, ...], count: int) -> tuple[float, ...]:
    cleaned = tuple(max(float(value), 0.0) for value in weights[:count])
    if len(cleaned) < count:
        cleaned = cleaned + tuple(0.0 for _ in range(count - len(cleaned)))
    total = sum(cleaned)
    if total <= 0:
        return tuple(1.0 / float(count) for _ in range(count))
    return tuple(value / total for value in cleaned)


def validate_yield_enhancement_pair(put_leg: YieldEnhancementLeg, call_leg: YieldEnhancementLeg) -> list[str]:
    rejects: list[str] = []
    if str(put_leg.option_type).lower() != "put":
        rejects.append("put_leg_option_type")
    if str(call_leg.option_type).lower() != "call":
        rejects.append("call_leg_option_type")
    if put_leg.symbol.upper() != call_leg.symbol.upper():
        rejects.append("symbol_mismatch")
    if put_leg.expiration != call_leg.expiration:
        rejects.append("expiration_mismatch")
    if put_leg.currency.upper() != call_leg.currency.upper():
        rejects.append("currency_mismatch")
    if float(put_leg.multiplier) != float(call_leg.multiplier):
        rejects.append("multiplier_mismatch")
    if put_leg.strike >= call_leg.strike:
        rejects.append("strike_order")
    if put_leg.spot <= 0 or call_leg.spot <= 0:
        rejects.append("spot")
    if put_leg.dte <= 0 or call_leg.dte <= 0:
        rejects.append("dte")
    if put_leg.bid <= 0 or call_leg.ask <= 0:
        rejects.append("execution_price")
    return rejects


def compute_yield_enhancement_metrics(
    *,
    put_leg: YieldEnhancementLeg,
    call_leg: YieldEnhancementLeg,
    put_sell_fee: float,
    call_buy_fee: float,
    expected_move_iv: float | None = None,
    scenario_move_factors: tuple[float, ...] = (0.0, 0.5, 1.0, 1.5),
    scenario_weights: tuple[float, ...] = (0.2, 0.3, 0.4, 0.1),
    min_combo_notional_floor: float = 1.0,
) -> YieldEnhancementMetrics:
    rejects = validate_yield_enhancement_pair(put_leg, call_leg)
    if rejects:
        raise ValueError(f"invalid yield enhancement pair: {', '.join(rejects)}")

    multiplier = float(put_leg.multiplier)
    spot = float(put_leg.spot)
    dte = int(min(put_leg.dte, call_leg.dte))
    put_proceeds = float(put_leg.bid) * multiplier - float(put_sell_fee)
    call_cost = float(call_leg.ask) * multiplier + float(call_buy_fee)
    net_credit = put_proceeds - call_cost
    net_debit = max(-net_credit, 0.0)
    funding_ratio = (put_proceeds / call_cost) if call_cost > 0 else None
    iv = _positive(expected_move_iv)
    expected_move = spot * iv * sqrt(float(dte) / 365.0) if iv is not None and dte > 0 else None
    if expected_move is None:
        raise ValueError("expected_move_iv is required for yield enhancement metrics")

    cash_required = float(put_leg.strike) * multiplier - net_credit
    if cash_required <= 0:
        raise ValueError("cash_required must be > 0")

    downside_breakeven = float(put_leg.strike) - net_credit / multiplier
    upside_breakeven = float(call_leg.strike) + net_debit / multiplier
    max_loss_if_zero = float(put_leg.strike) * multiplier - net_credit
    scenario_score = None
    annualized_scenario_score = None
    if cash_required > 0:
        factors = tuple(float(value) for value in scenario_move_factors if value is not None)
        if factors:
            weights = _normalize_weights(scenario_weights, len(factors))
            weighted = 0.0
            for factor, weight in zip(factors, weights):
                scenario_price = spot + expected_move * float(factor)
                scenario_pnl = max(0.0, scenario_price - float(call_leg.strike)) * multiplier + net_credit
                scenario_return = scenario_pnl / cash_required
                weighted += scenario_return * weight
            scenario_score = weighted
            annualized_scenario_score = scenario_score * (365.0 / float(dte)) if dte > 0 else None

    put_otm_pct = _pct_distance(spot - float(put_leg.strike), spot)
    call_otm_pct = _pct_distance(float(call_leg.strike) - spot, spot)
    gap_width_pct = _pct_distance(float(call_leg.strike) - float(put_leg.strike), spot)
    upside_breakeven_pct_above_spot = _pct_distance(upside_breakeven - spot, spot)

    put_spread = _safe_float(put_leg.spread)
    call_spread = _safe_float(call_leg.spread)
    combo_spread_ratio = None
    if put_spread is not None and call_spread is not None:
        spread_notional = (put_spread + call_spread) * multiplier
        denominator = max(abs(net_credit), float(min_combo_notional_floor or 1.0))
        combo_spread_ratio = spread_notional / denominator if denominator > 0 else None

    return YieldEnhancementMetrics(
        net_credit=round(net_credit, 6),
        net_debit=round(net_debit, 6),
        funding_ratio=(round(funding_ratio, 6) if funding_ratio is not None else None),
        cash_required=round(cash_required, 6),
        downside_breakeven=round(downside_breakeven, 6),
        upside_breakeven=round(upside_breakeven, 6),
        max_loss_if_zero=round(max_loss_if_zero, 6),
        put_otm_pct=round(put_otm_pct, 6),
        call_otm_pct=round(call_otm_pct, 6),
        gap_width_pct=round(gap_width_pct, 6),
        upside_breakeven_pct_above_spot=round(upside_breakeven_pct_above_spot, 6),
        combo_spread_ratio=(round(combo_spread_ratio, 6) if combo_spread_ratio is not None else None),
        expected_move_iv=(round(iv, 6) if iv is not None else None),
        expected_move=(round(expected_move, 6) if expected_move is not None else None),
        scenario_score=(round(scenario_score, 6) if scenario_score is not None else None),
        annualized_scenario_score=(round(annualized_scenario_score, 6) if annualized_scenario_score is not None else None),
    )


def _round_optional(value: float | None) -> float | None:
    return round(float(value), 6) if value is not None else None


def compute_yield_enhancement_funding_decision(
    *,
    put_leg: YieldEnhancementLeg,
    call_leg: YieldEnhancementLeg,
    put_sell_fee: float,
    call_buy_fee: float,
    combo_metrics: YieldEnhancementMetrics,
    min_combo_net_credit: float | None = None,
    max_call_cost_to_put_credit: float | None = None,
    min_upside_lift_to_call_cost: float | None = None,
    min_upside_lift_to_put_credit: float | None = None,
    max_combo_spread_ratio: float | None = None,
) -> YieldEnhancementFundingDecision:
    """Decide whether the put premium can sensibly fund a speculative long call."""
    rejects = validate_yield_enhancement_pair(put_leg, call_leg)
    reject_reasons: list[str] = list(rejects)

    multiplier = float(put_leg.multiplier)
    spot = float(put_leg.spot)
    put_net_credit = float(put_leg.bid) * multiplier - float(put_sell_fee)
    call_total_cost = float(call_leg.ask) * multiplier + float(call_buy_fee)
    combo_net_credit = float(combo_metrics.net_credit)

    combo_spread_ratio = _safe_float(combo_metrics.combo_spread_ratio)

    call_cost_ratio = (call_total_cost / put_net_credit) if put_net_credit > 0 else None
    if put_net_credit <= 0:
        reject_reasons.append("put_net_credit")
    if call_total_cost <= 0:
        reject_reasons.append("call_total_cost")

    expected_move = _safe_float(combo_metrics.expected_move)
    upside_scenario_price = (spot + expected_move) if expected_move is not None else None
    upside_lift = None
    upside_net_lift = None
    if upside_scenario_price is not None:
        upside_lift = max(0.0, upside_scenario_price - float(call_leg.strike)) * multiplier
        upside_net_lift = upside_lift - call_total_cost
    else:
        reject_reasons.append("expected_move")

    upside_lift_to_call_cost = (upside_lift / call_total_cost) if upside_lift is not None and call_total_cost > 0 else None
    upside_lift_to_put_credit = (upside_lift / put_net_credit) if upside_lift is not None and put_net_credit > 0 else None

    min_credit = _safe_float(min_combo_net_credit)
    if min_credit is not None and combo_net_credit < min_credit:
        reject_reasons.append("combo_net_credit")

    max_cost_ratio = _safe_float(max_call_cost_to_put_credit)
    if max_cost_ratio is not None:
        if call_cost_ratio is None or call_cost_ratio > max_cost_ratio:
            reject_reasons.append("call_cost_to_put_credit")

    min_lift_to_cost = _safe_float(min_upside_lift_to_call_cost)
    if min_lift_to_cost is not None:
        if upside_lift_to_call_cost is None or upside_lift_to_call_cost < min_lift_to_cost:
            reject_reasons.append("upside_lift_to_call_cost")

    min_lift_to_credit = _safe_float(min_upside_lift_to_put_credit)
    if min_lift_to_credit is not None:
        if upside_lift_to_put_credit is None or upside_lift_to_put_credit < min_lift_to_credit:
            reject_reasons.append("upside_lift_to_put_credit")

    max_combo_spread = _safe_float(max_combo_spread_ratio)
    if max_combo_spread is not None:
        if combo_spread_ratio is None or combo_spread_ratio > max_combo_spread:
            reject_reasons.append("combo_spread_ratio")

    spread_penalty = max(combo_spread_ratio or 0.0, 0.0) * 0.10
    cost_penalty = max(call_cost_ratio or 0.0, 0.0)
    components = {
        "upside_lift_to_call_cost": float(upside_lift_to_call_cost or 0.0),
        "upside_lift_to_put_credit": float(upside_lift_to_put_credit or 0.0),
        "call_cost_penalty": -float(cost_penalty),
        "spread_penalty": -float(spread_penalty),
    }
    premium_funding_score = sum(components.values())
    unique_rejects = tuple(dict.fromkeys(reject_reasons))

    return YieldEnhancementFundingDecision(
        accepted=(len(unique_rejects) == 0),
        reject_reasons=unique_rejects,
        put_net_credit=round(put_net_credit, 6),
        call_total_cost=round(call_total_cost, 6),
        combo_net_credit=round(combo_net_credit, 6),
        call_cost_ratio=_round_optional(call_cost_ratio),
        upside_scenario_price=_round_optional(upside_scenario_price),
        upside_lift=_round_optional(upside_lift),
        upside_net_lift=_round_optional(upside_net_lift),
        upside_lift_to_call_cost=_round_optional(upside_lift_to_call_cost),
        upside_lift_to_put_credit=_round_optional(upside_lift_to_put_credit),
        premium_funding_score=round(float(premium_funding_score), 6),
        combo_spread_ratio=_round_optional(combo_spread_ratio),
        score_components={name: round(float(value), 6) for name, value in components.items()},
    )


def yield_enhancement_rank_key(row: dict[str, Any]) -> tuple[float, ...]:
    def f(key: str, default: float = 0.0) -> float:
        value = _safe_float(row.get(key))
        return float(default if value is None else value)

    funding_accepted = str(row.get("funding_accepted") or "").strip().lower() in {"1", "true", "yes"}
    return (
        -1.0 if funding_accepted else 0.0,
        -f("premium_funding_score"),
        -f("upside_lift_to_call_cost"),
        -f("upside_lift_to_put_credit"),
        f("call_cost_to_put_credit", default=999.0),
        f("combo_spread_ratio", default=999.0),
        -f("combo_net_credit"),
        -f("scenario_score"),
        -f("annualized_scenario_score"),
        f("upside_breakeven_pct_above_spot", default=999.0),
        -f("net_credit"),
        -f("put_otm_pct"),
        -min(f("put_open_interest"), f("call_open_interest")),
        -f("call_delta"),
    )


def rank_yield_enhancement_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted([dict(row) for row in rows], key=yield_enhancement_rank_key)
