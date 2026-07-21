from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable

from src.application.agent_tool_contracts import AgentToolError
from src.application.agent_tools.daily_brief import read_daily_brief_view
from src.application.daily_decision_brief_repository import (
    DailyDecisionBriefStateError,
    inspect_daily_decision_brief_delivery,
    migrate_daily_decision_brief_delivery,
)
from src.application.runtime_paths import resolve_runtime_root


def add_daily_brief_commands(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "daily-brief",
        help="read the canonical local Daily Decision Brief without refreshing or sending",
    )
    daily_sub = parser.add_subparsers(dest="daily_brief_command", required=True)

    latest = daily_sub.add_parser("latest", help="read the latest available brief")
    _add_latest_arguments(latest)

    day = daily_sub.add_parser("day", help="read a trading-day brief")
    _add_day_arguments(day)
    day.add_argument("--date", dest="market_trading_date", required=True, help="market trading date YYYY-MM-DD")
    day.add_argument("--revision", type=int, help="exact non-negative revision")

    delivery_inspect = daily_sub.add_parser(
        "delivery-inspect",
        help="inspect delivery state and preview a v1 to v2 migration without writing",
    )
    _add_delivery_arguments(delivery_inspect)

    delivery_migrate = daily_sub.add_parser(
        "delivery-migrate",
        help="migrate one validated v1 delivery pointer; defaults to dry-run",
    )
    _add_delivery_arguments(delivery_migrate)
    mode = delivery_migrate.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="preview only (default)")
    mode.add_argument("--confirm", action="store_true", help="write v2 state after creating a v1 backup")


def _add_latest_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--account", help="optional account label; omitted reads all enabled accounts")
    parser.add_argument("--market", choices=("US", "HK", "us", "hk"), help="optional market; omitted reads all")
    parser.add_argument("--json", action="store_true", help="print structured JSON instead of Markdown")


def _add_day_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--account", required=True, help="account label such as lx")
    parser.add_argument("--market", choices=("US", "HK", "us", "hk"), default="US")
    parser.add_argument("--json", action="store_true", help="print structured JSON instead of Markdown")


def _add_delivery_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--account", required=True, help="account label such as lx")
    parser.add_argument("--market", choices=("US", "HK", "us", "hk"), required=True)


def handle_daily_brief_command(
    args: argparse.Namespace,
    *,
    repo_base_fn: Callable[[], Path],
) -> int:
    command = str(args.daily_brief_command or "")
    repo_root = repo_base_fn()
    runtime_root = resolve_runtime_root(repo_root=repo_root).runtime_root
    if command in {"delivery-inspect", "delivery-migrate"}:
        try:
            if command == "delivery-inspect":
                data = inspect_daily_decision_brief_delivery(
                    base=runtime_root,
                    account=args.account,
                    market=args.market,
                )
            else:
                data = migrate_daily_decision_brief_delivery(
                    base=runtime_root,
                    account=args.account,
                    market=args.market,
                    confirm=bool(getattr(args, "confirm", False)),
                )
        except ValueError as exc:
            raise AgentToolError(code="INPUT_ERROR", message=str(exc)) from exc
        except DailyDecisionBriefStateError as exc:
            raise AgentToolError(code="STATE_INVALID", message=str(exc)) from exc
        sys.stdout.write(json.dumps(_json_safe(data), ensure_ascii=False, indent=2) + "\n")
        return 0

    revision = getattr(args, "revision", None)
    market_trading_date = getattr(args, "market_trading_date", None)
    if command == "latest":
        revision = None
        market_trading_date = None
    elif command != "day":
        raise AgentToolError(code="INPUT_ERROR", message=f"unsupported daily-brief command: {command}")
    if revision is not None and revision < 0:
        raise AgentToolError(code="INPUT_ERROR", message="revision must be non-negative")

    try:
        data = read_daily_brief_view(
            base=runtime_root,
            account=getattr(args, "account", None),
            market=getattr(args, "market", None),
            market_trading_date=market_trading_date,
            revision=revision,
        )
    except ValueError as exc:
        raise AgentToolError(code="INPUT_ERROR", message=str(exc)) from exc
    if args.json:
        sys.stdout.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    else:
        sys.stdout.write(str(data["rendered_markdown"]).rstrip() + "\n")
    return 0


def _json_safe(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


__all__ = ["add_daily_brief_commands", "handle_daily_brief_command"]
