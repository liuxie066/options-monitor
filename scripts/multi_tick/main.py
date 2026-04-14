from __future__ import annotations

import argparse
import json
import os
import subprocess
from hashlib import sha256
from time import monotonic
from datetime import datetime, timedelta, timezone
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

from .cash_footer import query_cash_footer
from .required_data_prefetch import prefetch_required_data
from .notify_format import (
    flatten_auto_close_summary,
    build_account_message,
)
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
    maybe_parse_dt,
    atomic_symlink,
    ensure_account_output_dir,
    AccountResult,
    _safe_runlog_data,
)
from domain.domain import (
    Decision,
    DeliveryPlan,
    SchemaValidationError,
    SnapshotDTO,
    apply_scan_run_decision,
    build_account_messages,
    build_no_account_notification_payloads,
    build_shared_last_run_payload,
    cash_footer_for_account,
    decide_notify_dispatch,
    decide_should_notify,
    evaluate_dnd_quiet_hours,
    classify_failure,
    ensure_runtime_canonical_config,
    normalize_notify_subprocess_output,
    normalize_pipeline_subprocess_output,
    normalize_subprocess_adapter_payload,
    resolve_config_contract,
    resolve_allow_derived_config_gate,
    markets_for_trading_day_guard as domain_markets_for_trading_day_guard,
    reduce_trading_day_guard,
    resolve_notification_route_from_config,
    select_markets_to_run as domain_select_markets_to_run,
    select_scheduler_state_filename,
)
from domain.domain.engine import (
    AccountSchedulerDecisionView,
    apply_opend_degrade_to_yahoo,
    build_opend_unhealthy_execution_plan,
    decide_account_scan_gate,
    decide_notify_delivery_action,
    decide_pipeline_execution_result,
    decide_notification_meaningful,
    decide_trading_day_guard,
    build_failure_audit_fields,
    filter_notify_candidates as engine_filter_notify_candidates,
    rank_notify_candidates,
    resolve_multi_tick_engine_entrypoint,
)
from scripts.infra.service import (
    run_opend_watchdog,
    run_pipeline_script,
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


def _select_markets_to_run(now_utc: datetime, cfg: dict, market_config: str) -> list[str]:
    return domain_select_markets_to_run(now_utc, cfg, market_config)


def _markets_for_trading_day_guard(markets_to_run: list[str], cfg: dict, market_config: str) -> list[str]:
    return domain_markets_for_trading_day_guard(markets_to_run, cfg, market_config)


def _is_trading_day_guard_for_market(cfg: dict, market: str) -> tuple[bool | None, str]:
    """Return (is_trading_day, market_used) for one market.

    None means guard check failed and caller should continue without blocking.
    """
    return trading_day_via_futu(cfg, market)


def main() -> int:
    ap = argparse.ArgumentParser(description='Multi-account tick with merged notification')
    ap.add_argument('--config', default='config.us.json')
    ap.add_argument('--accounts', nargs='+', required=True)
    ap.add_argument('--default-account', default='lx')
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
    allow_derived_gate = resolve_allow_derived_config_gate(
        os.environ.get('OM_ALLOW_DERIVED_CONFIG', ''),
    )
    allow_derived_config = bool(allow_derived_gate.get('allow_derived'))
    allow_derived_error_code = str(allow_derived_gate.get('error_code') or '').strip()
    allow_derived_message = str(allow_derived_gate.get('message') or '').strip()
    allow_derived_migration_hint = str(allow_derived_gate.get('migration_hint') or '').strip()
    allow_derived_raw = str(allow_derived_gate.get('raw') or '').strip()
    if allow_derived_error_code:
        runlog.safe_event(
            'config_guard',
            'warn',
            error_code=allow_derived_error_code,
            message=allow_derived_message,
            data=_safe_runlog_data(
                {
                    'raw': allow_derived_raw,
                    'migration_hint': allow_derived_migration_hint,
                }
            ),
        )
    contract_info = resolve_config_contract(cfg_path, str(getattr(args, 'market_config', 'auto') or 'auto'))
    if allow_derived_config and (not contract_info.get('is_canonical', False) or not contract_info.get('market_match', False)):
        runlog.safe_event(
            'config_guard',
            'warn',
            error_code='OM_ALLOW_DERIVED_CONFIG_ENABLED',
            message='OM_ALLOW_DERIVED_CONFIG enabled for non-canonical or market-mismatch runtime config.',
            data=_safe_runlog_data(contract_info),
        )
    ensure_runtime_canonical_config(
        cfg_path,
        str(getattr(args, 'market_config', 'auto') or 'auto'),
        allow_derived=allow_derived_config,
    )
    base_cfg = json.loads(cfg_path.read_text(encoding='utf-8'))

    syms0 = base_cfg.get('symbols') or []
    src_counts: dict[str, int] = {}
    for it in syms0:
        if not isinstance(it, dict):
            continue
        src = str(((it.get('fetch') or {}).get('source') or 'yahoo')).lower()
        src_counts[src] = src_counts.get(src, 0) + 1
    runlog.safe_event(
        'run_start',
        'start',
        data=_safe_runlog_data({
            'accounts': [str(a).strip().lower() for a in (args.accounts or []) if str(a).strip()],
            'symbols_count': len([x for x in syms0 if isinstance(x, dict)]),
            'source_selections': src_counts,
            'market_config': str(getattr(args, 'market_config', 'auto') or 'auto'),
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
            syms = base_cfg.get('symbols') or []
            base_cfg['symbols'] = [it for it in syms if isinstance(it, dict) and (it.get('market') == market_cfg.upper())]
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
        fetch_policy = base_cfg.get('fetch_policy') if isinstance(base_cfg, dict) else None
        allow_downgrade = True
        try:
            if isinstance(fetch_policy, dict) and ('allow_downgrade_to_yahoo' in fetch_policy):
                allow_downgrade = bool(fetch_policy.get('allow_downgrade_to_yahoo'))
        except Exception:
            allow_downgrade = True

        need_opend = False
        ports = set()
        has_hk_opend = False
        for sym in (base_cfg.get('symbols') or []):
            fetch = (sym or {}).get('fetch') or {}
            if str(fetch.get('source') or '').lower() == 'opend':
                need_opend = True
                host = fetch.get('host') or '127.0.0.1'
                port = fetch.get('port') or 11111
                ports.add((str(host), int(port)))
                if str((sym or {}).get('market') or '').upper() == 'HK':
                    has_hk_opend = True

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

                degraded = apply_opend_degrade_to_yahoo(
                    symbols=(base_cfg.get('symbols') or []),
                    allow_downgrade=allow_downgrade,
                    has_hk_opend=has_hk_opend,
                    watchdog_timed_out=watchdog_timed_out,
                )
                opend_plan = resolve_multi_tick_engine_entrypoint(
                    opend_unhealthy={
                        'error_code': error_code,
                        'degraded': degraded,
                        'message_text': msg,
                        'detail_text': detail,
                        'host': host,
                        'port': port,
                    }
                ).get('watchdog') or build_opend_unhealthy_execution_plan(
                    error_code=error_code,
                    degraded=degraded,
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

                if bool(opend_plan.get('should_continue')):
                    log(f"[WARN] OpenD unhealthy ({error_code}); degraded US opend sources to yahoo for this run")
                    runlog.safe_event(
                        'watchdog',
                        'degraded',
                        duration_ms=int((monotonic() - t_watchdog0) * 1000),
                        error_code=error_code,
                        message=msg,
                        data=_safe_runlog_data({'degraded': True, 'host': host, 'port': port}),
                    )
                    _audit(
                        'fallback',
                        'degrade_opend_to_yahoo',
                        status='ok',
                        error_code=error_code,
                        fallback_used=bool(opend_plan.get('fallback_used')),
                        message=msg,
                    )
                else:
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

    out_link = base / 'output'
    if not out_link.exists():
        dst = accounts_root / args.default_account
        ensure_account_output_dir(dst)
        out_link.symlink_to(dst, target_is_directory=True)
    if not out_link.is_symlink():
        raise SystemExit(f"./output must be a symlink for multi-account mode: {out_link}")

    results: list[AccountResult] = []

    now_utc = datetime.now(timezone.utc)
    markets_to_run: list[str] = _select_markets_to_run(now_utc, base_cfg, getattr(args, 'market_config', 'auto'))

    if force_mode:
        print("force: bypass guard")
        runlog.safe_event(
            'trading_day_guard',
            'skip',
            message='force: bypass guard',
            data=_safe_runlog_data({'markets_to_run': markets_to_run, 'market_config': str(getattr(args, 'market_config', 'auto') or 'auto')}),
        )
    else:
        guard_markets = _markets_for_trading_day_guard(markets_to_run, base_cfg, getattr(args, 'market_config', 'auto'))
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
                results.append(AccountResult(acct0, False, False, False, err, ''))
        runlog.safe_event('run_end', 'error', error_code='SCHEDULER_FAILED', message=err)
        _guard_mark_failure('SCHEDULER_FAILED', 'scan_scheduler')
        return 0

    try:
        scheduler_raw = json.loads((scheduler_proc.stdout or '').strip())
        scheduler_input_snapshot = SnapshotDTO.from_payload(
            {
                'schema_kind': 'snapshot_dto',
                'schema_version': '1.0',
                'snapshot_name': 'scheduler_raw',
                'as_of_utc': utc_now(),
                'payload': {'scheduler_raw': scheduler_raw},
            }
        )
        scheduler_payload = scheduler_input_snapshot.payload.get('scheduler_raw')
        if not isinstance(scheduler_payload, dict):
            raise SchemaValidationError('scheduler_raw must be a dict')
        scheduler_bundle = resolve_multi_tick_engine_entrypoint(
            scheduler_raw=scheduler_payload,
        ).get('scheduler') or {}
        scheduler_decision = scheduler_bundle.get('scheduler_decision')
        scheduler_view = scheduler_bundle.get('scheduler_view')
        if not isinstance(scheduler_decision, dict) or scheduler_view is None:
            raise SchemaValidationError('scheduler decision engine entrypoint returned invalid payload')
    except SchemaValidationError as e:
        _fail_schema_validation(runlog=runlog, audit_fn=_audit, stage='scheduler_decision', exc=e)
    except Exception as e:
        _fail_schema_validation(runlog=runlog, audit_fn=_audit, stage='scheduler_parse', exc=e)
    should_run_global = bool(scheduler_view.should_run_scan)
    reason_global = str(scheduler_view.reason)

    notify_decision_by_account: dict[str, AccountSchedulerDecisionView | None] = {}
    for acct0 in [str(a).strip() for a in (args.accounts or []) if str(a).strip()]:
        account_scheduler_decision_view: AccountSchedulerDecisionView | None = None
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
            if sch_acct.returncode == 0:
                account_scheduler_raw = json.loads((sch_acct.stdout or '').strip())
                account_scheduler_bundle = resolve_multi_tick_engine_entrypoint(
                    scheduler_raw=scheduler_decision,
                    account_scheduler_raw_by_account={str(acct0): account_scheduler_raw},
                ).get('scheduler') or {}
                account_scheduler_decision_dto = (account_scheduler_bundle.get('account_scheduler_decisions') or {}).get(str(acct0))
                account_scheduler_snapshot = SnapshotDTO.from_payload(
                    {
                        'schema_kind': 'snapshot_dto',
                        'schema_version': '1.0',
                        'snapshot_name': f'account_scheduler_decision:{acct0}',
                        'as_of_utc': utc_now(),
                        'payload': {
                            'account': str(acct0),
                            'decision': account_scheduler_decision_dto,
                        },
                    }
                )
                account_decision_payload = account_scheduler_snapshot.payload.get('decision')
                if not isinstance(account_decision_payload, dict):
                    raise SchemaValidationError('account scheduler decision must be a dict')
                account_scheduler_decision_view = (account_scheduler_bundle.get('account_scheduler_views') or {}).get(str(acct0))
                if not isinstance(account_scheduler_decision_view, AccountSchedulerDecisionView):
                    raise SchemaValidationError('account scheduler decision view must be valid')
        except SchemaValidationError as e:
            _fail_schema_validation(runlog=runlog, audit_fn=_audit, stage='account_scheduler_decision', exc=e)
        except Exception:
            account_scheduler_decision_view = None
        notify_decision_by_account[acct0] = account_scheduler_decision_view

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

        acct_out = accounts_root / acct
        acct_metrics = {
            'account': acct,
            'scheduler_ms': scheduler_ms,
            'pipeline_ms': None,
            'ran_scan': False,
            'should_notify': False,
            'meaningful': False,
            'reason': '',
        }
        ensure_account_output_dir(acct_out)

        atomic_symlink(out_link, acct_out)

        cfg = json.loads(json.dumps(base_cfg))
        cfg.setdefault('portfolio', {})
        cfg['portfolio']['account'] = acct

        try:
            syms = cfg.get('symbols') or []
            if markets_to_run:
                syms = [it for it in syms if isinstance(it, dict) and (it.get('market') in markets_to_run)]
            cfg['symbols'] = syms
        except Exception:
            pass
        cfg_override = state_repo.write_account_state_json_text(
            base,
            acct,
            'config.override.json',
            cfg,
        )
        _audit('write', 'write_account_state_json_text:config.override.json', run_id=run_id, account=acct)

        acct_report_dir = run_repo.get_run_account_dir(base, run_id, acct)
        acct_state_dir = run_repo.get_run_account_state_dir(base, run_id, acct)
        try:
            run_repo.ensure_run_account_state_dir(base, run_id, acct)
        except Exception:
            pass

        def _write_acct_run_state(name: str, payload: dict):
            try:
                state_repo.write_account_run_state(base, run_id, acct, name, payload)
                _audit('write', f'write_account_run_state:{name}', run_id=run_id, account=acct)
            except Exception:
                pass

        notif_path = (acct_report_dir / 'symbols_notification.txt').resolve()

        should_notify_raw = decide_should_notify(
            account=acct,
            notify_decision_by_account=notify_decision_by_account,
            scheduler_decision=scheduler_view,
        )
        try:
            decision = Decision.from_payload(
                {
                    'schema_kind': 'decision',
                    'schema_version': '1.0',
                    'account': acct,
                    'should_run': bool(should_run_global),
                    'should_notify': bool(should_notify_raw),
                    'reason': str(reason_global),
                }
            )
        except SchemaValidationError as e:
            _fail_schema_validation(runlog=runlog, audit_fn=_audit, stage='decision', exc=e, run_id=run_id)
        should_run = bool(decision.should_run)
        should_notify = bool(decision.should_notify)
        reason = str(decision.reason)

        acct_metrics['should_notify'] = bool(should_notify)
        acct_metrics['reason'] = str(reason)

        _write_acct_run_state('account_metrics.json', {
            'as_of_utc': utc_now(),
            'account': acct,
            'markets_to_run': markets_to_run,
            'scheduler_ms': acct_metrics.get('scheduler_ms'),
            'pipeline_ms': acct_metrics.get('pipeline_ms'),
            'ran_scan': acct_metrics.get('ran_scan'),
            'should_notify': acct_metrics.get('should_notify'),
            'meaningful': acct_metrics.get('meaningful'),
            'reason': acct_metrics.get('reason'),
            'run_dir': str(run_dir),
        })

        scan_gate = decide_account_scan_gate(
            should_run=should_run,
            has_symbols=((not markets_to_run) or bool(cfg.get('symbols') or [])),
            reason=reason,
        )
        if not bool(scan_gate.get('run_pipeline')):
            acct_metrics['ran_scan'] = bool(scan_gate.get('ran_scan'))
            acct_metrics['meaningful'] = bool(scan_gate.get('meaningful'))
            acct_metrics['reason'] = str(scan_gate.get('result_reason') or reason)
            tick_metrics['accounts'].append(acct_metrics)
            results.append(
                AccountResult(
                    acct,
                    bool(scan_gate.get('ran_scan')),
                    should_notify,
                    bool(scan_gate.get('meaningful')),
                    str(scan_gate.get('result_reason') or reason),
                    '',
                )
            )
            continue

        if (not prefetch_done):
            runlog.safe_event('fetch_chain_cache', 'start', data=_safe_runlog_data({'account': acct, 'symbols_count': len(cfg.get('symbols') or [])}))
            prefetch_stats = prefetch_required_data(vpy=vpy, base=base, cfg=cfg, shared_required=shared_required)
            _audit(
                'tool_call',
                'required_data_prefetch',
                run_id=run_id,
                account=acct,
                status=('ok' if int(prefetch_stats.get('errors') or 0) == 0 else 'error'),
                tool_name='required_data_prefetch',
                extra={'stats': {k: v for k, v in prefetch_stats.items() if k != 'audit'}},
            )
            try:
                state_repo.write_account_run_state(
                    base,
                    run_id,
                    acct,
                    'required_data_prefetch_summary.json',
                    prefetch_stats,
                )
                for item in (prefetch_stats.get('audit') or []):
                    if isinstance(item, dict):
                        state_repo.append_run_audit_jsonl(
                            base,
                            run_id,
                            'tool_execution_audit.jsonl',
                            item,
                        )
                        _audit(
                            'tool_call',
                            'required_data_prefetch_item',
                            run_id=run_id,
                            account=acct,
                            status=('ok' if bool(item.get('ok')) else 'error'),
                            tool_name=str(item.get('tool_name') or 'required_data_prefetch'),
                            extra={'symbol': item.get('symbol'), 'message': item.get('message')},
                        )
            except Exception:
                pass
            runlog.safe_event('fetch_chain_cache', 'ok', data=_safe_runlog_data(prefetch_stats))
            prefetch_done = True

        acct_report_dir.mkdir(parents=True, exist_ok=True)

        runlog.safe_event(
            'snapshot_batches',
            'start',
            data=_safe_runlog_data({'account': acct}),
        )

        t_pipe0 = monotonic()
        pipe = run_pipeline_script(
            vpy=vpy,
            base=base,
            config=cfg_override,
            report_dir=acct_report_dir,
            state_dir=acct_state_dir,
            shared_required_data=shared_required,
            shared_context_dir=run_repo.get_run_state_dir(base, run_id),
            capture_output=True,
            text=True,
            env=dict(os.environ, PYTHONPATH=str(base)),
        )
        acct_metrics['pipeline_ms'] = int((monotonic() - t_pipe0) * 1000)
        _audit(
            'tool_call',
            'run_pipeline',
            run_id=run_id,
            account=acct,
            status=('ok' if pipe.returncode == 0 else 'error'),
            tool_name='run_pipeline',
            extra={'duration_ms': acct_metrics['pipeline_ms'], 'returncode': int(pipe.returncode)},
        )
        pipeline_tool_dto = normalize_pipeline_subprocess_output(
            returncode=pipe.returncode,
            stdout=pipe.stdout or '',
            stderr=pipe.stderr or '',
        )
        pipeline_result = decide_pipeline_execution_result(
            returncode=int(pipeline_tool_dto.get('returncode') or 0)
        )
        if not bool(pipeline_result.get('ok')):
            _audit(
                'tool_call',
                'run_pipeline_result',
                run_id=run_id,
                account=acct,
                status='error',
                tool_name='run_pipeline',
                extra=build_failure_audit_fields(
                    failure_kind='io_error',
                    failure_stage='run_pipeline',
                    failure_adapter=str(pipeline_tool_dto.get('adapter') or 'pipeline'),
                ),
            )
            runlog.safe_event(
                'snapshot_batches',
                'error',
                duration_ms=acct_metrics['pipeline_ms'],
                error_code='PIPELINE_FAILED',
                message=f'pipeline failed for {acct}',
                data=_safe_runlog_data({'account': acct, 'returncode': pipe.returncode}),
            )
            out = ((pipe.stdout or '') + '\n' + (pipe.stderr or '')).strip()
            if out:
                tail = '\n'.join(out.splitlines()[-60:])
                print(f"[ERR] pipeline failed ({acct})\n{tail}")
            acct_metrics['ran_scan'] = bool(pipeline_result.get('ran_scan'))
            acct_metrics['meaningful'] = bool(pipeline_result.get('meaningful'))
            acct_metrics['reason'] = str(pipeline_result.get('reason') or 'pipeline failed')
            tick_metrics['accounts'].append(acct_metrics)
            results.append(
                AccountResult(
                    acct,
                    bool(pipeline_result.get('ran_scan')),
                    should_notify,
                    bool(pipeline_result.get('meaningful')),
                    str(pipeline_result.get('reason') or 'pipeline failed'),
                    '',
                )
            )
            continue

        runlog.safe_event(
            'snapshot_batches',
            'ok',
            duration_ms=acct_metrics['pipeline_ms'],
            data=_safe_runlog_data({'account': acct}),
        )
        ran_any_pipeline = True

        text = notif_path.read_text(encoding='utf-8', errors='replace').strip() if notif_path.exists() else ''

        try:
            run_repo.write_run_account_text(
                base,
                run_id,
                acct,
                'symbols_notification.txt',
                text + '\n',
            )
            _audit('write', 'write_run_account_text:symbols_notification.txt', run_id=run_id, account=acct)
            if cfg_override.exists() and cfg_override.stat().st_size > 0:
                run_repo.copy_to_run_account(
                    base,
                    run_id,
                    acct,
                    cfg_override,
                    'config.override.json',
                )
                _audit('write', 'copy_to_run_account:config.override.json', run_id=run_id, account=acct)
        except Exception:
            pass

        auto_close_path = acct_report_dir / 'auto_close_summary.txt'
        auto_close_text = auto_close_path.read_text(encoding='utf-8', errors='replace').strip() if auto_close_path.exists() else ''
        auto_close_flat = flatten_auto_close_summary(auto_close_text, always_show=False)
        if auto_close_flat:
            text = (text.strip() + '\n\n' + auto_close_flat.strip()).strip()

        meaningful = decide_notification_meaningful(text)

        should_notify_effective = should_notify
        # [REMOVED] legacy override(high,dense) logic

        acct_metrics['ran_scan'] = True
        acct_metrics['should_notify'] = bool(should_notify_effective)
        acct_metrics['meaningful'] = bool(meaningful)
        acct_metrics['reason'] = str(reason)
        tick_metrics['accounts'].append(acct_metrics)
        results.append(AccountResult(acct, True, should_notify_effective, meaningful, reason, text))

    if ran_any_pipeline:
        try:
            run_scan_scheduler_cli(
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
        accts = (notif_cfg.get('cash_footer_accounts') or ['lx', 'sy'])
        timeout_sec = int(notif_cfg.get('cash_footer_timeout_sec') or 180)
        max_age_sec = int(notif_cfg.get('cash_snapshot_max_age_sec') or 900)
        cash_footer_lines = query_cash_footer(
            base,
            market=cfg_market,
            accounts=accts,
            timeout_sec=timeout_sec,
            snapshot_max_age_sec=max_age_sec,
        )
    except Exception:
        cash_footer_lines = []

    now_bj = bj_now()
    notify_candidates = rank_notify_candidates(engine_filter_notify_candidates(results))
    account_messages = build_account_messages(
        notify_candidates=notify_candidates,
        now_bj=now_bj,
        cash_footer_lines=cash_footer_lines,
        cash_footer_for_account_fn=cash_footer_for_account,
        build_account_message_fn=build_account_message,
    )
    try:
        account_messages_snapshot = SnapshotDTO.from_payload(
            {
                'schema_kind': 'snapshot_dto',
                'schema_version': '1.0',
                'snapshot_name': 'account_messages',
                'as_of_utc': utc_now(),
                'payload': {'account_messages': account_messages},
            }
        )
        raw_account_messages = account_messages_snapshot.payload.get('account_messages')
        if not isinstance(raw_account_messages, dict):
            raise SchemaValidationError('account_messages must be a dict')
        account_messages = {str(k): str(v) for k, v in raw_account_messages.items()}
    except SchemaValidationError as e:
        _fail_schema_validation(runlog=runlog, audit_fn=_audit, stage='account_messages_snapshot', exc=e, run_id=run_id)

    notify_threshold = resolve_multi_tick_engine_entrypoint(
        notify_account_messages=account_messages,
        notify_min_accounts=1,
    ).get('notify_threshold') or {}
    if not bool(notify_threshold.get('threshold_met')):
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

        runlog.safe_event('run_end', 'ok', data=_safe_runlog_data({'sent': False, 'reason': 'no_account_notification', 'accounts': [r.account for r in results]}))
        _guard_mark_success()
        return 0

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

    dispatch_decision = decide_notify_dispatch(
        no_send=no_send,
        target=target,
        dnd_is_quiet=bool(dnd_decision.get('is_quiet')),
    )
    dispatch_gate = resolve_multi_tick_engine_entrypoint(
        notify_dispatch=dispatch_decision,
        dnd_decision=dnd_decision,
    ).get('notify') or {}
    notify_delivery = resolve_multi_tick_engine_entrypoint(
        notify_dispatch_gate=dispatch_gate,
    ).get('notify_delivery') or decide_notify_delivery_action(
        dispatch_gate=dispatch_gate,
    )
    _audit(
        'notify',
        'dispatch_decision',
        run_id=run_id,
        status=('ok' if not dispatch_decision.get('config_error') else 'error'),
        target=(str(target) if target else None),
        extra={'reason': dispatch_decision.get('reason'), 'should_send': bool(dispatch_decision.get('should_send'))},
    )
    if str(notify_delivery.get('action') or '') == 'skip_quiet_hours':
        quiet_window = str(notify_delivery.get('quiet_window') or '')
        runlog.safe_event('notify', 'skip', message=f'in quiet hours ({quiet_window})')
        print(f"[SKIP] Currently in quiet hours (DND). Target was: {target}")
        _guard_mark_success()
        return 0

    sent_accounts: list[str] = []

    config_error = notify_delivery.get('config_error')
    if config_error:
        runlog.safe_event('notify', 'error', error_code='CONFIG_ERROR', message=str(config_error))
        raise SystemExit(f'[CONFIG_ERROR] {config_error}')

    delivery_plan: DeliveryPlan | None = None
    if bool(notify_delivery.get('should_send')):
        target = notify_delivery.get('effective_target')
        try:
            delivery_plan = DeliveryPlan.from_payload(
                {
                    'schema_kind': 'delivery_plan',
                    'schema_version': '1.0',
                    'channel': str(channel),
                    'target': str(target),
                    'account_messages': account_messages,
                    'should_send': True,
                }
            )
        except SchemaValidationError as e:
            _fail_schema_validation(runlog=runlog, audit_fn=_audit, stage='delivery_plan', exc=e, run_id=run_id)
        for acct, msg in delivery_plan.account_messages.items():
            runlog.safe_event('notify', 'start', data=_safe_runlog_data({'channel': channel, 'target_set': bool(target), 'account': acct, 'message_len': len(msg)}))

            t_notify0 = monotonic()
            send = send_openclaw_message(
                base=base,
                channel=str(delivery_plan.channel),
                target=str(delivery_plan.target),
                message=msg,
            )
            send_tool_dto = normalize_notify_subprocess_output(
                returncode=send.returncode,
                stdout=send.stdout or '',
                stderr=send.stderr or '',
            )
            if not bool(send_tool_dto.get('ok')):
                _audit(
                    'notify',
                    'send_openclaw_message',
                    run_id=run_id,
                    account=acct,
                    status='error',
                    target=str(delivery_plan.target),
                    error_code='SEND_FAILED',
                    extra={
                        'returncode': int(send.returncode),
                        **build_failure_audit_fields(
                            failure_kind='io_error',
                            failure_stage='send_openclaw_message',
                            failure_adapter=str(send_tool_dto.get('adapter') or 'notify'),
                        ),
                    },
                )
                runlog.safe_event(
                    'notify',
                    'error',
                    duration_ms=int((monotonic() - t_notify0) * 1000),
                    error_code='SEND_FAILED',
                    message=f'message send failed ({acct})',
                    data=_safe_runlog_data({'returncode': send.returncode, 'account': acct}),
                )
                _guard_mark_failure('SEND_FAILED', 'send_openclaw_message')
                raise SystemExit(send.returncode)

            sent_accounts.append(acct)
            _audit(
                'notify',
                'send_openclaw_message',
                run_id=run_id,
                account=acct,
                status='ok',
                target=str(delivery_plan.target),
                extra={
                    'returncode': int(send.returncode),
                    'message_id': send_tool_dto.get('message_id'),
                },
            )
            runlog.safe_event('notify', 'ok', duration_ms=int((monotonic() - t_notify0) * 1000), data=_safe_runlog_data({'channel': channel, 'account': acct}))
    else:
        target = notify_delivery.get('effective_target')
        sent_accounts = list(account_messages.keys())
        runlog.safe_event('notify', 'skip', message='no_send mode')

    if not no_send:
        try:
            for acct in sent_accounts:
                run_scan_scheduler_cli(
                    vpy=vpy,
                    base=base,
                    config=cfg_path,
                    state=state_path,
                    state_dir=run_repo.get_run_state_dir(base, run_id),
                    mark_notified=True,
                    schedule_key=str(scheduler_schedule_key),
                    account=str(acct),
                    capture_output=False,
                )
        except Exception:
            pass

    try:
        tick_metrics['sent'] = (not no_send) and bool(sent_accounts)
        tick_metrics['reason'] = ('sent' if ((not no_send) and bool(sent_accounts)) else ('no_send' if no_send else 'no_account_sent'))
        state_repo.write_tick_metrics(base, run_id, tick_metrics)
        state_repo.append_tick_metrics_history(base, run_id, tick_metrics)
        _audit('write', 'write_tick_metrics', run_id=run_id, extra={'sent': bool(tick_metrics.get('sent'))})
    except Exception:
        pass

    try:
        last_run_path = (state_repo.shared_state_dir(base) / 'last_run.json').resolve()
        prev = read_json(last_run_path, {})
        run_meta = {
            'last_run_utc': utc_now(),
            'sent': bool(sent_accounts),
            'channel': str(channel),
            'target': str(target),
            'accounts': [r.account for r in results],
            'sent_accounts': sent_accounts,
            'results': [r.__dict__ for r in results],
        }
        state_repo.write_shared_last_run(
            base,
            build_shared_last_run_payload(prev_payload=prev, run_meta=run_meta, history_limit=20),
        )
        _audit('write', 'write_shared_last_run', run_id=run_id, extra={'sent_accounts': list(sent_accounts)})
    except Exception:
        pass

    runlog.safe_event('run_end', 'ok', data=_safe_runlog_data({'sent': (not no_send) and bool(sent_accounts), 'accounts': [r.account for r in results], 'sent_accounts': sent_accounts}))
    _guard_mark_success()
    return 0


__all__ = ['main', '_CURRENT_RUN_ID']
