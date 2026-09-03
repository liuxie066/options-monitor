from __future__ import annotations

import argparse
from datetime import date, datetime, time, timedelta, timezone
import json
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from src.application.ledger.api import (
    ledger_store_payload,
    open_position_ledger_from_runtime_config as resolve_option_positions_repo,
    resolve_position_data_config_path,
)
from src.application.account_config import resolve_account_broker_binding_sets
from src.application.agent_tool_config import load_runtime_config
from src.infrastructure.futu_history_deals import OpenDHistoryDealClient
from src.application.trades.order_fee_sync import sync_order_fees
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
from src.application.write_contract import attach_write_contract
from src.interfaces.cli.ledger_write_safety import add_write_flags as _add_write_flags
from src.interfaces.cli.ledger_write_safety import guard_ledger_write as _guard_write
from src.interfaces.cli.ledger_write_safety import resolve_cli_write_control as _resolve_write_control
from src.interfaces.cli.ledger_write_safety import runtime_root_arg as _runtime_root_arg


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
        "futu_account_id": args.futu_account_id,
        "order_id": args.order_id,
        "record_id": args.record_id,
        "close_target_source_event_id": args.close_target_source_event_id,
    }


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

    p_repair = sub.add_parser(
        "repair",
        help="repair an event; verified Futu identity/time metadata is corrected in place",
    )
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
    p_repair.add_argument("--futu-account-id", default=None)
    p_repair.add_argument("--order-id", default=None)
    p_repair.add_argument("--record-id", default=None, help="explicit close target record_id for repaired close events")
    p_repair.add_argument("--close-target-source-event-id", default=None)
    p_repair.add_argument("--format", choices=["text", "json"], default="text")
    _add_write_flags(p_repair, high_risk=True)

    p_fees = sub.add_parser(
        "fees-sync",
        help="preview or apply OpenD actual order-fee enrichment",
    )
    p_fees.add_argument("--config-key", required=True, choices=["us", "hk"])
    p_fees.add_argument("--config", dest="config_path", default=None)
    p_fees.add_argument("--account", required=True)
    p_fees.add_argument("--start-date", required=True, help="YYYY-MM-DD")
    p_fees.add_argument("--end-date", required=True, help="YYYY-MM-DD")
    p_fees.add_argument("--format", choices=["text", "json"], default="json")
    _add_write_flags(p_fees, high_risk=True)
    p_fees.add_argument(
        "--confirm-write",
        dest="confirm",
        action="store_true",
        help=argparse.SUPPRESS,
    )

    args = parser.parse_args(argv)
    write_controls: dict[str, dict[str, bool]] = {}
    if args.cmd == "replay":
        write_controls[args.cmd] = _resolve_write_control(args, command_name="trade-events replay", high_risk=False)
    elif args.cmd in {"void", "repair", "fees-sync"}:
        write_controls[args.cmd] = _resolve_write_control(args, command_name=f"trade-events {args.cmd}", high_risk=True)
    base = Path(__file__).resolve().parents[3]
    if args.cmd == "fees-sync":
        return _run_fee_sync(
            args,
            base=base,
            write_requested=bool(write_controls["fees-sync"]["write_requested"]),
        )
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
        identity_binding = payload.get("operation") == "futu_order_identity_binding"
        time_correction = payload.get("operation") == "opend_trade_time_correction"
        in_place_repair = identity_binding or time_correction
        write_applied = bool(payload.get("mode") == "applied") if in_place_repair else should_apply
        payload["ledger_store"] = ledger_store
        payload = attach_write_contract(
            payload,
            dry_run=not should_apply,
            write_applied=write_applied,
            rollback_hint=(
                "in-place repair creates no backup or void event; restore a separately verified pre-write SQLite backup"
                if in_place_repair
                else "void repair events or restore option_positions SQLite from backup"
            ),
        )
        if args.format == "json":
            _print_json(payload)
            return 0
        if identity_binding:
            state = str(payload.get("mode") or "").upper()
            verb = "would bind" if payload.get("mode") == "dry_run" else "already bound" if payload.get("mode") == "no_op" else "bound"
            print(
                f"[{state}] {verb} Futu order identity event_id={args.event_id} "
                f"futu_account_id={payload.get('futu_account_id')} order_id={payload.get('order_id')}"
            )
            return 0
        if time_correction:
            state = str(payload.get("mode") or "").upper()
            verb = (
                "would correct"
                if payload.get("mode") == "dry_run"
                else "already corrected"
                if payload.get("mode") == "no_op"
                else "corrected"
            )
            print(
                f"[{state}] {verb} OpenD trade time event_id={args.event_id} "
                f"from={payload.get('before_trade_time_ms')} to={payload.get('after_trade_time_ms')}"
            )
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


def _run_fee_sync(
    args: argparse.Namespace,
    *,
    base: Path,
    write_requested: bool,
) -> int:
    config_path, cfg = load_runtime_config(
        config_key=args.config_key,
        config_path=args.config_path,
    )
    data_config_path = resolve_position_data_config_path(
        base=base,
        cfg=cfg,
        data_config=args.data_config,
        config_path=config_path,
    )
    if write_requested:
        guard = _guard_write(
            data_config=data_config_path,
            args=args,
            as_json=args.format == "json",
        )
        if guard is None:
            return 2
    _data_config, repo = resolve_option_positions_repo(
        base=base,
        cfg=cfg,
        data_config=data_config_path,
        config_path=config_path,
        runtime_root=_runtime_root_arg(args),
    )
    account = str(args.account or "").strip().lower()
    bindings = resolve_account_broker_binding_sets([(args.config_key, cfg)])
    binding = bindings.get(account)
    if binding is None or not binding.ok or binding.host is None or binding.port is None:
        raise SystemExit(f"fee sync broker binding is unavailable for account={account}")
    if str(binding.trd_env or "").upper() != "REAL":
        raise SystemExit("fee sync requires a REAL broker account binding")
    try:
        start_date = date.fromisoformat(args.start_date)
        end_date = date.fromisoformat(args.end_date)
    except ValueError as exc:
        raise SystemExit("start-date and end-date must be YYYY-MM-DD") from exc
    if start_date > end_date:
        raise SystemExit("start-date must be on or before end-date")
    reporting_tz = ZoneInfo("Asia/Shanghai")
    now_local = datetime.now(reporting_tz)
    if end_date > now_local.date():
        raise SystemExit("end-date cannot be in the future")
    start_ms = int(datetime.combine(start_date, time.min, tzinfo=reporting_tz).timestamp() * 1000)
    end_exclusive_ms = (
        int(now_local.timestamp() * 1000) + 1
        if end_date == now_local.date()
        else int(
            datetime.combine(
                end_date + timedelta(days=1),
                time.min,
                tzinfo=reporting_tz,
            ).timestamp()
            * 1000
        )
    )
    client = OpenDHistoryDealClient(host=binding.host, port=binding.port)
    try:
        receipt = sync_order_fees(
            repo,
            account=account,
            start_ms=start_ms,
            end_exclusive_ms=end_exclusive_ms,
            provider=client,
            apply=write_requested,
            observed_at_ms=int(datetime.now(timezone.utc).timestamp() * 1000),
            allowed_futu_account_ids=binding.required_account_ids,
        )
    finally:
        client.close()
    receipt["period"] = {
        "kind": "range",
        "reporting_timezone": "Asia/Shanghai",
        "requested_start_date": start_date.isoformat(),
        "requested_end_date": end_date.isoformat(),
        "effective_start_at_ms": start_ms,
        "effective_end_exclusive_at_ms": end_exclusive_ms,
        "valuation_open_at_ms": start_ms - 1,
        "valuation_end_at_ms": end_exclusive_ms - 1,
        "status": "partial_current" if end_date == now_local.date() else "complete_past",
    }
    receipt["ledger_store"] = ledger_store_payload(_data_config, repo)
    receipt = attach_write_contract(
        receipt,
        dry_run=not write_requested,
        write_applied=write_requested,
        rollback_hint="restore the ledger backup or apply a separately reviewed correction",
    )
    if args.format == "json":
        _print_json(receipt)
    else:
        mode = "DONE" if write_requested else "DRY_RUN"
        print(
            f"[{mode}] selected={receipt.get('selected_order_count')} "
            f"actual={receipt.get('actual_observation_count')} "
            f"reasons={receipt.get('reason_counts')}"
        )
    return 0 if not receipt.get("migration", {}).get("status_counts", {}).get("rolled_back") else 2


if __name__ == "__main__":
    raise SystemExit(main())
