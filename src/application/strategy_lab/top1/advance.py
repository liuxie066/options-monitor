from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from src.application.strategy_lab.top1.corpus import (
    CorpusError,
    capture_recommendation_point,
    discover_recommendation_points,
    read_market_calendar_binding,
    read_validation_day_source,
    read_validation_point_source,
    seal_committed_day_expectation,
    seal_day_expectation,
)
from src.application.strategy_lab.top1.fill_observation import observe_active_contracts
from src.application.strategy_lab.top1.lifecycle import (
    effective_feature_status,
    read_active_experiment_ids,
    read_advance_context,
    reconcile_disabled_experiments,
    recover_account_terminal_projections,
    terminate_experiment,
)
from src.application.strategy_lab.top1.outcome import (
    conclude_validation,
    settle_due_outcomes,
)
from src.application.strategy_lab.top1.validation import (
    consume_validation_point,
    record_validation_day_gap,
)
from src.infrastructure.strategy_lab.experiment_store import ExperimentStore


ADVANCE_RESULT_SCHEMA = "sell_put_top1_advance_result.v1"
ADVANCE_REVISION = "top1-advance.v1"


def _key(*parts: object) -> str:
    return hashlib.sha256("\0".join(map(str, parts)).encode()).hexdigest()


def _error(exc: Exception) -> dict[str, str]:
    return {
        "reason_code": str(getattr(exc, "reason_code", "advance_failed")),
        "message": str(exc),
    }


def _hk_date(timestamp: str) -> str:
    parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("occurred_at_utc must be timezone-aware")
    return parsed.astimezone(ZoneInfo("Asia/Hong_Kong")).date().isoformat()


def advance_scheduled(
    store: ExperimentStore,
    source_root: str | Path,
    artifact_root: str | Path,
    *,
    market: str,
    account: str,
    load_schedule: Callable[[], Mapping[str, Any]],
    load_readiness: Callable[[], Mapping[str, Any]],
    load_gateway: Callable[[], Any],
    advance_revision: str,
    advance_interval_seconds: int,
    actor: str,
    occurred_at_utc: str,
    idempotency_key: str,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Compose existing Top1 commands once for one account and scheduled instant."""

    feature = effective_feature_status(
        store, market=market, account=account, environ=environ
    )
    result: dict[str, Any] = {
        "schema_version": ADVANCE_RESULT_SCHEMA,
        "market": market.upper(),
        "account": account,
        "occurred_at_utc": occurred_at_utc,
        "feature": feature,
        "corpus": [],
        "readiness": None,
        "experiments": [],
        "recovered_experiment_ids": [],
    }
    invocation_key = _key(idempotency_key, occurred_at_utc)
    if not feature["effective"]:
        scope = "maintainer" if not feature["maintainer_available"] else "user"
        try:
            terminated = reconcile_disabled_experiments(
                store,
                market=market,
                account=account,
                disabled_scope=scope,
                actor=actor,
                occurred_at_utc=occurred_at_utc,
                idempotency_key=_key(invocation_key, "disabled"),
                artifact_root=artifact_root,
            )
        except Exception as exc:
            return {**result, "status": "failed", "error": _error(exc)}
        return {**result, "status": "disabled", "terminated_experiment_ids": terminated}

    experiment_results: dict[str, dict[str, Any]] = {}
    contexts: dict[str, dict[str, object]] = {}
    context_error_ids: set[str] = set()
    had_failure = False

    try:
        active_ids = read_active_experiment_ids(store, market=market, account=account)
    except Exception as exc:
        active_ids = []
        result["account_error"] = _error(exc)
        had_failure = True
    for experiment_id in active_ids:
        experiment_results[experiment_id] = {
            "experiment_id": experiment_id,
            "steps": [],
            "errors": [],
        }
        try:
            contexts[experiment_id] = read_advance_context(
                store, artifact_root, experiment_id=experiment_id
            )
        except Exception as exc:
            experiment_results[experiment_id]["errors"].append(_error(exc))
            context_error_ids.add(experiment_id)
            had_failure = True

    committed_by_date: dict[str, Mapping[str, Any]] = {}
    conflicted_dates: set[str] = set()
    for context in contexts.values():
        for day in context.get("committed_days", []):
            assert isinstance(day, Mapping)
            trading_date = str(day["trading_date"])
            if trading_date in committed_by_date and committed_by_date[trading_date] != day:
                conflicted_dates.add(trading_date)
                result["corpus"].append(
                    {
                        "status": "conflict",
                        "reason_code": "hidden_window_overlap",
                        "trading_date": trading_date,
                    }
                )
                had_failure = True
            else:
                committed_by_date[trading_date] = day
    denominator_unknown = bool(context_error_ids) or any(
        context["phase"] == "validation" and context["behavior_binding_drift"]
        for context in contexts.values()
    )

    schedule: Mapping[str, Any] | None = None
    try:
        schedule = load_schedule()
        if not isinstance(schedule, Mapping):
            raise ValueError("schedule loader must return an object")
    except Exception as exc:
        result["corpus"].append({"operation": "load_schedule", **_error(exc)})
        had_failure = True

    try:
        today = _hk_date(occurred_at_utc)
        committed_day = committed_by_date.get(today)
        if today in conflicted_dates:
            result["corpus"].append(
                {
                    "operation": "seal_day_expectation",
                    "status": "blocked",
                    "reason_code": "hidden_window_overlap",
                    "trading_date": today,
                }
            )
        elif committed_day is not None:
            owner = next(
                context
                for context in contexts.values()
                if committed_day in context.get("committed_days", [])
            )
            commitment = owner["commitment"]
            assert isinstance(commitment, Mapping)
            sealed = seal_committed_day_expectation(
                store,
                artifact_root,
                market=market,
                account=account,
                committed_day=committed_day,
                market_calendar_version=str(commitment["market_calendar_version"]),
                market_calendar_sha256=str(
                    commitment["market_calendar_snapshot_content_sha256"]
                ),
                schedule_config_sha256=str(commitment["schedule_config_sha256"]),
                sealed_at_utc=occurred_at_utc,
                environ=environ,
            )
            result["corpus"].append(sealed)
        elif schedule is not None and not denominator_unknown:
            calendar = read_market_calendar_binding(artifact_root, market=market)
            if not calendar["coverage_start"] <= today <= calendar["coverage_end"]:
                raise CorpusError(
                    "market_calendar_binding_unavailable",
                    "current date is outside market calendar coverage",
                )
            if today in calendar["trading_dates"]:
                result["corpus"].append(
                    seal_day_expectation(
                        store,
                        artifact_root,
                        market=market,
                        account=account,
                        schedule=schedule,
                        trading_date=today,
                        market_calendar_version=str(
                            calendar["market_calendar_version"]
                        ),
                        market_calendar_sha256=str(
                            calendar["snapshot_content_sha256"]
                        ),
                        sealed_at_utc=occurred_at_utc,
                        environ=environ,
                    )
                )
            else:
                result["corpus"].append(
                    {
                        "operation": "seal_day_expectation",
                        "status": "no_op",
                        "reason_code": "market_closed",
                        "trading_date": today,
                    }
                )
        elif schedule is not None:
            result["corpus"].append(
                {
                    "operation": "seal_day_expectation",
                    "status": "blocked",
                    "reason_code": "experiment_preflight_unavailable",
                    "trading_date": today,
                }
            )
    except Exception as exc:
        result["corpus"].append({"operation": "seal_day_expectation", **_error(exc)})
        had_failure = True

    try:
        discovered = discover_recommendation_points(
            source_root, market=market, account=account
        )
        for point in discovered:
            if point["status"] != "available":
                result["corpus"].append(point)
                had_failure = True
                continue
            try:
                result["corpus"].append(
                    capture_recommendation_point(
                        store,
                        source_root,
                        artifact_root,
                        point_ref=str(point["point_ref"]),
                        trading_date=str(point["trading_date"]),
                        captured_at_utc=occurred_at_utc,
                        environ=environ,
                    )
                )
            except Exception as exc:
                result["corpus"].append(
                    {"operation": "capture_recommendation_point", **point, **_error(exc)}
                )
                had_failure = True
    except Exception as exc:
        result["corpus"].append(
            {"operation": "discover_recommendation_points", **_error(exc)}
        )
        had_failure = True

    runtime_ready = False
    try:
        readiness = load_readiness()
        if not isinstance(readiness, Mapping):
            raise ValueError("readiness loader must return an object")
        result["readiness"] = dict(readiness)
        runtime_ready = readiness.get("validation_runtime_ready") is True
    except Exception as exc:
        result["readiness"] = {"validation_runtime_ready": False, **_error(exc)}

    gateway_loaded = False
    gateway: Any = None
    gateway_error: dict[str, str] | None = None
    timer_reported: set[str] = set()

    def provider_for(context: Mapping[str, object]) -> Any:
        nonlocal gateway, gateway_error, gateway_loaded, had_failure
        experiment_id = str(context["experiment_id"])
        timer = context.get("timer_binding")
        timer_matches = isinstance(timer, Mapping) and (
            timer.get("revision") == advance_revision
            and timer.get("advance_cadence_seconds") == advance_interval_seconds
        )
        if experiment_id not in timer_reported and not timer_matches:
            experiment_results[experiment_id]["steps"].append(
                {"operation": "provider_access", "status": "blocked", "reason_code": "timer_binding_mismatch"}
            )
            timer_reported.add(experiment_id)
        if not runtime_ready or not timer_matches:
            return None
        if not gateway_loaded:
            gateway_loaded = True
            try:
                gateway = load_gateway()
            except Exception as exc:
                gateway_error = _error(exc)
                had_failure = True
        return gateway

    for experiment_id in active_ids:
        if experiment_id in context_error_ids:
            continue
        context = contexts[experiment_id]
        steps = experiment_results[experiment_id]["steps"]
        try:
            if context["terminal_mode"] is not None:
                continue
            if context["behavior_binding_drift"]:
                terminate_experiment(
                    store,
                    experiment_id=experiment_id,
                    reason="behavior_binding_drift",
                    disabled_scope=None,
                    actor=actor,
                    occurred_at_utc=occurred_at_utc,
                    idempotency_key=_key(invocation_key, experiment_id, "drift"),
                    artifact_root=artifact_root,
                )
                steps.append({"operation": "terminate", "status": "completed"})
                continue
            if context["phase"] != "validation" or context[
                "validation_progress"
            ] != "collecting_decisions":
                continue
            trading_date = context["open_trading_date"]
            if trading_date is None:
                continue
            if str(trading_date) in conflicted_dates:
                steps.append(
                    {
                        "operation": "collect_validation_day",
                        "status": "blocked",
                        "reason_code": "hidden_window_overlap",
                        "trading_date": trading_date,
                    }
                )
                continue
            day_source = read_validation_day_source(
                store,
                artifact_root,
                market=market,
                account=account,
                trading_date=str(trading_date),
            )
            if day_source["status"] != "available":
                try:
                    gap = record_validation_day_gap(
                        store,
                        artifact_root,
                        experiment_id=experiment_id,
                        trading_date=str(trading_date),
                        actor=actor,
                        occurred_at_utc=occurred_at_utc,
                        idempotency_key=_key(
                            invocation_key, experiment_id, trading_date, "day-gap"
                        ),
                        environ=environ,
                    )
                except Exception as exc:
                    if getattr(exc, "reason_code", None) == "validation_deadline_not_reached":
                        steps.append(
                            {
                                "operation": "record_validation_day_gap",
                                "status": "pending",
                            }
                        )
                        continue
                    raise
                steps.append({"operation": "record_validation_day_gap", **gap})
                continue
            expectation = day_source["expectation"]
            assert isinstance(expectation, Mapping)
            expected_ids = list(expectation["expected_recommendation_point_ids"])
            consumed_ids = list(context["consumed_point_ids"])
            if consumed_ids != expected_ids[: len(consumed_ids)]:
                raise ValueError("consumed validation points are not an expectation prefix")
            last_available = context["last_consumed_available_point_id"]
            if last_available is not None:
                observed = observe_active_contracts(
                    store,
                    artifact_root,
                    experiment_id=experiment_id,
                    observed_recommendation_point_id=str(last_available),
                    gateway=provider_for(context),
                    actor=actor,
                    occurred_at_utc=occurred_at_utc,
                    idempotency_key=_key(
                        invocation_key, experiment_id, last_available, "observe"
                    ),
                    environ=environ,
                )
                steps.append({"operation": "observe_active_contracts", **observed})
            for point_id in expected_ids[len(consumed_ids) :]:
                source = read_validation_point_source(
                    store,
                    artifact_root,
                    market=market,
                    account=account,
                    trading_date=str(trading_date),
                    recommendation_point_id=str(point_id),
                )
                source_status = (
                    "missing_after_deadline"
                    if source["status"] == "missing"
                    else source["status"]
                )
                try:
                    consumed = consume_validation_point(
                        store,
                        artifact_root,
                        experiment_id=experiment_id,
                        recommendation_point_id=str(point_id),
                        source_status=str(source_status),
                        actor=actor,
                        occurred_at_utc=occurred_at_utc,
                        idempotency_key=_key(
                            invocation_key, experiment_id, point_id, "consume"
                        ),
                        environ=environ,
                    )
                except Exception as exc:
                    if getattr(exc, "reason_code", None) == "validation_deadline_not_reached":
                        steps.append(
                            {
                                "operation": "consume_validation_point",
                                "status": "pending",
                                "recommendation_point_id": point_id,
                            }
                        )
                        break
                    raise
                steps.append({"operation": "consume_validation_point", **consumed})
                if source_status == "available":
                    observed = observe_active_contracts(
                        store,
                        artifact_root,
                        experiment_id=experiment_id,
                        observed_recommendation_point_id=str(point_id),
                        gateway=provider_for(context),
                        actor=actor,
                        occurred_at_utc=occurred_at_utc,
                        idempotency_key=_key(
                            invocation_key, experiment_id, point_id, "observe"
                        ),
                        environ=environ,
                    )
                    steps.append({"operation": "observe_active_contracts", **observed})
        except Exception as exc:
            experiment_results[experiment_id]["errors"].append(_error(exc))
            had_failure = True

    try:
        due_ids = read_active_experiment_ids(store, market=market, account=account)
    except Exception as exc:
        due_ids = []
        result["due_error"] = _error(exc)
        had_failure = True
    for experiment_id in due_ids:
        if experiment_id in context_error_ids:
            continue
        experiment_results.setdefault(
            experiment_id, {"experiment_id": experiment_id, "steps": [], "errors": []}
        )
        try:
            context = read_advance_context(
                store, artifact_root, experiment_id=experiment_id
            )
            if context["behavior_binding_drift"] or context["phase"] != "validation":
                continue
            if context["validation_progress"] not in {
                "collecting_decisions",
                "awaiting_outcomes",
            }:
                continue
            settled = settle_due_outcomes(
                store,
                artifact_root,
                experiment_id=experiment_id,
                gateway=(provider_for(context) if context["has_outcome_jobs"] else None),
                actor=actor,
                occurred_at_utc=occurred_at_utc,
                idempotency_key=_key(invocation_key, experiment_id, "settle"),
                environ=environ,
            )
            experiment_results[experiment_id]["steps"].append(
                {"operation": "settle_due_outcomes", **settled}
            )
        except Exception as exc:
            experiment_results[experiment_id]["errors"].append(_error(exc))
            had_failure = True

    try:
        conclude_ids = read_active_experiment_ids(store, market=market, account=account)
    except Exception as exc:
        conclude_ids = []
        result["conclusion_error"] = _error(exc)
        had_failure = True
    for experiment_id in conclude_ids:
        if experiment_id in context_error_ids:
            continue
        try:
            context = read_advance_context(
                store, artifact_root, experiment_id=experiment_id
            )
            if context["validation_progress"] != "ready_to_conclude":
                continue
            concluded = conclude_validation(
                store,
                artifact_root,
                experiment_id=experiment_id,
                actor=actor,
                occurred_at_utc=occurred_at_utc,
                idempotency_key=_key(invocation_key, experiment_id, "conclude"),
                environ=environ,
            )
            experiment_results[experiment_id]["steps"].append(
                {"operation": "conclude_validation", **concluded}
            )
        except Exception as exc:
            experiment_results[experiment_id]["errors"].append(_error(exc))
            had_failure = True

    try:
        result["recovered_experiment_ids"] = recover_account_terminal_projections(
            store, artifact_root, market=market, account=account
        )
    except Exception as exc:
        result["recovery_error"] = _error(exc)
        had_failure = True
    if gateway_error is not None:
        result["gateway_error"] = gateway_error
    for item in experiment_results.values():
        item["status"] = "failed" if item["errors"] else "ok"
    result["experiments"] = [experiment_results[key] for key in sorted(experiment_results)]
    result["status"] = "partial" if had_failure else "ok"
    return result


__all__ = ["ADVANCE_RESULT_SCHEMA", "ADVANCE_REVISION", "advance_scheduled"]
