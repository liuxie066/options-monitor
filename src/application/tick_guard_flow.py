from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from domain.domain import classify_failure
from domain.domain.engine import build_opend_unhealthy_execution_plan, resolve_multi_tick_engine_entrypoint
from domain.domain.fetch_source import is_futu_fetch_source
from domain.storage.repositories import state_repo
from src.application.config_loader import resolve_watchlist_config, set_watchlist_config
from src.application.multi_tick.misc import _safe_runlog_data
from src.application.multi_tick.opend_guard import (
    clear_opend_phone_verify_pending,
    is_opend_phone_verify_pending,
    mark_opend_phone_verify_pending,
    send_opend_alert,
    send_opend_recovery_notice,
)
from src.application.multi_tick.project_guard import admit_project_run, apply_project_load_shed
from src.application.multi_tick_watchdog import run_multi_tick_watchdog
from src.application.tick_account_execution import resolve_default_account
from src.infrastructure.external_services import run_opend_watchdog
from src.infrastructure.io_utils import parse_last_json_obj, utc_now


@dataclass(frozen=True)
class TickGuardRequest:
    base: Path
    base_cfg: dict[str, Any]
    accounts: list[str]
    default_account: str
    market_config: str
    no_send: bool
    opend_phone_verify_continue: bool
    vpy: Path
    runlog: Any
    audit_helper: Any
    complete_tick_idempotency_fn: Callable[..., None]
    admit_project_run_fn: Callable[..., dict[str, Any]] = admit_project_run
    apply_project_load_shed_fn: Callable[..., list[str]] = apply_project_load_shed
    clear_opend_phone_verify_pending_fn: Callable[..., None] = clear_opend_phone_verify_pending
    is_opend_phone_verify_pending_fn: Callable[..., bool] = is_opend_phone_verify_pending
    run_opend_watchdog_fn: Callable[..., Any] = run_opend_watchdog
    mark_opend_phone_verify_pending_fn: Callable[..., None] = mark_opend_phone_verify_pending
    send_opend_alert_fn: Callable[..., Any] = send_opend_alert
    send_opend_recovery_notice_fn: Callable[..., Any] = send_opend_recovery_notice


@dataclass(frozen=True)
class TickGuardOutcome:
    should_continue: bool
    return_code: int
    base_cfg: dict[str, Any]
    accounts: list[str]
    default_account: str
    bj_tz: ZoneInfo


def run_tick_guard_flow(request: TickGuardRequest) -> TickGuardOutcome:
    base_cfg = request.base_cfg
    accounts = [str(a).strip() for a in request.accounts if str(a).strip()]
    default_account = request.default_account
    bj_tz = ZoneInfo("Asia/Shanghai")

    def outcome(should_continue: bool, return_code: int = 0) -> TickGuardOutcome:
        return TickGuardOutcome(
            should_continue=should_continue,
            return_code=return_code,
            base_cfg=base_cfg,
            accounts=accounts,
            default_account=default_account,
            bj_tz=bj_tz,
        )

    guard_admission = request.admit_project_run_fn(request.base, base_cfg)
    if not bool(guard_admission.get("allowed")):
        msg = str(guard_admission.get("reason") or "project guard blocked")
        err = str(guard_admission.get("error_code") or "PROJECT_GUARD_BLOCKED")
        request.runlog.safe_event("project_guard", "skip", error_code=err, message=msg)
        request.runlog.safe_event("run_end", "skip", error_code=err, message=msg)
        request.complete_tick_idempotency_fn(status="skipped", message=msg)
        return outcome(False, 0)

    accounts_effective = request.apply_project_load_shed_fn(accounts, guard_admission)
    if accounts_effective != accounts:
        default_after_load_shed = (
            default_account
            if str(default_account or "").strip().lower()
            in {str(a).strip().lower() for a in accounts_effective if str(a).strip()}
            else None
        )
        accounts = accounts_effective
        default_account = resolve_default_account(default_after_load_shed, accounts_effective)
        request.runlog.safe_event(
            "project_guard",
            "degraded",
            message="half-open probe mode load shedding",
            data=_safe_runlog_data(
                {
                    "mode": guard_admission.get("mode"),
                    "accounts_before": request.accounts,
                    "accounts_after": accounts_effective,
                }
            ),
        )

    market_cfg = str(request.market_config or "auto").strip().lower()
    if market_cfg in ("hk", "us"):
        try:
            base_cfg = dict(base_cfg)
            syms = resolve_watchlist_config(base_cfg)
            set_watchlist_config(
                base_cfg,
                [
                    it
                    for it in syms
                    if isinstance(it, dict) and (it.get("broker") == market_cfg.upper())
                ],
            )
        except Exception:
            pass

    schedule_cfg = base_cfg.get("schedule", {}) or {}
    bj_tz = ZoneInfo(schedule_cfg.get("beijing_timezone", "Asia/Shanghai"))

    if request.opend_phone_verify_continue:
        request.clear_opend_phone_verify_pending_fn(request.base)

    if request.is_opend_phone_verify_pending_fn(request.base):
        request.audit_helper.audit("guard", "opend_phone_verify_pending", status="skip")
        request.runlog.safe_event(
            "run_end",
            "skip",
            message="opend phone verify pending; paused until user confirmation",
        )
        request.audit_helper.guard_mark_success()
        request.complete_tick_idempotency_fn(status="skipped", message="opend_phone_verify_pending")
        return outcome(False, 0)

    watchdog_outcome = run_multi_tick_watchdog(
        base=request.base,
        base_cfg=base_cfg,
        accounts=accounts,
        no_send=request.no_send,
        vpy=request.vpy,
        runlog=request.runlog,
        safe_data_fn=_safe_runlog_data,
        utc_now_fn=utc_now,
        audit_fn=request.audit_helper.audit,
        on_guard_failure=request.audit_helper.guard_mark_failure,
        run_opend_watchdog=request.run_opend_watchdog_fn,
        parse_last_json_obj=parse_last_json_obj,
        classify_failure=classify_failure,
        resolve_watchlist_config=resolve_watchlist_config,
        is_futu_fetch_source=is_futu_fetch_source,
        resolve_multi_tick_engine_entrypoint=resolve_multi_tick_engine_entrypoint,
        build_opend_unhealthy_execution_plan=build_opend_unhealthy_execution_plan,
        mark_opend_phone_verify_pending=request.mark_opend_phone_verify_pending_fn,
        send_opend_alert=request.send_opend_alert_fn,
        send_opend_recovery_notice=request.send_opend_recovery_notice_fn,
        state_repo=state_repo,
    )
    if not watchdog_outcome.should_continue:
        return outcome(False, watchdog_outcome.return_code)

    return outcome(True, 0)
