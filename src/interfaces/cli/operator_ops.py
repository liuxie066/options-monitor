from __future__ import annotations

import argparse
from typing import Any, Callable

from src.application.close_advice_pipeline import run_close_advice
from src.application.agent_tool_contracts import AgentToolError
from src.application.notification_pipeline import preview_notification
from src.application.scan_pipeline import run_scan


def add_operator_commands(subparsers: Any) -> None:
    scan = subparsers.add_parser("scan", help="run opportunity scan")
    scan.add_argument("--config-key", default=None, choices=("us", "hk"))
    scan.add_argument("--config-path", default=None)
    scan.add_argument("--symbols", default=None, help="comma-separated symbols")
    scan.add_argument("--top-n", type=int, default=None)
    scan.add_argument("--no-context", action="store_true")

    close_advice = subparsers.add_parser("close-advice", help="run close advice flow")
    close_advice.add_argument("--config-key", default=None, choices=("us", "hk"))
    close_advice.add_argument("--config-path", default=None)
    close_advice.add_argument("--account", default=None)
    close_advice.add_argument("--output-dir", default=None)

    notify = subparsers.add_parser("notify", help="notification helpers")
    notify_sub = notify.add_subparsers(dest="notify_command", required=True)
    preview = notify_sub.add_parser("preview", help="preview notification content")
    preview.add_argument("--alerts-path", default=None)
    preview.add_argument("--changes-path", default=None)
    preview.add_argument("--alerts-text", default=None)
    preview.add_argument("--changes-text", default=None)
    preview.add_argument("--account-label", default=None)


def handle_operator_command(
    args: argparse.Namespace,
    *,
    run_scan_fn: Callable[..., dict[str, Any]] = run_scan,
    run_close_advice_fn: Callable[..., dict[str, Any]] = run_close_advice,
    preview_notification_fn: Callable[..., dict[str, Any]] = preview_notification,
) -> dict[str, Any]:
    if args.command == "scan":
        symbols = [s.strip().upper() for s in str(args.symbols or "").split(",") if s.strip()] or None
        return run_scan_fn(
            config_key=args.config_key,
            config_path=args.config_path,
            symbols=symbols,
            top_n=args.top_n,
            no_context=bool(args.no_context),
        )

    if args.command == "close-advice":
        return run_close_advice_fn(
            config_key=args.config_key,
            config_path=args.config_path,
            account=args.account,
            output_dir=args.output_dir,
        )

    if args.command == "notify" and args.notify_command == "preview":
        return preview_notification_fn(
            alerts_path=args.alerts_path,
            changes_path=args.changes_path,
            alerts_text=args.alerts_text,
            changes_text=args.changes_text,
            account_label=args.account_label,
        )

    raise AgentToolError(code="INPUT_ERROR", message=f"unsupported operator command: {args.command}")
