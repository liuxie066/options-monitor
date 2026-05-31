from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Callable

from src.application.agent_tool_contracts import AgentToolError, build_response
from src.application.multi_account_tick import run_tick
from src.application.tick_cron import run_tick_cron


def _dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def _print(payload: dict[str, Any]) -> int:
    sys.stdout.write(_dumps(payload))
    return 0 if payload.get("ok", True) else 2


def add_run_commands(subparsers: Any) -> None:
    run = subparsers.add_parser("run", help="run long-lived workflows")
    run_sub = run.add_subparsers(dest="run_command", required=True)
    tick = run_sub.add_parser("tick", help="multi-account tick orchestration")
    tick.add_argument("--config", required=True)
    tick.add_argument("--accounts", nargs="+", default=None)
    tick.add_argument("--default-account", default=None)
    tick.add_argument("--market-config", default="auto", choices=["auto", "hk", "us", "all"])
    tick.add_argument("--no-send", action="store_true")
    tick.add_argument("--smoke", action="store_true")
    tick.add_argument("--force", action="store_true")
    tick.add_argument("--debug", action="store_true")
    tick.add_argument("--opend-phone-verify-continue", action="store_true")
    tick.add_argument("--allow-stale-config", action="store_true")
    tick_cron = run_sub.add_parser("tick-cron", help="cron-safe tick wrapper with lock, timeout, and trigger diagnostics")
    tick_cron.add_argument("--market", required=True, choices=("us", "hk"))
    tick_cron.add_argument("--accounts", nargs="+", default=None)
    tick_cron.add_argument("--timeout", dest="timeout_seconds", type=int, default=600)
    tick_cron.add_argument("--config", default=None)
    tick_cron.add_argument("--lock-path", default=None)
    tick_cron.add_argument("--trigger-job-id", default=None)
    tick_cron.add_argument("--trigger-job-name", default=None)
    tick_cron.add_argument("--trigger-schedule", default=None)
    tick_cron.add_argument("--dry-run-command", action="store_true")
    tick_cron.add_argument("--no-send", action="store_true")
    tick_cron.add_argument("--force", action="store_true")
    tick_cron.add_argument("--debug", action="store_true")
    tick_cron.add_argument("--allow-stale-config", action="store_true")
    trade_intake = run_sub.add_parser("trade-intake", help="run OpenD trade intake listener")
    trade_intake.add_argument("--config", required=True)
    trade_intake.add_argument("--data-config", default=None)
    trade_intake.add_argument("--mode", choices=["dry-run", "apply"], default=None)
    trade_intake.add_argument("--confirm", action="store_true")
    trade_intake.add_argument("--yes", action="store_true", help="non-interactive confirmation; emits an audit_id")
    trade_intake.add_argument("--state-path", default=None)
    trade_intake.add_argument("--audit-path", default=None)
    trade_intake.add_argument("--status-path", default=None)
    trade_intake.add_argument("--host", default="127.0.0.1")
    trade_intake.add_argument("--port", type=int, default=11111)
    trade_intake.add_argument("--once", action="store_true")
    trade_intake.add_argument("--deal-json", default=None)
    trade_intake.add_argument("--retry-failed", action="store_true")
    trade_intake.add_argument("--reconcile-state", action="store_true")
    trade_intake.add_argument("--deal-id", action="append", default=None)
    trade_intake.add_argument("--apply", action="store_true")
    trade_intake.add_argument("--dry-run", action="store_true")


def _tick_argv(args: argparse.Namespace) -> list[str]:
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
    if args.allow_stale_config:
        tick_argv.append("--allow-stale-config")
    return tick_argv


def _trade_intake_argv(args: argparse.Namespace) -> list[str]:
    intake_argv: list[str] = ["--config", str(args.config)]
    if args.data_config:
        intake_argv.extend(["--data-config", str(args.data_config)])
    if args.mode:
        intake_argv.extend(["--mode", str(args.mode)])
    if args.confirm:
        intake_argv.append("--confirm")
    if args.yes:
        intake_argv.append("--yes")
    if args.state_path:
        intake_argv.extend(["--state-path", str(args.state_path)])
    if args.audit_path:
        intake_argv.extend(["--audit-path", str(args.audit_path)])
    if args.status_path:
        intake_argv.extend(["--status-path", str(args.status_path)])
    if args.host:
        intake_argv.extend(["--host", str(args.host)])
    if args.port:
        intake_argv.extend(["--port", str(args.port)])
    if args.once:
        intake_argv.append("--once")
    if args.deal_json:
        intake_argv.extend(["--deal-json", str(args.deal_json)])
    if args.retry_failed:
        intake_argv.append("--retry-failed")
    if args.reconcile_state:
        intake_argv.append("--reconcile-state")
    for deal_id in args.deal_id or []:
        intake_argv.extend(["--deal-id", str(deal_id)])
    if args.apply:
        intake_argv.append("--apply")
    if args.dry_run:
        intake_argv.append("--dry-run")
    return intake_argv


def handle_run_command(
    args: argparse.Namespace,
    *,
    run_tick_fn: Callable[[list[str]], int] = run_tick,
    run_tick_cron_fn: Callable[..., int | dict[str, Any]] = run_tick_cron,
    run_trade_intake_fn: Callable[[list[str]], int] | None = None,
) -> int:
    if args.run_command == "tick":
        return int(run_tick_fn(_tick_argv(args)))

    if args.run_command == "tick-cron":
        out = run_tick_cron_fn(
            market=args.market,
            accounts=args.accounts,
            timeout_seconds=args.timeout_seconds,
            config_path=args.config,
            lock_path=args.lock_path,
            trigger_job_id=args.trigger_job_id,
            trigger_job_name=args.trigger_job_name,
            trigger_schedule=args.trigger_schedule,
            dry_run_command=bool(args.dry_run_command),
            no_send=bool(args.no_send),
            force=bool(args.force),
            debug=bool(args.debug),
            allow_stale_config=bool(args.allow_stale_config),
        )
        if isinstance(out, dict):
            return _print(build_response(tool_name="run.tick-cron", ok=True, data=out))
        return int(out)

    if args.run_command == "trade-intake":
        if run_trade_intake_fn is None:
            from src.application.trades.auto_intake import main as run_trade_intake_fn

        return int(run_trade_intake_fn(_trade_intake_argv(args)))

    raise AgentToolError(code="INPUT_ERROR", message=f"unsupported run command: {args.run_command}")
