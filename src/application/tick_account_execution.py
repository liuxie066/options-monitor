from __future__ import annotations

from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any, Callable, TypeVar

from domain.domain.engine import decide_account_scan_gate
from domain.domain.multi_tick import decide_should_notify
from domain.storage.repositories import run_repo, state_repo
from src.application.account_run import (
    AccountRunOutcome,
    AccountRunRequest,
    _resolve_account_scan_decision,
    build_account_runtime_config,
    run_one_account,
)
from src.application.config_sections import resolve_watchlist_config
from src.application.multi_tick.misc import AccountResult
from src.application.multi_tick.required_data_prefetch import prefetch_required_data
from src.application.prepared_portfolio_context import (
    PREPARED_PORTFOLIO_CONTEXT_SCHEMA,
    PreparedPortfolioContextError,
    load_prepared_portfolio_context,
    prepare_portfolio_contexts,
)
from src.application.required_data_prefetch_planning import (
    build_cross_account_prefetch_config,
)
from src.application.required_data_snapshot import (
    RequiredDataSnapshotError,
    load_required_data_snapshot_manifest,
    seal_required_data_snapshot,
)
from src.application.position_advice_source_receipts import sha256_bytes
from src.infrastructure.io_utils import atomic_write_json


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


def run_tick_account_execution(request: TickAccountExecutionRequest) -> TickAccountExecutionOutcome:
    account_count = len(request.account_ids)
    shared_event_prefetch_state: dict[str, object] = {}
    shared_event_prefetch_lock = Lock() if account_count > 1 else None
    account_configs = {
        str(account).strip().lower(): build_account_runtime_config(
            base_cfg=request.base_cfg,
            cfg_path=request.cfg_path,
            account=str(account).strip().lower(),
            markets_to_run=request.markets_to_run,
            symbols_arg=request.symbols_arg,
        )
        for account in request.account_ids
    }
    scanning_accounts = [
        account
        for account in request.account_ids
        if _account_pipeline_is_required(
            request=request,
            account=str(account).strip().lower(),
            cfg=account_configs[str(account).strip().lower()],
        )
    ]
    scheduled_scan_targets_by_account = _scheduled_targets(request)
    snapshot_manifest_path: Path | None = None
    prepared_manifest_paths: dict[str, Path] = {}
    snapshot_status: str | None = None
    barrier_reason: str | None = None
    prefetch_done = bool(request.prefetch_done)
    prefetch_invocation_count = 0
    snapshot_manifest_sha256: str | None = None
    prepared_context_metrics: list[dict[str, Any]] = []

    if scanning_accounts and not request.prefetch_done:
        run_state_dir = run_repo.ensure_run_state_dir(request.base, request.run_id)
        scanning_configs = {
            str(account).strip().lower(): account_configs[
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
                account_configs=scanning_configs,
                account_state_dirs=account_state_dirs,
                shared_state_dir=run_state_dir,
                timeout_sec=portfolio_timeout_sec,
                python_executable=request.vpy,
            )
        except Exception as exc:
            prepared = _publish_unavailable_prepared_contexts(
                request=request,
                accounts=list(scanning_configs),
                reason="portfolio_context_preparation_failed",
                error_type=type(exc).__name__,
            )
        prepared_contexts: dict[str, dict[str, Any] | None] = {}
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
                    )
                    if manifest.get(key) is not None
                }
            )
            manifest_path = Path(str(manifest["manifest_path"])).resolve()
            prepared_manifest_paths[account] = manifest_path
            try:
                prepared_contexts[account] = load_prepared_portfolio_context(
                    manifest_path=manifest_path,
                    expected_run_id=request.run_id,
                    expected_account=account,
                )
            except PreparedPortfolioContextError:
                prepared_contexts[account] = None

        union_cfg = build_cross_account_prefetch_config(
            base_config=request.base_cfg,
            account_configs=scanning_configs,
            prepared_portfolio_contexts=prepared_contexts,
        )
        request.runlog.safe_event(
            "fetch_chain_cache",
            "start",
            data={"accounts": sorted(scanning_configs)},
        )
        try:
            prefetch_invocation_count += 1
            prefetch_summary = prefetch_required_data(
                vpy=request.vpy,
                base=request.base,
                repo_root=(request.repo_root or request.base),
                cfg=union_cfg,
                shared_required=request.shared_required,
                force_refresh=bool(request.force_mode),
                producer_run_id=request.run_id,
            )
            snapshot_manifest_path = (
                run_state_dir / "required_data_snapshot_manifest.json"
            ).resolve()
            manifest = seal_required_data_snapshot(
                manifest_path=snapshot_manifest_path,
                required_data_root=request.shared_required,
                run_id=request.run_id,
                prefetch_summary=prefetch_summary,
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
            if snapshot_status == "failed":
                barrier_reason = "required_data_snapshot_failed"
            for account in scanning_accounts:
                prepared = (
                    run_repo.get_run_account_state_dir(
                        request.base,
                        request.run_id,
                        str(account).strip().lower(),
                    )
                    / "prepared_portfolio_context.v1.json"
                ).resolve()
                if prepared.is_file():
                    prepared_manifest_paths[str(account).strip().lower()] = prepared
        except (OSError, RequiredDataSnapshotError):
            barrier_reason = "required_data_snapshot_manifest_unavailable"
            snapshot_status = "unavailable"
            prefetch_done = False

    def _run_account(acct: str) -> AccountRunOutcome:
        acct = str(acct).strip()
        return run_one_account(
            request=AccountRunRequest(
                acct=acct,
                base=request.base,
                repo_root=request.repo_root,
                base_cfg=request.base_cfg,
                cfg_path=request.cfg_path,
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
                required_data_snapshot_status=snapshot_status,
                required_data_snapshot_sha256=snapshot_manifest_sha256,
            ),
            runlog=request.runlog,
            audit_fn=request.audit_helper.audit,
            fail_schema_validation=lambda *, stage, exc, run_id=None: request.audit_helper.fail_schema_validation(
                stage=stage,
                exc=exc,
                run_id=run_id,
            ),
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
            account_ids=request.account_ids,
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
    reason: str,
    error_type: str,
) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for account in sorted(accounts):
        state_dir = run_repo.ensure_run_account_state_dir(
            request.base,
            request.run_id,
            account,
        )
        manifest_path = (state_dir / "prepared_portfolio_context.v1.json").resolve()
        payload = {
            "schema_version": PREPARED_PORTFOLIO_CONTEXT_SCHEMA,
            "run_id": request.run_id,
            "account": account,
            "status": "unavailable",
            "reason": reason,
            "error_type": error_type,
        }
        atomic_write_json(manifest_path, payload)
        payload["manifest_path"] = str(manifest_path)
        out[account] = payload
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
