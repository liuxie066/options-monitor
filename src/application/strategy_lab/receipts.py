from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, NoReturn

from src.application.candidate_snapshot_contract import utc_timestamp
from src.application.strategy_lab.contracts import (
    RESEARCH_SESSIONS,
    canonical_sha256,
    strict_json_bytes,
)
from src.infrastructure.private_storage import (
    atomic_write_private_bytes,
    exclusive_private_file_lock,
    open_private_text,
    private_path,
)


_EXPERIMENT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}").fullmatch


class StrategyLabReceiptError(RuntimeError):
    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


def _fail(reason_code: str, message: str) -> NoReturn:
    raise StrategyLabReceiptError(reason_code, message)


def _identity(value: object, label: str) -> str:
    if not isinstance(value, str) or _EXPERIMENT_ID(value) is None:
        _fail("receipt_input_invalid", f"{label} is invalid")
    return value


def _receipt_ref(experiment_id: str, kind: str) -> str:
    if kind not in {"research", "final"}:
        _fail("receipt_kind_unsupported", "receipt kind is unsupported")
    return f"experiments/{experiment_id}/receipts/{kind}.json"


def _canonical_observations(values: object) -> list[dict[str, Any]]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        _fail("receipt_input_invalid", "observations must be a sequence")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, Mapping):
            _fail("receipt_input_invalid", "observation is invalid")
        key = value.get("observation_key")
        if not isinstance(key, str) or not key or key in seen:
            _fail("receipt_input_invalid", "observation key is invalid")
        seen.add(key)
        result.append(
            {
                "observation_key": key,
                "recommendation_point_id": value.get("recommendation_point_id"),
                "arm_id": value.get("arm_id"),
                "kind": value.get("kind"),
                "status": value.get("status"),
                "payload": value.get("payload"),
                "artifact_ref": value.get("artifact_ref"),
                "artifact_sha256": value.get("artifact_sha256"),
                "created_at_utc": value.get("created_at_utc"),
            }
        )
    return sorted(result, key=lambda value: value["observation_key"])


def _canonical_comparisons(values: object) -> list[dict[str, Any]]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        _fail("receipt_input_invalid", "comparisons must be a sequence")
    result = [dict(value) for value in values if isinstance(value, Mapping)]
    if len(result) != len(values):
        _fail("receipt_input_invalid", "comparison is invalid")
    try:
        return sorted(
            result,
            key=lambda value: (
                float(value["near_return_threshold"]),
                str(value["variant_id"]),
            ),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise StrategyLabReceiptError("receipt_input_invalid", "comparison identity is invalid") from exc


def build_research_receipt(
    experiment: object,
    observations: object,
    comparisons: object,
    conclusion: object,
    concluded_at_utc: object,
) -> dict[str, Any]:
    if not isinstance(experiment, Mapping):
        _fail("receipt_input_invalid", "experiment is invalid")
    experiment_id = _identity(experiment.get("experiment_id"), "experiment_id")
    spec = experiment.get("spec")
    spec_sha256 = experiment.get("spec_sha256")
    manifest = experiment.get("behavior_manifest")
    behavior_sha256 = experiment.get("evaluator_behavior_sha256")
    if (
        not isinstance(spec, Mapping)
        or canonical_sha256(spec) != spec_sha256
        or canonical_sha256(manifest) != behavior_sha256
        or not isinstance(experiment.get("source_commit_sha"), str)
        or len(experiment["source_commit_sha"]) != 40
    ):
        _fail("receipt_input_invalid", "experiment bindings are invalid")
    research_window = spec.get("research_window")
    selected_days = research_window.get("selected_trading_dates") if isinstance(research_window, Mapping) else None
    sessions = research_window.get("sessions") if isinstance(research_window, Mapping) else None
    if (
        not isinstance(selected_days, list)
        or len(selected_days) != RESEARCH_SESSIONS
        or any(not isinstance(value, str) or not value for value in selected_days)
        or len(set(selected_days)) != RESEARCH_SESSIONS
        or not isinstance(sessions, list)
        or len(sessions) != RESEARCH_SESSIONS
    ):
        _fail("receipt_input_invalid", "research window is not exactly 20 sessions")
    canonical_comparisons = _canonical_comparisons(comparisons)
    if not isinstance(conclusion, Mapping):
        _fail("receipt_input_invalid", "research conclusion is invalid")
    canonical_conclusion = dict(conclusion)
    status = canonical_conclusion.get("status")
    leader = canonical_conclusion.get("leader")
    if (
        status not in {"leader", "no_leader", "insufficient_evidence"}
        or (status == "leader") != isinstance(leader, Mapping)
        or not isinstance(canonical_conclusion.get("passing_variant_ids"), list)
    ):
        _fail("receipt_input_invalid", "research conclusion is invalid")
    if isinstance(leader, Mapping):
        matches = [item for item in canonical_comparisons if item.get("variant_id") == leader.get("variant_id")]
        if len(matches) != 1 or leader.get("comparison_sha256") != canonical_sha256(matches[0]):
            _fail("receipt_input_invalid", "research leader binding is invalid")
    return {
        "kind": "research",
        "experiment_id": experiment_id,
        "spec": dict(spec),
        "spec_sha256": spec_sha256,
        "source_commit_sha": experiment["source_commit_sha"],
        "behavior_manifest": manifest,
        "evaluator_behavior_sha256": behavior_sha256,
        "research_window": research_window,
        "observations": _canonical_observations(observations),
        "comparisons": canonical_comparisons,
        "conclusion": canonical_conclusion,
        "provisional": True,
        "fill_declaration": "simulated_fill_not_real_trade",
        "concluded_at_utc": utc_timestamp(concluded_at_utc, "concluded_at_utc"),
    }


def build_final_receipt(
    experiment: object,
    events: object,
    observations: object,
    comparison: object,
    conclusion: object,
) -> dict[str, Any]:
    if not isinstance(experiment, Mapping):
        _fail("receipt_input_invalid", "experiment is invalid")
    experiment_id = _identity(experiment.get("experiment_id"), "experiment_id")
    plan = experiment.get("validation_plan")
    if (
        not isinstance(plan, Mapping)
        or canonical_sha256(plan) != experiment.get("validation_plan_sha256")
        or not isinstance(experiment.get("spec"), Mapping)
        or canonical_sha256(experiment["spec"]) != experiment.get("spec_sha256")
        or canonical_sha256(experiment.get("behavior_manifest")) != experiment.get("evaluator_behavior_sha256")
    ):
        _fail("receipt_input_invalid", "final receipt bindings are invalid")
    calendar = plan.get("market_calendar")
    sessions = calendar.get("sessions") if isinstance(calendar, Mapping) else None
    if not isinstance(sessions, list) or len(sessions) != 10:
        _fail("receipt_input_invalid", "validation window is not exactly 10 sessions")
    expected_point_ids = {
        point_id
        for session in sessions
        if isinstance(session, Mapping)
        for point_id in session.get("expected_recommendation_point_ids", [])
        if isinstance(point_id, str)
    }
    if conclusion not in {
        "challenger_passed",
        "keep_baseline",
        "insufficient_evidence",
    }:
        _fail("receipt_input_invalid", "final conclusion is invalid")
    if not isinstance(comparison, Mapping):
        _fail("receipt_input_invalid", "final comparison is invalid")
    expected_conclusion = (
        "challenger_passed"
        if comparison.get("status") == "complete" and comparison.get("passed") is True
        else "keep_baseline"
        if comparison.get("status") == "complete" and comparison.get("passed") is False
        else "insufficient_evidence"
    )
    if conclusion != expected_conclusion:
        _fail("receipt_input_invalid", "final conclusion does not match comparison")
    if not isinstance(events, Sequence) or isinstance(events, (str, bytes)):
        _fail("receipt_input_invalid", "events are invalid")
    confirmations: dict[str, dict[str, Any]] = {}
    for event in events:
        if isinstance(event, Mapping) and event.get("event_type") in {
            "research_confirmed",
            "validation_confirmed",
        }:
            confirmations[str(event["event_type"])] = {
                "occurred_at_utc": event.get("occurred_at_utc"),
                "event_sha256": canonical_sha256(dict(event)),
            }
    if set(confirmations) != {"research_confirmed", "validation_confirmed"}:
        _fail("receipt_input_invalid", "confirmation events are unavailable")
    terminal = []
    for value in _canonical_observations(observations):
        if value["kind"] in {"validation_point", "validation_fill", "single_result"}:
            if value["recommendation_point_id"] in expected_point_ids:
                terminal.append(value)
            continue
        if value["kind"] == "expiry_close_query":
            payload = value.get("payload")
            query = payload.get("query") if isinstance(payload, Mapping) else None
            if isinstance(query, Mapping) and query.get("validation_plan_sha256") == experiment.get(
                "validation_plan_sha256"
            ):
                terminal.append(value)
    durable_times = [
        str(value["created_at_utc"]) for value in terminal if isinstance(value.get("created_at_utc"), str)
    ] + [value["occurred_at_utc"] for value in confirmations.values()]
    if not durable_times:
        _fail("receipt_input_invalid", "final conclusion time is unavailable")
    return {
        "kind": "final",
        "experiment_id": experiment_id,
        "spec_sha256": experiment["spec_sha256"],
        "validation_plan_sha256": experiment["validation_plan_sha256"],
        "evaluator_behavior_sha256": experiment["evaluator_behavior_sha256"],
        "behavior_manifest": experiment["behavior_manifest"],
        "research_receipt_ref": experiment["research_receipt_ref"],
        "research_receipt_sha256": experiment["research_receipt_sha256"],
        "confirmations": confirmations,
        "leader": experiment["leader"],
        "validation_window": {
            "sessions": sessions,
            "expected_recommendation_point_ids": [
                point_id for session in sessions for point_id in session.get("expected_recommendation_point_ids", [])
            ],
        },
        "terminal_observations": terminal,
        "comparison": comparison,
        "safety_status": ("pass" if comparison.get("status") == "complete" else "not_evaluable"),
        "conclusion": conclusion,
        "declarations": {
            "observed_fill_is_quote_evidence_not_broker_execution": True,
            "experimental_improvement_is_not_realized_online_profit": True,
        },
        "concluded_at_utc": utc_timestamp(
            max(datetime.fromisoformat(utc_timestamp(value).replace("Z", "+00:00")) for value in durable_times),
            "concluded_at_utc",
        ),
    }


def _read_path(target: Path, *, receipt_ref: str) -> dict[str, Any]:
    try:
        with open_private_text(target) as handle:
            content = handle.read()
        encoded = content.encode("utf-8")
        payload = json.loads(content)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StrategyLabReceiptError("receipt_artifact_invalid", "research receipt cannot be read") from exc
    if not isinstance(payload, dict) or strict_json_bytes(payload) != encoded:
        _fail("receipt_artifact_invalid", "research receipt is not canonical")
    return {
        "receipt": payload,
        "receipt_ref": receipt_ref,
        "receipt_sha256": hashlib.sha256(encoded).hexdigest(),
    }


def publish_receipt(
    artifact_root: str | Path,
    experiment_id: object,
    kind: object,
    payload: object,
) -> dict[str, Any]:
    identity = _identity(experiment_id, "experiment_id")
    if not isinstance(kind, str):
        _fail("receipt_kind_unsupported", "receipt kind is invalid")
    ref = _receipt_ref(identity, kind)
    if not isinstance(payload, Mapping) or payload.get("kind") != kind or payload.get("experiment_id") != identity:
        _fail("receipt_input_invalid", "receipt payload identity is invalid")
    encoded = strict_json_bytes(payload)
    root = private_path(artifact_root)
    target = root.joinpath(*ref.split("/"))
    lock_path = target.with_suffix(f"{target.suffix}.lock")
    with exclusive_private_file_lock(lock_path):
        if target.exists():
            existing = _read_path(target, receipt_ref=ref)
            if strict_json_bytes(existing["receipt"]) != encoded:
                _fail("receipt_immutable_conflict", "research receipt already differs")
            return existing
        atomic_write_private_bytes(target, encoded)
        readback = _read_path(target, receipt_ref=ref)
        if readback["receipt_sha256"] != hashlib.sha256(encoded).hexdigest():
            _fail("receipt_artifact_invalid", "research receipt readback hash changed")
        return readback


def read_receipt_artifact(
    artifact_root: str | Path,
    experiment_id: object,
    kind: object,
) -> dict[str, Any]:
    identity = _identity(experiment_id, "experiment_id")
    if not isinstance(kind, str):
        _fail("receipt_kind_unsupported", "receipt kind is invalid")
    ref = _receipt_ref(identity, kind)
    target = private_path(artifact_root).joinpath(*ref.split("/"))
    if not target.is_file() or target.is_symlink():
        _fail("receipt_not_found", "research receipt does not exist")
    return _read_path(target, receipt_ref=ref)


__all__ = [
    "StrategyLabReceiptError",
    "build_research_receipt",
    "build_final_receipt",
    "publish_receipt",
    "read_receipt_artifact",
]
