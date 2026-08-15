from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping, NoReturn, Sequence, cast

from domain.domain.decision_state_fingerprint import canonical_sha256
from src.application.recommendation_point import strategy_lab_top1_available
from src.application.shadow_replay.common import artifact_content_sha256, render_json_text
from src.application.strategy_lab.top1.contracts import (
    Top1CoreContractError,
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


HIDDEN_WINDOW_COMMITMENT_SCHEMA = "sell_put_top1_hidden_window_commitment.v1"
PUBLIC_STATUS_SCHEMA = "sell_put_top1_experiment_status.v1"

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
    trading_dates: Sequence[object],
    market_calendar_version: str,
    challenger_variant_id: str,
    research_spec_sha256: str,
    research_terminal_file_sha256: str,
    behavior_binding_sha256: str,
) -> dict[str, object]:
    experiment_id = _segment(experiment_id, "experiment_id")
    _, account = _identity("HK", account)
    dates = _trading_dates(trading_dates)
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
            market_calendar_version, "market_calendar_version"
        ),
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


def lock_challenger(
    store: ExperimentStore,
    validation_spec: object,
    *,
    challenger_variant_id: str,
    trading_dates: Sequence[object],
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
    commitment = build_hidden_window_commitment(
        experiment_id=experiment_id,
        account=account,
        trading_dates=trading_dates,
        market_calendar_version=str(economics["market_calendar_version"]),
        challenger_variant_id=challenger_variant_id,
        research_spec_sha256=research_hash,
        research_terminal_file_sha256=str(
            research_generation["terminal_file_sha256"]
        ),
        behavior_binding_sha256=str(baseline["behavior_binding_sha256"]),
    )
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
    commitment = json.loads(str(experiment["proposed_commitment_json"]))
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
        commitment_dates=_trading_dates(commitment["trading_dates"]),
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
    pending_ids = {
        str(event["experiment_id"])
        for event in _call(store.pending_projections)
        if event["experiment_id"] is not None
    }
    for experiment_id in sorted(pending_ids):
        experiment = _call(store.experiment, experiment_id)
        if experiment["market"] == market and experiment["account"] == account:
            _recover_projection(store, artifact_root, experiment_id=experiment_id)
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


def read_public_status(
    store: ExperimentStore,
    *,
    experiment_id: str,
    environ: Mapping[str, str] | None = None,
) -> dict[str, object]:
    experiment_id = _segment(experiment_id, "experiment_id")
    experiment = _call(store.experiment, experiment_id)
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
    "read_public_receipt",
    "read_public_status",
    "reconcile_disabled_experiments",
    "record_generation_revision",
    "seal_generation",
    "set_account_opt_in",
    "start_research",
    "start_validation",
    "terminate_experiment",
]
