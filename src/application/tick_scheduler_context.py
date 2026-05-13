from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from domain.domain.intermediate_objects import SchemaValidationError, SnapshotDTO
from domain.domain.multi_tick import (
    markets_for_trading_day_guard as domain_markets_for_trading_day_guard,
    reduce_trading_day_guard,
    select_markets_to_run as domain_select_markets_to_run,
    select_scheduler_state_filename,
)
from domain.domain.engine import (
    AccountSchedulerDecisionView,
    SchedulerDecisionView,
    build_failure_audit_fields,
    decide_trading_day_guard,
    resolve_multi_tick_engine_entrypoint,
)
from domain.storage import paths as storage_paths
from domain.storage.repositories import state_repo
from src.application.multi_tick.misc import AccountResult, _safe_runlog_data
from src.application.multi_tick_scheduler import resolve_market_run, run_scheduler_flow
from src.infrastructure.external_services import run_scan_scheduler_cli
from src.infrastructure.io_utils import utc_now


@dataclass(frozen=True)
class TickSchedulerRequest:
    vpy: Path
    base: Path
    cfg_path: Path
    base_cfg: dict[str, Any]
    accounts: list[str]
    market_config: str
    force_mode: bool
    smoke: bool
    run_id: str
    runlog: Any
    audit_helper: Any
    check_trading_day_for_market: Callable[[str], tuple[bool | None, str]]
    run_scan_scheduler_cli_fn: Callable[..., Any] = run_scan_scheduler_cli
    account_view_cls: type[AccountSchedulerDecisionView] = AccountSchedulerDecisionView


@dataclass(frozen=True)
class TickSchedulerContext:
    markets_to_run: list[str]
    scheduler_markets: list[str]
    state_path: Path
    scheduler_schedule_key: str
    scheduler_ms: int
    scheduler_decision: dict[str, Any]
    scheduler_view: SchedulerDecisionView
    notify_decision_by_account: dict[str, AccountSchedulerDecisionView | None]
    scan_decision_by_account: dict[str, dict[str, Any]]
    should_run_global: bool
    reason_global: str


@dataclass(frozen=True)
class TickSchedulerOutcome:
    should_continue: bool
    return_code: int
    context: TickSchedulerContext | None
    results: list[AccountResult]


def build_tick_scheduler_context(request: TickSchedulerRequest) -> TickSchedulerOutcome:
    now_utc = datetime.now(timezone.utc)
    market_resolution = resolve_market_run(
        now_utc=now_utc,
        base_cfg=request.base_cfg,
        market_config=request.market_config,
        force_mode=request.force_mode,
        runlog=request.runlog,
        safe_data_fn=_safe_runlog_data,
        domain_select_markets_to_run=domain_select_markets_to_run,
        domain_markets_for_trading_day_guard=domain_markets_for_trading_day_guard,
        decide_trading_day_guard=decide_trading_day_guard,
        reduce_trading_day_guard=reduce_trading_day_guard,
        check_trading_day_for_market=request.check_trading_day_for_market,
    )
    markets_to_run = list(market_resolution.markets_to_run)
    scheduler_markets = list(market_resolution.scheduler_markets)

    state_repo.shared_state_dir(request.base)
    state_path = storage_paths.shared_state_path(
        request.base,
        select_scheduler_state_filename(scheduler_markets),
    )

    _ensure_scheduler_state_file(request.base, state_path)
    scheduler_schedule_key = "schedule"
    if scheduler_markets == ["HK"] and "schedule_hk" in (request.base_cfg or {}):
        scheduler_schedule_key = "schedule_hk"

    if bool(market_resolution.trading_day_blocked):
        reason_global = str(market_resolution.skip_message or "trading_day_guard_skip")
        scheduler_ms = 0
        scheduler_decision = {
            "schema_kind": "scheduler_decision",
            "schema_version": "1.0",
            "should_run_scan": False,
            "is_notify_window_open": False,
            "reason": reason_global,
        }
        scheduler_view = SchedulerDecisionView.from_payload(scheduler_decision)
        notify_decision_by_account: dict[str, AccountSchedulerDecisionView | None] = {}
        scan_decision_by_account: dict[str, dict[str, Any]] = {}
        should_run_global = False
        request.audit_helper.audit(
            "guard",
            "trading_day_blocked",
            status="skip",
            run_id=request.run_id,
            message=reason_global,
            extra={"guard_results": market_resolution.guard_results},
        )
    else:
        try:
            scheduler_result = run_scheduler_flow(
                vpy=request.vpy,
                base=request.base,
                cfg_path=request.cfg_path,
                base_cfg=request.base_cfg,
                state_path=state_path,
                scheduler_schedule_key=scheduler_schedule_key,
                accounts=[str(a).strip() for a in (request.accounts or []) if str(a).strip()],
                force_mode=request.force_mode,
                smoke=request.smoke,
                snapshot_cls=SnapshotDTO,
                engine_entrypoint=resolve_multi_tick_engine_entrypoint,
                account_view_cls=request.account_view_cls,
                run_scan_scheduler_cli=request.run_scan_scheduler_cli_fn,
                build_failure_audit_fields=build_failure_audit_fields,
                audit_fn=request.audit_helper.audit,
                fail_schema_validation=request.audit_helper.fail_schema_validation,
            )
            scheduler_ms = scheduler_result.scheduler_ms
            scheduler_decision = scheduler_result.scheduler_decision
            scheduler_view = scheduler_result.scheduler_view
            notify_decision_by_account = scheduler_result.notify_decision_by_account
            scan_decision_by_account = scheduler_result.scan_decision_by_account
            should_run_global = scheduler_result.should_run_global
            reason_global = scheduler_result.reason_global
        except RuntimeError as exc:
            err = str(exc)
            results = []
            for acct in request.accounts:
                acct0 = str(acct).strip()
                if acct0:
                    results.append(AccountResult(acct0, False, False, err, ""))
            request.runlog.safe_event("run_end", "error", error_code="SCHEDULER_FAILED", message=err)
            request.audit_helper.guard_mark_failure("SCHEDULER_FAILED", "scan_scheduler")
            return TickSchedulerOutcome(False, 0, None, results)

    context = TickSchedulerContext(
        markets_to_run=markets_to_run,
        scheduler_markets=scheduler_markets,
        state_path=state_path,
        scheduler_schedule_key=scheduler_schedule_key,
        scheduler_ms=scheduler_ms,
        scheduler_decision=scheduler_decision,
        scheduler_view=scheduler_view,
        notify_decision_by_account=notify_decision_by_account,
        scan_decision_by_account=scan_decision_by_account,
        should_run_global=should_run_global,
        reason_global=reason_global,
    )
    _write_scheduler_snapshot(request, context)
    return TickSchedulerOutcome(True, 0, context, [])


def _ensure_scheduler_state_file(base: Path, state_path: Path) -> None:
    try:
        if (not state_path.exists()) or state_path.stat().st_size <= 0:
            state_repo.write_shared_state(
                base,
                state_path.name,
                {
                    "last_scan_utc": None,
                    "last_notify_utc": None,
                },
            )
    except Exception:
        pass


def _write_scheduler_snapshot(request: TickSchedulerRequest, context: TickSchedulerContext) -> None:
    try:
        scheduler_snapshot = SnapshotDTO.from_payload(
            {
                "schema_kind": "snapshot_dto",
                "schema_version": "1.0",
                "snapshot_name": "scheduler_decision",
                "as_of_utc": utc_now(),
                "payload": {
                    "schedule_key": str(context.scheduler_schedule_key),
                    "decision": context.scheduler_decision,
                    "state_path": str(context.state_path),
                },
            }
        )
        state_repo.write_scheduler_decision(request.base, request.run_id, scheduler_snapshot.to_payload())
        request.audit_helper.audit("write", "write_scheduler_decision", run_id=request.run_id)
    except SchemaValidationError as exc:
        request.audit_helper.fail_schema_validation(stage="snapshot_dto", exc=exc, run_id=request.run_id)
    except Exception:
        pass
