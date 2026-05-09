#!/usr/bin/env python3
"""Manage position lots via trade events.

Supports open, buy-to-close, and list flows on top of the
trade-events / position-lots model.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from scripts.option_positions_core.domain import (
    effective_expiration_ymd,
    effective_strike,
    exp_ms_to_ymd,
    normalize_account,
    normalize_broker,
    normalize_option_type,
    normalize_side,
)
from scripts.option_positions_core.service import persist_manual_void_event
from scripts.trade_contract_identity import canonical_contract_symbol
from src.application.position_workflows import execute_manual_adjust, execute_manual_close, execute_manual_open
from src.application.option_positions_facade import (
    format_cash_secured_amount,
    format_position_money,
    list_position_rows,
    resolve_option_positions_repo,
)
from src.application.option_positions_v2_service import load_option_positions_v2_records, refresh_option_positions_v2_state
from src.application.option_positions_v2_service import reconcile_option_positions_snapshot, snapshot_current_positions_as_verification


def _safe_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _optional_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _iso_to_trade_time_ms(value: object) -> int | None:
    text = str(value or '').strip()
    if not text:
        return None
    try:
        return int(datetime.fromisoformat(text).timestamp() * 1000)
    except ValueError:
        print(f"[WARN] invalid v2 event_at_utc timestamp: {text}; using null trade_time_ms")
        return None


def _identity_matches_payload(
    payload: dict[str, object],
    *,
    account: str | None,
    symbol: str | None,
    option_type: str | None,
    strike: float | None,
    expiration_ymd: str | None,
) -> bool:
    if account and normalize_account(payload.get('account')) != normalize_account(account):
        return False
    if symbol and canonical_contract_symbol(payload.get('symbol')) != canonical_contract_symbol(symbol):
        return False
    if option_type and normalize_option_type(payload.get('option_type')) != normalize_option_type(option_type):
        return False
    if strike is not None:
        current_strike = _safe_float(payload.get('strike'))
        if current_strike is None or abs(current_strike - float(strike)) >= 1e-9:
            return False
    if expiration_ymd:
        current_expiration = str(payload.get('expiration_ymd') or '').strip() or effective_expiration_ymd(payload)
        if current_expiration != str(expiration_ymd).strip():
            return False
    return True


def _v2_position_effect(event_kind: object) -> str:
    mapping = {
        "open_trade": "open",
        "close_trade": "close",
        "manual_adjustment": "adjust",
    }
    return mapping.get(str(event_kind or "").strip(), str(event_kind or "").strip())


def _related_legacy_void_rows(repo, *, related_event_ids: set[str], record_id: str | None) -> list[dict[str, object]]:
    list_trade_events = getattr(repo, "list_trade_events", None)
    if not callable(list_trade_events):
        return []
    rows: list[dict[str, object]] = []
    raw_events = list_trade_events()
    events = raw_events if isinstance(raw_events, list) else []
    for event in events:
        if not isinstance(event, dict):
            continue
        if str(event.get("position_effect") or "").strip().lower() != "void":
            continue
        payload = event.get("raw_payload") or {}
        if not isinstance(payload, dict):
            payload = {}
        target_event_id = str(payload.get("void_target_event_id") or "").strip()
        target_record_id = str(payload.get("record_id") or "").strip()
        if target_event_id not in related_event_ids and (record_id is None or target_record_id != str(record_id).strip()):
            continue
        rows.append(
            {
                "event_id": str(event.get("event_id") or "").strip(),
                "trade_time_ms": event.get("trade_time_ms"),
                "source_type": event.get("source_type"),
                "source_name": event.get("source_name"),
                "broker": normalize_broker(event.get("broker")),
                "account": normalize_account(event.get("account")) if event.get("account") else None,
                "symbol": event.get("symbol"),
                "option_type": event.get("option_type"),
                "side": event.get("side"),
                "position_effect": "void",
                "contracts": event.get("contracts"),
                "price": event.get("price"),
                "strike": event.get("strike"),
                "expiration_ymd": event.get("expiration_ymd"),
                "currency": event.get("currency"),
                "void_target_event_id": target_event_id or None,
                "adjust_target_source_event_id": None,
                "close_target_source_event_id": None,
                "record_id": target_record_id or record_id,
                "patch": None,
            }
        )
    return rows


def _related_legacy_adjust_rows(
    repo,
    *,
    record_id: str,
    fields: dict[str, object],
) -> list[dict[str, object]]:
    list_trade_events = getattr(repo, "list_trade_events", None)
    if not callable(list_trade_events):
        return []
    rows: list[dict[str, object]] = []
    raw_events = list_trade_events()
    events = raw_events if isinstance(raw_events, list) else []
    for event in events:
        if not isinstance(event, dict):
            continue
        if str(event.get("position_effect") or "").strip().lower() != "adjust":
            continue
        if not _matches_event_selector(
            event,
            record_id=record_id,
            account=_optional_text(fields.get("account")),
            symbol=_optional_text(fields.get("symbol")),
            option_type=_optional_text(fields.get("option_type")),
            strike=effective_strike(fields),
            expiration_ymd=effective_expiration_ymd(fields),
        ):
            continue
        payload = event.get("raw_payload") or {}
        if not isinstance(payload, dict):
            payload = {}
        rows.append(
            {
                "event_id": str(event.get("event_id") or "").strip(),
                "trade_time_ms": event.get("trade_time_ms"),
                "source_type": event.get("source_type"),
                "source_name": event.get("source_name"),
                "broker": normalize_broker(event.get("broker")),
                "account": normalize_account(event.get("account")) if event.get("account") else None,
                "symbol": event.get("symbol"),
                "option_type": event.get("option_type"),
                "side": event.get("side"),
                "position_effect": "adjust",
                "contracts": event.get("contracts"),
                "price": event.get("price"),
                "strike": event.get("strike"),
                "expiration_ymd": event.get("expiration_ymd"),
                "currency": event.get("currency"),
                "void_target_event_id": None,
                "adjust_target_source_event_id": payload.get("adjust_target_source_event_id"),
                "close_target_source_event_id": None,
                "record_id": payload.get("record_id") or record_id,
                "patch": payload.get("patch") if isinstance(payload.get("patch"), dict) else None,
            }
        )
    return rows

def _generate_verification_snapshot_id() -> str:
    return f"verify-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}"


def _load_verification_snapshot_payload(path: str) -> dict[str, object]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(payload, dict) and payload.get("snapshot_type"):
        return payload
    if isinstance(payload, dict) and isinstance(payload.get("lots"), list):
        lots = payload.get("lots") or []
        snapshot_id = str(payload.get("snapshot_id") or _generate_verification_snapshot_id()).strip()
        return {
            "snapshot_id": snapshot_id,
            "snapshot_type": "verification",
            "snapshot_at_utc": str(payload.get("snapshot_at_utc") or datetime.now().astimezone().isoformat()),
            "source_name": str(payload.get("source_name") or "cli_reconcile"),
            "source_type": str(payload.get("source_type") or "manual_verification"),
            "note": payload.get("note"),
            "lots": lots,
        }
    if isinstance(payload, list):
        return {
            "snapshot_id": _generate_verification_snapshot_id(),
            "snapshot_type": "verification",
            "snapshot_at_utc": datetime.now().astimezone().isoformat(),
            "source_name": "cli_reconcile",
            "source_type": "manual_verification",
            "lots": payload,
        }
    raise ValueError("verification snapshot file must be a snapshot object or a lots array")


def build_lot_event_history(repo, *, base: Path, record_id: str) -> list[dict[str, object]]:
    compat = load_option_positions_v2_records(base=base, repo=repo)
    current = next((item for item in compat.records if str(item.get('record_id') or '').strip() == str(record_id).strip()), None)
    if current is None:
        raise ValueError(f"position lot not found: {record_id}")
    fields = current.get('fields') or {}
    if not isinstance(fields, dict):
        fields = {}
    history: list[dict[str, object]] = []
    related_event_ids: set[str] = set()
    for event in compat.state.events:
        if not _identity_matches_payload(
            event,
            account=fields.get('account'),
            symbol=fields.get('symbol'),
            option_type=fields.get('option_type'),
            strike=effective_strike(fields),
            expiration_ymd=effective_expiration_ymd(fields),
        ):
            continue
        event_id = str(event.get('event_id') or '').strip()
        if event_id:
            related_event_ids.add(event_id)
        history.append(
            {
                'event_id': event_id,
                'trade_time_ms': _iso_to_trade_time_ms(event.get('event_at_utc')),
                'source_type': event.get('source_type'),
                'source_name': event.get('source_name'),
                'broker': normalize_broker(event.get('broker')),
                'account': normalize_account(event.get('account')) if event.get('account') else None,
                'symbol': event.get('symbol'),
                'option_type': event.get('option_type'),
                'side': event.get('side'),
                'position_effect': _v2_position_effect(event.get('event_kind')),
                'contracts': event.get('contracts'),
                'price': None,
                'strike': event.get('strike'),
                'expiration_ymd': event.get('expiration_ymd'),
                'currency': event.get('currency'),
                'void_target_event_id': None,
                'adjust_target_source_event_id': None,
                'close_target_source_event_id': None,
                'record_id': event.get('snapshot_lot_id') or record_id,
                'patch': {'target_contracts': event.get('target_contracts')} if event.get('target_contracts') is not None else None,
            }
        )
    for row in _related_legacy_adjust_rows(repo, record_id=record_id, fields=fields):
        event_id = str(row.get('event_id') or '').strip()
        if event_id and event_id not in related_event_ids:
            related_event_ids.add(event_id)
            history.append(row)
    history.extend(_related_legacy_void_rows(repo, related_event_ids=related_event_ids, record_id=record_id))
    history.sort(key=lambda row: (_safe_int(row.get('trade_time_ms')), str(row.get('event_id') or '')))
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
    if symbol and canonical_contract_symbol(fields.get('symbol')) != canonical_contract_symbol(symbol):
        return False
    if option_type and str(fields.get('option_type') or '').strip().lower() != str(option_type).strip().lower():
        return False
    if strike is not None:
        current_strike = _safe_float(fields.get('strike'))
        if current_strike is None or abs(current_strike - float(strike)) >= 1e-9:
            return False
    if expiration_ymd:
        current_expiration = exp_ms_to_ymd(fields.get('expiration')) or str(fields.get('expiration') or '')
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
    if symbol and canonical_contract_symbol(event.get('symbol')) != canonical_contract_symbol(symbol):
        return False
    if option_type and str(event.get('option_type') or '').strip().lower() != str(option_type).strip().lower():
        return False
    if strike is not None:
        current_strike = _safe_float(event.get('strike'))
        if current_strike is None or abs(current_strike - float(strike)) >= 1e-9:
            return False
    if expiration_ymd and str(event.get('expiration_ymd') or '').strip() != str(expiration_ymd).strip():
        return False
    return True


def _should_show_projected_position(row: dict[str, object]) -> bool:
    if _safe_int(row.get('baseline_contracts')) > 0:
        return True
    if _safe_int(row.get('current_contracts')) > 0:
        return True
    return bool(row.get('applied_events') or row.get('applied_verifications'))


def inspect_projection_state(
    repo,
    *,
    base: Path,
    record_id: str | None = None,
    feishu_record_id: str | None = None,
    account: str | None = None,
    symbol: str | None = None,
    option_type: str | None = None,
    strike: float | None = None,
    expiration_ymd: str | None = None,
) -> dict[str, object]:
    compat = load_option_positions_v2_records(base=base, repo=repo)
    current_rows = compat.records
    state = compat.state
    projection = state.projection
    baseline_snapshot = state.baseline_snapshot
    events = state.events

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
    matched_position_keys = {
        str(((row.get('fields') or {}).get('position_key') or '')).strip()
        for row in matched_current
        if isinstance(row.get('fields'), dict)
    }
    matched_projected = [
        row for row in (projection.get('positions') or [])
        if _should_show_projected_position(row)
        and (
            str(row.get('position_key') or '').strip() in matched_position_keys
            or _identity_matches_payload(
                row,
                account=account,
                symbol=symbol,
                option_type=option_type,
                strike=strike,
                expiration_ymd=expiration_ymd,
            )
        )
    ]
    matched_position_keys.update(str(row.get('position_key') or '').strip() for row in matched_projected if str(row.get('position_key') or '').strip())
    baseline_lots = [
        row for row in (baseline_snapshot.get('lots') or [])
        if str(row.get('position_key') or '').strip() in matched_position_keys
        or _identity_matches_payload(
            row,
            account=account,
            symbol=symbol,
            option_type=option_type,
            strike=strike,
            expiration_ymd=expiration_ymd,
        )
    ]
    related_events = [
        {
            'event_id': str(event.get('event_id') or '').strip(),
            'trade_time_ms': _iso_to_trade_time_ms(event.get('event_at_utc')),
            'source_type': event.get('source_type'),
            'source_name': event.get('source_name'),
            'broker': event.get('broker'),
            'account': event.get('account'),
            'symbol': event.get('symbol'),
            'option_type': event.get('option_type'),
            'side': event.get('side'),
            'position_effect': event.get('event_kind'),
            'contracts': event.get('contracts'),
            'price': None,
            'strike': event.get('strike'),
            'expiration_ymd': event.get('expiration_ymd'),
            'currency': event.get('currency'),
            'record_id': event.get('snapshot_lot_id'),
            'close_target_source_event_id': None,
            'adjust_target_source_event_id': None,
            'void_target_event_id': None,
        }
        for event in events
        if str(event.get('position_key') or '').strip() in matched_position_keys
        or _identity_matches_payload(
            event,
            account=account,
            symbol=symbol,
            option_type=option_type,
            strike=strike,
            expiration_ymd=expiration_ymd,
        )
    ]
    related_events.sort(key=lambda row: (int(row.get('trade_time_ms') or 0), str(row.get('event_id') or '')))

    filtered_diagnostics = [
        item for item in (projection.get('diagnostics') or [])
        if str(item.get('position_key') or '').strip() in matched_position_keys
        or str(item.get('event_id') or '').strip() in {str(event.get('event_id') or '').strip() for event in related_events}
    ]
    latest_reconciliation_report = state.latest_reconciliation_report or None
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
        'persisted_baseline_snapshot_id': state.persisted_baseline_snapshot.get('snapshot_id'),
        'projection_checkpoint_snapshot_id': (state.latest_verification_snapshot or {}).get('snapshot_id'),
        'baseline_snapshot_id': baseline_snapshot.get('snapshot_id'),
        'verification_snapshot_count': len(state.verification_snapshots),
        'accepted_verification_snapshot_count': len(state.accepted_verification_snapshots),
        'latest_verification_snapshot_id': (state.latest_verification_snapshot or {}).get('snapshot_id'),
        'baseline_lots': baseline_lots,
        'related_events': related_events,
        'projection_diagnostics': filtered_diagnostics,
        'all_projection_diagnostic_count': len(projection.get('diagnostics') or []),
        'latest_reconciliation_report': latest_reconciliation_report,
        'latest_reconciliation_summary': (latest_reconciliation_report or {}).get('summary') or {},
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
            snapshot = _load_verification_snapshot_payload(args.snapshot_file)
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
