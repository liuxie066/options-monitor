from __future__ import annotations

import argparse
from typing import Any, Callable

from src.application.agent_tool_contracts import AgentToolError, build_response
from src.application.events.probe import probe_event_source


def add_event_source_commands(subparsers: Any) -> None:
    event_source = subparsers.add_parser("event-source", help="probe read-only event-risk data sources")
    event_source_sub = event_source.add_subparsers(dest="event_source_command", required=True)
    probe = event_source_sub.add_parser("probe", help="fetch event dates without writing runtime state")
    probe.add_argument("--provider", default="futu", choices=("futu", "opend", "yfinance", "yahoo", "all"))
    probe.add_argument("--symbols", nargs="+", required=True, help="symbols or comma-separated symbols")
    probe.add_argument("--host", default="127.0.0.1", help="Futu OpenD host for provider=futu")
    probe.add_argument("--port", type=int, default=11111, help="Futu OpenD port for provider=futu")


def handle_event_source_command(
    args: argparse.Namespace,
    *,
    probe_event_source_fn: Callable[..., dict[str, Any]] = probe_event_source,
) -> dict[str, Any]:
    if args.event_source_command == "probe":
        data = probe_event_source_fn(
            provider=args.provider,
            symbols=_split_symbols(args.symbols),
            host=args.host,
            port=int(args.port),
        )
        return build_response(
            tool_name="event_source_probe",
            ok=bool(data.get("ok", True)),
            data=data,
        )

    raise AgentToolError(code="INPUT_ERROR", message=f"unsupported event-source command: {args.event_source_command}")


def _split_symbols(values: list[str] | None) -> list[str]:
    out: list[str] = []
    for value in values or []:
        for item in str(value or "").split(","):
            symbol = item.strip()
            if symbol:
                out.append(symbol)
    return out
