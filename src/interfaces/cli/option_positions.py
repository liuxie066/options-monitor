"""Manage position lots via trade events.

Supports open, buy-to-close, and list flows on top of the
trade-events / position-lots model.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, cast

from domain.domain.ledger.position_fields import (
    normalize_account,
    normalize_broker,
)
from src.application.config_loader import resolve_data_config_path
from src.application.ledger.api import (
    format_position_cash_secured,
    format_position_money,
    inspect_ledger_stores,
    ledger_store_payload,
    list_position_rows,
    open_position_ledger_from_data_config as resolve_option_positions_repo,
    reconcile_position_snapshot,
    record_trade_event_void,
    refresh_position_lot_projection,
)
from src.application.positions.auto_close import main as run_option_positions_auto_close
from src.application.positions.feishu_sync import main as run_option_positions_feishu_sync
from src.application.positions.workflows import (
    ManualCloseMatchError,
    execute_manual_adjust,
    execute_manual_close,
    execute_manual_open,
    format_manual_close_match_error,
)
from src.application.positions.inspection import build_lot_event_history, inspect_projection_state
from src.application.verification_snapshot_io import load_verification_snapshot_payload


def _resolve_path_under(path: str | Path, *, base: Path) -> Path:
    resolved = Path(path).expanduser()
    if not resolved.is_absolute():
        resolved = (base / resolved).resolve()
    return resolved


def _load_json_object(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise SystemExit(f"expected JSON object: {path}")
    return payload


def _store_inspect_data_config(args: argparse.Namespace, *, base: Path) -> tuple[Path, Path | None]:
    config_ref = str(getattr(args, "store_config", "") or "").strip()
    explicit_data_config = str(getattr(args, "store_data_config", "") or getattr(args, "data_config", "") or "").strip()
    if not config_ref:
        return resolve_data_config_path(base=base, data_config=(explicit_data_config or None)), None

    config_path = _resolve_path_under(config_ref, base=base)
    if explicit_data_config:
        return _resolve_path_under(explicit_data_config, base=base), config_path

    cfg = _load_json_object(config_path)
    portfolio_cfg = cfg.get("portfolio") if isinstance(cfg.get("portfolio"), dict) else {}
    data_ref = str(portfolio_cfg.get("data_config") or "").strip() if isinstance(portfolio_cfg, dict) else ""
    if data_ref:
        data_path = Path(data_ref).expanduser()
        if not data_path.is_absolute():
            data_path = (config_path.parent / data_path).resolve()
        return data_path, config_path
    return (config_path.parent / "secrets" / "portfolio.sqlite.json").resolve(), config_path


def _print_store_inspect_text(payload: dict[str, object]) -> None:
    active_raw = payload.get("active")
    summary_raw = payload.get("summary")
    active = cast(dict[str, object], active_raw) if isinstance(active_raw, dict) else {}
    summary = cast(dict[str, object], summary_raw) if isinstance(summary_raw, dict) else {}
    print("# option_positions store")
    print(f"active: {active.get('sqlite_path')}")
    print(f"runtime_root: {active.get('runtime_root')} ({active.get('runtime_root_source')})")
    print(
        "active_counts: "
        f"trade_events={active.get('trade_event_count')} "
        f"position_lots={active.get('position_lot_count')} "
        f"exists={active.get('db_exists')}"
    )
    print(
        "summary: "
        f"existing={summary.get('existing_candidate_count')} "
        f"populated={summary.get('populated_candidate_count')} "
        f"multiple_populated={summary.get('multiple_populated')}"
    )
    candidates = payload.get("candidates")
    if isinstance(candidates, list):
        print("# candidates")
        for item in candidates:
            if not isinstance(item, dict):
                continue
            item_map = cast(dict[str, object], item)
            roles_raw = item_map.get("roles")
            roles = ",".join(str(role) for role in roles_raw) if isinstance(roles_raw, list) else ""
            print(
                f"- {roles or '-'} | exists={item_map.get('exists')} "
                f"trade_events={item_map.get('trade_event_count')} "
                f"position_lots={item_map.get('position_lot_count')} | {item_map.get('path')}"
            )
    warnings = payload.get("warnings")
    if isinstance(warnings, list) and warnings:
        print("# warnings")
        for warning in warnings:
            print(f"- {warning}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description='Manage position lots via trade events')
    ap.add_argument('--data-config', default=None, help='portfolio data config path; auto-resolves when omitted')

    sub = ap.add_subparsers(dest='cmd', required=True)

    p_list = sub.add_parser('list', help='list records')
    p_list.add_argument('--broker', default='富途')
    p_list.add_argument('--account', default=None)
    p_list.add_argument('--status', default='open', choices=['open', 'close', 'all'])
    p_list.add_argument('--format', default='text', choices=['text', 'json'])
    p_list.add_argument('--limit', type=int, default=50)
    p_list.add_argument('--exp-within-days', type=int, default=None, help='only include rows expiring within N days from today')

    p_add = sub.add_parser('add', help='add a record')
    p_add.add_argument('--broker', default='富途')
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

    p_buy_close = sub.add_parser('buy-close', help='buy to close a position by record_id or strict unique selector')
    p_buy_close.add_argument('--record-id', default=None)
    p_buy_close.add_argument('--broker', default='富途')
    p_buy_close.add_argument('--account', default=None, help='required when --record-id is omitted')
    p_buy_close.add_argument('--symbol', default=None, help='required when --record-id is omitted')
    p_buy_close.add_argument('--option-type', default=None, choices=['put', 'call'], help='required when --record-id is omitted')
    p_buy_close.add_argument('--side', default='short', choices=['short', 'long'], help='target position side; buy-close normally targets short')
    p_buy_close.add_argument('--strike', type=float, default=None, help='required when --record-id is omitted')
    p_buy_close.add_argument('--exp', default=None, help='YYYY-MM-DD; required when --record-id is omitted')
    p_buy_close.add_argument('--contracts', type=int, required=True, help='contracts to close; supports partial close')
    p_buy_close.add_argument('--close-price', type=float, default=None, help='buy-to-close price per share/contract unit')
    p_buy_close.add_argument('--close-reason', default='manual_buy_to_close')
    p_buy_close.add_argument('--dry-run', action='store_true')

    p_events = sub.add_parser('events', help='list canonical trade events')
    p_events.add_argument('--broker', default=None)
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

    p_store = sub.add_parser('store', help='inspect option-position SQLite store resolution')
    store_sub = p_store.add_subparsers(dest='store_cmd', required=True)
    p_store_inspect = store_sub.add_parser('inspect', help='diagnose active and legacy SQLite store candidates')
    p_store_inspect.add_argument("--config", dest="store_config", default=None, help="runtime config path; resolves portfolio.data_config relative to the config file")
    p_store_inspect.add_argument("--data-config", dest="store_data_config", default=None, help="portfolio data config path override")
    p_store_inspect.add_argument("--runtime-root", default=None, help="override runtime root for standard ledger path resolution")
    p_store_inspect.add_argument("--format", default="json", choices=["json", "text"])

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

    p_report = sub.add_parser('report', help='read-only reports for position lots')
    report_sub = p_report.add_subparsers(dest='report_cmd', required=True)
    p_monthly = report_sub.add_parser(
        'monthly-income',
        help='monthly option income report (cashflow, realized PnL, and open-basis attribution)',
        description=(
            'Monthly option income report.\n'
            '- net_cashflow_gross: groups account cash movements by trade month.\n'
            '- realized_pnl_gross: groups closed option PnL by close month.\n'
            '- open_basis_lifecycle_pnl_gross: attributes lifecycle PnL back to open month.\n'
            '- premium_received_gross/realized_gross are compatibility aliases.\n'
            '- *_cny columns are best-effort exchange-rate conversions from rate_cache.json.'
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )
    p_monthly.add_argument('--broker', default='富途')
    p_monthly.add_argument('--account', default=None)
    p_monthly.add_argument('--month', default=None, help='YYYY-MM')
    p_monthly.add_argument('--format', choices=['text', 'json'], default='text')
    p_monthly.add_argument('--include-rows', action='store_true')

    p_sync_feishu = sub.add_parser('sync-feishu', help='sync option positions to Feishu')
    p_sync_feishu.add_argument("--config", dest="sync_config", default=None, help="runtime config path; when provided, portfolio.data_config and runtime sync switch are used")
    p_sync_feishu.add_argument("--data-config", dest="sync_data_config", default=None, help="portfolio data config path; auto-resolves when omitted")
    p_sync_feishu.add_argument("--apply", action="store_true", help="apply changes to Feishu and persist local sync metadata")
    p_sync_feishu.add_argument("--dry-run", action="store_true", help="preview actions without writing to Feishu")
    p_sync_feishu.add_argument("--limit", type=int, default=None, help="maximum number of local lots to inspect")
    p_sync_feishu.add_argument("--only-record-id", default=None, help="sync a single local record_id")
    p_sync_feishu.add_argument("--only-open", action="store_true", help="only sync open positions")
    p_sync_feishu.add_argument("--since-updated-ms", type=int, default=None, help="only include rows last synced before this ms watermark")
    p_sync_feishu.add_argument(
        "--prune-remote-missing-local",
        action="store_true",
        help="delete remote rows whose local_record_id no longer exists locally; disabled by default",
    )
    p_sync_feishu.add_argument("--no-send", action="store_true", help="do not send sync receipt notifications")
    p_sync_feishu.add_argument("--verbose", action="store_true", help="print payload details")

    p_auto_close = sub.add_parser('auto-close-expired', help='auto-close expired option position lots')
    p_auto_close.add_argument("--config", dest="auto_close_config", default=None, help="runtime config path; provides accounts and portfolio.data_config")
    p_auto_close.add_argument("--data-config", dest="auto_close_data_config", default=None, help="portfolio data config path; overrides runtime config when provided")
    p_auto_close.add_argument("--accounts", nargs="+", default=None, help="accounts to process; defaults to runtime config accounts")
    p_auto_close.add_argument("--broker", default=None, help="optional broker filter override")
    p_auto_close.add_argument("--apply", action="store_true", help="append close events for expired lots")
    p_auto_close.add_argument("--dry-run", action="store_true", help="preview without writing close events")
    p_auto_close.add_argument("--as-of-utc", default=None, help="ISO datetime; default is current UTC")
    p_auto_close.add_argument("--no-send", action="store_true", help="do not send auto-close receipt notifications")
    p_auto_close.add_argument("--format", choices=["json", "text"], default="json")
    p_auto_close.add_argument("--quiet", action="store_true", help="suppress stdout")

    args = ap.parse_args(argv)

    base = Path(__file__).resolve().parents[3]
    if args.cmd == 'store':
        data_config_path, config_path = _store_inspect_data_config(args, base=base)
        payload = inspect_ledger_stores(
            data_config_path,
            runtime_root=getattr(args, "runtime_root", None),
            config_path=config_path,
        )
        if args.format == "json":
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            _print_store_inspect_text(payload)
        return 0

    if args.cmd == 'auto-close-expired':
        auto_close_argv: list[str] = []
        if args.auto_close_config:
            auto_close_argv.extend(["--config", str(args.auto_close_config)])
        if args.auto_close_data_config:
            auto_close_argv.extend(["--data-config", str(args.auto_close_data_config)])
        elif args.data_config:
            auto_close_argv.extend(["--data-config", str(args.data_config)])
        if args.accounts:
            auto_close_argv.append("--accounts")
            auto_close_argv.extend(str(item) for item in args.accounts)
        if args.broker:
            auto_close_argv.extend(["--broker", str(args.broker)])
        if args.apply:
            auto_close_argv.append("--apply")
        if args.dry_run:
            auto_close_argv.append("--dry-run")
        if args.as_of_utc:
            auto_close_argv.extend(["--as-of-utc", str(args.as_of_utc)])
        if args.no_send:
            auto_close_argv.append("--no-send")
        if args.format:
            auto_close_argv.extend(["--format", str(args.format)])
        if args.quiet:
            auto_close_argv.append("--quiet")
        return int(run_option_positions_auto_close(auto_close_argv))

    if args.cmd == 'sync-feishu':
        sync_argv: list[str] = []
        if args.sync_config:
            sync_argv.extend(["--config", str(args.sync_config)])
        if args.sync_data_config:
            sync_argv.extend(["--data-config", str(args.sync_data_config)])
        elif args.data_config:
            sync_argv.extend(["--data-config", str(args.data_config)])
        if args.apply:
            sync_argv.append("--apply")
        if args.dry_run:
            sync_argv.append("--dry-run")
        if args.limit is not None:
            sync_argv.extend(["--limit", str(args.limit)])
        if args.only_record_id:
            sync_argv.extend(["--only-record-id", str(args.only_record_id)])
        if args.only_open:
            sync_argv.append("--only-open")
        if args.since_updated_ms is not None:
            sync_argv.extend(["--since-updated-ms", str(args.since_updated_ms)])
        if args.prune_remote_missing_local:
            sync_argv.append("--prune-remote-missing-local")
        if args.no_send:
            sync_argv.append("--no-send")
        if args.verbose:
            sync_argv.append("--verbose")
        run_option_positions_feishu_sync(sync_argv)
        return 0

    _data_config, repo = resolve_option_positions_repo(base=base, data_config=args.data_config)
    state_base = Path(str(_data_config)).resolve().parent
    ledger_store = ledger_store_payload(_data_config, repo)

    if args.cmd == 'list':
        broker = normalize_broker(args.broker)
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
            return 0

        if not rows:
            print('(no records)')
            return 0
        print('# position_lots')
        for r in rows:
            ccy = str(r.get('currency') or 'USD').upper()
            cash_txt = format_position_cash_secured(r.get('cash_secured_amount'), ccy)
            print(
                f"- {r['record_id']} | {r.get('account')} | {r.get('symbol')} | {r.get('side')} {r.get('option_type')} | "
                f"exp {r.get('expiration_ymd') or '-'} | strike {r.get('strike') if r.get('strike') is not None else '-'} | "
                f"contracts {r.get('contracts')} open {r.get('contracts_open')} closed {r.get('contracts_closed')} | "
                f"{ccy} cash_secured {cash_txt} | status {r.get('status')}"
            )
        return 0

    if args.cmd == 'add':
        broker = normalize_broker(args.broker)
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
            return 0

        res = out["result"]
        print(f"[DONE] created event_id={res.get('event_id')}")
        if fields.get('cash_secured_amount') is not None:
            print(
                f"cash_secured_amount={format_position_money(float(fields['cash_secured_amount']), fields.get('currency') or '')}"
            )
        return 0

    if args.cmd == 'buy-close':
        try:
            out = execute_manual_close(
                repo,
                record_id=args.record_id,
                contracts_to_close=int(args.contracts),
                close_price=args.close_price,
                close_reason=args.close_reason,
                dry_run=bool(args.dry_run),
                broker=args.broker,
                account=args.account,
                symbol=args.symbol,
                option_type=args.option_type,
                position_side=args.side,
                strike=args.strike,
                expiration_ymd=((args.exp or '').strip() or None),
            )
        except ManualCloseMatchError as e:
            raise SystemExit(format_manual_close_match_error(e))
        except ValueError as e:
            raise SystemExit(str(e))
        raw_match = out.get("match")
        match: dict[str, Any] = raw_match if isinstance(raw_match, dict) else {}
        if match.get("rule") == "strict_contract_unique":
            print(f"[MATCH] rule={match.get('rule')} record_id={match.get('record_id')}")
        patch = out["patch"]
        if args.dry_run:
            print('[DRY_RUN] update fields:')
            print(json.dumps(patch, ensure_ascii=False, indent=2))
            return 0
        res = out["result"]
        closed_record_id = (match.get("record_id") if match else None) or args.record_id
        print(f"[DONE] buy-closed {closed_record_id} contracts={int(args.contracts)} event_id={res.get('event_id')}")
        return 0

    if args.cmd == 'events':
        broker = normalize_broker(args.broker) if args.broker else None
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
            return 0
        if not rows:
            print('(no events)')
            return 0
        print('# trade_events')
        for row in rows:
            print(
                f"- {row.get('event_id')} | {row.get('account')} | {row.get('symbol')} | "
                f"{row.get('side')} {row.get('option_type')} {row.get('position_effect')} | "
                f"contracts {row.get('contracts')} | source {row.get('source_type')}:{row.get('source_name')}"
            )
        return 0

    if args.cmd == 'history':
        try:
            history = build_lot_event_history(repo, base=state_base, record_id=args.record_id)
        except ValueError as e:
            raise SystemExit(str(e))
        if args.format == 'json':
            print(json.dumps(history, ensure_ascii=False, indent=2))
            return 0
        if not history:
            print('(no related events)')
            return 0
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
        return 0

    if args.cmd == 'rebuild':
        raw_result = refresh_position_lot_projection(repo)
        result = dict(raw_result) if isinstance(raw_result, dict) else raw_result.to_dict()
        result["mode"] = "canonical_position_lots_rebuild"
        result["source_of_truth"] = "trade_events"
        result["projection"] = "position_lots"
        result["ledger_store"] = ledger_store
        if args.format == 'json':
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        print(
            "[DONE] rebuilt canonical position_lots projection "
            f"trade_events={result.get('trade_event_count')} "
            f"position_lots={result.get('position_lot_count')} "
            f"diagnostics={result.get('projection_diagnostic_count')} "
            f"unmatched_explicit_close={result.get('unmatched_explicit_close_count')} "
            f"unmatched_heuristic_close={result.get('unmatched_heuristic_close_count')} "
            f"preserved_sync_meta={result.get('preserved_sync_meta_record_count')}"
        )
        return 0

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
        payload["ledger_store"] = ledger_store
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    if args.cmd == 'report':
        from src.interfaces.cli.option_positions_report import run_report

        return run_report(args, base=base, repo=repo)

    if args.cmd == 'reconcile':
        try:
            snapshot = load_verification_snapshot_payload(args.snapshot_file)
            report = reconcile_position_snapshot(
                base=state_base,
                repo=repo,
                verification_snapshot=snapshot,
            )
        except ValueError as e:
            raise SystemExit(str(e))
        if args.format == 'json':
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0
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
        return 0

    if args.cmd == 'void-event':
        try:
            result = record_trade_event_void(repo, event_id=args.event_id, reason=args.void_reason)
        except ValueError as e:
            raise SystemExit(str(e))
        print(
            f"[DONE] voided event_id={args.event_id} "
            f"via={result.get('event_id')} "
            f"position_lots={result.get('position_lot_count')}"
        )
        print("[WARN] Feishu mirror rows are not auto-deleted by void-event; rerun review/sync before trusting remote mirror.")
        return 0

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
            return 0
        res = out["result"]
        print(f"[DONE] adjusted {args.record_id} event_id={res.get('event_id')}")
        return 0

    raise SystemExit("unknown cmd")

if __name__ == '__main__':
    raise SystemExit(main())
