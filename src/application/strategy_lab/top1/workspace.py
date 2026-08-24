from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import Path
from typing import Any, NoReturn

from domain.domain.decision_state_fingerprint import canonical_sha256
from domain.domain.option_lifecycle import expiration_observation_start_ms
from domain.domain.performance.models import FXRateFact, select_fx_rate
from src.application.recommendation_point import strategy_lab_top1_available
from src.application.shadow_replay.common import render_json_text
from src.application.strategy_lab.top1.contracts import (
    PREVIEW_SCHEMA_VERSION,
    RECIPE_ID,
    RECIPE_VERSION,
    Top1CoreContractError,
    build_research_spec_sha256,
    build_sell_put_top1_research_preview_sha256,
    build_sell_put_top1_research_spec,
    validate_confirmed_start_command,
)
from src.application.strategy_lab.top1.economics import build_fx_rate_binding
from src.application.strategy_lab.top1.lifecycle import (
    Top1LifecycleError,
    authorize_research,
    prepare_experiment,
)
from src.application.strategy_lab.top1.research import (
    RESEARCH_EVALUATION_INPUT_SCHEMA,
    ResearchEvaluationError,
    required_research_close_keys,
)
from src.application.strategy_lab.top1.research_runner import run_research
from src.application.strategy_lab.top1.readiness import CAPABILITY_FACTS
from src.application.strategy_lab.top1.research_window import (
    ResearchWindowError,
    build_research_window,
    load_research_window,
)
from src.application.strategy_lab.top1.terminal_projection import publish_exact_text
from src.infrastructure.strategy_lab.experiment_store import ExperimentStore


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


def _preview(
    *,
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
        "stage": "research",
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


def _research_preview(
    artifact_root: str | Path,
    *,
    market: str,
    account: str,
    cutoff_at_utc: str,
    latest_mature_trading_date: str,
    market_calendar: Mapping[str, Any],
    datasets_root_ref: str,
    runs_root_ref: str,
    fee_contract: Mapping[str, object],
    capability_facts: Mapping[str, object],
    evidence_bundle: object,
    environ: Mapping[str, str] | None,
) -> tuple[dict[str, object], dict[str, Any] | None, dict[str, object]]:
    if market != "HK" or account != "lx":
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
    try:
        window = build_research_window(
            artifact_root,
            market=market,
            account=account,
            cutoff_at_utc=cutoff_at_utc,
            latest_mature_trading_date=latest_mature_trading_date,
            market_calendar=market_calendar,
            datasets_root_ref=datasets_root_ref,
            runs_root_ref=runs_root_ref,
        )
        window_text = render_json_text(window)
        window_file_sha256 = hashlib.sha256(window_text.encode()).hexdigest()
        window_ref = (
            "strategy_lab/top1/research_windows/"
            f"{window['content_sha256']}.json"
        )
        experiment_id = "sell-put-top1-" + canonical_sha256(
            {
                "topic_id": _TOPIC_ID,
                "market": market,
                "account": account,
                "recipe_id": RECIPE_ID,
                "recipe_version": RECIPE_VERSION,
                "research_window_content_sha256": window["content_sha256"],
            }
        )[:32]
        spec = build_sell_put_top1_research_spec(
            topic_id=_TOPIC_ID,
            experiment_id=experiment_id,
            market_calendar_version=str(window["market_calendar_version"]),
            research_source={
                "mode": "historical_research_window",
                "dataset_ref": window_ref,
                "dataset_sha256": window_file_sha256,
                "research_cutoff_at": cutoff_at_utc,
                "start_trading_date": window["selected_trading_dates"][0],
                "end_trading_date": window["selected_trading_dates"][-1],
            },
        )
        observed_points = load_research_window(artifact_root, window)
        research_input = {
            "schema_version": RESEARCH_EVALUATION_INPUT_SCHEMA,
            "experiment_spec": spec,
            "dataset_ref": window_ref,
            "research_window": window,
            "observed_points": observed_points,
        }
        requirements = required_research_close_keys(research_input, fee_contract)
    except (ResearchWindowError, ResearchEvaluationError, Top1CoreContractError) as exc:
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
        (
            {
                "recommendation_point_id": point["recommendation_point_id"],
                "candidate_facts_sha256": point["candidate_facts_sha256"],
                "source_files": sorted(
                    point["source_files"], key=lambda item: (item["kind"], item["ref"])
                ),
            }
            for day in window["days"]
            for point in day["points"]
        ),
        key=lambda item: str(item["recommendation_point_id"]),
    )
    source_bindings: dict[str, object] = {
        "market_calendar": {
            "ref": window["market_calendar_ref"],
            "content_sha256": window["market_calendar_content_sha256"],
            "file_sha256": window["market_calendar_file_sha256"],
        },
        "research_window": {
            "ref": window_ref,
            "content_sha256": window["content_sha256"],
            "file_sha256": window_file_sha256,
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
        window,
        terminal_bindings,
    )


def preview_sell_put_top1_research(
    artifact_root: str | Path,
    **kwargs: Any,
) -> dict[str, object]:
    preview, _window, _terminal_bindings = _research_preview(
        artifact_root, **kwargs
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
    preview, window, terminal_bindings = _research_preview(
        artifact_root, **preview_kwargs
    )
    if preview["status"] != "available" or window is None:
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
            render_json_text(window).encode(),
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


__all__ = [
    "Top1WorkspaceError",
    "preview_sell_put_top1_research",
    "start_confirmed_research",
]
