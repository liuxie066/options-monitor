#!/usr/bin/env python3
from __future__ import annotations

# Allow running as a script without installation.
# When executed as `python scripts/run_pipeline.py`, ensure repo root is on sys.path
# so `import scripts.*` works consistently.
import sys
from pathlib import Path

repo_base = Path(__file__).resolve().parents[1]
if str(repo_base) not in sys.path:
    sys.path.insert(0, str(repo_base))

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

from scripts.report_builders import build_symbols_digest, build_symbols_summary


LOG = __import__('scripts.logging_config', fromlist=['get_logger']).get_logger('run_pipeline')


def log(msg: str) -> None:
    try:
        if msg.startswith('[WARN]'):
            LOG.warning(msg)
        elif msg.startswith('[INFO]'):
            LOG.info(msg)
        elif msg.startswith('[ERR]'):
            LOG.error(msg)
        else:
            LOG.info(msg)
    except Exception:
        print(msg)


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

    from scripts.config_loader import load_config

    cfg = load_config(
        base=base,
        config_path=cfg_path,
        is_scheduled=IS_SCHEDULED,
        log=log,
    )

    py = sys.executable

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
            from scripts.pipeline_alert_steps import run_stage_only_alert_notify

            run_stage_only_alert_notify(
                py=py,
                base=base,
                report_dir=report_dir,
                is_scheduled=IS_SCHEDULED,
                stage_only=STAGE_ONLY,
                want=want,
                log=log,
            )
            return

        # Ensure per-run report dir exists.
        report_dir.mkdir(parents=True, exist_ok=True)

        from scripts.pipeline_context import build_pipeline_context
        from scripts.pipeline_watchlist import run_watchlist_pipeline

        summary_rows = run_watchlist_pipeline(
            py=py,
            base=base,
            cfg=cfg,
            report_dir=report_dir,
            is_scheduled=IS_SCHEDULED,
            top_n=top_n,
            symbol_timeout_sec=symbol_timeout_sec,
            portfolio_timeout_sec=portfolio_timeout_sec,
            want_scan=want('scan'),
            no_context=bool(getattr(args, 'no_context', False)),
            symbols_arg=getattr(args, 'symbols', None),
            log=log,
            want_fn=want,
            apply_profiles_fn=apply_profiles,
            process_symbol_fn=process_symbol,
            build_pipeline_context_fn=build_pipeline_context,
            build_symbols_summary_fn=lambda rows: build_symbols_summary(rows, report_dir, is_scheduled=IS_SCHEDULED),
            build_symbols_digest_fn=lambda rows, n: (None if IS_SCHEDULED else build_symbols_digest([r.get('symbol') for r in rows if r.get('symbol')], report_dir)),
        )

        symbols = [r.get('symbol') for r in summary_rows if r.get('symbol')]

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
    process_symbol(py, base, cfg, top_n, report_dir=report_dir, is_scheduled=IS_SCHEDULED)
    print('\n[DONE] Single-symbol pipeline finished')
    print(f'- {report_dir}/{{symbol}}_sell_put_candidates*.csv / alerts.txt')
    print(f'- {report_dir}/{{symbol}}_sell_call_candidates.csv / alerts.txt')


if __name__ == '__main__':
    main()
