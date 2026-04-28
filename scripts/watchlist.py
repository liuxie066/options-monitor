#!/usr/bin/env python3
"""Manage monitored symbols in options-monitor config.us.json/config.hk.json.

Supports:
- list: show current symbols and basic config
- add: add a new symbol skeleton
- rm: remove a symbol
- edit: patch fields on an existing symbol

This is intentionally lightweight and file-based (no external deps beyond stdlib).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from scripts.config_loader import resolve_watchlist_config, set_watchlist_config
from scripts.validate_config import validate_config
from src.application.runtime_config_paths import write_json_atomic
from src.application.watchlist_mutations import ensure_symbols_list, find_symbol_entry, normalize_symbol, set_path


def load_json(path: Path) -> dict:
    if not path.exists() or path.stat().st_size <= 0:
        raise SystemExit(f"config not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _save_validated_json(path: Path, data: dict) -> None:
    canonical = set_watchlist_config(data, resolve_watchlist_config(data))
    validate_config(dict(canonical))
    write_json_atomic(path, canonical)


def parse_value(s: str):
    # json-ish: true/false/null/number/string
    s = s.strip()
    if s.lower() in ("true", "false"):
        return s.lower() == "true"
    if s.lower() in ("null", "none"):
        return None
    try:
        if "." in s:
            return float(s)
        return int(s)
    except Exception:
        return s


def cmd_list(cfg: dict, fmt: str):
    rows = []
    for e in (cfg.get("symbols") or []):
        rows.append(
            {
                "symbol": e.get("symbol"),
                "use": e.get("use"),
                "accounts": e.get("accounts"),
                "put": bool((e.get("sell_put") or {}).get("enabled", False)),
                "call": bool((e.get("sell_call") or {}).get("enabled", False)),
                "put_strike": [
                    (e.get("sell_put") or {}).get("min_strike"),
                    (e.get("sell_put") or {}).get("max_strike"),
                ],
                "put_dte": [
                    (e.get("sell_put") or {}).get("min_dte"),
                    (e.get("sell_put") or {}).get("max_dte"),
                ],
            }
        )

    if fmt == "json":
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return

    if not rows:
        print("(no symbols)")
        return

    print("# options-monitor symbols")
    for r in rows:
        use = r["use"]
        acct = r.get('accounts')
        acct_txt = 'all' if not acct else ','.join([str(x) for x in acct])
        print(
            f"- {r['symbol']}: put={'on' if r['put'] else 'off'} call={'on' if r['call'] else 'off'} | "
            f"accounts={acct_txt} | "
            f"put strike {r['put_strike'][0]}~{r['put_strike'][1]} | put dte {r['put_dte'][0]}~{r['put_dte'][1]} | use={use}"
        )


def cmd_add(cfg: dict, symbol: str, use: str, limit_exp: int, put: bool, call: bool, accounts: list[str] | None = None):
    sym = normalize_symbol(symbol)
    _, existing = find_symbol_entry(cfg, sym, resolve_watchlist_config=resolve_watchlist_config)
    if existing:
        raise SystemExit(f"symbol already exists: {sym}")

    entry = {
        "symbol": sym,
        "fetch": {"limit_expirations": int(limit_exp)},
        "use": use,
        "sell_put": {"enabled": bool(put)},
        "sell_call": {"enabled": bool(call)},
    }

    if accounts is not None:
        entry['accounts'] = [normalize_symbol(a) for a in accounts if str(a).strip()]

    ensure_symbols_list(cfg, error_factory=SystemExit).append(entry)


def cmd_rm(cfg: dict, symbol: str):
    sym = normalize_symbol(symbol)
    idx, _ = find_symbol_entry(cfg, sym, resolve_watchlist_config=resolve_watchlist_config)
    if idx is None:
        raise SystemExit(f"symbol not found: {sym}")
    ensure_symbols_list(cfg, error_factory=SystemExit).pop(idx)


def cmd_edit(cfg: dict, symbol: str, sets: list[str]):
    sym = normalize_symbol(symbol)
    idx, entry = find_symbol_entry(cfg, sym, resolve_watchlist_config=resolve_watchlist_config)
    if idx is None or entry is None:
        raise SystemExit(f"symbol not found: {sym}")

    for s in sets:
        if "=" not in s:
            raise SystemExit(f"invalid --set: {s} (expected path=value)")
        k, v = s.split("=", 1)
        set_path(entry, k.strip(), parse_value(v))

    ensure_symbols_list(cfg, error_factory=SystemExit)[idx] = entry


def main():
    ap = argparse.ArgumentParser(description="Manage options-monitor symbols config (config.us.json/config.hk.json)")
    ap.add_argument("--config", default="config.us.json")

    sub = ap.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list")
    p_list.add_argument("--format", choices=["text", "json"], default="text")

    p_add = sub.add_parser("add")
    p_add.add_argument("symbol")
    p_add.add_argument("--use", default="put_base", help="template(s) to use, e.g. put_base or call_base")
    p_add.add_argument("--limit-exp", type=int, default=8)
    p_add.add_argument("--put", action="store_true", help="enable sell_put")
    p_add.add_argument("--call", action="store_true", help="enable sell_call")
    p_add.add_argument("--accounts", nargs='*', default=None, help="optional accounts list for this symbol (e.g. lx sy). default: all accounts")

    p_rm = sub.add_parser("rm")
    p_rm.add_argument("symbol")

    p_edit = sub.add_parser("edit")
    p_edit.add_argument("symbol")
    p_edit.add_argument("--set", action="append", default=[], help="patch path=value (repeatable)")

    args = ap.parse_args()

    base = Path(__file__).resolve().parents[1]
    cfg_path = Path(args.config)
    if not cfg_path.is_absolute():
        cfg_path = (base / cfg_path).resolve()

    cfg = load_json(cfg_path)

    if args.cmd == "list":
        cmd_list(cfg, args.format)
        return

    if args.cmd == "add":
        if not (args.put or args.call):
            raise SystemExit("add requires at least one of: --put, --call")
        cmd_add(cfg, args.symbol, args.use, args.limit_exp, args.put, args.call, accounts=args.accounts)
        _save_validated_json(cfg_path, cfg)
        print(f"[DONE] added {normalize_symbol(args.symbol)} -> {cfg_path}")
        return

    if args.cmd == "rm":
        cmd_rm(cfg, args.symbol)
        _save_validated_json(cfg_path, cfg)
        print(f"[DONE] removed {normalize_symbol(args.symbol)} -> {cfg_path}")
        return

    if args.cmd == "edit":
        if not args.set:
            raise SystemExit("edit requires at least one --set path=value")
        cmd_edit(cfg, args.symbol, args.set)
        _save_validated_json(cfg_path, cfg)
        print(f"[DONE] edited {normalize_symbol(args.symbol)} -> {cfg_path}")
        return

    raise SystemExit("unknown cmd")


if __name__ == "__main__":
    main()
