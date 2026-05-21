from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, cast

from src.application.ledger.api import (
    ledger_store_write_guard,
    ledger_store_payload,
    open_position_ledger_from_runtime_config as resolve_option_positions_repo,
    resolve_position_data_config_path,
)
from src.application.trades.review import (
    apply_repair_trade_event,
    apply_void_trade_event,
    list_trade_event_reviews,
    preview_repair_trade_event,
    preview_void_trade_event,
    replay_trade_events,
    show_trade_event_review,
)
from src.application.trade_time_format import format_trade_time_beijing
from src.application.write_contract import attach_write_contract, write_control


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _repair_overrides(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "broker": args.broker,
        "account": args.account,
        "symbol": args.symbol,
        "option_type": args.option_type,
        "side": args.side,
        "position_effect": args.effect,
        "contracts": args.contracts,
        "price": args.price,
        "strike": args.strike,
        "multiplier": args.multiplier,
        "expiration_ymd": args.exp,
        "currency": args.currency,
        "trade_time_ms": args.trade_time_ms,
        "order_id": args.order_id,
        "record_id": args.record_id,
        "close_target_source_event_id": args.close_target_source_event_id,
    }


def _runtime_root_arg(args: argparse.Namespace) -> str | None:
    return str(getattr(args, "runtime_root", "") or "").strip() or None


def _print_guard_failure(guard: dict[str, object], *, as_json: bool) -> None:
    payload = {"ok": False, "error": "ledger_store_guard_failed", "ledger_store_guard": guard}
    if as_json:
        _print_json(payload)
        return
    raw_errors = guard.get("errors")
    errors = raw_errors if isinstance(raw_errors, list) else []
    for error in errors:
        print(f"[LEDGER_FAIL] {error}")
    raw_active = guard.get("active")
    active = cast(dict[str, object], raw_active) if isinstance(raw_active, dict) else {}
    print(
        f"[LEDGER] sqlite={active.get('sqlite_path') or '-'} "
        f"runtime_root={active.get('runtime_root') or '-'} "
        f"source={active.get('runtime_root_source') or '-'}"
    )
    raw_remediation = guard.get("remediation")
    remediation = raw_remediation if isinstance(raw_remediation, list) else []
    for item in remediation:
        print(f"[REMEDIATION] {item}")


def _guard_write(
    *,
    data_config: Path,
    args: argparse.Namespace,
    as_json: bool,
) -> dict[str, object] | None:
    guard = ledger_store_write_guard(data_config, runtime_root=_runtime_root_arg(args))
    if bool(guard.get("ok")):
        return guard
    _print_guard_failure(guard, as_json=as_json)
    return None


def _add_write_flags(parser: argparse.ArgumentParser, *, high_risk: bool) -> None:
    parser.add_argument("--apply", action="store_true", help="allow local state writes")
    if high_risk:
        parser.add_argument("--confirm", action="store_true", help="confirm high-risk trade-event writes")
        parser.add_argument("--yes", action="store_true", help="non-interactive confirmation; emits an audit_id")
    else:
        parser.add_argument("--confirm", action="store_true", help="alias for --apply on local state writes")
        parser.add_argument("--yes", action="store_true", help="non-interactive alias for --apply; emits an audit_id")
    parser.add_argument("--dry-run", action="store_true", help="preview without writing; this is the default")


def _resolve_write_control(args: argparse.Namespace, *, command_name: str, high_risk: bool) -> dict[str, bool]:
    has_dry_run = bool(getattr(args, "dry_run", False))
    has_write_flag = any(bool(getattr(args, name, False)) for name in ("apply", "confirm", "yes"))
    if has_dry_run and has_write_flag:
        raise SystemExit("--dry-run cannot be combined with --apply, --confirm, or --yes")
    control = write_control(
        apply=bool(getattr(args, "apply", False)),
        confirm=bool(getattr(args, "confirm", False)),
        yes=bool(getattr(args, "yes", False)),
        high_risk=high_risk,
    )
    if control["confirmation_required"]:
        raise SystemExit(f"{command_name} writes trade_events; use --confirm or --yes to apply")
    return control


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Review, repair, replay, and void trade events")
    parser.add_argument("--data-config", default=None, help="portfolio data config path; auto-resolves when omitted")
    parser.add_argument("--runtime-root", default=None, help="runtime root for active ledger store, e.g. /var/lib/options-monitor")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="list trade events with review status")
    p_list.add_argument("--status", default="all", choices=["all", "active", "needs_review", "voided", "void_event"])
    p_list.add_argument("--broker", default=None)
    p_list.add_argument("--account", default=None)
    p_list.add_argument("--limit", type=int, default=50)
    p_list.add_argument("--format", choices=["text", "json"], default="text")

    p_show = sub.add_parser("show", help="show one trade event and projection diagnostics")
    p_show.add_argument("event_id")
    p_show.add_argument("--format", choices=["text", "json"], default="json")

    p_replay = sub.add_parser("replay", help="replay trade_events into position_lots projection")
    p_replay.add_argument("--runtime-root", default=None, help="runtime root for active ledger store")
    p_replay.add_argument("--format", choices=["text", "json"], default="text")
    _add_write_flags(p_replay, high_risk=False)

    p_void = sub.add_parser("void", help="append a void event for an existing trade event")
    p_void.add_argument("--runtime-root", default=None, help="runtime root for active ledger store")
    p_void.add_argument("event_id")
    p_void.add_argument("--reason", default="manual_void")
    p_void.add_argument("--format", choices=["text", "json"], default="text")
    _add_write_flags(p_void, high_risk=True)

    p_repair = sub.add_parser("repair", help="void an event and append a corrected replacement event")
    p_repair.add_argument("--runtime-root", default=None, help="runtime root for active ledger store")
    p_repair.add_argument("event_id")
    p_repair.add_argument("--reason", default="manual_repair")
    p_repair.add_argument("--broker", default=None)
    p_repair.add_argument("--account", default=None)
    p_repair.add_argument("--symbol", default=None)
    p_repair.add_argument("--option-type", default=None, choices=["put", "call"])
    p_repair.add_argument("--side", default=None, choices=["buy", "sell"])
    p_repair.add_argument("--effect", default=None, choices=["open", "close"])
    p_repair.add_argument("--contracts", type=int, default=None)
    p_repair.add_argument("--price", type=float, default=None)
    p_repair.add_argument("--strike", type=float, default=None)
    p_repair.add_argument("--multiplier", type=int, default=None)
    p_repair.add_argument("--exp", default=None, help="YYYY-MM-DD")
    p_repair.add_argument("--currency", default=None, choices=["USD", "HKD", "CNY"])
    p_repair.add_argument("--trade-time-ms", type=int, default=None)
    p_repair.add_argument("--order-id", default=None)
    p_repair.add_argument("--record-id", default=None, help="explicit close target record_id for repaired close events")
    p_repair.add_argument("--close-target-source-event-id", default=None)
    p_repair.add_argument("--format", choices=["text", "json"], default="text")
    _add_write_flags(p_repair, high_risk=True)

    args = parser.parse_args(argv)
    write_controls: dict[str, dict[str, bool]] = {}
    if args.cmd == "replay":
        write_controls[args.cmd] = _resolve_write_control(args, command_name="trade-events replay", high_risk=False)
    elif args.cmd in {"void", "repair"}:
        write_controls[args.cmd] = _resolve_write_control(args, command_name=f"trade-events {args.cmd}", high_risk=True)
    base = Path(__file__).resolve().parents[3]
    data_config_path = resolve_position_data_config_path(base=base, data_config=args.data_config)
    if bool(write_controls.get(args.cmd, {}).get("write_requested", False)):
        guard = _guard_write(
            data_config=data_config_path,
            args=args,
            as_json=(str(getattr(args, "format", "") or "") == "json"),
        )
        if guard is None:
            return 2
    _data_config, repo = resolve_option_positions_repo(base=base, cfg=None, data_config=args.data_config, runtime_root=_runtime_root_arg(args))
    ledger_store = ledger_store_payload(_data_config, repo)

    if args.cmd == "list":
        rows = list_trade_event_reviews(
            repo,
            status=args.status,
            broker=args.broker,
            account=args.account,
            limit=int(args.limit),
        )
        if args.format == "json":
            _print_json(rows)
            return 0
        if not rows:
            print("(no trade events)")
            return 0
        print("# trade_events")
        for row in rows:
            diag = row.get("diagnostics") or []
            diag_text = f" diagnostics={len(diag)}" if diag else ""
            print(
                f"- {row.get('event_id')} | {row.get('status')} | {row.get('account')} | {row.get('symbol')} | "
                f"{row.get('side')} {row.get('option_type')} {row.get('position_effect')} | "
                f"contracts {row.get('contracts')} | time {row.get('trade_time_beijing') or '-'} | "
                f"source {row.get('source_type')}:{row.get('source_name')}{diag_text}"
            )
        return 0

    if args.cmd == "show":
        try:
            payload = show_trade_event_review(repo, event_id=args.event_id)
        except ValueError as exc:
            print(str(exc))
            return 2
        payload["ledger_store"] = ledger_store
        if args.format == "json":
            _print_json(payload)
            return 0
        event = payload["event"]
        trade_time = format_trade_time_beijing(event.get("trade_time_ms")) or "-"
        print(
            f"{event.get('event_id')} | {payload.get('status')} | {event.get('account')} | {event.get('symbol')} | "
            f"{event.get('side')} {event.get('option_type')} {event.get('position_effect')} | time {trade_time}"
        )
        return 0

    if args.cmd == "replay":
        should_apply = bool(write_controls["replay"]["write_requested"])
        payload = replay_trade_events(repo, apply=should_apply)
        payload["ledger_store"] = ledger_store
        payload = attach_write_contract(
            payload,
            dry_run=not should_apply,
            write_applied=should_apply,
            rollback_hint="rerun trade-events replay from canonical trade_events",
        )
        if args.format == "json":
            _print_json(payload)
            return 0
        print(
            f"[{'DONE' if should_apply else 'DRY_RUN'}] replay trade_events={payload.get('trade_event_count')} "
            f"position_lots={payload.get('position_lot_count')} diagnostics={payload.get('projection_diagnostic_count')}"
        )
        return 0

    if args.cmd == "void":
        should_apply = bool(write_controls["void"]["write_requested"])
        try:
            payload = (
                apply_void_trade_event(repo, event_id=args.event_id, reason=args.reason)
                if should_apply
                else preview_void_trade_event(repo, event_id=args.event_id, reason=args.reason)
            )
        except ValueError as exc:
            print(str(exc))
            return 2
        payload["ledger_store"] = ledger_store
        payload = attach_write_contract(
            payload,
            dry_run=not should_apply,
            write_applied=should_apply,
            rollback_hint="void appends an immutable correction; restore from backup if this was accidental",
        )
        if args.format == "json":
            _print_json(payload)
            return 0
        if should_apply:
            print(f"[DONE] voided event_id={args.event_id} via={payload.get('event_id')}")
        else:
            print(f"[DRY_RUN] would void event_id={args.event_id} reason={args.reason}")
        return 0

    if args.cmd == "repair":
        should_apply = bool(write_controls["repair"]["write_requested"])
        overrides = _repair_overrides(args)
        try:
            payload = (
                apply_repair_trade_event(repo, event_id=args.event_id, overrides=overrides, reason=args.reason)
                if should_apply
                else preview_repair_trade_event(repo, event_id=args.event_id, overrides=overrides, reason=args.reason)
            )
        except ValueError as exc:
            print(str(exc))
            return 2
        payload["ledger_store"] = ledger_store
        payload = attach_write_contract(
            payload,
            dry_run=not should_apply,
            write_applied=should_apply,
            rollback_hint="void repair events or restore option_positions SQLite from backup",
        )
        if args.format == "json":
            _print_json(payload)
            return 0
        if should_apply:
            print(
                f"[DONE] repaired event_id={args.event_id} "
                f"void={payload.get('void_event_id')} repair={payload.get('repair_event_id')} "
                f"position_lots={payload.get('position_lot_count')}"
            )
        else:
            repair_event = payload.get("repair_event") or {}
            print(
                f"[DRY_RUN] would repair event_id={args.event_id} "
                f"repair={repair_event.get('event_id')}"
            )
        return 0

    raise SystemExit("unknown trade-events command")


if __name__ == "__main__":
    raise SystemExit(main())
