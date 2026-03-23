#!/usr/bin/env python3
"""Manage monitored symbols in options-monitor/config.json.

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


def load_json(path: Path) -> dict:
    if not path.exists() or path.stat().st_size <= 0:
        raise SystemExit(f"config not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, data: dict):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def norm_symbol(s: str) -> str:
    return s.strip().upper()


def find_symbol_entry(cfg: dict, symbol: str):
    sym = norm_symbol(symbol)
    for i, e in enumerate(cfg.get("symbols") or []):
        if norm_symbol(str(e.get("symbol") or "")) == sym:
            return i, e
    return None, None


def set_path(obj: dict, path: str, value):
    # dot path e.g. sell_put.enabled or fetch.limit_expirations
    cur = obj
    parts = path.split(".")
    for p in parts[:-1]:
        if p not in cur or not isinstance(cur[p], dict):
            cur[p] = {}
        cur = cur[p]
    cur[parts[-1]] = value


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

    print("# options-monitor watchlist")
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
    sym = norm_symbol(symbol)
    _, existing = find_symbol_entry(cfg, sym)
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
        entry['accounts'] = [norm_symbol(a) for a in accounts if str(a).strip()]

    cfg.setdefault("symbols", [])
    cfg["symbols"].append(entry)


def cmd_rm(cfg: dict, symbol: str):
    sym = norm_symbol(symbol)
    idx, _ = find_symbol_entry(cfg, sym)
    if idx is None:
        raise SystemExit(f"symbol not found: {sym}")
    cfg["symbols"].pop(idx)


def cmd_edit(cfg: dict, symbol: str, sets: list[str]):
    sym = norm_symbol(symbol)
    idx, entry = find_symbol_entry(cfg, sym)
    if idx is None or entry is None:
        raise SystemExit(f"symbol not found: {sym}")

    for s in sets:
        if "=" not in s:
            raise SystemExit(f"invalid --set: {s} (expected path=value)")
        k, v = s.split("=", 1)
        set_path(entry, k.strip(), parse_value(v))

    cfg["symbols"][idx] = entry


def main():
    ap = argparse.ArgumentParser(description="Manage options-monitor watchlist (config.json symbols)")
    ap.add_argument("--config", default="config.json")

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
        save_json(cfg_path, cfg)
        print(f"[DONE] added {norm_symbol(args.symbol)} -> {cfg_path}")
        return

    if args.cmd == "rm":
        cmd_rm(cfg, args.symbol)
        save_json(cfg_path, cfg)
        print(f"[DONE] removed {norm_symbol(args.symbol)} -> {cfg_path}")
        return

    if args.cmd == "edit":
        if not args.set:
            raise SystemExit("edit requires at least one --set path=value")
        cmd_edit(cfg, args.symbol, args.set)
        save_json(cfg_path, cfg)
        print(f"[DONE] edited {norm_symbol(args.symbol)} -> {cfg_path}")
        return

    raise SystemExit("unknown cmd")


if __name__ == "__main__":
    main()
