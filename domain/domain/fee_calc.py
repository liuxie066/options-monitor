"""Futu option and stock fee calculation helpers."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal, ROUND_CEILING
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
    price = _require_positive("order_price", float(order_price))
    qty = int(shares)
    if qty <= 0:
        raise ValueError("shares must be > 0")

    transaction_amount = price * qty
    commission = max(transaction_amount * 0.0003, 3.0)
    platform_fee = 15.0
    settlement_fee = transaction_amount * 0.000042
    stamp_duty = float(
        (Decimal(str(price)) * qty * Decimal("0.001")).to_integral_value(rounding=ROUND_CEILING)
    )
    trading_fee = max(transaction_amount * 0.0000565, 0.01)
    sfc_levy = max(transaction_amount * 0.000027, 0.01)
    frc_levy = transaction_amount * 0.0000015
    return round(
        commission
        + platform_fee
        + settlement_fee
        + stamp_duty
        + trading_fee
        + sfc_levy
        + frc_levy,
        6,
    )


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
