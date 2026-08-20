from __future__ import annotations

from typing import Any, Mapping

from domain.domain.decision_state_fingerprint import canonical_sha256
from domain.domain.risk_capacity import allocate_opening_share_capacity
from src.application.futu_portfolio_context import fetch_futu_portfolio_context
from src.application.positions.context_builder import build_context
from src.application.wheel.read_model import build_wheel_read_model_from_rows


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
    batches_by_id = {
        str(row.get("stock_lot_id") or ""): dict(row)
        for row in wheel_read_model.get("batches") or []
        if isinstance(row, Mapping)
    }
    raw_by_batch = wheel_scan.get("raw_candidates")
    raw_by_batch = raw_by_batch if isinstance(raw_by_batch, Mapping) else {}
    snapshot_batches: list[dict[str, Any]] = []
    for scope in wheel_scan.get("scope_results") or []:
        if not isinstance(scope, Mapping):
            continue
        stock_lot_id = str(scope.get("stock_lot_id") or "")
        batch = batches_by_id.get(stock_lot_id)
        if batch is None:
            continue
        raw_candidates = [
            dict(item)
            for item in raw_by_batch.get(stock_lot_id) or []
            if isinstance(item, Mapping)
        ]
        allocation = by_claim.get(f"wheel:{stock_lot_id}")
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
        final = None
        if raw_candidates and granted > 0:
            final = {
                **raw_candidates[0],
                "account": account,
                "stock_lot_id": stock_lot_id,
                "final_candidate_id": raw_candidates[0]["candidate_id"],
                "requested_contracts": int(
                    (allocation or {}).get("requested_contracts") or 0
                ),
                "granted_contracts": granted,
                "granted_shares": int(
                    (allocation or {}).get("granted_shares") or 0
                ),
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
        snapshot_batches.append(
            {
                "account": account,
                "symbol": str(batch.get("symbol") or "").upper(),
                "stock_lot_id": stock_lot_id,
                "batch_generation_hash": batch.get("batch_generation_hash"),
                "projection_hash": batch.get("projection_hash"),
                "shares_remaining": int(batch.get("shares_remaining") or 0),
                "phase": batch.get("phase"),
                "candidate_status": source_scope.get("status"),
                "reason_code": (
                    (allocation or {}).get("allocation_reason")
                    if raw_candidates and granted <= 0
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
        if statuses == {"completed"}:
            status = "completed"
            reason = None if raw_count else "no_candidate"
        elif statuses == {"not_applicable"}:
            status = "not_applicable"
            reason = str(source_scopes[0].get("reason_code") or "wheel_not_applicable")
        elif "completed" in statuses:
            status = "completed"
            reason = "partial_data"
        elif "failed" in statuses:
            status, reason = "failed", "wheel_integrity_conflict"
        else:
            status, reason = "unavailable", "wheel_candidate_data_unavailable"
        scopes.append(
            {
                "scope": "strategy",
                "account": account,
                "symbol": symbol,
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


__all__ = [
    "build_shared_coverage_facts",
    "finalize_wheel_capacity",
    "load_shared_coverage_fact",
]
