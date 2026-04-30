#!/usr/bin/env python3
"""Manage position lots via trade events.

Supports open, buy-to-close, and list flows on top of the
trade-events / position-lots model.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from scripts.option_positions_core.ledger import project_position_lot_records_with_diagnostics
from scripts.option_positions_core.domain import (
    exp_ms_to_datetime,
    normalize_account,
    normalize_broker,
)
from scripts.option_positions_core.service import rebuild_position_lots_from_trade_events
from scripts.option_positions_core.service import persist_manual_void_event
from src.application.position_workflows import execute_manual_adjust, execute_manual_close, execute_manual_open
from src.application.option_positions_facade import (
    format_cash_secured_amount,
    format_position_money,
    list_position_rows,
    resolve_option_positions_repo,
)


def build_lot_event_history(repo, *, record_id: str) -> list[dict[str, object]]:
    fields = repo.get_record_fields(record_id)
    events = repo.list_trade_events()
    seed_ids = {
        str(fields.get('source_event_id') or '').strip(),
        str(fields.get('last_close_event_id') or '').strip(),
    }
    seed_ids.discard("")
    for event in events:
        payload = event.get('raw_payload') or {}
        if not isinstance(payload, dict):
            continue
        if str(payload.get('record_id') or '').strip() == str(record_id).strip():
            event_id = str(event.get('event_id') or '').strip()
            if event_id:
                seed_ids.add(event_id)
    selected_ids = set(seed_ids)
    changed = True
    while changed:
        changed = False
        for event in events:
            event_id = str(event.get('event_id') or '').strip()
            payload = event.get('raw_payload') or {}
            if not isinstance(payload, dict):
                payload = {}
            adjust_target_source_event_id = str(payload.get('adjust_target_source_event_id') or '').strip()
            void_target_event_id = str(payload.get('void_target_event_id') or '').strip()
            if event_id in selected_ids or adjust_target_source_event_id in selected_ids or void_target_event_id in selected_ids:
                before = len(selected_ids)
                if event_id:
                    selected_ids.add(event_id)
                if adjust_target_source_event_id:
                    selected_ids.add(adjust_target_source_event_id)
                if void_target_event_id:
                    selected_ids.add(void_target_event_id)
                changed = changed or len(selected_ids) != before

    history: list[dict[str, object]] = []
    for event in events:
        event_id = str(event.get('event_id') or '').strip()
        if event_id not in selected_ids:
            continue
        payload = event.get('raw_payload') or {}
        if not isinstance(payload, dict):
            payload = {}
        history.append(
            {
                'event_id': event_id,
                'trade_time_ms': event.get('trade_time_ms'),
                'source_type': event.get('source_type'),
                'source_name': event.get('source_name'),
                'broker': normalize_broker(event.get('broker')),
                'account': normalize_account(event.get('account')) if event.get('account') else None,
                'symbol': event.get('symbol'),
                'option_type': event.get('option_type'),
                'side': event.get('side'),
                'position_effect': event.get('position_effect'),
                'contracts': event.get('contracts'),
                'price': event.get('price'),
                'strike': event.get('strike'),
                'expiration_ymd': event.get('expiration_ymd'),
                'currency': event.get('currency'),
                'void_target_event_id': payload.get('void_target_event_id'),
                'adjust_target_source_event_id': payload.get('adjust_target_source_event_id'),
                'close_target_source_event_id': payload.get('close_target_source_event_id'),
                'record_id': payload.get('record_id'),
                'patch': payload.get('patch'),
            }
        )
    history.sort(key=lambda row: (int(row.get('trade_time_ms') or 0), str(row.get('event_id') or '')))
    return history


def _matches_lot_selector(
    row: dict[str, object],
    *,
    record_id: str | None,
    feishu_record_id: str | None,
    account: str | None,
    symbol: str | None,
    option_type: str | None,
    strike: float | None,
    expiration_ymd: str | None,
) -> bool:
    row_record_id = str(row.get('record_id') or '').strip()
    fields = row.get('fields') or {}
    if not isinstance(fields, dict):
        return False
    if record_id and row_record_id != str(record_id).strip():
        return False
    if feishu_record_id and str(fields.get('feishu_record_id') or '').strip() != str(feishu_record_id).strip():
        return False
    if account and normalize_account(fields.get('account')) != normalize_account(account):
        return False
    if symbol and str(fields.get('symbol') or '').strip().upper() != str(symbol).strip().upper():
        return False
    if option_type and str(fields.get('option_type') or '').strip().lower() != str(option_type).strip().lower():
        return False
    if strike is not None:
        current_strike = fields.get('strike')
        if current_strike is None or abs(float(current_strike) - float(strike)) >= 1e-9:
            return False
    if expiration_ymd:
        current_expiration_dt = exp_ms_to_datetime(fields.get('expiration'))
        current_expiration = current_expiration_dt.date().isoformat() if current_expiration_dt is not None else str(fields.get('expiration') or '')
        current_note = str(fields.get('note') or '')
        if expiration_ymd not in current_note and expiration_ymd not in current_expiration:
            return False
    return True


def _matches_event_selector(
    event: dict[str, object],
    *,
    record_id: str | None,
    account: str | None,
    symbol: str | None,
    option_type: str | None,
    strike: float | None,
    expiration_ymd: str | None,
) -> bool:
    payload = event.get('raw_payload') or {}
    if not isinstance(payload, dict):
        payload = {}
    if record_id and str(payload.get('record_id') or '').strip() != str(record_id).strip():
        return False
    if account and normalize_account(event.get('account')) != normalize_account(account):
        return False
    if symbol and str(event.get('symbol') or '').strip().upper() != str(symbol).strip().upper():
        return False
    if option_type and str(event.get('option_type') or '').strip().lower() != str(option_type).strip().lower():
        return False
    if strike is not None:
        current_strike = event.get('strike')
        if current_strike is None or abs(float(current_strike) - float(strike)) >= 1e-9:
            return False
    if expiration_ymd and str(event.get('expiration_ymd') or '').strip() != str(expiration_ymd).strip():
        return False
    return True


def inspect_projection_state(
    repo,
    *,
    record_id: str | None = None,
    feishu_record_id: str | None = None,
    account: str | None = None,
    symbol: str | None = None,
    option_type: str | None = None,
    strike: float | None = None,
    expiration_ymd: str | None = None,
) -> dict[str, object]:
    current_rows = repo.list_position_lots()
    events = repo.list_trade_events()
    projection = project_position_lot_records_with_diagnostics(events)

    matched_current = [
        row for row in current_rows
        if _matches_lot_selector(
            row,
            record_id=record_id,
            feishu_record_id=feishu_record_id,
            account=account,
            symbol=symbol,
            option_type=option_type,
            strike=strike,
            expiration_ymd=expiration_ymd,
        )
    ]
    matched_record_ids = {str(row.get('record_id') or '').strip() for row in matched_current if str(row.get('record_id') or '').strip()}
    matched_projected = [
        row for row in projection.lots
        if (
            str(row.get('record_id') or '').strip() in matched_record_ids
            or _matches_lot_selector(
                row,
                record_id=record_id,
                feishu_record_id=None,
                account=account,
                symbol=symbol,
                option_type=option_type,
                strike=strike,
                expiration_ymd=expiration_ymd,
            )
        )
    ]
    matched_record_ids.update(str(row.get('record_id') or '').strip() for row in matched_projected if str(row.get('record_id') or '').strip())

    direct_event_ids = {
        str(event.get('event_id') or '').strip()
        for event in events
        if _matches_event_selector(
            event,
            record_id=record_id,
            account=account,
            symbol=symbol,
            option_type=option_type,
            strike=strike,
            expiration_ymd=expiration_ymd,
        )
    }
    tracked_ids: set[str] = set(matched_record_ids)
    if record_id:
        tracked_ids.add(str(record_id).strip())
    tracked_ids.update(item for item in direct_event_ids if item)
    for row in matched_current + matched_projected:
        fields = row.get('fields') or {}
        if not isinstance(fields, dict):
            continue
        for key in ('source_event_id', 'last_close_event_id'):
            value = str(fields.get(key) or '').strip()
            if value:
                tracked_ids.add(value)

    related_events: list[dict[str, object]] = []
    changed = True
    while changed:
        changed = False
        for event in events:
            event_id = str(event.get('event_id') or '').strip()
            payload = event.get('raw_payload') or {}
            if not isinstance(payload, dict):
                payload = {}
            seeds = {
                event_id,
                str(payload.get('record_id') or '').strip(),
                str(payload.get('close_target_source_event_id') or '').strip(),
                str(payload.get('adjust_target_source_event_id') or '').strip(),
                str(payload.get('void_target_event_id') or '').strip(),
            }
            if tracked_ids.isdisjoint({item for item in seeds if item}):
                continue
            before = len(tracked_ids)
            tracked_ids.update(item for item in seeds if item)
            if len(tracked_ids) != before:
                changed = True

    diagnostic_map: dict[str, list[dict[str, object]]] = {}
    for item in projection.diagnostics:
        diagnostic_map.setdefault(item.event_id, []).append(item.to_dict())

    for event in events:
        event_id = str(event.get('event_id') or '').strip()
        payload = event.get('raw_payload') or {}
        if not isinstance(payload, dict):
            payload = {}
        seeds = {
            event_id,
            str(payload.get('record_id') or '').strip(),
            str(payload.get('close_target_source_event_id') or '').strip(),
            str(payload.get('adjust_target_source_event_id') or '').strip(),
            str(payload.get('void_target_event_id') or '').strip(),
        }
        if event_id not in direct_event_ids and tracked_ids.isdisjoint({item for item in seeds if item}):
            continue
        related_events.append(
            {
                'event_id': event_id,
                'trade_time_ms': event.get('trade_time_ms'),
                'source_type': event.get('source_type'),
                'source_name': event.get('source_name'),
                'broker': event.get('broker'),
                'account': event.get('account'),
                'symbol': event.get('symbol'),
                'option_type': event.get('option_type'),
                'side': event.get('side'),
                'position_effect': event.get('position_effect'),
                'contracts': event.get('contracts'),
                'price': event.get('price'),
                'strike': event.get('strike'),
                'expiration_ymd': event.get('expiration_ymd'),
                'currency': event.get('currency'),
                'record_id': payload.get('record_id'),
                'close_target_source_event_id': payload.get('close_target_source_event_id'),
                'adjust_target_source_event_id': payload.get('adjust_target_source_event_id'),
                'void_target_event_id': payload.get('void_target_event_id'),
                'projection_diagnostics': diagnostic_map.get(event_id, []),
            }
        )
    related_events.sort(key=lambda row: (int(row.get('trade_time_ms') or 0), str(row.get('event_id') or '')))

    filtered_diagnostics = [
        item.to_dict()
        for item in projection.diagnostics
        if str(item.event_id or '').strip() in {str(event.get('event_id') or '').strip() for event in related_events}
    ]
    return {
        'selectors': {
            'record_id': record_id,
            'feishu_record_id': feishu_record_id,
            'account': account,
            'symbol': symbol,
            'option_type': option_type,
            'strike': strike,
            'expiration_ymd': expiration_ymd,
        },
        'matched_record_ids': sorted(matched_record_ids),
        'current_lots': matched_current,
        'projected_lots': matched_projected,
        'related_events': related_events,
        'projection_diagnostics': filtered_diagnostics,
        'all_projection_diagnostic_count': len(projection.diagnostics),
    }


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
    p_list.add_argument('--exp-within-days', type=int, default=None, help='only include rows expiring within N days from today')

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

    p_events = sub.add_parser('events', help='list canonical trade events')
    p_events.add_argument('--broker', default=None)
    p_events.add_argument('--market', default=None, help='DEPRECATED alias of --broker')
    p_events.add_argument('--account', default=None)
    p_events.add_argument('--format', default='text', choices=['text', 'json'])
    p_events.add_argument('--limit', type=int, default=50)

    p_history = sub.add_parser('history', help='show related trade events for a position lot')
    p_history.add_argument('--record-id', required=True)
    p_history.add_argument('--format', default='text', choices=['text', 'json'])

    p_rebuild = sub.add_parser('rebuild', help='rebuild position_lots projection from trade_events')
    p_rebuild.add_argument('--format', default='text', choices=['text', 'json'])

    p_inspect = sub.add_parser('inspect', help='inspect projected lot state and related trade events')
    p_inspect.add_argument('--record-id', default=None)
    p_inspect.add_argument('--feishu-record-id', default=None)
    p_inspect.add_argument('--account', default=None)
    p_inspect.add_argument('--symbol', default=None)
    p_inspect.add_argument('--option-type', default=None, choices=['put', 'call'])
    p_inspect.add_argument('--strike', type=float, default=None)
    p_inspect.add_argument('--exp', default=None, help='YYYY-MM-DD')
    p_inspect.add_argument('--format', default='json', choices=['json'])

    p_void_event = sub.add_parser('void-event', help='append a void event for a canonical trade event')
    p_void_event.add_argument('--event-id', required=True)
    p_void_event.add_argument('--void-reason', default='manual_void')

    p_adjust = sub.add_parser('adjust-lot', help='append an adjustment event for an existing position lot')
    p_adjust.add_argument('--record-id', required=True)
    p_adjust.add_argument('--contracts', type=int, default=None)
    p_adjust.add_argument('--strike', type=float, default=None)
    p_adjust.add_argument('--exp', default=None, help='YYYY-MM-DD')
    p_adjust.add_argument('--premium-per-share', type=float, default=None)
    p_adjust.add_argument('--multiplier', type=float, default=None)
    p_adjust.add_argument('--opened-at-ms', type=int, default=None)
    p_adjust.add_argument('--dry-run', action='store_true')

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
            expiration_within_days=args.exp_within_days,
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
                f"exp {r.get('expiration_ymd') or '-'} | strike {r.get('strike') if r.get('strike') is not None else '-'} | "
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

    if args.cmd == 'events':
        broker = normalize_broker(args.market or args.broker) if (args.market or args.broker) else None
        account = normalize_account(args.account) if args.account else None
        events = repo.list_trade_events()
        rows: list[dict[str, object]] = []
        for event in reversed(events):
            event_broker = normalize_broker(event.get('broker'))
            event_account = normalize_account(event.get('account')) if event.get('account') else None
            if broker and event_broker != broker:
                continue
            if account and event_account != account:
                continue
            rows.append(
                {
                    'event_id': event.get('event_id'),
                    'trade_time_ms': event.get('trade_time_ms'),
                    'source_type': event.get('source_type'),
                    'source_name': event.get('source_name'),
                    'broker': event_broker,
                    'account': event_account,
                    'symbol': event.get('symbol'),
                    'option_type': event.get('option_type'),
                    'side': event.get('side'),
                    'position_effect': event.get('position_effect'),
                    'contracts': event.get('contracts'),
                    'price': event.get('price'),
                    'strike': event.get('strike'),
                    'expiration_ymd': event.get('expiration_ymd'),
                    'currency': event.get('currency'),
                }
            )
            if len(rows) >= max(args.limit, 1):
                break
        if args.format == 'json':
            print(json.dumps(rows, ensure_ascii=False, indent=2))
            return
        if not rows:
            print('(no events)')
            return
        print('# trade_events')
        for row in rows:
            print(
                f"- {row.get('event_id')} | {row.get('account')} | {row.get('symbol')} | "
                f"{row.get('side')} {row.get('option_type')} {row.get('position_effect')} | "
                f"contracts {row.get('contracts')} | source {row.get('source_type')}:{row.get('source_name')}"
            )
        return

    if args.cmd == 'history':
        try:
            history = build_lot_event_history(repo, record_id=args.record_id)
        except ValueError as e:
            raise SystemExit(str(e))
        if args.format == 'json':
            print(json.dumps(history, ensure_ascii=False, indent=2))
            return
        if not history:
            print('(no related events)')
            return
        print(f'# lot_history {args.record_id}')
        for row in history:
            extra = []
            if row.get('void_target_event_id'):
                extra.append(f"void_target={row.get('void_target_event_id')}")
            if row.get('adjust_target_source_event_id'):
                extra.append(f"adjust_target={row.get('adjust_target_source_event_id')}")
            if row.get('close_target_source_event_id'):
                extra.append(f"close_target_src={row.get('close_target_source_event_id')}")
            print(
                f"- {row.get('event_id')} | {row.get('side')} {row.get('option_type')} {row.get('position_effect')} | "
                f"contracts {row.get('contracts')} | source {row.get('source_type')}:{row.get('source_name')}"
                + (f" | {' '.join(extra)}" if extra else "")
            )
        return

    if args.cmd == 'rebuild':
        result = rebuild_position_lots_from_trade_events(repo)
        if args.format == 'json':
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return
        print(
            "[DONE] rebuilt position_lots "
            f"trade_events={result.get('trade_event_count')} "
            f"position_lots={result.get('position_lot_count')} "
            f"preserved_sync_meta={result.get('preserved_sync_meta_record_count')} "
            f"unmatched_explicit_close={result.get('unmatched_explicit_close_count')}"
        )
        return

    if args.cmd == 'inspect':
        if not any(
            value is not None and str(value).strip()
            for value in (args.record_id, args.feishu_record_id, args.account, args.symbol, args.option_type, args.exp)
        ) and args.strike is None:
            raise SystemExit("inspect requires at least one selector")
        payload = inspect_projection_state(
            repo,
            record_id=args.record_id,
            feishu_record_id=args.feishu_record_id,
            account=args.account,
            symbol=args.symbol,
            option_type=args.option_type,
            strike=args.strike,
            expiration_ymd=((args.exp or '').strip() or None),
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    if args.cmd == 'void-event':
        try:
            result = persist_manual_void_event(
                repo,
                target_event_id=args.event_id,
                void_reason=args.void_reason,
            )
        except ValueError as e:
            raise SystemExit(str(e))
        print(
            f"[DONE] voided event_id={args.event_id} "
            f"via={result.get('event_id')} "
            f"position_lots={result.get('position_lot_count')}"
        )
        print("[WARN] Feishu mirror rows are not auto-deleted by void-event; rerun review/sync before trusting remote mirror.")
        return

    if args.cmd == 'adjust-lot':
        try:
            out = execute_manual_adjust(
                repo,
                record_id=args.record_id,
                contracts=args.contracts,
                strike=args.strike,
                expiration_ymd=((args.exp or '').strip() or None),
                premium_per_share=args.premium_per_share,
                multiplier=args.multiplier,
                opened_at_ms=args.opened_at_ms,
                dry_run=bool(args.dry_run),
            )
        except ValueError as e:
            raise SystemExit(str(e))
        patch = out["patch"]
        if args.dry_run:
            print('[DRY_RUN] adjust fields:')
            print(json.dumps(patch, ensure_ascii=False, indent=2))
            return
        res = out["result"]
        print(f"[DONE] adjusted {args.record_id} event_id={res.get('event_id')}")
        return

if __name__ == '__main__':
    main()
