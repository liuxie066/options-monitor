from __future__ import annotations

from .writer_common import (
    Any,
    LifecycleAttemptAuditEnvelope,
    build_notification_intent,
    canonical_state_fingerprint,
    resolve_allocations,
    with_sqlite_repo_transaction,
)

from .writer_decision import (
    _advance_settlement_admission_head,
    _append_lifecycle_observation_attempt,
    _begin_lifecycle_decision_projection,
    _finish_lifecycle_attempt_cleanup,
    _finish_lifecycle_decision_projection,
    _match_lifecycle_attempt_replay,
    _persist_settlement_admission_evidence,
    _prepare_settlement_admission,
    _require_lifecycle_generation,
)

from .writer_lifecycle_support import (
    _effective_void_target_ids,
    _lifecycle_state_payload,
    _require_settlement_foreign_keys_clean,
)

def advance_lifecycle_case_state_atomically(
    repo: Any,
    *,
    case_id: str,
    status: str,
    derived_summary: dict[str, Any],
    public_transition: str | None,
    expected_lifecycle_generation_token: str | None = None,
    evidence: dict[str, Any] | None = None,
    attempt_audit: LifecycleAttemptAuditEnvelope | None = None,
) -> dict[str, Any]:
    """Advance a derived lifecycle state and optional fixed Outbox slot."""

    case_id_value = str(case_id or "").strip()
    status_value = str(status or "").strip().lower()
    summary_input = dict(derived_summary or {})
    evidence_payload = dict(evidence or {})
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
        replay = _match_lifecycle_attempt_replay(
            sqlite_repo,
            conn=conn,
            case_id=case_id_value,
            attempt_audit=attempt_audit,
        )
        if replay is not None:
            return replay
        if attempt_audit is None and evidence_payload:
            raise ValueError(
                "lifecycle state attempt evidence requires an attempt audit"
            )
        _require_settlement_foreign_keys_clean(
            sqlite_repo,
            conn=conn,
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
        decision_fence, prior_decision_fact = (
            _begin_lifecycle_decision_projection(
                sqlite_repo,
                conn=conn,
                lifecycle_case=lifecycle_case,
            )
        )
        admission = _prepare_settlement_admission(
            sqlite_repo,
            conn=conn,
            case_id=case_id_value,
            evidence=evidence_payload,
            expected_generation_token=(
                expected_lifecycle_generation_token
            ),
        )
        if attempt_audit is not None and admission is None:
            raise ValueError(
                "lifecycle state attempt audit requires observation admission"
            )
        evidence_created, evidence_bound = (
            _persist_settlement_admission_evidence(
                sqlite_repo,
                conn=conn,
                case_id=case_id_value,
                evidence=evidence_payload,
                admission=admission,
            )
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
        decision_projection = _finish_lifecycle_decision_projection(
            sqlite_repo,
            conn=conn,
            fence=decision_fence,
            prior_fact=prior_decision_fact,
            case_id=case_id_value,
        )
        sqlite_repo.assert_foreign_keys_clean(conn=conn)
        return {
            "case_id": case_id_value,
            "evidence_id": (
                str(admission.get("evidence_id") or "").strip()
                if admission is not None
                else None
            ),
            "evidence_created": evidence_created,
            "evidence_bound": evidence_bound,
            "admission_status": (
                "duplicate_semantic"
                if admission is not None and bool(admission.get("duplicate"))
                else (
                    "admitted_semantic"
                    if admission is not None
                    else "not_applicable"
                )
            ),
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
            "decision_projection": decision_projection,
            **audit_result,
        }

    return _finish_lifecycle_attempt_cleanup(
        repo,
        with_sqlite_repo_transaction(repo, _run),
    )
