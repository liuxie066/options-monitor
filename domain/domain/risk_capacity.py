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
class SellCallShareCapacity:
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


def compute_sell_call_share_capacity(
    *,
    shares_total: Any,
    shares_locked: Any = 0,
    multiplier: Any,
    shares_available_for_cover: Any = None,
) -> SellCallShareCapacity:
    """Compute account share capacity for sell-call candidates."""

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
        return SellCallShareCapacity(
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
    return SellCallShareCapacity(
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

    if open_contracts <= 0:
        return 0

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

    if open_contracts <= 0:
        return 0.0

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


def allocate_portfolio_capacity_shadow(ranked_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Greedily allocate existing ranked candidates without changing their rank."""

    cash_pools = _consistent_pools(
        ranked_rows,
        key_fields=("account",),
        value_fields=("cash_free_cny", "cash_free_total_cny"),
    )
    share_pools = _consistent_pools(
        ranked_rows,
        key_fields=("account", "symbol"),
        value_fields=("shares_available_for_cover",),
    )
    selected_groups: set[tuple[str, str, str]] = set()
    out: list[dict[str, Any]] = []
    for rank, source in enumerate(ranked_rows, start=1):
        row = dict(source)
        account = str(row.get("account") or "").strip().lower()
        symbol = str(row.get("symbol") or "").strip().upper()
        family = _strategy_family(row)
        group = (account, symbol, family)
        result = {
            **row,
            "allocation_rank": rank,
            "strategy_family": family,
            "allocation_status": "not_evaluable",
            "allocation_reason": "strategy_family_missing",
            "allocated_contracts": 0,
            "capacity_before": None,
            "capacity_required": None,
            "capacity_after": None,
            "capacity_unit": None,
        }
        if not account or not symbol or family not in {"sell_put", "covered_call"}:
            out.append(result)
            continue
        if group in selected_groups:
            result.update(
                allocation_status="alternative_not_allocated",
                allocation_reason="primary_candidate_already_allocated",
            )
            out.append(result)
            continue

        contracts = max(1, _to_nonnegative_int(row.get("contracts") or row.get("contract_count") or 1))
        if family == "sell_put":
            pool_key = (account,)
            pool = cash_pools.get(pool_key)
            required = _to_float(row.get("cash_required_cny") or row.get("assignment_notional_cny"))
            unit = "CNY"
        else:
            pool_key = (account, symbol.lower())
            pool = share_pools.get(pool_key)
            multiplier = _to_float(row.get("multiplier"))
            required = multiplier * contracts if multiplier is not None and multiplier > 0 else None
            unit = "shares"
        result.update(capacity_before=pool, capacity_required=required, capacity_unit=unit)
        if pool is None:
            result["allocation_reason"] = "capacity_pool_missing_or_inconsistent"
        elif required is None or required <= 0:
            result["allocation_reason"] = "candidate_capacity_requirement_missing"
        elif required > pool:
            result.update(
                allocation_status="capacity_blocked",
                allocation_reason="portfolio_capacity_insufficient",
                capacity_after=pool,
            )
        else:
            remaining = max(0.0, pool - required)
            if family == "sell_put":
                cash_pools[pool_key] = remaining
            else:
                share_pools[pool_key] = remaining
            selected_groups.add(group)
            result.update(
                allocation_status="allocated",
                allocation_reason="portfolio_capacity_supported",
                allocated_contracts=contracts,
                capacity_after=remaining,
            )
        out.append(result)
    return out


def _strategy_family(row: dict[str, Any]) -> str:
    value = str(row.get("strategy_family") or "").strip().lower()
    if value in {"sell_put", "covered_call"}:
        return value
    option_type = str(row.get("option_type") or row.get("mode") or "").strip().lower()
    return "sell_put" if option_type == "put" else "covered_call" if option_type == "call" else ""


def _consistent_pools(
    rows: list[dict[str, Any]],
    *,
    key_fields: tuple[str, ...],
    value_fields: tuple[str, ...],
) -> dict[tuple[str, ...], float | None]:
    values: dict[tuple[str, ...], list[float]] = {}
    for row in rows:
        key = tuple(str(row.get(field) or "").strip().lower() for field in key_fields)
        if not all(key):
            continue
        value = next(
            (
                parsed
                for field in value_fields
                if (parsed := _to_float(row.get(field))) is not None
            ),
            None,
        )
        if value is not None and value >= 0:
            values.setdefault(key, []).append(value)
    out: dict[tuple[str, ...], float | None] = {}
    for key, items in values.items():
        first = items[0]
        out[key] = first if all(abs(item - first) <= 1e-6 for item in items[1:]) else None
    return out
