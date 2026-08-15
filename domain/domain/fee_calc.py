"""Canonical Futu option, stock, and terminal fee calculations."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal, ROUND_CEILING
from math import isfinite
from typing import Any

import pandas as pd

from domain.domain.option_position_identity import normalize_currency


FUTU_US_FEE_SCHEDULE_URL = "https://www.futuhk.com/en/support/topic2_283"
FUTU_HK_FEE_SCHEDULE_URL = "https://www.futuhk.com/en/support/topic2_335"
FUTU_US_OPTION_FEE_BASIS = "futu_us_fixed_package_2026-07-22"
FUTU_HK_OPTION_FEE_BASIS = "futu_hk_tier1_upper_bound_2026-07-22"
FUTU_OPTION_FEE_SCHEDULE_VERSION = "futu_option_sell_fee.v1"
FUTU_US_OPTION_CANDIDATE_FEE_BASIS = "futu_us_candidate_upper_bound_2026-08-06"
FUTU_US_CANDIDATE_PLATFORM_FEE_UPPER_BOUND = 0.60
FUTU_US_FIXED_PLATFORM_FEE = 0.30

FUTU_HK_TERMINAL_FEE_SCHEDULE_VERSION = "futu_hk_terminal_fee.v1"
FUTU_HK_EXERCISE_FEE_PER_CONTRACT = 2.0


@dataclass(frozen=True)
class OptionFeeEstimate:
    amount: float
    currency: str
    fee_schedule_version: str
    fee_basis: str
    fee_schedule_url: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

_ACTUAL_FEE_TOTAL_KEYS = (
    "total_fee",
    "total_fees",
    "fees_total",
    "fee_total",
    "commission_and_fees",
    "charges",
    "fees",
    "fee",
)
_ACTUAL_FEE_COMPONENT_KEYS = (
    "commission",
    "commission_fee",
    "platform_fee",
    "transaction_fee",
    "trading_fee",
    "exchange_fee",
    "settlement_fee",
    "system_fee",
    "regulatory_fee",
    "reg_fee",
    "sec_fee",
    "taf",
    "orf",
    "stamp_duty",
)


def _require_positive(name: str, value: float | None) -> float:
    if value is None or value <= 0:
        raise ValueError(f"{name} must be > 0")
    return float(value)


def calc_futu_us_option_fee(
    order_price: float,
    *,
    contracts: int = 1,
    multiplier: int = 100,
    is_sell: bool = True,
) -> float:
    """Estimate Futu HK US-option fees using the dated fixed package."""
    price = _require_positive("order_price", float(order_price))
    qty = int(contracts)
    if qty <= 0:
        raise ValueError("contracts must be > 0")
    unit_multiplier = int(multiplier)
    if unit_multiplier <= 0:
        raise ValueError("multiplier must be > 0")

    transaction_amount = price * unit_multiplier * qty
    commission_per_contract = 0.65 if price > 0.1 else 0.15
    commission = max(commission_per_contract * qty, 1.99)
    platform_fee = FUTU_US_FIXED_PLATFORM_FEE * qty
    orf = 0.013 * qty
    occ_fee = min(0.02 * qty, 55.0)
    settlement_fee = 0.18 * qty
    cat_fee = 0.0003 * qty
    sec_fee = max(transaction_amount * 0.0000206, 0.01) if is_sell else 0.0
    taf = max(0.00329 * qty, 0.01) if is_sell else 0.0
    total = commission + platform_fee + orf + occ_fee + settlement_fee + cat_fee + sec_fee + taf
    return round(total, 6)


def calc_futu_hk_option_fee(
    order_price: float,
    *,
    contracts: int = 1,
    multiplier: int = 100,
    is_sell: bool = True,
) -> float:
    """Estimate Futu HK option fees with a conservative Tier-1 tariff."""
    del is_sell
    price = _require_positive("order_price", float(order_price))
    qty = int(contracts)
    if qty <= 0:
        raise ValueError("contracts must be > 0")
    unit_multiplier = int(multiplier)
    if unit_multiplier <= 0:
        raise ValueError("multiplier must be > 0")

    transaction_amount = price * unit_multiplier * qty
    commission = max(transaction_amount * 0.002, 3.0)
    platform_fee = 15.0
    system_fee = 0.0 if Decimal(str(price)) == Decimal("0.01") else 3.0 * qty
    total = commission + platform_fee + system_fee
    return round(total, 6)


def calc_futu_option_fee(
    currency: str | None,
    order_price: float,
    *,
    contracts: int = 1,
    multiplier: int = 100,
    is_sell: bool = True,
) -> float:
    ccy = normalize_currency(currency)
    if ccy not in {"USD", "HKD"}:
        raise ValueError("currency must resolve to USD or HKD")
    if ccy == "HKD":
        return calc_futu_hk_option_fee(
            order_price,
            contracts=contracts,
            multiplier=multiplier,
            is_sell=is_sell,
        )
    return calc_futu_us_option_fee(
        order_price,
        contracts=contracts,
        multiplier=multiplier,
        is_sell=is_sell,
    )


def estimate_futu_option_sell_fee(
    currency: str | None,
    order_price: float,
    *,
    contracts: int,
    multiplier: int,
) -> OptionFeeEstimate:
    """Return the versioned candidate-stage sell-fee estimate.

    Candidate calculations must bind the broker-observed contract multiplier;
    this strict facade intentionally has no multiplier default. Actual trade
    performance continues to use broker-reported fees via ``extract_actual_fees``.
    """

    ccy = normalize_currency(currency)
    if ccy == "USD":
        basis = FUTU_US_OPTION_CANDIDATE_FEE_BASIS
        url = FUTU_US_FEE_SCHEDULE_URL
    elif ccy == "HKD":
        basis = FUTU_HK_OPTION_FEE_BASIS
        url = FUTU_HK_FEE_SCHEDULE_URL
    else:
        raise ValueError("currency must resolve to USD or HKD")
    amount = calc_futu_option_fee(
        ccy,
        order_price,
        contracts=contracts,
        multiplier=multiplier,
        is_sell=True,
    )
    if ccy == "USD":
        # Opening candidates have no account-level package fact. Use the
        # confirmed conservative platform-fee upper bound without changing
        # compatibility callers that explicitly model the fixed package.
        amount += (
            FUTU_US_CANDIDATE_PLATFORM_FEE_UPPER_BOUND
            - FUTU_US_FIXED_PLATFORM_FEE
        ) * int(contracts)
        amount = round(amount, 6)
    return OptionFeeEstimate(
        amount=amount,
        currency=ccy,
        fee_schedule_version=FUTU_OPTION_FEE_SCHEDULE_VERSION,
        fee_basis=basis,
        fee_schedule_url=url,
    )


def calc_futu_us_stock_fee(
    order_price: float,
    *,
    shares: int,
    is_sell: bool = True,
) -> float:
    """Estimate a US stock trade using Futu HK's standard fixed fee package."""

    price = _require_positive("order_price", float(order_price))
    qty = int(shares)
    if qty <= 0:
        raise ValueError("shares must be > 0")

    transaction_amount = price * qty
    commission = max(0.0049 * qty, 0.99)
    platform_fee = max(0.005 * qty, 1.0)
    settlement_fee = 0.003 * qty
    regulatory_fee = max(0.0000206 * transaction_amount, 0.01) if is_sell else 0.0
    trading_activity_fee = min(max(0.000195 * qty, 0.01), 9.79) if is_sell else 0.0
    cat_fee = 0.000003 * qty
    return round(
        commission
        + platform_fee
        + settlement_fee
        + regulatory_fee
        + trading_activity_fee
        + cat_fee,
        6,
    )


def calc_futu_hk_stock_fee(
    order_price: float,
    *,
    shares: int,
    is_sell: bool = True,
) -> float:
    """Estimate a HK stock trade using Futu HK's standard fixed fee package."""

    del is_sell
    components = _standard_fixed_hk_stock_fee_components(order_price, shares)
    return round(sum(float(value) for value in components.values()), 6)


def calc_futu_stock_fee(
    currency: str | None,
    order_price: float,
    *,
    shares: int,
    is_sell: bool = True,
) -> float:
    ccy = normalize_currency(currency)
    if ccy == "HKD":
        return calc_futu_hk_stock_fee(order_price, shares=shares, is_sell=is_sell)
    if ccy == "USD":
        return calc_futu_us_stock_fee(order_price, shares=shares, is_sell=is_sell)
    raise ValueError(f"unsupported stock fee currency: {ccy or currency}")


def _standard_fixed_hk_stock_fee_components(order_price: float, shares: int) -> dict[str, float]:
    """HK stock settlement leg under Futu HK's standard fixed (non-commission-free) package."""

    price = _require_positive("order_price", float(order_price))
    qty = int(shares)
    if qty <= 0:
        raise ValueError("shares must be > 0")

    transaction_amount = price * qty
    return {
        "commission": max(transaction_amount * 0.0003, 3.0),
        "platform_fee": 15.0,
        "settlement_fee": transaction_amount * 0.000042,
        "stamp_duty": float(
            (Decimal(str(price)) * qty * Decimal("0.001")).to_integral_value(rounding=ROUND_CEILING)
        ),
        "trading_fee": max(transaction_amount * 0.0000565, 0.01),
        "sfc_levy": max(transaction_amount * 0.000027, 0.01),
        "frc_levy": transaction_amount * 0.0000015,
    }


def _positive_integral(value: Any) -> int | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        number = Decimal(str(value))
    except Exception:
        return None
    if not number.is_finite() or number <= 0 or number != number.to_integral_value():
        return None
    return int(number)


def _finite_float(value: Any) -> float | None:
    if isinstance(value, (bool, str)) or value is None:
        return None
    try:
        number = float(value)
    except Exception:
        return None
    return number if isfinite(number) else None


def calc_futu_hk_terminal_fee(
    kind: str,
    *,
    order_price: float | None = None,
    shares: int = 0,
    contracts: int = 0,
    account_fee_plan: Any = None,
) -> dict[str, Any]:
    """Structured HK option terminal (assignment/exercise/expired-worthless) fee.

    Reuses the standard fixed HK stock package for the stock settlement leg and
    adds the terminal option leg: assignment exercise fee is 0, exercise is
    HK$2/contract, expired-worthless is 0. Requires explicit account fee-plan
    facts (commission_free, platform_fee, fee_plan_ref); when any is missing
    the result fails closed (complete=false) while keeping a clearly named
    standard fixed non-commission-free estimate for audit. Actual broker fees
    remain authoritative via extract_actual_fees / fee_provenance upstream and
    are never overridden by this estimate.
    """

    terminal_kind = str(kind or "").strip().lower()
    if terminal_kind not in {"assignment", "exercise", "expired_worthless"}:
        raise ValueError(f"unsupported HK terminal fee kind: {kind}")

    result: dict[str, Any] = {
        "kind": terminal_kind,
        "currency": "HKD",
        "source": FUTU_HK_FEE_SCHEDULE_URL,
        "schedule_version": FUTU_HK_TERMINAL_FEE_SCHEDULE_VERSION,
        "complete": False,
        "basis": "missing",
        "amount": None,
        "reason": "stock_fee_inputs_incomplete",
        "fee_plan_ref": None,
        "missing_plan_facts": [],
        "components": {},
        "estimated_components": {},
        "estimated_amount": None,
        "estimated_basis": "standard_fixed_non_commission_free",
    }

    # Expired-worthless options lapse with no settlement and no fee; this is
    # policy-complete regardless of the account fee plan.
    if terminal_kind == "expired_worthless":
        result.update(
            {
                "complete": True,
                "basis": "estimated",
                "amount": 0.0,
                "reason": "hk_expired_worthless_no_fee",
                "estimated_amount": 0.0,
            }
        )
        return result

    plan = account_fee_plan if isinstance(account_fee_plan, dict) else {}
    commission_free = plan.get("commission_free")
    platform_fee = _finite_float(plan.get("platform_fee"))
    raw_fee_plan_ref = plan.get("fee_plan_ref")
    fee_plan_ref = raw_fee_plan_ref.strip() if isinstance(raw_fee_plan_ref, str) else ""
    missing_plan_facts = [
        name
        for name, ok in (
            ("commission_free", isinstance(commission_free, bool)),
            ("platform_fee", platform_fee is not None and platform_fee >= 0),
            ("fee_plan_ref", bool(fee_plan_ref)),
        )
        if not ok
    ]

    qty = _positive_integral(contracts)
    share_qty = _positive_integral(shares)
    price = _finite_float(order_price)
    inputs_complete = qty is not None and share_qty is not None and price is not None and price > 0
    estimated_components: dict[str, float] = {}
    if inputs_complete:
        try:
            stock_components = _standard_fixed_hk_stock_fee_components(price, share_qty)
            candidate_components = {
                **stock_components,
                "exercise_fee": round(
                    FUTU_HK_EXERCISE_FEE_PER_CONTRACT * qty
                    if terminal_kind == "exercise"
                    else 0.0,
                    6,
                ),
            }
        except (ArithmeticError, TypeError, ValueError):
            inputs_complete = False
        else:
            if all(isfinite(value) for value in candidate_components.values()) and isfinite(
                sum(candidate_components.values())
            ):
                estimated_components = candidate_components
            else:
                inputs_complete = False
    estimated_amount = (
        round(sum(estimated_components.values()), 6) if estimated_components else None
    )
    result.update(
        {
            "fee_plan_ref": fee_plan_ref or None,
            "estimated_components": estimated_components,
            "estimated_amount": estimated_amount,
            "missing_plan_facts": missing_plan_facts,
        }
    )

    if not inputs_complete:
        return result

    if missing_plan_facts:
        result["reason"] = "hk_account_fee_plan_missing"
        return result

    effective_components = dict(estimated_components)
    if commission_free:
        effective_components["commission"] = 0.0
    effective_components["platform_fee"] = float(platform_fee)
    amount = round(sum(effective_components.values()), 6)
    result.update(
        {
            "complete": True,
            "basis": "estimated",
            "amount": amount,
            "reason": "account_fee_plan_applied",
            "components": effective_components,
        }
    )
    return result


def extract_actual_fees(payload: Any) -> dict[str, Any] | None:
    """Extract explicitly supplied broker fees without treating absent zero as actual."""

    for source_name, source in _fee_sources(payload):
        for key in _ACTUAL_FEE_TOTAL_KEYS:
            if key not in source:
                continue
            value = safe_float(source.get(key))
            if value is not None:
                return {
                    "amount": round(abs(value), 6),
                    "source": f"{source_name}.{key}",
                    "components": [key],
                }

        components: list[str] = []
        total = 0.0
        for key in _ACTUAL_FEE_COMPONENT_KEYS:
            if key not in source:
                continue
            value = safe_float(source.get(key))
            if value is None:
                continue
            components.append(key)
            total += abs(value)
        if components:
            return {
                "amount": round(total, 6),
                "source": f"{source_name}.components",
                "components": components,
            }
    return None


def _fee_sources(payload: Any) -> list[tuple[str, dict[str, Any]]]:
    if not isinstance(payload, dict):
        return []
    out: list[tuple[str, dict[str, Any]]] = [("raw_payload", payload)]
    for key in ("raw", "deal", "broker_payload", "raw_payload"):
        nested = payload.get(key)
        if isinstance(nested, dict) and nested is not payload:
            out.append((f"raw_payload.{key}", nested))
    return out


def safe_float(v):
    try:
        if pd.isna(v):
            return None
        return float(v)
    except Exception:
        return None


def safe_int(v):
    try:
        if pd.isna(v):
            return None
        return int(float(v))
    except Exception:
        return None
