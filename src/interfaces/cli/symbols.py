"""Manage monitored symbols in options-monitor runtime configs."""

from __future__ import annotations

import argparse
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from src.application.account_config import normalize_accounts
from src.application.config_loader import resolve_watchlist_config, set_watchlist_config
from src.application.config_validator import validate_config
from src.application.runtime_config_paths import write_json_atomic
from src.application.symbol_mutations import add_symbol_entry, edit_symbol_entry, remove_symbol_entry
from src.application.write_contract import attach_write_contract


def load_json(path: Path) -> dict:
    if not path.exists() or path.stat().st_size <= 0:
        raise SystemExit(f"config not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _save_validated_json(path: Path, data: dict) -> None:
    canonical = set_watchlist_config(data, resolve_watchlist_config(data))
    validate_config(dict(canonical))
    write_json_atomic(path, canonical)


def parse_value(s: str) -> Any:
    raw = str(s or "").strip()
    if raw.lower() in ("true", "false"):
        return raw.lower() == "true"
    if raw.lower() in ("null", "none"):
        return None
    try:
        if "." in raw:
            return float(raw)
        return int(raw)
    except Exception:
        return raw


def cmd_list(cfg: dict, fmt: str) -> None:
    rows = []
    for e in (cfg.get("symbols") or []):
        rows.append(
            {
                "symbol": e.get("symbol"),
                "use": e.get("use"),
                "accounts": e.get("accounts"),
                "put": bool((e.get("sell_put") or {}).get("enabled", False)),
                "call": bool((e.get("sell_call") or {}).get("enabled", False)),
                "put_strike": [(e.get("sell_put") or {}).get("min_strike"), (e.get("sell_put") or {}).get("max_strike")],
                "put_dte": [(e.get("sell_put") or {}).get("min_dte"), (e.get("sell_put") or {}).get("max_dte")],
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
        acct = r.get("accounts")
        acct_txt = "all" if not acct else ",".join([str(x) for x in acct])
        print(
            f"- {r['symbol']}: put={'on' if r['put'] else 'off'} call={'on' if r['call'] else 'off'} | "
            f"accounts={acct_txt} | put strike {r['put_strike'][0]}~{r['put_strike'][1]} | "
            f"put dte {r['put_dte'][0]}~{r['put_dte'][1]} | use={r['use']}"
        )


def cmd_add(cfg: dict, symbol: str, use: str, limit_exp: int, put: bool, call: bool, accounts: list[str] | None = None):
    return add_symbol_entry(
        cfg,
        symbol=symbol,
        use=use,
        limit_expirations=limit_exp,
        sell_put_enabled=put,
        sell_call_enabled=call,
        accounts=accounts,
        normalize_accounts=lambda value: normalize_accounts(value, fallback=()),
        error_factory=SystemExit,
    )


def cmd_rm(cfg: dict, symbol: str):
    return remove_symbol_entry(cfg, symbol=symbol, error_factory=SystemExit)


def cmd_edit(cfg: dict, symbol: str, sets: list[str]):
    patch: dict[str, Any] = {}
    for s in sets:
        if "=" not in s:
            raise SystemExit(f"invalid --set: {s} (expected path=value)")
        k, v = s.split("=", 1)
        patch[k.strip()] = parse_value(v)
    return edit_symbol_entry(cfg, symbol=symbol, sets=patch, error_factory=SystemExit)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Manage options-monitor monitored symbols config")
    ap.add_argument("--config", default="config.us.json")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_list = sub.add_parser("list")
    p_list.add_argument("--format", choices=["text", "json"], default="text")
    p_add = sub.add_parser("add")
    p_add.add_argument("symbol")
    p_add.add_argument("--use", default="put_base")
    p_add.add_argument("--limit-exp", type=int, default=8)
    p_add.add_argument("--put", action="store_true")
    p_add.add_argument("--call", action="store_true")
    p_add.add_argument("--accounts", nargs="*", default=None)
    p_add.add_argument("--dry-run", action="store_true")
    p_add.add_argument("--apply", action="store_true")
    p_add.add_argument("--confirm", action="store_true", help="alias for --apply on local config writes")
    p_add.add_argument("--yes", action="store_true", help="non-interactive alias for --apply; emits an audit_id")
    p_add.add_argument("--format", choices=["text", "json"], default="text")
    p_rm = sub.add_parser("rm", aliases=["remove"])
    p_rm.add_argument("symbol")
    p_rm.add_argument("--dry-run", action="store_true")
    p_rm.add_argument("--apply", action="store_true")
    p_rm.add_argument("--confirm", action="store_true", help="alias for --apply on local config writes")
    p_rm.add_argument("--yes", action="store_true", help="non-interactive alias for --apply; emits an audit_id")
    p_rm.add_argument("--format", choices=["text", "json"], default="text")
    p_edit = sub.add_parser("edit")
    p_edit.add_argument("symbol")
    p_edit.add_argument("--set", action="append", default=[])
    p_edit.add_argument("--dry-run", action="store_true")
    p_edit.add_argument("--apply", action="store_true")
    p_edit.add_argument("--confirm", action="store_true", help="alias for --apply on local config writes")
    p_edit.add_argument("--yes", action="store_true", help="non-interactive alias for --apply; emits an audit_id")
    p_edit.add_argument("--format", choices=["text", "json"], default="text")
    args = ap.parse_args(argv)

    base = Path(__file__).resolve().parents[3]
    cfg_path = Path(args.config)
    if not cfg_path.is_absolute():
        cfg_path = (base / cfg_path).resolve()
    cfg = load_json(cfg_path)
    if args.cmd == "list":
        cmd_list(cfg, args.format)
        return 0
    if args.cmd == "add" and not (args.put or args.call):
        raise SystemExit("add requires at least one of: --put, --call")
    if args.cmd == "edit" and not args.set:
        raise SystemExit("edit requires at least one --set path=value")
    preview_cfg = deepcopy(cfg)
    summary = _apply_command(preview_cfg, args)
    write_requested = bool(args.apply or args.confirm or args.yes)
    if args.dry_run and write_requested:
        raise SystemExit("--dry-run cannot be combined with --apply, --confirm, or --yes")
    payload = attach_write_contract(
        summary.public_payload(),
        dry_run=not write_requested,
        write_applied=write_requested,
        rollback_hint=f"restore {cfg_path} from version control or revert this symbol mutation",
    )
    if not write_requested:
        if args.format == "json":
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            _print_preview(summary.public_payload(), cfg_path=cfg_path)
        return 0
    _save_validated_json(cfg_path, preview_cfg)
    if args.format == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"[DONE] {summary.action} {summary.canonical_symbol} -> {cfg_path}")
    return 0


def _apply_command(cfg: dict, args: argparse.Namespace):
    if args.cmd == "add":
        return cmd_add(cfg, args.symbol, args.use, args.limit_exp, args.put, args.call, accounts=args.accounts)
    if args.cmd in {"rm", "remove"}:
        return cmd_rm(cfg, args.symbol)
    if args.cmd == "edit":
        return cmd_edit(cfg, args.symbol, args.set)
    raise SystemExit("unknown cmd")


def _print_preview(summary: dict[str, Any], *, cfg_path: Path) -> None:
    cal = summary.get("calibration") if isinstance(summary.get("calibration"), dict) else {}
    action_text = {"add": "新增", "edit": "修改", "remove": "删除"}.get(str(summary.get("action")), str(summary.get("action")))
    lines = [
        "监控标的变更预览",
        f"操作：{action_text}",
        f"输入：{summary.get('raw_symbol') or '-'}",
        f"校准为：{summary.get('canonical_symbol') or '-'}",
        f"市场：{cal.get('market') or '-'}",
        f"Futu code：{cal.get('futu_code') or '-'}",
        f"来源：{cal.get('source_kind') or '-'}",
        f"配置：{cfg_path}",
    ]
    changed_paths = summary.get("changed_paths")
    if isinstance(changed_paths, list) and changed_paths:
        lines.append("变更：" + "、".join(str(item) for item in changed_paths))
    existing = str(summary.get("existing_symbol") or "").strip()
    if existing:
        lines.append(f"匹配现有记录：{existing}")
    lines.extend(["", "未写入配置。确认写入请追加 --apply。"])
    print("\n".join(lines))


if __name__ == "__main__":
    raise SystemExit(main())
