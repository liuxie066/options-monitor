from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

from domain.domain.multi_tick import (
    FEISHU_APP_NOTIFICATION_PROVIDER,
    SUPPORTED_NOTIFICATION_PROVIDERS,
    normalize_notification_provider,
)
from domain.domain.tool_boundary import normalize_subprocess_adapter_payload
from src.application.channels.feishu import build_feishu_channel_adapter
from src.application.channels.feishu_notification_renderer import (
    normalize_feishu_notification_envelope,
)
from src.application.channels.service import ChannelRegistry
from src.application.channels.wechat_clawbot.adapter import build_wechat_clawbot_channel_adapter
from src.application.channels.wechat_clawbot.notification import (
    normalize_wechat_clawbot_send_output,
    send_wechat_clawbot_message_process,
)
from src.application.secret_resolver import resolve_feishu_bot_config
from src.infrastructure.feishu_bitable import FeishuError, FeishuPermanentError
from src.infrastructure.feishu_bot import FEISHU_SEND_TOO_LARGE, send_message, send_post_message


@dataclass(frozen=True)
class NotificationDeliveryAdapter:
    send_fn: Callable[..., Any]
    normalize_fn: Callable[..., dict[str, Any]]
    failure_stage: str


def build_notification_idempotency_key(
    *,
    run_id: str,
    account: str,
    target: str,
    message: str,
) -> str:
    raw = "\n".join(
        [
            str(run_id or "").strip(),
            str(account or "").strip().lower(),
            str(target or "").strip(),
            hashlib.sha256(str(message or "").encode("utf-8")).hexdigest(),
        ]
    )
    return "om-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def build_notification_transport_key(logical_key: str) -> str:
    """Map a stable logical delivery key to a provider-safe compact key."""

    value = str(logical_key or "").strip()
    if not value:
        raise ValueError("logical notification idempotency key is required")
    return "om-" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]


def resolve_feishu_bot_send_target(
    *,
    notifications: dict[str, Any] | None = None,
) -> str:
    return resolve_feishu_bot_config(notifications).user_open_id


def send_feishu_app_message(
    *,
    base: Path,
    channel: str,
    target: str,
    message: str,
    notifications: dict[str, Any] | None = None,
    receive_id_type: str = "open_id",
    idempotency_key: str | None = None,
    transport_envelope: dict[str, Any] | None = None,
) -> dict[str, Any]:
    del base, target
    resolved_channel = str(channel or "").strip().lower()
    if resolved_channel != FEISHU_APP_NOTIFICATION_PROVIDER:
        raise ValueError(f"unsupported notification provider for feishu app sender: {channel}")

    bot_cfg = resolve_feishu_bot_config(notifications)
    missing = bot_cfg.credential_missing_fields
    if missing:
        raise ValueError("Feishu bot env missing required fields: " + ", ".join(missing))

    receive_id = bot_cfg.user_open_id
    if not receive_id:
        raise ValueError("Feishu bot user open_id is required")
    if receive_id_type != "open_id":
        raise ValueError(f"unsupported receive_id_type for phase1: {receive_id_type}")

    request_path = f"/open-apis/im/v1/messages?receive_id_type={receive_id_type}"
    http_attempts: list[dict[str, Any]] = []
    envelope = (
        normalize_feishu_notification_envelope(
            transport_envelope,
            expected_text=str(message or ""),
        )
        if transport_envelope is not None
        else None
    )
    fallback_used = False
    effective_idempotency_key = idempotency_key
    try:
        if envelope is None:
            response_json = send_post_message(
                app_id=bot_cfg.app_id,
                app_secret=bot_cfg.app_secret,
                open_id=receive_id,
                markdown=str(message or ""),
                uuid=idempotency_key,
                log_fn=http_attempts.append,
            )
        else:
            transport = dict(envelope["transport"])
            try:
                response_json = send_message(
                    app_id=bot_cfg.app_id,
                    app_secret=bot_cfg.app_secret,
                    open_id=receive_id,
                    msg_type=str(transport["msg_type"]),
                    content=dict(transport["content"]),
                    uuid=idempotency_key,
                    log_fn=http_attempts.append,
                )
            except FeishuPermanentError as exc:
                if (
                    not _card_permanent_failure_allows_fallback(exc)
                    or _http_attempts_have_ambiguous_send(http_attempts)
                ):
                    raise
                fallback = dict(envelope["fallback"])
                fallback_used = True
                effective_idempotency_key = f"{idempotency_key}:fallback" if idempotency_key else None
                response_json = send_post_message(
                    app_id=bot_cfg.app_id,
                    app_secret=bot_cfg.app_secret,
                    open_id=receive_id,
                    markdown=str(fallback["markdown"]),
                    uuid=effective_idempotency_key,
                    log_fn=http_attempts.append,
                )
        return {
            "ok": True,
            "http_status": 200,
            "request_path": request_path,
            "response_json": response_json,
            "response_tail": json.dumps(response_json, ensure_ascii=False)[-500:],
            "idempotency_key": idempotency_key,
            "effective_idempotency_key": effective_idempotency_key,
            "http_attempts": http_attempts,
            "render_mode": envelope.get("render_mode") if envelope else "post_markdown",
            "fallback_used": fallback_used,
        }
    except FeishuError as exc:
        response = exc.response if isinstance(exc.response, dict) else {}
        body_text = str(response.get("body") or "")
        response_json = response if isinstance(response.get("code"), int) else None
        if body_text:
            try:
                parsed = json.loads(body_text)
                if isinstance(parsed, dict):
                    response_json = parsed
            except Exception:
                pass
        response_http_attempts = response.get("http_attempts")
        return {
            "ok": False,
            "http_status": response.get("http_status"),
            "feishu_code": response.get("feishu_code"),
            "request_path": request_path,
            "response_json": response_json,
            "response_tail": body_text[-500:],
            "error_type": type(exc).__name__,
            "error_message": str(exc),
            "local_error_code": response.get("local_error_code"),
            "request_body_bytes": response.get("request_body_bytes"),
            "request_body_budget_bytes": response.get("request_body_budget_bytes"),
            "normalized_markdown_chars": response.get("normalized_markdown_chars"),
            "normalized_markdown_sha256": response.get("normalized_markdown_sha256"),
            "idempotency_key": idempotency_key,
            "effective_idempotency_key": effective_idempotency_key,
            "http_attempts": response_http_attempts if isinstance(response_http_attempts, list) else http_attempts,
            "render_mode": envelope.get("render_mode") if envelope else "post_markdown",
            "fallback_used": fallback_used,
        }


def _card_permanent_failure_allows_fallback(exc: FeishuPermanentError) -> bool:
    response = exc.response if isinstance(exc.response, dict) else {}
    if str(response.get("local_error_code") or "") == FEISHU_SEND_TOO_LARGE:
        return True
    http_status = response.get("http_status")
    return exc.code is not None or (
        isinstance(http_status, int) and 400 <= http_status <= 499
    )


def _http_attempts_have_ambiguous_send(
    http_attempts: list[dict[str, Any]],
) -> bool:
    return any(
        isinstance(item, dict)
        and str(item.get("category") or "") == "transient"
        for item in http_attempts
    )


def normalize_feishu_app_send_output(*, send_result: dict[str, Any]) -> dict[str, Any]:
    result = send_result if isinstance(send_result, dict) else {}
    raw_response_json = result.get("response_json")
    response_json: dict[str, Any] = raw_response_json if isinstance(raw_response_json, dict) else {}
    raw_data = response_json.get("data")
    data: dict[str, Any] = raw_data if isinstance(raw_data, dict) else {}
    message_id = data.get("message_id")
    http_status = result.get("http_status")
    feishu_code = response_json.get("code") if isinstance(response_json.get("code"), int) else None
    if feishu_code is None and isinstance(result.get("feishu_code"), int):
        feishu_code = result.get("feishu_code")
    feishu_msg = str(response_json.get("msg") or result.get("error_message") or "").strip()
    local_error_code = str(result.get("local_error_code") or "").strip() or None
    request_path = str(result.get("request_path") or "/open-apis/im/v1/messages?receive_id_type=open_id")
    response_tail = str(result.get("response_tail") or "")
    idempotency_key = str(result.get("idempotency_key") or "").strip() or None
    effective_idempotency_key = str(result.get("effective_idempotency_key") or "").strip() or None
    http_attempts = result.get("http_attempts") if isinstance(result.get("http_attempts"), list) else []
    retry_attempt_count = max(0, len(http_attempts) - 1)
    ambiguous_send = _http_attempts_have_ambiguous_send(http_attempts)
    fallback_used = bool(result.get("fallback_used"))
    duplicate_risk = bool(
        ambiguous_send and (not idempotency_key or fallback_used)
    )

    transport_ok = http_status == 200
    command_ok = bool(transport_ok and feishu_code == 0)
    delivery_confirmed = bool(
        command_ok
        and message_id
        and not (fallback_used and ambiguous_send)
    )
    ok = delivery_confirmed
    error_code = (
        local_error_code
        or (
            "FEISHU_PROVIDER_REJECTED"
            if transport_ok and feishu_code not in {None, 0}
            else "SEND_UNCONFIRMED"
            if command_ok and not delivery_confirmed
            else "FEISHU_SEND_FAILED"
        )
    )

    if ok:
        message = f"message_id={message_id}"
    elif command_ok and feishu_code == 0 and not message_id:
        message = "feishu send returned success but data.message_id is missing"
    else:
        parts = [
            f"http_status={http_status}",
            f"feishu_code={feishu_code}",
            f"feishu_msg={feishu_msg or ''}",
            f"message_id={message_id}",
            f"request_path={request_path}",
        ]
        if response_tail:
            parts.append(f"response_tail={response_tail}")
        message = " ".join(parts)

    local_diagnostics = {
        key: result.get(key)
        for key in (
            "request_body_bytes",
            "request_body_budget_bytes",
            "normalized_markdown_chars",
            "normalized_markdown_sha256",
        )
        if result.get(key) is not None
    }

    return normalize_subprocess_adapter_payload(
        adapter="notify",
        tool_name="feishu_app_message_send",
        returncode=(0 if command_ok else 1),
        stdout=response_tail,
        stderr="",
        ok=ok,
        message=message,
        extra={
            "command_ok": command_ok,
            "transport_ok": transport_ok,
            "delivery_confirmed": delivery_confirmed,
            "message_id": (None if message_id is None else str(message_id)),
            "http_status": http_status,
            "feishu_code": feishu_code,
            "provider_response_code": feishu_code,
            "feishu_msg": feishu_msg,
            "request_path": request_path,
            "response_tail": response_tail,
            "idempotency_key": idempotency_key,
            "effective_idempotency_key": effective_idempotency_key,
            "http_attempts": http_attempts,
            "retry_attempt_count": retry_attempt_count,
            "ambiguous_send": ambiguous_send,
            "duplicate_risk": duplicate_risk,
            "local_error_code": local_error_code,
            "error_code": (None if ok else error_code),
            "render_mode": str(result.get("render_mode") or ""),
            "fallback_used": fallback_used,
            **local_diagnostics,
        },
    )


def normalize_notification_delivery_result(
    send_result: Any,
    *,
    normalize_fn: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    if isinstance(send_result, dict) and ("delivery_confirmed" in send_result or "command_ok" in send_result):
        return dict(send_result)
    try:
        return normalize_fn(send_result=getattr(send_result, "raw", send_result))
    except TypeError:
        return normalize_fn(
            returncode=int(getattr(send_result, "returncode", 0) or 0),
            stdout=str(getattr(send_result, "stdout", "") or ""),
            stderr=str(getattr(send_result, "stderr", "") or ""),
        )


def send_feishu_app_message_process(
    *,
    base: Path,
    channel: str,
    target: str,
    message: str,
    notifications: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
    transport_envelope: dict[str, Any] | None = None,
) -> Any:
    send_result = send_feishu_app_message(
        base=base,
        channel=channel,
        target=target,
        message=message,
        notifications=notifications,
        idempotency_key=idempotency_key,
        transport_envelope=transport_envelope,
    )
    normalized = normalize_feishu_app_send_output(send_result=send_result)
    stdout = ""
    if isinstance(send_result, dict):
        response_json = send_result.get("response_json")
        if isinstance(response_json, dict) and response_json:
            stdout = json.dumps(response_json, ensure_ascii=False)
        elif send_result.get("response_tail"):
            stdout = str(send_result.get("response_tail") or "")
    stderr = "" if bool(normalized.get("command_ok")) else str(normalized.get("message") or "")
    return SimpleNamespace(returncode=int(normalized.get("returncode") or 0), stdout=stdout, stderr=stderr, raw=send_result)


def build_notification_channel_registry() -> ChannelRegistry:
    return ChannelRegistry(
        (
            build_feishu_channel_adapter(
                send_fn=send_feishu_app_message_process,
                normalize_fn=normalize_feishu_app_send_output,
                failure_stage="send_feishu_app_message",
            ),
            build_wechat_clawbot_channel_adapter(
                send_fn=send_wechat_clawbot_message_process,
                normalize_fn=normalize_wechat_clawbot_send_output,
                failure_stage="send_wechat_clawbot_message",
            ),
        )
    )


def select_notification_delivery_adapter(provider: Any) -> NotificationDeliveryAdapter:
    resolved_provider = normalize_notification_provider(provider)
    try:
        channel_adapter = build_notification_channel_registry().require(resolved_provider)
    except ValueError as exc:
        allowed = ", ".join(SUPPORTED_NOTIFICATION_PROVIDERS)
        raise ValueError(f"unsupported notification provider: {provider}; expected one of: {allowed}") from exc
    if channel_adapter.send_fn is None or channel_adapter.normalize_fn is None:
        raise ValueError(f"notification channel is missing outbound send support: {resolved_provider}")
    return NotificationDeliveryAdapter(
        send_fn=channel_adapter.send_fn,
        normalize_fn=channel_adapter.normalize_fn,
        failure_stage=channel_adapter.failure_stage,
    )
