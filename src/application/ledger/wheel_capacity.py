"""Adapt qualified cash and ledger facts to the shared domain capacity rules."""
from __future__ import annotations
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence
from domain.domain.cash_secured_utils import normalize_cash_secured_total_by_ccy
from domain.domain.decision_state_fingerprint import canonical_sha256
from domain.domain.risk_capacity import allocate_wheel_put_cash_capacity
from domain.domain.portfolio_scope import portfolio_scope_id
from src.application.portfolio_context_service import (
    portfolio_position_snapshot_errors, cash_snapshot_is_usable, cash_snapshot_evidence,
)
from src.application.ledger.position_context import build_position_risk_context as build_context
from src.application.ledger.assigned_stock_projection import build_wheel_read_model_from_rows
from src.application.ledger.decision_snapshot import decision_state_snapshot_from_locked_rows
from src.application.ledger.read_only_evidence import open_position_query_repo
from src.application.ledger.repository_core import with_sqlite_repo_writer_lock
from src.infrastructure.exchange_rates import capacity_currency_converter as _frozen_fx_converter

WHEEL_PUT_CASH_CAPACITY_FACT_SCHEMA = "wheel_put_cash_capacity_fact.v1"


def read_wheel_capacity_context(
    repo: Any, *, account: str, broker: str, as_of_ms: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Freeze Wheel and option capacity from one ledger read and Inbox evidence.

    The existing intake lock binds the ledger read to the sidecar Inbox. SQLite
    uses the canonical query-only reader: no initialization, repair, or provider
    access. Only derived contexts cross the ledger boundary.
    """
    candidate = getattr(repo, "primary_repo", repo)
    reader = open_position_query_repo(candidate.db_path)
    with with_sqlite_repo_writer_lock(candidate):
        rows = reader.read_lifecycle_account_rows(account=account)
        decision_snapshot = decision_state_snapshot_from_locked_rows(
            candidate, rows, account=account,
            portfolio_scope_id=portfolio_scope_id(account),
            source_observed_at=datetime.now(timezone.utc).isoformat(),
            current_decision_now_ms=as_of_ms,
        )
    wheel_model = build_wheel_read_model_from_rows(
        rows, account=account, as_of_ms=max(int(as_of_ms), 1),
    )
    option_context = build_context(
        rows["stored_position_lots"], broker=str(broker or "futu"), account=account,
        decision_snapshot=decision_snapshot, lifecycle_now_ms=as_of_ms,
    )
    return wheel_model, option_context


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



def broker_capacity_observation_is_fresh(
    observed_at: Any, *, account: str | None = None, market: str | None = None,
) -> bool:
    """Requalify holdings evidence at the new decision time, independently of cash TTL."""
    evidence = observed_at.get("position_evidence") if isinstance(observed_at, Mapping) else None
    return isinstance(evidence, Mapping) and not portfolio_position_snapshot_errors(
        evidence, account=account, market=market, now_utc=datetime.now(timezone.utc),
    )



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
    unavailable = option_context.get("cash_secured_unavailable_by_symbol")
    unavailable_valid = unavailable is None or isinstance(unavailable, Mapping)
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
    for condition, unavailable_reason in (
        (not account_value or authority_account != account_value, "cash_authority_account_mismatch"),
        (str(authority.get("status") or "").strip().lower() != "available", "cash_authority_unavailable"),
        (not cash_by_currency, "cash_by_currency_missing"),
        (not cash_snapshot_is_usable(portfolio_context), "cash_snapshot_unavailable"),
        (cash_secured is None, "cash_secured_positions_unavailable"),
        (option_context.get("context_status") not in (None, "available") or
         option_context.get("decision_snapshot_status") != "trusted", "option_decision_snapshot_unavailable"),
        (not unavailable_valid or bool(unavailable), "option_cash_secured_unavailable"),
        (reservations_invalid, "wheel_put_intent_reservations_unavailable"),
        (not isinstance(fx_snapshot, Mapping), "fx_snapshot_unavailable"),
    ):
        if condition:
            status, reason = "unavailable", unavailable_reason
            break
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
        "source_observed_at": portfolio_context.get("cash_source_observed_at"),
        "cash_evidence": cash_snapshot_evidence(portfolio_context),
        "decision_state_fingerprint": option_context.get("decision_state_fingerprint"),
        "wheel_intent_reservations": reservations,
        "fx_snapshot": normalized_fx,
        "status": status,
        "reason": reason,
    }
    return {
        **identity_payload,
        "capacity_identity_hash": canonical_sha256({key: value for key, value in identity_payload.items() if key not in {"status", "reason"}}),
        "cash_snapshot": portfolio_context.get("cash_snapshot"),
        "fx_snapshot_hash": canonical_sha256(identity_payload["fx_snapshot"]),
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



def _convert_currency_fn(
    exchange_rate_converter: Any,
    *,
    fx_snapshot: Mapping[str, Any] | None = None,
) -> Any:
    if isinstance(fx_snapshot, Mapping):
        return _frozen_fx_converter(fx_snapshot)
    if callable(exchange_rate_converter):
        return exchange_rate_converter
    converter = getattr(exchange_rate_converter, "convert", None)
    if callable(converter):
        return lambda amount, source, target: converter(
            amount,
            from_ccy=source,
            to_ccy=target,
        )
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
    decision_snapshot: Mapping[str, Any] | None = None,
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
        decision_snapshot=dict(decision_snapshot or {}),
        lifecycle_now_ms=as_of_ms,
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
