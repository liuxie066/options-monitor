from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable

from src.application.agent_tool_contracts import AgentToolError
from src.application.inbound import (
    build_feishu_ws_settings,
    check_feishu_ws_settings,
    handle_feishu_payload,
    serve_feishu_ws,
)


def _dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def _print(payload: dict[str, Any]) -> int:
    sys.stdout.write(_dumps(payload))
    return 0 if payload.get("ok", True) else 2


def add_inbound_commands(subparsers: Any) -> None:
    inbound = subparsers.add_parser("inbound", help="handle channel transport adapters")
    inbound_sub = inbound.add_subparsers(dest="inbound_command", required=True)
    inbound_feishu = inbound_sub.add_parser("feishu", help="handle one Feishu event payload through assistant control")
    feishu_input = inbound_feishu.add_mutually_exclusive_group(required=True)
    feishu_input.add_argument("--input-json", default=None)
    feishu_input.add_argument("--input-file", default=None)
    feishu_input.add_argument("--stdin", action="store_true")
    inbound_feishu.add_argument("--config-key", default=None, choices=("us", "hk"))
    inbound_feishu.add_argument("--config-path", default=None)
    inbound_feishu.add_argument("--assistant-config", default=None)
    inbound_feishu.add_argument("--audit-db", default=None)
    inbound_feishu.add_argument("--env-file", default=None)
    inbound_feishu.add_argument("--no-local-env-file", action="store_true")
    inbound_feishu.add_argument("--format", choices=("json", "text"), default="json")
    inbound_ws = inbound_sub.add_parser("feishu-ws", help="serve the Feishu App long-connection inbound client")
    inbound_ws.add_argument("--config-key", default=None, choices=("us", "hk"))
    inbound_ws.add_argument("--config-path", default=None)
    inbound_ws.add_argument("--assistant-config", default=None)
    inbound_ws.add_argument("--audit-db", default=None)
    inbound_ws.add_argument("--env-file", default=None)
    inbound_ws.add_argument(
        "--credential-env-file",
        default=None,
        help="deprecated compatibility credential env file for --check, loaded after --env-file",
    )
    inbound_ws.add_argument("--no-local-env-file", action="store_true")
    inbound_ws.add_argument("--no-reply", action="store_true")
    inbound_ws.add_argument("--reply-in-thread", action="store_true", default=None)
    inbound_ws.add_argument("--max-reply-chars", type=int, default=None)
    inbound_ws.add_argument("--queue-size", type=int, default=None)
    inbound_ws.add_argument("--lock-path", default=None)
    inbound_ws.add_argument(
        "--check",
        action="store_true",
        help="validate and print redacted long-connection configuration without starting the client",
    )


def _load_json_payload(*, json_text: str | None, file_path: str | None, stdin_enabled: bool = False) -> dict[str, Any]:
    try:
        if file_path:
            payload = json.loads(Path(file_path).read_text(encoding="utf-8"))
        elif stdin_enabled:
            payload = json.loads(sys.stdin.read())
        elif json_text:
            payload = json.loads(json_text)
        else:
            raise AgentToolError(code="INPUT_ERROR", message="missing JSON payload")
    except AgentToolError:
        raise
    except Exception as exc:
        raise AgentToolError(
            code="INPUT_ERROR",
            message="failed to parse JSON payload",
            details={"error": f"{type(exc).__name__}: {exc}"},
        ) from exc
    if not isinstance(payload, dict):
        raise AgentToolError(code="INPUT_ERROR", message="JSON payload must be an object")
    return payload


def handle_inbound_command(
    args: argparse.Namespace,
    *,
    handle_feishu_payload_fn: Callable[..., dict[str, Any]] = handle_feishu_payload,
    build_feishu_ws_settings_fn: Callable[..., Any] = build_feishu_ws_settings,
    check_feishu_ws_settings_fn: Callable[..., dict[str, Any]] = check_feishu_ws_settings,
    serve_feishu_ws_fn: Callable[..., Any] = serve_feishu_ws,
) -> int:
    if args.inbound_command == "feishu":
        out = handle_feishu_payload_fn(
            _load_json_payload(
                json_text=args.input_json,
                file_path=args.input_file,
                stdin_enabled=bool(args.stdin),
            ),
            config_key=args.config_key,
            config_path=args.config_path,
            audit_db=args.audit_db,
            assistant_config_path=args.assistant_config,
        )
        if args.format == "text":
            data_raw = out.get("data")
            data = data_raw if isinstance(data_raw, dict) else {}
            text = str(data.get("response_text") or data.get("challenge") or "").strip() or _dumps(out)
            sys.stdout.write(text + "\n")
            return 0 if out.get("ok", True) else 2
        return _print(out)

    if args.inbound_command == "feishu-ws":
        if args.credential_env_file and not args.check:
            raise AgentToolError(
                code="INPUT_ERROR",
                message="--credential-env-file is only supported with --check",
            )
        settings = build_feishu_ws_settings_fn(
            config_key=args.config_key,
            config_path=args.config_path,
            assistant_config_path=args.assistant_config,
            audit_db=args.audit_db,
            reply_enabled=False if bool(args.no_reply) else None,
            reply_in_thread=args.reply_in_thread,
            max_reply_chars=args.max_reply_chars,
            queue_size=args.queue_size,
            environ=os.environ,
            env_file=args.env_file,
            credential_env_file=args.credential_env_file,
        )
        if args.check:
            return _print(check_feishu_ws_settings_fn(settings))
        serve_feishu_ws_fn(settings, lock_path=args.lock_path)
        return 0

    raise AgentToolError(code="INPUT_ERROR", message=f"unsupported inbound command: {args.inbound_command}")
