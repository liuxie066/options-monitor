from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable

from domain.domain.symbol_identity import OPTION_CODE_RE, canonical_symbol
from src.application.ledger.api import (
    LegacySettlementSemanticUnavailable,
    SettlementAdmissionStateIncoherent,
    SettlementSemanticUnavailable,
    advance_lifecycle_case_state,
    list_trade_lifecycle_due_candidates,
)
from src.application.trades.close_reason_reconciliation import (
    reconcile_due_lifecycle_cases,
    reconcile_lifecycle_close_reason,
)
from src.application.trades.inbox import (
    SETTLEMENT_ATTEMPT_MIN_LEASE_MS,
    SettlementAttemptClaimOwnershipLost,
    claim_settlement_attempt,
    claim_settlement_provider_batch,
    complete_settlement_attempt,
    list_settlement_attempt_states,
    renew_settlement_attempt_claim,
    renew_settlement_provider_batch_claim,
    release_settlement_provider_batch_claim,
    require_trade_inbox_store_readable,
    settlement_attempt_summary,
    upsert_settlement_attempt_state,
)
from src.application.trades.lifecycle_reconciliation import (
    lifecycle_case_read_models_for_account,
)
from src.application.trades.lifecycle_timing import (
    bind_lifecycle_timing_policy,
)
from src.application.trades.settlement_observation import (
    LifecycleObservationGenerationChanged,
    SettlementObservationCollector,
    build_settlement_observation_collector,
)
from src.application.trades.settlement_attempts import (
    SETTLEMENT_COLLECTOR_CONTRACT_VERSION,
    SettlementAttemptOutcome,
    SETTLEMENT_OBSERVATION_CONTEXT_KEY,
    backoff_delay_ms,
    case_scope_fingerprint,
    prepare_provider_required_state,
    provider_input_scope_fingerprint,
    settlement_attempt_updates_after_outcome,
)


_SETTLEMENT_CLAIM_LEASE_MS = SETTLEMENT_ATTEMPT_MIN_LEASE_MS
_SETTLEMENT_CLAIM_RENEW_INTERVAL_SEC = 30.0
_SETTLEMENT_CLAIM_MONOTONIC_FN = time.monotonic


def _settlement_control_wall_clock_ms() -> int:
    return int(time.time() * 1000)


def _settlement_processing_failure_outcome(
    *,
    collector: SettlementObservationCollector,
    source_id: str,
    account: str,
    case_id: str,
    reason_code: str,
    error: Exception,
) -> SettlementAttemptOutcome:
    return SettlementAttemptOutcome(
        kind="unknown_error",
        source_id=source_id,
        account=account,
        case_id=case_id,
        contract_version=collector.contract.contract_version,
        capability_fingerprint=(
            collector.capability.capability_fingerprint
        ),
        reason_code=reason_code,
        error_class=type(error).__name__,
    )


def _optional_nonnegative_control_int(value: Any) -> int | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed >= 0 else None


def _settlement_completion_fallback_updates(
    state: dict[str, Any],
    *,
    outcome: SettlementAttemptOutcome,
    now_ms: int,
    case_scope: str,
    provider_scope: str,
    semantic_fingerprint: str | None,
    provider_attempted: bool,
) -> dict[str, Any]:
    previous_attempts = _optional_nonnegative_control_int(
        state.get("attempt_count")
    ) or 0
    previous_no_progress = _optional_nonnegative_control_int(
        state.get("no_progress_count")
    ) or 0
    prior_last_attempt = _optional_nonnegative_control_int(
        state.get("last_attempt_at_ms")
    )
    delay_ms = backoff_delay_ms(
        "unknown_error",
        attempt_count=previous_attempts,
        no_progress_count=previous_no_progress,
    )
    return {
        "case_scope_fingerprint": str(case_scope or "").strip(),
        "provider_input_scope_fingerprint": str(
            provider_scope or ""
        ).strip(),
        "collector_contract_version": outcome.contract_version,
        "capability_fingerprint": outcome.capability_fingerprint,
        "classification": "provider_required",
        "outcome_kind": "unknown_error",
        "reason_code": outcome.reason_code,
        "provider_code": None,
        "error_class": outcome.error_class,
        "attempt_count": previous_attempts
        + (1 if provider_attempted else 0),
        "no_progress_count": previous_no_progress,
        "next_attempt_at_ms": (
            int(now_ms) + int(delay_ms)
            if delay_ms is not None
            else None
        ),
        "last_attempt_at_ms": (
            int(now_ms) if provider_attempted else prior_last_attempt
        ),
        "last_semantic_fingerprint": (
            str(semantic_fingerprint or "").strip()
            or str(
                state.get("last_semantic_fingerprint") or ""
            ).strip()
            or None
        ),
        "updated_at_ms": int(now_ms),
    }


class _SettlementClaimLeaseGuard:
    """Keep one provider-attempt claim alive while its worker is active."""

    def __init__(
        self,
        *,
        inbox_path: Path,
        source_id: str,
        account: str,
        case_id: str,
        case_scope_fingerprint: str,
        claim_id: str,
        initial_now_ms: int,
        now_ms_fn: Callable[[], int],
        monotonic_fn: Callable[[], float] = time.monotonic,
        lease_ms: int = _SETTLEMENT_CLAIM_LEASE_MS,
        renew_interval_sec: float = (
            _SETTLEMENT_CLAIM_RENEW_INTERVAL_SEC
        ),
        renew_callback: Callable[[int, int], bool] | None = None,
    ) -> None:
        self._inbox_path = Path(inbox_path)
        self._source_id = str(source_id or "").strip()
        self._account = str(account or "").strip().lower()
        self._case_id = str(case_id or "").strip()
        self._case_scope_fingerprint = str(
            case_scope_fingerprint or ""
        ).strip()
        self._claim_id = str(claim_id or "").strip()
        self._initial_now_ms = int(initial_now_ms)
        self._now_ms_fn = now_ms_fn
        self._monotonic_fn = monotonic_fn
        self._initial_monotonic = float(self._monotonic_fn())
        self._lease_ms = max(
            SETTLEMENT_ATTEMPT_MIN_LEASE_MS,
            int(lease_ms or 0),
        )
        self._renew_interval_sec = max(
            0.001,
            float(renew_interval_sec),
        )
        self._renew_callback = renew_callback
        self._stop = threading.Event()
        self._renew_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._error: Exception | None = None
        self._thread = threading.Thread(
            target=self._run,
            name=f"settlement-claim-{self._case_id}",
            daemon=True,
        )
        self._started = False

    @property
    def error(self) -> Exception | None:
        with self._state_lock:
            return self._error

    @property
    def logical_now_ms(self) -> int:
        """Return the monotonic claim clock used for lease handoff."""

        return self._lease_now_ms()

    def start(self) -> _SettlementClaimLeaseGuard:
        if not self._started:
            self._started = True
            try:
                self._thread.start()
            except Exception as exc:
                self._record_error(exc)
        return self

    def renew_now(self) -> Exception | None:
        existing = self.error
        if existing is not None:
            return existing
        with self._renew_lock:
            existing = self.error
            if existing is not None:
                return existing
            try:
                lease_now_ms = self._lease_now_ms()
                renewed = (
                    self._renew_callback(
                        lease_now_ms,
                        self._lease_ms,
                    )
                    if self._renew_callback is not None
                    else renew_settlement_attempt_claim(
                        self._inbox_path,
                        source_id=self._source_id,
                        account=self._account,
                        case_id=self._case_id,
                        case_scope_fingerprint=(
                            self._case_scope_fingerprint
                        ),
                        claim_id=self._claim_id,
                        now_ms=lease_now_ms,
                        lease_ms=self._lease_ms,
                    )
                )
            except Exception as exc:
                self._record_error(exc)
                return exc
            if not renewed:
                error = SettlementAttemptClaimOwnershipLost(
                    "settlement attempt claim ownership changed during renewal"
                )
                self._record_error(error)
                return error
        return None

    def stop(self) -> Exception | None:
        self._stop.set()
        if (
            self._started
            and self._thread.is_alive()
            and threading.current_thread() is not self._thread
        ):
            self._thread.join()
        return self.error

    def __enter__(self) -> _SettlementClaimLeaseGuard:
        return self.start()

    def __exit__(self, *_exc_info: object) -> None:
        self.stop()

    def _record_error(self, error: Exception) -> None:
        with self._state_lock:
            if self._error is None:
                self._error = error
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(self._renew_interval_sec):
            if self.renew_now() is not None:
                return

    def _lease_now_ms(self) -> int:
        elapsed_ms = max(
            0,
            int(
                (float(self._monotonic_fn()) - self._initial_monotonic)
                * 1000
            ),
        )
        return max(
            int(self._now_ms_fn()),
            self._initial_now_ms + elapsed_ms,
        )


class _SettlementProviderBatchLease:
    """Own and clean up one source/account provider-batch lease."""

    def __init__(self) -> None:
        self._guard: _SettlementClaimLeaseGuard | None = None
        self._inbox_path: Path | None = None
        self._source_id = ""
        self._account = ""
        self._claim_id = ""
        self._claimed = False
        self._closed = False
        self._error: Exception | None = None

    def start(
        self,
        *,
        inbox_path: Path,
        source_id: str,
        account: str,
        claim_id: str,
        initial_now_ms: int,
        now_ms_fn: Callable[[], int],
        monotonic_fn: Callable[[], float],
        lease_ms: int,
        renew_interval_sec: float,
    ) -> Exception | None:
        self._inbox_path = Path(inbox_path)
        self._source_id = str(source_id or "").strip()
        self._account = str(account or "").strip().lower()
        self._claim_id = str(claim_id or "").strip()
        self._claimed = True

        def renew_batch(now_value: int, lease_value: int) -> bool:
            return renew_settlement_provider_batch_claim(
                self._inbox_path,
                source_id=self._source_id,
                account=self._account,
                claim_id=self._claim_id,
                now_ms=now_value,
                lease_ms=lease_value,
            )

        try:
            self._guard = _SettlementClaimLeaseGuard(
                inbox_path=self._inbox_path,
                source_id=self._source_id,
                account=self._account,
                case_id=f"provider-batch:{self._account}",
                case_scope_fingerprint="provider-batch",
                claim_id=self._claim_id,
                initial_now_ms=int(initial_now_ms),
                now_ms_fn=now_ms_fn,
                monotonic_fn=monotonic_fn,
                lease_ms=lease_ms,
                renew_interval_sec=renew_interval_sec,
                renew_callback=renew_batch,
            ).start()
            self._error = self._guard.error
            if self._error is None:
                self._error = self._guard.renew_now()
        except Exception as exc:
            self._error = exc
        return self._error

    def renew_now(self) -> Exception | None:
        if self._error is not None:
            return self._error
        if self._guard is None:
            return None
        self._error = self._guard.renew_now()
        return self._error

    def close(self) -> Exception | None:
        if self._closed:
            return None
        self._closed = True
        error = self._error
        if self._guard is not None:
            stopped_error = self._guard.stop()
            if error is None:
                error = stopped_error
        if self._claimed and self._inbox_path is not None:
            try:
                release_settlement_provider_batch_claim(
                    self._inbox_path,
                    source_id=self._source_id,
                    account=self._account,
                    claim_id=self._claim_id,
                )
            except Exception as exc:
                if error is None:
                    error = exc
        self._claimed = False
        self._error = error
        return error


def ensure_lifecycle_timing_after_intake(
    repo: Any,
    *,
    payload: dict[str, Any],
    result: dict[str, Any],
    gateway: Any | None = None,
    quote_gateway: Any | None = None,
    quote_dependency_error: str | None = None,
    now_ms: int,
    apply_changes: bool,
) -> dict[str, Any] | None:
    quote_gateway = quote_gateway or gateway
    adoption = _lifecycle_adoption(result)
    lifecycle_case = (
        dict(adoption.get("lifecycle_case") or {})
        if isinstance(adoption, dict)
        else {}
    )
    case_id = str(lifecycle_case.get("case_id") or "").strip()
    if not case_id:
        return None
    existing = repo.get_trade_lifecycle_timing_policy(case_id)
    if isinstance(existing, dict):
        binding = {
            "schema_version": "lifecycle_timing_binding_result.v1",
            "case_id": case_id,
            "apply_changes": bool(apply_changes),
            "created": False,
            "existing": True,
            "policy": existing,
        }
    else:
        try:
            if quote_gateway is None:
                raise RuntimeError(
                    str(quote_dependency_error or "").strip()
                    or "Futu quote dependency is unavailable"
                )
            contract_metadata = _registry_contract_metadata(
                payload,
                lifecycle_case=lifecycle_case,
            )
            expiration = date.fromisoformat(
                str(lifecycle_case.get("expiration_ymd") or "")
            )
            calendar_start = (
                expiration - timedelta(days=1)
            ).isoformat()
            calendar_end = (
                expiration + timedelta(days=14)
            ).isoformat()
            market = str(
                lifecycle_case.get("market")
                or contract_metadata.get("market")
                or ""
            ).strip().upper()
            calendar_result = (
                quote_gateway.get_trading_days_with_receipt(
                    market=market,
                    start=calendar_start,
                    end=calendar_end,
                )
            )
            if (
                not isinstance(calendar_result, dict)
                or not bool(
                    calendar_result.get("coverage_complete")
                )
                or not bool(
                    calendar_result.get("pagination_complete")
                )
                or not isinstance(
                    calendar_result.get("rows"),
                    list,
                )
            ):
                raise ValueError(
                    "Futu quote trading calendar coverage is incomplete"
                )
            binding = bind_lifecycle_timing_policy(
                repo,
                lifecycle_case={
                    **lifecycle_case,
                    "market": market,
                },
                contract_metadata=contract_metadata,
                trading_days=[
                    dict(item)
                    for item in calendar_result.get("rows") or []
                    if isinstance(item, dict)
                ],
                calendar_source="futu_request_trading_days",
                calendar_observed_at_ms=int(now_ms),
                apply_changes=apply_changes,
            )
        except Exception as exc:
            failure = {
                "schema_version": "lifecycle_timing_binding_result.v1",
                "case_id": case_id,
                "apply_changes": bool(apply_changes),
                "created": False,
                "existing": False,
                "status": "needs_review",
                "reason_codes": [
                    "lifecycle_timing_policy_unavailable"
                ],
                "error": f"{type(exc).__name__}: {exc}",
            }
            if apply_changes:
                failure["write_result"] = (
                    advance_lifecycle_case_state(
                        repo,
                        case_id=case_id,
                        status="needs_review",
                        derived_summary={
                            "reason_state": "needs_review",
                            "close_reason": None,
                            "lifecycle_reason_codes": [
                                "lifecycle_timing_policy_unavailable"
                            ],
                            "timing_error": failure["error"],
                        },
                        public_transition="needs_review",
                    )
                )
            return failure

    if not apply_changes:
        return binding
    case_status = str(
        lifecycle_case.get("status") or ""
    ).strip().lower()
    if case_status in {"ledger_written", "conflict"}:
        return binding
    reconciliation = reconcile_lifecycle_close_reason(
        repo,
        case_id=case_id,
        now_ms=int(now_ms),
        apply_changes=True,
    )
    return {**binding, "reconciliation": reconciliation}


def reconcile_due_lifecycle_cases_for_source(
    repo: Any,
    *,
    source: dict[str, Any],
    gateway: Any | None = None,
    broker_gateway: Any | None = None,
    quote_gateway: Any | None = None,
    quote_dependency_error: str | None = None,
    trd_env: str = "REAL",
    now_ms: int,
    apply_changes: bool,
    settlement_collector: SettlementObservationCollector | None = None,
    settlement_collector_factory: (
        Callable[[], SettlementObservationCollector] | None
    ) = None,
    settlement_control_now_ms_fn: Callable[[], int] | None = None,
    process_metrics: dict[str, int] | None = None,
) -> dict[str, Any]:
    provider_batch_lease = _SettlementProviderBatchLease()
    try:
        return _reconcile_due_lifecycle_cases_for_source(
            repo,
            source=source,
            gateway=gateway,
            broker_gateway=broker_gateway,
            quote_gateway=quote_gateway,
            quote_dependency_error=quote_dependency_error,
            trd_env=trd_env,
            now_ms=now_ms,
            apply_changes=apply_changes,
            settlement_collector=settlement_collector,
            settlement_collector_factory=settlement_collector_factory,
            settlement_control_now_ms_fn=(
                settlement_control_now_ms_fn
            ),
            process_metrics=process_metrics,
            provider_batch_lease=provider_batch_lease,
        )
    finally:
        provider_batch_lease.close()


def _reconcile_due_lifecycle_cases_for_source(
    repo: Any,
    *,
    source: dict[str, Any],
    gateway: Any | None = None,
    broker_gateway: Any | None = None,
    quote_gateway: Any | None = None,
    quote_dependency_error: str | None = None,
    trd_env: str = "REAL",
    now_ms: int,
    apply_changes: bool,
    settlement_collector: SettlementObservationCollector | None = None,
    settlement_collector_factory: (
        Callable[[], SettlementObservationCollector] | None
    ) = None,
    settlement_control_now_ms_fn: Callable[[], int] | None = None,
    process_metrics: dict[str, int] | None = None,
    provider_batch_lease: _SettlementProviderBatchLease,
) -> dict[str, Any]:
    account = str(source.get("account") or "").strip().lower()
    account_ids = [
        str(item or "").strip()
        for item in list(source.get("futu_account_ids") or [])
        if str(item or "").strip()
    ]
    if not account or not account_ids:
        raise ValueError(
            "due lifecycle source requires one account and at least one Futu account id"
    )
    source_id = str(source.get("id") or account).strip()
    if (
        settlement_collector is not None
        and settlement_collector_factory is not None
    ):
        raise ValueError(
            "settlement collector and factory are mutually exclusive"
        )
    collector = settlement_collector

    def require_collector() -> SettlementObservationCollector:
        nonlocal collector
        if collector is None:
            collector = (
                settlement_collector_factory()
                if settlement_collector_factory is not None
                else build_settlement_observation_collector(
                    repo=repo,
                    gateway=gateway,
                    broker_gateway=broker_gateway,
                    quote_gateway=quote_gateway,
                    quote_dependency_error=quote_dependency_error,
                    futu_account_ids=account_ids,
                    trd_env=trd_env,
                    now_ms_fn=lambda: int(now_ms),
                    source_id=source_id,
                )
            )
        return collector

    if not apply_changes:
        return reconcile_due_lifecycle_cases(
            repo,
            account=account,
            now_ms=int(now_ms),
            apply_changes=False,
            observation_collector=require_collector(),
        )

    control_now_ms_fn = (
        settlement_control_now_ms_fn
        or _settlement_control_wall_clock_ms
    )

    def control_now_ms() -> int:
        return int(control_now_ms_fn())

    inbox_path = _settlement_control_path(source)
    settlement_cfg = source.get("settlement_observation")
    collector_enabled = (
        bool(settlement_cfg.get("enabled", True))
        if isinstance(settlement_cfg, dict)
        else True
    )
    metrics = process_metrics if process_metrics is not None else {}
    for key in (
        "collector_attempt_count",
        "semantic_admission_count",
        "semantic_duplicate_count",
    ):
        metrics[key] = int(metrics.get(key) or 0)

    candidates = list_trade_lifecycle_due_candidates(
        repo,
        account=account,
    )
    candidates_by_id = _due_candidates_by_id(candidates)
    fingerprints = {
        case_id: case_scope_fingerprint(candidate)
        for case_id, candidate in candidates_by_id.items()
    }
    states, control_error = _run_settlement_control_operation(
        inbox_path,
        list_settlement_attempt_states,
        inbox_path,
        source_id=source_id,
        account=account,
        case_ids=tuple(candidates_by_id),
    )
    if control_error is not None:
        local_result = _plan_due_cases(
            repo,
            account=account,
            case_ids=tuple(candidates_by_id),
            now_ms=int(now_ms),
            apply_changes=True,
        )
        return _control_store_unavailable_result(
            account=account,
            source_id=source_id,
            collector_enabled=collector_enabled,
            collector=collector,
            candidate_count=len(candidates_by_id),
            planned_case_count=len(candidates_by_id),
            provider_claim_count=0,
            provider_attempt_count=0,
            control_error=control_error,
            local_result=local_result,
            metrics=metrics,
        )
    if not isinstance(states, dict):
        raise TypeError("settlement attempt state listing is invalid")

    needs_plan: list[str] = []
    provider_case_ids: list[str] = []
    provider_batch_claim_active = False
    skipped_counts = {
        "cached_local": 0,
        "claimed": 0,
        "backoff": 0,
        "blocked": 0,
        "disabled": 0,
    }
    for case_id, candidate in candidates_by_id.items():
        state = states.get(case_id)
        if _active_claim(state, now_ms=control_now_ms()):
            provider_batch_claim_active = True
            skipped_counts["claimed"] += 1
            continue
        fingerprint = fingerprints[case_id]
        if (
            state is None
            or str(state.get("case_scope_fingerprint") or "")
            != fingerprint
        ):
            needs_plan.append(case_id)
            continue
        classification = str(
            state.get("classification") or ""
        ).strip()
        if classification == "provider_required":
            provider_case_ids.append(case_id)
            continue
        if classification == "local":
            next_recheck = state.get("next_attempt_at_ms")
            if (
                next_recheck is not None
                and int(next_recheck) <= int(now_ms)
            ):
                needs_plan.append(case_id)
            else:
                skipped_counts["cached_local"] += 1
            continue
        needs_plan.append(case_id)

    local_result: dict[str, Any] = {
        "schema_version": "due_lifecycle_reconciliation.v2",
        "account": account,
        "now_ms": int(now_ms),
        "apply_changes": True,
        "case_count": 0,
        "results": [],
    }

    def control_unavailable(
        error: Exception,
        *,
        provider_claim_count: int = 0,
        provider_attempt_count: int = 0,
        provider_results: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        return _control_store_unavailable_result(
            account=account,
            source_id=source_id,
            collector_enabled=collector_enabled,
            collector=collector,
            candidate_count=len(candidates_by_id),
            planned_case_count=len(needs_plan),
            provider_claim_count=provider_claim_count,
            provider_attempt_count=provider_attempt_count,
            control_error=error,
            local_result=local_result,
            metrics=metrics,
            provider_results=provider_results,
        )

    def complete_claim(
        *,
        case_id: str,
        claim_id: str,
        state: dict[str, Any],
        outcome: SettlementAttemptOutcome,
        case_scope: str,
        provider_scope: str,
        semantic_fingerprint: str | None,
        provider_attempted: bool,
    ) -> tuple[
        dict[str, Any] | None,
        Exception | None,
        SettlementAttemptOutcome,
    ]:
        completion_now_ms = control_now_ms()
        effective_outcome = outcome
        try:
            updates = settlement_attempt_updates_after_outcome(
                state,
                outcome=effective_outcome,
                now_ms=completion_now_ms,
                case_scope_fingerprint_value=case_scope,
                provider_input_scope_fingerprint_value=provider_scope,
                semantic_fingerprint=semantic_fingerprint,
                provider_attempted=provider_attempted,
            )
        except Exception as exc:
            effective_outcome = _settlement_processing_failure_outcome(
                collector=collector,
                source_id=source_id,
                account=account,
                case_id=case_id,
                reason_code="settlement_attempt_completion_failed",
                error=exc,
            )
            updates = _settlement_completion_fallback_updates(
                state,
                outcome=effective_outcome,
                now_ms=completion_now_ms,
                case_scope=case_scope,
                provider_scope=provider_scope,
                semantic_fingerprint=semantic_fingerprint,
                provider_attempted=provider_attempted,
            )
        completed, completion_error = (
            _run_settlement_control_operation(
                inbox_path,
                complete_settlement_attempt,
                inbox_path,
                source_id=source_id,
                account=account,
                case_id=case_id,
                claim_id=claim_id,
                updates=updates,
            )
        )
        return (
            dict(completed) if isinstance(completed, dict) else None,
            completion_error,
            effective_outcome,
        )

    prepared_models: dict[str, dict[str, Any]] = {}
    if needs_plan:
        prepared_models = lifecycle_case_read_models_for_account(
            repo,
            account=account,
            now_ms=int(now_ms),
        )
        local_result = reconcile_due_lifecycle_cases(
            repo,
            account=account,
            now_ms=int(now_ms),
            apply_changes=True,
            observation_collector=None,
            case_ids=needs_plan,
            prepared_read_models=prepared_models,
        )
        plan_results = _results_by_case(local_result)
        post_plan_candidates = _due_candidates_by_id(
            list_trade_lifecycle_due_candidates(
                repo,
                account=account,
            )
        )
        candidates_by_id.update(post_plan_candidates)
        for case_id in needs_plan:
            candidate = post_plan_candidates.get(
                case_id,
                candidates_by_id[case_id],
            )
            fingerprint = case_scope_fingerprint(candidate)
            lifecycle_case = dict(
                candidate.get("lifecycle_case") or {}
            )
            read_model = prepared_models.get(case_id, {})
            result = plan_results.get(case_id)
            if str((result or {}).get("status") or "") == (
                "observation_required"
            ):
                state_collector = (
                    require_collector()
                    if collector_enabled
                    else collector
                )
                provider_scope = provider_input_scope_fingerprint(
                    lifecycle_case=lifecycle_case,
                    read_model=read_model,
                )
                state = prepare_provider_required_state(
                    states.get(case_id),
                    source_id=source_id,
                    account=account,
                    case_id=case_id,
                    case_scope_fingerprint_value=fingerprint,
                    provider_input_scope_fingerprint_value=(
                        provider_scope
                    ),
                    contract_version=_collector_contract_version(
                        state_collector
                    ),
                    capability_fingerprint=(
                        _collector_capability_fingerprint(
                            state_collector
                        )
                    ),
                    now_ms=control_now_ms(),
                )
                stored, control_error = _run_settlement_control_operation(
                    inbox_path,
                    upsert_settlement_attempt_state,
                    inbox_path,
                    state=state,
                )
                if control_error is not None:
                    return control_unavailable(control_error)
                if not isinstance(stored, dict):
                    raise TypeError("settlement attempt state is invalid")
                states[case_id] = stored
                stored_matches_scope = (
                    str(stored.get("case_scope_fingerprint") or "")
                    == fingerprint
                )
                stored_claim_active = _active_claim(
                    stored,
                    now_ms=control_now_ms(),
                )
                if stored_matches_scope and stored_claim_active:
                    provider_batch_claim_active = True
                    skipped_counts["claimed"] += 1
                elif stored_matches_scope:
                    provider_case_ids.append(case_id)
                continue
            local_state = _local_control_state(
                source_id=source_id,
                account=account,
                case_id=case_id,
                case_scope_fingerprint_value=fingerprint,
                collector=collector,
                read_model=read_model,
                result=result,
                business_now_ms=int(now_ms),
                control_now_ms=control_now_ms(),
            )
            stored, control_error = _run_settlement_control_operation(
                inbox_path,
                upsert_settlement_attempt_state,
                inbox_path,
                state=local_state,
            )
            if control_error is not None:
                return control_unavailable(control_error)
            if not isinstance(stored, dict):
                raise TypeError("settlement attempt state is invalid")
            states[case_id] = stored

    eligible_provider_case_ids: list[str] = []
    provider_claim_count = 0
    provider_call_count = 0
    provider_results: list[dict[str, Any]] = []
    provider_case_ids = list(dict.fromkeys(provider_case_ids))
    for case_id in provider_case_ids:
        candidate = candidates_by_id.get(case_id)
        state = states.get(case_id)
        if candidate is None or state is None:
            continue
        current_scope = case_scope_fingerprint(candidate)
        if (
            str(state.get("case_scope_fingerprint") or "")
            != current_scope
        ):
            continue
        if _active_claim(state, now_ms=control_now_ms()):
            provider_batch_claim_active = True
            skipped_counts["claimed"] += 1
            continue
        if not collector_enabled:
            if str(state.get("outcome_kind") or "") == "disabled":
                skipped_counts["disabled"] += 1
                continue
            _stored, control_error = _run_settlement_control_operation(
                inbox_path,
                upsert_settlement_attempt_state,
                inbox_path,
                state={
                    **state,
                    "outcome_kind": "disabled",
                    "reason_code": "collector_disabled",
                    "provider_code": None,
                    "error_class": None,
                    "next_attempt_at_ms": None,
                    "claim_id": None,
                    "claim_until_ms": None,
                    "updated_at_ms": control_now_ms(),
                },
            )
            if control_error is not None:
                return control_unavailable(
                    control_error,
                    provider_claim_count=provider_claim_count,
                )
            skipped_counts["disabled"] += 1
            continue
        active_collector = require_collector()
        if not _collector_scope_matches(state, active_collector):
            state = _refresh_capability_scope(
                state,
                collector=active_collector,
                source_id=source_id,
                account=account,
                case_id=case_id,
                case_scope_fingerprint_value=current_scope,
                now_ms=control_now_ms(),
            )
            state, control_error = _run_settlement_control_operation(
                inbox_path,
                upsert_settlement_attempt_state,
                inbox_path,
                state=state,
            )
            if control_error is not None:
                return control_unavailable(
                    control_error,
                    provider_claim_count=provider_claim_count,
                )
            if not isinstance(state, dict):
                raise TypeError("settlement attempt state is invalid")
            states[case_id] = state
        if not active_collector.capability.supported:
            if str(state.get("outcome_kind") or "") == "blocked_static":
                skipped_counts["blocked"] += 1
                continue
            blocked = SettlementAttemptOutcome(
                kind="blocked_static",
                source_id=source_id,
                account=account,
                case_id=case_id,
                contract_version=(
                    active_collector.contract.contract_version
                ),
                capability_fingerprint=(
                    active_collector.capability.capability_fingerprint
                ),
                reason_code="missing_static_capability",
                error_class="missing_static",
            )
            _stored, control_error = _run_settlement_control_operation(
                inbox_path,
                upsert_settlement_attempt_state,
                inbox_path,
                state={
                    **state,
                    **settlement_attempt_updates_after_outcome(
                        state,
                        outcome=blocked,
                        now_ms=control_now_ms(),
                        case_scope_fingerprint_value=current_scope,
                        provider_input_scope_fingerprint_value=str(
                            state.get(
                                "provider_input_scope_fingerprint"
                            )
                            or ""
                        ),
                    ),
                    "claim_id": None,
                    "claim_until_ms": None,
                },
            )
            if control_error is not None:
                return control_unavailable(
                    control_error,
                    provider_claim_count=provider_claim_count,
                )
            skipped_counts["blocked"] += 1
            continue
        if str(state.get("outcome_kind") or "") in {
            "blocked_static",
            "legacy_semantic_unavailable",
        }:
            skipped_counts["blocked"] += 1
            continue
        next_attempt = state.get("next_attempt_at_ms")
        if (
            next_attempt is not None
            and int(next_attempt) > control_now_ms()
        ):
            skipped_counts["backoff"] += 1
            continue
        eligible_provider_case_ids.append(case_id)

    preclaimed: dict[str, str] = {}
    preclaim_handoff_now_ms: dict[str, int] = {}
    if eligible_provider_case_ids and provider_batch_claim_active:
        eligible_provider_case_ids = []
    elif eligible_provider_case_ids:
        batch_claim_id = uuid.uuid4().hex
        batch_claim_now_ms = control_now_ms()
        batch_claim_acquired, control_error = (
            _run_settlement_control_operation(
                inbox_path,
                claim_settlement_provider_batch,
                inbox_path,
                source_id=source_id,
                account=account,
                claim_id=batch_claim_id,
                now_ms=batch_claim_now_ms,
                lease_ms=_SETTLEMENT_CLAIM_LEASE_MS,
            )
        )
        if control_error is not None:
            return control_unavailable(
                control_error,
                provider_claim_count=provider_claim_count,
            )
        if not bool(batch_claim_acquired):
            skipped_counts["claimed"] += 1
            eligible_provider_case_ids = []
        else:
            batch_start_error = provider_batch_lease.start(
                inbox_path=inbox_path,
                source_id=source_id,
                account=account,
                claim_id=batch_claim_id,
                initial_now_ms=batch_claim_now_ms,
                now_ms_fn=control_now_ms,
                monotonic_fn=_SETTLEMENT_CLAIM_MONOTONIC_FN,
                lease_ms=_SETTLEMENT_CLAIM_LEASE_MS,
                renew_interval_sec=(
                    _SETTLEMENT_CLAIM_RENEW_INTERVAL_SEC
                ),
            )
            if batch_start_error is not None:
                if isinstance(
                    batch_start_error,
                    (
                        sqlite3.OperationalError,
                        SettlementAttemptClaimOwnershipLost,
                    ),
                ):
                    provider_batch_lease.close()
                    if isinstance(
                        batch_start_error,
                        sqlite3.OperationalError,
                    ):
                        require_trade_inbox_store_readable(inbox_path)
                    return control_unavailable(
                        batch_start_error,
                        provider_claim_count=provider_claim_count,
                    )
                collector = require_collector()
                failed_case_id = eligible_provider_case_ids[0]
                failed_state = states[failed_case_id]
                failed_outcome = _settlement_processing_failure_outcome(
                    collector=collector,
                    source_id=source_id,
                    account=account,
                    case_id=failed_case_id,
                    reason_code="settlement_attempt_lease_guard_failed",
                    error=batch_start_error,
                )
                failure_now_ms = control_now_ms()
                try:
                    failure_updates = (
                        settlement_attempt_updates_after_outcome(
                            failed_state,
                            outcome=failed_outcome,
                            now_ms=failure_now_ms,
                            case_scope_fingerprint_value=str(
                                failed_state.get(
                                    "case_scope_fingerprint"
                                )
                                or ""
                            ),
                            provider_input_scope_fingerprint_value=str(
                                failed_state.get(
                                    "provider_input_scope_fingerprint"
                                )
                                or ""
                            ),
                            provider_attempted=False,
                        )
                    )
                except Exception as exc:
                    failed_outcome = (
                        _settlement_processing_failure_outcome(
                            collector=collector,
                            source_id=source_id,
                            account=account,
                            case_id=failed_case_id,
                            reason_code=(
                                "settlement_attempt_completion_failed"
                            ),
                            error=exc,
                        )
                    )
                    failure_updates = (
                        _settlement_completion_fallback_updates(
                            failed_state,
                            outcome=failed_outcome,
                            now_ms=failure_now_ms,
                            case_scope=str(
                                failed_state.get(
                                    "case_scope_fingerprint"
                                )
                                or ""
                            ),
                            provider_scope=str(
                                failed_state.get(
                                    "provider_input_scope_fingerprint"
                                )
                                or ""
                            ),
                            semantic_fingerprint=None,
                            provider_attempted=False,
                        )
                    )
                stored, control_error = (
                    _run_settlement_control_operation(
                        inbox_path,
                        upsert_settlement_attempt_state,
                        inbox_path,
                        state={
                            **failed_state,
                            **failure_updates,
                            "claim_id": None,
                            "claim_until_ms": None,
                        },
                    )
                )
                batch_close_error = provider_batch_lease.close()
                if control_error is not None:
                    return control_unavailable(
                        control_error,
                        provider_claim_count=provider_claim_count,
                    )
                if batch_close_error is not None:
                    return control_unavailable(
                        batch_close_error,
                        provider_claim_count=provider_claim_count,
                    )
                if not isinstance(stored, dict):
                    raise TypeError("settlement attempt state is invalid")
                states[failed_case_id] = stored
                provider_results.append(
                    {
                        "case_id": failed_case_id,
                        "outcome": failed_outcome.to_dict(
                            include_observation=False
                        ),
                        "semantic_fingerprint": None,
                        "admission_status": None,
                    }
                )
                eligible_provider_case_ids = []

    if eligible_provider_case_ids:
        collector = require_collector()
        first_case_id = eligible_provider_case_ids[0]
        first_state = states[first_case_id]
        first_claim_id = uuid.uuid4().hex
        first_claim_now_ms = control_now_ms()
        claim_acquired, control_error = (
            _run_settlement_control_operation(
                inbox_path,
                claim_settlement_attempt,
                inbox_path,
                source_id=source_id,
                account=account,
                case_id=first_case_id,
                case_scope_fingerprint=str(
                    first_state.get("case_scope_fingerprint") or ""
                ),
                claim_id=first_claim_id,
                now_ms=first_claim_now_ms,
                lease_ms=_SETTLEMENT_CLAIM_LEASE_MS,
            )
        )
        if control_error is not None:
            return control_unavailable(
                control_error,
                provider_claim_count=provider_claim_count,
            )
        if bool(claim_acquired):
            preclaimed[first_case_id] = first_claim_id
            provider_claim_count += 1
        else:
            skipped_counts["claimed"] += 1
            eligible_provider_case_ids = []

    if eligible_provider_case_ids:
        collector = require_collector()
        preclaimed_case_id, preclaimed_claim_id = next(
            iter(preclaimed.items())
        )
        preclaimed_state = states[preclaimed_case_id]
        preparation_guard = _SettlementClaimLeaseGuard(
            inbox_path=inbox_path,
            source_id=source_id,
            account=account,
            case_id=preclaimed_case_id,
            case_scope_fingerprint=str(
                preclaimed_state.get("case_scope_fingerprint") or ""
            ),
            claim_id=preclaimed_claim_id,
            initial_now_ms=first_claim_now_ms,
            now_ms_fn=control_now_ms,
            monotonic_fn=_SETTLEMENT_CLAIM_MONOTONIC_FN,
            lease_ms=_SETTLEMENT_CLAIM_LEASE_MS,
            renew_interval_sec=_SETTLEMENT_CLAIM_RENEW_INTERVAL_SEC,
        ).start()
        preparation_guard_start_error = preparation_guard.error
        preparation_lease_error: Exception | None = None
        preparation_batch_error: Exception | None = None
        preparation_error: Exception | None = None
        try:
            if preparation_guard_start_error is not None:
                raise preparation_guard_start_error
            preparation_batch_error = provider_batch_lease.renew_now()
            if preparation_batch_error is None:
                preparation_lease_error = preparation_guard.renew_now()
            if (
                preparation_batch_error is None
                and preparation_lease_error is None
            ):
                current_models = lifecycle_case_read_models_for_account(
                    repo,
                    account=account,
                    now_ms=int(now_ms),
                    settlement_context_case_ids=(
                        eligible_provider_case_ids
                    ),
                )
                current_candidates = _due_candidates_by_id(
                    list_trade_lifecycle_due_candidates(
                        repo,
                        account=account,
                    )
                )
                validation = reconcile_due_lifecycle_cases(
                    repo,
                    account=account,
                    now_ms=int(now_ms),
                    apply_changes=True,
                    observation_collector=None,
                    case_ids=eligible_provider_case_ids,
                    prepared_read_models=current_models,
                )
                validation_results = _results_by_case(validation)
                preparation_lease_error = (
                    preparation_guard.renew_now()
                )
                if preparation_lease_error is None:
                    preparation_batch_error = (
                        provider_batch_lease.renew_now()
                    )
        except Exception as exc:
            preparation_error = exc
        finally:
            stopped_error = preparation_guard.stop()
            if (
                preparation_lease_error is None
                and preparation_guard_start_error is None
            ):
                preparation_lease_error = stopped_error

        if preparation_lease_error is not None and isinstance(
            preparation_lease_error,
            (sqlite3.OperationalError, SettlementAttemptClaimOwnershipLost),
        ):
            if isinstance(preparation_lease_error, sqlite3.OperationalError):
                require_trade_inbox_store_readable(inbox_path)
            return control_unavailable(
                preparation_lease_error,
                provider_claim_count=provider_claim_count,
                provider_attempt_count=provider_call_count,
                provider_results=provider_results,
            )

        if isinstance(preparation_batch_error, sqlite3.OperationalError):
            require_trade_inbox_store_readable(inbox_path)

        failure_error = (
            preparation_error
            or preparation_lease_error
            or preparation_batch_error
        )
        if failure_error is not None:
            failure_reason = (
                "settlement_provider_batch_lease_failed"
                if preparation_batch_error is not None
                else (
                    "settlement_attempt_lease_guard_failed"
                    if (
                        preparation_guard_start_error is not None
                        or preparation_lease_error is not None
                    )
                    else "settlement_attempt_preparation_failed"
                )
            )
            failure = _settlement_processing_failure_outcome(
                collector=collector,
                source_id=source_id,
                account=account,
                case_id=preclaimed_case_id,
                reason_code=failure_reason,
                error=failure_error,
            )
            _completed, completion_error, completed_outcome = (
                complete_claim(
                    case_id=preclaimed_case_id,
                    claim_id=preclaimed_claim_id,
                    state=preclaimed_state,
                    outcome=failure,
                    case_scope=str(
                        preclaimed_state.get(
                            "case_scope_fingerprint"
                        )
                        or ""
                    ),
                    provider_scope=str(
                        preclaimed_state.get(
                            "provider_input_scope_fingerprint"
                        )
                        or ""
                    ),
                    semantic_fingerprint=None,
                    provider_attempted=False,
                )
            )
            provider_results.append(
                {
                    "case_id": preclaimed_case_id,
                    "outcome": completed_outcome.to_dict(
                        include_observation=False
                    ),
                    "semantic_fingerprint": None,
                    "admission_status": None,
                }
            )
            if completion_error is not None:
                return control_unavailable(
                    completion_error,
                    provider_claim_count=provider_claim_count,
                    provider_attempt_count=provider_call_count,
                    provider_results=provider_results,
                )
            eligible_provider_case_ids = []
            if preparation_batch_error is not None:
                return control_unavailable(
                    preparation_batch_error,
                    provider_claim_count=provider_claim_count,
                    provider_attempt_count=provider_call_count,
                    provider_results=provider_results,
                )
        else:
            preclaim_handoff_now_ms[preclaimed_case_id] = (
                preparation_guard.logical_now_ms
            )

    if eligible_provider_case_ids:
        for case_id in eligible_provider_case_ids:
            if case_id not in preclaimed:
                batch_lease_error = provider_batch_lease.renew_now()
                if batch_lease_error is not None:
                    if isinstance(
                        batch_lease_error,
                        sqlite3.OperationalError,
                    ):
                        require_trade_inbox_store_readable(inbox_path)
                    return control_unavailable(
                        batch_lease_error,
                        provider_claim_count=provider_claim_count,
                        provider_attempt_count=provider_call_count,
                        provider_results=provider_results,
                    )
            provider_attempted = False
            state = states[case_id]
            candidate = current_candidates.get(case_id)
            read_model = current_models.get(case_id)
            validation_result = validation_results.get(case_id)
            provider_scope = str(
                state.get("provider_input_scope_fingerprint") or ""
            )
            reconciliation = validation_result
            semantic_fingerprint = None
            if (
                candidate is None
                or not isinstance(read_model, dict)
                or str(
                    (validation_result or {}).get("status") or ""
                )
                != "observation_required"
            ):
                outcome: SettlementAttemptOutcome = (
                    SettlementAttemptOutcome(
                        kind="stale_generation",
                        source_id=source_id,
                        account=account,
                        case_id=case_id,
                        contract_version=(
                            collector.contract.contract_version
                        ),
                        capability_fingerprint=(
                            collector.capability.capability_fingerprint
                        ),
                        reason_code="provider_requirement_changed",
                        error_class="stale_generation",
                    )
                )
                lifecycle_case: dict[str, Any] | None = None
            else:
                lifecycle_case = dict(
                    candidate.get("lifecycle_case") or {}
                )
                current_scope = case_scope_fingerprint(candidate)
                provider_scope = provider_input_scope_fingerprint(
                    lifecycle_case=lifecycle_case,
                    read_model=read_model,
                )
                if (
                    current_scope
                    != str(state.get("case_scope_fingerprint") or "")
                    or provider_scope
                    != str(
                        state.get(
                            "provider_input_scope_fingerprint"
                        )
                        or ""
                    )
                ):
                    outcome = SettlementAttemptOutcome(
                        kind="stale_generation",
                        source_id=source_id,
                        account=account,
                        case_id=case_id,
                        contract_version=(
                            collector.contract.contract_version
                        ),
                        capability_fingerprint=(
                            collector.capability.capability_fingerprint
                        ),
                        reason_code="provider_input_scope_changed",
                        error_class="stale_generation",
                    )
                    lifecycle_case = None
                    reconciliation = None
                else:
                    outcome = SettlementAttemptOutcome(
                        kind="stale_generation",
                        source_id=source_id,
                        account=account,
                        case_id=case_id,
                        contract_version=(
                            collector.contract.contract_version
                        ),
                        capability_fingerprint=(
                            collector.capability.capability_fingerprint
                        ),
                    )

            already_claimed = case_id in preclaimed
            claim_id = preclaimed.get(case_id) or uuid.uuid4().hex
            claim_now_ms = max(
                control_now_ms(),
                int(preclaim_handoff_now_ms.get(case_id) or 0),
            )
            claim_acquired, control_error = (
                _run_settlement_control_operation(
                    inbox_path,
                    claim_settlement_attempt,
                    inbox_path,
                    source_id=source_id,
                    account=account,
                    case_id=case_id,
                    case_scope_fingerprint=str(
                        state.get("case_scope_fingerprint") or ""
                    ),
                    claim_id=claim_id,
                    now_ms=claim_now_ms,
                    lease_ms=_SETTLEMENT_CLAIM_LEASE_MS,
                )
            )
            if control_error is not None:
                return control_unavailable(
                    control_error,
                    provider_claim_count=provider_claim_count,
                    provider_attempt_count=provider_call_count,
                    provider_results=provider_results,
                )
            if not bool(claim_acquired):
                skipped_counts["claimed"] += 1
                continue
            if not already_claimed:
                provider_claim_count += 1

            lease_error: Exception | None = None
            skip_post_refresh = False
            if lifecycle_case is not None:
                lease_guard = _SettlementClaimLeaseGuard(
                    inbox_path=inbox_path,
                    source_id=source_id,
                    account=account,
                    case_id=case_id,
                    case_scope_fingerprint=str(
                        state.get("case_scope_fingerprint") or ""
                    ),
                    claim_id=claim_id,
                    initial_now_ms=claim_now_ms,
                    now_ms_fn=control_now_ms,
                    monotonic_fn=_SETTLEMENT_CLAIM_MONOTONIC_FN,
                    lease_ms=_SETTLEMENT_CLAIM_LEASE_MS,
                    renew_interval_sec=(
                        _SETTLEMENT_CLAIM_RENEW_INTERVAL_SEC
                    ),
                ).start()
                lease_guard_start_error = lease_guard.error
                try:
                    if lease_guard_start_error is not None:
                        skip_post_refresh = True
                        raise lease_guard_start_error
                    provider_attempted = True
                    provider_call_count += 1
                    metrics["collector_attempt_count"] += 1
                    outcome = collector.collect_outcome(
                        lifecycle_case,
                        read_model,
                    )
                    reconciliation = None
                    semantic_fingerprint = None
                    lease_error = lease_guard.renew_now()
                    if (
                        lease_error is None
                        and isinstance(outcome.observation, dict)
                    ):
                        semantic_fingerprint = str(
                            outcome.observation.get(
                                "semantic_fingerprint"
                            )
                            or ""
                        ).strip() or None
                        try:
                            reconciliation = (
                                reconcile_lifecycle_close_reason(
                                    repo,
                                    case_id=case_id,
                                    now_ms=int(now_ms),
                                    observation=dict(
                                        outcome.observation
                                    ),
                                    apply_changes=True,
                                    coherent_facts=(
                                        read_model.get(
                                            SETTLEMENT_OBSERVATION_CONTEXT_KEY
                                        )
                                        if isinstance(read_model, dict)
                                        else None
                                    ),
                                    refresh_read_model=False,
                                )
                            )
                        except LifecycleObservationGenerationChanged:
                            outcome = SettlementAttemptOutcome(
                                kind="stale_generation",
                                source_id=source_id,
                                account=account,
                                case_id=case_id,
                                contract_version=(
                                    collector.contract.contract_version
                                ),
                                capability_fingerprint=(
                                    collector.capability.capability_fingerprint
                                ),
                                reason_code=(
                                    "lifecycle_generation_changed"
                                ),
                                error_class="stale_generation",
                            )
                            reconciliation = None
                            semantic_fingerprint = None
                        except LegacySettlementSemanticUnavailable:
                            outcome = SettlementAttemptOutcome(
                                kind="legacy_semantic_unavailable",
                                source_id=source_id,
                                account=account,
                                case_id=case_id,
                                contract_version=(
                                    collector.contract.contract_version
                                ),
                                capability_fingerprint=(
                                    collector.capability.capability_fingerprint
                                ),
                                reason_code=(
                                    "legacy_semantic_unavailable"
                                ),
                                error_class=(
                                    "canonical_evidence_unavailable"
                                ),
                            )
                            reconciliation = None
                            semantic_fingerprint = None
                        except SettlementAdmissionStateIncoherent:
                            outcome = SettlementAttemptOutcome(
                                kind="unknown_error",
                                source_id=source_id,
                                account=account,
                                case_id=case_id,
                                contract_version=(
                                    collector.contract.contract_version
                                ),
                                capability_fingerprint=(
                                    collector.capability.capability_fingerprint
                                ),
                                reason_code=(
                                    "settlement_admission_state_incoherent"
                                ),
                                error_class="canonical_state",
                            )
                            reconciliation = None
                            semantic_fingerprint = None
                        except SettlementSemanticUnavailable:
                            outcome = SettlementAttemptOutcome(
                                kind="unknown_error",
                                source_id=source_id,
                                account=account,
                                case_id=case_id,
                                contract_version=(
                                    collector.contract.contract_version
                                ),
                                capability_fingerprint=(
                                    collector.capability.capability_fingerprint
                                ),
                                reason_code=(
                                    "current_semantic_unavailable"
                                ),
                                error_class="semantic_contract",
                            )
                            reconciliation = None
                            semantic_fingerprint = None
                        except ValueError as exc:
                            if str(exc) != (
                                "lifecycle generation compare-and-set failed"
                            ):
                                raise
                            outcome = SettlementAttemptOutcome(
                                kind="stale_generation",
                                source_id=source_id,
                                account=account,
                                case_id=case_id,
                                contract_version=(
                                    collector.contract.contract_version
                                ),
                                capability_fingerprint=(
                                    collector.capability.capability_fingerprint
                                ),
                                reason_code=(
                                    "lifecycle_generation_changed"
                                ),
                                error_class="stale_generation",
                            )
                            reconciliation = None
                            semantic_fingerprint = None
                        admission = _find_admission_status(
                            reconciliation
                        )
                        if admission == "admitted_semantic":
                            metrics["semantic_admission_count"] += 1
                        elif admission == "duplicate_semantic":
                            metrics["semantic_duplicate_count"] += 1
                        lease_error = lease_guard.renew_now()
                except Exception as exc:
                    outcome = _settlement_processing_failure_outcome(
                        collector=collector,
                        source_id=source_id,
                        account=account,
                        case_id=case_id,
                        reason_code=(
                            "settlement_attempt_lease_guard_failed"
                            if exc is lease_guard_start_error
                            else "settlement_attempt_processing_failed"
                        ),
                        error=exc,
                    )
                    reconciliation = None
                    semantic_fingerprint = None
                    if (
                        lease_error is None
                        and lease_guard_start_error is None
                    ):
                        lease_error = lease_guard.renew_now()
                finally:
                    stopped_error = lease_guard.stop()
                    if (
                        lease_error is None
                        and lease_guard_start_error is None
                    ):
                        lease_error = stopped_error

            if lease_error is not None:
                if isinstance(lease_error, sqlite3.OperationalError):
                    require_trade_inbox_store_readable(inbox_path)
                elif not isinstance(
                    lease_error,
                    SettlementAttemptClaimOwnershipLost,
                ):
                    raise lease_error
                provider_results.append(
                    {
                        "case_id": case_id,
                        "outcome": outcome.to_dict(
                            include_observation=False
                        ),
                        "semantic_fingerprint": semantic_fingerprint,
                        "admission_status": _find_admission_status(
                            reconciliation
                        ),
                    }
                )
                return control_unavailable(
                    lease_error,
                    provider_claim_count=provider_claim_count,
                    provider_attempt_count=provider_call_count,
                    provider_results=provider_results,
                )

            provider_result = {
                "case_id": case_id,
                "outcome": outcome.to_dict(
                    include_observation=False
                ),
                "semantic_fingerprint": semantic_fingerprint,
                "admission_status": _find_admission_status(
                    reconciliation
                ),
            }
            post_lease_error: Exception | None = None
            post_refresh_error: Exception | None = None
            post_candidates: dict[str, dict[str, Any]] = {}
            if not skip_post_refresh:
                post_guard = _SettlementClaimLeaseGuard(
                    inbox_path=inbox_path,
                    source_id=source_id,
                    account=account,
                    case_id=case_id,
                    case_scope_fingerprint=str(
                        state.get("case_scope_fingerprint") or ""
                    ),
                    claim_id=claim_id,
                    initial_now_ms=claim_now_ms,
                    now_ms_fn=control_now_ms,
                    monotonic_fn=_SETTLEMENT_CLAIM_MONOTONIC_FN,
                    lease_ms=_SETTLEMENT_CLAIM_LEASE_MS,
                    renew_interval_sec=(
                        _SETTLEMENT_CLAIM_RENEW_INTERVAL_SEC
                    ),
                ).start()
                post_guard_start_error = post_guard.error
                try:
                    if post_guard_start_error is not None:
                        raise post_guard_start_error
                    post_lease_error = post_guard.renew_now()
                    if post_lease_error is None:
                        post_candidates = _due_candidates_by_id(
                            list_trade_lifecycle_due_candidates(
                                repo,
                                account=account,
                            )
                        )
                        post_lease_error = post_guard.renew_now()
                except Exception as exc:
                    post_refresh_error = exc
                finally:
                    stopped_error = post_guard.stop()
                    if (
                        post_lease_error is None
                        and post_guard_start_error is None
                    ):
                        post_lease_error = stopped_error
            if post_lease_error is not None:
                if isinstance(post_lease_error, sqlite3.OperationalError):
                    require_trade_inbox_store_readable(inbox_path)
                elif not isinstance(
                    post_lease_error,
                    SettlementAttemptClaimOwnershipLost,
                ):
                    raise post_lease_error
                provider_results.append(provider_result)
                return control_unavailable(
                    post_lease_error,
                    provider_claim_count=provider_claim_count,
                    provider_attempt_count=provider_call_count,
                    provider_results=provider_results,
                )

            if post_refresh_error is not None:
                outcome = _settlement_processing_failure_outcome(
                    collector=collector,
                    source_id=source_id,
                    account=account,
                    case_id=case_id,
                    reason_code="settlement_attempt_refresh_failed",
                    error=post_refresh_error,
                )
                provider_result = {
                    "case_id": case_id,
                    "outcome": outcome.to_dict(
                        include_observation=False
                    ),
                    "semantic_fingerprint": semantic_fingerprint,
                    "admission_status": _find_admission_status(
                        reconciliation
                    ),
                }

            prior_state = dict(state)
            post_candidate = post_candidates.get(case_id)
            try:
                post_scope = (
                    case_scope_fingerprint(post_candidate)
                    if post_candidate is not None
                    else str(
                        prior_state.get("case_scope_fingerprint") or ""
                    )
                )
            except Exception as exc:
                outcome = _settlement_processing_failure_outcome(
                    collector=collector,
                    source_id=source_id,
                    account=account,
                    case_id=case_id,
                    reason_code="settlement_attempt_completion_failed",
                    error=exc,
                )
                post_scope = str(
                    prior_state.get("case_scope_fingerprint") or ""
                )
                provider_result = {
                    "case_id": case_id,
                    "outcome": outcome.to_dict(
                        include_observation=False
                    ),
                    "semantic_fingerprint": semantic_fingerprint,
                    "admission_status": _find_admission_status(
                        reconciliation
                    ),
                }
            _completed, control_error, completed_outcome = complete_claim(
                case_id=case_id,
                claim_id=claim_id,
                state=prior_state,
                outcome=outcome,
                case_scope=post_scope,
                provider_scope=provider_scope,
                semantic_fingerprint=semantic_fingerprint,
                provider_attempted=provider_attempted,
            )
            if completed_outcome is not outcome:
                outcome = completed_outcome
                provider_result = {
                    "case_id": case_id,
                    "outcome": outcome.to_dict(
                        include_observation=False
                    ),
                    "semantic_fingerprint": semantic_fingerprint,
                    "admission_status": _find_admission_status(
                        reconciliation
                    ),
                }
            if control_error is not None:
                provider_results.append(provider_result)
                return control_unavailable(
                    control_error,
                    provider_claim_count=provider_claim_count,
                    provider_attempt_count=provider_call_count,
                    provider_results=provider_results,
                )
            provider_results.append(provider_result)
            batch_lease_error = provider_batch_lease.renew_now()
            if batch_lease_error is not None:
                if isinstance(
                    batch_lease_error,
                    sqlite3.OperationalError,
                ):
                    require_trade_inbox_store_readable(inbox_path)
                return control_unavailable(
                    batch_lease_error,
                    provider_claim_count=provider_claim_count,
                    provider_attempt_count=provider_call_count,
                    provider_results=provider_results,
                )

    batch_close_error = provider_batch_lease.close()
    if batch_close_error is not None:
        if isinstance(batch_close_error, sqlite3.OperationalError):
            require_trade_inbox_store_readable(inbox_path)
        return control_unavailable(
            batch_close_error,
            provider_claim_count=provider_claim_count,
            provider_attempt_count=provider_call_count,
            provider_results=provider_results,
        )
    control_summary, control_error = _run_settlement_control_operation(
        inbox_path,
        settlement_attempt_summary,
        inbox_path,
        source_id=source_id,
        now_ms=control_now_ms(),
        account=account,
        case_ids=tuple(candidates_by_id),
    )
    if control_error is not None:
        return control_unavailable(
            control_error,
            provider_claim_count=provider_claim_count,
            provider_attempt_count=provider_call_count,
            provider_results=provider_results,
        )
    if not isinstance(control_summary, dict):
        raise TypeError("settlement attempt summary is invalid")
    return {
        "schema_version": "settlement_due_runtime.v1",
        "account": account,
        "source_id": source_id,
        "collector_enabled": collector_enabled,
        "collector_contract_version": _collector_contract_version(
            collector
        ),
        "capability": _collector_capability_status(collector),
        "candidate_count": len(candidates_by_id),
        "planned_case_count": len(needs_plan),
        "provider_claim_count": provider_claim_count,
        "provider_attempt_count": provider_call_count,
        "skipped_counts": skipped_counts,
        "control_status": "ok",
        "control_summary": control_summary,
        "local_reconciliation": local_result,
        "provider_results": provider_results,
        "process_counters": dict(metrics),
    }


def _run_settlement_control_operation(
    inbox_path: Path,
    operation: Any,
    /,
    *args: Any,
    **kwargs: Any,
) -> tuple[Any, Exception | None]:
    try:
        return operation(*args, **kwargs), None
    except SettlementAttemptClaimOwnershipLost as exc:
        return None, exc
    except sqlite3.OperationalError as exc:
        require_trade_inbox_store_readable(inbox_path)
        return None, exc


def _control_store_unavailable_result(
    *,
    account: str,
    source_id: str,
    collector_enabled: bool,
    collector: SettlementObservationCollector | None,
    candidate_count: int,
    planned_case_count: int,
    provider_claim_count: int,
    provider_attempt_count: int,
    control_error: Exception,
    local_result: dict[str, Any],
    metrics: dict[str, int],
    provider_results: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": "settlement_due_runtime.v1",
        "account": account,
        "source_id": source_id,
        "collector_enabled": collector_enabled,
        "collector_contract_version": _collector_contract_version(
            collector
        ),
        "capability": _collector_capability_status(collector),
        "candidate_count": int(candidate_count),
        "planned_case_count": int(planned_case_count),
        "provider_claim_count": int(provider_claim_count),
        "provider_attempt_count": int(provider_attempt_count),
        "control_status": (
            "claim_ownership_lost"
            if isinstance(
                control_error,
                SettlementAttemptClaimOwnershipLost,
            )
            else "control_store_unavailable"
        ),
        "control_error_class": type(control_error).__name__,
        "local_reconciliation": local_result,
        "provider_results": list(provider_results or ()),
        "process_counters": dict(metrics),
    }


def _collector_contract_version(
    collector: SettlementObservationCollector | None,
) -> str:
    return (
        collector.contract.contract_version
        if collector is not None
        else SETTLEMENT_COLLECTOR_CONTRACT_VERSION
    )


def _collector_capability_fingerprint(
    collector: SettlementObservationCollector | None,
) -> str:
    return (
        collector.capability.capability_fingerprint
        if collector is not None
        else "not_inspected"
    )


def _collector_capability_status(
    collector: SettlementObservationCollector | None,
) -> dict[str, Any]:
    if collector is not None:
        return collector.capability.to_dict()
    return {
        "contract_version": SETTLEMENT_COLLECTOR_CONTRACT_VERSION,
        "capability_fingerprint": "not_inspected",
        "inspection_status": "not_required",
        "supported": None,
        "missing_keys": [],
        "capabilities": {},
    }


def _settlement_control_path(source: dict[str, Any]) -> Path:
    explicit = source.get("inbox_path")
    if explicit:
        return Path(explicit)
    state_path = Path(
        source.get("state_path")
        or "output_shared/state/auto_trade_intake_state.json"
    )
    return state_path.with_name("trade_intake_inbox.sqlite3")


def _due_candidates_by_id(
    candidates: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        lifecycle_case = candidate.get("lifecycle_case")
        if not isinstance(lifecycle_case, dict):
            continue
        case_id = str(lifecycle_case.get("case_id") or "").strip()
        if case_id:
            output[case_id] = dict(candidate)
    return output


def _results_by_case(
    result: dict[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    return {
        str(item.get("case_id") or "").strip(): dict(item)
        for item in (result or {}).get("results") or []
        if isinstance(item, dict)
        and str(item.get("case_id") or "").strip()
    }


def _active_claim(
    state: dict[str, Any] | None,
    *,
    now_ms: int,
) -> bool:
    return bool(
        isinstance(state, dict)
        and str(state.get("claim_id") or "")
        and int(state.get("claim_until_ms") or 0) > int(now_ms)
    )


def _plan_due_cases(
    repo: Any,
    *,
    account: str,
    case_ids: tuple[str, ...],
    now_ms: int,
    apply_changes: bool,
) -> dict[str, Any]:
    if not case_ids:
        return {
            "schema_version": "due_lifecycle_reconciliation.v2",
            "account": account,
            "now_ms": int(now_ms),
            "apply_changes": bool(apply_changes),
            "case_count": 0,
            "results": [],
        }
    read_models = lifecycle_case_read_models_for_account(
        repo,
        account=account,
        now_ms=int(now_ms),
    )
    return reconcile_due_lifecycle_cases(
        repo,
        account=account,
        now_ms=int(now_ms),
        apply_changes=apply_changes,
        observation_collector=None,
        case_ids=case_ids,
        prepared_read_models=read_models,
    )


def _local_control_state(
    *,
    source_id: str,
    account: str,
    case_id: str,
    case_scope_fingerprint_value: str,
    collector: SettlementObservationCollector | None,
    read_model: dict[str, Any],
    result: dict[str, Any] | None,
    business_now_ms: int,
    control_now_ms: int,
) -> dict[str, Any]:
    result_model = (
        result.get("lifecycle_read_model")
        if isinstance(result, dict)
        and isinstance(result.get("lifecycle_read_model"), dict)
        else read_model
    )
    return {
        "source_id": source_id,
        "account": account,
        "case_id": case_id,
        "case_scope_fingerprint": case_scope_fingerprint_value,
        "provider_input_scope_fingerprint": None,
        "collector_contract_version": _collector_contract_version(
            collector
        ),
        "capability_fingerprint": _collector_capability_fingerprint(
            collector
        ),
        "classification": "local",
        "outcome_kind": str(
            (result or {}).get("status") or "not_due"
        ).strip(),
        "reason_code": None,
        "provider_code": None,
        "error_class": None,
        "attempt_count": 0,
        "no_progress_count": 0,
        "next_attempt_at_ms": _next_local_recheck_ms(
            result_model,
            now_ms=business_now_ms,
        ),
        "last_attempt_at_ms": None,
        "last_semantic_fingerprint": None,
        "claim_id": None,
        "claim_until_ms": None,
        "updated_at_ms": int(control_now_ms),
    }


def _next_local_recheck_ms(
    read_model: dict[str, Any],
    *,
    now_ms: int,
) -> int | None:
    boundaries: list[int] = []
    for key in ("pairing_until_ms", "pending_until_ms"):
        value = read_model.get(key)
        if value is None:
            continue
        boundary = int(value)
        if boundary > int(now_ms):
            boundaries.append(boundary)
    return min(boundaries) if boundaries else None


def _refresh_capability_scope(
    state: dict[str, Any],
    *,
    collector: SettlementObservationCollector,
    source_id: str,
    account: str,
    case_id: str,
    case_scope_fingerprint_value: str,
    now_ms: int,
) -> dict[str, Any]:
    return prepare_provider_required_state(
        state,
        source_id=source_id,
        account=account,
        case_id=case_id,
        case_scope_fingerprint_value=case_scope_fingerprint_value,
        provider_input_scope_fingerprint_value=str(
            state.get("provider_input_scope_fingerprint") or ""
        ),
        contract_version=collector.contract.contract_version,
        capability_fingerprint=(
            collector.capability.capability_fingerprint
        ),
        now_ms=int(now_ms),
    )


def _collector_scope_matches(
    state: dict[str, Any],
    collector: SettlementObservationCollector,
) -> bool:
    return (
        str(state.get("collector_contract_version") or "")
        == collector.contract.contract_version
        and str(state.get("capability_fingerprint") or "")
        == collector.capability.capability_fingerprint
    )


def _find_admission_status(value: Any) -> str | None:
    if isinstance(value, dict):
        direct = str(value.get("admission_status") or "").strip()
        if direct:
            return direct
        for nested in value.values():
            found = _find_admission_status(nested)
            if found:
                return found
    elif isinstance(value, list):
        for nested in value:
            found = _find_admission_status(nested)
            if found:
                return found
    return None


def _lifecycle_adoption(
    result: dict[str, Any],
) -> dict[str, Any] | None:
    diagnostics = (
        dict(result.get("diagnostics") or {})
        if isinstance(result.get("diagnostics"), dict)
        else {}
    )
    adoption = diagnostics.get("lifecycle_adoption")
    return dict(adoption) if isinstance(adoption, dict) else None


def _registry_contract_metadata(
    payload: dict[str, Any],
    *,
    lifecycle_case: dict[str, Any],
) -> dict[str, Any]:
    raw_code = str(
        payload.get("code")
        or payload.get("stock_code")
        or payload.get("broker_symbol")
        or ""
    ).strip().upper()
    match = OPTION_CODE_RE.match(raw_code)
    if match is None:
        raise ValueError(
            "standard broker option contract class is unproven"
        )
    market = str(match.group("market") or "").strip().upper()
    broker_symbol = canonical_symbol(raw_code)
    case_symbol = canonical_symbol(lifecycle_case.get("symbol"))
    if not broker_symbol or not case_symbol or broker_symbol != case_symbol:
        raise ValueError(
            "broker option code conflicts with lifecycle contract"
        )
    return {
        "market": market,
        "settlement_style": "physical",
        "underlying_security_type": "equity",
        "contract_class": "standard_equity_option",
    }


__all__ = [
    "ensure_lifecycle_timing_after_intake",
    "reconcile_due_lifecycle_cases_for_source",
]
