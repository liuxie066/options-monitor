#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from scripts.io_utils import (
    copy_if_exists,
    has_shared_required_data,
    is_fresh,
    load_cached_json,
    safe_read_csv,
)
from scripts.config_profiles import apply_profiles
from scripts.pipeline_symbol import process_symbol
from scripts.subprocess_utils import run_cmd

import pandas as pd
import yaml

from scripts.report_builders import build_symbols_digest, build_symbols_summary



def main():
    global RUNTIME_MODE, IS_SCHEDULED

    parser = argparse.ArgumentParser(description='Run options-monitor pipeline')
    parser.add_argument('--config', required=True, help='Path to JSON config (single-symbol or watchlist). YAML is legacy.')
    parser.add_argument('--mode', default='dev', choices=['dev', 'scheduled'], help='Runtime mode: dev (verbose) vs scheduled (fast)')
    parser.add_argument('--symbols', default=None, help='Comma-separated symbol whitelist; only process these symbols')
    parser.add_argument('--stage', default='all', choices=['fetch','scan','alert','notify','all'], help='Pipeline stage: fetch|scan|alert|notify|all (dev speed; runs up to this stage)')
    parser.add_argument('--stage-only', default=None, choices=['alert','notify'], help='Run ONLY a late stage (no fetch/scan). Requires existing output files.')
    parser.add_argument('--refresh-multiplier-cache', action='store_true', help='Refresh output_shared/state/multiplier_cache.json via OpenD before running (best-effort).')
    parser.add_argument('--no-context', action='store_true', help='Skip portfolio/option_positions context fetch (dev speed). Useful when tuning filters only.')
    parser.add_argument('--shared-required-data', default=None, help='Path to shared required_data directory (contains raw/ and parsed/). If set, it is authoritative and fetch is skipped when artifacts exist.')
    # New flow (transitional): allow redirecting report outputs away from ./output/reports
    parser.add_argument('--report-dir', default=None, help='Directory to write reports (symbols_summary/alerts/notification). Default: output/reports')
    # Backward-compatible no-op flags (legacy shared scan plumbing removed)
    parser.add_argument('--shared-scan-dir', default=None, help='[no-op] legacy compatibility flag')
    parser.add_argument('--reuse-shared-scan', action='store_true', help='[no-op] legacy compatibility flag')
    args = parser.parse_args()

    RUNTIME_MODE = str(args.mode)
    IS_SCHEDULED = (RUNTIME_MODE == 'scheduled')
    STAGE = str(args.stage)
    STAGE_ONLY = (str(args.stage_only) if args.stage_only else None)

    global SHARED_REQUIRED_DATA
    SHARED_REQUIRED_DATA = (str(args.shared_required_data) if getattr(args, 'shared_required_data', None) else None)

    def want(name: str) -> bool:
        # stage-only mode: run ONLY the requested late stage
        if STAGE_ONLY is not None:
            return name == STAGE_ONLY
        # normal mode: run up to STAGE
        if STAGE == 'all':
            return True
        order = ['fetch', 'scan', 'alert', 'notify']
        try:
            return order.index(name) <= order.index(STAGE)
        except Exception:
            return True

    def stage_only_changes_out() -> str:
        # In dev iteration, stage-only should not mutate snapshot/change history.
        # (Otherwise, formatting tests would pollute symbols_summary_prev.csv / changes.)
        return '/dev/null'


    base = Path(__file__).resolve().parents[1]
    cfg_path = Path(args.config)

    # report_dir override (new flow: write into run_dir/accounts/<acct>/)
    report_dir = (Path(args.report_dir).resolve() if getattr(args, 'report_dir', None) else (base / 'output' / 'reports').resolve())
    report_dir.mkdir(parents=True, exist_ok=True)

    # Manual multiplier cache refresh (best-effort)
    if bool(getattr(args, 'refresh_multiplier_cache', False)):
        try:
            from scripts import multiplier_cache
            cache_path = multiplier_cache.default_cache_path(base)
            cfg0 = json.loads(cfg_path.read_text(encoding='utf-8'))
            syms = cfg0.get('watchlist') or cfg0.get('symbols') or []
            syms = [it for it in syms if isinstance(it, dict) and str(((it.get('fetch') or {}).get('source') or '')).lower() == 'opend']
            cache = multiplier_cache.load_cache(cache_path)
            for it in syms:
                sym = str(it.get('symbol') or '').strip().upper()
                fetch = it.get('fetch') or {}
                host = fetch.get('host') or '127.0.0.1'
                port = int(fetch.get('port') or 11111)
                r = multiplier_cache.refresh_via_opend(repo_base=base, symbol=sym, host=str(host), port=int(port), limit_expirations=1)
                if r.ok and r.multiplier:
                    cache[sym] = {'multiplier': int(r.multiplier), 'as_of_utc': multiplier_cache.utc_now(), 'source': 'opend'}
            multiplier_cache.save_cache(cache_path, cache)
        except Exception:
            pass
    if not cfg_path.is_absolute():
        cfg_path = (base / cfg_path).resolve()

    # config supports YAML or JSON
    if cfg_path.suffix.lower() == '.json':
        cfg = json.loads(cfg_path.read_text(encoding='utf-8'))
    else:
        with open(cfg_path, 'r', encoding='utf-8') as f:
            cfg = yaml.safe_load(f)

    # Validate config early (fail fast)
    # - dev mode: always validate
    # - scheduled mode: validate only when config content changes (hash cache)
    try:
        from scripts.validate_config import validate_config as _validate_config

        should_validate = True
        if IS_SCHEDULED:
            try:
                import hashlib
                import json as _json
                state_dir = (base / 'output' / 'state').resolve()
                state_dir.mkdir(parents=True, exist_ok=True)
                cache_path = state_dir / 'config_validation_cache.json'
                payload = _json.dumps(cfg, ensure_ascii=False, sort_keys=True)
                h = hashlib.sha256(payload.encode('utf-8')).hexdigest()
                prev = None
                if cache_path.exists() and cache_path.stat().st_size > 0:
                    prev = _json.loads(cache_path.read_text(encoding='utf-8')).get('sha256')
                if prev == h:
                    should_validate = False
                else:
                    cache_path.write_text(_json.dumps({'sha256': h}, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
            except Exception:
                should_validate = True

        if should_validate:
            _validate_config(cfg)
    except SystemExit:
        raise
    except Exception:
        # don't block if validator import fails
        pass

    py = sys.executable

    # naming aliases (prefer more intuitive names):
    # - templates == profiles (legacy internal name)
    # - symbols == watchlist (legacy internal name)
    if 'templates' in cfg and 'profiles' not in cfg:
        cfg['profiles'] = cfg.get('templates')
    if 'symbols' in cfg and 'watchlist' not in cfg:
        cfg['watchlist'] = cfg.get('symbols')

    if 'watchlist' in cfg:
        # Optional symbols whitelist (comma-separated)
        sym_whitelist = None
        if args.symbols:
            sym_whitelist = {s.strip() for s in str(args.symbols).split(',') if s.strip()}

        top_n = cfg.get('outputs', {}).get('top_n_alerts', 3)
        runtime = cfg.get('runtime', {}) or {}
        symbol_timeout_sec = int(runtime.get('symbol_timeout_sec', 120))
        portfolio_timeout_sec = int(runtime.get('portfolio_timeout_sec', 60))

        # stage-only late-stage runner: skip fetch/scan and re-use existing outputs.
        # Typical usage:
        #   --stage-only alert  (requires output/reports/symbols_summary.csv)
        #   --stage-only notify (requires output/reports/symbols_alerts.txt)
        if STAGE_ONLY is not None:
            summary_path = (report_dir / 'symbols_summary.csv').resolve()
            alerts_path = (report_dir / 'symbols_alerts.txt').resolve()
            if STAGE_ONLY == 'alert':
                if not (summary_path.exists() and summary_path.stat().st_size > 0):
                    raise SystemExit(f"[STAGE_ONLY_ERROR] missing required file: {summary_path}")
            if STAGE_ONLY == 'notify':
                if not (alerts_path.exists() and alerts_path.stat().st_size > 0):
                    raise SystemExit(f"[STAGE_ONLY_ERROR] missing required file: {alerts_path}")

            changes_out = stage_only_changes_out() if STAGE_ONLY else ('/dev/null' if IS_SCHEDULED else str((report_dir / 'symbols_changes.txt').as_posix()))
            alert_cmd = [
                py, 'scripts/alert_engine.py',
                '--summary-input', str((report_dir / 'symbols_summary.csv').as_posix()),
                '--output', str((report_dir / 'symbols_alerts.txt').as_posix()),
                '--changes-output', changes_out,
            ]
            # stage-only: do NOT update snapshot/history
            if (not IS_SCHEDULED) and (not STAGE_ONLY):
                alert_cmd.extend([
                    '--previous-summary', 'output/state/symbols_summary_prev.csv',
                    '--update-snapshot',
                ])
            if want('alert'):
                run_cmd(alert_cmd, cwd=base, is_scheduled=IS_SCHEDULED)

            if want('notify'):
                run_cmd([
                    py, 'scripts/notify_symbols.py',
                    '--alerts-input', str((report_dir / 'symbols_alerts.txt').as_posix()),
                    '--changes-input', (changes_out if STAGE_ONLY else ('/dev/null' if IS_SCHEDULED else str((report_dir / 'symbols_changes.txt').as_posix()))),
                    '--output', str((report_dir / 'symbols_notification.txt').as_posix()),
                ], cwd=base, is_scheduled=IS_SCHEDULED)

            log(f"[INFO] stage-only done: {STAGE_ONLY}")
            return

        symbols = []
        summary_rows: list[dict] = []

        # Ensure per-run report dir exists.
        report_dir.mkdir(parents=True, exist_ok=True)

        if (not want('scan')) or bool(getattr(args, 'no_context', False)):
            portfolio_ctx = None
            option_ctx = None
            fx_usd_per_cny = None
            hkdcny = None
        else:
            # portfolio context
            portfolio_cfg = cfg.get('portfolio', {}) or {}
            pm_config = portfolio_cfg.get('pm_config', '../portfolio-management/config.json')
            market = portfolio_cfg.get('market', '富途')
            account = portfolio_cfg.get('account')

            portfolio_ctx = None
            option_ctx = None

            # Cache policy (TTL seconds)
            # - scheduled: longer TTL (reduce PM subprocess overhead)
            # - dev: shorter TTL (keep reasonably fresh)
            ttl_opt_ctx = int(runtime.get('option_positions_context_ttl_sec', 900 if IS_SCHEDULED else 120) or 0)
            ttl_port_ctx = int(runtime.get('portfolio_context_ttl_sec', 900 if IS_SCHEDULED else 60) or 0)

            # 1) portfolio_context cache
            try:
                port_path = (base / 'output/state/portfolio_context.json').resolve()
                cached = None
                if ttl_port_ctx > 0 and is_fresh(port_path, ttl_port_ctx):
                    cached = load_cached_json(port_path)
                if cached is not None:
                    portfolio_ctx = cached
                else:
                    cmd = [
                        py, 'scripts/fetch_portfolio_context.py',
                        '--pm-config', str(pm_config),
                        '--market', str(market),
                        '--out', 'output/state/portfolio_context.json',
                    ]
                    if account:
                        cmd.extend(['--account', str(account)])
                    run_cmd(cmd, cwd=base, timeout_sec=portfolio_timeout_sec, is_scheduled=IS_SCHEDULED)
                    portfolio_ctx = load_cached_json(port_path) or json.loads(port_path.read_text(encoding='utf-8'))
            except BaseException as e:
                # Important: run_cmd() raises SystemExit on non-zero return codes.
                # For unattended cron, portfolio context is best-effort and should not kill the whole scan.
                log(f"[WARN] portfolio context not available: {e}")
                portfolio_ctx = None

            # 2) option_positions_context cache (and auto-close only on refresh)
            try:
                opt_path = (base / 'output/state/option_positions_context.json').resolve()
                refreshed = False
                cached = None
                if ttl_opt_ctx > 0 and is_fresh(opt_path, ttl_opt_ctx):
                    cached = load_cached_json(opt_path)
                if cached is not None:
                    option_ctx = cached
                else:
                    cmd = [
                        py, 'scripts/fetch_option_positions_context.py',
                        '--pm-config', str(pm_config),
                        '--market', str(market),
                        '--out', 'output/state/option_positions_context.json',
                    ]
                    if account:
                        cmd.extend(['--account', str(account)])
                    run_cmd(cmd, cwd=base, timeout_sec=portfolio_timeout_sec, is_scheduled=IS_SCHEDULED)
                    option_ctx = load_cached_json(opt_path) or json.loads(opt_path.read_text(encoding='utf-8'))
                    refreshed = True

                if refreshed:
                    # Auto-close expired open positions (table maintenance) without extra scans.
                    # Only run when we refreshed context (avoid repeated close calls during rapid dev loops).
                    try:
                        run_cmd([
                            py, 'scripts/auto_close_expired_positions.py',
                            '--pm-config', str(pm_config),
                            '--context', 'output/state/option_positions_context.json',
                            '--grace-days', '1',
                            '--max-close', '20',
                            '--summary-out', str((report_dir / 'auto_close_summary.txt').as_posix()),
                        ], cwd=base, timeout_sec=portfolio_timeout_sec, is_scheduled=IS_SCHEDULED)
                    except Exception as e2:
                        log(f"[WARN] auto-close expired positions failed: {e2}")

            except BaseException as e:
                # best-effort; do not kill pipeline if this fails
                log(f"[WARN] option positions context not available: {e}")
                option_ctx = None

            # FX (once per pipeline).
            fx_usd_per_cny = None
            hkdcny = None
            try:
                # scripts/ is not a package; load fx_rates.py by path
                import importlib.util
                fx_path = (base / 'scripts' / 'fx_rates.py').resolve()
                import sys as _sys
                spec = importlib.util.spec_from_file_location('fx_rates', fx_path)
                assert spec and spec.loader
                mod = importlib.util.module_from_spec(spec)
                # dataclasses expects module to exist in sys.modules during exec
                _sys.modules['fx_rates'] = mod
                spec.loader.exec_module(mod)  # type: ignore
                fx_usd_per_cny = mod.get_usd_per_cny(base)  # type: ignore
                # also load HKDCNY (CNY per 1 HKD) from cache
                try:
                    rates = mod.get_rates((base / 'output/state/rate_cache.json').resolve(), None)  # type: ignore
                    hkdcny = float(rates.get('HKDCNY')) if rates and rates.get('HKDCNY') else None
                except Exception:
                    hkdcny = None
            except BaseException as e:
                # best-effort
                log(f"[WARN] fx rates not available: {e}")

        profiles = cfg.get('profiles') or {}

        for item in cfg['watchlist']:
            try:
                # Optional whitelist filter
                if sym_whitelist is not None:
                    s0 = str((item or {}).get('symbol') or '').strip()
                    if s0 and s0 not in sym_whitelist:
                        continue

                item = apply_profiles(item, profiles)
                # inject option_ctx into portfolio_ctx for now (minimal change):
                if portfolio_ctx is not None and option_ctx is not None:
                    portfolio_ctx['option_ctx'] = option_ctx
                if not want('scan'):
                    # fetch-only: just pull required_data and stop
                    item_fetch = dict(item)
                    item_fetch['sell_put'] = {'enabled': False}
                    item_fetch['sell_call'] = {'enabled': False}
                    process_symbol(py, base, item_fetch, top_n, portfolio_ctx=None, fx_usd_per_cny=None, hkdcny=None, timeout_sec=symbol_timeout_sec)
                else:
                    summary_rows.extend(process_symbol(py, base, item, top_n, portfolio_ctx=portfolio_ctx, fx_usd_per_cny=fx_usd_per_cny, hkdcny=hkdcny, timeout_sec=symbol_timeout_sec))
            except Exception as e:
                symbol = item.get('symbol', 'UNKNOWN')
                log(f'[WARN] {symbol} processing failed: {e}')
                summary_rows.append({
                    'symbol': symbol,
                    'strategy': 'sell_put',
                    'candidate_count': 0,
                    'top_contract': '',
                    'expiration': '',
                    'strike': None,
                    'dte': None,
                    'net_income': None,
                    'annualized_return': None,
                    'risk_label': '',
                    'note': f'处理失败: {e}',
                })
                summary_rows.append({
                    'symbol': symbol,
                    'strategy': 'sell_call',
                    'candidate_count': 0,
                    'top_contract': '',
                    'expiration': '',
                    'strike': None,
                    'dte': None,
                    'net_income': None,
                    'annualized_return': None,
                    'risk_label': '',
                    'note': f'处理失败: {e}',
                })
            symbols.append(item['symbol'])

        # fetch-only stage: stop after market-data fetch
        # (but do not interfere with stage-only late-stage runs)
        if (STAGE_ONLY is None) and (not want('scan')):
            log(f"[INFO] stage={STAGE}: fetch done")
            return

        # Write summary directly into report_dir
        build_symbols_summary(summary_rows, report_dir, is_scheduled=IS_SCHEDULED)

        if not IS_SCHEDULED:
            build_symbols_digest(symbols, report_dir)

        changes_out = ('/dev/null' if IS_SCHEDULED else str((report_dir / 'symbols_changes.txt').as_posix()))
        alert_cmd = [
            py, 'scripts/alert_engine.py',
            '--summary-input', str((report_dir / 'symbols_summary.csv').as_posix()),
            '--output', str((report_dir / 'symbols_alerts.txt').as_posix()),
            '--changes-output', changes_out,
        ]
        if not IS_SCHEDULED:
            alert_cmd.extend([
                '--previous-summary', 'output/state/symbols_summary_prev.csv',
                '--update-snapshot',
            ])
        # alert policy overrides (optional)
        try:
            policy = cfg.get('alert_policy')
            if isinstance(policy, dict) and policy:
                p = base / 'output' / 'state' / 'alert_policy.json'
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(json.dumps(policy, ensure_ascii=False, indent=2), encoding='utf-8')
                alert_cmd.extend(['--policy-json', str(p)])
            elif isinstance(policy, str) and policy.strip():
                alert_cmd.extend(['--policy-json', policy.strip()])
        except Exception:
            pass
        if want('alert'):
            run_cmd(alert_cmd, cwd=base, is_scheduled=IS_SCHEDULED)

        if want('notify'):
            run_cmd([
                py, 'scripts/notify_symbols.py',
                '--alerts-input', str((report_dir / 'symbols_alerts.txt').as_posix()),
                '--changes-input', ('/dev/null' if IS_SCHEDULED else str((report_dir / 'symbols_changes.txt').as_posix())),
                '--output', str((report_dir / 'symbols_notification.txt').as_posix()),
            ], cwd=base, is_scheduled=IS_SCHEDULED)

            # Scheduled mode artifact cleanup:
            # If report_dir was overridden, do not touch legacy output/reports.
            if IS_SCHEDULED and (report_dir == (base / 'output' / 'reports').resolve()):
                try:
                    import glob
                    keep = {
                        (base / 'output/reports/symbols_summary.csv').resolve(),
                        (base / 'output/reports/symbols_notification.txt').resolve(),
                    }
                    patterns = [
                        'output/reports/*sell_put_candidates*.csv',
                        'output/reports/*sell_call_candidates*.csv',
                        'output/reports/*sell_put_alerts*.txt',
                        'output/reports/*sell_call_alerts*.txt',
                        'output/reports/symbols_summary.txt',
                        'output/reports/symbols_digest.txt',
                        'output/reports/symbols_alerts.txt',
                        'output/reports/symbols_changes.txt',
                    ]
                    for pat in patterns:
                        for fp in glob.glob(str((base / pat).resolve())):
                            p0 = Path(fp).resolve()
                            if p0 in keep:
                                continue
                            try:
                                if p0.exists():
                                    p0.unlink()
                            except Exception:
                                pass
                except Exception:
                    pass

        # Append cash summaries at the bottom (optional).
        # In multi-account merged notifications, we prefer adding cash footer only once in send_if_needed_multi.py.
        include_cash_footer = True
        try:
            include_cash_footer = bool((cfg.get('notifications') or {}).get('include_cash_footer', True))
        except Exception:
            include_cash_footer = True

        if include_cash_footer and (not IS_SCHEDULED):
            run_cmd([
                py, 'scripts/append_cash_summary.py',
                '--pm-config', str(pm_config),
                '--market', str(market),
                '--accounts', 'lx', 'sy',
                '--notification', str((report_dir / 'symbols_notification.txt').as_posix()),
            ], cwd=base, is_scheduled=IS_SCHEDULED)

        notifications_cfg = cfg.get('notifications', {}) or {}
        if notifications_cfg.get('enabled', False):
            log('[INFO] notifications enabled in config; pipeline prepared notification text for sending.')
        else:
            log('[INFO] notifications disabled; generated notification text only.')
        if not IS_SCHEDULED:
            print('\n[DONE] Symbols pipeline finished')
            print(f"- {report_dir}/symbols_summary.csv")
            print(f"- {report_dir}/symbols_alerts.txt")
            print(f"- {report_dir}/symbols_changes.txt")
            print(f"- {report_dir}/symbols_notification.txt")
            print('')

        return

    top_n = cfg.get('outputs', {}).get('top_n_alerts', 3)
    process_symbol(py, base, cfg, top_n, report_dir=report_dir)
    print('\n[DONE] Single-symbol pipeline finished')
    print(f'- {report_dir}/{{symbol}}_sell_put_candidates*.csv / alerts.txt')
    print(f'- {report_dir}/{{symbol}}_sell_call_candidates.csv / alerts.txt')


if __name__ == '__main__':
    main()
