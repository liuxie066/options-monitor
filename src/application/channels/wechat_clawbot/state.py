from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.application.channels.wechat_clawbot.ilink_client import DEFAULT_ILINK_BASE_URL
from src.application.channels.wechat_clawbot.state_store import WechatClawbotStateStore


DEFAULT_WECHAT_CLAWBOT_LABEL = "default"


@dataclass(frozen=True)
class WechatClawbotState:
    label: str
    state_dir: Path
    base_url: str
    bot_token: str
    get_updates_buf: str = ""
    typing_ticket: str = ""


@dataclass(frozen=True)
class WechatClawbotBinding:
    label: str
    name: str
    target: str
    to_user_id: str
    context_token: str
    group_id: str | None = None
    chat_key: str | None = None
    updated_at_utc: str | None = None


@dataclass(frozen=True)
class WechatClawbotTarget:
    label: str
    binding_name: str
    raw: str


def default_wechat_clawbot_state_root(base: Path) -> Path:
    return (base / "output_shared" / "state" / "channels" / "wechat_clawbot").resolve()


def resolve_wechat_clawbot_target(
    target: str,
    *,
    notifications: dict[str, Any] | None = None,
) -> WechatClawbotTarget:
    raw = str(target or "").strip()
    if not raw:
        raise ValueError("wechat_clawbot target is required")
    default_label = str((notifications or {}).get("wechat_clawbot_label") or DEFAULT_WECHAT_CLAWBOT_LABEL).strip() or DEFAULT_WECHAT_CLAWBOT_LABEL
    parts = [part.strip() for part in raw.split(":") if part.strip()]
    if len(parts) >= 3 and parts[0] in {"wechat", "wechat_clawbot"}:
        return WechatClawbotTarget(label=parts[1], binding_name=":".join(parts[2:]), raw=raw)
    if len(parts) == 2 and parts[0] in {"wechat", "wechat_clawbot"}:
        return WechatClawbotTarget(label=default_label, binding_name=parts[1], raw=raw)
    return WechatClawbotTarget(label=default_label, binding_name=raw, raw=raw)


def resolve_wechat_clawbot_state_dir(
    *,
    base: Path,
    label: str,
    notifications: dict[str, Any] | None = None,
) -> Path:
    raw = str((notifications or {}).get("wechat_clawbot_state_dir") or "").strip()
    if raw:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = (base / path).resolve()
        return path
    return default_wechat_clawbot_state_root(base) / str(label or DEFAULT_WECHAT_CLAWBOT_LABEL)


def load_wechat_clawbot_state(
    *,
    base: Path,
    label: str,
    notifications: dict[str, Any] | None = None,
) -> WechatClawbotState:
    state_dir = resolve_wechat_clawbot_state_dir(base=base, label=label, notifications=notifications)
    payload = WechatClawbotStateStore(state_dir).load_state()
    bot_token = str(payload.get("bot_token") or "").strip()
    if not bot_token:
        raise ValueError("WeChat ClawBot bot_token is missing; run QR login binding first")
    return WechatClawbotState(
        label=str(label or DEFAULT_WECHAT_CLAWBOT_LABEL),
        state_dir=state_dir,
        base_url=str(payload.get("base_url") or DEFAULT_ILINK_BASE_URL).strip() or DEFAULT_ILINK_BASE_URL,
        bot_token=bot_token,
        get_updates_buf=str(payload.get("get_updates_buf") or ""),
        typing_ticket=str(payload.get("typing_ticket") or ""),
    )


def load_wechat_clawbot_binding(
    *,
    base: Path,
    target: str,
    notifications: dict[str, Any] | None = None,
) -> WechatClawbotBinding:
    resolved = resolve_wechat_clawbot_target(target, notifications=notifications)
    state_dir = resolve_wechat_clawbot_state_dir(base=base, label=resolved.label, notifications=notifications)
    payload = WechatClawbotStateStore(state_dir).load_bindings()
    bindings_raw = payload.get("bindings")
    bindings = bindings_raw if isinstance(bindings_raw, dict) else {}
    binding_raw = bindings.get(resolved.binding_name)
    if not isinstance(binding_raw, dict):
        raise ValueError(f"WeChat ClawBot binding not found: {resolved.raw}")
    to_user_id = str(binding_raw.get("to_user_id") or "").strip()
    context_token = str(binding_raw.get("context_token") or "").strip()
    if not to_user_id or not context_token:
        raise ValueError(f"WeChat ClawBot binding is incomplete: {resolved.raw}")
    return WechatClawbotBinding(
        label=resolved.label,
        name=resolved.binding_name,
        target=resolved.raw,
        to_user_id=to_user_id,
        context_token=context_token,
        group_id=str(binding_raw.get("group_id") or "").strip() or None,
        chat_key=str(binding_raw.get("chat_key") or "").strip() or None,
        updated_at_utc=str(binding_raw.get("updated_at_utc") or "").strip() or None,
    )
