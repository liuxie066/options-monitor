"""Optional message transports and current authoring controls.

Importing this module never imports an SDK or enables a channel.
"""
from __future__ import annotations

import importlib
import importlib.util
from types import ModuleType
from typing import Any

from src.application.agent_tool_contracts import AgentToolError

CHANNEL_PACKAGES = {
    "feishu": ("om-feishu-channel-plugin", "om_feishu_channel"),
    "wechat_clawbot": ("om-wechat-channel-plugin", "om_wechat_channel"),
}
CHANNEL_FLAGS = frozenset({"enabled", "receive_enabled", "notify_enabled"})


def canonical_channel(channel: str) -> str:
    channel = {"feishu_app": "feishu", "wechat": "wechat_clawbot"}.get(channel, channel)
    if channel not in CHANNEL_PACKAGES:
        raise ValueError(f"unsupported message channel: {channel}")
    return channel


def channel_plugin_installed(channel: str) -> bool:
    _, module = CHANNEL_PACKAGES[canonical_channel(channel)]
    return importlib.util.find_spec(module) is not None


def load_channel_plugin(channel: str, module: str | None = None) -> ModuleType:
    channel = canonical_channel(channel)
    distribution, root = CHANNEL_PACKAGES[channel]
    if not channel_plugin_installed(channel):
        raise AgentToolError(code="CHANNEL_PLUGIN_NOT_INSTALLED",
            message=f"{channel} channel requires optional package {distribution}",
            hint=f"运行 om plugins install {'feishu' if channel == 'feishu' else 'wechat'} 预览安装；确认后加 --confirm，安装不启用")
    # An installed package's broken dependencies are genuine failures, not absence.
    return importlib.import_module(root + (f".{module}" if module else ""))


def channel_activity_enabled(config: dict[str, Any] | None, channel: str,
                             activity: str) -> bool:
    channel = canonical_channel(channel)
    if activity not in {"receive", "notify"}:
        raise ValueError(f"unsupported channel activity: {activity}")
    cfg = config or {}
    if "channels" not in cfg:
        # Preserve legacy explicitly invoked inbound and existing outbound gate.
        return activity == "receive" or (
            isinstance(cfg.get("notifications"), dict)
            and cfg["notifications"].get("enabled") is True)
    channels = cfg.get("channels")
    selected = channels.get(channel) if isinstance(channels, dict) else None
    if not isinstance(selected, dict):
        return False
    return selected.get("enabled") is True and selected.get(f"{activity}_enabled") is True
