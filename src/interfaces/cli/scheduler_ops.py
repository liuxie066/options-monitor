from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Callable

from src.application.agent_tool_config import repo_base
from src.application.agent_tool_contracts import AgentToolError
from src.application.cash_headroom_query import query_sell_put_cash
from src.application.runtime_paths import resolve_runtime_root
from src.application.scan_scheduler import run_scheduler


def add_scheduler_commands(subparsers: Any) -> None:
    scheduler = subparsers.add_parser("scheduler", help="scan scheduler / frequency controller")
    scheduler.add_argument("--config", required=True)
    scheduler.add_argument("--state-dir", default=None)
    scheduler.add_argument("--state", default=None)
    scheduler.add_argument("--schedule-key", default="schedule")
    scheduler.add_argument("--account", default=None)
    scheduler.add_argument("--run-if-due", action="store_true")
    scheduler.add_argument("--mark-notified", action="store_true")
    scheduler.add_argument("--mark-scanned", action="store_true")
    scheduler.add_argument("--jsonl", action="store_true")
    scheduler.add_argument("--force", action="store_true")

    sell_put_cash = subparsers.add_parser("sell-put-cash", help="query cash headroom for sell-put")
    sell_put_cash.add_argument("--config", default=None)
    sell_put_cash.add_argument("--data-config", default=None)
    sell_put_cash.add_argument("--market", default="富途")
    sell_put_cash.add_argument("--account", default=None)
    sell_put_cash.add_argument("--format", choices=("text", "json"), default="text")
    sell_put_cash.add_argument("--top", type=int, default=10)
    sell_put_cash.add_argument("--no-exchange-rates", action="store_true")
    sell_put_cash.add_argument("--out-dir", default=None)


def handle_scheduler_command(
    args: argparse.Namespace,
    *,
    repo_base_fn: Callable[[], Path] = repo_base,
    run_scheduler_fn: Callable[..., object] = run_scheduler,
    query_sell_put_cash_fn: Callable[..., object] = query_sell_put_cash,
) -> int:
    runtime_root = resolve_runtime_root(repo_root=repo_base_fn()).runtime_root

    if args.command == "scheduler":
        run_scheduler_fn(
            config=args.config,
            state_dir=args.state_dir or str((runtime_root / "output_shared" / "state").resolve()),
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
        query_sell_put_cash_fn(
            config=args.config,
            data_config=args.data_config,
            market=args.market,
            account=args.account,
            output_format=args.format,
            top=args.top,
            no_exchange_rates=bool(args.no_exchange_rates),
            out_dir=args.out_dir or str((runtime_root / "output_shared" / "state").resolve()),
        )
        return 0

    raise AgentToolError(code="INPUT_ERROR", message=f"unsupported scheduler command: {args.command}")
