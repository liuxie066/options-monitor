#!/usr/bin/env python3
"""Append cash summary (lx + sy) to the end of an existing notification text.

We do NOT compute any "after buying" remaining cash.
We only compute current base free(CNY) per account.

Input:
- notification text file: output/reports/symbols_notification.txt (per-account)

Output:
- same file, with a footer appended.

This script is designed to run in the per-account context where ./output symlink points to that account output dir.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path


def run_capture(cmd: list[str], cwd: Path, timeout_sec: int = 120) -> str:
    p = subprocess.run(cmd, cwd=str(cwd), timeout=timeout_sec, capture_output=True, text=True)
    if p.returncode != 0:
        raise SystemExit(p.returncode)
    return p.stdout or ''


def parse_last_json(stdout: str) -> dict:
    lines = (stdout or '').splitlines()
    buf = []
    for ln in reversed(lines):
        if not ln.strip():
            continue
        buf.append(ln)
        if ln.strip().startswith('{'):
            break
    txt = '\n'.join(reversed(buf)).strip()
    return json.loads(txt)


def money_cny(v) -> str:
    try:
        if v is None:
            return '-'
        return f"¥{float(v):,.0f}"
    except Exception:
        return '-'


def main():
    ap = argparse.ArgumentParser(description='Append lx/sy cash summary to notification footer')
    ap.add_argument('--pm-config', default='../portfolio-management/config.json')
    ap.add_argument('--market', default='富途')
    ap.add_argument('--accounts', nargs='+', default=['lx', 'sy'])
    ap.add_argument('--notification', default='output/reports/symbols_notification.txt')
    args = ap.parse_args()

    base = Path(__file__).resolve().parents[1]
    notif_path = base / args.notification
    text = notif_path.read_text(encoding='utf-8').strip() if notif_path.exists() else ''

    py = str(base / '.venv' / 'bin' / 'python')

    rows = []
    for acct in args.accounts:
        out = run_capture([
            py, 'scripts/query_sell_put_cash.py',
            '--pm-config', str((base / args.pm_config).resolve()) if not Path(args.pm_config).is_absolute() else str(args.pm_config),
            '--market', args.market,
            '--account', acct,
            '--format', 'json',
        ], cwd=base, timeout_sec=180)
        payload = parse_last_json(out)
        rows.append((acct.upper(), payload.get('cash_free_cny')))

    footer = []
    footer.append('现金结余:')
    for acct, free_cny in rows:
        footer.append(f"{acct}账户 base free(CNY): {money_cny(free_cny)}")

    new_text = (text + '\n\n' + '\n'.join(footer).strip() + '\n').strip() + '\n'
    notif_path.write_text(new_text, encoding='utf-8')


if __name__ == '__main__':
    raise SystemExit(main())
