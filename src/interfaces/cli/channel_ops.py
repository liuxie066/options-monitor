from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Callable

from src.application.agent_tool_config import repo_base
from src.application.agent_tool_contracts import AgentToolError, build_response
from src.application.channels.status import channel_status_response
from src.application.channels.wechat_clawbot.binding import (
    bind_wechat_clawbot_target,
    check_wechat_clawbot_qrcode,
    connect_wechat_clawbot_target,
    list_wechat_clawbot_bindings,
    start_wechat_clawbot_qrcode,
)
from src.application.channels.wechat_clawbot.inbound import (
    build_wechat_clawbot_serve_settings,
    check_wechat_clawbot_serve_settings,
    poll_wechat_clawbot_once,
    serve_wechat_clawbot,
)
from src.application.channels.wechat_clawbot.ilink_client import DEFAULT_ILINK_BASE_URL
from src.application.channels.wechat_clawbot.state import DEFAULT_WECHAT_CLAWBOT_LABEL


def add_channel_commands(subparsers: Any) -> None:
    channel = subparsers.add_parser("channel", help="manage first-class message channels")
    channel_sub = channel.add_subparsers(dest="channel_command", required=True)

    status = channel_sub.add_parser("status", help="inspect first-class message channel health")
    status.add_argument("--runtime-root", default=None)
    status.add_argument("--profile-path", default=None)
    status.add_argument("--assistant-config", default=None)
    status.add_argument("--env-file", default=None)
    status.add_argument("--include-service-status", action="store_true")

    wechat = channel_sub.add_parser("wechat-clawbot", help="bind and inspect WeChat ClawBot targets")
    wechat_sub = wechat.add_subparsers(dest="wechat_clawbot_command", required=True)

    def _add_common(parser: Any) -> None:
        parser.add_argument("--label", default=None)
        parser.add_argument("--state-dir", default=None)

    connect = wechat_sub.add_parser("connect", help="scan QR and bind a WeChat ClawBot notification target")
    _add_common(connect)
    connect.add_argument("--name", default="ops")
    connect.add_argument("--match-text", default=None)
    connect.add_argument("--from-user-id", default=None)
    connect.add_argument("--base-url", default=DEFAULT_ILINK_BASE_URL)
    connect.add_argument("--timeout-sec", type=int, default=20)
    connect.add_argument("--login-timeout-sec", type=int, default=180)
    connect.add_argument("--bind-timeout-sec", type=int, default=180)
    connect.add_argument("--poll-interval-sec", type=float, default=3.0)
    connect.add_argument("--print-open-command", action="store_true")

    qrcode = wechat_sub.add_parser("qrcode", help="request a WeChat ClawBot QR login code")
    _add_common(qrcode)
    qrcode.add_argument("--base-url", default=DEFAULT_ILINK_BASE_URL)
    qrcode.add_argument("--timeout-sec", type=int, default=20)

    qr_status = wechat_sub.add_parser("qr-status", help="check QR login status and persist state when confirmed")
    _add_common(qr_status)
    qr_status.add_argument("--qrcode", default=None)
    qr_status.add_argument("--base-url", default=None)
    qr_status.add_argument("--timeout-sec", type=int, default=20)

    bind = wechat_sub.add_parser("bind", help="bind the latest matching WeChat message as a notification target")
    _add_common(bind)
    bind.add_argument("--name", required=True)
    bind.add_argument("--match-text", default=None)
    bind.add_argument("--from-user-id", default=None)
    bind.add_argument("--timeout-sec", type=int, default=20)

    list_cmd = wechat_sub.add_parser("list", help="list persisted WeChat ClawBot bindings")
    _add_common(list_cmd)

    poll_once = wechat_sub.add_parser("poll-once", help="poll one WeChat ClawBot inbound batch and optionally reply")
    _add_common(poll_once)
    poll_once.add_argument("--config-key", default=None, choices=("us", "hk"))
    poll_once.add_argument("--config-path", default=None)
    poll_once.add_argument("--assistant-config", default=None)
    poll_once.add_argument("--audit-db", default=None)
    poll_once.add_argument("--allowed-senders", default=None)
    poll_once.add_argument("--no-reply", action="store_true")
    poll_once.add_argument("--max-reply-chars", type=int, default=3500)
    poll_once.add_argument("--timeout-sec", type=int, default=20)

    serve = wechat_sub.add_parser("serve", help="serve the WeChat ClawBot inbound polling client")
    _add_common(serve)
    serve.add_argument("--config-key", default=None, choices=("us", "hk"))
    serve.add_argument("--config-path", default=None)
    serve.add_argument("--assistant-config", default=None)
    serve.add_argument("--audit-db", default=None)
    serve.add_argument("--allowed-senders", default=None)
    serve.add_argument("--no-reply", action="store_true")
    serve.add_argument("--max-reply-chars", type=int, default=None)
    serve.add_argument("--timeout-sec", type=int, default=None)
    serve.add_argument("--poll-interval-sec", type=float, default=None)
    serve.add_argument("--lock-path", default=None)
    serve.add_argument(
        "--check",
        action="store_true",
        help="validate and print redacted WeChat ClawBot inbound configuration without starting the client",
    )


def handle_channel_command(
    args: argparse.Namespace,
    *,
    repo_base_fn: Callable[[], Path] = repo_base,
    start_qrcode_fn: Callable[..., dict[str, Any]] = start_wechat_clawbot_qrcode,
    check_qrcode_fn: Callable[..., dict[str, Any]] = check_wechat_clawbot_qrcode,
    bind_target_fn: Callable[..., dict[str, Any]] = bind_wechat_clawbot_target,
    connect_target_fn: Callable[..., dict[str, Any]] = connect_wechat_clawbot_target,
    list_bindings_fn: Callable[..., dict[str, Any]] = list_wechat_clawbot_bindings,
    poll_once_fn: Callable[..., dict[str, Any]] = poll_wechat_clawbot_once,
    build_serve_settings_fn: Callable[..., Any] = build_wechat_clawbot_serve_settings,
    check_serve_settings_fn: Callable[..., dict[str, Any]] = check_wechat_clawbot_serve_settings,
    serve_fn: Callable[..., Any] = serve_wechat_clawbot,
    channel_status_response_fn: Callable[..., dict[str, Any]] = channel_status_response,
) -> dict[str, Any]:
    base = repo_base_fn()
    if args.channel_command == "status":
        environ = None
        if getattr(args, "env_file", None):
            from src.application.settings import build_effective_env

            environ = build_effective_env(env_file=getattr(args, "env_file", None)).values
        runtime_root = None
        if getattr(args, "runtime_root", None):
            runtime_root = Path(str(args.runtime_root)).expanduser()
        payload = {
            key: value
            for key, value in {
                "runtime_root": getattr(args, "runtime_root", None),
                "profile_path": getattr(args, "profile_path", None),
                "assistant_config_path": getattr(args, "assistant_config", None),
            }.items()
            if value
        }
        return channel_status_response_fn(
            base=base,
            payload=payload,
            runtime_root=runtime_root,
            environ=environ,
            include_service_status=bool(getattr(args, "include_service_status", False)),
        )
    if args.channel_command != "wechat-clawbot":
        raise AgentToolError(code="INPUT_ERROR", message=f"unsupported channel command: {args.channel_command}")
    raw_label = getattr(args, "label", None)
    label = str(raw_label or DEFAULT_WECHAT_CLAWBOT_LABEL)
    state_dir = getattr(args, "state_dir", None)
    subcommand = str(getattr(args, "wechat_clawbot_command", "") or "")

    if subcommand == "connect":
        return connect_target_fn(
            base=base,
            name=args.name,
            label=label,
            match_text=args.match_text,
            from_user_id=args.from_user_id,
            state_dir=state_dir,
            base_url=args.base_url,
            timeout_sec=args.timeout_sec,
            login_timeout_sec=args.login_timeout_sec,
            bind_timeout_sec=args.bind_timeout_sec,
            poll_interval_sec=args.poll_interval_sec,
            progress_fn=lambda event: _print_connect_progress(
                event,
                print_open_command=bool(getattr(args, "print_open_command", False)),
            ),
        )
    if subcommand == "qrcode":
        return start_qrcode_fn(
            base=base,
            label=label,
            state_dir=state_dir,
            base_url=args.base_url,
            timeout_sec=args.timeout_sec,
        )
    if subcommand == "qr-status":
        return check_qrcode_fn(
            base=base,
            label=label,
            qrcode=args.qrcode,
            state_dir=state_dir,
            base_url=args.base_url,
            timeout_sec=args.timeout_sec,
        )
    if subcommand == "bind":
        return bind_target_fn(
            base=base,
            name=args.name,
            label=label,
            match_text=args.match_text,
            from_user_id=args.from_user_id,
            state_dir=state_dir,
            timeout_sec=args.timeout_sec,
        )
    if subcommand == "list":
        return list_bindings_fn(base=base, label=label, state_dir=state_dir)
    if subcommand == "poll-once":
        return poll_once_fn(
            base=base,
            label=label,
            state_dir=state_dir,
            config_key=args.config_key,
            config_path=args.config_path,
            assistant_config_path=args.assistant_config,
            audit_db=args.audit_db,
            allowed_senders=args.allowed_senders,
            reply_enabled=not bool(args.no_reply),
            max_reply_chars=args.max_reply_chars,
            timeout_sec=args.timeout_sec,
        )
    if subcommand == "serve":
        settings = build_serve_settings_fn(
            base=base,
            label=raw_label,
            state_dir=state_dir,
            config_key=args.config_key,
            config_path=args.config_path,
            assistant_config_path=args.assistant_config,
            audit_db=args.audit_db,
            allowed_senders=args.allowed_senders,
            reply_enabled=False if bool(args.no_reply) else None,
            max_reply_chars=args.max_reply_chars,
            poll_interval_sec=args.poll_interval_sec,
            timeout_sec=args.timeout_sec,
        )
        if args.check:
            return check_serve_settings_fn(settings)
        result = serve_fn(settings, lock_path=args.lock_path)
        if isinstance(result, dict):
            return result
        return build_response(tool_name="wechat_clawbot.serve", ok=True, data={"stopped": True})
    raise AgentToolError(code="INPUT_ERROR", message=f"unsupported wechat-clawbot command: {subcommand}")


def _print_connect_progress(event: dict[str, Any], *, print_open_command: bool = False) -> None:
    event_name = str(event.get("event") or "")
    if event_name == "qrcode":
        qrcode = str(event.get("qrcode") or "").strip()
        qrcode_artifact_path = str(event.get("qrcode_artifact_path") or "").strip()
        qrcode_artifact_open_command = str(event.get("qrcode_artifact_open_command") or "").strip()
        state_dir = str(event.get("state_dir") or "").strip()
        sys.stderr.write("WeChat ClawBot QR code:\n")
        sys.stderr.write(f"{qrcode}\n")
        if qrcode_artifact_path:
            sys.stderr.write(f"QR artifact: {qrcode_artifact_path}\n")
        if print_open_command and qrcode_artifact_open_command:
            sys.stderr.write(f"Open QR artifact: {qrcode_artifact_open_command}\n")
        if state_dir:
            sys.stderr.write(f"State dir: {state_dir}\n")
        sys.stderr.write("Scan the QR code in WeChat, then keep this command running.\n")
        sys.stderr.flush()
        return
    if event_name == "login_confirmed":
        sys.stderr.write("WeChat login confirmed.\n")
        sys.stderr.flush()
        return
    if event_name == "bind_instruction":
        match_text = str(event.get("match_text") or "").strip()
        sys.stderr.write(f"Send this text to the target WeChat chat: {match_text}\n")
        sys.stderr.flush()
        return
    if event_name == "bound":
        target = str(event.get("target") or "").strip()
        sys.stderr.write(f"WeChat ClawBot target bound: {target}\n")
        sys.stderr.flush()
