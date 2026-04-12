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
    is_high_priority_notification,
    flatten_auto_close_summary,
    build_account_message,
)
from .opend_guard import (
    mark_opend_phone_verify_pending,
    clear_opend_phone_verify_pending,
    is_opend_phone_verify_pending,
    send_opend_alert,
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
from om.domain import (
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
    markets_for_trading_day_guard as domain_markets_for_trading_day_guard,
    reduce_trading_day_guard,
    resolve_notification_channel_target,
    select_markets_to_run as domain_select_markets_to_run,
    select_scheduler_state_filename,
)
from om.domain.engine import (
    SchedulerDecisionView,
    build_scheduler_decision_dto,
    decide_notification_meaningful,
    decide_opend_degrade_to_yahoo,
    filter_notify_candidates as engine_filter_notify_candidates,
    rank_notify_candidates,
)
from scripts.infra.service import (
    run_opend_watchdog,
    run_pipeline_script,
    run_scan_scheduler_cli,
    send_openclaw_message,
    trading_day_via_futu,
)

try:
    from om.storage import paths as storage_paths
    from om.storage.repositories import run_repo, state_repo
except Exception:
    from scripts.om.storage import paths as storage_paths  # type: ignore
    from scripts.om.storage.repositories import run_repo, state_repo  # type: ignore


_CURRENT_RUN_ID: str | None = None


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
    allow_derived_config = str(os.environ.get('OM_ALLOW_DERIVED_CONFIG', '')).strip() in {'1', 'true', 'TRUE'}
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

                degraded = False
                if decide_opend_degrade_to_yahoo(
                    allow_downgrade=allow_downgrade,
                    has_hk_opend=has_hk_opend,
                    watchdog_timed_out=watchdog_timed_out,
                ):
                    try:
                        for sym in (base_cfg.get('symbols') or []):
                            if str((sym or {}).get('market') or '').upper() != 'US':
                                continue
                            fetch = (sym or {}).get('fetch') or {}
                            if str(fetch.get('source') or '').lower() == 'opend':
                                fetch['source'] = 'yahoo'
                                for k in ['host', 'port', 'spot_from_portfolio_management']:
                                    fetch.pop(k, None)
                                sym['fetch'] = fetch
                                degraded = True
                    except Exception:
                        degraded = False

                if error_code == 'OPEND_NEEDS_PHONE_VERIFY':
                    mark_opend_phone_verify_pending(
                        base,
                        detail=(f"{host}:{port} {detail}" if host is not None and port is not None else detail),
                    )

                    send_opend_alert(
                        base,
                        base_cfg,
                        error_code=error_code,
                        message_text=msg + "（已暂停：等待你在飞书确认后再继续）",
                        detail=(f"{host}:{port} {detail}" if host is not None and port is not None else detail),
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
                        fallback_used=False,
                    )
                    return 0

                send_opend_alert(
                    base,
                    base_cfg,
                    error_code=error_code,
                    message_text=msg,
                    detail=(f"{host}:{port} {detail}" if host is not None and port is not None else detail),
                    no_send=no_send,
                )

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

                if degraded:
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
                        fallback_used=True,
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
                        fallback_used=False,
                        message=msg,
                    )
                    return 0

    except SystemExit:
        raise
    except Exception as e:
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
        guard_results: list[dict] = []
        for gm in guard_markets:
            is_td, gm_used = _is_trading_day_guard_for_market(base_cfg, gm)
            guard_results.append({'market': gm_used, 'is_trading_day': is_td})
            log(f"[TRADING_DAY_GUARD] market={gm_used} result={is_td}")

        runlog.safe_event(
            'trading_day_guard',
            'check',
            data=_safe_runlog_data({'results': guard_results, 'markets_to_run': markets_to_run, 'market_config': str(getattr(args, 'market_config', 'auto') or 'auto')}),
        )

        guard_reduced = reduce_trading_day_guard(
            markets_to_run=markets_to_run,
            guard_results=guard_results,
        )
        markets_to_run = list(guard_reduced.get('markets_to_run') or [])
        if bool(guard_reduced.get('should_skip')):
            runlog.safe_event('run_end', 'skip', message=str(guard_reduced.get('skip_message') or ''))
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
    scheduler_ms = int((monotonic() - t_sch0) * 1000)
    _audit(
        'tool_call',
        'scan_scheduler',
        status=('ok' if scheduler_proc.returncode == 0 else 'error'),
        tool_name='scan_scheduler_cli',
        extra={'duration_ms': scheduler_ms, 'returncode': int(scheduler_proc.returncode)},
    )
    if scheduler_proc.returncode != 0:
        err = f"scheduler error: {(scheduler_proc.stderr or scheduler_proc.stdout).strip()}"
        for acct in args.accounts:
            acct0 = str(acct).strip()
            if acct0:
                results.append(AccountResult(acct0, False, False, False, err, ''))
        runlog.safe_event('run_end', 'error', error_code='SCHEDULER_FAILED', message=err)
        return 0

    scheduler_raw = json.loads((scheduler_proc.stdout or '').strip())
    scheduler_decision = build_scheduler_decision_dto(scheduler_raw)
    scheduler_view = SchedulerDecisionView.from_payload(scheduler_decision)
    should_run_global = bool(scheduler_view.should_run_scan)
    reason_global = str(scheduler_view.reason)

    notify_decision_by_account: dict[str, dict | None] = {}
    for acct0 in [str(a).strip() for a in (args.accounts or []) if str(a).strip()]:
        account_scheduler_decision_raw: dict | None = None
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
                account_scheduler_decision_raw = json.loads((sch_acct.stdout or '').strip())
        except Exception:
            account_scheduler_decision_raw = None
        notify_decision_by_account[acct0] = account_scheduler_decision_raw

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
        state_repo.write_scheduler_decision(base, run_id, {
            'as_of_utc': utc_now(),
            'schedule_key': str(scheduler_schedule_key),
            'decision': scheduler_decision,
            'state_path': str(state_path),
        })
        _audit('write', 'write_scheduler_decision', run_id=run_id)
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

        should_run = bool(should_run_global)
        should_notify = decide_should_notify(
            account=acct,
            notify_decision_by_account=notify_decision_by_account,
            scheduler_decision=scheduler_view,
        )
        reason = str(reason_global)

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

        if not should_run:
            acct_metrics['ran_scan'] = False
            acct_metrics['meaningful'] = False
            tick_metrics['accounts'].append(acct_metrics)
            results.append(AccountResult(acct, False, should_notify, False, reason, ''))
            continue

        try:
            if markets_to_run and (not (cfg.get('symbols') or [])):
                results.append(AccountResult(acct, False, should_notify, False, reason + ' | 本时段无对应市场标的', ''))
                continue
        except Exception:
            pass

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
        if pipe.returncode != 0:
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
            acct_metrics['ran_scan'] = True
            acct_metrics['meaningful'] = False
            acct_metrics['reason'] = 'pipeline failed'
            tick_metrics['accounts'].append(acct_metrics)
            results.append(AccountResult(acct, True, should_notify, False, 'pipeline failed', ''))
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

    if not account_messages:
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

        return 0

    notif_cfg = base_cfg.get('notifications') or {}
    notify_route = resolve_notification_channel_target(notifications=notif_cfg)
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
    _audit(
        'notify',
        'dispatch_decision',
        run_id=run_id,
        status=('ok' if not dispatch_decision.get('config_error') else 'error'),
        target=(str(target) if target else None),
        extra={'reason': dispatch_decision.get('reason'), 'should_send': bool(dispatch_decision.get('should_send'))},
    )
    if str(dispatch_decision.get('reason') or '') == 'quiet_hours':
        quiet_window = str(dnd_decision.get('quiet_window') or '')
        runlog.safe_event('notify', 'skip', message=f'in quiet hours ({quiet_window})')
        print(f"[SKIP] Currently in quiet hours (DND). Target was: {target}")
        return 0

    sent_accounts: list[str] = []

    config_error = dispatch_decision.get('config_error')
    if config_error:
        runlog.safe_event('notify', 'error', error_code='CONFIG_ERROR', message=str(config_error))
        raise SystemExit(f'[CONFIG_ERROR] {config_error}')

    if bool(dispatch_decision.get('should_send')):
        target = dispatch_decision.get('effective_target')
        for acct, msg in account_messages.items():
            runlog.safe_event('notify', 'start', data=_safe_runlog_data({'channel': channel, 'target_set': bool(target), 'account': acct, 'message_len': len(msg)}))

            t_notify0 = monotonic()
            send = send_openclaw_message(
                base=base,
                channel=str(channel),
                target=str(target),
                message=msg,
            )
            if send.returncode != 0:
                _audit(
                    'notify',
                    'send_openclaw_message',
                    run_id=run_id,
                    account=acct,
                    status='error',
                    target=str(target),
                    error_code='SEND_FAILED',
                    extra={'returncode': int(send.returncode)},
                )
                runlog.safe_event(
                    'notify',
                    'error',
                    duration_ms=int((monotonic() - t_notify0) * 1000),
                    error_code='SEND_FAILED',
                    message=f'message send failed ({acct})',
                    data=_safe_runlog_data({'returncode': send.returncode, 'account': acct}),
                )
                raise SystemExit(send.returncode)

            sent_accounts.append(acct)
            _audit(
                'notify',
                'send_openclaw_message',
                run_id=run_id,
                account=acct,
                status='ok',
                target=str(target),
                extra={'returncode': int(send.returncode)},
            )
            runlog.safe_event('notify', 'ok', duration_ms=int((monotonic() - t_notify0) * 1000), data=_safe_runlog_data({'channel': channel, 'account': acct}))
    else:
        target = dispatch_decision.get('effective_target')
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

    return 0


__all__ = ['main', '_CURRENT_RUN_ID']
