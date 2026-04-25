from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from typing import Any, Callable

from domain.domain import (
    SchemaValidationError,
    SnapshotDTO,
    apply_scan_run_decision,
    normalize_subprocess_adapter_payload,
)
from domain.domain.engine import AccountSchedulerDecisionView
from src.application.scheduled_notification import (
    build_multi_tick_account_scheduler_view,
    build_multi_tick_scheduler_decision,
)


@dataclass(frozen=True)
class MultiTickSchedulerResult:
    markets_to_run: list[str]
    state_path: Path
    scheduler_schedule_key: str
    scheduler_decision: dict[str, Any]
    scheduler_view: Any
    notify_decision_by_account: dict[str, AccountSchedulerDecisionView | None]
    scheduler_ms: int
    should_run_global: bool
    reason_global: str


def resolve_markets_to_run(
    *,
    now_utc: datetime,
    base_cfg: dict[str, Any],
    market_config: str,
    force_mode: bool,
    runlog,
    safe_data_fn: Callable[[dict[str, Any]], dict[str, Any]],
    domain_select_markets_to_run: Callable[..., list[str]],
    domain_markets_for_trading_day_guard: Callable[..., list[str]],
    decide_trading_day_guard: Callable[..., dict[str, Any]],
    reduce_trading_day_guard,
    check_trading_day_for_market: Callable[[str], tuple[bool | None, str]],
    on_skip: Callable[[], Any],
) -> list[str]:
    markets_to_run = domain_select_markets_to_run(now_utc, base_cfg, market_config)
    if force_mode:
        print("force: bypass guard")
        runlog.safe_event(
            "trading_day_guard",
            "skip",
            message="force: bypass guard",
            data=safe_data_fn({"markets_to_run": markets_to_run, "market_config": market_config}),
        )
        return markets_to_run

    guard_markets = domain_markets_for_trading_day_guard(markets_to_run, base_cfg, market_config)
    guard_decision = decide_trading_day_guard(
        markets_to_run=markets_to_run,
        guard_markets=guard_markets,
        check_trading_day_for_market=lambda gm: check_trading_day_for_market(gm),
        reduce_guard_fn=reduce_trading_day_guard,
    )
    guard_results = list(guard_decision.get("guard_results") or [])
    runlog.safe_event(
        "trading_day_guard",
        "check",
        data=safe_data_fn({"results": guard_results, "markets_to_run": markets_to_run, "market_config": market_config}),
    )
    markets_to_run = list(guard_decision.get("markets_to_run") or [])
    if bool(guard_decision.get("should_skip")):
        runlog.safe_event("run_end", "skip", message=str(guard_decision.get("skip_message") or ""))
        on_skip()
        raise SystemExit(0)
    return markets_to_run


def run_scheduler_flow(
    *,
    vpy: Path,
    base: Path,
    cfg_path: Path,
    base_cfg: dict[str, Any],
    state_path: Path,
    scheduler_schedule_key: str,
    accounts: list[str],
    force_mode: bool,
    smoke: bool,
    snapshot_cls: type[SnapshotDTO],
    engine_entrypoint,
    account_view_cls: type[AccountSchedulerDecisionView],
    run_scan_scheduler_cli,
    build_failure_audit_fields,
    audit_fn: Callable[..., Any],
    fail_schema_validation: Callable[..., Any],
) -> MultiTickSchedulerResult:
    t_sch0 = monotonic()
    scheduler_proc = run_scan_scheduler_cli(
        vpy=vpy,
        base=base,
        config=cfg_path,
        state=state_path,
        jsonl=True,
        schedule_key=str(scheduler_schedule_key),
        capture_output=True,
    )
    scheduler_tool_dto = normalize_subprocess_adapter_payload(
        adapter="scheduler",
        tool_name="scan_scheduler_cli",
        returncode=scheduler_proc.returncode,
        stdout=scheduler_proc.stdout,
        stderr=scheduler_proc.stderr,
        message="scan_scheduler_cli completed",
    )
    scheduler_ms = int((monotonic() - t_sch0) * 1000)
    scheduler_extra = {
        "duration_ms": scheduler_ms,
        "returncode": int(scheduler_proc.returncode),
    }
    if not bool(scheduler_tool_dto.get("ok")):
        scheduler_extra.update(
            build_failure_audit_fields(
                failure_kind="io_error",
                failure_stage="scan_scheduler",
                failure_adapter=str(scheduler_tool_dto.get("adapter") or "scheduler"),
            )
        )
    audit_fn(
        "tool_call",
        "scan_scheduler",
        status=("ok" if scheduler_proc.returncode == 0 else "error"),
        tool_name="scan_scheduler_cli",
        extra=scheduler_extra,
    )
    if not bool(scheduler_tool_dto.get("ok")):
        err = f"scheduler error: {(scheduler_proc.stderr or scheduler_proc.stdout).strip()}"
        raise RuntimeError(err)

    try:
        scheduler_decision, scheduler_view = build_multi_tick_scheduler_decision(
            scheduler_stdout=str(scheduler_proc.stdout or ""),
            as_of_utc=datetime.now(timezone.utc).isoformat(),
            snapshot_cls=snapshot_cls,
            engine_entrypoint=engine_entrypoint,
        )
    except Exception as exc:
        stage = "scheduler_decision" if isinstance(exc, SchemaValidationError) else "scheduler_parse"
        fail_schema_validation(stage=stage, exc=exc)

    notify_decision_by_account: dict[str, AccountSchedulerDecisionView | None] = {}
    for acct0 in [str(a).strip() for a in accounts if str(a).strip()]:
        try:
            sch_acct = run_scan_scheduler_cli(
                vpy=vpy,
                base=base,
                config=cfg_path,
                state=state_path,
                jsonl=True,
                schedule_key=str(scheduler_schedule_key),
                account=str(acct0),
                capture_output=True,
            )
            notify_decision_by_account[acct0] = (
                build_multi_tick_account_scheduler_view(
                    account=str(acct0),
                    scheduler_stdout=str(sch_acct.stdout or ""),
                    scheduler_decision=scheduler_decision,
                    as_of_utc=datetime.now(timezone.utc).isoformat(),
                    snapshot_cls=snapshot_cls,
                    engine_entrypoint=engine_entrypoint,
                    account_view_cls=account_view_cls,
                )
                if sch_acct.returncode == 0
                else None
            )
        except SchemaValidationError as exc:
            fail_schema_validation(stage="account_scheduler_decision", exc=exc)
        except Exception:
            notify_decision_by_account[acct0] = None

    should_run_global, reason_global = apply_scan_run_decision(
        should_run_global=bool(scheduler_view.should_run_scan),
        reason_global=str(scheduler_view.reason),
        force_mode=force_mode,
        smoke=smoke,
    )
    return MultiTickSchedulerResult(
        markets_to_run=[],
        state_path=state_path,
        scheduler_schedule_key=scheduler_schedule_key,
        scheduler_decision=scheduler_decision,
        scheduler_view=scheduler_view,
        notify_decision_by_account=notify_decision_by_account,
        scheduler_ms=scheduler_ms,
        should_run_global=should_run_global,
        reason_global=reason_global,
    )
