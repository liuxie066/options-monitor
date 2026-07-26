from __future__ import annotations

import argparse
from typing import Any

from src.application.quality.service import OMQualityService
from src.interfaces.quality.http import serve_quality_http


def add_quality_commands(subparsers: Any) -> None:
    quality = subparsers.add_parser("quality", help="read or refresh OM runtime and data-quality status")
    commands = quality.add_subparsers(dest="quality_command", required=True)
    status = commands.add_parser("status", help="read the latest published quality artifact")
    status.add_argument("--json", action="store_true", help="emit the canonical JSON payload")
    refresh = commands.add_parser("refresh", help="run read-only quality checks and publish an atomic artifact")
    refresh.add_argument("--config-key", action="append", choices=("us", "hk"), dest="config_keys")
    refresh.add_argument("--no-deep", action="store_true", help="skip authoritative OpenD reads")
    refresh.add_argument("--day-end-strict", action="store_true")
    serve = commands.add_parser("serve", help="serve the latest artifact over a loopback-only HTTP endpoint")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8792)


def handle_quality_command(args: argparse.Namespace) -> dict[str, Any] | int:
    service = OMQualityService()
    if args.quality_command == "status":
        payload = service.read_published()
        if payload is None:
            return {
                "ok": False,
                "error": {
                    "code": "QUALITY_STATUS_UNAVAILABLE",
                    "message": "No valid published OM quality status is available.",
                },
            }
        return payload
    if args.quality_command == "refresh":
        return service.refresh(
            config_keys=args.config_keys,
            deep=not bool(args.no_deep),
            day_end_strict=bool(args.day_end_strict),
        )
    if args.quality_command == "serve":
        serve_quality_http(service=service, host=args.host, port=args.port)
        return 0
    raise ValueError(f"unsupported quality command: {args.quality_command}")


__all__ = ["add_quality_commands", "handle_quality_command"]
