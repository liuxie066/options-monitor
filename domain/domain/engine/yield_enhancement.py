from __future__ import annotations

from dataclasses import dataclass
from math import sqrt
from typing import Any, Iterable


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
    put_only_net_credit: float
    net_credit_yield: float
    annualized_net_credit_yield: float | None
    funding_ratio: float | None
    cash_required: float
    put_only_breakeven: float
    combo_breakeven: float | None
    downside_breakeven_penalty: float | None
    lottery_budget_ratio: float | None
    residual_premium_ratio: float | None
    downside_breakeven: float | None
    upside_breakeven: float | None
    max_loss_if_zero: float | None
    put_otm_pct: float
    call_otm_pct: float
    gap_width_pct: float
    upside_breakeven_pct_above_spot: float | None
    combo_spread_ratio: float | None
    expected_move_iv: float | None = None
    expected_move: float | None = None
    call_payoff_multiple_at_1_5_sigma: float | None = None
    call_payoff_multiple_at_2_0_sigma: float | None = None
    scenario_score: float | None = None
    annualized_scenario_score: float | None = None


@dataclass(frozen=True)
class YieldEnhancementFundingDecision:
    accepted: bool
    reject_reasons: tuple[str, ...]
    put_net_credit: float
    call_total_cost: float
    combo_net_credit: float
    net_credit_yield: float
    annualized_net_credit_yield: float | None
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


def validate_yield_enhancement_pair(
    put_leg: YieldEnhancementLeg,
    call_leg: YieldEnhancementLeg,
) -> list[str]:
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
    lottery_budget_ratio = (call_cost / put_proceeds) if put_proceeds > 0 else None
    residual_premium_ratio = (net_credit / put_proceeds) if put_proceeds > 0 else None
    iv = _positive(expected_move_iv)
    expected_move = spot * iv * sqrt(float(dte) / 365.0) if iv is not None and dte > 0 else None

    cash_required = float(put_leg.strike) * multiplier - net_credit
    if cash_required <= 0:
        raise ValueError("cash_required must be > 0")
    net_credit_yield = net_credit / cash_required
    annualized_net_credit_yield = net_credit_yield * (365.0 / float(dte)) if dte > 0 else None

    put_only_breakeven = float(put_leg.strike) - put_proceeds / multiplier
    combo_breakeven = float(put_leg.strike) - net_credit / multiplier
    downside_breakeven_penalty = call_cost / multiplier
    downside_breakeven = combo_breakeven
    upside_breakeven = float(call_leg.strike) + net_debit / multiplier
    max_loss_if_zero = float(put_leg.strike) * multiplier - net_credit
    call_payoff_multiple_at_1_5_sigma = None
    call_payoff_multiple_at_2_0_sigma = None
    if expected_move is not None and call_cost > 0:
        call_payoff_multiple_at_1_5_sigma = (
            max(spot + 1.5 * expected_move - float(call_leg.strike), 0.0) * multiplier / call_cost
        )
        call_payoff_multiple_at_2_0_sigma = (
            max(spot + 2.0 * expected_move - float(call_leg.strike), 0.0) * multiplier / call_cost
        )
    scenario_score = None
    annualized_scenario_score = None
    if cash_required > 0 and expected_move is not None:
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
    upside_breakeven_pct_above_spot = (
        _pct_distance(upside_breakeven - spot, spot)
        if upside_breakeven is not None
        else None
    )

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
        put_only_net_credit=round(put_proceeds, 6),
        net_credit_yield=round(net_credit_yield, 6),
        annualized_net_credit_yield=(
            round(annualized_net_credit_yield, 6)
            if annualized_net_credit_yield is not None
            else None
        ),
        funding_ratio=(round(funding_ratio, 6) if funding_ratio is not None else None),
        cash_required=round(cash_required, 6),
        put_only_breakeven=round(put_only_breakeven, 6),
        combo_breakeven=_round_optional(combo_breakeven),
        downside_breakeven_penalty=_round_optional(downside_breakeven_penalty),
        lottery_budget_ratio=_round_optional(lottery_budget_ratio),
        residual_premium_ratio=_round_optional(residual_premium_ratio),
        downside_breakeven=_round_optional(downside_breakeven),
        upside_breakeven=_round_optional(upside_breakeven),
        max_loss_if_zero=_round_optional(max_loss_if_zero),
        put_otm_pct=round(put_otm_pct, 6),
        call_otm_pct=round(call_otm_pct, 6),
        gap_width_pct=round(gap_width_pct, 6),
        upside_breakeven_pct_above_spot=_round_optional(upside_breakeven_pct_above_spot),
        combo_spread_ratio=(round(combo_spread_ratio, 6) if combo_spread_ratio is not None else None),
        expected_move_iv=(round(iv, 6) if iv is not None else None),
        expected_move=(round(expected_move, 6) if expected_move is not None else None),
        call_payoff_multiple_at_1_5_sigma=_round_optional(call_payoff_multiple_at_1_5_sigma),
        call_payoff_multiple_at_2_0_sigma=_round_optional(call_payoff_multiple_at_2_0_sigma),
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
    min_net_credit_annualized: float | None = None,
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
    net_credit_yield = float(combo_metrics.net_credit_yield)
    annualized_net_credit_yield = _safe_float(combo_metrics.annualized_net_credit_yield)

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

    upside_lift_to_call_cost = (upside_lift / call_total_cost) if upside_lift is not None and call_total_cost > 0 else None
    upside_lift_to_put_credit = (upside_lift / put_net_credit) if upside_lift is not None and put_net_credit > 0 else None

    min_credit = _safe_float(min_combo_net_credit)
    if min_credit is not None and combo_net_credit < min_credit:
        reject_reasons.append("combo_net_credit")

    min_annualized_net_credit = _safe_float(min_net_credit_annualized)
    if min_annualized_net_credit is not None:
        if annualized_net_credit_yield is None or annualized_net_credit_yield < min_annualized_net_credit:
            reject_reasons.append("annualized_net_credit_yield")

    max_combo_spread = _safe_float(max_combo_spread_ratio)
    if max_combo_spread is not None:
        if combo_spread_ratio is None or combo_spread_ratio > max_combo_spread:
            reject_reasons.append("combo_spread_ratio")

    spread_penalty = max(combo_spread_ratio or 0.0, 0.0) * 0.10
    cost_penalty = max(call_cost_ratio or 0.0, 0.0)
    net_credit_retention = (combo_net_credit / put_net_credit) if put_net_credit > 0 else 0.0
    normalized_call_delta = _safe_float(call_leg.delta)
    call_delta = abs(normalized_call_delta) if normalized_call_delta is not None else 0.0
    participation_score = call_delta if call_delta > 0 else max(0.0, 1.0 - max(float(combo_metrics.call_otm_pct), 0.0) / 0.20)
    components = {
        "annualized_net_credit_yield": float(annualized_net_credit_yield or 0.0),
        "net_credit_retention": float(net_credit_retention),
        "call_participation": float(participation_score),
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
        net_credit_yield=round(net_credit_yield, 6),
        annualized_net_credit_yield=_round_optional(annualized_net_credit_yield),
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


def yield_enhancement_rank_key(row: dict[str, Any]) -> tuple[Any, ...]:
    def f(key: str, default: float = 0.0) -> float:
        value = _safe_float(row.get(key))
        return float(default if value is None else value)

    funding_accepted = str(row.get("funding_accepted") or "").strip().lower() in {"1", "true", "yes"}
    assignment_margin = f("put_assignment_margin_pct")
    if assignment_margin == 0.0:
        assignment_margin = f("put_otm_pct")
    call_participation = f("call_delta")
    if call_participation == 0.0:
        call_participation = max(0.0, 1.0 - max(f("call_otm_pct"), 0.0) / 0.20)
    return (
        -1.0 if funding_accepted else 0.0,
        -f("net_credit_retention"),
        -call_participation,
        f("combo_spread_ratio", default=999.0),
        -min(f("put_open_interest"), f("call_open_interest")),
        -assignment_margin,
        -f("annualized_net_credit_yield"),
        -f("combo_net_credit"),
        f("upside_breakeven_pct_above_spot", default=999.0),
        -f("net_credit"),
        f("call_cost_to_put_credit", default=999.0),
    )


def rank_yield_enhancement_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted([dict(row) for row in rows], key=yield_enhancement_rank_key)


@dataclass(frozen=True)
class ComboYieldResearchPolicy:
    """Immutable Shadow-only policy for proposed Combo Yield selection."""

    variant_id: str
    structure_mode: str
    min_net_credit_retention: float
    min_abs_call_delta: float
    target_abs_call_delta: float
    max_abs_call_delta: float
    max_call_cost_to_put_credit: float | None = None

    def __post_init__(self) -> None:
        variant_id = str(self.variant_id or "").strip()
        if not variant_id:
            raise ValueError("variant_id is required")
        mode = str(self.structure_mode or "").strip().lower()
        if mode != "same_expiry_pair":
            raise ValueError(f"unsupported Combo Yield research structure_mode: {mode or '<empty>'}")
        retention = float(self.min_net_credit_retention)
        if not 0.0 <= retention <= 1.0:
            raise ValueError("min_net_credit_retention must be between 0 and 1")
        delta_values = (
            float(self.min_abs_call_delta),
            float(self.target_abs_call_delta),
            float(self.max_abs_call_delta),
        )
        if not 0.0 <= delta_values[0] <= delta_values[1] <= delta_values[2] <= 1.0:
            raise ValueError("Call Delta bounds must satisfy 0 <= min <= target <= max <= 1")
        if self.max_call_cost_to_put_credit is not None:
            max_cost = float(self.max_call_cost_to_put_credit)
            if not 0.0 <= max_cost <= 1.0:
                raise ValueError("max_call_cost_to_put_credit must be between 0 and 1")


def combo_yield_proposed_gate_reasons(
    row: dict[str, Any],
    policy: ComboYieldResearchPolicy,
) -> tuple[str, ...]:
    """Return deterministic Shadow gate failures without mutating production rows."""

    reasons: list[str] = []
    mode = str(row.get("structure_mode") or "").strip().lower()
    if mode != policy.structure_mode:
        reasons.append("structure_mode")
    if (
        mode == "same_expiry_pair"
        and row.get("same_expiry_min_net_credit_annualized_pass") is False
    ):
        reasons.append("min_net_credit_annualized")

    put_credit = _safe_float(row.get("put_net_credit"))
    call_cost = _safe_float(row.get("call_total_cost"))
    combo_credit = _safe_float(row.get("combo_net_credit"))
    retention = _safe_float(row.get("net_credit_retention"))
    cost_ratio = _safe_float(row.get("call_cost_to_put_credit"))
    call_delta = _safe_float(row.get("call_delta"))
    multiplier = _safe_float(row.get("multiplier"))
    put_bid = _safe_float(row.get("put_bid"))
    call_ask = _safe_float(row.get("call_ask"))
    put_iv = _safe_float(row.get("put_implied_volatility"))
    call_iv = _safe_float(row.get("call_implied_volatility"))
    put_strike = _safe_float(row.get("put_strike"))
    call_strike = _safe_float(row.get("call_strike"))
    spot = _safe_float(row.get("spot"))
    if put_credit is None or put_credit <= 0:
        reasons.append("put_net_credit")
    if call_cost is None or call_cost <= 0:
        reasons.append("call_total_cost")
    if combo_credit is None or combo_credit < 0:
        reasons.append("combo_net_credit")
    if multiplier is None or multiplier <= 0:
        reasons.append("multiplier")
    if put_bid is None or put_bid <= 0:
        reasons.append("put_bid")
    if call_ask is None or call_ask <= 0:
        reasons.append("call_ask")
    if put_iv is None or put_iv <= 0:
        reasons.append("put_implied_volatility")
    if call_iv is None or call_iv <= 0:
        reasons.append("call_implied_volatility")
    if not str(row.get("currency") or "").strip():
        reasons.append("currency")
    if put_strike is None or call_strike is None or put_strike >= call_strike:
        reasons.append("strike_relation")
    if retention is None or retention < float(policy.min_net_credit_retention):
        reasons.append("min_net_credit_retention")
    if policy.max_call_cost_to_put_credit is not None and (
        cost_ratio is None or cost_ratio > float(policy.max_call_cost_to_put_credit)
    ):
        reasons.append("max_call_cost_to_put_credit")
    if call_delta is None:
        reasons.append("call_delta")
    else:
        absolute_delta = abs(call_delta)
        if absolute_delta < float(policy.min_abs_call_delta):
            reasons.append("min_abs_call_delta")
        if absolute_delta > float(policy.max_abs_call_delta):
            reasons.append("max_abs_call_delta")

    funding_rank = _positive_int(row.get("funding_put_rank"))
    if funding_rank is None:
        reasons.append("funding_put_rank")
    for field in ("put_contract_symbol", "call_contract_symbol"):
        if not str(row.get(field) or "").strip():
            reasons.append(field)
    for field in (
        "put_spread_ratio",
        "put_open_interest",
        "call_spread_ratio",
        "call_open_interest",
    ):
        if _safe_float(row.get(field)) is None:
            reasons.append(field)

    gap = _expiry_gap_days(row)
    if gap is None or gap != 0:
        reasons.append("same_expiry")
    if spot is None or call_strike is None or call_strike < spot:
        reasons.append("call_strike_below_spot")
    return tuple(dict.fromkeys(reasons))


def combo_yield_proposed_rank_key(
    row: dict[str, Any],
    policy: ComboYieldResearchPolicy,
) -> tuple[Any, ...]:
    """Rank a gate-passing proposed row; Funding Put quality is dominant."""

    funding_rank = _positive_int(row.get("funding_put_rank"))
    if funding_rank is None:
        funding_rank = 2**31 - 1
    call_delta = abs(_safe_float(row.get("call_delta")) or 0.0)
    call_spread = _safe_float(row.get("call_spread_ratio"))
    call_oi = _safe_float(row.get("call_open_interest"))
    key: list[Any] = [funding_rank]
    key.extend(
        [
            abs(call_delta - float(policy.target_abs_call_delta)),
            float(call_spread if call_spread is not None else 999.0),
            -float(call_oi if call_oi is not None else 0.0),
            str(row.get("put_contract_symbol") or ""),
            str(row.get("call_contract_symbol") or ""),
        ]
    )
    return tuple(key)


def rank_combo_yield_proposed_rows(
    rows: Iterable[dict[str, Any]],
    policy: ComboYieldResearchPolicy,
) -> list[dict[str, Any]]:
    """Return accepted proposed rows with explicit variant diagnostics."""

    accepted: list[dict[str, Any]] = []
    for source in rows:
        row = dict(source)
        reasons = combo_yield_proposed_gate_reasons(row, policy)
        if reasons:
            continue
        row["policy_variant_id"] = policy.variant_id
        row["proposed_rank_key"] = _typed_rank_key(combo_yield_proposed_rank_key(row, policy))
        accepted.append(row)
    return sorted(accepted, key=lambda row: combo_yield_proposed_rank_key(row, policy))


def rank_combo_yield_proposed_calls_for_put(
    rows: Iterable[dict[str, Any]],
    policy: ComboYieldResearchPolicy,
) -> list[dict[str, Any]]:
    accepted = rank_combo_yield_proposed_rows(rows, policy)
    put_symbols = {str(row.get("put_contract_symbol") or "") for row in accepted}
    if len(put_symbols) > 1:
        raise ValueError("proposed Call ranking requires exactly one Funding Put")
    return sorted(
        accepted,
        key=lambda row: combo_yield_proposed_rank_key(row, policy)[1:],
    )


def select_best_combo_yield_proposed_pairs(
    rows: Iterable[dict[str, Any]],
    policy: ComboYieldResearchPolicy,
) -> list[dict[str, Any]]:
    accepted = rank_combo_yield_proposed_rows(rows, policy)
    by_put: dict[str, list[dict[str, Any]]] = {}
    for row in accepted:
        by_put.setdefault(str(row.get("put_contract_symbol") or ""), []).append(row)
    per_put = [
        rank_combo_yield_proposed_calls_for_put(group, policy)[0]
        for _put, group in sorted(by_put.items())
        if group
    ]
    selected: list[dict[str, Any]] = []
    seen_symbols: set[str] = set()
    for row in sorted(per_put, key=lambda item: combo_yield_proposed_rank_key(item, policy)):
        symbol = str(row.get("symbol") or "").strip().upper()
        if symbol in seen_symbols:
            continue
        seen_symbols.add(symbol)
        selected.append(row)
    return selected


def _positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 1 else None


def _expiry_gap_days(row: dict[str, Any]) -> int | None:
    direct = _safe_float(row.get("expiry_gap_days"))
    if direct is not None:
        return int(direct)
    try:
        from datetime import date

        put_expiry = date.fromisoformat(str(row.get("put_expiration") or "")[:10])
        call_expiry = date.fromisoformat(str(row.get("call_expiration") or "")[:10])
    except (TypeError, ValueError):
        return None
    return (call_expiry - put_expiry).days


def _typed_rank_key(values: tuple[Any, ...]) -> list[dict[str, Any]]:
    return [
        {
            "type": (
                "bool"
                if isinstance(value, bool)
                else "int"
                if isinstance(value, int)
                else "float"
                if isinstance(value, float)
                else "str"
            ),
            "value": value,
        }
        for value in values
    ]


def select_best_yield_enhancement_per_symbol(
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return one canonical highest-ranked Combo Yield pair per underlying."""
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rank_yield_enhancement_rows(rows):
        symbol = str(row.get("symbol") or "").strip().upper()
        key = symbol or str(
            row.get("candidate_pair_id")
            or row.get("strategy_group_id")
            or row.get("put_contract_symbol")
            or ""
        )
        if key in seen:
            continue
        seen.add(key)
        selected.append(row)
    return selected


def rank_yield_enhancement_calls_for_put(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    copied = [dict(row) for row in rows]
    return rank_yield_enhancement_rows(copied)


def yield_enhancement_call_lottery_rank_key(row: dict[str, Any]) -> tuple[Any, ...]:
    def f(key: str, default: float = 0.0) -> float:
        value = _safe_float(row.get(key))
        return float(default if value is None else value)

    raw_call_delta = _safe_float(row.get("call_delta"))
    call_delta = abs(raw_call_delta) if raw_call_delta is not None else -1.0
    return (
        -call_delta,
        -f("call_payoff_multiple_at_1_5_sigma", default=-1.0),
        -f("call_payoff_multiple_at_2_0_sigma", default=-1.0),
        f("call_spread_ratio", default=999.0),
        -f("call_open_interest"),
        str(row.get("call_contract_symbol") or ""),
    )


def yield_enhancement_pair_shadow_rank_key(row: dict[str, Any]) -> tuple[Any, ...]:
    def f(key: str, default: float = 0.0) -> float:
        value = _safe_float(row.get(key))
        return float(default if value is None else value)

    assignment_margin = f("put_assignment_margin_pct")
    if assignment_margin == 0.0:
        assignment_margin = f("put_otm_pct")
    put_only_period_return = f("put_only_period_net_return", default=-1.0)
    if put_only_period_return < 0:
        put_only_period_return = f("period_net_return_on_cash_basis", default=-1.0)
    raw_call_delta = _safe_float(row.get("call_delta"))
    call_delta = abs(raw_call_delta) if raw_call_delta is not None else -1.0
    return (
        -put_only_period_return,
        -assignment_margin,
        -call_delta,
        -f("call_payoff_multiple_at_1_5_sigma", default=-1.0),
        -f("call_payoff_multiple_at_2_0_sigma", default=-1.0),
        f("combo_spread_ratio", default=999.0),
        -f("annualized_net_credit_yield", default=-1.0),
        -f("residual_premium_ratio", default=-1.0),
        str(row.get("put_contract_symbol") or ""),
        str(row.get("call_contract_symbol") or ""),
    )


def rank_yield_enhancement_call_lottery_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted([dict(row) for row in rows], key=yield_enhancement_call_lottery_rank_key)


def rank_yield_enhancement_shadow_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted([dict(row) for row in rows], key=yield_enhancement_pair_shadow_rank_key)
