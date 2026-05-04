#!/usr/bin/env python3
"""Chat-friendly option intake -> trade-events / position-lots writer.

Usage examples:
  ./scripts/option_intake.py --text "期权：腾讯20260330 put，strike500，成本5.425每股，乘数100，short 10张，sy，HKD" --dry-run
  ./scripts/option_intake.py --text "期权：腾讯20260330 put，strike500，成本5.425每股，乘数100，short 10张，sy，HKD" --apply
  ./scripts/option_intake.py --text "/om -r -lx open 【成交提醒】成功卖出2张$腾讯 260429 480.00 沽$，成交价格：3.93..."
  ./scripts/option_intake.py --text "/om -r -lx close --record-id recxxx 【成交提醒】成功买入1张$腾讯 260429 480.00 沽$，成交价格：1.20..."

Design:
- Parses message with scripts.parse_option_message.parse_option_message_text
- Writes through shared position workflow application helpers
- Default dry-run (safe). Use --apply to persist.
"""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path

repo_base = Path(__file__).resolve().parents[1]
if str(repo_base) not in sys.path:
    sys.path.insert(0, str(repo_base))

from scripts.account_config import accounts_from_config_path
from scripts.parse_option_message import parse_option_message_text
from src.application.option_positions_facade import resolve_option_positions_repo
from src.application.position_workflows import execute_manual_close, execute_manual_open


@dataclass(frozen=True)
class IntakeCommand:
    text: str
    action: str | None = None
    account: str | None = None
    record_id: str | None = None
    dry_run: bool | None = None
    apply: bool | None = None


def parse_om_command(text: str) -> IntakeCommand:
    """Parse lightweight chat command prefixes.

    Supported forms:
    - /om -r -lx open <message>
    - /om --apply --account lx close --record-id recxxx <message>
    - /om -r -sy c -id recxxx <message>

    Unknown tokens after /om are treated as the beginning of the message body.
    """
    raw = str(text or "").strip()
    if not raw.startswith("/om"):
        return IntakeCommand(text=raw)

    try:
        tokens = shlex.split(raw)
    except ValueError:
        tokens = raw.split()
    if not tokens or tokens[0] != "/om":
        return IntakeCommand(text=raw)

    action: str | None = None
    account: str | None = None
    record_id: str | None = None
    dry_run: bool | None = None
    apply_flag: bool | None = None
    body_start = len(tokens)
    i = 1
    while i < len(tokens):
        tok = tokens[i]
        low = tok.lower()
        if low in ("-r", "--review", "--dry-run", "dry-run"):
            dry_run = True
            apply_flag = False
            i += 1
            continue
        if low in ("--apply", "-a", "apply"):
            apply_flag = True
            dry_run = False
            i += 1
            continue
        if low in ("-lx", "--lx"):
            account = "lx"
            i += 1
            continue
        if low in ("-sy", "--sy"):
            account = "sy"
            i += 1
            continue
        if low in ("--account", "-acct") and i + 1 < len(tokens):
            account = tokens[i + 1].strip().lower()
            i += 2
            continue
        if low.startswith("--account="):
            account = tok.split("=", 1)[1].strip().lower()
            i += 1
            continue
        if low in ("--record-id", "--record_id", "-id") and i + 1 < len(tokens):
            record_id = tokens[i + 1].strip()
            i += 2
            continue
        if low.startswith("--record-id=") or low.startswith("--record_id="):
            record_id = tok.split("=", 1)[1].strip()
            i += 1
            continue
        if low in ("open", "o", "开仓", "開倉"):
            action = "open"
            i += 1
            continue
        if low in ("close", "c", "平仓", "平倉", "buy-close", "buy_close"):
            action = "close"
            i += 1
            continue
        body_start = i
        break

    body = " ".join(tokens[body_start:]).strip()
    return IntakeCommand(
        text=body,
        action=action,
        account=account,
        record_id=record_id,
        dry_run=dry_run,
        apply=apply_flag,
    )


def _missing_for_action(parsed: dict, action: str) -> list[str]:
    p = parsed.get("parsed") or {}
    if action == "close":
        return [
            k for k, v in {
                "contracts": p.get("contracts"),
                "account": p.get("account"),
                "close_price": p.get("premium_per_share"),
            }.items() if v in (None, "")
        ]
    return list(parsed.get("missing") or [])


def main():
    ap = argparse.ArgumentParser(description='Option intake (parse + write)')
    ap.add_argument('--text', required=True)
    ap.add_argument('--config', default=None, help='optional options-monitor config used to resolve account labels')
    ap.add_argument('--accounts', nargs='*', default=None, help='optional account labels to recognize')
    ap.add_argument('--market', default='富途')
    ap.add_argument('--data-config', default=None, help='portfolio data config path; auto-resolves when omitted')
    ap.add_argument('--action', choices=['open', 'close'], default=None, help='explicit action; /om command can also provide open/close')
    ap.add_argument('--account', default=None, help='override parsed account, e.g. lx/sy')
    ap.add_argument('--record-id', default=None, help='required for close/buy-close; no auto matching')
    ap.add_argument('--close-reason', default='manual_buy_to_close')
    ap.add_argument('--dry-run', action='store_true', help='default behavior if neither --dry-run nor --apply specified')
    ap.add_argument('--apply', action='store_true')
    args = ap.parse_args()

    base = repo_base

    command = parse_om_command(args.text)
    action = args.action or command.action or 'open'
    account_override = args.account or command.account
    record_id = args.record_id or command.record_id
    text = command.text or args.text

    # default safe mode; command flags can set dry-run/apply when CLI flags are absent.
    if not args.dry_run and not args.apply:
        if command.apply is True:
            args.apply = True
        elif command.dry_run is True:
            args.dry_run = True
        else:
            args.dry_run = True

    accounts = args.accounts
    if accounts is None and args.config:
        cfg_path = Path(args.config)
        if not cfg_path.is_absolute():
            cfg_path = (base / cfg_path).resolve()
        accounts = accounts_from_config_path(cfg_path)

    parsed = parse_option_message_text(text, accounts=accounts)
    p = parsed['parsed']
    if account_override:
        p['account'] = str(account_override).strip().lower()
        parsed['missing'] = [x for x in (parsed.get('missing') or []) if x != 'account']

    missing = _missing_for_action(parsed, action)
    if missing:
        print('[PARSE_FAIL] missing: ' + ','.join(missing))
        print(json.dumps(parsed, ensure_ascii=False, indent=2))
        return 2

    market = (p.get('market') or args.market)
    need_repo = action == 'close' or bool(args.apply)
    repo = None
    if need_repo:
        _data_config, repo = resolve_option_positions_repo(base=base, data_config=args.data_config)

    if action == 'close':
        if not record_id:
            print('[PARSE_FAIL] missing: record_id')
            print(json.dumps(parsed, ensure_ascii=False, indent=2))
            return 2
        try:
            out = execute_manual_close(
                repo,
                record_id=record_id,
                contracts_to_close=int(p['contracts']),
                close_price=(float(p['premium_per_share']) if p.get('premium_per_share') is not None else None),
                close_reason=args.close_reason,
                dry_run=bool(args.dry_run and not args.apply),
            )
        except ValueError as exc:
            print(str(exc))
            return 2
        if args.dry_run and (not args.apply):
            print('[DRY_RUN] update fields:')
            print(json.dumps(out['patch'], ensure_ascii=False, indent=2))
            return 0
        print(f"[DONE] buy-closed {record_id} contracts={int(p['contracts'])} event_id={out['result'].get('event_id')}")
        return 0
    else:
        try:
            out = execute_manual_open(
                repo,
                broker=market,
                account=p['account'],
                symbol=p['symbol'],
                option_type=p['option_type'],
                side=p['side'],
                contracts=int(p['contracts']),
                currency=p['currency'],
                strike=float(p['strike']),
                multiplier=float(p['multiplier']) if p.get('multiplier') is not None else None,
                expiration_ymd=p['exp'],
                premium_per_share=(float(p['premium_per_share']) if p.get('premium_per_share') is not None else None),
                underlying_share_locked=None,
                note=f"user_input: {parsed.get('raw')}",
                opened_at_ms=p.get('fill_time_ms'),
                dry_run=bool(args.dry_run and not args.apply),
            )
        except ValueError as exc:
            print(str(exc))
            return 2
        if args.dry_run and (not args.apply):
            print('[DRY_RUN] create fields:')
            print(json.dumps(out['fields'], ensure_ascii=False, indent=2))
            return 0
        print(f"[DONE] created event_id={out['result'].get('event_id')}")
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
