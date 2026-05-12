from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from src.application.option_positions_facade import resolve_option_positions_repo
from src.application.trade_event_review import (
    apply_repair_trade_event,
    apply_void_trade_event,
    list_trade_event_reviews,
    preview_repair_trade_event,
    preview_void_trade_event,
    replay_trade_events,
    show_trade_event_review,
)


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Review, repair, replay, and void trade events")
    parser.add_argument("--data-config", default=None, help="portfolio data config path; auto-resolves when omitted")
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
    p_replay.add_argument("--apply", action="store_true", help="persist the replayed position_lots projection")
    p_replay.add_argument("--dry-run", action="store_true", help="preview replay result without writing")
    p_replay.add_argument("--format", choices=["text", "json"], default="text")

    p_void = sub.add_parser("void", help="append a void event for an existing trade event")
    p_void.add_argument("event_id")
    p_void.add_argument("--reason", default="manual_void")
    p_void.add_argument("--apply", action="store_true")
    p_void.add_argument("--dry-run", action="store_true")
    p_void.add_argument("--format", choices=["text", "json"], default="text")

    p_repair = sub.add_parser("repair", help="void an event and append a corrected replacement event")
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
    p_repair.add_argument("--apply", action="store_true")
    p_repair.add_argument("--dry-run", action="store_true")
    p_repair.add_argument("--format", choices=["text", "json"], default="text")

    args = parser.parse_args(argv)
    if args.cmd in {"replay", "void", "repair"} and bool(getattr(args, "apply", False)) and bool(getattr(args, "dry_run", False)):
        raise SystemExit("--apply and --dry-run are mutually exclusive")
    base = Path(__file__).resolve().parents[3]
    _data_config, repo = resolve_option_positions_repo(base=base, data_config=args.data_config)

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
                f"contracts {row.get('contracts')} | source {row.get('source_type')}:{row.get('source_name')}{diag_text}"
            )
        return 0

    if args.cmd == "show":
        try:
            payload = show_trade_event_review(repo, event_id=args.event_id)
        except ValueError as exc:
            print(str(exc))
            return 2
        if args.format == "json":
            _print_json(payload)
            return 0
        event = payload["event"]
        print(
            f"{event.get('event_id')} | {payload.get('status')} | {event.get('account')} | {event.get('symbol')} | "
            f"{event.get('side')} {event.get('option_type')} {event.get('position_effect')}"
        )
        return 0

    if args.cmd == "replay":
        payload = replay_trade_events(repo, apply=bool(args.apply))
        if args.format == "json":
            _print_json(payload)
            return 0
        print(
            f"[{'DONE' if args.apply else 'DRY_RUN'}] replay trade_events={payload.get('trade_event_count')} "
            f"position_lots={payload.get('position_lot_count')} diagnostics={payload.get('projection_diagnostic_count')}"
        )
        return 0

    if args.cmd == "void":
        try:
            payload = (
                apply_void_trade_event(repo, event_id=args.event_id, reason=args.reason)
                if args.apply
                else preview_void_trade_event(repo, event_id=args.event_id, reason=args.reason)
            )
        except ValueError as exc:
            print(str(exc))
            return 2
        if args.format == "json":
            _print_json(payload)
            return 0
        if args.apply:
            print(f"[DONE] voided event_id={args.event_id} via={payload.get('event_id')}")
        else:
            print(f"[DRY_RUN] would void event_id={args.event_id} reason={args.reason}")
        return 0

    if args.cmd == "repair":
        overrides = _repair_overrides(args)
        try:
            payload = (
                apply_repair_trade_event(repo, event_id=args.event_id, overrides=overrides, reason=args.reason)
                if args.apply
                else preview_repair_trade_event(repo, event_id=args.event_id, overrides=overrides, reason=args.reason)
            )
        except ValueError as exc:
            print(str(exc))
            return 2
        if args.format == "json":
            _print_json(payload)
            return 0
        if args.apply:
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
