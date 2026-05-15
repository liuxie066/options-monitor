from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from src.infrastructure.io_utils import (
    read_json,
    utc_now,
)
from src.infrastructure.run_log import RunLogger
from src.application.account_config import accounts_from_config
from src.application.config_loader import resolve_watchlist_config
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
from src.application.multi_tick.misc import (
    set_debug,
    AccountResult,
    _safe_runlog_data,
)
from src.application.multi_tick.cash_footer import query_cash_footer
from src.application.multi_tick.notify_format import build_account_message
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
    mark_scanned_accounts as _mark_scanned_accounts,
    resolve_account_run_max_workers as _resolve_account_run_max_workers,
    resolve_default_account as _resolve_default_account,
    run_tick_account_execution,
    run_account_outcomes as _run_account_outcomes,
    should_update_account_legacy_output as _should_update_account_legacy_output,
)
from src.application.tick_notification_flow import (
    TickNotificationRequest,
    run_tick_notification_flow,
)
from src.application.tick_run_context import (
    build_tick_idempotency_context,
    complete_tick_idempotency as _complete_tick_idempotency,
)
from src.application.tick_run_workspace import prepare_tick_run_workspace
from src.application.runtime_trigger_context import build_trigger_context
from src.application.runtime_config_freshness import RuntimeConfigFreshnessError, ensure_runtime_config_freshness
from src.application.tick_scheduler_context import (
    TickSchedulerRequest,
    build_tick_scheduler_context,
)
from src.infrastructure.external_services import (
    run_opend_watchdog,
    run_scan_scheduler_cli,
    select_notification_delivery_adapter,
    trading_day_via_futu,
)

from domain.storage.repositories import state_repo


_CURRENT_RUN_ID: str | None = None


def current_run_id() -> str | None:
    """Public accessor for wrapper-level error logging compatibility."""
    return _CURRENT_RUN_ID


def account_run_state_dir(run_dir: Path, account: str) -> Path:
    """Legacy helper kept for compatibility with existing tests/callers."""
    return (run_dir / 'accounts' / str(account).strip() / 'state').resolve()


def _is_trading_day_guard_for_market(cfg: dict[str, Any], market: str) -> tuple[bool | None, str]:
    """Return (is_trading_day, market_used) for one market.

    None means guard check failed and caller should continue without blocking.
    """
    return trading_day_via_futu(cfg, market)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description='Multi-account tick with per-account notifications')
    ap.add_argument('--config', default='config.us.json')
    ap.add_argument('--accounts', nargs='+', default=None)
    ap.add_argument('--default-account', default=None)
    ap.add_argument('--market-config', default='auto', choices=['auto', 'hk', 'us', 'all'], help='Select symbols by market at config-load time (auto=by session).')
    ap.add_argument('--no-send', action='store_true', help='Do not send messages (for smoke tests / debugging).')
    ap.add_argument('--smoke', action='store_true', help='Smoke mode: run scheduler decisions but skip pipeline execution.')
    ap.add_argument('--force', action='store_true', help='Force running scan pipeline regardless of run window / run points (sending still respects --no-send and should_notify decisions).')
    ap.add_argument('--debug', action='store_true', help='Verbose logs to stdout (for manual debugging).')
    ap.add_argument('--opend-phone-verify-continue', action='store_true', help='Clear OpenD phone-verify pending pause and continue running.')
    ap.add_argument('--allow-stale-config', action='store_true', help='Emergency override: skip generated runtime config freshness checks.')
    args = ap.parse_args(argv)

    set_debug(bool(getattr(args, 'debug', False)))

    no_send = bool(getattr(args, 'no_send', False))
    smoke = bool(getattr(args, 'smoke', False))
    force_mode = bool(getattr(args, 'force', False))

    base = Path(__file__).resolve().parents[2]
    vpy = base / '.venv' / 'bin' / 'python'
    runlog = RunLogger(base)
    global _CURRENT_RUN_ID
    _CURRENT_RUN_ID = runlog.run_id  # pyright: ignore[reportConstantRedefinition]
    run_id = runlog.run_id

    cfg_path = Path(args.config)
    if not cfg_path.is_absolute():
        cfg_path = (base / cfg_path).resolve()
    contract_info = resolve_config_contract(
        cfg_path,
        str(getattr(args, 'market_config', 'auto') or 'auto'),
        repo_base=base,
    )
    ensure_runtime_canonical_config(
        cfg_path,
        str(getattr(args, 'market_config', 'auto') or 'auto'),
        repo_base=base,
        require_sibling_external=True,
    )
    base_cfg = json.loads(cfg_path.read_text(encoding='utf-8'))
    schedule_contract_info = ensure_runtime_schedule_matches_market(
        base_cfg,
        config_path=cfg_path,
        market_config=str(getattr(args, 'market_config', 'auto') or 'auto'),
    )
    allow_stale_config = bool(getattr(args, 'allow_stale_config', False))
    freshness_info: dict[str, Any] | None = None
    freshness_market = str(schedule_contract_info.get('market') or '').strip().lower()
    if freshness_market and not allow_stale_config:
        try:
            freshness_info = ensure_runtime_config_freshness(
                base_cfg,
                repo_root=base,
                market=freshness_market,
                runtime_config_path=cfg_path,
            )
        except RuntimeConfigFreshnessError as exc:
            raise SystemExit(str(exc)) from exc
    trigger_context = build_trigger_context()
    if args.accounts is None:
        args.accounts = accounts_from_config(base_cfg)
    else:
        args.accounts = accounts_from_config({'accounts': args.accounts})
    args.default_account = _resolve_default_account(args.default_account, args.accounts)

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
            'source_selections': src_counts,
            'market_config': str(getattr(args, 'market_config', 'auto') or 'auto'),
            'config_source_path': contract_info.get('resolved_path'),
            'config_canonical_path': contract_info.get('sibling_canonical_path'),
            'config_schedule_contract': schedule_contract_info,
            'config_freshness': freshness_info,
            'allow_stale_config': allow_stale_config,
            'trigger_source': trigger_context.get('source'),
            'trigger_job_id': trigger_context.get('job_id'),
            'outer_delivery_mode': trigger_context.get('delivery_mode'),
            'outer_announce_expected': trigger_context.get('announce_expected'),
            'outer_timeout_seconds': trigger_context.get('timeout_seconds'),
            'no_send': no_send,
            'smoke': bool(smoke),
            'force': force_mode,
        }),
    )
    idempotency = build_tick_idempotency_context(
        cfg_path=cfg_path,
        market_config=str(getattr(args, 'market_config', 'auto') or 'auto'),
        accounts=args.accounts or [],
    )
    market_cfg = idempotency.market_config
    execution_bucket = idempotency.bucket
    execution_idempotency_key = idempotency.key
    idempotency_accounts = idempotency.accounts
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
    )

    def complete_tick_idempotency(status: str = "completed", message: str | None = None) -> None:
        try:
            _complete_tick_idempotency(
                base=base,
                key=execution_idempotency_key,
                run_id=run_id,
                market_config=market_cfg,
                accounts=idempotency_accounts,
                status=status,
                message=message,
            )
        except Exception:
            pass

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
        },
    )
    if not bool(dedupe.get('claimed')):
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
            run_opend_watchdog_fn=run_opend_watchdog,
            mark_opend_phone_verify_pending_fn=mark_opend_phone_verify_pending,
            send_opend_alert_fn=send_opend_alert,
            send_opend_recovery_notice_fn=send_opend_recovery_notice,
        )
    )
    if not guard_outcome.should_continue:
        return guard_outcome.return_code
    base_cfg = guard_outcome.base_cfg
    args.accounts = guard_outcome.accounts
    args.default_account = guard_outcome.default_account
    bj_tz = guard_outcome.bj_tz

    results: list[AccountResult] = []

    workspace = prepare_tick_run_workspace(
        base=base,
        run_id=run_id,
        default_account=args.default_account,
    )
    accounts_root = workspace.accounts_root
    legacy_output_tmp_dir = workspace.legacy_output_tmp_dir
    out_link = workspace.out_link
    run_dir = workspace.run_dir
    prefetch_done = False
    shared_required = workspace.shared_required

    scheduler_outcome = build_tick_scheduler_context(
        TickSchedulerRequest(
            vpy=vpy,
            base=base,
            cfg_path=cfg_path,
            base_cfg=base_cfg,
            accounts=[str(a).strip() for a in (args.accounts or []) if str(a).strip()],
            market_config=str(getattr(args, 'market_config', 'auto') or 'auto'),
            force_mode=force_mode,
            smoke=smoke,
            run_id=run_id,
            runlog=runlog,
            audit_helper=audit_helper,
            check_trading_day_for_market=lambda gm: _is_trading_day_guard_for_market(base_cfg, gm),
            run_scan_scheduler_cli_fn=run_scan_scheduler_cli,
            account_view_cls=AccountSchedulerDecisionView,
        )
    )
    if not scheduler_outcome.should_continue:
        results.extend(scheduler_outcome.results)
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

    tick_metrics: dict[str, Any] = {
        'as_of_utc': utc_now(),
        'markets_to_run': markets_to_run,
        'scheduler_markets': scheduler_markets,
        'run_dir': str(run_dir),
        'scheduler_ms': scheduler_ms,
        'scheduler_decision': scheduler_decision,
        'trigger_context': trigger_context,
        'accounts': [],
        'sent': False,
        'reason': '',
    }

    account_ids = [str(acct).strip() for acct in (args.accounts or []) if str(acct).strip()]
    account_count = len(account_ids)
    account_workers = _resolve_account_run_max_workers(base_cfg, account_count)
    update_account_legacy_output = _should_update_account_legacy_output(account_count)
    account_execution = run_tick_account_execution(
        TickAccountExecutionRequest(
            account_ids=account_ids,
            account_workers=account_workers,
            base=base,
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
            out_link=out_link,
            legacy_output_tmp_dir=legacy_output_tmp_dir,
            accounts_root=accounts_root,
            prefetch_done=prefetch_done,
            force_mode=force_mode,
            smoke=smoke,
            no_send=no_send,
            scan_decision_by_account=scan_decision_by_account,
            state_path=state_path,
            scheduler_schedule_key=str(scheduler_schedule_key),
            update_legacy_output=update_account_legacy_output,
            runlog=runlog,
            audit_helper=audit_helper,
        )
    )
    tick_metrics['accounts'].extend(account_execution.account_metrics)
    results.extend(account_execution.results)

    return run_tick_notification_flow(
        TickNotificationRequest(
            base=base,
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
            complete_tick_idempotency_fn=complete_tick_idempotency,
        )
    )


multi_tick_main = main


def run_tick(argv: list[str] | None = None) -> int:
    return int(multi_tick_main(list(argv or [])))


__all__ = ['main', 'multi_tick_main', 'run_tick', 'current_run_id', 'account_run_state_dir', '_CURRENT_RUN_ID']
