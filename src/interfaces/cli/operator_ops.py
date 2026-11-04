from __future__ import annotations

import argparse
from typing import Any, Callable

from src.application.agent_tool_contracts import AgentToolError
from src.application.notification_pipeline import preview_notification
from src.application.scan_pipeline import run_scan
from src.application.symbol_mutations import split_symbol_whitelist


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

    advice = subparsers.add_parser("position-advice", help="持仓评估与建议")
    advice_sub = advice.add_subparsers(dest="position_advice_command", required=True)
    assess = advice_sub.add_parser("evaluate", help="评估账本选择或临时持仓，不写账本、不通知")
    assess.add_argument("--config-key", choices=("us", "hk"))
    assess.add_argument("--config-path")
    assess.add_argument("--request-file", required=True, help="JSON: account plus symbols/lot_ids/contracts, or positions")
    assess.add_argument("--format", choices=("text", "json"), default="text", help="人工展示（默认）或原业务 JSON；其他命令保留各自默认格式")
    roll = advice_sub.add_parser("roll-down", help="查询已有 short put 向下移仓候选，不下单、不写账本、不通知")
    roll.add_argument("--config-key", choices=("us", "hk"))
    roll.add_argument("--request-file", required=True, help="JSON: 必填 account、lot_id、max_new_expiration（无默认）；可选 min_new_strike > 0、limit 1..50（默认 10）")
    roll.add_argument("--format", choices=("text", "json"), default="text", help="人工展示（默认）或原业务 JSON")

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
        items = split_symbol_whitelist(args.symbols)
        symbols = [item.upper() for item in items] if items is not None else None
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

    if args.command == "position-advice":
        import json
        from pathlib import Path
        from src.application.tool_execution import execute_tool

        try:
            request_path = Path(args.request_file)
            if request_path.stat().st_size > 262144:
                raise ValueError("position advice request must not exceed 256 KiB")
            payload = json.loads(request_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("position advice request must be a JSON object")
            if args.config_key:
                payload["config_key"] = args.config_key
            if getattr(args, "config_path", None):
                payload["config_path"] = args.config_path
        except (OSError, ValueError) as exc:
            raise AgentToolError(code="INPUT_ERROR", message=str(exc)) from exc
        tool_name = (
            "short_put_roll_candidates"
            if args.position_advice_command == "roll-down"
            else "position_advice_evaluate"
        )
        result = execute_tool(tool_name, payload)
        if not result["ok"]:
            raise AgentToolError(**result["error"])
        return result["data"]

    if args.command == "notify" and args.notify_command == "preview":
        return preview_notification_fn(
            account=args.account,
            market=args.market,
            date=args.date,
            revision=args.revision,
        )

    raise AgentToolError(code="INPUT_ERROR", message=f"unsupported operator command: {args.command}")
