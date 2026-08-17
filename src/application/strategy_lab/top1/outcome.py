from __future__ import annotations

import json
import math
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NoReturn, cast

from domain.domain.decision_state_fingerprint import canonical_sha256
from src.application.strategy_lab.top1.contracts import (
    EXPIRY_OUTCOME_CONTRACT_VERSION,
    Top1CoreContractError,
    VALIDATION_REQUIRED_DAYS,
    validate_experiment_spec,
)
from src.application.strategy_lab.top1.corpus import (
    CorpusError,
    read_validation_day_source,
    read_validation_point_source,
)
from src.application.strategy_lab.top1.economics import calculate_expiry_efficiency
from src.application.strategy_lab.top1.lifecycle import (
    Top1LifecycleError,
    _call,
    _command_fields,
    _derived_key,
    _recover_projection,
    _require_effective,
    _segment,
)
from src.application.strategy_lab.top1.statistics import (
    summarize_paired_daily_deltas,
)
from src.application.strategy_lab.top1.terminal_projection import (
    Publisher,
    build_completed_receipt_request,
    build_generation_terminal_request,
)
from src.application.strategy_lab.top1.validation import (
    _generation,
    _revision_request,
    _utc,
)
from src.infrastructure.futu_gateway import FutuGatewayDataContractError
from src.infrastructure.strategy_lab.experiment_store import (
    ExperimentStore,
    compact_json,
)


OUTCOME_REVISION_SCHEMA = "sell_put_top1_outcome_revision.v1"


class Top1OutcomeError(ValueError):
    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


def _fail(reason_code: str, message: str) -> NoReturn:
    raise Top1OutcomeError(reason_code, message)


def _spec(experiment: Mapping[str, Any]) -> dict[str, object]:
    try:
        return validate_experiment_spec(json.loads(str(experiment["spec_json"])))
    except (json.JSONDecodeError, Top1CoreContractError) as exc:
        raise Top1OutcomeError("experiment_conflict", "experiment spec is invalid") from exc


def _pending_update(
    job: Mapping[str, Any],
    *,
    status: str | None = None,
    reason_code: str | None = None,
    terms_point_id: str | None = None,
    terms: Mapping[str, object] | None = None,
    result: Mapping[str, object] | None = None,
    occurred_at_utc: str,
) -> dict[str, object]:
    return {
        "target_point_id": job["target_point_id"],
        "arm": job["arm"],
        "expected_status": job["status"],
        "status": status or job["status"],
        "terms_point_id": terms_point_id,
        "terms_json": compact_json(terms) if terms is not None else None,
        "terms_sha256": canonical_sha256(terms) if terms is not None else None,
        "result_json": compact_json(result) if result is not None else None,
        "result_sha256": canonical_sha256(result) if result is not None else None,
        "reason_code": reason_code,
        "last_attempt_at_utc": occurred_at_utc,
    }


def _attempt_replayed(
    events: list[dict[str, Any]],
    *,
    idempotency_key: str,
    actor: str,
    occurred_at_utc: str,
) -> bool:
    event = next(
        (
            item
            for item in events
            if item["command_scope"]
            == f"experiment:{item['experiment_id']}:outcome"
            and item["idempotency_key"] == idempotency_key
        ),
        None,
    )
    if event is None:
        return False
    if event["actor"] != actor or event["occurred_at_utc"] != occurred_at_utc:
        _fail("idempotency_conflict", "outcome attempt identity changed")
    return True


def _commit_outcome(
    store: ExperimentStore,
    artifact_root: str | Path,
    *,
    experiment_id: str,
    updates: list[dict[str, object]],
    close_fact: Mapping[str, object] | None,
    actor: str,
    occurred_at_utc: str,
    idempotency_key: str,
) -> dict[str, Any]:
    experiment = _call(store.experiment, experiment_id)
    generation = _generation(store, experiment_id, "outcome")
    revision_args, _post = _revision_request(
        artifact_root,
        experiment_id=experiment_id,
        generation=generation,
        mutation={
            "operation": "settle_due_outcomes",
            "job_updates": updates,
            "close_fact_sha256": close_fact.get("fact_sha256") if close_fact else None,
        },
        occurred_at_utc=occurred_at_utc,
        schema_version=OUTCOME_REVISION_SCHEMA,
    )
    return cast(
        dict[str, Any],
        _call(
            store.commit_outcome_batch,
            experiment_id=experiment_id,
            expected_state_version=int(experiment["state_version"]),
            job_updates=updates,
            close_fact=close_fact,
            actor=actor,
            occurred_at_utc=occurred_at_utc,
            idempotency_key=idempotency_key,
            **revision_args,
        ),
    )


def _terms_match(terms: Mapping[str, Any], job_payload: Mapping[str, Any]) -> bool:
    return (
        str(terms.get("contract_symbol")) == str(job_payload["contract_symbol"]).upper()
        and str(terms.get("stock_owner")) == str(job_payload["stock_owner"]).upper()
        and terms.get("expiration") == job_payload["expiration"]
        and terms.get("option_type") == "PUT"
        and terms.get("option_standard_type") == "STANDARD"
        and float(cast(float, terms.get("strike"))) == float(job_payload["strike"])
        and int(cast(int, terms.get("multiplier"))) == int(job_payload["multiplier"])
        and str(terms.get("currency")) == str(job_payload["currency"]).upper()
    )


def _terms_source(
    store: ExperimentStore,
    artifact_root: str | Path,
    *,
    experiment: Mapping[str, Any],
    expiration: str,
) -> dict[str, Any] | None:
    try:
        day = read_validation_day_source(
            store,
            artifact_root,
            market=str(experiment["market"]),
            account=str(experiment["account"]),
            trading_date=expiration,
        )
        if day["status"] != "available":
            return None
        expectation = cast(Mapping[str, Any], day["expectation"])
        point_id = cast(list[str], expectation["expected_recommendation_point_ids"])[-1]
        point = read_validation_point_source(
            store,
            artifact_root,
            market=str(experiment["market"]),
            account=str(experiment["account"]),
            trading_date=expiration,
            recommendation_point_id=point_id,
        )
    except (CorpusError, IndexError):
        return None
    return point if point["status"] == "available" else None


def _calendar_has_date(value: object, expiration: str) -> bool:
    if isinstance(value, list):
        raw_rows = value
    else:
        to_dict = getattr(value, "to_dict", None)
        if not callable(to_dict):
            return False
        try:
            raw_rows = to_dict(orient="records")
        except (TypeError, ValueError):
            return False
    if not isinstance(raw_rows, list):
        return False
    dates: set[str] = set()
    for row in raw_rows:
        if isinstance(row, str):
            dates.add(row[:10])
        elif isinstance(row, Mapping):
            value = row.get("time") or row.get("date") or row.get("trading_date")
            if isinstance(value, str):
                dates.add(value[:10])
    return dates == {expiration}


def _close_result(
    job: Mapping[str, Any], close: float
) -> tuple[dict[str, object], dict[str, object]]:
    payload = json.loads(str(job["job_json"]))
    economic = calculate_expiry_efficiency(
        {
            "stage": "validation",
            "fill_status": "observed_fill",
            "holding_start_date": payload["fill_date"],
            "expiration": payload["expiration"],
            "opening_net_premium": payload["opening_net_premium"],
            "net_cash_basis": payload["net_cash_basis"],
            "strike": payload["strike"],
            "multiplier": payload["multiplier"],
            "underlier_close": close,
            "account_fee_plan": payload["account_fee_plan"],
        }
    )
    result = {
        "target_point_id": job["target_point_id"],
        "arm": job["arm"],
        "close": close,
        "economics": economic,
    }
    return result, payload


def settle_due_outcomes(
    store: ExperimentStore,
    artifact_root: str | Path,
    *,
    experiment_id: str,
    gateway: Any,
    actor: str,
    occurred_at_utc: str,
    idempotency_key: str,
    environ: Mapping[str, str] | None = None,
) -> dict[str, object]:
    experiment_id = _segment(experiment_id, "experiment_id")
    actor, occurred_at_utc, idempotency_key = _command_fields(
        actor, occurred_at_utc, idempotency_key
    )
    experiment = _call(store.experiment, experiment_id)
    _require_effective(
        store,
        market=str(experiment["market"]),
        account=str(experiment["account"]),
        actor=actor,
        occurred_at_utc=occurred_at_utc,
        idempotency_key=idempotency_key,
        artifact_root=artifact_root,
        environ=environ,
    )
    if experiment["terminal_mode"] is not None:
        return {"status": "terminal", "processed": 0}
    now = _utc(occurred_at_utc)
    processed = 0
    committed_events = _call(store.events, experiment_id)
    initial_jobs = _call(store.outcome_jobs, experiment_id)
    terms_groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for job in initial_jobs:
        if job["status"] == "pending_terms":
            terms_groups.setdefault(
                (str(job["stock_owner"]), str(job["expiration"]), str(job["contract_symbol"])),
                [],
            ).append(job)
    for (stock_owner, expiration, contract_symbol), jobs in terms_groups.items():
        due = _utc(str(jobs[0]["due_at_utc"]))
        group_key = canonical_sha256([stock_owner, expiration, contract_symbol])
        attempt_key = _derived_key(idempotency_key, "terms", group_key)
        if _attempt_replayed(
            committed_events,
            idempotency_key=attempt_key,
            actor=actor,
            occurred_at_utc=occurred_at_utc,
        ):
            processed += len(jobs)
            continue
        if now >= due:
            updates = [
                _pending_update(
                    job,
                    status="outcome_unavailable",
                    reason_code="expiry_terms_unavailable_at_due",
                    occurred_at_utc=occurred_at_utc,
                )
                for job in jobs
            ]
        else:
            source = _terms_source(
                store,
                artifact_root,
                experiment=experiment,
                expiration=expiration,
            )
            if source is None:
                continue
            point_row = cast(Mapping[str, Any], source["point_row"])
            try:
                raw_terms = gateway.get_exact_expiration_option_terms(
                    code=stock_owner,
                    expiration=expiration,
                    contract_symbol=contract_symbol,
                )
            except FutuGatewayDataContractError:
                updates = [
                    _pending_update(
                        job,
                        status="outcome_unavailable",
                        reason_code="expiry_terms_conflict",
                        occurred_at_utc=occurred_at_utc,
                    )
                    for job in jobs
                ]
            except Exception:
                updates = [
                    _pending_update(
                        job,
                        reason_code="expiry_terms_provider_retryable",
                        occurred_at_utc=occurred_at_utc,
                    )
                    for job in jobs
                ]
            else:
                terms = dict(raw_terms) if isinstance(raw_terms, Mapping) else None
                valid = terms is not None
                if valid:
                    try:
                        valid = all(
                            _terms_match(terms, json.loads(str(job["job_json"])))
                            for job in jobs
                        )
                    except (KeyError, TypeError, ValueError):
                        valid = False
                if not valid:
                    updates = [
                        _pending_update(
                            job,
                            status="outcome_unavailable",
                            reason_code="expiry_terms_conflict",
                            occurred_at_utc=occurred_at_utc,
                        )
                        for job in jobs
                    ]
                else:
                    assert terms is not None
                    receipt = {
                        "captured_at_utc": occurred_at_utc,
                        "terms_point_id": point_row["recommendation_point_id"],
                        "source_ref": point_row["projection_ref"],
                        "source_content_sha256": point_row[
                            "projection_content_sha256"
                        ],
                        "terms": terms,
                    }
                    updates = [
                        _pending_update(
                            job,
                            status="pending_outcome",
                            terms_point_id=str(point_row["recommendation_point_id"]),
                            terms=receipt,
                            occurred_at_utc=occurred_at_utc,
                        )
                        for job in jobs
                    ]
        _commit_outcome(
            store,
            artifact_root,
            experiment_id=experiment_id,
            updates=updates,
            close_fact=None,
            actor=actor,
            occurred_at_utc=occurred_at_utc,
            idempotency_key=attempt_key,
        )
        processed += len(updates)

    current_jobs = _call(store.outcome_jobs, experiment_id)
    close_groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for job in current_jobs:
        if job["status"] == "pending_outcome":
            close_groups.setdefault(
                (str(job["stock_owner"]), str(job["expiration"])), []
            ).append(job)
    for (stock_owner, expiration), jobs in close_groups.items():
        due = _utc(str(jobs[0]["due_at_utc"]))
        if now < due:
            continue
        group_key = canonical_sha256([stock_owner, expiration])
        attempt_key = _derived_key(idempotency_key, "close", group_key)
        if _attempt_replayed(
            committed_events,
            idempotency_key=attempt_key,
            actor=actor,
            occurred_at_utc=occurred_at_utc,
        ):
            processed += len(jobs)
            continue
        deadline = min(_utc(str(job["deadline_at_utc"])) for job in jobs)
        fact = _call(
            store.expiry_close_fact,
            experiment_id,
            stock_owner,
            expiration,
            EXPIRY_OUTCOME_CONTRACT_VERSION,
        )
        close_value: float | None = None
        provider_failed = False
        if fact is not None and fact["status"] == "available":
            close_value = float(json.loads(str(fact["fact_json"]))["close"])
        elif fact is None:
            try:
                calendar = gateway.get_trading_days(
                    market=str(experiment["market"]),
                    start=expiration,
                    end=expiration,
                )
                if not _calendar_has_date(calendar, expiration):
                    raise ValueError("expiration is absent from exact calendar response")
                close = gateway.get_exact_expiration_close(
                    code=stock_owner,
                    expiration=expiration,
                )
                if not isinstance(close, Mapping):
                    raise ValueError("exact expiration close is unavailable")
                close_value = float(close["close"])
                if not math.isfinite(close_value) or close_value <= 0:
                    raise ValueError("exact expiration close is invalid")
            except Exception:
                provider_failed = True
        if provider_failed or close_value is None:
            terminal = now >= deadline
            updates = [
                _pending_update(
                    job,
                    status="outcome_unavailable" if terminal else None,
                    reason_code=(
                        "expiry_close_unavailable"
                        if terminal
                        else "expiry_close_provider_retryable"
                    ),
                    occurred_at_utc=occurred_at_utc,
                )
                for job in jobs
            ]
            close_fact = None
        else:
            fact_payload = {
                "stock_owner": stock_owner,
                "expiration": expiration,
                "contract_version": EXPIRY_OUTCOME_CONTRACT_VERSION,
                "close": close_value,
                "captured_at_utc": occurred_at_utc,
                "ktype": "K_DAY",
                "autype": "NONE",
            }
            close_fact = (
                None
                if fact is not None
                else {
                    **fact_payload,
                    "status": "available",
                    "fact_json": compact_json(fact_payload),
                    "fact_sha256": canonical_sha256(fact_payload),
                    "created_at_utc": occurred_at_utc,
                }
            )
            updates = []
            for job in jobs:
                try:
                    result, _payload = _close_result(job, close_value)
                except (KeyError, TypeError, ValueError):
                    result = {"economics": {"status": "not_evaluable"}}
                economic = cast(Mapping[str, Any], result["economics"])
                updates.append(
                    _pending_update(
                        job,
                        status=(
                            "evaluable"
                            if economic.get("status") == "evaluable"
                            else "outcome_unavailable"
                        ),
                        reason_code=(
                            None
                            if economic.get("status") == "evaluable"
                            else "required_outcome_missing"
                        ),
                        result=(result if economic.get("status") == "evaluable" else None),
                        occurred_at_utc=occurred_at_utc,
                    )
                )
        _commit_outcome(
            store,
            artifact_root,
            experiment_id=experiment_id,
            updates=updates,
            close_fact=close_fact,
            actor=actor,
            occurred_at_utc=occurred_at_utc,
            idempotency_key=attempt_key,
        )
        processed += len(updates)
    return {
        "status": "processed" if processed else "pending",
        "processed": processed,
        "pending": sum(
            job["status"] in {"pending_terms", "pending_outcome"}
            for job in _call(store.outcome_jobs, experiment_id)
        ),
    }


def _statistics_rows(
    store: ExperimentStore, experiment_id: str
) -> tuple[list[dict[str, object]], bool]:
    jobs = {
        (str(job["target_point_id"]), str(job["arm"])): job
        for job in _call(store.outcome_jobs, experiment_id)
    }
    rows: list[dict[str, object]] = []
    evidence_missing = any(
        day["hard_risk_status"] == "missing"
        for day in _call(store.validation_days, experiment_id)
    )
    for decision in _call(store.validation_decisions, experiment_id):
        candidates: dict[str, Mapping[str, Any] | None] = {}
        efficiencies: dict[str, float | None] = {}
        row_missing = False
        for arm in ("baseline", "challenger"):
            raw = decision[f"{arm}_json"]
            candidate = json.loads(str(raw)) if raw is not None else None
            candidates[arm] = candidate
            status = decision[f"{arm}_fill_status"]
            if candidate is None:
                efficiencies[arm] = None
            elif status == "no_observed_fill":
                efficiencies[arm] = float(
                    calculate_expiry_efficiency(
                        {"stage": "validation", "fill_status": "no_observed_fill"}
                    )["efficiency"]
                )
            elif status == "observed_fill":
                job = jobs.get((str(decision["recommendation_point_id"]), arm))
                if job is None or job["status"] != "evaluable":
                    efficiencies[arm] = None
                    row_missing = True
                else:
                    result = json.loads(str(job["result_json"]))
                    efficiencies[arm] = float(result["economics"]["efficiency"])
            else:
                efficiencies[arm] = None
                row_missing = True
        evidence_missing = evidence_missing or row_missing
        baseline = candidates["baseline"]
        challenger = candidates["challenger"]
        rows.append(
            {
                "recommendation_point_id": decision["recommendation_point_id"],
                "trading_date": decision["trading_date"],
                "baseline_candidate_id": baseline.get("candidate_id") if baseline else None,
                "challenger_candidate_id": (
                    challenger.get("candidate_id") if challenger else None
                ),
                "baseline_efficiency": efficiencies["baseline"],
                "challenger_efficiency": efficiencies["challenger"],
                "hard_risk_status": (
                    "missing" if row_missing else decision["hard_risk_status"]
                ),
                "baseline_concentration": (
                    baseline.get("concentration") if baseline else None
                ),
                "challenger_concentration": (
                    challenger.get("concentration") if challenger else None
                ),
            }
        )
    return rows, evidence_missing


def conclude_validation(
    store: ExperimentStore,
    artifact_root: str | Path,
    *,
    experiment_id: str,
    actor: str,
    occurred_at_utc: str,
    idempotency_key: str,
    environ: Mapping[str, str] | None = None,
    publisher: Publisher | None = None,
) -> dict[str, object]:
    experiment_id = _segment(experiment_id, "experiment_id")
    actor, occurred_at_utc, idempotency_key = _command_fields(
        actor, occurred_at_utc, idempotency_key
    )
    initial = _call(store.experiment, experiment_id)
    _require_effective(
        store,
        market=str(initial["market"]),
        account=str(initial["account"]),
        actor=actor,
        occurred_at_utc=occurred_at_utc,
        idempotency_key=idempotency_key,
        artifact_root=artifact_root,
        environ=environ,
    )
    if initial["terminal_mode"] is not None:
        if initial["terminal_mode"] != "completed":
            _fail("terminal_conflict", "experiment was aborted")
        _recover_projection(
            store, artifact_root, experiment_id=experiment_id, publisher=publisher
        )
        return {"status": str(initial["final_outcome_status"]), "idempotent": True}
    for _attempt in range(3):
        experiment = _call(store.experiment, experiment_id)
        if not (
            experiment["validation_progress"] == "ready_to_conclude"
            and int(experiment["completed_validation_partitions"])
            == VALIDATION_REQUIRED_DAYS
        ):
            return {
                "status": "blocked",
                "reason_code": "validation_not_ready",
                "completed_days": experiment["completed_validation_partitions"],
            }
        spec = _spec(experiment)
        rows, evidence_missing = _statistics_rows(store, experiment_id)
        days = _call(store.validation_days, experiment_id)
        for day in days:
            if day["expected_point_count"] is None:
                rows.append(
                    {
                        "recommendation_point_id": f"day-gap:{day['trading_date']}",
                        "trading_date": day["trading_date"],
                        "baseline_candidate_id": None,
                        "challenger_candidate_id": None,
                        "baseline_efficiency": None,
                        "challenger_efficiency": None,
                        "hard_risk_status": "missing",
                        "baseline_concentration": None,
                        "challenger_concentration": None,
                    }
                )
        metrics_policy = cast(Mapping[str, Any], spec["validation_metrics"])
        result = summarize_paired_daily_deltas(
            rows,
            {
                "required_days": VALIDATION_REQUIRED_DAYS,
                "confidence_level": metrics_policy["confidence_level"],
                "worst_fraction": metrics_policy["worst_fraction"],
                "require_concentration_non_increase": True,
            },
        )
        if evidence_missing and result["decision"] != "insufficient_evidence":
            _fail("experiment_conflict", "missing evidence produced a decisive result")
        final_status = (
            "candidate_for_adoption"
            if result["decision"] == "pass"
            else str(result["decision"])
        )
        summary = {
            key: result[key]
            for key in (
                "decision",
                "reason_codes",
                "required_days",
                "effective_days",
                "mean_daily_delta",
                "sample_std",
                "standard_error",
                "t_critical",
                "one_sided_lower_bound",
                "worst_k",
                "worst_tail_mean",
                "serial_correlation_unadjusted",
            )
        }
        jobs = _call(store.outcome_jobs, experiment_id)
        decisions = _call(store.validation_decisions, experiment_id)
        coverage = {
            "committed_days": len(days),
            "expected_points": sum(
                int(day["expected_point_count"] or 0) for day in days
            ),
            "consumed_points": len(decisions),
            "observed_fill_arms": sum(
                decision[f"{arm}_fill_status"] == "observed_fill"
                for decision in decisions
                for arm in ("baseline", "challenger")
            ),
            "no_observed_fill_arms": sum(
                decision[f"{arm}_fill_status"] == "no_observed_fill"
                for decision in decisions
                for arm in ("baseline", "challenger")
            ),
            "not_evaluable_arms": sum(
                decision[f"{arm}_fill_status"] == "not_evaluable"
                for decision in decisions
                for arm in ("baseline", "challenger")
            ),
            "outcome_jobs": len(jobs),
            "evaluable_outcomes": sum(job["status"] == "evaluable" for job in jobs),
        }
        generations = _call(store.generations, experiment_id)
        outcome = next(
            row for row in generations if row["generation_kind"] == "outcome"
        )
        revision_args, post_generation = _revision_request(
            artifact_root,
            experiment_id=experiment_id,
            generation=outcome,
            mutation={
                "operation": "conclude_validation",
                "final_outcome_status": final_status,
                "result": summary,
                "coverage": coverage,
            },
            occurred_at_utc=occurred_at_utc,
            schema_version=OUTCOME_REVISION_SCHEMA,
        )
        terminal = build_generation_terminal_request(
            post_generation,
            terminal_mode="completed",
            reason=None,
            disabled_scope=None,
            occurred_at_utc=occurred_at_utc,
        )
        versions = {
            "fill": cast(Mapping[str, Any], spec["fill_observation"])[
                "contract_version"
            ],
            "metrics": metrics_policy["contract_version"],
            "expiry_outcome": cast(Mapping[str, Any], spec["expiry_outcome"])[
                "contract_version"
            ],
            "fee_schedule": cast(Mapping[str, Any], spec["economics_contracts"])[
                "fee_schedule_version"
            ],
        }
        receipt = build_completed_receipt_request(
            experiment,
            generations,
            terminal,
            final_outcome_status=final_status,
            result=summary,
            coverage=coverage,
            contract_versions=versions,
            occurred_at_utc=occurred_at_utc,
        )
        try:
            _call(
                store.complete_validation,
                experiment_id=experiment_id,
                expected_state_version=int(experiment["state_version"]),
                final_outcome_status=final_status,
                result_sha256=canonical_sha256(summary),
                outcome_terminal_request=terminal,
                receipt_request=receipt,
                actor=actor,
                occurred_at_utc=occurred_at_utc,
                idempotency_key=idempotency_key,
                **revision_args,
            )
            break
        except Top1LifecycleError as exc:
            if exc.reason_code != "generation_conflict":
                raise
    else:
        _fail("terminal_conflict", "experiment changed during conclusion")
    _recover_projection(
        store, artifact_root, experiment_id=experiment_id, publisher=publisher
    )
    return {"status": final_status, "result": summary, "coverage": coverage}


__all__ = [
    "OUTCOME_REVISION_SCHEMA",
    "Top1OutcomeError",
    "conclude_validation",
    "settle_due_outcomes",
]
