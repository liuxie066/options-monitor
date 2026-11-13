"""Private state-store access shared by inbound handling and replies."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from src.application.agent_tool_contracts import AgentToolError
from src.application.channels.wechat_clawbot.state import resolve_wechat_clawbot_state_dir
from src.application.channels.wechat_clawbot.state_store import WechatClawbotStateStore


def _state_store(*, base: Path, label: str, state_dir: str | None = None) -> WechatClawbotStateStore:
    notifications = {"wechat_clawbot_state_dir": state_dir} if state_dir else None
    return WechatClawbotStateStore(resolve_wechat_clawbot_state_dir(base=base, label=label, notifications=notifications))


def _load_store_json(load_fn: Callable[..., dict[str, Any]], *, default: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        return load_fn(default=default)
    except ValueError as exc:
        raise AgentToolError(code="STATE_ERROR", message=str(exc)) from exc
