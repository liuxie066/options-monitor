from __future__ import annotations

import argparse
import json
import os
from hashlib import sha256
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    from scripts.run_log import RunLogger
except Exception:
    from run_log import RunLogger

from scripts.io_utils import (
    parse_last_json_obj,
    read_json,
    utc_now,
    bj_now,
)
from scripts.account_config import accounts_from_config, cash_footer_accounts_from_config
from scripts.config_loader import resolve_watchlist_config, set_watchlist_config
from domain.domain.fetch_source import is_futu_fetch_source, resolve_symbol_fetch_source

from .cash_footer import query_cash_footer
from .notify_format import build_account_message
from .opend_guard import (
    clear_opend_phone_verify_pending,
    is_opend_phone_verify_pending,
    mark_opend_phone_verify_pending,
    send_opend_alert,
)
from .project_guard import (
    admit_project_run,
    apply_project_load_shed,
    record_project_failure,
    record_project_success,
)
from .misc import (
    set_debug,
    log,
    parse_hhmm,
    update_legacy_output_link,
    ensure_account_output_dir,
    AccountResult,
    _safe_runlog_data,
)
from domain.domain import (
    SchemaValidationError,
    SnapshotDTO,
    build_account_messages,
    build_no_candidate_account_messages,
    cash_footer_for_account,
    evaluate_dnd_quiet_hours,
    classify_failure,
    ensure_runtime_canonical_config,
    normalize_notify_subprocess_output,
    resolve_config_contract,
    markets_for_trading_day_guard as domain_markets_for_trading_day_guard,
    reduce_trading_day_guard,
    resolve_notification_route_from_config,
    select_markets_to_run as domain_select_markets_to_run,
    select_scheduler_state_filename,
)
from domain.domain.engine import (
    AccountSchedulerDecisionView,
    build_opend_unhealthy_execution_plan,
    decide_notification_delivery,
    decide_trading_day_guard,
    build_failure_audit_fields,
    filter_notify_candidates as engine_filter_notify_candidates,
    rank_notify_candidates,
    resolve_multi_tick_engine_entrypoint,
)
from src.application.cron_runtime import (
    apply_notify_results_to_tick_metrics,
    build_notify_summary,
    mark_accounts_notified,
    request_scheduler_update,
)
from src.application.account_run import AccountRunRequest, run_one_account
from src.application.multi_tick_audit import MultiTickAuditHelper
from src.application.multi_tick_finalization import (
    finalize_multi_tick_run,
    finalize_no_account_notification,
)
from src.application.multi_tick_scheduler import (
    resolve_markets_to_run,
    run_scheduler_flow,
)
from src.application.multi_tick_watchdog import run_multi_tick_watchdog
from src.application.scheduled_notification import (
    build_multi_account_delivery,
    execute_multi_account_delivery,
    prepare_multi_account_messages,
)
from scripts.infra.service import (
    normalize_feishu_app_send_output,
    run_opend_watchdog,
    run_scan_scheduler_cli,
    send_feishu_app_message_process,
    trading_day_via_futu,
)

try:
    from domain.storage import paths as storage_paths
    from domain.storage.repositories import run_repo, state_repo
except Exception:
    from scripts.domain.storage import paths as storage_paths  # type: ignore
    from scripts.domain.storage.repositories import run_repo, state_repo  # type: ignore


_CURRENT_RUN_ID: str | None = None


def current_run_id() -> str | None:
    """Public accessor for wrapper-level error logging compatibility."""
    return _CURRENT_RUN_ID


def account_run_state_dir(run_dir: Path, account: str) -> Path:
    """Legacy helper kept for compatibility with existing tests/callers."""
    return (run_dir / 'accounts' / str(account).strip() / 'state').resolve()


def _is_trading_day_guard_for_market(cfg: dict, market: str) -> tuple[bool | None, str]:
    """Return (is_trading_day, market_used) for one market.

    None means guard check failed and caller should continue without blocking.
    """
    return trading_day_via_futu(cfg, market)


def main() -> int:
    ap = argparse.ArgumentParser(description='Multi-account tick with merged notification')
    ap.add_argument('--config', default='config.us.json')
    ap.add_argument('--accounts', nargs='+', default=None)
    ap.add_argument('--default-account', default=None)
    ap.add_argument('--market-config', default='auto', choices=['auto', 'hk', 'us', 'all'], help='Select symbols by market at config-load time (auto=by session).')
    ap.add_argument('--no-send', action='store_true', help='Do not send messages (for smoke tests / debugging).')
    ap.add_argument('--smoke', action='store_true', help='Smoke mode: run scheduler decisions but skip pipeline execution.')
    ap.add_argument('--force', action='store_true', help='Force running scan pipeline regardless of market hours / scan interval (sending still respects --no-send and should_notify decisions).')
    ap.add_argument('--debug', action='store_true', help='Verbose logs to stdout (for manual debugging).')
    ap.add_argument('--opend-phone-verify-continue', action='store_true', help='Clear OpenD phone-verify pending pause and continue running.')
    args = ap.parse_args()

    set_debug(bool(getattr(args, 'debug', False)))

    no_send = bool(getattr(args, 'no_send', False))
    smoke = bool(getattr(args, 'smoke', False))
    force_mode = bool(getattr(args, 'force', False))

    base = Path(__file__).resolve().parents[2]
    vpy = base / '.venv' / 'bin' / 'python'
    runlog = RunLogger(base)
    global _CURRENT_RUN_ID
    _CURRENT_RUN_ID = runlog.run_id

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
    if args.accounts is None:
        args.accounts = accounts_from_config(base_cfg)
    else:
        args.accounts = accounts_from_config({'accounts': args.accounts})
    if args.default_account is None:
        args.default_account = args.accounts[0]
    else:
        args.default_account = str(args.default_account).strip().lower()

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
            'no_send': no_send,
            'smoke': bool(smoke),
            'force': force_mode,
        }),
    )
    market_cfg = str(getattr(args, 'market_config', 'auto') or 'auto').lower()
    execution_bucket = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M')
    execution_idempotency_key = sha256(
        (
            f"{cfg_path.resolve()}|{market_cfg}|"
            f"{','.join(sorted([str(a).strip().lower() for a in (args.accounts or []) if str(a).strip()]))}|"
            f"{execution_bucket}"
        ).encode('utf-8')
    ).hexdigest()
    audit_helper = MultiTickAuditHelper(
        base=base,
        base_cfg=base_cfg,
        runlog=runlog,
        safe_data_fn=_safe_runlog_data,
        append_audit_event=state_repo.append_audit_event,
        record_project_failure=record_project_failure,
        record_project_success=record_project_success,
        build_failure_audit_fields=build_failure_audit_fields,
        run_id=runlog.run_id,
        idempotency_key=execution_idempotency_key,
    )

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

    dedupe = state_repo.put_idempotency_success(
        base,
        scope='tick_execution',
        key=execution_idempotency_key,
        payload={
            'ok': True,
            'status': 'started',
            'run_id': runlog.run_id,
            'market_config': market_cfg,
            'accounts': [str(a).strip().lower() for a in (args.accounts or []) if str(a).strip()],
        },
    )
    if not bool(dedupe.get('created')):
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

    guard_admission = admit_project_run(base, base_cfg)
    if not bool(guard_admission.get('allowed')):
        msg = str(guard_admission.get('reason') or 'project guard blocked')
        err = str(guard_admission.get('error_code') or 'PROJECT_GUARD_BLOCKED')
        runlog.safe_event('project_guard', 'skip', error_code=err, message=msg)
        runlog.safe_event('run_end', 'skip', error_code=err, message=msg)
        return 0

    accounts_normalized = [str(a).strip() for a in (args.accounts or []) if str(a).strip()]
    accounts_effective = apply_project_load_shed(accounts_normalized, guard_admission)
    if accounts_effective != accounts_normalized:
        args.accounts = accounts_effective
        runlog.safe_event(
            'project_guard',
            'degraded',
            message='half-open probe mode load shedding',
            data=_safe_runlog_data({
                'mode': guard_admission.get('mode'),
                'accounts_before': accounts_normalized,
                'accounts_after': accounts_effective,
            }),
        )

    if market_cfg in ('hk', 'us'):
        try:
            base_cfg = dict(base_cfg)
            syms = resolve_watchlist_config(base_cfg)
            set_watchlist_config(
                base_cfg,
                [it for it in syms if isinstance(it, dict) and (it.get('broker') == market_cfg.upper())],
            )
        except Exception:
            pass

    schedule_cfg = base_cfg.get('schedule', {}) or {}
    bj_tz = ZoneInfo(schedule_cfg.get('beijing_timezone', 'Asia/Shanghai'))

    if bool(getattr(args, 'opend_phone_verify_continue', False)):
        clear_opend_phone_verify_pending(base)

    if is_opend_phone_verify_pending(base):
        audit_helper.audit('guard', 'opend_phone_verify_pending', status='skip')
        runlog.safe_event('run_end', 'skip', message='opend phone verify pending; paused until user confirmation')
        audit_helper.guard_mark_success()
        return 0
    watchdog_outcome = run_multi_tick_watchdog(
        base=base,
        base_cfg=base_cfg,
        accounts=[str(a).strip() for a in (args.accounts or []) if str(a).strip()],
        no_send=no_send,
        vpy=vpy,
        runlog=runlog,
        safe_data_fn=_safe_runlog_data,
        utc_now_fn=utc_now,
        audit_fn=audit_helper.audit,
        on_guard_failure=audit_helper.guard_mark_failure,
        run_opend_watchdog=run_opend_watchdog,
        parse_last_json_obj=parse_last_json_obj,
        classify_failure=classify_failure,
        resolve_watchlist_config=resolve_watchlist_config,
        is_futu_fetch_source=is_futu_fetch_source,
        resolve_multi_tick_engine_entrypoint=resolve_multi_tick_engine_entrypoint,
        build_opend_unhealthy_execution_plan=build_opend_unhealthy_execution_plan,
        mark_opend_phone_verify_pending=mark_opend_phone_verify_pending,
        send_opend_alert=send_opend_alert,
        state_repo=state_repo,
    )
    if not watchdog_outcome.should_continue:
        return watchdog_outcome.return_code

    try:
        import shutil, time, re
        runs_root = (base / 'output_runs').resolve()
        runs_root.mkdir(parents=True, exist_ok=True)
        cutoff = time.time() - 7 * 86400
        pat = re.compile(r'^\d{8}T\d{6}$')
        for d in runs_root.iterdir():
            try:
                if not d.is_dir():
                    continue
                if not pat.match(d.name):
                    continue
                if d.stat().st_mtime < cutoff:
                    shutil.rmtree(d, ignore_errors=True)
            except Exception:
                pass
    except Exception:
        pass

    accounts_root = (base / 'output_accounts').resolve()
    accounts_root.mkdir(parents=True, exist_ok=True)
    legacy_output_tmp_dir = (base / 'output_shared' / 'tmp' / 'legacy_output_link').resolve()
    legacy_output_tmp_dir.mkdir(parents=True, exist_ok=True)

    out_link = base / 'output'
    if not out_link.exists():
        dst = accounts_root / args.default_account
        ensure_account_output_dir(dst)
        try:
            update_legacy_output_link(out_link, dst, tmp_dir=legacy_output_tmp_dir)
        except RuntimeError as exc:
            raise SystemExit(str(exc))
    elif not out_link.is_symlink():
        if os.access(out_link.parent, os.W_OK):
            raise SystemExit(f"./output must be a symlink for multi-account mode: {out_link}")
        log(f'skip legacy output link validation on read-only repo root: {out_link}')

    results: list[AccountResult] = []

    now_utc = datetime.now(timezone.utc)
    markets_to_run = resolve_markets_to_run(
        now_utc=now_utc,
        base_cfg=base_cfg,
        market_config=str(getattr(args, 'market_config', 'auto') or 'auto'),
        force_mode=force_mode,
        runlog=runlog,
        safe_data_fn=_safe_runlog_data,
        domain_select_markets_to_run=domain_select_markets_to_run,
        domain_markets_for_trading_day_guard=domain_markets_for_trading_day_guard,
        decide_trading_day_guard=decide_trading_day_guard,
        reduce_trading_day_guard=reduce_trading_day_guard,
        check_trading_day_for_market=lambda gm: _is_trading_day_guard_for_market(base_cfg, gm),
        on_skip=audit_helper.guard_mark_success,
    )

    state_repo.shared_state_dir(base)
    state_path = storage_paths.shared_state_path(base, select_scheduler_state_filename(markets_to_run))

    try:
        if (not state_path.exists()) or state_path.stat().st_size <= 0:
            state_repo.write_shared_state(base, state_path.name, {
                'last_scan_utc': None,
                'last_notify_utc': None,
            })
    except Exception:
        pass

    scheduler_schedule_key = 'schedule_hk' if (markets_to_run == ['HK'] and ('schedule_hk' in (base_cfg or {}))) else 'schedule'
    try:
        scheduler_result = run_scheduler_flow(
            vpy=vpy,
            base=base,
            cfg_path=cfg_path,
            base_cfg=base_cfg,
            state_path=state_path,
            scheduler_schedule_key=scheduler_schedule_key,
            accounts=[str(a).strip() for a in (args.accounts or []) if str(a).strip()],
            force_mode=force_mode,
            smoke=smoke,
            snapshot_cls=SnapshotDTO,
            engine_entrypoint=resolve_multi_tick_engine_entrypoint,
            account_view_cls=AccountSchedulerDecisionView,
            run_scan_scheduler_cli=run_scan_scheduler_cli,
            build_failure_audit_fields=build_failure_audit_fields,
            audit_fn=audit_helper.audit,
            fail_schema_validation=audit_helper.fail_schema_validation,
        )
        scheduler_ms = scheduler_result.scheduler_ms
        scheduler_decision = scheduler_result.scheduler_decision
        scheduler_view = scheduler_result.scheduler_view
        notify_decision_by_account = scheduler_result.notify_decision_by_account
        should_run_global = scheduler_result.should_run_global
        reason_global = scheduler_result.reason_global
    except RuntimeError as exc:
        err = str(exc)
        for acct in args.accounts:
            acct0 = str(acct).strip()
            if acct0:
                results.append(AccountResult(acct0, False, False, err, ''))
        runlog.safe_event('run_end', 'error', error_code='SCHEDULER_FAILED', message=err)
        audit_helper.guard_mark_failure('SCHEDULER_FAILED', 'scan_scheduler')
        return 0

    ran_any_pipeline = False

    run_id = utc_now().replace(':', '').replace('-', '').split('.')[0]
    run_dir = run_repo.ensure_run_dir(base, run_id)
    required_dir = (run_dir / 'required_data').resolve()
    required_raw = (required_dir / 'raw').resolve()
    required_parsed = (required_dir / 'parsed').resolve()
    required_raw.mkdir(parents=True, exist_ok=True)
    required_parsed.mkdir(parents=True, exist_ok=True)

    prefetch_done = False
    shared_required = required_dir
    run_repo.ensure_run_state_dir(base, run_id)

    try:
        state_repo.write_last_run_dir_pointer(base, run_id)
    except Exception:
        pass
    tick_metrics = {
        'as_of_utc': utc_now(),
        'markets_to_run': markets_to_run,
        'run_dir': str(run_dir),
        'scheduler_ms': scheduler_ms,
        'scheduler_decision': scheduler_decision,
        'accounts': [],
        'sent': False,
        'reason': '',
    }

    try:
        scheduler_snapshot = SnapshotDTO.from_payload(
            {
                'schema_kind': 'snapshot_dto',
                'schema_version': '1.0',
                'snapshot_name': 'scheduler_decision',
                'as_of_utc': utc_now(),
                'payload': {
                    'schedule_key': str(scheduler_schedule_key),
                    'decision': scheduler_decision,
                    'state_path': str(state_path),
                },
            }
        )
        state_repo.write_scheduler_decision(base, run_id, scheduler_snapshot.to_payload())
        audit_helper.audit('write', 'write_scheduler_decision', run_id=run_id)
    except SchemaValidationError as e:
        audit_helper.fail_schema_validation(stage='snapshot_dto', exc=e, run_id=run_id)
    except Exception:
        pass

    for acct in args.accounts:
        acct = str(acct).strip()
        if not acct:
            continue
        outcome = run_one_account(
            request=AccountRunRequest(
                acct=acct,
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
            ),
            runlog=runlog,
            audit_fn=audit_helper.audit,
            fail_schema_validation=lambda *, stage, exc, run_id=None: audit_helper.fail_schema_validation(
                stage=stage,
                exc=exc,
                run_id=run_id,
            ),
        )
        prefetch_done = bool(outcome.prefetch_done)
        ran_any_pipeline = bool(ran_any_pipeline or outcome.ran_pipeline)
        tick_metrics['accounts'].append(outcome.acct_metrics)
        results.append(outcome.result)

    if ran_any_pipeline:
        try:
            request_scheduler_update(
                runner=run_scan_scheduler_cli,
                vpy=vpy,
                base=base,
                config=cfg_path,
                state=state_path,
                state_dir=run_repo.get_run_state_dir(base, run_id),
                mark_scanned=True,
                schedule_key=str(scheduler_schedule_key),
                capture_output=False,
            )
        except Exception:
            pass

    runlog.safe_event(
        'notify',
        'prepare',
        data=_safe_runlog_data({
            'results_count': len(results),
            'notify_candidates': len(engine_filter_notify_candidates(results)),
        }),
    )

    cash_footer_lines: list[str] = []
    try:
        cfg = base_cfg or {}
        cfg_market = str((cfg.get('portfolio') or {}).get('broker') or '富途')
        notif_cfg = (cfg.get('notifications') or {}) if isinstance(cfg, dict) else {}
        accts = cash_footer_accounts_from_config(cfg)
        timeout_sec = int(notif_cfg.get('cash_footer_timeout_sec') or 180)
        max_age_sec = int(notif_cfg.get('cash_snapshot_max_age_sec') or 900)
        cash_footer_lines = query_cash_footer(
            base,
            config_path=str(cfg_path),
            market=cfg_market,
            accounts=accts,
            timeout_sec=timeout_sec,
            snapshot_max_age_sec=max_age_sec,
        )
    except Exception:
        cash_footer_lines = []

    now_bj = bj_now()
    notify_candidates = rank_notify_candidates(engine_filter_notify_candidates(results))
    try:
        prepared_messages = prepare_multi_account_messages(
            notify_candidates=notify_candidates,
            results=results,
            now_bj=now_bj,
            cash_footer_lines=cash_footer_lines,
            cash_footer_for_account_fn=cash_footer_for_account,
            build_account_message_fn=build_account_message,
            build_account_messages_fn=build_account_messages,
            build_no_candidate_account_messages_fn=build_no_candidate_account_messages,
            as_of_utc=utc_now(),
            snapshot_cls=SnapshotDTO,
            engine_entrypoint=resolve_multi_tick_engine_entrypoint,
        )
    except SchemaValidationError as e:
        audit_helper.fail_schema_validation(stage='account_messages_snapshot', exc=e, run_id=run_id)
    account_messages = prepared_messages.account_messages

    if not bool(prepared_messages.threshold_met):
        return finalize_no_account_notification(
            base=base,
            run_id=run_id,
            runlog=runlog,
            results=results,
            tick_metrics=tick_metrics,
            no_send=no_send,
            state_repo=state_repo,
            utc_now_fn=utc_now,
            audit_fn=audit_helper.audit,
            safe_data_fn=_safe_runlog_data,
            on_success=audit_helper.guard_mark_success,
        )

    if prepared_messages.used_heartbeat:
        runlog.safe_event(
            'notify',
            'prepare',
            message='no candidates; sending monitor heartbeat',
            data=_safe_runlog_data({'accounts': list(account_messages.keys())}),
        )
        try:
            for acct in account_messages:
                for acct_metrics in tick_metrics.get('accounts', []):
                    if str(acct_metrics.get('account') or '').strip().lower() == str(acct).strip().lower():
                        acct_metrics['meaningful'] = True
                        acct_metrics['notification_type'] = 'no_candidate'
        except Exception:
            pass

    notify_route = resolve_notification_route_from_config(config=base_cfg)
    notif_cfg = notify_route.get('notifications') or {}
    channel = notify_route.get('channel')
    target = notify_route.get('target')
    schedule_cfg0 = base_cfg.get('schedule') or {}
    schedule_v2_enabled = bool((schedule_cfg0.get('schedule_v2') or {}).get('enabled', False))
    quiet_hours = notif_cfg.get('quiet_hours_beijing')
    dnd_decision = evaluate_dnd_quiet_hours(
        schedule_v2_enabled=schedule_v2_enabled,
        quiet_hours=quiet_hours,
        no_send=no_send,
        now_bj_time=datetime.now(timezone.utc).astimezone(bj_tz).time(),
        parse_hhmm_fn=parse_hhmm,
    )
    parse_error = dnd_decision.get('parse_error')
    if parse_error:
        runlog.safe_event('notify', 'error', message=f'failed to parse quiet_hours: {parse_error}')

    try:
        notify_delivery, delivery_plan, target = build_multi_account_delivery(
            channel=channel,
            target=target,
            account_messages=account_messages,
            no_send=no_send,
            is_quiet=bool(dnd_decision.get('is_quiet')),
            quiet_window=str(dnd_decision.get('quiet_window') or ''),
            decision_builder=decide_notification_delivery,
        )
    except ValueError as err:
        runlog.safe_event('notify', 'error', error_code='CONFIG_ERROR', message=str(err))
        raise SystemExit(f'[CONFIG_ERROR] {err}')
    except SchemaValidationError as e:
        audit_helper.fail_schema_validation(stage='delivery_plan', exc=e, run_id=run_id)
    audit_helper.audit(
        'notify',
        'delivery_decision',
        run_id=run_id,
        status=('ok' if not notify_delivery.get('config_error') else 'error'),
        target=(str(target) if target else None),
        extra={'reason': notify_delivery.get('reason'), 'should_send': bool(notify_delivery.get('should_send'))},
    )
    if str(notify_delivery.get('action') or '') == 'skip_quiet_hours':
        quiet_window = str(notify_delivery.get('quiet_window') or '')
        runlog.safe_event('notify', 'skip', message=f'in quiet hours ({quiet_window})')
        print(f"[SKIP] Currently in quiet hours (DND). Target was: {target}")
        audit_helper.guard_mark_success()
        return 0

    sent_accounts: list[str] = []
    notify_failures: list[dict[str, object]] = []
    if bool(notify_delivery.get('should_send')):
        assert delivery_plan is not None
        execution = execute_multi_account_delivery(
            delivery_plan=delivery_plan,
            run_id=run_id,
            runlog=runlog,
            audit_fn=audit_helper.audit,
            safe_data_fn=_safe_runlog_data,
            send_fn=lambda **kwargs: send_feishu_app_message_process(
                **kwargs,
                notifications=notify_route.get('notifications') or {},
            ),
            normalize_fn=normalize_feishu_app_send_output,
            failure_fields_builder=build_failure_audit_fields,
            on_failure=lambda error_code: audit_helper.guard_mark_failure(error_code, 'send_feishu_app_message'),
            base=base,
        )
        sent_accounts = execution.sent_accounts
        notify_failures = execution.notify_failures
    else:
        sent_accounts = list(account_messages.keys())
        runlog.safe_event('notify', 'skip', message='no_send mode')

    if not no_send:
        try:
            mark_accounts_notified(
                runner=run_scan_scheduler_cli,
                vpy=vpy,
                base=base,
                config=cfg_path,
                state=state_path,
                state_dir=run_repo.get_run_state_dir(base, run_id),
                schedule_key=str(scheduler_schedule_key),
                accounts=sent_accounts,
            )
        except Exception:
            pass

    try:
        notify_summary = build_notify_summary(
            sent_accounts=sent_accounts,
            notify_failures=notify_failures,
            total_accounts=len(account_messages),
        )
        apply_notify_results_to_tick_metrics(
            tick_metrics=tick_metrics,
            no_send=no_send,
            sent_accounts=sent_accounts,
            notify_failures=notify_failures,
            notify_summary=notify_summary,
        )
        state_repo.write_tick_metrics(base, run_id, tick_metrics)
        state_repo.append_tick_metrics_history(base, run_id, tick_metrics)
        audit_helper.audit('write', 'write_tick_metrics', run_id=run_id, extra={'sent': bool(tick_metrics.get('sent'))})
    except Exception:
        pass
    return finalize_multi_tick_run(
        base=base,
        run_id=run_id,
        runlog=runlog,
        results=results,
        tick_metrics=tick_metrics,
        no_send=no_send,
        sent_accounts=sent_accounts,
        notify_failures=notify_failures,
        notify_summary=notify_summary,
        channel=(str(channel) if channel else None),
        target=(str(target) if target else None),
        state_repo=state_repo,
        read_json_fn=read_json,
        shared_state_dir_getter=state_repo.shared_state_dir,
        utc_now_fn=utc_now,
        audit_fn=audit_helper.audit,
        safe_data_fn=_safe_runlog_data,
        on_success=audit_helper.guard_mark_success,
    )


__all__ = ['main', '_CURRENT_RUN_ID']
