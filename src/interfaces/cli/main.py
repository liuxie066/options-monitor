from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from scripts.agent_plugin.config import load_runtime_config
from scripts.agent_plugin.contracts import AgentToolError, build_error_payload, build_response
from scripts.validate_config import validate_config
from src.application.account_management import add_account, edit_account, remove_account
from src.application.close_advice_pipeline import run_close_advice
from src.application.healthcheck import run_healthcheck
from src.application.multi_account_tick import run_tick
from src.application.notification_pipeline import preview_notification
from src.application.pipeline_runtime import main as run_scan_pipeline
from src.application.runtime_setup import init_runtime
from src.application.scan_pipeline import run_scan
from scripts.query_sell_put_cash import query_sell_put_cash
from scripts.scan_scheduler import run_scheduler


def _dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="options-monitor unified CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    health = sub.add_parser("healthcheck", help="run readiness checks")
    health.add_argument("--config-key", default=None, choices=("us", "hk"))
    health.add_argument("--config-path", default=None)
    health.add_argument("--accounts", nargs="*", default=None)

    scan = sub.add_parser("scan", help="run opportunity scan")
    scan.add_argument("--config-key", default=None, choices=("us", "hk"))
    scan.add_argument("--config-path", default=None)
    scan.add_argument("--symbols", default=None, help="comma-separated symbols")
    scan.add_argument("--top-n", type=int, default=None)
    scan.add_argument("--no-context", action="store_true")

    close_advice = sub.add_parser("close-advice", help="run close advice flow")
    close_advice.add_argument("--config-key", default=None, choices=("us", "hk"))
    close_advice.add_argument("--config-path", default=None)
    close_advice.add_argument("--account", default=None)
    close_advice.add_argument("--output-dir", default=None)

    notify = sub.add_parser("notify", help="notification helpers")
    notify_sub = notify.add_subparsers(dest="notify_command", required=True)
    preview = notify_sub.add_parser("preview", help="preview notification content")
    preview.add_argument("--alerts-path", default=None)
    preview.add_argument("--changes-path", default=None)
    preview.add_argument("--alerts-text", default=None)
    preview.add_argument("--changes-text", default=None)
    preview.add_argument("--account-label", default=None)

    accounts = sub.add_parser("accounts", help="manage runtime accounts")
    account_sub = accounts.add_subparsers(dest="accounts_command", required=True)
    add = account_sub.add_parser("add", help="add account")
    add.add_argument("--market", required=True, choices=("us", "hk"))
    add.add_argument("--account-label", required=True)
    add.add_argument("--account-type", required=True, choices=("futu", "external_holdings"))
    add.add_argument("--config-path", default=None)
    add.add_argument("--futu-acc-id", default=None)
    add.add_argument("--holdings-account", default=None)
    edit = account_sub.add_parser("edit", help="edit account")
    edit.add_argument("--market", required=True, choices=("us", "hk"))
    edit.add_argument("--account-label", required=True)
    edit.add_argument("--config-path", default=None)
    edit.add_argument("--account-type", default=None, choices=("futu", "external_holdings"))
    edit.add_argument("--futu-acc-id", default=None)
    edit.add_argument("--holdings-account", default=None)
    edit.add_argument("--clear-holdings-account", action="store_true")
    remove = account_sub.add_parser("remove", help="remove account")
    remove.add_argument("--market", required=True, choices=("us", "hk"))
    remove.add_argument("--account-label", required=True)
    remove.add_argument("--config-path", default=None)

    config = sub.add_parser("config", help="config operations")
    config_sub = config.add_subparsers(dest="config_command", required=True)
    validate = config_sub.add_parser("validate", help="validate runtime config")
    validate.add_argument("--config-key", default=None, choices=("us", "hk"))
    validate.add_argument("--config-path", default=None)

    scheduler = sub.add_parser("scheduler", help="scan scheduler / frequency controller")
    scheduler.add_argument("--config", required=True)
    scheduler.add_argument("--state-dir", default="output/state")
    scheduler.add_argument("--state", default=None)
    scheduler.add_argument("--schedule-key", default="schedule")
    scheduler.add_argument("--account", default=None)
    scheduler.add_argument("--run-if-due", action="store_true")
    scheduler.add_argument("--mark-notified", action="store_true")
    scheduler.add_argument("--mark-scanned", action="store_true")
    scheduler.add_argument("--jsonl", action="store_true")
    scheduler.add_argument("--force", action="store_true")

    sell_put_cash = sub.add_parser("sell-put-cash", help="query cash headroom for sell-put")
    sell_put_cash.add_argument("--config", default=None)
    sell_put_cash.add_argument("--data-config", default=None)
    sell_put_cash.add_argument("--market", default="富途")
    sell_put_cash.add_argument("--account", default=None)
    sell_put_cash.add_argument("--format", choices=("text", "json"), default="text")
    sell_put_cash.add_argument("--top", type=int, default=10)
    sell_put_cash.add_argument("--no-exchange-rates", action="store_true")
    sell_put_cash.add_argument("--out-dir", default="output/state")

    init_cmd = sub.add_parser("init", help="initialize runtime config")
    init_sub = init_cmd.add_subparsers(dest="init_command", required=True)
    runtime = init_sub.add_parser("runtime", help="generate runtime config")
    runtime.add_argument("--market", required=True, choices=("us", "hk"))
    runtime.add_argument("--futu-acc-id", required=True)
    runtime.add_argument("--account-label", default="user1")
    runtime.add_argument("--config-path", default=None)
    runtime.add_argument("--data-config-path", default=None)
    runtime.add_argument("--symbol", action="append", dest="symbols", default=None)
    runtime.add_argument("--holdings-account", default=None)
    runtime.add_argument("--opend-host", default="127.0.0.1")
    runtime.add_argument("--opend-port", type=int, default=11111)
    runtime.add_argument("--force", action="store_true")

    run = sub.add_parser("run", help="run long-lived workflows")
    run_sub = run.add_subparsers(dest="run_command", required=True)
    tick = run_sub.add_parser("tick", help="multi-account tick orchestration")
    tick.add_argument("--config", default="config.us.json")
    tick.add_argument("--accounts", nargs="+", default=None)
    tick.add_argument("--default-account", default=None)
    tick.add_argument("--market-config", default="auto", choices=["auto", "hk", "us", "all"])
    tick.add_argument("--no-send", action="store_true")
    tick.add_argument("--smoke", action="store_true")
    tick.add_argument("--force", action="store_true")
    tick.add_argument("--debug", action="store_true")
    tick.add_argument("--opend-phone-verify-continue", action="store_true")

    return parser.parse_args(argv)


def _print(payload: dict[str, Any]) -> int:
    sys.stdout.write(_dumps(payload))
    return 0 if payload.get("ok", True) else 2


def _validate_runtime_config(*, config_key: str | None = None, config_path: str | None = None) -> dict[str, Any]:
    path, cfg = load_runtime_config(config_key=config_key, config_path=config_path)
    validate_config(dict(cfg))
    return {
        "ok": True,
        "config_path": str(path),
        "config_key": str(config_key or "").strip().lower() or None,
    }


def main(argv: list[str] | None = None) -> int:
    if argv and argv[0] == "scan-pipeline":
        return int(run_scan_pipeline(argv[1:]))

    args = parse_args(argv)
    try:
        if args.command == "healthcheck":
            return _print(run_healthcheck(config_key=args.config_key, config_path=args.config_path, accounts=args.accounts))

        if args.command == "scan":
            symbols = [s.strip().upper() for s in str(args.symbols or "").split(",") if s.strip()] or None
            return _print(run_scan(config_key=args.config_key, config_path=args.config_path, symbols=symbols, top_n=args.top_n, no_context=bool(args.no_context)))

        if args.command == "close-advice":
            return _print(run_close_advice(config_key=args.config_key, config_path=args.config_path, account=args.account, output_dir=args.output_dir))

        if args.command == "notify" and args.notify_command == "preview":
            return _print(preview_notification(
                alerts_path=args.alerts_path,
                changes_path=args.changes_path,
                alerts_text=args.alerts_text,
                changes_text=args.changes_text,
                account_label=args.account_label,
            ))

        if args.command == "accounts" and args.accounts_command == "add":
            return _print(build_response(tool_name="accounts.add", ok=True, data=add_account(
                market=args.market,
                account_label=args.account_label,
                account_type=args.account_type,
                config_path=args.config_path,
                futu_acc_id=args.futu_acc_id,
                holdings_account=args.holdings_account,
            )))

        if args.command == "accounts" and args.accounts_command == "edit":
            return _print(build_response(tool_name="accounts.edit", ok=True, data=edit_account(
                market=args.market,
                account_label=args.account_label,
                config_path=args.config_path,
                account_type=args.account_type,
                futu_acc_id=args.futu_acc_id,
                holdings_account=args.holdings_account,
                clear_holdings_account=bool(args.clear_holdings_account),
            )))

        if args.command == "accounts" and args.accounts_command == "remove":
            return _print(build_response(tool_name="accounts.remove", ok=True, data=remove_account(
                market=args.market,
                account_label=args.account_label,
                config_path=args.config_path,
            )))

        if args.command == "config" and args.config_command == "validate":
            return _print(_validate_runtime_config(config_key=args.config_key, config_path=args.config_path))

        if args.command == "scheduler":
            run_scheduler(
                config=args.config,
                state_dir=args.state_dir,
                state=args.state,
                schedule_key=args.schedule_key,
                account=args.account,
                run_if_due=bool(args.run_if_due),
                mark_notified=bool(args.mark_notified),
                mark_scanned=bool(args.mark_scanned),
                jsonl=bool(args.jsonl),
                force=bool(args.force),
            )
            return 0

        if args.command == "sell-put-cash":
            query_sell_put_cash(
                config=args.config,
                data_config=args.data_config,
                market=args.market,
                account=args.account,
                output_format=args.format,
                top=args.top,
                no_exchange_rates=bool(args.no_exchange_rates),
                out_dir=args.out_dir,
            )
            return 0

        if args.command == "init" and args.init_command == "runtime":
            return _print(build_response(tool_name="init.runtime", ok=True, data=init_runtime(
                market=args.market,
                futu_acc_id=args.futu_acc_id,
                account_label=args.account_label,
                config_path=args.config_path,
                data_config_path=args.data_config_path,
                symbols=args.symbols,
                holdings_account=args.holdings_account,
                opend_host=args.opend_host,
                opend_port=args.opend_port,
                force=bool(args.force),
            )))

        if args.command == "run" and args.run_command == "tick":
            tick_argv: list[str] = ["--config", str(args.config)]
            if args.accounts:
                tick_argv.extend(["--accounts", *[str(x) for x in args.accounts]])
            if args.default_account:
                tick_argv.extend(["--default-account", str(args.default_account)])
            if args.market_config:
                tick_argv.extend(["--market-config", str(args.market_config)])
            if args.no_send:
                tick_argv.append("--no-send")
            if args.smoke:
                tick_argv.append("--smoke")
            if args.force:
                tick_argv.append("--force")
            if args.debug:
                tick_argv.append("--debug")
            if args.opend_phone_verify_continue:
                tick_argv.append("--opend-phone-verify-continue")
            return int(run_tick(tick_argv))
    except AgentToolError as err:
        return _print(build_response(tool_name="om", ok=False, error=build_error_payload(err)))

    raise SystemExit(f"unsupported command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
