from __future__ import annotations

from domain.domain.ledger.position_fields import strategy_metadata_fields_from_payload

from .writer_common import (
    Any,
    LegacySettlementSemanticUnavailable,
    LifecycleAttemptAuditEnvelope,
    SETTLEMENT_SEMANTIC_SCHEMA,
    Sequence,
    SettlementSemanticUnavailable,
    _APPEND_SAFE_EVENT_TYPES,
    advance_lifecycle_case_decision_fact,
    build_initial_lifecycle_case_decision_fact,
    canonical_source_economic_payload,
    canonical_source_payload_hash,
    capture_current_decision_projection_fence,
    capture_trade_event_decision_projection_fence,
    defer_current_decision_projection,
    finalize_current_decision_projection,
    lifecycle_case_generation_token,
    read_lifecycle_case_decision_fact,
    resolve_lifecycle_account_rows,
    settlement_evidence_id,
    settlement_semantic_from_evidence,
    utc_now_ms,
    write_lifecycle_case_decision_fact,
)

from .writer_lifecycle_support import (
    _validate_existing_lifecycle_evidence,
)

def _projection_mode_for_events(
    events: Sequence[Any],
    *,
    force_full: bool = False,
) -> str:
    if force_full or any(
        str(getattr(item, "event_type", "") or "").strip().lower()
        not in _APPEND_SAFE_EVENT_TYPES
        for item in events
    ) or any(
        str(
            strategy_metadata_fields_from_payload(
                getattr(item, "raw_payload", None)
            ).get("strategy")
            or ""
        ).strip().lower()
        == "combo_yield"
        for item in events
    ):
        return "forced_full"
    return "fast_if_safe"

def _trade_events_by_id(
    repo: Any,
    event_ids: Sequence[str],
    *,
    conn: Any,
) -> dict[str, dict[str, Any]]:
    getter = getattr(repo, "get_trade_events_by_ids", None)
    if not callable(getter):
        raise TypeError("trade event persistence requires primary-key event lookup")
    return {
        str(item.get("event_id") or "").strip(): dict(item)
        for item in getter(event_ids, conn=conn)
        if isinstance(item, dict) and str(item.get("event_id") or "").strip()
    }

def _event_position_record_id(event: Any) -> str | None:
    payload = dict(getattr(event, "raw_payload", {}) or {})
    explicit = str(
        payload.get("record_id")
        or payload.get("target_lot_id")
        or getattr(event, "target_lot_id", None)
        or ""
    ).strip()
    if explicit:
        return explicit
    if str(getattr(event, "event_type", "") or "").strip().lower() != "open":
        return None
    return str(
        getattr(event, "lot_id", None)
        or f"lot_{str(getattr(event, 'event_id', '') or '').strip()}"
    ).strip() or None

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

def _begin_lifecycle_decision_projection(
    sqlite_repo: Any,
    *,
    conn: Any,
    lifecycle_case: dict[str, Any],
    allow_missing_fact: bool = False,
    global_event_owner: bool = False,
) -> tuple[Any, dict[str, Any] | None]:
    account = str(lifecycle_case.get("account") or "").strip().lower()
    fence = (
        capture_trade_event_decision_projection_fence(
            sqlite_repo,
            conn=conn,
            account=account,
        )
        if global_event_owner
        else capture_current_decision_projection_fence(
            sqlite_repo,
            accounts=(account,),
            conn=conn,
        )
    )
    begin = next(
        (item for item in fence.accounts if item.account == account),
        None,
    )
    if begin is None:
        raise ValueError("lifecycle account is outside decision projection fence")
    prior = (
        read_lifecycle_case_decision_fact(
            sqlite_repo,
            case_id=str(lifecycle_case.get("case_id") or ""),
            conn=conn,
        )
        if begin.projection_present and begin.clean_at_start
        else None
    )
    if (
        begin.projection_present
        and begin.clean_at_start
        and prior is None
        and not allow_missing_fact
    ):
        raise ValueError("clean current decision projection is missing lifecycle fact")
    return fence, prior

def _finish_lifecycle_decision_projection(
    sqlite_repo: Any,
    *,
    conn: Any,
    fence: Any,
    prior_fact: dict[str, Any] | None,
    case_id: str,
    publish_case: bool = True,
    resolution: dict[str, Any] | None = None,
    timing: dict[str, Any] | None = None,
    trade_event_mutations: Sequence[tuple[Any, bool]] = (),
) -> dict[str, Any]:
    lifecycle_case = sqlite_repo.get_trade_lifecycle_case(case_id, conn=conn)
    if lifecycle_case is None:
        raise ValueError("current decision lifecycle fact source disappeared")
    account = str(lifecycle_case.get("account") or "").strip().lower()
    begin = next(
        (item for item in fence.accounts if item.account == account),
        None,
    )
    if begin is None:
        raise ValueError("lifecycle account is outside decision projection fence")
    if (
        not publish_case
        or not begin.projection_present
        or not begin.clean_at_start
    ):
        return finalize_current_decision_projection(
            sqlite_repo,
            fence=fence,
            updated_at_ms=int(utc_now_ms()),
            conn=conn,
            trade_event_mutations=trade_event_mutations,
        )
    fact_state = sqlite_repo.get_current_decision_lifecycle_fact_state(
        case_id,
        conn=conn,
    )
    if fact_state is None:
        raise ValueError("current decision lifecycle fact source disappeared")
    final_fact = (
        advance_lifecycle_case_decision_fact(
            prior_fact,
            lifecycle_case=lifecycle_case,
            fact_state=fact_state,
            resolution=resolution,
            timing=timing,
        )
        if prior_fact is not None
        else build_initial_lifecycle_case_decision_fact(
            lifecycle_case=lifecycle_case,
            fact_state=fact_state,
            resolution=resolution,
            timing=timing,
        )
    )
    write_lifecycle_case_decision_fact(
        sqlite_repo,
        fact=final_fact,
        conn=conn,
    )
    return finalize_current_decision_projection(
        sqlite_repo,
        fence=fence,
        updated_at_ms=int(utc_now_ms()),
        conn=conn,
        case_mutations_by_account={account: ((prior_fact, final_fact),)},
        trade_event_mutations=trade_event_mutations,
    )

def _defer_lifecycle_decision_projection(fence: Any) -> dict[str, Any]:
    result = defer_current_decision_projection(fence)
    if result is None:
        raise ValueError("decision projection fence is missing")
    return result

def _finish_trade_event_decision_projection(
    sqlite_repo: Any,
    *,
    conn: Any,
    fence: Any,
    events: Sequence[Any],
    created_flags: Sequence[bool],
) -> dict[str, Any] | None:
    if fence is None:
        return None
    mutations = tuple(zip(events, created_flags, strict=True))
    if not any(created for _event, created in mutations):
        return defer_current_decision_projection(fence, reason="not_required")
    if any(
        created
        and str(getattr(event, "event_type", "") or "").strip().lower()
        == "void"
        for event, created in mutations
    ):
        return defer_current_decision_projection(fence)
    return finalize_current_decision_projection(
        sqlite_repo,
        fence=fence,
        updated_at_ms=int(utc_now_ms()),
        conn=conn,
        trade_event_mutations=mutations,
    )

def _lifecycle_resolution_after_allocations(
    prior_fact: dict[str, Any] | None,
    *,
    allocations: Sequence[dict[str, Any]],
    created_flags: Sequence[bool],
) -> dict[str, Any] | None:
    if prior_fact is None:
        return None
    prior = dict(prior_fact["resolution"])
    resolved = dict(prior["resolved_contracts_by_lot"])
    remaining = dict(prior["remaining_contracts_by_lot"])
    terminal = dict(prior["resolved_contracts_by_terminal_type"])
    requested = dict(prior["requested_reservations_by_lot"])
    effective = dict(prior["effective_reservations_by_lot"])
    for allocation, created in zip(allocations, created_flags, strict=True):
        if not created:
            continue
        lot_id = str(allocation.get("target_lot_id") or "").strip()
        contracts = int(allocation.get("contracts_allocated") or 0)
        terminal_type = str(allocation.get("terminal_type") or "").strip().lower()
        if (
            lot_id not in resolved
            or lot_id not in remaining
            or not terminal_type
            or contracts <= 0
            or contracts > int(remaining[lot_id])
        ):
            raise ValueError("lifecycle allocation exceeds compact remaining quantity")
        resolved[lot_id] = int(resolved[lot_id]) + contracts
        remaining[lot_id] = int(remaining[lot_id]) - contracts
        terminal[terminal_type] = int(terminal.get(terminal_type, 0)) + contracts
        if lot_id not in requested:
            continue
        if (
            lot_id not in effective
            or contracts > int(requested[lot_id])
            or contracts > int(effective[lot_id])
        ):
            raise ValueError("lifecycle allocation exceeds compact reservation")
        for reservations in (requested, effective):
            reservation_remaining = int(reservations[lot_id]) - contracts
            if reservation_remaining:
                reservations[lot_id] = reservation_remaining
            else:
                del reservations[lot_id]
    return {
        "resolved_contracts_by_lot": resolved,
        "remaining_contracts_by_lot": remaining,
        "resolved_contracts_by_terminal_type": terminal,
        "requested_reservations_by_lot": requested,
        "effective_reservations_by_lot": effective,
    }

def _prepare_settlement_admission(
    sqlite_repo: Any,
    *,
    conn: Any,
    case_id: str,
    evidence: dict[str, Any],
    expected_generation_token: str | None,
) -> dict[str, Any] | None:
    if (
        str(evidence.get("source_type") or "").strip().lower()
        != "broker_settlement_observation"
    ):
        return None
    if not isinstance(evidence.get("observation"), dict):
        # Historical/manual terminal evidence reused this source label before
        # the collector observation envelope existed.  It is not eligible for
        # semantic admission because there is no frozen observation to compare.
        return None
    expected = str(expected_generation_token or "").strip()
    if not expected:
        raise ValueError(
            "settlement admission requires lifecycle generation token"
        )
    try:
        semantic, fingerprint = settlement_semantic_from_evidence(
            evidence
        )
    except SettlementSemanticUnavailable:
        raise
    except Exception as exc:
        raise SettlementSemanticUnavailable(
            "settlement semantic projection failed"
        ) from exc

    latest = (
        sqlite_repo.get_latest_trade_lifecycle_settlement_evidence(
            case_id=case_id,
            conn=conn,
        )
    )
    head = sqlite_repo.get_trade_lifecycle_settlement_admission_head(
        case_id=case_id,
        conn=conn,
    )
    head_repaired = False
    latest_id = str((latest or {}).get("evidence_id") or "").strip()
    if latest is not None and (
        head is None
        or str(head.get("evidence_id") or "").strip() != latest_id
    ):
        try:
            _latest_semantic, latest_fingerprint = (
                settlement_semantic_from_evidence(latest)
            )
        except SettlementSemanticUnavailable as exc:
            raise LegacySettlementSemanticUnavailable(
                "legacy_semantic_unavailable"
            ) from exc
        sqlite_repo.upsert_trade_lifecycle_settlement_admission_head(
            case_id=case_id,
            semantic_schema=SETTLEMENT_SEMANTIC_SCHEMA,
            semantic_fingerprint=latest_fingerprint,
            evidence_id=latest_id,
            evidence_created_at_ms=int(
                latest.get("_created_at_ms") or 0
            ),
            updated_at_ms=int(utc_now_ms()),
            conn=conn,
        )
        head_repaired = True
        head = (
            sqlite_repo.get_trade_lifecycle_settlement_admission_head(
                case_id=case_id,
                conn=conn,
            )
        )
    elif latest is None and head is not None:
        raise SettlementSemanticUnavailable(
            "settlement admission head has no evidence"
        )

    if (
        head is not None
        and str(head.get("semantic_schema") or "").strip()
        == SETTLEMENT_SEMANTIC_SCHEMA
        and str(head.get("semantic_fingerprint") or "").strip()
        == fingerprint
    ):
        return {
            "duplicate": True,
            "semantic": semantic,
            "semantic_fingerprint": fingerprint,
            "evidence_id": str(head.get("evidence_id") or "").strip(),
            "previous_evidence_id": latest_id or None,
            "head_repaired": head_repaired,
        }

    expected_evidence_id = settlement_evidence_id(
        case_id=case_id,
        semantic_fingerprint=fingerprint,
        expected_generation_token=expected,
        previous_evidence_id=latest_id or None,
    )
    incoming_evidence_id = str(
        evidence.get("evidence_id") or ""
    ).strip()
    if incoming_evidence_id != expected_evidence_id:
        raise ValueError(
            "settlement evidence id does not match semantic admission"
        )
    if (
        str(evidence.get("semantic_schema") or "").strip()
        != SETTLEMENT_SEMANTIC_SCHEMA
        or str(evidence.get("semantic_fingerprint") or "").strip()
        != fingerprint
    ):
        raise ValueError("settlement evidence semantic metadata mismatch")
    return {
        "duplicate": False,
        "semantic": semantic,
        "semantic_fingerprint": fingerprint,
        "evidence_id": incoming_evidence_id,
        "previous_evidence_id": latest_id or None,
        "head_repaired": head_repaired,
    }

def _advance_settlement_admission_head(
    sqlite_repo: Any,
    *,
    conn: Any,
    case_id: str,
    admission: dict[str, Any] | None,
) -> None:
    if admission is None or bool(admission.get("duplicate")):
        return
    latest = sqlite_repo.get_latest_trade_lifecycle_settlement_evidence(
        case_id=case_id,
        conn=conn,
    )
    evidence_id = str(admission.get("evidence_id") or "").strip()
    if (
        latest is None
        or str(latest.get("evidence_id") or "").strip()
        != evidence_id
    ):
        raise ValueError(
            "settlement admission evidence is not the latest case row"
        )
    sqlite_repo.upsert_trade_lifecycle_settlement_admission_head(
        case_id=case_id,
        semantic_schema=SETTLEMENT_SEMANTIC_SCHEMA,
        semantic_fingerprint=str(
            admission.get("semantic_fingerprint") or ""
        ),
        evidence_id=evidence_id,
        evidence_created_at_ms=int(latest.get("_created_at_ms") or 0),
        updated_at_ms=int(utc_now_ms()),
        conn=conn,
    )

def _persist_settlement_admission_evidence(
    sqlite_repo: Any,
    *,
    conn: Any,
    case_id: str,
    evidence: dict[str, Any],
    admission: dict[str, Any] | None,
) -> tuple[bool, bool]:
    if admission is None or bool(admission.get("duplicate")):
        return False, False
    evidence_id = str(admission.get("evidence_id") or "").strip()
    if str(evidence.get("evidence_id") or "").strip() != evidence_id:
        raise ValueError("settlement admission evidence identity mismatch")
    if evidence.get("case_id") not in (None, "", case_id):
        raise ValueError("lifecycle evidence is bound to another case")
    existing = sqlite_repo.get_trade_lifecycle_evidence(
        evidence_id,
        conn=conn,
    )
    if existing is None:
        created = sqlite_repo.insert_trade_lifecycle_evidence_once(
            evidence,
            conn=conn,
        )
    else:
        _validate_existing_lifecycle_evidence(
            existing=existing,
            incoming=evidence,
            case_id=case_id,
        )
        created = False
    bound = sqlite_repo.bind_trade_lifecycle_evidence_case_once(
        evidence_id=evidence_id,
        case_id=case_id,
        conn=conn,
    )
    return bool(created), bool(bound)

def _persist_direct_stock_settlement_evidence(
    sqlite_repo: Any,
    *,
    conn: Any,
    evidence: dict[str, Any],
) -> bool:
    evidence_id = str(evidence.get("evidence_id") or "").strip()
    source_key = str(evidence.get("source_event_id") or "").strip()
    if (
        not evidence_id
        or not source_key
        or str(evidence.get("evidence_type") or "").strip().lower()
        != "stock_settlement_leg"
    ):
        raise ValueError("direct stock settlement evidence is invalid")
    incoming = canonical_source_economic_payload(
        source_key=source_key,
        source_role="stock_settlement",
        payload=evidence,
    )
    existing = sqlite_repo.get_trade_lifecycle_evidence(
        evidence_id,
        conn=conn,
    )
    if existing is not None:
        stored = canonical_source_economic_payload(
            source_key=str(existing.get("source_event_id") or ""),
            source_role="stock_settlement",
            payload=existing,
        )
        if canonical_source_payload_hash(stored) != canonical_source_payload_hash(
            incoming
        ):
            raise ValueError("lifecycle evidence economic payload conflict")
        return False
    return bool(
        sqlite_repo.insert_trade_lifecycle_evidence_once(
            evidence,
            conn=conn,
        )
    )

def _match_lifecycle_attempt_replay(
    sqlite_repo: Any,
    *,
    conn: Any,
    case_id: str,
    attempt_audit: LifecycleAttemptAuditEnvelope | None,
) -> dict[str, Any] | None:
    if attempt_audit is None:
        return None
    if attempt_audit.case_id != case_id:
        raise ValueError("lifecycle attempt audit case mismatch")
    replay = sqlite_repo.match_trade_lifecycle_attempt_audit_invocation(
        attempt_audit,
        conn=conn,
    )
    if replay is not None:
        return {
            "case_id": case_id,
            "admission_status": "duplicate_invocation",
            **replay,
        }
    if attempt_audit.outcome_code not in (1, 2):
        raise ValueError(
            "lifecycle evidence writer requires an observed attempt audit"
        )
    return None

def _append_lifecycle_observation_attempt(
    sqlite_repo: Any,
    *,
    conn: Any,
    attempt_audit: LifecycleAttemptAuditEnvelope | None,
    admission: dict[str, Any] | None,
) -> dict[str, Any]:
    if attempt_audit is None:
        return {}
    if admission is None:
        raise ValueError(
            "lifecycle attempt audit requires semantic observation admission"
        )
    return sqlite_repo.append_trade_lifecycle_attempt_audit_in_transaction(
        attempt_audit=attempt_audit,
        first_evidence_id=str(admission.get("evidence_id") or "").strip(),
        conn=conn,
    )

def _finish_lifecycle_attempt_cleanup(
    repo: Any,
    result: dict[str, Any],
) -> dict[str, Any]:
    cleanup_hash = result.pop("_cleanup_receipt_sha256", None)
    if cleanup_hash is None:
        return result
    sqlite_repo = getattr(repo, "primary_repo", repo)
    try:
        sqlite_repo.delete_unreferenced_trade_lifecycle_receipt_blob(
            cleanup_hash
        )
    except Exception as exc:
        result["cleanup_warning"] = {
            "code": "receipt_blob_cleanup_failed",
            "receipt_sha256": cleanup_hash.hex(),
            "error_class": type(exc).__name__[:128],
        }
    return result
