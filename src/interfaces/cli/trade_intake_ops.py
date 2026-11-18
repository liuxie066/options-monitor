"""Trade-intake CLI adaptation without loading other long-lived workflows."""
from __future__ import annotations

import argparse
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from typing import Any

from src.application.agent_tool_contracts import AgentToolError, build_error_payload, build_response
from src.application.runtime_paths import repo_base
from src.interfaces.cli.command_environment import command_environment


def _run_trade_intake_cli(argv: list[str]) -> int | None:
    parser = argparse.ArgumentParser(prog="om")
    run = parser.add_subparsers(dest="command", required=True).add_parser("run")
    add_trade_intake_command(run.add_subparsers(dest="run_command", required=True))
    try:
        with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            args = parser.parse_args(argv)
    except SystemExit:
        # The full CLI owns help (including abbreviations) and invalid usage,
        # preserving its environment/error ordering before argparse exits.
        return None
    try:
        with command_environment(argv, repo_root=repo_base(), discover_local=True):
            from src.application.trades.process_supervisor import run_trade_intake_process

            return int(run_trade_intake_process(_trade_intake_argv(args)))
    except AgentToolError as err:
        return _print_error(err)
    except ValueError as exc:
        return _print_error(AgentToolError(code="CONFIG_ERROR", message=str(exc)))


def _print_error(err: AgentToolError) -> int:
    print(json.dumps(build_response(tool_name="om", ok=False, error=build_error_payload(err)),
                     ensure_ascii=False, indent=2))
    return 2


def add_trade_intake_command(run_sub: Any) -> None:
    trade_intake = run_sub.add_parser("trade-intake", help="run OpenD trade intake listener")
    trade_intake.add_argument("action", nargs="?", default="listen", choices=["listen", "attribution-migrate"])
    trade_intake.add_argument("--effective-from-ms", type=int)
    trade_intake.add_argument("--actor")
    trade_intake.add_argument("--manifest")
    trade_intake.add_argument("--backup-path")
    trade_intake.add_argument("--writers-stopped", action="store_true")
    trade_intake.add_argument("--config", required=True)
    trade_intake.add_argument("--data-config", default=None)
    trade_intake.add_argument("--runtime-root", default=None)
    trade_intake.add_argument("--mode", choices=["dry-run", "apply"], default=None)
    trade_intake.add_argument("--confirm", action="store_true")
    trade_intake.add_argument("--yes", action="store_true", help="non-interactive confirmation; emits an audit_id")
    trade_intake.add_argument("--state-path", default=None)
    trade_intake.add_argument("--audit-path", default=None)
    trade_intake.add_argument("--status-path", default=None)
    trade_intake.add_argument("--host", default=None)
    trade_intake.add_argument("--port", type=int, default=None)
    trade_intake.add_argument("--once", action="store_true")
    trade_intake.add_argument("--deal-json", default=None)
    trade_intake.add_argument("--execution-file", default=None)
    trade_intake.add_argument("--inbox-id", default=None)
    trade_intake.add_argument("--recover-skipped", action="store_true")
    trade_intake.add_argument("--expected-recovery-hash", default=None)
    trade_intake.add_argument("--retry-failed", action="store_true")
    trade_intake.add_argument("--reconcile-state", action="store_true")
    trade_intake.add_argument("--compensate-receipts", action="store_true")
    trade_intake.add_argument(
        "--compensation-reason",
        default="legacy_false_outbox_marker",
    )
    trade_intake.add_argument("--expected-payload-hash", default=None)
    trade_intake.add_argument("--account", default=None)
    trade_intake.add_argument("--deal-id", action="append", default=None)
    trade_intake.add_argument("--apply", action="store_true")
    trade_intake.add_argument("--dry-run", action="store_true")


def _trade_intake_argv(args: argparse.Namespace) -> list[str]:
    intake_argv: list[str] = ["--config", str(args.config)]
    if getattr(args, "action", "listen") != "listen":
        intake_argv.append(args.action)
    for name in ("effective_from_ms", "actor", "manifest", "backup_path"):
        value = getattr(args, name, None)
        if value is not None:
            intake_argv.extend(["--" + name.replace("_", "-"), str(value)])
    if getattr(args, "writers_stopped", False):
        intake_argv.append("--writers-stopped")
    if args.data_config:
        intake_argv.extend(["--data-config", str(args.data_config)])
    if args.runtime_root:
        intake_argv.extend(["--runtime-root", str(args.runtime_root)])
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
    if args.execution_file:
        intake_argv.extend(["--execution-file", str(args.execution_file)])
    if args.inbox_id:
        intake_argv.extend(["--inbox-id", str(args.inbox_id)])
    if args.recover_skipped:
        intake_argv.append("--recover-skipped")
    if args.expected_recovery_hash:
        intake_argv.extend(["--expected-recovery-hash", str(args.expected_recovery_hash)])
    if args.retry_failed:
        intake_argv.append("--retry-failed")
    if args.reconcile_state:
        intake_argv.append("--reconcile-state")
    if args.compensate_receipts:
        intake_argv.append("--compensate-receipts")
        intake_argv.extend(
            ["--compensation-reason", str(args.compensation_reason)]
        )
    if args.account:
        intake_argv.extend(["--account", str(args.account)])
    for deal_id in args.deal_id or []:
        intake_argv.extend(["--deal-id", str(deal_id)])
    if args.expected_payload_hash:
        intake_argv.extend(
            ["--expected-payload-hash", str(args.expected_payload_hash)]
        )
    if args.apply:
        intake_argv.append("--apply")
    if args.dry_run:
        intake_argv.append("--dry-run")
    return intake_argv
