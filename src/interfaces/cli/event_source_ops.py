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
    probe.add_argument(
        "--summary-only",
        action="store_true",
        help="omit raw event payloads and print only probe health/counts",
    )


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
        if bool(getattr(args, "summary_only", False)):
            data = _compact_probe_data(data)
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


def _compact_probe_data(data: dict[str, Any]) -> dict[str, Any]:
    symbols = data.get("symbols") if isinstance(data.get("symbols"), dict) else {}
    compact_symbols = {
        str(symbol): _compact_symbol_probe(row)
        for symbol, row in symbols.items()
        if isinstance(row, dict)
    }
    compact: dict[str, Any] = {
        "ok": bool(data.get("ok")),
        "provider": data.get("provider"),
        "created_at": data.get("created_at"),
        "summary": data.get("summary", {}),
        "symbols": compact_symbols,
    }
    if isinstance(data.get("endpoint"), dict):
        compact["endpoint"] = data["endpoint"]
    providers = data.get("providers")
    if isinstance(providers, dict):
        compact["providers"] = {
            str(provider): {
                "ok": bool(payload.get("ok")),
                "summary": payload.get("summary", {}),
            }
            for provider, payload in providers.items()
            if isinstance(payload, dict)
        }
    if isinstance(data.get("error"), dict):
        compact["error"] = data["error"]
    return compact


def _compact_symbol_probe(row: dict[str, Any]) -> dict[str, Any]:
    compact: dict[str, Any] = {"ok": bool(row.get("ok"))}
    for key in ("event_count", "error_code", "error_type", "source_error"):
        if key in row:
            compact[key] = row.get(key)
    source_results = row.get("source_results")
    if isinstance(source_results, dict):
        compact["source_results"] = {
            str(provider): _compact_symbol_probe(payload)
            for provider, payload in source_results.items()
            if isinstance(payload, dict)
        }
    return compact
