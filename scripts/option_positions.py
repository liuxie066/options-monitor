#!/usr/bin/env python3
"""Manage option_positions records.

Supports open, buy-to-close (including partial close), auto-close-compatible
fields, list, and low-level edit.

Primary storage is SQLite; Feishu is an optional best-effort backup.
This script still uses Feishu app_id/app_secret from --pm-config when backup sync is enabled.
New deployments should prefer secrets/portfolio.feishu.json or OM_PM_CONFIG.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from scripts.config_loader import resolve_pm_config_path
from scripts.feishu_bitable import (
    bitable_create_record,
    bitable_update_record,
    parse_note_kv,
    merge_note,
    safe_float,
)
from scripts.option_positions_core.domain import (
    OpenPositionCommand,
    build_buy_to_close_patch,
    build_open_fields,
    calc_cash_secured,
    effective_contracts_open,
    normalize_account,
    normalize_broker,
    normalize_close_type,
    normalize_currency,
    normalize_option_type,
    normalize_side,
    normalize_status,
)
from scripts.option_positions_core.service import (
    buy_to_close_position,
    load_option_positions_repo,
    open_position,
)


def format_money(v: float | None, ccy: str) -> str:
    if v is None:
        return '-'
    c = ccy.upper()
    if c == 'USD':
        return f"${v:,.2f}"
    if c == 'HKD':
        return f"HKD {v:,.2f}"
    if c == 'CNY':
        return f"¥{v:,.2f}"
    return f"{v:,.2f} {c}"


def main():
    ap = argparse.ArgumentParser(description='Manage option_positions storage')
    ap.add_argument('--pm-config', default=None, help='Feishu/Bitable credential config path; auto-resolves when omitted')

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

    p_close = sub.add_parser('close', help='DEPRECATED alias: close all remaining contracts by record_id')
    p_close.add_argument('--record-id', required=True)
    p_close.add_argument('--close-price', type=float, default=None)
    p_close.add_argument('--close-reason', default='manual_buy_to_close')
    p_close.add_argument('--dry-run', action='store_true')

    p_edit = sub.add_parser('edit', help='patch fields for a record')
    p_edit.add_argument('--record-id', required=True)
    p_edit.add_argument('--set', action='append', default=[], help='field=value (repeatable)')
    p_edit.add_argument('--dry-run', action='store_true')

    p_sync = sub.add_parser('sync-backup', help='retry Feishu backup sync for SQLite records')
    p_sync.add_argument('--record-id', default=None)
    p_sync.add_argument('--limit', type=int, default=200)
    p_sync.add_argument('--dry-run', action='store_true')

    args = ap.parse_args()

    base = Path(__file__).resolve().parents[1]
    pm_config = resolve_pm_config_path(base=base, pm_config=args.pm_config)

    repo = load_option_positions_repo(pm_config)

    if args.cmd == 'list':
        items = repo.list_records(page_size=200)
        rows = []
        for it in items:
            rid = it.get('record_id')
            f = it.get('fields') or {}
            broker = normalize_broker(args.broker)
            if args.market:
                broker = normalize_broker(args.market)
            if broker and normalize_broker(f.get('broker') or f.get('market')) != broker:
                continue
            if args.account and normalize_account(f.get('account')) != normalize_account(args.account):
                continue
            st = normalize_status(f.get('status'))
            if args.status != 'all' and st != args.status:
                continue
            rows.append({
                'record_id': rid,
                'broker': normalize_broker(f.get('broker') or f.get('market')),
                'account': normalize_account(f.get('account')) or f.get('account'),
                'symbol': f.get('symbol'),
                'option_type': normalize_option_type(f.get('option_type')),
                'side': normalize_side(f.get('side')),
                'contracts': f.get('contracts'),
                'contracts_open': f.get('contracts_open'),
                'contracts_closed': f.get('contracts_closed'),
                'currency': normalize_currency(f.get('currency')),
                'cash_secured_amount': f.get('cash_secured_amount'),
                'underlying_share_locked': f.get('underlying_share_locked'),
                'close_type': normalize_close_type(f.get('close_type')) if f.get('close_type') else None,
                'close_reason': f.get('close_reason'),
                'status': st,
                'note': f.get('note'),
            })
        rows = rows[: max(args.limit, 1)]
        if args.format == 'json':
            print(json.dumps(rows, ensure_ascii=False, indent=2))
            return

        if not rows:
            print('(no records)')
            return
        print('# option_positions')
        for r in rows:
            ccy = (r.get('currency') or 'USD').upper()
            cash = safe_float(r.get('cash_secured_amount'))
            cash_txt = format_money(cash, ccy) if cash is not None else '-'
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
        cmd = OpenPositionCommand(
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
        )
        try:
            fields = build_open_fields(cmd)
        except ValueError as e:
            raise SystemExit(str(e))

        if args.dry_run:
            print('[DRY_RUN] create fields:')
            print(json.dumps(fields, ensure_ascii=False, indent=2))
            return

        res = open_position(repo, cmd)
        rec = (res.get('record') or {})
        rid = rec.get('record_id')
        print(f"[DONE] created record_id={rid}")
        if fields.get('cash_secured_amount') is not None:
            print(f"cash_secured_amount={format_money(float(fields['cash_secured_amount']), fields.get('currency') or '')}")
        return

    if args.cmd == 'buy-close':
        existing = repo.get_record_fields(args.record_id)
        try:
            patch = build_buy_to_close_patch(
                existing,
                contracts_to_close=int(args.contracts),
                close_price=args.close_price,
                close_reason=args.close_reason,
            )
        except ValueError as e:
            raise SystemExit(str(e))
        if args.dry_run:
            print('[DRY_RUN] update fields:')
            print(json.dumps(patch, ensure_ascii=False, indent=2))
            return
        buy_to_close_position(
            repo,
            record_id=args.record_id,
            contracts_to_close=int(args.contracts),
            close_price=args.close_price,
            close_reason=args.close_reason,
        )
        print(f"[DONE] buy-closed {args.record_id} contracts={int(args.contracts)}")
        return

    if args.cmd == 'close':
        print('[WARN] close is deprecated; use buy-close --contracts <remaining>')
        existing = repo.get_record_fields(args.record_id)
        remaining = effective_contracts_open(existing)
        try:
            patch = build_buy_to_close_patch(
                existing,
                contracts_to_close=remaining,
                close_price=args.close_price,
                close_reason=args.close_reason,
            )
        except ValueError as e:
            raise SystemExit(str(e))
        if args.dry_run:
            print('[DRY_RUN] update fields:')
            print(json.dumps(patch, ensure_ascii=False, indent=2))
            return
        buy_to_close_position(
            repo,
            record_id=args.record_id,
            contracts_to_close=remaining,
            close_price=args.close_price,
            close_reason=args.close_reason,
        )
        print(f"[DONE] closed {args.record_id} contracts={remaining}")
        return

    if args.cmd == 'edit':
        if not args.set:
            raise SystemExit('edit requires at least one --set field=value')
        patch = {}
        for s in args.set:
            if '=' not in s:
                raise SystemExit(f"invalid --set: {s}")
            k, v = s.split('=', 1)
            k = k.strip()
            v = v.strip()
            patch[k] = v

        # If user edits strike/multiplier/contracts on a short put, offer recalculation when requested.
        # Minimal: if patch includes strike or multiplier, recompute cash_secured_amount.
        # We need existing record to know side/option_type/currency/contracts.
        items = repo.list_records(page_size=200)
        existing = None
        for it in items:
            if it.get('record_id') == args.record_id:
                existing = it.get('fields') or {}
                break
        if not existing:
            raise SystemExit(f"record not found: {args.record_id}")

        side = normalize_side(existing.get('side'))
        opt_type = normalize_option_type(existing.get('option_type'))

        # merge note if user passes note+= style? Keep simple: if patch has note_append, append.
        if 'note_append' in patch:
            new_note = merge_note(existing.get('note'), {'append': patch.pop('note_append')})
            patch['note'] = new_note

        # Recalc trigger
        recalc = False
        if side == 'short' and opt_type == 'put':
            if any(k in patch for k in ('strike', 'multiplier', 'contracts', 'cash_secured_amount')):
                # If user explicitly sets cash_secured_amount, do not override.
                if 'cash_secured_amount' not in patch and ('strike' in patch or 'multiplier' in patch or 'contracts' in patch):
                    recalc = True

        if recalc:
            # strike/multiplier may be in patch, else in note
            note = patch.get('note') if 'note' in patch else (existing.get('note') or '')
            strike = safe_float(patch.get('strike') or parse_note_kv(note, 'strike'))
            mult = safe_float(patch.get('multiplier') or parse_note_kv(note, 'multiplier'))
            contracts = int(safe_float(patch.get('contracts') or existing.get('contracts') or 0) or 0)
            if strike is None or mult is None or contracts <= 0:
                raise SystemExit('recalc requires strike, multiplier, contracts')
            patch['cash_secured_amount'] = str(calc_cash_secured(strike, mult, contracts))

        # Clean: only allow actual table fields
        allowed = {
            'market','account','symbol','option_type','side','contracts','currency','status',
            'cash_secured_amount','note','underlying_share_locked',
            'contracts_open','contracts_closed','closed_at','last_action_at',
            'close_type','close_reason','close_price','broker','strike','expiration','premium',
        }
        patch2 = {k: v for k, v in patch.items() if k in allowed}
        if 'broker' in patch2:
            patch2['broker'] = normalize_broker(patch2.get('broker'))
        if 'market' in patch2:
            patch2['market'] = normalize_broker(patch2.get('market'))
        if 'account' in patch2:
            patch2['account'] = normalize_account(patch2.get('account'))
        if 'symbol' in patch2:
            patch2['symbol'] = str(patch2.get('symbol') or '').strip().upper()
        try:
            if 'option_type' in patch2:
                patch2['option_type'] = normalize_option_type(patch2.get('option_type'), strict=True)
            if 'side' in patch2:
                patch2['side'] = normalize_side(patch2.get('side'), strict=True)
            if 'status' in patch2:
                patch2['status'] = normalize_status(patch2.get('status'), strict=True)
            if 'currency' in patch2:
                patch2['currency'] = normalize_currency(patch2.get('currency'), strict=True)
            if 'close_type' in patch2 and patch2.get('close_type'):
                patch2['close_type'] = normalize_close_type(patch2.get('close_type'), strict=True)
        except ValueError as e:
            raise SystemExit(str(e))

        if args.dry_run:
            print('[DRY_RUN] update fields:')
            print(json.dumps(patch2, ensure_ascii=False, indent=2))
            return

        repo.update_record(args.record_id, patch2)
        print(f"[DONE] updated {args.record_id}")
        return

    if args.cmd == 'sync-backup':
        result = repo.sync_backup(
            limit=max(int(args.limit), 1),
            record_id=((args.record_id or '').strip() or None),
            dry_run=bool(args.dry_run),
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return


if __name__ == '__main__':
    main()
