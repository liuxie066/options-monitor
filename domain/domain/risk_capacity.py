from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SellPutCashCapacity:
    accepted: bool
    basis: str | None
    reason: str
    cash_required: float | None
    cash_free: float | None


@dataclass(frozen=True)
class CoveredCallShareCapacity:
    accepted: bool
    reason: str
    shares_total: int
    shares_locked: int
    shares_available_for_cover: int
    covered_contracts_available: int
    is_fully_covered_available: bool


def _to_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        v = float(value)
    except Exception:
        return None
    try:
        if v != v:
            return None
    except Exception:
        return None
    return v


def _to_nonnegative_int(value: Any) -> int:
    v = _to_float(value)
    if v is None:
        return 0
    return max(0, int(v))


def compute_sell_put_cash_capacity(
    *,
    cash_required_cny: Any = None,
    cash_free_cny: Any = None,
    cash_free_total_cny: Any = None,
    cash_required_usd: Any = None,
    cash_free_usd: Any = None,
) -> SellPutCashCapacity:
    """Decide whether a sell-put candidate has enough known cash headroom."""

    req_cny = _to_float(cash_required_cny)
    free_cny = _to_float(cash_free_cny)
    free_total_cny = _to_float(cash_free_total_cny)
    req_usd = _to_float(cash_required_usd)
    free_usd = _to_float(cash_free_usd)

    if req_cny is not None and free_cny is not None:
        accepted = req_cny <= free_cny
        return SellPutCashCapacity(
            accepted=accepted,
            basis="base_cny",
            reason=("cash_supported" if accepted else "base_cny_cash_insufficient"),
            cash_required=req_cny,
            cash_free=free_cny,
        )

    if req_cny is not None and free_total_cny is not None:
        accepted = req_cny <= free_total_cny
        return SellPutCashCapacity(
            accepted=accepted,
            basis="total_cny",
            reason=("cash_supported" if accepted else "total_cny_cash_insufficient"),
            cash_required=req_cny,
            cash_free=free_total_cny,
        )

    if req_usd is not None and free_usd is not None:
        accepted = req_usd <= free_usd
        return SellPutCashCapacity(
            accepted=accepted,
            basis="usd",
            reason=("cash_supported" if accepted else "usd_cash_insufficient"),
            cash_required=req_usd,
            cash_free=free_usd,
        )

    return SellPutCashCapacity(
        accepted=False,
        basis=None,
        reason="cash_basis_missing",
        cash_required=None,
        cash_free=None,
    )


def compute_covered_call_share_capacity(
    *,
    shares_total: Any,
    shares_locked: Any = 0,
    multiplier: Any,
    shares_available_for_cover: Any = None,
) -> CoveredCallShareCapacity:
    """Compute account share capacity for covered-call candidates."""

    total = _to_nonnegative_int(shares_total)
    locked = _to_nonnegative_int(shares_locked)
    explicit_available = _to_float(shares_available_for_cover)
    if explicit_available is None:
        available = max(0, total - locked)
    else:
        available = max(0, int(explicit_available))

    multiplier_v = _to_float(multiplier)
    multiplier_int = int(multiplier_v) if multiplier_v is not None else 0
    if multiplier_v is None or multiplier_int <= 0:
        return CoveredCallShareCapacity(
            accepted=False,
            reason="invalid_multiplier",
            shares_total=total,
            shares_locked=locked,
            shares_available_for_cover=available,
            covered_contracts_available=0,
            is_fully_covered_available=False,
        )

    covered_contracts = max(0, available) // multiplier_int
    accepted = covered_contracts >= 1
    return CoveredCallShareCapacity(
        accepted=accepted,
        reason=("share_capacity_supported" if accepted else "share_capacity_insufficient"),
        shares_total=total,
        shares_locked=locked,
        shares_available_for_cover=available,
        covered_contracts_available=covered_contracts,
        is_fully_covered_available=accepted,
    )


def compute_short_call_locked_shares(
    *,
    contracts_open: Any,
    multiplier: Any = None,
    underlying_share_locked: Any = None,
    contracts_total: Any = None,
) -> int | None:
    locked = _to_float(underlying_share_locked)
    open_contracts = _to_nonnegative_int(contracts_open)
    total_contracts = _to_nonnegative_int(contracts_total)

    if locked is not None:
        if total_contracts > 0 and open_contracts < total_contracts:
            locked = float(locked) / float(total_contracts) * float(open_contracts)
        return max(0, int(locked))

    multiplier_v = _to_float(multiplier)
    if multiplier_v is None or multiplier_v <= 0:
        return None
    return max(0, int(multiplier_v * open_contracts))


def compute_short_put_cash_secured(
    *,
    contracts_open: Any,
    contracts_total: Any = None,
    cash_secured_amount: Any = None,
    strike: Any = None,
    multiplier: Any = None,
) -> float | None:
    open_contracts = _to_nonnegative_int(contracts_open)
    total_contracts = _to_nonnegative_int(contracts_total)
    cash_secured = _to_float(cash_secured_amount)

    if cash_secured is None:
        strike_v = _to_float(strike)
        multiplier_v = _to_float(multiplier)
        if strike_v is None or multiplier_v is None or multiplier_v <= 0:
            return None
        basis_contracts = total_contracts if total_contracts > 0 else open_contracts
        cash_secured = strike_v * multiplier_v * float(basis_contracts)

    if total_contracts > 0 and open_contracts < total_contracts:
        cash_secured = float(cash_secured) / float(total_contracts) * float(open_contracts)
    return max(0.0, float(cash_secured))
