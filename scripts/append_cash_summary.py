#!/usr/bin/env python3
"""Append cash summary (lx + sy) to the end of an existing notification text.

We do NOT compute any "after buying" remaining cash.
We only compute current base free(CNY) per account.

Input:
- notification text file: <report_dir>/symbols_notification.txt (per-account; default report_dir=output/reports)

Output:
- same file, with a footer appended.

This script is designed to run in the per-account context where ./output symlink points to that account output dir.
"""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

from scripts.io_utils import parse_last_json, money_cny


def run_capture(cmd: list[str], cwd: Path, timeout_sec: int = 120) -> str:
    p = subprocess.run(cmd, cwd=str(cwd), timeout=timeout_sec, capture_output=True, text=True)
    if p.returncode != 0:
        raise SystemExit(p.returncode)
    return p.stdout or ''


def main():
    ap = argparse.ArgumentParser(description='Append lx/sy cash summary to notification footer')
    ap.add_argument('--pm-config', default='../portfolio-management/config.json')
    ap.add_argument('--market', default='富途')
    ap.add_argument('--accounts', nargs='+', default=['lx', 'sy'])
    ap.add_argument('--report-dir', default='output/reports', help='Report dir for default notification path (default: output/reports)')
    ap.add_argument('--notification', default=None, help='Notification text file (default: <report-dir>/symbols_notification.txt)')
    args = ap.parse_args()

    base = Path(__file__).resolve().parents[1]

    report_dir = Path(args.report_dir)
    if not report_dir.is_absolute():
        report_dir = (base / report_dir).resolve()

    if args.notification:
        notif_path = Path(args.notification)
        if not notif_path.is_absolute():
            notif_path = (base / notif_path).resolve()
    else:
        notif_path = (report_dir / 'symbols_notification.txt').resolve()

    text = notif_path.read_text(encoding='utf-8').strip() if notif_path.exists() else ''

    # Remove any previous cash footer blocks to keep the file clean and idempotent.
    # We support older formats as well.
    def _strip_old_cash_blocks(t: str) -> str:
        if not t:
            return t
        lines = t.splitlines()
        cut = len(lines)
        headers = {
            '现金结余:',
            '现金（holding表，CNY）:',
            '现金（CNY）:',
        }
        for i, ln in enumerate(lines):
            if ln.strip() in headers:
                cut = i
                break
        return '\n'.join(lines[:cut]).rstrip()

    text = _strip_old_cash_blocks(text)

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
        # Show BOTH:
        # - holding-table cash (cash_available_cny)
        # - free cash after subtracting put cash-secured (cash_free_cny)
        rows.append((
            acct.upper(),
            payload.get('cash_available_cny'),
            payload.get('cash_free_cny'),
        ))

    footer = []
    footer.append('现金（CNY）:')
    for acct, avail_cny, free_cny in rows:
        footer.append(f"{acct}: holding {money_cny(avail_cny)} | free {money_cny(free_cny)}")

    new_text = (text + '\n\n' + '\n'.join(footer).strip() + '\n').strip() + '\n'
    notif_path.write_text(new_text, encoding='utf-8')


if __name__ == '__main__':
    raise SystemExit(main())
