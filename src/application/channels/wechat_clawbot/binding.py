from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from src.application.agent_tool_contracts import AgentToolError, build_response
from src.application.channels.wechat_clawbot.ilink_client import DEFAULT_ILINK_BASE_URL, WechatClawbotClient
from src.application.channels.wechat_clawbot.state import (
    DEFAULT_WECHAT_CLAWBOT_LABEL,
    resolve_wechat_clawbot_state_dir,
)
from src.infrastructure.io_utils import utc_now


ClientFactory = Callable[..., WechatClawbotClient]


def _json_default(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    return str(value)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except Exception:
        pass


def _read_json(path: Path, default: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return dict(default or {})
    except Exception as exc:
        raise AgentToolError(code="STATE_ERROR", message=f"failed to read {path.name}", details={"path": str(path), "error": str(exc)}) from exc
    if not isinstance(payload, dict):
        raise AgentToolError(code="STATE_ERROR", message=f"{path.name} must be a JSON object", details={"path": str(path)})
    return payload


def _state_dir(*, base: Path, label: str, state_dir: str | None = None) -> Path:
    notifications = {"wechat_clawbot_state_dir": state_dir} if state_dir else None
    return resolve_wechat_clawbot_state_dir(base=base, label=label, notifications=notifications)


def _walk_dicts(value: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if isinstance(value, dict):
        out.append(value)
        for child in value.values():
            out.extend(_walk_dicts(child))
    elif isinstance(value, list):
        for child in value:
            out.extend(_walk_dicts(child))
    return out


def _extract_first_string(payload: dict[str, Any], keys: tuple[str, ...]) -> str:
    for item in _walk_dicts(payload):
        for key in keys:
            value = item.get(key)
            if value is None:
                continue
            text = str(value).strip()
            if text:
                return text
    return ""


def _extract_messages(payload: dict[str, Any]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for item in _walk_dicts(payload):
        for key in ("message_list", "messages", "updates", "item_list", "items"):
            raw = item.get(key)
            if isinstance(raw, list):
                for entry in raw:
                    if isinstance(entry, dict):
                        messages.append(entry)
    if not messages:
        for item in _walk_dicts(payload):
            if _message_context_token(item) and _message_user_id(item):
                messages.append(item)
    return messages


def _message_context_token(message: dict[str, Any]) -> str:
    return _extract_first_string(message, ("context_token", "contextToken", "reply_context_token"))


def _message_user_id(message: dict[str, Any]) -> str:
    for key in ("from_user_id", "fromUserId", "user_id", "userId", "sender_id", "senderId"):
        value = message.get(key)
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _message_group_id(message: dict[str, Any]) -> str:
    return _extract_first_string(message, ("group_id", "groupId", "room_id", "roomId"))


def _message_id(message: dict[str, Any]) -> str:
    return _extract_first_string(message, ("message_id", "messageId", "msg_id", "msgId", "id"))


def _message_text(message: dict[str, Any]) -> str:
    for item in _walk_dicts(message):
        for key in ("text", "content"):
            value = item.get(key)
            text = str(value or "").strip()
            if text:
                return text
    return ""


def _state_payload_path(state_dir: Path) -> Path:
    return state_dir / "state.json"


def _pending_payload_path(state_dir: Path) -> Path:
    return state_dir / "pending_login.json"


def _bindings_payload_path(state_dir: Path) -> Path:
    return state_dir / "bindings.json"


def start_wechat_clawbot_qrcode(
    *,
    base: Path,
    label: str = DEFAULT_WECHAT_CLAWBOT_LABEL,
    state_dir: str | None = None,
    base_url: str = DEFAULT_ILINK_BASE_URL,
    timeout_sec: int = 20,
    client_factory: ClientFactory = WechatClawbotClient,
) -> dict[str, Any]:
    resolved_dir = _state_dir(base=base, label=label, state_dir=state_dir)
    client = client_factory(bot_token=None, base_url=base_url, timeout=timeout_sec)
    response = client.get_bot_qrcode(bot_type=3)
    qrcode = _extract_first_string(response, ("qrcode", "qr_code", "qrCode", "qrcode_url", "qr_url", "url"))
    payload = {
        "label": label,
        "state_dir": str(resolved_dir),
        "base_url": str(base_url or DEFAULT_ILINK_BASE_URL),
        "qrcode": qrcode,
        "created_at_utc": utc_now(),
        "response_json": response,
    }
    _write_json(_pending_payload_path(resolved_dir), payload)
    return build_response(
        tool_name="wechat_clawbot.qrcode",
        ok=bool(qrcode),
        data={k: v for k, v in payload.items() if k != "response_json"},
        warnings=[] if qrcode else ["qrcode not found in iLink response"],
        error=None if qrcode else {"code": "UPSTREAM_RESPONSE_ERROR", "message": "qrcode not found in iLink response", "details": {"response": response}},
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
    resolved_dir = _state_dir(base=base, label=label, state_dir=state_dir)
    pending = _read_json(_pending_payload_path(resolved_dir), {})
    resolved_qrcode = str(qrcode or pending.get("qrcode") or "").strip()
    if not resolved_qrcode:
        raise AgentToolError(code="INPUT_ERROR", message="qrcode is required; run qrcode first or pass --qrcode")
    resolved_base_url = str(base_url or pending.get("base_url") or DEFAULT_ILINK_BASE_URL)
    client = client_factory(bot_token=None, base_url=resolved_base_url, timeout=timeout_sec)
    response = client.get_qrcode_status(qrcode=resolved_qrcode)
    bot_token = _extract_first_string(response, ("bot_token", "botToken", "token", "access_token", "accessToken"))
    status = _extract_first_string(response, ("status", "state", "qrcode_status", "qrcodeStatus", "message", "msg"))
    ok = bool(bot_token)
    state_payload: dict[str, Any] | None = None
    if ok:
        state_payload = {
            "label": label,
            "base_url": resolved_base_url,
            "bot_token": bot_token,
            "get_updates_buf": _extract_first_string(response, ("get_updates_buf", "getUpdatesBuf")),
            "login_status": status,
            "updated_at_utc": utc_now(),
            "login_response_json": response,
        }
        _write_json(_state_payload_path(resolved_dir), state_payload)
    return build_response(
        tool_name="wechat_clawbot.qr_status",
        ok=True,
        data={
            "label": label,
            "state_dir": str(resolved_dir),
            "qrcode": resolved_qrcode,
            "status": status,
            "bound": ok,
            "state_path": str(_state_payload_path(resolved_dir)) if ok else None,
        },
        warnings=[] if ok else ["QR login is not confirmed yet or token was not present in response"],
        meta={"response_json": response},
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
    resolved_dir = _state_dir(base=base, label=label, state_dir=state_dir)
    state = _read_json(_state_payload_path(resolved_dir), {})
    bot_token = str(state.get("bot_token") or "").strip()
    if not bot_token:
        raise AgentToolError(code="STATE_ERROR", message="WeChat ClawBot bot_token is missing; run qrcode and qr-status first")
    client = client_factory(
        bot_token=bot_token,
        base_url=str(state.get("base_url") or DEFAULT_ILINK_BASE_URL),
        timeout=timeout_sec,
    )
    response = client.get_updates(get_updates_buf=str(state.get("get_updates_buf") or ""))
    new_buf = _extract_first_string(response, ("get_updates_buf", "getUpdatesBuf"))

    expected_text = str(match_text or "").strip()
    expected_user_id = str(from_user_id or "").strip()
    selected: dict[str, Any] | None = None
    messages = _extract_messages(response)
    for message in messages:
        user_id = _message_user_id(message)
        context_token = _message_context_token(message)
        text = _message_text(message)
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
            data={"label": label, "state_dir": str(resolved_dir), "binding_name": binding_name, "candidate_count": len(messages)},
        )

    binding = {
        "to_user_id": _message_user_id(selected),
        "context_token": _message_context_token(selected),
        "group_id": _message_group_id(selected) or None,
        "chat_key": _extract_first_string(selected, ("chat_key", "chatKey", "conversation_id", "conversationId")) or None,
        "last_message_id": _message_id(selected) or None,
        "last_text": _message_text(selected)[:500],
        "updated_at_utc": utc_now(),
    }
    bindings_payload = _read_json(_bindings_payload_path(resolved_dir), {"bindings": {}})
    bindings = bindings_payload.get("bindings") if isinstance(bindings_payload.get("bindings"), dict) else {}
    bindings[str(binding_name)] = binding
    bindings_payload["bindings"] = bindings
    bindings_payload["updated_at_utc"] = binding["updated_at_utc"]
    _write_json(_bindings_payload_path(resolved_dir), bindings_payload)
    if new_buf:
        state["get_updates_buf"] = new_buf
        state["updated_at_utc"] = binding["updated_at_utc"]
        _write_json(_state_payload_path(resolved_dir), state)
    return build_response(
        tool_name="wechat_clawbot.bind",
        ok=True,
        data={
            "label": label,
            "state_dir": str(resolved_dir),
            "binding_name": binding_name,
            "target": f"wechat:{label}:{binding_name}",
            "binding": {k: v for k, v in binding.items() if k not in {"context_token"}},
            "bindings_path": str(_bindings_payload_path(resolved_dir)),
        },
    )


def list_wechat_clawbot_bindings(
    *,
    base: Path,
    label: str = DEFAULT_WECHAT_CLAWBOT_LABEL,
    state_dir: str | None = None,
) -> dict[str, Any]:
    resolved_dir = _state_dir(base=base, label=label, state_dir=state_dir)
    bindings_payload = _read_json(_bindings_payload_path(resolved_dir), {"bindings": {}})
    bindings = bindings_payload.get("bindings") if isinstance(bindings_payload.get("bindings"), dict) else {}
    safe_bindings: dict[str, Any] = {}
    for name, value in bindings.items():
        if not isinstance(value, dict):
            continue
        safe_bindings[str(name)] = {k: v for k, v in value.items() if k != "context_token"}
    return build_response(
        tool_name="wechat_clawbot.list",
        ok=True,
        data={
            "label": label,
            "state_dir": str(resolved_dir),
            "bindings": safe_bindings,
            "binding_count": len(safe_bindings),
        },
    )
