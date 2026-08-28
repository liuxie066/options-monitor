from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable

from src.application.agent_tool_contracts import AgentToolError, build_response
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
    response_message_id,
)
from src.application.channels.wechat_clawbot.state import (
    DEFAULT_WECHAT_CLAWBOT_LABEL,
    resolve_wechat_clawbot_target,
    resolve_wechat_clawbot_state_dir,
)
from src.application.channels.wechat_clawbot.state_store import WechatClawbotStateStore
from src.infrastructure.io_utils import utc_now


ClientFactory = Callable[..., WechatClawbotClient]
ProgressFn = Callable[[dict[str, Any]], None]
SleepFn = Callable[[float], None]


def _state_dir(*, base: Path, label: str, state_dir: str | None = None) -> Path:
    notifications = {"wechat_clawbot_state_dir": state_dir} if state_dir else None
    return resolve_wechat_clawbot_state_dir(base=base, label=label, notifications=notifications)


def _state_store(*, base: Path, label: str, state_dir: str | None = None) -> WechatClawbotStateStore:
    return WechatClawbotStateStore(_state_dir(base=base, label=label, state_dir=state_dir))


def _load_store_json(load_fn: Callable[..., dict[str, Any]], *, default: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        return load_fn(default=default)
    except ValueError as exc:
        raise AgentToolError(code="STATE_ERROR", message=str(exc)) from exc


def start_wechat_clawbot_qrcode(
    *,
    base: Path,
    label: str = DEFAULT_WECHAT_CLAWBOT_LABEL,
    state_dir: str | None = None,
    base_url: str = DEFAULT_ILINK_BASE_URL,
    timeout_sec: int = 20,
    client_factory: ClientFactory = WechatClawbotClient,
) -> dict[str, Any]:
    store = _state_store(base=base, label=label, state_dir=state_dir)
    client = client_factory(bot_token=None, base_url=base_url, timeout=timeout_sec)
    response = client.get_bot_qrcode(bot_type=3)
    qrcode = extract_first_string(response, ("qrcode", "qr_code", "qrCode", "qrcode_url", "qr_url", "url"))
    qrcode_artifact_path = store.write_qrcode_artifact(
        extract_first_string(response, ("qrcode_img_content", "qrcodeImgContent", "qrcode_url", "qrcodeUrl", "qr_url", "qrUrl", "url")),
    )
    qrcode_artifact_open_command = _open_artifact_command(qrcode_artifact_path)
    payload = {
        "label": label,
        "state_dir": str(store.state_dir),
        "base_url": str(base_url or DEFAULT_ILINK_BASE_URL),
        "qrcode": qrcode,
        "qrcode_artifact_path": qrcode_artifact_path,
        "qrcode_artifact_open_command": qrcode_artifact_open_command,
        "created_at_utc": utc_now(),
    }
    store.save_pending_login(payload)
    return build_response(
        tool_name="wechat_clawbot.qrcode",
        ok=bool(qrcode),
        data=payload,
        warnings=[] if qrcode else ["qrcode not found in iLink response"],
        error=None if qrcode else {"code": "UPSTREAM_RESPONSE_ERROR", "message": "qrcode not found in iLink response"},
    )


def check_wechat_clawbot_qrcode(
    *,
    base: Path,
    label: str = DEFAULT_WECHAT_CLAWBOT_LABEL,
    qrcode: str | None = None,
    state_dir: str | None = None,
    base_url: str | None = None,
    timeout_sec: int = 20,
    client_factory: ClientFactory = WechatClawbotClient,
) -> dict[str, Any]:
    store = _state_store(base=base, label=label, state_dir=state_dir)
    pending = _load_store_json(store.load_pending_login, default={})
    resolved_qrcode = str(qrcode or pending.get("qrcode") or "").strip()
    if not resolved_qrcode:
        raise AgentToolError(code="INPUT_ERROR", message="qrcode is required; run qrcode first or pass --qrcode")
    resolved_base_url = str(base_url or pending.get("base_url") or DEFAULT_ILINK_BASE_URL)
    client = client_factory(bot_token=None, base_url=resolved_base_url, timeout=timeout_sec)
    response = client.get_qrcode_status(qrcode=resolved_qrcode)
    bot_token = extract_first_string(response, ("bot_token", "botToken", "token", "access_token", "accessToken"))
    status = extract_first_string(response, ("status", "state", "qrcode_status", "qrcodeStatus", "message", "msg"))
    ok = bool(bot_token)
    state_payload: dict[str, Any] | None = None
    if ok:
        state_payload = {
            "label": label,
            "base_url": resolved_base_url,
            "bot_token": bot_token,
            "get_updates_buf": extract_first_string(response, ("get_updates_buf", "getUpdatesBuf")),
            "login_status": status,
            "updated_at_utc": utc_now(),
        }
        store.save_state(state_payload)
    return build_response(
        tool_name="wechat_clawbot.qr_status",
        ok=True,
        data={
            "label": label,
            "state_dir": str(store.state_dir),
            "qrcode": resolved_qrcode,
            "status": status,
            "bound": ok,
            "state_path": str(store.state_path) if ok else None,
        },
        warnings=[] if ok else ["QR login is not confirmed yet or token was not present in response"],
        meta={"token_present": ok},
    )


def bind_wechat_clawbot_target(
    *,
    base: Path,
    name: str,
    label: str = DEFAULT_WECHAT_CLAWBOT_LABEL,
    match_text: str | None = None,
    from_user_id: str | None = None,
    state_dir: str | None = None,
    timeout_sec: int = 20,
    client_factory: ClientFactory = WechatClawbotClient,
) -> dict[str, Any]:
    binding_name = str(name or "").strip()
    if not binding_name:
        raise AgentToolError(code="INPUT_ERROR", message="binding name is required")
    store = _state_store(base=base, label=label, state_dir=state_dir)
    state = _load_store_json(store.load_state, default={})
    bot_token = str(state.get("bot_token") or "").strip()
    if not bot_token:
        raise AgentToolError(code="STATE_ERROR", message="WeChat ClawBot bot_token is missing; run qrcode and qr-status first")
    client = client_factory(
        bot_token=bot_token,
        base_url=str(state.get("base_url") or DEFAULT_ILINK_BASE_URL),
        timeout=timeout_sec,
    )
    response = client.get_updates(get_updates_buf=str(state.get("get_updates_buf") or ""))
    new_buf = extract_first_string(response, ("get_updates_buf", "getUpdatesBuf"))

    expected_text = str(match_text or "").strip()
    expected_user_id = str(from_user_id or "").strip()
    selected: dict[str, Any] | None = None
    messages = extract_messages(response)
    for message in messages:
        user_id = message_user_id(message)
        context_token = message_context_token(message)
        text = message_text(message)
        if not user_id or not context_token:
            continue
        if expected_user_id and user_id != expected_user_id:
            continue
        if expected_text and expected_text not in text:
            continue
        selected = message
        break

    if selected is None:
        return build_response(
            tool_name="wechat_clawbot.bind",
            ok=False,
            error={
                "code": "BINDING_MESSAGE_NOT_FOUND",
                "message": "no bindable WeChat ClawBot message found in getupdates response",
                "details": {"match_text": expected_text or None, "from_user_id": expected_user_id or None},
            },
            data={"label": label, "state_dir": str(store.state_dir), "binding_name": binding_name, "candidate_count": len(messages)},
        )

    binding = {
        "to_user_id": message_user_id(selected),
        "context_token": message_context_token(selected),
        "group_id": message_group_id(selected) or None,
        "chat_key": message_chat_key(selected) or None,
        "last_message_id": message_id(selected) or None,
        "last_text": message_text(selected)[:500],
        "updated_at_utc": utc_now(),
    }
    bindings_payload = _load_store_json(store.load_bindings, default={"bindings": {}})
    bindings = bindings_payload.get("bindings") if isinstance(bindings_payload.get("bindings"), dict) else {}
    bindings[str(binding_name)] = binding
    bindings_payload["bindings"] = bindings
    bindings_payload["updated_at_utc"] = binding["updated_at_utc"]
    store.save_bindings(bindings_payload)
    if new_buf:
        state["get_updates_buf"] = new_buf
        state["updated_at_utc"] = binding["updated_at_utc"]
        store.save_state(state)
    return build_response(
        tool_name="wechat_clawbot.bind",
        ok=True,
        data={
            "label": label,
            "state_dir": str(store.state_dir),
            "binding_name": binding_name,
            "target": f"wechat:{label}:{binding_name}",
            "binding": {k: v for k, v in binding.items() if k not in {"context_token"}},
            "bindings_path": str(store.bindings_path),
        },
    )


def refresh_wechat_clawbot_bindings_from_message(
    *,
    base: Path,
    label: str = DEFAULT_WECHAT_CLAWBOT_LABEL,
    message: dict[str, Any],
    state_dir: str | None = None,
) -> dict[str, Any]:
    user_id = message_user_id(message)
    context_token = message_context_token(message)
    if not user_id or not context_token:
        return {"attempted": False, "updated_count": 0, "reason": "missing_context"}

    store = _state_store(base=base, label=label, state_dir=state_dir)
    bindings_payload = _load_store_json(store.load_bindings, default={"bindings": {}})
    bindings = bindings_payload.get("bindings") if isinstance(bindings_payload.get("bindings"), dict) else {}
    if not bindings:
        return {"attempted": True, "updated_count": 0, "reason": "no_bindings"}

    group_id = message_group_id(message) or None
    chat_key = message_chat_key(message) or None
    now = utc_now()
    updated_names: list[str] = []
    next_bindings = dict(bindings)
    for name, raw_binding in bindings.items():
        if not isinstance(raw_binding, dict):
            continue
        if not _binding_matches_message(raw_binding, user_id=user_id, group_id=group_id, chat_key=chat_key):
            continue
        refreshed = {
            **raw_binding,
            "to_user_id": user_id,
            "context_token": context_token,
            "group_id": group_id,
            "chat_key": chat_key,
            "last_message_id": message_id(message) or raw_binding.get("last_message_id"),
            "last_text": message_text(message)[:500],
            "updated_at_utc": now,
            "refreshed_from_inbound_at_utc": now,
        }
        if refreshed != raw_binding:
            next_bindings[str(name)] = refreshed
            updated_names.append(str(name))

    if not updated_names:
        return {"attempted": True, "updated_count": 0, "reason": "no_matching_binding"}

    bindings_payload["bindings"] = next_bindings
    bindings_payload["updated_at_utc"] = now
    store.save_bindings(bindings_payload)
    return {
        "attempted": True,
        "updated_count": len(updated_names),
        "updated_bindings": updated_names,
        "reason": "refreshed",
    }


def refresh_wechat_clawbot_binding_from_inbound_message(
    *,
    base: Path,
    target: str,
    message: dict[str, Any],
    notifications: dict[str, Any] | None = None,
) -> dict[str, Any]:
    user_id = message_user_id(message)
    context_token = message_context_token(message)
    if not user_id or not context_token:
        return {"attempted": False, "updated_count": 0, "reason": "missing_context"}

    resolved = resolve_wechat_clawbot_target(target, notifications=notifications)
    state_dir = resolve_wechat_clawbot_state_dir(base=base, label=resolved.label, notifications=notifications)
    store = WechatClawbotStateStore(state_dir)
    bindings_payload = _load_store_json(store.load_bindings, default={"bindings": {}})
    bindings = bindings_payload.get("bindings") if isinstance(bindings_payload.get("bindings"), dict) else {}
    raw_binding = bindings.get(resolved.binding_name)
    if not isinstance(raw_binding, dict):
        return {
            "attempted": True,
            "updated_count": 0,
            "target": resolved.raw,
            "reason": "binding_not_found",
        }

    now = utc_now()
    inbound_message_id = message_id(message) or None
    refreshed = {
        **raw_binding,
        "to_user_id": user_id,
        "context_token": context_token,
        "group_id": message_group_id(message) or None,
        "chat_key": message_chat_key(message) or None,
        "last_message_id": inbound_message_id or raw_binding.get("last_message_id"),
        "last_text": message_text(message)[:500],
        "updated_at_utc": now,
        "refreshed_from_inbound_at_utc": now,
        "last_inbound_message_id": inbound_message_id,
    }
    if refreshed == raw_binding:
        return {
            "attempted": True,
            "updated_count": 0,
            "target": resolved.raw,
            "reason": "unchanged",
        }

    next_bindings = dict(bindings)
    next_bindings[resolved.binding_name] = refreshed
    bindings_payload["bindings"] = next_bindings
    bindings_payload["updated_at_utc"] = now
    store.save_bindings(bindings_payload)
    return {
        "attempted": True,
        "updated_count": 1,
        "updated_bindings": [resolved.binding_name],
        "target": resolved.raw,
        "reason": "refreshed_from_inbound",
    }


def refresh_wechat_clawbot_binding_from_reply(
    *,
    base: Path,
    target: str,
    message: dict[str, Any],
    reply_status: dict[str, Any],
    notifications: dict[str, Any] | None = None,
) -> dict[str, Any]:
    user_id = message_user_id(message)
    context_token = message_context_token(message)
    if not user_id or not context_token:
        return {"attempted": False, "updated_count": 0, "reason": "missing_context"}
    if not (bool(reply_status.get("attempted")) and bool(reply_status.get("ok"))):
        return {"attempted": False, "updated_count": 0, "reason": "reply_not_successful"}

    resolved = resolve_wechat_clawbot_target(target, notifications=notifications)
    state_dir = resolve_wechat_clawbot_state_dir(base=base, label=resolved.label, notifications=notifications)
    store = WechatClawbotStateStore(state_dir)
    bindings_payload = _load_store_json(store.load_bindings, default={"bindings": {}})
    bindings = bindings_payload.get("bindings") if isinstance(bindings_payload.get("bindings"), dict) else {}
    raw_binding = bindings.get(resolved.binding_name)
    if not isinstance(raw_binding, dict):
        return {
            "attempted": True,
            "updated_count": 0,
            "target": resolved.raw,
            "reason": "binding_not_found",
        }

    now = utc_now()
    inbound_message_id = message_id(message) or None
    refreshed = {
        **raw_binding,
        "to_user_id": user_id,
        "context_token": context_token,
        "group_id": message_group_id(message) or None,
        "chat_key": message_chat_key(message) or None,
        "last_message_id": inbound_message_id or raw_binding.get("last_message_id"),
        "last_text": message_text(message)[:500],
        "updated_at_utc": now,
        "refreshed_from_reply_at_utc": now,
        "last_inbound_message_id": inbound_message_id,
        "reply_message_id": _reply_message_id(reply_status),
    }
    if refreshed == raw_binding:
        return {
            "attempted": True,
            "updated_count": 0,
            "target": resolved.raw,
            "reason": "unchanged",
        }

    next_bindings = dict(bindings)
    next_bindings[resolved.binding_name] = refreshed
    bindings_payload["bindings"] = next_bindings
    bindings_payload["updated_at_utc"] = now
    store.save_bindings(bindings_payload)
    return {
        "attempted": True,
        "updated_count": 1,
        "updated_bindings": [resolved.binding_name],
        "target": resolved.raw,
        "reason": "refreshed_from_reply",
    }


def _reply_message_id(reply_status: dict[str, Any]) -> str | None:
    for key in ("reply_message_id", "outbound_message_id", "message_id"):
        value = reply_status.get(key)
        if value:
            return str(value)
    api_response = reply_status.get("api_response")
    if isinstance(api_response, dict):
        return response_message_id(api_response)
    return None


def _binding_matches_message(
    binding: dict[str, Any],
    *,
    user_id: str,
    group_id: str | None,
    chat_key: str | None,
) -> bool:
    if str(binding.get("to_user_id") or "").strip() != str(user_id or "").strip():
        return False
    binding_group_id = str(binding.get("group_id") or "").strip()
    if binding_group_id and binding_group_id != str(group_id or "").strip():
        return False
    binding_chat_key = str(binding.get("chat_key") or "").strip()
    if binding_chat_key and chat_key and binding_chat_key != str(chat_key or "").strip():
        return False
    return True


def connect_wechat_clawbot_target(
    *,
    base: Path,
    name: str,
    label: str = DEFAULT_WECHAT_CLAWBOT_LABEL,
    match_text: str | None = None,
    from_user_id: str | None = None,
    state_dir: str | None = None,
    base_url: str = DEFAULT_ILINK_BASE_URL,
    timeout_sec: int = 20,
    login_timeout_sec: int = 180,
    bind_timeout_sec: int = 180,
    poll_interval_sec: float = 3.0,
    client_factory: ClientFactory = WechatClawbotClient,
    progress_fn: ProgressFn | None = None,
    sleep_fn: SleepFn = time.sleep,
) -> dict[str, Any]:
    binding_name = str(name or "").strip()
    if not binding_name:
        raise AgentToolError(code="INPUT_ERROR", message="binding name is required")
    expected_text = str(match_text or "").strip() or f"bind {binding_name}"
    sleep_interval = max(0.0, float(poll_interval_sec or 0.0))

    qrcode_out = start_wechat_clawbot_qrcode(
        base=base,
        label=label,
        state_dir=state_dir,
        base_url=base_url,
        timeout_sec=timeout_sec,
        client_factory=client_factory,
    )
    qrcode_data = qrcode_out.get("data") if isinstance(qrcode_out.get("data"), dict) else {}
    qrcode = str(qrcode_data.get("qrcode") or "").strip()
    qrcode_artifact_path = str(qrcode_data.get("qrcode_artifact_path") or "").strip() or None
    qrcode_artifact_open_command = str(qrcode_data.get("qrcode_artifact_open_command") or "").strip() or None
    resolved_state_dir = str(qrcode_data.get("state_dir") or _state_dir(base=base, label=label, state_dir=state_dir))
    if progress_fn is not None:
        progress_fn(
            {
                "event": "qrcode",
                "qrcode": qrcode,
                "qrcode_artifact_path": qrcode_artifact_path,
                "qrcode_artifact_open_command": qrcode_artifact_open_command,
                "state_dir": resolved_state_dir,
            }
        )
    if not bool(qrcode_out.get("ok")):
        return build_response(
            tool_name="wechat_clawbot.connect",
            ok=False,
            data={"label": label, "state_dir": resolved_state_dir, "binding_name": binding_name},
            warnings=list(qrcode_out.get("warnings") or []),
            error=qrcode_out.get("error") or {"code": "QRCODE_ERROR", "message": "failed to request WeChat ClawBot QR code"},
        )

    login_deadline = time.monotonic() + max(0, int(login_timeout_sec or 0))
    qr_status_out: dict[str, Any] | None = None
    while True:
        qr_status_out = check_wechat_clawbot_qrcode(
            base=base,
            label=label,
            qrcode=qrcode,
            state_dir=state_dir,
            base_url=base_url,
            timeout_sec=timeout_sec,
            client_factory=client_factory,
        )
        status_data = qr_status_out.get("data") if isinstance(qr_status_out.get("data"), dict) else {}
        if bool(status_data.get("bound")):
            if progress_fn is not None:
                progress_fn({"event": "login_confirmed", "status": status_data.get("status"), "state_dir": resolved_state_dir})
                progress_fn({"event": "bind_instruction", "match_text": expected_text, "binding_name": binding_name})
            break
        if time.monotonic() >= login_deadline:
            return build_response(
                tool_name="wechat_clawbot.connect",
                ok=False,
                data={
                    "label": label,
                    "state_dir": resolved_state_dir,
                    "binding_name": binding_name,
                    "qrcode": qrcode,
                    "qrcode_artifact_path": qrcode_artifact_path,
                    "qrcode_artifact_open_command": qrcode_artifact_open_command,
                    "status": status_data.get("status"),
                },
                warnings=["QR login was not confirmed before timeout"],
                error={
                    "code": "QRCODE_LOGIN_TIMEOUT",
                    "message": "WeChat ClawBot QR login was not confirmed before timeout",
                    "details": {"login_timeout_sec": login_timeout_sec},
                },
            )
        if sleep_interval:
            sleep_fn(sleep_interval)

    bind_deadline = time.monotonic() + max(0, int(bind_timeout_sec or 0))
    last_bind_out: dict[str, Any] | None = None
    while True:
        bind_out = bind_wechat_clawbot_target(
            base=base,
            name=binding_name,
            label=label,
            match_text=expected_text,
            from_user_id=from_user_id,
            state_dir=state_dir,
            timeout_sec=timeout_sec,
            client_factory=client_factory,
        )
        last_bind_out = bind_out
        if bool(bind_out.get("ok")):
            bind_data = bind_out.get("data") if isinstance(bind_out.get("data"), dict) else {}
            if progress_fn is not None:
                progress_fn({"event": "bound", "target": bind_data.get("target"), "binding_name": binding_name})
            return build_response(
                tool_name="wechat_clawbot.connect",
                ok=True,
                data={
                    "label": label,
                    "state_dir": resolved_state_dir,
                    "binding_name": binding_name,
                    "match_text": expected_text,
                    "target": bind_data.get("target"),
                    "binding": bind_data.get("binding"),
                    "bindings_path": bind_data.get("bindings_path"),
                    "qrcode_artifact_path": qrcode_artifact_path,
                    "qrcode_artifact_open_command": qrcode_artifact_open_command,
                },
            )
        error = bind_out.get("error") if isinstance(bind_out.get("error"), dict) else {}
        if error.get("code") != "BINDING_MESSAGE_NOT_FOUND":
            return build_response(
                tool_name="wechat_clawbot.connect",
                ok=False,
                data={"label": label, "state_dir": resolved_state_dir, "binding_name": binding_name, "match_text": expected_text},
                warnings=list(bind_out.get("warnings") or []),
                error=error or {"code": "BINDING_ERROR", "message": "failed to bind WeChat ClawBot target"},
            )
        if time.monotonic() >= bind_deadline:
            last_data = last_bind_out.get("data") if isinstance(last_bind_out.get("data"), dict) else {}
            return build_response(
                tool_name="wechat_clawbot.connect",
                ok=False,
                data={
                    "label": label,
                    "state_dir": resolved_state_dir,
                    "binding_name": binding_name,
                    "match_text": expected_text,
                    "candidate_count": last_data.get("candidate_count"),
                },
                warnings=["No matching WeChat message was received before timeout"],
                error={
                    "code": "BINDING_MESSAGE_TIMEOUT",
                    "message": "no matching WeChat ClawBot message was received before timeout",
                    "details": {"bind_timeout_sec": bind_timeout_sec, "match_text": expected_text},
                },
            )
        if sleep_interval:
            sleep_fn(sleep_interval)


def list_wechat_clawbot_bindings(
    *,
    base: Path,
    label: str = DEFAULT_WECHAT_CLAWBOT_LABEL,
    state_dir: str | None = None,
) -> dict[str, Any]:
    store = _state_store(base=base, label=label, state_dir=state_dir)
    try:
        safe_bindings = store.safe_bindings()
    except ValueError as exc:
        raise AgentToolError(code="STATE_ERROR", message=str(exc)) from exc
    bindings_with_hints = _bindings_with_sender_hints(safe_bindings)
    return build_response(
        tool_name="wechat_clawbot.list",
        ok=True,
        data={
            "label": label,
            "state_dir": str(store.state_dir),
            "bindings": bindings_with_hints,
            "binding_count": len(bindings_with_hints),
            "allowed_sender_hints": _allowed_sender_hints(bindings_with_hints),
        },
    )


def _bindings_with_sender_hints(bindings: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for name, binding in bindings.items():
        item = dict(binding)
        user_id = str(item.get("to_user_id") or "").strip()
        if user_id:
            item["inbound_sender_id_hint"] = f"wechat:{user_id}"
        out[name] = item
    return out


def _allowed_sender_hints(bindings: dict[str, dict[str, Any]]) -> list[str]:
    hints: list[str] = []
    for binding in bindings.values():
        hint = str(binding.get("inbound_sender_id_hint") or "").strip()
        if hint and hint not in hints:
            hints.append(hint)
    return sorted(hints)


def _open_artifact_command(path: str | None) -> str | None:
    raw = str(path or "").strip()
    if not raw:
        return None
    return "open " + _shell_quote(raw)


def _shell_quote(value: str) -> str:
    return "'" + str(value).replace("'", "'\"'\"'") + "'"
