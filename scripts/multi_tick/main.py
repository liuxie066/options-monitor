from __future__ import annotations

import argparse
import json
import os
import subprocess
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
    atomic_write_json as write_json,
    parse_last_json_obj,
    utc_now,
    bj_now,
)

from .cash_footer import query_cash_footer
from .required_data_prefetch import prefetch_required_data
from .notify_format import (
    is_high_priority_notification,
    flatten_auto_close_summary,
    build_merged_message,
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
    append_json_list,
)


_CURRENT_RUN_ID: str | None = None


def _select_markets_to_run(now_utc: datetime, cfg: dict, market_config: str) -> list[str]:
    mc = str(market_config or 'auto').lower()
    if mc == 'hk':
        return ['HK']
    if mc == 'us':
        return ['US']
    if mc == 'all':
        return ['HK', 'US']

    schedule_hk = (cfg.get('schedule_hk') or {}) if isinstance(cfg, dict) else {}
    schedule_us = (cfg.get('schedule') or {}) if isinstance(cfg, dict) else {}

    try:
        from scripts.scan_scheduler import decide

        state0: dict = {
            'last_scan_utc': None,
            'last_notify_utc': None,
        }

        d_hk = decide(schedule_hk, state0, now_utc, account=None, schedule_key='schedule_hk')
        if d_hk.in_market_hours:
            return ['HK']

        d_us = decide(schedule_us, state0, now_utc, account=None, schedule_key='schedule')
        if d_us.in_market_hours:
            return ['US']
    except Exception:
        pass

    return []


def main() -> int:
    ap = argparse.ArgumentParser(description='Multi-account tick with merged notification')
    ap.add_argument('--config', default='config.json')
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

    base = Path(__file__).resolve().parents[2]
    vpy = base / '.venv' / 'bin' / 'python'
    runlog = RunLogger(base)
    global _CURRENT_RUN_ID
    _CURRENT_RUN_ID = runlog.run_id

    cfg_path = Path(args.config)
    if not cfg_path.is_absolute():
        cfg_path = (base / cfg_path).resolve()
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
        }),
    )

    market_cfg = str(getattr(args, 'market_config', 'auto') or 'auto').lower()
    if market_cfg in ('hk', 'us'):
        try:
            base_cfg = dict(base_cfg)
            syms = base_cfg.get('symbols') or []
            base_cfg['symbols'] = [it for it in syms if isinstance(it, dict) and (it.get('market') == market_cfg.upper())]
        except Exception:
            pass

    schedule_cfg = base_cfg.get('schedule', {}) or {}
    dense_notify_cooldown_min = int(schedule_cfg.get('notify_cooldown_dense_min', 30))
    sparse_after_beijing = parse_hhmm(schedule_cfg.get('sparse_after_beijing', '02:00'))
    bj_tz = ZoneInfo(schedule_cfg.get('beijing_timezone', 'Asia/Shanghai'))

    if bool(getattr(args, 'opend_phone_verify_continue', False)):
        clear_opend_phone_verify_pending(base)

    if is_opend_phone_verify_pending(base):
        runlog.safe_event('run_end', 'skip', message='opend phone verify pending; paused until user confirmation')
        return 0

    t_watchdog0 = monotonic()
    runlog.safe_event('watchdog', 'start')
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
            for host, port in sorted(ports):
                try:
                    wd0 = subprocess.run(
                        [str(vpy), 'scripts/opend_watchdog.py', '--ensure', '--host', str(host), '--port', str(port), '--json'],
                        cwd=str(base),
                        capture_output=True,
                        text=True,
                        timeout=35,
                    )
                    payload0 = parse_last_json_obj((wd0.stdout or '') + '\n' + (wd0.stderr or ''))
                    ok0 = bool(payload0.get('ok')) if payload0 else (wd0.returncode == 0)
                    if not ok0:
                        unhealthy = {
                            'host': host,
                            'port': port,
                            'payload': payload0,
                            'detail': ((wd0.stdout or '') + '\n' + (wd0.stderr or '')).strip(),
                        }
                        break
                except Exception as e:
                    unhealthy = {
                        'host': host,
                        'port': port,
                        'payload': {'ok': False, 'error_code': 'OPEND_API_ERROR', 'message': 'OpenD 看门狗执行失败'},
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
                if allow_downgrade and (not has_hk_opend):
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
                        state_dir = (base / 'output_accounts' / acct0 / 'state')
                        state_dir.mkdir(parents=True, exist_ok=True)
                        write_json(state_dir / 'last_run.json', {
                            'last_run_utc': now,
                            'sent': False,
                            'reason': 'opend_unhealthy',
                            'error_code': error_code,
                            'detail': msg,
                        })
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

    shared_state_dir = (base / 'output_shared' / 'state').resolve()
    shared_state_dir.mkdir(parents=True, exist_ok=True)
    if markets_to_run == ['HK']:
        state_path = shared_state_dir / 'scheduler_state_hk.json'
    elif markets_to_run == ['US']:
        state_path = shared_state_dir / 'scheduler_state_us.json'
    else:
        state_path = shared_state_dir / 'scheduler_state.json'

    try:
        if (not state_path.exists()) or state_path.stat().st_size <= 0:
            write_json(state_path, {
                'last_scan_utc': None,
                'last_notify_utc': None,
            })
    except Exception:
        pass

    scheduler_schedule_key = 'schedule_hk' if (markets_to_run == ['HK'] and ('schedule_hk' in (base_cfg or {}))) else 'schedule'
    scheduler_cmd = [
        str(vpy), 'scripts/scan_scheduler.py',
        '--config', str(cfg_path),
        '--state', str(state_path),
        '--jsonl',
        '--schedule-key', str(scheduler_schedule_key),
    ]
    t_sch0 = monotonic()
    scheduler_proc = subprocess.run(
        scheduler_cmd,
        cwd=str(base),
        capture_output=True,
        text=True,
    )
    scheduler_ms = int((monotonic() - t_sch0) * 1000)
    if scheduler_proc.returncode != 0:
        err = f"scheduler error: {(scheduler_proc.stderr or scheduler_proc.stdout).strip()}"
        for acct in args.accounts:
            acct0 = str(acct).strip()
            if acct0:
                results.append(AccountResult(acct0, False, False, False, err, ''))
        runlog.safe_event('run_end', 'error', error_code='SCHEDULER_FAILED', message=err)
        return 0

    scheduler_decision = json.loads((scheduler_proc.stdout or '').strip())
    should_run_global = bool(scheduler_decision.get('should_run_scan'))
    should_notify_global = bool(scheduler_decision.get('is_notify_window_open', scheduler_decision.get('should_notify')))
    reason_global = str(scheduler_decision.get('reason') or '')

    if bool(getattr(args, 'force', False)):
        should_run_global = True
        reason_global = (reason_global + ' | force').strip(' |')

    if smoke:
        should_run_global = False
        reason_global = (str(reason_global) + ' | smoke_skip_pipeline').strip()

    ran_any_pipeline = False

    run_id = utc_now().replace(':', '').replace('-', '').split('.')[0]
    run_dir = (base / 'output_runs' / run_id).resolve()
    required_dir = (run_dir / 'required_data').resolve()
    required_raw = (required_dir / 'raw').resolve()
    required_parsed = (required_dir / 'parsed').resolve()
    required_raw.mkdir(parents=True, exist_ok=True)
    required_parsed.mkdir(parents=True, exist_ok=True)

    prefetch_done = False
    shared_required = required_dir
    tick_metrics_path = (base / 'output_shared' / 'state' / 'tick_metrics.json').resolve()
    tick_metrics_history_path = (base / 'output_shared' / 'state' / 'tick_metrics_history.json').resolve()

    run_state_dir = (run_dir / 'state').resolve()
    run_state_dir.mkdir(parents=True, exist_ok=True)
    tick_metrics_run_path = (run_state_dir / 'tick_metrics.json').resolve()
    tick_metrics_history_run_path = (run_state_dir / 'tick_metrics_history.json').resolve()

    try:
        tick_metrics_run_dir_path = (base / 'output_shared' / 'state' / 'last_run_dir.txt').resolve()
        tick_metrics_run_dir_path.write_text(str(run_dir) + "\n", encoding='utf-8')
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
        write_json((run_dir / 'state' / 'scheduler_decision.json').resolve(), {
            'as_of_utc': utc_now(),
            'schedule_key': str(scheduler_schedule_key),
            'decision': scheduler_decision,
            'state_path': str(state_path),
        })
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
        cfg_override = acct_out / 'state' / 'config.override.json'
        cfg_override.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

        acct_report_dir = (run_dir / 'accounts' / acct).resolve()
        acct_state_dir = (acct_report_dir / 'state').resolve()
        try:
            acct_state_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

        def _write_acct_run_state(name: str, payload: dict):
            try:
                acct_state_dir.mkdir(parents=True, exist_ok=True)
                write_json((acct_state_dir / name).resolve(), payload)
            except Exception:
                pass

        notif_path = (acct_report_dir / 'symbols_notification.txt').resolve()

        should_run = bool(should_run_global)
        should_notify = bool(should_notify_global)
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
            runlog.safe_event('fetch_chain_cache', 'ok', data=_safe_runlog_data(prefetch_stats))
            prefetch_done = True

        acct_report_dir.mkdir(parents=True, exist_ok=True)

        pipe_cmd = [
            str(vpy), 'scripts/run_pipeline.py',
            '--config', str(cfg_override),
            '--mode', 'scheduled',
            '--shared-required-data', str(shared_required),
            '--report-dir', str(acct_report_dir),
            '--state-dir', str((run_dir / 'state').resolve()),
        ]
        runlog.safe_event(
            'snapshot_batches',
            'start',
            data=_safe_runlog_data({'account': acct}),
        )

        t_pipe0 = monotonic()
        pipe = subprocess.run(
            pipe_cmd,
            cwd=str(base),
            capture_output=True,
            text=True,
            env=dict(os.environ, PYTHONPATH=str(base)),
        )
        acct_metrics['pipeline_ms'] = int((monotonic() - t_pipe0) * 1000)
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
            acct_run_dir = (run_dir / 'accounts' / acct).resolve()
            acct_run_dir.mkdir(parents=True, exist_ok=True)
            (acct_run_dir / 'symbols_notification.txt').write_text(text + '\n', encoding='utf-8')
            if cfg_override.exists() and cfg_override.stat().st_size > 0:
                (acct_run_dir / 'config.override.json').write_bytes(cfg_override.read_bytes())
        except Exception:
            pass

        auto_close_path = acct_report_dir / 'auto_close_summary.txt'
        auto_close_text = auto_close_path.read_text(encoding='utf-8', errors='replace').strip() if auto_close_path.exists() else ''
        auto_close_flat = flatten_auto_close_summary(auto_close_text, always_show=False)
        if auto_close_flat:
            text = (text.strip() + '\n\n' + auto_close_flat.strip()).strip()

        meaningful = bool(text) and (text != '今日无需要主动提醒的内容。')

        should_notify_effective = should_notify
        try:
            now_bj = datetime.now(timezone.utc).astimezone(bj_tz)
            before_sparse = now_bj.time() < sparse_after_beijing
            high_pri = meaningful and is_high_priority_notification(text)

            if (not should_notify_effective) and before_sparse and high_pri:
                st = read_json(state_path, {'last_notify_utc': None})
                last_notify = maybe_parse_dt((st or {}).get('last_notify_utc')) if isinstance(st, dict) else None
                if last_notify is None:
                    should_notify_effective = True
                    reason = (reason + f" | override(high,dense): last_notify missing")
                else:
                    elapsed = datetime.now(timezone.utc) - last_notify.astimezone(timezone.utc)
                    if elapsed >= timedelta(minutes=dense_notify_cooldown_min):
                        should_notify_effective = True
                        reason = (reason + f" | override(high,dense): elapsed>={dense_notify_cooldown_min}m")
        except Exception:
            pass

        acct_metrics['ran_scan'] = True
        acct_metrics['should_notify'] = bool(should_notify_effective)
        acct_metrics['meaningful'] = bool(meaningful)
        acct_metrics['reason'] = str(reason)
        tick_metrics['accounts'].append(acct_metrics)
        results.append(AccountResult(acct, True, should_notify_effective, meaningful, reason, text))

    if ran_any_pipeline:
        try:
            sch_args = [
                str(vpy), 'scripts/scan_scheduler.py',
                '--config', str(cfg_path),
                '--state', str(state_path),
                '--state-dir', str((run_dir / 'state').resolve()),
                '--mark-scanned',
                '--schedule-key', str(scheduler_schedule_key),
            ]
            subprocess.run(sch_args, cwd=str(base))
        except Exception:
            pass

    runlog.safe_event(
        'notify',
        'prepare',
        data=_safe_runlog_data({
            'results_count': len(results),
            'notify_candidates': len([r for r in results if r.should_notify and r.meaningful and bool(r.notification_text.strip())]),
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
    merged = build_merged_message(
        results,
        now_bj=now_bj,
        cash_footer_lines=cash_footer_lines,
    )
    if not merged:
        runlog.safe_event('notify', 'skip', message='no merged notification content')

        try:
            shared_last = (base / 'output_shared' / 'state' / 'last_run.json').resolve()
            shared_last.parent.mkdir(parents=True, exist_ok=True)
            write_json(shared_last, {
                'last_run_utc': utc_now(),
                'sent': False,
                'reason': 'no_merged_notification',
                'account': 'merged',
                'accounts': [r.account for r in results],
                'results': [r.__dict__ for r in results],
            })
        except Exception:
            pass

        try:
            for r in results:
                acct_out = accounts_root / r.account
                payload = {
                    'last_run_utc': utc_now(),
                    'sent': False,
                    'reason': 'no_merged_notification',
                    'account': r.account,
                    'result': r.__dict__,
                    'run_dir': str(run_dir),
                }
                write_json(acct_out / 'state' / 'last_run.json', payload)
                write_json((run_dir / 'accounts' / r.account / 'state' / 'last_run.json').resolve(), payload)
        except Exception:
            pass

        try:
            tick_metrics['sent'] = False
            tick_metrics['reason'] = 'no_merged_notification'
            write_json(tick_metrics_path, tick_metrics)
            append_json_list(tick_metrics_history_path, tick_metrics)
            write_json(tick_metrics_run_path, tick_metrics)
            append_json_list(tick_metrics_history_run_path, tick_metrics)
        except Exception:
            pass

        runlog.safe_event('run_end', 'ok', data=_safe_runlog_data({'sent': False, 'reason': 'no_merged_notification', 'accounts': [r.account for r in results]}))

        return 0

    channel = (base_cfg.get('notifications') or {}).get('channel') or 'feishu'
    target = (base_cfg.get('notifications') or {}).get('target')

    if not no_send:
        if not target:
            runlog.safe_event('notify', 'error', error_code='CONFIG_ERROR', message='notifications.target is required')
            raise SystemExit('[CONFIG_ERROR] notifications.target is required')

        runlog.safe_event('notify', 'start', data=_safe_runlog_data({'channel': channel, 'target_set': bool(target), 'message_len': len(merged)}))

        t_notify0 = monotonic()
        send = subprocess.run(
            ['openclaw', 'message', 'send', '--channel', str(channel), '--target', str(target), '--message', merged, '--json'],
            cwd=str(base),
            capture_output=True,
            text=True,
        )
        if send.returncode != 0:
            runlog.safe_event(
                'notify',
                'error',
                duration_ms=int((monotonic() - t_notify0) * 1000),
                error_code='SEND_FAILED',
                message='message send failed',
                data=_safe_runlog_data({'returncode': send.returncode}),
            )
            raise SystemExit(send.returncode)

        runlog.safe_event('notify', 'ok', duration_ms=int((monotonic() - t_notify0) * 1000), data=_safe_runlog_data({'channel': channel}))
    else:
        target = None
        runlog.safe_event('notify', 'skip', message='no_send mode')

    if not no_send:
        try:
            sch_args = [
                str(vpy), 'scripts/scan_scheduler.py',
                '--config', str(cfg_path),
                '--state', str(state_path),
                '--state-dir', str((run_dir / 'state').resolve()),
                '--mark-notified',
                '--schedule-key', str(scheduler_schedule_key),
            ]
            subprocess.run(sch_args, cwd=str(base))
        except Exception:
            pass

    try:
        tick_metrics['sent'] = (not no_send)
        tick_metrics['reason'] = ('sent' if (not no_send) else 'no_send')
        write_json(tick_metrics_path, tick_metrics)
        append_json_list(tick_metrics_history_path, tick_metrics)
        write_json(tick_metrics_run_path, tick_metrics)
        append_json_list(tick_metrics_history_run_path, tick_metrics)
    except Exception:
        pass

    try:
        last_run_path = (base / 'output_shared' / 'state' / 'last_run.json').resolve()
        prev = read_json(last_run_path, {})
        run_meta = {
            'last_run_utc': utc_now(),
            'sent': True,
            'channel': str(channel),
            'target': str(target),
            'account': 'merged',
            'accounts': [r.account for r in results],
            'results': [r.__dict__ for r in results],
        }
        hist = prev.get('history') if isinstance(prev, dict) else None
        if not isinstance(hist, list):
            hist = []
        hist.append(run_meta)
        hist = hist[-20:]
        write_json(last_run_path, {
            **(prev if isinstance(prev, dict) else {}),
            **run_meta,
            'history': hist,
        })
    except Exception:
        pass

    runlog.safe_event('run_end', 'ok', data=_safe_runlog_data({'sent': (not no_send), 'accounts': [r.account for r in results]}))

    return 0


__all__ = ['main', '_CURRENT_RUN_ID']
