from __future__ import annotations

from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from threading import Lock
from typing import Any, Callable, TypeVar

from domain.domain.engine import decide_account_scan_gate
from domain.domain.multi_tick import decide_should_notify
from domain.domain.expiration_dates import expiration_business_today
from domain.storage.repositories import run_repo, state_repo
from src.application.account_run import (
    AccountRunOutcome,
    AccountRunRequest,
    _resolve_account_scan_decision,
    build_account_runtime_config,
    run_one_account,
)
from src.application.account_config import normalize_account_label
from src.application.config_sections import resolve_watchlist_config
from src.application.multi_tick.misc import AccountResult
from src.application.multi_tick.required_data_prefetch import prefetch_required_data
from src.application.prepared_portfolio_context import (
    PREPARED_PORTFOLIO_CONTEXT_SCHEMA,
    PreparedPortfolioContextError,
    load_prepared_portfolio_context,
    prepare_portfolio_contexts,
)
from src.application.prepared_option_positions_context import (
    PREPARED_OPTION_POSITIONS_MANIFEST_NAME,
    PreparedOptionPositionsBatch,
    prepare_option_positions_contexts,
)
from src.application.required_data_prefetch_planning import (
    build_cross_account_prefetch_config,
    merge_close_advice_requirements_into_prefetch_config,
)
from src.application.close_advice_required_data import (
    CloseAdviceRequiredDataPlanError,
    PLAN_FILE_NAME,
    build_close_advice_required_data_plan,
    publish_close_advice_required_data_plan,
    resolve_bound_close_advice_required_data_plan,
)
from src.application.ledger.api import (
    list_position_lot_snapshots,
    open_position_ledger_from_data_config,
    resolve_position_data_config_path,
)
from src.application.required_data_snapshot import (
    RequiredDataSnapshotError,
    load_required_data_snapshot_manifest,
    seal_required_data_snapshot,
)
from src.application.position_advice_source_receipts import sha256_bytes
from src.application.tick_run_workspace import (
    AccountRunConfigAuthority,
    AccountRunConfigError,
    load_account_run_config,
    publish_account_run_config,
    write_account_run_state_bytes_once_safely,
    write_account_run_state_json_safely,
)


T = TypeVar("T")


def to_positive_int(value: object, default: int) -> int:
    try:
        parsed = int(value)  # pyright: ignore[reportArgumentType]
    except Exception:
        parsed = int(default)
    return max(1, parsed)


def resolve_account_run_max_workers(cfg: Mapping[str, object], account_count: int) -> int:
    if account_count <= 1:
        return 1
    runtime_cfg = cfg.get("runtime")
    runtime = runtime_cfg if isinstance(runtime_cfg, Mapping) else {}
    raw_workers = runtime.get("multi_account_max_workers")
    if raw_workers is None:
        raw_workers = runtime.get("account_max_workers")
    workers = to_positive_int(raw_workers, 1)
    return min(account_count, workers)


def resolve_default_account(default_account: str | None, accounts: list[str]) -> str:
    account_ids = [str(a).strip().lower() for a in (accounts or []) if str(a).strip()]
    if not account_ids:
        raise SystemExit("[CONFIG_ERROR] at least one account is required")
    if default_account is None:
        return account_ids[0]
    resolved = str(default_account).strip().lower()
    if not resolved:
        raise SystemExit("[CONFIG_ERROR] --default-account cannot be empty")
    if resolved not in account_ids:
        raise SystemExit(
            "[CONFIG_ERROR] --default-account must be one of active accounts: "
            + ", ".join(account_ids)
        )
    return resolved


def run_account_outcomes(
    *,
    account_ids: list[str],
    max_workers: int,
    run_account_fn: Callable[[str], T],
) -> list[T]:
    if len(account_ids) <= 1 or max_workers <= 1:
        return [run_account_fn(acct) for acct in account_ids]

    outcomes_by_account: dict[str, T] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_by_account = {
            executor.submit(run_account_fn, acct): acct
            for acct in account_ids
        }
        for future in as_completed(future_by_account):
            acct = future_by_account[future]
            outcomes_by_account[acct] = future.result()

    return [outcomes_by_account[acct] for acct in account_ids]



@dataclass(frozen=True)
class TickAccountExecutionRequest:
    account_ids: list[str]
    account_workers: int
    base: Path
    base_cfg: dict[str, Any]
    cfg_path: Path
    vpy: Path
    markets_to_run: list[str]
    scheduler_ms: int
    scheduler_view: Any
    notify_decision_by_account: dict[str, Any]
    should_run_global: bool
    reason_global: str
    run_id: str
    run_dir: Path
    shared_required: Path
    accounts_root: Path
    prefetch_done: bool
    force_mode: bool
    smoke: bool
    no_send: bool
    scan_decision_by_account: dict[str, dict[str, Any]]
    state_path: Path
    scheduler_schedule_key: str
    runlog: Any
    audit_helper: Any
    repo_root: Path | None = None
    symbols_arg: str | None = None


@dataclass(frozen=True)
class TickAccountExecutionOutcome:
    results: list[Any]
    account_metrics: list[dict[str, Any]]
    ran_any_pipeline: bool
    ran_pipeline_accounts: list[str]
    scheduled_scan_targets_by_account: dict[str, str | None]
    prefetch_done: bool
    prefetch_invocation_count: int = 0
    snapshot_status: str | None = None
    snapshot_manifest_sha256: str | None = None
    prepared_context_metrics: tuple[dict[str, Any], ...] = ()


def _build_close_advice_barrier_plan(
    *,
    request: TickAccountExecutionRequest,
    scanning_configs: dict[str, dict[str, Any]],
    candidate_config: dict[str, Any],
    run_state_dir: Path,
    run_started_at_utc: datetime,
    position_records_by_account: Mapping[
        str, list[dict[str, Any]]
    ] | None = None,
    unavailable_by_account: Mapping[str, str] | None = None,
) -> tuple[dict[str, Any], Path]:
    records_by_account: dict[str, list[dict[str, Any]]] = {
        str(account).strip().lower(): list(records)
        for account, records in (position_records_by_account or {}).items()
    }
    unavailable: dict[str, str] = {
        str(account).strip().lower(): str(reason)
        for account, reason in (unavailable_by_account or {}).items()
    }
    if position_records_by_account is None:
        records_by_path: dict[Path, list[dict[str, Any]]] = {}
        for account in sorted(scanning_configs):
            config = scanning_configs[account]
            close_cfg = (
                config.get("close_advice")
                if isinstance(config.get("close_advice"), Mapping)
                else {}
            )
            if not bool(close_cfg.get("enabled", False)):
                continue
            try:
                data_config_path = resolve_position_data_config_path(
                    base=request.base,
                    cfg=config,
                    config_path=request.cfg_path,
                ).resolve()
                if data_config_path not in records_by_path:
                    _resolved_path, repo = open_position_ledger_from_data_config(
                        base=request.base,
                        data_config=data_config_path,
                    )
                    records_by_path[data_config_path] = list(
                        list_position_lot_snapshots(
                            repo,
                            base=request.base,
                        )
                    )
                records_by_account[account] = records_by_path[data_config_path]
            except Exception as exc:
                unavailable[account] = (
                    f"position_ledger_unavailable:{type(exc).__name__}"
                )
    else:
        for account, config in sorted(scanning_configs.items()):
            close_cfg = (
                config.get("close_advice")
                if isinstance(config.get("close_advice"), Mapping)
                else {}
            )
            if (
                bool(close_cfg.get("enabled", False))
                and account not in records_by_account
                and account not in unavailable
            ):
                unavailable[account] = "prepared_option_context_missing"

    plan = build_close_advice_required_data_plan(
        run_id=request.run_id,
        run_started_at_utc=run_started_at_utc,
        business_date=expiration_business_today(run_started_at_utc),
        account_configs=scanning_configs,
        base_config=request.base_cfg,
        markets_to_run=request.markets_to_run,
        position_records_by_account=records_by_account,
        unavailable_by_account=unavailable,
    )
    merged_config, plan = merge_close_advice_requirements_into_prefetch_config(
        candidate_config=candidate_config,
        requirements_plan=plan,
    )
    plan_path = (Path(run_state_dir) / PLAN_FILE_NAME).resolve()
    publish_close_advice_required_data_plan(
        path=plan_path,
        payload=plan,
    )
    return merged_config, plan_path


def run_tick_account_execution(request: TickAccountExecutionRequest) -> TickAccountExecutionOutcome:
    try:
        account_ids = [normalize_account_label(item) for item in request.account_ids]
    except ValueError as exc:
        raise AccountRunConfigError(
            "ACCOUNT_CONFIG_IDENTITY_INVALID",
            "tick account scope contains an invalid account label",
        ) from exc
    if len(account_ids) != len(set(account_ids)):
        raise AccountRunConfigError(
            "ACCOUNT_CONFIG_IDENTITY_DUPLICATE",
            "tick account scope contains duplicate canonical account labels",
        )
    account_count = len(account_ids)
    shared_event_prefetch_state: dict[str, object] = {}
    shared_event_prefetch_lock = Lock() if account_count > 1 else None
    account_configs: dict[str, dict[str, Any]] = {}
    account_config_authorities: dict[str, AccountRunConfigAuthority] = {}
    account_config_errors: dict[str, AccountRunConfigError] = {}
    for account in account_ids:
        try:
            config = build_account_runtime_config(
                base_cfg=request.base_cfg,
                cfg_path=request.cfg_path,
                account=account,
                markets_to_run=request.markets_to_run,
                symbols_arg=request.symbols_arg,
            )
            authority = publish_account_run_config(
                base=request.base,
                run_id=request.run_id,
                account=account,
                config=config,
            )
            account_configs[account] = load_account_run_config(
                authority=authority,
                base=request.base,
                run_id=request.run_id,
                account=account,
            )
            account_config_authorities[account] = authority
            try:
                request.audit_helper.audit(
                    "write",
                    "publish_run_account_config",
                    run_id=request.run_id,
                    account=account,
                    status="ok",
                    extra={
                        "account_config_sha256": authority.account_config_sha256,
                        "state_path": str(authority.state_path),
                        "compatibility_path": str(authority.compatibility_path),
                    },
                )
            except Exception:
                pass
        except AccountRunConfigError as exc:
            account_config_errors[account] = exc
        except Exception as exc:
            account_config_errors[account] = AccountRunConfigError(
                "ACCOUNT_CONFIG_BUILD_FAILED",
                f"failed to build account runtime config: {type(exc).__name__}",
            )
    scanning_accounts = [
        account
        for account in account_ids
        if account in account_configs
        if _account_pipeline_is_required(
            request=request,
            account=account,
            cfg=account_configs[account],
        )
    ]
    # Freeze every published account generation before any account branch can
    # create its workspace, including accounts whose scan predicate is false.
    for account in list(account_configs):
        try:
            account_configs[account] = load_account_run_config(
                authority=account_config_authorities[account],
                base=request.base,
                run_id=request.run_id,
                account=account,
            )
        except AccountRunConfigError as exc:
            account_config_errors[account] = exc
    scanning_accounts = [
        account
        for account in scanning_accounts
        if account not in account_config_errors
    ]
    scheduled_scan_targets_by_account = _scheduled_targets(request)
    snapshot_manifest_path: Path | None = None
    prepared_manifest_paths: dict[str, Path] = {}
    prepared_manifest_sha256_by_account: dict[str, str] = {}
    prepared_option_manifest_paths: dict[str, Path] = {}
    prepared_option_manifest_sha256_by_account: dict[str, str] = {}
    prepared_option_records_by_account: dict[
        str, list[dict[str, Any]]
    ] = {}
    prepared_option_unavailable_by_account: dict[str, str] = {}
    snapshot_status: str | None = None
    barrier_reason: str | None = None
    prefetch_done = bool(request.prefetch_done)
    prefetch_invocation_count = 0
    snapshot_manifest_sha256: str | None = None
    close_advice_required_data_plan_path: Path | None = None
    prepared_context_metrics: list[dict[str, Any]] = []

    if scanning_accounts and not request.prefetch_done:
        run_started_at_utc = datetime.now(timezone.utc)
        run_state_dir = run_repo.ensure_run_state_dir(request.base, request.run_id)
        scanning_configs = {
            str(account).strip().lower(): account_configs[
                str(account).strip().lower()
            ]
            for account in scanning_accounts
        }
        scanning_config_authorities = {
            str(account).strip().lower(): account_config_authorities[
                str(account).strip().lower()
            ]
            for account in scanning_accounts
        }
        account_state_dirs = {
            account: run_repo.ensure_run_account_state_dir(
                request.base,
                request.run_id,
                account,
            )
            for account in scanning_configs
        }
        runtime = (
            request.base_cfg.get("runtime")
            if isinstance(request.base_cfg.get("runtime"), Mapping)
            else {}
        )
        portfolio_timeout_sec = float(
            runtime.get("portfolio_timeout_sec", 60) or 60
        )
        try:
            prepared = prepare_portfolio_contexts(
                base=request.base,
                repo_root=(request.repo_root or request.base),
                run_id=request.run_id,
                account_config_authorities=scanning_config_authorities,
                account_state_dirs=account_state_dirs,
                shared_state_dir=run_state_dir,
                timeout_sec=portfolio_timeout_sec,
                python_executable=request.vpy,
            )
        except Exception as exc:
            prepared = _publish_unavailable_prepared_contexts(
                request=request,
                accounts=list(scanning_configs),
                account_config_sha256_by_account={
                    account: authority.account_config_sha256
                    for account, authority in scanning_config_authorities.items()
                },
                reason="portfolio_context_preparation_failed",
                error_type=type(exc).__name__,
            )
        prepared_contexts: dict[str, dict[str, Any] | None] = {}
        invalid_prepared_accounts: set[str] = set()
        for account, manifest in prepared.items():
            prepared_context_metrics.append(
                {
                    key: manifest.get(key)
                    for key in (
                        "account",
                        "status",
                        "reason",
                        "deadline_seconds",
                        "preparation_started_at_utc",
                        "deadline_at_utc",
                        "child_finished_at_utc",
                        "promoted_at_utc",
                        "worker_returncode",
                        "account_config_sha256",
                        "error_code",
                    )
                    if manifest.get(key) is not None
                }
            )
            manifest_path = Path(str(manifest["manifest_path"])).resolve()
            try:
                account_configs[account] = load_account_run_config(
                    authority=scanning_config_authorities[account],
                    base=request.base,
                    run_id=request.run_id,
                    account=account,
                )
                scanning_configs[account] = account_configs[account]
            except AccountRunConfigError as exc:
                account_config_errors[account] = exc
                invalid_prepared_accounts.add(account)
                continue
            error_code = str(manifest.get("error_code") or "").strip().upper()
            if error_code.startswith("ACCOUNT_CONFIG_"):
                account_config_errors[account] = AccountRunConfigError(
                    error_code,
                    str(manifest.get("reason") or "prepared account config invalid"),
                )
                invalid_prepared_accounts.add(account)
                continue
            try:
                prepared_contexts[account] = load_prepared_portfolio_context(
                    manifest_path=manifest_path,
                    expected_base=request.base,
                    expected_run_id=request.run_id,
                    expected_account=account,
                    expected_account_config_sha256=(
                        scanning_config_authorities[
                            account
                        ].account_config_sha256
                    ),
                    expected_manifest_sha256=str(
                        manifest.get("manifest_sha256") or ""
                    ),
                    expected_runtime_config=scanning_configs[account],
                )
            except PreparedPortfolioContextError as exc:
                account_config_errors[account] = AccountRunConfigError(
                    "ACCOUNT_CONFIG_PREPARED_CONTEXT_INVALID",
                    str(exc),
                )
                invalid_prepared_accounts.add(account)
                continue
            prepared_manifest_paths[account] = manifest_path
            prepared_manifest_sha256_by_account[account] = str(
                manifest.get("manifest_sha256") or ""
            )

        if invalid_prepared_accounts:
            scanning_accounts = [
                account
                for account in scanning_accounts
                if account not in invalid_prepared_accounts
            ]
            scanning_configs = {
                account: config
                for account, config in scanning_configs.items()
                if account not in invalid_prepared_accounts
            }

        try:
            prepared_options = prepare_option_positions_contexts(
                base=request.base,
                run_id=request.run_id,
                config_path=request.cfg_path,
                account_configs=scanning_configs,
                account_config_authorities={
                    account: scanning_config_authorities[account]
                    for account in scanning_configs
                },
                run_state_dir=run_state_dir,
                log=lambda message: request.runlog.safe_event(
                    "prepared_option_positions_context",
                    "degraded" if str(message).startswith("[WARN]") else "info",
                    message=str(message),
                ),
            )
        except Exception as exc:
            prepared_options = PreparedOptionPositionsBatch(
                manifests={},
                position_records_by_account={},
                unavailable_by_account={
                    account: (
                        "prepared_option_context_failed:"
                        f"{type(exc).__name__}"
                    )
                    for account in scanning_configs
                },
                observed_at_utc=datetime.now(timezone.utc).isoformat(),
                ledger_read_count=0,
                fx_observation_count=0,
            )
        prepared_option_records_by_account = dict(
            prepared_options.position_records_by_account
        )
        prepared_option_unavailable_by_account = dict(
            prepared_options.unavailable_by_account
        )
        invalid_prepared_option_accounts: set[str] = set()
        for account in sorted(scanning_configs):
            manifest = prepared_options.manifests.get(account)
            if (
                not isinstance(manifest, Mapping)
                or str(manifest.get("status") or "").strip().lower()
                != "ready"
            ):
                reason = (
                    str((manifest or {}).get("reason") or "").strip()
                    if isinstance(manifest, Mapping)
                    else ""
                )
                if not reason:
                    reason = prepared_option_unavailable_by_account.get(
                        account,
                        "prepared option context unavailable",
                    )
                account_config_errors[account] = AccountRunConfigError(
                    "ACCOUNT_CONFIG_PREPARED_OPTION_CONTEXT_INVALID",
                    reason,
                )
                invalid_prepared_option_accounts.add(account)
                continue
            manifest_path = Path(str(manifest.get("manifest_path") or ""))
            manifest_sha256 = str(
                manifest.get("manifest_sha256") or ""
            ).strip().lower()
            if (
                not manifest_path.is_file()
                or len(manifest_sha256) != 64
                or any(
                    character not in "0123456789abcdef"
                    for character in manifest_sha256
                )
            ):
                account_config_errors[account] = AccountRunConfigError(
                    "ACCOUNT_CONFIG_PREPARED_OPTION_CONTEXT_INVALID",
                    "prepared option context authority is incomplete",
                )
                invalid_prepared_option_accounts.add(account)
                continue
            prepared_option_manifest_paths[account] = manifest_path.resolve()
            prepared_option_manifest_sha256_by_account[account] = (
                manifest_sha256
            )

        request.runlog.safe_event(
            "prepared_option_positions_context",
            (
                "ok"
                if not invalid_prepared_option_accounts
                else "degraded"
            ),
            data={
                "accounts": sorted(scanning_configs),
                "ready_accounts": sorted(prepared_option_manifest_paths),
                "ledger_read_count": prepared_options.ledger_read_count,
                "fx_observation_count": prepared_options.fx_observation_count,
            },
        )
        if invalid_prepared_option_accounts:
            scanning_accounts = [
                account
                for account in scanning_accounts
                if account not in invalid_prepared_option_accounts
            ]
            scanning_configs = {
                account: config
                for account, config in scanning_configs.items()
                if account not in invalid_prepared_option_accounts
            }

        union_cfg = build_cross_account_prefetch_config(
            base_config=request.base_cfg,
            account_configs=scanning_configs,
            prepared_portfolio_contexts=prepared_contexts,
        )
        try:
            if scanning_configs:
                (
                    union_cfg,
                    close_advice_required_data_plan_path,
                ) = _build_close_advice_barrier_plan(
                    request=request,
                    scanning_configs=scanning_configs,
                    candidate_config=union_cfg,
                    run_state_dir=run_state_dir,
                    run_started_at_utc=run_started_at_utc,
                    position_records_by_account=(
                        prepared_option_records_by_account
                    ),
                    unavailable_by_account=(
                        prepared_option_unavailable_by_account
                    ),
                )
        except Exception as exc:
            request.audit_helper.audit(
                "plan",
                "close_advice_required_data_plan",
                run_id=request.run_id,
                status="error",
                message=str(exc),
                extra={"error_type": type(exc).__name__},
            )
            request.runlog.safe_event(
                "close_advice_required_data_plan",
                "degraded",
                message=str(exc),
                data={"error_type": type(exc).__name__},
            )
        request.runlog.safe_event(
            "fetch_chain_cache",
            "start",
            data={"accounts": sorted(scanning_configs)},
        )
        try:
            if not scanning_configs:
                raise PreparedPortfolioContextError(
                    "no account with valid config authority remains"
                )
            prefetch_invocation_count += 1
            prefetch_summary = prefetch_required_data(
                vpy=request.vpy,
                base=request.base,
                repo_root=(request.repo_root or request.base),
                cfg=union_cfg,
                shared_required=request.shared_required,
                force_refresh=bool(request.force_mode),
                producer_run_id=request.run_id,
                scan_at_utc=run_started_at_utc,
            )
            snapshot_manifest_path = (
                run_state_dir / "required_data_snapshot_manifest.json"
            ).resolve()
            manifest = seal_required_data_snapshot(
                manifest_path=snapshot_manifest_path,
                required_data_root=request.shared_required,
                run_id=request.run_id,
                prefetch_summary=prefetch_summary,
                close_advice_required_data_plan_path=(
                    close_advice_required_data_plan_path
                ),
            )
            manifest_hash = sha256_bytes(snapshot_manifest_path.read_bytes())
            snapshot_manifest_sha256 = manifest_hash
            prefetch_summary = dict(prefetch_summary)
            prefetch_summary.update(
                {
                    "snapshot_manifest_relpath": snapshot_manifest_path.relative_to(
                        request.run_dir
                    ).as_posix(),
                    "snapshot_manifest_sha256": manifest_hash,
                    "snapshot_status": manifest["status"],
                }
            )
            _publish_prefetch_summary_to_accounts(
                request=request,
                accounts=list(scanning_configs),
                payload=prefetch_summary,
            )
            snapshot_status = str(manifest["status"])
            prefetch_done = True
            request.audit_helper.audit(
                "tool_call",
                "required_data_prefetch",
                run_id=request.run_id,
                status=(
                    "ok" if snapshot_status in {"complete", "partial"} else "error"
                ),
                tool_name="required_data_prefetch",
                extra={
                    "snapshot_status": snapshot_status,
                    "manifest_sha256": manifest_hash,
                },
            )
            request.runlog.safe_event(
                "fetch_chain_cache",
                "ok" if snapshot_status in {"complete", "partial"} else "error",
                data={
                    "snapshot_status": snapshot_status,
                    "manifest_sha256": manifest_hash,
                },
            )
            if snapshot_status == "failed":
                barrier_reason = "required_data_snapshot_failed"
        except Exception as exc:
            snapshot_manifest_path = None
            snapshot_status = "unavailable"
            barrier_reason = "required_data_snapshot_manifest_unavailable"
            prefetch_done = False
            request.audit_helper.audit(
                "tool_call",
                "required_data_prefetch",
                run_id=request.run_id,
                status="error",
                tool_name="required_data_prefetch",
                extra={
                    "snapshot_status": snapshot_status,
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                },
            )
            request.runlog.safe_event(
                "fetch_chain_cache",
                "error",
                message=str(exc),
                data={"snapshot_status": snapshot_status},
            )
    elif scanning_accounts and request.prefetch_done:
        candidate = (
            run_repo.get_run_state_dir(request.base, request.run_id)
            / "required_data_snapshot_manifest.json"
        ).resolve()
        try:
            manifest, _root = load_required_data_snapshot_manifest(
                manifest_path=candidate,
                expected_run_id=request.run_id,
                expected_required_data_root=request.shared_required,
            )
            snapshot_manifest_path = candidate
            snapshot_status = str(manifest["status"])
            snapshot_manifest_sha256 = sha256_bytes(candidate.read_bytes())
            try:
                resolved_plan = (
                    resolve_bound_close_advice_required_data_plan(
                        manifest_path=candidate,
                        manifest=manifest,
                        expected_run_id=request.run_id,
                    )
                )
                if resolved_plan is not None:
                    _plan, close_advice_required_data_plan_path = (
                        resolved_plan
                    )
            except CloseAdviceRequiredDataPlanError as exc:
                request.audit_helper.audit(
                    "plan",
                    "close_advice_required_data_plan_recovery",
                    run_id=request.run_id,
                    status="error",
                    message=str(exc),
                    extra={"error_type": type(exc).__name__},
                )
            if snapshot_status == "failed":
                barrier_reason = "required_data_snapshot_failed"
            for account in scanning_accounts:
                account_key = str(account).strip().lower()
                prepared = (
                    run_repo.get_run_account_state_dir(
                        request.base,
                        request.run_id,
                        account_key,
                    )
                    / "prepared_portfolio_context.v1.json"
                ).resolve()
                if prepared.is_file():
                    prepared_manifest_paths[account_key] = prepared
                    prepared_manifest_sha256_by_account[account_key] = sha256_bytes(
                        prepared.read_bytes()
                    )
                prepared_option = (
                    run_repo.get_run_account_state_dir(
                        request.base,
                        request.run_id,
                        account_key,
                    )
                    / PREPARED_OPTION_POSITIONS_MANIFEST_NAME
                ).resolve()
                if prepared_option.is_file():
                    prepared_option_manifest_paths[account_key] = (
                        prepared_option
                    )
                    prepared_option_manifest_sha256_by_account[account_key] = (
                        sha256_bytes(prepared_option.read_bytes())
                    )
                else:
                    account_config_errors[account_key] = AccountRunConfigError(
                        "ACCOUNT_CONFIG_PREPARED_OPTION_CONTEXT_INVALID",
                        "prepared option context manifest is unavailable",
                    )
        except (OSError, RequiredDataSnapshotError):
            barrier_reason = "required_data_snapshot_manifest_unavailable"
            snapshot_status = "unavailable"
            prefetch_done = False

    def _run_account(acct: str) -> AccountRunOutcome:
        acct = str(acct).strip().lower()
        try:
            config_error = account_config_errors.get(acct)
            if config_error is not None:
                raise config_error
            return run_one_account(
                request=AccountRunRequest(
                    acct=acct,
                    base=request.base,
                    repo_root=request.repo_root,
                    account_config_authority=account_config_authorities[acct],
                    vpy=request.vpy,
                    markets_to_run=request.markets_to_run,
                    scheduler_ms=request.scheduler_ms,
                    scheduler_view=request.scheduler_view,
                    notify_decision_by_account=request.notify_decision_by_account,
                    should_run_global=request.should_run_global,
                    reason_global=request.reason_global,
                    run_id=request.run_id,
                    run_dir=request.run_dir,
                    shared_required=request.shared_required,
                    accounts_root=request.accounts_root,
                    prefetch_done=prefetch_done,
                    force_mode=request.force_mode,
                    allow_mutations=(not request.smoke),
                    allow_notifications=(not request.no_send),
                    prefetch_lock=shared_event_prefetch_lock,
                    prefetch_state=shared_event_prefetch_state,
                    scan_decision_by_account=request.scan_decision_by_account,
                    symbols_arg=request.symbols_arg,
                    required_data_snapshot_manifest=(
                        snapshot_manifest_path if acct in scanning_accounts else None
                    ),
                    prepared_portfolio_context_manifest=(
                        prepared_manifest_paths.get(acct)
                    ),
                    prepared_portfolio_context_manifest_sha256=(
                        prepared_manifest_sha256_by_account.get(acct)
                    ),
                    prepared_option_positions_context_manifest=(
                        prepared_option_manifest_paths.get(acct)
                    ),
                    prepared_option_positions_context_manifest_sha256=(
                        prepared_option_manifest_sha256_by_account.get(acct)
                    ),
                    account_config_generation_frozen=True,
                    required_data_snapshot_status=snapshot_status,
                    required_data_snapshot_sha256=snapshot_manifest_sha256,
                    close_advice_required_data_plan=(
                        close_advice_required_data_plan_path
                        if acct in scanning_accounts
                        else None
                    ),
                ),
                runlog=request.runlog,
                audit_fn=request.audit_helper.audit,
                fail_schema_validation=lambda *, stage, exc, run_id=None: request.audit_helper.fail_schema_validation(
                    stage=stage,
                    exc=exc,
                    run_id=run_id,
                ),
            )
        except AccountRunConfigError as exc:
            return _account_config_failure_outcome(
                request=request,
                account=acct,
                error=exc,
                prefetch_done=prefetch_done,
            )
        except Exception as exc:
            reason = f"account_execution_exception:{type(exc).__name__}"
            request.audit_helper.audit(
                "account_run",
                "account_execution_exception",
                run_id=request.run_id,
                account=acct,
                status="error",
                message=str(exc),
                extra={"exception_type": type(exc).__name__, "isolated": True},
            )
            request.runlog.safe_event(
                "account_run",
                "error",
                error_code="ACCOUNT_EXECUTION_EXCEPTION",
                message=str(exc),
                data={"account": acct, "exception_type": type(exc).__name__},
            )
            return AccountRunOutcome(
                result=AccountResult(
                    account=acct,
                    ran_scan=False,
                    should_notify=False,
                    decision_reason=reason,
                    notification_text="",
                ),
                acct_metrics={
                    "account": acct,
                    "scheduler_ms": request.scheduler_ms,
                    "pipeline_ms": None,
                    "ran_scan": False,
                    "ran_pipeline": False,
                    "should_notify": False,
                    "meaningful": False,
                    "reason": reason,
                    "error": str(exc),
                },
                prefetch_done=prefetch_done,
                ran_pipeline=False,
            )

    ran_any_pipeline = False
    ran_pipeline_accounts: list[str] = []
    results: list[Any] = []
    account_metrics: list[dict[str, Any]] = []
    if barrier_reason:
        outcomes = _terminal_barrier_outcomes(
            request=request,
            scanning_accounts={str(item).strip().lower() for item in scanning_accounts},
            barrier_reason=barrier_reason,
            snapshot_status=str(snapshot_status or "unavailable"),
            run_account_fn=_run_account,
        )
    else:
        outcomes = run_account_outcomes(
            account_ids=account_ids,
            max_workers=request.account_workers,
            run_account_fn=_run_account,
        )
    for outcome in outcomes:
        prefetch_done = bool(
            prefetch_done
            or outcome.prefetch_done
        )
        ran_any_pipeline = bool(ran_any_pipeline or outcome.ran_pipeline)
        account = str(outcome.result.account)
        if outcome.ran_pipeline:
            ran_pipeline_accounts.append(account)
        account_metrics.append(outcome.acct_metrics)
        results.append(outcome.result)

    return TickAccountExecutionOutcome(
        results=results,
        account_metrics=account_metrics,
        ran_any_pipeline=ran_any_pipeline,
        ran_pipeline_accounts=ran_pipeline_accounts,
        scheduled_scan_targets_by_account=scheduled_scan_targets_by_account,
        prefetch_done=prefetch_done,
        prefetch_invocation_count=prefetch_invocation_count,
        snapshot_status=snapshot_status,
        snapshot_manifest_sha256=snapshot_manifest_sha256,
        prepared_context_metrics=tuple(prepared_context_metrics),
    )


def _account_config_failure_outcome(
    *,
    request: TickAccountExecutionRequest,
    account: str,
    error: AccountRunConfigError,
    prefetch_done: bool,
) -> AccountRunOutcome:
    reason = error.reason
    metrics = {
        "run_id": request.run_id,
        "account": account,
        "scheduler_ms": request.scheduler_ms,
        "pipeline_ms": None,
        "ran_scan": False,
        "ran_pipeline": False,
        "should_notify": False,
        "meaningful": False,
        "reason": reason,
        "typed_reason": reason,
        "error_code": error.code,
        "error": str(error),
    }
    safe_run_path = True
    try:
        write_account_run_state_json_safely(
            base=request.base,
            run_id=request.run_id,
            account=account,
            name="account_metrics.json",
            payload=metrics,
        )
    except Exception:
        safe_run_path = False
    if safe_run_path:
        try:
            request.audit_helper.audit(
                "account_run",
                "account_config_authority_failure",
                run_id=request.run_id,
                account=account,
                status="error",
                message=str(error),
                extra={"error_code": error.code, "isolated": True},
            )
        except Exception:
            pass
    try:
        request.runlog.safe_event(
            "account_run",
            "error",
            error_code=error.code,
            message=str(error),
            data={"account": account, "typed_reason": reason},
        )
    except Exception:
        pass
    return AccountRunOutcome(
        result=AccountResult(
            account=account,
            ran_scan=False,
            should_notify=False,
            decision_reason=reason,
            notification_text="",
        ),
        acct_metrics=metrics,
        prefetch_done=prefetch_done,
        ran_pipeline=False,
    )


def _account_pipeline_is_required(
    *,
    request: TickAccountExecutionRequest,
    account: str,
    cfg: dict[str, Any],
) -> bool:
    should_run, reason = _resolve_account_scan_decision(
        account=account,
        scan_decision_by_account=request.scan_decision_by_account,
        should_run_global=request.should_run_global,
        reason_global=request.reason_global,
    )
    gate = decide_account_scan_gate(
        should_run=should_run,
        has_symbols=(
            (not request.markets_to_run) or bool(resolve_watchlist_config(cfg))
        ),
        reason=reason,
    )
    return bool(gate.get("run_pipeline"))


def _scheduled_targets(
    request: TickAccountExecutionRequest,
) -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    for raw_account in request.account_ids:
        account = str(raw_account).strip().lower()
        decision = request.scan_decision_by_account.get(account, {})
        scheduler = decision.get("scheduler_decision")
        if decision.get("should_run") is not False and isinstance(scheduler, Mapping):
            target = str(
                scheduler.get("scheduled_scan_target_market") or ""
            ).strip()
            out[account] = target or None
    return out


def _publish_unavailable_prepared_contexts(
    *,
    request: TickAccountExecutionRequest,
    accounts: list[str],
    account_config_sha256_by_account: Mapping[str, str],
    reason: str,
    error_type: str,
) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for account in sorted(accounts):
        payload = {
            "schema_version": PREPARED_PORTFOLIO_CONTEXT_SCHEMA,
            "run_id": request.run_id,
            "account": account,
            "status": "unavailable",
            "reason": reason,
            "error_type": error_type,
            "account_config_sha256": str(
                account_config_sha256_by_account.get(account) or ""
            ).strip().lower(),
        }
        manifest_bytes = (
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
        try:
            manifest_path = write_account_run_state_bytes_once_safely(
                base=request.base,
                run_id=request.run_id,
                account=account,
                name="prepared_portfolio_context.v1.json",
                payload=manifest_bytes,
            )
        except AccountRunConfigError:
            manifest_path = (
                Path(request.base).resolve()
                / "output_runs"
                / request.run_id
                / "accounts"
                / account
                / "state"
                / "prepared_portfolio_context.v1.json"
            )
        promoted = dict(payload)
        promoted["manifest_path"] = str(manifest_path)
        promoted["manifest_sha256"] = sha256_bytes(manifest_bytes)
        out[account] = promoted
    return out


def _publish_prefetch_summary_to_accounts(
    *,
    request: TickAccountExecutionRequest,
    accounts: list[str],
    payload: dict[str, Any],
) -> None:
    for account in sorted(accounts):
        state_repo.write_account_run_state(
            request.base,
            request.run_id,
            account,
            "required_data_prefetch_summary.json",
            payload,
        )


def _terminal_barrier_outcomes(
    *,
    request: TickAccountExecutionRequest,
    scanning_accounts: set[str],
    barrier_reason: str,
    snapshot_status: str,
    run_account_fn: Callable[[str], AccountRunOutcome],
) -> list[AccountRunOutcome]:
    outcomes: list[AccountRunOutcome] = []
    notify_decisions = {
        key: value
        for key, value in (request.notify_decision_by_account or {}).items()
        if value is not None
    }
    for raw_account in request.account_ids:
        account = str(raw_account).strip().lower()
        if account not in scanning_accounts:
            outcomes.append(run_account_fn(account))
            continue
        should_notify = bool(
            decide_should_notify(
                account=account,
                notify_decision_by_account=notify_decisions,
                scheduler_decision=request.scheduler_view,
            )
        )
        metrics = {
            "run_id": request.run_id,
            "account": account,
            "scheduler_ms": request.scheduler_ms,
            "pipeline_ms": None,
            "ran_scan": False,
            "ran_pipeline": False,
            "should_notify": should_notify,
            "meaningful": False,
            "reason": barrier_reason,
            "typed_reason": barrier_reason,
            "snapshot_status": snapshot_status,
        }
        state_repo.write_account_run_state(
            request.base,
            request.run_id,
            account,
            "account_metrics.json",
            metrics,
        )
        outcomes.append(
            AccountRunOutcome(
                result=AccountResult(
                    account=account,
                    ran_scan=False,
                    should_notify=should_notify,
                    decision_reason=barrier_reason,
                    notification_text="",
                ),
                acct_metrics=metrics,
                prefetch_done=(barrier_reason == "required_data_snapshot_failed"),
                ran_pipeline=False,
            )
        )
    return outcomes
