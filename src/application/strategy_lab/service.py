from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NoReturn
from zoneinfo import ZoneInfo

from src.application.agent_tool_config import load_runtime_config
from src.application.agent_tool_contracts import AgentToolError
from src.application.account_config import (
    ACCOUNT_TYPE_FUTU,
    resolve_account_type,
    resolve_configured_accounts,
)
from src.application.futu_portfolio_context import infer_futu_portfolio_settings
from src.application.ledger.api import resolve_position_ledger_sqlite_path
from src.application.candidate_snapshot_contract import utc_timestamp
from src.application.opend_fetch_config import resolve_opend_fetch_limits
from src.application.source_identity import source_commit_sha
from src.application.strategy_lab.comparison import compare_single_recommendations
from src.application.strategy_lab.contracts import (
    ACCOUNT,
    MARKET,
    RECIPE_ID,
    StrategyLabContractError,
    build_evaluator_behavior_manifest,
    canonical_sha256,
    evaluator_behavior_sha256,
)
from src.application.strategy_lab.evidence import (
    StrategyLabEvidenceError,
    collect_research_fill_evidence,
    load_research_projection,
    next_missing_research_evidence,
    publish_research_evidence_artifact,
    resolve_expiry_outcome,
)
from src.application.strategy_lab.recipe import check_recipe_readiness, describe_recipe
from src.application.strategy_lab.readiness import HISTORY_K_POC_NOT_BEFORE_HK
from src.application.strategy_lab.receipts import (
    StrategyLabReceiptError,
    build_research_receipt,
    publish_receipt,
    read_receipt_artifact,
)
from src.application.tick_cron import tick_cron_is_busy
from src.infrastructure.futu_gateway import FutuGatewayError, build_futu_gateway
from src.infrastructure.private_storage import exclusive_private_file_lock
from src.infrastructure.strategy_lab.experiment_store import (
    ExperimentStore,
    ExperimentStoreError,
)


_HK_TZ = ZoneInfo("Asia/Hong_Kong")


class StrategyLabContextError(ValueError):
    reason_code = "strategy_lab_context_invalid"


class StrategyLabServiceError(ValueError):
    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


def _fail(message: str) -> NoReturn:
    raise StrategyLabContextError(message)


def _absolute_path(profile: Mapping[str, Any], key: str) -> Path:
    raw = str(profile.get(key) or "").strip()
    path = Path(raw).expanduser()
    if not raw or not path.is_absolute():
        _fail(f"profile {key} must be an absolute path")
    return path


def resolve_strategy_lab_runtime_context(
    profile: Mapping[str, Any],
    *,
    market: str,
) -> dict[str, Any]:
    """Resolve shared Strategy Lab paths without depending on the retired Top1 shell."""

    if not isinstance(profile, Mapping):
        _fail("service profile must be an object")
    market_key = str(market or "").strip().lower()
    markets = profile.get("markets")
    if market_key not in {"hk", "us"} or not isinstance(markets, list) or market_key not in markets:
        _fail("Strategy Lab market is absent from the service profile")
    repo_root = _absolute_path(profile, "repo_root")
    runtime_root = _absolute_path(profile, "runtime_root")
    config_paths = profile.get("config_paths")
    raw_config_path = config_paths.get(market_key) if isinstance(config_paths, Mapping) else None
    config_path = Path(str(raw_config_path or "")).expanduser()
    if not str(raw_config_path or "").strip() or not config_path.is_absolute():
        _fail(f"profile {market_key.upper()} runtime config path is invalid")
    artifact_root = runtime_root / "output_shared" / "research" / "strategy_lab"
    return {
        "profile": dict(profile),
        "repo_root": repo_root,
        "runtime_root": runtime_root,
        "config_path": config_path,
        "market": market_key,
        "artifact_root": artifact_root,
        "store_path": artifact_root / "experiments.sqlite3",
        "opend_limiter_root": runtime_root,
        "tick_lock_path": runtime_root / "locks" / f"tick-{market_key}.lock",
    }


def resolve_strategy_lab_context(profile: Mapping[str, Any]) -> dict[str, Any]:
    """Resolve Strategy Lab from the ordinary HK service/runtime authorities."""

    runtime = resolve_strategy_lab_runtime_context(profile, market="hk")
    repo_root = runtime["repo_root"]
    runtime_root = runtime["runtime_root"]
    account = "lx"
    accounts = profile.get("accounts")
    if not isinstance(accounts, list) or account not in accounts:
        _fail("Strategy Lab requires account lx in the service profile")
    config_hk = runtime["config_path"]
    try:
        _config_path, config = load_runtime_config(
            config_path=config_hk,
            expected_market="hk",
        )
        resolve_configured_accounts(config, requested=account)
        if resolve_account_type(config, account=account) != ACCOUNT_TYPE_FUTU:
            _fail("Strategy Lab account lx must use the Futu account type")
        binding = infer_futu_portfolio_settings(config, account=account)
        ledger_path = resolve_position_ledger_sqlite_path(
            base=repo_root,
            cfg=config,
            config_path=config_hk,
            runtime_root=runtime_root,
        )
    except (AgentToolError, OSError, ValueError) as exc:
        raise StrategyLabContextError(str(exc)) from exc
    host = binding.get("host")
    port = binding.get("port")
    if not isinstance(host, str) or not host.strip() or type(port) is not int or not 0 < port <= 65535:
        _fail("Strategy Lab HK OpenD binding is missing or invalid")
    artifact_root = runtime["artifact_root"]
    return {
        "profile": dict(profile),
        "repo_root": repo_root,
        "runtime_root": runtime_root,
        "config_hk": config_hk,
        "market": "hk",
        "account": account,
        "opend_binding": {"host": host.strip(), "port": port},
        "ledger_path": ledger_path,
        "store_path": runtime["store_path"],
        "artifact_root": artifact_root,
        "opend_limiter_root": runtime_root,
        "tick_lock_path": runtime_root / "locks" / "tick-hk.lock",
    }


def _preview_request(value: Mapping[str, Any]) -> dict[str, str]:
    expected = {
        "hypothesis",
        "recipe_id",
        "market",
        "account",
        "maturity_cutoff_utc",
        "fee_plan_receipt_path",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        _fail("Strategy Lab preview request fields are invalid")
    hypothesis = value.get("hypothesis")
    if not isinstance(hypothesis, str) or not hypothesis.strip():
        _fail("Strategy Lab hypothesis must be non-empty text")
    if value.get("recipe_id") != RECIPE_ID:
        _fail("Strategy Lab Recipe is unsupported")
    if (value.get("market"), value.get("account")) != (MARKET, ACCOUNT):
        _fail("Strategy Lab preview supports only hk/lx")
    cutoff = utc_timestamp(value.get("maturity_cutoff_utc"), "maturity_cutoff_utc")
    fee_path = Path(str(value.get("fee_plan_receipt_path") or "")).expanduser()
    if not fee_path.is_absolute():
        _fail("Strategy Lab fee-plan receipt path must be absolute")
    return {
        "hypothesis": hypothesis.strip(),
        "recipe_id": RECIPE_ID,
        "market": MARKET,
        "account": ACCOUNT,
        "maturity_cutoff_utc": cutoff,
        "fee_plan_receipt_path": str(fee_path),
    }


def _occurred_at(value: object) -> str:
    return utc_timestamp(value, "occurred_at_utc")


def _require_preview_context(context: Mapping[str, Any]) -> None:
    if not isinstance(context, Mapping) or (context.get("market"), context.get("account")) != (
        MARKET,
        ACCOUNT,
    ):
        _fail("Strategy Lab preview context must use hk/lx")


def list_recipes(
    context: Mapping[str, Any],
    *,
    fee_plan_receipt_path: str | Path,
    maturity_cutoff_utc: str,
    occurred_at_utc: str,
) -> dict[str, Any]:
    _require_preview_context(context)
    request = _preview_request(
        {
            "hypothesis": "catalog readiness",
            "recipe_id": RECIPE_ID,
            "market": MARKET,
            "account": ACCOUNT,
            "maturity_cutoff_utc": maturity_cutoff_utc,
            "fee_plan_receipt_path": str(fee_plan_receipt_path),
        }
    )
    readiness = check_recipe_readiness(
        context,
        request,
        occurred_at_utc=_occurred_at(occurred_at_utc),
    )
    return {
        "recipes": [
            {
                **describe_recipe(RECIPE_ID),
                "readiness": {
                    "status": readiness["status"],
                    "blockers": readiness["blockers"],
                },
            }
        ]
    }


def preview_experiment(
    context: Mapping[str, Any],
    request: Mapping[str, Any],
    *,
    occurred_at_utc: str,
) -> dict[str, Any]:
    _require_preview_context(context)
    canonical_request = _preview_request(request)
    readiness = check_recipe_readiness(
        context,
        canonical_request,
        occurred_at_utc=_occurred_at(occurred_at_utc),
    )
    blockers = list(readiness["blockers"])
    source_sha = source_commit_sha(Path(context["repo_root"]))
    if source_sha is None:
        blockers.append(
            {
                "reason_code": "source_commit_unavailable",
                "message": "Strategy Lab requires a clean source commit",
            }
        )
    try:
        behavior_manifest = build_evaluator_behavior_manifest(context["repo_root"])
        behavior_sha256 = evaluator_behavior_sha256(behavior_manifest)
    except StrategyLabContractError as exc:
        behavior_manifest = []
        behavior_sha256 = None
        blockers.append({"reason_code": exc.reason_code, "message": str(exc)})
    spec: dict[str, Any] = {
        "hypothesis": canonical_request["hypothesis"],
        "recipe": describe_recipe(RECIPE_ID),
        "scope": {"market": MARKET, "account": ACCOUNT, "strategy": "sell_put"},
        "research_window": readiness["window"],
        "fee_plan": {
            "receipt_path": canonical_request["fee_plan_receipt_path"],
            "receipt": readiness["fee_plan"],
        },
        "terminal_fx_bindings": readiness["terminal_fx_bindings"],
        "history_k_authority": readiness["history_k_authority"],
        "source_commit_sha": source_sha,
        "behavior_manifest": behavior_manifest,
        "evaluator_behavior_sha256": behavior_sha256,
    }
    spec_sha256 = canonical_sha256(spec)
    preview = {
        "request": canonical_request,
        "spec": spec,
        "spec_sha256": spec_sha256,
    }
    return {
        "status": "available" if not blockers else "blocked",
        "blockers": blockers,
        **preview,
        "preview_sha256": canonical_sha256(preview),
    }


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise StrategyLabServiceError("experiment_input_invalid", f"{label} is invalid")
    return value


def _required_sha256(value: object, label: str) -> str:
    text = _required_text(value, label)
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise StrategyLabServiceError("experiment_input_invalid", f"{label} is invalid")
    return text


def _store(context: Mapping[str, Any], *, initialize: bool = False) -> ExperimentStore:
    path = Path(context["store_path"])
    if not initialize and (path.is_symlink() or not path.is_file()):
        raise StrategyLabServiceError("experiment_store_not_found", "experiment Store does not exist")
    store = ExperimentStore(path)
    if initialize:
        store.initialize()
    return store


def _experiment(store: ExperimentStore, experiment_id: object) -> dict[str, Any]:
    item = store.get_experiment(_required_text(experiment_id, "experiment_id"))
    if item is None:
        raise StrategyLabServiceError("experiment_not_found", "experiment does not exist")
    return item


def _experiment_view(item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: item[key]
        for key in (
            "experiment_id",
            "state",
            "spec_sha256",
            "source_commit_sha",
            "evaluator_behavior_sha256",
            "leader",
            "research_receipt_ref",
            "research_receipt_sha256",
            "revision",
            "created_at_utc",
            "updated_at_utc",
        )
    }


def confirm_research(
    context: Mapping[str, Any],
    request: Mapping[str, Any],
    *,
    confirmed_preview_sha256: str,
    actor: str,
    idempotency_key: str,
    occurred_at_utc: str,
) -> dict[str, Any]:
    """Rebuild and persist only the exact currently available preview."""

    confirmation = _required_sha256(confirmed_preview_sha256, "confirmed_preview_sha256")
    actor_text = _required_text(actor, "actor")
    key = _required_text(idempotency_key, "idempotency_key")
    occurred = _occurred_at(occurred_at_utc)
    preview = preview_experiment(context, request, occurred_at_utc=occurred)
    if preview["status"] != "available":
        raise StrategyLabServiceError(
            "experiment_preview_blocked", "experiment preview is not currently available"
        )
    if preview["preview_sha256"] != confirmation:
        raise StrategyLabServiceError(
            "experiment_confirmation_mismatch", "confirmed preview hash changed"
        )
    spec = preview["spec"]
    experiment_id = "exp-" + canonical_sha256(
        {"confirmed_preview_sha256": confirmation, "idempotency_key": key}
    )
    try:
        item = _store(context, initialize=True).create_experiment(
            experiment_id=experiment_id,
            spec=spec,
            spec_sha256=preview["spec_sha256"],
            source_commit_sha=spec["source_commit_sha"],
            behavior_manifest=spec["behavior_manifest"],
            evaluator_behavior_sha256=spec["evaluator_behavior_sha256"],
            confirmation_sha256=confirmation,
            idempotency_key=key,
            actor=actor_text,
            occurred_at_utc=occurred,
        )
    except (ExperimentStoreError, OSError) as exc:
        raise StrategyLabServiceError(
            str(getattr(exc, "reason_code", "experiment_store_incompatible")), str(exc)
        ) from exc
    return {"status": "confirmed", "experiment": _experiment_view(item)}


def get_experiment_status(
    context: Mapping[str, Any], experiment_id: str
) -> dict[str, Any]:
    """Read the durable experiment and observation summary without creating state."""

    try:
        store = _store(context)
        item = _experiment(store, experiment_id)
        observations = store.list_observations(item["experiment_id"])
    except (ExperimentStoreError, OSError) as exc:
        raise StrategyLabServiceError(
            str(getattr(exc, "reason_code", "experiment_store_incompatible")), str(exc)
        ) from exc
    counts: dict[str, int] = {}
    for observation in observations:
        key = str(observation["kind"])
        counts[key] = counts.get(key, 0) + 1
    try:
        _current_behavior(context, item)
        projection = load_research_projection(item["spec"])
        indexed = {value["observation_key"]: value for value in observations}

        def observation_status(key: str) -> object:
            observation = indexed.get(key)
            payload = observation.get("payload") if isinstance(observation, Mapping) else None
            if isinstance(payload, Mapping) and isinstance(payload.get("payload"), Mapping):
                payload = payload["payload"]
            return payload.get("status") if isinstance(payload, Mapping) else None

        required_outcomes = {
            arm["expiry_close_query_sha256"]
            for arm in projection["arms"]
            if observation_status(arm["research_fill_key"]) == "simulated_fill"
        }
        progress = {
            "history_k_queries": {
                "completed": counts.get("history_k_query", 0),
                "total": len(projection["history_k_queries"]),
            },
            "research_fills": {
                "completed": counts.get("research_fill", 0),
                "total": len(projection["arms"]),
            },
            "expiry_close_queries": {
                "completed": sum(
                    f"expiry_close_query:{digest}" in indexed
                    for digest in required_outcomes
                ),
                "total_required": len(required_outcomes),
            },
            "single_results": {
                "completed": counts.get("single_result", 0),
                "total": len(projection["arms"]),
            },
        }
        blocker = None
        if item["state"] == "research_running":
            action = next_missing_research_evidence(
                item["spec"], observations, context["artifact_root"]
            )
            next_action = {
                "action": action["action"],
                "observation_key": action.get("observation_key"),
                "provider_required": action["action"].startswith("collect_"),
                "provider_admission_checked": False,
            }
        elif item["state"] == "research_complete":
            next_action = {"action": "publish_research_receipt", "provider_required": False}
        elif item["state"] == "awaiting_validation_confirmation":
            next_action = {
                "action": "awaiting_validation_confirmation",
                "provider_required": False,
            }
        else:
            next_action = {"action": "none", "provider_required": False}
    except StrategyLabServiceError as exc:
        progress = None
        blocker = {"reason_code": exc.reason_code, "message": str(exc)}
        next_action = {
            "action": "restore_evaluator_behavior",
            "provider_required": False,
        }
    except StrategyLabEvidenceError as exc:
        progress = None
        blocker = {"reason_code": exc.reason_code, "message": str(exc)}
        next_action = {"action": "repair_research_evidence", "provider_required": False}
    return {
        "experiment": _experiment_view(item),
        "observation_count": len(observations),
        "observation_counts": counts,
        "progress": progress,
        "blocker": blocker,
        "next_action": next_action,
    }


def _current_behavior(context: Mapping[str, Any], experiment: Mapping[str, Any]) -> str:
    try:
        manifest = build_evaluator_behavior_manifest(context["repo_root"])
        behavior_sha = evaluator_behavior_sha256(manifest)
    except StrategyLabContractError as exc:
        raise StrategyLabServiceError(exc.reason_code, str(exc)) from exc
    frozen_behavior = experiment["evaluator_behavior_sha256"]
    spec = experiment.get("spec")
    if (
        not isinstance(spec, Mapping)
        or spec.get("evaluator_behavior_sha256") != frozen_behavior
        or behavior_sha != frozen_behavior
    ):
        raise StrategyLabServiceError(
            "evaluator_behavior_mismatch", "confirmed evaluator behavior changed"
        )
    return behavior_sha


def _blocked(experiment: Mapping[str, Any], reason_code: str) -> dict[str, Any]:
    return {
        "status": "blocked",
        "reason_code": reason_code,
        "experiment_id": experiment["experiment_id"],
        "state": experiment["state"],
    }


def _observe_source_commit(
    store: ExperimentStore,
    experiment: dict[str, Any],
    source_commit: str,
    *,
    actor: str,
    occurred_at_utc: str,
) -> dict[str, Any]:
    if source_commit == experiment["source_commit_sha"]:
        return experiment
    return store.append_event_and_transition(
        experiment["experiment_id"],
        expected_state="research_running",
        expected_revision=experiment["revision"],
        new_state="research_running",
        event_type="source_commit_observed",
        actor=actor,
        payload={"source_commit_sha": source_commit},
        occurred_at_utc=occurred_at_utc,
        idempotency_key=f"source_commit_observed:{experiment['experiment_id']}:{source_commit}",
    )


def _provider_guard(context: Mapping[str, Any], occurred_at_utc: str) -> str | None:
    occurred = datetime.fromisoformat(occurred_at_utc.replace("Z", "+00:00"))
    local = occurred.astimezone(_HK_TZ)
    if local.weekday() < 5 and local.time() < HISTORY_K_POC_NOT_BEFORE_HK:
        return "tick_protection_window"
    if tick_cron_is_busy(context["tick_lock_path"]):
        return "tick_busy"
    return None


def _put_action(
    store: ExperimentStore,
    experiment_id: str,
    action: Mapping[str, Any],
    occurred_at_utc: str,
) -> None:
    if action["action"] == "bind_artifact":
        artifact = action["artifact"]
        payload = artifact["payload"]
        store.put_observation(
            experiment_id,
            observation_key=action["observation_key"],
            kind=action["kind"],
            status=payload["status"],
            payload={
                "query": action["query"],
                "query_sha256": action["query_sha256"],
                "payload": payload,
            },
            artifact_ref=artifact["artifact_ref"],
            artifact_sha256=artifact["artifact_sha256"],
            created_at_utc=occurred_at_utc,
        )
        return
    payload = action["payload"]
    store.put_observation(
        experiment_id,
        observation_key=action["observation_key"],
        recommendation_point_id=action.get("recommendation_point_id"),
        arm_id=action.get("arm_id"),
        kind=action["kind"],
        status=payload["status"],
        payload=payload,
        created_at_utc=occurred_at_utc,
    )


def _comparisons(spec: Mapping[str, Any], observations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    projection = load_research_projection(spec)
    expected = [
        {
            "recommendation_point_id": item["recommendation_point_id"],
            "trading_day": item["trading_day"],
        }
        for item in projection["expected_points"]
    ]
    results = [item["payload"] for item in observations if item["kind"] == "single_result"]
    baseline = [item for item in results if item.get("arm") == "baseline"]
    variants = sorted(
        {
            str(item["variant_id"])
            for item in results
            if item.get("arm") == "challenger" and item.get("variant_id")
        }
    )
    comparisons: list[dict[str, Any]] = []
    for variant in variants:
        challenger = [item for item in results if item.get("variant_id") == variant]
        comparison = compare_single_recommendations(expected, baseline, challenger)
        comparison.setdefault("variant_id", variant)
        comparison.setdefault("near_return_threshold", challenger[0]["near_return_threshold"])
        comparisons.append(comparison)
    return comparisons


def _conclude_research(
    context: Mapping[str, Any],
    store: ExperimentStore,
    experiment: Mapping[str, Any],
    *,
    actor: str,
) -> dict[str, Any]:
    observations = store.list_observations(experiment["experiment_id"])
    comparisons = _comparisons(experiment["spec"], observations)
    receipt = build_research_receipt(
        experiment,
        observations,
        comparisons,
        experiment["updated_at_utc"],
    )
    published = publish_receipt(
        context["artifact_root"], experiment["experiment_id"], "research", receipt
    )
    conclusion = receipt["conclusion"]
    leader = conclusion["leader"] if conclusion["status"] == "leader" else None
    new_state = "awaiting_validation_confirmation" if leader is not None else "completed"
    attached = store.attach_research_receipt_and_transition(
        experiment["experiment_id"],
        expected_state="research_complete",
        expected_revision=experiment["revision"],
        new_state=new_state,
        receipt_ref=published["receipt_ref"],
        receipt_sha256=published["receipt_sha256"],
        leader=leader,
        actor=actor,
        occurred_at_utc=experiment["updated_at_utc"],
        payload={"status": conclusion["status"], "reason_code": conclusion["reason_code"]},
        idempotency_key=f"research_concluded:{published['receipt_sha256']}",
    )
    return {
        "status": "complete",
        "experiment": _experiment_view(attached),
        "receipt_ref": published["receipt_ref"],
        "receipt_sha256": published["receipt_sha256"],
        "conclusion": conclusion,
    }


def execute_research(
    context: Mapping[str, Any],
    experiment_id: str,
    *,
    actor: str,
    occurred_at_utc: str,
) -> dict[str, Any]:
    """Resume local research while consuming at most one provider unit."""

    actor_text = _required_text(actor, "actor")
    occurred = _occurred_at(occurred_at_utc)
    try:
        store = _store(context)
        item = _experiment(store, experiment_id)
        _current_behavior(context, item)
        if item["state"] in {"awaiting_validation_confirmation", "completed"}:
            return {"status": "complete", "experiment": _experiment_view(item)}
        if item["state"] == "research_complete":
            return _conclude_research(context, store, item, actor=actor_text)
        current_source = source_commit_sha(Path(context["repo_root"]))
        if current_source is None:
            return _blocked(item, "source_commit_unavailable")
        provider_units = 0
        while True:
            observations = store.list_observations(item["experiment_id"])
            action = next_missing_research_evidence(
                item["spec"], observations, context["artifact_root"]
            )
            if action["action"] == "complete":
                item = _observe_source_commit(
                    store,
                    item,
                    current_source,
                    actor=actor_text,
                    occurred_at_utc=occurred,
                )
                item = store.append_event_and_transition(
                    item["experiment_id"],
                    expected_state="research_running",
                    expected_revision=item["revision"],
                    new_state="research_complete",
                    event_type="research_materialized",
                    actor=actor_text,
                    payload={"observation_count": len(observations)},
                    occurred_at_utc=occurred,
                    idempotency_key=f"research_materialized:{item['experiment_id']}",
                )
                if item["state"] in {"awaiting_validation_confirmation", "completed"}:
                    return {"status": "complete", "experiment": _experiment_view(item)}
                return _conclude_research(context, store, item, actor=actor_text)
            if action["action"] in {
                "bind_artifact",
                "derive_research_fill",
                "derive_single_result",
            }:
                if action["action"] == "bind_artifact":
                    item = _observe_source_commit(
                        store,
                        item,
                        action["artifact"]["artifact"][
                            "producer_source_commit_sha"
                        ],
                        actor=actor_text,
                        occurred_at_utc=occurred,
                    )
                item = _observe_source_commit(
                    store,
                    item,
                    current_source,
                    actor=actor_text,
                    occurred_at_utc=occurred,
                )
                _put_action(store, item["experiment_id"], action, occurred)
                continue
            if provider_units:
                return {
                    "status": "progress",
                    "experiment_id": item["experiment_id"],
                    "state": item["state"],
                    "provider_logical_units": provider_units,
                    "next_action": action["action"],
                }
            try:
                with exclusive_private_file_lock(action["lock_path"], blocking=False):
                    observations = store.list_observations(item["experiment_id"])
                    locked_action = next_missing_research_evidence(
                        item["spec"], observations, context["artifact_root"]
                    )
                    if locked_action["action"] != action["action"] or locked_action.get(
                        "query_sha256"
                    ) != action.get("query_sha256"):
                        continue
                    blocker = _provider_guard(context, occurred)
                    if blocker is not None:
                        return _blocked(item, blocker)
                    source = locked_action["query"].get("provider_source")
                    frozen_binding = (
                        source.get("opend_binding") if isinstance(source, Mapping) else None
                    )
                    if frozen_binding != context["opend_binding"]:
                        return _blocked(item, "research_provider_binding_mismatch")
                    _config_path, config = load_runtime_config(
                        config_path=context["config_hk"], expected_market="hk"
                    )
                    limit = resolve_opend_fetch_limits(config).history_kline
                    binding = context["opend_binding"]
                    gateway = build_futu_gateway(
                        host=str(binding["host"]),
                        port=int(binding["port"]),
                        is_option_chain_cache_enabled=False,
                    )
                    try:
                        if locked_action["action"] == "collect_history_k":
                            payload = collect_research_fill_evidence(
                                gateway,
                                locked_action["query"],
                                limiter_root=context["opend_limiter_root"],
                                window_sec=limit.window_sec,
                                max_calls=limit.max_calls,
                            )
                        else:
                            query = locked_action["query"]
                            payload = resolve_expiry_outcome(
                                gateway,
                                query,
                                query["fee_plan"],
                                query["terminal_fx_binding"],
                                limiter_root=context["opend_limiter_root"],
                                window_sec=limit.window_sec,
                                max_calls=limit.max_calls,
                            )
                        item = _observe_source_commit(
                            store,
                            item,
                            current_source,
                            actor=actor_text,
                            occurred_at_utc=occurred,
                        )
                        publish_research_evidence_artifact(
                            context["artifact_root"],
                            locked_action["artifact_kind"],
                            locked_action["query_sha256"],
                            payload,
                            query=locked_action["query"],
                            observed_at_utc=occurred,
                            producer_source_commit_sha=current_source,
                        )
                    finally:
                        gateway.close()
                    provider_units = 1
            except BlockingIOError:
                return _blocked(item, "research_evidence_busy")
            except StrategyLabEvidenceError as exc:
                if exc.reason_code in {"opend_low_priority_deferred", "research_provider_failed"}:
                    return _blocked(item, exc.reason_code)
                raise
            except FutuGatewayError:
                return _blocked(item, "research_provider_failed")
    except StrategyLabServiceError:
        raise
    except (
        ExperimentStoreError,
        StrategyLabEvidenceError,
        StrategyLabReceiptError,
        OSError,
    ) as exc:
        raise StrategyLabServiceError(
            str(getattr(exc, "reason_code", "strategy_lab_research_failed")), str(exc)
        ) from exc


def read_receipt(
    context: Mapping[str, Any], experiment_id: str, *, kind: str = "research"
) -> dict[str, Any]:
    """Read the one Phase 2 receipt without creating Store or provider state."""

    try:
        store = _store(context)
        item = _experiment(store, experiment_id)
        receipt_ref = item["research_receipt_ref"]
        receipt_sha256 = item["research_receipt_sha256"]
        if receipt_ref is None and receipt_sha256 is None:
            raise StrategyLabServiceError(
                "receipt_not_found", "research receipt is not attached"
            )
        if (
            not isinstance(receipt_ref, str)
            or not isinstance(receipt_sha256, str)
            or item["state"] not in {"awaiting_validation_confirmation", "completed"}
        ):
            raise StrategyLabServiceError(
                "receipt_immutable_conflict", "research receipt Store binding is invalid"
            )
        try:
            artifact = read_receipt_artifact(
                context["artifact_root"], item["experiment_id"], kind
            )
        except StrategyLabReceiptError as exc:
            if exc.reason_code == "receipt_not_found":
                raise StrategyLabServiceError(
                    "receipt_artifact_invalid", "attached research receipt is missing"
                ) from exc
            raise
        if (
            artifact["receipt_ref"] != receipt_ref
            or artifact["receipt_sha256"] != receipt_sha256
        ):
            raise StrategyLabServiceError(
                "receipt_immutable_conflict", "research receipt artifact changed"
            )
    except StrategyLabServiceError:
        raise
    except (ExperimentStoreError, StrategyLabReceiptError, OSError) as exc:
        raise StrategyLabServiceError(
            str(getattr(exc, "reason_code", "receipt_not_found")), str(exc)
        ) from exc
    return {"experiment": _experiment_view(item), **artifact}


__all__ = [
    "StrategyLabContextError",
    "StrategyLabServiceError",
    "confirm_research",
    "execute_research",
    "get_experiment_status",
    "list_recipes",
    "preview_experiment",
    "read_receipt",
    "resolve_strategy_lab_context",
    "resolve_strategy_lab_runtime_context",
]
