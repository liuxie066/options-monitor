from __future__ import annotations

from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any, Callable, TypeVar

from domain.storage.repositories import run_repo
from src.application.account_run import AccountRunOutcome, AccountRunRequest, run_one_account
from src.application.scan_scheduler import mark_scheduler_accounts


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


def should_update_account_legacy_output(account_count: int) -> bool:
    return int(account_count) == 1


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


def mark_scanned_accounts(
    *,
    base: Path,
    config: Path,
    state: Path,
    state_dir: Path,
    schedule_key: str,
    accounts: list[str],
) -> None:
    mark_scheduler_accounts(
        config=config,
        state=state,
        state_dir=state_dir,
        schedule_key=str(schedule_key),
        accounts=[str(a).strip() for a in accounts if str(a).strip()],
        mark_scanned=True,
        base_dir=base,
    )


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
    out_link: Path
    legacy_output_tmp_dir: Path
    accounts_root: Path
    prefetch_done: bool
    force_mode: bool
    smoke: bool
    scan_decision_by_account: dict[str, dict[str, Any]]
    state_path: Path
    scheduler_schedule_key: str
    update_legacy_output: bool
    runlog: Any
    audit_helper: Any


@dataclass(frozen=True)
class TickAccountExecutionOutcome:
    results: list[Any]
    account_metrics: list[dict[str, Any]]
    ran_any_pipeline: bool
    ran_pipeline_accounts: list[str]
    prefetch_done: bool


def run_tick_account_execution(request: TickAccountExecutionRequest) -> TickAccountExecutionOutcome:
    account_count = len(request.account_ids)
    shared_prefetch_state: dict[str, object] = {"done": bool(request.prefetch_done)}
    shared_prefetch_lock = Lock() if account_count > 1 else None
    shared_maintenance_lock = Lock() if account_count > 1 else None

    def _run_account(acct: str) -> AccountRunOutcome:
        acct = str(acct).strip()
        return run_one_account(
            request=AccountRunRequest(
                acct=acct,
                base=request.base,
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
                out_link=request.out_link,
                legacy_output_tmp_dir=request.legacy_output_tmp_dir,
                accounts_root=request.accounts_root,
                prefetch_done=request.prefetch_done,
                force_mode=request.force_mode,
                allow_mutations=(not request.smoke),
                update_legacy_output=request.update_legacy_output,
                prefetch_lock=shared_prefetch_lock,
                prefetch_state=shared_prefetch_state,
                maintenance_lock=shared_maintenance_lock,
                scan_decision_by_account=request.scan_decision_by_account,
            ),
            runlog=request.runlog,
            audit_fn=request.audit_helper.audit,
            fail_schema_validation=lambda *, stage, exc, run_id=None: request.audit_helper.fail_schema_validation(
                stage=stage,
                exc=exc,
                run_id=run_id,
            ),
        )

    prefetch_done = bool(request.prefetch_done)
    ran_any_pipeline = False
    ran_pipeline_accounts: list[str] = []
    results: list[Any] = []
    account_metrics: list[dict[str, Any]] = []
    for outcome in run_account_outcomes(
        account_ids=request.account_ids,
        max_workers=request.account_workers,
        run_account_fn=_run_account,
    ):
        prefetch_done = bool(outcome.prefetch_done)
        ran_any_pipeline = bool(ran_any_pipeline or outcome.ran_pipeline)
        if outcome.ran_pipeline:
            ran_pipeline_accounts.append(str(outcome.result.account))
        account_metrics.append(outcome.acct_metrics)
        results.append(outcome.result)

    if ran_any_pipeline:
        try:
            mark_scanned_accounts(
                base=request.base,
                config=request.cfg_path,
                state=request.state_path,
                state_dir=run_repo.get_run_state_dir(request.base, request.run_id),
                schedule_key=str(request.scheduler_schedule_key),
                accounts=ran_pipeline_accounts,
            )
        except Exception:
            pass

    return TickAccountExecutionOutcome(
        results=results,
        account_metrics=account_metrics,
        ran_any_pipeline=ran_any_pipeline,
        ran_pipeline_accounts=ran_pipeline_accounts,
        prefetch_done=prefetch_done,
    )
