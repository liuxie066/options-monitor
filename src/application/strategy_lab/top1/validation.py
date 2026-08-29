from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, NoReturn, cast
from zoneinfo import ZoneInfo

from domain.domain.decision_state_fingerprint import canonical_sha256
from domain.domain.fee_calc import FUTU_HK_TERMINAL_FEE_SCHEDULE_VERSION
from src.application.shadow_replay.common import render_json_text
from src.application.strategy_lab.top1.contracts import (
    Top1CoreContractError,
    VALIDATION_REQUIRED_DAYS,
    build_validation_spec_sha256,
    validate_experiment_spec,
)
from src.application.strategy_lab.top1.corpus import (
    CorpusError,
    read_validation_day_source,
    read_validation_point_source,
)
from src.application.strategy_lab.top1.lifecycle import (
    _call,
    _command_fields,
    _require_service_available,
    _segment,
)
from src.application.strategy_lab.top1.ranking import (
    Top1RankingError,
    rerank_recommendation_point,
)
from src.application.strategy_lab.top1.research_artifacts import (
    ResearchArtifactError,
    load_recorded_research_revision,
)
from src.application.strategy_lab.top1.terminal_projection import (
    build_generation_terminal_request,
    publish_exact_text,
)
from src.infrastructure.strategy_lab.experiment_store import (
    ExperimentStore,
    compact_json,
)


VALIDATION_REVISION_SCHEMA = "sell_put_top1_validation_revision.v1"
_SOURCE_STATUSES = {"available", "missing_after_deadline", "not_evaluable"}
_HASH = re.compile(r"[0-9a-f]{64}\Z")


class Top1ValidationError(ValueError):
    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


def _fail(reason_code: str, message: str) -> NoReturn:
    raise Top1ValidationError(reason_code, message)


def _utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise Top1ValidationError(
            "validation_input_invalid", "timestamp must be ISO-8601 UTC"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        _fail("validation_input_invalid", "timestamp must be ISO-8601 UTC")
    return parsed.astimezone(timezone.utc)


def _generation(store: ExperimentStore, experiment_id: str, kind: str) -> dict[str, Any]:
    row = next(
        (item for item in _call(store.generations, experiment_id) if item["generation_kind"] == kind),
        None,
    )
    if row is None:
        _fail("generation_conflict", f"{kind} generation is missing")
    return row


def _context(
    store: ExperimentStore,
    *,
    experiment_id: str,
    actor: str,
    occurred_at_utc: str,
    idempotency_key: str,
    artifact_root: str | Path,
    environ: Mapping[str, str] | None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any], str]:
    _require_service_available(environ)
    experiment = _call(store.experiment, experiment_id)
    if experiment["terminal_mode"] is not None or not (
        experiment["phase"] == "validation"
        and experiment["validation_progress"] == "collecting_decisions"
    ):
        _fail("late_write", "validation intake is closed")
    try:
        spec = validate_experiment_spec(json.loads(str(experiment["spec_json"])))
    except (json.JSONDecodeError, Top1CoreContractError) as exc:
        raise Top1ValidationError("experiment_conflict", "experiment spec is invalid") from exc
    commitment = json.loads(str(experiment["proposed_commitment_json"]))
    research = _generation(store, experiment_id, "research")
    try:
        expected_validation_hash = build_validation_spec_sha256(
            spec,
            research_terminal_sha256=str(research["terminal_file_sha256"]),
            challenger_variant_id=str(experiment["research_leader"]),
            hidden_window_commitment_sha256=str(experiment["proposed_commitment_sha256"]),
        )
    except Top1CoreContractError as exc:
        raise Top1ValidationError(
            "experiment_conflict", "validation authorization binding is invalid"
        ) from exc
    if expected_validation_hash != experiment["validation_spec_sha256"]:
        _fail("experiment_conflict", "validation authorization binding changed")
    dates = _call(store.commitment_dates, experiment_id)
    completed = int(experiment["completed_validation_partitions"])
    if completed >= len(dates):
        _fail("late_write", "validation commitment is complete")
    return experiment, spec, commitment, research, str(dates[completed])


def _challenger_profile(spec: Mapping[str, Any], variant_id: str) -> str:
    for raw in cast(list[object], spec["variants"]):
        variant = cast(Mapping[str, Any], raw)
        if variant["variant_id"] == variant_id:
            patch = cast(Mapping[str, Any], variant["patch"])
            profile = patch.get("ranking_profile")
            if isinstance(profile, str) and profile:
                return profile
    _fail("experiment_conflict", "locked challenger profile is missing")


def _fee_plan(
    artifact_root: str | Path,
    research_generation: Mapping[str, Any],
    *,
    market: str,
    account: str,
) -> Mapping[str, object] | None:
    try:
        revision = load_recorded_research_revision(artifact_root, research_generation)
    except ResearchArtifactError as exc:
        raise Top1ValidationError("experiment_conflict", str(exc)) from exc
    fee = revision.get("fee_contract")
    if not isinstance(fee, Mapping) or set(fee) != {
        "market",
        "account",
        "fee_schedule_version",
        "account_fee_plan",
    }:
        _fail("experiment_conflict", "research fee contract is invalid")
    if (
        fee["market"] != market
        or fee["account"] != account
        or fee["fee_schedule_version"] != FUTU_HK_TERMINAL_FEE_SCHEDULE_VERSION
    ):
        _fail("experiment_conflict", "research fee contract binding changed")
    raw_plan = fee["account_fee_plan"]
    if raw_plan is None:
        return None
    if not isinstance(raw_plan, Mapping) or not set(raw_plan).issubset(
        {"commission_free", "platform_fee", "fee_plan_ref"}
    ):
        _fail("experiment_conflict", "research account fee plan is invalid")
    return dict(raw_plan)


def _candidate(projection: Mapping[str, Any], candidate_id: str | None) -> Mapping[str, Any] | None:
    if candidate_id is None:
        return None
    for raw in cast(list[object], projection["candidates"]):
        candidate = cast(Mapping[str, Any], raw)
        if candidate["candidate_id"] == candidate_id:
            return candidate
    _fail("ranking_projection_incomplete", "selected candidate is absent")


def _arm(candidate: Mapping[str, Any] | None, fee_plan: Mapping[str, object] | None) -> dict[str, object] | None:
    if candidate is None:
        return None
    return {
        "candidate_id": candidate["candidate_id"],
        "contract_symbol": candidate["contract_symbol"],
        "stock_owner": candidate["stock_owner"],
        "expiration": candidate["expiration"],
        "sell_limit": candidate["sell_limit"],
        "opening_net_premium": candidate["net_premium"],
        "net_cash_basis": candidate["net_cash_basis"],
        "strike": candidate["strike"],
        "multiplier": candidate["multiplier"],
        "currency": candidate["currency"],
        "concentration": candidate["symbol_concentration_after"],
        "fee_schedule_version": candidate["fee_schedule_version"],
        "fee_basis": candidate["fee_basis"],
        "fee_schedule_url": candidate["fee_schedule_url"],
        "account_fee_plan": dict(fee_plan) if fee_plan is not None else None,
    }


def _revision_request(
    artifact_root: str | Path,
    *,
    experiment_id: str,
    generation: Mapping[str, Any],
    mutation: Mapping[str, object],
    occurred_at_utc: str,
    schema_version: str = VALIDATION_REVISION_SCHEMA,
) -> tuple[dict[str, object], dict[str, object]]:
    revision = int(generation["revision"]) + 1
    generation_kind = str(generation["generation_kind"])
    payload: dict[str, object] = {
        "schema_version": schema_version,
        "experiment_id": experiment_id,
        "generation_kind": generation_kind,
        "revision": revision,
        "previous_frozen_row_sha256": generation["frozen_row_content_sha256"],
        "mutation": dict(mutation),
        "occurred_at_utc": occurred_at_utc,
    }
    payload["content_sha256"] = canonical_sha256(payload)
    text = render_json_text(payload)
    file_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    ref = (
        f"strategy_lab/top1/experiments/{experiment_id}/generations/{generation_kind}/"
        f"revisions/{revision:06d}-{payload['content_sha256']}.json"
    )
    try:
        publish_exact_text(artifact_root, ref, text.encode("utf-8"))
    except (OSError, ValueError) as exc:
        raise Top1ValidationError(
            "projection_conflict", "validation revision cannot be published"
        ) from exc
    frozen_hash = canonical_sha256(
        {
            "previous": generation["frozen_row_content_sha256"],
            "revision_content_sha256": payload["content_sha256"],
        }
    )
    post_generation = {
        **generation,
        "revision": revision,
        "last_revision_ref": ref,
        "last_revision_file_sha256": file_hash,
        "frozen_row_content_sha256": frozen_hash,
    }
    return (
        {
            "revision": revision,
            "revision_ref": ref,
            "revision_file_sha256": file_hash,
            "frozen_row_sha256": frozen_hash,
        },
        post_generation,
    )


def _terminal_if_final(
    experiment: Mapping[str, Any],
    post_generation: Mapping[str, Any],
    *,
    day_will_seal: bool,
    occurred_at_utc: str,
) -> Mapping[str, object] | None:
    if not day_will_seal or int(experiment["completed_validation_partitions"]) != (
        VALIDATION_REQUIRED_DAYS - 1
    ):
        return None
    return build_generation_terminal_request(
        post_generation,
        terminal_mode="completed",
        reason=None,
        occurred_at_utc=occurred_at_utc,
    )


def _gap_observations(
    store: ExperimentStore,
    *,
    experiment_id: str,
    trading_date: str,
    observed_point_id: str,
    reason_code: str,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    observations: list[dict[str, object]] = []
    updates: list[dict[str, object]] = []
    for decision in _call(store.validation_decisions, experiment_id):
        if decision["trading_date"] != trading_date:
            continue
        for arm in ("baseline", "challenger"):
            if decision[f"{arm}_fill_status"] != "monitoring":
                continue
            receipt = {
                "status": "gap",
                "reason_code": reason_code,
                "target_point_id": decision["recommendation_point_id"],
                "arm": arm,
                "observed_point_id": observed_point_id,
            }
            observations.append(
                {
                    **receipt,
                    "trading_date": trading_date,
                    "observation_status": "gap",
                    "crossing": None,
                    "observation_json": compact_json(receipt),
                    "observation_sha256": canonical_sha256(receipt),
                }
            )
            updates.append(
                {
                    "target_point_id": decision["recommendation_point_id"],
                    "arm": arm,
                    "fill_status": "not_evaluable",
                }
            )
    return observations, updates


def consume_validation_point(
    store: ExperimentStore,
    artifact_root: str | Path,
    *,
    experiment_id: str,
    recommendation_point_id: str,
    source_status: str,
    actor: str,
    occurred_at_utc: str,
    idempotency_key: str,
    environ: Mapping[str, str] | None = None,
) -> dict[str, object]:
    experiment_id = _segment(experiment_id, "experiment_id")
    if source_status not in _SOURCE_STATUSES:
        _fail("validation_input_invalid", "source_status is unsupported")
    if (
        not isinstance(recommendation_point_id, str)
        or _HASH.fullmatch(recommendation_point_id) is None
    ):
        _fail("validation_input_invalid", "recommendation_point_id is invalid")
    actor, occurred_at_utc, idempotency_key = _command_fields(
        actor, occurred_at_utc, idempotency_key
    )
    _require_service_available(environ)
    existing = _call(store.validation_decision, experiment_id, recommendation_point_id)
    if existing is not None:
        if existing["source_status"] != source_status:
            _fail("validation_conflict", "point source status changed")
        return {"status": "idempotent", "decision": existing}
    experiment, spec, _commitment, research, trading_date = _context(
        store,
        experiment_id=experiment_id,
        actor=actor,
        occurred_at_utc=occurred_at_utc,
        idempotency_key=idempotency_key,
        artifact_root=artifact_root,
        environ=environ,
    )
    try:
        source = read_validation_point_source(
            artifact_root,
            market=str(experiment["market"]),
            account=str(experiment["account"]),
            trading_date=trading_date,
            recommendation_point_id=recommendation_point_id,
        )
    except CorpusError as exc:
        raise Top1ValidationError(exc.reason_code, str(exc)) from exc
    observed_source_status = (
        "missing_after_deadline" if source["status"] == "missing" else source["status"]
    )
    if observed_source_status != source_status:
        _fail("validation_source_conflict", "declared source status does not match corpus")
    if not isinstance(source.get("expectation"), dict) or not isinstance(
        source.get("row"), dict
    ):
        _fail(
            "validation_source_conflict",
            "point intake requires a clean canonical day expectation",
        )
    expectation = cast(dict[str, Any], source["expectation"])
    expected_ids = cast(list[str], expectation["expected_recommendation_point_ids"])
    consumed = [
        row
        for row in _call(store.validation_decisions, experiment_id)
        if row["trading_date"] == trading_date
    ]
    point_index = len(consumed)
    if point_index >= len(expected_ids) or expected_ids[point_index] != recommendation_point_id:
        _fail("validation_point_out_of_order", "point is not the next expectation")
    target_at_utc = str(expectation["scheduled_scan_targets_market"][point_index])
    if source_status == "missing_after_deadline":
        timer = cast(Mapping[str, Any], spec["timer_binding"])
        deadline = _utc(target_at_utc) + timedelta(
            seconds=int(timer["producer_catchup_grace_seconds"])
            + int(timer["producer_run_timeout_upper_bound_seconds"])
        )
        if _utc(occurred_at_utc) < deadline:
            _fail("validation_deadline_not_reached", "point gap deadline has not passed")

    baseline: dict[str, object] | None = None
    challenger: dict[str, object] | None = None
    hard_risk_status = "missing"
    reason_code = cast(str | None, source.get("reason_code"))
    if source_status == "available":
        projection = cast(Mapping[str, Any], source["projection"])
        try:
            baseline_rank = rerank_recommendation_point(
                projection, ranking_profile="current_tie_break"
            )
            challenger_rank = rerank_recommendation_point(
                projection,
                ranking_profile=_challenger_profile(
                    spec, str(experiment["research_leader"])
                ),
            )
        except Top1RankingError as exc:
            raise Top1ValidationError(exc.reason_code, str(exc)) from exc
        baseline_id = cast(str | None, baseline_rank["top1_candidate_id"])
        challenger_id = cast(str | None, challenger_rank["top1_candidate_id"])
        if (baseline_id is None) != (challenger_id is None):
            _fail("official_decision_incomplete", "Top1 selection is one-sided")
        fee_plan = _fee_plan(
            artifact_root,
            research,
            market=str(experiment["market"]),
            account=str(experiment["account"]),
        )
        baseline = _arm(_candidate(projection, baseline_id), fee_plan)
        challenger = _arm(_candidate(projection, challenger_id), fee_plan)
        selected = [item for item in (baseline, challenger) if item is not None]
        hard_risk_status = (
            "passed"
            if all(item["concentration"] is not None for item in selected)
            else "missing"
        )
        reason_code = None if hard_risk_status == "passed" else "risk_evidence_missing"

    day_row = cast(Mapping[str, Any], source["row"])
    decision = {
        "recommendation_point_id": recommendation_point_id,
        "trading_date": trading_date,
        "point_index": point_index,
        "source_status": source_status,
        "expectation_ref": day_row["expectation_ref"],
        "expectation_content_sha256": day_row["expectation_content_sha256"],
        "target_at_utc": target_at_utc,
        "source_ref": (
            cast(Mapping[str, Any], source["point_row"])["projection_ref"]
            if source_status == "available"
            else cast(Mapping[str, Any], source.get("point_row") or {}).get("source_point_ref")
        ),
        "source_file_sha256": (
            cast(Mapping[str, Any], source["point_row"])["projection_file_sha256"]
            if source_status == "available"
            else None
        ),
        "source_content_sha256": (
            cast(Mapping[str, Any], source["point_row"])["projection_content_sha256"]
            if source_status == "available"
            else cast(Mapping[str, Any], source.get("point_row") or {}).get(
                "source_point_content_sha256"
            )
        ),
        "hard_risk_status": hard_risk_status,
        "baseline_json": compact_json(baseline) if baseline is not None else None,
        "challenger_json": compact_json(challenger) if challenger is not None else None,
        "baseline_fill_status": "monitoring" if baseline is not None else None,
        "challenger_fill_status": "monitoring" if challenger is not None else None,
        "reason_code": reason_code,
    }
    gap_observations: list[dict[str, object]] = []
    fill_updates: list[dict[str, object]] = []
    if source_status != "available":
        gap_observations, fill_updates = _gap_observations(
            store,
            experiment_id=experiment_id,
            trading_date=trading_date,
            observed_point_id=recommendation_point_id,
            reason_code=reason_code or "validation_source_missing",
        )
    is_final_point = point_index + 1 == len(expected_ids)
    day: dict[str, object] | None = None
    if source_status != "available" and is_final_point:
        day = {
            "trading_date": trading_date,
            "expectation_ref": day_row["expectation_ref"],
            "expectation_content_sha256": day_row["expectation_content_sha256"],
            "expectation_file_sha256": day_row["expectation_file_sha256"],
            "expected_point_count": len(expected_ids),
            "consumed_point_count": point_index + 1,
            "hard_risk_status": "missing",
            "reason_code": reason_code or "validation_source_missing",
            "deadline_at_utc": None,
            "daily_json": compact_json({"status": "not_evaluable"}),
        }
    hidden = _generation(store, experiment_id, "hidden")
    revision_args, post_generation = _revision_request(
        artifact_root,
        experiment_id=experiment_id,
        generation=hidden,
        mutation={
            "operation": "consume_validation_point",
            "decision": decision,
            "gap_observations": gap_observations,
            "day": day,
        },
        occurred_at_utc=occurred_at_utc,
    )
    terminal = _terminal_if_final(
        experiment,
        post_generation,
        day_will_seal=day is not None,
        occurred_at_utc=occurred_at_utc,
    )
    committed = _call(
        store.commit_validation_decision,
        experiment_id=experiment_id,
        expected_state_version=int(experiment["state_version"]),
        decision=decision,
        gap_observations=gap_observations,
        fill_status_updates=fill_updates,
        day=day,
        terminal_request=terminal,
        actor=actor,
        occurred_at_utc=occurred_at_utc,
        idempotency_key=idempotency_key,
        **revision_args,
    )
    return {"status": "committed", "decision": committed}


def record_validation_day_gap(
    store: ExperimentStore,
    artifact_root: str | Path,
    *,
    experiment_id: str,
    trading_date: str,
    actor: str,
    occurred_at_utc: str,
    idempotency_key: str,
    environ: Mapping[str, str] | None = None,
) -> dict[str, object]:
    experiment_id = _segment(experiment_id, "experiment_id")
    try:
        if date.fromisoformat(trading_date).isoformat() != trading_date:
            raise ValueError
    except (TypeError, ValueError):
        _fail("validation_input_invalid", "trading_date must be a canonical ISO date")
    actor, occurred_at_utc, idempotency_key = _command_fields(
        actor, occurred_at_utc, idempotency_key
    )
    _require_service_available(environ)
    existing = _call(store.validation_day, experiment_id, trading_date)
    if existing is not None:
        if existing["expected_point_count"] is not None:
            _fail("validation_conflict", "date was sealed from point evidence")
        return {"status": "idempotent", "day": existing}
    experiment, spec, _commitment, _research, open_date = _context(
        store,
        experiment_id=experiment_id,
        actor=actor,
        occurred_at_utc=occurred_at_utc,
        idempotency_key=idempotency_key,
        artifact_root=artifact_root,
        environ=environ,
    )
    if trading_date != open_date:
        _fail("validation_conflict", "trading_date is not the open commitment date")
    if any(
        row["trading_date"] == trading_date
        for row in _call(store.validation_decisions, experiment_id)
    ):
        _fail("validation_conflict", "point intake already began for this date")
    try:
        source = read_validation_day_source(
            artifact_root,
            market=str(experiment["market"]),
            account=str(experiment["account"]),
            trading_date=trading_date,
        )
    except CorpusError:
        source = {"status": "not_evaluable", "reason_code": "corpus_artifact_invalid"}
    if source["status"] == "available":
        _fail("validation_conflict", "canonical day expectation is available")
    timer = cast(Mapping[str, Any], spec["timer_binding"])
    day_date = date.fromisoformat(trading_date)
    deadline = datetime.combine(
        day_date + timedelta(days=1),
        time.min,
        tzinfo=ZoneInfo("Asia/Hong_Kong"),
    ).astimezone(timezone.utc) + timedelta(
        seconds=int(timer["producer_catchup_grace_seconds"])
        + int(timer["producer_run_timeout_upper_bound_seconds"])
    )
    if _utc(occurred_at_utc) < deadline:
        _fail("validation_deadline_not_reached", "whole-day gap deadline has not passed")
    day = {
        "trading_date": trading_date,
        "expectation_ref": None,
        "expectation_content_sha256": None,
        "expectation_file_sha256": None,
        "expected_point_count": None,
        "consumed_point_count": 0,
        "hard_risk_status": "missing",
        "reason_code": source.get("reason_code") or "corpus_day_expectation_missing",
        "deadline_at_utc": deadline.isoformat().replace("+00:00", "Z"),
        "daily_json": compact_json({"status": "not_evaluable"}),
    }
    hidden = _generation(store, experiment_id, "hidden")
    revision_args, post_generation = _revision_request(
        artifact_root,
        experiment_id=experiment_id,
        generation=hidden,
        mutation={"operation": "record_validation_day_gap", "day": day},
        occurred_at_utc=occurred_at_utc,
    )
    terminal = _terminal_if_final(
        experiment,
        post_generation,
        day_will_seal=True,
        occurred_at_utc=occurred_at_utc,
    )
    result = _call(
        store.commit_validation_day_gap,
        experiment_id=experiment_id,
        expected_state_version=int(experiment["state_version"]),
        day=day,
        terminal_request=terminal,
        actor=actor,
        occurred_at_utc=occurred_at_utc,
        idempotency_key=idempotency_key,
        **revision_args,
    )
    return {"status": "committed", "experiment": result}


__all__ = [
    "Top1ValidationError",
    "VALIDATION_REVISION_SCHEMA",
    "consume_validation_point",
    "record_validation_day_gap",
]
