from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Callable

from domain.domain.multi_tick import WECHAT_CLAWBOT_NOTIFICATION_PROVIDER
from src.application.agent_tool_contracts import AgentToolError
from src.application.channels.wechat_clawbot.ilink_client import DEFAULT_ILINK_BASE_URL, WechatClawbotClient
from src.application.channels.wechat_clawbot.state import DEFAULT_WECHAT_CLAWBOT_LABEL, resolve_wechat_clawbot_state_dir
from src.application.channels.wechat_clawbot.state_store import WechatClawbotStateStore
from src.infrastructure.io_utils import utc_now


ClientFactory = Callable[..., WechatClawbotClient]


def reply_wechat_clawbot_text(
    *,
    base: Path,
    label: str = DEFAULT_WECHAT_CLAWBOT_LABEL,
    state_dir: str | None = None,
    to_user_id: str,
    context_token: str,
    text: str,
    group_id: str | None = None,
    timeout_sec: int = 20,
    idempotency_key: str | None = None,
    client_factory: ClientFactory = WechatClawbotClient,
) -> dict[str, Any]:
    if not str(to_user_id or "").strip() or not str(context_token or "").strip():
        return {"attempted": False, "ok": False, "reason": "missing_reply_context"}
    store = _state_store(base=base, label=label, state_dir=state_dir)
    idempotency_key_text = str(idempotency_key or "").strip()
    previous_receipt = _load_outbound_receipt(store=store, idempotency_key=idempotency_key_text)
    if previous_receipt is not None:
        return previous_receipt
    state = _load_store_json(store.load_state, default={})
    bot_token = str(state.get("bot_token") or "").strip()
    if not bot_token:
        return {"attempted": True, "ok": False, "reason": "missing_bot_token"}
    base_url = str(state.get("base_url") or DEFAULT_ILINK_BASE_URL).strip() or DEFAULT_ILINK_BASE_URL
    client = client_factory(bot_token=bot_token, base_url=base_url, timeout=timeout_sec)
    client_id = _idempotent_client_id(idempotency_key_text)
    api_response = client.send_text_message(
        to_user_id=str(to_user_id),
        context_token=str(context_token),
        text=str(text or ""),
        group_id=str(group_id).strip() if str(group_id or "").strip() else None,
        client_id=client_id,
    )
    outbound_message_id = _extract_message_id(api_response)
    ok = _response_success(api_response)
    receipt = {
        "attempted": True,
        "ok": ok,
        "reason": "sent" if ok else "reply_failed",
        "provider": WECHAT_CLAWBOT_NOTIFICATION_PROVIDER,
        "message_id": outbound_message_id,
        "outbound_message_id": outbound_message_id,
        "idempotency_key": idempotency_key_text or None,
        "client_id": client_id,
        "api_response": api_response,
    }
    if ok:
        _save_outbound_receipt(store=store, idempotency_key=idempotency_key_text, receipt=receipt)
    return receipt


def _state_store(*, base: Path, label: str, state_dir: str | None = None) -> WechatClawbotStateStore:
    notifications = {"wechat_clawbot_state_dir": state_dir} if state_dir else None
    return WechatClawbotStateStore(resolve_wechat_clawbot_state_dir(base=base, label=label, notifications=notifications))


def _load_store_json(load_fn: Callable[..., dict[str, Any]], *, default: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        return load_fn(default=default)
    except ValueError as exc:
        raise AgentToolError(code="STATE_ERROR", message=str(exc)) from exc


def _load_outbound_receipt(*, store: WechatClawbotStateStore, idempotency_key: str) -> dict[str, Any] | None:
    key = str(idempotency_key or "").strip()
    if not key:
        return None
    try:
        payload = store.load_outbound_receipts(default={"receipts": {}})
    except ValueError:
        return None
    receipts = payload.get("receipts") if isinstance(payload.get("receipts"), dict) else {}
    receipt = receipts.get(key) if isinstance(receipts, dict) else None
    if not isinstance(receipt, dict) or not bool(receipt.get("ok")):
        return None
    return {
        **dict(receipt),
        "attempted": False,
        "ok": True,
        "reason": "idempotent_replay",
        "provider": WECHAT_CLAWBOT_NOTIFICATION_PROVIDER,
        "idempotency_key": key,
        "replayed": True,
    }


def _save_outbound_receipt(*, store: WechatClawbotStateStore, idempotency_key: str, receipt: dict[str, Any]) -> None:
    key = str(idempotency_key or "").strip()
    if not key:
        return
    try:
        payload = store.load_outbound_receipts(default={"receipts": {}})
        receipts = payload.get("receipts") if isinstance(payload.get("receipts"), dict) else {}
        receipts = dict(receipts) if isinstance(receipts, dict) else {}
        receipts[key] = {
            **dict(receipt),
            "stored_at_utc": utc_now(),
        }
        payload["receipts"] = receipts
        store.save_outbound_receipts(payload)
    except Exception as exc:
        receipt["receipt_store_warning"] = f"{type(exc).__name__}: {exc}"


def _idempotent_client_id(idempotency_key: str) -> str | None:
    key = str(idempotency_key or "").strip()
    if not key:
        return None
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]


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
