from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping, NoReturn, Sequence, cast
from zoneinfo import ZoneInfo

from domain.domain.decision_state_fingerprint import canonical_sha256
from src.application.recommendation_point import (
    build_recommendation_point_id,
    strategy_lab_top1_available,
)
from src.application.scan_scheduler import scheduled_scan_targets_for_date
from src.application.shadow_replay.common import artifact_content_sha256, render_json_text
from src.application.strategy_lab.top1.contracts import (
    Top1CoreContractError,
    build_current_behavior_binding,
    build_research_spec_sha256,
    build_validation_spec_sha256,
    validate_experiment_spec,
)
from src.application.strategy_lab.top1.terminal_projection import (
    Publisher,
    build_aborted_receipt_request,
    build_generation_terminal_request,
    publish_exact_text,
    recover_terminal_projection,
)
from src.infrastructure.strategy_lab.experiment_store import (
    ExperimentStore,
    ExperimentStoreError,
    compact_json,
)


HIDDEN_WINDOW_COMMITMENT_SCHEMA = "sell_put_top1_hidden_window_commitment.v2"
PUBLIC_STATUS_SCHEMA = "sell_put_top1_experiment_status.v1"

_HIDDEN_DAY_FIELDS = frozenset(
    {
        "trading_date",
        "scheduled_scan_targets_market",
        "expected_recommendation_point_ids",
    }
)
_HIDDEN_COMMITMENT_FIELDS = frozenset(
    {
        "schema_version",
        "experiment_id",
        "market",
        "account",
        "strategy_family",
        "trading_dates",
        "start_trading_date",
        "end_trading_date",
        "market_calendar_version",
        "market_calendar_snapshot_ref",
        "market_calendar_snapshot_content_sha256",
        "market_calendar_snapshot_file_sha256",
        "market_calendar_coverage_start",
        "market_calendar_coverage_end",
        "schedule_config_sha256",
        "days",
        "point_selector",
        "capture_schema",
        "challenger_variant_id",
        "research_spec_sha256",
        "research_terminal_file_sha256",
        "behavior_binding_sha256",
    }
)

_HASH = re.compile(r"[0-9a-f]{64}\Z")
_PATH_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


class Top1LifecycleError(ValueError):
    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


def _fail(reason_code: str, message: str) -> NoReturn:
    raise Top1LifecycleError(reason_code, message)


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        _fail("experiment_invalid", f"{label} must be non-empty canonical text")
    return value


def _segment(value: object, label: str) -> str:
    text = _text(value, label)
    if _PATH_SEGMENT.fullmatch(text) is None:
        _fail("experiment_invalid", f"{label} must be a safe path segment")
    return text


def _hash(value: object, label: str) -> str:
    text = _text(value, label)
    if _HASH.fullmatch(text) is None:
        _fail("experiment_invalid", f"{label} must be a lowercase SHA-256")
    return text


def _ref(value: object, label: str) -> str:
    text = _text(value, label)
    if (
        text.startswith("/")
        or "\\" in text
        or any(part in {"", ".", ".."} for part in text.split("/"))
    ):
        _fail("experiment_invalid", f"{label} must be a safe relative POSIX path")
    return text


def _timestamp(value: object, label: str = "occurred_at_utc") -> str:
    text = _text(value, label)
    if not text.endswith("Z") or "T" not in text:
        _fail("experiment_invalid", f"{label} must be an ISO-8601 UTC timestamp")
    try:
        parsed = datetime.fromisoformat(f"{text[:-1]}+00:00")
    except ValueError:
        _fail("experiment_invalid", f"{label} must be an ISO-8601 UTC timestamp")
    if parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        _fail("experiment_invalid", f"{label} must be UTC")
    return text


def _trading_dates(values: Sequence[object]) -> list[str]:
    if isinstance(values, (str, bytes)) or len(values) != 20:
        _fail("experiment_invalid", "hidden commitment must contain exactly 20 dates")
    parsed: list[date] = []
    texts: list[str] = []
    for index, value in enumerate(values):
        text = _text(value, f"trading_dates[{index}]")
        try:
            item = date.fromisoformat(text)
        except ValueError:
            _fail("experiment_invalid", "trading dates must be canonical ISO dates")
        if item.isoformat() != text:
            _fail("experiment_invalid", "trading dates must be canonical ISO dates")
        parsed.append(item)
        texts.append(text)
    if any(left >= right for left, right in zip(parsed, parsed[1:])):
        _fail("experiment_invalid", "trading dates must be strictly increasing")
    return texts


def _iso_date(value: object, label: str) -> str:
    text = _text(value, label)
    try:
        parsed = date.fromisoformat(text)
    except ValueError:
        _fail("experiment_invalid", f"{label} must be a canonical ISO date")
    if parsed.isoformat() != text:
        _fail("experiment_invalid", f"{label} must be a canonical ISO date")
    return text


def _utc_datetime(value: object, label: str) -> datetime:
    text = _timestamp(value, label)
    return datetime.fromisoformat(f"{text[:-1]}+00:00")


def _identity(market: object, account: object) -> tuple[str, str]:
    if market != "HK":
        _fail("experiment_invalid", "market must equal HK")
    account_text = _text(account, "account")
    if account_text != account_text.lower():
        _fail("experiment_invalid", "account must be lowercase")
    return "HK", account_text


def _command_fields(
    actor: object, occurred_at_utc: object, idempotency_key: object
) -> tuple[str, str, str]:
    return (
        _text(actor, "actor"),
        _timestamp(occurred_at_utc),
        _segment(idempotency_key, "idempotency_key"),
    )


def _derived_key(*parts: str) -> str:
    return hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()


def _raise_store(exc: ExperimentStoreError) -> NoReturn:
    mapping = {
        "schema_unsupported": "schema_unsupported",
        "invalid_transition": "invalid_transition",
        "authorization_required": "authorization_required",
        "authorization_hash_mismatch": "authorization_hash_mismatch",
        "hidden_window_overlap": "hidden_window_overlap",
        "validation_slot_occupied": "validation_slot_occupied",
        "generation_conflict": "generation_conflict",
        "generation_not_found": "generation_conflict",
        "late_write": "late_write",
        "terminal_conflict": "terminal_conflict",
        "projection_conflict": "projection_conflict",
        "stale_snapshot": "generation_conflict",
    }
    reason = mapping.get(exc.reason_code, "experiment_conflict")
    raise Top1LifecycleError(reason, str(exc)) from exc


def _call(function: Any, /, *args: Any, **kwargs: Any) -> Any:
    try:
        return function(*args, **kwargs)
    except ExperimentStoreError as exc:
        _raise_store(exc)


def _recover_projection(
    store: ExperimentStore,
    artifact_root: str | Path,
    *,
    experiment_id: str | None = None,
    publisher: Publisher | None = None,
) -> None:
    try:
        recover_terminal_projection(
            store,
            artifact_root,
            experiment_id=experiment_id,
            publisher=publisher,
        )
    except ExperimentStoreError as exc:
        _raise_store(exc)
    except (OSError, ValueError) as exc:
        _fail("projection_conflict", str(exc))


def effective_feature_status(
    store: ExperimentStore,
    *,
    market: str,
    account: str,
    environ: Mapping[str, str] | None = None,
) -> dict[str, object]:
    market, account = _identity(market, account)
    try:
        feature = store.feature(market, account)
    except ExperimentStoreError as exc:
        _raise_store(exc)
    maintainer_available = strategy_lab_top1_available(environ)
    user_opt_in = bool(feature and feature["user_opt_in"])
    return {
        "maintainer_available": maintainer_available,
        "user_opt_in": user_opt_in,
        "effective": maintainer_available and user_opt_in,
    }


def _require_effective(
    store: ExperimentStore,
    *,
    market: str,
    account: str,
    actor: str,
    occurred_at_utc: str,
    idempotency_key: str,
    artifact_root: str | Path,
    environ: Mapping[str, str] | None,
) -> None:
    status = effective_feature_status(
        store, market=market, account=account, environ=environ
    )
    if status["effective"]:
        return
    scope = "maintainer" if not status["maintainer_available"] else "user"
    reconcile_disabled_experiments(
        store,
        market=market,
        account=account,
        disabled_scope=scope,
        actor=actor,
        occurred_at_utc=occurred_at_utc,
        idempotency_key=_derived_key(idempotency_key, "gate-disable"),
        artifact_root=artifact_root,
    )
    _fail("feature_disabled", "Strategy Lab Top1 is disabled")


def set_account_opt_in(
    store: ExperimentStore,
    *,
    market: str,
    account: str,
    enabled: bool,
    actor: str,
    occurred_at_utc: str,
    idempotency_key: str,
    artifact_root: str | Path,
    environ: Mapping[str, str] | None = None,
) -> dict[str, object]:
    market, account = _identity(market, account)
    actor, occurred_at_utc, idempotency_key = _command_fields(
        actor, occurred_at_utc, idempotency_key
    )
    if type(enabled) is not bool:
        _fail("experiment_invalid", "enabled must be boolean")
    if enabled and not strategy_lab_top1_available(environ):
        _fail("feature_disabled", "maintainer availability is off")
    _call(
        store.set_feature,
        market=market,
        account=account,
        enabled=enabled,
        actor=actor,
        occurred_at_utc=occurred_at_utc,
        idempotency_key=idempotency_key,
    )
    if not enabled:
        reconcile_disabled_experiments(
            store,
            market=market,
            account=account,
            disabled_scope="user",
            actor=actor,
            occurred_at_utc=occurred_at_utc,
            idempotency_key=_derived_key(idempotency_key, "user-disable"),
            artifact_root=artifact_root,
        )
    return effective_feature_status(
        store, market=market, account=account, environ=environ
    )


def prepare_experiment(
    store: ExperimentStore,
    spec: object,
    *,
    provenance: Mapping[str, object],
    actor: str,
    occurred_at_utc: str,
    idempotency_key: str,
    artifact_root: str | Path,
    environ: Mapping[str, str] | None = None,
) -> dict[str, object]:
    actor, occurred_at_utc, idempotency_key = _command_fields(
        actor, occurred_at_utc, idempotency_key
    )
    try:
        validated = validate_experiment_spec(spec)
    except Top1CoreContractError as exc:
        _fail("experiment_invalid", str(exc))
    if "validation_evaluation" in validated:
        _fail("experiment_invalid", "prepare requires a research-only ExperimentSpec")
    experiment_id = _segment(validated["experiment_id"], "experiment_id")
    topic_id = _text(validated["topic_id"], "topic_id")
    market, account = _identity(validated["market"], validated["account"])
    _require_effective(
        store,
        market=market,
        account=account,
        actor=actor,
        occurred_at_utc=occurred_at_utc,
        idempotency_key=idempotency_key,
        artifact_root=artifact_root,
        environ=environ,
    )
    if not isinstance(provenance, Mapping) or not provenance:
        _fail("experiment_invalid", "provenance must be a non-empty mapping")
    try:
        provenance_json = compact_json(dict(provenance))
    except (TypeError, ValueError) as exc:
        _fail("experiment_invalid", f"provenance is not canonical JSON: {exc}")
    return _call(
        store.prepare_experiment,
        experiment_id=experiment_id,
        topic_id=topic_id,
        market=market,
        account=account,
        spec_json=compact_json(validated),
        research_spec_sha256=build_research_spec_sha256(validated),
        provenance_json=provenance_json,
        actor=actor,
        occurred_at_utc=occurred_at_utc,
        idempotency_key=idempotency_key,
    )


def authorize_research(
    store: ExperimentStore,
    *,
    experiment_id: str,
    research_spec_sha256: str,
    actor: str,
    occurred_at_utc: str,
    idempotency_key: str,
    artifact_root: str | Path,
    environ: Mapping[str, str] | None = None,
) -> dict[str, object]:
    return _authorize(
        store,
        experiment_id=experiment_id,
        stage="research",
        authorized_hash=research_spec_sha256,
        actor=actor,
        occurred_at_utc=occurred_at_utc,
        idempotency_key=idempotency_key,
        artifact_root=artifact_root,
        environ=environ,
    )


def authorize_validation(
    store: ExperimentStore,
    *,
    experiment_id: str,
    validation_spec_sha256: str,
    actor: str,
    occurred_at_utc: str,
    idempotency_key: str,
    artifact_root: str | Path,
    environ: Mapping[str, str] | None = None,
) -> dict[str, object]:
    return _authorize(
        store,
        experiment_id=experiment_id,
        stage="validation",
        authorized_hash=validation_spec_sha256,
        actor=actor,
        occurred_at_utc=occurred_at_utc,
        idempotency_key=idempotency_key,
        artifact_root=artifact_root,
        environ=environ,
    )


def _authorize(
    store: ExperimentStore,
    *,
    experiment_id: str,
    stage: str,
    authorized_hash: str,
    actor: str,
    occurred_at_utc: str,
    idempotency_key: str,
    artifact_root: str | Path,
    environ: Mapping[str, str] | None,
) -> dict[str, object]:
    experiment_id = _segment(experiment_id, "experiment_id")
    authorized_hash = _hash(authorized_hash, f"{stage}_spec_sha256")
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
    return _call(
        store.authorize,
        experiment_id=experiment_id,
        stage=stage,
        authorized_hash=authorized_hash,
        actor=actor,
        occurred_at_utc=occurred_at_utc,
        idempotency_key=idempotency_key,
    )


def start_research(
    store: ExperimentStore,
    *,
    experiment_id: str,
    research_spec_sha256: str,
    actor: str,
    occurred_at_utc: str,
    idempotency_key: str,
    artifact_root: str | Path,
    environ: Mapping[str, str] | None = None,
) -> dict[str, object]:
    experiment_id = _segment(experiment_id, "experiment_id")
    research_spec_sha256 = _hash(research_spec_sha256, "research_spec_sha256")
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
    spec = json.loads(str(experiment["spec_json"]))
    source = spec["research_source"]
    return _call(
        store.start_research,
        experiment_id=experiment_id,
        authorized_hash=research_spec_sha256,
        dataset_ref=_ref(source["dataset_ref"], "research_source.dataset_ref"),
        dataset_file_sha256=_hash(
            source["dataset_sha256"], "research_source.dataset_sha256"
        ),
        frozen_row_sha256=_hash(
            source["dataset_sha256"], "research_source.dataset_sha256"
        ),
        actor=actor,
        occurred_at_utc=occurred_at_utc,
        idempotency_key=idempotency_key,
    )


def record_generation_revision(
    store: ExperimentStore,
    *,
    experiment_id: str,
    generation_kind: str,
    revision: int,
    revision_ref: str,
    revision_file_sha256: str,
    frozen_row_sha256: str,
    actor: str,
    occurred_at_utc: str,
    idempotency_key: str,
    artifact_root: str | Path,
    environ: Mapping[str, str] | None = None,
) -> dict[str, object]:
    experiment_id = _segment(experiment_id, "experiment_id")
    if generation_kind not in {"research", "hidden", "outcome"}:
        _fail("experiment_invalid", "generation_kind is unsupported")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision <= 0:
        _fail("experiment_invalid", "revision must be a positive integer")
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
    return _call(
        store.record_generation_revision,
        experiment_id=experiment_id,
        generation_kind=generation_kind,
        revision=revision,
        revision_ref=_ref(revision_ref, "revision_ref"),
        revision_file_sha256=_hash(
            revision_file_sha256, "revision_file_sha256"
        ),
        frozen_row_sha256=_hash(frozen_row_sha256, "frozen_row_sha256"),
        actor=actor,
        occurred_at_utc=occurred_at_utc,
        idempotency_key=idempotency_key,
    )


def seal_generation(
    store: ExperimentStore,
    *,
    experiment_id: str,
    generation_kind: str,
    actor: str,
    occurred_at_utc: str,
    idempotency_key: str,
    artifact_root: str | Path,
    environ: Mapping[str, str] | None = None,
) -> dict[str, object]:
    experiment_id = _segment(experiment_id, "experiment_id")
    if generation_kind != "research":
        _fail(
            "experiment_invalid",
            "W3 seal_generation only accepts research; hidden seals at day 20",
        )
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
    generation = next(
        (
            item
            for item in _call(store.generations, experiment_id)
            if item["generation_kind"] == generation_kind
        ),
        None,
    )
    if generation is None:
        _fail("generation_conflict", "generation does not exist")
    request = build_generation_terminal_request(
        generation,
        terminal_mode="completed",
        reason=None,
        disabled_scope=None,
        occurred_at_utc=occurred_at_utc,
    )
    return _call(
        store.request_generation_terminal,
        experiment_id=experiment_id,
        generation_kind=generation_kind,
        request=request,
        actor=actor,
        occurred_at_utc=occurred_at_utc,
        idempotency_key=idempotency_key,
    )


def build_hidden_window_commitment(
    *,
    experiment_id: str,
    account: str,
    validation_start_trading_date: str,
    market_calendar_binding: Mapping[str, object],
    schedule: Mapping[str, Any],
    challenger_variant_id: str,
    research_spec_sha256: str,
    research_terminal_file_sha256: str,
    behavior_binding_sha256: str,
) -> dict[str, object]:
    experiment_id = _segment(experiment_id, "experiment_id")
    _, account = _identity("HK", account)
    start = _iso_date(
        validation_start_trading_date, "validation_start_trading_date"
    )
    if not isinstance(market_calendar_binding, Mapping):
        _fail("experiment_invalid", "market calendar binding must be an object")
    if market_calendar_binding.get("market") != "HK":
        _fail("experiment_invalid", "market calendar binding must be for HK")
    raw_dates = market_calendar_binding.get("trading_dates")
    if not isinstance(raw_dates, list):
        _fail("experiment_invalid", "market calendar trading dates are missing")
    calendar_dates = [
        _iso_date(value, f"market_calendar_binding.trading_dates[{index}]")
        for index, value in enumerate(raw_dates)
    ]
    if calendar_dates != sorted(set(calendar_dates)):
        _fail("experiment_invalid", "market calendar trading dates are invalid")
    raw_sessions = market_calendar_binding.get("trading_sessions")
    if not isinstance(raw_sessions, list):
        _fail("experiment_invalid", "market calendar trading sessions are missing")
    session_dates: list[str] = []
    session_types: list[str] = []
    for index, raw_session in enumerate(raw_sessions):
        if not isinstance(raw_session, Mapping) or set(raw_session) != {
            "trading_date",
            "trade_date_type",
        }:
            _fail("experiment_invalid", "market calendar trading sessions are invalid")
        session_dates.append(
            _iso_date(
                raw_session["trading_date"],
                f"market_calendar_binding.trading_sessions[{index}].trading_date",
            )
        )
        session_types.append(
            _text(
                raw_session["trade_date_type"],
                f"market_calendar_binding.trading_sessions[{index}].trade_date_type",
            )
        )
    if session_dates != calendar_dates or any(
        value not in {"WHOLE", "MORNING", "AFTERNOON"} for value in session_types
    ):
        _fail("experiment_invalid", "market calendar trading sessions are invalid")
    session_by_date = dict(zip(session_dates, session_types, strict=True))
    try:
        start_index = calendar_dates.index(start)
    except ValueError:
        _fail("experiment_invalid", "validation start is not a trading date")
    dates = calendar_dates[start_index : start_index + 20]
    if len(dates) != 20:
        _fail("experiment_invalid", "market calendar does not cover 20 trading days")
    if not isinstance(schedule, Mapping) or schedule.get("timezone") != (
        "Asia/Hong_Kong"
    ):
        _fail("experiment_invalid", "schedule must use Asia/Hong_Kong")
    schedule_payload = dict(schedule)
    schedule_hash = canonical_sha256(schedule_payload)
    days: list[dict[str, object]] = []
    for day in dates:
        try:
            targets = [
                target.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
                for target in scheduled_scan_targets_for_date(
                    schedule_payload,
                    day,
                    trade_date_type=session_by_date[day],
                )
            ]
        except (TypeError, ValueError) as exc:
            raise Top1LifecycleError(
                "experiment_invalid", "schedule is invalid"
            ) from exc
        if not targets:
            _fail("experiment_invalid", "committed trading day has no scan target")
        days.append(
            {
                "trading_date": day,
                "scheduled_scan_targets_market": targets,
                "expected_recommendation_point_ids": [
                    build_recommendation_point_id("HK", account, target)
                    for target in targets
                ],
            }
        )
    payload: dict[str, object] = {
        "schema_version": HIDDEN_WINDOW_COMMITMENT_SCHEMA,
        "experiment_id": experiment_id,
        "market": "HK",
        "account": account,
        "strategy_family": "sell_put",
        "trading_dates": dates,
        "start_trading_date": dates[0],
        "end_trading_date": dates[-1],
        "market_calendar_version": _text(
            market_calendar_binding.get("market_calendar_version"),
            "market_calendar_version",
        ),
        "market_calendar_snapshot_ref": _ref(
            market_calendar_binding.get("snapshot_ref"),
            "market_calendar_snapshot_ref",
        ),
        "market_calendar_snapshot_content_sha256": _hash(
            market_calendar_binding.get("snapshot_content_sha256"),
            "market_calendar_snapshot_content_sha256",
        ),
        "market_calendar_snapshot_file_sha256": _hash(
            market_calendar_binding.get("snapshot_file_sha256"),
            "market_calendar_snapshot_file_sha256",
        ),
        "market_calendar_coverage_start": _iso_date(
            market_calendar_binding.get("coverage_start"),
            "market_calendar_coverage_start",
        ),
        "market_calendar_coverage_end": _iso_date(
            market_calendar_binding.get("coverage_end"),
            "market_calendar_coverage_end",
        ),
        "schedule_config_sha256": schedule_hash,
        "days": days,
        "point_selector": "official_scheduled_sell_put.v1",
        "capture_schema": "recommendation_point.v1",
        "challenger_variant_id": _text(
            challenger_variant_id, "challenger_variant_id"
        ),
        "research_spec_sha256": _hash(
            research_spec_sha256, "research_spec_sha256"
        ),
        "research_terminal_file_sha256": _hash(
            research_terminal_file_sha256, "research_terminal_file_sha256"
        ),
        "behavior_binding_sha256": _hash(
            behavior_binding_sha256, "behavior_binding_sha256"
        ),
    }
    return payload


def validate_hidden_window_commitment(
    payload: object,
    *,
    expected_experiment_id: str | None = None,
    expected_account: str | None = None,
) -> dict[str, object]:
    if not isinstance(payload, Mapping):
        _fail("experiment_conflict", "hidden commitment must be an object")
    item = dict(payload)
    if set(item) != _HIDDEN_COMMITMENT_FIELDS:
        _fail("experiment_conflict", "hidden commitment keys are invalid")
    if item["schema_version"] != HIDDEN_WINDOW_COMMITMENT_SCHEMA:
        _fail("experiment_conflict", "hidden commitment schema is invalid")
    experiment_id = _segment(item["experiment_id"], "experiment_id")
    market, account = _identity(item["market"], item["account"])
    if expected_experiment_id is not None and experiment_id != expected_experiment_id:
        _fail("experiment_conflict", "hidden commitment experiment changed")
    if expected_account is not None and account != expected_account:
        _fail("experiment_conflict", "hidden commitment account changed")
    if item["strategy_family"] != "sell_put":
        _fail("experiment_conflict", "hidden commitment strategy changed")
    dates = _trading_dates(cast(Sequence[object], item["trading_dates"]))
    if item["start_trading_date"] != dates[0] or item["end_trading_date"] != dates[-1]:
        _fail("experiment_conflict", "hidden commitment date bounds changed")
    coverage_start = _iso_date(
        item["market_calendar_coverage_start"], "market_calendar_coverage_start"
    )
    coverage_end = _iso_date(
        item["market_calendar_coverage_end"], "market_calendar_coverage_end"
    )
    if coverage_start > dates[0] or coverage_end < dates[-1]:
        _fail("experiment_conflict", "hidden commitment exceeds calendar coverage")
    _text(item["market_calendar_version"], "market_calendar_version")
    snapshot_content_hash = _hash(
        item["market_calendar_snapshot_content_sha256"],
        "market_calendar_snapshot_content_sha256",
    )
    if item["market_calendar_snapshot_ref"] != (
        "strategy_lab/top1/capabilities/market-calendar/"
        f"{market.lower()}/snapshots/{snapshot_content_hash}.json"
    ):
        _fail("experiment_conflict", "calendar snapshot ref is not content-addressed")
    _hash(
        item["market_calendar_snapshot_file_sha256"],
        "market_calendar_snapshot_file_sha256",
    )
    _hash(item["schedule_config_sha256"], "schedule_config_sha256")
    raw_days = item["days"]
    if not isinstance(raw_days, list) or len(raw_days) != 20:
        _fail("experiment_conflict", "hidden commitment must contain 20 day entries")
    days: list[dict[str, object]] = []
    for index, raw_day in enumerate(raw_days):
        if not isinstance(raw_day, Mapping) or set(raw_day) != _HIDDEN_DAY_FIELDS:
            _fail("experiment_conflict", "hidden commitment day is invalid")
        day = dict(raw_day)
        if day["trading_date"] != dates[index]:
            _fail("experiment_conflict", "hidden commitment day order changed")
        targets = day["scheduled_scan_targets_market"]
        point_ids = day["expected_recommendation_point_ids"]
        if not isinstance(targets, list) or not targets or not isinstance(point_ids, list):
            _fail("experiment_conflict", "hidden commitment denominator is invalid")
        canonical_targets = [
            _timestamp(target, f"days[{index}].targets[{target_index}]")
            for target_index, target in enumerate(targets)
        ]
        if canonical_targets != sorted(set(canonical_targets)):
            _fail("experiment_conflict", "hidden commitment targets changed")
        if any(
            _utc_datetime(target, "scheduled_scan_target_market")
            .astimezone(ZoneInfo("Asia/Hong_Kong"))
            .date()
            .isoformat()
            != dates[index]
            for target in canonical_targets
        ):
            _fail("experiment_conflict", "hidden commitment target date changed")
        expected_ids = [
            build_recommendation_point_id(market, account, target)
            for target in canonical_targets
        ]
        if point_ids != expected_ids:
            _fail("experiment_conflict", "hidden commitment point IDs changed")
        days.append(
            {
                "trading_date": dates[index],
                "scheduled_scan_targets_market": canonical_targets,
                "expected_recommendation_point_ids": expected_ids,
            }
        )
    if item["point_selector"] != "official_scheduled_sell_put.v1" or item[
        "capture_schema"
    ] != "recommendation_point.v1":
        _fail("experiment_conflict", "hidden commitment point contract changed")
    _text(item["challenger_variant_id"], "challenger_variant_id")
    _hash(item["research_spec_sha256"], "research_spec_sha256")
    _hash(
        item["research_terminal_file_sha256"],
        "research_terminal_file_sha256",
    )
    _hash(item["behavior_binding_sha256"], "behavior_binding_sha256")
    return {**item, "trading_dates": dates, "days": days}


def lock_challenger(
    store: ExperimentStore,
    validation_spec: object,
    *,
    challenger_variant_id: str,
    validation_start_trading_date: str,
    schedule: Mapping[str, Any],
    actor: str,
    occurred_at_utc: str,
    idempotency_key: str,
    artifact_root: str | Path,
    environ: Mapping[str, str] | None = None,
) -> dict[str, object]:
    # Local imports avoid corpus -> lifecycle -> research -> corpus initialization.
    from src.application.strategy_lab.top1.research import (
        ResearchEvaluationError,
        validate_internal_research_revision,
    )
    from src.application.strategy_lab.top1.research_artifacts import (
        ResearchArtifactError,
        load_materialized_research_input,
        load_recorded_research_revision,
    )
    from src.application.strategy_lab.top1.corpus import (
        CorpusError,
        read_market_calendar_binding,
    )

    actor, occurred_at_utc, idempotency_key = _command_fields(
        actor, occurred_at_utc, idempotency_key
    )
    challenger_variant_id = _text(challenger_variant_id, "challenger_variant_id")
    if challenger_variant_id == "baseline":
        _fail("experiment_invalid", "challenger must be non-baseline")
    try:
        spec = validate_experiment_spec(validation_spec)
    except Top1CoreContractError as exc:
        _fail("experiment_invalid", str(exc))
    if "validation_evaluation" not in spec:
        _fail("experiment_invalid", "validation-ready ExperimentSpec is required")
    experiment_id = _segment(spec["experiment_id"], "experiment_id")
    market, account = _identity(spec["market"], spec["account"])
    experiment = _call(store.experiment, experiment_id)
    _require_effective(
        store,
        market=market,
        account=account,
        actor=actor,
        occurred_at_utc=occurred_at_utc,
        idempotency_key=idempotency_key,
        artifact_root=artifact_root,
        environ=environ,
    )
    research_hash = build_research_spec_sha256(spec)
    if research_hash != experiment["research_spec_sha256"]:
        _fail("experiment_conflict", "research hash changed after start")
    research_generation = next(
        (
            item
            for item in _call(store.generations, experiment_id)
            if item["generation_kind"] == "research"
        ),
        None,
    )
    if (
        research_generation is None
        or int(research_generation["revision"]) != 1
        or research_generation["terminal_file_sha256"] is None
        or research_generation["terminal_published_event_id"] is None
    ):
        _fail("invalid_transition", "published research terminal is required")
    try:
        research_spec = {
            key: value
            for key, value in spec.items()
            if key
            not in {
                "validation_evaluation",
                "fill_observation",
                "timer_binding",
                "validation_metrics",
            }
        }
        dataset = load_materialized_research_input(
            artifact_root, research_spec
        )
        revision = load_recorded_research_revision(
            artifact_root, research_generation
        )
        validated_revision = validate_internal_research_revision(dataset, revision)
    except (ResearchArtifactError, ResearchEvaluationError) as exc:
        _fail("experiment_conflict", f"research revision is invalid: {exc}")
    evaluation = cast(Mapping[str, object], validated_revision["evaluation"])
    if evaluation["selection"] != "research_leader":
        _fail("invalid_transition", "research did not select a challenger")
    if evaluation["leader_variant_id"] != challenger_variant_id:
        _fail("experiment_invalid", "challenger does not match the research leader")
    research_receipt_ref = _ref(
        research_generation["last_revision_ref"], "research_receipt_ref"
    )
    research_receipt_file_sha256 = _hash(
        research_generation["last_revision_file_sha256"],
        "research_receipt_file_sha256",
    )
    economics = cast(Mapping[str, object], spec["economics_contracts"])
    baseline = cast(Mapping[str, object], spec["baseline"])
    try:
        calendar_binding = read_market_calendar_binding(
            artifact_root, market=market
        )
    except CorpusError as exc:
        raise Top1LifecycleError(exc.reason_code, str(exc)) from exc
    if calendar_binding["market_calendar_version"] != economics[
        "market_calendar_version"
    ]:
        _fail("experiment_invalid", "calendar version does not match ExperimentSpec")
    commitment = build_hidden_window_commitment(
        experiment_id=experiment_id,
        account=account,
        validation_start_trading_date=validation_start_trading_date,
        market_calendar_binding=calendar_binding,
        schedule=schedule,
        challenger_variant_id=challenger_variant_id,
        research_spec_sha256=research_hash,
        research_terminal_file_sha256=str(
            research_generation["terminal_file_sha256"]
        ),
        behavior_binding_sha256=str(baseline["behavior_binding_sha256"]),
    )
    first_target = cast(list[dict[str, object]], commitment["days"])[0][
        "scheduled_scan_targets_market"
    ]
    assert isinstance(first_target, list)
    if _utc_datetime(first_target[0], "first_target_at_utc") <= _utc_datetime(
        occurred_at_utc, "occurred_at_utc"
    ):
        _fail("experiment_invalid", "validation first target must be future")
    commitment_sha256 = canonical_sha256(commitment)
    commitment_text = render_json_text(commitment)
    commitment_file_sha256 = hashlib.sha256(
        commitment_text.encode("utf-8")
    ).hexdigest()
    commitment_ref = (
        f"strategy_lab/top1/experiments/{experiment_id}/hidden_window_commitments/"
        f"{commitment_sha256}.json"
    )
    validation_hash = build_validation_spec_sha256(
        spec,
        research_terminal_sha256=str(research_generation["terminal_file_sha256"]),
        challenger_variant_id=challenger_variant_id,
        hidden_window_commitment_sha256=commitment_sha256,
    )
    variants = {
        str(cast(Mapping[str, object], item)["variant_id"])
        for item in cast(list[object], spec["variants"])
    }
    if challenger_variant_id not in variants:
        _fail("experiment_invalid", "system leader is not an ExperimentSpec variant")
    return _call(
        store.lock_challenger,
        experiment_id=experiment_id,
        spec_json=compact_json(spec),
        research_spec_sha256=research_hash,
        validation_spec_sha256=validation_hash,
        research_leader=challenger_variant_id,
        research_receipt_ref=research_receipt_ref,
        research_receipt_file_sha256=research_receipt_file_sha256,
        commitment_json=compact_json(commitment),
        commitment_sha256=commitment_sha256,
        commitment_ref=commitment_ref,
        commitment_content_sha256=artifact_content_sha256(commitment),
        commitment_file_sha256=commitment_file_sha256,
        actor=actor,
        occurred_at_utc=occurred_at_utc,
        idempotency_key=idempotency_key,
    )


def _validate_commitment_calendar(
    artifact_root: str | Path,
    commitment: Mapping[str, object],
) -> dict[str, Any]:
    from src.application.strategy_lab.top1.corpus import (
        CorpusError,
        read_bound_market_calendar_snapshot,
    )

    try:
        binding = read_bound_market_calendar_snapshot(
            artifact_root,
            market=str(commitment["market"]),
            snapshot_ref=str(commitment["market_calendar_snapshot_ref"]),
            snapshot_content_sha256=str(
                commitment["market_calendar_snapshot_content_sha256"]
            ),
            snapshot_file_sha256=str(
                commitment["market_calendar_snapshot_file_sha256"]
            ),
        )
    except CorpusError as exc:
        raise Top1LifecycleError(exc.reason_code, str(exc)) from exc
    expected = {
        "market_calendar_version": commitment["market_calendar_version"],
        "coverage_start": commitment["market_calendar_coverage_start"],
        "coverage_end": commitment["market_calendar_coverage_end"],
    }
    if any(binding[key] != value for key, value in expected.items()):
        _fail("experiment_conflict", "committed calendar binding changed")
    calendar_dates = cast(list[str], binding["trading_dates"])
    start = str(commitment["start_trading_date"])
    try:
        start_index = calendar_dates.index(start)
    except ValueError:
        _fail("experiment_conflict", "committed start is absent from calendar")
    if calendar_dates[start_index : start_index + 20] != commitment["trading_dates"]:
        _fail("experiment_conflict", "committed dates are not consecutive")
    return binding


def start_validation(
    store: ExperimentStore,
    *,
    experiment_id: str,
    validation_spec_sha256: str,
    actor: str,
    occurred_at_utc: str,
    idempotency_key: str,
    artifact_root: str | Path,
    environ: Mapping[str, str] | None = None,
) -> dict[str, object]:
    experiment_id = _segment(experiment_id, "experiment_id")
    validation_spec_sha256 = _hash(
        validation_spec_sha256, "validation_spec_sha256"
    )
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
    if experiment["terminal_mode"] is not None or not (
        experiment["phase"] == "research"
        and experiment["research_progress"] == "challenger_locked"
    ):
        _fail("invalid_transition", "validation cannot start")
    if (
        experiment["validation_authorization_status"] != "confirmed"
        or experiment["validation_authorized_hash"] != validation_spec_sha256
        or experiment["validation_spec_sha256"] != validation_spec_sha256
    ):
        _fail(
            "authorization_required",
            "current validation hash is not confirmed",
        )
    try:
        commitment = validate_hidden_window_commitment(
            json.loads(str(experiment["proposed_commitment_json"])),
            expected_experiment_id=experiment_id,
            expected_account=str(experiment["account"]),
        )
    except json.JSONDecodeError as exc:
        raise Top1LifecycleError(
            "experiment_conflict", "commitment JSON is invalid"
        ) from exc
    text = render_json_text(commitment)
    if canonical_sha256(commitment) != experiment["proposed_commitment_sha256"]:
        _fail("experiment_conflict", "commitment semantic hash changed")
    if artifact_content_sha256(commitment) != experiment[
        "proposed_commitment_content_sha256"
    ]:
        _fail("experiment_conflict", "commitment content hash changed")
    expected_ref = (
        f"strategy_lab/top1/experiments/{experiment_id}/hidden_window_commitments/"
        f"{experiment['proposed_commitment_sha256']}.json"
    )
    if experiment["proposed_commitment_ref"] != expected_ref:
        _fail("experiment_conflict", "commitment ref is not content-addressed")
    if hashlib.sha256(text.encode("utf-8")).hexdigest() != experiment[
        "proposed_commitment_file_sha256"
    ]:
        _fail("experiment_conflict", "commitment file hash changed")
    _validate_commitment_calendar(artifact_root, commitment)
    first_target = cast(list[dict[str, object]], commitment["days"])[0][
        "scheduled_scan_targets_market"
    ]
    assert isinstance(first_target, list)
    if _utc_datetime(first_target[0], "first_target_at_utc") <= _utc_datetime(
        occurred_at_utc, "occurred_at_utc"
    ):
        _fail("invalid_transition", "validation first target is no longer future")
    try:
        publish_exact_text(
            artifact_root,
            str(experiment["proposed_commitment_ref"]),
            text.encode("utf-8"),
        )
    except (OSError, ValueError) as exc:
        _fail("experiment_conflict", f"commitment publication failed: {exc}")
    return _call(
        store.start_validation,
        experiment_id=experiment_id,
        authorized_hash=validation_spec_sha256,
        commitment_sha256=str(experiment["proposed_commitment_sha256"]),
        commitment_dates=cast(list[str], commitment["trading_dates"]),
        actor=actor,
        occurred_at_utc=occurred_at_utc,
        idempotency_key=idempotency_key,
    )


def terminate_experiment(
    store: ExperimentStore,
    *,
    experiment_id: str,
    reason: str,
    disabled_scope: str | None,
    actor: str,
    occurred_at_utc: str,
    idempotency_key: str,
    artifact_root: str | Path,
    publisher: Publisher | None = None,
) -> dict[str, object]:
    experiment_id = _segment(experiment_id, "experiment_id")
    actor, occurred_at_utc, idempotency_key = _command_fields(
        actor, occurred_at_utc, idempotency_key
    )
    if reason not in {
        "human_abandoned",
        "behavior_binding_drift",
        "experimental_feature_disabled",
    }:
        _fail("experiment_invalid", "termination reason is unsupported")
    if reason == "experimental_feature_disabled":
        if disabled_scope not in {"user", "maintainer"}:
            _fail("experiment_invalid", "feature disable requires disabled_scope")
    elif disabled_scope is not None:
        _fail("experiment_invalid", "disabled_scope is only valid for feature disable")

    for _ in range(3):
        experiment = _call(store.experiment, experiment_id)
        if experiment["terminal_mode"] is not None:
            if (
                experiment["terminal_reason"] != reason
                or experiment["disabled_scope"] != disabled_scope
                or experiment["terminal_at_utc"] != occurred_at_utc
            ):
                _fail("terminal_conflict", "experiment terminal intent already differs")
            _recover_projection(
                store, artifact_root, experiment_id=experiment_id, publisher=publisher
            )
            return _call(store.experiment, experiment_id)
        generations = _call(store.generations, experiment_id)
        generation_requests = [
            build_generation_terminal_request(
                generation,
                terminal_mode="aborted",
                reason=reason,
                disabled_scope=disabled_scope,
                occurred_at_utc=occurred_at_utc,
                partial_summary={
                    "revision": generation["revision"],
                    "completed_validation_partitions": experiment[
                        "completed_validation_partitions"
                    ],
                },
            )
            for generation in generations
            if generation["terminal_request_event_id"] is None
        ]
        partition = (
            int(experiment["completed_validation_partitions"])
            if experiment["phase"] == "validation"
            else None
        )
        receipt_request = build_aborted_receipt_request(
            experiment,
            generations,
            generation_requests,
            reason=reason,
            disabled_scope=disabled_scope,
            occurred_at_utc=occurred_at_utc,
            terminated_at_partition=partition,
        )
        try:
            _call(
                store.terminate,
                experiment_id=experiment_id,
                expected_state_version=int(experiment["state_version"]),
                reason=reason,
                disabled_scope=disabled_scope,
                terminated_at_partition=partition,
                generation_requests=generation_requests,
                receipt_request=receipt_request,
                actor=actor,
                occurred_at_utc=occurred_at_utc,
                idempotency_key=idempotency_key,
            )
            break
        except Top1LifecycleError as exc:
            if exc.reason_code != "generation_conflict":
                raise
    else:
        _fail("terminal_conflict", "experiment changed during termination")
    _recover_projection(
        store, artifact_root, experiment_id=experiment_id, publisher=publisher
    )
    return _call(store.experiment, experiment_id)


def reconcile_disabled_experiments(
    store: ExperimentStore,
    *,
    market: str,
    account: str,
    disabled_scope: str,
    actor: str,
    occurred_at_utc: str,
    idempotency_key: str,
    artifact_root: str | Path,
) -> list[str]:
    market, account = _identity(market, account)
    actor, occurred_at_utc, idempotency_key = _command_fields(
        actor, occurred_at_utc, idempotency_key
    )
    if disabled_scope not in {"user", "maintainer"}:
        _fail("experiment_invalid", "disabled_scope is unsupported")
    recover_account_terminal_projections(
        store,
        artifact_root,
        market=market,
        account=account,
    )
    experiment_ids: list[str] = []
    for experiment in _call(store.active_experiments, market, account):
        experiment_id = str(experiment["experiment_id"])
        terminate_experiment(
            store,
            experiment_id=experiment_id,
            reason="experimental_feature_disabled",
            disabled_scope=disabled_scope,
            actor=actor,
            occurred_at_utc=occurred_at_utc,
            idempotency_key=_derived_key(
                idempotency_key, experiment_id, "feature-disable"
            ),
            artifact_root=artifact_root,
        )
        experiment_ids.append(experiment_id)
    return experiment_ids


def recover_account_terminal_projections(
    store: ExperimentStore,
    artifact_root: str | Path,
    *,
    market: str,
    account: str,
    publisher: Publisher | None = None,
) -> list[str]:
    market, account = _identity(market, account)
    pending_ids = {
        str(event["experiment_id"])
        for event in _call(store.pending_projections)
        if event["experiment_id"] is not None
    }
    recovered: list[str] = []
    for experiment_id in sorted(pending_ids):
        experiment = _call(store.experiment, experiment_id)
        if experiment["market"] != market or experiment["account"] != account:
            continue
        _recover_projection(
            store,
            artifact_root,
            experiment_id=experiment_id,
            publisher=publisher,
        )
        recovered.append(experiment_id)
    return recovered


def read_active_experiment_ids(
    store: ExperimentStore,
    *,
    market: str,
    account: str,
) -> list[str]:
    market, account = _identity(market, account)
    return sorted(
        str(item["experiment_id"])
        for item in _call(store.active_experiments, market, account)
    )


def read_advance_context(
    store: ExperimentStore,
    artifact_root: str | Path,
    *,
    experiment_id: str,
) -> dict[str, object]:
    """Return only the validated routing facts needed by the scheduled composer."""

    experiment_id = _segment(experiment_id, "experiment_id")
    experiment = _call(store.experiment, experiment_id)
    base: dict[str, object] = {
        "experiment_id": experiment_id,
        "market": experiment["market"],
        "account": experiment["account"],
        "phase": experiment["phase"],
        "validation_progress": experiment["validation_progress"],
        "terminal_mode": experiment["terminal_mode"],
        "behavior_binding_drift": False,
    }
    if experiment["terminal_mode"] is not None:
        return base
    try:
        raw_spec = json.loads(str(experiment["spec_json"]))
        if not isinstance(raw_spec, Mapping):
            raise ValueError("spec is not an object")
        baseline = raw_spec.get("baseline")
        if not isinstance(baseline, Mapping):
            raise ValueError("baseline is missing")
        stored_behavior = _hash(
            baseline.get("behavior_binding_sha256"), "behavior_binding_sha256"
        )
        current_behavior = build_current_behavior_binding(raw_spec)
    except (json.JSONDecodeError, KeyError, TypeError, ValueError, Top1CoreContractError) as exc:
        raise Top1LifecycleError(
            "experiment_conflict", "experiment behavior binding is invalid"
        ) from exc
    if stored_behavior != current_behavior:
        return {**base, "behavior_binding_drift": True}
    try:
        spec = validate_experiment_spec(raw_spec)
    except Top1CoreContractError as exc:
        raise Top1LifecycleError("experiment_conflict", str(exc)) from exc
    if experiment["phase"] != "validation":
        return base
    try:
        commitment = validate_hidden_window_commitment(
            json.loads(str(experiment["proposed_commitment_json"])),
            expected_experiment_id=experiment_id,
            expected_account=str(experiment["account"]),
        )
    except json.JSONDecodeError as exc:
        raise Top1LifecycleError(
            "experiment_conflict", "commitment JSON is invalid"
        ) from exc
    commitment_text = render_json_text(commitment)
    expected_ref = (
        f"strategy_lab/top1/experiments/{experiment_id}/hidden_window_commitments/"
        f"{experiment['proposed_commitment_sha256']}.json"
    )
    bindings_match = (
        canonical_sha256(commitment) == experiment["proposed_commitment_sha256"]
        and artifact_content_sha256(commitment)
        == experiment["proposed_commitment_content_sha256"]
        and hashlib.sha256(commitment_text.encode("utf-8")).hexdigest()
        == experiment["proposed_commitment_file_sha256"]
        and experiment["proposed_commitment_ref"] == expected_ref
        and commitment["challenger_variant_id"] == experiment["research_leader"]
        and commitment["research_spec_sha256"] == experiment["research_spec_sha256"]
        and commitment["behavior_binding_sha256"] == stored_behavior
    )
    if not bindings_match:
        _fail("experiment_conflict", "hidden commitment binding changed")
    research = next(
        (
            item
            for item in _call(store.generations, experiment_id)
            if item["generation_kind"] == "research"
        ),
        None,
    )
    if research is None or commitment["research_terminal_file_sha256"] != research[
        "terminal_file_sha256"
    ]:
        _fail("experiment_conflict", "research terminal binding changed")
    _validate_commitment_calendar(artifact_root, commitment)
    dates = cast(list[str], commitment["trading_dates"])
    if _call(store.commitment_dates, experiment_id) != dates:
        _fail("experiment_conflict", "stored commitment dates changed")
    completed = int(experiment["completed_validation_partitions"])
    open_date = dates[completed] if completed < len(dates) else None
    decisions = _call(store.validation_decisions, experiment_id)
    open_decisions = (
        [item for item in decisions if item["trading_date"] == open_date]
        if open_date is not None
        else []
    )
    return {
        **base,
        "spec": spec,
        "timer_binding": spec["timer_binding"],
        "commitment": commitment,
        "committed_days": commitment["days"],
        "open_trading_date": open_date,
        "consumed_point_ids": [
            item["recommendation_point_id"] for item in open_decisions
        ],
        "last_consumed_available_point_id": (
            open_decisions[-1]["recommendation_point_id"]
            if open_decisions and open_decisions[-1]["source_status"] == "available"
            else None
        ),
        "has_outcome_jobs": bool(_call(store.outcome_jobs, experiment_id)),
    }


def read_public_status(
    store: ExperimentStore,
    *,
    experiment_id: str,
    expected_market: str | None = None,
    expected_account: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, object]:
    experiment_id = _segment(experiment_id, "experiment_id")
    experiment = _call(store.experiment, experiment_id)
    if (
        expected_market is not None
        and experiment["market"] != expected_market
        or expected_account is not None
        and experiment["account"] != expected_account
    ):
        _fail("experiment_conflict", "experiment identity changed")
    feature = effective_feature_status(
        store,
        market=str(experiment["market"]),
        account=str(experiment["account"]),
        environ=environ,
    )
    generations = _call(store.generations, experiment_id)
    decisions = _call(store.validation_decisions, experiment_id)
    jobs = _call(store.outcome_jobs, experiment_id)
    return {
        "schema_version": PUBLIC_STATUS_SCHEMA,
        "feature": feature,
        "experiment": {
            "experiment_id": experiment_id,
            "topic_id": experiment["topic_id"],
            "market": experiment["market"],
            "account": experiment["account"],
            "strategy_family": experiment["strategy_family"],
            "phase": experiment["phase"],
            "research_progress": experiment["research_progress"],
            "validation_progress": experiment["validation_progress"],
            "completed_validation_partitions": experiment[
                "completed_validation_partitions"
            ],
            "blocked_reason": experiment["blocked_reason"],
            "research_authorization_status": experiment[
                "research_authorization_status"
            ],
            "research_authorized_hash": experiment["research_authorized_hash"],
            "validation_authorization_status": experiment[
                "validation_authorization_status"
            ],
            "validation_authorized_hash": experiment[
                "validation_authorized_hash"
            ],
            "research_spec_sha256": experiment["research_spec_sha256"],
            "validation_spec_sha256": experiment["validation_spec_sha256"],
            "hidden_window_commitment_sha256": experiment[
                "proposed_commitment_sha256"
            ],
            "terminal_mode": experiment["terminal_mode"],
            "terminal_reason": experiment["terminal_reason"],
            "disabled_scope": experiment["disabled_scope"],
            "final_outcome_status": (
                experiment["final_outcome_status"]
                if experiment["phase"] == "concluded"
                else None
            ),
            "projection_state": (
                "published"
                if experiment["phase"] == "concluded"
                else "pending"
                if experiment["terminal_mode"] is not None
                else "not_requested"
            ),
        },
        "validation": {
            "consumed_point_count": len(decisions),
            "outcome_job_count": len(jobs),
            "pending_outcome_count": sum(
                item["status"] in {"pending_terms", "pending_outcome"}
                for item in jobs
            ),
        },
        "generations": [
            {
                "generation_kind": item["generation_kind"],
                "state": item["state"],
                "revision": item["revision"],
                "terminal_mode": item["terminal_mode"],
                "terminal_ref": item["terminal_ref"],
                "terminal_content_sha256": item["terminal_content_sha256"],
                "terminal_file_sha256": item["terminal_file_sha256"],
                "projection_state": (
                    "published"
                    if item["terminal_published_event_id"] is not None
                    else "pending"
                    if item["terminal_request_event_id"] is not None
                    else "not_requested"
                ),
            }
            for item in generations
        ],
    }


def read_public_receipt(
    store: ExperimentStore, *, experiment_id: str
) -> dict[str, object] | None:
    experiment_id = _segment(experiment_id, "experiment_id")
    text = _call(store.receipt_text, experiment_id)
    if text is None:
        return None
    payload = json.loads(text)
    if not isinstance(payload, dict):
        _fail("projection_conflict", "receipt payload is not an object")
    return payload


__all__ = [
    "HIDDEN_WINDOW_COMMITMENT_SCHEMA",
    "PUBLIC_STATUS_SCHEMA",
    "Top1LifecycleError",
    "authorize_research",
    "authorize_validation",
    "build_hidden_window_commitment",
    "effective_feature_status",
    "lock_challenger",
    "prepare_experiment",
    "read_active_experiment_ids",
    "read_advance_context",
    "read_public_receipt",
    "read_public_status",
    "recover_account_terminal_projections",
    "reconcile_disabled_experiments",
    "record_generation_revision",
    "seal_generation",
    "set_account_opt_in",
    "start_research",
    "start_validation",
    "terminate_experiment",
    "validate_hidden_window_commitment",
]
