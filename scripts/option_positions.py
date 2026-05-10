#!/usr/bin/env python3
"""Manage position lots via trade events.

Supports open, buy-to-close, and list flows on top of the
trade-events / position-lots model.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

repo_base = Path(__file__).resolve().parents[1]
if str(repo_base) not in sys.path:
    sys.path.insert(0, str(repo_base))

from domain.domain.option_position_lots import (
    normalize_account,
    normalize_broker,
    normalize_side,
)
from src.application.option_positions_service import persist_manual_void_event
from src.application.position_workflows import execute_manual_adjust, execute_manual_close, execute_manual_open
from src.application.option_positions_facade import (
    format_cash_secured_amount,
    format_position_money,
    list_position_rows,
    resolve_option_positions_repo,
)
from src.application.option_positions_inspection import build_lot_event_history, inspect_projection_state
from src.application.option_positions_v2_service import refresh_option_positions_v2_state
from src.application.option_positions_v2_service import reconcile_option_positions_snapshot, snapshot_current_positions_as_verification
from src.application.verification_snapshot_io import load_verification_snapshot_payload


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
    p_add.add_argument('--currency', default=None, choices=['USD', 'HKD', 'CNY'], help='optional; inferred from symbol when omitted (.HK => HKD, otherwise USD)')
    p_add.add_argument('--strike', type=float, default=None, help='required for auto cash_secured on short put')
    p_add.add_argument('--multiplier', type=float, default=None, help='default 100 for US; required for HK if strike provided')
    p_add.add_argument('--exp', default=None, help='YYYY-MM-DD (required for option lots)')
    p_add.add_argument('--premium-per-share', type=float, default=None, help='premium per share stored on the lot')
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

    p_reconcile = sub.add_parser('reconcile', help='store a verification snapshot and generate a reconciliation report')
    p_reconcile.add_argument('--snapshot-file', required=True, help='JSON file with a verification snapshot object or lots array')
    p_reconcile.add_argument('--format', default='text', choices=['text', 'json'])

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
    state_base = Path(str(_data_config)).resolve().parent

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
            ccy = str(r.get('currency') or 'USD').upper()
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
            history = build_lot_event_history(repo, base=state_base, record_id=args.record_id)
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
        state = refresh_option_positions_v2_state(base=state_base, repo=repo)
        result = {
            'persisted_baseline_snapshot_id': state.persisted_baseline_snapshot.get('snapshot_id'),
            'baseline_snapshot_id': state.baseline_snapshot.get('snapshot_id'),
            'latest_verification_snapshot_id': (state.latest_verification_snapshot or {}).get('snapshot_id'),
            'verification_snapshot_count': int(len(state.verification_snapshots)),
            'accepted_verification_snapshot_count': int(len(state.accepted_verification_snapshots)),
            'baseline_lot_count': int(len(state.baseline_snapshot.get('lots') or [])),
            'trade_event_count': int(len(state.events)),
            'position_lot_count': int(len(state.projection.get('positions') or [])),
            'diagnostic_count': int(len(state.projection.get('diagnostics') or [])),
            'latest_reconciliation_report_id': (state.latest_reconciliation_report or {}).get('report_id'),
            'skipped_legacy_event_count': int(len(state.skipped_legacy_events)),
        }
        if args.format == 'json':
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return
        print(
            "[DONE] rebuilt option_positions_v2 projection "
            f"baseline_snapshot={result.get('baseline_snapshot_id')} "
            f"baseline_lots={result.get('baseline_lot_count')} "
            f"trade_events={result.get('trade_event_count')} "
            f"position_lots={result.get('position_lot_count')} "
            f"diagnostics={result.get('diagnostic_count')} "
            f"skipped_legacy_events={result.get('skipped_legacy_event_count')}"
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
            base=state_base,
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

    if args.cmd == 'reconcile':
        try:
            snapshot = load_verification_snapshot_payload(args.snapshot_file)
            report = reconcile_option_positions_snapshot(
                base=state_base,
                repo=repo,
                verification_snapshot=snapshot,
            )
        except ValueError as e:
            raise SystemExit(str(e))
        if args.format == 'json':
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return
        summary = report.get('summary') or {}
        print(
            "[DONE] wrote verification snapshot and reconciliation report "
            f"snapshot_id={report.get('snapshot_id')} "
            f"matched={int(summary.get('matched', 0))} "
            f"quantity_mismatch={int(summary.get('quantity_mismatch', 0))} "
            f"missing_in_projection={int(summary.get('missing_in_projection', 0))} "
            f"missing_in_snapshot={int(summary.get('missing_in_snapshot', 0))} "
            f"field_mismatch={int(summary.get('field_mismatch', 0))}"
        )
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
        try:
            snapshot_current_positions_as_verification(
                base=state_base,
                repo=repo,
                snapshot_id=f"verify-{result.get('event_id')}",
                source_name="cli_manual_void",
                source_type="manual_verification",
                note="manual_void checkpoint",
            )
            v2_state = refresh_option_positions_v2_state(base=state_base, repo=repo)
            result["v2_result"] = {
                'baseline_snapshot_id': v2_state.baseline_snapshot.get('snapshot_id'),
                'processed_event_count': len(v2_state.events),
                'diagnostic_count': len(v2_state.projection.get('diagnostics') or []),
            }
        except Exception:
            result["v2_result"] = None
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
