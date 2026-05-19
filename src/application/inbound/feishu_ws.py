from __future__ import annotations

import logging
import os
import queue
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, cast

from src.application.agent_tool_contracts import AgentToolError, build_error_payload, build_response, mask_path
from src.application.inbound.feishu import handle_feishu_payload
from src.application.inbound.router import ExecuteToolFn
from src.application.secret_resolver import (
    DEFAULT_FEISHU_BOT_APP_ID_ENV,
    DEFAULT_FEISHU_BOT_APP_SECRET_ENV,
    resolve_feishu_bot_config,
)
from src.infrastructure.feishu_bot import add_message_reaction, reply_text_message
from src.infrastructure.feishu_ws_client import is_feishu_ws_sdk_available, start_feishu_ws_client


DEFAULT_FEISHU_REPLY_MAX_CHARS = 3500
DEFAULT_FEISHU_WS_QUEUE_SIZE = 100

ReplyFn = Callable[..., dict[str, Any]]
ReactionFn = Callable[..., dict[str, Any]]
StartClientFn = Callable[..., None]
SdkAvailableFn = Callable[[], bool]

LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class FeishuWsSettings:
    config_key: str | None = "us"
    config_path: str | None = None
    audit_db: str | None = None
    allowed_senders: str | None = None
    app_id: str = ""
    app_secret: str = ""
    reply_enabled: bool = True
    reply_in_thread: bool = False
    max_reply_chars: int = DEFAULT_FEISHU_REPLY_MAX_CHARS
    ack_reaction: str = ""
    queue_size: int = DEFAULT_FEISHU_WS_QUEUE_SIZE

    def validate_for_serve(self) -> None:
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
            "audit_db": mask_path(self.audit_db),
            "allowed_senders_configured": bool(self.allowed_senders),
            "app_id_configured": bool(self.app_id),
            "app_secret_configured": bool(self.app_secret),
            "reply_enabled": bool(self.reply_enabled),
            "reply_in_thread": bool(self.reply_in_thread),
            "max_reply_chars": int(self.max_reply_chars),
            "ack_reaction": self.ack_reaction,
            "queue_size": int(self.queue_size),
        }
        if sdk_available is not None:
            out["sdk_available"] = bool(sdk_available)
        return out


def build_feishu_ws_settings(
    *,
    config_key: str | None = "us",
    config_path: str | None = None,
    audit_db: str | None = None,
    reply_enabled: bool = True,
    reply_in_thread: bool | None = None,
    max_reply_chars: int | None = None,
    queue_size: int | None = None,
    environ: Mapping[str, str] | None = None,
) -> FeishuWsSettings:
    env = environ if environ is not None else os.environ
    bot_cfg = resolve_feishu_bot_config(environ=env)
    return FeishuWsSettings(
        config_key=str(config_key or "").strip().lower() or None,
        config_path=_first_text(config_path),
        audit_db=_first_text(audit_db, env.get("OM_INBOUND_AUDIT_DB")),
        allowed_senders=bot_cfg.default_allowed_senders(),
        app_id=bot_cfg.app_id,
        app_secret=bot_cfg.app_secret,
        reply_enabled=bool(reply_enabled),
        reply_in_thread=bool(reply_in_thread) if reply_in_thread is not None else _truthy(env.get("OM_FEISHU_REPLY_IN_THREAD")),
        max_reply_chars=int(max_reply_chars or _int_env(env, "OM_FEISHU_REPLY_MAX_CHARS", DEFAULT_FEISHU_REPLY_MAX_CHARS)),
        ack_reaction=str(env.get("OM_FEISHU_ACK_REACTION") or "").strip().upper(),
        queue_size=max(1, int(queue_size or _int_env(env, "OM_FEISHU_WS_QUEUE_SIZE", DEFAULT_FEISHU_WS_QUEUE_SIZE))),
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
    execute_tool_fn: ExecuteToolFn | None = None,
) -> dict[str, Any]:
    inbound_kwargs: dict[str, Any] = {"allowed_senders": settings.allowed_senders}
    if execute_tool_fn is not None:
        inbound_kwargs["execute_tool_fn"] = execute_tool_fn
    inbound = handle_feishu_payload(
        payload,
        config_key=settings.config_key,
        config_path=settings.config_path,
        audit_db=settings.audit_db,
        **inbound_kwargs,
    )
    reaction_status = _maybe_react(inbound=inbound, settings=settings, reaction_fn=reaction_fn)
    reply_status = _maybe_reply(inbound=inbound, settings=settings, reply_fn=reply_fn)
    return build_response(
        tool_name="inbound.feishu_ws",
        ok=bool(inbound.get("ok", False)) and bool(reply_status.get("ok", True)),
        data={
            "event": _event_summary(payload),
            "inbound": inbound,
            "reaction": reaction_status,
            "reply": reply_status,
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
    data = _inbound_message_data(inbound)
    if data is None:
        return {"attempted": False, "ok": True, "reason": "not_message"}

    inbound_result = _inbound_result(data)
    if _inbound_error_code(inbound_result) == "PERMISSION_DENIED":
        return {"attempted": False, "ok": True, "reason": "permission_denied"}
    if not settings.reply_enabled:
        return {"attempted": False, "ok": True, "reason": "reply_disabled"}

    response_text = _trim_reply(str(data.get("response_text") or ""), max_chars=settings.max_reply_chars)
    if not response_text:
        return {"attempted": False, "ok": True, "reason": "empty_response"}
    if not (settings.app_id and settings.app_secret):
        return {"attempted": True, "ok": False, "reason": "missing_app_credentials"}

    message_id = _message_id_from_inbound_data(data)
    if not message_id:
        return {"attempted": True, "ok": False, "reason": "missing_message_id"}
    command_id = _inbound_command_id(inbound_result)
    try:
        api_response = reply_fn(
            app_id=settings.app_id,
            app_secret=settings.app_secret,
            message_id=message_id,
            text=response_text,
            uuid=command_id,
            reply_in_thread=settings.reply_in_thread,
        )
    except Exception as exc:
        return {
            "attempted": True,
            "ok": False,
            "reason": "reply_failed",
            "error": f"{type(exc).__name__}: {exc}",
        }
    return {
        "attempted": True,
        "ok": True,
        "reason": "sent",
        "message_id": message_id,
        "api_response": api_response,
    }


def _inbound_message_data(inbound: dict[str, Any]) -> dict[str, Any] | None:
    data_raw = inbound.get("data")
    data = cast(dict[str, Any], data_raw) if isinstance(data_raw, dict) else {}
    return data if data.get("kind") == "message" else None


def _inbound_result(data: dict[str, Any]) -> dict[str, Any]:
    inbound_result_raw = data.get("inbound_result")
    return cast(dict[str, Any], inbound_result_raw) if isinstance(inbound_result_raw, dict) else {}


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


def _inbound_error_code(inbound_result: dict[str, Any]) -> str | None:
    error_raw = inbound_result.get("error")
    error = cast(dict[str, Any], error_raw) if isinstance(error_raw, dict) else {}
    return _first_text(error.get("code"))


def _inbound_command_id(inbound_result: dict[str, Any]) -> str | None:
    data_raw = inbound_result.get("data")
    data = cast(dict[str, Any], data_raw) if isinstance(data_raw, dict) else {}
    return _first_text(data.get("command_id"))


def _trim_reply(value: str, *, max_chars: int) -> str:
    text = str(value or "").strip()
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 20)].rstrip() + "\n...(truncated)"


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _first_text(*values: Any) -> str | None:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return None


def _int_env(env: Mapping[str, str], name: str, default: int) -> int:
    try:
        return int(str(env.get(name) or "").strip() or default)
    except Exception:
        return default


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


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
