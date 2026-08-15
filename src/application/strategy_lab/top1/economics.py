from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import date
from typing import cast

from domain.domain.fee_calc import (
    FUTU_HK_TERMINAL_FEE_SCHEDULE_VERSION,
    calc_futu_hk_terminal_fee,
)


_NO_FILL_KEYS = frozenset({"stage", "fill_status"})
_FILLED_KEYS = frozenset(
    {
        "stage",
        "fill_status",
        "holding_start_date",
        "expiration",
        "opening_net_premium",
        "net_cash_basis",
        "strike",
        "multiplier",
        "underlier_close",
        "account_fee_plan",
    }
)
_FEE_PLAN_KEYS = frozenset({"commission_free", "platform_fee", "fee_plan_ref"})


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a mapping")
    raw_mapping = cast(Mapping[object, object], value)
    if not all(isinstance(key, str) for key in raw_mapping):
        raise ValueError(f"{label} keys must be strings")
    return cast(Mapping[str, object], raw_mapping)


def _exact_keys(value: Mapping[str, object], expected: frozenset[str], label: str) -> None:
    if set(value) != set(expected):
        raise ValueError(f"{label} keys are incomplete or unexpected")


def _number(value: object, label: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be numeric")
    number = float(value)
    if not math.isfinite(number) or (positive and number <= 0):
        raise ValueError(f"{label} is invalid")
    return number


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _iso_date(value: object, label: str) -> date:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{label} must be a canonical ISO date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be a canonical ISO date") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{label} must be a canonical ISO date")
    return parsed


def _result(
    *,
    status: str,
    reason_code: str | None,
    reason_detail: str | None,
    stage: str,
    fill_status: str,
    assignment_proxy: bool | None,
    intrinsic_per_share: float | None,
    holding_calendar_days: int | None,
    terminal_fee_schedule_version: str | None,
    terminal_fee_reason: str | None,
    terminal_fee_amount: float | None,
    economic_pnl: float | None,
    efficiency: float | None,
) -> dict[str, object]:
    return {
        "status": status,
        "reason_code": reason_code,
        "reason_detail": reason_detail,
        "stage": stage,
        "fill_status": fill_status,
        "assignment_proxy": assignment_proxy,
        "intrinsic_per_share": intrinsic_per_share,
        "holding_calendar_days": holding_calendar_days,
        "terminal_fee_schedule_version": terminal_fee_schedule_version,
        "terminal_fee_reason": terminal_fee_reason,
        "terminal_fee_amount": terminal_fee_amount,
        "economic_pnl": economic_pnl,
        "efficiency": efficiency,
    }


def calculate_expiry_efficiency(economic_facts: object) -> dict[str, object]:
    facts = _mapping(economic_facts, "economic_facts")

    if set(facts) == set(_NO_FILL_KEYS):
        if (
            facts["stage"] != "validation"
            or facts["fill_status"] != "no_observed_fill"
        ):
            raise ValueError("no-fill facts must describe validation/no_observed_fill")
        return _result(
            status="evaluable",
            reason_code=None,
            reason_detail=None,
            stage="validation",
            fill_status="no_observed_fill",
            assignment_proxy=None,
            intrinsic_per_share=None,
            holding_calendar_days=None,
            terminal_fee_schedule_version=None,
            terminal_fee_reason=None,
            terminal_fee_amount=None,
            economic_pnl=0.0,
            efficiency=0.0,
        )

    _exact_keys(facts, _FILLED_KEYS, "economic_facts")
    raw_stage = facts["stage"]
    raw_fill_status = facts["fill_status"]
    expected_fill = {"research": "t0_assumed_fill", "validation": "observed_fill"}
    if (
        not isinstance(raw_stage, str)
        or not isinstance(raw_fill_status, str)
        or expected_fill.get(raw_stage) != raw_fill_status
    ):
        raise ValueError("stage and fill_status are inconsistent")
    stage = raw_stage
    fill_status = raw_fill_status

    start = _iso_date(facts["holding_start_date"], "holding_start_date")
    expiration = _iso_date(facts["expiration"], "expiration")
    premium = _number(facts["opening_net_premium"], "opening_net_premium", positive=True)
    cash_basis = _number(facts["net_cash_basis"], "net_cash_basis", positive=True)
    strike = _number(facts["strike"], "strike", positive=True)
    multiplier = _positive_int(facts["multiplier"], "multiplier")
    close = _number(facts["underlier_close"], "underlier_close", positive=True)

    raw_fee_plan = facts["account_fee_plan"]
    fee_plan: dict[str, object] | None = None
    if raw_fee_plan is not None:
        fee_plan_mapping = _mapping(raw_fee_plan, "account_fee_plan")
        if not set(fee_plan_mapping).issubset(set(_FEE_PLAN_KEYS)):
            raise ValueError("account_fee_plan contains unexpected keys")
        fee_plan = dict(fee_plan_mapping)

    holding_days = (expiration - start).days
    intrinsic = max(strike - close, 0.0)
    assignment_proxy = intrinsic > 0
    if holding_days <= 0:
        return _result(
            status="not_evaluable",
            reason_code="holding_period_non_positive",
            reason_detail=None,
            stage=stage,
            fill_status=fill_status,
            assignment_proxy=assignment_proxy,
            intrinsic_per_share=intrinsic,
            holding_calendar_days=holding_days,
            terminal_fee_schedule_version=None,
            terminal_fee_reason=None,
            terminal_fee_amount=None,
            economic_pnl=None,
            efficiency=None,
        )

    raw_terminal_fee: object = calc_futu_hk_terminal_fee(
        "assignment" if assignment_proxy else "expired_worthless",
        order_price=strike,
        shares=multiplier,
        contracts=1,
        account_fee_plan=fee_plan,
    )
    terminal_fee = _mapping(raw_terminal_fee, "terminal fee result")
    if terminal_fee.get("currency") != "HKD":
        raise ValueError("terminal fee result must use HKD")
    schedule_version = terminal_fee.get("schedule_version")
    if (
        not isinstance(schedule_version, str)
        or schedule_version != FUTU_HK_TERMINAL_FEE_SCHEDULE_VERSION
    ):
        raise ValueError("terminal fee schedule version mismatch")
    complete = terminal_fee.get("complete")
    if not isinstance(complete, bool):
        raise ValueError("terminal fee result complete flag is invalid")
    reason = terminal_fee.get("reason")
    if not isinstance(reason, str) or not reason or reason != reason.strip():
        raise ValueError("terminal fee reason is invalid")

    if not complete:
        return _result(
            status="not_evaluable",
            reason_code="required_outcome_missing",
            reason_detail="expiry_fee_unavailable",
            stage=stage,
            fill_status=fill_status,
            assignment_proxy=assignment_proxy,
            intrinsic_per_share=intrinsic,
            holding_calendar_days=holding_days,
            terminal_fee_schedule_version=schedule_version,
            terminal_fee_reason=reason,
            terminal_fee_amount=None,
            economic_pnl=None,
            efficiency=None,
        )

    if terminal_fee.get("basis") != "estimated":
        raise ValueError("complete terminal fee result must be estimated")
    fee_amount = _number(terminal_fee.get("amount"), "terminal_fee.amount")
    if fee_amount < 0:
        raise ValueError("terminal_fee.amount must be non-negative")

    pnl = premium - fee_amount
    if assignment_proxy:
        pnl += (close - strike) * multiplier
    efficiency = pnl / cash_basis / holding_days * 365
    return _result(
        status="evaluable",
        reason_code=None,
        reason_detail=None,
        stage=stage,
        fill_status=fill_status,
        assignment_proxy=assignment_proxy,
        intrinsic_per_share=intrinsic,
        holding_calendar_days=holding_days,
        terminal_fee_schedule_version=schedule_version,
        terminal_fee_reason=reason,
        terminal_fee_amount=fee_amount,
        economic_pnl=pnl,
        efficiency=efficiency,
    )
