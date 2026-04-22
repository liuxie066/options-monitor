#!/usr/bin/env python3
from __future__ import annotations

# Allow running as a script without installation.
# When executed as `python scripts/run_pipeline.py`, ensure repo root is on sys.path
# so `import scripts.*` works consistently.
import sys
from pathlib import Path

from scripts.config_loader import load_config, resolve_pm_config_path, resolve_watchlist_config

repo_base = Path(__file__).resolve().parents[1]
if str(repo_base) not in sys.path:
    sys.path.insert(0, str(repo_base))

import argparse
import json
import sys
from pathlib import Path

from scripts.config_profiles import apply_profiles
from scripts.account_config import cash_footer_accounts_from_config
from scripts.pipeline_symbol import process_symbol
from scripts.subprocess_utils import run_cmd

import pandas as pd

from scripts.report_builders import build_symbols_digest, build_symbols_summary
try:
    from domain.storage.repositories import report_repo
except Exception:
    from scripts.domain.storage.repositories import report_repo  # type: ignore

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
    parser.add_argument('--config', required=True, help='Path to JSON config with symbols[]. YAML is legacy.')
    parser.add_argument('--mode', default='dev', choices=['dev', 'scheduled'], help='Runtime mode: dev (verbose) vs scheduled (fast)')
    parser.add_argument('--symbols', default=None, help='Comma-separated symbol whitelist; only process these symbols')
    parser.add_argument('--stage', default='all', choices=['fetch','scan','alert','notify','all'], help='Pipeline stage: fetch|scan|alert|notify|all (dev speed; runs up to this stage)')
    parser.add_argument('--stage-only', default=None, choices=['alert','notify'], help='Run ONLY a late stage (no fetch/scan). Requires existing output files.')
    parser.add_argument('--refresh-multiplier-cache', action='store_true', help='Refresh output_shared/state/multiplier_cache.json via OpenD before running (best-effort).')
    parser.add_argument('--no-context', action='store_true', help='Skip portfolio/option_positions context fetch (dev speed). Useful when tuning filters only.')
    parser.add_argument('--shared-required-data', default=None, help='Path to shared required_data directory (contains raw/ and parsed/). If set, it is authoritative and fetch is skipped when artifacts exist.')
    # New flow (transitional): allow redirecting report outputs away from ./output/reports
    parser.add_argument('--report-dir', default=None, help='Directory to write reports (symbols_summary/alerts/notification). Default: output/reports')
    # New flow (Stage 4): allow redirecting state/context cache away from ./output/state
    parser.add_argument('--state-dir', default=None, help='Directory to read/write state cache (portfolio_context/option_positions_context/rate_cache/etc). Default: output/state')
    parser.add_argument('--shared-context-dir', default=None, help='Optional shared context cache directory for cross-account reuse within one tick')
    # Backward-compatible no-op flags (legacy shared scan plumbing removed)
    parser.add_argument('--shared-scan-dir', default=None, help='[no-op] legacy compatibility flag')
    parser.add_argument('--reuse-shared-scan', action='store_true', help='[no-op] legacy compatibility flag')
    args = parser.parse_args()

    runtime_mode = str(args.mode)
    is_scheduled = (runtime_mode == 'scheduled')
    stage = str(args.stage)
    stage_only = (str(args.stage_only) if args.stage_only else None)

    global RUNTIME_MODE, IS_SCHEDULED, STAGE, STAGE_ONLY
    RUNTIME_MODE = runtime_mode
    IS_SCHEDULED = is_scheduled
    STAGE = stage
    STAGE_ONLY = stage_only

    # stage semantics helper
    # - stage=fetch  => fetch only (no scan/alert/notify)
    # - stage=scan   => fetch + scan (no alert/notify)
    # - stage=alert  => fetch + scan + alert
    # - stage=notify => fetch + scan + alert + notify
    # - stage=all    => same as notify
    def want(step: str) -> bool:
        s = str(step or '').strip().lower()
        if not s:
            return False

        # stage-only late-stage runner (no fetch/scan)
        if STAGE_ONLY is not None:
            if s == 'alert':
                return STAGE_ONLY == 'alert'
            if s == 'notify':
                return STAGE_ONLY == 'notify'
            return False

        order = {'fetch': 0, 'scan': 1, 'alert': 2, 'notify': 3, 'all': 3}
        cur = order.get(str(STAGE or 'all'), 3)
        need = order.get(s)
        if need is None:
            return False
        return cur >= need

    global SHARED_REQUIRED_DATA
    SHARED_REQUIRED_DATA = (str(args.shared_required_data) if getattr(args, 'shared_required_data', None) else None)

    base = Path(__file__).resolve().parents[1]
    cfg_path = Path(args.config)

    # report_dir override (new flow: write into run_dir/accounts/<acct>/)
    report_dir, state_dir = report_repo.prepare_dirs(
        base=base,
        report_dir=getattr(args, 'report_dir', None),
        state_dir=getattr(args, 'state_dir', None),
    )
    shared_context_dir = (Path(args.shared_context_dir).resolve() if getattr(args, 'shared_context_dir', None) else None)

    # Manual multiplier cache refresh (best-effort)
    if bool(getattr(args, 'refresh_multiplier_cache', False)):
        try:
            from scripts import multiplier_cache
            from domain.domain.fetch_source import is_futu_fetch_source
            cache_path = multiplier_cache.default_cache_path(base)
            cfg0 = json.loads(cfg_path.read_text(encoding='utf-8'))
            syms = [
                it for it in resolve_watchlist_config(cfg0)
                if is_futu_fetch_source((it.get('fetch') or {}).get('source'))
            ]
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

    cfg = load_config(
        base=base,
        config_path=cfg_path,
        is_scheduled=IS_SCHEDULED,
        log=log,
    )

    py = sys.executable

    if 'symbols' in cfg:
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
                state_dir=state_dir,
                is_scheduled=IS_SCHEDULED,
                stage_only=STAGE_ONLY,
                want=want,
                log=log,
            )
            return

        # Ensure per-run report dir exists.
        report_repo.ensure_report_dir(report_dir)

        from scripts.pipeline_context import build_pipeline_context
        from scripts.pipeline_watchlist import run_watchlist_pipeline

        # required_data_dir contract:
        # - when --shared-required-data is set, treat it as the authoritative required_data root
        # - otherwise fall back to legacy ./output
        required_data_dir = (Path(SHARED_REQUIRED_DATA).resolve() if SHARED_REQUIRED_DATA else (base / 'output').resolve())

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
            process_symbol_fn=(
                lambda *a, **kw: process_symbol(
                    *a,
                    **kw,
                    required_data_dir=required_data_dir,
                    report_dir=report_dir,
                    state_dir=state_dir,
                )
            ),
            build_pipeline_context_fn=(
                lambda **kw: build_pipeline_context(
                    **kw,
                    state_dir=state_dir,
                    shared_state_dir=shared_context_dir,
                )
            ),
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
            py, 'scripts/cli/alert_engine_cli.py',
            '--summary-input', str((report_dir / 'symbols_summary.csv').as_posix()),
            '--output', str((report_dir / 'symbols_alerts.txt').as_posix()),
            '--changes-output', changes_out,
            '--state-dir', str(state_dir),
        ]
        if not IS_SCHEDULED:
            alert_cmd.extend([
                '--previous-summary', str((state_dir / 'symbols_summary_prev.csv').as_posix()),
                '--update-snapshot',
            ])
        # alert policy overrides (optional)
        try:
            policy = cfg.get('alert_policy')
            if isinstance(policy, dict) and policy:
                p = (state_dir / 'alert_policy.json').resolve()
                report_repo.write_state_json_text(state_dir, 'alert_policy.json', policy)
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
                '--state-dir', str(state_dir),
            ], cwd=base, is_scheduled=IS_SCHEDULED)

            # Scheduled mode artifact cleanup:
            # If report_dir was overridden, do not touch legacy output/reports.
            if IS_SCHEDULED and (report_dir == (base / 'output' / 'reports').resolve()):
                try:
                    import glob
                    keep = {
                        (report_dir / 'symbols_summary.csv').resolve(),
                        (report_dir / 'symbols_notification.txt').resolve(),
                    }
                    patterns = [
                        str((report_dir / '*sell_put_candidates*.csv').resolve()),
                        str((report_dir / '*sell_call_candidates*.csv').resolve()),
                        str((report_dir / '*sell_put_alerts*.txt').resolve()),
                        str((report_dir / '*sell_call_alerts*.txt').resolve()),
                        str((report_dir / 'symbols_summary.txt').resolve()),
                        str((report_dir / 'symbols_digest.txt').resolve()),
                        str((report_dir / 'symbols_alerts.txt').resolve()),
                        str((report_dir / 'symbols_changes.txt').resolve()),
                    ]
                    for pat in patterns:
                        for fp in glob.glob(pat):
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
        # Keep behavior-compatible defaults: fallback to default pm config/market when not present.
        portfolio_cfg = cfg.get('portfolio', {}) or {}
        pm_config = str(resolve_pm_config_path(base=base, pm_config=portfolio_cfg.get('pm_config')))
        market = str(portfolio_cfg.get('market', '富途'))

        include_cash_footer = True
        try:
            include_cash_footer = bool((cfg.get('notifications') or {}).get('include_cash_footer', True))
        except Exception:
            include_cash_footer = True

        if include_cash_footer and (not IS_SCHEDULED):
            cash_footer_accounts = cash_footer_accounts_from_config(cfg)
            run_cmd([
                py, 'scripts/append_cash_summary.py',
                '--config', str(cfg_path),
                '--pm-config', str(pm_config),
                '--market', str(market),
                '--accounts', *cash_footer_accounts,
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
    process_symbol(py, base, cfg, top_n, report_dir=report_dir, state_dir=state_dir, is_scheduled=IS_SCHEDULED)
    print('\n[DONE] Single-symbol pipeline finished')
    print(f'- {report_dir}/{{symbol}}_sell_put_candidates*.csv / alerts.txt')
    print(f'- {report_dir}/{{symbol}}_sell_call_candidates.csv / alerts.txt')


if __name__ == '__main__':
    main()
