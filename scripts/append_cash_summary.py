#!/usr/bin/env python3
"""Append per-account cash summary to the end of an existing notification text.

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
import json
from pathlib import Path

from scripts.account_config import cash_footer_accounts_from_config
from scripts.config_loader import resolve_data_config_path
from scripts.io_utils import money_cny
from scripts.query_sell_put_cash import query_sell_put_cash


def main():
    ap = argparse.ArgumentParser(description='Append per-account cash summary to notification footer')
    ap.add_argument('--config', default=None, help='options-monitor config used to resolve default accounts')
    ap.add_argument('--data-config', default=None, help='portfolio data config path; auto-resolves when omitted')
    ap.add_argument('--market', default='富途')
    ap.add_argument('--accounts', nargs='+', default=None)
    ap.add_argument('--report-dir', default='output/reports', help='Report dir for default notification path (default: output/reports)')
    ap.add_argument('--notification', default=None, help='Notification text file (default: <report-dir>/symbols_notification.txt)')
    args = ap.parse_args()

    base = Path(__file__).resolve().parents[1]
    cfg = {}
    cfg_path: Path | None = None
    if args.config:
        cfg_path = Path(args.config)
        if not cfg_path.is_absolute():
            cfg_path = (base / cfg_path).resolve()
        try:
            cfg = json.loads(cfg_path.read_text(encoding='utf-8'))
        except Exception:
            cfg = {}
    accounts = (
        cash_footer_accounts_from_config(cfg)
        if args.accounts is None
        else cash_footer_accounts_from_config({'notifications': {'cash_footer_accounts': args.accounts}})
    )

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

    data_config = resolve_data_config_path(base=base, data_config=args.data_config)

    rows = []
    for acct in accounts:
        payload = query_sell_put_cash(
            config=(str(cfg_path) if cfg_path is not None else None),
            data_config=str(data_config),
            market=args.market,
            account=acct,
            output_format='json',
            out_dir='output/state',
            base_dir=base,
        )
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
