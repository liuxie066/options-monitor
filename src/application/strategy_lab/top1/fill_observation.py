from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, NoReturn, cast

from domain.domain.decision_state_fingerprint import canonical_sha256
from domain.domain.option_lifecycle import expiration_observation_start_ms
from src.application.strategy_lab.top1.corpus import (
    CorpusError,
    read_validation_day_source,
)
from src.application.strategy_lab.top1.lifecycle import (
    _call,
    _command_fields,
    _require_service_available,
    _segment,
)
from src.application.strategy_lab.top1.validation import (
    Top1ValidationError,
    _context,
    _generation,
    _revision_request,
    _terminal_if_final,
    _utc,
)
from src.infrastructure.strategy_lab.experiment_store import (
    ExperimentStore,
    compact_json,
)


_HASH = re.compile(r"[0-9a-f]{64}\Z")


class FillObservationError(ValueError):
    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


def _fail(reason_code: str, message: str) -> NoReturn:
    raise FillObservationError(reason_code, message)


def _rows(value: object) -> list[dict[str, Any]]:
    if isinstance(value, list):
        if any(not isinstance(item, Mapping) for item in value):
            _fail("fill_observation_invalid", "snapshot rows must be objects")
        return [dict(cast(Mapping[str, Any], item)) for item in value]
    to_dict = getattr(value, "to_dict", None)
    if not callable(to_dict):
        _fail("fill_observation_invalid", "snapshot result must be tabular")
    try:
        raw = to_dict(orient="records")
    except (TypeError, ValueError) as exc:
        raise FillObservationError(
            "fill_observation_invalid", "snapshot result cannot be projected"
        ) from exc
    if not isinstance(raw, list) or any(not isinstance(item, Mapping) for item in raw):
        _fail("fill_observation_invalid", "snapshot rows must be objects")
    return [dict(cast(Mapping[str, Any], item)) for item in raw]


def _price(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _snapshot_by_code(
    value: object, requested_codes: list[str]
) -> tuple[dict[str, dict[str, Any]], str | None]:
    rows = _rows(value)
    requested = set(requested_codes)
    by_code: dict[str, dict[str, Any]] = {}
    for row in rows:
        code = str(row.get("code") or "").strip().upper()
        if not code or code not in requested or code in by_code:
            return {}, "snapshot_coverage_conflict"
        by_code[code] = row
    return by_code, None


def _job(
    *,
    decision: Mapping[str, Any],
    arm: str,
    candidate: Mapping[str, Any],
    market: str,
    pending_hours: int,
) -> dict[str, object]:
    due_ms = expiration_observation_start_ms(str(candidate["expiration"]), market)
    if due_ms is None:
        _fail("outcome_contract_invalid", "expiration due boundary is unavailable")
    due = datetime.fromtimestamp(due_ms / 1000, tz=timezone.utc)
    deadline = due + timedelta(hours=pending_hours)
    payload: dict[str, object] = {
        "target_point_id": decision["recommendation_point_id"],
        "arm": arm,
        "trading_date": decision["trading_date"],
        "fill_date": decision["trading_date"],
        "fill_price": candidate["sell_limit"],
        "contract_symbol": candidate["contract_symbol"],
        "stock_owner": candidate["stock_owner"],
        "expiration": candidate["expiration"],
        "strike": candidate["strike"],
        "multiplier": candidate["multiplier"],
        "currency": candidate["currency"],
        "opening_net_premium": candidate["opening_net_premium"],
        "net_cash_basis": candidate["net_cash_basis"],
        "account_fee_plan": candidate["account_fee_plan"],
        "fee_schedule_version": candidate["fee_schedule_version"],
        "source_ref": decision["source_ref"],
        "source_content_sha256": decision["source_content_sha256"],
        "terms_capture_trading_date": candidate["expiration"],
        "due_at_utc": due.isoformat().replace("+00:00", "Z"),
        "deadline_at_utc": deadline.isoformat().replace("+00:00", "Z"),
    }
    return {
        **payload,
        "job_json": compact_json(payload),
        "job_sha256": canonical_sha256(payload),
    }


def _day(
    decisions: list[dict[str, Any]],
    *,
    expectation_row: Mapping[str, Any],
    expected_count: int,
    updates: list[dict[str, object]],
) -> dict[str, object]:
    status_by_key = {
        (str(row["target_point_id"]), str(row["arm"])): row["fill_status"]
        for row in updates
    }
    terminal_statuses: list[str] = []
    for decision in decisions:
        for arm in ("baseline", "challenger"):
            current = decision[f"{arm}_fill_status"]
            if current is not None:
                terminal_statuses.append(
                    str(
                        status_by_key.get(
                            (str(decision["recommendation_point_id"]), arm), current
                        )
                    )
                )
    evidence_missing = any(
        decision["hard_risk_status"] != "passed" for decision in decisions
    ) or any(status == "not_evaluable" for status in terminal_statuses)
    hard_risk = "missing" if evidence_missing else "passed"
    reason = "validation_evidence_missing" if evidence_missing else None
    daily = {
        "status": "not_evaluable" if evidence_missing else "evaluable",
        "decision_count": len(decisions),
        "arm_count": len(terminal_statuses),
        "observed_fill_count": terminal_statuses.count("observed_fill"),
        "no_observed_fill_count": terminal_statuses.count("no_observed_fill"),
        "not_evaluable_count": terminal_statuses.count("not_evaluable"),
    }
    return {
        "trading_date": decisions[0]["trading_date"],
        "expectation_ref": expectation_row["expectation_ref"],
        "expectation_content_sha256": expectation_row["expectation_content_sha256"],
        "expectation_file_sha256": expectation_row["expectation_file_sha256"],
        "expected_point_count": expected_count,
        "consumed_point_count": len(decisions),
        "hard_risk_status": hard_risk,
        "reason_code": reason,
        "deadline_at_utc": None,
        "daily_json": compact_json(daily),
    }


def observe_active_contracts(
    store: ExperimentStore,
    artifact_root: str | Path,
    *,
    experiment_id: str,
    observed_recommendation_point_id: str,
    gateway: Any,
    actor: str,
    occurred_at_utc: str,
    idempotency_key: str,
    environ: Mapping[str, str] | None = None,
) -> dict[str, object]:
    experiment_id = _segment(experiment_id, "experiment_id")
    if _HASH.fullmatch(observed_recommendation_point_id) is None:
        _fail("fill_observation_invalid", "observed point ID is invalid")
    actor, occurred_at_utc, idempotency_key = _command_fields(
        actor, occurred_at_utc, idempotency_key
    )
    _require_service_available(environ)
    current = _call(store.experiment, experiment_id)
    if _call(
        store.validation_observation_committed,
        experiment_id,
        observed_recommendation_point_id,
    ):
        return {
            "status": "idempotent",
            "observations": _call(
                store.fill_observations,
                experiment_id,
                observed_point_id=observed_recommendation_point_id,
            ),
        }
    if current["terminal_mode"] is not None or current["validation_progress"] != (
        "collecting_decisions"
    ):
        return {"status": "terminal", "observations": []}
    try:
        experiment, spec, _commitment, _research, trading_date = _context(
            store,
            experiment_id=experiment_id,
            actor=actor,
            occurred_at_utc=occurred_at_utc,
            idempotency_key=idempotency_key,
            artifact_root=artifact_root,
            environ=environ,
        )
    except Top1ValidationError as exc:
        raise FillObservationError(exc.reason_code, str(exc)) from exc
    decisions = [
        row
        for row in _call(store.validation_decisions, experiment_id)
        if row["trading_date"] == trading_date
    ]
    if not decisions or decisions[-1]["recommendation_point_id"] != (
        observed_recommendation_point_id
    ):
        _fail("fill_observation_conflict", "observed point is not the latest decision")
    observed = decisions[-1]
    try:
        day_source = read_validation_day_source(
            artifact_root,
            market=str(experiment["market"]),
            account=str(experiment["account"]),
            trading_date=trading_date,
        )
    except CorpusError as exc:
        raise FillObservationError(exc.reason_code, str(exc)) from exc
    if day_source["status"] != "available":
        _fail("fill_observation_conflict", "day expectation is unavailable")
    expectation = cast(Mapping[str, Any], day_source["expectation"])
    expected_ids = cast(list[str], expectation["expected_recommendation_point_ids"])
    is_final = observed_recommendation_point_id == expected_ids[-1]
    if [row["recommendation_point_id"] for row in decisions] != expected_ids[: len(decisions)]:
        _fail("fill_observation_conflict", "decision sequence changed")

    active: list[tuple[dict[str, Any], str, dict[str, Any]]] = []
    for decision in decisions:
        for arm in ("baseline", "challenger"):
            if decision[f"{arm}_fill_status"] != "monitoring":
                continue
            candidate = json.loads(str(decision[f"{arm}_json"]))
            if not isinstance(candidate, dict):
                _fail("fill_observation_conflict", "candidate facts are invalid")
            active.append((decision, arm, candidate))
    codes = sorted({str(candidate["contract_symbol"]).upper() for _, _, candidate in active})
    rows_by_code: dict[str, dict[str, Any]] = {}
    batch_reason: str | None = None
    timer = cast(Mapping[str, Any], spec["timer_binding"])
    target = _utc(str(observed["target_at_utc"]))
    command_time = _utc(occurred_at_utc)
    if command_time < target or command_time > target + timedelta(
        seconds=int(timer["fill_observation_duration_upper_bound_seconds"])
    ):
        batch_reason = "fill_observation_outside_window"
    elif codes:
        try:
            rows_by_code, batch_reason = _snapshot_by_code(
                gateway.get_snapshot(codes), codes
            )
        except Exception:
            batch_reason = "snapshot_provider_error"

    observations: list[dict[str, object]] = []
    updates: list[dict[str, object]] = []
    jobs: list[dict[str, object]] = []
    current_observation_keys: set[tuple[str, str, str]] = set()
    pending_hours = int(cast(Mapping[str, Any], spec["expiry_outcome"])["pending_elapsed_hours"])
    for decision, arm, candidate in active:
        code = str(candidate["contract_symbol"]).upper()
        row = rows_by_code.get(code)
        bid = _price(row.get("bid_price", row.get("bid"))) if row else None
        ask = _price(row.get("ask_price", row.get("ask"))) if row else None
        reason = batch_reason
        if reason is None and row is None:
            reason = "snapshot_missing"
        if reason is None and (
            bid is None or bid < 0 or ask is None or ask <= 0 or ask < bid
        ):
            reason = "snapshot_quote_invalid"
        crossing = reason is None and bid is not None and bid >= float(candidate["sell_limit"])
        receipt = {
            "status": "evaluable" if reason is None else "not_evaluable",
            "reason_code": reason,
            "target_point_id": decision["recommendation_point_id"],
            "arm": arm,
            "observed_point_id": observed_recommendation_point_id,
            "contract_symbol": code,
            "captured_at_utc": occurred_at_utc,
            "bid": bid,
            "ask": ask,
            "source_ref": decision["source_ref"],
            "source_content_sha256": decision["source_content_sha256"],
            "crossing": crossing if reason is None else None,
        }
        observations.append(
            {
                **receipt,
                "trading_date": trading_date,
                "observation_status": "quote",
                "observation_json": compact_json(receipt),
                "observation_sha256": canonical_sha256(receipt),
            }
        )
        current_observation_keys.add(
            (
                str(decision["recommendation_point_id"]),
                arm,
                observed_recommendation_point_id,
            )
        )
        if reason is not None:
            updates.append(
                {
                    "target_point_id": decision["recommendation_point_id"],
                    "arm": arm,
                    "fill_status": "not_evaluable",
                }
            )
        elif crossing:
            updates.append(
                {
                    "target_point_id": decision["recommendation_point_id"],
                    "arm": arm,
                    "fill_status": "observed_fill",
                }
            )
            jobs.append(
                _job(
                    decision=decision,
                    arm=arm,
                    candidate=candidate,
                    market=str(experiment["market"]),
                    pending_hours=pending_hours,
                )
            )

    if is_final:
        prior_keys = {
            (
                str(row["target_point_id"]),
                str(row["arm"]),
                str(row["observed_point_id"]),
            )
            for row in _call(store.fill_observations, experiment_id)
            if row["trading_date"] == trading_date
        }
        update_keys = {
            (str(row["target_point_id"]), str(row["arm"])) for row in updates
        }
        for decision, arm, _candidate in active:
            key = (str(decision["recommendation_point_id"]), arm)
            if key in update_keys:
                continue
            suffix = expected_ids[int(decision["point_index"]) :]
            complete = all(
                (key[0], arm, point_id) in prior_keys | current_observation_keys
                for point_id in suffix
            )
            updates.append(
                {
                    "target_point_id": key[0],
                    "arm": arm,
                    "fill_status": (
                        "no_observed_fill" if complete else "not_evaluable"
                    ),
                }
            )

    day = None
    if is_final:
        day = _day(
            decisions,
            expectation_row=cast(Mapping[str, Any], day_source["row"]),
            expected_count=len(expected_ids),
            updates=updates,
        )
    hidden = _generation(store, experiment_id, "hidden")
    revision_args, post_generation = _revision_request(
        artifact_root,
        experiment_id=experiment_id,
        generation=hidden,
        mutation={
            "operation": "observe_active_contracts",
            "observed_point_id": observed_recommendation_point_id,
            "observations": observations,
            "fill_status_updates": updates,
            "new_job_sha256s": [job["job_sha256"] for job in jobs],
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
    result = _call(
        store.commit_validation_observation_batch,
        experiment_id=experiment_id,
        expected_state_version=int(experiment["state_version"]),
        observed_point_id=observed_recommendation_point_id,
        observations=observations,
        fill_status_updates=updates,
        new_jobs=jobs,
        day=day,
        terminal_request=terminal,
        actor=actor,
        occurred_at_utc=occurred_at_utc,
        idempotency_key=idempotency_key,
        **revision_args,
    )
    return {"status": result["status"], "observations": observations}


__all__ = ["FillObservationError", "observe_active_contracts"]
