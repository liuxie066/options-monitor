from __future__ import annotations

import argparse
import json
import sys

from src.application.agent_tool_contracts import AgentToolError
from src.application.portfolio_assignment_scenario import (
    AssignmentScenarioInputError,
    query_portfolio_assignment_scenario,
    render_assignment_scenario_text,
)


def add_portfolio_commands(subparsers) -> None:
    parser = subparsers.add_parser(
        "portfolio",
        help="read-only cross-product portfolio analysis",
    )
    commands = parser.add_subparsers(dest="portfolio_command", required=True)
    scenario = commands.add_parser(
        "assignment-scenario",
        help="project all open short puts and calls as assigned",
    )
    scenario.add_argument(
        "--accounts",
        nargs="+",
        required=True,
        help="OM account labels, for example: --accounts lx sy",
    )
    scenario.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="output format (default: text)",
    )


def handle_portfolio_command(args: argparse.Namespace) -> int:
    if args.portfolio_command != "assignment-scenario":
        raise AgentToolError(
            code="INPUT_ERROR",
            message=f"unsupported portfolio command: {args.portfolio_command}",
        )
    try:
        result = query_portfolio_assignment_scenario(args.accounts)
    except AssignmentScenarioInputError as exc:
        raise AgentToolError(code="INPUT_ERROR", message=str(exc)) from exc
    if args.format == "json":
        sys.stdout.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    else:
        sys.stdout.write(render_assignment_scenario_text(result))
    return 0


__all__ = ["add_portfolio_commands", "handle_portfolio_command"]
