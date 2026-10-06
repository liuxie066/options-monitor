from __future__ import annotations

from domain.domain.assigned_stock import assigned_stock_lot_id_for_event

from decimal import Decimal
from typing import Any, Mapping, Sequence

from domain.domain.trade_contract_identity import (
    contract_share_quantity,
    stock_settlement_unit_issues,
)

from domain.domain.decision_state_fingerprint import canonical_sha256
from domain.domain.ledger.cash_facts import (
    assignment_principal_anchor,
    broker_settlement_multiplier_evidence,
)
from domain.domain.strategy_membership import (
    resolve_option_strategy_membership,
    resolve_strategy_metadata,
)
from domain.domain.symbol_identity import symbol_market
from domain.domain.wheel import (
    build_wheel_branch_created_event,
    lot_strategy_metadata_for_lot,
    merge_lot_strategy_metadata,
    project_wheel_branches,
    project_wheel_call_intents,
    build_legacy_wheel_called_away_event_from_call_assignment,
)
from src.application.ledger.assigned_stock_projection import (
    project_assigned_stock_lifecycle_from_rows,
)
from src.application.ledger.combo_membership import resolve_combo_assignment_proof
from src.application.ledger.event_codec import valid_void_target_event_id


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
        if "active_intent_reserved_shares" not in batch:
            try:
                batch["active_intent_reserved_shares"] = contract_share_quantity(
                    batch.get("active_intent_reserved_contracts") or 0, batch.get("multiplier"),
                )
            except ValueError:
                batch["active_intent_reserved_shares"] = None
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


def _stock_lot(report: Mapping[str, Any], lot_id: str) -> dict[str, Any] | None:
    rows = report.get("_all_assigned_stock_lots") or report.get("assigned_stock_lots") or []
    matches = [
        dict(item)
        for item in rows
        if isinstance(item, Mapping)
        and str(item.get("stock_lot_id") or "").strip() == lot_id
    ]
    if len(matches) > 1:
        raise ValueError(f"assigned stock lot is not unique: {lot_id}")
    return matches[0] if matches else None


def _source_open_event(
    rows: Mapping[str, Any],
    source_fields: Mapping[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    # ``open_event_id`` is the converged spelling of the lot's source open
    # (``source_event_id`` is retired, write-side-definition §2).
    source_event_id = str(source_fields.get("open_event_id") or "").strip()
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
    assignment_settlement_evidence = broker_settlement_multiplier_evidence(assignment)
    if assignment_settlement_evidence:
        evidence["assignment_settlement"] = assignment_settlement_evidence
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
    elif (
        assignment_multiplier is not None
        and assignment_multiplier == source_multiplier
        and assignment_settlement_evidence is not None
    ):
        status = "broker_settlement_pair"
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
        try:
            fields = repo.get_position_lot_fields(target_lot_id, conn=conn)
        except ValueError as exc:
            if not str(exc).startswith("position lot not found:"):
                raise
            # A batch can open both Combo legs before assigning its short leg.
            fields = None
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


def plan_wheel_assignment_companion(
    event: Any,
    fields: Mapping[str, Any] | None,
    rows: Mapping[str, Any],
    activation_window: Mapping[str, Any] | None,
    *,
    recorded_at_ms: int,
) -> tuple[dict[str, Any] | None, str | None]:
    """Plan the same assignment companion for intake and exact-event recovery."""
    event_id = str(getattr(event, "event_id", "") or "").strip()
    account = _event_account(event)
    if fields is None:
        reason = "assignment_source_lot_unavailable"
        return None, reason
    source_open, source_open_reason = _source_open_event(rows, fields)
    if source_open is None:
        reason = str(source_open_reason)
        return None, reason
    if int(source_open.get("event_time_ms") or 0) >= _event_time_ms(event):
        return None, "assignment_source_open_after_assignment"
    as_of_events = [
        item for item in rows.get("trade_events") or []
        if int(item.get("event_time_ms") or 0) < _event_time_ms(event)
    ]
    as_of_metadata = lot_strategy_metadata_for_lot(fields.get("lot_id"), as_of_events)
    strategy_fields = merge_lot_strategy_metadata(
        fields,
        as_of_metadata,
    )
    strategy_fields.update(as_of_metadata)
    resolved_metadata = resolve_strategy_metadata(
        strategy_fields, source_id=str(source_open.get("event_id") or ""),
    )
    metadata = resolved_metadata.metadata
    group_id = str(metadata.strategy_group_id or "").strip()
    has_wheel_source = bool(
        metadata.strategy == "wheel"
        or metadata.source_wheel_branch_id
        or metadata.leg_role in {"wheel_call", "wheel_put"}
    )
    has_combo_source = bool(metadata.strategy == "combo_yield" or group_id)
    if has_wheel_source and has_combo_source:
        return None, "strategy_attribution_conflict"
    valid_combo_group_ids: set[str] = set()
    if has_combo_source and group_id:
        variant, proof_reason = resolve_combo_assignment_proof(
            assignment=event,
            group_id=group_id,
            trade_events=rows.get("trade_events") or [],
            identities=(rows.get("account_combo_identities")
                        or rows.get("account_strategy_group_identities") or []),
        )
        if proof_reason:
            return None, proof_reason
        if variant:
            valid_combo_group_ids.add(group_id)
    membership = resolve_option_strategy_membership(
        getattr(event, "contract_key"),
        getattr(event, "position_side"),
        # The strategy-metadata family left ``fields_json`` in the convergence
        # batch (design §7.5); resolve it from this lot's events instead.
        strategy_fields,
        valid_combo_group_ids=valid_combo_group_ids,
        source_id=str(source_open.get("event_id") or ""),
    )
    if membership.issues:
        reason = "strategy_membership_unresolved"
        return None, reason
    combo_short_leg = (
        membership.strategy == "csp_lc"
        and membership.leg_role == "funding_put"
        and membership.parent_universe == "csp"
        or membership.strategy == "cc_lp"
        and membership.leg_role == "short_call"
        and membership.parent_universe == "cc"
    )
    if membership.strategy not in {"csp", "cc", "wheel"} and not combo_short_leg:
        return None, "strategy_not_eligible"
    option_type = str(
        getattr(getattr(event, "contract_key", None), "option_type", "") or ""
    ).strip().lower()
    if option_type not in {"put", "call"}:
        return None, "option_type_not_eligible"
    direction = "call" if option_type == "put" else "put"
    internal = membership.strategy == "wheel"
    parent_branch_id = None
    if internal:
        activation_window = None
        parent_branch_id = str(
            membership.source_wheel_branch_id
            or membership.source_lot_id
            or ""
        ).strip()
        if not parent_branch_id:
            reason = "wheel_parent_branch_unavailable"
            return None, reason
        parents = [
            branch
            for branch in _wheel_branches_from_rows(
                rows,
                account=account,
                as_of_ms=_event_time_ms(event),
            )
            if branch.get("wheel_branch_id") == parent_branch_id
            and branch.get("lifecycle_status") == "active"
        ]
        if len(parents) != 1:
            reason = "wheel_parent_branch_not_unique"
            return None, reason
    else:
        if membership.strategy in {"cc", "cc_lp"}:
            source_stock_lot_id = str(
                membership.source_lot_id or fields.get("source_stock_lot_id") or ""
            ).strip()
            voided_source_ids = {
                target for item in rows.get("trade_events") or []
                if (target := valid_void_target_event_id(item))
            }
            overlaps = [
                branch
                for branch in _wheel_branches_from_rows(
                    rows,
                    account=account,
                    as_of_ms=_event_time_ms(event),
                )
                if branch.get("lifecycle_status") == "active"
                and branch.get("symbol") == event.contract_key.underlying_symbol
                and branch.get("source_assignment_event_id") not in voided_source_ids
            ]
            if membership.strategy == "cc":
                if source_stock_lot_id and any(
                    branch.get("stock_lot_id") == source_stock_lot_id
                    for branch in overlaps
                ):
                    return None, "ordinary_cc_overlaps_active_wheel_stock"
            elif overlaps:
                stock_report = project_assigned_stock_lifecycle_from_rows(
                    rows, account=account, as_of_ms=_event_time_ms(event),
                )
                source_stock = _stock_lot(stock_report, source_stock_lot_id) if source_stock_lot_id else None
                if (
                    source_stock is None
                    or str(source_stock.get("symbol") or "").strip().upper()
                    != event.contract_key.underlying_symbol
                    or str(source_stock.get("account") or "").strip().lower() != account
                    or str(source_stock.get("broker") or "").strip()
                    != event.contract_key.broker
                    or any(branch.get("stock_lot_id") == source_stock_lot_id for branch in overlaps)
                ):
                    return None, "combo_cc_overlaps_active_wheel_stock"
        symbol = str(
            getattr(getattr(event, "contract_key", None), "underlying_symbol", "")
            or ""
        ).strip().upper()
        market = str(symbol_market(symbol) or "").strip().lower()
        if not market:
            reason = "wheel_market_unavailable"
            return None, reason
        if activation_window is None:
            return None, "wheel_activation_window_unavailable"
    multiplier, multiplier_source, multiplier_evidence_hash = _multiplier_evidence(
        event,
        source_open,
    )
    if multiplier is None or multiplier_source == "conflict" or (internal and multiplier_source == "unproven"):
        reason = f"multiplier_{multiplier_source}"
        return None, reason
    if multiplier != getattr(event, "multiplier", None) or multiplier != source_open.get("multiplier"):
        return None, "assignment_quantity_inconsistent"
    reason = "multiplier_unproven" if multiplier_source == "unproven" else None
    stock = getattr(event, "raw_payload", None) or {}
    stock = stock.get("stock_settlement") if isinstance(stock, Mapping) else None
    if not isinstance(stock, Mapping):
        return None, "assignment_stock_settlement_unavailable"
    contracts = int(getattr(event, "contracts", 0) or 0)
    unit_issues = stock_settlement_unit_issues(
        terminal_type="assignment",
        option_type=option_type,
        position_side="short",
        stock_side=str(stock.get("side") or "").strip().lower(),
        contracts=contracts,
        multiplier=multiplier,
        shares=stock.get("shares"),
    )
    if unit_issues:
        return None, unit_issues[0]
    (
        principal_anchor,
        currency,
        principal_anchor_reason,
        principal_anchor_fact_ids,
    ) = assignment_principal_anchor(event, direction)
    if principal_anchor_reason is not None:
        if internal or principal_anchor_reason != "assignment_cash_facts_unavailable":
            return None, principal_anchor_reason
        reason = reason or principal_anchor_reason
    stock_currency = str(stock.get("currency") or "").strip().upper()
    anchor_currency = str(currency or "").strip().upper()
    if not anchor_currency or (internal and not stock_currency):
        reason = "assignment_currency_unavailable"
        return None, reason
    if stock_currency and (
        stock_currency != anchor_currency
        or stock_currency != str(getattr(event, "currency", "") or "").strip().upper()
    ):
        reason = "assignment_currency_conflict"
        return None, reason
    lot_id = (
        assigned_stock_lot_id_for_event(event_id) if direction == "call" else None
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
        currency=anchor_currency,
        principal_anchor=principal_anchor,
        principal_anchor_reason=principal_anchor_reason,
        principal_anchor_fact_ids=principal_anchor_fact_ids,
        lot_id=lot_id,
        parent_branch_id=parent_branch_id,
        lifecycle_status="pending_decision" if internal else "active",
        activation_window=activation_window,
    )
    return companion, reason


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
    rolling_wheel_events: dict[str, list[dict[str, Any]]] = {}
    companion_by_trade: dict[str, str] = {}
    review_reason_by_trade: dict[str, str] = {}
    for event in sorted(
        new_assignments,
        key=lambda item: (_event_time_ms(item), str(getattr(item, "event_id", ""))),
    ):
        event_id = str(getattr(event, "event_id", "") or "").strip()
        account = _event_account(event)
        fields = source_fields.get(event_id)
        planner_rows = dict(before_rows[account])
        planner_rows["trade_events"] = [
            *(before_rows[account].get("trade_events") or []),
            *(
                item.to_dict()
                for item, created in zip(events, created_flags, strict=True)
                if created
                and _event_account(item) == account
                and (
                    _event_time_ms(item) < _event_time_ms(event)
                    or valid_void_target_event_id(item)
                )
            ),
        ]
        planner_rows["account_wheel_events"] = [
            *(before_rows[account].get("account_wheel_events") or []),
            *(
                item for item in rolling_wheel_events.get(account, [])
                if int(item.get("occurred_at_ms") or 0) < _event_time_ms(event)
            ),
        ]
        if fields is None:
            matches = [
                item.get("fields")
                for item in after_rows[account].get("account_position_lots") or []
                if str(item.get("record_id") or "").strip()
                == str(getattr(event, "target_lot_id", "") or "").strip()
            ]
            if len(matches) == 1 and isinstance(matches[0], Mapping):
                fields = dict(matches[0])
        if isinstance(fields, Mapping) and account in before_rows:
            # The strategy-metadata family is read from the event layer now
            # (design §7.5), so fold this lot's replayed metadata into the
            # fields every downstream check below reads.
            fields = merge_lot_strategy_metadata(
                fields,
                lot_strategy_metadata_for_lot(
                    fields.get("lot_id"),
                    planner_rows["trade_events"],
                ),
            )
        symbol = str(event.contract_key.underlying_symbol)
        market = str(symbol_market(symbol) or "").lower()
        activation_window = repo.get_wheel_activation_window_for_event(
            market=market, account=account, occurred_at_ms=_event_time_ms(event), conn=conn,
        ) if market else None
        companion, review_reason = plan_wheel_assignment_companion(
            event, fields, planner_rows, activation_window,
            recorded_at_ms=recorded_at_ms,
        )
        if review_reason and review_reason not in {
            "strategy_not_eligible", "option_type_not_eligible", "wheel_activation_window_unavailable",
        }:
            review_reason_by_trade[event_id] = review_reason
        if companion is None:
            continue
        membership = resolve_option_strategy_membership(event.contract_key, event.position_side, fields)
        internal = membership.strategy == "wheel"
        direction = companion["payload"]["direction"]
        settlement_shares = event.raw_payload["stock_settlement"]["shares"]
        legacy_terminal = None
        lot_id = str(fields.get("source_stock_lot_id") or "").strip()
        if internal and not membership.source_wheel_branch_id and direction == "put":
            instant = _event_time_ms(event)
            key = (account, lot_id)
            stock_lot_before = rolling_stock_lots.get(key)
            if stock_lot_before is None:
                before_projection = project_assigned_stock_lifecycle_from_rows(
                    before_rows[account],
                    account=account,
                    as_of_ms=instant,
                )
                stock_lot_before = _stock_lot(before_projection, lot_id)
            stock_lot_after = dict(stock_lot_before or {})
            stock_lot_after["shares_remaining"] = int(
                (stock_lot_before or {}).get("shares_remaining") or 0
            ) - settlement_shares
            legacy_terminal = build_legacy_wheel_called_away_event_from_call_assignment(
                event,
                fields,
                stock_lot_before,
                stock_lot_after,
                recorded_at_ms=recorded_at_ms,
            )
            rolling_stock_lots[key] = stock_lot_after
            rolling_stock_as_of[key] = instant
        repo.append_wheel_event_once(companion, conn=conn)
        rolling_wheel_events.setdefault(account, []).append(companion)
        companion_by_trade[event_id] = companion["event_id"]
        if legacy_terminal is not None:
            repo.append_wheel_event_once(legacy_terminal, conn=conn)
            rolling_wheel_events[account].append(legacy_terminal)

    for (account, lot_id), expected in rolling_stock_lots.items():
        actual_projection = project_assigned_stock_lifecycle_from_rows(
            after_rows[account],
            account=account,
            as_of_ms=rolling_stock_as_of[(account, lot_id)],
        )
        actual = _stock_lot(actual_projection, lot_id)
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
    account = _event_account(linked_event)
    rows = repo.read_lifecycle_account_rows(account=account, conn=conn)
    # The strategy-metadata family lives on the event layer (design §7.5), so the
    # linkage the adjust event just wrote is verified against the replayed
    # metadata rather than against retired flat payload keys.
    strategy_fields = lot_strategy_metadata_for_lot(
        lot_id,
        rows.get("trade_events") or [],
    )
    if (
        str(strategy_fields.get("strategy") or "") != "wheel"
        or str(strategy_fields.get("leg_role") or "") != "wheel_call"
        or str(strategy_fields.get("source_stock_lot_id") or "")
        != str(intent_event.get("stock_lot_id") or "")
    ):
        raise ValueError("Wheel Call intent linkage verification failed")
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
        lot_id=str(intent_event["stock_lot_id"]),
        as_of_ms=max(_event_time_ms(linked_event), 1),
        known_trade_event_ids={str(row.get("event_id") or "") for row in rows.get("trade_events") or []},
    )
    intent = next((row for row in summaries if row["intent_id"] == intent_event["intent_id"]), None)
    if (
        len(matches) != 1
        or matches[0]["integrity_status"] != "trusted"
        or lot_id not in matches[0]["active_call_lot_ids"]
        or intent is None
        or intent["status"] not in {"active", "consumed", "expired", "cancelled"}
        or (intent_event["intent_id"] in matches[0]["active_intent_ids"]) != (intent["status"] == "active" and int(intent.get("remaining_contracts") or 0) > 0)
    ):
        raise ValueError("Wheel Call intent projection verification failed")


__all__ = [
    "append_and_verify_wheel_intent_consumption",
    "append_wheel_trade_companions",
    "capture_wheel_trade_companion_context",
]
