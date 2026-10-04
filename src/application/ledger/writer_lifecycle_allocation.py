from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from domain.domain.lifecycle_allocation import validate_stock_settlement_allocation_group
from domain.domain.ledger.cash_facts import cash_facts_for_trade_event
from domain.domain.money import quantize_money
from domain.domain.performance.cash_conversion import (
    cash_conversion_id,
    cash_conversion_identity,
    validate_observed_cash_conversion,
)
from src.application.ledger.external_event_key import futu_compatibility_source_key

from .writer_common import (
    Any,
    LifecycleAttemptAuditEnvelope,
    Sequence,
    attach_trade_event_cash_conversions,
    build_notification_intent,
    build_source_consumption_claim,
    canonical_state_fingerprint,
    load_cash_fx_payload,
    resolve_allocations,
    run_position_projection_in_transaction,
    utc_now_ms,
    with_sqlite_repo_transaction,
)

from .writer_decision import (
    _advance_settlement_admission_head,
    _append_lifecycle_observation_attempt,
    _begin_lifecycle_decision_projection,
    _finish_lifecycle_attempt_cleanup,
    _finish_lifecycle_decision_projection,
    _lifecycle_resolution_after_allocations,
    _match_lifecycle_attempt_replay,
    _persist_settlement_admission_evidence,
    _prepare_settlement_admission,
    _require_lifecycle_generation,
    _trade_events_by_id,
)

from .writer_lifecycle_support import (
    _effective_void_target_ids,
    _lifecycle_notification_transition,
    _lifecycle_state_payload,
    _projected_remaining_by_lot,
    _require_duplicate_settlement_allocation_state,
    _require_settlement_foreign_keys_clean,
    _validate_broker_settlement_pair_for_write,
    _validate_existing_lifecycle_evidence,
    _validate_lifecycle_event_allocation_plan,
)

from .writer_trade_events import (
    _canonical_rows,
    _canonical_storage_event,
    _event_with_existing_cash_conversions,
    _event_with_audited_settlement_fee,
    _prepare_fee_evidence_for_storage,
)


def _retain_pending_close_economics(event: Any, original: dict[str, Any]) -> Any:
    """Move current broker option fee and its frozen FX to the replacement."""
    prior = _canonical_storage_event(original)
    prior_raw = dict(prior.raw_payload or {})
    raw = dict(event.raw_payload or {})
    if raw.get("pending_close_event_id") != prior.event_id:
        raise ValueError("pending close economic source mismatch")
    raw["fee_provenance"] = dict(prior_raw.get("fee_provenance") or {})
    event = replace(event, fees=prior.fees, raw_payload=raw)
    old_conversions = prior_raw.get("cash_conversions") or {}
    new_conversions = dict(raw.get("cash_conversions") or {})
    for fact in cash_facts_for_trade_event(event):
        if not fact.fact_kind.startswith("option_"):
            continue
        old = old_conversions.get(fact.fact_kind) if isinstance(old_conversions, dict) else None
        if not isinstance(old, dict):
            new_conversions.pop(fact.fact_kind, None)
            continue
        if old.get("status") != "observed":
            continue
        if fact.amount is None or not fact.currency:
            raise ValueError("pending close cash fact changed")
        rate = old.get("fx_rate")
        amount_cny = (
            quantize_money(fact.amount * Decimal(str(rate)))
            if rate is not None else Decimal(0)
        )
        identity = cash_conversion_identity(
            cash_fact_id=fact.fact_id,
            native_amount=fact.amount,
            native_currency=fact.currency,
            fx_rate=rate,
            amount_cny=amount_cny,
            rate_source_id=old.get("rate_source_id"),
            effective_at_ms=fact.effective_at_ms,
        )
        updated = {
            **old, **identity,
            "conversion_id": cash_conversion_id(identity),
        }
        _value, problem = validate_observed_cash_conversion(
            updated, cash_fact_id=fact.fact_id,
            native_amount=fact.amount, native_currency=fact.currency,
            effective_at_ms=fact.effective_at_ms,
        )
        if problem:
            raise ValueError("pending close cash conversion invalid: " + problem)
        new_conversions[fact.fact_kind] = updated
    if new_conversions:
        raw["cash_conversions"] = new_conversions
    else:
        raw.pop("cash_conversions", None)
    return replace(event, raw_payload=raw)

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
    notification_status: str = "pending",
    broker_ownership_validator: Any = None,
    attempt_evidence: dict[str, Any] | None = None,
    attempt_audit: LifecycleAttemptAuditEnvelope | None = None,
    wheel_start_enabled: bool = False,
    _conn: Any = None,
    _fresh_anchor_evidence: bool = False,
    _new_case: bool = False,
    _decision_fence: Any = None,
    _prior_decision_fact: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Adopt evidence, terminal events, projection and allocations as one fact."""

    from src.application.ledger.wheel_trade_companions import (
        append_wheel_trade_companions,
        capture_wheel_trade_companion_context,
    )

    case_id_value = str(case_id or "").strip()
    evidence_payload = dict(evidence or {})
    attempt_evidence_payload = dict(attempt_evidence or {})
    allocation_rows = [dict(item or {}) for item in allocations]
    event_rows = [_canonical_storage_event(item) for item in terminal_events]
    correction_void_rows = [
        _canonical_storage_event(item)
        for item in correction_void_events
    ]

    def _run(sqlite_repo: Any, conn: Any | None) -> dict[str, Any]:
        if conn is None:
            raise TypeError("lifecycle allocation requires SQLite transaction authority")
        replay = _match_lifecycle_attempt_replay(
            sqlite_repo,
            conn=conn,
            case_id=case_id_value,
            attempt_audit=attempt_audit,
        )
        if replay is not None:
            return replay
        if attempt_evidence_payload and attempt_audit is None:
            raise ValueError(
                "lifecycle attempt evidence requires an attempt audit"
            )
        _require_settlement_foreign_keys_clean(sqlite_repo, conn=conn)
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
        decision_fence, prior_decision_fact = (
            (_decision_fence, _prior_decision_fact)
            if _decision_fence is not None
            else _begin_lifecycle_decision_projection(
                sqlite_repo,
                conn=conn,
                lifecycle_case=lifecycle_case,
                allow_missing_fact=_new_case,
                global_event_owner=bool(event_rows or correction_void_rows),
            )
        )
        admission = _prepare_settlement_admission(
            sqlite_repo,
            conn=conn,
            case_id=case_id_value,
            evidence=(
                attempt_evidence_payload or evidence_payload
            ),
            expected_generation_token=(
                expected_lifecycle_generation_token
            ),
        )
        if attempt_audit is not None and admission is None:
            raise ValueError(
                "lifecycle allocation attempt audit requires observation admission"
            )
        if (
            not attempt_evidence_payload
            and admission is not None
            and bool(admission.get("duplicate"))
        ):
            duplicate_state = (
                _require_duplicate_settlement_allocation_state(
                    sqlite_repo,
                    conn=conn,
                    lifecycle_case=lifecycle_case,
                    admission=admission,
                    requested_status=derived_status,
                )
            )
            current_summary = duplicate_state["summary"]
            audit_result = _append_lifecycle_observation_attempt(
                sqlite_repo,
                conn=conn,
                attempt_audit=attempt_audit,
                admission=admission,
            )
            decision_projection = _finish_lifecycle_decision_projection(
                sqlite_repo,
                conn=conn,
                fence=decision_fence,
                prior_fact=prior_decision_fact,
                case_id=case_id_value,
                publish_case=bool(admission.get("head_repaired")),
            )
            return {
                "case_id": case_id_value,
                "evidence_id": admission["evidence_id"],
                "evidence_created": False,
                "evidence_bound": False,
                "stock_source_claim_created": False,
                "close_source_claim_created": False,
                "terminal_event_ids": [],
                "terminal_events_created": [],
                "correction_void_event_ids": [],
                "correction_void_events_created": [],
                "allocation_ids": [],
                "allocations_created": [],
                "status_changed": False,
                "resolution_revision": int(
                    current_summary.get("resolution_revision") or 0
                ),
                "state_fingerprint": str(
                    current_summary.get("state_fingerprint") or ""
                ),
                "business_state_changed": False,
                "notification_outbox_id": None,
                "notification_outbox_created": False,
                "notification_audit_codes": list(
                    current_summary.get("notification_audit_codes") or []
                ),
                "position_lot_count": len(
                    sqlite_repo.list_position_lots(conn=conn)
                ),
                "admission_status": "duplicate_semantic",
                "semantic_fingerprint": admission[
                    "semantic_fingerprint"
                ],
                "decision_projection": decision_projection,
                **audit_result,
            }
        if attempt_evidence_payload:
            _persist_settlement_admission_evidence(
                sqlite_repo,
                conn=conn,
                case_id=case_id_value,
                evidence=attempt_evidence_payload,
                admission=admission,
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
        if (existing_evidence is not None and not existing_evidence_allocations
                and not _fresh_anchor_evidence):
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

        proposed_void_target_ids: set[str] = set()
        prior_pending_by_lot: dict[str, dict[str, Any]] = {}
        if correction_void_rows:
            anchor_id = str(
                evidence_payload.get("pending_close_anchor_evidence_id") or ""
            ).strip()
            if anchor_id:
                anchor = sqlite_repo.get_trade_lifecycle_evidence(anchor_id, conn=conn)
                source_claims = sqlite_repo.list_trade_lifecycle_source_consumptions(
                    case_id=case_id_value, conn=conn,
                )
                if (
                    not isinstance(anchor, dict)
                    or str(anchor.get("case_id") or "") != case_id_value
                    or str(anchor.get("evidence_type") or "") != "option_zero_price_close"
                    or not any(
                        str(claim.get("owner_evidence_id") or "") == anchor_id
                        and str(claim.get("source_role") or "") == "option_anchor"
                        for claim in source_claims
                    )
                ):
                    raise ValueError("pending_close_anchor_unproven")
                anchor_allocations = [
                    item for item in case_allocations
                    if str(item.get("evidence_id") or "") == anchor_id
                    and str(item.get("canonical_terminal_event_id") or "") not in void_event_ids
                ]
                target_ids = {
                    str(item.get("canonical_terminal_event_id") or "")
                    for item in anchor_allocations
                }
                if (
                    not target_ids
                    or target_ids != {str(item.target_event_id or "") for item in correction_void_rows}
                    or sum(int(item.get("contracts_allocated") or 0) for item in anchor_allocations)
                    != int(evidence_payload.get("contracts") or 0)
                ):
                    raise ValueError("pending_close_replacement_not_exact")
                prior_events = _trade_events_by_id(sqlite_repo, target_ids, conn=conn)
                replacements = {str(item.target_lot_id or ""): item for item in event_rows}
                if len(replacements) != len(anchor_allocations):
                    raise ValueError("pending_close_replacement_not_exact")
                for item in anchor_allocations:
                    event_id = str(item.get("canonical_terminal_event_id") or "")
                    original = prior_events.get(event_id)
                    lot_id = str(item.get("target_lot_id") or "")
                    replacement = replacements.get(lot_id)
                    if (
                        not isinstance(original, dict)
                        or str(original.get("event_type") or "") != "close"
                        or str((original.get("raw_payload") or {}).get("close_type") or "")
                        != "cause_pending"
                        or replacement is None
                        or replacement.contracts != int(item.get("contracts_allocated") or 0)
                        or replacement.event_time_ms != int(original.get("event_time_ms") or 0)
                        or replacement.contract_key != _canonical_storage_event(original).contract_key
                        or any(
                            str(replacement.raw_payload.get(key) or "")
                            != str((original.get("raw_payload") or {}).get(key) or "")
                            for key in ("source_deal_id", "futu_account_id", "order_id")
                        )
                    ):
                        raise ValueError("pending_close_replacement_not_exact")
                    prior_pending_by_lot[lot_id] = original
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
                proposed_void_target_ids.add(target_event_id)
            void_event_ids = tuple(
                sorted(set(void_event_ids) | proposed_void_target_ids)
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
            if stock_source_key and any(
                futu_compatibility_source_key(
                    account=row.get("account"),
                    futu_account_id=row.get("futu_account_id"),
                    source_deal_id=row.get("source_deal_id"),
                    execution_input=row.get("execution_input"),
                ) == stock_source_key
                for row in sqlite_repo.list_assigned_stock_events(conn=conn)
            ):
                raise ValueError("broker_stock_source_already_consumed")
            if (
                str(evidence_payload.get("source_type") or "") == "broker_settlement_pair"
                and not existing_evidence_allocations
            ):
                if not callable(broker_ownership_validator):
                    raise ValueError("broker_ownership_validation_required")
                broker_ownership_validator(
                    sqlite_repo, conn=conn, case=lifecycle_case, evidence=evidence_payload,
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
        wheel_context = capture_wheel_trade_companion_context(
            sqlite_repo,
            conn=conn,
            events=event_rows,
            wheel_start_enabled=wheel_start_enabled,
        )
        projection_rows = [*correction_void_rows, *event_rows]
        existing_by_id = _trade_events_by_id(
            sqlite_repo,
            [item.event_id for item in projection_rows],
            conn=conn,
        )
        projection_rows = [
            _event_with_audited_settlement_fee(event, existing_by_id[event.event_id], conn=conn)
            if event.event_id in existing_by_id else event
            for event in projection_rows
        ]
        settlement_rows = [
            event
            for event in projection_rows
            if event.event_type in {"assignment", "exercise"}
        ]
        if settlement_rows:
            incoming_settlement_source = validate_stock_settlement_allocation_group(
                settlement_rows
            )
            existing_settlement_source = validate_stock_settlement_allocation_group(
                [
                    _canonical_storage_event(existing_by_id[event.event_id])
                    if event.event_id in existing_by_id
                    else event
                    for event in settlement_rows
                ]
            )
            if existing_settlement_source != incoming_settlement_source:
                raise ValueError("lifecycle stock settlement replay source conflicts")
        observed_at_ms = utc_now_ms()
        projection_rows = _prepare_fee_evidence_for_storage(
            projection_rows,
            existing_by_id=existing_by_id,
            frozen_at_ms=observed_at_ms,
        )
        fx_payload = load_cash_fx_payload(sqlite_repo, conn=conn)
        projection_rows = [
            _event_with_existing_cash_conversions(item, existing_by_id[item.event_id])
            if item.event_id in existing_by_id
            else attach_trade_event_cash_conversions(
                item,
                fx_payload=fx_payload,
                observed_at_ms=observed_at_ms,
            )
            for item in projection_rows
        ]
        if prior_pending_by_lot:
            projection_rows = [
                _retain_pending_close_economics(item, prior_pending_by_lot[item.target_lot_id])
                if item.target_lot_id in prior_pending_by_lot and item.event_type != "void"
                else item
                for item in projection_rows
            ]
        runtime = run_position_projection_in_transaction(
            sqlite_repo,
            projection_rows,
            conn=conn,
            mode="forced_full",
        )
        if settlement_rows:
            stored_by_id = _trade_events_by_id(
                sqlite_repo,
                [event.event_id for event in settlement_rows],
                conn=conn,
            )
            stored_settlement_source = validate_stock_settlement_allocation_group(
                [
                    _canonical_storage_event(stored_by_id[event.event_id])
                    for event in settlement_rows
                ]
            )
            if stored_settlement_source != incoming_settlement_source:
                raise ValueError("stored lifecycle stock settlement source conflicts")
        correction_count = len(correction_void_rows)
        correction_void_created = list(runtime.created_flags[:correction_count])
        terminal_event_created = list(runtime.created_flags[correction_count:])
        wheel_companions, wheel_companion_review_reasons = append_wheel_trade_companions(
            sqlite_repo,
            conn=conn,
            events=projection_rows,
            created_flags=runtime.created_flags,
            context=wheel_context,
            recorded_at_ms=utc_now_ms(),
        )
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
        target_lot_ids = list(
            dict(lifecycle_case.get("target_contracts_by_lot") or {})
        )
        projected_remaining = _projected_remaining_by_lot(
            sqlite_repo.get_position_lots_by_ids(
                target_lot_ids,
                conn=conn,
            ),
            target_lot_ids=target_lot_ids,
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
                "market": lifecycle_case.get("market"),
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
            status=notification_status,
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
        _advance_settlement_admission_head(
            sqlite_repo,
            conn=conn,
            case_id=case_id_value,
            admission=admission,
        )
        audit_result = _append_lifecycle_observation_attempt(
            sqlite_repo,
            conn=conn,
            attempt_audit=attempt_audit,
            admission=admission,
        )
        if correction_void_rows:
            corrected = resolve_allocations(
                lifecycle_case["target_contracts_by_lot"],
                post_allocations,
                void_event_ids=void_event_ids,
            )
            if corrected.status != "ok":
                raise ValueError("corrected lifecycle allocation conflict")
            resolution_update = {
                "resolved_contracts_by_lot": corrected.resolved_contracts_by_lot,
                "remaining_contracts_by_lot": corrected.remaining_contracts_by_lot,
                "resolved_contracts_by_terminal_type": corrected.resolved_contracts_by_terminal_type,
            }
        else:
            resolution_update = _lifecycle_resolution_after_allocations(
                prior_decision_fact,
                allocations=allocation_rows,
                created_flags=allocation_created,
            )
        decision_projection = _finish_lifecycle_decision_projection(
            sqlite_repo,
            conn=conn,
            fence=decision_fence,
            prior_fact=prior_decision_fact,
            case_id=case_id_value,
            resolution=resolution_update,
            trade_event_mutations=tuple(zip(
                projection_rows if correction_void_rows else event_rows,
                runtime.created_flags if correction_void_rows else terminal_event_created,
                strict=True,
            )),
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
            "wheel_event_ids_by_trade_event": wheel_companions,
            "wheel_manual_review_reasons_by_trade_event": (
                wheel_companion_review_reasons
            ),
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
            "position_lot_count": int(runtime.position_lot_count),
            "admission_status": (
                "admitted_semantic"
                if admission is not None
                else "not_applicable"
            ),
            "semantic_fingerprint": (
                admission.get("semantic_fingerprint")
                if admission is not None
                else None
            ),
            "decision_projection": decision_projection,
            **audit_result,
        }

    if _conn is not None:
        return _run(repo, _conn)
    return _finish_lifecycle_attempt_cleanup(
        repo,
        with_sqlite_repo_transaction(
            repo,
            _run,
            require_projection_publication=True,
        ),
    )
