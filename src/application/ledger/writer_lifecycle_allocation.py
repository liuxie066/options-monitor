from __future__ import annotations

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
    _defer_lifecycle_decision_projection,
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
    _prepare_fee_evidence_for_storage,
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
    attempt_evidence: dict[str, Any] | None = None,
    attempt_audit: LifecycleAttemptAuditEnvelope | None = None,
    wheel_start_enabled: bool = False,
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
            _begin_lifecycle_decision_projection(
                sqlite_repo,
                conn=conn,
                lifecycle_case=lifecycle_case,
                global_event_owner=bool(
                    event_rows or correction_void_rows
                ),
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

        proposed_void_target_ids: set[str] = set()
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
        observed_at_ms = utc_now_ms()
        projection_rows = _prepare_fee_evidence_for_storage(
            projection_rows,
            existing_by_id=existing_by_id,
            frozen_at_ms=observed_at_ms,
        )
        fx_payload = load_cash_fx_payload(sqlite_repo)
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
        runtime = run_position_projection_in_transaction(
            sqlite_repo,
            projection_rows,
            conn=conn,
            mode="forced_full",
        )
        correction_count = len(correction_void_rows)
        correction_void_created = list(runtime.created_flags[:correction_count])
        terminal_event_created = list(runtime.created_flags[correction_count:])
        wheel_companions = append_wheel_trade_companions(
            sqlite_repo,
            conn=conn,
            events=event_rows,
            created_flags=terminal_event_created,
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
            decision_projection = _defer_lifecycle_decision_projection(
                decision_fence
            )
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
                trade_event_mutations=tuple(
                    zip(
                        event_rows,
                        terminal_event_created,
                        strict=True,
                    )
                ),
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

    return _finish_lifecycle_attempt_cleanup(
        repo,
        with_sqlite_repo_transaction(
            repo,
            _run,
            require_projection_publication=True,
        ),
    )
