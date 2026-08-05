from __future__ import annotations

from typing import Any, Callable, Iterable, Mapping

from domain.domain.lifecycle_allocation import resolve_allocations
from domain.domain.option_close_reason import (
    CloseReasonEvidenceBundle,
    CloseReasonTarget,
    EffectiveLifecycleTiming,
    resolve_close_reason,
)
from domain.domain.option_lifecycle import LIFECYCLE_CASE_SCHEMA
from src.application.ledger.api import (
    advance_lifecycle_case_state,
    lifecycle_case_coherent_facts,
    record_lifecycle_evidence_issue,
)
from src.application.trades.close_reason_evidence import (
    canonical_hash,
    derive_effective_lifecycle_timing,
)
from src.application.trades.lifecycle_reconciliation import (
    LifecycleCaseDataError,
    lifecycle_case_read_model,
    reconcile_lifecycle_evidence,
)
from src.application.trades.lifecycle import (
    reconcile_polled_stock_settlement_evidence,
)
from src.application.trades.settlement_observation import (
    LifecycleObservationGenerationChanged,
    SettlementObservationDataError,
)


def reconcile_lifecycle_close_reason(
    repo: Any,
    *,
    case_id: str,
    now_ms: int,
    observation: dict[str, Any] | None = None,
    apply_changes: bool = False,
    coherent_facts: Mapping[str, Any] | None = None,
    refresh_read_model: bool = True,
) -> dict[str, Any]:
    observation_payload = dict(observation or {})
    facts = (
        dict(coherent_facts)
        if isinstance(coherent_facts, Mapping)
        else lifecycle_case_coherent_facts(repo, case_id=case_id)
    )
    lifecycle_case = dict(facts["lifecycle_case"])
    if str(lifecycle_case.get("case_id") or "").strip() != str(
        case_id or ""
    ).strip():
        raise LifecycleObservationGenerationChanged(
            "prepared lifecycle facts belong to another case"
        )
    prepared_generation_token = str(
        observation_payload.get(
            "expected_lifecycle_generation_token"
        )
        or ""
    ).strip()
    current_generation_token = str(
        facts["generation_token"].get("generation_token") or ""
    ).strip()
    if (
        prepared_generation_token
        and prepared_generation_token != current_generation_token
    ):
        raise LifecycleObservationGenerationChanged(
            "lifecycle generation changed after provider collection"
        )
    expected_generation_token = (
        prepared_generation_token or current_generation_token
    )
    stock_candidates, stock_candidate_reasons = (
        _canonical_stock_settlement_candidates(
            observation_payload.get("stock_settlement_candidates")
            or []
        )
    )
    if stock_candidate_reasons:
        return {
            "schema_version": (
                "close_reason_reconciliation_result.v1"
            ),
            "case_id": case_id,
            "apply_changes": bool(apply_changes),
            "decision": {
                "status": "needs_review",
                "close_reason": None,
                "contracts_resolved": 0,
                "evidence_ids": sorted(
                    {
                        str(item.get("evidence_id") or "").strip()
                        for item in stock_candidates
                        if str(item.get("evidence_id") or "").strip()
                    }
                ),
                "reason_codes": list(stock_candidate_reasons),
                "public_transition": None,
            },
            "timing": None,
            "timing_error": None,
            "observation_id": str(
                observation_payload.get("observation_id") or ""
            ).strip()
            or None,
            "lifecycle_generation_token": current_generation_token,
            "poll_settlement_results": [],
            "write_status": "not_attempted",
            "lifecycle_read_model": _refreshed_lifecycle_read_model(
                repo,
                case_id=case_id,
                now_ms=now_ms,
                refresh=refresh_read_model,
            ),
        }
    poll_results: list[dict[str, Any]] = []
    for candidate in stock_candidates:
        resolution = (
            reconcile_polled_stock_settlement_evidence(
                repo,
                evidence=dict(candidate),
                apply_changes=apply_changes,
                expected_lifecycle_generation_token=(
                    expected_generation_token
                ),
            )
        )
        poll_results.append(
            {
                "status": resolution.status,
                "action": resolution.action,
                "reason": resolution.reason,
                "diagnostics": dict(resolution.diagnostics),
            }
        )
    if poll_results and any(
        item["status"] in {"applied", "skipped", "dry_run"}
        for item in poll_results
    ):
        return {
            "schema_version": (
                "close_reason_reconciliation_result.v1"
            ),
            "case_id": case_id,
            "apply_changes": bool(apply_changes),
            "poll_settlement_results": poll_results,
            "lifecycle_read_model": _refreshed_lifecycle_read_model(
                repo,
                case_id=case_id,
                now_ms=now_ms,
                refresh=refresh_read_model,
            ),
        }
    case_resolution = dict(facts["case_resolution"])
    if case_resolution.get("status") == "conflict":
        return {
            "schema_version": ("close_reason_reconciliation_result.v1"),
            "case_id": case_id,
            "apply_changes": bool(apply_changes),
            "decision": {
                "status": "needs_review",
                "close_reason": None,
                "contracts_resolved": 0,
                "evidence_ids": [],
                "reason_codes": list(
                    case_resolution.get("reason_codes") or []
                ),
                "public_transition": None,
            },
            "timing": None,
            "timing_error": None,
            "observation_id": None,
            "poll_settlement_results": poll_results,
        }
    evidence_rows = [
        dict(item) for item in facts["validated_anchors"]
    ]
    allocations = [
        dict(item)
        for item in facts["case_allocations"]
        if isinstance(item, dict)
    ]
    void_event_ids = tuple(
        facts.get("effective_void_event_ids") or ()
    )
    option_rows = [
        item
        for item in evidence_rows
        if str(item.get("evidence_type") or "").strip().lower()
        == "option_zero_price_close"
    ]
    option_rows.sort(
        key=lambda item: (
            int(item.get("received_at_ms") or 0),
            str(item.get("evidence_id") or ""),
        )
    )
    option_anchor = option_rows[0] if option_rows else {}
    timing_policy = facts.get("timing_policy")
    effective_timing_payload: dict[str, Any] | None = None
    timing: EffectiveLifecycleTiming | None = None
    timing_error: str | None = None
    if isinstance(timing_policy, dict):
        try:
            effective_timing_payload = (
                derive_effective_lifecycle_timing(
                    policy=timing_policy,
                    option_close_evidence=option_rows,
                )
            )
            timing = EffectiveLifecycleTiming(
                pairing_until_ms=int(
                    effective_timing_payload["pairing_until_ms"]
                ),
                settlement_deadline_ms=int(
                    effective_timing_payload[
                        "settlement_deadline_ms"
                    ]
                ),
                last_trade_cutoff_ms=int(
                    effective_timing_payload[
                        "last_trade_cutoff_ms"
                    ]
                ),
                settlement_style=str(
                    effective_timing_payload["settlement_style"]
                ),
            )
        except (TypeError, ValueError, OverflowError) as exc:
            timing_error = str(exc)

    target_manifest = {
        str(key): int(value)
        for key, value in dict(
            lifecycle_case.get("target_contracts_by_lot") or {}
        ).items()
    }
    resolution = resolve_allocations(
        target_manifest,
        allocations,
        void_event_ids=void_event_ids,
    )
    stock_contracts = sum(
        int(value)
        for terminal_type, value in (
            resolution.resolved_contracts_by_terminal_type.items()
        )
        if terminal_type in {"assignment", "exercise"}
    )
    target_total = sum(target_manifest.values())
    if stock_contracts <= 0:
        stock_status = "none"
    elif stock_contracts < target_total:
        stock_status = "partial"
    elif stock_contracts == target_total:
        stock_status = "full"
    else:
        stock_status = "conflict"
    option_price = (
        option_anchor.get("price")
        if option_anchor
        else observation_payload.get("option_close_price")
    )
    evidence_ids = {
        str(item.get("evidence_id") or "").strip()
        for item in evidence_rows
        if str(item.get("evidence_id") or "").strip()
    }
    observation_id = str(
        observation_payload.get("observation_id") or ""
    ).strip()
    if observation_id:
        evidence_ids.add(observation_id)
    target = CloseReasonTarget(
        account=str(lifecycle_case.get("account") or ""),
        futu_account_id=str(
            option_anchor.get("futu_account_id")
            or observation_payload.get("futu_account_id")
            or ""
        ),
        position_side=str(
            lifecycle_case.get("position_side") or ""
        ),
        option_type=str(lifecycle_case.get("option_type") or ""),
        expiration_ymd=str(
            lifecycle_case.get("expiration_ymd") or ""
        ),
        target_contracts_by_lot=target_manifest,
        frozen_preterminal_remaining_by_lot=target_manifest,
        reservation_exclusive=bool(
            observation_payload.get("reservation_exclusive", True)
        ),
        competing_effective_consumption=bool(
            observation_payload.get(
                "competing_effective_consumption",
                False,
            )
        ),
    )
    evidence_bundle = CloseReasonEvidenceBundle(
        evidence_ids=tuple(sorted(evidence_ids)),
        option_close_present=bool(option_anchor),
        option_close_price=option_price,
        option_execution_time_ms=(
            int(
                option_anchor.get("event_time_ms")
                or option_anchor.get("trade_time_ms")
                or 0
            )
            or None
        ),
        option_execution_local_ymd=str(
            observation_payload.get(
                "option_execution_local_ymd"
            )
            or ""
        )
        or None,
        exact_normal_order=bool(
            observation_payload.get("normal_order_present")
        ),
        exact_normal_close_deal=bool(
            observation_payload.get("normal_close_deal_present")
        ),
        stock_match_status=stock_status,
        stock_contracts=stock_contracts,
        proposed_allocations=tuple(
            dict(item)
            for item in list(
                observation_payload.get("proposed_allocations") or []
            )
            if isinstance(item, dict)
        ),
        mutually_exclusive_terminal_facts=bool(
            observation_payload.get(
                "mutually_exclusive_terminal_facts"
            )
        ),
        duplicate_source_consumption=bool(
            observation_payload.get("duplicate_source_consumption")
        ),
        over_allocation=resolution.status != "ok",
        projection_drift=not bool(
            observation_payload.get(
                "projection_matches_frozen_remaining",
                True,
            )
        ),
        observation_complete=bool(
            observation_payload.get("complete")
        ),
        broker_option_position_absent=bool(
            observation_payload.get("broker_option_position_absent")
        ),
        projection_matches_frozen_remaining=bool(
            observation_payload.get(
                "projection_matches_frozen_remaining"
            )
        ),
        no_stock_settlement=not bool(
            observation_payload.get("stock_settlement_present")
        ),
        no_normal_order=not bool(
            observation_payload.get("normal_order_present")
        ),
    )
    decision = resolve_close_reason(
        target,
        evidence_bundle,
        timing,
        int(now_ms),
    )
    preview = {
        "schema_version": "close_reason_reconciliation_result.v1",
        "case_id": case_id,
        "apply_changes": bool(apply_changes),
        "decision": {
            "status": decision.status,
            "close_reason": decision.close_reason,
            "contracts_resolved": decision.contracts_resolved,
            "evidence_ids": list(decision.evidence_ids),
            "reason_codes": list(decision.reason_codes),
            "public_transition": decision.public_transition,
        },
        "timing": effective_timing_payload,
        "timing_error": timing_error,
        "observation_id": observation_id or None,
        "lifecycle_generation_token": current_generation_token,
        "poll_settlement_results": poll_results,
    }
    if not apply_changes or decision.status == "not_started":
        return preview
    if (
        decision.status == "resolved"
        and decision.close_reason == "expiration_no_settlement"
    ):
        if not observation_id:
            raise ValueError(
                "complete settlement observation identity is required"
            )
        terminal_evidence = {
            "evidence_id": observation_id,
            "case_id": case_id,
            "source_type": "broker_settlement_observation",
            "source_event_id": observation_id,
            "evidence_type": "expire_close",
            "terminal_type": "expire_close",
            "account": lifecycle_case.get("account"),
            "symbol": lifecycle_case.get("symbol"),
            "option_type": lifecycle_case.get("option_type"),
            "position_side": lifecycle_case.get("position_side"),
            "strike": lifecycle_case.get("strike"),
            "expiration_ymd": lifecycle_case.get("expiration_ymd"),
            "contracts": sum(
                resolution.remaining_contracts_by_lot.values()
            ),
            "event_time_ms": int(
                observation_payload.get("observed_at_ms") or now_ms
            ),
            "currency": lifecycle_case.get("currency"),
            "observation_hash": str(
                observation_payload.get("semantic_fingerprint")
                or canonical_hash(observation_payload)
            ),
            "semantic_schema": observation_payload.get(
                "semantic_schema"
            ),
            "semantic_fingerprint": observation_payload.get(
                "semantic_fingerprint"
            ),
            "semantic_projection": observation_payload.get(
                "semantic_projection"
            ),
            "previous_settlement_evidence_id": (
                observation_payload.get(
                    "previous_settlement_evidence_id"
                )
            ),
            "observation": observation_payload,
        }
        result = reconcile_lifecycle_evidence(
            repo,
            evidence=terminal_evidence,
            case_id=case_id,
            apply_changes=True,
            now_ms=now_ms,
            expected_lifecycle_generation_token=(
                expected_generation_token
            ),
            refresh_read_model=refresh_read_model,
        )
        return {**preview, "write_result": result.to_dict()}
    summary = {
        "reason_state": decision.status,
        "close_reason": decision.close_reason,
        "lifecycle_reason_codes": list(decision.reason_codes),
        "pairing_until_ms": (
            timing.pairing_until_ms if timing is not None else None
        ),
        "settlement_deadline_ms": (
            timing.settlement_deadline_ms
            if timing is not None
            else None
        ),
        "timing_policy_hash": (
            effective_timing_payload.get("timing_policy_hash")
            if effective_timing_payload is not None
            else (
                canonical_hash(timing_policy)
                if isinstance(timing_policy, dict)
                else None
            )
        ),
        "observation_hash": (
            str(
                observation_payload.get("semantic_fingerprint")
                or canonical_hash(observation_payload)
            )
            if observation_payload
            else None
        ),
    }
    if (
        decision.status in {"needs_review", "conflict"}
        and observation_id
    ):
        issue_evidence = {
            "evidence_id": observation_id,
            "case_id": case_id,
            "source_type": "broker_settlement_observation",
            "source_event_id": observation_id,
            "evidence_type": "settlement_observation",
            "account": lifecycle_case.get("account"),
            "symbol": lifecycle_case.get("symbol"),
            "contracts": sum(
                resolution.remaining_contracts_by_lot.values()
            ),
            "observation_hash": str(
                observation_payload.get("semantic_fingerprint")
                or canonical_hash(observation_payload)
            ),
            "semantic_schema": observation_payload.get(
                "semantic_schema"
            ),
            "semantic_fingerprint": observation_payload.get(
                "semantic_fingerprint"
            ),
            "semantic_projection": observation_payload.get(
                "semantic_projection"
            ),
            "previous_settlement_evidence_id": (
                observation_payload.get(
                    "previous_settlement_evidence_id"
                )
            ),
            "observation": observation_payload,
        }
        write_result = record_lifecycle_evidence_issue(
            repo,
            case_id=case_id,
            evidence=issue_evidence,
            status=decision.status,
            reason_codes=list(decision.reason_codes),
            expected_lifecycle_generation_token=(
                expected_generation_token
            ),
        )
    else:
        if (
            decision.status == "resolved"
            and decision.close_reason
            in {"assignment", "exercise"}
            and resolution.remaining_contracts > 0
        ):
            raise ValueError(
                "resolved lifecycle cause requires terminal allocations"
            )
        persisted_status = {
            "cause_pending": "waiting_settlement_evidence",
            "partially_resolved": "partially_resolved",
            "needs_review": "needs_review",
            "conflict": "conflict",
            "resolved": "ledger_written",
        }.get(decision.status, decision.status)
        write_result = advance_lifecycle_case_state(
            repo,
            case_id=case_id,
            status=persisted_status,
            derived_summary=summary,
            public_transition=decision.public_transition,
            expected_lifecycle_generation_token=(
                expected_generation_token
            ),
        )
    return {
        **preview,
        "write_result": write_result,
        "lifecycle_read_model": _refreshed_lifecycle_read_model(
            repo,
            case_id=case_id,
            now_ms=now_ms,
            refresh=refresh_read_model,
        ),
    }


def _refreshed_lifecycle_read_model(
    repo: Any,
    *,
    case_id: str,
    now_ms: int,
    refresh: bool,
) -> dict[str, Any] | None:
    if not refresh:
        return None
    return lifecycle_case_read_model(
        repo,
        case_id=case_id,
        now_ms=now_ms,
    )


def _reconcile_deadline_without_effective_pairing(
    repo: Any,
    *,
    lifecycle_case: dict[str, Any],
    read_model: dict[str, Any],
    now_ms: int,
    apply_changes: bool,
) -> dict[str, Any] | None:
    case_id = str(lifecycle_case.get("case_id") or "").strip()
    evidence_status = str(
        read_model.get("lifecycle_evidence_status") or ""
    ).strip().lower()
    if evidence_status != "missing":
        return reconcile_lifecycle_close_reason(
            repo,
            case_id=case_id,
            now_ms=int(now_ms),
            apply_changes=apply_changes,
        )
    if str(read_model.get("reason_state") or "").strip().lower() != "needs_review":
        return None

    reason_codes = sorted(
        {
            str(item).strip()
            for item in read_model.get("lifecycle_reason_codes") or []
            if str(item or "").strip()
        }
    )
    close_reason = str(read_model.get("close_reason") or "").strip() or None
    preview = {
        "schema_version": "close_reason_reconciliation_result.v1",
        "case_id": case_id,
        "apply_changes": bool(apply_changes),
        "decision": {
            "status": "needs_review",
            "close_reason": close_reason,
            "contracts_resolved": sum(
                int(value or 0)
                for value in dict(
                    read_model.get("resolved_contracts_by_lot") or {}
                ).values()
            ),
            "evidence_ids": [],
            "reason_codes": reason_codes,
            "public_transition": None,
        },
        "timing": None,
        "timing_error": read_model.get("timing_error"),
        "observation_id": None,
        "lifecycle_generation_token": str(
            read_model.get("lifecycle_generation_token") or ""
        ),
        "poll_settlement_results": [],
        "lifecycle_read_model": dict(read_model),
    }
    if not apply_changes:
        return preview

    write_result = advance_lifecycle_case_state(
        repo,
        case_id=case_id,
        status="needs_review",
        derived_summary={
            "reason_state": "needs_review",
            "close_reason": close_reason,
            "lifecycle_reason_codes": reason_codes,
            "pairing_until_ms": read_model.get("pairing_until_ms"),
            "settlement_deadline_ms": read_model.get("pending_until_ms"),
            "timing_policy_hash": read_model.get("timing_policy_hash"),
            "observation_hash": None,
        },
        public_transition=None,
        expected_lifecycle_generation_token=str(
            read_model.get("lifecycle_generation_token") or ""
        ),
    )
    return {
        **preview,
        "write_result": write_result,
        "lifecycle_read_model": lifecycle_case_read_model(
            repo,
            case_id=case_id,
            now_ms=now_ms,
        ),
    }


def reconcile_due_lifecycle_cases(
    repo: Any,
    *,
    account: str,
    now_ms: int,
    apply_changes: bool = False,
    observation_collector: (
        Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]
        | None
    ) = None,
    case_ids: Iterable[str] | None = None,
    prepared_read_models: (
        Mapping[str, dict[str, Any]] | None
    ) = None,
) -> dict[str, Any]:
    account_value = str(account or "").strip().lower()
    if not account_value:
        raise ValueError("due reconciliation account is required")
    selected_case_ids = (
        {
            str(item or "").strip()
            for item in case_ids
            if str(item or "").strip()
        }
        if case_ids is not None
        else None
    )
    prepared = dict(prepared_read_models or {})
    results: list[dict[str, Any]] = []
    for lifecycle_case in repo.list_trade_lifecycle_cases(
        account=account_value
    ):
        case_id = str(lifecycle_case.get("case_id") or "").strip()
        if (
            not case_id
            or (
                selected_case_ids is not None
                and case_id not in selected_case_ids
            )
            or str(lifecycle_case.get("schema_version") or "").strip() != LIFECYCLE_CASE_SCHEMA
            or str(lifecycle_case.get("status") or "").strip().lower() == "superseded"
        ):
            continue
        target_manifest = lifecycle_case.get("target_contracts_by_lot")
        if not isinstance(target_manifest, dict) or not target_manifest:
            results.append(
                _case_data_failure(
                    case_id=case_id,
                    reason_code="lifecycle_target_manifest_empty",
                    stage="case_validation",
                    field="target_contracts_by_lot",
                    apply_changes=apply_changes,
                )
            )
            continue
        try:
            read_model = (
                dict(prepared[case_id])
                if isinstance(prepared.get(case_id), dict)
                else lifecycle_case_read_model(
                    repo,
                    case_id=case_id,
                    now_ms=now_ms,
                )
            )
            pairing_until = read_model.get("pairing_until_ms")
            settlement_deadline = read_model.get("pending_until_ms")
            pairing_until_value = (
                int(pairing_until)
                if pairing_until is not None
                else None
            )
            settlement_deadline_value = (
                int(settlement_deadline)
                if settlement_deadline is not None
                else None
            )
        except LifecycleCaseDataError as exc:
            results.append(
                _case_data_failure(
                    case_id=case_id,
                    reason_code="lifecycle_read_model_invalid",
                    stage="read_model",
                    field=None,
                    apply_changes=apply_changes,
                    error=exc,
                )
            )
            continue
        reason_state = str(
            read_model.get("reason_state") or ""
        ).strip().lower()
        if pairing_until_value is None:
            if (
                settlement_deadline_value is None
                or int(now_ms) < settlement_deadline_value
                or reason_state
                not in {
                    "cause_pending",
                    "partially_resolved",
                    "needs_review",
                }
            ):
                continue
            try:
                result = _reconcile_deadline_without_effective_pairing(
                    repo,
                    lifecycle_case=dict(lifecycle_case),
                    read_model=dict(read_model),
                    now_ms=int(now_ms),
                    apply_changes=apply_changes,
                )
            except LifecycleCaseDataError as exc:
                results.append(
                    _case_data_failure(
                        case_id=case_id,
                        reason_code="lifecycle_close_reason_data_invalid",
                        stage="close_reason",
                        field=None,
                        apply_changes=apply_changes,
                        error=exc,
                    )
                )
                continue
            if result is not None:
                results.append(result)
            continue
        if (
            int(now_ms) < pairing_until_value
            or reason_state
            not in {"cause_pending", "partially_resolved"}
        ):
            continue
        observation: dict[str, Any] | None = None
        observation_required = (
            settlement_deadline_value is not None
            and int(now_ms) >= settlement_deadline_value
            and read_model.get("reason_state") != "resolved"
        )
        if observation_required and observation_collector is not None:
            try:
                observation = observation_collector(
                    dict(lifecycle_case),
                    dict(read_model),
                )
            except SettlementObservationDataError as exc:
                results.append(
                    _case_data_failure(
                        case_id=case_id,
                        reason_code=(
                            "settlement_observation_data_invalid"
                        ),
                        stage="provider_observation_input",
                        field=None,
                        apply_changes=apply_changes,
                        error=exc,
                    )
                )
                continue
        if observation_required and observation is None:
            results.append(
                {
                    "case_id": case_id,
                    "status": "observation_required",
                    "apply_changes": bool(apply_changes),
                }
            )
            continue
        try:
            result = reconcile_lifecycle_close_reason(
                repo,
                case_id=case_id,
                now_ms=now_ms,
                observation=observation,
                apply_changes=apply_changes,
            )
        except LifecycleCaseDataError as exc:
            results.append(
                _case_data_failure(
                    case_id=case_id,
                    reason_code="lifecycle_close_reason_data_invalid",
                    stage="close_reason",
                    field=None,
                    apply_changes=apply_changes,
                    error=exc,
                )
            )
            continue
        results.append(result)
    return {
        "schema_version": "due_lifecycle_reconciliation.v2",
        "account": account_value,
        "now_ms": int(now_ms),
        "apply_changes": bool(apply_changes),
        "case_count": len(results),
        "results": results,
    }


_STOCK_SETTLEMENT_CANDIDATE_FIELDS = (
    "evidence_id",
    "case_id",
    "observed_case_id",
    "source_type",
    "source_event_id",
    "evidence_type",
    "account",
    "futu_account_id",
    "symbol",
    "side",
    "stock_qty",
    "stock_price",
    "trade_time_ms",
    "order_id",
    "clearing_date",
)


def _canonical_stock_settlement_candidates(
    candidates: Any,
) -> tuple[list[dict[str, Any]], tuple[str, ...]]:
    """Dedupe one provider event and reject ambiguous batches before writes."""

    if not isinstance(candidates, list):
        return [], ("stock_settlement_candidates_invalid",)
    by_source: dict[str, tuple[str, dict[str, Any]]] = {}
    reasons: set[str] = set()
    for raw in candidates:
        if not isinstance(raw, Mapping):
            reasons.add("stock_settlement_candidate_invalid")
            continue
        candidate = dict(raw)
        source_event_id = str(
            candidate.get("source_event_id") or ""
        ).strip()
        evidence_id = str(
            candidate.get("evidence_id") or ""
        ).strip()
        if not source_event_id or not evidence_id:
            reasons.add("stock_settlement_candidate_identity_missing")
            continue
        semantic_payload = {
            field: candidate.get(field)
            for field in _STOCK_SETTLEMENT_CANDIDATE_FIELDS
        }
        semantic_hash = canonical_hash(semantic_payload)
        existing = by_source.get(source_event_id)
        if existing is None:
            by_source[source_event_id] = (
                semantic_hash,
                candidate,
            )
        elif existing[0] != semantic_hash:
            reasons.add("stock_settlement_source_conflict")
    normalized = [
        by_source[source_event_id][1]
        for source_event_id in sorted(by_source)
    ]
    if len(normalized) > 1:
        reasons.add("stock_settlement_multiple_candidates_unresolved")
    return normalized, tuple(sorted(reasons))


def _case_data_failure(
    *,
    case_id: str,
    reason_code: str,
    stage: str,
    field: str | None,
    apply_changes: bool,
    error: Exception | None = None,
) -> dict[str, Any]:
    result = {
        "case_id": case_id,
        "status": "needs_review",
        "reason_codes": [reason_code],
        "failure_class": "case_data",
        "failure_stage": stage,
        "failure_field": field,
        "write_status": "not_attempted",
        "apply_changes": bool(apply_changes),
    }
    if error is not None:
        result["error"] = f"{type(error).__name__}: {error}"
    return result


__all__ = [
    "reconcile_due_lifecycle_cases",
    "reconcile_lifecycle_close_reason",
]
