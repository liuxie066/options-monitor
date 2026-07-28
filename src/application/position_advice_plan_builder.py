from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping

from domain.domain.combo_identity import classify_combo_structure
from domain.domain.decision_state_fingerprint import canonical_sha256
from domain.domain.fee_calc import calc_futu_option_fee
from domain.domain.option_lifecycle import expiration_observation_start_ms
from domain.domain.position_advice import (
    ECONOMIC_MODEL,
    ShortOptionCarryInput,
    evaluate_short_option_switch,
    long_call_observable_facts,
)
from domain.domain.position_advice_allocator import (
    ALLOCATOR_VERSION,
    allocate_position_advice,
)
from domain.domain.risk_capacity import (
    compute_short_call_locked_shares,
    compute_short_put_cash_secured,
)
from domain.domain.symbol_identity import canonical_symbol, symbol_market
from src.application.ledger.api import position_lot_risk_view
from src.application.positions.context_builder import (
    build_lifecycle_read_models_from_decision_snapshot,
)


POSITION_ADVICE_PLAN_SCHEMA = "position_advice.output.v2"
POSITION_ADVICE_LEG_PLAN_SCHEMA = "leg_plan.v2"
ACTIONABLE_RECOMMENDATIONS = frozenset(
    {"roll", "replace", "reallocate", "review"}
)
BLOCKED_LIFECYCLE_STATES = frozenset(
    {
        "settlement_pending",
        "partially_resolved",
        "assigned",
        "exercised",
        "expired_unassigned",
        "resolved_mixed",
        "needs_review",
        "conflict",
    }
)


class PositionAdvicePlanError(RuntimeError):
    """Raised when an immutable v2 input cannot produce a coherent plan."""


def quote_rows_from_source_payloads(
    payloads: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Decode the exact required-data bytes adopted from quote receipts."""

    rows: list[dict[str, Any]] = []
    for raw in payloads:
        payload = dict(raw or {})
        if payload.get("schema_version") == "required_data_quote_snapshot.v1":
            encoded = str(payload.get("raw_json_base64") or "")
            try:
                decoded = base64.b64decode(encoded, validate=True)
                original = json.loads(decoded.decode("utf-8"))
            except (
                ValueError,
                UnicodeDecodeError,
                json.JSONDecodeError,
            ) as exc:
                raise PositionAdvicePlanError(
                    "adopted quote payload is unreadable"
                ) from exc
            source_rows = (
                original.get("rows") if isinstance(original, dict) else None
            )
        else:
            source_rows = payload.get("rows")
        if source_rows is None:
            source_rows = []
        if not isinstance(source_rows, list):
            raise PositionAdvicePlanError("quote rows must be a list")
        for item in source_rows:
            if not isinstance(item, dict):
                continue
            rows.append(
                {
                    **item,
                    "_position_advice_quote_snapshot_id": payload.get(
                        "_position_advice_quote_snapshot_id"
                    ),
                    "_position_advice_quote_as_of": payload.get(
                        "_position_advice_quote_as_of"
                    ),
                }
            )
    return rows


def build_position_advice_plan(
    *,
    immutable_input: Mapping[str, Any],
    candidate_decisions: Iterable[Mapping[str, Any]],
    quote_rows: Iterable[Mapping[str, Any]],
    cash_capacity: Mapping[str, Any],
    share_coverage: Mapping[str, Any],
    fx_payload: Mapping[str, Any],
    checked_at: datetime | str,
) -> dict[str, Any]:
    """Build the deterministic account-level v2 plan from one immutable input."""

    input_payload = dict(immutable_input or {})
    snapshot = dict(input_payload.get("decision_state_snapshot") or {})
    if (
        snapshot.get("snapshot_status") != "trusted"
        or snapshot.get("actionable") is not True
    ):
        raise PositionAdvicePlanError("decision snapshot is not trusted")
    fingerprint = _sha256(
        snapshot.get("decision_state_fingerprint"),
        "decision_state_fingerprint",
    )
    if fingerprint != input_payload.get("decision_state_fingerprint"):
        raise PositionAdvicePlanError("immutable input fingerprint mismatch")
    checked = _parse_timestamp(checked_at)

    decisions, candidate_errors = _validated_candidates(candidate_decisions)
    quotes = _quote_index(quote_rows)
    fx_to_cny = _fx_to_cny(fx_payload)
    positions = [
        dict(item)
        for item in snapshot.get("account_position_lots", [])
        if isinstance(item, Mapping)
    ]
    lifecycle_by_lot = build_lifecycle_read_models_from_decision_snapshot(
        snapshot,
        now_ms=int(checked.timestamp() * 1000),
    )
    group_states = _group_structure_states(
        snapshot=snapshot,
        positions=positions,
        lifecycle_by_lot=lifecycle_by_lot,
    )
    resource_pools = _resource_pools(
        input_payload=input_payload,
        cash_capacity=cash_capacity,
        share_coverage=share_coverage,
    )

    rows_by_id: dict[str, dict[str, Any]] = {}
    proposals: list[dict[str, Any]] = []
    for item in positions:
        view = position_lot_risk_view(item)
        if not view.record_id or not view.is_open or view.contracts_open <= 0:
            continue
        row, position_proposals = _position_row_and_proposals(
            view=view,
            checked_at=checked,
            fingerprint=fingerprint,
            input_payload=input_payload,
            lifecycle_by_lot=lifecycle_by_lot,
            group_states=group_states,
            decisions=decisions,
            candidate_errors=candidate_errors,
            quotes=quotes,
            fx_to_cny=fx_to_cny,
            cash_capacity=cash_capacity,
            share_coverage=share_coverage,
        )
        rows_by_id[view.record_id] = row
        proposals.extend(position_proposals)

    candidate_quantities: dict[str, int] = {}
    for proposal in proposals:
        candidate_id = str(proposal.get("candidate_id") or "")
        contracts = int(proposal.get("candidate_contracts") or 0)
        candidate_quantities[candidate_id] = max(
            candidate_quantities.get(candidate_id, 0),
            contracts,
        )
    allocation = allocate_position_advice(
        proposals=proposals,
        resource_pools=resource_pools,
        candidate_quantities=candidate_quantities,
    )
    selected_by_source = {
        source_id: dict(proposal)
        for proposal in allocation.selected
        for source_id in proposal.get("source_position_ids") or []
    }
    alternatives_by_source: dict[str, list[dict[str, Any]]] = {}
    for proposal in allocation.alternatives:
        for source_id in proposal.get("source_position_ids") or []:
            alternatives_by_source.setdefault(str(source_id), []).append(
                dict(proposal)
            )

    portfolio_plan_id = canonical_sha256(
        {
            "schema_version": POSITION_ADVICE_PLAN_SCHEMA,
            "decision_state_fingerprint": fingerprint,
            "selected_proposal_ids": [
                str(item.get("proposal_id") or "")
                for item in allocation.selected
            ],
            "resource_pools_before": allocation.resource_pools_before,
            "resource_pools_after": allocation.resource_pools_after,
        }
    )
    for position_id, row in rows_by_id.items():
        selected = selected_by_source.get(position_id)
        alternatives = alternatives_by_source.get(position_id, [])
        if selected is not None:
            _apply_selected_proposal(
                row,
                selected=selected,
                portfolio_plan_id=portfolio_plan_id,
                fingerprint=fingerprint,
                group_states=group_states,
                snapshot=snapshot,
            )
        elif alternatives and row.get("recommendation") == "hold":
            best = sorted(
                alternatives,
                key=lambda item: (
                    -_decimal_or_zero(item.get("net_carry_improvement_H")),
                    str(item.get("candidate_id") or ""),
                ),
            )[0]
            row["best_candidate"] = dict(best.get("candidate") or {})
            row["opening_decision_hash"] = best.get("opening_decision_hash")
            row["invariant_decision_hash"] = best.get(
                "invariant_decision_hash"
            )
            row["replacement_decision_hash"] = best.get(
                "replacement_decision_hash"
            )
            row["replacement_eligibility"] = best.get(
                "replacement_eligibility"
            )
            row["candidate_daily_carry"] = best.get(
                "candidate_daily_carry"
            )
            row["candidate_capital_efficiency"] = best.get(
                "candidate_capital_efficiency"
            )
            row["comparison_horizon_days"] = best.get(
                "comparison_horizon_days"
            )
            row["friction"] = best.get("friction")
            row["net_carry_improvement_H"] = best.get(
                "net_carry_improvement_H"
            )
            row["net_carry_improvement_H_base_cny"] = best.get(
                "net_carry_improvement_H_base_cny"
            )
            row["payback_days"] = best.get("payback_days")
            row["reason_codes"] = sorted(
                {
                    *row.get("reason_codes", []),
                    str(best.get("allocator_reason") or "not_selected"),
                }
            )
        _apply_authority_scope(row, input_payload=input_payload)
        row["portfolio_plan_id"] = portfolio_plan_id

    output_rows = sorted(
        rows_by_id.values(),
        key=lambda row: (
            int(row.get("execution_order") or 10**9),
            str(row.get("strategy_family") or ""),
            str(row.get("position_id") or ""),
        ),
    )
    source_manifest = [
        dict(item)
        for item in input_payload.get("source_manifest", [])
        if isinstance(item, Mapping)
    ]
    freshness = {
        "status": "fresh",
        "freshness_policy": input_payload.get("freshness_policy"),
        "source_expires_at": {
            str(item.get("source_kind")): item.get("expires_at")
            for item in source_manifest
        },
    }
    return {
        "schema_version": POSITION_ADVICE_PLAN_SCHEMA,
        "economic_model": ECONOMIC_MODEL,
        "allocator_version": ALLOCATOR_VERSION,
        "account_run_id": input_payload.get("account_run_id"),
        "broker": input_payload.get("broker"),
        "account": input_payload.get("account"),
        "normalized_account": input_payload.get("normalized_account"),
        "included_markets": list(input_payload.get("included_markets") or []),
        "portfolio_scope_id": input_payload.get("portfolio_scope_id"),
        "normalized_portfolio_source": input_payload.get(
            "normalized_portfolio_source"
        ),
        "portfolio_account_identity_hash": input_payload.get(
            "portfolio_account_identity_hash"
        ),
        "capacity_pool_authority_id": input_payload.get(
            "capacity_pool_authority_id"
        ),
        "authority_mode": input_payload.get("authority_mode"),
        "authority_generation": input_payload.get("authority_generation"),
        "authority_policy_hash": input_payload.get("authority_policy_hash"),
        "authority_resolution_status": input_payload.get(
            "authority_resolution_status"
        ),
        "authority_covered_strategy_families": list(
            input_payload.get("authority_covered_strategy_families") or []
        ),
        "decision_state_fingerprint": fingerprint,
        "decision_snapshot_status": snapshot.get("snapshot_status"),
        "input_hash": input_payload.get("input_hash"),
        "input_snapshot_ids": list(
            input_payload.get("input_snapshot_ids") or []
        ),
        "source_manifest_hash": input_payload.get("source_manifest_hash"),
        "source_manifest": source_manifest,
        "source_receipt_hashes": list(
            input_payload.get("source_receipt_hashes") or []
        ),
        "freshness": freshness,
        "advice_checked_at": _timestamp(checked),
        "portfolio_plan_id": portfolio_plan_id,
        "resource_pools_before": allocation.resource_pools_before,
        "resource_pools_after": allocation.resource_pools_after,
        "candidate_quantity_before": allocation.candidate_quantity_before,
        "candidate_quantity_after": allocation.candidate_quantity_after,
        "selected_proposals": [dict(item) for item in allocation.selected],
        "alternative_proposals": [
            dict(item) for item in allocation.alternatives
        ],
        "rows": output_rows,
    }


def _position_row_and_proposals(
    *,
    view: Any,
    checked_at: datetime,
    fingerprint: str,
    input_payload: Mapping[str, Any],
    lifecycle_by_lot: Mapping[str, Mapping[str, Any]],
    group_states: Mapping[str, Mapping[str, Any]],
    decisions: list[dict[str, Any]],
    candidate_errors: Mapping[str, tuple[str, ...]],
    quotes: Mapping[tuple[str, str, str, str], list[dict[str, Any]]],
    fx_to_cny: Mapping[str, Decimal],
    cash_capacity: Mapping[str, Any],
    share_coverage: Mapping[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    position_id = str(view.record_id)
    symbol = canonical_symbol(view.symbol) or str(view.symbol or "").strip().upper()
    option_type = str(view.option_type or "").strip().lower()
    side = str(view.side or "").strip().lower()
    leg_role = str(view.fields.get("leg_role") or "").strip().lower() or None
    group_id = (
        str(view.fields.get("strategy_group_id") or "").strip() or None
    )
    group_state = (
        dict(group_states.get(group_id) or {})
        if group_id
        else {"state": "standalone"}
    )
    lifecycle, lifecycle_reasons = _position_lifecycle(
        view=view,
        checked_at=checked_at,
        lifecycle_by_lot=lifecycle_by_lot,
    )
    strategy_family = _strategy_family(
        option_type=option_type,
        side=side,
        leg_role=leg_role,
    )
    row = _base_row(
        position_id=position_id,
        strategy_family=strategy_family,
        strategy_group_id=group_id,
        leg_role=leg_role,
        symbol=symbol,
        option_type=option_type,
        side=side,
        expiration=str(view.expiration_ymd or ""),
        strike=view.strike,
        contract_symbol=(
            str(
                view.fields.get("contract_symbol")
                or view.fields.get("contract_code")
                or view.fields.get("code")
                or ""
            )
            .strip()
            .upper()
        ),
        lifecycle_state=lifecycle,
        group_structure_state=str(group_state.get("state") or "standalone"),
        reason_codes=lifecycle_reasons,
    )
    if side != "short":
        if option_type == "call":
            _apply_long_call_facts(
                row,
                view=view,
                quote=_unique_quote_for_position(view, quotes),
            )
        return row, []
    if option_type not in {"put", "call"}:
        row["reason_codes"] = ["unsupported_short_option_type"]
        return row, []
    if lifecycle in BLOCKED_LIFECYCLE_STATES:
        if lifecycle in {"needs_review", "conflict"}:
            row["recommendation"] = "review"
            row["model_trade_actionable"] = False
            row["model_actionable"] = False
            row["human_review_required"] = True
            row["action_scope"] = "lifecycle_fact_review"
        row["reason_codes"] = sorted(
            {*row["reason_codes"], f"lifecycle_{lifecycle}"}
        )
        return row, []
    if group_id and group_state.get("state") in {
        "identity_unverified",
        "opening_incomplete",
        "partially_decomposed",
        "review_required",
    }:
        row["recommendation"] = "review"
        row["model_trade_actionable"] = False
        row["model_actionable"] = False
        row["human_review_required"] = True
        row["action_scope"] = "group_fact_review"
        row["reason_codes"] = sorted(
            {
                *row["reason_codes"],
                f"combo_{group_state.get('state')}",
            }
        )
        return row, []
    current_quote = _unique_quote_for_position(view, quotes)
    if current_quote is None:
        row["reason_codes"] = sorted(
            {*row["reason_codes"], "current_quote_missing_or_ambiguous"}
        )
        return row, []

    proposals: list[dict[str, Any]] = []
    decision_mode = "put" if option_type == "put" else "call"
    compatible = [
        item
        for item in decisions
        if item["strategy_mode"] == decision_mode
        and (
            option_type == "put"
            or (
                canonical_symbol(item["normalized_input"].get("symbol"))
                or str(item["normalized_input"].get("symbol") or "").upper()
            )
            == symbol
        )
    ]
    for decision in compatible:
        proposal = _proposal_for_candidate(
            view=view,
            lifecycle_state=lifecycle,
            group_state=str(group_state.get("state") or "standalone"),
            current_quote=current_quote,
            decision=decision,
            fingerprint=fingerprint,
            input_payload=input_payload,
            fx_to_cny=fx_to_cny,
            cash_capacity=cash_capacity,
            share_coverage=share_coverage,
            checked_at=checked_at,
        )
        if proposal is not None:
            proposals.append(proposal)
    if not proposals:
        errors = sorted(
            {
                reason
                for candidate_id, reasons in candidate_errors.items()
                for reason in reasons
                if any(
                    str(item.get("candidate_id") or "") == candidate_id
                    and str(item.get("strategy_mode") or "") == decision_mode
                    for item in decisions
                )
            }
        )
        row["reason_codes"] = sorted(
            {
                *row["reason_codes"],
                *(errors or ["no_economically_eligible_replacement"]),
            }
        )
    return row, proposals


def _proposal_for_candidate(
    *,
    view: Any,
    lifecycle_state: str,
    group_state: str,
    current_quote: Mapping[str, Any],
    decision: Mapping[str, Any],
    fingerprint: str,
    input_payload: Mapping[str, Any],
    fx_to_cny: Mapping[str, Decimal],
    cash_capacity: Mapping[str, Any],
    share_coverage: Mapping[str, Any],
    checked_at: datetime,
) -> dict[str, Any] | None:
    if lifecycle_state != "open":
        return None
    replacement = dict(
        decision.get("replacement_candidate_decision") or {}
    )
    eligibility = str(
        replacement.get("replacement_eligibility") or ""
    )
    if eligibility not in {
        "accepted_opening",
        "capacity_deferred_to_allocator",
    }:
        return None
    candidate = dict(decision.get("normalized_input") or {})
    contracts = int(view.contracts_open or 0)
    multiplier = _positive_decimal(view.multiplier)
    candidate_multiplier = _positive_decimal(candidate.get("multiplier"))
    if (
        contracts <= 0
        or multiplier is None
        or candidate_multiplier is None
        or multiplier != candidate_multiplier
    ):
        return None
    current_price = _quality_price(
        current_quote,
        side="ask",
        max_spread_ratio=(
            dict(decision.get("invariant_decision") or {})
            .get("risk_policy", {})
            .get("max_spread_ratio")
        ),
    )
    candidate_price = _quality_price(
        candidate,
        side="bid",
        max_spread_ratio=(
            dict(decision.get("invariant_decision") or {})
            .get("risk_policy", {})
            .get("max_spread_ratio")
        ),
    )
    if current_price is None or candidate_price is None:
        return None
    option_type = str(view.option_type or "").strip().lower()
    current_currency = str(
        current_quote.get("currency") or view.currency or ""
    ).strip().upper()
    candidate_currency = str(candidate.get("currency") or "").strip().upper()
    symbol = canonical_symbol(view.symbol) or str(view.symbol or "").upper()
    candidate_symbol = (
        canonical_symbol(candidate.get("symbol"))
        or str(candidate.get("symbol") or "").upper()
    )
    current_capacity: Decimal | None
    candidate_capacity: Decimal | None
    resource_delta: dict[str, Any]
    if option_type == "put":
        current_capacity = _current_put_capacity(view)
        candidate_capacity = (
            _positive_decimal(candidate.get("strike"))
            * candidate_multiplier
            * contracts
            if _positive_decimal(candidate.get("strike")) is not None
            else None
        )
        authority_id = str(
            input_payload.get("capacity_pool_authority_id") or ""
        )
        if (
            current_capacity is None
            or candidate_capacity is None
            or len(authority_id) != 64
            or cash_capacity.get("status") != "available"
        ):
            return None
        released = _to_cny(
            current_capacity,
            currency=current_currency,
            fx_to_cny=fx_to_cny,
        )
        required = _to_cny(
            candidate_capacity,
            currency=candidate_currency,
            fx_to_cny=fx_to_cny,
        )
        if released is None or required is None:
            return None
        resource_delta = {
            "resource_kind": "cash_base_cny",
            "pool_key": (
                f"cash:{input_payload.get('portfolio_scope_id')}:{authority_id}"
            ),
            "unit": "CNY",
            "released": _decimal_text(released),
            "required": _decimal_text(required),
        }
    else:
        if candidate_symbol != symbol:
            return None
        current_locked = compute_short_call_locked_shares(
            contracts_open=view.contracts_open,
            contracts_total=view.contracts,
            multiplier=view.multiplier,
            underlying_share_locked=view.underlying_share_locked,
        )
        candidate_required = candidate_multiplier * contracts
        if (
            current_locked is None
            or candidate_required != int(candidate_required)
            or int(current_locked) != int(candidate_required)
        ):
            return None
        coverage = _share_coverage_item(share_coverage, symbol)
        if (
            coverage.get("status") != "available"
            or coverage.get("avg_cost") in {None, ""}
            or not str(coverage.get("currency") or "").strip()
        ):
            return None
        current_capacity = Decimal(int(current_locked))
        candidate_capacity = Decimal(int(candidate_required))
        resource_delta = {
            "resource_kind": "covered_shares",
            "pool_key": (
                f"shares:{input_payload.get('broker')}:"
                f"{input_payload.get('account')}:{symbol}"
            ),
            "unit": "shares",
            "released": str(int(current_locked)),
            "required": str(int(candidate_required)),
        }
    current_dte = _nonnegative_int(
        current_quote.get("dte")
        if current_quote.get("dte") not in {None, ""}
        else _days_to_expiry(
            view.expiration_ymd,
            as_of_date=checked_at.date(),
        )
    )
    candidate_dte = _positive_int(candidate.get("dte"))
    current_spot = _nonnegative_decimal(current_quote.get("spot"))
    candidate_spot = _nonnegative_decimal(candidate.get("spot"))
    current_strike = _positive_decimal(view.strike)
    candidate_strike = _positive_decimal(candidate.get("strike"))
    if None in {
        current_dte,
        candidate_dte,
        current_spot,
        candidate_spot,
        current_strike,
        candidate_strike,
        current_capacity,
        candidate_capacity,
    }:
        return None
    try:
        close_fee = Decimal(
            str(
                calc_futu_option_fee(
                    current_currency,
                    float(current_price),
                    contracts=contracts,
                    multiplier=int(multiplier),
                    is_sell=False,
                )
            )
        )
        open_fee = Decimal(
            str(
                calc_futu_option_fee(
                    candidate_currency,
                    float(candidate_price),
                    contracts=contracts,
                    multiplier=int(candidate_multiplier),
                    is_sell=True,
                )
            )
        )
    except (TypeError, ValueError, OverflowError):
        return None
    same_symbol = candidate_symbol == symbol
    proposed_action = (
        "reallocate"
        if not same_symbol
        else "replace"
        if group_state == "active_combo"
        else "roll"
    )
    result = evaluate_short_option_switch(
        current=ShortOptionCarryInput(
            option_type=option_type,
            spot=current_spot,
            strike=current_strike,
            price=current_price,
            dte=int(current_dte),
            multiplier=multiplier,
            contracts=contracts,
            capacity=current_capacity,
            currency=current_currency,
        ),
        candidate=ShortOptionCarryInput(
            option_type=option_type,
            spot=candidate_spot,
            strike=candidate_strike,
            price=candidate_price,
            dte=int(candidate_dte),
            multiplier=candidate_multiplier,
            contracts=contracts,
            capacity=candidate_capacity,
            currency=candidate_currency,
        ),
        close_fee=close_fee,
        open_fee=open_fee,
        proposed_action=proposed_action,
        replacement_eligible=True,
        fx_to_cny={key: value for key, value in fx_to_cny.items()},
        fx_fresh=True,
        evidence_complete=True,
        allocator_selected=True,
    )
    economics = result.to_dict()
    if not result.actionable:
        return None
    net_native = result.net_carry_improvement_h
    net_base = (
        _to_cny(
            net_native,
            currency=str(result.comparison_currency or ""),
            fx_to_cny=fx_to_cny,
        )
        if net_native is not None
        else None
    )
    current_daily = result.current_daily_carry or Decimal("0")
    candidate_daily = result.candidate_daily_carry or Decimal("0")
    if resource_delta["resource_kind"] == "cash_base_cny":
        current_daily_base = _to_cny(
            current_daily,
            currency=str(result.comparison_currency or ""),
            fx_to_cny=fx_to_cny,
        )
        candidate_daily_base = _to_cny(
            candidate_daily,
            currency=str(result.comparison_currency or ""),
            fx_to_cny=fx_to_cny,
        )
        released_units = _positive_decimal(resource_delta["released"])
        required_units = _positive_decimal(resource_delta["required"])
        pool_efficiency = (
            candidate_daily_base / required_units
            - current_daily_base / released_units
            if current_daily_base is not None
            and candidate_daily_base is not None
            and released_units is not None
            and required_units is not None
            else None
        )
    else:
        current_daily_base = _to_cny(
            current_daily,
            currency=str(result.comparison_currency or ""),
            fx_to_cny=fx_to_cny,
        )
        candidate_daily_base = _to_cny(
            candidate_daily,
            currency=str(result.comparison_currency or ""),
            fx_to_cny=fx_to_cny,
        )
        released_units = _positive_decimal(resource_delta["released"])
        required_units = _positive_decimal(resource_delta["required"])
        pool_efficiency = (
            candidate_daily / required_units
            - current_daily / released_units
            if released_units is not None and required_units is not None
            else None
        )
    friction_base = _to_cny(
        result.switch_friction,
        currency=str(result.comparison_currency or ""),
        fx_to_cny=fx_to_cny,
    )
    if (
        net_base is None
        or pool_efficiency is None
        or current_daily_base is None
        or candidate_daily_base is None
        or friction_base is None
    ):
        return None
    proposal_payload = {
        "source_position_ids": [str(view.record_id)],
        "candidate_id": decision.get("candidate_id"),
        "candidate_contracts": contracts,
        "proposed_action": proposed_action,
        "resource_deltas": [resource_delta],
        "net_carry_improvement_H": economics[
            "net_carry_improvement_H"
        ],
        "net_carry_improvement_H_base_cny": _decimal_text(net_base),
        "pool_efficiency_improvement": _decimal_text(pool_efficiency),
        "payback_days": economics["payback_days"],
        "friction": economics["switch_friction"],
        "friction_base_cny": _decimal_text(friction_base),
        "current_daily_carry_base_cny": _decimal_text(
            current_daily_base
        ),
        "candidate_daily_carry_base_cny": _decimal_text(
            candidate_daily_base
        ),
        "evidence_status": "complete",
        "risk_eligibility_status": "accepted",
        "opening_decision_hash": replacement.get(
            "opening_decision_hash"
        ),
        "invariant_decision_hash": replacement.get(
            "invariant_decision_hash"
        ),
        "replacement_decision_hash": replacement.get(
            "replacement_decision_hash"
        ),
        "replacement_eligibility": eligibility,
        "candidate": candidate,
        "current_extrinsic": economics["current_extrinsic"],
        "current_daily_carry": economics["current_daily_carry"],
        "current_capital_efficiency": economics[
            "current_capital_efficiency"
        ],
        "candidate_extrinsic": economics["candidate_extrinsic"],
        "candidate_daily_carry": economics["candidate_daily_carry"],
        "candidate_capital_efficiency": economics[
            "candidate_capital_efficiency"
        ],
        "comparison_horizon_days": economics[
            "comparison_horizon_days"
        ],
        "comparison_currency": economics["comparison_currency"],
        "quote_as_of": max(
            str(current_quote.get("_position_advice_quote_as_of") or ""),
            str(
                _quote_as_of_for_snapshot(
                    input_payload,
                    decision.get("quote_snapshot_id"),
                )
                or ""
            ),
        )
        or None,
        "allocation_rank": _nonnegative_int(
            dict(decision.get("opening_decision") or {})
            .get("rank_key", {})
            .get("allocation_rank")
        )
        or 0,
        "decision_state_fingerprint": fingerprint,
    }
    proposal_id = canonical_sha256(
        {
            "schema_version": "position_advice_proposal.v2",
            **proposal_payload,
        }
    )
    return {"proposal_id": proposal_id, **proposal_payload}


def _apply_selected_proposal(
    row: dict[str, Any],
    *,
    selected: Mapping[str, Any],
    portfolio_plan_id: str,
    fingerprint: str,
    group_states: Mapping[str, Mapping[str, Any]],
    snapshot: Mapping[str, Any],
) -> None:
    row.update(
        {
            "recommendation": selected.get("proposed_action"),
            "model_trade_actionable": True,
            "model_actionable": True,
            "action_scope": (
                "combo_group"
                if row.get("group_structure_state") == "active_combo"
                else "position"
            ),
            "best_candidate": dict(selected.get("candidate") or {}),
            "opening_decision_hash": selected.get(
                "opening_decision_hash"
            ),
            "invariant_decision_hash": selected.get(
                "invariant_decision_hash"
            ),
            "replacement_decision_hash": selected.get(
                "replacement_decision_hash"
            ),
            "replacement_eligibility": selected.get(
                "replacement_eligibility"
            ),
            "current_extrinsic": selected.get("current_extrinsic"),
            "current_daily_carry": selected.get("current_daily_carry"),
            "current_capital_efficiency": selected.get(
                "current_capital_efficiency"
            ),
            "candidate_daily_carry": selected.get(
                "candidate_daily_carry"
            ),
            "candidate_capital_efficiency": selected.get(
                "candidate_capital_efficiency"
            ),
            "comparison_currency": selected.get("comparison_currency"),
            "comparison_horizon_days": selected.get(
                "comparison_horizon_days"
            ),
            "friction": selected.get("friction"),
            "friction_base_cny": selected.get("friction_base_cny"),
            "net_carry_improvement_H": selected.get(
                "net_carry_improvement_H"
            ),
            "net_carry_improvement_H_base_cny": selected.get(
                "net_carry_improvement_H_base_cny"
            ),
            "payback_days": selected.get("payback_days"),
            "resource_deltas": [
                dict(item) for item in selected.get("resource_deltas") or []
            ],
            "execution_order": selected.get("execution_order"),
            "depends_on": list(selected.get("depends_on") or []),
            "reason_codes": ["observable_carry_improved"],
            "quote_as_of": selected.get("quote_as_of"),
        }
    )
    if row.get("group_structure_state") != "active_combo":
        return
    group_id = str(row.get("strategy_group_id") or "")
    group = dict(group_states.get(group_id) or {})
    identity = dict(group.get("identity") or {})
    participation_call_id = str(
        identity.get("participation_call_record_id") or ""
    )
    candidate_id = str(selected.get("candidate_id") or "")
    contracts = int(selected.get("candidate_contracts") or 0)
    operations = [
        {
            "sequence": 1,
            "operation_type": "close_option",
            "target_position_id": row.get("position_id"),
            "target_candidate_id": None,
            "option_type": "put",
            "side": "short",
            "contracts": contracts,
            "strategy_after": None,
            "strategy_group_after": None,
            "depends_on": [],
            "requires_user_confirmation": True,
        },
        {
            "sequence": 2,
            "operation_type": "open_option",
            "target_position_id": None,
            "target_candidate_id": candidate_id,
            "option_type": "put",
            "side": "short",
            "contracts": contracts,
            "strategy_after": "sell_put",
            "strategy_group_after": None,
            "depends_on": [1],
            "requires_user_confirmation": True,
        },
        {
            "sequence": 3,
            "operation_type": "hold_option",
            "target_position_id": participation_call_id,
            "target_candidate_id": None,
            "option_type": "call",
            "side": "long",
            "contracts": contracts,
            "strategy_after": "participation_call",
            "strategy_group_after": group_id,
            "depends_on": [],
            "requires_user_confirmation": True,
        },
    ]
    plan_payload = {
        "schema_version": POSITION_ADVICE_LEG_PLAN_SCHEMA,
        "portfolio_plan_id": portfolio_plan_id,
        "decision_state_fingerprint": fingerprint,
        "group_id": group_id,
        "group_structure_before": "active_combo",
        "recommendation": selected.get("proposed_action"),
        "action_scope": "combo_group",
        "decomposes_group": True,
        "operations": operations,
        "total_friction": selected.get("friction"),
        "resource_deltas": [
            dict(item) for item in selected.get("resource_deltas") or []
        ],
        "reason_codes": [
            "funding_put_carry_improved",
            "combo_decomposes_on_leg_close",
            "new_put_is_independent",
        ],
    }
    plan_payload["plan_id"] = canonical_sha256(
        {
            "schema_version": POSITION_ADVICE_LEG_PLAN_SCHEMA,
            "decision_state_fingerprint": fingerprint,
            "ordered_operations": operations,
        }
    )
    row["leg_plan"] = plan_payload
    if selected.get("proposed_action") == "roll":
        row["recommendation"] = "replace"


def _apply_authority_scope(
    row: dict[str, Any],
    *,
    input_payload: Mapping[str, Any],
) -> None:
    mode = str(input_payload.get("authority_mode") or "")
    family = str(row.get("strategy_family") or "")
    covered = {
        str(item)
        for item in input_payload.get(
            "authority_covered_strategy_families",
            [],
        )
    }
    promotable_family = (
        "short_put"
        if family in {"short_put", "funding_put"}
        else "covered_call"
        if family == "covered_call"
        else None
    )
    if promotable_family is None:
        status = "not_applicable"
    elif mode == "v2":
        status = (
            "covered"
            if promotable_family in covered
            else "promotion_scope_uncovered"
        )
    elif mode == "v2_shadow":
        status = "shadow_evaluation"
    else:
        status = "v1_authoritative"
    row["promotion_scope_status"] = status
    model_trade_actionable = bool(
        row.get(
            "model_trade_actionable",
            row.get("model_actionable"),
        )
    )
    row["model_actionable"] = model_trade_actionable
    row["actionable"] = bool(
        model_trade_actionable
        and mode == "v2"
        and (
            promotable_family is None
            or promotable_family in covered
        )
    )
    if model_trade_actionable and not row["actionable"]:
        row["reason_codes"] = sorted(
            {
                *row.get("reason_codes", []),
                (
                    "promotion_scope_uncovered"
                    if status == "promotion_scope_uncovered"
                    else "v2_not_authoritative"
                ),
            }
        )


def _base_row(
    *,
    position_id: str,
    strategy_family: str,
    strategy_group_id: str | None,
    leg_role: str | None,
    symbol: str,
    option_type: str,
    side: str,
    expiration: str,
    strike: Any,
    contract_symbol: str,
    lifecycle_state: str,
    group_structure_state: str,
    reason_codes: Iterable[str],
) -> dict[str, Any]:
    return {
        "position_id": position_id,
        "strategy_family": strategy_family,
        "strategy_group_id": strategy_group_id,
        "leg_role": leg_role,
        "symbol": symbol,
        "option_type": option_type,
        "side": side,
        "expiration": expiration,
        "strike": strike,
        "contract_symbol": contract_symbol,
        "lifecycle_state": lifecycle_state,
        "group_structure_state": group_structure_state,
        "recommendation": "hold" if lifecycle_state == "open" else "not_evaluable",
        "model_trade_actionable": False,
        "model_actionable": False,
        "human_review_required": False,
        "actionable": False,
        "action_scope": "none",
        "current_extrinsic": None,
        "current_daily_carry": None,
        "current_capital_efficiency": None,
        "comparison_currency": None,
        "best_candidate": None,
        "opening_decision_hash": None,
        "invariant_decision_hash": None,
        "replacement_decision_hash": None,
        "replacement_eligibility": None,
        "candidate_daily_carry": None,
        "candidate_capital_efficiency": None,
        "comparison_horizon_days": None,
        "friction": None,
        "net_carry_improvement_H": None,
        "net_carry_improvement_H_base_cny": None,
        "payback_days": None,
        "reason_codes": sorted({str(item) for item in reason_codes if str(item)}),
        "resource_deltas": [],
        "leg_plan": None,
        "quote_as_of": None,
        "promotion_scope_status": None,
        "execution_order": None,
        "depends_on": [],
    }


def _apply_long_call_facts(
    row: dict[str, Any],
    *,
    view: Any,
    quote: Mapping[str, Any] | None,
) -> None:
    if quote is None:
        row["recommendation"] = "not_evaluable"
        row["reason_codes"] = ["long_call_quote_missing_or_ambiguous"]
        return
    bid = _quality_price(quote, side="bid", max_spread_ratio=None)
    dte = _nonnegative_int(quote.get("dte"))
    if bid is None or dte is None:
        row["recommendation"] = "not_evaluable"
        row["reason_codes"] = ["long_call_quote_unusable"]
        return
    try:
        facts = long_call_observable_facts(
            spot=quote.get("spot"),
            strike=view.strike,
            bid=bid,
            contracts=view.contracts_open,
            multiplier=view.multiplier,
            dte=dte,
            fee=calc_futu_option_fee(
                str(quote.get("currency") or view.currency or ""),
                float(bid),
                contracts=view.contracts_open,
                multiplier=int(float(view.multiplier)),
                is_sell=True,
            ),
        )
    except (TypeError, ValueError, OverflowError):
        row["recommendation"] = "not_evaluable"
        row["reason_codes"] = ["long_call_economic_evidence_incomplete"]
        return
    row.update(facts)
    row["recommendation"] = "not_evaluable"
    row["model_trade_actionable"] = False
    row["model_actionable"] = False
    row["reason_codes"] = ["long_call_forward_model_not_approved"]


def _validated_candidates(
    decisions: Iterable[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, tuple[str, ...]]]:
    valid: list[dict[str, Any]] = []
    errors: dict[str, tuple[str, ...]] = {}
    seen: set[str] = set()
    for raw in decisions:
        item = dict(raw or {})
        candidate_id = str(item.get("candidate_id") or "").strip()
        reasons: list[str] = []
        if not candidate_id or candidate_id in seen:
            reasons.append("candidate_identity_conflict")
        if item.get("schema_version") != "candidate_all_decisions.v1":
            reasons.append("candidate_schema_invalid")
        normalized = dict(item.get("normalized_input") or {})
        if item.get("normalized_input_hash") != canonical_sha256(normalized):
            reasons.append("candidate_input_hash_mismatch")
        opening = dict(item.get("opening_decision") or {})
        invariant = dict(item.get("invariant_decision") or {})
        replacement = dict(
            item.get("replacement_candidate_decision") or {}
        )
        if not _embedded_hash_valid(opening, "decision_hash"):
            reasons.append("opening_decision_hash_mismatch")
        if not _embedded_hash_valid(invariant, "decision_hash"):
            reasons.append("invariant_decision_hash_mismatch")
        risk_policy = dict(invariant.get("risk_policy") or {})
        if (
            not risk_policy
            or invariant.get("risk_policy_hash")
            != canonical_sha256(risk_policy)
        ):
            reasons.append("risk_policy_hash_mismatch")
        if not _embedded_hash_valid(
            replacement,
            "replacement_decision_hash",
        ):
            reasons.append("replacement_decision_hash_mismatch")
        if (
            replacement.get("candidate_id") != candidate_id
            or replacement.get("quote_snapshot_id")
            != item.get("quote_snapshot_id")
        ):
            reasons.append("replacement_candidate_binding_mismatch")
        if reasons:
            errors[candidate_id or f"invalid-{len(errors)}"] = tuple(
                sorted(set(reasons))
            )
            continue
        seen.add(candidate_id)
        valid.append(item)
    valid.sort(
        key=lambda item: (
            str(item.get("strategy_mode") or ""),
            str(item.get("candidate_id") or ""),
        )
    )
    return valid, errors


def _embedded_hash_valid(payload: Mapping[str, Any], field: str) -> bool:
    item = dict(payload or {})
    expected = item.pop(field, None)
    return isinstance(expected, str) and expected == canonical_sha256(item)


def _quote_index(
    quote_rows: Iterable[Mapping[str, Any]],
) -> dict[tuple[str, str, str, str], list[dict[str, Any]]]:
    output: dict[tuple[str, str, str, str], list[dict[str, Any]]] = {}
    for raw in quote_rows:
        item = dict(raw or {})
        key = _quote_key(
            symbol=item.get("symbol"),
            option_type=item.get("option_type"),
            expiration=item.get("expiration"),
            strike=item.get("strike"),
        )
        if key is not None:
            output.setdefault(key, []).append(item)
    return output


def _unique_quote_for_position(
    view: Any,
    quotes: Mapping[tuple[str, str, str, str], list[dict[str, Any]]],
) -> dict[str, Any] | None:
    key = _quote_key(
        symbol=view.symbol,
        option_type=view.option_type,
        expiration=view.expiration_ymd,
        strike=view.strike,
    )
    if key is None:
        return None
    rows = quotes.get(key) or []
    unique = {
        canonical_sha256(item): dict(item)
        for item in rows
    }
    return next(iter(unique.values())) if len(unique) == 1 else None


def _quote_key(
    *,
    symbol: Any,
    option_type: Any,
    expiration: Any,
    strike: Any,
) -> tuple[str, str, str, str] | None:
    symbol_value = canonical_symbol(symbol) or str(symbol or "").strip().upper()
    option_value = str(option_type or "").strip().lower()
    expiration_value = str(expiration or "").strip()[:10]
    strike_value = _positive_decimal(strike)
    if (
        not symbol_value
        or option_value not in {"put", "call"}
        or len(expiration_value) != 10
        or strike_value is None
    ):
        return None
    return (
        symbol_value,
        option_value,
        expiration_value,
        _decimal_text(strike_value),
    )


def _quality_price(
    quote: Mapping[str, Any],
    *,
    side: str,
    max_spread_ratio: Any,
) -> Decimal | None:
    bid = _nonnegative_decimal(quote.get("bid"))
    ask = _nonnegative_decimal(quote.get("ask"))
    if bid is None or ask is None or bid <= 0 or ask < bid:
        return None
    midpoint = (bid + ask) / Decimal("2")
    if midpoint <= 0:
        return None
    spread_ratio = (ask - bid) / midpoint
    threshold = _nonnegative_decimal(max_spread_ratio)
    if threshold is not None and spread_ratio > threshold:
        return None
    return ask if side == "ask" else bid


def _resource_pools(
    *,
    input_payload: Mapping[str, Any],
    cash_capacity: Mapping[str, Any],
    share_coverage: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    pools: dict[str, dict[str, Any]] = {}
    authority_id = str(
        input_payload.get("capacity_pool_authority_id") or ""
    )
    if cash_capacity.get("status") == "available" and len(authority_id) == 64:
        pools[
            f"cash:{input_payload.get('portfolio_scope_id')}:{authority_id}"
        ] = {
            "resource_kind": "cash_base_cny",
            "unit": "CNY",
            "available": cash_capacity.get(
                "uncommitted_cash_headroom_base_cny"
            ),
        }
    by_symbol = share_coverage.get("by_symbol")
    if isinstance(by_symbol, Mapping):
        for raw_symbol, raw in by_symbol.items():
            item = dict(raw or {}) if isinstance(raw, Mapping) else {}
            symbol = canonical_symbol(raw_symbol) or str(raw_symbol).upper()
            if item.get("status") != "available":
                continue
            pools[
                f"shares:{input_payload.get('broker')}:"
                f"{input_payload.get('account')}:{symbol}"
            ] = {
                "resource_kind": "covered_shares",
                "unit": "shares",
                "available": item.get("uncommitted_covered_shares"),
            }
    return pools


def _group_structure_states(
    *,
    snapshot: Mapping[str, Any],
    positions: Iterable[Mapping[str, Any]],
    lifecycle_by_lot: Mapping[str, Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    lots = {
        str(item.get("record_id") or ""): dict(item.get("fields") or {})
        for item in positions
        if str(item.get("record_id") or "")
    }
    identities = {
        str(item.get("group_id") or ""): dict(item)
        for item in snapshot.get("account_combo_identities", [])
        if isinstance(item, Mapping) and str(item.get("group_id") or "")
    }
    group_ids = {
        str(fields.get("strategy_group_id") or "")
        for fields in lots.values()
        if str(fields.get("strategy_group_id") or "")
    }
    output: dict[str, dict[str, Any]] = {}
    for group_id in sorted(group_ids | set(identities)):
        identity = identities.get(group_id)
        if identity is None:
            output[group_id] = {
                "state": "identity_unverified",
                "identity": None,
            }
            continue
        put_id = str(identity.get("funding_put_record_id") or "")
        call_id = str(identity.get("participation_call_record_id") or "")
        put_fields = lots.get(put_id, {})
        call_fields = lots.get(call_id, {})
        put_open = int(put_fields.get("contracts_open") or 0)
        call_open = int(call_fields.get("contracts_open") or 0)
        put_terminal = _resolved_contracts(
            lifecycle_by_lot.get(put_id)
        )
        call_terminal = _resolved_contracts(
            lifecycle_by_lot.get(call_id)
        )
        evidence_conflict = any(
            str(dict(lifecycle_by_lot.get(lot_id) or {}).get(
                "lifecycle_state"
            ))
            == "conflict"
            for lot_id in (put_id, call_id)
        )
        state = classify_combo_structure(
            identity=identity,
            funding_put_contracts_open=put_open,
            participation_call_contracts_open=call_open,
            funding_put_terminal_allocated=put_terminal,
            participation_call_terminal_allocated=call_terminal,
            assigned_stock_contracts=0,
            evidence_conflict=evidence_conflict,
        )
        output[group_id] = {"state": state, "identity": identity}
    return output


def _resolved_contracts(model: Mapping[str, Any] | None) -> int:
    values = dict(model or {}).get("resolved_contracts_by_lot")
    if not isinstance(values, Mapping):
        return 0
    return sum(int(value or 0) for value in values.values())


def _position_lifecycle(
    *,
    view: Any,
    checked_at: datetime,
    lifecycle_by_lot: Mapping[str, Mapping[str, Any]],
) -> tuple[str, list[str]]:
    model = dict(lifecycle_by_lot.get(str(view.record_id)) or {})
    if model:
        state = str(model.get("lifecycle_state") or "").strip()
        return (
            state or "needs_review",
            [
                str(item)
                for item in model.get("lifecycle_reason_codes") or []
                if str(item)
            ],
        )
    market = str(symbol_market(view.symbol) or "").upper()
    try:
        observation_start = expiration_observation_start_ms(
            str(view.expiration_ymd or ""),
            market,
        )
    except ValueError:
        return "needs_review", ["expiration_or_market_policy_invalid"]
    if int(checked_at.timestamp() * 1000) >= observation_start:
        return "needs_review", ["lifecycle_read_model_missing"]
    return "open", []


def _strategy_family(
    *,
    option_type: str,
    side: str,
    leg_role: str | None,
) -> str:
    if leg_role in {"funding_put", "sell_put"}:
        return "funding_put"
    if leg_role in {"participation_call", "enhancement_call"}:
        return "long_call"
    if side == "short" and option_type == "put":
        return "short_put"
    if side == "short" and option_type == "call":
        return "covered_call"
    if side == "long" and option_type == "call":
        return "long_call"
    return "unsupported"


def _current_put_capacity(view: Any) -> Decimal | None:
    amount = compute_short_put_cash_secured(
        contracts_open=view.contracts_open,
        contracts_total=view.contracts,
        cash_secured_amount=view.cash_secured_amount,
        strike=view.strike,
        multiplier=view.multiplier,
    )
    return _positive_decimal(amount)


def _share_coverage_item(
    share_coverage: Mapping[str, Any],
    symbol: str,
) -> dict[str, Any]:
    by_symbol = share_coverage.get("by_symbol")
    if not isinstance(by_symbol, Mapping):
        return {}
    for raw_symbol, raw in by_symbol.items():
        normalized = canonical_symbol(raw_symbol) or str(raw_symbol).upper()
        if normalized == symbol and isinstance(raw, Mapping):
            return dict(raw)
    return {}


def _fx_to_cny(payload: Mapping[str, Any]) -> dict[str, Decimal]:
    fx = payload.get("fx") if isinstance(payload.get("fx"), Mapping) else payload
    rates = fx.get("rates") if isinstance(fx, Mapping) else None
    if not isinstance(rates, Mapping):
        return {"CNY": Decimal("1")}
    output = {"CNY": Decimal("1")}
    for currency, key in (("USD", "USDCNY"), ("HKD", "HKDCNY")):
        rate = _positive_decimal(rates.get(key))
        if rate is not None:
            output[currency] = rate
    return output


def _to_cny(
    value: Decimal | None,
    *,
    currency: str,
    fx_to_cny: Mapping[str, Decimal],
) -> Decimal | None:
    if value is None:
        return None
    rate = fx_to_cny.get(str(currency or "").strip().upper())
    return value * rate if rate is not None else None


def _days_to_expiry(
    expiration: Any,
    *,
    as_of_date: Any,
) -> int | None:
    text = str(expiration or "").strip()[:10]
    try:
        expiry = datetime.fromisoformat(text).date()
    except ValueError:
        return None
    return max((expiry - as_of_date).days, 0)


def _quote_as_of_for_snapshot(
    input_payload: Mapping[str, Any],
    snapshot_id: Any,
) -> str | None:
    expected = str(snapshot_id or "").strip()
    for item in input_payload.get("source_manifest") or []:
        if (
            isinstance(item, Mapping)
            and item.get("source_kind") == "quotes"
            and item.get("snapshot_id") == expected
        ):
            value = str(item.get("source_observed_at") or "").strip()
            return value or None
    return None


def _positive_int(value: Any) -> int | None:
    parsed = _nonnegative_int(value)
    return parsed if parsed is not None and parsed > 0 else None


def _nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        numeric = Decimal(str(value))
        parsed = int(numeric)
    except (InvalidOperation, TypeError, ValueError, OverflowError):
        return None
    if not numeric.is_finite() or numeric != parsed or parsed < 0:
        return None
    return parsed


def _positive_decimal(value: Any) -> Decimal | None:
    parsed = _nonnegative_decimal(value)
    return parsed if parsed is not None and parsed > 0 else None


def _nonnegative_decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not parsed.is_finite() or parsed < 0:
        return None
    return parsed


def _decimal_or_zero(value: Any) -> Decimal:
    return _nonnegative_decimal(value) or Decimal("0")


def _decimal_text(value: Decimal) -> str:
    rendered = format(value.normalize(), "f")
    return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered


def _sha256(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise PositionAdvicePlanError(f"{field} is invalid")
    return text


def _parse_timestamp(value: datetime | str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PositionAdvicePlanError("timestamp must be timezone aware")
    return parsed.astimezone(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


__all__ = [
    "ACTIONABLE_RECOMMENDATIONS",
    "POSITION_ADVICE_LEG_PLAN_SCHEMA",
    "POSITION_ADVICE_PLAN_SCHEMA",
    "PositionAdvicePlanError",
    "build_position_advice_plan",
    "quote_rows_from_source_payloads",
]
