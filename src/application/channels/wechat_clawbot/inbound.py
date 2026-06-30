from __future__ import annotations

import json
import logging
import os
import shlex
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, cast

from domain.domain.multi_tick import WECHAT_CLAWBOT_NOTIFICATION_PROVIDER
from src.application.agent_tool_config import load_runtime_config
from src.application.agent_tool_contracts import AgentToolError, build_error_payload, build_response, mask_path
from src.application.assistant.audit import InboundAuditStore
from src.application.assistant.contracts import AssistantRequest
from src.application.assistant.policy import check_sender_allowed
from src.application.channels.reply_decision import (
    decide_inbound_reply,
    inbound_command_id as _inbound_command_id,
    inbound_message_data as _inbound_message_data,
    inbound_message_result as _inbound_message_result,
    permission_denied_message,
)
from src.application.channels.service import ChannelService
from src.application.channels.wechat_clawbot.adapter import build_wechat_clawbot_inbound_channel_service
from src.application.channels.wechat_clawbot.binding import (
    refresh_wechat_clawbot_binding_from_inbound_message,
    refresh_wechat_clawbot_binding_from_reply,
)
from src.application.channels.wechat_clawbot.ilink_client import DEFAULT_ILINK_BASE_URL, WechatClawbotClient
from src.application.channels.wechat_clawbot.message import (
    extract_first_string,
    extract_messages,
    message_chat_key,
    message_context_token,
    message_group_id,
    message_id,
    message_text,
    message_user_id,
)
from src.application.channels.wechat_clawbot.state import DEFAULT_WECHAT_CLAWBOT_LABEL, resolve_wechat_clawbot_state_dir
from src.application.channels.wechat_clawbot.reply import reply_wechat_clawbot_text
from src.application.channels.wechat_clawbot.state_store import WechatClawbotStateStore
from src.application.conversation_scope import wechat_window_conversation_id
from src.infrastructure.io_utils import utc_now


ExecuteToolFn = Callable[[str, dict[str, Any]], dict[str, Any]]
ClientFactory = Callable[..., WechatClawbotClient]
DEFAULT_WECHAT_REPLY_MAX_CHARS = 3500
DEFAULT_WECHAT_POLL_INTERVAL_SEC = 3.0
DEFAULT_WECHAT_KEEPALIVE_INTERVAL_SEC = 1800.0

LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class WechatClawbotServeSettings:
    base: Path
    label: str = DEFAULT_WECHAT_CLAWBOT_LABEL
    state_dir: str | None = None
    config_key: str | None = None
    config_path: str | None = None
    assistant_config_path: str | None = None
    audit_db: str | None = None
    allowed_senders: str | None = None
    reply_enabled: bool = True
    max_reply_chars: int = DEFAULT_WECHAT_REPLY_MAX_CHARS
    poll_interval_sec: float = DEFAULT_WECHAT_POLL_INTERVAL_SEC
    keepalive_interval_sec: float = DEFAULT_WECHAT_KEEPALIVE_INTERVAL_SEC
    timeout_sec: int = 20

    def validate_for_serve(self) -> None:
        if not (self.config_path or self.config_key):
            raise AgentToolError(
                code="CONFIG_ERROR",
                message="missing inbound runtime config scope for WeChat ClawBot",
                hint="Pass --config-key us/hk or --config-path.",
            )
        if not self.allowed_senders:
            raise AgentToolError(
                code="CONFIG_ERROR",
                message="missing inbound sender allowlist for WeChat ClawBot",
                hint="Pass --allowed-senders wechat:<from_user_id>.",
            )
        state = _load_store_json(_state_store(base=self.base, label=self.label, state_dir=self.state_dir).load_state, default={})
        if not str(state.get("bot_token") or "").strip():
            raise AgentToolError(
                code="STATE_ERROR",
                message="WeChat ClawBot bot_token is missing; run connect first",
                hint=f"Run {self.connect_command_template()} and scan the QR code.",
            )
        if self.poll_interval_sec < 0:
            raise AgentToolError(code="CONFIG_ERROR", message="wechat_clawbot poll_interval_sec must be >= 0")
        if self.keepalive_interval_sec < 0:
            raise AgentToolError(code="CONFIG_ERROR", message="wechat_clawbot keepalive_interval_sec must be >= 0")

    def connect_command_template(self, *, name: str = "ops") -> str:
        command = ["./om", "channel", "wechat-clawbot", "connect", "--label", self.label, "--name", str(name or "ops")]
        if self.state_dir:
            command.extend(["--state-dir", self.state_dir])
        return " ".join(shlex.quote(str(part)) for part in command)

    def redacted_status(self) -> dict[str, Any]:
        store = _state_store(base=self.base, label=self.label, state_dir=self.state_dir)
        state: dict[str, Any] = {}
        try:
            state = _load_store_json(store.load_state, default={})
        except AgentToolError:
            state = {}
        return {
            "label": self.label,
            "state_dir": str(store.state_dir),
            "config_key": self.config_key,
            "config_path": self.config_path,
            "assistant_config_path": self.assistant_config_path,
            "audit_db": mask_path(self.audit_db),
            "allowed_senders_configured": bool(self.allowed_senders),
            "bot_token_configured": bool(str(state.get("bot_token") or "").strip()),
            "base_url_configured": bool(str(state.get("base_url") or "").strip()),
            "connect_command_template": self.connect_command_template(),
            "reply_enabled": bool(self.reply_enabled),
            "max_reply_chars": int(self.max_reply_chars),
            "poll_interval_sec": float(self.poll_interval_sec),
            "keepalive_interval_sec": float(self.keepalive_interval_sec),
            "timeout_sec": int(self.timeout_sec),
        }


def build_wechat_clawbot_serve_settings(
    *,
    base: Path,
    label: str | None = None,
    state_dir: str | None = None,
    config_key: str | None = None,
    config_path: str | None = None,
    assistant_config_path: str | None = None,
    audit_db: str | None = None,
    allowed_senders: str | None = None,
    reply_enabled: bool | None = None,
    max_reply_chars: int | None = None,
    poll_interval_sec: float | None = None,
    keepalive_interval_sec: float | None = None,
    timeout_sec: int | None = None,
) -> WechatClawbotServeSettings:
    from src.application.assistant.settings import AssistantSettings

    assistant_cfg = _load_assistant_behavior_config(config_path=assistant_config_path)
    behavior_cfg = _dict(_dict(assistant_cfg.get("inbound")).get("wechat_clawbot"))
    assistant_settings = AssistantSettings.from_runtime_config(assistant_cfg)
    default_config_key = (
        assistant_settings.default_market_scope
        if assistant_settings.default_market_scope in {"us", "hk"}
        else None
    )
    return WechatClawbotServeSettings(
        base=base,
        label=_first_text(label, behavior_cfg.get("label")) or DEFAULT_WECHAT_CLAWBOT_LABEL,
        state_dir=_first_text(state_dir, behavior_cfg.get("state_dir")),
        config_key=_normalize_config_key(_first_text(config_key, default_config_key)),
        config_path=_first_text(config_path),
        assistant_config_path=_first_text(assistant_config_path),
        audit_db=_first_text(audit_db),
        allowed_senders=_first_text(allowed_senders, behavior_cfg.get("allowed_senders")),
        reply_enabled=_config_bool(reply_enabled, behavior_cfg.get("reply_enabled"), default=True),
        max_reply_chars=_config_positive_int(
            max_reply_chars,
            behavior_cfg.get("max_reply_chars"),
            default=DEFAULT_WECHAT_REPLY_MAX_CHARS,
        ),
        poll_interval_sec=_config_non_negative_float(
            poll_interval_sec,
            behavior_cfg.get("poll_interval_sec"),
            default=DEFAULT_WECHAT_POLL_INTERVAL_SEC,
        ),
        keepalive_interval_sec=_config_non_negative_float(
            keepalive_interval_sec,
            behavior_cfg.get("keepalive_interval_sec"),
            default=DEFAULT_WECHAT_KEEPALIVE_INTERVAL_SEC,
        ),
        timeout_sec=_config_positive_int(timeout_sec, behavior_cfg.get("timeout_sec"), default=20),
    )


def check_wechat_clawbot_serve_settings(settings: WechatClawbotServeSettings) -> dict[str, Any]:
    ok = True
    error: dict[str, Any] | None = None
    try:
        settings.validate_for_serve()
    except AgentToolError as err:
        ok = False
        error = build_error_payload(err)
    return build_response(
        tool_name="wechat_clawbot.serve.check",
        ok=ok,
        data={"settings": settings.redacted_status()},
        error=error,
    )


def handle_wechat_clawbot_message(
    payload: dict[str, Any],
    *,
    base: Path | None = None,
    label: str = DEFAULT_WECHAT_CLAWBOT_LABEL,
    state_dir: str | None = None,
    config_key: str | None = None,
    config_path: str | None = None,
    audit_db: str | None = None,
    execute_tool_fn: ExecuteToolFn | None = None,
    allowed_senders: str | None = None,
    assistant_settings: Any | None = None,
    assistant_config_path: str | None = None,
    model_turn_fn: Any | None = None,
    generate_reply_fn: Any | None = None,
) -> dict[str, Any]:
    text = message_text(payload)
    if not text:
        return build_response(
            tool_name="inbound.wechat_clawbot",
            ok=True,
            data={"kind": "ignored_message", "reason": "empty_or_non_text"},
        )

    try:
        request = wechat_clawbot_message_to_assistant_request(
            payload,
            base=base,
            label=label,
            state_dir=state_dir,
            config_key=config_key,
            config_path=config_path,
            audit_db=audit_db,
            assistant_config_path=assistant_config_path,
        )
    except AgentToolError as exc:
        return build_response(
            tool_name="inbound.wechat_clawbot",
            ok=False,
            error=build_error_payload(exc),
        )

    kwargs: dict[str, Any] = {"allowed_senders": allowed_senders}
    if execute_tool_fn is not None:
        kwargs["execute_tool_fn"] = execute_tool_fn
    if model_turn_fn is not None:
        kwargs["model_turn_fn"] = model_turn_fn
    if generate_reply_fn is not None:
        kwargs["generate_reply_fn"] = generate_reply_fn
    settings = assistant_settings or _assistant_settings(assistant_config_path=assistant_config_path)

    from src.application.assistant.runtime import handle_assistant_turn

    kwargs["settings"] = settings
    turn = handle_assistant_turn(request, **kwargs)
    inbound_result = turn.public_payload()
    return build_response(
        tool_name="inbound.wechat_clawbot",
        ok=turn.ok,
        data={
            "kind": "message",
            "request": request.public_payload(),
            "response_text": turn.response_text,
            "inbound_result": inbound_result,
        },
        error=turn.error if not turn.ok else None,
        meta=dict(turn.meta or {}),
    )


def wechat_clawbot_message_to_assistant_request(
    payload: dict[str, Any],
    *,
    base: Path | None = None,
    label: str = DEFAULT_WECHAT_CLAWBOT_LABEL,
    state_dir: str | None = None,
    config_key: str | None = None,
    config_path: str | None = None,
    audit_db: str | None = None,
    assistant_config_path: str | None = None,
) -> AssistantRequest:
    sender_id = message_user_id(payload)
    if not sender_id:
        raise AgentToolError(code="INPUT_ERROR", message="failed to extract WeChat ClawBot sender id")
    text = message_text(payload)
    if not text:
        raise AgentToolError(code="INPUT_ERROR", message="failed to extract WeChat ClawBot text message")
    chat_key = message_chat_key(payload)
    context_token = message_context_token(payload)
    group_id = message_group_id(payload) or None
    base_path = (base or Path.cwd()).expanduser()
    resolved_label = str(label or DEFAULT_WECHAT_CLAWBOT_LABEL)
    store = _state_store(base=base_path, label=resolved_label, state_dir=state_dir)
    reply_context = {
        "provider": WECHAT_CLAWBOT_NOTIFICATION_PROVIDER,
        "base": str(base_path),
        "label": resolved_label,
        "state_dir": str(store.state_dir),
        "to_user_id": sender_id,
        "context_token": context_token,
        "group_id": group_id,
    }
    return AssistantRequest(
        text=text,
        sender_id=sender_id,
        channel="wechat",
        message_id=message_id(payload) or None,
        conversation_id=wechat_window_conversation_id(
            chat_key=chat_key,
            group_id=group_id,
            sender_id=sender_id,
        ),
        config_key=config_key,
        config_path=config_path,
        audit_db=audit_db,
        assistant_config_path=assistant_config_path,
        reply_context={key: value for key, value in reply_context.items() if value},
    )


def poll_wechat_clawbot_once(
    *,
    base: Path,
    label: str = DEFAULT_WECHAT_CLAWBOT_LABEL,
    state_dir: str | None = None,
    config_key: str | None = None,
    config_path: str | None = None,
    assistant_config_path: str | None = None,
    audit_db: str | None = None,
    allowed_senders: str | None = None,
    reply_enabled: bool = True,
    max_reply_chars: int = DEFAULT_WECHAT_REPLY_MAX_CHARS,
    keepalive_interval_sec: float = 0.0,
    timeout_sec: int = 20,
    client_factory: ClientFactory = WechatClawbotClient,
    channel_service: ChannelService | None = None,
    execute_tool_fn: ExecuteToolFn | None = None,
    model_turn_fn: Callable[..., Any] | None = None,
    generate_reply_fn: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    store = _state_store(base=base, label=label, state_dir=state_dir)
    state = _load_store_json(store.load_state, default={})
    bot_token = str(state.get("bot_token") or "").strip()
    if not bot_token:
        raise AgentToolError(code="STATE_ERROR", message="WeChat ClawBot bot_token is missing; run connect first")
    base_url = str(state.get("base_url") or DEFAULT_ILINK_BASE_URL).strip() or DEFAULT_ILINK_BASE_URL
    cursor_before = str(state.get("get_updates_buf") or "")
    client = client_factory(bot_token=bot_token, base_url=base_url, timeout=timeout_sec)
    response = client.get_updates(get_updates_buf=cursor_before)
    cursor_after = extract_first_string(response, ("get_updates_buf", "getUpdatesBuf"))
    messages = extract_messages(response)
    service = channel_service or build_wechat_clawbot_inbound_channel_service()
    binding_refresh_route = _resolve_binding_refresh_route(
        state_dir=state_dir,
        config_key=config_key,
        config_path=config_path,
    )
    results: list[dict[str, Any]] = []
    processed_count = 0
    reply_count = 0

    for message in messages:
        inbound_kwargs: dict[str, Any] = {
            "base": base,
            "label": label,
            "state_dir": state_dir,
            "config_key": config_key,
            "config_path": config_path,
            "audit_db": audit_db,
            "assistant_config_path": assistant_config_path,
            "allowed_senders": allowed_senders,
        }
        if execute_tool_fn is not None:
            inbound_kwargs["execute_tool_fn"] = execute_tool_fn
        if model_turn_fn is not None:
            inbound_kwargs["model_turn_fn"] = model_turn_fn
        if generate_reply_fn is not None:
            inbound_kwargs["generate_reply_fn"] = generate_reply_fn
        typing_status = _maybe_start_typing(message=message, client=client, allowed_senders=allowed_senders)
        binding_refresh: dict[str, Any] = {"attempted": False, "updated_count": 0, "reason": "not_started"}
        try:
            inbound = service.handle_inbound(WECHAT_CLAWBOT_NOTIFICATION_PROVIDER, message, **inbound_kwargs)
            data = _dict(inbound.get("data"))
            binding_refresh = _refresh_bound_context_from_inbound(
                base=base,
                route=binding_refresh_route,
                message=message,
                allowed_senders=allowed_senders,
            )
            reply_status = _maybe_reply(
                message=message,
                inbound=inbound,
                client=client,
                reply_enabled=reply_enabled,
                max_reply_chars=max_reply_chars,
            )
            _record_reply_receipt(inbound=inbound, audit_db=audit_db, reply_status=reply_status)
            reply_binding_refresh = _refresh_bound_context_from_reply(
                base=base,
                inbound=inbound,
                route=binding_refresh_route,
                message=message,
                reply_status=reply_status,
                allowed_senders=allowed_senders,
            )
            if bool(reply_binding_refresh.get("attempted")):
                binding_refresh = reply_binding_refresh
        finally:
            stop_status = _maybe_stop_typing(message=message, client=client, typing_status=typing_status)
            typing_status = {**typing_status, "stop": stop_status}
        if data.get("kind") == "message":
            processed_count += 1
        if bool(reply_status.get("attempted")) and bool(reply_status.get("ok")):
            reply_count += 1
        typing_public = {key: value for key, value in typing_status.items() if key != "typing_ticket"}
        results.append(
            {
                "message_id": message_id(message) or None,
                "sender_id": message_user_id(message) or None,
                "kind": data.get("kind") or "unknown",
                "ok": bool(inbound.get("ok", False)),
                "inbound": inbound,
                "reply": reply_status,
                "binding_refresh": binding_refresh,
                "typing": typing_public,
            }
        )

    keepalive_status = _maybe_keepalive_bound_context(
        store=store,
        state=state,
        client=client,
        interval_sec=keepalive_interval_sec,
    )
    keepalive_state_changed = bool(keepalive_status.pop("_state_changed", False))

    cursor_updated = False
    state_changed = False
    if cursor_after:
        state["get_updates_buf"] = cursor_after
        state["updated_at_utc"] = utc_now()
        state_changed = True
        cursor_updated = cursor_after != cursor_before
    if state_changed or keepalive_state_changed:
        store.save_state(state)

    all_ok = all(bool(item.get("ok")) and bool(_dict(item.get("reply")).get("ok", True)) for item in results)
    return build_response(
        tool_name="wechat_clawbot.poll_once",
        ok=all_ok,
        data={
            "label": label,
            "state_dir": str(store.state_dir),
            "update_count": len(messages),
            "processed_count": processed_count,
            "reply_count": reply_count,
            "cursor_before": cursor_before,
            "cursor_after": cursor_after or cursor_before,
            "cursor_updated": cursor_updated,
            "keepalive": keepalive_status,
            "results": results,
        },
        error=None if all_ok else {"code": "INBOUND_PROCESSING_FAILED", "message": "one or more WeChat ClawBot inbound messages failed"},
        meta={"response_tail": json.dumps(response, ensure_ascii=False)[-500:]},
    )


def _resolve_binding_refresh_route(
    *,
    state_dir: str | None,
    config_key: str | None,
    config_path: str | None,
) -> dict[str, Any]:
    from src.application.notification_delivery_route import resolve_notification_delivery_route

    try:
        _loaded_path, cfg = load_runtime_config(
            config_key=config_key,
            config_path=config_path,
            require_identity=False,
        )
        route = resolve_notification_delivery_route(config=cfg)
    except Exception as exc:
        return {
            "ok": False,
            "provider": None,
            "target": None,
            "notifications": {"wechat_clawbot_state_dir": state_dir} if state_dir else {},
            "reason": "notification_route_unavailable",
            "error": f"{type(exc).__name__}: {exc}",
        }
    notifications = route.get("notifications") if isinstance(route.get("notifications"), dict) else {}
    notifications = dict(notifications)
    if state_dir:
        notifications["wechat_clawbot_state_dir"] = state_dir
    provider = str(route.get("provider") or "").strip()
    target = str(route.get("target") or "").strip()
    if provider != WECHAT_CLAWBOT_NOTIFICATION_PROVIDER:
        return {
            "ok": False,
            "provider": provider or None,
            "target": target or None,
            "notifications": notifications,
            "reason": "notification_route_not_wechat_clawbot",
        }
    if not target:
        return {
            "ok": False,
            "provider": provider,
            "target": None,
            "notifications": notifications,
            "reason": "notification_route_target_missing",
        }
    return {
        "ok": True,
        "provider": provider,
        "target": target,
        "notifications": notifications,
        "reason": "notification_route_ready",
    }


def _refresh_bound_context_from_reply(
    *,
    base: Path,
    inbound: dict[str, Any],
    route: dict[str, Any],
    message: dict[str, Any],
    reply_status: dict[str, Any],
    allowed_senders: str | None,
) -> dict[str, Any]:
    if not (bool(reply_status.get("attempted")) and bool(reply_status.get("ok"))):
        return {"attempted": False, "updated_count": 0, "reason": "reply_not_successful"}
    sender_id = message_user_id(message)
    sender_decision = check_sender_allowed(channel="wechat", sender_id=sender_id or "", allowed_senders=allowed_senders)
    if not sender_decision.allowed:
        return {"attempted": False, "updated_count": 0, "reason": sender_decision.reason}
    if not bool(inbound.get("ok")):
        return {"attempted": False, "updated_count": 0, "reason": "inbound_not_successful"}
    if not bool(route.get("ok")):
        return {
            "attempted": False,
            "updated_count": 0,
            "reason": str(route.get("reason") or "notification_route_unavailable"),
            **({"error": route.get("error")} if route.get("error") else {}),
        }
    try:
        return refresh_wechat_clawbot_binding_from_reply(
            base=base,
            target=str(route.get("target") or ""),
            message=message,
            reply_status=reply_status,
            notifications=_dict(route.get("notifications")),
        )
    except Exception as exc:
        LOG.warning("failed to refresh WeChat ClawBot binding context from successful reply", exc_info=True)
        return {
            "attempted": True,
            "updated_count": 0,
            "reason": "refresh_failed",
            "error": f"{type(exc).__name__}: {exc}",
        }


def _refresh_bound_context_from_inbound(
    *,
    base: Path,
    route: dict[str, Any],
    message: dict[str, Any],
    allowed_senders: str | None,
) -> dict[str, Any]:
    sender_id = message_user_id(message)
    sender_decision = check_sender_allowed(channel="wechat", sender_id=sender_id or "", allowed_senders=allowed_senders)
    if not sender_decision.allowed:
        return {"attempted": False, "updated_count": 0, "reason": sender_decision.reason}
    if not bool(route.get("ok")):
        return {
            "attempted": False,
            "updated_count": 0,
            "reason": str(route.get("reason") or "notification_route_unavailable"),
            **({"error": route.get("error")} if route.get("error") else {}),
        }
    try:
        return refresh_wechat_clawbot_binding_from_inbound_message(
            base=base,
            target=str(route.get("target") or ""),
            message=message,
            notifications=_dict(route.get("notifications")),
        )
    except Exception as exc:
        LOG.warning("failed to refresh WeChat ClawBot binding context from inbound message", exc_info=True)
        return {
            "attempted": True,
            "updated_count": 0,
            "reason": "refresh_failed",
            "error": f"{type(exc).__name__}: {exc}",
        }


def serve_wechat_clawbot(
    settings: WechatClawbotServeSettings,
    *,
    poll_once_fn: Callable[..., dict[str, Any]] = poll_wechat_clawbot_once,
    sleep_fn: Callable[[float], Any] = time.sleep,
    lock_path: str | os.PathLike[str] | None = None,
    stop_after_batches: int | None = None,
) -> None:
    settings.validate_for_serve()
    batches = 0
    with _single_instance_lock(lock_path):
        while True:
            try:
                poll_once_fn(
                    base=settings.base,
                    label=settings.label,
                    state_dir=settings.state_dir,
                    config_key=settings.config_key,
                    config_path=settings.config_path,
                    assistant_config_path=settings.assistant_config_path,
                    audit_db=settings.audit_db,
                    allowed_senders=settings.allowed_senders,
                    reply_enabled=settings.reply_enabled,
                    max_reply_chars=settings.max_reply_chars,
                    keepalive_interval_sec=settings.keepalive_interval_sec,
                    timeout_sec=settings.timeout_sec,
                )
            except Exception:
                LOG.exception("failed to process WeChat ClawBot inbound batch")
            batches += 1
            if stop_after_batches is not None and batches >= stop_after_batches:
                return
            sleep_fn(settings.poll_interval_sec)


def _maybe_keepalive_bound_context(
    *,
    store: WechatClawbotStateStore,
    state: dict[str, Any],
    client: WechatClawbotClient,
    interval_sec: float,
) -> dict[str, Any]:
    interval = max(0.0, float(interval_sec or 0.0))
    if interval <= 0:
        return {"attempted": False, "ok": True, "reason": "disabled"}

    previous = _dict(state.get("keepalive"))
    now = datetime.now(timezone.utc)
    last_attempt = _parse_utc_datetime(previous.get("last_attempt_at_utc"))
    if last_attempt is not None and (now - last_attempt).total_seconds() < interval:
        return {
            "attempted": False,
            "ok": True,
            "reason": "throttled",
            "last_attempt_at_utc": previous.get("last_attempt_at_utc"),
            "binding_count": previous.get("binding_count"),
        }

    try:
        payload = _load_store_json(store.load_bindings, default={"bindings": {}})
        bindings = payload.get("bindings") if isinstance(payload.get("bindings"), dict) else {}
    except Exception as exc:
        return _record_keepalive_state(
            state=state,
            previous=previous,
            now=now,
            ok=False,
            reason="bindings_unavailable",
            binding_count=0,
            ok_count=0,
            failed_bindings=(),
            error=f"{type(exc).__name__}: {exc}",
        )

    targets: list[tuple[str, str, str]] = []
    for name, value in sorted(bindings.items(), key=lambda item: str(item[0])):
        binding = _dict(value)
        to_user_id = _first_text(binding.get("to_user_id"))
        context_token = _first_text(binding.get("context_token"))
        if to_user_id and context_token:
            targets.append((str(name), to_user_id, context_token))
    if not targets:
        return {"attempted": False, "ok": True, "reason": "no_bound_context", "binding_count": 0}

    ok_count = 0
    failures: list[str] = []
    last_error: str | None = None
    for name, to_user_id, context_token in targets:
        try:
            response = client.get_config(ilink_user_id=to_user_id, context_token=context_token)
            if _response_success(response):
                ok_count += 1
                continue
            failures.append(name)
            last_error = f"{name}: response={json.dumps(response, ensure_ascii=False)[-300:]}"
        except Exception as exc:
            failures.append(name)
            last_error = f"{name}: {type(exc).__name__}: {exc}"

    ok = not failures
    return _record_keepalive_state(
        state=state,
        previous=previous,
        now=now,
        ok=ok,
        reason="ok" if ok else "failed",
        binding_count=len(targets),
        ok_count=ok_count,
        failed_bindings=tuple(failures),
        error=last_error,
    )


def _record_keepalive_state(
    *,
    state: dict[str, Any],
    previous: dict[str, Any],
    now: datetime,
    ok: bool,
    reason: str,
    binding_count: int,
    ok_count: int,
    failed_bindings: tuple[str, ...],
    error: str | None,
) -> dict[str, Any]:
    now_text = now.isoformat()
    saved = dict(previous)
    saved.update(
        {
            "last_attempt_at_utc": now_text,
            "last_ok": bool(ok),
            "last_reason": reason,
            "binding_count": int(binding_count),
            "ok_count": int(ok_count),
        }
    )
    if ok:
        saved["last_success_at_utc"] = now_text
        saved.pop("last_error", None)
        saved.pop("failed_bindings", None)
    else:
        if error:
            saved["last_error"] = error
        if failed_bindings:
            saved["failed_bindings"] = list(failed_bindings)
    state["keepalive"] = saved

    out: dict[str, Any] = {
        "attempted": True,
        "ok": bool(ok),
        "reason": reason,
        "binding_count": int(binding_count),
        "ok_count": int(ok_count),
        "last_attempt_at_utc": now_text,
        "_state_changed": True,
    }
    if failed_bindings:
        out["failed_bindings"] = list(failed_bindings)
    if error:
        out["error"] = error
    return out


def _maybe_start_typing(
    *,
    message: dict[str, Any],
    client: WechatClawbotClient,
    allowed_senders: str | None,
) -> dict[str, Any]:
    to_user_id = message_user_id(message)
    context_token = message_context_token(message)
    if not to_user_id or not context_token:
        return {"attempted": False, "ok": True, "reason": "missing_context"}
    sender_decision = check_sender_allowed(channel="wechat", sender_id=to_user_id, allowed_senders=allowed_senders)
    if not sender_decision.allowed:
        return {"attempted": False, "ok": True, "reason": sender_decision.reason}
    try:
        config_response = client.get_config(ilink_user_id=to_user_id, context_token=context_token)
        typing_ticket = extract_first_string(config_response, ("typing_ticket", "typingTicket"))
        if not typing_ticket:
            return {
                "attempted": True,
                "ok": False,
                "reason": "missing_typing_ticket",
            }
        typing_response = client.send_typing(ilink_user_id=to_user_id, typing_ticket=typing_ticket, status=1)
    except Exception as exc:
        return {
            "attempted": True,
            "ok": False,
            "reason": "typing_failed",
            "error": f"{type(exc).__name__}: {exc}",
        }
    return {
        "attempted": True,
        "ok": _response_success(typing_response),
        "reason": "typing_started" if _response_success(typing_response) else "typing_failed",
        "typing_ticket": typing_ticket,
    }


def _maybe_stop_typing(
    *,
    message: dict[str, Any],
    client: WechatClawbotClient,
    typing_status: dict[str, Any],
) -> dict[str, Any]:
    if not (typing_status.get("attempted") and typing_status.get("typing_ticket")):
        return {"attempted": False, "ok": True, "reason": "not_started"}
    to_user_id = message_user_id(message)
    if not to_user_id:
        return {"attempted": False, "ok": True, "reason": "missing_user_id"}
    try:
        response = client.send_typing(
            ilink_user_id=to_user_id,
            typing_ticket=str(typing_status.get("typing_ticket") or ""),
            status=2,
        )
    except Exception as exc:
        return {
            "attempted": True,
            "ok": False,
            "reason": "typing_cancel_failed",
            "error": f"{type(exc).__name__}: {exc}",
        }
    return {
        "attempted": True,
        "ok": _response_success(response),
        "reason": "typing_cancelled" if _response_success(response) else "typing_cancel_failed",
    }


def _maybe_reply(
    *,
    message: dict[str, Any],
    inbound: dict[str, Any],
    client: WechatClawbotClient,
    reply_enabled: bool,
    max_reply_chars: int,
) -> dict[str, Any]:
    decision = decide_inbound_reply(
        inbound,
        reply_enabled=reply_enabled,
        max_reply_chars=max_reply_chars,
        permission_denied_message_fn=permission_denied_message,
    )
    if not decision.should_send:
        return decision.status
    to_user_id = message_user_id(message)
    context_token = message_context_token(message)
    if not to_user_id or not context_token:
        return {"attempted": True, "ok": False, "reason": "missing_reply_context"}
    try:
        api_response = client.send_text_message(
            to_user_id=to_user_id,
            context_token=context_token,
            text=decision.text,
            group_id=message_group_id(message) or None,
        )
    except Exception as exc:
        return {
            "attempted": True,
            "ok": False,
            "reason": "reply_failed",
            "error": f"{type(exc).__name__}: {exc}",
        }
    outbound_message_id = _extract_message_id(api_response)
    return {
        "attempted": True,
        "ok": _response_success(api_response),
        "reason": decision.send_reason if _response_success(api_response) else "reply_failed",
        "message_id": outbound_message_id,
        "outbound_message_id": outbound_message_id,
        "api_response": api_response,
    }


def _record_reply_receipt(
    *,
    inbound: dict[str, Any],
    audit_db: str | None,
    reply_status: dict[str, Any],
) -> None:
    if not bool(reply_status.get("attempted")):
        return
    data = _inbound_message_data(inbound)
    if data is None:
        return
    command_id = _inbound_command_id(_inbound_message_result(data))
    if not command_id:
        return
    receipt = _reply_receipt_payload(data=data, reply_status=reply_status)
    if not receipt:
        return
    if not bool(reply_status.get("ok")):
        LOG.warning("WeChat ClawBot reply failed: reason=%s", receipt.get("reason") or "unknown")
    try:
        InboundAuditStore(audit_db).merge_response_data(
            command_id=command_id,
            data={"reply": receipt},
        )
    except Exception:
        LOG.warning("failed to persist WeChat ClawBot reply receipt to inbound audit", exc_info=True)


def _reply_receipt_payload(*, data: dict[str, Any], reply_status: dict[str, Any]) -> dict[str, Any]:
    request = _dict(data.get("request"))
    api_response = reply_status.get("api_response")
    outbound_message_id = _first_text(
        reply_status.get("outbound_message_id"),
        reply_status.get("message_id"),
        _extract_message_id(api_response) if isinstance(api_response, dict) else None,
    )
    receipt: dict[str, Any] = {
        "schema_version": "wechat-clawbot-reply-receipt-v1",
        "attempted": bool(reply_status.get("attempted")),
        "ok": bool(reply_status.get("ok")),
        "reason": _first_text(reply_status.get("reason")),
        "channel": _first_text(request.get("channel"), "wechat"),
        "provider": WECHAT_CLAWBOT_NOTIFICATION_PROVIDER,
        "sender_id": _first_text(request.get("sender_id")),
        "inbound_message_id": _first_text(request.get("message_id")),
        "message_id": outbound_message_id,
        "outbound_message_id": outbound_message_id,
    }
    if outbound_message_id and bool(reply_status.get("ok")):
        receipt["delivery_confirmed"] = True
    error = _first_text(reply_status.get("error"))
    if error:
        receipt["error"] = error
    if isinstance(api_response, dict):
        receipt["api_response"] = api_response
    return {key: value for key, value in receipt.items() if value is not None}


def _assistant_settings(*, assistant_config_path: str | None = None) -> Any:
    from src.application.assistant.config_loader import load_assistant_config
    from src.application.assistant.settings import AssistantSettings

    explicit = bool(assistant_config_path is not None and str(assistant_config_path).strip())
    _path, assistant_cfg = load_assistant_config(config_path=assistant_config_path, missing_ok=not explicit)
    if assistant_cfg:
        configured = AssistantSettings.from_runtime_config(assistant_cfg)
        return AssistantSettings(
            enabled=configured.enabled,
            context_window_messages=configured.context_window_messages,
            default_market_scope=configured.default_market_scope,
            llm=configured.llm,
        )
    return AssistantSettings()


def _state_store(*, base: Path, label: str, state_dir: str | None = None) -> WechatClawbotStateStore:
    notifications = {"wechat_clawbot_state_dir": state_dir} if state_dir else None
    return WechatClawbotStateStore(resolve_wechat_clawbot_state_dir(base=base, label=label, notifications=notifications))


def _load_store_json(load_fn: Callable[..., dict[str, Any]], *, default: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        return load_fn(default=default)
    except ValueError as exc:
        raise AgentToolError(code="STATE_ERROR", message=str(exc)) from exc


def _response_success(response: dict[str, Any]) -> bool:
    if response == {}:
        return True
    if response.get("ok") is True:
        return True
    for key in ("ret", "errcode", "code"):
        value = response.get(key)
        if isinstance(value, int):
            return value == 0
        if isinstance(value, str) and value.strip().lstrip("-").isdigit():
            return int(value) == 0
    return False


def _extract_message_id(response: dict[str, Any]) -> str | None:
    for key in ("message_id", "messageId", "id", "client_msg_id"):
        value = response.get(key)
        if value:
            return str(value)
    data = response.get("data")
    if isinstance(data, dict):
        return _extract_message_id(data)
    result = response.get("result")
    if isinstance(result, dict):
        return _extract_message_id(result)
    return None


def _normalize_config_key(value: str | None) -> str | None:
    text = str(value or "").strip().lower()
    if not text:
        return None
    if text not in {"us", "hk"}:
        raise AgentToolError(code="CONFIG_ERROR", message="wechat_clawbot config_key must be us or hk")
    return text


def _first_text(*values: Any) -> str | None:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return None


def _load_assistant_behavior_config(*, config_path: str | None) -> dict[str, Any]:
    from src.application.assistant.config_loader import load_assistant_config

    explicit_config_path = bool(config_path is not None and str(config_path).strip())
    try:
        _path, cfg = load_assistant_config(config_path=config_path, missing_ok=not explicit_config_path)
    except AgentToolError:
        if explicit_config_path:
            raise
        return {}
    return cfg if cfg else {}


def _config_bool(explicit: bool | None, configured: Any, *, default: bool) -> bool:
    if explicit is not None:
        return bool(explicit)
    if isinstance(configured, bool):
        return configured
    if configured is None:
        return bool(default)
    value = str(configured or "").strip().lower()
    if value in {"1", "true", "yes", "y", "on"}:
        return True
    if value in {"0", "false", "no", "n", "off"}:
        return False
    return bool(default)


def _config_positive_int(explicit: int | None, configured: Any, *, default: int) -> int:
    raw = explicit if explicit is not None else configured
    if raw is None or str(raw).strip() == "":
        raw = default
    try:
        value = int(raw)
    except Exception:
        value = default
    return max(1, value)


def _config_non_negative_float(explicit: float | None, configured: Any, *, default: float) -> float:
    raw = explicit if explicit is not None else configured
    if raw is None or str(raw).strip() == "":
        raw = default
    try:
        value = float(raw)
    except Exception:
        value = default
    return max(0.0, value)


def _parse_utc_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@contextmanager
def _single_instance_lock(lock_path: str | os.PathLike[str] | None) -> Any:
    raw = str(lock_path or "").strip()
    if not raw:
        yield
        return
    path = Path(raw).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+", encoding="utf-8")
    try:
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise AgentToolError(
                code="RESOURCE_BUSY",
                message="another WeChat ClawBot inbound client is already running",
                details={"lock_path": str(path)},
            ) from exc
        yield
    finally:
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _dict(value: Any) -> dict[str, Any]:
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}
