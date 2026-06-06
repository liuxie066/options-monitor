from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Callable

from src.application.agent_tool_config import repo_base
from src.application.agent_tool_contracts import AgentToolError
from src.application.channels.wechat_clawbot.binding import (
    bind_wechat_clawbot_target,
    check_wechat_clawbot_qrcode,
    list_wechat_clawbot_bindings,
    start_wechat_clawbot_qrcode,
)
from src.application.channels.wechat_clawbot.ilink_client import DEFAULT_ILINK_BASE_URL
from src.application.channels.wechat_clawbot.state import DEFAULT_WECHAT_CLAWBOT_LABEL


def add_channel_commands(subparsers: Any) -> None:
    channel = subparsers.add_parser("channel", help="manage first-class message channels")
    channel_sub = channel.add_subparsers(dest="channel_command", required=True)
    wechat = channel_sub.add_parser("wechat-clawbot", help="bind and inspect WeChat ClawBot targets")
    wechat_sub = wechat.add_subparsers(dest="wechat_clawbot_command", required=True)

    def _add_common(parser: Any) -> None:
        parser.add_argument("--label", default=DEFAULT_WECHAT_CLAWBOT_LABEL)
        parser.add_argument("--state-dir", default=None)

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


def handle_channel_command(
    args: argparse.Namespace,
    *,
    repo_base_fn: Callable[[], Path] = repo_base,
    start_qrcode_fn: Callable[..., dict[str, Any]] = start_wechat_clawbot_qrcode,
    check_qrcode_fn: Callable[..., dict[str, Any]] = check_wechat_clawbot_qrcode,
    bind_target_fn: Callable[..., dict[str, Any]] = bind_wechat_clawbot_target,
    list_bindings_fn: Callable[..., dict[str, Any]] = list_wechat_clawbot_bindings,
) -> dict[str, Any]:
    if args.channel_command != "wechat-clawbot":
        raise AgentToolError(code="INPUT_ERROR", message=f"unsupported channel command: {args.channel_command}")
    base = repo_base_fn()
    label = str(getattr(args, "label", None) or DEFAULT_WECHAT_CLAWBOT_LABEL)
    state_dir = getattr(args, "state_dir", None)
    subcommand = str(getattr(args, "wechat_clawbot_command", "") or "")

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
    raise AgentToolError(code="INPUT_ERROR", message=f"unsupported wechat-clawbot command: {subcommand}")
