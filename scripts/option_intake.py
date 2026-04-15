#!/usr/bin/env python3
"""Chat-friendly option intake -> option_positions writer.

Usage examples:
  ./scripts/option_intake.py --text "期权：腾讯20260330 put，strike500，成本5.425每股，乘数100，short 10张，sy，HKD" --dry-run
  ./scripts/option_intake.py --text "期权：腾讯20260330 put，strike500，成本5.425每股，乘数100，short 10张，sy，HKD" --apply
  ./scripts/option_intake.py --text "/om -r -lx open 【成交提醒】成功卖出2张$腾讯 260429 480.00 沽$，成交价格：3.93..."
  ./scripts/option_intake.py --text "/om -r -lx close --record-id recxxx 【成交提醒】成功买入1张$腾讯 260429 480.00 沽$，成交价格：1.20..."

Design:
- Parses message with scripts/cli/parse_option_message_cli.py
- Writes via scripts/option_positions.py add / buy-close
- Default dry-run (safe). Use --apply to persist.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
from dataclasses import dataclass
from json import JSONDecodeError, JSONDecoder
from pathlib import Path


def extract_json_tail(s: str) -> dict:
    decoder = JSONDecoder()
    for i, ch in enumerate(s):
        if ch != '{':
            continue
        try:
            obj, end = decoder.raw_decode(s[i:])
            if isinstance(obj, dict):
                return obj
        except JSONDecodeError:
            continue
    raise SystemExit('no valid JSON payload found from parser output')


def run_capture(cmd: list[str], cwd: Path, timeout_sec: int = 120, env: dict[str, str] | None = None) -> tuple[int, str, str]:
    p = subprocess.run(
        cmd,
        cwd=str(cwd),
        timeout=timeout_sec,
        capture_output=True,
        text=True,
        env=env,
    )
    return p.returncode, (p.stdout or ''), (p.stderr or '')


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
    ap.add_argument('--action', choices=['open', 'close'], default=None, help='explicit action; /om command can also provide open/close')
    ap.add_argument('--account', default=None, help='override parsed account, e.g. lx/sy')
    ap.add_argument('--record-id', default=None, help='required for close/buy-close; no auto matching')
    ap.add_argument('--close-reason', default='manual_buy_to_close')
    ap.add_argument('--dry-run', action='store_true', help='default behavior if neither --dry-run nor --apply specified')
    ap.add_argument('--apply', action='store_true')
    args = ap.parse_args()

    base = Path(__file__).resolve().parents[1]
    py = str(base / '.venv' / 'bin' / 'python')

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

    env = dict(os.environ)
    env.setdefault('PYTHONPATH', str(base))

    parse_cmd = [py, 'scripts/cli/parse_option_message_cli.py', '--text', text]
    if args.config:
        parse_cmd += ['--config', args.config]
    if args.accounts is not None:
        parse_cmd += ['--accounts', *args.accounts]
    code, out, err = run_capture(parse_cmd, cwd=base, timeout_sec=30, env=env)
    if code != 0:
        print(err.strip() or out.strip())
        return code

    parsed = extract_json_tail((out or ''))
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

    if action == 'close':
        if not record_id:
            print('[PARSE_FAIL] missing: record_id')
            print(json.dumps(parsed, ensure_ascii=False, indent=2))
            return 2
        cmd = [
            py, 'scripts/option_positions.py', 'buy-close',
            '--record-id', record_id,
            '--contracts', str(int(p['contracts'])),
            '--close-reason', args.close_reason,
        ]
        if p.get('premium_per_share') is not None:
            cmd += ['--close-price', str(float(p['premium_per_share']))]
    else:
        cmd = [
            py, 'scripts/option_positions.py', 'add',
            '--market', market,
            '--account', p['account'],
            '--symbol', p['symbol'],
            '--option-type', p['option_type'],
            '--side', p['side'],
            '--contracts', str(int(p['contracts'])),
            '--currency', p['currency'],
            '--strike', str(float(p['strike'])),
            '--multiplier', str(int(p['multiplier'])),
            '--exp', p['exp'],
        ]
        if p.get('premium_per_share') is not None:
            cmd += ['--premium-per-share', str(float(p['premium_per_share']))]
        cmd += ['--note', f"user_input: {parsed.get('raw')}"]

    if args.dry_run and (not args.apply):
        cmd.append('--dry-run')

    rc = subprocess.call(cmd, cwd=str(base), env=env)
    return rc


if __name__ == '__main__':
    raise SystemExit(main())
