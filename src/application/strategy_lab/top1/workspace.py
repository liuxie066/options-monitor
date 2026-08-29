from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any, NoReturn
from zoneinfo import ZoneInfo

from domain.domain.decision_state_fingerprint import canonical_sha256
from domain.domain.option_lifecycle import expiration_observation_start_ms
from domain.domain.performance.models import FXRateFact, select_fx_rate
from src.application.account_config import normalize_account_label
from src.application.recommendation_point import strategy_lab_top1_available
from src.application.research.formal_corpus import CORPUS_HEALTH_SCHEMA
from src.application.shadow_replay.common import render_json_text
from src.application.strategy_lab.top1.contracts import (
    PREVIEW_SCHEMA_VERSION,
    RECOMMENDATION_POINT_SELECTOR,
    RECIPE_ID,
    RECIPE_VERSION,
    RESEARCH_REQUIRED_DAYS,
    Top1CoreContractError,
    build_research_spec_sha256,
    build_sell_put_top1_research_preview_sha256,
    build_sell_put_top1_research_spec,
    build_sell_put_top1_validation_spec,
    build_validation_spec_sha256,
    validate_confirmed_start_command,
)
from src.application.strategy_lab.top1.corpus import (
    RESEARCH_WINDOW_FACTS_SCHEMA,
    CorpusError,
    preview_research_dataset,
    read_market_calendar_binding,
)
from src.application.strategy_lab.top1.economics import build_fx_rate_binding
from src.application.strategy_lab.top1.lifecycle import (
    Top1LifecycleError,
    authorize_research,
    authorize_validation,
    build_hidden_window_commitment,
    lock_challenger,
    prepare_experiment,
    read_published_research_leader,
    start_validation,
)
from src.application.strategy_lab.top1.research import (
    ResearchEvaluationError,
    required_research_close_keys,
)
from src.application.strategy_lab.top1.research_artifacts import (
    ResearchArtifactError,
    load_materialized_research_input,
)
from src.application.strategy_lab.top1.research_runner import run_research
from src.application.strategy_lab.top1.readiness import CAPABILITY_FACTS
from src.application.strategy_lab.top1.terminal_projection import publish_exact_text
from src.infrastructure.strategy_lab.experiment_store import (
    ExperimentStore,
    ExperimentStoreError,
)


_TOPIC_ID = "sell-put-top1-option-market-concentration"
_PRODUCTION_IMPACT = {
    "changes_configuration": False,
    "trades": False,
    "sends_notifications": False,
    "adopts_result": False,
}
_INVALIDATED_BY = [
    "research_source_binding_changed",
    "terminal_fx_binding_changed",
    "fee_contract_changed",
    "service_disabled",
]


class Top1WorkspaceError(RuntimeError):
    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


def _fail(reason_code: str, message: str) -> NoReturn:
    raise Top1WorkspaceError(reason_code, message)


def _derived_key(idempotency_key: str, step: str) -> str:
    return hashlib.sha256(f"{idempotency_key}\0{step}".encode()).hexdigest()


def _research_window_facts(
    *,
    market: str,
    account: str,
    cutoff_at_utc: str,
    latest_mature_trading_date: str,
    market_calendar: Mapping[str, Any],
) -> dict[str, object]:
    cutoff = datetime.fromisoformat(cutoff_at_utc.replace("Z", "+00:00"))
    if not cutoff_at_utc.endswith("Z") or cutoff.utcoffset() is None:
        raise ValueError("cutoff_at_utc must be canonical UTC")
    cutoff_day = cutoff.astimezone(ZoneInfo("Asia/Hong_Kong")).date().isoformat()
    dates = [
        str(value)
        for value in market_calendar["trading_dates"]
        if str(value) <= cutoff_day
    ]
    if not dates:
        raise ValueError("market calendar does not cover the research cutoff")
    facts: dict[str, object] = {
        "schema_version": RESEARCH_WINDOW_FACTS_SCHEMA,
        "market": market,
        "account": account,
        "cutoff_at_utc": cutoff_at_utc,
        "cutoff_trading_date": dates[-1],
        "market_calendar_version": market_calendar["market_calendar_version"],
        "market_calendar_ref": market_calendar["snapshot_ref"],
        "market_calendar_sha256": market_calendar["snapshot_content_sha256"],
        "trading_calendar_dates": dates,
        "trading_calendar_dates_sha256": canonical_sha256(dates),
        "latest_mature_trading_date": latest_mature_trading_date,
        "maturity_evidence_ref": market_calendar["snapshot_ref"],
        "maturity_evidence_sha256": market_calendar["snapshot_content_sha256"],
        "recommendation_point_selector": RECOMMENDATION_POINT_SELECTOR,
    }
    facts["content_sha256"] = canonical_sha256(facts)
    return facts


def _preview(
    *,
    stage: str = "research",
    status: str,
    reason_codes: list[str],
    experiment_id: str | None = None,
    experiment_spec: dict[str, object] | None = None,
    stage_spec_sha256: str | None = None,
    preview_sha256: str | None = None,
    source_bindings: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "schema_version": PREVIEW_SCHEMA_VERSION,
        "stage": stage,
        "status": status,
        "reason_codes": reason_codes,
        "experiment_id": experiment_id,
        "experiment_spec": experiment_spec,
        "stage_spec_sha256": stage_spec_sha256,
        "preview_sha256": preview_sha256,
        "source_bindings": source_bindings,
        "production_impact": dict(_PRODUCTION_IMPACT),
        "invalidated_by": list(_INVALIDATED_BY),
    }


def _is_canonical_account(value: object) -> bool:
    try:
        return value == normalize_account_label(value)
    except ValueError:
        return False


def _research_preview(
    store: ExperimentStore,
    artifact_root: str | Path,
    *,
    market: str,
    account: str,
    cutoff_at_utc: str,
    latest_mature_trading_date: str,
    market_calendar: Mapping[str, Any],
    fee_contract: Mapping[str, object],
    capability_facts: Mapping[str, object],
    corpus_status: Mapping[str, object] | None,
    evidence_bundle: object,
    environ: Mapping[str, str] | None,
) -> tuple[dict[str, object], dict[str, Any] | None, dict[str, object]]:
    if market != "HK" or not _is_canonical_account(account):
        return _preview(status="unsupported", reason_codes=["unsupported_recipe"]), None, {}
    if not strategy_lab_top1_available(environ):
        return _preview(
            status="disabled", reason_codes=["strategy_lab_service_disabled"]
        ), None, {}
    if set(capability_facts) != set(CAPABILITY_FACTS) or not all(
        capability_facts[name] is True for name in CAPABILITY_FACTS
    ):
        return _preview(
            status="blocked", reason_codes=["top1_capability_evidence_missing"]
        ), None, {}
    if store.schema_state().get("status") != "ready":
        return _preview(
            status="blocked", reason_codes=["strategy_lab_store_not_ready"]
        ), None, {}
    if corpus_status is None:
        return _preview(
            status="blocked", reason_codes=["strategy_lab_top1_corpus_unavailable"]
        ), None, {}
    if (
        corpus_status.get("schema_version") != CORPUS_HEALTH_SCHEMA
        or corpus_status.get("status") not in {"healthy", "unhealthy"}
    ):
        return _preview(
            status="blocked", reason_codes=["strategy_lab_top1_corpus_invalid"]
        ), None, {}
    complete_days = corpus_status.get("continuous_complete_trading_days")
    if (
        isinstance(complete_days, bool)
        or not isinstance(complete_days, int)
        or complete_days < RESEARCH_REQUIRED_DAYS
    ):
        return _preview(
            status="blocked", reason_codes=["research_corpus_warming"]
        ), None, {}
    storage = corpus_status.get("storage")
    capacity = storage.get("capacity") if isinstance(storage, Mapping) else None
    capacity_status = capacity.get("status") if isinstance(capacity, Mapping) else None
    if capacity_status in {"warning", "critical"}:
        return _preview(
            status="blocked", reason_codes=["research_storage_capacity_risk"]
        ), None, {}
    if capacity_status not in {"ok", "insufficient_history"}:
        return _preview(
            status="blocked", reason_codes=["research_storage_capacity_unavailable"]
        ), None, {}
    try:
        freeze, dataset = preview_research_dataset(
            artifact_root,
            window_facts=_research_window_facts(
                market=market,
                account=account,
                cutoff_at_utc=cutoff_at_utc,
                latest_mature_trading_date=latest_mature_trading_date,
                market_calendar=market_calendar,
            ),
            environ=environ,
        )
        if freeze["status"] != "ready" or dataset is None:
            return _preview(
                status="blocked",
                reason_codes=[
                    str(freeze["reason_code"] or "research_dataset_coverage_missing")
                ],
            ), None, {}
        dataset_ref = str(freeze["dataset_ref"])
        dataset_file_sha256 = str(freeze["dataset_sha256"])
        experiment_id = "sell-put-top1-" + canonical_sha256(
            {
                "topic_id": _TOPIC_ID,
                "market": market,
                "account": account,
                "recipe_id": RECIPE_ID,
                "recipe_version": RECIPE_VERSION,
                "research_dataset_content_sha256": dataset["content_sha256"],
            }
        )[:32]
        spec = build_sell_put_top1_research_spec(
            topic_id=_TOPIC_ID,
            experiment_id=experiment_id,
            account=account,
            market_calendar_version=str(dataset["market_calendar_version"]),
            ranking_projection_schema_version=str(
                dataset["ranking_projection_schema_version"]
            ),
            research_source={
                "mode": "sealed_historical_dataset",
                "dataset_ref": dataset_ref,
                "dataset_sha256": dataset_file_sha256,
                "research_cutoff_at": cutoff_at_utc,
                "start_trading_date": dataset["selected_trading_dates"][0],
                "end_trading_date": dataset["selected_trading_dates"][-1],
            },
        )
        research_input = load_materialized_research_input(
            artifact_root,
            spec,
            research_source=dataset,
        )
        requirements = required_research_close_keys(research_input, fee_contract)
    except (
        CorpusError,
        KeyError,
        ResearchArtifactError,
        ResearchEvaluationError,
        Top1CoreContractError,
        TypeError,
        ValueError,
    ) as exc:
        return _preview(
            status="blocked",
            reason_codes=[str(getattr(exc, "reason_code", "research_preview_invalid"))],
        ), None, {}

    if getattr(evidence_bundle, "schema_state", None) != "initialized_v1":
        return _preview(
            status="blocked", reason_codes=["terminal_fx_evidence_missing"]
        ), None, {}
    terminal_bindings: dict[str, object] = {}
    fx_rates = getattr(evidence_bundle, "fx_rates", ())
    for _stock_owner, expiration in requirements:
        terminal_at_ms = expiration_observation_start_ms(expiration, market)
        if terminal_at_ms is None:
            return _preview(
                status="blocked", reason_codes=["terminal_fx_evidence_missing"]
            ), None, {}
        selected = select_fx_rate(
            fx_rates,
            base_currency="HKD",
            at_ms=terminal_at_ms,
        )
        if selected.status != "selected" or not isinstance(selected.fact, FXRateFact):
            return _preview(
                status="blocked", reason_codes=["terminal_fx_evidence_missing"]
            ), None, {}
        terminal_bindings[expiration] = build_fx_rate_binding(
            selected.fact,
            selected_at_ms=terminal_at_ms,
        )

    points = sorted(
        (dict(point) for day in dataset["days"] for point in day["points"]),
        key=lambda item: str(item["recommendation_point_id"]),
    )
    source_bindings: dict[str, object] = {
        "market_calendar": {
            "ref": market_calendar["snapshot_ref"],
            "content_sha256": market_calendar["snapshot_content_sha256"],
            "file_sha256": market_calendar["snapshot_file_sha256"],
        },
        "research_dataset": {
            "ref": dataset_ref,
            "content_sha256": dataset["content_sha256"],
            "file_sha256": dataset_file_sha256,
        },
        "recommendation_points": points,
        "terminal_fx_bindings": [
            {"expiration": expiration, "binding": terminal_bindings[expiration]}
            for expiration in sorted(terminal_bindings)
        ],
        "fee_contract": dict(fee_contract),
    }
    stage_hash = build_research_spec_sha256(spec)
    preview_hash = build_sell_put_top1_research_preview_sha256(
        experiment_id=experiment_id,
        stage_spec_sha256=stage_hash,
        source_bindings=source_bindings,
    )
    return (
        _preview(
            status="available",
            reason_codes=[],
            experiment_id=experiment_id,
            experiment_spec=spec,
            stage_spec_sha256=stage_hash,
            preview_sha256=preview_hash,
            source_bindings=source_bindings,
        ),
        dataset,
        terminal_bindings,
    )


def preview_sell_put_top1_research(
    store: ExperimentStore,
    artifact_root: str | Path,
    **kwargs: Any,
) -> dict[str, object]:
    preview, _dataset, _terminal_bindings = _research_preview(
        store, artifact_root, **kwargs
    )
    return preview


def start_confirmed_research(
    store: ExperimentStore,
    artifact_root: str | Path,
    *,
    confirmed_start: object,
    gateway: object,
    config: Mapping[str, Any] | None,
    **preview_kwargs: Any,
) -> dict[str, object]:
    try:
        command = validate_confirmed_start_command(confirmed_start)
    except Top1CoreContractError as exc:
        raise Top1WorkspaceError(exc.reason_code, str(exc)) from exc
    if command["stage"] != "research":
        _fail("confirmed_start_invalid", "research start requires the research stage")
    preview, dataset, terminal_bindings = _research_preview(
        store, artifact_root, **preview_kwargs
    )
    if preview["status"] != "available" or dataset is None:
        reason_codes = preview["reason_codes"]
        reason = reason_codes[0] if isinstance(reason_codes, list) and reason_codes else "research_preview_unavailable"
        _fail(str(reason), "research preview is not available")
    if (
        command["market"] != preview_kwargs["market"]
        or command["account"] != preview_kwargs["account"]
        or command["experiment_id"] != preview["experiment_id"]
        or command["confirmed_preview_sha256"] != preview["preview_sha256"]
    ):
        _fail("preview_hash_changed", "confirmed research preview no longer matches")

    actor = str(command["actor"])
    occurred_at = str(command["confirmed_at_utc"])
    key = str(command["idempotency_key"])
    spec = preview["experiment_spec"]
    assert isinstance(spec, dict)
    stage_hash = str(preview["stage_spec_sha256"])
    source_bindings = preview["source_bindings"]
    assert isinstance(source_bindings, dict)
    store.migrate(migrated_at_utc=occurred_at)
    try:
        prepare_experiment(
            store,
            spec,
            provenance={
                "confirmed_preview_sha256": preview["preview_sha256"],
                "source_bindings": source_bindings,
            },
            actor=actor,
            occurred_at_utc=occurred_at,
            idempotency_key=_derived_key(key, "prepare"),
            artifact_root=artifact_root,
            environ=preview_kwargs.get("environ"),
        )
        source = spec["research_source"]
        assert isinstance(source, Mapping)
        publish_exact_text(
            artifact_root,
            str(source["dataset_ref"]),
            render_json_text(dataset).encode(),
        )
        authorize_research(
            store,
            experiment_id=str(preview["experiment_id"]),
            research_spec_sha256=stage_hash,
            actor=actor,
            occurred_at_utc=occurred_at,
            idempotency_key=_derived_key(key, "authorize"),
            artifact_root=artifact_root,
            environ=preview_kwargs.get("environ"),
        )
        return run_research(
            store,
            artifact_root,
            experiment_id=str(preview["experiment_id"]),
            research_spec_sha256=stage_hash,
            fee_contract=preview_kwargs["fee_contract"],
            gateway=gateway,  # type: ignore[arg-type]
            terminal_fx_bindings=terminal_bindings,
            config=config,
            actor=actor,
            occurred_at_utc=occurred_at,
            idempotency_key=_derived_key(key, "run"),
            environ=preview_kwargs.get("environ"),
        )
    except Top1LifecycleError as exc:
        raise Top1WorkspaceError(exc.reason_code, str(exc)) from exc


def _validation_preview(
    store: ExperimentStore,
    artifact_root: str | Path,
    *,
    market: str,
    account: str,
    experiment_id: str,
    validation_start_trading_date: str,
    schedule: Mapping[str, Any],
    timer_binding: Mapping[str, object],
    as_of_utc: str,
    environ: Mapping[str, str] | None,
) -> tuple[dict[str, object], dict[str, object] | None, str | None]:
    if market != "HK" or not _is_canonical_account(account):
        return (
            _preview(
                stage="validation",
                status="blocked",
                reason_codes=["unsupported_recipe"],
                experiment_id=experiment_id,
            ),
            None,
            None,
        )
    if not strategy_lab_top1_available(environ):
        return (
            _preview(
                stage="validation",
                status="disabled",
                reason_codes=["strategy_lab_service_disabled"],
                experiment_id=experiment_id,
            ),
            None,
            None,
        )
    if store.schema_state().get("status") != "ready":
        return (
            _preview(
                stage="validation",
                status="blocked",
                reason_codes=["strategy_lab_store_not_ready"],
                experiment_id=experiment_id,
            ),
            None,
            None,
        )
    try:
        experiment = store.experiment(experiment_id)
        if experiment["market"] != market or experiment["account"] != account:
            raise Top1WorkspaceError(
                "experiment_conflict", "experiment identity changed"
            )
        already_started = (
            experiment["phase"] == "validation"
            and experiment["validation_progress"] == "collecting_decisions"
        )
        stored_spec = json.loads(str(experiment["spec_json"]))
        spec = build_sell_put_top1_validation_spec(
            stored_spec,
            timer_binding=timer_binding,
        )
        research = read_published_research_leader(
            store,
            spec,
            artifact_root=artifact_root,
            environ=environ,
        )
        research_hash = str(research["research_spec_sha256"])
        research_generation = research["research_generation"]
        assert isinstance(research_generation, Mapping)
        challenger = str(research["challenger_variant_id"])
        calendar = read_market_calendar_binding(artifact_root, market="HK")
        baseline = spec["baseline"]
        assert isinstance(baseline, Mapping)
        commitment = build_hidden_window_commitment(
            experiment_id=experiment_id,
            account=account,
            validation_start_trading_date=validation_start_trading_date,
            market_calendar_binding=calendar,
            schedule=schedule,
            challenger_variant_id=challenger,
            research_spec_sha256=research_hash,
            research_terminal_file_sha256=str(
                research_generation["terminal_file_sha256"]
            ),
            behavior_binding_sha256=str(baseline["behavior_binding_sha256"]),
        )
        days = commitment["days"]
        assert isinstance(days, list) and days
        first_targets = days[0]["scheduled_scan_targets_market"]
        assert isinstance(first_targets, list) and first_targets
        first_target = datetime.fromisoformat(
            str(first_targets[0]).replace("Z", "+00:00")
        )
        previewed_at = datetime.fromisoformat(as_of_utc.replace("Z", "+00:00"))
        if not already_started and first_target <= previewed_at:
            raise Top1WorkspaceError(
                "validation_start_not_future",
                "validation first target must be future",
            )
        commitment_sha256 = canonical_sha256(commitment)
        validation_hash = build_validation_spec_sha256(
            spec,
            research_terminal_sha256=str(
                research_generation["terminal_file_sha256"]
            ),
            challenger_variant_id=challenger,
            hidden_window_commitment_sha256=commitment_sha256,
        )
    except (
        CorpusError,
        ExperimentStoreError,
        KeyError,
        ResearchEvaluationError,
        Top1CoreContractError,
        Top1LifecycleError,
        Top1WorkspaceError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        return (
            _preview(
                stage="validation",
                status="blocked",
                reason_codes=[
                    str(getattr(exc, "reason_code", "validation_preview_invalid"))
                ],
                experiment_id=experiment_id,
            ),
            None,
            None,
        )
    bindings: dict[str, object] = {
        "research_terminal": {
            "ref": research_generation["terminal_ref"],
            "file_sha256": research_generation["terminal_file_sha256"],
        },
        "challenger_variant_id": challenger,
        "hidden_window_commitment": commitment,
        "hidden_window_commitment_sha256": commitment_sha256,
    }
    return (
        _preview(
            stage="validation",
            status="available",
            reason_codes=[],
            experiment_id=experiment_id,
            experiment_spec=spec,
            stage_spec_sha256=validation_hash,
            preview_sha256=validation_hash,
            source_bindings=bindings,
        ),
        spec,
        challenger,
    )


def preview_sell_put_top1_validation(
    store: ExperimentStore,
    artifact_root: str | Path,
    **kwargs: Any,
) -> dict[str, object]:
    preview, _spec, _challenger = _validation_preview(
        store,
        artifact_root,
        **kwargs,
    )
    return preview


def start_confirmed_validation(
    store: ExperimentStore,
    artifact_root: str | Path,
    *,
    confirmed_start: object,
    **preview_kwargs: Any,
) -> dict[str, object]:
    try:
        command = validate_confirmed_start_command(confirmed_start)
    except Top1CoreContractError as exc:
        raise Top1WorkspaceError(exc.reason_code, str(exc)) from exc
    if command["stage"] != "validation":
        _fail("confirmed_start_invalid", "validation start requires validation stage")
    preview, spec, challenger = _validation_preview(
        store,
        artifact_root,
        **preview_kwargs,
    )
    if preview["status"] != "available" or spec is None or challenger is None:
        reasons = preview["reason_codes"]
        reason = reasons[0] if isinstance(reasons, list) and reasons else "validation_preview_unavailable"
        _fail(str(reason), "validation preview is not available")
    if (
        command["market"] != preview_kwargs["market"]
        or command["account"] != preview_kwargs["account"]
        or command["experiment_id"] != preview["experiment_id"]
        or command["confirmed_preview_sha256"] != preview["preview_sha256"]
    ):
        _fail("preview_hash_changed", "confirmed validation preview no longer matches")
    actor = str(command["actor"])
    occurred_at = str(command["confirmed_at_utc"])
    key = str(command["idempotency_key"])
    validation_hash = str(preview["stage_spec_sha256"])
    try:
        lock_challenger(
            store,
            spec,
            challenger_variant_id=challenger,
            expected_validation_spec_sha256=validation_hash,
            validation_start_trading_date=str(
                preview_kwargs["validation_start_trading_date"]
            ),
            schedule=preview_kwargs["schedule"],
            actor=actor,
            occurred_at_utc=occurred_at,
            idempotency_key=_derived_key(key, "lock"),
            artifact_root=artifact_root,
            environ=preview_kwargs.get("environ"),
        )
        authorize_validation(
            store,
            experiment_id=str(preview["experiment_id"]),
            validation_spec_sha256=validation_hash,
            actor=actor,
            occurred_at_utc=occurred_at,
            idempotency_key=_derived_key(key, "authorize"),
            artifact_root=artifact_root,
            environ=preview_kwargs.get("environ"),
        )
        started = start_validation(
            store,
            experiment_id=str(preview["experiment_id"]),
            validation_spec_sha256=validation_hash,
            actor=actor,
            occurred_at_utc=occurred_at,
            idempotency_key=_derived_key(key, "start"),
            artifact_root=artifact_root,
            environ=preview_kwargs.get("environ"),
        )
    except Top1LifecycleError as exc:
        raise Top1WorkspaceError(exc.reason_code, str(exc)) from exc
    return {
        "status": "validation_started",
        "experiment_id": preview["experiment_id"],
        "validation_spec_sha256": validation_hash,
        "validation_progress": started["validation_progress"],
    }


__all__ = [
    "Top1WorkspaceError",
    "preview_sell_put_top1_research",
    "preview_sell_put_top1_validation",
    "start_confirmed_research",
    "start_confirmed_validation",
]
