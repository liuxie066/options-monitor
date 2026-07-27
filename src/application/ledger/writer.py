from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Sequence

from domain.domain.combo_identity import identity_from_intent
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
    derive_lifecycle_read_model,
    expiration_observation_start_ms,
)
from domain.domain.performance.models import canonical_decimal_text, quantize_money, to_decimal
from domain.domain.symbol_identity import symbol_market
from domain.domain.trade_contract_identity import (
    canonical_contract_symbol,
    normalize_contract_expiration,
    normalize_position_effect,
    normalize_trade_side,
)
from src.application.ledger.lot_resolver import LotCloseResolutionError, LotCloseSelector, resolve_fifo_close_targets
from src.application.ledger.event_codec import encode_trade_event_for_storage
from src.application.ledger.external_event_key import broker_external_event_key
from src.application.ledger.publisher import project_stored_trade_events_to_position_lots
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


def rebuild_position_lots_from_trade_events(repo: Any) -> ProjectionRefreshResult:
    def _run(sqlite_repo: Any, conn: Any | None) -> ProjectionRefreshResult:
        if conn is not None:
            events = sqlite_repo.list_trade_events(conn=conn)
            projection = project_stored_trade_events_to_position_lots(events)
            inserted = sqlite_repo.replace_position_lots(projection.lots, conn=conn)
        else:
            events = sqlite_repo.list_trade_events()
            projection = project_stored_trade_events_to_position_lots(events)
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
            records = projection.lots
            lot_count = sqlite_repo.replace_position_lots(records, conn=conn)
        else:
            created_flags = [sqlite_repo.upsert_trade_event(item) for item in storage_events]
            projection = project_stored_trade_events_to_position_lots(sqlite_repo.list_trade_events())
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
        identity_created = sqlite_repo.insert_strategy_group_identity(identity, conn=conn)
        sqlite_repo.assert_foreign_keys_clean(conn=conn)
        return {
            "event_id": storage_event.event_id,
            "record_id": second_leg["record_id"],
            "event_created": created,
            "identity_created": identity_created,
            "identity": identity,
            "position_lot_count": len(projection.lots),
        }

    return with_sqlite_repo_transaction(repo, _run)


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


def apply_lifecycle_allocation_atomically(
    repo: Any,
    *,
    case_id: str,
    evidence: dict[str, Any],
    terminal_events: Sequence[Any],
    allocations: Sequence[dict[str, Any]],
    derived_status: str,
    derived_summary: dict[str, Any],
) -> dict[str, Any]:
    """Adopt evidence, terminal events, projection and allocations as one fact."""

    case_id_value = str(case_id or "").strip()
    evidence_payload = dict(evidence or {})
    allocation_rows = [dict(item or {}) for item in allocations]
    event_rows = [_canonical_storage_event(item) for item in terminal_events]

    def _run(sqlite_repo: Any, conn: Any | None) -> dict[str, Any]:
        if conn is None:
            raise TypeError("lifecycle allocation requires SQLite transaction authority")
        sqlite_repo.assert_foreign_keys_clean(conn=conn)
        lifecycle_case = sqlite_repo.get_trade_lifecycle_case(case_id_value, conn=conn)
        if lifecycle_case is None:
            raise ValueError(f"lifecycle case not found: {case_id_value}")
        evidence_id = str(evidence_payload.get("evidence_id") or "").strip()
        if not evidence_id:
            raise ValueError("lifecycle evidence_id is required")
        if evidence_payload.get("case_id") not in (None, "", case_id_value):
            raise ValueError("lifecycle evidence is bound to another case")
        existing_evidence = sqlite_repo.get_trade_lifecycle_evidence(evidence_id, conn=conn)
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

        canonical_summary, canonical_status = _validate_lifecycle_event_allocation_plan(
            case_id=case_id_value,
            lifecycle_case=lifecycle_case,
            evidence=evidence_payload,
            terminal_events=event_rows,
            allocations=allocation_rows,
            existing_allocations=case_allocations,
        )
        requested_status = str(derived_status or "").strip().lower()
        if requested_status != canonical_status:
            raise ValueError("lifecycle derived status mismatch")
        incoming_summary = dict(derived_summary or {})
        for field, expected in canonical_summary.items():
            if field in incoming_summary and incoming_summary[field] != expected:
                raise ValueError(f"lifecycle derived summary mismatch: {field}")
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
        event_created = [
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
        status_changed = sqlite_repo.update_trade_lifecycle_case_derived_status(
            case_id=case_id_value,
            status=canonical_status,
            derived_summary=canonical_summary,
            conn=conn,
        )
        sqlite_repo.assert_foreign_keys_clean(conn=conn)
        return {
            "case_id": case_id_value,
            "evidence_id": evidence_id,
            "evidence_created": evidence_created,
            "evidence_bound": evidence_bound,
            "terminal_event_ids": [item.event_id for item in event_rows],
            "terminal_events_created": event_created,
            "allocation_ids": [str(item.get("allocation_id") or "") for item in allocation_rows],
            "allocations_created": allocation_created,
            "status_changed": status_changed,
            "position_lot_count": len(projection.lots),
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
                    existing_cases.append(lifecycle_case)
            else:
                would_create_case_ids.append(case_id)

        refreshed_case_ids: list[str] = []
        would_refresh_case_ids: list[str] = []
        for lifecycle_case in existing_cases:
            case_id = str(lifecycle_case.get("case_id") or "").strip()
            if (
                not case_id
                or str(lifecycle_case.get("schema_version") or "").strip()
                != "lifecycle_case.v2"
            ):
                continue
            persisted_status = str(lifecycle_case.get("status") or "").strip().lower()
            if persisted_status in {"ledger_written", "conflict"}:
                continue
            allocations = list(
                sqlite_repo.list_trade_lifecycle_allocations(
                    case_id=case_id,
                    conn=conn,
                )
            )
            read_model = derive_lifecycle_read_model(
                expiration_ymd=str(lifecycle_case.get("expiration_ymd") or ""),
                market=str(
                    lifecycle_case.get("market")
                    or symbol_market(lifecycle_case.get("symbol"))
                    or ""
                ),
                target_contracts_by_lot=dict(
                    lifecycle_case.get("target_contracts_by_lot") or {}
                ),
                allocations=allocations,
                now_ms=current_ms,
            )
            next_status = {
                "settlement_pending": "waiting_settlement_evidence",
                "partially_resolved": "partially_resolved",
                "needs_review": "needs_review",
                "assigned": "ledger_written",
                "exercised": "ledger_written",
                "expired_unassigned": "ledger_written",
                "resolved_mixed": "ledger_written",
                "conflict": "conflict",
            }.get(read_model.lifecycle_state, persisted_status)
            if next_status != persisted_status:
                if apply_changes:
                    sqlite_repo.update_trade_lifecycle_case_derived_status(
                        case_id=case_id,
                        status=next_status,
                        derived_summary={
                            "target_contracts_by_lot": dict(
                                lifecycle_case.get("target_contracts_by_lot") or {}
                            ),
                            "resolved_contracts_by_lot": (
                                read_model.resolved_contracts_by_lot
                            ),
                            "remaining_contracts_by_lot": (
                                read_model.remaining_contracts_by_lot
                            ),
                            "resolved_contracts_by_terminal_type": (
                                read_model.resolved_contracts_by_terminal_type
                            ),
                            "lifecycle_reason_codes": list(
                                read_model.lifecycle_reason_codes
                            ),
                        },
                        conn=conn,
                    )
                    refreshed_case_ids.append(case_id)
                else:
                    would_refresh_case_ids.append(case_id)
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
        resolution = resolve_allocations(
            lifecycle_case.get("target_contracts_by_lot"),
            allocations,
        )
        prior_summary = dict(lifecycle_case.get("derived_summary") or {})
        prior_conflicts = list(prior_summary.get("conflict_evidence_ids") or [])
        status_changed = sqlite_repo.update_trade_lifecycle_case_derived_status(
            case_id=case_id_value,
            status=status_value,
            derived_summary={
                "target_contracts_by_lot": resolution.target_contracts_by_lot,
                "resolved_contracts_by_lot": resolution.resolved_contracts_by_lot,
                "remaining_contracts_by_lot": resolution.remaining_contracts_by_lot,
                "resolved_contracts_by_terminal_type": (
                    resolution.resolved_contracts_by_terminal_type
                ),
                "lifecycle_reason_codes": reasons,
                "conflict_evidence_ids": sorted(
                    set(prior_conflicts + [evidence_id])
                ),
            },
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
            "terminal_event_ids": [],
            "allocation_ids": [],
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


def _validate_lifecycle_event_allocation_plan(
    *,
    case_id: str,
    lifecycle_case: dict[str, Any],
    evidence: dict[str, Any],
    terminal_events: Sequence[TradeEvent],
    allocations: Sequence[dict[str, Any]],
    existing_allocations: Sequence[dict[str, Any]],
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


def persist_trade_event(repo: Any, deal: Any) -> LedgerWriteResult:
    return persist_trade_event_object(repo, _trade_event_from_normalized_deal(deal))


def persist_normalized_trade_events_atomically(
    repo: Any,
    deals: Sequence[Any],
) -> list[LedgerWriteResult]:
    """Persist explicitly targeted broker-event splits in one transaction."""

    return persist_trade_event_objects_atomically(
        repo,
        [_trade_event_from_normalized_deal(deal) for deal in deals],
    )


def persist_trade_event_objects_atomically(
    repo: Any,
    events: Sequence[Any],
) -> list[LedgerWriteResult]:
    """Persist explicitly targeted canonical events in one transaction."""

    events = list(events)
    if not events:
        raise ValueError("atomic trade persistence requires at least one event")

    def _run(sqlite_repo: Any, conn: Any | None) -> list[LedgerWriteResult]:
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
        lot_count = (
            sqlite_repo.replace_position_lots(projection.lots, conn=conn)
            if conn is not None
            else sqlite_repo.replace_position_lots(projection.lots)
        )
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
