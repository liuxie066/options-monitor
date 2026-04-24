#!/usr/bin/env python3
"""Manage position lots via trade events.

Supports open, buy-to-close, and list flows on top of the
trade-events / position-lots model.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from scripts.option_positions_core.domain import (
    normalize_account,
    normalize_broker,
)
from src.application.position_workflows import execute_manual_close, execute_manual_open
from src.application.option_positions_facade import (
    format_cash_secured_amount,
    format_position_money,
    list_position_rows,
    resolve_option_positions_repo,
)


def main():
    ap = argparse.ArgumentParser(description='Manage position lots via trade events')
    ap.add_argument('--data-config', default=None, help='portfolio data config path; auto-resolves when omitted')

    sub = ap.add_subparsers(dest='cmd', required=True)

    p_list = sub.add_parser('list', help='list records')
    p_list.add_argument('--broker', default='富途')
    p_list.add_argument('--market', default=None, help='DEPRECATED alias of --broker')
    p_list.add_argument('--account', default=None)
    p_list.add_argument('--status', default='open', choices=['open', 'close', 'all'])
    p_list.add_argument('--format', default='text', choices=['text', 'json'])
    p_list.add_argument('--limit', type=int, default=50)

    p_add = sub.add_parser('add', help='add a record')
    p_add.add_argument('--broker', default='富途')
    p_add.add_argument('--market', default=None, help='DEPRECATED alias of --broker')
    p_add.add_argument('--account', required=True)
    p_add.add_argument('--symbol', required=True)
    p_add.add_argument('--option-type', required=True, choices=['put', 'call'])
    p_add.add_argument('--side', required=True, choices=['short', 'long'])
    p_add.add_argument('--contracts', type=int, required=True)
    p_add.add_argument('--currency', required=True, choices=['USD', 'HKD', 'CNY'])
    p_add.add_argument('--strike', type=float, default=None, help='required for auto cash_secured on short put')
    p_add.add_argument('--multiplier', type=float, default=None, help='default 100 for US; required for HK if strike provided')
    p_add.add_argument('--exp', default=None, help='YYYY-MM-DD (stored in note)')
    p_add.add_argument('--premium-per-share', type=float, default=None, help='stored in note')
    p_add.add_argument('--underlying-share-locked', type=int, default=None, help='for covered call locking shares')
    p_add.add_argument('--note', default=None)
    p_add.add_argument('--dry-run', action='store_true')

    p_buy_close = sub.add_parser('buy-close', help='buy to close a position by record_id')
    p_buy_close.add_argument('--record-id', required=True)
    p_buy_close.add_argument('--contracts', type=int, required=True, help='contracts to close; supports partial close')
    p_buy_close.add_argument('--close-price', type=float, default=None, help='buy-to-close price per share/contract unit')
    p_buy_close.add_argument('--close-reason', default='manual_buy_to_close')
    p_buy_close.add_argument('--dry-run', action='store_true')

    args = ap.parse_args()

    base = Path(__file__).resolve().parents[1]
    _data_config, repo = resolve_option_positions_repo(base=base, data_config=args.data_config)

    if args.cmd == 'list':
        broker = normalize_broker(args.market or args.broker)
        account = normalize_account(args.account) if args.account else None
        rows = list_position_rows(
            repo,
            broker=broker,
            account=account,
            status=args.status,
            limit=args.limit,
        )
        if args.format == 'json':
            print(json.dumps(rows, ensure_ascii=False, indent=2))
            return

        if not rows:
            print('(no records)')
            return
        print('# position_lots')
        for r in rows:
            ccy = (r.get('currency') or 'USD').upper()
            cash_txt = format_cash_secured_amount(r.get('cash_secured_amount'), ccy)
            print(
                f"- {r['record_id']} | {r.get('account')} | {r.get('symbol')} | {r.get('side')} {r.get('option_type')} | "
                f"contracts {r.get('contracts')} open {r.get('contracts_open')} closed {r.get('contracts_closed')} | "
                f"{ccy} cash_secured {cash_txt} | status {r.get('status')}"
            )
        return

    if args.cmd == 'add':
        broker = normalize_broker(args.broker)
        if args.market:
            broker = normalize_broker(args.market)
            print('[WARN] --market is deprecated; use --broker')
        try:
            out = execute_manual_open(
                repo,
                broker=broker,
                account=args.account,
                symbol=args.symbol,
                option_type=args.option_type,
                side=args.side,
                contracts=int(args.contracts),
                currency=args.currency,
                strike=args.strike,
                multiplier=args.multiplier,
                expiration_ymd=((args.exp or '').strip() or None),
                premium_per_share=args.premium_per_share,
                underlying_share_locked=args.underlying_share_locked,
                note=args.note,
                dry_run=bool(args.dry_run),
            )
        except ValueError as e:
            raise SystemExit(str(e))

        fields = out["fields"]
        if args.dry_run:
            print('[DRY_RUN] create fields:')
            print(json.dumps(fields, ensure_ascii=False, indent=2))
            return

        res = out["result"]
        print(f"[DONE] created event_id={res.get('event_id')}")
        if fields.get('cash_secured_amount') is not None:
            print(
                f"cash_secured_amount={format_position_money(float(fields['cash_secured_amount']), fields.get('currency') or '')}"
            )
        return

    if args.cmd == 'buy-close':
        try:
            out = execute_manual_close(
                repo,
                record_id=args.record_id,
                contracts_to_close=int(args.contracts),
                close_price=args.close_price,
                close_reason=args.close_reason,
                dry_run=bool(args.dry_run),
            )
        except ValueError as e:
            raise SystemExit(str(e))
        patch = out["patch"]
        if args.dry_run:
            print('[DRY_RUN] update fields:')
            print(json.dumps(patch, ensure_ascii=False, indent=2))
            return
        res = out["result"]
        print(f"[DONE] buy-closed {args.record_id} contracts={int(args.contracts)} event_id={res.get('event_id')}")
        return

if __name__ == '__main__':
    main()
