from __future__ import annotations

from .writer_common import (
    Any,
    ContractKey,
    Decimal,
    InvalidOperation,
    LedgerWriteResult,
    LotCloseResolutionError,
    LotCloseSelector,
    Mapping,
    ProjectionRefreshResult,
    Sequence,
    TradeEvent,
    _ACTUAL_FEE_COMPONENT_KEYS,
    _ACTUAL_FEE_RAW_SOURCE_KEYS,
    _ACTUAL_FEE_TOTAL_KEYS,
    attach_trade_event_cash_conversions,
    broker_external_event_key,
    build_combo_identity_intent,
    build_notification_intent,
    canonical_contract_symbol,
    canonical_decimal_text,
    canonical_state_fingerprint,
    capture_current_decision_projection_fence,
    capture_trade_event_decision_projection_fence,
    compare_projection_lots,
    defer_current_decision_projection,
    encode_trade_event_for_storage,
    ensure_projection_publishable,
    estimate_futu_executed_option_fee,
    finalize_current_decision_projection,
    identity_from_intent,
    json,
    load_cash_fx_payload,
    normalize_broker,
    normalize_contract_expiration,
    normalize_currency,
    normalize_position_effect,
    normalize_trade_side,
    project_stored_trade_events_to_position_lots,
    projection_diagnostics_summary,
    projection_refresh_result_from_runtime,
    quantize_money,
    replace,
    resolve_combo_group_membership,
    resolve_fifo_close_targets,
    run_position_projection_in_transaction,
    strip_retired_strategy_metadata,
    to_decimal,
    utc_now_ms,
    validate_combo_identity,
    with_sqlite_repo_transaction,
)

from .writer_decision import (
    _begin_lifecycle_decision_projection,
    _defer_lifecycle_decision_projection,
    _event_position_record_id,
    _finish_lifecycle_decision_projection,
    _finish_trade_event_decision_projection,
    _lifecycle_resolution_after_allocations,
    _projection_mode_for_events,
    _trade_events_by_id,
)

from .writer_lifecycle_support import (
    _assert_combo_membership_exact,
    _combo_contract_count,
    _combo_leg_from_projected_record,
    _existing_combo_adoption_leg,
)
from .order_fee_semantics import zero_option_fee_lifecycle_reason


def rebuild_position_lots_from_trade_events(repo: Any) -> ProjectionRefreshResult:
    def _run(sqlite_repo: Any, conn: Any | None) -> ProjectionRefreshResult:
        if conn is None:
            raise TypeError("position projection rebuild requires SQLite transaction authority")
        event_count = int(
            conn.execute("SELECT COUNT(*) FROM trade_events").fetchone()[0]
        )
        decision_fence = capture_trade_event_decision_projection_fence(
            sqlite_repo,
            conn=conn,
        )
        runtime = run_position_projection_in_transaction(
            sqlite_repo,
            conn=conn,
            mode="forced_full",
        )
        return projection_refresh_result_from_runtime(
            runtime,
            trade_event_count=event_count,
            decision_projection=defer_current_decision_projection(decision_fence),
        )

    return with_sqlite_repo_transaction(
        repo,
        _run,
        require_projection_publication=True,
    )

def persist_trade_event_object(repo: Any, event: Any) -> LedgerWriteResult:
    def _run(sqlite_repo: Any, conn: Any | None) -> LedgerWriteResult:
        if conn is None:
            raise TypeError("trade event persistence requires SQLite transaction authority")
        storage_events = [
            _canonical_storage_event(item)
            for item in _events_for_storage(sqlite_repo, event, conn=conn)
        ]
        existing_by_id = _trade_events_by_id(
            sqlite_repo,
            [item.event_id for item in storage_events],
            conn=conn,
        )
        observed_at_ms = utc_now_ms()
        storage_events = _prepare_fee_evidence_for_storage(
            storage_events,
            existing_by_id=existing_by_id,
            frozen_at_ms=observed_at_ms,
        )
        fx_payload = load_cash_fx_payload(sqlite_repo)
        storage_events = [
            _event_with_existing_cash_conversions(item, existing_by_id[item.event_id])
            if item.event_id in existing_by_id
            else attach_trade_event_cash_conversions(
                item,
                fx_payload=fx_payload,
                observed_at_ms=observed_at_ms,
            )
            for item in storage_events
        ]
        decision_fence = capture_trade_event_decision_projection_fence(
            sqlite_repo,
            conn=conn,
        )
        runtime = run_position_projection_in_transaction(
            sqlite_repo,
            storage_events,
            conn=conn,
            mode=_projection_mode_for_events(storage_events),
        )
        result = {
            "event_id": event.event_id,
            "record_id": _event_position_record_id(storage_events[0]),
            "created": any(runtime.created_flags),
            "position_lot_count": int(runtime.position_lot_count),
            "decision_projection": _finish_trade_event_decision_projection(
                sqlite_repo,
                conn=conn,
                fence=decision_fence,
                events=storage_events,
                created_flags=runtime.created_flags,
            ),
        }
        result.update(projection_diagnostics_summary(runtime.diagnostics))
        return LedgerWriteResult.from_payload(result)

    return with_sqlite_repo_transaction(
        repo,
        _run,
        require_projection_publication=True,
    )

def persist_trade_event_with_combo_identity(
    repo: Any,
    event: Any,
    *,
    combo_identity_intent: dict[str, Any],
) -> dict[str, Any]:
    """Persist the second Combo leg and immutable identity in one ledger transaction."""

    intent = dict(combo_identity_intent or {})

    def _run(sqlite_repo: Any, conn: Any | None) -> dict[str, Any]:
        if conn is None:
            raise TypeError("combo identity persistence requires SQLite transaction authority")
        expanded = [
            _canonical_storage_event(item)
            for item in _events_for_storage(sqlite_repo, event, conn=conn)
        ]
        if len(expanded) != 1 or expanded[0].event_type != "open":
            raise ValueError("combo identity persistence requires one explicitly targeted open event")
        storage_event = expanded[0]
        existing_by_id = _trade_events_by_id(
            sqlite_repo,
            (storage_event.event_id,),
            conn=conn,
        )
        observed_at_ms = utc_now_ms()
        storage_event = _prepare_fee_evidence_for_storage(
            [storage_event],
            existing_by_id=existing_by_id,
            frozen_at_ms=observed_at_ms,
        )[0]
        if storage_event.event_id in existing_by_id:
            storage_event = _event_with_existing_cash_conversions(
                storage_event, existing_by_id[storage_event.event_id]
            )
        else:
            storage_event = attach_trade_event_cash_conversions(
                storage_event,
                fx_payload=load_cash_fx_payload(sqlite_repo),
                observed_at_ms=observed_at_ms,
            )
        group_id = str(intent.get("group_id") or "").strip()
        existing_identity = sqlite_repo.get_strategy_group_identity(group_id, conn=conn)
        decision_fence = capture_trade_event_decision_projection_fence(
            sqlite_repo,
            conn=conn,
        )
        runtime = run_position_projection_in_transaction(
            sqlite_repo,
            (storage_event,),
            conn=conn,
            mode="forced_full",
        )
        created = runtime.created_flags[0]
        if not created and existing_identity is None:
            raise ValueError("identity_missing_for_existing_second_leg")
        events = sqlite_repo.list_trade_events(conn=conn)
        projected_lots = sqlite_repo.list_position_lots(conn=conn)
        records_by_open_event = {
            str((record.get("fields") or {}).get("source_event_id") or "").strip(): record
            for record in projected_lots
            if str((record.get("fields") or {}).get("source_event_id") or "").strip()
        }
        first_leg = _combo_leg_from_projected_record(
            intent=intent,
            prefix="first_leg",
            records_by_open_event=records_by_open_event,
        )
        second_leg = _combo_leg_from_projected_record(
            intent=intent,
            prefix="second_leg",
            records_by_open_event=records_by_open_event,
        )
        identity = identity_from_intent(
            intent,
            first_leg=first_leg,
            second_leg=second_leg,
        )
        if existing_identity is not None:
            existing_validation = validate_combo_identity(
                existing_identity
            )
            if (
                existing_validation.status != "valid"
                or existing_validation.identity_hash
                != existing_identity.get("identity_hash")
                or existing_identity != identity
            ):
                raise ValueError("strategy group identity conflict")
        membership = resolve_combo_group_membership(
            group_id=str(identity["group_id"]),
            account=str(identity["account"]),
            expected_symbol=str(identity["symbol"]),
            trade_events=events,
            projected_position_lots=projected_lots,
        )
        _assert_combo_membership_exact(
            membership,
            expected_record_ids={
                str(identity["funding_put_record_id"]),
                str(identity["participation_call_record_id"]),
            },
            require_fully_open=True,
        )
        identity_created = sqlite_repo.insert_strategy_group_identity(identity, conn=conn)
        readback = sqlite_repo.get_strategy_group_identity(
            str(identity["group_id"]),
            conn=conn,
        )
        if readback != identity:
            raise ValueError("strategy group identity readback conflict")
        membership_readback = resolve_combo_group_membership(
            group_id=str(identity["group_id"]),
            account=str(identity["account"]),
            expected_symbol=str(identity["symbol"]),
            trade_events=events,
            projected_position_lots=projected_lots,
        )
        if membership_readback.generation_hash != membership.generation_hash:
            raise ValueError("combo identity membership generation changed")
        sqlite_repo.assert_foreign_keys_clean(conn=conn)
        decision_projection = _finish_trade_event_decision_projection(
            sqlite_repo,
            conn=conn,
            fence=decision_fence,
            events=(storage_event,),
            created_flags=(created,),
        )
        return {
            "event_id": storage_event.event_id,
            "record_id": second_leg["record_id"],
            "event_created": created,
            "identity_created": identity_created,
            "identity": identity,
            "membership": membership.fact,
            "position_lot_count": int(runtime.position_lot_count),
            "decision_projection": decision_projection,
        }

    return with_sqlite_repo_transaction(
        repo,
        _run,
        require_projection_publication=True,
    )

def adopt_existing_combo_identity_atomically(
    repo: Any,
    *,
    group_id: str,
    funding_put_record_id: str,
    funding_put_open_event_id: str,
    participation_call_record_id: str,
    participation_call_open_event_id: str,
    expected_contracts: int,
    apply_changes: bool = False,
) -> dict[str, Any]:
    """Insert immutable identity for two exact, already-open Combo legs."""

    group_value = str(group_id or "").strip()
    expected = _combo_contract_count(expected_contracts)
    if not group_value:
        raise ValueError("combo identity adoption requires strategy_group_id")
    if expected is None:
        raise ValueError("combo identity adoption requires positive contracts")

    def _run(sqlite_repo: Any, conn: Any | None) -> dict[str, Any]:
        if conn is None:
            raise TypeError("combo identity adoption requires SQLite transaction authority")
        events = list(sqlite_repo.list_trade_events(conn=conn))
        projection = project_stored_trade_events_to_position_lots(events)
        ensure_projection_publishable(
            projection,
            operation="combo identity adoption",
        )
        current_lots = list(sqlite_repo.list_position_lots(conn=conn))
        comparison = compare_projection_lots(
            projected_lots=list(projection.lots),
            current_lots=current_lots,
            diagnostics=list(projection.diagnostics),
        )
        projection_errors = {
            key: int(value)
            for key, value in dict(comparison.get("summary") or {}).items()
            if key != "matched" and int(value) > 0
        }
        if projection_errors:
            raise ValueError(
                "combo identity adoption requires a matching trade_events projection"
            )
        existing = sqlite_repo.get_strategy_group_identity(
            group_value,
            conn=conn,
        )
        if existing is not None:
            existing_validation = validate_combo_identity(existing)
            if (
                existing_validation.status != "valid"
                or existing_validation.identity_hash
                != existing.get("identity_hash")
            ):
                raise ValueError("strategy group identity conflict")
        records_by_id = {
            str(record.get("record_id") or ""): record
            for record in current_lots
        }
        events_by_id = {
            str(item.get("event_id") or ""): dict(item)
            for item in events
            if isinstance(item, dict) and str(item.get("event_id") or "").strip()
        }
        funding_put = _existing_combo_adoption_leg(
            records_by_id=records_by_id,
            events_by_id=events_by_id,
            record_id=funding_put_record_id,
            open_event_id=funding_put_open_event_id,
            group_id=group_value,
            expected_contracts=expected,
            expected_option_type="put",
            expected_position_side="short",
            accepted_roles={"funding_put", "sell_put"},
            require_fully_open=existing is None,
        )
        participation_call = _existing_combo_adoption_leg(
            records_by_id=records_by_id,
            events_by_id=events_by_id,
            record_id=participation_call_record_id,
            open_event_id=participation_call_open_event_id,
            group_id=group_value,
            expected_contracts=expected,
            expected_option_type="call",
            expected_position_side="long",
            accepted_roles={
                "participation_call",
                "enhancement_call",
            },
            require_fully_open=existing is None,
        )
        if (
            funding_put["broker"] != participation_call["broker"]
            or funding_put["account"] != participation_call["account"]
            or funding_put["symbol"] != participation_call["symbol"]
            or funding_put["currency"] != participation_call["currency"]
            or funding_put["multiplier"] != participation_call["multiplier"]
        ):
            raise ValueError("combo identity adoption leg economics mismatch")
        if (
            funding_put["strike"] >= participation_call["strike"]
            or funding_put["expiration_ymd"] > participation_call["expiration_ymd"]
        ):
            raise ValueError("combo identity adoption leg structure mismatch")
        intent = build_combo_identity_intent(
            first_leg=funding_put,
            second_leg=participation_call,
        )
        identity = identity_from_intent(
            intent,
            first_leg=funding_put,
            second_leg=participation_call,
        )
        if existing is not None and existing != identity:
            raise ValueError("strategy group identity conflict")
        membership = resolve_combo_group_membership(
            group_id=group_value,
            account=str(identity["account"]),
            expected_symbol=str(identity["symbol"]),
            trade_events=events,
            projected_position_lots=projection.lots,
        )
        _assert_combo_membership_exact(
            membership,
            expected_record_ids={
                str(identity["funding_put_record_id"]),
                str(identity["participation_call_record_id"]),
            },
            require_fully_open=existing is None,
        )
        identity_created = False
        decision_projection = None
        if apply_changes and existing is None:
            decision_fence = capture_current_decision_projection_fence(
                sqlite_repo,
                accounts=(str(identity["account"]),),
                conn=conn,
            )
            identity_created = sqlite_repo.insert_strategy_group_identity(
                identity,
                conn=conn,
            )
            readback = sqlite_repo.get_strategy_group_identity(
                group_value,
                conn=conn,
            )
            if readback != identity:
                raise ValueError("strategy group identity readback conflict")
            membership_readback = resolve_combo_group_membership(
                group_id=group_value,
                account=str(identity["account"]),
                expected_symbol=str(identity["symbol"]),
                trade_events=sqlite_repo.list_trade_events(conn=conn),
                projected_position_lots=projection.lots,
            )
            if membership_readback.generation_hash != membership.generation_hash:
                raise ValueError(
                    "combo identity membership generation changed"
                )
            decision_projection = finalize_current_decision_projection(
                sqlite_repo,
                fence=decision_fence,
                updated_at_ms=int(utc_now_ms()),
                conn=conn,
            )
            sqlite_repo.assert_foreign_keys_clean(conn=conn)
        return {
            "schema_version": ("existing_combo_identity_adoption.v1"),
            "status": ("existing" if existing is not None else ("adopted" if apply_changes else "dry_run")),
            "apply_changes": bool(apply_changes),
            "identity_created": identity_created,
            "strategy_group_id": group_value,
            "intent": intent,
            "identity": identity,
            "funding_put": funding_put,
            "participation_call": participation_call,
            "membership": membership.fact,
            "projection_summary": comparison["summary"],
            "decision_projection": decision_projection,
        }

    return with_sqlite_repo_transaction(repo, _run)

def _canonical_rows(rows: Sequence[dict[str, Any]]) -> list[str]:
    return sorted(
        json.dumps(dict(item or {}), ensure_ascii=False, sort_keys=True)
        for item in rows
    )

def _canonical_storage_event(item: Any) -> TradeEvent:
    encoded = encode_trade_event_for_storage(item)
    if encoded.event is None:  # pragma: no cover - encoder raises before this branch
        raise ValueError("trade event could not be canonicalized for storage")
    return encoded.event

def _prepare_fee_evidence_for_storage(
    events: Sequence[TradeEvent],
    *,
    existing_by_id: Mapping[str, dict[str, Any]],
    frozen_at_ms: int,
) -> list[TradeEvent]:
    """Freeze fee estimates only for new events and preserve replay evidence."""

    rows = list(events)
    grouped: dict[str, list[int]] = {}
    for index, event in enumerate(rows):
        payload = event.raw_payload or {}
        fee_order_group_id = str(
            payload.get("source_deal_id") or payload.get("fee_order_group_id") or ""
        ).strip()
        if fee_order_group_id and event.event_type == "close":
            grouped.setdefault(fee_order_group_id, []).append(index)
    handled: set[int] = set()
    for indexes in grouped.values():
        if len(indexes) < 2:
            continue
        existing_count = sum(rows[index].event_id in existing_by_id for index in indexes)
        if 0 < existing_count < len(indexes):
            raise ValueError("broker close split replay is only partially present")
        if existing_count:
            continue
        group = [rows[index] for index in indexes]
        frozen = _freeze_source_deal_fee_group(
            group,
            frozen_at_ms=frozen_at_ms,
        )
        for index, event in zip(indexes, frozen, strict=True):
            rows[index] = event
            handled.add(index)

    for index, event in enumerate(rows):
        existing = existing_by_id.get(event.event_id)
        if existing is not None:
            rows[index] = _event_with_existing_fee_evidence(event, existing)
        elif index not in handled:
            rows[index] = _freeze_new_event_fee(event, frozen_at_ms=frozen_at_ms)
    return rows

def _freeze_source_deal_fee_group(
    events: Sequence[TradeEvent],
    *,
    frozen_at_ms: int,
) -> list[TradeEvent]:
    problem = _source_deal_group_problem(events)
    resolutions = [_incoming_fee_resolution(event) for event in events]
    diagnostics = _group_fee_candidate_diagnostics(resolutions)
    if problem is not None:
        return [
            _event_with_missing_fee(event, reason=problem, diagnostics=diagnostics)
            for event in events
        ]
    if all(item["status"] == "explicit_missing" for item in resolutions):
        return [replace(event, fees=0.0) for event in events]
    if any(item["status"] == "explicit_missing" for item in resolutions):
        return [
            _event_with_missing_fee(
                event,
                reason="source_deal_fee_evidence_conflict",
                diagnostics=diagnostics,
            )
            for event in events
        ]
    blocked = [item for item in resolutions if item["status"] == "missing"]
    actual = [item for item in resolutions if item["status"] == "actual"]
    if blocked or (actual and len(actual) != len(events)):
        reason = (
            str(blocked[0]["reason"])
            if blocked
            else "source_deal_fee_evidence_conflict"
        )
        return [
            _event_with_missing_fee(event, reason=reason, diagnostics=diagnostics)
            for event in events
        ]
    if actual:
        amounts = {item["amount"] for item in actual}
        sources = {str(item["source"]) for item in actual}
        if len(amounts) != 1 or len(sources) != 1:
            return [
                _event_with_missing_fee(
                    event,
                    reason="source_deal_fee_evidence_conflict",
                    diagnostics=diagnostics,
                )
                for event in events
            ]
        return _allocate_actual_fee_group(
            events,
            total=next(iter(amounts)),
            source=next(iter(sources)),
            frozen_at_ms=frozen_at_ms,
        )
    return _freeze_formula_fee_group(events, frozen_at_ms=frozen_at_ms)

def _source_deal_group_problem(events: Sequence[TradeEvent]) -> str | None:
    rows = list(events)
    comparable = {
        (
            event.contract_key.broker,
            event.contract_key.account,
            event.currency,
            event.contract_key.position_side,
            event.price,
            event.multiplier,
        )
        for event in rows
    }
    if len(comparable) != 1 or any(event.contracts <= 0 for event in rows):
        return "source_deal_fee_inputs_conflict"
    expected: set[int] = set()
    for event in rows:
        payload = event.raw_payload or {}
        completion = payload.get("broker_deal_completion")
        resolution = payload.get("close_target_resolution")
        raw_expected = (
            (completion or {}).get("expected_contracts")
            if isinstance(completion, Mapping)
            else None
        )
        if raw_expected in (None, "") and isinstance(resolution, Mapping):
            selector = resolution.get("selector")
            raw_expected = (
                selector.get("contracts_to_close")
                if isinstance(selector, Mapping)
                else None
            )
        if raw_expected in (None, ""):
            raw_expected = (resolution or {}).get("contracts_to_close") if isinstance(resolution, Mapping) else None
        try:
            if raw_expected not in (None, ""):
                expected.add(int(raw_expected))
        except (TypeError, ValueError):
            return "source_deal_fee_inputs_conflict"
    allocated = sum(event.contracts for event in rows)
    if not expected:
        return "source_deal_fee_contracts_unavailable"
    if len(expected) > 1 or expected != {allocated}:
        return "source_deal_fee_contracts_conflict"
    return None

def _allocate_actual_fee_group(
    events: Sequence[TradeEvent],
    *,
    total: Decimal,
    source: str,
    frozen_at_ms: int,
) -> list[TradeEvent]:
    rows = sorted(events, key=lambda item: (item.event_time_ms, item.event_id))
    total_contracts = sum(event.contracts for event in rows)
    allocated = Decimal(0)
    by_id: dict[str, TradeEvent] = {}
    for index, event in enumerate(rows):
        amount = (
            quantize_money(total - allocated)
            if index == len(rows) - 1
            else quantize_money(total * Decimal(event.contracts) / Decimal(total_contracts))
        )
        allocated = quantize_money(allocated + amount)
        by_id[event.event_id] = _event_with_actual_fee(
            event,
            amount=amount,
            source=source,
            frozen_at_ms=frozen_at_ms,
        )
    return [by_id[event.event_id] for event in events]

def _freeze_formula_fee_group(
    events: Sequence[TradeEvent],
    *,
    frozen_at_ms: int,
) -> list[TradeEvent]:
    rows = sorted(events, key=lambda item: (item.event_time_ms, item.event_id))
    if any(normalize_broker(event.contract_key.broker) != "富途" for event in rows):
        return [
            _event_with_missing_fee(event, reason="unsupported_broker_fee_schedule")
            for event in events
        ]
    first = rows[0]
    comparable = {
        (event.currency, event.price, event.multiplier, event.contract_key.position_side)
        for event in rows
    }
    if len(comparable) != 1:
        return [
            _event_with_missing_fee(event, reason="source_deal_fee_inputs_conflict")
            for event in events
        ]
    total_contracts = sum(int(event.contracts) for event in rows)
    try:
        estimate = estimate_futu_executed_option_fee(
            first.currency,
            first.price,
            contracts=total_contracts,
            multiplier=int(first.multiplier),
            is_sell=first.contract_key.position_side == "long",
        )
    except (TypeError, ValueError):
        return [
            _event_with_missing_fee(event, reason="option_fee_estimate_failed")
            for event in events
        ]
    total = quantize_money(estimate.amount)
    allocated = Decimal(0)
    by_id: dict[str, TradeEvent] = {}
    for index, event in enumerate(rows):
        amount = (
            quantize_money(total - allocated)
            if index == len(rows) - 1
            else quantize_money(total * Decimal(event.contracts) / Decimal(total_contracts))
        )
        allocated = quantize_money(allocated + amount)
        by_id[event.event_id] = _event_with_estimated_fee(
            event,
            amount=amount,
            estimate=estimate,
            frozen_at_ms=frozen_at_ms,
        )
    return [by_id[event.event_id] for event in events]

def _freeze_new_event_fee(event: TradeEvent, *, frozen_at_ms: int) -> TradeEvent:
    zero_reason = zero_option_fee_lifecycle_reason(event)
    if zero_reason and event.event_type == "assignment":
        resolution = _incoming_fee_resolution(event)
        if resolution["status"] == "actual":
            return _event_with_actual_fee(
                event,
                amount=resolution["amount"],
                source=str(resolution["source"]),
                frozen_at_ms=frozen_at_ms,
            )
        if resolution["status"] == "missing":
            return _event_with_missing_fee(
                event,
                reason=str(resolution["reason"]),
                diagnostics=list(resolution.get("diagnostics") or []),
            )
        return _event_with_actual_fee(
            event,
            amount=Decimal(0),
            source="option_assignment_lifecycle",
            reason=zero_reason,
            frozen_at_ms=frozen_at_ms,
        )
    if event.event_type == "expire_close":
        resolution = _incoming_fee_resolution(event)
        if resolution["status"] == "actual":
            return _event_with_actual_fee(
                event,
                amount=resolution["amount"],
                source=str(resolution["source"]),
                frozen_at_ms=frozen_at_ms,
            )
        if resolution["status"] == "explicit_missing":
            return replace(event, fees=0.0)
        if resolution["status"] == "missing":
            return _event_with_missing_fee(
                event,
                reason=str(resolution["reason"]),
                diagnostics=list(resolution.get("diagnostics") or []),
            )
        payload = event.raw_payload or {}
        if zero_reason:
            return _event_with_actual_fee(
                event,
                amount=Decimal(0),
                source="option_expiry_lifecycle",
                reason=zero_reason,
                frozen_at_ms=frozen_at_ms,
            )
        return _event_with_missing_fee(
            event,
            reason=(
                "broker_order_fee_pending"
                if str(payload.get("order_id") or "").strip()
                else "broker_order_identity_missing"
            ),
        )
    if event.event_type not in {"open", "close"}:
        return event
    if normalize_broker(event.contract_key.broker) != "富途":
        return _event_with_missing_fee(
            event,
            reason="unsupported_broker_fee_schedule",
        )
    resolution = _incoming_fee_resolution(event)
    if resolution["status"] == "actual":
        return _event_with_actual_fee(
            event,
            amount=resolution["amount"],
            source=str(resolution["source"]),
            frozen_at_ms=frozen_at_ms,
        )
    if resolution["status"] == "explicit_missing":
        return replace(event, fees=0.0)
    if resolution["status"] == "missing":
        return _event_with_missing_fee(
            event,
            reason=str(resolution["reason"]),
            diagnostics=list(resolution.get("diagnostics") or []),
        )
    try:
        estimate = estimate_futu_executed_option_fee(
            event.currency,
            event.price,
            contracts=int(event.contracts),
            multiplier=int(event.multiplier),
            is_sell=(
                event.contract_key.position_side == "short"
                if event.event_type == "open"
                else event.contract_key.position_side == "long"
            ),
        )
    except (TypeError, ValueError):
        return _event_with_missing_fee(event, reason="option_fee_estimate_failed")
    return _event_with_estimated_fee(
        event,
        amount=quantize_money(estimate.amount),
        estimate=estimate,
        frozen_at_ms=frozen_at_ms,
    )

def _incoming_fee_resolution(event: TradeEvent) -> dict[str, Any]:
    payload = dict(event.raw_payload or {})
    provenance = payload.get("fee_provenance")
    basis = ""
    if provenance is not None and not isinstance(provenance, Mapping):
        return _missing_fee_resolution("actual_fee_candidate_invalid")
    if isinstance(provenance, Mapping):
        basis = str(provenance.get("basis") or "").strip().lower()
        if basis not in {"actual", "estimated", "missing"}:
            return _missing_fee_resolution("actual_fee_candidate_invalid")

    raw_candidates, raw_present, raw_problem = _raw_actual_fee_candidates(payload)
    top_amount, top_problem = _optional_fee_amount(event.fees)
    top_present = top_problem is not None or (top_amount is not None and top_amount != 0)
    explicit_actual = basis == "actual"
    incoming_actual = bool(explicit_actual or raw_present or top_present)
    if not incoming_actual:
        if basis == "missing":
            return {"status": "explicit_missing", "diagnostics": []}
        return {"status": "formula", "diagnostics": []}
    if not _trusted_actual_fee_context(event):
        return _missing_fee_resolution("actual_fee_evidence_not_admitted")
    if top_problem is not None or raw_problem is not None:
        return _missing_fee_resolution("actual_fee_candidate_invalid")
    if top_present and basis in {"estimated", "missing"}:
        return _missing_fee_resolution("actual_fee_basis_conflict")

    candidates: list[tuple[int, str, Decimal, str]] = []
    if explicit_actual:
        amount, problem = _optional_fee_amount(provenance.get("amount"))
        if problem is not None:
            return _missing_fee_resolution("actual_fee_candidate_invalid")
        if amount is None:
            amount = top_amount or Decimal(0)
        candidates.append(
            (
                0,
                "explicit_provenance",
                amount,
                str(provenance.get("source") or "opend.trade_event"),
            )
        )
    candidates.extend(raw_candidates)
    if top_present and top_amount is not None:
        candidates.append((3, "legacy_top_level", top_amount, "legacy_top_level"))
    diagnostics = [
        {"source": label, "amount": format(amount, "f")}
        for _priority, label, amount, _source in sorted(
            candidates,
            key=lambda item: (item[1], item[2]),
        )
    ]
    if not candidates:
        return _missing_fee_resolution("actual_fee_candidate_invalid", diagnostics)
    if len({amount for _priority, _label, amount, _source in candidates}) != 1:
        return _missing_fee_resolution("actual_fee_candidates_conflict", diagnostics)
    selected = min(candidates, key=lambda item: (item[0], item[1]))
    return {
        "status": "actual",
        "amount": selected[2],
        "source": selected[3],
        "diagnostics": diagnostics,
    }

def _trusted_actual_fee_context(event: TradeEvent) -> bool:
    payload = event.raw_payload or {}
    return bool(
        normalize_broker(event.contract_key.broker) == "富途"
        and event.source == "opend_push"
        and str(payload.get("source_type") or "").strip().lower()
        == "broker_trade_event"
        and str(payload.get("futu_account_id") or "").strip()
        and str(payload.get("order_id") or "").strip()
    )

def _raw_actual_fee_candidates(
    payload: Mapping[str, Any],
) -> tuple[list[tuple[int, str, Decimal, str]], bool, str | None]:
    candidates: list[tuple[int, str, Decimal, str]] = []
    present = False
    for source_name, source in _raw_fee_sources(payload):
        totals = [(key, source.get(key)) for key in _ACTUAL_FEE_TOTAL_KEYS if key in source]
        components = [
            (key, source.get(key))
            for key in _ACTUAL_FEE_COMPONENT_KEYS
            if key in source
        ]
        if totals:
            present = True
            for key, value in totals:
                amount, problem = _optional_fee_amount(value)
                if problem is not None or amount is None:
                    return [], True, "actual_fee_candidate_invalid"
                candidates.append(
                    (1, f"{source_name}.{key}", amount, f"opend.trade_event.{source_name}.{key}")
                )
        elif components:
            present = True
            amount = Decimal(0)
            for _key, value in components:
                component, problem = _optional_fee_amount(value, absolute=True)
                if problem is not None or component is None:
                    return [], True, "actual_fee_candidate_invalid"
                amount = quantize_money(amount + component)
            candidates.append(
                (2, f"{source_name}.components", amount, f"opend.trade_event.{source_name}.components")
            )
    return candidates, present, None

def _raw_fee_sources(payload: Mapping[str, Any]) -> list[tuple[str, Mapping[str, Any]]]:
    sources: list[tuple[str, Mapping[str, Any]]] = [("raw_payload", payload)]
    for key in _ACTUAL_FEE_RAW_SOURCE_KEYS:
        value = payload.get(key)
        if isinstance(value, Mapping) and value is not payload:
            sources.append((key, value))
    return sources

def _optional_fee_amount(
    value: Any,
    *,
    absolute: bool = False,
) -> tuple[Decimal | None, str | None]:
    if value in (None, ""):
        return None, None
    try:
        amount = quantize_money(to_decimal(value, field_name="actual fee candidate"))
    except (InvalidOperation, TypeError, ValueError):
        return None, "actual_fee_candidate_invalid"
    if not amount.is_finite():
        return None, "actual_fee_candidate_invalid"
    if absolute:
        amount = abs(amount)
    elif amount < 0:
        return None, "actual_fee_candidate_invalid"
    return amount, None

def _missing_fee_resolution(
    reason: str,
    diagnostics: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    return {
        "status": "missing",
        "reason": reason,
        "diagnostics": [dict(item) for item in diagnostics],
    }

def _group_fee_candidate_diagnostics(
    resolutions: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    unique = {
        json.dumps(dict(item), ensure_ascii=False, sort_keys=True)
        for resolution in resolutions
        for item in resolution.get("diagnostics") or []
        if isinstance(item, Mapping)
    }
    return [json.loads(item) for item in sorted(unique)]

def _event_with_estimated_fee(
    event: TradeEvent,
    *,
    amount: Decimal,
    estimate: Any,
    frozen_at_ms: int,
) -> TradeEvent:
    payload = dict(event.raw_payload or {})
    payload["fee_provenance"] = {
        "basis": "estimated",
        "amount": canonical_decimal_text(amount),
        "source": "formula",
        "reason": "executed_option_fee_formula",
        "formula_version": estimate.fee_schedule_version,
        "formula_basis": estimate.fee_basis,
        "schedule_reference": estimate.fee_schedule_url,
        "frozen_at_ms": int(frozen_at_ms),
    }
    return replace(event, fees=0.0, raw_payload=payload)

def _event_with_actual_fee(
    event: TradeEvent,
    *,
    amount: Decimal,
    source: str,
    frozen_at_ms: int,
    reason: str = "broker_receipt_fee",
) -> TradeEvent:
    payload = dict(event.raw_payload or {})
    incoming = payload.get("fee_provenance")
    provenance = {
        key: incoming[key]
        for key in (
            "provider_observed_at_ms",
            "provider_batch_id",
            "fee_details_sha256",
        )
        if isinstance(incoming, Mapping) and incoming.get(key) not in (None, "")
    }
    payload["fee_provenance"] = {
        "basis": "actual",
        "amount": canonical_decimal_text(amount),
        "source": source,
        "reason": reason,
        "frozen_at_ms": int(frozen_at_ms),
        **provenance,
    }
    return replace(event, fees=float(amount), raw_payload=payload)

def _event_with_missing_fee(
    event: TradeEvent,
    *,
    reason: str,
    diagnostics: Sequence[Mapping[str, Any]] = (),
) -> TradeEvent:
    payload = dict(event.raw_payload or {})
    payload["fee_provenance"] = {
        "basis": "missing",
        "source": "writer",
        "reason": reason,
    }
    if diagnostics:
        payload["fee_provenance"]["candidate_diagnostics"] = [
            dict(item) for item in diagnostics
        ]
    return replace(event, fees=0.0, raw_payload=payload)

def _event_with_existing_fee_evidence(event: TradeEvent, existing: dict[str, Any]) -> TradeEvent:
    existing_payload = existing.get("raw_payload")
    if not isinstance(existing_payload, dict):
        return event
    incoming_payload = dict(event.raw_payload or {})
    if "fee_provenance" in incoming_payload or quantize_money(event.fees) != 0:
        stored_provenance = existing_payload.get("fee_provenance")
        if (
            isinstance(stored_provenance, dict)
            and stored_provenance.get("basis") == "missing"
            and stored_provenance.get("reason") == "actual_fee_evidence_not_admitted"
        ):
            incoming_payload["fee_provenance"] = dict(stored_provenance)
            event = replace(
                event,
                fees=float(existing.get("fees") or 0.0),
                raw_payload=incoming_payload,
            )
        if "cash_conversions" not in incoming_payload and isinstance(
            existing_payload.get("cash_conversions"), dict
        ):
            incoming_payload["cash_conversions"] = dict(existing_payload["cash_conversions"])
            event = replace(event, raw_payload=incoming_payload)
        incoming = encode_trade_event_for_storage(event).event_json
        stored = encode_trade_event_for_storage(existing).event_json
        if incoming != stored:
            raise ValueError(f"trade event fee evidence conflict for event_id={event.event_id}")
        return event
    if "fee_provenance" in existing_payload:
        incoming_payload["fee_provenance"] = dict(existing_payload["fee_provenance"])
    return replace(event, fees=float(existing.get("fees") or 0.0), raw_payload=incoming_payload)

def _event_with_existing_cash_conversions(event: TradeEvent, existing: dict[str, Any]) -> TradeEvent:
    existing_raw_payload = existing.get("raw_payload")
    if not isinstance(existing_raw_payload, dict):
        return event
    conversions = existing_raw_payload.get("cash_conversions")
    if not isinstance(conversions, dict):
        return event
    raw_payload = dict(event.raw_payload or {})
    raw_payload["cash_conversions"] = dict(conversions)
    return replace(event, raw_payload=raw_payload)

def _normal_close_notification_intent(
    events: Sequence[TradeEvent],
) -> dict[str, Any] | None:
    rows = list(events)
    if not rows or any(item.event_type != "close" for item in rows):
        return None
    first = rows[0]
    raw = dict(first.raw_payload or {})
    source_deal_id = str(raw.get("source_deal_id") or "").strip()
    futu_account_id = str(raw.get("futu_account_id") or "").strip()
    account = str(first.contract_key.account or "").strip().lower()
    if not source_deal_id or not futu_account_id or not account:
        return None
    broker_deal_key = (
        f"futu:{account}:{futu_account_id}:{source_deal_id}"
    )
    case_id = f"close:{broker_deal_key}"
    ordered = sorted(
        rows,
        key=lambda item: (
            str(item.target_lot_id or ""),
            str(item.event_id or ""),
        ),
    )
    payload = {
        "schema_version": "broker_close_notification.v1",
        "case_id": case_id,
        "transition_type": "resolution_confirmed",
        "resolution_revision": 1,
        "broker_deal_key": broker_deal_key,
        "account": account,
        "futu_account_id": futu_account_id,
        "symbol": first.contract_key.underlying_symbol,
        "option_type": first.contract_key.option_type,
        "position_side": first.contract_key.position_side,
        "strike": first.contract_key.strike,
        "expiration_ymd": first.contract_key.expiration_ymd,
        "execution_time_ms": int(first.event_time_ms or 0),
        "currency": first.currency,
        "total_contracts": sum(int(item.contracts) for item in ordered),
        "events": [
            {
                "event_id": item.event_id,
                "target_lot_id": item.target_lot_id,
                "contracts": int(item.contracts),
            }
            for item in ordered
        ],
    }
    state_fingerprint = canonical_state_fingerprint(
        {
            "schema_version": "broker_close_split_state.v1",
            "broker_deal_key": broker_deal_key,
            "account": account,
            "futu_account_id": futu_account_id,
            "contract": {
                "symbol": first.contract_key.underlying_symbol,
                "option_type": first.contract_key.option_type,
                "position_side": first.contract_key.position_side,
                "strike": canonical_decimal_text(
                    first.contract_key.strike
                ),
                "expiration_ymd": first.contract_key.expiration_ymd,
            },
            "execution_time_ms": int(first.event_time_ms or 0),
            "events": payload["events"],
            "total_contracts": payload["total_contracts"],
        }
    )
    payload["state_fingerprint"] = state_fingerprint
    return build_notification_intent(
        case_id=case_id,
        transition_type="resolution_confirmed",
        resolution_revision=1,
        delivery_revision=0,
        transition_key=f"{case_id}:resolution_confirmed",
        state_fingerprint=state_fingerprint,
        payload=payload,
    )

def persist_trade_event(repo: Any, deal: Any) -> LedgerWriteResult:
    return persist_trade_event_object(repo, _trade_event_from_normalized_deal(deal))

def persist_normalized_trade_events_atomically(
    repo: Any,
    deals: Sequence[Any],
) -> list[LedgerWriteResult]:
    """Persist explicitly targeted broker-event splits in one transaction."""

    events = [_trade_event_from_normalized_deal(deal) for deal in deals]
    return persist_trade_event_objects_atomically(repo, events)

def persist_trade_event_objects_atomically(
    repo: Any,
    events: Sequence[Any],
    *,
    lifecycle_case_update: dict[str, Any] | None = None,
    lifecycle_allocations: Sequence[dict[str, Any]] | None = None,
    wheel_start_enabled: bool = False,
    wheel_intent_coverage_fact: Mapping[str, Any] | None = None,
) -> list[LedgerWriteResult]:
    """Persist explicitly targeted canonical events in one transaction."""

    from src.application.ledger.wheel_trade_companions import (
        append_and_verify_wheel_intent_consumption,
        append_wheel_trade_companions,
        capture_wheel_trade_companion_context,
        prepare_wheel_intent_open_event,
    )

    events = list(events)
    case_update = dict(lifecycle_case_update or {})
    allocation_rows = [dict(item or {}) for item in (lifecycle_allocations or [])]
    if not events:
        raise ValueError("atomic trade persistence requires at least one event")

    def _run(sqlite_repo: Any, conn: Any | None) -> list[LedgerWriteResult]:
        if conn is None:
            raise TypeError("atomic trade persistence requires SQLite transaction authority")
        storage_events: list[TradeEvent] = []
        for event in events:
            expanded = [
                _canonical_storage_event(item)
                for item in _events_for_storage(sqlite_repo, event, conn=conn)
            ]
            if len(expanded) != 1:
                raise ValueError(
                    "atomic trade persistence requires explicitly targeted events"
                )
            storage_events.append(expanded[0])

        event_ids = [event.event_id for event in storage_events]
        if len(set(event_ids)) != len(event_ids):
            raise ValueError("atomic trade persistence contains duplicate event_id")

        existing_by_id = _trade_events_by_id(
            sqlite_repo,
            event_ids,
            conn=conn,
        )
        wheel_intent_events: dict[str, dict[str, Any]] = {}
        wheel_linkage_status: dict[str, str] = {}
        if wheel_intent_coverage_fact is not None:
            if len(storage_events) != 1:
                raise ValueError("Wheel intent intake requires one unsplit fill")
            original = storage_events[0]
            if original.event_id in existing_by_id:
                wheel_linkage_status[original.event_id] = "existing_trade_event"
            else:
                rows = sqlite_repo.read_lifecycle_account_rows(
                    account=original.contract_key.account,
                    conn=conn,
                )
                linked, intent_event, status = prepare_wheel_intent_open_event(
                    rows,
                    original,
                    wheel_intent_coverage_fact,
                    recorded_at_ms=utc_now_ms(),
                )
                storage_events[0] = linked
                wheel_linkage_status[original.event_id] = status
                if intent_event is not None:
                    wheel_intent_events[original.event_id] = intent_event
        observed_at_ms = utc_now_ms()
        storage_events = _prepare_fee_evidence_for_storage(
            storage_events,
            existing_by_id=existing_by_id,
            frozen_at_ms=observed_at_ms,
        )
        fx_payload = load_cash_fx_payload(sqlite_repo)
        storage_events = [
            _event_with_existing_cash_conversions(event, existing_by_id[event.event_id])
            if event.event_id in existing_by_id
            else attach_trade_event_cash_conversions(
                event,
                fx_payload=fx_payload,
                observed_at_ms=observed_at_ms,
            )
            for event in storage_events
        ]
        wheel_context = capture_wheel_trade_companion_context(
            sqlite_repo,
            conn=conn,
            events=storage_events,
            wheel_start_enabled=wheel_start_enabled,
        )
        prior_case_fact: dict[str, Any] | None = None
        if case_update:
            case_id_value = str(case_update.get("case_id") or "").strip()
            existing_case = sqlite_repo.get_trade_lifecycle_case(
                case_id_value,
                conn=conn,
            )
            decision_fence, prior_case_fact = (
                _begin_lifecycle_decision_projection(
                    sqlite_repo,
                    conn=conn,
                    lifecycle_case=existing_case or case_update,
                    allow_missing_fact=existing_case is None,
                    global_event_owner=True,
                )
            )
        else:
            decision_fence = capture_trade_event_decision_projection_fence(
                sqlite_repo,
                conn=conn,
            )
        runtime = run_position_projection_in_transaction(
            sqlite_repo,
            storage_events,
            conn=conn,
            mode=_projection_mode_for_events(
                storage_events,
                force_full=bool(case_update or allocation_rows),
            ),
        )
        created_flags = runtime.created_flags
        wheel_companions = append_wheel_trade_companions(
            sqlite_repo,
            conn=conn,
            events=storage_events,
            created_flags=created_flags,
            context=wheel_context,
            recorded_at_ms=observed_at_ms,
        )
        for event, created in zip(storage_events, created_flags, strict=True):
            intent_event = wheel_intent_events.get(event.event_id)
            if intent_event is None:
                continue
            if not created:
                raise ValueError("Wheel intent-linked fill unexpectedly replayed")
            append_and_verify_wheel_intent_consumption(
                sqlite_repo,
                conn=conn,
                linked_event=event,
                intent_event=intent_event,
            )
        notification_intent = _normal_close_notification_intent(
            storage_events
        )
        outbox_created = False
        notification_history_unseeded = False
        if notification_intent is not None:
            if any(created_flags) and not all(created_flags):
                raise ValueError(
                    "broker close split replay is only partially present"
                )
            if all(created_flags):
                outbox_created = (
                    sqlite_repo.insert_trade_lifecycle_notification_once(
                        notification_intent,
                        conn=conn,
                    )
                )
            elif not any(created_flags):
                existing_transition = (
                    sqlite_repo.get_trade_lifecycle_notification_by_transition(
                        transition_key=str(
                            notification_intent["transition_key"]
                        ),
                        delivery_revision=0,
                        conn=conn,
                    )
                )
                notification_history_unseeded = (
                    existing_transition is None
                )
        if case_update:
            upsert_case = getattr(sqlite_repo, "upsert_trade_lifecycle_case", None)
            if not callable(upsert_case):
                raise TypeError("repository cannot persist lifecycle case state")
            upsert_case(case_update, conn=conn)
        allocation_created: list[bool] = []
        if allocation_rows:
            bind_evidence = getattr(
                sqlite_repo,
                "bind_trade_lifecycle_evidence_case_once",
                None,
            )
            insert_allocation = getattr(
                sqlite_repo,
                "insert_trade_lifecycle_allocation",
                None,
            )
            if not callable(bind_evidence) or not callable(insert_allocation):
                raise TypeError("repository cannot persist lifecycle allocations")
            for case_id, evidence_id in sorted(
                {
                    (
                        str(row.get("case_id") or "").strip(),
                        str(row.get("evidence_id") or "").strip(),
                    )
                    for row in allocation_rows
                }
            ):
                bind_evidence(
                    evidence_id=evidence_id,
                    case_id=case_id,
                    conn=conn,
                )
            for row in allocation_rows:
                allocation_created.append(
                    bool(insert_allocation(row, conn=conn))
                )
            sqlite_repo.assert_foreign_keys_clean(conn=conn)
        event_mutations = tuple(
            zip(storage_events, created_flags, strict=True)
        )
        if case_update:
            decision_projection = (
                _defer_lifecycle_decision_projection(decision_fence)
                if any(
                    created
                    and event.event_type == "void"
                    for event, created in event_mutations
                )
                else _finish_lifecycle_decision_projection(
                    sqlite_repo,
                    conn=conn,
                    fence=decision_fence,
                    prior_fact=prior_case_fact,
                    case_id=str(case_update.get("case_id") or ""),
                    resolution=_lifecycle_resolution_after_allocations(
                        prior_case_fact,
                        allocations=allocation_rows,
                        created_flags=allocation_created,
                    ),
                    trade_event_mutations=event_mutations,
                )
            )
        else:
            decision_projection = _finish_trade_event_decision_projection(
                sqlite_repo,
                conn=conn,
                fence=decision_fence,
                events=storage_events,
                created_flags=created_flags,
            )
        diagnostics = projection_diagnostics_summary(runtime.diagnostics)
        return [
            LedgerWriteResult.from_payload(
                {
                    "event_id": event.event_id,
                    "record_id": (
                        str(
                            (event.raw_payload or {}).get("record_id")
                            or (event.raw_payload or {}).get("target_lot_id")
                            or event.target_lot_id
                            or ""
                        ).strip()
                        or None
                    ),
                    "created": bool(created),
                    "position_lot_count": int(runtime.position_lot_count),
                    "decision_projection": decision_projection,
                    **diagnostics,
                    **(
                        {"wheel_event_id": wheel_companions[event.event_id]}
                        if event.event_id in wheel_companions
                        else {}
                    ),
                    **(
                        {
                            "wheel_intent_event_id": (
                                wheel_intent_events.get(event.event_id) or {}
                            ).get("event_id"),
                            "wheel_linkage_status": wheel_linkage_status[event.event_id],
                        }
                        if event.event_id in wheel_linkage_status
                        else {}
                    ),
                    **(
                        {
                            "notification_outbox_id": notification_intent[
                                "outbox_id"
                            ],
                            "notification_outbox_created": outbox_created,
                            "notification_history_unseeded": (
                                notification_history_unseeded
                            ),
                        }
                        if notification_intent is not None
                        else {}
                    ),
                }
            )
            for event, created in zip(storage_events, created_flags, strict=True)
        ]

    return with_sqlite_repo_transaction(
        repo,
        _run,
        require_projection_publication=True,
    )

def persist_trade_event_with_wheel_intent(
    repo: Any,
    deal: Any,
    coverage_fact: Mapping[str, Any],
) -> LedgerWriteResult:
    return persist_trade_event_objects_atomically(
        repo,
        [_trade_event_from_normalized_deal(deal)],
        wheel_intent_coverage_fact=coverage_fact,
    )[0]

def _events_for_storage(
    repo: Any,
    event: Any,
    *,
    conn: Any | None = None,
) -> list[Any]:
    if hasattr(event, "event_type") and not hasattr(event, "position_effect"):
        if bool(getattr(event, "is_close", False)) and not getattr(event, "target_lot_id", None):
            return _canonical_close_events_for_storage(repo, event, conn=conn)
        return [event]
    if str(event.position_effect or "").strip().lower() != "close":
        return [event]
    payload = dict(event.raw_payload or {})
    if str(payload.get("record_id") or payload.get("target_lot_id") or "").strip():
        return [event]
    selector = LotCloseSelector.from_values(
        broker=event.broker,
        account=event.account,
        symbol=event.symbol,
        option_type=event.option_type,
        position_side=_close_position_side(event),
        strike=event.strike,
        expiration_ymd=event.expiration_ymd,
        contracts_to_close=event.contracts,
    )
    try:
        resolution = resolve_fifo_close_targets(
            repo,
            selector,
            source="stored_trade_close",
            conn=conn,
        )
    except LotCloseResolutionError as exc:
        raise ValueError(f"close trade event target resolution failed: {exc.code}") from exc
    out: list[TradeEvent] = []
    resolution_payload = resolution.to_dict()
    for index, match in enumerate(resolution.matches):
        event_id = event.event_id if index == 0 else f"{event.event_id}:target:{match.record_id}"
        match_payload = {
            **payload,
            "fee_order_group_id": event.event_id,
            "record_id": match.record_id,
            "target_lot_id": match.record_id,
            "close_target_resolution": resolution_payload,
        }
        source_event_id = getattr(match.candidate, "source_event_id", None)
        if source_event_id not in (None, ""):
            match_payload["close_target_source_event_id"] = source_event_id
        out.append(
            replace(
                event,
                event_id=event_id,
                contracts=int(match.contracts_to_close),
                raw_payload=match_payload,
            )
        )
    return out

def _close_position_side(event: Any) -> str:
    trade_side = normalize_trade_side(event.side)
    if trade_side == "buy":
        return "short"
    if trade_side == "sell":
        return "long"
    return str(event.side or "").strip().lower()

def _canonical_close_events_for_storage(
    repo: Any,
    event: TradeEvent,
    *,
    conn: Any | None = None,
) -> list[TradeEvent]:
    selector = LotCloseSelector.from_values(
        broker=event.contract_key.broker,
        account=event.contract_key.account,
        symbol=event.contract_key.underlying_symbol,
        option_type=event.contract_key.option_type,
        position_side=event.contract_key.position_side,
        strike=event.contract_key.strike,
        expiration_ymd=event.contract_key.expiration_ymd,
        contracts_to_close=event.contracts,
    )
    try:
        resolution = resolve_fifo_close_targets(
            repo,
            selector,
            source="stored_canonical_trade_close",
            conn=conn,
        )
    except LotCloseResolutionError as exc:
        raise ValueError(f"close trade event target resolution failed: {exc.code}") from exc
    out: list[TradeEvent] = []
    resolution_payload = resolution.to_dict()
    for index, match in enumerate(resolution.matches):
        event_id = event.event_id if index == 0 else f"{event.event_id}:target:{match.record_id}"
        raw_payload = {
            **dict(event.raw_payload or {}),
            "fee_order_group_id": event.event_id,
            "record_id": match.record_id,
            "target_lot_id": match.record_id,
            "close_target_resolution": resolution_payload,
        }
        source_event_id = getattr(match.candidate, "source_event_id", None)
        if source_event_id not in (None, ""):
            raw_payload["close_target_source_event_id"] = source_event_id
        out.append(
            replace(
                event,
                event_id=event_id,
                contracts=int(match.contracts_to_close),
                target_lot_id=match.record_id,
                raw_payload=raw_payload,
            )
        )
    return out

def _trade_event_from_normalized_deal(deal: Any) -> TradeEvent:
    trade_side = normalize_trade_side(getattr(deal, "side", None)) or ""
    position_effect = normalize_position_effect(getattr(deal, "position_effect", None)) or ""
    raw_payload = strip_retired_strategy_metadata(
        dict(getattr(deal, "raw_payload", {}) or {})
    )
    raw_payload.pop("fields", None)
    source_deal_id = str(getattr(deal, "deal_id", "") or "").strip()
    event_id = broker_external_event_key(deal)
    event_type = _event_type_from_position_effect(position_effect, raw_payload=raw_payload)
    position_side = _position_side_from_trade(effect=position_effect, trade_side=trade_side)
    raw_payload.setdefault("source_type", "broker_trade_event")
    raw_payload.setdefault("source", "api")
    if source_deal_id:
        raw_payload.setdefault("source_deal_id", source_deal_id)
    futu_account_id = str(getattr(deal, "futu_account_id", "") or "").strip()
    if futu_account_id:
        raw_payload.setdefault("futu_account_id", futu_account_id)
    if event_id:
        raw_payload.setdefault("external_event_key", event_id)
    raw_payload.setdefault("side", trade_side)
    order_id = str(getattr(deal, "order_id", "") or "").strip()
    if order_id:
        raw_payload.setdefault("order_id", order_id)
    multiplier_source = str(getattr(deal, "multiplier_source", "") or "").strip()
    if multiplier_source:
        raw_payload.setdefault("multiplier_source", multiplier_source)
    event_time_ms = _required_broker_trade_time_ms(deal)
    contract_key = ContractKey.from_values(
        broker=getattr(deal, "broker", None) or "富途",
        account=getattr(deal, "internal_account", None) or "",
        underlying_symbol=canonical_contract_symbol(getattr(deal, "symbol", "")),
        option_type=getattr(deal, "option_type", None) or "",
        position_side=position_side,
        strike=getattr(deal, "strike", None),
        expiration_ymd=normalize_contract_expiration(getattr(deal, "expiration_ymd", None)),
    )
    return TradeEvent(
        event_id=event_id,
        event_type=event_type,
        event_time_ms=event_time_ms,
        contract_key=contract_key,
        contracts=int(getattr(deal, "contracts", 0) or 0),
        price=float(getattr(deal, "price", 0.0) or 0.0),
        currency=normalize_currency(getattr(deal, "currency", None)),
        source="opend_push",
        multiplier=float(getattr(deal, "multiplier", None) or 100),
        fees=0.0,
        target_lot_id=str(raw_payload.get("target_lot_id") or raw_payload.get("record_id") or "").strip() or None,
        raw_payload=raw_payload,
    )

def _event_type_from_position_effect(position_effect: str, *, raw_payload: dict[str, Any] | None = None) -> str:
    if position_effect == "open":
        return "open"
    if position_effect == "close":
        payload = raw_payload if isinstance(raw_payload, dict) else {}
        close_type = str(payload.get("close_type") or payload.get("broker_close_type") or "").strip().lower()
        if close_type in {"expire_auto_close", "expire_close", "expiration_close", "expiration_zero_close"}:
            return "expire_close"
        return "close"
    if position_effect in {"adjust", "void"}:
        return position_effect
    return position_effect

def _required_broker_trade_time_ms(deal: Any) -> int:
    raw = getattr(deal, "trade_time_ms", None)
    if raw in (None, ""):
        value = 0
    else:
        try:
            value = int(raw)
        except Exception:
            value = 0
    if value <= 0:
        deal_id = str(getattr(deal, "deal_id", "") or "").strip()
        suffix = f" deal_id={deal_id}" if deal_id else ""
        raise ValueError(f"broker trade event requires positive trade_time_ms; refusing event_time_ms=0{suffix}")
    return value

def _position_side_from_trade(*, effect: str, trade_side: str) -> str:
    if effect == "open":
        return "short" if trade_side == "sell" else "long"
    if effect == "close":
        return "short" if trade_side == "buy" else "long"
    return trade_side
