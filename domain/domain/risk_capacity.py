from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Callable, Mapping

from domain.domain.decision_state_fingerprint import canonical_sha256


@dataclass(frozen=True)
class SellPutCashCapacity:
    accepted: bool
    basis: str | None
    reason: str
    cash_required: float | None
    cash_free: float | None
    max_new_contracts: int


@dataclass(frozen=True)
class SellCallShareCapacity:
    accepted: bool
    reason: str
    shares_total: int
    shares_can_sell: int | None
    shares_eligible: int
    shares_locked: int
    shares_available_for_cover: int
    covered_contracts_available: int
    is_fully_covered_available: bool


@dataclass(frozen=True)
class SellPutEffectiveCash:
    available: bool
    native_currency: str
    cash_free: float | None
    reason: str
    skipped_positive_currencies: tuple[str, ...] = ()


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


def _normalized_currency_amounts(values: Mapping[str, Any] | None) -> dict[str, float] | None:
    if not isinstance(values, Mapping):
        return None
    normalized: dict[str, float] = {}
    for raw_currency, raw_amount in values.items():
        currency = str(raw_currency or "").strip().upper()
        if currency == "RMB":
            currency = "CNY"
        if not currency:
            continue
        amount = _to_float(raw_amount)
        if amount is None:
            return None
        normalized[currency] = normalized.get(currency, 0.0) + amount
    return normalized


def compute_sell_put_effective_cash(
    *,
    cash_by_currency: Mapping[str, Any] | None,
    cash_secured_by_currency: Mapping[str, Any] | None,
    native_currency: str,
    convert_currency: Callable[[float, str, str], float | None],
    cash_required_native: Any = None,
    fx_status: str | None = None,
) -> SellPutEffectiveCash:
    """Compute assignment capacity in the option's native currency.

    Existing short-put collateral is deducted in its own currency first. The
    candidate's native-currency pool is used at 100%; positive free cash in
    other currencies is used at 100% only when a usable FX observation exists.
    Missing/stale foreign-currency funds are excluded. They block the candidate
    only when the remaining known pool cannot cover one gross assignment.
    """

    native = str(native_currency or "").strip().upper()
    if native == "RMB":
        native = "CNY"
    if not native:
        return SellPutEffectiveCash(False, "", None, "native_currency_missing")
    cash = _normalized_currency_amounts(cash_by_currency)
    secured = _normalized_currency_amounts(cash_secured_by_currency or {})
    if cash is None or secured is None:
        return SellPutEffectiveCash(False, native, None, "cash_by_currency_invalid")
    if not cash:
        return SellPutEffectiveCash(False, native, None, "cash_by_currency_missing")

    required = _to_float(cash_required_native)
    if required is not None and required <= 0:
        return SellPutEffectiveCash(False, native, None, "cash_required_invalid")

    total_native = cash.get(native, 0.0) - secured.get(native, 0.0)
    usable_component_count = 1 if native in cash or native in secured else 0
    skipped_positive: list[str] = []
    fx_unavailable_label = (
        "fx_stale"
        if str(fx_status or "").strip().lower() in {"stale", "unavailable_stale"}
        else "fx_unavailable"
    )
    for currency in sorted(set(cash) | set(secured)):
        net_amount = cash.get(currency, 0.0) - secured.get(currency, 0.0)
        if currency == native:
            continue
        if net_amount == 0:
            continue
        converted = convert_currency(net_amount, currency, native)
        converted_value = _to_float(converted)
        if converted_value is None or (
            net_amount > 0 and converted_value <= 0
        ) or (
            net_amount < 0 and converted_value >= 0
        ):
            if net_amount < 0:
                return SellPutEffectiveCash(
                    False,
                    native,
                    None,
                    f"cross_currency_secured_cash_{fx_unavailable_label}:"
                    f"{currency}->{native}",
                )
            skipped_positive.append(currency)
            continue
        usable_component_count += 1
        total_native += converted_value

    if usable_component_count == 0:
        reason = (
            f"cross_currency_cash_{fx_unavailable_label}:"
            + ",".join(skipped_positive)
            if skipped_positive
            else "cash_capacity_unavailable"
        )
        return SellPutEffectiveCash(False, native, None, reason, tuple(skipped_positive))
    if skipped_positive and required is not None and total_native < required:
        return SellPutEffectiveCash(
            False,
            native,
            None,
            f"cross_currency_cash_{fx_unavailable_label}:"
            + ",".join(skipped_positive),
            tuple(skipped_positive),
        )
    if skipped_positive:
        reason = (
            f"known_cash_only_cross_currency_{fx_unavailable_label}:"
            + ",".join(skipped_positive)
        )
    elif usable_component_count > (1 if native in cash or native in secured else 0):
        reason = "cash_supported_by_same_currency_then_fx"
    else:
        reason = "cash_supported_by_same_currency"
    return SellPutEffectiveCash(
        True,
        native,
        max(0.0, total_native),
        reason,
        tuple(skipped_positive),
    )


def compute_sell_put_cash_capacity(
    *,
    cash_required_native: Any = None,
    cash_free_effective_native: Any = None,
    cash_native_currency: Any = None,
    cash_required_cny: Any = None,
    cash_free_cny: Any = None,
    cash_free_total_cny: Any = None,
    cash_required_usd: Any = None,
    cash_free_usd: Any = None,
) -> SellPutCashCapacity:
    """Decide whether a sell-put candidate has enough known cash headroom."""

    req_native = _to_float(cash_required_native)
    free_native = _to_float(cash_free_effective_native)
    native_currency = str(cash_native_currency or "").strip().upper()
    if req_native is not None and free_native is not None and native_currency:
        accepted = req_native <= free_native
        return SellPutCashCapacity(
            accepted=accepted,
            basis=f"same_currency_then_fx:{native_currency}",
            reason=("cash_supported" if accepted else "effective_native_cash_insufficient"),
            cash_required=req_native,
            cash_free=free_native,
            max_new_contracts=(max(0, math.floor(free_native / req_native)) if req_native > 0 else 0),
        )

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
            max_new_contracts=(max(0, math.floor(free_cny / req_cny)) if req_cny > 0 else 0),
        )

    if req_cny is not None and free_total_cny is not None:
        accepted = req_cny <= free_total_cny
        return SellPutCashCapacity(
            accepted=accepted,
            basis="total_cny",
            reason=("cash_supported" if accepted else "total_cny_cash_insufficient"),
            cash_required=req_cny,
            cash_free=free_total_cny,
            max_new_contracts=(max(0, math.floor(free_total_cny / req_cny)) if req_cny > 0 else 0),
        )

    if req_usd is not None and free_usd is not None:
        accepted = req_usd <= free_usd
        return SellPutCashCapacity(
            accepted=accepted,
            basis="usd",
            reason=("cash_supported" if accepted else "usd_cash_insufficient"),
            cash_required=req_usd,
            cash_free=free_usd,
            max_new_contracts=(max(0, math.floor(free_usd / req_usd)) if req_usd > 0 else 0),
        )

    return SellPutCashCapacity(
        accepted=False,
        basis=None,
        reason="cash_basis_missing",
        cash_required=None,
        cash_free=None,
        max_new_contracts=0,
    )


def compute_sell_call_share_capacity(
    *,
    shares_total: Any,
    shares_can_sell: Any = None,
    shares_locked: Any = 0,
    multiplier: Any,
    shares_available_for_cover: Any = None,
) -> SellCallShareCapacity:
    """Compute account share capacity for sell-call candidates."""

    total_value = _to_float(shares_total)
    total = _to_nonnegative_int(shares_total)
    can_sell_value = _to_float(shares_can_sell)
    can_sell = (
        _to_nonnegative_int(shares_can_sell)
        if can_sell_value is not None
        else None
    )
    locked = _to_nonnegative_int(shares_locked)
    explicit_available = _to_float(shares_available_for_cover)
    eligible = min(total, can_sell) if can_sell is not None else total
    available = eligible - locked

    def _result(reason: str) -> SellCallShareCapacity:
        return SellCallShareCapacity(
            accepted=False,
            reason=reason,
            shares_total=total,
            shares_can_sell=can_sell,
            shares_eligible=eligible,
            shares_locked=locked,
            shares_available_for_cover=max(0, available),
            covered_contracts_available=0,
            is_fully_covered_available=False,
        )

    if total_value is None or total_value < 0:
        return _result("shares_total_invalid")
    if can_sell_value is None or can_sell_value < 0:
        return _result("can_sell_qty_missing_or_invalid")
    if locked > eligible:
        return _result("locked_shares_exceed_eligible_underlying")
    if explicit_available is not None and (
        explicit_available < 0
        or float(int(explicit_available)) != explicit_available
        or int(explicit_available) != available
    ):
        return _result("share_capacity_facts_inconsistent")

    multiplier_v = _to_float(multiplier)
    multiplier_int = int(multiplier_v) if multiplier_v is not None else 0
    if (
        multiplier_v is None
        or multiplier_int <= 0
        or float(multiplier_int) != multiplier_v
    ):
        return _result("invalid_multiplier")

    covered_contracts = max(0, available) // multiplier_int
    accepted = covered_contracts >= 1
    return SellCallShareCapacity(
        accepted=accepted,
        reason=("share_capacity_supported" if accepted else "share_capacity_insufficient"),
        shares_total=total,
        shares_can_sell=can_sell,
        shares_eligible=eligible,
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


def revalidate_opening_share_coverage(
    coverage_fact: Mapping[str, Any],
    position_lots: list[Mapping[str, Any]],
    wheel_batches: list[Mapping[str, Any]],
    *,
    account: str,
    symbol: str,
) -> dict[str, Any]:
    """Refresh ledger-owned coverage inside the caller's current transaction."""

    account_value = str(account or "").strip().lower()
    symbol_value = str(symbol or "").strip().upper()
    fact = dict(coverage_fact or {})
    if (
        str(fact.get("account") or "").strip().lower() != account_value
        or str(fact.get("symbol") or "").strip().upper() != symbol_value
        or str(fact.get("status") or "").strip().lower() != "available"
    ):
        return {**fact, "status": "unavailable", "reason": "coverage_fact_invalid"}
    locked = 0
    for raw in position_lots:
        fields = raw.get("fields") if isinstance(raw.get("fields"), Mapping) else raw
        if (
            str(fields.get("account") or "").strip().lower() != account_value
            or str(fields.get("symbol") or "").strip().upper() != symbol_value
            or str(fields.get("option_type") or "").strip().lower() != "call"
            or str(fields.get("side") or fields.get("position_side") or "").strip().lower()
            != "short"
            or str(fields.get("status") or "").strip().lower() == "close"
        ):
            continue
        shares = compute_short_call_locked_shares(
            contracts_open=fields.get("contracts_open", fields.get("contracts")),
            contracts_total=fields.get("contracts"),
            multiplier=fields.get("multiplier"),
            underlying_share_locked=fields.get("underlying_share_locked"),
        )
        if shares is None:
            return {
                **fact,
                "status": "unavailable",
                "reason": "short_call_locked_shares_basis_missing",
            }
        locked += shares
    reserved = sum(
        int(batch.get("active_intent_reserved_shares") or 0)
        for batch in wheel_batches
        if str(batch.get("account") or "").strip().lower() == account_value
        and str(batch.get("symbol") or "").strip().upper() == symbol_value
        and str(batch.get("lifecycle_status") or "") == "active"
    )
    try:
        prior_locked = int(fact.get("shares_locked") or 0)
        prior_reserved = int(fact.get("shares_reserved") or 0)
        eligible = int(
            fact.get("shares_eligible")
            if fact.get("shares_eligible") is not None
            else int(fact.get("shares_available_for_cover"))
            + prior_locked
            + prior_reserved
        )
    except (TypeError, ValueError):
        return {**fact, "status": "unavailable", "reason": "shares_eligible_invalid"}
    status = "available" if min(eligible, locked, reserved) >= 0 else "unavailable"
    if locked + reserved > eligible:
        status = "unavailable"
    reason = None if status == "available" else "share_capacity_oversubscribed"
    identity = str(fact.get("capacity_identity_hash") or "").strip()
    if (
        prior_locked != locked
        or prior_reserved != reserved
    ):
        identity = canonical_sha256(
            {
                "prior_capacity_identity_hash": identity,
                "account": account_value,
                "symbol": symbol_value,
                "shares_eligible": eligible,
                "shares_locked": locked,
                "shares_reserved": reserved,
            }
        )
    return {
        **fact,
        "status": status,
        "reason": reason,
        "shares_locked": locked,
        "shares_reserved": reserved,
        "shares_available_for_cover": max(0, eligible - locked - reserved),
        "capacity_identity_hash": identity,
    }


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


def allocate_opening_share_capacity(
    coverage_facts: list[dict[str, Any]],
    claims: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Allocate one shared stock pool to Wheel first, then ordinary CC."""

    facts: dict[tuple[str, str], dict[str, Any]] = {}
    for raw in coverage_facts:
        account = str(raw.get("account") or "").strip().lower()
        symbol = str(raw.get("symbol") or "").strip().upper()
        if not account or not symbol:
            continue
        key = (account, symbol)
        facts[key] = dict(raw) if key not in facts else {"status": "unavailable"}

    def _positive_exact_int(value: Any) -> int:
        if isinstance(value, bool):
            return 0
        number = _to_float(value)
        if number is None or number <= 0 or not number.is_integer():
            return 0
        return int(number)

    prepared: list[dict[str, Any]] = []
    claim_indexes: dict[str, list[int]] = {}
    invalid_indexes: set[int] = set()
    for index, raw in enumerate(claims):
        row = dict(raw)
        account = str(row.get("account") or "").strip().lower()
        symbol = str(row.get("symbol") or "").strip().upper()
        claim_id = str(row.get("claim_id") or "").strip()
        multiplier = _positive_exact_int(row.get("multiplier"))
        requested = _positive_exact_int(row.get("requested_contracts"))
        assignment_at = _to_float(row.get("assignment_at_ms")) or 0.0
        if not account or not symbol or not claim_id or not multiplier or not requested:
            invalid_indexes.add(index)
        claim_indexes.setdefault(claim_id, []).append(index)
        prepared.append(
            {
                "row": row,
                "key": (account, symbol),
                "multiplier": multiplier,
                "requested": requested,
                "assignment_at": assignment_at,
            }
        )
    for claim_id, indexes in claim_indexes.items():
        if not claim_id or len(indexes) > 1:
            invalid_indexes.update(indexes)
    invalid_pools = {
        prepared[index]["key"]
        for index in invalid_indexes
        if all(prepared[index]["key"])
    }

    indexed = list(enumerate(prepared))
    indexed.sort(
        key=lambda item: (
            0
            if str(item[1]["row"].get("strategy_family") or "").lower() == "wheel"
            else 1,
            item[1]["assignment_at"],
            str(item[1]["row"].get("stock_lot_id") or ""),
            item[0],
        )
    )
    remaining: dict[tuple[str, str], int] = {}
    out_by_index: dict[int, dict[str, Any]] = {}
    for index, prepared_claim in indexed:
        row = dict(prepared_claim["row"])
        key = prepared_claim["key"]
        fact = facts.get(key)
        multiplier = int(prepared_claim["multiplier"])
        requested = int(prepared_claim["requested"])
        result = {
            **row,
            "requested_contracts": max(0, requested),
            "requested_shares": max(0, requested * multiplier),
            "granted_contracts": 0,
            "granted_shares": 0,
            "capacity_before": None,
            "capacity_after": None,
            "allocation_status": "blocked",
            "allocation_reason": "share_capacity_fact_unavailable",
        }
        if index in invalid_indexes:
            result["allocation_reason"] = "share_capacity_claim_invalid"
            out_by_index[index] = result
            continue
        if key in invalid_pools:
            result["allocation_reason"] = "share_capacity_pool_invalid"
            out_by_index[index] = result
            continue
        if (
            not isinstance(fact, Mapping)
            or str(fact.get("status") or "").lower() != "available"
        ):
            out_by_index[index] = result
            continue
        if key not in remaining:
            try:
                eligible = int(fact.get("shares_eligible"))
                occupied = int(fact.get("shares_locked"))
                reserved = int(fact.get("shares_reserved"))
            except (TypeError, ValueError):
                result["allocation_reason"] = "share_capacity_fact_invalid"
                out_by_index[index] = result
                continue
            if min(eligible, occupied, reserved) < 0 or occupied + reserved > eligible:
                result["allocation_reason"] = "share_capacity_oversubscribed"
                result["risk_level"] = "high"
                out_by_index[index] = result
                remaining[key] = 0
                continue
            remaining[key] = eligible - occupied - reserved
        before = remaining[key]
        granted = min(requested, before // multiplier)
        after = before - granted * multiplier
        remaining[key] = after
        result.update(
            granted_contracts=granted,
            granted_shares=granted * multiplier,
            capacity_before=before,
            capacity_after=after,
            allocation_status=("allocated" if granted else "blocked"),
            allocation_reason=(
                "share_capacity_supported"
                if granted == requested
                else "share_capacity_partially_supported"
                if granted
                else "share_capacity_insufficient"
            ),
        )
        out_by_index[index] = result
    return [out_by_index[index] for index in range(len(claims))]


def allocate_portfolio_capacity_shadow(ranked_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Greedily allocate existing ranked candidates without changing their rank."""

    scoped_rows = [
        {**row, "_capacity_scope": _capacity_scope(row)}
        for row in ranked_rows
    ]
    cash_pools = _consistent_pools(
        scoped_rows,
        key_fields=("account", "_capacity_scope"),
        value_fields=("cash_free_cny", "cash_free_total_cny"),
    )
    share_pools = _consistent_pools(
        scoped_rows,
        key_fields=("account", "_capacity_scope", "symbol"),
        value_fields=("shares_available_for_cover",),
    )
    selected_groups: set[tuple[str, str, str, str]] = set()
    out: list[dict[str, Any]] = []
    for rank, source in enumerate(ranked_rows, start=1):
        row = dict(source)
        account = str(row.get("account") or "").strip().lower()
        symbol = str(row.get("symbol") or "").strip().upper()
        capacity_scope = _capacity_scope(row)
        family = _strategy_family(row)
        group = (account, capacity_scope, symbol, family)
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
            pool_key = (account, capacity_scope)
            pool = cash_pools.get(pool_key)
            required = _to_float(row.get("cash_required_cny") or row.get("assignment_notional_cny"))
            unit = "CNY"
        else:
            pool_key = (account, capacity_scope, symbol.lower())
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


def _capacity_scope(row: Mapping[str, Any]) -> str:
    return str(
        row.get("capacity_identity_hash")
        or row.get("futu_account_id")
        or row.get("account")
        or ""
    ).strip().lower()


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
