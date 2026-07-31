from __future__ import annotations

from typing import Any, Callable

from domain.domain.lifecycle_allocation import resolve_allocations
from domain.domain.option_close_reason import (
    CloseReasonEvidenceBundle,
    CloseReasonTarget,
    EffectiveLifecycleTiming,
    resolve_close_reason,
)
from src.application.ledger.api import (
    advance_lifecycle_case_state,
    lifecycle_evidence_facts,
    lifecycle_reconciliation_facts,
    record_lifecycle_evidence_issue,
)
from src.application.trades.close_reason_evidence import (
    canonical_hash,
    derive_effective_lifecycle_timing,
)
from src.application.trades.lifecycle_reconciliation import (
    lifecycle_case_read_model,
    reconcile_lifecycle_evidence,
)
from src.application.trades.lifecycle import (
    reconcile_polled_stock_settlement_evidence,
)


def reconcile_lifecycle_close_reason(
    repo: Any,
    *,
    case_id: str,
    now_ms: int,
    observation: dict[str, Any] | None = None,
    apply_changes: bool = False,
) -> dict[str, Any]:
    observation_payload = dict(observation or {})
    poll_results: list[dict[str, Any]] = []
    for candidate in observation_payload.get(
        "stock_settlement_candidates"
    ) or []:
        if not isinstance(candidate, dict):
            continue
        resolution = (
            reconcile_polled_stock_settlement_evidence(
                repo,
                evidence=dict(candidate),
                apply_changes=apply_changes,
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
            "lifecycle_read_model": lifecycle_case_read_model(
                repo,
                case_id=case_id,
                now_ms=now_ms,
            ),
        }
    facts = lifecycle_reconciliation_facts(repo, case_id=case_id)
    lifecycle_case = next(iter(facts["cases"]), None)
    if not isinstance(lifecycle_case, dict):
        raise ValueError(f"lifecycle case not found: {case_id}")
    evidence_rows = [
        dict(item)
        for item in facts["evidence"]
        if isinstance(item, dict)
    ]
    allocations = [
        dict(item)
        for item in facts["allocations"]
        if isinstance(item, dict)
    ]
    void_event_ids = tuple(
        facts.get("effective_void_event_ids") or ()
    )
    evidence_facts = lifecycle_evidence_facts(
        evidence=evidence_rows,
        allocations=allocations,
        void_event_ids=void_event_ids,
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
    timing_policy = repo.get_trade_lifecycle_timing_policy(case_id)
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
        cash_settlement_evidence=bool(
            observation_payload.get("cash_settlement_present")
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
        no_cash_settlement=not bool(
            observation_payload.get("cash_settlement_present")
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
            "observation_hash": canonical_hash(
                observation_payload
            ),
            "observation": observation_payload,
        }
        result = reconcile_lifecycle_evidence(
            repo,
            evidence=terminal_evidence,
            case_id=case_id,
            apply_changes=True,
            now_ms=now_ms,
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
            canonical_hash(observation_payload)
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
            "observation_hash": canonical_hash(
                observation_payload
            ),
            "observation": observation_payload,
        }
        write_result = record_lifecycle_evidence_issue(
            repo,
            case_id=case_id,
            evidence=issue_evidence,
            status=decision.status,
            reason_codes=list(decision.reason_codes),
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
) -> dict[str, Any]:
    account_value = str(account or "").strip().lower()
    if not account_value:
        raise ValueError("due reconciliation account is required")
    results: list[dict[str, Any]] = []
    for lifecycle_case in repo.list_trade_lifecycle_cases(
        account=account_value
    ):
        case_id = str(lifecycle_case.get("case_id") or "").strip()
        read_model = lifecycle_case_read_model(
            repo,
            case_id=case_id,
            now_ms=now_ms,
        )
        pairing_until = read_model.get("pairing_until_ms")
        settlement_deadline = read_model.get("pending_until_ms")
        if (
            pairing_until is None
            or int(now_ms) < int(pairing_until)
            or read_model.get("reason_state")
            not in {"cause_pending", "partially_resolved"}
        ):
            continue
        observation: dict[str, Any] | None = None
        observation_required = (
            settlement_deadline is not None
            and int(now_ms) >= int(settlement_deadline)
            and read_model.get("reason_state") != "resolved"
        )
        if observation_required and observation_collector is not None:
            observation = observation_collector(
                dict(lifecycle_case),
                dict(read_model),
            )
        if observation_required and observation is None:
            results.append(
                {
                    "case_id": case_id,
                    "status": "observation_required",
                    "apply_changes": bool(apply_changes),
                }
            )
            continue
        results.append(
            reconcile_lifecycle_close_reason(
                repo,
                case_id=case_id,
                now_ms=now_ms,
                observation=observation,
                apply_changes=apply_changes,
            )
        )
    return {
        "schema_version": "due_lifecycle_reconciliation.v1",
        "account": account_value,
        "now_ms": int(now_ms),
        "apply_changes": bool(apply_changes),
        "case_count": len(results),
        "results": results,
    }


__all__ = [
    "reconcile_due_lifecycle_cases",
    "reconcile_lifecycle_close_reason",
]
