from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import Any, Mapping, Sequence

from domain.domain.decision_state_fingerprint import canonical_sha256
from domain.domain.ledger.cash_facts import cash_facts_for_trade_event
from domain.domain.strategy_membership import resolve_option_strategy_membership
from domain.domain.symbol_identity import symbol_market
from domain.domain.wheel import (
    build_wheel_branch_created_event,
    plan_wheel_call_intent_consume,
    project_wheel_branches,
    project_wheel_call_intents,
    wheel_called_away_event_from_call_assignment,
)
from domain.domain.risk_capacity import revalidate_opening_share_coverage
from src.application.ledger.assigned_stock_projection import (
    project_assigned_stock_lifecycle_from_rows,
)


def _wheel_batches_from_rows(
    rows: Mapping[str, Any],
    *,
    account: str,
    as_of_ms: int,
) -> list[dict[str, Any]]:
    assigned_stock = project_assigned_stock_lifecycle_from_rows(
        rows,
        account=account,
        as_of_ms=as_of_ms,
    )
    branches = project_wheel_branches(
        rows.get("account_wheel_events") or [],
        rows.get("trade_events") or [],
        rows.get("account_position_lots") or [],
        assigned_stock,
        as_of_ms,
    )
    batches = []
    for branch in branches:
        if str(branch.get("direction") or "call").strip().lower() != "call":
            continue
        batch = dict(branch)
        if not batch.get("legacy_call_adapter"):
            batch["phase"] = {
                "option_open": "call_open",
                "intent_pending": "call_pending",
                "residual_capacity": "residual_stock",
            }.get(batch.get("phase"), batch.get("phase"))
        batch.setdefault("batch_generation_hash", batch.get("branch_generation_hash"))
        batch.setdefault(
            "active_call_lot_ids",
            list(batch.get("active_option_lot_ids") or []),
        )
        batch.setdefault(
            "active_intent_reserved_shares",
            int(batch.get("active_intent_reserved_contracts") or 0)
            * int(batch.get("multiplier") or 0),
        )
        batches.append(batch)
    return batches


def _wheel_branches_from_rows(
    rows: Mapping[str, Any],
    *,
    account: str,
    as_of_ms: int,
) -> list[dict[str, Any]]:
    assigned_stock = project_assigned_stock_lifecycle_from_rows(
        rows,
        account=account,
        as_of_ms=as_of_ms,
    )
    return project_wheel_branches(
        rows.get("account_wheel_events") or [],
        rows.get("trade_events") or [],
        rows.get("account_position_lots") or [],
        assigned_stock,
        as_of_ms,
    )


def _event_account(event: Any) -> str:
    key = getattr(event, "contract_key", None)
    return str(getattr(key, "account", "") or "").strip().lower()


def _event_time_ms(event: Any) -> int:
    return int(getattr(event, "event_time_ms", 0) or 0)


def _stock_lot(report: Mapping[str, Any], stock_lot_id: str) -> dict[str, Any] | None:
    rows = report.get("_all_assigned_stock_lots") or report.get("assigned_stock_lots") or []
    matches = [
        dict(item)
        for item in rows
        if isinstance(item, Mapping)
        and str(item.get("stock_lot_id") or "").strip() == stock_lot_id
    ]
    if len(matches) > 1:
        raise ValueError(f"assigned stock lot is not unique: {stock_lot_id}")
    return matches[0] if matches else None


def _source_open_event(
    rows: Mapping[str, Any],
    source_fields: Mapping[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    source_event_id = str(source_fields.get("source_event_id") or "").strip()
    matches = [
        dict(item)
        for item in rows.get("trade_events") or []
        if isinstance(item, Mapping)
        and str(item.get("event_id") or "").strip() == source_event_id
        and str(item.get("event_type") or "").strip().lower() == "open"
    ]
    if not matches:
        return None, "assignment_source_open_unavailable"
    if len(matches) != 1:
        return None, "assignment_source_open_not_unique"
    return matches[0], None


def _multiplier_evidence(
    assignment: Any,
    source_open: Mapping[str, Any],
) -> tuple[int | None, str, str]:
    def positive_multiplier(value: Any) -> int | None:
        if isinstance(value, bool):
            return None
        try:
            multiplier = Decimal(str(value))
        except (ArithmeticError, TypeError, ValueError):
            return None
        if (
            not multiplier.is_finite()
            or multiplier <= 0
            or multiplier != multiplier.to_integral_value()
        ):
            return None
        return int(multiplier)

    assignment_multiplier = positive_multiplier(getattr(assignment, "multiplier", None))
    source_multiplier = positive_multiplier(source_open.get("multiplier"))
    raw = source_open.get("raw_payload")
    raw = raw if isinstance(raw, Mapping) else {}
    source = str(raw.get("multiplier_source") or "").strip().lower()
    source_type = str(raw.get("source_type") or "").strip().lower()
    source_event_id = str(source_open.get("event_id") or "").strip()
    evidence_hash = str(raw.get("multiplier_evidence_hash") or "").strip().lower()
    multiplier_evidence = raw.get("multiplier_evidence")
    multiplier_evidence = (
        dict(multiplier_evidence)
        if isinstance(multiplier_evidence, Mapping)
        else {}
    )
    contract_key = source_open.get("contract_key")
    source_symbol = str(
        (contract_key or {}).get("underlying_symbol")
        if isinstance(contract_key, Mapping)
        else ""
    ).strip().upper()
    evidence = {
        key: raw.get(key)
        for key in (
            "source_deal_id",
            "deal_id",
            "order_id",
            "external_event_key",
            "execution_id",
            "lot_record_id",
            "source",
            "source_type",
            "manual_request_id",
            "manual_request_intent_hash",
            "multiplier_evidence_hash",
        )
        if raw.get(key) not in (None, "")
    }
    if multiplier_evidence:
        evidence["multiplier_evidence"] = multiplier_evidence
    broker_receipt = bool(
        raw.get("execution_id")
        or (
            raw.get("external_event_key")
            and (raw.get("source_deal_id") or raw.get("deal_id"))
        )
    )
    manual_receipt = bool(
        raw.get("manual_request_id") and raw.get("manual_request_intent_hash")
    )
    valid_evidence_hash = len(evidence_hash) == 64 and all(
        character in "0123456789abcdef" for character in evidence_hash
    ) and evidence_hash == canonical_sha256(multiplier_evidence)
    evidence_multiplier = positive_multiplier(multiplier_evidence.get("multiplier"))
    evidence_bound = (
        multiplier_evidence.get("schema_version")
        == "contract_multiplier_evidence.v1"
        and str(multiplier_evidence.get("source") or "").strip().lower()
        == source
        and str(multiplier_evidence.get("canonical_symbol") or "").strip().upper()
        == source_symbol
        and evidence_multiplier == source_multiplier
    )
    receipt_hash = str(
        multiplier_evidence.get("source_receipt_sha256") or ""
    ).strip().lower()
    resolver_receipt = (
        valid_evidence_hash
        and evidence_bound
        and len(receipt_hash) == 64
        and all(character in "0123456789abcdef" for character in receipt_hash)
    )
    bootstrap_receipt = (
        source_event_id.startswith("bootstrap:")
        and raw.get("lot_record_id")
        and isinstance(raw.get("fields"), Mapping)
        and resolver_receipt
        and multiplier_evidence.get("source_receipt_id") == source_event_id
        and receipt_hash
        == canonical_sha256(
            {
                "source": raw.get("source"),
                "lot_record_id": raw.get("lot_record_id"),
                "fields": raw.get("fields"),
            }
        )
    )
    provenance_proven = (
        source_type == "broker_trade_event"
        and broker_receipt
        and (
            source in {"payload", "input"}
            or source in {"cache", "opend"} and resolver_receipt
        )
    ) or (
        source_type == "manual_trade_event"
        and source == "payload"
        and manual_receipt
    ) or (
        source_type == "bootstrap_snapshot"
        and source == "bootstrap_snapshot"
        and bootstrap_receipt
    )
    if (
        assignment_multiplier is not None
        and source_multiplier is not None
        and assignment_multiplier != source_multiplier
    ):
        status = "conflict"
        multiplier = assignment_multiplier
    elif (
        assignment_multiplier is not None
        and assignment_multiplier == source_multiplier
        and source_event_id
        and provenance_proven
    ):
        status = source
        multiplier = assignment_multiplier
    else:
        status = "unproven"
        multiplier = assignment_multiplier or source_multiplier
    return (
        multiplier,
        status,
        canonical_sha256(
            {
                "schema_version": "wheel_multiplier_evidence.v1",
                "source_trade_event_id": source_event_id,
                "assignment_multiplier": assignment_multiplier,
                "source_multiplier": source_multiplier,
                "multiplier": multiplier,
                "multiplier_source": status,
                "evidence": evidence,
            }
        ),
    )


def _assignment_principal_anchor(
    event: Any,
    direction: str,
) -> tuple[str | None, str | None, str | None, tuple[str, ...]]:
    facts = {
        fact.fact_kind: fact
        for fact in cash_facts_for_trade_event(event)
        if fact.fact_kind.startswith("stock_settlement_")
    }
    gross = facts.get("stock_settlement_cash_gross")
    fee = facts.get("stock_settlement_fee_cash")
    fact_ids = tuple(sorted(fact.fact_id for fact in facts.values()))
    if (
        gross is None
        or fee is None
        or gross.amount is None
        or fee.amount is None
        or not gross.currency
        or gross.currency != fee.currency
    ):
        currency = gross.currency if gross is not None else fee.currency if fee is not None else None
        return None, currency, "assignment_cash_facts_unavailable", fact_ids
    net = Decimal(gross.amount) + Decimal(fee.amount)
    anchor = -net if direction == "call" else net
    if anchor < 0:
        return None, gross.currency, "assignment_cash_facts_invalid", fact_ids
    return format(anchor, "f"), gross.currency, None, fact_ids


def capture_wheel_trade_companion_context(
    repo: Any,
    *,
    conn: Any,
    events: Sequence[Any],
    wheel_start_enabled: bool,
) -> dict[str, Any]:
    del wheel_start_enabled
    source_fields: dict[str, dict[str, Any]] = {}
    accounts: set[str] = set()
    for event in events:
        if str(getattr(event, "event_type", "") or "").strip().lower() != "assignment":
            continue
        event_id = str(getattr(event, "event_id", "") or "").strip()
        target_lot_id = str(getattr(event, "target_lot_id", "") or "").strip()
        if not event_id or not target_lot_id:
            continue
        fields = repo.get_position_lot_fields(target_lot_id, conn=conn)
        source_fields[event_id] = fields
        accounts.add(_event_account(event))
    before_rows = {
        account: repo.read_lifecycle_account_rows(account=account, conn=conn)
        for account in sorted(accounts)
        if account
    }
    return {
        "source_fields": source_fields,
        "before_rows": before_rows,
    }


def append_wheel_trade_companions(
    repo: Any,
    *,
    conn: Any,
    events: Sequence[Any],
    created_flags: Sequence[bool],
    context: Mapping[str, Any],
    recorded_at_ms: int,
) -> tuple[dict[str, str], dict[str, str]]:
    source_fields = dict(context.get("source_fields") or {})
    before_rows = dict(context.get("before_rows") or {})
    new_assignments = [
        event
        for event, created in zip(events, created_flags, strict=True)
        if created
        and str(getattr(event, "event_type", "") or "").strip().lower() == "assignment"
    ]
    if not new_assignments:
        return {}, {}
    accounts = sorted({_event_account(event) for event in new_assignments if _event_account(event)})
    after_rows = {
        account: repo.read_lifecycle_account_rows(account=account, conn=conn)
        for account in accounts
    }
    rolling_stock_lots: dict[tuple[str, str], dict[str, Any]] = {}
    rolling_stock_as_of: dict[tuple[str, str], int] = {}
    companion_by_trade: dict[str, str] = {}
    review_reason_by_trade: dict[str, str] = {}
    for event in sorted(
        new_assignments,
        key=lambda item: (_event_time_ms(item), str(getattr(item, "event_id", ""))),
    ):
        event_id = str(getattr(event, "event_id", "") or "").strip()
        account = _event_account(event)
        fields = source_fields.get(event_id)
        if fields is None:
            review_reason_by_trade[event_id] = "assignment_source_lot_unavailable"
            continue
        source_open, source_open_reason = _source_open_event(before_rows[account], fields)
        if source_open is None:
            review_reason_by_trade[event_id] = str(source_open_reason)
            continue
        membership = resolve_option_strategy_membership(
            getattr(event, "contract_key"),
            fields,
            source_id=str(source_open.get("event_id") or ""),
        )
        if membership.issues:
            review_reason_by_trade[event_id] = "strategy_membership_unresolved"
            continue
        if membership.strategy not in {"csp", "cc", "wheel"}:
            continue
        option_type = str(
            getattr(getattr(event, "contract_key", None), "option_type", "") or ""
        ).strip().lower()
        if option_type not in {"put", "call"}:
            continue
        direction = "call" if option_type == "put" else "put"
        internal = membership.strategy == "wheel"
        parent_branch_id = None
        activation_window = None
        if internal:
            parent_branch_id = str(
                membership.source_wheel_branch_id
                or membership.source_stock_lot_id
                or ""
            ).strip()
            if not parent_branch_id:
                review_reason_by_trade[event_id] = "wheel_parent_branch_unavailable"
                continue
            parents = [
                branch
                for branch in _wheel_branches_from_rows(
                    before_rows[account],
                    account=account,
                    as_of_ms=_event_time_ms(event),
                )
                if branch.get("wheel_branch_id") == parent_branch_id
                and branch.get("lifecycle_status") == "active"
            ]
            if len(parents) != 1:
                review_reason_by_trade[event_id] = "wheel_parent_branch_not_unique"
                continue
        else:
            if membership.strategy == "cc" and membership.source_stock_lot_id:
                overlaps = [
                    branch
                    for branch in _wheel_branches_from_rows(
                        before_rows[account],
                        account=account,
                        as_of_ms=_event_time_ms(event),
                    )
                    if branch.get("lifecycle_status") == "active"
                    and branch.get("stock_lot_id") == membership.source_stock_lot_id
                ]
                if overlaps:
                    review_reason_by_trade[event_id] = (
                        "ordinary_cc_overlaps_active_wheel_stock"
                    )
                    continue
            symbol = str(
                getattr(getattr(event, "contract_key", None), "underlying_symbol", "")
                or ""
            ).strip().upper()
            market = str(symbol_market(symbol) or "").strip().lower()
            if not market:
                review_reason_by_trade[event_id] = "wheel_market_unavailable"
                continue
            activation_window = repo.get_wheel_activation_window_for_event(
                market=market,
                account=account,
                occurred_at_ms=_event_time_ms(event),
                conn=conn,
            )
            if activation_window is None:
                continue
        multiplier, multiplier_source, multiplier_evidence_hash = _multiplier_evidence(
            event,
            source_open,
        )
        if multiplier is None or multiplier_source in {"unproven", "conflict"}:
            review_reason_by_trade[event_id] = f"multiplier_{multiplier_source}"
            continue
        (
            principal_anchor,
            currency,
            principal_anchor_reason,
            principal_anchor_fact_ids,
        ) = _assignment_principal_anchor(event, direction)
        if principal_anchor_reason is not None:
            review_reason_by_trade[event_id] = principal_anchor_reason
            continue
        stock = getattr(event, "raw_payload", None) or {}
        stock = stock.get("stock_settlement") if isinstance(stock, Mapping) else {}
        stock_currency = str(stock.get("currency") or "").strip().upper()
        anchor_currency = str(currency or "").strip().upper()
        if not stock_currency or not anchor_currency:
            review_reason_by_trade[event_id] = "assignment_currency_unavailable"
            continue
        if stock_currency != anchor_currency:
            review_reason_by_trade[event_id] = "assignment_currency_conflict"
            continue
        contracts = int(getattr(event, "contracts", 0) or 0)
        settlement_shares = int(stock.get("shares") or 0)
        if contracts <= 0 or settlement_shares != contracts * multiplier:
            review_reason_by_trade[event_id] = "assignment_quantity_inconsistent"
            continue
        stock_lot_id = (
            f"assigned-stock-{event_id}" if direction == "call" else None
        )
        companion = build_wheel_branch_created_event(
            account=account,
            source_assignment_event_id=event_id,
            direction=direction,
            occurred_at_ms=_event_time_ms(event),
            recorded_at_ms=recorded_at_ms,
            symbol=str(
                getattr(getattr(event, "contract_key", None), "underlying_symbol", "")
                or ""
            ),
            contracts=contracts,
            multiplier=multiplier,
            multiplier_source=multiplier_source,
            multiplier_evidence_hash=multiplier_evidence_hash,
            currency=stock_currency,
            principal_anchor=principal_anchor,
            principal_anchor_reason=principal_anchor_reason,
            principal_anchor_fact_ids=principal_anchor_fact_ids,
            stock_lot_id=stock_lot_id,
            parent_branch_id=parent_branch_id,
            lifecycle_status="pending_decision" if internal else "active",
            activation_window=activation_window,
        )
        legacy_terminal = None
        stock_lot_id = str(fields.get("source_stock_lot_id") or "").strip()
        if internal and not membership.source_wheel_branch_id and direction == "put":
            instant = _event_time_ms(event)
            key = (account, stock_lot_id)
            stock_lot_before = rolling_stock_lots.get(key)
            if stock_lot_before is None:
                before_projection = project_assigned_stock_lifecycle_from_rows(
                    before_rows[account],
                    account=account,
                    as_of_ms=instant,
                )
                stock_lot_before = _stock_lot(before_projection, stock_lot_id)
            stock_lot_after = dict(stock_lot_before or {})
            stock_lot_after["shares_remaining"] = int(
                (stock_lot_before or {}).get("shares_remaining") or 0
            ) - settlement_shares
            legacy_terminal = wheel_called_away_event_from_call_assignment(
                event,
                fields,
                stock_lot_before,
                stock_lot_after,
                recorded_at_ms=recorded_at_ms,
            )
            rolling_stock_lots[key] = stock_lot_after
            rolling_stock_as_of[key] = instant
        repo.append_wheel_event_once(companion, conn=conn)
        companion_by_trade[event_id] = companion["event_id"]
        if legacy_terminal is not None:
            repo.append_wheel_event_once(legacy_terminal, conn=conn)

    for (account, stock_lot_id), expected in rolling_stock_lots.items():
        actual_projection = project_assigned_stock_lifecycle_from_rows(
            after_rows[account],
            account=account,
            as_of_ms=rolling_stock_as_of[(account, stock_lot_id)],
        )
        actual = _stock_lot(actual_projection, stock_lot_id)
        if int((actual or {}).get("shares_remaining") or 0) != int(
            expected.get("shares_remaining") or 0
        ):
            raise ValueError("Wheel Call assignment rolling stock readback failed")

    if companion_by_trade:
        verification_rows = {
            account: repo.read_lifecycle_account_rows(account=account, conn=conn)
            for account in accounts
        }
        for event in new_assignments:
            event_id = str(getattr(event, "event_id", "") or "").strip()
            companion_id = companion_by_trade.get(event_id)
            if not companion_id:
                continue
            account = _event_account(event)
            branches = _wheel_branches_from_rows(
                verification_rows[account],
                account=account,
                as_of_ms=max(_event_time_ms(event), 1),
            )
            matches = [branch for branch in branches if branch["start_event_id"] == companion_id]
            if len(matches) != 1:
                raise ValueError("Wheel companion event projection verification failed")
    return companion_by_trade, review_reason_by_trade


def prepare_wheel_intent_open_event(
    rows: Mapping[str, Any],
    event: Any,
    coverage_fact: Mapping[str, Any],
    *,
    recorded_at_ms: int,
) -> tuple[Any, dict[str, Any] | None, str]:
    if (
        str(getattr(event, "event_type", "") or "").strip().lower() != "open"
        or str(getattr(getattr(event, "contract_key", None), "option_type", ""))
        != "call"
        or str(getattr(getattr(event, "contract_key", None), "position_side", ""))
        != "short"
    ):
        return event, None, "not_short_call_open"
    account = _event_account(event)
    instant = _event_time_ms(event)
    batches = _wheel_batches_from_rows(
        rows,
        account=account,
        as_of_ms=instant,
    )
    contract_key = getattr(event, "contract_key", None)
    current_coverage = revalidate_opening_share_coverage(
        coverage_fact,
        list(rows.get("account_position_lots") or []),
        batches,
        account=account,
        symbol=str(getattr(contract_key, "underlying_symbol", "") or ""),
    )
    known_trade_ids = {
        str(item.get("event_id") or "").strip()
        for item in rows.get("trade_events") or []
        if str(item.get("event_id") or "").strip()
    }
    plans: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for batch in batches:
        if batch["lifecycle_status"] != "active" or batch["integrity_status"] != "trusted":
            continue
        summaries = project_wheel_call_intents(
            rows.get("account_wheel_events") or [],
            account=account,
            stock_lot_id=batch["stock_lot_id"],
            as_of_ms=instant,
            known_trade_event_ids=known_trade_ids,
        )
        for intent in summaries:
            if intent.get("status") != "active":
                continue
            intent_payload = intent.get("payload")
            intent_payload = (
                intent_payload if isinstance(intent_payload, Mapping) else intent
            )
            intent_coverage = {
                **current_coverage,
                "shares_available_for_cover": int(
                    current_coverage.get("shares_available_for_cover") or 0
                )
                + int(intent.get("remaining_contracts") or 0)
                * int(intent_payload.get("multiplier") or 0),
            }
            try:
                plan = plan_wheel_call_intent_consume(
                    batch,
                    intent,
                    event,
                    intent_coverage,
                    recorded_at_ms=recorded_at_ms,
                )
            except ValueError:
                continue
            plans.append((batch, plan))
    if not plans:
        return event, None, "no_matching_intent"
    if len(plans) != 1:
        return event, None, "ambiguous_matching_intent"
    batch, plan = plans[0]
    raw_payload = {
        **dict(getattr(event, "raw_payload", None) or {}),
        "strategy": "wheel",
        "leg_role": "wheel_call",
        "source_stock_lot_id": batch["stock_lot_id"],
        "source_wheel_branch_id": batch["wheel_branch_id"],
        "wheel_call_intent_id": plan["intent_id"],
    }
    return (
        replace(
            event,
            lot_id=str(getattr(event, "lot_id", "") or f"lot_{event.event_id}"),
            raw_payload=raw_payload,
        ),
        plan,
        "matched_intent",
    )


def append_and_verify_wheel_intent_consumption(
    repo: Any,
    *,
    conn: Any,
    linked_event: Any,
    intent_event: Mapping[str, Any],
) -> None:
    if not repo.append_wheel_event_once(intent_event, conn=conn):
        raise ValueError("Wheel Call intent consumption unexpectedly replayed")
    lot_id = str(
        getattr(linked_event, "lot_id", "")
        or getattr(linked_event, "target_lot_id", "")
        or ""
    ).strip()
    fields = repo.get_position_lot_fields(lot_id, conn=conn)
    if (
        str(fields.get("strategy") or "") != "wheel"
        or str(fields.get("leg_role") or "") != "wheel_call"
        or str(fields.get("source_stock_lot_id") or "")
        != str(intent_event.get("stock_lot_id") or "")
    ):
        raise ValueError("Wheel Call intent linkage verification failed")
    account = _event_account(linked_event)
    rows = repo.read_lifecycle_account_rows(account=account, conn=conn)
    batches = _wheel_batches_from_rows(
        rows,
        account=account,
        as_of_ms=max(_event_time_ms(linked_event), 1),
    )
    matches = [
        batch
        for batch in batches
        if batch["stock_lot_id"] == intent_event["stock_lot_id"]
    ]
    summaries = project_wheel_call_intents(
        rows.get("account_wheel_events") or [], account=account,
        stock_lot_id=str(intent_event["stock_lot_id"]),
        as_of_ms=max(_event_time_ms(linked_event), 1),
        known_trade_event_ids={str(row.get("event_id") or "") for row in rows.get("trade_events") or []},
    )
    intent = next((row for row in summaries if row["intent_id"] == intent_event["intent_id"]), None)
    if (
        len(matches) != 1
        or matches[0]["integrity_status"] != "trusted"
        or lot_id not in matches[0]["active_call_lot_ids"]
        or intent is None
        or intent["status"] not in {"active", "consumed"}
        or (intent_event["intent_id"] in matches[0]["active_intent_ids"]) != (int(intent.get("remaining_contracts") or 0) > 0)
    ):
        raise ValueError("Wheel Call intent projection verification failed")


__all__ = [
    "append_and_verify_wheel_intent_consumption",
    "append_wheel_trade_companions",
    "capture_wheel_trade_companion_context",
    "prepare_wheel_intent_open_event",
]
