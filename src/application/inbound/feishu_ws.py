from __future__ import annotations

import logging
import os
import queue
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, cast

from src.application.assistant.config_loader import load_assistant_config
from src.application.assistant.audit import InboundAuditStore
from src.application.assistant.settings import DEFAULT_CONTEXT_WINDOW_MESSAGES, AssistantSettings, AssistantLlmSettings
from src.application.agent_tool_contracts import AgentToolError, build_error_payload, build_response, mask_path
from domain.domain.multi_tick import FEISHU_APP_NOTIFICATION_PROVIDER
from src.application.channels.feishu import build_feishu_inbound_channel_service
from src.application.channels.service import ChannelService
from src.application.channels.reply_decision import (
    decide_inbound_reply,
    inbound_command_id as _inbound_command_id,
    inbound_error_code as _inbound_error_code,
    inbound_message_data as _inbound_message_data,
    inbound_message_result as _inbound_result,
    permission_denied_message as _permission_denied_message,
    permission_denied_should_stay_silent as _permission_denied_should_stay_silent,
)
from src.application.copilot.host_store import CopilotHostStore
from src.application.secret_resolver import (
    DEFAULT_FEISHU_BOT_APP_ID_ENV,
    DEFAULT_FEISHU_BOT_APP_SECRET_ENV,
    resolve_feishu_bot_config,
)
from src.application.settings import build_effective_env
from src.infrastructure.feishu_bot import add_message_reaction, reply_text_message
from src.infrastructure.feishu_ws_client import is_feishu_ws_sdk_available, start_feishu_ws_client


DEFAULT_FEISHU_REPLY_MAX_CHARS = 3500
DEFAULT_FEISHU_WS_QUEUE_SIZE = 100

ReplyFn = Callable[..., dict[str, Any]]
ReactionFn = Callable[..., dict[str, Any]]
StartClientFn = Callable[..., None]
SdkAvailableFn = Callable[[], bool]
ExecuteToolFn = Callable[[str, dict[str, Any]], dict[str, Any]]

LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class FeishuWsSettings:
    config_key: str | None = None
    config_path: str | None = None
    assistant_config_path: str | None = None
    audit_db: str | None = None
    allowed_senders: str | None = None
    app_id: str = ""
    app_secret: str = ""
    reply_enabled: bool = True
    reply_in_thread: bool = False
    max_reply_chars: int = DEFAULT_FEISHU_REPLY_MAX_CHARS
    ack_reaction: str = ""
    queue_size: int = DEFAULT_FEISHU_WS_QUEUE_SIZE
    assistant_enabled: bool = True
    assistant_copilot_enabled: bool = False
    assistant_context_window_messages: int = DEFAULT_CONTEXT_WINDOW_MESSAGES
    assistant_default_market_scope: str = ""
    assistant_llm: AssistantLlmSettings = field(default_factory=AssistantLlmSettings)

    def validate_for_serve(self) -> None:
        if not (self.config_path or self.config_key):
            raise AgentToolError(
                code="CONFIG_ERROR",
                message="missing inbound runtime config scope for Feishu WebSocket",
                hint="Pass --config-key us/hk, --config-path, or set assistant.default_market_scope explicitly.",
            )
        if not self.allowed_senders:
            raise AgentToolError(
                code="CONFIG_ERROR",
                message="missing inbound sender allowlist for Feishu WebSocket",
                hint="Set OM_FEISHU_BOT_USER_OPEN_ID or OM_FEISHU_BOT_ALLOWED_OPEN_IDS.",
            )
        if not (self.app_id and self.app_secret):
            raise AgentToolError(
                code="CONFIG_ERROR",
                message="missing Feishu app credentials for long-connection inbound",
                hint=f"Set {DEFAULT_FEISHU_BOT_APP_ID_ENV}/{DEFAULT_FEISHU_BOT_APP_SECRET_ENV}.",
            )

    def redacted_status(self, *, sdk_available: bool | None = None) -> dict[str, Any]:
        out: dict[str, Any] = {
            "config_key": self.config_key,
            "config_path": self.config_path,
            "assistant_config_path": self.assistant_config_path,
            "audit_db": mask_path(self.audit_db),
            "allowed_senders_configured": bool(self.allowed_senders),
            "app_id_configured": bool(self.app_id),
            "app_secret_configured": bool(self.app_secret),
            "reply_enabled": bool(self.reply_enabled),
            "reply_in_thread": bool(self.reply_in_thread),
            "max_reply_chars": int(self.max_reply_chars),
            "ack_reaction": self.ack_reaction,
            "queue_size": int(self.queue_size),
            "assistant_enabled": bool(self.assistant_enabled),
            "assistant_copilot_enabled": bool(self.assistant_copilot_enabled),
            "assistant_context_window_messages": int(self.assistant_context_window_messages),
            "assistant_default_market_scope": self.assistant_default_market_scope,
            "assistant_llm": self.assistant_llm.public_payload(),
        }
        if sdk_available is not None:
            out["sdk_available"] = bool(sdk_available)
        return out


def build_feishu_ws_settings(
    *,
    config_key: str | None = None,
    config_path: str | None = None,
    assistant_config_path: str | None = None,
    audit_db: str | None = None,
    reply_enabled: bool | None = None,
    reply_in_thread: bool | None = None,
    max_reply_chars: int | None = None,
    queue_size: int | None = None,
    environ: Mapping[str, str] | None = None,
    env_file: str | Path | None = None,
) -> FeishuWsSettings:
    env = build_effective_env(environ=environ, env_file=env_file).values
    bot_cfg = resolve_feishu_bot_config(environ=env)
    assistant_cfg = _load_assistant_behavior_config(config_path=assistant_config_path)
    behavior_cfg = _dict(_dict(assistant_cfg.get("inbound")).get("feishu_ws"))
    assistant_settings = AssistantSettings.from_runtime_config(assistant_cfg)
    default_config_key = (
        assistant_settings.default_market_scope
        if assistant_settings.default_market_scope in {"us", "hk"}
        else None
    )
    return FeishuWsSettings(
        config_key=str(config_key or default_config_key or "").strip().lower() or None,
        config_path=_first_text(config_path),
        assistant_config_path=_first_text(assistant_config_path),
        audit_db=_first_text(audit_db, env.get("OM_INBOUND_AUDIT_DB")),
        allowed_senders=bot_cfg.default_allowed_senders(),
        app_id=bot_cfg.app_id,
        app_secret=bot_cfg.app_secret,
        reply_enabled=_config_bool(reply_enabled, behavior_cfg.get("reply_enabled"), default=True),
        reply_in_thread=_config_bool(reply_in_thread, behavior_cfg.get("reply_in_thread"), default=False),
        max_reply_chars=_config_positive_int(
            max_reply_chars,
            behavior_cfg.get("max_reply_chars"),
            default=DEFAULT_FEISHU_REPLY_MAX_CHARS,
        ),
        ack_reaction=str(behavior_cfg.get("ack_reaction") or "").strip().upper(),
        queue_size=_config_positive_int(
            queue_size,
            behavior_cfg.get("queue_size"),
            default=DEFAULT_FEISHU_WS_QUEUE_SIZE,
        ),
        assistant_enabled=bool(assistant_settings.enabled),
        assistant_copilot_enabled=bool(assistant_settings.copilot.enabled),
        assistant_context_window_messages=assistant_settings.context_window_messages,
        assistant_default_market_scope=assistant_settings.default_market_scope,
        assistant_llm=assistant_settings.llm,
    )


def check_feishu_ws_settings(
    settings: FeishuWsSettings,
    *,
    sdk_available_fn: SdkAvailableFn | None = None,
) -> dict[str, Any]:
    config_ok = True
    error: dict[str, Any] | None = None
    try:
        settings.validate_for_serve()
    except AgentToolError as err:
        config_ok = False
        error = build_error_payload(err)
    sdk_available = bool((sdk_available_fn or is_feishu_ws_sdk_available)())
    if config_ok and not sdk_available:
        error = build_error_payload(
            AgentToolError(
                code="CONFIG_ERROR",
                message="Feishu WebSocket SDK is missing",
                hint="Install requirements/server.txt before running ./om inbound feishu-ws.",
            )
        )
    return build_response(
        tool_name="inbound.feishu_ws.check",
        ok=bool(config_ok and sdk_available),
        data={"settings": settings.redacted_status(sdk_available=sdk_available)},
        error=error,
    )


def handle_feishu_ws_event(
    payload: dict[str, Any],
    *,
    settings: FeishuWsSettings,
    reply_fn: ReplyFn = reply_text_message,
    reaction_fn: ReactionFn = add_message_reaction,
    channel_service: ChannelService | None = None,
    execute_tool_fn: ExecuteToolFn | None = None,
) -> dict[str, Any]:
    outbox_retry = _retry_pending_feishu_reply(settings=settings, reply_fn=reply_fn)
    inbound_kwargs: dict[str, Any] = {"allowed_senders": settings.allowed_senders}
    if execute_tool_fn is not None:
        inbound_kwargs["execute_tool_fn"] = execute_tool_fn
    service = channel_service or build_feishu_inbound_channel_service()
    inbound = service.handle_inbound(
        FEISHU_APP_NOTIFICATION_PROVIDER,
        payload,
        config_key=settings.config_key,
        config_path=settings.config_path,
        audit_db=settings.audit_db,
        assistant_config_path=settings.assistant_config_path,
        **inbound_kwargs,
    )
    reaction_status = _maybe_react(inbound=inbound, settings=settings, reaction_fn=reaction_fn)
    reply_status = _maybe_reply(inbound=inbound, settings=settings, reply_fn=reply_fn)
    _record_reply_receipt(inbound=inbound, settings=settings, reply_status=reply_status)
    return build_response(
        tool_name="inbound.feishu_ws",
        ok=bool(inbound.get("ok", False)) and bool(reply_status.get("ok", True)),
        data={
            "event": _event_summary(payload),
            "inbound": inbound,
            "reaction": reaction_status,
            "reply": reply_status,
            "outbox_retry": outbox_retry,
        },
        error=inbound.get("error") if not bool(inbound.get("ok", False)) else None,
    )


def serve_feishu_ws(
    settings: FeishuWsSettings,
    *,
    reply_fn: ReplyFn = reply_text_message,
    reaction_fn: ReactionFn = add_message_reaction,
    execute_tool_fn: ExecuteToolFn | None = None,
    start_client_fn: StartClientFn = start_feishu_ws_client,
    lock_path: str | os.PathLike[str] | None = None,
) -> None:
    settings.validate_for_serve()
    with _single_instance_lock(lock_path):
        worker = _FeishuWsWorker(
            settings=settings,
            reply_fn=reply_fn,
            reaction_fn=reaction_fn,
            execute_tool_fn=execute_tool_fn,
        )
        worker.start()
        try:
            start_client_fn(
                app_id=settings.app_id,
                app_secret=settings.app_secret,
                on_event=worker.submit,
            )
        finally:
            worker.stop()


class _FeishuWsWorker:
    def __init__(
        self,
        *,
        settings: FeishuWsSettings,
        reply_fn: ReplyFn,
        reaction_fn: ReactionFn,
        execute_tool_fn: ExecuteToolFn | None,
    ) -> None:
        self._settings = settings
        self._reply_fn = reply_fn
        self._reaction_fn = reaction_fn
        self._execute_tool_fn = execute_tool_fn
        self._queue: queue.Queue[dict[str, Any] | None] = queue.Queue(maxsize=max(1, int(settings.queue_size)))
        self._thread = threading.Thread(target=self._run, name="om-feishu-ws-worker", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        self._thread.join(timeout=5)

    def submit(self, payload: dict[str, Any]) -> None:
        self._queue.put_nowait(dict(payload))

    def _run(self) -> None:
        while True:
            payload = self._queue.get()
            if payload is None:
                return
            try:
                handle_feishu_ws_event(
                    payload,
                    settings=self._settings,
                    reply_fn=self._reply_fn,
                    reaction_fn=self._reaction_fn,
                    execute_tool_fn=self._execute_tool_fn,
                )
            except Exception:
                LOG.exception("failed to process Feishu WebSocket event")


def _maybe_react(
    *,
    inbound: dict[str, Any],
    settings: FeishuWsSettings,
    reaction_fn: ReactionFn,
) -> dict[str, Any]:
    data = _inbound_message_data(inbound)
    if data is None:
        return {"attempted": False, "ok": True, "reason": "not_message"}

    inbound_result = _inbound_result(data)
    if _inbound_error_code(inbound_result) == "PERMISSION_DENIED":
        if _permission_denied_should_stay_silent(inbound_result):
            return {"attempted": False, "ok": True, "reason": "permission_denied"}

    emoji_type = str(settings.ack_reaction or "").strip().upper()
    if not emoji_type:
        return {"attempted": False, "ok": True, "reason": "reaction_disabled"}
    if not (settings.app_id and settings.app_secret):
        return {"attempted": True, "ok": False, "reason": "missing_app_credentials"}

    message_id = _message_id_from_inbound_data(data)
    if not message_id:
        return {"attempted": True, "ok": False, "reason": "missing_message_id"}

    try:
        api_response = reaction_fn(
            app_id=settings.app_id,
            app_secret=settings.app_secret,
            message_id=message_id,
            emoji_type=emoji_type,
        )
    except Exception as exc:
        return {
            "attempted": True,
            "ok": False,
            "reason": "reaction_failed",
            "message_id": message_id,
            "emoji_type": emoji_type,
            "error": f"{type(exc).__name__}: {exc}",
        }
    return {
        "attempted": True,
        "ok": True,
        "reason": "sent",
        "message_id": message_id,
        "emoji_type": emoji_type,
        "api_response": api_response,
    }


def _maybe_reply(
    *,
    inbound: dict[str, Any],
    settings: FeishuWsSettings,
    reply_fn: ReplyFn,
) -> dict[str, Any]:
    decision = decide_inbound_reply(
        inbound,
        reply_enabled=settings.reply_enabled,
        max_reply_chars=settings.max_reply_chars,
        permission_denied_message_fn=_permission_denied_message,
    )
    if not decision.should_send:
        return decision.status
    if not (settings.app_id and settings.app_secret):
        return {"attempted": True, "ok": False, "reason": "missing_app_credentials"}

    message_id = _message_id_from_inbound_data(decision.data)
    if not message_id:
        return {"attempted": True, "ok": False, "reason": "missing_message_id"}
    command_id = _inbound_command_id(decision.inbound_result)
    outbox: CopilotHostStore | None = None
    delivery_key: str | None = None
    if str(settings.audit_db or "").strip() and command_id:
        outbox = CopilotHostStore(str(settings.audit_db))
        delivery_key = f"feishu:{command_id}"
        record = outbox.enqueue_reply(
            delivery_key=delivery_key,
            channel="feishu",
            payload={
                "message_id": message_id,
                "text": decision.text,
                "reply_in_thread": settings.reply_in_thread,
            },
        )
        if str(record.get("status") or "") == "delivered":
            return {
                "attempted": False,
                "ok": True,
                "reason": "idempotent_replay",
                "delivery_key": delivery_key,
            }
        if outbox.claim_reply(delivery_key=delivery_key) is None:
            status = str(record.get("status") or "")
            return {
                "attempted": False,
                "ok": status != "terminal_failed",
                "reason": "reply_pending" if status != "terminal_failed" else "reply_terminal_failed",
                "delivery_key": delivery_key,
            }
    try:
        api_response = reply_fn(
            app_id=settings.app_id,
            app_secret=settings.app_secret,
            message_id=message_id,
            text=decision.text,
            uuid=delivery_key or command_id,
            reply_in_thread=settings.reply_in_thread,
        )
    except Exception as exc:
        if outbox is not None and delivery_key:
            outbox.mark_reply_failed(delivery_key, error=f"{type(exc).__name__}: {exc}", retryable=True)
        return {
            "attempted": True,
            "ok": False,
            "reason": "reply_failed",
            "error": f"{type(exc).__name__}: {exc}",
        }
    if not _reply_api_success(api_response):
        if outbox is not None and delivery_key:
            outbox.mark_reply_failed(
                delivery_key,
                error="channel returned an unsuccessful response",
                retryable=True,
            )
        return {
            "attempted": True,
            "ok": False,
            "reason": "reply_failed",
            "api_response": api_response,
            **({"delivery_key": delivery_key} if delivery_key else {}),
        }
    outbound_message_id = _reply_api_message_id(api_response)
    if outbox is not None and delivery_key:
        outbox.mark_reply_delivered(delivery_key)
    return {
        "attempted": True,
        "ok": True,
        "reason": decision.send_reason,
        "message_id": message_id,
        "outbound_message_id": outbound_message_id,
        "api_response": api_response,
        **({"delivery_key": delivery_key} if delivery_key else {}),
    }


def _retry_pending_feishu_reply(*, settings: FeishuWsSettings, reply_fn: ReplyFn) -> dict[str, Any]:
    if not str(settings.audit_db or "").strip():
        return {"attempted": False, "reason": "outbox_disabled"}
    store = CopilotHostStore(str(settings.audit_db))
    record = store.claim_reply(channel="feishu")
    if record is None:
        return {"attempted": False, "reason": "outbox_empty"}
    delivery_key = str(record.get("delivery_key") or "")
    try:
        import json

        payload = json.loads(str(record.get("payload_json") or "{}"))
        api_response = reply_fn(
            app_id=settings.app_id,
            app_secret=settings.app_secret,
            message_id=str(payload.get("message_id") or ""),
            text=str(payload.get("text") or ""),
            uuid=delivery_key,
            reply_in_thread=bool(payload.get("reply_in_thread")),
        )
    except Exception as exc:
        store.mark_reply_failed(delivery_key, error=f"{type(exc).__name__}: {exc}", retryable=True)
        return {"attempted": True, "ok": False, "reason": "reply_failed", "delivery_key": delivery_key}
    if not _reply_api_success(api_response):
        store.mark_reply_failed(
            delivery_key,
            error="channel returned an unsuccessful response",
            retryable=True,
        )
        return {"attempted": True, "ok": False, "reason": "reply_failed", "delivery_key": delivery_key}
    store.mark_reply_delivered(delivery_key)
    return {
        "attempted": True,
        "ok": True,
        "reason": "sent",
        "delivery_key": delivery_key,
        "outbound_message_id": _reply_api_message_id(api_response),
    }


def _record_reply_receipt(
    *,
    inbound: dict[str, Any],
    settings: FeishuWsSettings,
    reply_status: dict[str, Any],
) -> None:
    if not bool(reply_status.get("attempted")):
        return
    data = _inbound_message_data(inbound)
    if data is None:
        return
    command_id = _inbound_command_id(_inbound_result(data))
    if not command_id:
        return
    receipt = _reply_receipt_payload(data=data, reply_status=reply_status)
    if not receipt:
        return
    try:
        InboundAuditStore(settings.audit_db).merge_response_data(
            command_id=command_id,
            data={"reply": receipt},
        )
    except Exception:
        LOG.warning("failed to persist Feishu WebSocket reply receipt to inbound audit", exc_info=True)


def _reply_receipt_payload(*, data: dict[str, Any], reply_status: dict[str, Any]) -> dict[str, Any]:
    request = _dict(data.get("request"))
    api_response = reply_status.get("api_response")
    outbound_message_id = _first_text(reply_status.get("outbound_message_id"), _reply_api_message_id(api_response))
    receipt: dict[str, Any] = {
        "schema_version": "feishu-reply-receipt-v1",
        "attempted": bool(reply_status.get("attempted")),
        "ok": bool(reply_status.get("ok")),
        "reason": _first_text(reply_status.get("reason")),
        "channel": _first_text(request.get("channel"), "feishu"),
        "provider": FEISHU_APP_NOTIFICATION_PROVIDER,
        "sender_id": _first_text(request.get("sender_id")),
        "inbound_message_id": _first_text(reply_status.get("message_id"), request.get("message_id")),
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


def _reply_api_message_id(api_response: Any) -> str | None:
    response = _dict(api_response)
    data = _dict(response.get("data"))
    result = _dict(response.get("result"))
    return _first_text(
        response.get("message_id"),
        response.get("messageId"),
        response.get("id"),
        response.get("client_msg_id"),
        data.get("message_id"),
        data.get("messageId"),
        data.get("id"),
        data.get("client_msg_id"),
        result.get("message_id"),
        result.get("messageId"),
        result.get("id"),
        result.get("client_msg_id"),
    )


def _reply_api_success(api_response: Any) -> bool:
    response = _dict(api_response)
    code = response.get("code")
    return not isinstance(code, int) or code == 0


def _message_id_from_inbound_data(data: dict[str, Any]) -> str | None:
    request_raw = data.get("request")
    request = cast(dict[str, Any], request_raw) if isinstance(request_raw, dict) else {}
    return _first_text(request.get("message_id"))


def _event_summary(payload: dict[str, Any]) -> dict[str, Any]:
    header = _dict(payload.get("header"))
    event = _dict(payload.get("event"))
    message = _dict(event.get("message"))
    return {
        "event_id": _first_text(header.get("event_id")),
        "event_type": _first_text(header.get("event_type"), event.get("type")),
        "message_id": _first_text(message.get("message_id")),
    }


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _load_assistant_behavior_config(*, config_path: str | None) -> dict[str, Any]:
    explicit_config_path = bool(config_path is not None and str(config_path).strip())
    try:
        _path, cfg = load_assistant_config(config_path=config_path, missing_ok=not explicit_config_path)
    except AgentToolError:
        if explicit_config_path:
            raise
        return {}
    if cfg:
        return cfg
    return cfg


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


def _first_text(*values: Any) -> str | None:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return None


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
                message="another Feishu WebSocket inbound client is already running",
                details={"lock_path": str(path)},
            ) from exc
        yield
    finally:
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
