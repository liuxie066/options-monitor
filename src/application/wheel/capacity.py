from __future__ import annotations

from typing import Any, Mapping, Sequence

from domain.domain.cash_secured_utils import normalize_cash_secured_total_by_ccy
from domain.domain.decision_state_fingerprint import canonical_sha256
from domain.domain.risk_capacity import (
    allocate_opening_share_capacity,
    allocate_wheel_put_cash_capacity,
    withdraw_opening_share_capacity_grants,
)
from src.application.futu_portfolio_context import fetch_futu_portfolio_context
from src.application.positions.context_builder import build_context
from src.application.wheel.read_model import build_wheel_read_model_from_rows


WHEEL_PUT_CASH_CAPACITY_FACT_SCHEMA = "wheel_put_cash_capacity_fact.v1"


def _normalized_fx_snapshot(fx_snapshot: Mapping[str, Any]) -> dict[str, Any]:
    raw_facts = fx_snapshot.get("fx_rate_facts")
    if not isinstance(raw_facts, (list, tuple)):
        return dict(fx_snapshot)
    facts: list[dict[str, Any]] = []
    for raw in raw_facts:
        if isinstance(raw, Mapping):
            facts.append(dict(raw))
            continue
        normalized_payload = getattr(raw, "normalized_payload", None)
        if callable(normalized_payload):
            facts.append(dict(normalized_payload()))
    return {**dict(fx_snapshot), "fx_rate_facts": facts}


def build_shared_cash_capacity_fact(
    *,
    account: str,
    portfolio_context: Mapping[str, Any],
    option_context: Mapping[str, Any],
    wheel_read_model: Mapping[str, Any],
    fx_snapshot: Mapping[str, Any],
) -> dict[str, Any]:
    """Bind canonical physical cash, ledger collateral, intents, and frozen FX."""

    account_value = str(account or "").strip().lower()
    authority = portfolio_context.get("capacity_authority")
    authority = dict(authority) if isinstance(authority, Mapping) else {}
    cash_by_currency = portfolio_context.get("cash_by_currency")
    cash_by_currency = (
        dict(cash_by_currency) if isinstance(cash_by_currency, Mapping) else None
    )
    try:
        cash_secured = normalize_cash_secured_total_by_ccy(dict(option_context))
    except (TypeError, ValueError):
        cash_secured = None
    reservations: list[dict[str, Any]] = []
    reservations_invalid = False
    for branch in wheel_read_model.get("wheel_branches") or []:
        if (
            not isinstance(branch, Mapping)
            or str(branch.get("direction") or "").strip().lower() != "put"
        ):
            continue
        raw_reservations = branch.get("active_intent_reservations") or []
        if not isinstance(raw_reservations, list):
            reservations_invalid = True
            continue
        for raw in raw_reservations:
            if not isinstance(raw, Mapping):
                reservations_invalid = True
                continue
            reservations.append(dict(raw))
    authority_account = str(
        authority.get("logical_account") or authority.get("account") or account_value
    ).strip().lower()
    status = "available"
    reason = None
    if not account_value or authority_account != account_value:
        status, reason = "unavailable", "cash_authority_account_mismatch"
    elif str(authority.get("status") or "").strip().lower() != "available":
        status, reason = "unavailable", "cash_authority_unavailable"
    elif not cash_by_currency:
        status, reason = "unavailable", "cash_by_currency_missing"
    elif cash_secured is None:
        status, reason = "unavailable", "cash_secured_positions_unavailable"
    elif reservations_invalid:
        status, reason = "unavailable", "wheel_put_intent_reservations_unavailable"
    elif not isinstance(fx_snapshot, Mapping):
        status, reason = "unavailable", "fx_snapshot_unavailable"
    cash_authority_hash = str(
        portfolio_context.get("capacity_identity_hash") or ""
    ).strip() or canonical_sha256(authority)
    normalized_fx = _normalized_fx_snapshot(fx_snapshot)
    identity_payload = {
        "schema_version": WHEEL_PUT_CASH_CAPACITY_FACT_SCHEMA,
        "account": account_value,
        "cash_authority": authority,
        "cash_authority_hash": cash_authority_hash,
        "cash_by_currency": cash_by_currency,
        "cash_secured_by_currency": cash_secured,
        "wheel_intent_reservations": reservations,
        "fx_snapshot": normalized_fx,
        "status": status,
        "reason": reason,
    }
    return {
        **identity_payload,
        "capacity_identity_hash": canonical_sha256(identity_payload),
        "fx_snapshot_hash": canonical_sha256(identity_payload["fx_snapshot"]),
    }


def load_shared_cash_capacity_fact(
    repo: Any,
    *,
    config: dict[str, Any],
    account: str,
    broker: str,
    as_of_ms: int,
    fx_snapshot: Mapping[str, Any],
) -> dict[str, Any]:
    account_value = str(account or "").strip().lower()
    try:
        rows = repo.read_lifecycle_account_rows(account=account_value)
        wheel_model = build_wheel_read_model_from_rows(
            rows,
            account=account_value,
            as_of_ms=max(int(as_of_ms), 1),
        )
        portfolio = fetch_futu_portfolio_context(cfg=config, account=account_value)
        option_context = build_context(
            repo.list_position_lots(),
            broker=str(broker or "futu"),
            account=account_value,
        )
        return build_shared_cash_capacity_fact(
            account=account_value,
            portfolio_context=portfolio,
            option_context=option_context,
            wheel_read_model=wheel_model,
            fx_snapshot=fx_snapshot,
        )
    except Exception as exc:
        reason = f"{type(exc).__name__}:{exc}"
        return {
            "schema_version": WHEEL_PUT_CASH_CAPACITY_FACT_SCHEMA,
            "account": account_value,
            "status": "unavailable",
            "reason": reason,
            "cash_authority": {},
            "cash_authority_hash": canonical_sha256({"reason": reason}),
            "cash_by_currency": None,
            "cash_secured_by_currency": None,
            "wheel_intent_reservations": [],
            "fx_snapshot": _normalized_fx_snapshot(fx_snapshot),
            "fx_snapshot_hash": canonical_sha256(_normalized_fx_snapshot(fx_snapshot)),
            "capacity_identity_hash": canonical_sha256(
                {"account": account_value, "reason": reason}
            ),
        }


def _ordinary_put_cash_claims(
    opening_put_candidates: Sequence[Mapping[str, Any]],
    *,
    account: str,
) -> list[dict[str, Any]]:
    claims: list[dict[str, Any]] = []
    for index, candidate in enumerate(opening_put_candidates):
        contracts = int(
            candidate.get("allocated_contracts")
            or candidate.get("contracts")
            or candidate.get("contract_count")
            or 1
        )
        claims.append(
            {
                "claim_id": f"ordinary-put:{index}",
                "strategy_family": "sell_put",
                "account": account,
                "symbol": str(candidate.get("symbol") or "").strip().upper(),
                "currency": str(
                    candidate.get("currency")
                    or candidate.get("cash_native_currency")
                    or ""
                ).strip().upper(),
                "strike": candidate.get("strike"),
                "multiplier": candidate.get("multiplier"),
                "requested_contracts": contracts,
            }
        )
    return claims


def _frozen_fx_converter(fx_snapshot: Mapping[str, Any]) -> Any:
    raw_rates = fx_snapshot.get("rates")
    rates = raw_rates if isinstance(raw_rates, Mapping) else fx_snapshot
    fact_rates: dict[tuple[str, str], tuple[tuple[int, int, int, str], float]] = {}
    raw_facts = fx_snapshot.get("fx_rate_facts")
    if isinstance(raw_facts, (list, tuple)):
        for raw in raw_facts:
            row = raw if isinstance(raw, Mapping) else {}
            base = str(row.get("base_currency") or "").strip().upper()
            quote = str(row.get("quote_currency") or "").strip().upper()
            try:
                rate_value = float(row.get("rate"))
                order = (
                    int(row.get("effective_at_ms") or 0),
                    int(row.get("revision") or 1),
                    int(row.get("observed_at_ms") or 0),
                    str(row.get("fact_id") or ""),
                )
            except (TypeError, ValueError):
                continue
            if not base or not quote or rate_value <= 0:
                continue
            current = fact_rates.get((base, quote))
            if current is None or order > current[0]:
                fact_rates[(base, quote)] = (order, rate_value)

    def convert(amount: float, source: str, target: str) -> float | None:
        source_value = str(source or "").strip().upper()
        target_value = str(target or "").strip().upper()
        if source_value == target_value:
            return float(amount)
        fact_rate = fact_rates.get((source_value, target_value))
        reverse_fact_rate = fact_rates.get((target_value, source_value))
        if fact_rate is not None:
            return float(amount) * fact_rate[1]
        if reverse_fact_rate is not None:
            return float(amount) / reverse_fact_rate[1]
        direct_keys = (
            f"{source_value}{target_value}",
            f"{source_value}/{target_value}",
            f"{source_value}-{target_value}",
        )
        reverse_keys = (
            f"{target_value}{source_value}",
            f"{target_value}/{source_value}",
            f"{target_value}-{source_value}",
        )
        rate = next((rates.get(key) for key in direct_keys if key in rates), None)
        reverse = next((rates.get(key) for key in reverse_keys if key in rates), None)
        if isinstance(rate, Mapping):
            rate = rate.get("rate")
        if isinstance(reverse, Mapping):
            reverse = reverse.get("rate")
        try:
            if rate is not None and float(rate) > 0:
                return float(amount) * float(rate)
            if reverse is not None and float(reverse) > 0:
                return float(amount) / float(reverse)
        except (TypeError, ValueError):
            return None
        return None

    return convert


def _convert_currency_fn(
    exchange_rate_converter: Any,
    *,
    fx_snapshot: Mapping[str, Any] | None = None,
) -> Any:
    if callable(exchange_rate_converter):
        return exchange_rate_converter
    converter = getattr(exchange_rate_converter, "convert", None)
    if callable(converter):
        return lambda amount, source, target: converter(
            amount,
            from_ccy=source,
            to_ccy=target,
        )
    if isinstance(fx_snapshot, Mapping):
        return _frozen_fx_converter(fx_snapshot)
    raise TypeError("Wheel Put capacity requires exchange_rate_converter")


def revalidate_selected_wheel_put_candidate(
    *,
    cash_capacity_fact: Mapping[str, Any],
    final_candidate: Mapping[str, Any],
    opening_put_candidates: Sequence[Mapping[str, Any]] = (),
    exchange_rate_converter: Any = None,
) -> dict[str, Any]:
    """Re-grant one selected Put against current facts before an intent write."""

    account = str(cash_capacity_fact.get("account") or "").strip().lower()
    requested = int(final_candidate.get("granted_contracts") or 0)
    claim_id = str(
        final_candidate.get("claim_id")
        or f"wheel:put:{final_candidate.get('wheel_branch_id') or ''}"
    ).strip()
    result = allocate_wheel_put_cash_capacity(
        cash_capacity_fact=cash_capacity_fact,
        ordinary_put_claims=_ordinary_put_cash_claims(
            opening_put_candidates,
            account=account,
        ),
        active_wheel_put_intents=[
            dict(item)
            for item in cash_capacity_fact.get("wheel_intent_reservations") or []
            if isinstance(item, Mapping)
        ],
        wheel_put_claims=[
            {
                "claim_id": claim_id,
                "strategy_family": "wheel",
                "account": account,
                "symbol": final_candidate.get("symbol"),
                "wheel_branch_id": final_candidate.get("wheel_branch_id"),
                "currency": final_candidate.get("cash_reservation_currency")
                or final_candidate.get("currency"),
                "strike": final_candidate.get("strike"),
                "multiplier": final_candidate.get("multiplier"),
                "requested_contracts": requested,
            }
        ],
        convert_currency=_convert_currency_fn(
            exchange_rate_converter,
            fx_snapshot=(
                cash_capacity_fact.get("fx_snapshot")
                if isinstance(cash_capacity_fact.get("fx_snapshot"), Mapping)
                else None
            ),
        ),
    )
    allocation = dict((result.get("allocations") or [{}])[0])
    if (
        allocation.get("allocation_status") != "allocated"
        or int(allocation.get("granted_contracts") or 0) != requested
    ):
        raise ValueError("Wheel Put candidate no longer has cash capacity")
    expected_hash = str(final_candidate.get("capacity_identity_hash") or "").strip()
    if expected_hash and allocation.get("capacity_identity_hash") != expected_hash:
        raise ValueError("Wheel Put cash capacity facts changed")
    return allocation


def revalidate_selected_wheel_put_candidate_from_rows(
    *,
    account: str,
    portfolio_context: Mapping[str, Any],
    position_lots: list[Mapping[str, Any]],
    lifecycle_rows: Mapping[str, Any],
    broker: str,
    as_of_ms: int,
    fx_snapshot: Mapping[str, Any],
    final_candidate: Mapping[str, Any],
    opening_put_candidates: Sequence[Mapping[str, Any]] = (),
    exchange_rate_converter: Any = None,
) -> dict[str, Any]:
    """Rebuild SQLite-owned reservations and re-grant one Put candidate."""

    account_value = str(account or "").strip().lower()
    wheel_model = build_wheel_read_model_from_rows(
        lifecycle_rows,
        account=account_value,
        as_of_ms=max(int(as_of_ms), 1),
    )
    option_context = build_context(
        list(position_lots),
        broker=str(broker or "futu"),
        account=account_value,
    )
    fact = build_shared_cash_capacity_fact(
        account=account_value,
        portfolio_context=portfolio_context,
        option_context=option_context,
        wheel_read_model=wheel_model,
        fx_snapshot=fx_snapshot,
    )
    return revalidate_selected_wheel_put_candidate(
        cash_capacity_fact=fact,
        final_candidate=final_candidate,
        opening_put_candidates=opening_put_candidates,
        exchange_rate_converter=exchange_rate_converter,
    )


def load_shared_coverage_fact(
    repo: Any,
    *,
    config: dict[str, Any],
    account: str,
    symbol: str,
    broker: str,
    as_of_ms: int,
    source_identity: str = "",
) -> dict[str, Any]:
    account_value = str(account or "").strip().lower()
    symbol_value = str(symbol or "").strip().upper()
    try:
        rows = repo.read_lifecycle_account_rows(account=account_value)
        wheel_model = build_wheel_read_model_from_rows(
            rows,
            account=account_value,
            as_of_ms=max(int(as_of_ms), 1),
        )
        portfolio = fetch_futu_portfolio_context(cfg=config, account=account_value)
        option_context = build_context(
            repo.list_position_lots(),
            broker=str(broker or "futu"),
            account=account_value,
        )
        matches = [
            row
            for row in build_shared_coverage_facts(
                account=account_value,
                portfolio_context=portfolio,
                option_context=option_context,
                wheel_read_model=wheel_model,
            )
            if row.get("symbol") == symbol_value
        ]
        if len(matches) != 1:
            raise ValueError("wheel_coverage_fact_not_unique")
        return matches[0]
    except Exception as exc:
        reason = f"{type(exc).__name__}:{exc}"
        return {
            "account": account_value,
            "symbol": symbol_value,
            "status": "unavailable",
            "reason": reason,
            "shares_available_for_cover": 0,
            "capacity_identity_hash": canonical_sha256(
                {
                    "account": account_value,
                    "symbol": symbol_value,
                    "source_identity": str(source_identity or ""),
                    "reason": reason,
                }
            ),
        }


def build_shared_coverage_facts(
    *,
    account: str,
    portfolio_context: Mapping[str, Any],
    option_context: Mapping[str, Any],
    wheel_read_model: Mapping[str, Any],
) -> list[dict[str, Any]]:
    stocks = portfolio_context.get("stocks_by_symbol")
    stocks = stocks if isinstance(stocks, Mapping) else {}
    locked_by_symbol = option_context.get("locked_shares_by_symbol")
    locked_by_symbol = locked_by_symbol if isinstance(locked_by_symbol, Mapping) else {}
    locked_unavailable = option_context.get("locked_shares_unavailable_by_symbol")
    locked_unavailable = locked_unavailable if isinstance(locked_unavailable, Mapping) else {}
    reserved: dict[str, int] = {}
    for batch in wheel_read_model.get("batches") or []:
        if not isinstance(batch, Mapping) or batch.get("lifecycle_status") != "active":
            continue
        symbol = str(batch.get("symbol") or "").strip().upper()
        reserved[symbol] = reserved.get(symbol, 0) + int(
            batch.get("active_intent_reserved_shares") or 0
        )
    facts: list[dict[str, Any]] = []
    for symbol in sorted(set(stocks) | set(locked_by_symbol) | set(reserved)):
        stock = stocks.get(symbol)
        status = "available"
        reason = None
        try:
            if not isinstance(stock, Mapping):
                raise ValueError("holding_missing")
            shares_total = int(stock.get("shares"))
            shares_can_sell = int(stock.get("can_sell_qty"))
            shares_locked = int(locked_by_symbol.get(symbol, 0))
            shares_reserved = int(reserved.get(symbol, 0))
            if min(shares_total, shares_can_sell, shares_locked, shares_reserved) < 0:
                raise ValueError("holding_invalid")
            if str(option_context.get("locked_shares_status") or "") != "available":
                raise ValueError("short_call_coverage_unavailable")
            if symbol in locked_unavailable:
                raise ValueError(str(locked_unavailable[symbol]))
        except (TypeError, ValueError) as exc:
            shares_total = shares_can_sell = shares_locked = shares_reserved = 0
            status = "unavailable"
            reason = str(exc)
        eligible = min(shares_total, shares_can_sell)
        identity = canonical_sha256(
            {
                "account": account,
                "symbol": symbol,
                "shares_total": shares_total,
                "shares_can_sell": shares_can_sell,
                "shares_locked": shares_locked,
                "shares_reserved": shares_reserved,
                "source_observed_at": portfolio_context.get("source_observed_at"),
                "ledger_generation_sha256": (
                    (option_context.get("prepared_authority") or {}).get(
                        "ledger_generation_sha256"
                    )
                    if isinstance(option_context.get("prepared_authority"), Mapping)
                    else None
                ),
            }
        )
        facts.append(
            {
                "account": account,
                "symbol": symbol,
                "status": status,
                "reason": reason,
                "shares_total": shares_total,
                "shares_can_sell": shares_can_sell,
                "shares_eligible": eligible,
                "shares_locked": shares_locked,
                "shares_reserved": shares_reserved,
                "shares_available_for_cover": max(
                    0, eligible - shares_locked - shares_reserved
                ),
                "capacity_identity_hash": identity,
            }
        )
    return facts


def _final_wheel_candidate(
    *,
    account: str,
    batch: Mapping[str, Any],
    internal_candidates: list[dict[str, Any]],
    allocation: Mapping[str, Any] | None,
    coverage_facts: list[dict[str, Any]],
) -> dict[str, Any] | None:
    granted = int((allocation or {}).get("granted_contracts") or 0)
    if not internal_candidates or granted <= 0:
        return None
    top = internal_candidates[0]
    grant_evaluations = top.get("_grant_evaluations")
    if isinstance(grant_evaluations, Mapping):
        exact = grant_evaluations.get(str(granted))
    else:
        exact = top if int(top.get("contracts") or 0) == granted else None
    if not isinstance(exact, Mapping) or exact.get("accepted") is not True:
        return None
    return {
        **{key: value for key, value in top.items() if key != "_grant_evaluations"},
        **dict(exact),
        "account": account,
        "stock_lot_id": str(batch.get("stock_lot_id") or ""),
        "wheel_branch_id": str(
            batch.get("wheel_branch_id") or batch.get("stock_lot_id") or ""
        ),
        "direction": "call",
        "final_candidate_id": top["candidate_id"],
        "requested_contracts": int((allocation or {}).get("requested_contracts") or 0),
        "granted_contracts": granted,
        "granted_shares": int((allocation or {}).get("granted_shares") or 0),
        "capacity_identity_hash": next(
            (
                row["capacity_identity_hash"]
                for row in coverage_facts
                if row.get("account") == account
                and row.get("symbol") == batch.get("symbol")
            ),
            None,
        ),
    }


def finalize_wheel_capacity(
    *,
    account: str,
    wheel_read_model: Mapping[str, Any],
    wheel_scan: Mapping[str, Any],
    opening_call_candidates: list[dict[str, Any]],
    coverage_facts: list[dict[str, Any]],
) -> dict[str, Any]:
    ordinary_claims: list[dict[str, Any]] = []
    seen_symbols: set[str] = set()
    for index, candidate in enumerate(opening_call_candidates):
        symbol = str(candidate.get("symbol") or "").strip().upper()
        if not symbol or symbol in seen_symbols:
            continue
        seen_symbols.add(symbol)
        ordinary_claims.append(
            {
                "claim_id": f"covered_call:{symbol}",
                "strategy_family": "covered_call",
                "account": account,
                "symbol": symbol,
                "candidate_id": str(
                    candidate.get("candidate_id")
                    or candidate.get("contract_symbol")
                    or candidate.get("code")
                    or index
                ),
                "requested_contracts": int(
                    candidate.get("max_new_contracts")
                    or candidate.get("covered_contracts_available")
                    or 1
                ),
                "multiplier": int(float(candidate.get("multiplier") or 0)),
            }
        )
    wheel_claims = [
        dict(item)
        for item in wheel_scan.get("capacity_claims") or []
        if isinstance(item, Mapping)
    ]
    allocations = allocate_opening_share_capacity(
        coverage_facts,
        ordinary_claims + wheel_claims,
    )
    by_claim = {str(row.get("claim_id") or ""): row for row in allocations}
    branch_rows = wheel_read_model.get("wheel_branches") or wheel_read_model.get("batches") or []
    batches_by_id = {
        str(row.get("stock_lot_id") or ""): dict(row)
        for row in branch_rows
        if isinstance(row, Mapping)
        and str(row.get("direction") or "call").strip().lower() == "call"
    }
    raw_by_batch = wheel_scan.get("raw_candidates")
    raw_by_batch = raw_by_batch if isinstance(raw_by_batch, Mapping) else {}
    rejected_claim_ids: set[str] = set()
    for stock_lot_id, batch in batches_by_id.items():
        branch_id = str(batch.get("wheel_branch_id") or stock_lot_id)
        claim_id = f"wheel:call:{branch_id}"
        allocation = by_claim.get(claim_id) or by_claim.get(f"wheel:{stock_lot_id}")
        internal_candidates = [
            dict(item)
            for item in raw_by_batch.get(stock_lot_id) or []
            if isinstance(item, Mapping)
        ]
        if (
            int((allocation or {}).get("granted_contracts") or 0) > 0
            and _final_wheel_candidate(
                account=account,
                batch=batch,
                internal_candidates=internal_candidates,
                allocation=allocation,
                coverage_facts=coverage_facts,
            )
            is None
        ):
            rejected_claim_ids.add(
                claim_id if claim_id in by_claim else f"wheel:{stock_lot_id}"
            )
    allocations = withdraw_opening_share_capacity_grants(
        allocations,
        rejected_claim_ids,
    )
    by_claim = {str(row.get("claim_id") or ""): row for row in allocations}
    snapshot_batches: list[dict[str, Any]] = []
    for scope in wheel_scan.get("scope_results") or []:
        if not isinstance(scope, Mapping):
            continue
        stock_lot_id = str(scope.get("stock_lot_id") or "")
        batch = batches_by_id.get(stock_lot_id)
        if batch is None:
            continue
        internal_candidates = [
            dict(item)
            for item in raw_by_batch.get(stock_lot_id) or []
            if isinstance(item, Mapping)
        ]
        raw_candidates = [
            {key: value for key, value in item.items() if key != "_grant_evaluations"}
            for item in internal_candidates
        ]
        branch_id = str(batch.get("wheel_branch_id") or stock_lot_id)
        allocation = by_claim.get(f"wheel:call:{branch_id}") or by_claim.get(
            f"wheel:{stock_lot_id}"
        )
        granted = int((allocation or {}).get("granted_contracts") or 0)
        source_scope = next(
            (
                item
                for item in wheel_scan.get("scope_results") or []
                if isinstance(item, Mapping)
                and str(item.get("stock_lot_id") or "") == stock_lot_id
            ),
            {},
        )
        final = _final_wheel_candidate(
            account=account,
            batch=batch,
            internal_candidates=internal_candidates,
            allocation=allocation,
            coverage_facts=coverage_facts,
        )
        snapshot_batches.append(
            {
                "account": account,
                "symbol": str(batch.get("symbol") or "").upper(),
                "stock_lot_id": stock_lot_id,
                "wheel_branch_id": branch_id,
                "direction": "call",
                "branch_generation_hash": batch.get("branch_generation_hash")
                or batch.get("batch_generation_hash"),
                "batch_generation_hash": batch.get("batch_generation_hash")
                or batch.get("branch_generation_hash"),
                "projection_hash": batch.get("projection_hash"),
                "shares_remaining": int(batch.get("shares_remaining") or 0),
                "phase": batch.get("phase"),
                "candidate_status": source_scope.get("status"),
                "reason_code": (
                    (allocation or {}).get("allocation_reason")
                    if raw_candidates and granted <= 0
                    else "wheel_capacity_grant_candidate_rejected"
                    if raw_candidates and granted > 0 and final is None
                    else source_scope.get("reason_code")
                ),
                "raw_candidates": raw_candidates,
                "allocation": allocation,
                "granted_contracts": granted,
                "final_candidate": final,
            }
        )
    scopes: list[dict[str, Any]] = []
    for symbol in sorted({str(row.get("symbol") or "").upper() for row in snapshot_batches}):
        symbol_batches = [row for row in snapshot_batches if row["symbol"] == symbol]
        source_scopes = [
            row
            for row in wheel_scan.get("scope_results") or []
            if isinstance(row, Mapping) and str(row.get("symbol") or "").upper() == symbol
        ]
        raw_count = sum(len(row.get("raw_candidates") or []) for row in symbol_batches)
        statuses = {str(row.get("status") or "") for row in source_scopes}
        partial_data = any(
            str(row.get("reason_code") or "") == "partial_data"
            for row in source_scopes
        )
        failed_reasons = {
            str(row.get("reason_code") or "").strip()
            for row in source_scopes
            if str(row.get("status") or "") == "failed"
            and str(row.get("reason_code") or "").strip()
        }
        if statuses == {"completed"}:
            status = "completed"
            reason = "partial_data" if partial_data else None if raw_count else "no_candidate"
        elif statuses == {"not_applicable"}:
            status = "not_applicable"
            reason = str(source_scopes[0].get("reason_code") or "wheel_not_applicable")
        elif "completed" in statuses:
            status = "completed"
            reason = "partial_data"
        elif "failed" in statuses:
            status = "failed"
            reason = (
                next(iter(failed_reasons))
                if statuses == {"failed"} and len(failed_reasons) == 1
                else "wheel_integrity_conflict"
            )
        else:
            status, reason = "unavailable", "wheel_candidate_data_unavailable"
        scopes.append(
            {
                "scope": "strategy",
                "account": account,
                "symbol": symbol,
                "direction": "call",
                "strategy_family": "wheel",
                "strategy_mode": "wheel",
                "candidate_owner": "wheel",
                "status": status,
                "reason_code": reason,
                "candidate_count": raw_count,
            }
        )
    return {
        "coverage_facts": coverage_facts,
        "allocations": allocations,
        "scope_results": scopes,
        "batches": snapshot_batches,
        "allocation_hash": canonical_sha256(allocations),
    }


def _final_wheel_put_candidate(
    *,
    account: str,
    branch: Mapping[str, Any],
    internal_candidates: list[dict[str, Any]],
    allocation: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    granted = int((allocation or {}).get("granted_contracts") or 0)
    if not internal_candidates or granted <= 0:
        return None
    top = internal_candidates[0]
    grant_evaluations = top.get("_grant_evaluations")
    exact = (
        grant_evaluations.get(str(granted))
        if isinstance(grant_evaluations, Mapping)
        else top if int(top.get("contracts") or 0) == granted else None
    )
    if not isinstance(exact, Mapping) or exact.get("accepted") is not True:
        return None
    currency = str((allocation or {}).get("cash_reservation_currency") or "").upper()
    reserved_amount = float((allocation or {}).get("cash_reservation_amount") or 0)
    if (
        currency != str(exact.get("cash_reservation_currency") or "").upper()
        or round(reserved_amount, 6)
        != round(float(exact.get("cash_reservation_amount") or 0), 6)
    ):
        return None
    branch_id = str(branch.get("wheel_branch_id") or "")
    return {
        **{key: value for key, value in top.items() if key != "_grant_evaluations"},
        **dict(exact),
        "account": account,
        "wheel_branch_id": branch_id,
        "direction": "put",
        "final_candidate_id": top["candidate_id"],
        "requested_contracts": int((allocation or {}).get("requested_contracts") or 0),
        "granted_contracts": granted,
        "capacity_identity_hash": (allocation or {}).get("capacity_identity_hash"),
        "allocation_input_hash": (allocation or {}).get("allocation_input_hash"),
        "cash_reservation_amount": reserved_amount,
        "cash_reservation_currency": currency,
        "branch_generation_hash": branch.get("branch_generation_hash"),
    }


def finalize_wheel_put_capacity(
    *,
    account: str,
    wheel_read_model: Mapping[str, Any],
    wheel_scan: Mapping[str, Any],
    opening_put_candidates: list[dict[str, Any]],
    cash_capacity_fact: Mapping[str, Any],
    exchange_rate_converter: Any,
) -> dict[str, Any]:
    """Allocate one account-wide cash pool after immutable ordinary Put claims."""

    account_value = str(account or "").strip().lower()
    allocation_result = allocate_wheel_put_cash_capacity(
        cash_capacity_fact=cash_capacity_fact,
        ordinary_put_claims=_ordinary_put_cash_claims(
            opening_put_candidates,
            account=account_value,
        ),
        active_wheel_put_intents=[
            dict(item)
            for item in cash_capacity_fact.get("wheel_intent_reservations") or []
            if isinstance(item, Mapping)
        ],
        wheel_put_claims=[
            dict(item)
            for item in wheel_scan.get("capacity_claims") or []
            if isinstance(item, Mapping)
        ],
        convert_currency=_convert_currency_fn(
            exchange_rate_converter,
            fx_snapshot=(
                cash_capacity_fact.get("fx_snapshot")
                if isinstance(cash_capacity_fact.get("fx_snapshot"), Mapping)
                else None
            ),
        ),
    )
    allocations = [
        dict(item)
        for item in allocation_result.get("allocations") or []
        if isinstance(item, Mapping)
    ]
    by_branch = {
        str(row.get("wheel_branch_id") or ""): row for row in allocations
    }
    branches = {
        str(row.get("wheel_branch_id") or ""): dict(row)
        for row in wheel_read_model.get("wheel_branches") or []
        if isinstance(row, Mapping)
        and str(row.get("direction") or "").strip().lower() == "put"
    }
    raw_by_branch = wheel_scan.get("raw_candidates")
    raw_by_branch = raw_by_branch if isinstance(raw_by_branch, Mapping) else {}
    snapshot_batches: list[dict[str, Any]] = []
    branch_scopes: list[dict[str, Any]] = []
    for raw_scope in wheel_scan.get("scope_results") or []:
        if not isinstance(raw_scope, Mapping):
            continue
        scope = dict(raw_scope)
        branch_id = str(scope.get("wheel_branch_id") or "")
        branch = branches.get(branch_id)
        if branch is None:
            continue
        internal_candidates = [
            dict(item)
            for item in raw_by_branch.get(branch_id) or []
            if isinstance(item, Mapping)
        ]
        raw_candidates = [
            {key: value for key, value in item.items() if key != "_grant_evaluations"}
            for item in internal_candidates
        ]
        allocation = by_branch.get(branch_id)
        granted = int((allocation or {}).get("granted_contracts") or 0)
        final = _final_wheel_put_candidate(
            account=account_value,
            branch=branch,
            internal_candidates=internal_candidates,
            allocation=allocation,
        )
        reason = scope.get("reason_code")
        if raw_candidates and granted <= 0:
            reason = (allocation or {}).get("allocation_reason")
        elif raw_candidates and final is None:
            reason = "wheel_capacity_grant_candidate_rejected"
        snapshot_batches.append(
            {
                "account": account_value,
                "symbol": str(branch.get("symbol") or "").upper(),
                "wheel_branch_id": branch_id,
                "direction": "put",
                "branch_generation_hash": branch.get("branch_generation_hash"),
                "projection_hash": branch.get("projection_hash"),
                "phase": branch.get("phase"),
                "candidate_status": scope.get("status"),
                "reason_code": reason,
                "raw_candidates": raw_candidates,
                "allocation": allocation,
                "granted_contracts": granted,
                "final_candidate": final,
            }
        )
        branch_scopes.append(
            {
                **scope,
                "direction": "put",
                "candidate_count": len(raw_candidates),
                "reason_code": reason,
            }
        )
    scope_results: list[dict[str, Any]] = []
    symbols = sorted(
        {str(row.get("symbol") or "").strip().upper() for row in branch_scopes}
    )
    for symbol in symbols:
        rows = [
            row
            for row in branch_scopes
            if str(row.get("symbol") or "").strip().upper() == symbol
        ]
        statuses = {str(row.get("status") or "") for row in rows}
        reasons = {str(row.get("reason_code") or "") for row in rows}
        failed_reasons = {
            str(row.get("reason_code") or "").strip()
            for row in rows
            if str(row.get("status") or "") == "failed"
            and str(row.get("reason_code") or "").strip()
        }
        candidate_count = sum(int(row.get("candidate_count") or 0) for row in rows)
        if statuses == {"completed"}:
            status = "completed"
            reason = "partial_data" if "partial_data" in reasons else None
            if candidate_count == 0 and reason is None:
                reason = "no_candidate"
        elif statuses == {"not_applicable"}:
            status = "not_applicable"
            reason = next(iter(reasons)) if len(reasons) == 1 else "wheel_not_applicable"
        elif "completed" in statuses:
            status, reason = "completed", "partial_data"
        elif "failed" in statuses:
            status = "failed"
            reason = (
                next(iter(failed_reasons))
                if statuses == {"failed"} and len(failed_reasons) == 1
                else "wheel_integrity_conflict"
            )
        else:
            status, reason = "unavailable", "wheel_candidate_data_unavailable"
        scope_results.append(
            {
                "scope": "strategy",
                "account": account_value,
                "symbol": symbol,
                "direction": "put",
                "strategy_family": "wheel",
                "strategy_mode": "wheel",
                "candidate_owner": "wheel",
                "status": status,
                "reason_code": reason,
                "candidate_count": candidate_count,
            }
        )
    return {
        "cash_capacity_fact": dict(cash_capacity_fact),
        "allocations": allocations,
        "scope_results": scope_results,
        "batches": snapshot_batches,
        "capacity_identity_hash": allocation_result.get("capacity_identity_hash"),
        "allocation_input_hash": allocation_result.get("allocation_input_hash"),
        "allocation_hash": canonical_sha256(allocations),
    }


def finalize_wheel_bidirectional_capacity(
    *,
    account: str,
    wheel_read_model: Mapping[str, Any],
    call_scan: Mapping[str, Any],
    put_scan: Mapping[str, Any],
    opening_call_candidates: list[dict[str, Any]],
    opening_put_candidates: list[dict[str, Any]],
    coverage_facts: list[dict[str, Any]],
    cash_capacity_fact: Mapping[str, Any],
    exchange_rate_converter: Any,
) -> dict[str, Any]:
    call_result = finalize_wheel_capacity(
        account=account,
        wheel_read_model=wheel_read_model,
        wheel_scan=call_scan,
        opening_call_candidates=opening_call_candidates,
        coverage_facts=coverage_facts,
    )
    put_result = finalize_wheel_put_capacity(
        account=account,
        wheel_read_model=wheel_read_model,
        wheel_scan=put_scan,
        opening_put_candidates=opening_put_candidates,
        cash_capacity_fact=cash_capacity_fact,
        exchange_rate_converter=exchange_rate_converter,
    )
    scopes = sorted(
        [*call_result["scope_results"], *put_result["scope_results"]],
        key=lambda row: (
            str(row.get("symbol") or ""),
            str(row.get("direction") or ""),
        ),
    )
    batches = sorted(
        [*call_result["batches"], *put_result["batches"]],
        key=lambda row: (
            str(row.get("wheel_branch_id") or ""),
            str(row.get("direction") or ""),
        ),
    )
    allocations = [*call_result["allocations"], *put_result["allocations"]]
    return {
        "coverage_facts": call_result["coverage_facts"],
        "cash_capacity_fact": put_result["cash_capacity_fact"],
        "share_allocations": call_result["allocations"],
        "cash_allocations": put_result["allocations"],
        "allocations": allocations,
        "scope_results": scopes,
        "batches": batches,
        "allocation_hash": canonical_sha256(
            {
                "share": call_result["allocation_hash"],
                "cash": put_result["allocation_hash"],
            }
        ),
    }


__all__ = [
    "WHEEL_PUT_CASH_CAPACITY_FACT_SCHEMA",
    "build_shared_cash_capacity_fact",
    "build_shared_coverage_facts",
    "finalize_wheel_bidirectional_capacity",
    "finalize_wheel_capacity",
    "finalize_wheel_put_capacity",
    "load_shared_cash_capacity_fact",
    "load_shared_coverage_fact",
    "revalidate_selected_wheel_put_candidate",
    "revalidate_selected_wheel_put_candidate_from_rows",
]
