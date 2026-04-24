from __future__ import annotations

import argparse
import json
import os
import subprocess
from hashlib import sha256
from time import monotonic
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    from scripts.run_log import RunLogger
except Exception:
    from run_log import RunLogger

from scripts.io_utils import (
    read_json,
    parse_last_json_obj,
    utc_now,
    bj_now,
)
from scripts.account_config import accounts_from_config, cash_footer_accounts_from_config
from scripts.config_loader import resolve_watchlist_config, set_watchlist_config
from domain.domain.fetch_source import is_futu_fetch_source, resolve_symbol_fetch_source

from .cash_footer import query_cash_footer
from .notify_format import build_account_message
from .opend_guard import (
    mark_opend_phone_verify_pending,
    clear_opend_phone_verify_pending,
    is_opend_phone_verify_pending,
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
    apply_scan_run_decision,
    build_account_messages,
    build_no_candidate_account_messages,
    build_no_account_notification_payloads,
    build_shared_last_run_payload,
    cash_footer_for_account,
    evaluate_dnd_quiet_hours,
    classify_failure,
    ensure_runtime_canonical_config,
    normalize_notify_subprocess_output,
    normalize_subprocess_adapter_payload,
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
    build_run_end_payload,
    build_shared_last_run_meta,
    mark_accounts_notified,
    request_scheduler_update,
)
from src.application.account_run import AccountRunRequest, run_one_account
from src.application.scheduled_notification import (
    build_multi_account_delivery,
    build_multi_tick_account_scheduler_view,
    build_multi_tick_scheduler_decision,
    execute_multi_account_delivery,
    prepare_multi_account_messages,
)
from scripts.infra.service import (
    run_opend_watchdog,
    run_scan_scheduler_cli,
    send_openclaw_message,
    trading_day_via_futu,
)

try:
    from domain.storage import paths as storage_paths
    from domain.storage.repositories import run_repo, state_repo
except Exception:
    from scripts.domain.storage import paths as storage_paths  # type: ignore
    from scripts.domain.storage.repositories import run_repo, state_repo  # type: ignore


_CURRENT_RUN_ID: str | None = None
SCHEMA_VALIDATION_ERROR_CODE = 'SCHEMA_VALIDATION_FAILED'


def current_run_id() -> str | None:
    """Public accessor for wrapper-level error logging compatibility."""
    return _CURRENT_RUN_ID


def _fail_schema_validation(*, runlog: RunLogger, audit_fn, stage: str, exc: BaseException, run_id: str | None = None) -> None:
    msg = f"{stage}: {type(exc).__name__}: {exc}"
    runlog.safe_event('contract', 'error', error_code=SCHEMA_VALIDATION_ERROR_CODE, message=msg)
    failure_fields = build_failure_audit_fields(
        failure_kind='decision_error',
        failure_stage=str(stage),
    )
    try:
        audit_fn(
            'contract',
            f'validate_{stage}',
            run_id=run_id,
            status='error',
            error_code=SCHEMA_VALIDATION_ERROR_CODE,
            message=msg,
            **failure_fields,
        )
    except Exception:
        pass
    raise SystemExit(f'[CONTRACT_ERROR] {msg}')


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
    guard_failure_recorded = False

    def _guard_mark_failure(error_code: str, stage: str) -> None:
        nonlocal guard_failure_recorded
        if guard_failure_recorded:
            return
        try:
            g = record_project_failure(
                base,
                base_cfg,
                error_code=str(error_code),
                stage=str(stage),
            )
            runlog.safe_event(
                'project_guard',
                ('open' if bool(g.get('opened')) else 'record_failure'),
                error_code=str(error_code),
                data=_safe_runlog_data({
                    'stage': str(stage),
                    'state': g.get('state'),
                    'failure_count': g.get('failure_count'),
                    'open_until_utc': g.get('open_until_utc'),
                }),
            )
        except Exception:
            pass
        guard_failure_recorded = True

    def _guard_mark_success() -> None:
        if guard_failure_recorded:
            return
        try:
            g = record_project_success(base, base_cfg)
            if bool(g.get('closed')):
                runlog.safe_event('project_guard', 'closed', data=_safe_runlog_data({'state': g.get('state')}))
        except Exception:
            pass

    market_cfg = str(getattr(args, 'market_config', 'auto') or 'auto').lower()
    execution_bucket = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M')
    execution_idempotency_key = sha256(
        (
            f"{cfg_path.resolve()}|{market_cfg}|"
            f"{','.join(sorted([str(a).strip().lower() for a in (args.accounts or []) if str(a).strip()]))}|"
            f"{execution_bucket}"
        ).encode('utf-8')
    ).hexdigest()

    def _audit(event_type: str, action: str, *, status: str = 'ok', run_id: str | None = None, account: str | None = None, **kwargs) -> None:
        try:
            payload = {
                'event_type': event_type,
                'action': action,
                'status': status,
                'run_id': run_id or runlog.run_id,
                'account': account,
                'idempotency_key': execution_idempotency_key,
            }
            payload.update(kwargs)
            state_repo.append_audit_event(base, payload, run_id=(run_id or runlog.run_id))
        except Exception:
            pass

    for it in syms0:
        if not isinstance(it, dict):
            continue
        sym = str(it.get('symbol') or '').strip().upper()
        if not sym:
            continue
        src, decision = resolve_symbol_fetch_source(it.get('fetch') or {})
        _audit(
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
        _audit(
            'idempotency',
            'skip_duplicate_tick',
            status='skip',
            message='duplicate tick in same execution bucket',
            extra={'bucket': execution_bucket},
        )
        runlog.safe_event('run_end', 'skip', message='duplicate tick execution skipped')
        return 0
    _audit('idempotency', 'claim_tick_execution', extra={'bucket': execution_bucket})

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
                [it for it in syms if isinstance(it, dict) and (it.get('market') == market_cfg.upper())],
            )
        except Exception:
            pass

    schedule_cfg = base_cfg.get('schedule', {}) or {}
    bj_tz = ZoneInfo(schedule_cfg.get('beijing_timezone', 'Asia/Shanghai'))

    if bool(getattr(args, 'opend_phone_verify_continue', False)):
        clear_opend_phone_verify_pending(base)

    if is_opend_phone_verify_pending(base):
        _audit('guard', 'opend_phone_verify_pending', status='skip')
        runlog.safe_event('run_end', 'skip', message='opend phone verify pending; paused until user confirmation')
        _guard_mark_success()
        return 0

    t_watchdog0 = monotonic()
    runlog.safe_event('watchdog', 'start')
    _audit('tool_call', 'opend_watchdog_start', tool_name='opend_watchdog')
    try:
        need_opend = False
        ports = set()
        for sym in resolve_watchlist_config(base_cfg):
            fetch = (sym or {}).get('fetch') or {}
            if is_futu_fetch_source(fetch.get('source')):
                need_opend = True
                host = fetch.get('host') or '127.0.0.1'
                port = fetch.get('port') or 11111
                ports.add((str(host), int(port)))

        if need_opend:
            unhealthy = None
            watchdog_timed_out = False
            for host, port in sorted(ports):
                try:
                    wd0 = run_opend_watchdog(
                        vpy=vpy,
                        base=base,
                        host=str(host),
                        port=int(port),
                        ensure=True,
                        timeout_sec=35,
                    )
                    payload0 = parse_last_json_obj((wd0.stdout or '') + '\n' + (wd0.stderr or ''))
                    ok0 = bool(payload0.get('ok')) if payload0 else (wd0.returncode == 0)
                    _audit(
                        'tool_call',
                        'opend_watchdog_result',
                        status=('ok' if ok0 else 'error'),
                        tool_name='opend_watchdog',
                        extra={'host': str(host), 'port': int(port), 'returncode': int(wd0.returncode)},
                    )
                    if not ok0:
                        unhealthy = {
                            'host': host,
                            'port': port,
                            'payload': payload0,
                            'detail': ((wd0.stdout or '') + '\n' + (wd0.stderr or '')).strip(),
                        }
                        break
                except Exception as e:
                    watchdog_timed_out = isinstance(e, subprocess.TimeoutExpired)
                    classified = classify_failure(
                        exc=e,
                        upstream='opend',
                        error_code=('OPEND_TIMEOUT' if watchdog_timed_out else 'OPEND_API_ERROR'),
                        message=str(e),
                    )
                    unhealthy = {
                        'host': host,
                        'port': port,
                        'payload': {
                            'ok': False,
                            'error_code': str(classified.get('error_code') or 'OPEND_API_ERROR'),
                            'message': 'OpenD 看门狗执行失败',
                            'category': classified.get('category'),
                        },
                        'detail': f'{type(e).__name__}: {e}',
                    }
                    break

            if unhealthy is not None:
                payload = unhealthy.get('payload') or {}
                error_code = str(payload.get('error_code') or 'OPEND_API_ERROR')
                msg = str(payload.get('message') or payload.get('error') or 'OpenD 不健康')
                detail = str(unhealthy.get('detail') or '')
                host = unhealthy.get('host')
                port = unhealthy.get('port')

                opend_plan = resolve_multi_tick_engine_entrypoint(
                    opend_unhealthy={
                        'error_code': error_code,
                        'degraded': False,
                        'message_text': msg,
                        'detail_text': detail,
                        'host': host,
                        'port': port,
                    }
                ).get('watchdog') or build_opend_unhealthy_execution_plan(
                    error_code=error_code,
                    degraded=False,
                    message_text=msg,
                    detail_text=detail,
                    host=host,
                    port=port,
                )

                alert_message_text = str(opend_plan.get('alert_message_text') or msg)
                alert_detail = str(opend_plan.get('alert_detail') or detail)
                if bool(opend_plan.get('should_mark_phone_verify_pending')):
                    mark_opend_phone_verify_pending(
                        base,
                        detail=alert_detail,
                    )

                    send_opend_alert(
                        base,
                        base_cfg,
                        error_code=error_code,
                        message_text=alert_message_text,
                        detail=alert_detail,
                        no_send=no_send,
                    )

                    runlog.safe_event(
                        'run_end',
                        'skip',
                        error_code=error_code,
                        message='opend needs phone verify; paused until user confirmation',
                        data=_safe_runlog_data({'sent': False, 'reason': 'opend_phone_verify_pending'}),
                    )
                    _audit(
                        'notify',
                        'send_opend_alert',
                        status='error',
                        error_code=error_code,
                        message='opend needs phone verify; paused',
                        fallback_used=bool(opend_plan.get('fallback_used')),
                    )
                    return 0

                send_opend_alert(
                    base,
                    base_cfg,
                    error_code=error_code,
                    message_text=alert_message_text,
                    detail=alert_detail,
                    no_send=no_send,
                )
                _guard_mark_failure(error_code, 'opend_watchdog')

                now = utc_now()
                for acct in args.accounts:
                    acct0 = str(acct).strip().lower()
                    if not acct0:
                        continue
                    try:
                        state_repo.write_account_last_run(base, acct0, {
                            'last_run_utc': now,
                            'sent': False,
                            'reason': 'opend_unhealthy',
                            'error_code': error_code,
                            'detail': msg,
                        })
                        _audit('write', 'write_account_last_run', account=acct0, error_code=error_code)
                    except Exception:
                        pass

                runlog.safe_event(
                    'watchdog',
                    'error',
                    duration_ms=int((monotonic() - t_watchdog0) * 1000),
                    error_code=error_code,
                    message=msg,
                    data=_safe_runlog_data({'degraded': False, 'host': host, 'port': port}),
                )
                runlog.safe_event(
                    'run_end',
                    'error',
                    error_code=error_code,
                    message='opend watchdog unhealthy',
                    data=_safe_runlog_data({'sent': False, 'reason': 'opend_unhealthy'}),
                )
                _audit(
                    'fallback',
                    'opend_unhealthy_no_fallback',
                    status='error',
                    error_code=error_code,
                    fallback_used=bool(opend_plan.get('fallback_used')),
                    message=msg,
                )
                return 0

    except SystemExit:
        raise
    except Exception as e:
        _guard_mark_failure('WATCHDOG_EXCEPTION', 'opend_watchdog')
        runlog.safe_event(
            'watchdog',
            'error',
            duration_ms=int((monotonic() - t_watchdog0) * 1000),
            error_code='WATCHDOG_EXCEPTION',
            message=str(e),
        )
    runlog.safe_event('watchdog', 'ok', duration_ms=int((monotonic() - t_watchdog0) * 1000))

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
    markets_to_run: list[str] = domain_select_markets_to_run(now_utc, base_cfg, getattr(args, 'market_config', 'auto'))

    if force_mode:
        print("force: bypass guard")
        runlog.safe_event(
            'trading_day_guard',
            'skip',
            message='force: bypass guard',
            data=_safe_runlog_data({'markets_to_run': markets_to_run, 'market_config': str(getattr(args, 'market_config', 'auto') or 'auto')}),
        )
    else:
        guard_markets = domain_markets_for_trading_day_guard(markets_to_run, base_cfg, getattr(args, 'market_config', 'auto'))
        guard_decision = decide_trading_day_guard(
            markets_to_run=markets_to_run,
            guard_markets=guard_markets,
            check_trading_day_for_market=lambda gm: _is_trading_day_guard_for_market(base_cfg, gm),
            reduce_guard_fn=reduce_trading_day_guard,
        )
        guard_results = list(guard_decision.get('guard_results') or [])
        for item in guard_results:
            gm_used = str(item.get('market') or '')
            is_td = item.get('is_trading_day')
            log(f"[TRADING_DAY_GUARD] market={gm_used} result={is_td}")

        runlog.safe_event(
            'trading_day_guard',
            'check',
            data=_safe_runlog_data({'results': guard_results, 'markets_to_run': markets_to_run, 'market_config': str(getattr(args, 'market_config', 'auto') or 'auto')}),
        )

        markets_to_run = list(guard_decision.get('markets_to_run') or [])
        if bool(guard_decision.get('should_skip')):
            runlog.safe_event('run_end', 'skip', message=str(guard_decision.get('skip_message') or ''))
            _guard_mark_success()
            return 0

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
        adapter='scheduler',
        tool_name='scan_scheduler_cli',
        returncode=scheduler_proc.returncode,
        stdout=scheduler_proc.stdout,
        stderr=scheduler_proc.stderr,
        message='scan_scheduler_cli completed',
    )
    scheduler_ms = int((monotonic() - t_sch0) * 1000)
    scheduler_extra = {
        'duration_ms': scheduler_ms,
        'returncode': int(scheduler_proc.returncode),
    }
    if not bool(scheduler_tool_dto.get('ok')):
        scheduler_extra.update(
            build_failure_audit_fields(
                failure_kind='io_error',
                failure_stage='scan_scheduler',
                failure_adapter=str(scheduler_tool_dto.get('adapter') or 'scheduler'),
            )
        )
    _audit(
        'tool_call',
        'scan_scheduler',
        status=('ok' if scheduler_proc.returncode == 0 else 'error'),
        tool_name='scan_scheduler_cli',
        extra=scheduler_extra,
    )
    if not bool(scheduler_tool_dto.get('ok')):
        err = f"scheduler error: {(scheduler_proc.stderr or scheduler_proc.stdout).strip()}"
        for acct in args.accounts:
            acct0 = str(acct).strip()
            if acct0:
                results.append(AccountResult(acct0, False, False, err, ''))
        runlog.safe_event('run_end', 'error', error_code='SCHEDULER_FAILED', message=err)
        _guard_mark_failure('SCHEDULER_FAILED', 'scan_scheduler')
        return 0

    try:
        scheduler_decision, scheduler_view = build_multi_tick_scheduler_decision(
            scheduler_stdout=str(scheduler_proc.stdout or ''),
            as_of_utc=utc_now(),
            snapshot_cls=SnapshotDTO,
            engine_entrypoint=resolve_multi_tick_engine_entrypoint,
        )
    except SchemaValidationError as e:
        _fail_schema_validation(runlog=runlog, audit_fn=_audit, stage='scheduler_decision', exc=e)
    except Exception as e:
        _fail_schema_validation(runlog=runlog, audit_fn=_audit, stage='scheduler_parse', exc=e)
    should_run_global = bool(scheduler_view.should_run_scan)
    reason_global = str(scheduler_view.reason)

    notify_decision_by_account: dict[str, AccountSchedulerDecisionView | None] = {}
    for acct0 in [str(a).strip() for a in (args.accounts or []) if str(a).strip()]:
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
                    scheduler_stdout=str(sch_acct.stdout or ''),
                    scheduler_decision=scheduler_decision,
                    as_of_utc=utc_now(),
                    snapshot_cls=SnapshotDTO,
                    engine_entrypoint=resolve_multi_tick_engine_entrypoint,
                    account_view_cls=AccountSchedulerDecisionView,
                )
                if sch_acct.returncode == 0
                else None
            )
        except SchemaValidationError as e:
            _fail_schema_validation(runlog=runlog, audit_fn=_audit, stage='account_scheduler_decision', exc=e)
        except Exception:
            notify_decision_by_account[acct0] = None

    should_run_global, reason_global = apply_scan_run_decision(
        should_run_global=should_run_global,
        reason_global=reason_global,
        force_mode=force_mode,
        smoke=smoke,
    )

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
        _audit('write', 'write_scheduler_decision', run_id=run_id)
    except SchemaValidationError as e:
        _fail_schema_validation(runlog=runlog, audit_fn=_audit, stage='snapshot_dto', exc=e, run_id=run_id)
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
            ),
            runlog=runlog,
            audit_fn=_audit,
            fail_schema_validation=lambda *, stage, exc, run_id=None: _fail_schema_validation(
                runlog=runlog,
                audit_fn=_audit,
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
        cfg_market = str((cfg.get('portfolio') or {}).get('market') or '富途')
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
        _fail_schema_validation(runlog=runlog, audit_fn=_audit, stage='account_messages_snapshot', exc=e, run_id=run_id)
    account_messages = prepared_messages.account_messages

    if not bool(prepared_messages.threshold_met):
        runlog.safe_event('notify', 'skip', message='no account notification content')

        shared_payload, account_payloads = build_no_account_notification_payloads(
            now_utc_fn=utc_now,
            results=results,
            run_dir=str(run_dir),
        )
        try:
            state_repo.write_shared_last_run(base, shared_payload)
            _audit('write', 'write_shared_last_run', run_id=run_id, status='skip', message='no_account_notification')
        except Exception:
            pass

        try:
            for r in results:
                payload = account_payloads.get(str(r.account), {})
                state_repo.write_account_last_run(base, r.account, payload)
                state_repo.write_run_account_last_run(base, run_id, r.account, payload)
                _audit('write', 'write_account_last_run', run_id=run_id, account=str(r.account), status='skip', message='no_account_notification')
        except Exception:
            pass

        try:
            tick_metrics['sent'] = False
            tick_metrics['reason'] = 'no_account_notification'
            state_repo.write_tick_metrics(base, run_id, tick_metrics)
            state_repo.append_tick_metrics_history(base, run_id, tick_metrics)
            _audit('write', 'write_tick_metrics', run_id=run_id, status='skip', message='no_account_notification')
        except Exception:
            pass

        runlog.safe_event(
            'run_end',
            'ok',
            data=_safe_runlog_data(
                build_run_end_payload(
                    no_send=no_send,
                    results=results,
                    sent_accounts=[],
                    reason='no_account_notification',
                )
            ),
        )
        _guard_mark_success()
        return 0

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
        _fail_schema_validation(runlog=runlog, audit_fn=_audit, stage='delivery_plan', exc=e, run_id=run_id)
    _audit(
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
        _guard_mark_success()
        return 0

    sent_accounts: list[str] = []
    notify_failures: list[dict[str, object]] = []
    if bool(notify_delivery.get('should_send')):
        assert delivery_plan is not None
        execution = execute_multi_account_delivery(
            delivery_plan=delivery_plan,
            run_id=run_id,
            runlog=runlog,
            audit_fn=_audit,
            safe_data_fn=_safe_runlog_data,
            send_fn=send_openclaw_message,
            normalize_fn=normalize_notify_subprocess_output,
            failure_fields_builder=build_failure_audit_fields,
            on_failure=lambda error_code: _guard_mark_failure(error_code, 'send_openclaw_message'),
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
        _audit('write', 'write_tick_metrics', run_id=run_id, extra={'sent': bool(tick_metrics.get('sent'))})
    except Exception:
        pass

    try:
        last_run_path = (state_repo.shared_state_dir(base) / 'last_run.json').resolve()
        prev = read_json(last_run_path, {})
        run_meta = build_shared_last_run_meta(
            now_utc=utc_now(),
            channel=channel,
            target=target,
            results=results,
            sent_accounts=sent_accounts,
            notify_failures=notify_failures,
            notify_summary=notify_summary,
        )
        state_repo.write_shared_last_run(
            base,
            build_shared_last_run_payload(prev_payload=prev, run_meta=run_meta, history_limit=20),
        )
        _audit('write', 'write_shared_last_run', run_id=run_id, extra={'sent_accounts': list(sent_accounts)})
    except Exception:
        pass

    if notify_failures:
        runlog.safe_event(
            'run_end',
            'error',
            error_code=('NOTIFY_PARTIAL_FAILED' if sent_accounts else 'NOTIFY_FAILED'),
            data=_safe_runlog_data(
                build_run_end_payload(
                    no_send=no_send,
                    results=results,
                    sent_accounts=sent_accounts,
                    notify_failures=notify_failures,
                    notify_summary=notify_summary,
                )
            ),
        )
        return 1

    runlog.safe_event(
        'run_end',
        'ok',
        data=_safe_runlog_data(
            build_run_end_payload(
                no_send=no_send,
                results=results,
                sent_accounts=sent_accounts,
                notify_summary=notify_summary,
            )
        ),
    )
    _guard_mark_success()
    return 0


__all__ = ['main', '_CURRENT_RUN_ID']
