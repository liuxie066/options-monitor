from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any


ECONOMIC_MODEL = "observable_carry.v1"


@dataclass(frozen=True)
class ShortOptionCarryInput:
    option_type: str
    spot: Decimal
    strike: Decimal
    price: Decimal
    dte: int
    multiplier: Decimal
    contracts: int
    capacity: Decimal
    currency: str


@dataclass(frozen=True)
class ObservableCarryResult:
    recommendation: str
    actionable: bool
    reason_codes: tuple[str, ...]
    current_extrinsic: Decimal | None = None
    current_daily_carry: Decimal | None = None
    current_capital_efficiency: Decimal | None = None
    candidate_extrinsic: Decimal | None = None
    candidate_daily_carry: Decimal | None = None
    candidate_capital_efficiency: Decimal | None = None
    comparison_horizon_days: int | None = None
    switch_friction: Decimal | None = None
    gross_carry_improvement_h: Decimal | None = None
    net_carry_improvement_h: Decimal | None = None
    payback_days: Decimal | None = None
    comparison_currency: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "economic_model": ECONOMIC_MODEL,
            "recommendation": self.recommendation,
            "actionable": self.actionable,
            "reason_codes": list(self.reason_codes),
            "current_extrinsic": _decimal_or_none(self.current_extrinsic),
            "current_daily_carry": _decimal_or_none(self.current_daily_carry),
            "current_capital_efficiency": _decimal_or_none(self.current_capital_efficiency),
            "candidate_extrinsic": _decimal_or_none(self.candidate_extrinsic),
            "candidate_daily_carry": _decimal_or_none(self.candidate_daily_carry),
            "candidate_capital_efficiency": _decimal_or_none(self.candidate_capital_efficiency),
            "comparison_horizon_days": self.comparison_horizon_days,
            "switch_friction": _decimal_or_none(self.switch_friction),
            "gross_carry_improvement_H": _decimal_or_none(self.gross_carry_improvement_h),
            "net_carry_improvement_H": _decimal_or_none(self.net_carry_improvement_h),
            "payback_days": _decimal_or_none(self.payback_days),
            "comparison_currency": self.comparison_currency,
        }


def decimal_value(value: Any, *, field: str, positive: bool = False, nonnegative: bool = False) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be numeric")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field} must be numeric") from exc
    if not parsed.is_finite():
        raise ValueError(f"{field} must be finite")
    if positive and parsed <= 0:
        raise ValueError(f"{field} must be > 0")
    if nonnegative and parsed < 0:
        raise ValueError(f"{field} must be >= 0")
    return parsed


def _decimal_or_none(value: Decimal | None) -> str | None:
    if value is None:
        return None
    rendered = format(value.normalize(), "f")
    return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered


def intrinsic_value(*, option_type: str, spot: Decimal, strike: Decimal) -> Decimal:
    option = str(option_type or "").strip().lower()
    if option == "put":
        return max(strike - spot, Decimal("0"))
    if option == "call":
        return max(spot - strike, Decimal("0"))
    raise ValueError(f"unsupported option_type: {option_type}")


def _positive_integer(value: Any, *, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a positive integer")
    try:
        parsed = int(value)
        numeric = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{field} must be a positive integer") from exc
    if not numeric.is_finite() or numeric != parsed or parsed <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return parsed


def _validated_short_option_input(
    item: ShortOptionCarryInput,
    *,
    require_positive_dte: bool,
) -> ShortOptionCarryInput:
    option_type = str(item.option_type or "").strip().lower()
    if option_type not in {"put", "call"}:
        raise ValueError("option_type_invalid")
    currency = str(item.currency or "").strip().upper()
    if not currency:
        raise ValueError("currency_missing")
    if isinstance(item.dte, bool):
        raise ValueError("dte_invalid")
    try:
        dte = int(item.dte)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("dte_invalid") from exc
    if dte != item.dte or (require_positive_dte and dte <= 0):
        raise ValueError("dte_invalid")
    return ShortOptionCarryInput(
        option_type=option_type,
        spot=decimal_value(item.spot, field="spot", nonnegative=True),
        strike=decimal_value(item.strike, field="strike", positive=True),
        price=decimal_value(item.price, field="price", nonnegative=True),
        dte=dte,
        multiplier=decimal_value(item.multiplier, field="multiplier", positive=True),
        contracts=_positive_integer(item.contracts, field="contracts"),
        capacity=decimal_value(item.capacity, field="capacity", positive=True),
        currency=currency,
    )


def short_option_carry(item: ShortOptionCarryInput) -> tuple[Decimal, Decimal, Decimal]:
    normalized = _validated_short_option_input(item, require_positive_dte=False)
    intrinsic = intrinsic_value(
        option_type=normalized.option_type,
        spot=normalized.spot,
        strike=normalized.strike,
    )
    extrinsic = (
        max(normalized.price - intrinsic, Decimal("0"))
        * normalized.multiplier
        * normalized.contracts
    )
    daily = extrinsic / max(normalized.dte, 1)
    efficiency = daily / normalized.capacity
    return extrinsic, daily, efficiency


def _convert(
    value: Decimal,
    *,
    currency: str,
    comparison_currency: str,
    fx_to_cny: dict[str, Any] | None,
) -> Decimal:
    source = str(currency or "").strip().upper()
    target = str(comparison_currency or "").strip().upper()
    if not source or not target:
        raise ValueError("comparison_currency_missing")
    if source == target:
        return value
    if target != "CNY" or not fx_to_cny:
        raise ValueError("cross-currency comparison requires one fresh CNY FX snapshot")
    rate = decimal_value(fx_to_cny.get(source), field=f"FX {source}/CNY", positive=True)
    return value * rate


def evaluate_short_option_switch(
    *,
    current: ShortOptionCarryInput,
    candidate: ShortOptionCarryInput,
    close_fee: Any,
    open_fee: Any,
    proposed_action: str,
    replacement_eligible: bool,
    fx_to_cny: dict[str, Any] | None = None,
    fx_fresh: bool = True,
    evidence_complete: bool = True,
    allocator_selected: bool = False,
) -> ObservableCarryResult:
    reasons: list[str] = []
    action = str(proposed_action or "").strip().lower()
    if action not in {"roll", "replace", "reallocate"}:
        return ObservableCarryResult("not_evaluable", False, ("unsupported_proposed_action",))
    try:
        current_input = _validated_short_option_input(current, require_positive_dte=False)
        candidate_input = _validated_short_option_input(candidate, require_positive_dte=True)
    except ValueError:
        return ObservableCarryResult("not_evaluable", False, ("economic_input_invalid",))
    if current_input.option_type != candidate_input.option_type:
        return ObservableCarryResult("not_evaluable", False, ("option_type_mismatch",))
    currencies_differ = current_input.currency != candidate_input.currency
    if currencies_differ and (not fx_fresh or not fx_to_cny):
        return ObservableCarryResult("not_evaluable", False, ("fx_missing_or_stale",))
    if not evidence_complete:
        return ObservableCarryResult("not_evaluable", False, ("economic_evidence_incomplete",))
    if not replacement_eligible:
        return ObservableCarryResult("hold", False, ("replacement_ineligible",))

    comparison_currency = "CNY" if currencies_differ else current_input.currency
    try:
        current_extrinsic, current_daily, current_efficiency = short_option_carry(current_input)
        candidate_extrinsic, candidate_daily, candidate_efficiency = short_option_carry(candidate_input)
        current_extrinsic = _convert(
            current_extrinsic,
            currency=current_input.currency,
            comparison_currency=comparison_currency,
            fx_to_cny=fx_to_cny,
        )
        current_daily = _convert(
            current_daily,
            currency=current_input.currency,
            comparison_currency=comparison_currency,
            fx_to_cny=fx_to_cny,
        )
        candidate_extrinsic = _convert(
            candidate_extrinsic,
            currency=candidate_input.currency,
            comparison_currency=comparison_currency,
            fx_to_cny=fx_to_cny,
        )
        candidate_daily = _convert(
            candidate_daily,
            currency=candidate_input.currency,
            comparison_currency=comparison_currency,
            fx_to_cny=fx_to_cny,
        )
        current_capacity = _convert(
            current_input.capacity,
            currency=current_input.currency,
            comparison_currency=comparison_currency,
            fx_to_cny=fx_to_cny,
        )
        candidate_capacity = _convert(
            candidate_input.capacity,
            currency=candidate_input.currency,
            comparison_currency=comparison_currency,
            fx_to_cny=fx_to_cny,
        )
        current_efficiency = current_daily / current_capacity
        candidate_efficiency = candidate_daily / candidate_capacity
        friction = _convert(
            decimal_value(close_fee, field="close_fee", nonnegative=True),
            currency=current_input.currency,
            comparison_currency=comparison_currency,
            fx_to_cny=fx_to_cny,
        ) + _convert(
            decimal_value(open_fee, field="open_fee", nonnegative=True),
            currency=candidate_input.currency,
            comparison_currency=comparison_currency,
            fx_to_cny=fx_to_cny,
        )
    except ValueError as exc:
        return ObservableCarryResult("not_evaluable", False, (str(exc),))

    horizon = min(max(current_input.dte, 1), candidate_input.dte)
    incremental_daily = candidate_daily - current_daily
    gross = incremental_daily * horizon
    net = gross - friction
    payback = friction / incremental_daily if incremental_daily > 0 else None
    if candidate_efficiency <= current_efficiency:
        reasons.append("capital_efficiency_not_improved")
    if net <= 0:
        reasons.append("net_carry_improvement_not_positive")
    if net < friction:
        reasons.append("improvement_below_two_times_friction")
    if payback is None or payback > horizon:
        reasons.append("payback_exceeds_horizon")
    if not allocator_selected:
        reasons.append("portfolio_allocator_not_selected")
    actionable = not reasons
    return ObservableCarryResult(
        recommendation=action if actionable else "hold",
        actionable=actionable,
        reason_codes=tuple(reasons or ("observable_carry_improved",)),
        current_extrinsic=current_extrinsic,
        current_daily_carry=current_daily,
        current_capital_efficiency=current_efficiency,
        candidate_extrinsic=candidate_extrinsic,
        candidate_daily_carry=candidate_daily,
        candidate_capital_efficiency=candidate_efficiency,
        comparison_horizon_days=horizon,
        switch_friction=friction,
        gross_carry_improvement_h=gross,
        net_carry_improvement_h=net,
        payback_days=payback,
        comparison_currency=comparison_currency,
    )


def long_call_observable_facts(
    *,
    spot: Any,
    strike: Any,
    bid: Any,
    contracts: int,
    multiplier: Any,
    dte: int,
    fee: Any = 0,
) -> dict[str, Any]:
    spot_value = decimal_value(spot, field="spot", nonnegative=True)
    strike_value = decimal_value(strike, field="strike", nonnegative=True)
    bid_value = decimal_value(bid, field="bid", nonnegative=True)
    multiplier_value = decimal_value(multiplier, field="multiplier", positive=True)
    fee_value = decimal_value(fee, field="fee", nonnegative=True)
    intrinsic = intrinsic_value(option_type="call", spot=spot_value, strike=strike_value)
    contracts_value = _positive_integer(contracts, field="contracts")
    if isinstance(dte, bool):
        raise ValueError("dte must be a nonnegative integer")
    try:
        dte_value = int(dte)
        dte_numeric = Decimal(str(dte))
    except (InvalidOperation, TypeError, ValueError, OverflowError) as exc:
        raise ValueError("dte must be a nonnegative integer") from exc
    if not dte_numeric.is_finite() or dte_numeric != dte_value or dte_value < 0:
        raise ValueError("dte must be a nonnegative integer")
    total_scale = multiplier_value * contracts_value
    return {
        "economic_model": ECONOMIC_MODEL,
        "sale_now_net": _decimal_or_none(bid_value * total_scale - fee_value),
        "current_intrinsic": _decimal_or_none(intrinsic * total_scale),
        "current_extrinsic": _decimal_or_none(max(bid_value - intrinsic, Decimal("0")) * total_scale),
        "days_to_expiry": dte_value,
        "recommendation": "not_evaluable",
        "reason": "long_call_forward_model_not_approved",
        "actionable": False,
    }


__all__ = [
    "ECONOMIC_MODEL",
    "ObservableCarryResult",
    "ShortOptionCarryInput",
    "decimal_value",
    "evaluate_short_option_switch",
    "intrinsic_value",
    "long_call_observable_facts",
    "short_option_carry",
]
