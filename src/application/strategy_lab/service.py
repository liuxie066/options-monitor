from __future__ import annotations

import hashlib
import sys
from collections.abc import Mapping
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, NoReturn
from zoneinfo import ZoneInfo

from src.application.agent_tool_config import load_runtime_config
from src.application.agent_tool_contracts import AgentToolError
from src.application.account_config import (
    ACCOUNT_TYPE_FUTU,
    resolve_account_type,
    resolve_configured_accounts,
)
from src.application.account_run import build_account_runtime_config
from src.application.futu_portfolio_context import infer_futu_portfolio_settings
from src.application.ledger.api import resolve_position_ledger_sqlite_path
from src.application.candidate_snapshot_contract import utc_timestamp
from src.application.opend_fetch_config import OpenDEndpointRateLimit, resolve_opend_fetch_limits
from src.application.opend_market_snapshot_fetching import fetch_option_snapshots
from src.application.opend_call_coordinator import (
    LowPriorityOpenDCallDeferred,
    try_low_priority_opend_call,
)
from src.application.research.formal_corpus import (
    FormalCorpusError,
    load_formal_expectation,
    load_formal_point,
)
from src.application.service_deploy import next_systemd_tick_target_utc
from src.application.source_identity import source_commit_sha
from src.application.strategy_lab.comparison import compare_single_recommendations
from src.application.strategy_lab.contracts import (
    ACCOUNT,
    HIDDEN_SNAPSHOT_BATCH_CEILING,
    HIDDEN_SNAPSHOT_LOW_PRIORITY_CALLS_PER_WINDOW,
    MARKET,
    RECIPE_ID,
    TICK_PROTECTION_SECONDS,
    VALIDATION_WAKE_TOLERANCE_SECONDS,
    StrategyLabContractError,
    build_evaluator_behavior_manifest,
    build_strategy_lab_timer_binding,
    canonical_sha256,
    evaluator_behavior_sha256,
)
from src.application.strategy_lab.evidence import (
    StrategyLabEvidenceError,
    build_hidden_batch_manifest,
    build_validation_fill_evidence,
    build_validation_point_evidence,
    collect_research_fill_evidence,
    evidence_artifact_location,
    hidden_quote_rows,
    load_research_projection,
    next_missing_research_evidence,
    normalize_hidden_snapshot,
    publish_evidence_artifact,
    read_evidence_artifact,
    resolve_expiry_outcome,
)
from src.application.strategy_lab.recipe import (
    StrategyLabRecipeError,
    build_validation_plan,
    check_recipe_readiness,
    describe_recipe,
)
from src.application.strategy_lab.readiness import HISTORY_K_POC_NOT_BEFORE_HK
from src.application.strategy_lab.receipts import (
    StrategyLabReceiptError,
    build_research_receipt,
    publish_receipt,
    read_receipt_artifact,
)
from src.application.tick_cron import tick_cron_is_busy
from src.application.tick_run_workspace import canonical_account_run_config_bytes
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
            "validation_plan_sha256",
            "final_receipt_ref",
            "final_receipt_sha256",
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


def _validation_request(experiment_id: object, requested_start: object) -> dict[str, str]:
    identity = _required_text(experiment_id, "experiment_id")
    start = _required_text(requested_start, "requested_start")
    try:
        parsed = datetime.strptime(start, "%Y-%m-%d").date()
    except ValueError as exc:
        raise StrategyLabServiceError(
            "validation_plan_invalid", "requested_start must use YYYY-MM-DD"
        ) from exc
    if parsed.isoformat() != start:
        raise StrategyLabServiceError(
            "validation_plan_invalid", "requested_start must be canonical"
        )
    return {"experiment_id": identity, "requested_start": start}


def _research_confirmation(events: list[dict[str, Any]]) -> dict[str, Any]:
    matches = [event for event in events if event.get("event_type") == "research_confirmed"]
    if len(matches) != 1:
        raise StrategyLabServiceError(
            "validation_plan_invalid", "research confirmation identity is unavailable"
        )
    event = matches[0]
    confirmation = _required_sha256(event.get("confirmation_sha256"), "confirmation_sha256")
    return {
        "sequence": event["sequence"],
        "confirmation_sha256": confirmation,
        "idempotency_key": event["idempotency_key"],
        "occurred_at_utc": event["occurred_at_utc"],
    }


def _hk_schedule(config: Mapping[str, Any]) -> dict[str, Any]:
    selected = config.get("schedule_hk")
    if not isinstance(selected, Mapping):
        fallback = config.get("schedule")
        selected = (
            fallback
            if isinstance(fallback, Mapping)
            and fallback.get("timezone") == "Asia/Hong_Kong"
            else None
        )
    if not isinstance(selected, Mapping):
        raise StrategyLabServiceError(
            "validation_plan_invalid", "HK schedule is unavailable"
        )
    return dict(selected)


def preview_validation(
    context: Mapping[str, Any],
    experiment_id: str,
    requested_start: str,
    *,
    occurred_at_utc: str,
) -> dict[str, Any]:
    """Build the second-confirmation payload without provider access or writes."""

    request = _validation_request(experiment_id, requested_start)
    occurred = _occurred_at(occurred_at_utc)
    try:
        store = _store(context)
        item = _experiment(store, request["experiment_id"])
        if item["state"] != "awaiting_validation_confirmation":
            raise StrategyLabServiceError(
                "validation_preview_blocked", "experiment is not awaiting validation confirmation"
            )
        _current_behavior(context, item)
        current_source = source_commit_sha(Path(context["repo_root"]))
        if current_source is None:
            raise StrategyLabServiceError(
                "source_commit_unavailable", "Strategy Lab requires a clean source commit"
            )
        receipt = read_receipt(context, item["experiment_id"])["receipt"]
        events = store.list_events(item["experiment_id"])
        config_path, config = load_runtime_config(
            config_path=context["config_hk"], expected_market="hk"
        )
        account_config = build_account_runtime_config(
            base_cfg=config,
            cfg_path=config_path,
            account=ACCOUNT,
            markets_to_run=["HK"],
        )
        account_config_sha256 = hashlib.sha256(
            canonical_account_run_config_bytes(account_config)
        ).hexdigest()
        authority = {
            "provider": "futu_opend",
            "endpoint": "market_snapshot",
            "opend_binding": dict(context["opend_binding"]),
        }
        provider_source = {
            **authority,
            "source_authority_sha256": canonical_sha256(authority),
        }
        plan = build_validation_plan(
            context,
            item,
            receipt,
            _research_confirmation(events),
            requested_start=request["requested_start"],
            occurred_at_utc=occurred,
            schedule=_hk_schedule(config),
            account_run_config_sha256=account_config_sha256,
            provider_source=provider_source,
            timer_binding=build_strategy_lab_timer_binding(),
        )
    except StrategyLabServiceError as exc:
        return {
            "status": "blocked",
            "blockers": [{"reason_code": exc.reason_code, "message": str(exc)}],
            "occurred_at_utc": occurred,
            "request": request,
        }
    except (
        AgentToolError,
        ExperimentStoreError,
        StrategyLabRecipeError,
        StrategyLabReceiptError,
        OSError,
        TypeError,
        ValueError,
    ) as exc:
        return {
            "status": "blocked",
            "blockers": [
                {
                    "reason_code": str(
                        getattr(exc, "reason_code", "validation_plan_invalid")
                    ),
                    "message": str(exc),
                }
            ],
            "occurred_at_utc": occurred,
            "request": request,
        }
    plan_sha256 = canonical_sha256(plan)
    payload = {
        "request": request,
        "validation_plan": plan,
        "validation_plan_sha256": plan_sha256,
    }
    return {
        "status": "available",
        "blockers": [],
        "occurred_at_utc": occurred,
        **payload,
        "preview_sha256": canonical_sha256(payload),
    }


def confirm_validation(
    context: Mapping[str, Any],
    experiment_id: str,
    requested_start: str,
    *,
    confirmed_preview_sha256: str,
    actor: str,
    idempotency_key: str,
    occurred_at_utc: str,
) -> dict[str, Any]:
    confirmation = _required_sha256(
        confirmed_preview_sha256, "confirmed_preview_sha256"
    )
    actor_text = _required_text(actor, "actor")
    key = _required_text(idempotency_key, "idempotency_key")
    occurred = _occurred_at(occurred_at_utc)
    try:
        retry_store = _store(context)
        retry_item = _experiment(retry_store, experiment_id)
        if retry_item["state"] in {
            "validation_collecting",
            "waiting_outcome",
            "completed",
        }:
            matches = [
                event
                for event in retry_store.list_events(retry_item["experiment_id"])
                if event.get("idempotency_key") == key
            ]
            plan = retry_item.get("validation_plan")
            if len(matches) != 1 or not isinstance(plan, Mapping):
                raise StrategyLabServiceError("idempotency_conflict", "validation retry changed")
            event = matches[0]
            payload = event.get("payload")
            if (
                not isinstance(payload, Mapping)
                or event.get("event_type") != "validation_confirmed"
                or event.get("actor") != actor_text
                or event.get("confirmation_sha256") != confirmation
                or plan.get("requested_start") != requested_start
            ):
                raise StrategyLabServiceError("idempotency_conflict", "validation retry changed")
            item = retry_store.confirm_validation(
                retry_item["experiment_id"],
                expected_revision=payload["expected_revision"],
                validation_plan=plan,
                validation_plan_sha256=retry_item["validation_plan_sha256"],
                preview_sha256=confirmation,
                actor=actor_text,
                idempotency_key=key,
                occurred_at_utc=occurred,
            )
            return {"status": "confirmed", "experiment": _experiment_view(item)}
    except StrategyLabServiceError:
        raise
    except (ExperimentStoreError, KeyError, OSError) as exc:
        raise StrategyLabServiceError(
            str(getattr(exc, "reason_code", "experiment_store_incompatible")), str(exc)
        ) from exc
    preview = preview_validation(
        context,
        experiment_id,
        requested_start,
        occurred_at_utc=occurred,
    )
    if preview["status"] != "available":
        raise StrategyLabServiceError(
            "validation_preview_blocked", "validation preview is not currently available"
        )
    if preview["preview_sha256"] != confirmation:
        raise StrategyLabServiceError(
            "validation_confirmation_mismatch", "confirmed validation preview changed"
        )
    try:
        store = _store(context)
        item = _experiment(store, experiment_id)
        current_source = source_commit_sha(Path(context["repo_root"]))
        if current_source is None:
            raise StrategyLabServiceError(
                "source_commit_unavailable", "Strategy Lab requires a clean source commit"
            )
        item = _observe_source_commit(
            store,
            item,
            current_source,
            actor=actor_text,
            occurred_at_utc=occurred,
        )
        item = store.confirm_validation(
            item["experiment_id"],
            expected_revision=item["revision"],
            validation_plan=preview["validation_plan"],
            validation_plan_sha256=preview["validation_plan_sha256"],
            preview_sha256=confirmation,
            actor=actor_text,
            idempotency_key=key,
            occurred_at_utc=occurred,
        )
    except StrategyLabServiceError:
        raise
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
        indexed = {value["observation_key"]: value for value in observations}

        if item["state"] in {"validation_collecting", "waiting_outcome"}:
            plan = item.get("validation_plan")
            if (
                not isinstance(plan, Mapping)
                or canonical_sha256(plan) != item.get("validation_plan_sha256")
            ):
                raise StrategyLabServiceError(
                    "validation_plan_invalid", "frozen validation plan binding changed"
                )
            calendar = plan.get("market_calendar") if isinstance(plan, Mapping) else None
            sessions = calendar.get("sessions") if isinstance(calendar, Mapping) else None
            if not isinstance(sessions, list):
                raise StrategyLabServiceError(
                    "validation_plan_invalid", "frozen validation plan is unavailable"
                )
            expected_points = sum(
                len(session.get("expected_recommendation_point_ids", []))
                for session in sessions
                if isinstance(session, Mapping)
            )
            progress = {
                "validation_sessions": {"total": len(sessions)},
                "validation_points": {
                    "completed": counts.get("validation_point", 0),
                    "total": expected_points,
                },
                "hidden_batches": {
                    "settled": sum(
                        observation["status"] in {"complete", "gap"}
                        for observation in observations
                        if observation["kind"] == "hidden_batch"
                    ),
                    "started": sum(
                        observation["status"] == "started"
                        for observation in observations
                        if observation["kind"] == "hidden_batch"
                    ),
                },
                "validation_fills": {"completed": counts.get("validation_fill", 0)},
            }
            blocker = None
            next_action = {
                "action": (
                    "collect_validation_evidence"
                    if item["state"] == "validation_collecting"
                    else "settle_validation_outcomes"
                ),
                "provider_required": True,
                "provider_admission_checked": False,
            }
            return {
                "experiment": _experiment_view(item),
                "observation_count": len(observations),
                "observation_counts": counts,
                "progress": progress,
                "blocker": blocker,
                "next_action": next_action,
            }

        projection = load_research_projection(item["spec"])

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
        next_action = (
            {"action": "inspect_validation_plan", "provider_required": False}
            if exc.reason_code == "validation_plan_invalid"
            else {
                "action": "restore_evaluator_behavior",
                "provider_required": False,
            }
        )
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
        expected_state=experiment["state"],
        expected_revision=experiment["revision"],
        new_state=experiment["state"],
        event_type="source_commit_observed",
        actor=actor,
        payload={"source_commit_sha": source_commit},
        occurred_at_utc=occurred_at_utc,
        idempotency_key=f"source_commit_observed:{experiment['experiment_id']}:{source_commit}",
    )


def _observed_source_commits(
    store: ExperimentStore, experiment: Mapping[str, Any]
) -> set[str]:
    commits = {str(experiment["source_commit_sha"])}
    for event in store.list_events(experiment["experiment_id"]):
        if event.get("event_type") != "source_commit_observed":
            continue
        command = event.get("payload")
        payload = command.get("payload") if isinstance(command, Mapping) else None
        source_commit = payload.get("source_commit_sha") if isinstance(payload, Mapping) else None
        if isinstance(source_commit, str):
            commits.add(source_commit)
    return commits


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
        if item["state"] in {
            "awaiting_validation_confirmation",
            "validation_collecting",
            "waiting_outcome",
            "completed",
        }:
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
                        publish_evidence_artifact(
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


_ADVANCE_ACTOR = "strategy-lab-advance"


def _validation_plan(experiment: Mapping[str, Any]) -> dict[str, Any]:
    plan = experiment.get("validation_plan")
    timer_binding = build_strategy_lab_timer_binding()
    if (
        not isinstance(plan, Mapping)
        or canonical_sha256(plan) != experiment.get("validation_plan_sha256")
        or plan.get("experiment_id") != experiment.get("experiment_id")
        or plan.get("hidden_snapshot_batch_ceiling")
        != HIDDEN_SNAPSHOT_BATCH_CEILING
        or plan.get("validation_wake_tolerance_seconds")
        != VALIDATION_WAKE_TOLERANCE_SECONDS
        or plan.get("tick_protection_seconds") != TICK_PROTECTION_SECONDS
        or plan.get("timer_binding") != timer_binding
        or plan.get("timer_binding_sha256")
        != canonical_sha256(timer_binding)
    ):
        raise StrategyLabServiceError(
            "validation_plan_invalid", "frozen validation plan binding changed"
        )
    return dict(plan)


def _validation_sessions(plan: Mapping[str, Any]) -> list[dict[str, Any]]:
    calendar = plan.get("market_calendar")
    sessions = calendar.get("sessions") if isinstance(calendar, Mapping) else None
    if not isinstance(sessions, list) or len(sessions) != 10 or any(
        not isinstance(session, Mapping) for session in sessions
    ):
        raise StrategyLabServiceError(
            "validation_plan_invalid", "frozen validation sessions are unavailable"
        )
    return [dict(session) for session in sessions]


def _expectation_binding_matches(
    plan: Mapping[str, Any],
    session: Mapping[str, Any],
    expectation: Mapping[str, Any],
) -> bool:
    calendar = plan["market_calendar"]
    expected = expectation.get("expectation")
    return bool(
        expectation.get("status") == "available"
        and isinstance(expected, Mapping)
        and (expected.get("market"), expected.get("account"), expected.get("trading_date"))
        == ("HK", ACCOUNT, session.get("trading_date"))
        and expected.get("market_calendar_version") == calendar.get("market_calendar_version")
        and expected.get("market_calendar_sha256") == calendar.get("snapshot_content_sha256")
        and expected.get("schedule_config_sha256")
        == plan["schedule"]["schedule_config_sha256"]
        and expected.get("scheduled_scan_targets_market")
        == session.get("scheduled_scan_targets_utc")
        and expected.get("expected_recommendation_point_ids")
        == session.get("expected_recommendation_point_ids")
    )


def _formal_point_identity_matches(
    session: Mapping[str, Any],
    target: str,
    point_id: str,
    loaded: Mapping[str, Any],
) -> bool:
    point = loaded.get("point")
    source = point.get("source_binding") if isinstance(point, Mapping) else None
    return bool(
        isinstance(point, Mapping)
        and (point.get("market"), point.get("account"), point.get("trading_date"))
        == ("HK", ACCOUNT, session.get("trading_date"))
        and point.get("recommendation_point_id") == point_id
        and point.get("content_sha256") == loaded.get("artifact_content_sha256")
        and isinstance(source, Mapping)
        and (source.get("market"), source.get("account")) == ("HK", ACCOUNT)
        and source.get("scheduled_scan_target_market") == target
    )


def _point_binding_matches(
    plan: Mapping[str, Any],
    session: Mapping[str, Any],
    target: str,
    point_id: str,
    expectation: Mapping[str, Any],
    loaded: Mapping[str, Any],
) -> bool:
    point = loaded.get("point")
    if not isinstance(point, Mapping):
        return False
    recommendation = point.get("recommendation_point")
    return bool(
        _expectation_binding_matches(plan, session, expectation)
        and loaded.get("status") == "available"
        and _formal_point_identity_matches(session, target, point_id, loaded)
        and isinstance(recommendation, Mapping)
        and recommendation.get("recommendation_point_id") == point_id
        and recommendation.get("scheduled_scan_target_market") == target
        and recommendation.get("account_config_sha256") == plan.get("account_run_config_sha256")
    )


def _bind_validation_points(
    context: Mapping[str, Any],
    store: ExperimentStore,
    experiment: Mapping[str, Any],
    plan: Mapping[str, Any],
    occurred_at_utc: str,
) -> None:
    indexed = {
        item["observation_key"]: item
        for item in store.list_observations(experiment["experiment_id"])
    }
    now = datetime.fromisoformat(occurred_at_utc.replace("Z", "+00:00"))
    for session in _validation_sessions(plan):
        day = str(session["trading_date"])
        endpoint = datetime.fromisoformat(
            str(session["session_endpoint_utc"]).replace("Z", "+00:00")
        )
        try:
            expectation = load_formal_expectation(
                context["runtime_root"], market="HK", account=ACCOUNT, trading_date=day
            )
        except FormalCorpusError as exc:
            raise StrategyLabServiceError(
                "validation_source_binding_mismatch", str(exc)
            ) from exc
        targets = session["scheduled_scan_targets_utc"]
        point_ids = session["expected_recommendation_point_ids"]
        if expectation.get("status") not in {"available", "missing"}:
            raise StrategyLabServiceError(
                "validation_source_binding_mismatch", "formal expectation conflicts with the frozen plan"
            )
        if expectation.get("status") == "available" and not _expectation_binding_matches(
            plan, session, expectation
        ):
            raise StrategyLabServiceError(
                "validation_source_binding_mismatch",
                "formal expectation changed from the frozen plan",
            )
        for target, point_id in zip(targets, point_ids, strict=True):
            key = f"validation_point:{point_id}"
            existing = indexed.get(key)
            if expectation.get("status") == "missing":
                if existing is not None:
                    if existing["payload"].get("reason_code") != "formal_expectation_missing":
                        raise StrategyLabServiceError(
                            "validation_source_binding_mismatch",
                            "bound formal expectation is no longer available",
                        )
                    continue
                loaded = {"status": "missing", "reason_code": "formal_expectation_missing"}
            else:
                try:
                    loaded = load_formal_point(
                        context["runtime_root"],
                        market="HK",
                        account=ACCOUNT,
                        trading_date=day,
                        recommendation_point_id=point_id,
                    )
                except FormalCorpusError as exc:
                    raise StrategyLabServiceError(
                        "validation_source_binding_mismatch", str(exc)
                    ) from exc
            if existing is not None:
                stored = existing["payload"]
                if existing["status"] == "available":
                    if (
                        not _point_binding_matches(
                            plan,
                            session,
                            str(target),
                            str(point_id),
                            expectation,
                            loaded,
                        )
                        or stored.get("formal_point_ref") != loaded.get("artifact_ref")
                        or stored.get("formal_point_sha256")
                        != loaded.get("artifact_file_sha256")
                        or stored.get("formal_point_content_sha256")
                        != loaded.get("artifact_content_sha256")
                    ):
                        raise StrategyLabServiceError(
                            "validation_source_binding_mismatch",
                            "bound formal point evidence changed",
                        )
                elif stored.get("formal_point_ref") is not None:
                    source_matches = (
                        _point_binding_matches(
                            plan,
                            session,
                            str(target),
                            str(point_id),
                            expectation,
                            loaded,
                        )
                        if stored.get("reason_code") == "validation_point_late"
                        else loaded.get("status") == "not_evaluable"
                        and _formal_point_identity_matches(
                            session, str(target), str(point_id), loaded
                        )
                    )
                    if (
                        not source_matches
                        or stored.get("formal_point_ref") != loaded.get("artifact_ref")
                        or stored.get("formal_point_sha256")
                        != loaded.get("artifact_file_sha256")
                        or stored.get("formal_point_content_sha256")
                        != loaded.get("artifact_content_sha256")
                        or existing.get("artifact_ref") != loaded.get("artifact_ref")
                        or existing.get("artifact_sha256")
                        != loaded.get("artifact_file_sha256")
                    ):
                        raise StrategyLabServiceError(
                            "validation_source_binding_mismatch",
                            "bound non-evaluable formal point evidence changed",
                        )
                continue
            if loaded.get("status") == "missing":
                if now <= endpoint:
                    continue
                store.put_observation(
                    experiment["experiment_id"],
                    observation_key=key,
                    recommendation_point_id=point_id,
                    kind="validation_point",
                    status="not_evaluable",
                    payload={
                        "status": "not_evaluable",
                        "reason_code": str(
                            loaded.get("reason_code") or "formal_point_evidence_missing"
                        ),
                        "trading_day": day,
                        "recommendation_point_id": point_id,
                        "active_slots_utc": [],
                        "arms": [],
                    },
                    created_at_utc=occurred_at_utc,
                )
                continue
            if loaded.get("status") == "not_evaluable":
                if not _formal_point_identity_matches(
                    session, str(target), str(point_id), loaded
                ):
                    raise StrategyLabServiceError(
                        "validation_source_binding_mismatch",
                        "non-evaluable formal point changed from the frozen plan",
                    )
                store.put_observation(
                    experiment["experiment_id"],
                    observation_key=key,
                    recommendation_point_id=point_id,
                    kind="validation_point",
                    status="not_evaluable",
                    payload={
                        "status": "not_evaluable",
                        "reason_code": str(
                            loaded.get("reason_code") or "formal_point_not_evaluable"
                        ),
                        "trading_day": day,
                        "recommendation_point_id": point_id,
                        "active_slots_utc": [],
                        "arms": [],
                        "formal_point_ref": loaded["artifact_ref"],
                        "formal_point_sha256": loaded["artifact_file_sha256"],
                        "formal_point_content_sha256": loaded[
                            "artifact_content_sha256"
                        ],
                    },
                    artifact_ref=loaded["artifact_ref"],
                    artifact_sha256=loaded["artifact_file_sha256"],
                    created_at_utc=occurred_at_utc,
                )
                continue
            if not _point_binding_matches(
                plan, session, str(target), str(point_id), expectation, loaded
            ):
                raise StrategyLabServiceError(
                    "validation_source_binding_mismatch", "formal point changed from the frozen plan"
                )
            try:
                payload = build_validation_point_evidence(
                    loaded["point"],
                    loaded,
                    plan["leader"],
                    session,
                )
            except (StrategyLabEvidenceError, StrategyLabRecipeError) as exc:
                raise StrategyLabServiceError(
                    str(getattr(exc, "reason_code", "validation_source_binding_mismatch")),
                    str(exc),
                ) from exc
            if not payload["active_slots_utc"]:
                payload = {
                    "status": "not_evaluable",
                    "reason_code": "validation_point_late",
                    "trading_day": day,
                    "recommendation_point_id": point_id,
                    "active_slots_utc": [],
                    "arms": [],
                    "formal_point_ref": loaded["artifact_ref"],
                    "formal_point_sha256": loaded["artifact_file_sha256"],
                    "formal_point_content_sha256": loaded[
                        "artifact_content_sha256"
                    ],
                }
                store.put_observation(
                    experiment["experiment_id"],
                    observation_key=key,
                    recommendation_point_id=point_id,
                    kind="validation_point",
                    status="not_evaluable",
                    payload=payload,
                    artifact_ref=loaded["artifact_ref"],
                    artifact_sha256=loaded["artifact_file_sha256"],
                    created_at_utc=occurred_at_utc,
                )
            else:
                payload["validation_plan_sha256"] = experiment["validation_plan_sha256"]
                payload["evaluator_behavior_sha256"] = experiment[
                    "evaluator_behavior_sha256"
                ]
                store.put_observation(
                    experiment["experiment_id"],
                    observation_key=key,
                    recommendation_point_id=point_id,
                    kind="validation_point",
                    status="available",
                    payload=payload,
                    artifact_ref=loaded["artifact_ref"],
                    artifact_sha256=loaded["artifact_file_sha256"],
                    created_at_utc=occurred_at_utc,
                )


def _validation_observation_index(
    observations: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]], dict[tuple[str, str], str]]:
    by_key = {item["observation_key"]: item for item in observations}
    points = [
        dict(item["payload"])
        for item in observations
        if item["kind"] == "validation_point" and item["status"] == "available"
    ]
    crossed: dict[tuple[str, str], str] = {}
    for item in observations:
        if item["kind"] != "hidden_quote" or item["status"] != "observed_fill":
            continue
        identity = (str(item["recommendation_point_id"]), str(item["arm_id"]))
        slot = str(item["observation_slot_utc"])
        crossed[identity] = min(slot, crossed.get(identity, slot))
    return by_key, points, crossed


def _available_validation_points(
    points: list[dict[str, Any]],
    crossed_at: Mapping[tuple[str, str], str],
    *,
    before_slot_utc: str,
) -> list[dict[str, Any]]:
    crossed = {
        (point["recommendation_point_id"], arm["arm_id"])
        for point in points
        for arm in point.get("arms", [])
        if crossed_at.get(
            (str(point["recommendation_point_id"]), str(arm["arm_id"])),
            before_slot_utc,
        )
        < before_slot_utc
    }
    available: list[dict[str, Any]] = []
    for item in points:
        payload = dict(item)
        payload["arms"] = [
            arm
            for arm in payload.get("arms", [])
            if (payload["recommendation_point_id"], arm["arm_id"])
            not in crossed
        ]
        if payload["arms"]:
            available.append(payload)
    return available


def _current_validation_slot(
    plan: Mapping[str, Any], occurred_at_utc: str
) -> tuple[str, str] | None:
    occurred = datetime.fromisoformat(occurred_at_utc.replace("Z", "+00:00"))
    slot = occurred.replace(second=0, microsecond=0)
    tolerance = int(plan["validation_wake_tolerance_seconds"])
    if not slot <= occurred <= slot + timedelta(seconds=tolerance):
        return None
    slot_text = slot.isoformat().replace("+00:00", "Z")
    for session in _validation_sessions(plan):
        if slot_text in session["minute_grid_utc"]:
            return str(session["trading_date"]), slot_text
    return None


def _batch_manifest_for(
    experiment: Mapping[str, Any],
    plan: Mapping[str, Any],
    observation_index: tuple[
        dict[str, dict[str, Any]], list[dict[str, Any]], dict[tuple[str, str], str]
    ],
    day: str,
    slot: str,
) -> dict[str, Any] | None:
    key = f"hidden_batch:{day}:{slot}"
    by_key, points, crossed_at = observation_index
    existing = by_key.get(key)
    if existing is not None:
        manifest = dict(existing["payload"])
        if (
            manifest.get("trading_day") != day
            or manifest.get("observation_slot_utc") != slot
            or manifest.get("validation_plan_sha256")
            != experiment["validation_plan_sha256"]
        ):
            raise StrategyLabServiceError(
                "validation_batch_manifest_conflict", "hidden batch manifest changed"
            )
        return manifest
    points = _available_validation_points(
        points, crossed_at, before_slot_utc=slot
    )
    active = [point for point in points if slot in point.get("active_slots_utc", [])]
    if not active:
        return None
    manifest = build_hidden_batch_manifest(
        plan, active, trading_day=day, observation_slot_utc=slot
    )
    if manifest["validation_plan_sha256"] != experiment["validation_plan_sha256"]:
        raise StrategyLabServiceError(
            "validation_plan_invalid", "validation plan hash changed"
        )
    return manifest


def _validation_experiment_lock_path(
    context: Mapping[str, Any], experiment_id: str
) -> Path:
    digest = hashlib.sha256(experiment_id.encode()).hexdigest()
    return Path(context["artifact_root"]) / ".locks" / "validation" / f"{digest}.lock"


def _validation_batch_lock_path(
    context: Mapping[str, Any], experiment_id: str, day: str, slot: str
) -> Path:
    digest = canonical_sha256(
        {"experiment_id": experiment_id, "trading_day": day, "observation_slot_utc": slot}
    )
    return Path(context["artifact_root"]) / ".locks" / "validation-batches" / f"{digest}.lock"


@contextmanager
def _freeze_current_batch(
    context: Mapping[str, Any],
    store: ExperimentStore,
    experiment: Mapping[str, Any],
    plan: Mapping[str, Any],
    day: str,
    slot: str,
    *,
    start_new: bool,
    occurred_at_utc: str,
) -> Iterator[tuple[dict[str, Any] | None, dict[str, Any] | None]]:
    """Acquire experiment then stable batch lock, release experiment before I/O."""

    experiment_lock = exclusive_private_file_lock(
        _validation_experiment_lock_path(context, str(experiment["experiment_id"])),
    )
    experiment_lock.__enter__()
    batch_lock = exclusive_private_file_lock(
        _validation_batch_lock_path(
            context, str(experiment["experiment_id"]), day, slot
        ),
        blocking=False,
    )
    batch_entered = False
    try:
        batch_lock.__enter__()
        batch_entered = True
        observations = store.list_observations(str(experiment["experiment_id"]))
        manifest = _batch_manifest_for(
            experiment,
            plan,
            _validation_observation_index(observations),
            day,
            slot,
        )
        key = f"hidden_batch:{day}:{slot}"
        batch = next(
            (item for item in observations if item["observation_key"] == key), None
        )
        if manifest is not None and batch is None and start_new:
            batch = store.start_observation(
                str(experiment["experiment_id"]),
                observation_key=key,
                manifest=manifest,
                created_at_utc=occurred_at_utc,
            )
    except BaseException:
        if batch_entered:
            batch_lock.__exit__(*sys.exc_info())
        experiment_lock.__exit__(*sys.exc_info())
        raise
    experiment_lock.__exit__(None, None, None)
    if not start_new:
        batch_lock.__exit__(None, None, None)
        batch_entered = False
    try:
        yield manifest, batch
    finally:
        if batch_entered:
            batch_lock.__exit__(None, None, None)


def _matching_batch_artifact(
    context: Mapping[str, Any],
    manifest: Mapping[str, Any],
    source_commits: set[str],
) -> tuple[str, dict[str, Any] | None, dict[str, str]]:
    query_sha = canonical_sha256(manifest)
    location = evidence_artifact_location(context["artifact_root"], "hidden_batch", query_sha)
    artifact = read_evidence_artifact(context["artifact_root"], "hidden_batch", query_sha)
    if artifact is not None and (
        artifact["artifact"].get("query") != manifest
        or artifact["artifact"].get("producer_source_commit_sha") not in source_commits
    ):
        raise StrategyLabServiceError(
            "validation_source_binding_mismatch", "hidden batch artifact binding changed"
        )
    return query_sha, artifact, location


def _complete_batch_from_artifact(
    store: ExperimentStore,
    experiment: Mapping[str, Any],
    manifest: Mapping[str, Any],
    artifact: Mapping[str, Any],
    occurred_at_utc: str,
    *,
    quotes: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    key = f"hidden_batch:{manifest['trading_day']}:{manifest['observation_slot_utc']}"
    return store.complete_observation(
        experiment["experiment_id"],
        observation_key=key,
        manifest=manifest,
        quotes=quotes if quotes is not None else hidden_quote_rows(manifest, artifact),
        artifact_ref=artifact["artifact_ref"],
        artifact_sha256=artifact["artifact_sha256"],
        updated_at_utc=occurred_at_utc,
    )


def _tick_guard(context: Mapping[str, Any], occurred_at_utc: str) -> str | None:
    if tick_cron_is_busy(context["tick_lock_path"]):
        return "tick_busy"
    occurred = datetime.fromisoformat(occurred_at_utc.replace("Z", "+00:00"))
    target = next_systemd_tick_target_utc(
        "hk", occurred - timedelta(seconds=TICK_PROTECTION_SECONDS)
    )
    if abs((target - occurred).total_seconds()) <= TICK_PROTECTION_SECONDS:
        return "tick_protection_window"
    return None


def _current_validation_config(
    context: Mapping[str, Any], plan: Mapping[str, Any]
) -> OpenDEndpointRateLimit:
    if plan["provider_source"].get("opend_binding") != context.get("opend_binding"):
        raise StrategyLabServiceError(
            "validation_source_binding_mismatch", "frozen OpenD binding changed"
        )
    config_path, config = load_runtime_config(
        config_path=context["config_hk"], expected_market="hk"
    )
    account_config = build_account_runtime_config(
        base_cfg=config,
        cfg_path=config_path,
        account=ACCOUNT,
        markets_to_run=["HK"],
    )
    account_sha = hashlib.sha256(
        canonical_account_run_config_bytes(account_config)
    ).hexdigest()
    if (
        account_sha != plan["account_run_config_sha256"]
        or canonical_sha256(_hk_schedule(config))
        != plan["schedule"]["schedule_config_sha256"]
    ):
        raise StrategyLabServiceError(
            "validation_source_binding_mismatch", "current HK config changed"
        )
    limit = resolve_opend_fetch_limits(config).market_snapshot
    return OpenDEndpointRateLimit(
        window_sec=limit.window_sec, max_calls=limit.max_calls, max_wait_sec=0.0
    )


def _advance_current_batch(
    context: Mapping[str, Any],
    store: ExperimentStore,
    experiment: Mapping[str, Any],
    plan: Mapping[str, Any],
    manifest: Mapping[str, Any],
    source_commit: str,
    source_commits: set[str],
    snapshot_limit: OpenDEndpointRateLimit,
    *,
    provider_capable: bool,
    occurred_at_utc: str,
) -> dict[str, Any]:
    day = str(manifest["trading_day"])
    slot = str(manifest["observation_slot_utc"])
    key = f"hidden_batch:{day}:{slot}"
    try:
        with _freeze_current_batch(
            context,
            store,
            experiment,
            plan,
            day,
            slot,
            start_new=False,
            occurred_at_utc=occurred_at_utc,
        ) as (fresh_manifest, batch):
            if fresh_manifest is None:
                return {"status": "progress", "reason_code": "validation_slot_inactive"}
            manifest = fresh_manifest
            _query_sha, artifact, _location = _matching_batch_artifact(
                context, manifest, source_commits
            )
            if batch is not None:
                if batch["payload"] != manifest:
                    raise StrategyLabServiceError(
                        "validation_batch_manifest_conflict", "hidden batch manifest changed"
                    )
                if batch["status"] == "complete":
                    return {"status": "progress", "reason_code": "validation_batch_complete"}
                if batch["status"] == "gap":
                    if artifact is not None:
                        raise StrategyLabServiceError(
                            "validation_batch_manifest_conflict", "gap has a matching artifact"
                        )
                    return {"status": "progress", "reason_code": "validation_slot_late"}
                if artifact is not None:
                    _complete_batch_from_artifact(
                        store, experiment, manifest, artifact, occurred_at_utc
                    )
                    return {"status": "progress", "reason_code": "validation_batch_recovered"}
                deadline = datetime.fromisoformat(
                    str(manifest["deadline_utc"]).replace("Z", "+00:00")
                )
                occurred = datetime.fromisoformat(occurred_at_utc.replace("Z", "+00:00"))
                if occurred > deadline:
                    store.expire_started_observation(
                        experiment["experiment_id"],
                        observation_key=key,
                        manifest=manifest,
                        updated_at_utc=occurred_at_utc,
                    )
                    return {"status": "progress", "reason_code": "validation_slot_late"}
                return {"status": "progress", "reason_code": "validation_batch_started"}
            if artifact is not None:
                raise StrategyLabServiceError(
                    "validation_batch_manifest_conflict", "hidden artifact has no started batch"
                )
            if not provider_capable:
                return {
                    "status": "blocked",
                    "reason_code": "advance_external_timeout_required",
                }
            blocker = _tick_guard(context, occurred_at_utc)
            if blocker is not None:
                return {"status": "blocked", "reason_code": blocker}
            reserve = snapshot_limit.max_calls - min(
                HIDDEN_SNAPSHOT_LOW_PRIORITY_CALLS_PER_WINDOW,
                max(0, snapshot_limit.max_calls - 1),
            )

            def collect() -> dict[str, Any]:
                with _freeze_current_batch(
                    context,
                    store,
                    experiment,
                    plan,
                    day,
                    slot,
                    start_new=True,
                    occurred_at_utc=occurred_at_utc,
                ) as (frozen_manifest, frozen_batch):
                    if frozen_manifest is None or frozen_batch is None:
                        raise StrategyLabServiceError(
                            "validation_batch_manifest_conflict",
                            "validation slot is no longer active",
                        )
                    if frozen_batch["status"] != "started":
                        return frozen_batch
                    query_sha = canonical_sha256(frozen_manifest)
                    binding = context["opend_binding"]
                    gateway = build_futu_gateway(
                        host=str(binding["host"]),
                        port=int(binding["port"]),
                        is_option_chain_cache_enabled=False,
                    )
                    try:
                        result = fetch_option_snapshots(
                            option_codes=list(frozen_manifest["option_codes"]),
                            gateway=gateway,
                            snapshot_limit=snapshot_limit,
                            base_dir=Path(context["opend_limiter_root"]),
                            snapshot_batch_size=int(
                                plan["hidden_snapshot_batch_ceiling"]
                            ),
                            snapshot_fallback_max_codes=0,
                            no_retry=True,
                            rate_limited_call=lambda **kwargs: kwargs["call"](),
                        )
                    finally:
                        gateway.close()
                    payload = normalize_hidden_snapshot(frozen_manifest, result)
                    published = publish_evidence_artifact(
                        context["artifact_root"],
                        "hidden_batch",
                        query_sha,
                        payload,
                        query=frozen_manifest,
                        observed_at_utc=occurred_at_utc,
                        producer_source_commit_sha=source_commit,
                        lock_held=True,
                    )
                    _complete_batch_from_artifact(
                        store,
                        experiment,
                        frozen_manifest,
                        published,
                        occurred_at_utc,
                    )
                    return published

            try:
                try_low_priority_opend_call(
                    base_dir=Path(context["opend_limiter_root"]),
                    endpoint="market_snapshot",
                    window_sec=snapshot_limit.window_sec,
                    max_calls=snapshot_limit.max_calls,
                    production_reserve_calls=reserve,
                    call=collect,
                )
            except LowPriorityOpenDCallDeferred:
                return {"status": "blocked", "reason_code": "opend_low_priority_deferred"}
            except FutuGatewayError:
                return {"status": "blocked", "reason_code": "research_provider_failed"}
            except StrategyLabEvidenceError as exc:
                return {"status": "blocked", "reason_code": exc.reason_code}
            return {"status": "progress", "provider_logical_units": 1}
    except BlockingIOError:
        return {"status": "progress", "reason_code": "validation_evidence_busy"}


def _recover_one_elapsed_day(
    context: Mapping[str, Any],
    store: ExperimentStore,
    experiment: Mapping[str, Any],
    plan: Mapping[str, Any],
    source_commits: set[str],
    occurred_at_utc: str,
) -> str | None:
    occurred = datetime.fromisoformat(occurred_at_utc.replace("Z", "+00:00"))
    with exclusive_private_file_lock(
        _validation_experiment_lock_path(context, str(experiment["experiment_id"]))
    ):
        observations = store.list_observations(experiment["experiment_id"])
        observation_index = _validation_observation_index(observations)
        indexed, _points, crossed_at = observation_index
        for session in _validation_sessions(plan):
            day = str(session["trading_date"])
            changed = False
            unsettled = False
            for slot in session["minute_grid_utc"]:
                manifest = _batch_manifest_for(
                    experiment,
                    plan,
                    observation_index,
                    day,
                    slot,
                )
                if manifest is None:
                    continue
                deadline = datetime.fromisoformat(
                    str(manifest["deadline_utc"]).replace("Z", "+00:00")
                )
                if occurred <= deadline:
                    continue
                key = f"hidden_batch:{day}:{manifest['observation_slot_utc']}"
                batch = indexed.get(key)
                if batch is not None and batch["status"] in {"complete", "gap"}:
                    continue
                try:
                    with exclusive_private_file_lock(
                        _validation_batch_lock_path(
                            context,
                            str(experiment["experiment_id"]),
                            day,
                            str(slot),
                        ),
                        blocking=False,
                    ):
                        batch = store.get_observation(experiment["experiment_id"], key)
                        if batch is not None and batch["payload"] != manifest:
                            raise StrategyLabServiceError(
                                "validation_batch_manifest_conflict",
                                "hidden batch manifest changed",
                            )
                        _query_sha, artifact, _location = _matching_batch_artifact(
                            context, manifest, source_commits
                        )
                        if batch is not None and batch["status"] in {"complete", "gap"}:
                            if batch["status"] == "gap" and artifact is not None:
                                raise StrategyLabServiceError(
                                    "validation_batch_manifest_conflict",
                                    "gap has a matching artifact",
                                )
                            continue
                        unsettled = True
                        if artifact is not None and batch is not None:
                            quotes = hidden_quote_rows(manifest, artifact)
                            committed = _complete_batch_from_artifact(
                                store,
                                experiment,
                                manifest,
                                artifact,
                                occurred_at_utc,
                                quotes=quotes,
                            )
                            indexed[key] = committed
                            for quote in quotes:
                                point_id = str(quote["recommendation_point_id"])
                                arm_id = str(quote["arm_id"])
                                quote_slot = str(manifest["observation_slot_utc"])
                                quote_key = (
                                    f"hidden_quote:{point_id}:{arm_id}:{quote_slot}"
                                )
                                indexed[quote_key] = {
                                    "experiment_id": experiment["experiment_id"],
                                    "observation_key": quote_key,
                                    "recommendation_point_id": point_id,
                                    "arm_id": arm_id,
                                    "observation_slot_utc": quote_slot,
                                    "kind": "hidden_quote",
                                    "status": quote["status"],
                                    "payload": quote["payload"],
                                    "artifact_ref": artifact["artifact_ref"],
                                    "artifact_sha256": artifact["artifact_sha256"],
                                    "created_at_utc": occurred_at_utc,
                                    "updated_at_utc": occurred_at_utc,
                                }
                                if quote["status"] == "observed_fill":
                                    identity = (point_id, arm_id)
                                    crossed_at[identity] = min(
                                        quote_slot,
                                        crossed_at.get(identity, quote_slot),
                                    )
                        elif batch is not None:
                            indexed[key] = store.expire_started_observation(
                                experiment["experiment_id"],
                                observation_key=key,
                                manifest=manifest,
                                updated_at_utc=occurred_at_utc,
                            )
                        elif artifact is not None:
                            raise StrategyLabServiceError(
                                "validation_batch_manifest_conflict",
                                "hidden artifact has no batch",
                            )
                        else:
                            indexed[key] = store.materialize_elapsed_observation_gap(
                                experiment["experiment_id"],
                                observation_key=key,
                                manifest=manifest,
                                updated_at_utc=occurred_at_utc,
                            )
                        changed = True
                except BlockingIOError:
                    unsettled = True
            if unsettled:
                return day if changed else None
    return None


def _derive_validation_fills(
    context: Mapping[str, Any],
    store: ExperimentStore,
    experiment: Mapping[str, Any],
    source_commit: str,
    source_commits: set[str],
    occurred_at_utc: str,
) -> int:
    observations = store.list_observations(experiment["experiment_id"])
    fills = {
        (item["recommendation_point_id"], item["arm_id"])
        for item in observations
        if item["kind"] == "validation_fill"
    }
    now = datetime.fromisoformat(occurred_at_utc.replace("Z", "+00:00"))
    created = 0
    for point_observation in observations:
        if point_observation["kind"] != "validation_point" or point_observation["status"] != "available":
            continue
        point = point_observation["payload"]
        endpoint = datetime.fromisoformat(
            str(point["session_endpoint_utc"]).replace("Z", "+00:00")
        )
        point_quotes = [
            item
            for item in observations
            if item["kind"] == "hidden_quote"
            and item["recommendation_point_id"] == point["recommendation_point_id"]
        ]
        for arm in point["arms"]:
            identity = (point["recommendation_point_id"], arm["arm_id"])
            if identity in fills:
                continue
            has_crossing = any(
                item["arm_id"] == arm["arm_id"] and item["status"] == "observed_fill"
                for item in point_quotes
            )
            if not has_crossing and now <= endpoint:
                continue
            evidence = build_validation_fill_evidence(
                point, arm["arm_id"], point_quotes
            )
            if evidence is None:
                continue
            published = read_evidence_artifact(
                context["artifact_root"], "validation_fill", evidence["query_sha256"]
            )
            if published is not None and (
                published["artifact"].get("query") != evidence["query"]
                or published["artifact"].get("payload") != evidence["payload"]
                or published["artifact"].get("producer_source_commit_sha")
                not in source_commits
            ):
                raise StrategyLabServiceError(
                    "validation_source_binding_mismatch",
                    "validation fill artifact binding changed",
                )
            if published is None:
                try:
                    published = publish_evidence_artifact(
                        context["artifact_root"],
                        "validation_fill",
                        evidence["query_sha256"],
                        evidence["payload"],
                        query=evidence["query"],
                        observed_at_utc=evidence["observed_at_utc"],
                        producer_source_commit_sha=source_commit,
                    )
                except BlockingIOError:
                    continue
            payload = {
                **evidence["payload"],
                "fill_evidence_ref": {
                    "artifact_ref": published["artifact_ref"],
                    "artifact_sha256": published["artifact_sha256"],
                },
            }
            store.put_observation(
                experiment["experiment_id"],
                observation_key=f"validation_fill:{identity[0]}:{identity[1]}",
                recommendation_point_id=identity[0],
                arm_id=identity[1],
                kind="validation_fill",
                status=evidence["payload"]["status"],
                payload=payload,
                artifact_ref=published["artifact_ref"],
                artifact_sha256=published["artifact_sha256"],
                created_at_utc=occurred_at_utc,
            )
            created += 1
    return created


def _validation_is_materialized(
    plan: Mapping[str, Any], observations: list[dict[str, Any]]
) -> bool:
    expected_points = sum(
        len(session["expected_recommendation_point_ids"])
        for session in _validation_sessions(plan)
    )
    points = [item for item in observations if item["kind"] == "validation_point"]
    if len(points) != expected_points:
        return False
    fills = {
        (item["recommendation_point_id"], item["arm_id"])
        for item in observations
        if item["kind"] == "validation_fill"
    }
    return all(
        point["status"] == "not_evaluable"
        or all(
            (point["recommendation_point_id"], arm["arm_id"]) in fills
            for arm in point["payload"].get("arms", [])
        )
        for point in points
    )


def advance_experiment(
    context: Mapping[str, Any],
    experiment_id: str,
    *,
    occurred_at_utc: str,
    provider_capable: bool = False,
) -> dict[str, Any]:
    """Advance hidden validation with at most one low-priority snapshot call."""

    occurred = _occurred_at(occurred_at_utc)
    try:
        store = _store(context)
        item = _experiment(store, experiment_id)
        if item["state"] == "waiting_outcome":
            return {"status": "complete", "experiment": _experiment_view(item)}
        if item["state"] != "validation_collecting":
            return _blocked(item, "experiment_not_validation_collecting")
        plan = _validation_plan(item)
        _current_behavior(context, item)
        current_source = source_commit_sha(Path(context["repo_root"]))
        if current_source is None:
            return _blocked(item, "source_commit_unavailable")
        snapshot_limit = _current_validation_config(context, plan)
        item = _observe_source_commit(
            store,
            item,
            current_source,
            actor=_ADVANCE_ACTOR,
            occurred_at_utc=occurred,
        )
        source_commits = _observed_source_commits(store, item)
        with exclusive_private_file_lock(
            _validation_experiment_lock_path(context, item["experiment_id"]),
        ):
            _bind_validation_points(context, store, item, plan, occurred)
        observations = store.list_observations(item["experiment_id"])
        observation_index = _validation_observation_index(observations)
        current = _current_validation_slot(plan, occurred)
        if current is not None:
            day, slot = current
            manifest = _batch_manifest_for(
                item,
                plan,
                observation_index,
                day,
                slot,
            )
            if manifest is not None:
                response = _advance_current_batch(
                    context,
                    store,
                    item,
                    plan,
                    manifest,
                    current_source,
                    source_commits,
                    snapshot_limit,
                    provider_capable=provider_capable is True,
                    occurred_at_utc=occurred,
                )
                if response.get("reason_code") != "validation_batch_complete":
                    return {**response, "experiment_id": item["experiment_id"], "state": item["state"]}
        recovered_day = _recover_one_elapsed_day(
            context, store, item, plan, source_commits, occurred
        )
        derived = _derive_validation_fills(
            context, store, item, current_source, source_commits, occurred
        )
        observations = store.list_observations(item["experiment_id"])
        if _validation_is_materialized(plan, observations):
            item = store.complete_validation_collection(
                item["experiment_id"],
                expected_revision=item["revision"],
                actor=_ADVANCE_ACTOR,
                occurred_at_utc=occurred,
            )
            return {"status": "complete", "experiment": _experiment_view(item)}
        return {
            "status": "progress",
            "experiment_id": item["experiment_id"],
            "state": item["state"],
            "recovered_trading_day": recovered_day,
            "derived_fill_count": derived,
        }
    except StrategyLabServiceError:
        raise
    except (AgentToolError, ExperimentStoreError, StrategyLabEvidenceError, OSError, ValueError) as exc:
        raise StrategyLabServiceError(
            str(getattr(exc, "reason_code", "strategy_lab_validation_failed")), str(exc)
        ) from exc


def advance_scheduled(
    context: Mapping[str, Any],
    *,
    occurred_at_utc: str,
    provider_capable: bool = False,
) -> dict[str, Any]:
    """Advance the globally unique active experiment, or no-op successfully."""

    try:
        store = _store(context)
        item = store.get_active_experiment()
    except (ExperimentStoreError, OSError) as exc:
        raise StrategyLabServiceError(
            str(getattr(exc, "reason_code", "experiment_store_incompatible")), str(exc)
        ) from exc
    if item is None or item["state"] not in {"validation_collecting", "waiting_outcome"}:
        return {"status": "noop", "reason_code": "no_advanceable_experiment"}
    return advance_experiment(
        context,
        item["experiment_id"],
        occurred_at_utc=occurred_at_utc,
        provider_capable=provider_capable,
    )


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
            or item["state"]
            not in {
                "awaiting_validation_confirmation",
                "validation_collecting",
                "waiting_outcome",
                "completed",
            }
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
    "advance_experiment",
    "advance_scheduled",
    "confirm_research",
    "confirm_validation",
    "execute_research",
    "get_experiment_status",
    "list_recipes",
    "preview_experiment",
    "preview_validation",
    "read_receipt",
    "resolve_strategy_lab_context",
    "resolve_strategy_lab_runtime_context",
]
