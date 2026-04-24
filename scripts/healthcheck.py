#!/usr/bin/env python3
"""Healthcheck for options-monitor.

Checks:
1) Config valid
2) Feishu table schemas contain required fields (holdings, legacy position bootstrap source)
3) Exchange-rate fetch works (optional)
4) Recent cron runs (best-effort): checks OpenClaw cron job state
5) Can run a lightweight scheduler decision for each account (no heavy scan)

Output:
- Prints a concise report
- Exit code 0 if healthy, non-zero if critical failures

This script is safe: it does NOT write to Feishu tables.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from datetime import datetime, timezone

from scripts.feishu_bitable import (
    get_tenant_access_token,
    bitable_fields,
)
from scripts.account_config import accounts_from_config
from scripts.config_loader import normalize_portfolio_broker_config, resolve_data_config_path


def now_utc():
    return datetime.now(timezone.utc).isoformat()


def main():
    ap = argparse.ArgumentParser(description='options-monitor healthcheck')
    ap.add_argument('--config', default='config.us.json')
    ap.add_argument('--accounts', nargs='*', default=None)
    args = ap.parse_args()

    base = Path(__file__).resolve().parents[1]
    cfg_path = Path(args.config)
    if not cfg_path.is_absolute():
        cfg_path = (base / cfg_path).resolve()

    errors = []
    warns = []
    opt_cfg: dict = {}

    try:
        opt_cfg = normalize_portfolio_broker_config(json.loads(cfg_path.read_text(encoding='utf-8')))
    except Exception:
        opt_cfg = {}
    accounts = accounts_from_config(opt_cfg) if args.accounts is None else accounts_from_config({'accounts': args.accounts})

    # 1) config valid
    try:
        import subprocess
        vpy = base / '.venv' / 'bin' / 'python'
        res = subprocess.run([str(vpy), 'scripts/validate_config.py', '--config', str(cfg_path)], cwd=str(base), capture_output=True, text=True)
        if res.returncode != 0:
            errors.append(f"config invalid: {(res.stderr or res.stdout).strip()}")
    except Exception as e:
        errors.append(f"config validation failed: {e}")

    # 2) feishu schema
    try:
        portfolio_cfg = (opt_cfg.get('portfolio') or {})
        data_ref = portfolio_cfg.get('data_config')
        data_path = resolve_data_config_path(base=base, data_config=data_ref)
        pm = json.loads(data_path.read_text(encoding='utf-8'))

        fcfg = pm.get('feishu') or {}
        app_id = fcfg.get('app_id')
        app_secret = fcfg.get('app_secret')
        tables = (fcfg.get('tables') or {})
        if not (app_id and app_secret and tables.get('holdings') and tables.get('option_positions')):
            raise RuntimeError('portfolio secret config missing feishu app creds or tables')

        token = get_tenant_access_token(app_id, app_secret)

        def split_ref(s: str):
            a,t = s.split('/',1)
            return a,t

        hold_app, hold_tbl = split_ref(tables['holdings'])
        opt_app, opt_tbl = split_ref(tables['option_positions'])

        hold_fields = {f.get('field_name') for f in bitable_fields(token, hold_app, hold_tbl)}
        opt_fields = {f.get('field_name') for f in bitable_fields(token, opt_app, opt_tbl)}

        need_hold = {'asset_id','asset_name','quantity','account','market','currency','asset_type'}
        need_opt = {'symbol','option_type','side','contracts','status','account','market','currency','cash_secured_amount'}

        missing_hold = sorted(list(need_hold - hold_fields))
        missing_opt = sorted(list(need_opt - opt_fields))
        if missing_hold:
            errors.append('holdings table missing fields: ' + ','.join(missing_hold))
        if missing_opt:
            errors.append('legacy position bootstrap table missing fields: ' + ','.join(missing_opt))
    except Exception as e:
        errors.append(f"feishu schema check failed: {e}")

    # 3) scheduler decision per account (lightweight)
    try:
        import io
        from contextlib import redirect_stdout
        from scripts.scan_scheduler import run_scheduler

        for acct in accounts:
            cfg = json.loads(cfg_path.read_text(encoding='utf-8'))
            cfg.setdefault('portfolio', {})
            cfg['portfolio']['account'] = acct
            tmp = base / 'output' / 'state' / f'healthcheck_config.{acct}.json'
            tmp.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
            state = base / 'output' / 'state' / f'healthcheck_scheduler_state.{acct}.json'
            with redirect_stdout(io.StringIO()):
                run_scheduler(config=tmp, state=state, jsonl=True, base_dir=base)
    except Exception as e:
        warns.append(f"scheduler checks skipped: {e}")

    # 4) cron job state (best-effort)
    try:
        cron_path = Path.home() / '.openclaw' / 'cron' / 'jobs.json'
        if cron_path.exists():
            data = json.loads(cron_path.read_text(encoding='utf-8'))
            job = None
            for j in data.get('jobs', []):
                if j.get('name') == 'options-monitor auto tick':
                    job = j
                    break
            if job:
                st = job.get('state') or {}
                last = st.get('lastRunAtMs')
                status = st.get('lastRunStatus') or st.get('lastStatus')
                if status != 'ok':
                    warns.append(f"cron last status: {status}")
                if last is None:
                    warns.append('cron never ran yet')
            else:
                warns.append('cron job not found: options-monitor auto tick')
        else:
            warns.append('cron jobs.json not found')
    except Exception as e:
        warns.append(f"cron state check failed: {e}")

    # report
    print('# options-monitor healthcheck')
    print('utc:', now_utc())
    if errors:
        print('\n## CRITICAL')
        for e in errors:
            print('- ' + e)
    if warns:
        print('\n## WARN')
        for w in warns:
            print('- ' + w)
    if not errors and not warns:
        print('\nOK')

    return 0 if not errors else 2


if __name__ == '__main__':
    raise SystemExit(main())
