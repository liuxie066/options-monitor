from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Sequence

from domain.domain.combo_identity import (
    build_combo_identity_intent,
    identity_from_intent,
    validate_combo_identity,
)
from domain.domain.fee_calc import extract_actual_fees
from domain.domain.ledger import ContractKey, TradeEvent
from domain.domain.ledger.position_fields import (
    effective_contracts_open,
    effective_expiration_ymd,
    effective_multiplier,
    effective_strike,
)
from domain.domain.lifecycle_allocation import (
    allocation_id_for,
    resolve_allocations,
    terminal_event_id_for,
)
from domain.domain.option_position_identity import normalize_currency
from domain.domain.option_lifecycle import (
    build_lifecycle_case,
    expiration_observation_start_ms,
)
from domain.domain.performance.models import canonical_decimal_text, quantize_money, to_decimal
from domain.domain.symbol_identity import canonical_symbol, symbol_market
from domain.domain.trade_contract_identity import (
    canonical_contract_symbol,
    normalize_contract_expiration,
    normalize_position_effect,
    normalize_trade_side,
)
from src.application.ledger.lot_resolver import LotCloseResolutionError, LotCloseSelector, resolve_fifo_close_targets
from src.application.ledger.combo_membership import (
    ComboMembershipResolution,
    resolve_combo_group_membership,
)
from src.application.ledger.lifecycle_overlay import (
    lifecycle_case_generation_token,
    lifecycle_evidence_facts,
    resolve_lifecycle_account_rows,
)
from src.application.ledger.notification_outbox import (
    build_notification_intent,
    canonical_payload_hash,
    canonical_state_fingerprint,
)
from src.application.ledger.source_consumption import (
    build_source_consumption_claim,
)
from src.application.ledger.event_codec import (
    encode_trade_event_for_storage,
    valid_void_target_event_id,
)
from src.application.ledger.external_event_key import broker_external_event_key
from src.application.ledger.publisher import (
    ensure_projection_publishable,
    project_stored_trade_events_to_position_lots,
)
from src.application.ledger.projection_verify import (
    compare_projection_lots,
)
from src.application.ledger.repository import with_sqlite_repo_transaction
from src.application.ledger.results import LedgerWriteResult, ProjectionRefreshResult
from src.application.cash_conversion import (
    attach_trade_event_cash_conversions,
    load_cash_fx_payload,
    utc_now_ms,
)


def projection_diagnostics_summary(diagnostics: Sequence[Any]) -> dict[str, Any]:
    explicit_close_codes = {
        "close_explicit_target_not_found",
        "close_explicit_target_conflict",
        "close_explicit_target_already_closed",
        "close_explicit_target_mismatch",
        "close_explicit_target_oversized",
        "close_explicit_source_event_target_not_found",
        "close_explicit_source_event_target_already_closed",
        "close_explicit_source_event_target_mismatch",
        "close_explicit_source_event_target_oversized",
        "target_lot_id_required",
        "target_lot_not_found",
        "target_contract_mismatch",
        "target_lot_already_closed",
        "close_contracts_exceed_open",
    }
    return {
        "projection_diagnostic_count": int(len(diagnostics)),
        "unmatched_explicit_close_count": int(sum(1 for item in diagnostics if item.code in explicit_close_codes)),
        "unmatched_heuristic_close_count": int(
            sum(1 for item in diagnostics if item.code == "close_unmatched_contracts")
        ),
        "projection_diagnostics": [item.to_dict() for item in diagnostics],
    }


def safe_int_count(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _require_lifecycle_generation(
    sqlite_repo: Any,
    *,
    conn: Any,
    case_id: str,
    expected_generation_token: str | None,
) -> None:
    expected = str(expected_generation_token or "").strip()
    if not expected:
        return
    lifecycle_case = sqlite_repo.get_trade_lifecycle_case(
        case_id,
        conn=conn,
    )
    if lifecycle_case is None:
        raise ValueError(f"lifecycle case not found: {case_id}")
    rows = sqlite_repo.read_lifecycle_account_rows(
        account=str(lifecycle_case.get("account") or ""),
        conn=conn,
    )
    resolution = resolve_lifecycle_account_rows(rows)
    token = lifecycle_case_generation_token(
        resolution,
        case_id=case_id,
    )
    observed = str(
        (token or {}).get("generation_token") or ""
    ).strip()
    if observed != expected:
        raise ValueError(
            "lifecycle generation compare-and-set failed"
        )


def rebuild_position_lots_from_trade_events(repo: Any) -> ProjectionRefreshResult:
    def _run(sqlite_repo: Any, conn: Any | None) -> ProjectionRefreshResult:
        if conn is not None:
            events = sqlite_repo.list_trade_events(conn=conn)
            projection = project_stored_trade_events_to_position_lots(events)
            ensure_projection_publishable(projection, operation="position projection rebuild")
            inserted = sqlite_repo.replace_position_lots(projection.lots, conn=conn)
        else:
            events = sqlite_repo.list_trade_events()
            projection = project_stored_trade_events_to_position_lots(events)
            ensure_projection_publishable(projection, operation="position projection rebuild")
            inserted = sqlite_repo.replace_position_lots(projection.lots)
        result = {
            "trade_event_count": int(len(events)),
            "position_lot_count": int(inserted),
        }
        result.update(projection_diagnostics_summary(projection.diagnostics))
        return ProjectionRefreshResult.from_payload(result)

    return with_sqlite_repo_transaction(repo, _run)


def persist_trade_event_object(repo: Any, event: Any) -> LedgerWriteResult:
    def _run(sqlite_repo: Any, conn: Any | None) -> LedgerWriteResult:
        storage_events = [
            _canonical_storage_event(item)
            for item in _events_for_storage(sqlite_repo, event)
        ]
        existing_events = sqlite_repo.list_trade_events(conn=conn) if conn is not None else sqlite_repo.list_trade_events()
        existing_by_id = {
            str(item.get("event_id") or ""): item
            for item in existing_events
            if isinstance(item, dict) and str(item.get("event_id") or "")
        }
        fx_payload = load_cash_fx_payload(sqlite_repo)
        observed_at_ms = utc_now_ms()
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
        if conn is not None:
            created_flags = [sqlite_repo.upsert_trade_event(item, conn=conn) for item in storage_events]
            projection = project_stored_trade_events_to_position_lots(sqlite_repo.list_trade_events(conn=conn))
            ensure_projection_publishable(projection, operation="trade event persistence")
            records = projection.lots
            lot_count = sqlite_repo.replace_position_lots(records, conn=conn)
        else:
            created_flags = [sqlite_repo.upsert_trade_event(item) for item in storage_events]
            projection = project_stored_trade_events_to_position_lots(sqlite_repo.list_trade_events())
            ensure_projection_publishable(projection, operation="trade event persistence")
            records = projection.lots
            lot_count = sqlite_repo.replace_position_lots(records)
        payload = storage_events[0].raw_payload or {}
        explicit_record_id = str(payload.get("record_id") or "").strip()
        record_id = explicit_record_id or next(
            (
                record.record_id
                for record in records
                if str(record.fields.get("source_event_id") or "").strip() == str(event.event_id).strip()
            ),
            "",
        )
        result = {
            "event_id": event.event_id,
            "record_id": record_id or None,
            "created": any(created_flags),
            "position_lot_count": int(lot_count),
        }
        result.update(projection_diagnostics_summary(projection.diagnostics))
        return LedgerWriteResult.from_payload(result)

    return with_sqlite_repo_transaction(repo, _run)


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
        expanded = [_canonical_storage_event(item) for item in _events_for_storage(sqlite_repo, event)]
        if len(expanded) != 1 or expanded[0].event_type != "open":
            raise ValueError("combo identity persistence requires one explicitly targeted open event")
        storage_event = expanded[0]
        existing_events = sqlite_repo.list_trade_events(conn=conn)
        existing_by_id = {
            str(item.get("event_id") or ""): item
            for item in existing_events
            if isinstance(item, dict) and str(item.get("event_id") or "")
        }
        if storage_event.event_id in existing_by_id:
            storage_event = _event_with_existing_cash_conversions(
                storage_event,
                existing_by_id[storage_event.event_id],
            )
        else:
            storage_event = attach_trade_event_cash_conversions(
                storage_event,
                fx_payload=load_cash_fx_payload(sqlite_repo),
                observed_at_ms=utc_now_ms(),
            )
        created = sqlite_repo.upsert_trade_event(storage_event, conn=conn)
        group_id = str(intent.get("group_id") or "").strip()
        existing_identity = sqlite_repo.get_strategy_group_identity(group_id, conn=conn)
        if not created and existing_identity is None:
            raise ValueError("identity_missing_for_existing_second_leg")

        projection = project_stored_trade_events_to_position_lots(
            sqlite_repo.list_trade_events(conn=conn)
        )
        if projection.has_errors:
            codes = ",".join(
                sorted({item.code for item in projection.diagnostics if item.severity == "error"})
            )
            raise ValueError(f"combo identity projection failed: {codes}")
        sqlite_repo.replace_position_lots(projection.lots, conn=conn)
        records_by_open_event = {
            str(record.fields.get("source_event_id") or "").strip(): record
            for record in projection.lots
            if str(record.fields.get("source_event_id") or "").strip()
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
            trade_events=sqlite_repo.list_trade_events(conn=conn),
            projected_position_lots=projection.lots,
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
            trade_events=sqlite_repo.list_trade_events(conn=conn),
            projected_position_lots=projection.lots,
        )
        if membership_readback.generation_hash != membership.generation_hash:
            raise ValueError("combo identity membership generation changed")
        sqlite_repo.assert_foreign_keys_clean(conn=conn)
        return {
            "event_id": storage_event.event_id,
            "record_id": second_leg["record_id"],
            "event_created": created,
            "identity_created": identity_created,
            "identity": identity,
            "membership": membership.fact,
            "position_lot_count": len(projection.lots),
        }

    return with_sqlite_repo_transaction(repo, _run)


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
            raise ValueError("combo identity adoption requires a matching trade_events projection")
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
        records_by_id = {str(record.record_id): record for record in projection.lots}
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
        if apply_changes and existing is None:
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
        }

    return with_sqlite_repo_transaction(repo, _run)


def _existing_combo_adoption_leg(
    *,
    records_by_id: dict[str, Any],
    events_by_id: dict[str, dict[str, Any]],
    record_id: str,
    open_event_id: str,
    group_id: str,
    expected_contracts: int,
    expected_option_type: str,
    expected_position_side: str,
    accepted_roles: set[str],
    require_fully_open: bool,
) -> dict[str, Any]:
    record_value = str(record_id or "").strip()
    event_value = str(open_event_id or "").strip()
    record = records_by_id.get(record_value)
    event = events_by_id.get(event_value)
    if record is None or event is None:
        raise ValueError("combo identity adoption requires exact record and open event ids")
    fields = dict(record.fields)
    event_contract = dict(event.get("contract_key") or {}) if isinstance(event.get("contract_key"), dict) else {}
    option_type = str(fields.get("option_type") or "").strip().lower()
    position_side = str(fields.get("side") or "").strip().lower()
    role = str(fields.get("leg_role") or "").strip().lower()
    original_contracts = _combo_contract_count(fields.get("contracts"))
    open_contracts = _combo_nonnegative_contract_count(
        fields.get("contracts_open")
    )
    if (
        str(fields.get("source_event_id") or "").strip() != event_value
        or str(event.get("event_type") or "").strip().lower() != "open"
        or _combo_contract_count(event.get("contracts")) != expected_contracts
        or original_contracts != expected_contracts
        or open_contracts is None
        or open_contracts > expected_contracts
        or (require_fully_open and open_contracts != expected_contracts)
        or option_type != expected_option_type
        or position_side != expected_position_side
        or role not in accepted_roles
        or str(fields.get("strategy") or "").strip().lower() != "combo_yield"
        or str(fields.get("strategy_group_id") or "").strip() != group_id
    ):
        raise ValueError("combo identity adoption leg metadata mismatch")
    contract_key = ContractKey.from_values(
        broker=fields.get("broker"),
        account=fields.get("account"),
        underlying_symbol=fields.get("symbol"),
        option_type=option_type,
        position_side=position_side,
        strike=fields.get("strike"),
        expiration_ymd=fields.get("expiration_ymd"),
    )
    event_key = ContractKey.from_values(
        broker=event_contract.get("broker"),
        account=event_contract.get("account"),
        underlying_symbol=event_contract.get("underlying_symbol"),
        option_type=event_contract.get("option_type"),
        position_side=event_contract.get("position_side"),
        strike=event_contract.get("strike"),
        expiration_ymd=event_contract.get("expiration_ymd"),
    )
    if contract_key != event_key:
        raise ValueError("combo identity adoption contract key mismatch")
    multiplier = effective_multiplier(fields)
    currency = normalize_currency(fields.get("currency"))
    if multiplier is None or not currency:
        raise ValueError("combo identity adoption leg economics incomplete")
    return {
        "strategy_group_id": group_id,
        "strategy": "combo_yield",
        "broker": contract_key.broker,
        "account": contract_key.account,
        "symbol": contract_key.underlying_symbol,
        "leg_role": role,
        "contracts": expected_contracts,
        "open_event_id": event_value,
        "record_id": record_value,
        "contract_key": contract_key.to_dict(),
        "currency": currency,
        "multiplier": float(multiplier),
        "strike": float(contract_key.strike),
        "expiration_ymd": contract_key.expiration_ymd,
    }


def _combo_contract_count(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        numeric = Decimal(str(value))
        parsed = int(numeric)
    except (
        InvalidOperation,
        TypeError,
        ValueError,
        OverflowError,
    ):
        return None
    if not numeric.is_finite() or parsed <= 0 or numeric != parsed:
        return None
    return parsed


def _combo_nonnegative_contract_count(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        numeric = Decimal(str(value))
        parsed = int(numeric)
    except (
        InvalidOperation,
        TypeError,
        ValueError,
        OverflowError,
    ):
        return None
    if not numeric.is_finite() or parsed < 0 or numeric != parsed:
        return None
    return parsed


def _assert_combo_membership_exact(
    membership: ComboMembershipResolution,
    *,
    expected_record_ids: set[str],
    require_fully_open: bool,
) -> None:
    expected = tuple(sorted(expected_record_ids))
    if (
        membership.fact.get("status") != "exact"
        or membership.global_current_record_ids != expected
        or membership.global_historical_record_ids != expected
        or membership.retag_events
        or (
            require_fully_open
            and membership.global_live_record_ids != expected
        )
        or any(
            record_id not in expected_record_ids
            for record_id in membership.global_live_record_ids
        )
    ):
        reasons = ",".join(membership.fact.get("reason_codes") or ())
        raise ValueError(
            "combo identity membership conflict"
            + (f": {reasons}" if reasons else "")
        )


def _combo_leg_from_projected_record(
    *,
    intent: dict[str, Any],
    prefix: str,
    records_by_open_event: dict[str, Any],
) -> dict[str, Any]:
    event_id = str(intent.get(f"{prefix}_open_event_id") or "").strip()
    expected_record_id = str(intent.get(f"{prefix}_expected_record_id") or "").strip()
    role = str(intent.get(f"{prefix}_role") or "").strip().lower()
    record = records_by_open_event.get(event_id)
    if record is None or record.record_id != expected_record_id:
        raise ValueError(f"combo identity {prefix} projected record mismatch")
    fields = dict(record.fields)
    expected_contracts = int(intent.get("expected_contracts") or 0)
    if int(fields.get("contracts") or 0) != expected_contracts:
        raise ValueError(f"combo identity {prefix} original quantity mismatch")
    if int(fields.get("contracts_open") or 0) != expected_contracts:
        raise ValueError(f"combo identity {prefix} is not fully open")
    contract_key_name = (
        "funding_put"
        if role in {"funding_put", "sell_put"}
        else "participation_call"
    )
    contract_keys = intent.get("contract_keys")
    contract_key = (
        dict(contract_keys.get(contract_key_name) or {})
        if isinstance(contract_keys, dict)
        else {}
    )
    return {
        "strategy_group_id": intent.get("group_id"),
        "strategy": intent.get("strategy"),
        "account": intent.get("account"),
        "symbol": intent.get("symbol"),
        "leg_role": role,
        "contracts": expected_contracts,
        "open_event_id": event_id,
        "record_id": record.record_id,
        "contract_key": contract_key,
    }


def _lifecycle_evidence_business_fact(
    evidence: dict[str, Any],
) -> dict[str, Any]:
    payload = dict(evidence or {})
    stock = (
        dict(payload.get("stock_settlement") or {})
        if isinstance(payload.get("stock_settlement"), dict)
        else {}
    )
    fact = {
        "evidence_id": str(payload.get("evidence_id") or "").strip(),
        "source_type": str(payload.get("source_type") or "").strip(),
        "source_event_id": str(
            payload.get("source_event_id") or ""
        ).strip(),
        "evidence_type": str(
            payload.get("terminal_type")
            or payload.get("evidence_type")
            or ""
        ).strip().lower(),
        "account": str(payload.get("account") or "").strip().lower(),
        "symbol": str(payload.get("symbol") or "").strip().upper(),
        "option_type": str(
            payload.get("option_type") or ""
        ).strip().lower(),
        "position_side": str(
            payload.get("position_side") or ""
        ).strip().lower(),
        "strike": (
            canonical_decimal_text(payload.get("strike"))
            if payload.get("strike") is not None
            else None
        ),
        "expiration_ymd": str(
            payload.get("expiration_ymd") or ""
        ).strip(),
        "contracts": int(payload.get("contracts") or 0),
        "event_time_ms": int(
            payload.get("event_time_ms")
            or payload.get("observed_at_ms")
            or 0
        ),
        "option_event_time_ms": int(
            payload.get("option_event_time_ms") or 0
        ),
        "target_contracts_by_lot": {
            str(key): int(value)
            for key, value in sorted(
                dict(payload.get("target_contracts_by_lot") or {}).items()
            )
        },
        "stock_settlement": {
            "source_event_id": str(
                stock.get("source_event_id") or ""
            ).strip(),
            "symbol": str(stock.get("symbol") or "").strip().upper(),
            "side": str(stock.get("side") or "").strip().lower(),
            "shares": (
                canonical_decimal_text(stock.get("shares"))
                if stock.get("shares") is not None
                else None
            ),
            "price": (
                canonical_decimal_text(stock.get("price"))
                if stock.get("price") is not None
                else None
            ),
            "event_time_ms": int(stock.get("event_time_ms") or 0),
            "order_id": str(stock.get("order_id") or "").strip() or None,
            "clearing_date": (
                str(stock.get("clearing_date") or "").strip() or None
            ),
        },
        "observation_hashes": sorted(
            {
                str(value).strip()
                for key, value in payload.items()
                if (
                    key.endswith("_hash")
                    or key in {"observation_hash", "calendar_hash"}
                )
                and str(value or "").strip()
            }
        ),
    }
    return {
        "evidence_id": fact["evidence_id"],
        "evidence_hash": canonical_payload_hash(fact),
    }


def _projected_remaining_by_lot(
    projection_lots: Sequence[Any],
    *,
    target_lot_ids: Sequence[str],
) -> dict[str, int]:
    wanted = {str(item or "").strip() for item in target_lot_ids}
    remaining: dict[str, int] = {}
    for record in projection_lots:
        record_id = str(getattr(record, "record_id", "") or "").strip()
        if record_id not in wanted:
            continue
        fields = dict(getattr(record, "fields", {}) or {})
        remaining[record_id] = int(fields.get("contracts_open") or 0)
    missing = sorted(wanted - set(remaining))
    if missing:
        raise ValueError(
            "lifecycle target projection missing: " + ",".join(missing)
        )
    return dict(sorted(remaining.items()))


def _lifecycle_state_payload(
    *,
    lifecycle_case: dict[str, Any],
    evidence_rows: Sequence[dict[str, Any]],
    source_claims: Sequence[dict[str, Any]],
    allocations: Sequence[dict[str, Any]],
    void_event_ids: Sequence[str],
    projected_remaining_by_lot: dict[str, int],
    status: str,
    summary: dict[str, Any],
) -> dict[str, Any]:
    void_ids = {
        str(item or "").strip()
        for item in void_event_ids
        if str(item or "").strip()
    }
    effective_allocations = [
        {
            "allocation_id": str(
                item.get("allocation_id") or ""
            ).strip(),
            "evidence_id": str(item.get("evidence_id") or "").strip(),
            "target_lot_id": str(
                item.get("target_lot_id") or ""
            ).strip(),
            "terminal_event_id": str(
                item.get("canonical_terminal_event_id") or ""
            ).strip(),
            "terminal_type": str(
                item.get("terminal_type") or ""
            ).strip().lower(),
            "contracts": int(item.get("contracts_allocated") or 0),
        }
        for item in allocations
        if str(
            item.get("canonical_terminal_event_id") or ""
        ).strip()
        not in void_ids
    ]
    claims = [
        {
            "source_key": str(item.get("source_key") or "").strip(),
            "owner_evidence_id": str(
                item.get("owner_evidence_id") or ""
            ).strip(),
            "source_role": str(
                item.get("source_role") or ""
            ).strip().lower(),
            "source_payload_hash": str(
                item.get("source_payload_hash") or ""
            ).strip(),
        }
        for item in source_claims
    ]
    reasons = sorted(
        {
            str(item or "").strip()
            for item in (
                summary.get("lifecycle_reason_codes")
                or summary.get("reason_codes")
                or []
            )
            if str(item or "").strip()
        }
    )
    return {
        "case": {
            "case_id": str(lifecycle_case.get("case_id") or "").strip(),
            "schema_version": str(
                lifecycle_case.get("schema_version") or ""
            ).strip(),
            "target_contracts_by_lot": {
                str(key): int(value)
                for key, value in sorted(
                    dict(
                        lifecycle_case.get("target_contracts_by_lot")
                        or {}
                    ).items()
                )
            },
        },
        "evidence": sorted(
            (
                _lifecycle_evidence_business_fact(item)
                for item in evidence_rows
            ),
            key=lambda item: item["evidence_id"],
        ),
        "source_claims": sorted(
            claims,
            key=lambda item: (
                item["source_key"],
                item["source_role"],
                item["owner_evidence_id"],
            ),
        ),
        "effective_allocations": sorted(
            effective_allocations,
            key=lambda item: (
                item["target_lot_id"],
                item["terminal_event_id"],
            ),
        ),
        "effective_void_event_ids": sorted(void_ids),
        "projected_remaining_by_lot": dict(
            sorted(projected_remaining_by_lot.items())
        ),
        "reason_state": str(status or "").strip().lower(),
        "close_reason": str(
            summary.get("close_reason")
            or summary.get("decision_type")
            or ""
        ).strip().lower(),
        "reason_codes": reasons,
        "pairing_until_ms": (
            int(summary["pairing_until_ms"])
            if summary.get("pairing_until_ms") is not None
            else None
        ),
        "timing_policy_hash": str(
            summary.get("timing_policy_hash") or ""
        ).strip()
        or None,
        "observation_hashes": sorted(
            {
                str(value).strip()
                for key, value in summary.items()
                if (
                    key.endswith("_hash")
                    or key == "observation_hash"
                )
                and str(value or "").strip()
            }
        ),
    }


def _lifecycle_notification_transition(
    *,
    case_id: str,
    status: str,
) -> tuple[str, str]:
    status_value = str(status or "").strip().lower()
    if status_value == "ledger_written":
        transition_type = "resolution_confirmed"
    elif status_value in {"needs_review", "conflict"}:
        transition_type = status_value
    else:
        transition_type = "option_leg_closed"
    return (
        transition_type,
        f"lifecycle:{case_id}:{transition_type}",
    )


def apply_lifecycle_allocation_atomically(
    repo: Any,
    *,
    case_id: str,
    evidence: dict[str, Any],
    terminal_events: Sequence[Any],
    allocations: Sequence[dict[str, Any]],
    derived_status: str,
    derived_summary: dict[str, Any],
    expected_resolution_revision: int | None = None,
    expected_lifecycle_generation_token: str | None = None,
    correction_void_events: Sequence[Any] = (),
    notification_transition_type: str | None = None,
) -> dict[str, Any]:
    """Adopt evidence, terminal events, projection and allocations as one fact."""

    case_id_value = str(case_id or "").strip()
    evidence_payload = dict(evidence or {})
    allocation_rows = [dict(item or {}) for item in allocations]
    event_rows = [_canonical_storage_event(item) for item in terminal_events]
    correction_void_rows = [
        _canonical_storage_event(item)
        for item in correction_void_events
    ]

    def _run(sqlite_repo: Any, conn: Any | None) -> dict[str, Any]:
        if conn is None:
            raise TypeError("lifecycle allocation requires SQLite transaction authority")
        sqlite_repo.assert_foreign_keys_clean(conn=conn)
        lifecycle_case = sqlite_repo.get_trade_lifecycle_case(case_id_value, conn=conn)
        if lifecycle_case is None:
            raise ValueError(f"lifecycle case not found: {case_id_value}")
        _require_lifecycle_generation(
            sqlite_repo,
            conn=conn,
            case_id=case_id_value,
            expected_generation_token=(
                expected_lifecycle_generation_token
            ),
        )
        _validate_broker_settlement_pair_for_write(
            sqlite_repo,
            conn=conn,
            lifecycle_case=lifecycle_case,
            evidence=evidence_payload,
        )
        current_summary_for_cas = (
            dict(lifecycle_case.get("derived_summary") or {})
            if isinstance(
                lifecycle_case.get("derived_summary"),
                dict,
            )
            else {}
        )
        if (
            expected_resolution_revision is not None
            and int(
                current_summary_for_cas.get(
                    "resolution_revision"
                )
                or 0
            )
            != int(expected_resolution_revision)
        ):
            raise ValueError(
                "lifecycle resolution revision compare-and-set failed"
            )
        evidence_id = str(evidence_payload.get("evidence_id") or "").strip()
        if not evidence_id:
            raise ValueError("lifecycle evidence_id is required")
        if evidence_payload.get("case_id") not in (None, "", case_id_value):
            raise ValueError("lifecycle evidence is bound to another case")
        existing_evidence = sqlite_repo.get_trade_lifecycle_evidence(evidence_id, conn=conn)
        void_event_ids = _effective_void_target_ids(sqlite_repo, conn=conn)
        case_allocations = list(
            sqlite_repo.list_trade_lifecycle_allocations(
                case_id=case_id_value,
                conn=conn,
            )
        )
        existing_evidence_allocations = [
            item
            for item in case_allocations
            if str(item.get("evidence_id") or "").strip() == evidence_id
        ]
        if existing_evidence is not None and not existing_evidence_allocations:
            raise ValueError("evidence_without_allocation_requires_review")
        if existing_evidence_allocations and _canonical_rows(
            existing_evidence_allocations
        ) != _canonical_rows(
            allocation_rows
        ):
            raise ValueError("lifecycle evidence allocation replay conflict")

        existing_resolution = resolve_allocations(
            lifecycle_case.get("target_contracts_by_lot"),
            case_allocations,
            void_event_ids=void_event_ids,
        )
        if existing_resolution.status != "ok":
            raise ValueError(
                "existing lifecycle allocations conflict: "
                + ",".join(existing_resolution.reason_codes)
            )
        for lot_id, expected_remaining in (
            existing_resolution.remaining_contracts_by_lot.items()
        ):
            try:
                fields = sqlite_repo.get_position_lot_fields(lot_id, conn=conn)
                actual_remaining = int(fields.get("contracts_open") or 0)
            except (TypeError, ValueError) as exc:
                raise ValueError("target_lot_quantity_drift") from exc
            if actual_remaining != expected_remaining:
                raise ValueError("target_lot_quantity_drift")

        correction_void_created: list[bool] = []
        if correction_void_rows:
            effective_allocated_event_ids = {
                str(
                    item.get("canonical_terminal_event_id")
                    or ""
                ).strip()
                for item in case_allocations
                if str(
                    item.get("canonical_terminal_event_id")
                    or ""
                ).strip()
                and str(
                    item.get("canonical_terminal_event_id")
                    or ""
                ).strip()
                not in set(void_event_ids)
            }
            seen_targets: set[str] = set()
            for void_event in correction_void_rows:
                target_event_id = str(
                    void_event.target_event_id or ""
                ).strip()
                if (
                    void_event.event_type != "void"
                    or not target_event_id
                ):
                    raise ValueError(
                        "lifecycle correction requires canonical void events"
                    )
                if target_event_id in seen_targets:
                    raise ValueError(
                        "lifecycle correction void target is duplicated"
                    )
                seen_targets.add(target_event_id)
                if target_event_id not in effective_allocated_event_ids:
                    raise ValueError(
                        "lifecycle correction target is not an "
                        "effective allocation event"
                    )
                correction_void_created.append(
                    sqlite_repo.upsert_trade_event(
                        void_event,
                        conn=conn,
                    )
                )
            void_event_ids = _effective_void_target_ids(
                sqlite_repo,
                conn=conn,
            )

        canonical_summary, canonical_status = _validate_lifecycle_event_allocation_plan(
            case_id=case_id_value,
            lifecycle_case=lifecycle_case,
            evidence=evidence_payload,
            terminal_events=event_rows,
            allocations=allocation_rows,
            existing_allocations=case_allocations,
            void_event_ids=void_event_ids,
        )
        requested_status = str(derived_status or "").strip().lower()
        if requested_status != canonical_status:
            raise ValueError("lifecycle derived status mismatch")
        incoming_summary = dict(derived_summary or {})
        for field, expected in canonical_summary.items():
            if field in incoming_summary and incoming_summary[field] != expected:
                raise ValueError(f"lifecycle derived summary mismatch: {field}")
        existing_source_claims = list(
            sqlite_repo.list_trade_lifecycle_source_consumptions(
                case_id=case_id_value,
                conn=conn,
            )
        )
        option_anchor_claims = [
            item
            for item in existing_source_claims
            if str(item.get("source_role") or "").strip().lower()
            == "option_anchor"
        ]
        requires_broker_claims = (
            str(
                evidence_payload.get("source_type") or ""
            ).strip().lower()
            == "broker_settlement_pair"
            or bool(evidence_payload.get("source_evidence_ids"))
        )
        if requires_broker_claims and not option_anchor_claims:
            raise ValueError("lifecycle_option_anchor_claim_missing")
        terminal_type = str(
            evidence_payload.get("terminal_type")
            or evidence_payload.get("evidence_type")
            or ""
        ).strip().lower()
        stock_claim: dict[str, Any] | None = None
        close_claim: dict[str, Any] | None = None
        if (
            terminal_type in {"assignment", "exercise"}
            and requires_broker_claims
        ):
            stock = (
                dict(evidence_payload.get("stock_settlement") or {})
                if isinstance(
                    evidence_payload.get("stock_settlement"),
                    dict,
                )
                else {}
            )
            stock_source_key = str(
                stock.get("source_event_id") or ""
            ).strip()
            stock_claim = build_source_consumption_claim(
                source_key=stock_source_key,
                case_id=case_id_value,
                owner_evidence_id=evidence_id,
                source_role="stock_settlement",
                economic_payload={
                    "account": lifecycle_case.get("account"),
                    "futu_account_id": stock.get("futu_account_id"),
                    "symbol": stock.get("symbol")
                    or lifecycle_case.get("symbol"),
                    "side": stock.get("side"),
                    "shares": stock.get("shares"),
                    "price": stock.get("price"),
                    "execution_time_ms": stock.get("event_time_ms"),
                    "order_id": stock.get("order_id"),
                    "clearing_date": stock.get("clearing_date"),
                },
            )
        if terminal_type == "close" and requires_broker_claims:
            broker_close = (
                dict(evidence_payload.get("broker_close") or {})
                if isinstance(
                    evidence_payload.get("broker_close"),
                    dict,
                )
                else {}
            )
            close_source_key = str(
                broker_close.get("source_event_id") or ""
            ).strip()
            close_claim = build_source_consumption_claim(
                source_key=close_source_key,
                case_id=case_id_value,
                owner_evidence_id=evidence_id,
                source_role="option_anchor",
                economic_payload={
                    "account": lifecycle_case.get("account"),
                    "futu_account_id": broker_close.get(
                        "futu_account_id"
                    ),
                    "symbol": lifecycle_case.get("symbol"),
                    "option_type": lifecycle_case.get(
                        "option_type"
                    ),
                    "position_side": lifecycle_case.get(
                        "position_side"
                    ),
                    "strike": lifecycle_case.get("strike"),
                    "expiration_ymd": lifecycle_case.get(
                        "expiration_ymd"
                    ),
                    "multiplier": lifecycle_case.get(
                        "multiplier"
                    ),
                    "side": broker_close.get("side"),
                    "contracts": evidence_payload.get(
                        "contracts"
                    ),
                    "price": evidence_payload.get("price"),
                    "execution_time_ms": evidence_payload.get(
                        "event_time_ms"
                    ),
                    "order_id": broker_close.get("order_id"),
                    "clearing_date": broker_close.get(
                        "clearing_date"
                    ),
                },
            )
        if existing_evidence is None:
            evidence_created = sqlite_repo.insert_trade_lifecycle_evidence_once(
                evidence_payload,
                conn=conn,
            )
        else:
            _validate_existing_lifecycle_evidence(
                existing=existing_evidence,
                incoming=evidence_payload,
                case_id=case_id_value,
            )
            evidence_created = False
        evidence_bound = sqlite_repo.bind_trade_lifecycle_evidence_case_once(
            evidence_id=evidence_id,
            case_id=case_id_value,
            conn=conn,
        )
        stock_claim_created = (
            sqlite_repo.insert_trade_lifecycle_source_consumption_once(
                stock_claim,
                conn=conn,
            )
            if stock_claim is not None
            else False
        )
        close_claim_created = (
            sqlite_repo.insert_trade_lifecycle_source_consumption_once(
                close_claim,
                conn=conn,
            )
            if close_claim is not None
            else False
        )
        terminal_event_created = [
            sqlite_repo.upsert_trade_event(item, conn=conn)
            for item in event_rows
        ]
        projection = project_stored_trade_events_to_position_lots(
            sqlite_repo.list_trade_events(conn=conn)
        )
        if projection.has_errors:
            codes = ",".join(
                sorted({item.code for item in projection.diagnostics if item.severity == "error"})
            )
            raise ValueError(f"lifecycle allocation projection failed: {codes}")
        sqlite_repo.replace_position_lots(projection.lots, conn=conn)
        allocation_created = [
            sqlite_repo.insert_trade_lifecycle_allocation(item, conn=conn)
            for item in allocation_rows
        ]
        current_summary = (
            dict(lifecycle_case.get("derived_summary") or {})
            if isinstance(lifecycle_case.get("derived_summary"), dict)
            else {}
        )
        current_revision = int(
            current_summary.get("resolution_revision") or 0
        )
        current_state_fingerprint = str(
            current_summary.get("state_fingerprint") or ""
        ).strip()
        post_allocations = list(
            sqlite_repo.list_trade_lifecycle_allocations(
                case_id=case_id_value,
                conn=conn,
            )
        )
        post_evidence = list(
            sqlite_repo.list_trade_lifecycle_evidence(
                case_id=case_id_value,
                conn=conn,
            )
        )
        post_source_claims = list(
            sqlite_repo.list_trade_lifecycle_source_consumptions(
                case_id=case_id_value,
                conn=conn,
            )
        )
        canonical_summary = {
            **{
                key: value
                for key, value in incoming_summary.items()
                if key
                not in {
                    "resolution_revision",
                    "state_fingerprint",
                    "notification_audit_codes",
                }
            },
            **canonical_summary,
        }
        projected_remaining = _projected_remaining_by_lot(
            projection.lots,
            target_lot_ids=list(
                dict(
                    lifecycle_case.get("target_contracts_by_lot") or {}
                )
            ),
        )
        state_fingerprint = canonical_state_fingerprint(
            _lifecycle_state_payload(
                lifecycle_case=lifecycle_case,
                evidence_rows=post_evidence,
                source_claims=post_source_claims,
                allocations=post_allocations,
                void_event_ids=void_event_ids,
                projected_remaining_by_lot=projected_remaining,
                status=canonical_status,
                summary=canonical_summary,
            )
        )
        business_state_changed = (
            state_fingerprint != current_state_fingerprint
        )
        resolution_revision = (
            current_revision + 1
            if business_state_changed
            else current_revision
        )
        if resolution_revision <= 0:
            raise ValueError("lifecycle resolution revision is invalid")
        requested_transition_type = str(
            notification_transition_type or ""
        ).strip().lower()
        if requested_transition_type:
            if requested_transition_type != "resolution_corrected":
                raise ValueError(
                    "unsupported lifecycle notification transition"
                )
            if not correction_void_rows:
                raise ValueError(
                    "resolution_corrected requires a correction void"
                )
            transition_type = requested_transition_type
            transition_key = (
                f"lifecycle:{case_id_value}:"
                f"{transition_type}:{resolution_revision}"
            )
        else:
            transition_type, transition_key = (
                _lifecycle_notification_transition(
                    case_id=case_id_value,
                    status=canonical_status,
                )
            )
        notification_intent = build_notification_intent(
            case_id=case_id_value,
            transition_type=transition_type,
            resolution_revision=resolution_revision,
            delivery_revision=0,
            transition_key=transition_key,
            state_fingerprint=state_fingerprint,
            payload={
                "schema_version": "trade_lifecycle_notification.v1",
                "case_id": case_id_value,
                "transition_type": transition_type,
                "resolution_revision": resolution_revision,
                "state_fingerprint": state_fingerprint,
                "account": lifecycle_case.get("account"),
                "symbol": lifecycle_case.get("symbol"),
                "option_type": lifecycle_case.get("option_type"),
                "position_side": lifecycle_case.get("position_side"),
                "strike": lifecycle_case.get("strike"),
                "expiration_ymd": lifecycle_case.get("expiration_ymd"),
                "close_reason": str(
                    canonical_summary.get("close_reason")
                    or evidence_payload.get("terminal_type")
                    or evidence_payload.get("evidence_type")
                    or ""
                ).strip().lower(),
                "terminal_event_ids": sorted(
                    item.event_id for item in event_rows
                ),
                "void_event_ids": sorted(
                    item.event_id
                    for item in correction_void_rows
                ),
                "void_target_event_ids": sorted(
                    str(item.target_event_id or "")
                    for item in correction_void_rows
                ),
                "allocations": sorted(
                    [
                        {
                            "allocation_id": item.get("allocation_id"),
                            "target_lot_id": item.get("target_lot_id"),
                            "contracts": int(
                                item.get("contracts_allocated") or 0
                            ),
                            "terminal_event_id": item.get(
                                "canonical_terminal_event_id"
                            ),
                        }
                        for item in allocation_rows
                    ],
                    key=lambda item: (
                        str(item["target_lot_id"] or ""),
                        str(item["terminal_event_id"] or ""),
                    ),
                ),
            },
        )
        notification_audit_codes = list(
            current_summary.get("notification_audit_codes") or []
        )
        existing_transition = (
            sqlite_repo.get_trade_lifecycle_notification_by_transition(
                transition_key=transition_key,
                delivery_revision=0,
                conn=conn,
            )
        )
        outbox_created = False
        if business_state_changed:
            if (
                existing_transition is not None
                and (
                    str(
                        existing_transition.get("state_fingerprint")
                        or ""
                    )
                    != state_fingerprint
                    or str(existing_transition.get("payload_hash") or "")
                    != str(notification_intent.get("payload_hash") or "")
                )
            ):
                notification_audit_codes = sorted(
                    set(
                        notification_audit_codes
                        + ["notification_transition_conflict"]
                    )
                )
            else:
                outbox_created = (
                    sqlite_repo.insert_trade_lifecycle_notification_once(
                        notification_intent,
                        conn=conn,
                    )
                )
        canonical_summary = {
            **current_summary,
            **canonical_summary,
            "resolution_revision": resolution_revision,
            "state_fingerprint": state_fingerprint,
            "notification_audit_codes": notification_audit_codes,
        }
        status_changed = sqlite_repo.update_trade_lifecycle_case_derived_status(
            case_id=case_id_value,
            status=canonical_status,
            derived_summary=canonical_summary,
            expected_state_fingerprint=current_state_fingerprint,
            conn=conn,
        )
        sqlite_repo.assert_foreign_keys_clean(conn=conn)
        return {
            "case_id": case_id_value,
            "evidence_id": evidence_id,
            "evidence_created": evidence_created,
            "evidence_bound": evidence_bound,
            "stock_source_claim_created": stock_claim_created,
            "close_source_claim_created": close_claim_created,
            "terminal_event_ids": [item.event_id for item in event_rows],
            "terminal_events_created": terminal_event_created,
            "correction_void_event_ids": [
                item.event_id for item in correction_void_rows
            ],
            "correction_void_events_created": correction_void_created,
            "allocation_ids": [str(item.get("allocation_id") or "") for item in allocation_rows],
            "allocations_created": allocation_created,
            "status_changed": status_changed,
            "resolution_revision": resolution_revision,
            "state_fingerprint": state_fingerprint,
            "business_state_changed": business_state_changed,
            "notification_outbox_id": notification_intent["outbox_id"],
            "notification_outbox_created": outbox_created,
            "notification_audit_codes": notification_audit_codes,
            "position_lot_count": len(projection.lots),
        }

    return with_sqlite_repo_transaction(repo, _run)


def accept_option_close_evidence_atomically(
    repo: Any,
    *,
    contract_identity: dict[str, Any],
    evidence: dict[str, Any],
    apply_changes: bool = True,
) -> dict[str, Any]:
    """Create/reuse one lifecycle_case.v2 and accept zero-price close evidence."""

    identity = dict(contract_identity or {})
    evidence_payload = dict(evidence or {})

    def _run(sqlite_repo: Any, conn: Any | None) -> dict[str, Any]:
        if conn is None:
            raise TypeError(
                "option close evidence acceptance requires SQLite transaction authority"
            )
        account = str(identity.get("account") or "").strip().lower()
        futu_account_id = str(
            identity.get("futu_account_id") or ""
        ).strip()
        source_event_id = str(
            evidence_payload.get("source_event_id") or ""
        ).strip()
        evidence_id = str(evidence_payload.get("evidence_id") or "").strip()
        contracts = _positive_lifecycle_contracts(
            evidence_payload.get("contracts")
        )
        expected_source_prefix = f"futu:{account}:{futu_account_id}:"
        if (
            not account
            or not futu_account_id
            or not evidence_id
            or not source_event_id.startswith(expected_source_prefix)
            or source_event_id == expected_source_prefix
        ):
            raise ValueError("canonical_broker_identity_missing")
        if (
            str(evidence_payload.get("evidence_type") or "").strip().lower()
            != "option_zero_price_close"
        ):
            raise ValueError("option close evidence type is invalid")
        try:
            price = Decimal(str(evidence_payload.get("price")))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError("option close evidence price is invalid") from exc
        if not price.is_finite() or price != 0:
            raise ValueError("option close evidence must have exact zero price")

        contract_key = ContractKey.from_values(
            broker=identity.get("broker"),
            account=account,
            underlying_symbol=identity.get("symbol"),
            option_type=identity.get("option_type"),
            position_side=identity.get("position_side"),
            strike=identity.get("strike"),
            expiration_ymd=identity.get("expiration_ymd"),
        )
        existing_evidence = sqlite_repo.get_trade_lifecycle_evidence(
            evidence_id,
            conn=conn,
        )
        existing_source_claim = (
            sqlite_repo.get_trade_lifecycle_source_consumption(
                source_event_id,
                conn=conn,
            )
        )
        if (
            existing_source_claim is not None
            and str(
                existing_source_claim.get("owner_evidence_id") or ""
            ).strip()
            != evidence_id
        ):
            raise ValueError("lifecycle_source_event_already_consumed")
        if existing_evidence is not None:
            existing_case_id = str(
                existing_evidence.get("case_id") or ""
            ).strip()
            lifecycle_case = sqlite_repo.get_trade_lifecycle_case(
                existing_case_id,
                conn=conn,
            )
            if lifecycle_case is None:
                raise ValueError("lifecycle evidence case is missing")
            bound_futu_account_id = str(
                lifecycle_case.get("futu_account_id") or ""
            ).strip()
            if (
                not bound_futu_account_id
                or bound_futu_account_id != futu_account_id
            ):
                raise ValueError(
                    "lifecycle_case_futu_account_mismatch"
                )
            _validate_existing_zero_price_evidence(
                existing=existing_evidence,
                incoming=evidence_payload,
                contract_key=contract_key,
                contracts=contracts,
            )
            expected_claim = build_source_consumption_claim(
                source_key=source_event_id,
                case_id=existing_case_id,
                owner_evidence_id=evidence_id,
                source_role="option_anchor",
                economic_payload={
                    **identity,
                    **existing_evidence,
                    "account": account,
                    "futu_account_id": futu_account_id,
                },
            )
            if existing_source_claim is None:
                raise ValueError(
                    "lifecycle_source_claim_history_unseeded"
                )
            sqlite_repo.insert_trade_lifecycle_source_consumption_once(
                expected_claim,
                conn=conn,
            )
            return {
                "status": "existing",
                "case_id": existing_case_id,
                "case_created": False,
                "evidence_id": evidence_id,
                "evidence_created": False,
                "broker_evidence_accepted": True,
                "lifecycle_case": lifecycle_case,
                "lifecycle_evidence": existing_evidence,
                "source_claim": expected_claim,
                "source_claim_created": False,
            }

        cases = [
            item
            for item in sqlite_repo.list_trade_lifecycle_cases(
                account=account,
                conn=conn,
            )
            if str(item.get("schema_version") or "").strip()
            == "lifecycle_case.v2"
            and str(item.get("contract_key") or "").strip()
            == contract_key.position_key
        ]
        if len(cases) > 1:
            raise ValueError("multiple_lifecycle_cases_for_contract")
        lifecycle_case = dict(cases[0]) if cases else None
        case_preexisting = lifecycle_case is not None
        position_lots = list(sqlite_repo.list_position_lots(conn=conn))
        matching_lots = _matching_lifecycle_lots(
            position_lots,
            contract_key=contract_key,
        )
        if lifecycle_case is None:
            if not matching_lots:
                raise ValueError("lifecycle_close_target_not_found")
            target_contracts_by_lot = {
                lot_id: remaining
                for lot_id, remaining, _opened_at in matching_lots
            }
            lifecycle_case = {
                **build_lifecycle_case(
                    account=account,
                    broker=contract_key.broker,
                    contract_key=contract_key.position_key,
                    position_side=contract_key.position_side,
                    expiration_ymd=contract_key.expiration_ymd,
                    market=str(identity.get("market") or ""),
                    target_contracts_by_lot=target_contracts_by_lot,
                    futu_account_id=futu_account_id,
                ),
                "market": str(identity.get("market") or "").strip().upper(),
                "symbol": contract_key.underlying_symbol,
                "option_type": contract_key.option_type,
                "strike": contract_key.strike,
                "currency": normalize_currency(identity.get("currency")),
                "multiplier": float(identity.get("multiplier") or 100),
            }
        else:
            bound_futu_account_id = str(
                lifecycle_case.get("futu_account_id") or ""
            ).strip()
            if (
                bound_futu_account_id
                and bound_futu_account_id != futu_account_id
            ):
                raise ValueError(
                    "lifecycle_case_futu_account_mismatch"
                )
            lifecycle_case["futu_account_id"] = futu_account_id
        target_contracts_by_lot = dict(
            lifecycle_case.get("target_contracts_by_lot") or {}
        )
        void_event_ids = _effective_void_target_ids(sqlite_repo, conn=conn)
        allocations = list(
            sqlite_repo.list_trade_lifecycle_allocations(
                case_id=str(lifecycle_case.get("case_id") or ""),
                conn=conn,
            )
        )
        case_evidence = list(
            sqlite_repo.list_trade_lifecycle_evidence(
                case_id=str(lifecycle_case.get("case_id") or ""),
                conn=conn,
            )
        )
        resolution = resolve_allocations(
            target_contracts_by_lot,
            allocations,
            void_event_ids=void_event_ids,
        )
        if resolution.status != "ok":
            raise ValueError(
                "existing lifecycle allocations conflict: "
                + ",".join(resolution.reason_codes)
            )
        evidence_facts = lifecycle_evidence_facts(
            evidence=case_evidence,
            allocations=allocations,
            void_event_ids=void_event_ids,
        )
        for lot_id, expected_remaining in (
            resolution.remaining_contracts_by_lot.items()
        ):
            fields = sqlite_repo.get_position_lot_fields(lot_id, conn=conn)
            if int(fields.get("contracts_open") or 0) != expected_remaining:
                raise ValueError("target_lot_quantity_drift")
        available_by_lot = {
            lot_id: max(
                int(remaining)
                - int(
                    evidence_facts.reservation_contracts_by_lot.get(
                        lot_id,
                        0,
                    )
                ),
                0,
            )
            for lot_id, remaining in resolution.remaining_contracts_by_lot.items()
        }
        evidence_target_manifest = _allocate_lifecycle_reservation(
            contracts=contracts,
            available_by_lot=available_by_lot,
            matching_lots=matching_lots,
        )
        accepted_evidence = {
            **evidence_payload,
            "case_id": str(lifecycle_case.get("case_id") or ""),
            "account": account,
            "symbol": contract_key.underlying_symbol,
            "option_type": contract_key.option_type,
            "position_side": contract_key.position_side,
            "strike": contract_key.strike,
            "expiration_ymd": contract_key.expiration_ymd,
            "contracts": contracts,
            "price": "0",
            "target_contracts_by_lot": evidence_target_manifest,
            "target_lot_id": (
                next(iter(evidence_target_manifest))
                if len(evidence_target_manifest) == 1
                else None
            ),
        }
        case_created = False
        evidence_created = False
        source_claim = build_source_consumption_claim(
            source_key=source_event_id,
            case_id=str(lifecycle_case.get("case_id") or ""),
            owner_evidence_id=evidence_id,
            source_role="option_anchor",
            economic_payload={
                **identity,
                **accepted_evidence,
                "account": account,
                "futu_account_id": futu_account_id,
            },
        )
        source_claim_created = False
        if apply_changes:
            if case_preexisting:
                sqlite_repo.bind_trade_lifecycle_case_futu_account_once(
                    case_id=str(
                        lifecycle_case.get("case_id") or ""
                    ),
                    futu_account_id=futu_account_id,
                    conn=conn,
                )
                lifecycle_case = (
                    sqlite_repo.get_trade_lifecycle_case(
                        str(lifecycle_case.get("case_id") or ""),
                        conn=conn,
                    )
                    or lifecycle_case
                )
            case_created = sqlite_repo.insert_trade_lifecycle_case_once(
                lifecycle_case,
                conn=conn,
            )
            if not case_preexisting:
                sqlite_repo.bind_trade_lifecycle_case_futu_account_once(
                    case_id=str(
                        lifecycle_case.get("case_id") or ""
                    ),
                    futu_account_id=futu_account_id,
                    conn=conn,
                )
            evidence_created = sqlite_repo.insert_trade_lifecycle_evidence_once(
                accepted_evidence,
                conn=conn,
            )
            source_claim_created = (
                sqlite_repo.insert_trade_lifecycle_source_consumption_once(
                    source_claim,
                    conn=conn,
                )
            )
            sqlite_repo.assert_foreign_keys_clean(conn=conn)
        return {
            "status": "accepted" if apply_changes else "dry_run",
            "case_id": str(lifecycle_case.get("case_id") or ""),
            "case_created": case_created,
            "evidence_id": evidence_id,
            "evidence_created": evidence_created,
            "broker_evidence_accepted": bool(apply_changes),
            "lifecycle_case": lifecycle_case,
            "lifecycle_evidence": accepted_evidence,
            "source_claim": source_claim,
            "source_claim_created": source_claim_created,
        }

    return with_sqlite_repo_transaction(repo, _run)


def discover_expired_lifecycle_cases_atomically(
    repo: Any,
    *,
    account: str | None = None,
    observed_at_ms: int | None = None,
    apply_changes: bool = True,
) -> dict[str, Any]:
    """Freeze expired open option lots into lifecycle_case.v2 rows."""

    account_value = str(account or "").strip().lower()
    current_ms = int(
        observed_at_ms
        if observed_at_ms is not None
        else datetime.now(timezone.utc).timestamp() * 1000
    )

    def _run(sqlite_repo: Any, conn: Any | None) -> dict[str, Any]:
        if conn is None:
            raise TypeError("lifecycle discovery requires SQLite transaction authority")
        sqlite_repo.assert_foreign_keys_clean(conn=conn)
        position_lots = list(sqlite_repo.list_position_lots(conn=conn))
        existing_cases = list(
            sqlite_repo.list_trade_lifecycle_cases(
                account=account_value or None,
                conn=conn,
            )
        )
        target_owner: dict[str, str] = {}
        for lifecycle_case in existing_cases:
            if str(lifecycle_case.get("schema_version") or "").strip() != "lifecycle_case.v2":
                continue
            case_id = str(lifecycle_case.get("case_id") or "").strip()
            target_manifest = dict(lifecycle_case.get("target_contracts_by_lot") or {})
            for lot_id in sorted(str(item or "").strip() for item in target_manifest):
                if not lot_id:
                    raise ValueError("lifecycle case target lot id is invalid")
                previous = target_owner.get(lot_id)
                if previous is not None and previous != case_id:
                    raise ValueError(f"lifecycle_case_target_overlap:{lot_id}")
                target_owner[lot_id] = case_id

        eligible_groups: dict[str, dict[str, Any]] = {}
        skipped_targeted_lot_ids: list[str] = []
        for row in position_lots:
            lot_id = str(row.get("record_id") or "").strip()
            fields = dict(row.get("fields") or {})
            lot_account = str(fields.get("account") or "").strip().lower()
            if account_value and lot_account != account_value:
                continue
            contracts_open = effective_contracts_open(fields)
            if not lot_id or contracts_open <= 0:
                continue
            expiration_ymd = effective_expiration_ymd(fields)
            strike = effective_strike(fields)
            multiplier = effective_multiplier(fields)
            try:
                contract_key = ContractKey.from_values(
                    broker=fields.get("broker"),
                    account=lot_account,
                    underlying_symbol=fields.get("symbol"),
                    option_type=fields.get("option_type"),
                    position_side=fields.get("side"),
                    strike=strike,
                    expiration_ymd=expiration_ymd,
                )
            except (TypeError, ValueError):
                continue
            market = str(symbol_market(contract_key.underlying_symbol) or "").strip().upper()
            observation_start = expiration_observation_start_ms(
                contract_key.expiration_ymd,
                market,
            )
            if observation_start is None:
                try:
                    expired_for_review = date.fromisoformat(
                        contract_key.expiration_ymd
                    ) < datetime.fromtimestamp(current_ms / 1000, tz=timezone.utc).date()
                except ValueError:
                    expired_for_review = False
                if not expired_for_review:
                    continue
            elif current_ms < observation_start:
                continue
            if lot_id in target_owner:
                skipped_targeted_lot_ids.append(lot_id)
                continue
            group = eligible_groups.setdefault(
                contract_key.position_key,
                {
                    "contract_key": contract_key,
                    "market": market,
                    "currency": normalize_currency(fields.get("currency")),
                    "multiplier": float(multiplier or 100.0),
                    "target_contracts_by_lot": {},
                },
            )
            group["target_contracts_by_lot"][lot_id] = contracts_open

        created_case_ids: list[str] = []
        would_create_case_ids: list[str] = []
        discovered_case_ids: list[str] = []
        for position_key, group in sorted(eligible_groups.items()):
            contract_key = group["contract_key"]
            lifecycle_case = {
                **build_lifecycle_case(
                    account=contract_key.account,
                    broker=contract_key.broker,
                    contract_key=position_key,
                    position_side=contract_key.position_side,
                    expiration_ymd=contract_key.expiration_ymd,
                    market=group["market"],
                    target_contracts_by_lot=group["target_contracts_by_lot"],
                ),
                "market": group["market"],
                "symbol": contract_key.underlying_symbol,
                "option_type": contract_key.option_type,
                "strike": contract_key.strike,
                "currency": group["currency"],
                "multiplier": group["multiplier"],
            }
            case_id = str(lifecycle_case["case_id"])
            discovered_case_ids.append(case_id)
            if apply_changes:
                created = sqlite_repo.insert_trade_lifecycle_case_once(
                    lifecycle_case,
                    conn=conn,
                )
                if created:
                    created_case_ids.append(case_id)
            else:
                would_create_case_ids.append(case_id)

        refreshed_case_ids: list[str] = []
        would_refresh_case_ids: list[str] = []
        sqlite_repo.assert_foreign_keys_clean(conn=conn)
        return {
            "schema_version": "lifecycle_discovery_result.v2",
            "observed_at_ms": current_ms,
            "account": account_value or None,
            "apply_changes": bool(apply_changes),
            "created_case_ids": sorted(created_case_ids),
            "would_create_case_ids": sorted(would_create_case_ids),
            "discovered_case_ids": sorted(discovered_case_ids),
            "refreshed_case_ids": sorted(refreshed_case_ids),
            "would_refresh_case_ids": sorted(would_refresh_case_ids),
            "skipped_targeted_lot_ids": sorted(set(skipped_targeted_lot_ids)),
        }

    return with_sqlite_repo_transaction(repo, _run)


def record_lifecycle_evidence_issue_atomically(
    repo: Any,
    *,
    case_id: str,
    evidence: dict[str, Any],
    status: str,
    reason_codes: Sequence[str],
    expected_lifecycle_generation_token: str | None = None,
) -> dict[str, Any]:
    """Persist a uniquely matched evidence issue without creating terminal facts."""

    case_id_value = str(case_id or "").strip()
    evidence_payload = dict(evidence or {})
    status_value = str(status or "").strip().lower()
    reasons = sorted(
        {
            str(item or "").strip()
            for item in reason_codes
            if str(item or "").strip()
        }
    )
    if status_value not in {"needs_review", "conflict"}:
        raise ValueError("lifecycle evidence issue status must be needs_review or conflict")
    if not reasons:
        raise ValueError("lifecycle evidence issue reason_codes are required")

    def _run(sqlite_repo: Any, conn: Any | None) -> dict[str, Any]:
        if conn is None:
            raise TypeError("lifecycle evidence issue requires SQLite transaction authority")
        sqlite_repo.assert_foreign_keys_clean(conn=conn)
        lifecycle_case = sqlite_repo.get_trade_lifecycle_case(case_id_value, conn=conn)
        if lifecycle_case is None:
            raise ValueError(f"lifecycle case not found: {case_id_value}")
        _require_lifecycle_generation(
            sqlite_repo,
            conn=conn,
            case_id=case_id_value,
            expected_generation_token=(
                expected_lifecycle_generation_token
            ),
        )
        evidence_id = str(evidence_payload.get("evidence_id") or "").strip()
        if not evidence_id:
            raise ValueError("lifecycle evidence_id is required")
        if evidence_payload.get("case_id") not in (None, "", case_id_value):
            raise ValueError("lifecycle evidence is bound to another case")
        existing = sqlite_repo.get_trade_lifecycle_evidence(evidence_id, conn=conn)
        if existing is None:
            evidence_created = sqlite_repo.insert_trade_lifecycle_evidence_once(
                evidence_payload,
                conn=conn,
            )
        else:
            _validate_existing_lifecycle_evidence(
                existing=existing,
                incoming=evidence_payload,
                case_id=case_id_value,
            )
            evidence_created = False
        allocations = list(
            sqlite_repo.list_trade_lifecycle_allocations(
                case_id=case_id_value,
                conn=conn,
            )
        )
        if any(
            str(item.get("evidence_id") or "").strip() == evidence_id
            for item in allocations
        ):
            raise ValueError("allocated lifecycle evidence cannot be reclassified as an issue")
        evidence_bound = sqlite_repo.bind_trade_lifecycle_evidence_case_once(
            evidence_id=evidence_id,
            case_id=case_id_value,
            conn=conn,
        )
        requires_broker_claims = (
            str(
                evidence_payload.get("source_type") or ""
            ).strip().lower()
            == "broker_settlement_pair"
            or bool(evidence_payload.get("source_evidence_ids"))
        )
        source_claim_created = False
        if requires_broker_claims:
            existing_claims = list(
                sqlite_repo.list_trade_lifecycle_source_consumptions(
                    case_id=case_id_value,
                    conn=conn,
                )
            )
            if not any(
                str(item.get("source_role") or "").strip().lower()
                == "option_anchor"
                for item in existing_claims
            ):
                raise ValueError(
                    "lifecycle_option_anchor_claim_missing"
                )
            stock = (
                dict(evidence_payload.get("stock_settlement") or {})
                if isinstance(
                    evidence_payload.get("stock_settlement"),
                    dict,
                )
                else {}
            )
            if str(stock.get("source_event_id") or "").strip():
                claim = build_source_consumption_claim(
                    source_key=str(stock["source_event_id"]),
                    case_id=case_id_value,
                    owner_evidence_id=evidence_id,
                    source_role="stock_settlement",
                    economic_payload={
                        "account": lifecycle_case.get("account"),
                        "futu_account_id": stock.get(
                            "futu_account_id"
                        ),
                        "symbol": stock.get("symbol")
                        or lifecycle_case.get("symbol"),
                        "side": stock.get("side"),
                        "shares": stock.get("shares"),
                        "price": stock.get("price"),
                        "execution_time_ms": stock.get(
                            "event_time_ms"
                        ),
                        "order_id": stock.get("order_id"),
                        "clearing_date": stock.get("clearing_date"),
                    },
                )
                source_claim_created = (
                    sqlite_repo.insert_trade_lifecycle_source_consumption_once(
                        claim,
                        conn=conn,
                    )
                )
        resolution = resolve_allocations(
            lifecycle_case.get("target_contracts_by_lot"),
            allocations,
            void_event_ids=_effective_void_target_ids(sqlite_repo, conn=conn),
        )
        prior_summary = dict(lifecycle_case.get("derived_summary") or {})
        prior_conflicts = list(prior_summary.get("conflict_evidence_ids") or [])
        void_event_ids = _effective_void_target_ids(
            sqlite_repo,
            conn=conn,
        )
        new_summary = {
            **prior_summary,
            "target_contracts_by_lot": resolution.target_contracts_by_lot,
            "resolved_contracts_by_lot": resolution.resolved_contracts_by_lot,
            "remaining_contracts_by_lot": (
                resolution.remaining_contracts_by_lot
            ),
            "resolved_contracts_by_terminal_type": (
                resolution.resolved_contracts_by_terminal_type
            ),
            "lifecycle_reason_codes": reasons,
            "conflict_evidence_ids": sorted(
                set(prior_conflicts + [evidence_id])
            ),
        }
        projected_remaining = {
            lot_id: int(
                sqlite_repo.get_position_lot_fields(
                    lot_id,
                    conn=conn,
                ).get("contracts_open")
                or 0
            )
            for lot_id in sorted(
                dict(
                    lifecycle_case.get("target_contracts_by_lot") or {}
                )
            )
        }
        state_fingerprint = canonical_state_fingerprint(
            _lifecycle_state_payload(
                lifecycle_case=lifecycle_case,
                evidence_rows=(
                    sqlite_repo.list_trade_lifecycle_evidence(
                        case_id=case_id_value,
                        conn=conn,
                    )
                ),
                source_claims=(
                    sqlite_repo.list_trade_lifecycle_source_consumptions(
                        case_id=case_id_value,
                        conn=conn,
                    )
                ),
                allocations=allocations,
                void_event_ids=void_event_ids,
                projected_remaining_by_lot=projected_remaining,
                status=status_value,
                summary=new_summary,
            )
        )
        prior_fingerprint = str(
            prior_summary.get("state_fingerprint") or ""
        ).strip()
        business_state_changed = (
            state_fingerprint != prior_fingerprint
        )
        resolution_revision = int(
            prior_summary.get("resolution_revision") or 0
        ) + int(business_state_changed)
        if resolution_revision <= 0:
            raise ValueError("lifecycle resolution revision is invalid")
        transition_type, transition_key = (
            _lifecycle_notification_transition(
                case_id=case_id_value,
                status=status_value,
            )
        )
        notification_intent = build_notification_intent(
            case_id=case_id_value,
            transition_type=transition_type,
            resolution_revision=resolution_revision,
            delivery_revision=0,
            transition_key=transition_key,
            state_fingerprint=state_fingerprint,
            payload={
                "schema_version": "trade_lifecycle_notification.v1",
                "case_id": case_id_value,
                "transition_type": transition_type,
                "resolution_revision": resolution_revision,
                "state_fingerprint": state_fingerprint,
                "account": lifecycle_case.get("account"),
                "symbol": lifecycle_case.get("symbol"),
                "option_type": lifecycle_case.get("option_type"),
                "position_side": lifecycle_case.get("position_side"),
                "strike": lifecycle_case.get("strike"),
                "expiration_ymd": lifecycle_case.get(
                    "expiration_ymd"
                ),
                "reason_codes": reasons,
                "evidence_id": evidence_id,
            },
        )
        notification_audit_codes = list(
            prior_summary.get("notification_audit_codes") or []
        )
        existing_transition = (
            sqlite_repo.get_trade_lifecycle_notification_by_transition(
                transition_key=transition_key,
                delivery_revision=0,
                conn=conn,
            )
        )
        outbox_created = False
        if business_state_changed:
            if (
                existing_transition is not None
                and (
                    str(
                        existing_transition.get("state_fingerprint")
                        or ""
                    )
                    != state_fingerprint
                    or str(existing_transition.get("payload_hash") or "")
                    != str(notification_intent.get("payload_hash") or "")
                )
            ):
                notification_audit_codes = sorted(
                    set(
                        notification_audit_codes
                        + ["notification_transition_conflict"]
                    )
                )
            else:
                outbox_created = (
                    sqlite_repo.insert_trade_lifecycle_notification_once(
                        notification_intent,
                        conn=conn,
                    )
                )
        new_summary.update(
            {
                "resolution_revision": resolution_revision,
                "state_fingerprint": state_fingerprint,
                "notification_audit_codes": (
                    notification_audit_codes
                ),
            }
        )
        status_changed = sqlite_repo.update_trade_lifecycle_case_derived_status(
            case_id=case_id_value,
            status=status_value,
            derived_summary=new_summary,
            expected_state_fingerprint=prior_fingerprint,
            conn=conn,
        )
        sqlite_repo.assert_foreign_keys_clean(conn=conn)
        return {
            "case_id": case_id_value,
            "evidence_id": evidence_id,
            "evidence_created": evidence_created,
            "evidence_bound": evidence_bound,
            "status": status_value,
            "reason_codes": reasons,
            "status_changed": status_changed,
            "source_claim_created": source_claim_created,
            "resolution_revision": resolution_revision,
            "state_fingerprint": state_fingerprint,
            "business_state_changed": business_state_changed,
            "notification_outbox_id": notification_intent["outbox_id"],
            "notification_outbox_created": outbox_created,
            "notification_audit_codes": notification_audit_codes,
            "terminal_event_ids": [],
            "allocation_ids": [],
        }

    return with_sqlite_repo_transaction(repo, _run)


def advance_lifecycle_case_state_atomically(
    repo: Any,
    *,
    case_id: str,
    status: str,
    derived_summary: dict[str, Any],
    public_transition: str | None,
    expected_lifecycle_generation_token: str | None = None,
) -> dict[str, Any]:
    """Advance a derived lifecycle state and optional fixed Outbox slot."""

    case_id_value = str(case_id or "").strip()
    status_value = str(status or "").strip().lower()
    summary_input = dict(derived_summary or {})
    transition_value = str(public_transition or "").strip().lower()
    if not case_id_value or not status_value:
        raise ValueError("lifecycle state identity is incomplete")
    if transition_value and transition_value not in {
        "option_leg_closed",
        "resolution_confirmed",
        "needs_review",
        "conflict",
    }:
        raise ValueError("lifecycle public transition is invalid")

    def _run(sqlite_repo: Any, conn: Any | None) -> dict[str, Any]:
        if conn is None:
            raise TypeError(
                "lifecycle state advance requires SQLite authority"
            )
        lifecycle_case = sqlite_repo.get_trade_lifecycle_case(
            case_id_value,
            conn=conn,
        )
        if lifecycle_case is None:
            raise ValueError(
                f"lifecycle case not found: {case_id_value}"
            )
        _require_lifecycle_generation(
            sqlite_repo,
            conn=conn,
            case_id=case_id_value,
            expected_generation_token=(
                expected_lifecycle_generation_token
            ),
        )
        prior_summary = (
            dict(lifecycle_case.get("derived_summary") or {})
            if isinstance(lifecycle_case.get("derived_summary"), dict)
            else {}
        )
        void_event_ids = _effective_void_target_ids(
            sqlite_repo,
            conn=conn,
        )
        allocations = list(
            sqlite_repo.list_trade_lifecycle_allocations(
                case_id=case_id_value,
                conn=conn,
            )
        )
        resolution = resolve_allocations(
            lifecycle_case.get("target_contracts_by_lot"),
            allocations,
            void_event_ids=void_event_ids,
        )
        if resolution.status != "ok":
            raise ValueError(
                "existing lifecycle allocations conflict: "
                + ",".join(resolution.reason_codes)
            )
        new_summary = {
            **prior_summary,
            **{
                key: value
                for key, value in summary_input.items()
                if key
                not in {
                    "resolution_revision",
                    "state_fingerprint",
                    "notification_audit_codes",
                }
            },
            "target_contracts_by_lot": (
                resolution.target_contracts_by_lot
            ),
            "resolved_contracts_by_lot": (
                resolution.resolved_contracts_by_lot
            ),
            "remaining_contracts_by_lot": (
                resolution.remaining_contracts_by_lot
            ),
            "resolved_contracts_by_terminal_type": (
                resolution.resolved_contracts_by_terminal_type
            ),
        }
        projected_remaining = {
            lot_id: int(
                sqlite_repo.get_position_lot_fields(
                    lot_id,
                    conn=conn,
                ).get("contracts_open")
                or 0
            )
            for lot_id in sorted(
                dict(
                    lifecycle_case.get("target_contracts_by_lot") or {}
                )
            )
        }
        state_fingerprint = canonical_state_fingerprint(
            _lifecycle_state_payload(
                lifecycle_case=lifecycle_case,
                evidence_rows=(
                    sqlite_repo.list_trade_lifecycle_evidence(
                        case_id=case_id_value,
                        conn=conn,
                    )
                ),
                source_claims=(
                    sqlite_repo.list_trade_lifecycle_source_consumptions(
                        case_id=case_id_value,
                        conn=conn,
                    )
                ),
                allocations=allocations,
                void_event_ids=void_event_ids,
                projected_remaining_by_lot=projected_remaining,
                status=status_value,
                summary=new_summary,
            )
        )
        prior_fingerprint = str(
            prior_summary.get("state_fingerprint") or ""
        ).strip()
        business_state_changed = (
            state_fingerprint != prior_fingerprint
        )
        resolution_revision = int(
            prior_summary.get("resolution_revision") or 0
        ) + int(business_state_changed)
        if resolution_revision <= 0:
            raise ValueError("lifecycle resolution revision is invalid")
        notification_audit_codes = list(
            prior_summary.get("notification_audit_codes") or []
        )
        notification_intent: dict[str, Any] | None = None
        outbox_created = False
        if transition_value:
            transition_key = (
                f"lifecycle:{case_id_value}:{transition_value}"
            )
            notification_intent = build_notification_intent(
                case_id=case_id_value,
                transition_type=transition_value,
                resolution_revision=resolution_revision,
                delivery_revision=0,
                transition_key=transition_key,
                state_fingerprint=state_fingerprint,
                payload={
                    "schema_version": (
                        "trade_lifecycle_notification.v1"
                    ),
                    "case_id": case_id_value,
                    "transition_type": transition_value,
                    "resolution_revision": resolution_revision,
                    "state_fingerprint": state_fingerprint,
                    "account": lifecycle_case.get("account"),
                    "symbol": lifecycle_case.get("symbol"),
                    "option_type": lifecycle_case.get("option_type"),
                    "position_side": lifecycle_case.get(
                        "position_side"
                    ),
                    "strike": lifecycle_case.get("strike"),
                    "expiration_ymd": lifecycle_case.get(
                        "expiration_ymd"
                    ),
                    "close_reason": new_summary.get("close_reason"),
                    "reason_codes": sorted(
                        {
                            str(item)
                            for item in (
                                new_summary.get(
                                    "lifecycle_reason_codes"
                                )
                                or []
                            )
                            if str(item or "").strip()
                        }
                    ),
                },
            )
            existing_transition = (
                sqlite_repo.get_trade_lifecycle_notification_by_transition(
                    transition_key=transition_key,
                    delivery_revision=0,
                    conn=conn,
                )
            )
            if (
                business_state_changed
                and existing_transition is not None
                and (
                    str(
                        existing_transition.get("state_fingerprint")
                        or ""
                    )
                    != state_fingerprint
                    or str(existing_transition.get("payload_hash") or "")
                    != str(notification_intent.get("payload_hash") or "")
                )
            ):
                notification_audit_codes = sorted(
                    set(
                        notification_audit_codes
                        + ["notification_transition_conflict"]
                    )
                )
            elif business_state_changed:
                outbox_created = (
                    sqlite_repo.insert_trade_lifecycle_notification_once(
                        notification_intent,
                        conn=conn,
                    )
                )
        new_summary.update(
            {
                "resolution_revision": resolution_revision,
                "state_fingerprint": state_fingerprint,
                "notification_audit_codes": (
                    notification_audit_codes
                ),
            }
        )
        status_changed = (
            sqlite_repo.update_trade_lifecycle_case_derived_status(
                case_id=case_id_value,
                status=status_value,
                derived_summary=new_summary,
                expected_state_fingerprint=prior_fingerprint,
                conn=conn,
            )
        )
        sqlite_repo.assert_foreign_keys_clean(conn=conn)
        return {
            "case_id": case_id_value,
            "status": status_value,
            "status_changed": status_changed,
            "business_state_changed": business_state_changed,
            "resolution_revision": resolution_revision,
            "state_fingerprint": state_fingerprint,
            "notification_outbox_id": (
                notification_intent.get("outbox_id")
                if notification_intent is not None
                else None
            ),
            "notification_outbox_created": outbox_created,
            "notification_audit_codes": notification_audit_codes,
        }

    return with_sqlite_repo_transaction(repo, _run)


def _validate_existing_lifecycle_evidence(
    *,
    existing: dict[str, Any],
    incoming: dict[str, Any],
    case_id: str,
) -> None:
    for field in (
        "evidence_id",
        "source_type",
        "source_event_id",
        "evidence_type",
        "account",
        "symbol",
        "contracts",
    ):
        if existing.get(field) != incoming.get(field):
            raise ValueError(f"lifecycle evidence immutable conflict: {field}")
    if str(existing.get("case_id") or "").strip() not in {"", case_id}:
        raise ValueError("lifecycle evidence is already bound to another case")


def _effective_void_target_ids(
    sqlite_repo: Any,
    *,
    conn: Any,
) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                target
                for item in sqlite_repo.list_trade_events(conn=conn)
                for target in [valid_void_target_event_id(item)]
                if target
            }
        )
    )


def _positive_lifecycle_contracts(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("lifecycle evidence contracts must be positive")
    try:
        numeric = Decimal(str(value))
        parsed = int(numeric)
    except (InvalidOperation, TypeError, ValueError, OverflowError) as exc:
        raise ValueError("lifecycle evidence contracts must be positive") from exc
    if not numeric.is_finite() or parsed <= 0 or numeric != parsed:
        raise ValueError("lifecycle evidence contracts must be positive")
    return parsed


def _matching_lifecycle_lots(
    position_lots: Sequence[dict[str, Any]],
    *,
    contract_key: ContractKey,
) -> list[tuple[str, int, int]]:
    matches: list[tuple[str, int, int]] = []
    for item in position_lots:
        if not isinstance(item, dict):
            continue
        lot_id = str(item.get("record_id") or "").strip()
        fields = dict(item.get("fields") or {})
        remaining = effective_contracts_open(fields)
        if not lot_id or remaining <= 0:
            continue
        try:
            candidate_key = ContractKey.from_values(
                broker=fields.get("broker"),
                account=fields.get("account"),
                underlying_symbol=fields.get("symbol"),
                option_type=fields.get("option_type"),
                position_side=fields.get("side"),
                strike=effective_strike(fields),
                expiration_ymd=effective_expiration_ymd(fields),
            )
        except (TypeError, ValueError):
            continue
        if candidate_key.position_key != contract_key.position_key:
            continue
        try:
            opened_at = int(fields.get("opened_at") or 0)
        except (TypeError, ValueError):
            opened_at = 0
        matches.append((lot_id, remaining, opened_at))
    return sorted(matches, key=lambda item: (item[2], item[0]))


def _allocate_lifecycle_reservation(
    *,
    contracts: int,
    available_by_lot: dict[str, int],
    matching_lots: Sequence[tuple[str, int, int]],
) -> dict[str, int]:
    remaining = int(contracts)
    allocation: dict[str, int] = {}
    lot_order = [lot_id for lot_id, _contracts, _opened_at in matching_lots]
    lot_order.extend(
        lot_id
        for lot_id in sorted(available_by_lot)
        if lot_id not in lot_order
    )
    for lot_id in lot_order:
        available = int(available_by_lot.get(lot_id, 0))
        if available <= 0 or remaining <= 0:
            continue
        allocated = min(available, remaining)
        allocation[lot_id] = allocated
        remaining -= allocated
    if remaining:
        raise ValueError("lifecycle_reservation_exceeds_available_target")
    return allocation


def _validate_existing_zero_price_evidence(
    *,
    existing: dict[str, Any],
    incoming: dict[str, Any],
    contract_key: ContractKey,
    contracts: int,
) -> None:
    for field in ("evidence_id", "source_type", "source_event_id", "evidence_type"):
        if str(existing.get(field) or "").strip() != str(
            incoming.get(field) or ""
        ).strip():
            raise ValueError(f"lifecycle evidence immutable conflict: {field}")
    if int(existing.get("contracts") or 0) != contracts:
        raise ValueError("lifecycle evidence immutable conflict: contracts")
    if (
        str(existing.get("account") or "").strip().lower()
        != contract_key.account
        or str(existing.get("symbol") or "").strip().upper()
        != contract_key.underlying_symbol
        or str(existing.get("option_type") or "").strip().lower()
        != contract_key.option_type
        or str(existing.get("position_side") or "").strip().lower()
        != contract_key.position_side
        or Decimal(str(existing.get("strike"))) != Decimal(contract_key.strike)
        or str(existing.get("expiration_ymd") or "").strip()
        != contract_key.expiration_ymd
    ):
        raise ValueError("lifecycle evidence immutable conflict: contract_identity")


def _validate_lifecycle_event_allocation_plan(
    *,
    case_id: str,
    lifecycle_case: dict[str, Any],
    evidence: dict[str, Any],
    terminal_events: Sequence[TradeEvent],
    allocations: Sequence[dict[str, Any]],
    existing_allocations: Sequence[dict[str, Any]],
    void_event_ids: Sequence[str] = (),
) -> tuple[dict[str, Any], str]:
    evidence_id = str(evidence.get("evidence_id") or "").strip()
    if not terminal_events or len(terminal_events) != len(allocations):
        raise ValueError("lifecycle evidence requires one terminal event per allocation")
    try:
        evidence_contracts = int(evidence.get("contracts") or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError("lifecycle evidence contracts are invalid") from exc
    if evidence_contracts <= 0:
        raise ValueError("lifecycle evidence contracts must be positive")
    events_by_id = {event.event_id: event for event in terminal_events}
    if len(events_by_id) != len(terminal_events):
        raise ValueError("lifecycle terminal event ids must be unique")
    case_account = str(lifecycle_case.get("account") or "").strip().lower()
    evidence_account = str(evidence.get("account") or "").strip().lower()
    case_symbol = str(lifecycle_case.get("symbol") or "").strip().upper()
    evidence_symbol = str(evidence.get("symbol") or "").strip().upper()
    if evidence_account != case_account or evidence_symbol != case_symbol:
        raise ValueError("lifecycle evidence account or symbol mismatch")
    target_contracts = lifecycle_case.get("target_contracts_by_lot")
    case_contract_key = str(lifecycle_case.get("contract_key") or "").strip()
    allocated_total = 0
    for allocation in allocations:
        if str(allocation.get("case_id") or "").strip() != case_id:
            raise ValueError("lifecycle allocation case mismatch")
        if str(allocation.get("evidence_id") or "").strip() != evidence_id:
            raise ValueError("lifecycle allocation evidence mismatch")
        event_id = str(allocation.get("canonical_terminal_event_id") or "").strip()
        event = events_by_id.get(event_id)
        if event is None:
            raise ValueError("lifecycle allocation terminal event missing")
        contracts = int(allocation.get("contracts_allocated") or 0)
        lot_id = str(allocation.get("target_lot_id") or "").strip()
        terminal_type = str(allocation.get("terminal_type") or "").strip().lower()
        expected_allocation_id = allocation_id_for(
            case_id=case_id,
            evidence_id=evidence_id,
            target_lot_id=lot_id,
        )
        expected_event_id = terminal_event_id_for(
            case_id=case_id,
            evidence_id=evidence_id,
            target_lot_id=lot_id,
            terminal_type=terminal_type,
            contracts_allocated=contracts,
        )
        if str(allocation.get("allocation_id") or "").strip() != expected_allocation_id:
            raise ValueError("lifecycle allocation id is not deterministic")
        if event_id != expected_event_id:
            raise ValueError("lifecycle terminal event id is not deterministic")
        if (
            contracts <= 0
            or event.contracts != contracts
            or str(event.target_lot_id or "") != lot_id
            or event.event_type != terminal_type
            or event.contract_key.position_key != case_contract_key
        ):
            raise ValueError("lifecycle allocation and terminal event mismatch")
        raw_payload = dict(event.raw_payload or {})
        if (
            str(raw_payload.get("case_id") or "").strip() != case_id
            or str(raw_payload.get("evidence_id") or "").strip() != evidence_id
            or str(raw_payload.get("allocation_id") or "").strip()
            != str(allocation.get("allocation_id") or "").strip()
            or int(raw_payload.get("contracts") or 0) != contracts
        ):
            raise ValueError("lifecycle terminal event provenance mismatch")
        allocated_total += contracts
    if allocated_total != evidence_contracts:
        raise ValueError("lifecycle allocated contracts do not equal evidence contracts")
    resolution = resolve_allocations(
        target_contracts,
        [*existing_allocations, *allocations],
        void_event_ids=void_event_ids,
    )
    if resolution.status != "ok":
        raise ValueError(
            "lifecycle allocation conflicts with frozen target: "
            + ",".join(resolution.reason_codes)
        )
    status = (
        "ledger_written"
        if resolution.remaining_contracts == 0
        else "partially_resolved"
    )
    summary = {
        "target_contracts_by_lot": resolution.target_contracts_by_lot,
        "resolved_contracts_by_lot": resolution.resolved_contracts_by_lot,
        "remaining_contracts_by_lot": resolution.remaining_contracts_by_lot,
        "resolved_contracts_by_terminal_type": (
            resolution.resolved_contracts_by_terminal_type
        ),
    }
    return summary, status


def _validate_broker_settlement_pair_for_write(
    repo: Any,
    *,
    conn: Any,
    lifecycle_case: dict[str, Any],
    evidence: dict[str, Any],
) -> None:
    if (
        str(evidence.get("source_type") or "").strip().lower()
        != "broker_settlement_pair"
        or str(
            evidence.get("terminal_type")
            or evidence.get("evidence_type")
            or ""
        ).strip().lower()
        not in {"assignment", "exercise"}
    ):
        return

    case_id = str(lifecycle_case.get("case_id") or "").strip()
    stock = (
        dict(evidence.get("stock_settlement") or {})
        if isinstance(evidence.get("stock_settlement"), dict)
        else {}
    )
    case_futu_account_id = str(
        lifecycle_case.get("futu_account_id") or ""
    ).strip()
    stock_futu_account_id = str(
        stock.get("futu_account_id") or ""
    ).strip()
    if (
        not case_futu_account_id
        or not stock_futu_account_id
        or case_futu_account_id != stock_futu_account_id
    ):
        raise ValueError("stock_settlement_futu_account_mismatch")

    case_symbol = canonical_symbol(lifecycle_case.get("symbol"))
    stock_symbol = canonical_symbol(stock.get("symbol"))
    if not case_symbol or stock_symbol != case_symbol:
        raise ValueError("stock_settlement_symbol_mismatch")

    try:
        strike = Decimal(str(lifecycle_case.get("strike")))
        stock_price = Decimal(str(stock.get("price")))
        shares = Decimal(str(stock.get("shares")))
        multiplier = Decimal(
            str(lifecycle_case.get("multiplier") or 100)
        )
        contracts = int(evidence.get("contracts") or 0)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(
            "stock_settlement_economic_fields_invalid"
        ) from exc
    if (
        not strike.is_finite()
        or not stock_price.is_finite()
        or stock_price != strike
    ):
        raise ValueError("stock_settlement_price_mismatch")
    if (
        contracts <= 0
        or not shares.is_finite()
        or not multiplier.is_finite()
        or multiplier <= 0
        or shares != multiplier * contracts
    ):
        raise ValueError("stock_settlement_quantity_mismatch")

    terminal_type = str(
        evidence.get("terminal_type")
        or evidence.get("evidence_type")
        or ""
    ).strip().lower()
    option_type = str(
        lifecycle_case.get("option_type") or ""
    ).strip().lower()
    position_side = str(
        lifecycle_case.get("position_side") or ""
    ).strip().lower()
    expected_side = {
        ("assignment", "put", "short"): "buy",
        ("assignment", "call", "short"): "sell",
        ("exercise", "call", "long"): "buy",
        ("exercise", "put", "long"): "sell",
    }.get((terminal_type, option_type, position_side))
    actual_side = str(stock.get("side") or "").strip().lower()
    if expected_side is None or actual_side != expected_side:
        raise ValueError("stock_settlement_side_mismatch")

    try:
        settlement_time_ms = int(
            stock.get("event_time_ms")
            or evidence.get("event_time_ms")
            or 0
        )
        option_event_time_ms = int(
            evidence.get("option_event_time_ms") or 0
        )
        observation_start_ms = int(
            lifecycle_case.get("observation_start_ms") or 0
        )
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("stock_settlement_time_invalid") from exc
    if settlement_time_ms <= 0:
        raise ValueError("stock_settlement_time_invalid")
    early_tolerance_ms = 5 * 60 * 1000
    near_option_event = (
        option_event_time_ms > 0
        and abs(settlement_time_ms - option_event_time_ms)
        <= early_tolerance_ms
    )
    if (
        observation_start_ms > 0
        and settlement_time_ms
        < observation_start_ms - early_tolerance_ms
        and not near_option_event
    ):
        raise ValueError("stock_settlement_before_lifecycle_window")
    timing_policy = repo.get_trade_lifecycle_timing_policy(
        case_id,
        conn=conn,
    )
    try:
        settlement_deadline_ms = int(
            (timing_policy or {}).get("settlement_deadline_ms")
            or 0
        )
    except (TypeError, ValueError, OverflowError):
        settlement_deadline_ms = 0
    if settlement_deadline_ms <= 0 and not near_option_event:
        raise ValueError("settlement_deadline_unavailable")
    if (
        settlement_deadline_ms > 0
        and settlement_time_ms > settlement_deadline_ms
    ):
        raise ValueError("stock_settlement_after_deadline")

    candidates = [
        item
        for item in repo.list_trade_lifecycle_cases(conn=conn)
        if _broker_settlement_case_identity_matches(
            item,
            lifecycle_case=lifecycle_case,
            stock_futu_account_id=stock_futu_account_id,
        )
    ]
    candidate_ids = {
        str(item.get("case_id") or "").strip()
        for item in candidates
    }
    if candidate_ids != {case_id}:
        raise ValueError("ambiguous_lifecycle_case_match")


def _broker_settlement_case_identity_matches(
    candidate: dict[str, Any],
    *,
    lifecycle_case: dict[str, Any],
    stock_futu_account_id: str,
) -> bool:
    if (
        str(candidate.get("schema_version") or "").strip()
        != "lifecycle_case.v2"
        or str(candidate.get("superseded_by_case_id") or "").strip()
    ):
        return False
    try:
        return (
            str(candidate.get("account") or "").strip().lower()
            == str(lifecycle_case.get("account") or "").strip().lower()
            and str(
                candidate.get("futu_account_id") or ""
            ).strip()
            == stock_futu_account_id
            and canonical_symbol(candidate.get("symbol"))
            == canonical_symbol(lifecycle_case.get("symbol"))
            and str(
                candidate.get("option_type") or ""
            ).strip().lower()
            == str(
                lifecycle_case.get("option_type") or ""
            ).strip().lower()
            and str(
                candidate.get("position_side") or ""
            ).strip().lower()
            == str(
                lifecycle_case.get("position_side") or ""
            ).strip().lower()
            and Decimal(str(candidate.get("strike")))
            == Decimal(str(lifecycle_case.get("strike")))
            and str(
                candidate.get("expiration_ymd") or ""
            ).strip()
            == str(
                lifecycle_case.get("expiration_ymd") or ""
            ).strip()
        )
    except (InvalidOperation, TypeError, ValueError):
        return False


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
) -> list[LedgerWriteResult]:
    """Persist explicitly targeted canonical events in one transaction."""

    events = list(events)
    case_update = dict(lifecycle_case_update or {})
    allocation_rows = [dict(item or {}) for item in (lifecycle_allocations or [])]
    if not events:
        raise ValueError("atomic trade persistence requires at least one event")

    def _run(sqlite_repo: Any, conn: Any | None) -> list[LedgerWriteResult]:
        if (case_update or allocation_rows) and conn is None:
            raise TypeError("lifecycle metadata persistence requires SQLite transaction authority")
        storage_events: list[TradeEvent] = []
        for event in events:
            expanded = [
                _canonical_storage_event(item)
                for item in _events_for_storage(sqlite_repo, event)
            ]
            if len(expanded) != 1:
                raise ValueError(
                    "atomic trade persistence requires explicitly targeted events"
                )
            storage_events.append(expanded[0])

        event_ids = [event.event_id for event in storage_events]
        if len(set(event_ids)) != len(event_ids):
            raise ValueError("atomic trade persistence contains duplicate event_id")

        existing_events = (
            sqlite_repo.list_trade_events(conn=conn)
            if conn is not None
            else sqlite_repo.list_trade_events()
        )
        existing_by_id = {
            str(item.get("event_id") or ""): item
            for item in existing_events
            if isinstance(item, dict) and str(item.get("event_id") or "")
        }
        fx_payload = load_cash_fx_payload(sqlite_repo)
        observed_at_ms = utc_now_ms()
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
        created_flags = [
            (
                sqlite_repo.upsert_trade_event(event, conn=conn)
                if conn is not None
                else sqlite_repo.upsert_trade_event(event)
            )
            for event in storage_events
        ]
        stored = (
            sqlite_repo.list_trade_events(conn=conn)
            if conn is not None
            else sqlite_repo.list_trade_events()
        )
        projection = project_stored_trade_events_to_position_lots(stored)
        ensure_projection_publishable(projection, operation="atomic trade event persistence")
        lot_count = (
            sqlite_repo.replace_position_lots(projection.lots, conn=conn)
            if conn is not None
            else sqlite_repo.replace_position_lots(projection.lots)
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
                insert_allocation(row, conn=conn)
            sqlite_repo.assert_foreign_keys_clean(conn=conn)
        diagnostics = projection_diagnostics_summary(projection.diagnostics)
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
                    "position_lot_count": int(lot_count),
                    **diagnostics,
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

    return with_sqlite_repo_transaction(repo, _run)


def _events_for_storage(repo: Any, event: Any) -> list[Any]:
    if hasattr(event, "event_type") and not hasattr(event, "position_effect"):
        if bool(getattr(event, "is_close", False)) and not getattr(event, "target_lot_id", None):
            return _canonical_close_events_for_storage(repo, event)
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
        resolution = resolve_fifo_close_targets(repo, selector, source="stored_trade_close")
    except LotCloseResolutionError as exc:
        raise ValueError(f"close trade event target resolution failed: {exc.code}") from exc
    out: list[TradeEvent] = []
    resolution_payload = resolution.to_dict()
    fee_splits = _close_fee_splits(event, resolution.matches)
    for index, match in enumerate(resolution.matches):
        event_id = event.event_id if index == 0 else f"{event.event_id}:target:{match.record_id}"
        match_payload = {
            **payload,
            "record_id": match.record_id,
            "target_lot_id": match.record_id,
            "close_target_resolution": resolution_payload,
        }
        source_event_id = getattr(match.candidate, "source_event_id", None)
        if source_event_id not in (None, ""):
            match_payload["close_target_source_event_id"] = source_event_id
        allocated_fee = fee_splits[index]
        match_payload = _payload_with_allocated_fee(match_payload, allocated_fee)
        out.append(
            replace(
                event,
                event_id=event_id,
                contracts=int(match.contracts_to_close),
                fees=float(allocated_fee),
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


def _canonical_close_events_for_storage(repo: Any, event: TradeEvent) -> list[TradeEvent]:
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
        resolution = resolve_fifo_close_targets(repo, selector, source="stored_canonical_trade_close")
    except LotCloseResolutionError as exc:
        raise ValueError(f"close trade event target resolution failed: {exc.code}") from exc
    out: list[TradeEvent] = []
    resolution_payload = resolution.to_dict()
    fee_splits = _close_fee_splits(event, resolution.matches)
    for index, match in enumerate(resolution.matches):
        event_id = event.event_id if index == 0 else f"{event.event_id}:target:{match.record_id}"
        raw_payload = {
            **dict(event.raw_payload or {}),
            "record_id": match.record_id,
            "target_lot_id": match.record_id,
            "close_target_resolution": resolution_payload,
        }
        source_event_id = getattr(match.candidate, "source_event_id", None)
        if source_event_id not in (None, ""):
            raw_payload["close_target_source_event_id"] = source_event_id
        allocated_fee = fee_splits[index]
        raw_payload = _payload_with_allocated_fee(raw_payload, allocated_fee)
        out.append(
            replace(
                event,
                event_id=event_id,
                contracts=int(match.contracts_to_close),
                fees=float(allocated_fee),
                target_lot_id=match.record_id,
                raw_payload=raw_payload,
            )
        )
    return out


def _close_fee_splits(event: Any, matches: Sequence[Any]) -> list[Decimal]:
    ordered = list(matches)
    if not ordered:
        return []
    total_contracts = sum(int(match.contracts_to_close) for match in ordered)
    if total_contracts <= 0:
        raise ValueError("close fee allocation requires positive matched contracts")

    payload = dict(getattr(event, "raw_payload", {}) or {})
    provenance = payload.get("fee_provenance")
    amount_raw: Any = getattr(event, "fees", 0.0)
    if isinstance(provenance, dict):
        basis = str(provenance.get("basis") or "").strip().lower()
        if basis in {"actual", "estimated"} and provenance.get("amount") not in (None, ""):
            amount_raw = provenance["amount"]
    try:
        total_fee = quantize_money(to_decimal(amount_raw, field_name="close fee"))
    except (TypeError, ValueError):
        try:
            total_fee = quantize_money(to_decimal(getattr(event, "fees", 0.0), field_name="close fee"))
        except (TypeError, ValueError):
            total_fee = Decimal(0)

    allocated_before = Decimal(0)
    out: list[Decimal] = []
    for index, match in enumerate(ordered):
        if index == len(ordered) - 1:
            allocated = quantize_money(total_fee - allocated_before)
        else:
            allocated = quantize_money(total_fee * Decimal(int(match.contracts_to_close)) / Decimal(total_contracts))
        out.append(allocated)
        allocated_before = quantize_money(allocated_before + allocated)
    return out


def _payload_with_allocated_fee(payload: dict[str, Any], amount: Decimal) -> dict[str, Any]:
    out = dict(payload)
    provenance = out.get("fee_provenance")
    if not isinstance(provenance, dict):
        return out
    updated = dict(provenance)
    basis = str(updated.get("basis") or "").strip().lower()
    if basis in {"actual", "estimated"}:
        existing_amount = updated.get("amount")
        if existing_amount not in (None, ""):
            try:
                to_decimal(existing_amount, field_name="fee provenance amount")
            except (TypeError, ValueError):
                return out
        updated["amount"] = canonical_decimal_text(amount, field_name="allocated close fee")
    elif basis == "missing":
        updated.pop("amount", None)
    out["fee_provenance"] = updated
    return out


def _trade_event_from_normalized_deal(deal: Any) -> TradeEvent:
    trade_side = normalize_trade_side(getattr(deal, "side", None)) or ""
    position_effect = normalize_position_effect(getattr(deal, "position_effect", None)) or ""
    raw_payload = dict(getattr(deal, "raw_payload", {}) or {})
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
    actual_fees = extract_actual_fees(raw_payload)
    if actual_fees is not None:
        raw_payload["fee_provenance"] = {
            "basis": "actual",
            "source": actual_fees["source"],
            "components": actual_fees["components"],
        }
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
        fees=float(actual_fees["amount"]) if actual_fees is not None else 0.0,
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
