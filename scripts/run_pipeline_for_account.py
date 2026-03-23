#!/usr/bin/env python3
"""Run the core pipeline for a specific account, filtered by per-symbol accounts.

This is used by send_if_needed_multi.py so each account run:
- filters watchlist symbols by symbol.accounts (if provided)
- uses portfolio.account=<acct>

Default behavior:
- If symbol has no 'accounts' field -> run for all accounts
- If symbol.accounts contains the current account -> run
- Else -> skip
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _norm(s: str) -> str:
    return str(s).strip().lower()


def main():
    ap = argparse.ArgumentParser(description='Run pipeline for a given account with watchlist filtering')
    ap.add_argument('--config', required=True, help='path to base config.json')
    ap.add_argument('--account', required=True, help='account (e.g. lx/sy)')
    ap.add_argument('--out-config', default=None, help='write filtered config to this path (optional)')
    args = ap.parse_args()

    base = Path(__file__).resolve().parents[1]

    cfg_path = Path(args.config)
    if not cfg_path.is_absolute():
        cfg_path = (base / cfg_path).resolve()

    cfg = json.loads(cfg_path.read_text(encoding='utf-8'))

    acct = _norm(args.account)
    cfg.setdefault('portfolio', {})
    cfg['portfolio']['account'] = args.account

    # filter symbols
    watchlist = cfg.get('symbols')
    if watchlist is None:
        watchlist = cfg.get('watchlist')
    if not isinstance(watchlist, list):
        raise SystemExit('[CONFIG_ERROR] symbols[] is required')

    filtered = []
    for item in watchlist:
        if not isinstance(item, dict):
            continue
        allowed = item.get('accounts')
        if not allowed:
            filtered.append(item)
            continue
        # allowed can be a string or list
        if isinstance(allowed, str):
            ok = (_norm(allowed) == acct)
        else:
            ok = any(_norm(x) == acct for x in (allowed or []))
        if ok:
            filtered.append(item)

    # keep both legacy + aliasing behavior in run_pipeline.py
    cfg['symbols'] = filtered

    # optionally write filtered config for inspection
    if args.out_config:
        outp = Path(args.out_config)
        if not outp.is_absolute():
            outp = (base / outp).resolve()
        outp.parent.mkdir(parents=True, exist_ok=True)
        outp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print(f"[DONE] filtered config -> {outp} (symbols={len(filtered)})")

    # delegate to run_pipeline.py
    import subprocess
    vpy = base / '.venv' / 'bin' / 'python'
    res = subprocess.run([str(vpy), 'scripts/run_pipeline.py', '--config', str(cfg_path if not args.out_config else outp)], cwd=str(base))
    raise SystemExit(res.returncode)


if __name__ == '__main__':
    main()
