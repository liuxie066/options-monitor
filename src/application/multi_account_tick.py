from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from functools import partial
import json
import os
import sys
from pathlib import Path
from time import monotonic
from typing import Any, Callable
from zoneinfo import ZoneInfo

from src.infrastructure.io_utils import (
    read_json,
    utc_now,
)
from src.infrastructure.run_log import RunLogger
from src.application.account_config import build_account_portfolio_source_plan, resolve_configured_accounts
from src.application.config_sections import resolve_watchlist_config
from src.application.symbol_mutations import split_symbol_whitelist
from src.application.config_validator import (
    validate_config,
    validate_retired_symbol_worker_config,
)
from domain.domain.fetch_source import resolve_symbol_fetch_source

from src.application.multi_tick.opend_guard import (
    clear_opend_phone_verify_pending,
    is_opend_phone_verify_pending,
    mark_opend_phone_verify_pending,
    send_opend_alert,
    send_opend_recovery_notice,
)
from src.application.multi_tick.project_guard import (
    admit_project_run,
    apply_project_load_shed,
    record_project_failure,
    record_project_success,
)
from src.application.multi_tick.required_data_prefetch import (
    warm_required_data_chain_cache,
)
from src.application.multi_tick.misc import (
    set_debug,
    AccountResult,
    _safe_runlog_data,
)
from domain.domain.config_contract import (
    ensure_runtime_canonical_config,
    ensure_runtime_schedule_matches_market,
    resolve_config_contract,
)
from domain.domain.engine import (
    AccountSchedulerDecisionView,
    build_failure_audit_fields,
)
from src.application.multi_tick_audit import MultiTickAuditHelper
from src.application.tick_guard_flow import TickGuardRequest, run_tick_guard_flow
from src.application.tick_account_execution import (
    TickAccountExecutionRequest,
    publish_runtime_portfolio_snapshot_shadows,
    resolve_account_run_max_workers as _resolve_account_run_max_workers,
    resolve_default_account as _resolve_default_account,
    run_tick_account_execution,
)
from src.application.tick_notification_flow import (
    TickNotificationRequest,
    TickNotificationOutcome,
    _daily_brief_recovery_intent_for_preparation,
    _prepare_daily_brief_notification,
    has_daily_brief_recovery_to_resume,
    run_tick_notification_flow,
)
from src.application.tick_brief_preparation import TickBriefPreparation, prepare_tick_briefs
from src.application.daily_decision_brief_repository import daily_brief_delivery_validation_scope
from src.application.multi_tick_finalization import (
    _record_finalize_degraded,
    failed_scan_accounts,
    finalize_multi_tick_run,
    finalize_no_account_notification,
    merge_scan_outcomes,
)
from src.application.multi_tick_audit import record_tick_latency
from src.application.tick_run_context import (
    build_tick_idempotency_context,
    tick_account_outcomes,
    write_child_tick_completion,
    complete_tick_idempotency as _complete_tick_idempotency,
)
from src.application.tick_run_workspace import prepare_tick_run_workspace
from src.application.scan_scheduler import mark_scheduler_accounts
from src.application.runtime_trigger_context import build_trigger_context, resolve_tick_deadline
from src.application.runtime_paths import resolve_runtime_root
from src.application.runtime_config_freshness import (
    RuntimeConfigFreshnessError,
    RuntimeConfigIdentityError,
    ensure_runtime_config_freshness,
    ensure_runtime_config_identity,
)
from src.application.tick_scheduler_context import (
    TickSchedulerRequest,
    build_tick_scheduler_context,
)
from src.application.experience_mode import validate_experience_request
from src.application.futu_quote_routing import resolve_futu_quote_route
from src.application.market_data_provider import require_market_data_provider
from src.application.opend_market_snapshot_fetching import trading_calendar_dates
from src.infrastructure.futu_gateway import check_fetch_control
from src.infrastructure.external_services import (
    run_opend_watchdog,
    run_scan_scheduler_cli,
    trading_day_via_futu,
)

from domain.storage.repositories import state_repo


_CURRENT_RUN_ID: str | None = None


@dataclass(frozen=True)
class TickExecutionResult:
    """Invocation-local identity; absent fields were not validated/bound yet."""

    rc: int = 2
    run_created: bool = False
    run_id: str | None = None
    runtime_root: Path | None = None
    config_path: Path | None = None
    accounts: tuple[str, ...] = ()
    market: str | None = None
    experience: bool = False
    experience_completed: bool = False
    scheduler_skipped_accounts: tuple[str, ...] = ()
    error: str | None = None


def current_run_id() -> str | None:
    return _CURRENT_RUN_ID


def _resolve_daily_brief_trigger_kind(*, force_mode: bool, trigger_context: dict[str, Any]) -> str:
    if force_mode:
        return 'force'
    source = str(trigger_context.get('source') or '').strip().lower()
    return 'scheduled' if source in {'cron', 'scheduler'} else 'manual'


def _has_scan_to_run(*, should_run_global: bool, scan_decision_by_account: dict[str, dict[str, Any]]) -> bool:
    if bool(should_run_global):
        return True
    for item in (scan_decision_by_account or {}).values():
        if isinstance(item, dict) and item.get("should_run") is True:
            return True
    return False


def _experience_scan_completed(
    *, requested_accounts: list[str], ran_pipeline_accounts: list[str]
) -> bool:
    requested = {
        str(value).strip().lower()
        for value in requested_accounts
        if str(value).strip()
    }
    completed = {
        str(value).strip().lower()
        for value in ran_pipeline_accounts
        if str(value).strip()
    }
    return requested == completed


def _is_trading_day_guard_for_market(
    cfg: dict[str, Any], market: str, *, deadline: float | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> tuple[bool | None, str]:
    """Return (is_trading_day, market_used) for one market.

    None means guard check failed and caller should continue without blocking.
    """
    check_fetch_control(deadline, cancelled)
    controls = {"deadline": deadline, "cancelled": cancelled} if deadline is not None or cancelled is not None else {}
    market_used = str(market or "").strip().upper() or "US"
    provider = require_market_data_provider(cfg)
    if provider.name != "opend":
        gateway = None
        try:
            route = provider.resolve_quote_route(cfg, market=market_used)
            if not route.ok or route.host is None or route.port is None:
                return None, market_used
            day = datetime.now(ZoneInfo({"US": "America/New_York", "HK": "Asia/Hong_Kong"}.get(
                market_used, "UTC"))).date()
            gateway = provider.open_quote_gateway(host=str(route.host), port=int(route.port))
            response = provider.get_trading_days_with_receipt(
                gateway, market=market_used, start=day.isoformat(), end=day.isoformat(), **controls)
            check_fetch_control(deadline, cancelled)
            return day.isoformat() in trading_calendar_dates(response, start=day, end=day, allow_empty=True), market_used
        except Exception:
            check_fetch_control(deadline, cancelled)
            return None, market_used
        finally:
            if gateway is not None:
                gateway.close()
    route = resolve_futu_quote_route(cfg, market=market_used)
    if not route.ok or route.host is None or route.port is None:
        return None, market_used
    return trading_day_via_futu(
        host=str(route.host),
        port=int(route.port),
        market=market_used,
        **controls,
    )


def _opening_chain_warmup_context(
    *,
    trigger_kind: str,
    scheduler_markets: list[str],
    base_cfg: dict[str, Any],
    scheduler_schedule_key: str,
    account_ids: list[str],
    scheduler_decisions_by_account: dict[str, dict[str, Any]],
) -> dict[str, str] | None:
    if trigger_kind != "scheduled" or scheduler_markets not in (["US"], ["HK"]):
        return None
    schedule = base_cfg.get(scheduler_schedule_key)
    if not isinstance(schedule, dict):
        return None
    try:
        interval = int(schedule.get("cron_interval_min") or 10)
    except (TypeError, ValueError):
        return None
    if interval <= 0:
        return None

    def aware(value: Any) -> datetime | None:
        if not isinstance(value, str) or not value or value != value.strip():
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None else None

    targets: set[datetime] = set()
    now_values: list[datetime] = []
    for account in account_ids:
        decision = scheduler_decisions_by_account.get(str(account).strip().lower())
        if (
            not isinstance(decision, dict)
            or decision.get("in_run_window") is not True
            or decision.get("should_run_scan") is not False
        ):
            return None
        now_beijing = aware(decision.get("now_beijing"))
        run_start = aware(decision.get("run_window_start_beijing"))
        next_target = aware(decision.get("next_run_market"))
        if (
            now_beijing is None
            or run_start is None
            or next_target is None
            or now_beijing < run_start
            or now_beijing >= run_start + timedelta(minutes=interval)
            or next_target <= now_beijing
        ):
            return None
        now_values.append(now_beijing.astimezone(timezone.utc))
        targets.add(next_target.astimezone(timezone.utc))
    if len(targets) != 1:
        return None

    target = next(iter(targets))
    lock_release_by = target - timedelta(seconds=120)
    worker_stop_at = lock_release_by - timedelta(seconds=10)
    now_utc = max(now_values)
    if now_utc >= worker_stop_at:
        return None

    def canonical(value: datetime) -> str:
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    return {
        "next_formal_target_utc": canonical(target),
        "worker_stop_at_utc": canonical(worker_stop_at),
        "lock_release_by_utc": canonical(lock_release_by),
    }


def main(
    argv: list[str] | None = None,
    *,
    _on_execution: Callable[[TickExecutionResult], None] | None = None,
    deadline: float | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> int:
    ap = argparse.ArgumentParser(description='Multi-account tick with per-account notifications')
    ap.add_argument('--config', required=True)
    ap.add_argument('--accounts', nargs='+', default=None)
    ap.add_argument('--symbols', nargs='+', default=None, help='Comma-separated or space-separated symbol whitelist for this tick.')
    ap.add_argument('--default-account', default=None)
    ap.add_argument('--market-config', default='auto', choices=['auto', 'hk', 'us', 'all'], help='Select symbols by market at config-load time (auto=by session).')
    ap.add_argument('--no-send', action='store_true', help='Do not send messages (for smoke tests / debugging).')
    ap.add_argument('--experience', action='store_true', help='Run a non-executable simulated-account experience scan.')
    ap.add_argument('--smoke', action='store_true', help='Smoke mode: run scheduler decisions but skip pipeline execution.')
    ap.add_argument('--force', action='store_true', help='Force the scan pipeline outside normal run points; force runs do not auto-send ordinary Tick notifications.')
    ap.add_argument('--debug', action='store_true', help='Verbose logs to stdout (for manual debugging).')
    ap.add_argument('--opend-phone-verify-continue', action='store_true', help='Clear OpenD phone-verify pending pause and continue running.')
    ap.add_argument('--allow-stale-config', action='store_true', help='Emergency override: skip generated runtime config freshness checks.')
    args = ap.parse_args(argv)
    trigger_context = build_trigger_context()
    deadline = resolve_tick_deadline(
        timeout_seconds=trigger_context.get("timeout_seconds") or 600,
        deadline=deadline,
    )
    symbols_arg = ",".join(str(item) for item in args.symbols) if args.symbols is not None else None
    try:
        split_symbol_whitelist(symbols_arg)
    except ValueError as exc:
        ap.error(str(exc))

    set_debug(bool(getattr(args, 'debug', False)))

    no_send = bool(getattr(args, 'no_send', False))
    experience = bool(getattr(args, 'experience', False))
    smoke = bool(getattr(args, 'smoke', False))
    force_mode = bool(getattr(args, 'force', False))
    repo_root = Path(__file__).resolve().parents[2]
    runtime_resolution = resolve_runtime_root(repo_root=repo_root)
    base = runtime_resolution.runtime_root
    vpy = Path(sys.executable)
    cfg_path = Path(args.config)
    if not cfg_path.is_absolute():
        cfg_path = (repo_root / cfg_path).resolve()
    execution = TickExecutionResult(
        runtime_root=base.resolve(), config_path=cfg_path.resolve(), experience=experience,
    )
    if _on_execution is not None:
        _on_execution(execution)
    contract_info = resolve_config_contract(
        cfg_path,
        str(getattr(args, 'market_config', 'auto') or 'auto'),
        repo_base=repo_root,
    )
    ensure_runtime_canonical_config(
        cfg_path,
        str(getattr(args, 'market_config', 'auto') or 'auto'),
        repo_base=repo_root,
        require_sibling_external=True,
    )
    base_cfg = json.loads(cfg_path.read_text(encoding='utf-8'))
    if not isinstance(base_cfg, dict):
        raise SystemExit(f'[CONFIG_ERROR] runtime config must be a JSON object: {cfg_path}')
    requested_market = str(getattr(args, 'market_config', 'auto') or 'auto').strip().lower()
    try:
        ensure_runtime_config_identity(
            base_cfg,
            explicit_market=requested_market if requested_market in {'us', 'hk'} else None,
            runtime_config_path=cfg_path,
        )
    except RuntimeConfigIdentityError as exc:
        raise SystemExit(str(exc)) from exc
    schedule_contract_info = ensure_runtime_schedule_matches_market(
        base_cfg,
        config_path=cfg_path,
        market_config=requested_market,
    )
    allow_stale_config = bool(getattr(args, 'allow_stale_config', False))
    freshness_info: dict[str, Any] | None = None
    freshness_market = str(schedule_contract_info.get('market') or '').strip().lower()
    if freshness_market and not allow_stale_config:
        try:
            freshness_info = ensure_runtime_config_freshness(
                base_cfg,
                repo_root=repo_root,
                market=freshness_market,
                runtime_config_path=cfg_path,
            )
        except RuntimeConfigFreshnessError as exc:
            raise SystemExit(str(exc)) from exc
    if allow_stale_config:
        # The emergency override skips source-freshness comparison only. It must
        # never revive schema fields removed by the currently running release.
        validate_config(deepcopy(base_cfg))
    else:
        validate_retired_symbol_worker_config(base_cfg)
    execution = replace(
        execution,
        market=str(schedule_contract_info.get('market') or base_cfg.get('_generated', {}).get('market') or '').upper() or None,
    )
    if _on_execution is not None:
        _on_execution(execution)
    try:
        for configured_account in resolve_configured_accounts(base_cfg):
            build_account_portfolio_source_plan(base_cfg, account=configured_account)
    except ValueError as exc:
        raise SystemExit(f"[CONFIG_ERROR] {exc}") from exc
    try:
        args.accounts = resolve_configured_accounts(base_cfg, args.accounts)
    except ValueError as exc:
        raise SystemExit(f"[CONFIG_ERROR] invalid account scope: {exc}") from exc
    args.default_account = _resolve_default_account(args.default_account, args.accounts)
    execution = replace(execution, accounts=tuple(args.accounts))
    if _on_execution is not None:
        _on_execution(execution)
    if experience:
        try:
            validate_experience_request(
                config=base_cfg,
                accounts=list(args.accounts or []),
                no_send=no_send,
                smoke=smoke,
                trigger_context=trigger_context,
                opend_phone_verify_continue=bool(
                    getattr(args, 'opend_phone_verify_continue', False)
                ),
            )
        except ValueError as exc:
            raise SystemExit(f"[INVALID_REQUEST] {exc}") from exc
    trigger_kind = _resolve_daily_brief_trigger_kind(
        force_mode=force_mode,
        trigger_context=trigger_context,
    )
    runlog = RunLogger(base)
    global _CURRENT_RUN_ID
    _CURRENT_RUN_ID = runlog.run_id  # pyright: ignore[reportConstantRedefinition]
    run_id = runlog.run_id
    if _on_execution is not None:
        _on_execution(replace(execution, run_created=True, run_id=run_id))

    syms0 = resolve_watchlist_config(base_cfg)
    src_counts: dict[str, int] = {}
    for it in syms0:
        if not isinstance(it, dict):
            continue
        src, _decision = resolve_symbol_fetch_source(it.get('fetch') or {})
        src_counts[src] = src_counts.get(src, 0) + 1
    runlog.safe_event(
        'run_start',
        'start',
        data=_safe_runlog_data({
            'accounts': [str(a).strip().lower() for a in (args.accounts or []) if str(a).strip()],
            'symbols_count': len([x for x in syms0 if isinstance(x, dict)]),
            'symbols_arg': symbols_arg,
            'source_selections': src_counts,
            'market_config': str(getattr(args, 'market_config', 'auto') or 'auto'),
            'config_source_path': contract_info.get('resolved_path'),
            'config_canonical_path': contract_info.get('sibling_canonical_path'),
            'config_schedule_contract': schedule_contract_info,
            'config_freshness': freshness_info,
            'allow_stale_config': allow_stale_config,
            'repo_root': str(repo_root),
            'runtime_root': str(base),
            'runtime_root_source': runtime_resolution.source,
            'trigger_source': trigger_context.get('source'),
            'trigger_kind': trigger_kind,
            'trigger_job_id': trigger_context.get('job_id'),
            'outer_delivery_mode': trigger_context.get('delivery_mode'),
            'outer_announce_expected': trigger_context.get('announce_expected'),
            'outer_timeout_seconds': trigger_context.get('timeout_seconds'),
            'no_send': no_send,
            'smoke': bool(smoke),
            'force': force_mode,
            'experience': experience,
        }),
    )
    idempotency = build_tick_idempotency_context(
        cfg_path=cfg_path,
        market_config=str(getattr(args, 'market_config', 'auto') or 'auto'),
        accounts=args.accounts or [],
        trigger_kind=trigger_kind,
        symbols=symbols_arg,
        no_send=no_send,
        experience=experience,
        trigger_job_id=str(trigger_context.get("job_id") or ""),
    )
    market_cfg = idempotency.market_config
    execution_bucket = idempotency.bucket
    execution_idempotency_key = idempotency.key
    idempotency_accounts = idempotency.accounts
    idempotency_trigger_kind = idempotency.trigger_kind
    audit_helper = MultiTickAuditHelper(
        base=base,
        base_cfg=base_cfg,
        runlog=runlog,
        safe_data_fn=_safe_runlog_data,
        append_audit_event=state_repo.append_audit_event,
        record_project_failure=record_project_failure,
        record_project_success=record_project_success,
        build_failure_audit_fields=build_failure_audit_fields,
        run_id=run_id,
        idempotency_key=execution_idempotency_key,
        write_run_artifacts=False,
    )

    tick_metrics: dict[str, Any] = {}

    def publish_scheduler_skips(accounts: tuple[str, ...]) -> None:
        if _on_execution is not None and not experience:
            _on_execution(replace(
                execution, run_created=True, run_id=run_id,
                scheduler_skipped_accounts=accounts,
            ))

    def publish_completion(account_outcomes, rc, *, inner_run_id, accounts):
        wrapper_run_id = str(os.environ.get("OM_TICK_CRON_RUN_ID") or "").strip()
        if (not wrapper_run_id or Path(wrapper_run_id).name != wrapper_run_id or wrapper_run_id in {".", ".."}
            or trigger_kind != "scheduled" or no_send or symbols_arg or not accounts
            or not isinstance(account_outcomes, list)
            or any(not isinstance(row, dict) for row in account_outcomes)
            or sorted(row.get("account", "") for row in account_outcomes) != sorted(accounts)):
            return
        write_child_tick_completion(base=base, wrapper_run_id=wrapper_run_id, inner_run_id=inner_run_id,
            market=market_cfg, accounts=accounts, account_outcomes=account_outcomes, rc=rc)

    def complete_tick_idempotency(
        status: str = "completed",
        message: str | None = None,
        *,
        ok: bool = True,
        error_code: str | None = None,
    ) -> None:
        try:
            _complete_tick_idempotency(
                base=base,
                key=execution_idempotency_key,
                run_id=run_id,
                market_config=market_cfg,
                accounts=idempotency_accounts,
                trigger_kind=idempotency_trigger_kind,
                status=status,
                message=message,
                ok=ok,
                error_code=error_code,
                account_outcomes=tick_account_outcomes(tick_metrics),
            )
        except Exception as exc:
            error = "TICK_IDEMPOTENCY_TERMINAL_WRITE_FAILED"
            runlog.safe_event(
                "idempotency",
                "error",
                error_code=error,
                message=str(exc),
                data=_safe_runlog_data({"status": status, "terminal_error_code": error_code}),
            )
            audit_helper.audit(
                "idempotency",
                "complete_tick_execution_failed",
                status="error",
                message=str(exc),
                extra={"status": status, "error_code": error, "terminal_error_code": error_code},
            )
            audit_helper.guard_mark_failure(error, "idempotency")
            raise

    for it in syms0:
        if not isinstance(it, dict):
            continue
        sym = str(it.get('symbol') or '').strip().upper()
        if not sym:
            continue
        src, decision = resolve_symbol_fetch_source(it.get('fetch') or {})
        audit_helper.audit(
            'config',
            'fetch_source_decision',
            status='ok',
            tool_name='fetch_source_resolution',
            extra={'symbol': sym, 'source': src, 'decision': decision},
        )

    dedupe = state_repo.claim_idempotency_record(
        base,
        scope='tick_execution',
        key=execution_idempotency_key,
        payload={
            'status': 'in_progress',
            'run_id': run_id,
            'pid': os.getpid(),
            'market_config': market_cfg,
            'accounts': idempotency_accounts,
            'trigger_kind': idempotency_trigger_kind,
        },
    )
    if not bool(dedupe.get('claimed')):
        record = dedupe.get('record') if isinstance(dedupe.get('record'), dict) else {}
        if str(record.get('status') or '').strip().lower() in {'unsupported_failed', 'failed'}:
            error_code = str(record.get('error_code') or 'TICK_EXEC_FAILED')
            audit_helper.audit(
                'idempotency',
                'replay_terminal_tick_failure',
                status='error',
                message=error_code,
                extra={'bucket': execution_bucket, 'error_code': error_code},
            )
            runlog.safe_event('run_end', 'error', error_code=error_code, message=error_code)
            replay_rc = 2 if record.get("status") == "unsupported_failed" else 1
            publish_completion(record.get("account_outcomes"), replay_rc,
                               inner_run_id=str(record.get("run_id") or ""), accounts=idempotency_accounts)
            return replay_rc
        audit_helper.audit(
            'idempotency',
            'skip_duplicate_tick',
            status='skip',
            message='duplicate tick in same execution bucket',
            extra={'bucket': execution_bucket},
        )
        runlog.safe_event('run_end', 'skip', message='duplicate tick execution skipped')
        return 0
    audit_helper.audit('idempotency', 'claim_tick_execution', extra={'bucket': execution_bucket})

    guard_outcome = run_tick_guard_flow(
        TickGuardRequest(
            base=base,
            base_cfg=base_cfg,
            accounts=[str(a).strip() for a in (args.accounts or []) if str(a).strip()],
            default_account=args.default_account,
            market_config=market_cfg,
            no_send=no_send,
            opend_phone_verify_continue=bool(getattr(args, 'opend_phone_verify_continue', False)),
            vpy=vpy,
            runlog=runlog,
            audit_helper=audit_helper,
            complete_tick_idempotency_fn=complete_tick_idempotency,
            admit_project_run_fn=admit_project_run,
            apply_project_load_shed_fn=apply_project_load_shed,
            clear_opend_phone_verify_pending_fn=clear_opend_phone_verify_pending,
            is_opend_phone_verify_pending_fn=is_opend_phone_verify_pending,
            run_opend_watchdog_fn=partial(run_opend_watchdog, deadline=deadline, cancelled=cancelled),
            mark_opend_phone_verify_pending_fn=mark_opend_phone_verify_pending,
            send_opend_alert_fn=send_opend_alert,
            send_opend_recovery_notice_fn=send_opend_recovery_notice,
            allow_operational_side_effects=(not experience),
        )
    )
    if not guard_outcome.should_continue:
        return guard_outcome.return_code
    base_cfg = guard_outcome.base_cfg
    args.accounts = guard_outcome.accounts
    args.default_account = guard_outcome.default_account
    bj_tz = guard_outcome.bj_tz
    account_ids = [str(acct).strip() for acct in (args.accounts or []) if str(acct).strip()]
    results: list[AccountResult] = []

    scheduler_outcome = build_tick_scheduler_context(
        TickSchedulerRequest(
            vpy=vpy,
            repo_root=repo_root,
            base=base,
            cfg_path=cfg_path,
            base_cfg=base_cfg,
            accounts=[str(a).strip() for a in (args.accounts or []) if str(a).strip()],
            market_config=str(getattr(args, 'market_config', 'auto') or 'auto'),
            force_mode=(force_mode or experience),
            smoke=smoke,
            run_id=run_id,
            runlog=runlog,
            audit_helper=audit_helper,
            check_trading_day_for_market=lambda gm: _is_trading_day_guard_for_market(
                base_cfg, gm, deadline=deadline, cancelled=cancelled,
            ),
            run_scan_scheduler_cli_fn=partial(run_scan_scheduler_cli, deadline=deadline, cancelled=cancelled),
            account_view_cls=AccountSchedulerDecisionView,
        )
    )
    if not scheduler_outcome.should_continue:
        results.extend(scheduler_outcome.results)
        complete_tick_idempotency(
            status="failed",
            message=scheduler_outcome.message or "scheduler_failed",
            ok=False,
            error_code=scheduler_outcome.error_code or "SCHEDULER_FAILED",
        )
        return scheduler_outcome.return_code
    assert scheduler_outcome.context is not None
    scheduler_context = scheduler_outcome.context
    markets_to_run = scheduler_context.markets_to_run
    scheduler_markets = scheduler_context.scheduler_markets
    state_path = scheduler_context.state_path
    scheduler_schedule_key = scheduler_context.scheduler_schedule_key
    scheduler_ms = scheduler_context.scheduler_ms
    scheduler_decision = scheduler_context.scheduler_decision
    scheduler_view = scheduler_context.scheduler_view
    notify_decision_by_account = scheduler_context.notify_decision_by_account
    scan_decision_by_account = scheduler_context.scan_decision_by_account
    should_run_global = scheduler_context.should_run_global
    reason_global = scheduler_context.reason_global
    scheduler_decisions_by_account = {
        str(account).strip().lower(): dict(item["scheduler_decision"])
        for account, item in scan_decision_by_account.items()
        if isinstance(item, dict) and isinstance(item.get("scheduler_decision"), dict)
    }

    def commit_scan_targets(targets: dict[str, str | None]) -> None:
        if trigger_kind != "scheduled":
            return
        mark_scheduler_accounts(
            config=cfg_path,
            state=state_path,
            schedule_key=str(scheduler_schedule_key),
            accounts=list(targets),
            mark_scanned=True,
            processed_scan_targets_by_account=targets,
            base_dir=repo_root,
        )

    if not _has_scan_to_run(
        should_run_global=should_run_global,
        scan_decision_by_account=scan_decision_by_account,
    ):
        delivery_only_allowed = (
            trigger_kind == "scheduled"
            and len(scheduler_markets) == 1
            and (any(item.get("in_run_window") is True for item in scheduler_decisions_by_account.values())
                 or (not no_send and any(has_daily_brief_recovery_to_resume(
                     base=base, config=base_cfg, account=account, market=scheduler_markets[0],
                     scheduler=scheduler_decisions_by_account.get(account, scheduler_decision),
                 ) for account in account_ids)))
        )
        if not delivery_only_allowed:
            runlog.safe_event(
                'run_end',
                'skip',
                message=str(reason_global or 'scheduler_skip_no_scan'),
                data=_safe_runlog_data(
                    {
                        'reason': reason_global,
                        'scheduler_decision': scheduler_decision,
                        'accounts': account_ids,
                        'run_workspace_created': False,
                    }
                ),
            )
            complete_tick_idempotency(status='skipped', message=str(reason_global or 'scheduler_skip_no_scan'))
            publish_scheduler_skips(tuple(
                account for account in account_ids
                if scan_decision_by_account.get(account, {}).get("should_run") is False
            ))
            return 0
        warmup_context = _opening_chain_warmup_context(
            trigger_kind=trigger_kind,
            scheduler_markets=scheduler_markets,
            base_cfg=base_cfg,
            scheduler_schedule_key=str(scheduler_schedule_key),
            account_ids=account_ids,
            scheduler_decisions_by_account=scheduler_decisions_by_account,
        )
        tick_metrics["delivery_only"] = True
        notification_rc = _run_notification_with_post_delivery_fallback(
            notification_request=TickNotificationRequest(
                base=base,
                repo_root=repo_root,
                cfg_path=cfg_path,
                state_path=state_path,
                scheduler_schedule_key=str(scheduler_schedule_key),
                base_cfg=base_cfg,
                run_id=run_id,
                runlog=runlog,
                results=[],
                tick_metrics=tick_metrics,
                no_send=no_send,
                bj_tz=bj_tz,
                audit_helper=audit_helper,
                vpy=vpy,
                deadline=deadline,
                cancelled=cancelled,
                markets_to_run=markets_to_run,
                scheduler_markets=scheduler_markets,
                scheduler_decision=scheduler_decision,
                account_ids=tuple(account_ids),
                scheduler_decisions_by_account=scheduler_decisions_by_account,
                delivery_only=True,
                trigger_kind=trigger_kind,
            ),
            complete_tick_idempotency_fn=complete_tick_idempotency,
        )
        if notification_rc == 0:
            publish_scheduler_skips(tuple(
                account for account in account_ids
                if scan_decision_by_account.get(account, {}).get("should_run") is False
            ))
        if notification_rc != 0 or warmup_context is None or smoke:
            return notification_rc
        try:
            warmup_summary = warm_required_data_chain_cache(
                base=base,
                base_cfg=base_cfg,
                cfg_path=cfg_path,
                accounts=account_ids,
                markets_to_run=markets_to_run,
                symbols_arg=symbols_arg,
                deadline=deadline,
                cancelled=cancelled,
                **warmup_context,
            )
        except Exception as exc:
            warmup_summary = {
                **warmup_context,
                "status": "degraded",
                "reason_codes": [f"supervisor_{type(exc).__name__.lower()}"],
                "planned_shards": 0,
                "cache_hit_shards": 0,
                "fetched_shards": 0,
                "failed_shards": 0,
                "deadline_skipped_shards": 0,
                "planned_symbols": 0,
                "completed_symbols": 0,
                "option_chain_opend_calls": 0,
                "option_chain_rate_gate_wait_ms": 0,
                "option_contract_snapshot_requested_codes": 0,
                "child_terminated": False,
                "duration_ms": 0,
            }
        observation_status = (
            "ok"
            if warmup_summary.get("status") in {"ready", "not_applicable"}
            else "degraded"
        )
        runlog.safe_event(
            "opening_chain_warmup",
            observation_status,
            data=_safe_runlog_data(warmup_summary),
        )
        audit_helper.audit(
            "prefetch",
            "opening_chain_warmup",
            run_id=run_id,
            status=observation_status,
            extra=warmup_summary,
        )
        return notification_rc

    workspace = prepare_tick_run_workspace(
        base=base,
        run_id=run_id,
        default_account=args.default_account,
    )
    audit_helper.enable_run_artifacts()
    accounts_root = workspace.accounts_root
    run_dir = workspace.run_dir
    shared_required = workspace.shared_required

    tick_metrics: dict[str, Any] = {
        'as_of_utc': utc_now(),
        'markets_to_run': markets_to_run,
        'scheduler_markets': scheduler_markets,
        'run_dir': str(run_dir),
        'scheduler_ms': scheduler_ms,
        'scheduler_decision': scheduler_decision,
        'trigger_context': trigger_context,
        'trigger_kind': trigger_kind,
        'accounts': [],
        'sent': False,
        'reason': '',
    }

    account_count = len(account_ids)
    account_workers = _resolve_account_run_max_workers(base_cfg, account_count)
    account_execution_request = TickAccountExecutionRequest(
        account_ids=account_ids,
        account_workers=account_workers,
        base=base,
        repo_root=repo_root,
        base_cfg=base_cfg,
        cfg_path=cfg_path,
        vpy=vpy,
        markets_to_run=markets_to_run,
        scheduler_ms=scheduler_ms,
        scheduler_view=scheduler_view,
        notify_decision_by_account=notify_decision_by_account,
        should_run_global=should_run_global,
        reason_global=reason_global,
        run_id=run_id,
        run_dir=run_dir,
        shared_required=shared_required,
        accounts_root=accounts_root,
        force_mode=force_mode,
        smoke=smoke,
        no_send=no_send,
        scan_decision_by_account=scan_decision_by_account,
        state_path=state_path,
        scheduler_schedule_key=str(scheduler_schedule_key),
        runlog=runlog,
        audit_helper=audit_helper,
        symbols_arg=symbols_arg,
        trigger_kind=trigger_kind,
        experience=experience,
        deadline=deadline,
        cancelled=cancelled,
    )
    account_execution = run_tick_account_execution(account_execution_request)
    tick_metrics['accounts'].extend(account_execution.account_metrics)
    tick_metrics["scan_outcomes"] = dict(account_execution.scan_outcomes)
    tick_metrics["required_data_prefetch_invocation_count"] = (
        account_execution.prefetch_invocation_count
    )
    tick_metrics["required_data_snapshot_status"] = (
        account_execution.snapshot_status
    )
    tick_metrics["required_data_snapshot_manifest_sha256"] = (
        account_execution.snapshot_manifest_sha256
    )
    tick_metrics["prepared_portfolio_contexts"] = list(
        account_execution.prepared_context_metrics
    )
    results.extend(account_execution.results)

    if experience:
        experience_ok = _experience_scan_completed(
            requested_accounts=account_ids,
            ran_pipeline_accounts=account_execution.ran_pipeline_accounts,
        )
        experience_reason = (
            "experience_mode" if experience_ok else "experience_scan_failed"
        )
        tick_metrics["scan_mode"] = "experience"
        tick_metrics["capacity_source"] = "demo_scenario"
        tick_metrics["executable"] = False
        tick_metrics["sent"] = False
        tick_metrics["reason"] = experience_reason
        tick_metrics["requested_account_count"] = len(account_ids)
        tick_metrics["completed_account_count"] = len(
            account_execution.ran_pipeline_accounts
        )
        try:
            metrics_action = "write_tick_metrics"
            state_repo.write_tick_metrics(base, run_id, tick_metrics)
            metrics_action = "append_tick_metrics_history"
            state_repo.append_tick_metrics_history(base, run_id, tick_metrics)
            metrics_action = "write_tick_metrics"
            audit_helper.audit(
                "write",
                "write_tick_metrics",
                run_id=run_id,
                status=("ok" if experience_ok else "error"),
                message=experience_reason,
            )
        except Exception as exc:
            runlog.safe_event(
                "finalize",
                "degraded",
                message=f"{metrics_action} failed",
                data=_safe_runlog_data({"action": metrics_action, "error": str(exc)}),
            )
        runlog.safe_event(
            "run_end",
            "ok" if experience_ok else "error",
            error_code=(None if experience_ok else "EXPERIENCE_SCAN_FAILED"),
            data=_safe_runlog_data(
                {
                    "scan_mode": "experience",
                    "sent": False,
                    "requested_account_count": len(account_ids),
                    "completed_account_count": len(
                        account_execution.ran_pipeline_accounts
                    ),
                }
            ),
        )
        complete_tick_idempotency(
            status=("completed" if experience_ok else "failed"),
            message=experience_reason,
            ok=experience_ok,
            error_code=(None if experience_ok else "EXPERIENCE_SCAN_FAILED"),
        )
        if experience_ok:
            audit_helper.guard_mark_success()
        if _on_execution is not None:
            _on_execution(replace(execution, run_created=True, run_id=run_id, experience_completed=experience_ok))
        return 0 if experience_ok else 2

    post_delivery_sidecars_finished = False

    def publish_post_delivery_sidecars_once() -> None:
        nonlocal post_delivery_sidecars_finished
        if post_delivery_sidecars_finished:
            return
        post_delivery_sidecars_finished = True
        publish_runtime_portfolio_snapshot_shadows(
            request=account_execution_request,
            tasks=account_execution.runtime_portfolio_snapshot_shadow_tasks,
        )

    notification_request = TickNotificationRequest(
        base=base,
        repo_root=repo_root,
        cfg_path=cfg_path,
        state_path=state_path,
        scheduler_schedule_key=str(scheduler_schedule_key),
        base_cfg=base_cfg,
        run_id=run_id,
        runlog=runlog,
        results=results,
        tick_metrics=tick_metrics,
        no_send=no_send,
        bj_tz=bj_tz,
        audit_helper=audit_helper,
        vpy=vpy,
        deadline=deadline,
        cancelled=cancelled,
        markets_to_run=markets_to_run,
        scheduler_markets=scheduler_markets,
        scheduler_decision=scheduler_decision,
        ran_pipeline_accounts=account_execution.ran_pipeline_accounts,
        account_ids=tuple(account_ids),
        scheduler_decisions_by_account=scheduler_decisions_by_account,
        scheduled_scan_targets_by_account=account_execution.scheduled_scan_targets_by_account,
        trigger_kind=trigger_kind,
    )
    rc = _run_notification_with_post_delivery_fallback(
        notification_request=notification_request,
        complete_tick_idempotency_fn=complete_tick_idempotency,
        commit_scan_targets_fn=commit_scan_targets,
        post_delivery_sidecars_fn=publish_post_delivery_sidecars_once,
    )
    try:
        publish_completion(tick_account_outcomes(tick_metrics), rc, inner_run_id=run_id, accounts=account_ids)
    except Exception as exc:
        audit_helper.guard_mark_failure("TICK_COMPLETION_RECEIPT_FAILED", "completion_receipt")
        runlog.safe_event("completion_receipt", "error", error_code="TICK_COMPLETION_RECEIPT_FAILED", message=str(exc)[:240])
        return 2
    if rc == 0:
        publish_scheduler_skips(tuple(
            row["account"] for row in account_execution.account_metrics
            if row.get("account") in account_ids
            and row.get("scan_outcome") == "scheduler_skipped"
            and row.get("ran_scan") is False and row.get("ran_pipeline") is False
        ))
    return rc


def _validate_scheduled_scan_targets(request: TickNotificationRequest) -> None:
    if request.delivery_only or request.trigger_kind != "scheduled":
        return
    targets = request.scheduled_scan_targets_by_account or {}
    missing = sorted(account for account in request.ran_pipeline_accounts if not targets.get(account))
    if missing:
        request.audit_helper.audit(
            "scheduler", "scheduled_scan_target_missing", run_id=request.run_id,
            status="error", extra={"accounts": missing},
        )
        raise RuntimeError(f"scheduled scan target missing for accounts: {', '.join(missing)}")


def _finish_tick_notification(
    request: TickNotificationRequest, outcome: TickNotificationOutcome,
    complete_tick_idempotency_fn: Callable[..., None],
) -> int:
    """The Tick owner alone combines scan qualification with delivery facts."""
    sent_accounts = list(outcome.sent_accounts)
    failures = list(outcome.notify_failures)
    scan_failures = failed_scan_accounts(request.tick_metrics)
    request.tick_metrics["scan_failure_notified_accounts"] = [
        account for account in outcome.failure_source_confirmed_accounts if account in scan_failures
    ]
    request.tick_metrics["would_send_accounts"] = list(outcome.would_send_accounts)
    common = dict(
        base=request.base, run_id=request.run_id, runlog=request.runlog,
        results=request.results, tick_metrics=request.tick_metrics, no_send=request.no_send,
        state_repo=state_repo, utc_now_fn=utc_now, audit_fn=request.audit_helper.audit,
        safe_data_fn=_safe_runlog_data, on_success=lambda: None,
    )
    error_code = outcome.error_code
    if request.delivery_only:
        rc = 2 if error_code and outcome.skip_reason else 1 if failures or error_code else 0
        request.runlog.safe_event(
            "run_end", "error" if rc else "skip" if outcome.skip_reason else "ok",
            error_code=error_code or ("NOTIFY_FAILED" if failures else None),
            data=_safe_runlog_data({
                "reason": outcome.skip_reason or "daily_brief_delivery_only",
                "delivery_only": True, "sent_accounts": sent_accounts,
                "notify_failures": failures, "notify_summary": outcome.notify_summary,
            }),
        )
    elif outcome.skip_reason or (error_code and not outcome.notify_summary):
        reason = outcome.skip_reason or str(error_code)
        rc = finalize_no_account_notification(
            **{**common, "no_send": request.no_send or reason == "notifications_disabled"},
            reason=reason, run_end_outcome="error" if error_code else "ok" if reason == "no_daily_brief_delivery" else "skip",
            error_code=error_code, return_code=2 if error_code else 0,
        )
    else:
        rc = finalize_multi_tick_run(
            **common, sent_accounts=sent_accounts, notify_failures=failures,
            notify_summary=outcome.notify_summary, channel=outcome.channel, target=outcome.target,
            read_json_fn=read_json, shared_state_dir_getter=state_repo.shared_state_dir,
        )
    if rc:
        error_code = error_code or ("SCAN_FAILED" if scan_failures else "NOTIFY_FAILED")
        request.audit_helper.guard_mark_failure(
            error_code, "daily_brief_retry_guard" if outcome.error_code else "candidate_scan" if scan_failures else "tick_finalization",
        )
        complete_tick_idempotency_fn(
            status="unsupported_failed" if outcome.error_code else "failed",
            message=error_code, ok=False, error_code=error_code,
        )
    else:
        if outcome.skip_reason:
            complete_tick_idempotency_fn(
                status="skipped" if outcome.skip_reason in {"no_daily_brief_delivery", "no_retryable_delivery", "delivery_only_no_send", "quiet_hours"} else "completed",
                message=outcome.skip_reason,
            )
        elif request.delivery_only:
            complete_tick_idempotency_fn(status="completed", message="daily_brief_delivery_only")
        else:
            complete_tick_idempotency_fn()
        request.audit_helper.guard_mark_success()
    return rc


@daily_brief_delivery_validation_scope()
def _run_notification_with_post_delivery_fallback(
    *, notification_request: TickNotificationRequest,
    complete_tick_idempotency_fn: Callable[..., None],
    commit_scan_targets_fn: Callable[[dict[str, str | None]], None] | None = None,
    post_delivery_sidecars_fn: Callable[[], None] | None = None,
) -> int:
    request = notification_request
    prepared_outcomes: dict[str, bool] = {}
    stage = "validate_scan_targets"
    try:
        _validate_scheduled_scan_targets(request)
        stage = "prepare_daily_brief"
        started = monotonic()
        request.runlog.safe_event("daily_brief_prepare", "start")
        prepare_outcome = "error"
        try:
            if request.delivery_only:
                markets = tuple(dict.fromkeys(str(value).strip().upper() for value in
                    (request.markets_to_run or request.scheduler_markets) if str(value).strip()))
                briefs = TickBriefPreparation(markets=markets)
            else:
                briefs = prepare_tick_briefs(
                    base=request.base, run_id=request.run_id, config=request.base_cfg,
                    markets=request.markets_to_run or request.scheduler_markets,
                    results=request.results, ran_pipeline_accounts=request.ran_pipeline_accounts,
                    scheduled_scan_targets_by_account=request.scheduled_scan_targets_by_account,
                    scheduler_decisions_by_account=request.scheduler_decisions_by_account,
                    scheduler_decision=request.scheduler_decision, runlog=request.runlog,
                    recovery_intent_factory=partial(_daily_brief_recovery_intent_for_preparation, request),
                    deadline=request.deadline, cancelled=request.cancelled,
                    on_account_prepared=prepared_outcomes.__setitem__,
                )
            merge_scan_outcomes(request.tick_metrics, briefs.scan_outcomes_by_account)
            stage = "prepare_notification"
            preparation = _prepare_daily_brief_notification(request, brief_preparation=briefs)
            prepare_outcome = "ok"
            request.runlog.safe_event("daily_brief_prepare", "ok")
        except BaseException as exc:
            request.runlog.safe_event("daily_brief_prepare", "error", data={"error_type": type(exc).__name__})
            raise
        finally:
            record_tick_latency(
                runlog=request.runlog, stage="daily_brief_prepare", started=started,
                data={"outcome": prepare_outcome, "delivery_only": request.delivery_only},
            )
        # History, recovery intent (including its frozen absence), and every
        # selected envelope/fanout are durable before the scheduler target moves.
        stage = "commit_scan_targets"
        if not request.delivery_only and request.trigger_kind == "scheduled":
            for (account, _market), source in briefs.sources.items():
                if not source["pipeline_reliable"]:
                    continue
                target = (request.scheduled_scan_targets_by_account or {}).get(account)
                if not target:
                    continue
                if commit_scan_targets_fn is None:
                    raise RuntimeError("scheduled scan target commit callback is required")
                targets = {account: target}
                commit_started = monotonic()
                try:
                    commit_scan_targets_fn(targets)
                except Exception as exc:
                    request.audit_helper.audit(
                        "scheduler", "scan_target_commit_failed", run_id=request.run_id,
                        status="error", message=str(exc), extra={"targets": targets},
                    )
                    raise
                finally:
                    record_tick_latency(
                        runlog=request.runlog, stage="scheduler_target_commit", started=commit_started,
                        data={"account": account},
                    )
                request.audit_helper.audit(
                    "scheduler", "scan_target_committed", run_id=request.run_id,
                    account=account, status="ok", extra={
                        "target": target, "brief_digest": source["persisted"]["current_brief_digest"],
                    },
                )
        stage = "notify"
        outcome = run_tick_notification_flow(replace(request, preparation=preparation))
    except BaseException as exc:
        codes = {
            "validate_scan_targets": "SCHEDULED_SCAN_TARGET_MISSING",
            "prepare_daily_brief": "DAILY_BRIEF_PREPARE_FAILED",
            "prepare_notification": "DAILY_BRIEF_PREPARE_FAILED",
            "commit_scan_targets": "SCHEDULER_TARGET_COMMIT_FAILED",
            "notify": "NOTIFY_FAILED",
        }
        code = codes[stage]
        if stage == "prepare_daily_brief":
            merge_scan_outcomes(request.tick_metrics, {
                account: prepared_outcomes.get(account, False)
                for account in request.ran_pipeline_accounts
            })
        # Terminal evidence and cleanup do not consume the cancelled business
        # budget. Preserve the original exception even if evidence writing fails.
        try:
            request.audit_helper.guard_mark_failure(code, stage)
            finalize_no_account_notification(
                base=request.base, run_id=request.run_id, runlog=request.runlog,
                results=request.results, tick_metrics=request.tick_metrics,
                no_send=request.no_send, state_repo=state_repo, utc_now_fn=utc_now,
                audit_fn=request.audit_helper.audit, safe_data_fn=_safe_runlog_data,
                on_success=lambda: None, reason=code, error_code=code,
                run_end_outcome="error", return_code=1,
            )
            complete_tick_idempotency_fn(status="failed", message=str(exc), ok=False, error_code=code)
        except Exception as terminal_exc:
            request.runlog.safe_event("finalize", "degraded", message=str(terminal_exc))
        raise
    finally:
        if post_delivery_sidecars_fn is not None:
            try:
                post_delivery_sidecars_fn()
            except Exception as exc:
                request.audit_helper.audit(
                    "observe", "post_delivery_sidecars_failed", run_id=request.run_id,
                    status="degraded", message=str(exc), extra={"exception_type": type(exc).__name__},
                )
                try:
                    request.runlog.safe_event(
                        "post_delivery_sidecars", "degraded", message=str(exc),
                        data={"exception_type": type(exc).__name__},
                    )
                except Exception:
                    pass
    return _finish_tick_notification(request, outcome, complete_tick_idempotency_fn)


multi_tick_main = main


def run_tick(argv: list[str] | None = None) -> int:
    return int(multi_tick_main(list(argv or [])))


def execute_tick(
    argv: list[str] | None = None,
    *, deadline: float | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> TickExecutionResult:
    """Run the same tick entry with a reference captured inside this invocation."""
    result = TickExecutionResult()

    def observe(value: TickExecutionResult) -> None:
        nonlocal result
        result = value

    try:
        rc = main(list(argv or []), _on_execution=observe, deadline=deadline, cancelled=cancelled)
    except SystemExit as exc:
        if result.runtime_root is None:
            raise
        rc = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 2)
        return replace(result, rc=rc, error=str(exc) if rc else None)
    except KeyboardInterrupt:
        return replace(result, rc=130, error="运行已取消")
    except Exception as exc:
        return replace(result, rc=2, error=str(exc))
    return replace(result, rc=int(rc))


__all__ = ['main', 'multi_tick_main', 'run_tick', 'execute_tick', 'TickExecutionResult', 'current_run_id', '_CURRENT_RUN_ID']
