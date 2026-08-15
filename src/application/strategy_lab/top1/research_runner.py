from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, NoReturn, cast

from src.application.opend_call_coordinator import rate_limited_opend_call
from src.application.opend_fetch_config import resolve_opend_fetch_limits
from src.application.shadow_replay.common import render_json_text
from src.application.strategy_lab.top1.contracts import (
    Top1CoreContractError,
    build_research_spec_sha256,
    validate_experiment_spec,
)
from src.application.strategy_lab.top1.lifecycle import (
    Top1LifecycleError,
    effective_feature_status,
    record_generation_revision,
    seal_generation,
    start_research,
)
from src.application.strategy_lab.top1.research import (
    RESEARCH_CLOSE_RECEIPT_SCHEMA,
    RESEARCH_EVALUATION_INPUT_SCHEMA,
    RESEARCH_EVALUATION_SCHEMA,
    ResearchEvaluationError,
    evaluate_research,
    required_research_close_keys,
)
from src.application.strategy_lab.top1.terminal_projection import (
    publish_exact_text,
    recover_terminal_projection,
)
from src.infrastructure.futu_gateway import FutuGateway
from src.infrastructure.private_storage import open_private_text, private_path
from src.infrastructure.strategy_lab.experiment_store import (
    ExperimentStore,
    ExperimentStoreError,
)


_HASH_64 = re.compile(r"[0-9a-f]{64}\Z")
_QUOTA_KEYS = frozenset({"used_quota", "remain_quota", "detail_list"})
_QUOTA_DETAIL_KEYS = frozenset({"code", "request_time"})
_CLOSE_KEYS = frozenset({"code", "expiration", "close"})


class ResearchRunnerError(RuntimeError):
    """Stable fail-closed error from the W5 orchestration boundary."""

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


def _fail(reason_code: str, message: str) -> NoReturn:
    raise ResearchRunnerError(reason_code, message)


def _hash(value: object, label: str) -> str:
    if not isinstance(value, str) or _HASH_64.fullmatch(value) is None:
        _fail("research_runner_input_invalid", f"{label} must be a lowercase SHA-256")
    return value


def _safe_ref(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        _fail("research_artifact_invalid", f"{label} must be canonical text")
    parts = value.split("/")
    if value.startswith("/") or "\\" in value or any(
        part in {"", ".", ".."} for part in parts
    ):
        _fail("research_artifact_invalid", f"{label} must be a safe relative ref")
    return value


def _file_sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _derived_key(idempotency_key: str, step: str) -> str:
    return hashlib.sha256(f"{idempotency_key}\0{step}".encode("utf-8")).hexdigest()


def _read_canonical_json(
    artifact_root: str | Path,
    *,
    ref: object,
    expected_file_sha256: object,
    label: str,
) -> dict[str, Any]:
    relative_ref = _safe_ref(ref, f"{label}.ref")
    expected_hash = _hash(expected_file_sha256, f"{label}.file_sha256")
    path = private_path(artifact_root).joinpath(*relative_ref.split("/"))
    try:
        with open_private_text(path) as handle:
            text = handle.read()
        content = text.encode("utf-8")
        payload = json.loads(text)
        canonical_text = render_json_text(payload) if isinstance(payload, dict) else None
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise ResearchRunnerError(
            "research_artifact_invalid", f"{label} artifact cannot be read"
        ) from exc
    if _file_sha256(content) != expected_hash:
        _fail("research_artifact_invalid", f"{label} file hash changed")
    if canonical_text != text:
        _fail("research_artifact_invalid", f"{label} bytes are not canonical JSON")
    return cast(dict[str, Any], payload)


def _load_spec(
    store: ExperimentStore,
    *,
    experiment_id: str,
    research_spec_sha256: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    expected_hash = _hash(research_spec_sha256, "research_spec_sha256")
    try:
        experiment = store.experiment(experiment_id)
        raw_spec = json.loads(str(experiment["spec_json"]))
        spec = validate_experiment_spec(raw_spec)
    except ExperimentStoreError as exc:
        raise ResearchRunnerError(exc.reason_code, str(exc)) from exc
    except (json.JSONDecodeError, Top1CoreContractError) as exc:
        raise ResearchRunnerError(
            "experiment_spec_invalid", "stored experiment spec is invalid"
        ) from exc
    if (
        spec["experiment_id"] != experiment_id
        or experiment["research_spec_sha256"] != expected_hash
        or build_research_spec_sha256(spec) != expected_hash
    ):
        _fail("experiment_spec_conflict", "research spec binding changed")
    return experiment, spec


def _research_generation(
    store: ExperimentStore, experiment_id: str
) -> dict[str, Any] | None:
    try:
        return next(
            (
                row
                for row in store.generations(experiment_id)
                if row["generation_kind"] == "research"
            ),
            None,
        )
    except ExperimentStoreError as exc:
        raise ResearchRunnerError(exc.reason_code, str(exc)) from exc


def _require_effective_feature(
    store: ExperimentStore,
    *,
    experiment: Mapping[str, Any],
    environ: Mapping[str, str] | None,
) -> None:
    try:
        effective = effective_feature_status(
            store,
            market=str(experiment["market"]),
            account=str(experiment["account"]),
            environ=environ,
        )["effective"]
    except Top1LifecycleError as exc:
        raise ResearchRunnerError(exc.reason_code, str(exc)) from exc
    if not effective:
        _fail("feature_disabled", "Strategy Lab Top1 is disabled")


def _load_dataset(
    artifact_root: str | Path, spec: Mapping[str, Any]
) -> dict[str, Any]:
    source = cast(Mapping[str, object], spec["research_source"])
    return _read_canonical_json(
        artifact_root,
        ref=source["dataset_ref"],
        expected_file_sha256=source["dataset_sha256"],
        label="sealed_dataset",
    )


def _materialize_input(
    artifact_root: str | Path,
    *,
    spec: dict[str, Any],
    sealed_dataset: dict[str, Any],
) -> dict[str, object]:
    raw_days = sealed_dataset.get("days")
    if not isinstance(raw_days, list):
        _fail("research_artifact_invalid", "sealed dataset days must be a list")
    projections: list[dict[str, object]] = []
    for raw_day in raw_days:
        if not isinstance(raw_day, Mapping) or not isinstance(
            raw_day.get("points"), list
        ):
            _fail("research_artifact_invalid", "sealed dataset point index is invalid")
        for raw_point in cast(list[object], raw_day["points"]):
            if not isinstance(raw_point, Mapping):
                _fail("research_artifact_invalid", "sealed dataset point is invalid")
            ref = raw_point.get("projection_ref")
            projections.append(
                {
                    "projection_ref": ref,
                    "projection": _read_canonical_json(
                        artifact_root,
                        ref=ref,
                        expected_file_sha256=raw_point.get(
                            "projection_file_sha256"
                        ),
                        label="ranking_projection",
                    ),
                }
            )
    return {
        "schema_version": RESEARCH_EVALUATION_INPUT_SCHEMA,
        "experiment_spec": spec,
        "dataset_ref": spec["research_source"]["dataset_ref"],
        "sealed_dataset": sealed_dataset,
        "ranking_projections": projections,
    }


def _validated_quota(value: object) -> tuple[int, set[str]]:
    if not isinstance(value, Mapping) or set(value) != _QUOTA_KEYS:
        _fail("research_history_quota_invalid", "history quota receipt is invalid")
    used = value["used_quota"]
    remaining = value["remain_quota"]
    details = value["detail_list"]
    if (
        isinstance(used, bool)
        or not isinstance(used, int)
        or used < 0
        or isinstance(remaining, bool)
        or not isinstance(remaining, int)
        or remaining < 0
        or not isinstance(details, list)
        or len(details) != used
    ):
        _fail("research_history_quota_invalid", "history quota receipt is invalid")
    codes: set[str] = set()
    for detail in cast(list[object], details):
        if not isinstance(detail, Mapping) or set(detail) != _QUOTA_DETAIL_KEYS:
            _fail("research_history_quota_invalid", "history quota detail is invalid")
        code = detail["code"]
        if (
            not isinstance(code, str)
            or not code
            or code != code.strip().upper()
            or code in codes
        ):
            _fail("research_history_quota_invalid", "history quota code is invalid")
        codes.add(code)
    return remaining, codes


def _close_receipts(
    *,
    artifact_root: str | Path,
    config: Mapping[str, Any] | None,
    gateway: FutuGateway,
    market: str,
    account: str,
    requirements: list[tuple[str, str]],
) -> list[dict[str, object]]:
    if not requirements:
        return []
    try:
        raw_quota = gateway.get_history_kl_quota()
    except Exception as exc:
        raise ResearchRunnerError(
            "research_history_quota_unavailable", "history quota cannot be read"
        ) from exc
    remaining, existing_codes = _validated_quota(raw_quota)
    required_codes = {stock_owner for stock_owner, _expiration in requirements}
    if len(required_codes - existing_codes) > remaining:
        _fail("research_history_quota_insufficient", "history quota is insufficient")

    limit = resolve_opend_fetch_limits(dict(config or {})).history_kline
    receipts: list[dict[str, object]] = []
    for stock_owner, expiration in requirements:
        try:
            result = rate_limited_opend_call(
                base_dir=private_path(artifact_root),
                endpoint="history_kline",
                **limit.call_kwargs(),
                call=lambda stock_owner=stock_owner, expiration=expiration: (
                    gateway.get_exact_expiration_close(
                        code=stock_owner,
                        expiration=expiration,
                    )
                ),
            )
        except Exception as exc:
            raise ResearchRunnerError(
                "research_expiry_close_unavailable",
                "exact-expiration close cannot be read",
            ) from exc
        if result is None:
            close = None
            status = "unavailable"
            reason = "expiry_close_missing_after_deadline"
        else:
            if not isinstance(result, Mapping) or set(result) != _CLOSE_KEYS:
                _fail(
                    "research_expiry_close_invalid",
                    "exact-expiration close receipt is invalid",
                )
            close = result["close"]
            if (
                result["code"] != stock_owner
                or result["expiration"] != expiration
                or isinstance(close, bool)
                or not isinstance(close, (int, float))
                or not math.isfinite(float(close))
                or float(close) <= 0
            ):
                _fail(
                    "research_expiry_close_invalid",
                    "exact-expiration close receipt is invalid",
                )
            close = float(close)
            status = "available"
            reason = None
        receipts.append(
            {
                "schema_version": RESEARCH_CLOSE_RECEIPT_SCHEMA,
                "market": market,
                "account": account,
                "stock_owner": stock_owner,
                "expiration": expiration,
                "spot_source": "opend_history_kline",
                "ktype": "K_DAY",
                "autype": "NONE",
                "price_field": "close",
                "status": status,
                "underlier_close": close,
                "reason_detail": reason,
            }
        )
    return receipts


def _evaluate(
    dataset: dict[str, object],
    close_receipts: list[dict[str, object]],
    fee_contract: object,
) -> dict[str, object]:
    try:
        return evaluate_research(dataset, close_receipts, fee_contract)
    except ResearchEvaluationError as exc:
        raise ResearchRunnerError(exc.reason_code, str(exc)) from exc


def _requirements(
    dataset: dict[str, object], fee_contract: object
) -> list[tuple[str, str]]:
    try:
        return required_research_close_keys(dataset, fee_contract)
    except ResearchEvaluationError as exc:
        raise ResearchRunnerError(exc.reason_code, str(exc)) from exc


def _record_revision(
    store: ExperimentStore,
    artifact_root: str | Path,
    *,
    experiment_id: str,
    generation: Mapping[str, Any],
    evaluation: dict[str, object],
    actor: str,
    occurred_at_utc: str,
    idempotency_key: str,
    environ: Mapping[str, str] | None,
) -> None:
    ref = (
        f"strategy_lab/top1/experiments/{experiment_id}/generations/"
        "research.revision.1.json"
    )
    text = render_json_text(evaluation)
    content = text.encode("utf-8")
    try:
        publish_exact_text(artifact_root, ref, content)
        record_generation_revision(
            store,
            experiment_id=experiment_id,
            generation_kind="research",
            revision=1,
            revision_ref=ref,
            revision_file_sha256=_file_sha256(content),
            frozen_row_sha256=str(generation["frozen_row_content_sha256"]),
            actor=actor,
            occurred_at_utc=occurred_at_utc,
            idempotency_key=_derived_key(idempotency_key, "revision"),
            artifact_root=artifact_root,
            environ=environ,
        )
    except Top1LifecycleError as exc:
        raise ResearchRunnerError(exc.reason_code, str(exc)) from exc
    except (OSError, ValueError) as exc:
        raise ResearchRunnerError(
            "research_revision_conflict", "research revision cannot be recorded"
        ) from exc


def _read_recorded_evaluation(
    artifact_root: str | Path,
    *,
    generation: Mapping[str, Any],
    experiment_id: str,
    research_spec_sha256: str,
    spec: Mapping[str, Any],
    sealed_dataset: Mapping[str, Any],
) -> dict[str, object]:
    evaluation = _read_canonical_json(
        artifact_root,
        ref=generation["last_revision_ref"],
        expected_file_sha256=generation["last_revision_file_sha256"],
        label="research_revision",
    )
    source = cast(Mapping[str, object], spec["research_source"])
    expected = {
        "schema_version": RESEARCH_EVALUATION_SCHEMA,
        "experiment_id": experiment_id,
        "research_spec_sha256": research_spec_sha256,
        "dataset_ref": source["dataset_ref"],
        "dataset_sha256": source["dataset_sha256"],
        "dataset_content_sha256": sealed_dataset.get("content_sha256"),
    }
    if any(evaluation.get(key) != value for key, value in expected.items()):
        _fail("research_revision_conflict", "research revision binding changed")
    return cast(dict[str, object], evaluation)


def _finish_terminal(
    store: ExperimentStore,
    artifact_root: str | Path,
    *,
    experiment_id: str,
    generation: Mapping[str, Any],
    actor: str,
    occurred_at_utc: str,
    idempotency_key: str,
    environ: Mapping[str, str] | None,
) -> None:
    if generation["terminal_mode"] not in {None, "completed"}:
        _fail("research_generation_terminal", "research generation is not completable")
    try:
        if generation["terminal_request_event_id"] is None:
            seal_generation(
                store,
                experiment_id=experiment_id,
                generation_kind="research",
                actor=actor,
                occurred_at_utc=occurred_at_utc,
                idempotency_key=_derived_key(idempotency_key, "seal"),
                artifact_root=artifact_root,
                environ=environ,
            )
        recovered = recover_terminal_projection(
            store,
            artifact_root,
            experiment_id=experiment_id,
        )
    except Top1LifecycleError as exc:
        raise ResearchRunnerError(exc.reason_code, str(exc)) from exc
    except (ExperimentStoreError, OSError, ValueError) as exc:
        raise ResearchRunnerError(
            "research_terminal_publish_failed", "research terminal cannot be published"
        ) from exc
    if recovered["pending"] != 0:
        _fail("research_terminal_pending", "research terminal remains pending")


def run_research(
    store: ExperimentStore,
    artifact_root: str | Path,
    *,
    experiment_id: str,
    research_spec_sha256: str,
    fee_contract: object,
    gateway: FutuGateway,
    config: Mapping[str, Any] | None,
    actor: str,
    occurred_at_utc: str,
    idempotency_key: str,
    environ: Mapping[str, str] | None = None,
) -> dict[str, object]:
    experiment, spec = _load_spec(
        store,
        experiment_id=experiment_id,
        research_spec_sha256=research_spec_sha256,
    )
    sealed_dataset = _load_dataset(artifact_root, spec)
    generation = _research_generation(store, experiment_id)
    if generation is not None and int(generation["revision"]) == 1:
        evaluation = _read_recorded_evaluation(
            artifact_root,
            generation=generation,
            experiment_id=experiment_id,
            research_spec_sha256=research_spec_sha256,
            spec=spec,
            sealed_dataset=sealed_dataset,
        )
        _require_effective_feature(store, experiment=experiment, environ=environ)
        _finish_terminal(
            store,
            artifact_root,
            experiment_id=experiment_id,
            generation=generation,
            actor=actor,
            occurred_at_utc=occurred_at_utc,
            idempotency_key=idempotency_key,
            environ=environ,
        )
        return evaluation
    if generation is not None and (
        int(generation["revision"]) != 0
        or generation["terminal_request_event_id"] is not None
    ):
        _fail("research_generation_conflict", "research generation state is unsupported")

    dataset = _materialize_input(
        artifact_root,
        spec=spec,
        sealed_dataset=sealed_dataset,
    )
    requirements = _requirements(dataset, fee_contract)
    if generation is None:
        try:
            start_research(
                store,
                experiment_id=experiment_id,
                research_spec_sha256=research_spec_sha256,
                actor=actor,
                occurred_at_utc=occurred_at_utc,
                idempotency_key=_derived_key(idempotency_key, "start"),
                artifact_root=artifact_root,
                environ=environ,
            )
        except Top1LifecycleError as exc:
            raise ResearchRunnerError(exc.reason_code, str(exc)) from exc
        generation = _research_generation(store, experiment_id)
        if generation is None:
            _fail("research_generation_conflict", "research generation was not created")
    else:
        _require_effective_feature(store, experiment=experiment, environ=environ)

    receipts = _close_receipts(
        artifact_root=artifact_root,
        config=config,
        gateway=gateway,
        market=str(spec["market"]),
        account=str(spec["account"]),
        requirements=requirements,
    )
    evaluation = _evaluate(dataset, receipts, fee_contract)
    _record_revision(
        store,
        artifact_root,
        experiment_id=experiment_id,
        generation=generation,
        evaluation=evaluation,
        actor=actor,
        occurred_at_utc=occurred_at_utc,
        idempotency_key=idempotency_key,
        environ=environ,
    )
    generation = _research_generation(store, experiment_id)
    if generation is None or int(generation["revision"]) != 1:
        _fail("research_generation_conflict", "research revision was not recorded")
    _finish_terminal(
        store,
        artifact_root,
        experiment_id=experiment_id,
        generation=generation,
        actor=actor,
        occurred_at_utc=occurred_at_utc,
        idempotency_key=idempotency_key,
        environ=environ,
    )
    return evaluation


__all__ = ["ResearchRunnerError", "run_research"]
