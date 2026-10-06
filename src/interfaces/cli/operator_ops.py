from __future__ import annotations

import argparse
from typing import Any, Callable

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

    close_advice = subparsers.add_parser(
        "close-advice",
        help="configure the scheduled Close Advice feature",
    )
    from src.interfaces.cli.feature_ops import add_feature_configure_parser

    add_feature_configure_parser(
        close_advice.add_subparsers(
            dest="close_advice_command",
            required=True,
        ),
        "close-advice",
    )

    notify = subparsers.add_parser("notify", help="notification helpers")
    notify_sub = notify.add_subparsers(dest="notify_command", required=True)
    preview = notify_sub.add_parser("preview", help="preview a persisted Daily Decision Brief")
    preview.add_argument("--account", default=None)
    preview.add_argument("--market", default=None, choices=("US", "HK", "us", "hk"))
    preview.add_argument("--date", default=None)
    preview.add_argument("--revision", default=None, type=int)


def handle_operator_command(
    args: argparse.Namespace,
    *,
    run_scan_fn: Callable[..., dict[str, Any]] = run_scan,
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
        if getattr(args, "close_advice_command", None) == "configure":
            from src.interfaces.cli.feature_ops import run_feature_configure

            return run_feature_configure(args)
        raise AgentToolError(
            code="INPUT_ERROR",
            message="close-advice only supports scheduled feature configuration",
        )

    if args.command == "notify" and args.notify_command == "preview":
        return preview_notification_fn(
            account=args.account,
            market=args.market,
            date=args.date,
            revision=args.revision,
        )

    raise AgentToolError(code="INPUT_ERROR", message=f"unsupported operator command: {args.command}")
