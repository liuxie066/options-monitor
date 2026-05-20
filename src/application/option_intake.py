#!/usr/bin/env python3
"""Chat-friendly option intake -> trade-events / position-lots writer.

Usage examples:
  python3 -m src.application.option_intake --text "期权：腾讯20260330 put，strike500，成本5.425每股，乘数100，short 10张，sy，HKD" --dry-run
  python3 -m src.application.option_intake --text "期权：腾讯20260330 put，strike500，成本5.425每股，乘数100，short 10张，sy，HKD" --apply
  python3 -m src.application.option_intake --text "/om open lx -r -- 【成交提醒】成功卖出2张$腾讯 260429 480.00 沽$，成交价格：3.93..."
  python3 -m src.application.option_intake --text "/om close sy -id recxxx -a -- 【成交提醒】成功买入1张$腾讯 260429 480.00 沽$，成交价格：1.20..."

Design:
- Parses message with src.application.parse_option_message.parse_option_message_text
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
from typing import Any

repo_base = Path(__file__).resolve().parents[2]
if str(repo_base) not in sys.path:
    sys.path.insert(0, str(repo_base))

from src.application.account_config import accounts_from_config_path
from src.application.config_loader import load_config
from src.application.parse_option_message import parse_option_message_text
from src.application.ledger.api import (
    inspect_ledger_stores,
    ledger_store_write_guard,
    ledger_store_payload,
    open_position_ledger_from_data_config,
    open_position_ledger_from_runtime_config,
    resolve_ledger_store,
    resolve_position_data_config_path,
)
from src.application.positions.workflows import (
    ManualCloseMatchError,
    execute_manual_close,
    execute_manual_open,
    format_manual_close_match_error,
)
from src.application.trade_time_format import format_trade_time_beijing
from src.application.trades.intent import trade_intent_from_manual_parse


@dataclass(frozen=True)
class IntakeCommand:
    text: str
    action: str | None = None
    account: str | None = None
    record_id: str | None = None
    dry_run: bool | None = None
    apply: bool | None = None


def _account_from_token(token: str) -> str | None:
    raw = str(token or "").strip()
    low = raw.lower()
    if low in ("-lx", "--lx", "lx"):
        return "lx"
    if low in ("-sy", "--sy", "sy"):
        return "sy"
    for prefix in ("@", "acct:", "account:"):
        if low.startswith(prefix) and len(raw) > len(prefix):
            return raw[len(prefix):].strip().lower()
    return None


def _record_id_from_token(token: str) -> str | None:
    raw = str(token or "").strip()
    low = raw.lower()
    for prefix in ("id:", "rec:", "record:"):
        if low.startswith(prefix) and len(raw) > len(prefix):
            return raw[len(prefix):].strip()
    if low.startswith("rec") and len(raw) > 3:
        return raw
    if low.startswith("lot_") and len(raw) > 4:
        return raw
    return None


def _is_close_token(token: str) -> bool:
    return str(token or "").strip().lower() in {
        "close",
        "c",
        "btc",
        "buy-close",
        "buy_close",
        "buytoclose",
        "平",
        "平仓",
        "平倉",
        "买平",
        "買平",
        "买入平仓",
        "買入平倉",
    }


def parse_om_command(text: str) -> IntakeCommand:
    """Parse lightweight chat command prefixes.

    Supported forms:
    - /om open lx -r -- <message>
    - /om close sy -id recxxx -a -- <message>
    - /om btc sy recxxx -r -- <message>
    - /om open @account --apply -- <message>
    - /om -r -lx open <message>
    - /om --apply --account lx close --record-id recxxx <message>
    - /om -r -sy c -id recxxx <message>

    Use "--" before the broker message when possible. Without it, the first
    unknown token after /om is treated as the beginning of the message body.
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
        if low == "--":
            body_start = i + 1
            break
        if low == "buy" and i + 2 < len(tokens) and tokens[i + 1].lower() == "to" and tokens[i + 2].lower() == "close":
            action = "close"
            i += 3
            continue
        if low in ("-r", "--review", "--dry-run", "dry-run", "review", "预览", "检查"):
            dry_run = True
            apply_flag = False
            i += 1
            continue
        if low in ("--apply", "-a", "apply", "确认", "写入"):
            apply_flag = True
            dry_run = False
            i += 1
            continue
        token_account = _account_from_token(tok)
        if token_account:
            account = token_account
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
        token_record_id = _record_id_from_token(tok)
        if token_record_id:
            record_id = token_record_id
            i += 1
            continue
        if low in ("open", "o", "开仓", "開倉"):
            action = "open"
            i += 1
            continue
        if _is_close_token(tok):
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


def _missing_for_action(parsed: dict[str, Any], action: str) -> list[str]:
    raw_parsed = parsed.get("parsed")
    p: dict[str, Any] = raw_parsed if isinstance(raw_parsed, dict) else {}
    if action == "close":
        return [
            k for k, v in {
                "contracts": p.get("contracts"),
                "account": p.get("account"),
                "close_price": p.get("premium_per_share"),
            }.items() if v in (None, "")
        ]
    return list(parsed.get("missing") or [])


def _target_position_side_for_close(parsed_fields: dict[str, Any], raw_text: str) -> str | None:
    raw = str(raw_text or "").strip().lower()
    if "买" in raw or "買" in raw or "buy" in raw:
        return "short"
    if "卖" in raw or "賣" in raw or "sell" in raw:
        return "long"
    side = str(parsed_fields.get("side") or "").strip().lower()
    if side in {"short", "long"}:
        return side
    return None


def _print_ledger_target(*, data_config: Path, repo: Any | None, config_path: Path | None, runtime_root: str | None = None) -> bool:
    store = (
        ledger_store_payload(data_config, repo)
        if repo is not None
        else resolve_ledger_store(data_config, config_path=config_path, runtime_root=runtime_root).to_dict()
    )
    sqlite_path = str(store.get("sqlite_path") or "")
    runtime_root = str(store.get("runtime_root") or "")
    runtime_root_source = str(store.get("runtime_root_source") or "")
    print(f"[LEDGER] sqlite={sqlite_path} runtime_root={runtime_root} source={runtime_root_source}")
    inspection = inspect_ledger_stores(data_config, config_path=config_path, runtime_root=runtime_root)
    raw_warnings = inspection.get("warnings")
    warnings = [str(item) for item in raw_warnings if str(item)] if isinstance(raw_warnings, list) else []
    for warning in warnings:
        print(f"[LEDGER_WARN] {warning}")
    raw_summary = inspection.get("summary")
    summary: dict[str, Any] = raw_summary if isinstance(raw_summary, dict) else {}
    return bool(summary.get("multiple_populated") or summary.get("active_empty_but_other_populated"))


def _print_ledger_guard_failure(guard: dict[str, Any]) -> None:
    raw_errors = guard.get("errors")
    errors = raw_errors if isinstance(raw_errors, list) else []
    if errors:
        print("[LEDGER_FAIL] divergent populated ledger stores detected; aborting apply")
    for error in errors:
        print(f"[LEDGER_FAIL] {error}")
    raw_active = guard.get("active")
    active: dict[str, Any] = raw_active if isinstance(raw_active, dict) else {}
    print(
        f"[LEDGER] sqlite={active.get('sqlite_path') or '-'} "
        f"runtime_root={active.get('runtime_root') or '-'} "
        f"source={active.get('runtime_root_source') or '-'}"
    )
    raw_remediation = guard.get("remediation")
    remediation = raw_remediation if isinstance(raw_remediation, list) else []
    for item in remediation:
        print(f"[REMEDIATION] {item}")


def _copy_with_beijing_time_fields(fields: dict[str, Any], *, keys: tuple[str, ...]) -> dict[str, Any]:
    out = dict(fields)
    for key in keys:
        formatted = format_trade_time_beijing(out.get(key))
        if formatted is not None:
            out[f"{key}_beijing"] = formatted
    return out


def _first_time_ms(*values: Any) -> Any:
    for value in values:
        if value not in (None, ""):
            return value
    return None


def _format_done_trade_time(ms: Any) -> str:
    formatted = format_trade_time_beijing(ms)
    return f" 成交时间={formatted}" if formatted else ""


def main() -> int:
    ap = argparse.ArgumentParser(description='Option intake (parse + write)')
    ap.add_argument('--text', required=True)
    ap.add_argument('--config', default=None, help='optional options-monitor config used to resolve account labels')
    ap.add_argument('--accounts', nargs='*', default=None, help='optional account labels to recognize')
    ap.add_argument('--market', default='富途')
    ap.add_argument('--data-config', default=None, help='portfolio data config path; auto-resolves when omitted')
    ap.add_argument('--runtime-root', default=None, help='runtime root for active ledger store, e.g. /var/lib/options-monitor')
    ap.add_argument('--action', choices=['open', 'close'], default=None, help='explicit action; /om command can also provide open/close')
    ap.add_argument('--account', default=None, help='override parsed account, e.g. lx/sy')
    ap.add_argument('--record-id', default=None, help='optional for close/buy-close; omitted close uses strict unique auto matching')
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

    runtime_config: dict[str, Any] | None = None
    cfg_path: Path | None = None
    if args.config:
        cfg_path = Path(args.config)
        if not cfg_path.is_absolute():
            cfg_path = (base / cfg_path).resolve()
        runtime_config = load_config(base=base, config_path=cfg_path, is_scheduled=False, log=lambda msg: print(msg, file=sys.stderr))

    accounts = args.accounts
    if accounts is None and cfg_path is not None:
        accounts = accounts_from_config_path(cfg_path)

    parsed = parse_option_message_text(text, accounts=accounts, resolve_multiplier=(action == 'open'))
    raw_fields = parsed.get("parsed")
    p: dict[str, Any] = raw_fields if isinstance(raw_fields, dict) else {}
    if account_override:
        p['account'] = str(account_override).strip().lower()
        parsed['missing'] = [x for x in (parsed.get('missing') or []) if x != 'account']

    missing = _missing_for_action(parsed, action)
    if missing:
        print('[PARSE_FAIL] missing: ' + ','.join(missing))
        print(json.dumps(parsed, ensure_ascii=False, indent=2))
        return 2

    market = (p.get('market') or args.market)
    intent = trade_intent_from_manual_parse(
        parsed,
        action=action,
        raw_text=text,
        broker=market,
        record_id=record_id,
    )
    data_config_path = resolve_position_data_config_path(
        base=base,
        cfg=runtime_config,
        data_config=args.data_config,
        config_path=cfg_path,
    )
    if args.dry_run or args.apply or action == "close":
        _print_ledger_target(
            data_config=data_config_path,
            repo=None,
            config_path=cfg_path,
            runtime_root=args.runtime_root,
        )
        if args.apply:
            guard = ledger_store_write_guard(data_config_path, config_path=cfg_path, runtime_root=args.runtime_root)
            if not bool(guard.get("ok")):
                _print_ledger_guard_failure(guard)
                return 2

    need_repo = action == 'close' or bool(args.apply)
    repo = None
    if need_repo:
        if runtime_config is not None:
            _resolved_data_config, repo = open_position_ledger_from_runtime_config(
                base=base,
                cfg=runtime_config,
                data_config=args.data_config,
                config_path=cfg_path,
                runtime_root=args.runtime_root,
            )
        elif cfg_path is not None or args.runtime_root:
            _resolved_data_config, repo = open_position_ledger_from_runtime_config(
                base=base,
                cfg=None,
                data_config=args.data_config,
                config_path=cfg_path,
                runtime_root=args.runtime_root,
            )
        else:
            _resolved_data_config, repo = open_position_ledger_from_data_config(
                base=base,
                data_config=args.data_config,
            )

    if action == 'close':
        target_position_side = intent.target_position_side or _target_position_side_for_close(p, text)
        try:
            out = execute_manual_close(
                repo,
                record_id=intent.record_id,
                contracts_to_close=int(intent.contracts or 0),
                close_price=intent.price,
                close_reason=args.close_reason,
                dry_run=bool(args.dry_run and not args.apply),
                broker=intent.broker,
                account=intent.account,
                symbol=intent.symbol,
                option_type=intent.option_type,
                position_side=target_position_side,
                strike=intent.strike,
                expiration_ymd=intent.expiration_ymd,
                as_of_ms=intent.trade_time_ms,
            )
        except ManualCloseMatchError as exc:
            print(format_manual_close_match_error(exc))
            return 2
        except ValueError as exc:
            print(str(exc))
            return 2
        raw_match = out.get("match")
        match: dict[str, Any] = raw_match if isinstance(raw_match, dict) else {}
        if match.get("rule") == "strict_contract_unique":
            print(f"[MATCH] rule={match.get('rule')} record_id={match.get('record_id')}")
        if args.dry_run and (not args.apply):
            print('[DRY_RUN] update fields:')
            print(json.dumps(_copy_with_beijing_time_fields(out['patch'], keys=("closed_at", "last_action_at")), ensure_ascii=False, indent=2))
            return 0
        closed_record_id = (match.get("record_id") if match else None) or intent.record_id
        trade_time_ms = _first_time_ms(
            intent.trade_time_ms,
            (out.get("ledger_preflight") or {}).get("event_time_ms") if isinstance(out.get("ledger_preflight"), dict) else None,
            (out.get("patch") or {}).get("closed_at") if isinstance(out.get("patch"), dict) else None,
        )
        print(
            f"[DONE] buy-closed {closed_record_id} contracts={int(intent.contracts or 0)} "
            f"event_id={out['result'].get('event_id')}{_format_done_trade_time(trade_time_ms)}"
        )
        return 0
    else:
        try:
            out = execute_manual_open(
                repo,
                broker=intent.broker,
                account=str(intent.account or ""),
                symbol=str(intent.symbol or ""),
                option_type=str(intent.option_type or ""),
                side=str(intent.target_position_side or ""),
                contracts=int(intent.contracts or 0),
                currency=intent.currency,
                strike=intent.strike,
                multiplier=float(intent.multiplier) if intent.multiplier is not None else None,
                expiration_ymd=intent.expiration_ymd,
                premium_per_share=intent.price,
                underlying_share_locked=None,
                note=f"user_input: {parsed.get('raw')}",
                opened_at_ms=intent.trade_time_ms,
                dry_run=bool(args.dry_run and not args.apply),
            )
        except ValueError as exc:
            print(str(exc))
            return 2
        if args.dry_run and (not args.apply):
            print('[DRY_RUN] create fields:')
            print(json.dumps(_copy_with_beijing_time_fields(out['fields'], keys=("opened_at", "last_action_at")), ensure_ascii=False, indent=2))
            return 0
        raw_fields_out = out.get("fields")
        fields: dict[str, Any] = raw_fields_out if isinstance(raw_fields_out, dict) else {}
        trade_time_ms = _first_time_ms(intent.trade_time_ms, fields.get("opened_at"), fields.get("last_action_at"))
        raw_result = out.get("result")
        result: dict[str, Any] = raw_result if isinstance(raw_result, dict) else {}
        print(f"[DONE] created event_id={result.get('event_id')}{_format_done_trade_time(trade_time_ms)}")
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
