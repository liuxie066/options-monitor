from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

from domain.domain.multi_tick import FEISHU_APP_NOTIFICATION_PROVIDER, notification_route_identity_hash
from domain.domain.tool_boundary import normalize_subprocess_adapter_payload
from src.application.channels.feishu_notification_renderer import normalize_feishu_notification_envelope
from src.application.secret_resolver import resolve_feishu_bot_config
from src.infrastructure.feishu_bitable import FeishuError, FeishuPermanentError
from src.infrastructure.fetch_control import check_fetch_control
from src.infrastructure.feishu_bot import (
    FEISHU_SEND_TOO_LARGE,
    permanent_failure_allows_fallback,
    send_message,
    send_post_message,
)


def resolve_feishu_bot_send_target(
    *,
    notifications: dict[str, Any] | None = None,
) -> str:
    return resolve_feishu_bot_config(notifications, metadata_only=True).user_open_id


def resolve_feishu_notification_target(
    *, target: str, notifications: dict[str, Any],
) -> str:
    if notifications.get("_explicit_route_target") is True:
        return target
    return resolve_feishu_bot_send_target(notifications=notifications)


def resolve_feishu_notification_identity(
    *, base: Path, route: dict[str, Any],
) -> dict[str, Any] | None:
    del base
    bot = resolve_feishu_bot_config(metadata_only=True)
    recipient = (route.get("target") if route["notifications"].get("_explicit_route_target")
                 else bot.user_open_id)
    if not bot.app_id or not recipient:
        return None
    return {"sender": bot.app_id, "recipient": recipient}


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
    deadline: float | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    check_fetch_control(deadline, cancelled)
    controls = ({"deadline": deadline, "cancelled": cancelled}
                if deadline is not None or cancelled is not None else {})
    del base
    resolved_channel = str(channel or "").strip().lower()
    if resolved_channel != FEISHU_APP_NOTIFICATION_PROVIDER:
        raise ValueError(f"unsupported notification provider for feishu app sender: {channel}")

    bot_cfg = resolve_feishu_bot_config(notifications)
    missing = bot_cfg.credential_missing_fields
    if missing:
        raise ValueError("Feishu bot env missing required fields: " + ", ".join(missing))

    receive_id = (str(target or "").strip() if (notifications or {}).get("_explicit_route_target") is True
                  else bot_cfg.user_open_id)
    if not receive_id:
        raise ValueError("Feishu bot user open_id is required")
    if receive_id_type != "open_id":
        raise ValueError(f"unsupported receive_id_type for phase1: {receive_id_type}")

    attribution_route_key = notification_route_identity_hash(
        provider=FEISHU_APP_NOTIFICATION_PROVIDER, channel=resolved_channel,
        sender=bot_cfg.app_id, recipient=receive_id)

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
                **controls,
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
                    **controls,
                )
            except FeishuPermanentError as exc:
                check_fetch_control(deadline, cancelled)
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
                    **controls,
                )
        check_fetch_control(deadline, cancelled)
        return {
            "ok": True,
            "http_status": 200,
            "request_path": request_path,
            "feishu_code": response_json.get("code") if isinstance(response_json.get("code"), int) else None,
            "message_id": _feishu_message_id(response_json),
            "idempotency_key": idempotency_key,
            "effective_idempotency_key": effective_idempotency_key,
            "http_attempts": _safe_http_attempts(http_attempts),
            "render_mode": envelope.get("render_mode") if envelope else "post_markdown",
            "fallback_used": fallback_used,
            "attribution_route_key": attribution_route_key,
        }
    except FeishuError as exc:
        check_fetch_control(deadline, cancelled)
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
            "feishu_code": _first_int(response.get("feishu_code"), (response_json or {}).get("code")),
            "message_id": _feishu_message_id(response_json or {}),
            "request_path": request_path,
            "error_type": type(exc).__name__,
            "local_error_code": response.get("local_error_code"),
            "request_body_bytes": response.get("request_body_bytes"),
            "request_body_budget_bytes": response.get("request_body_budget_bytes"),
            "normalized_markdown_chars": response.get("normalized_markdown_chars"),
            "normalized_markdown_sha256": response.get("normalized_markdown_sha256"),
            "idempotency_key": idempotency_key,
            "effective_idempotency_key": effective_idempotency_key,
            "http_attempts": _safe_http_attempts(
                response_http_attempts if isinstance(response_http_attempts, list) else http_attempts
            ),
            "render_mode": envelope.get("render_mode") if envelope else "post_markdown",
            "fallback_used": fallback_used,
            "attribution_route_key": attribution_route_key,
        }


def _card_permanent_failure_allows_fallback(exc: FeishuPermanentError) -> bool:
    return permanent_failure_allows_fallback(exc, too_large_code=FEISHU_SEND_TOO_LARGE)


def _first_int(*values: Any) -> int | None:
    for value in values:
        if isinstance(value, int):
            return value
    return None


def _feishu_message_id(response: dict[str, Any]) -> str | None:
    data = response.get("data") if isinstance(response.get("data"), dict) else {}
    value = data.get("message_id")
    return str(value) if value else None


def _safe_http_attempts(value: Any) -> list[dict[str, Any]]:
    allowed = {
        "level",
        "category",
        "http_status",
        "feishu_code",
        "attempt",
        "max_attempts",
        "url_path",
        "sleep_s",
        "message_id",
    }
    if not isinstance(value, list):
        return []
    return [
        {key: item.get(key) for key in allowed if item.get(key) is not None}
        for item in value
        if isinstance(item, dict)
    ]


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
    message_id = result.get("message_id") or data.get("message_id")
    http_status = result.get("http_status")
    feishu_code = response_json.get("code") if isinstance(response_json.get("code"), int) else None
    if feishu_code is None and isinstance(result.get("feishu_code"), int):
        feishu_code = result.get("feishu_code")
    local_error_code = str(result.get("local_error_code") or "").strip() or None
    request_path = str(result.get("request_path") or "/open-apis/im/v1/messages?receive_id_type=open_id")
    idempotency_key = str(result.get("idempotency_key") or "").strip() or None
    effective_idempotency_key = str(result.get("effective_idempotency_key") or "").strip() or None
    http_attempts = _safe_http_attempts(result.get("http_attempts"))
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
            f"message_id={message_id}",
            f"request_path={request_path}",
        ]
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
        stdout="",
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
            "request_path": request_path,
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
            "attribution_route_key": result.get("attribution_route_key"),
            **local_diagnostics,
        },
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
    deadline: float | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> Any:
    send_result = send_feishu_app_message(
        base=base,
        channel=channel,
        target=target,
        message=message,
        notifications=notifications,
        idempotency_key=idempotency_key,
        transport_envelope=transport_envelope,
        **({"deadline": deadline, "cancelled": cancelled}
           if deadline is not None or cancelled is not None else {}),
    )
    normalized = normalize_feishu_app_send_output(send_result=send_result)
    stdout = json.dumps(
        {
            "ok": bool(normalized.get("ok")),
            "http_status": normalized.get("http_status"),
            "provider_response_code": normalized.get("provider_response_code"),
            "message_id": normalized.get("message_id"),
        },
        ensure_ascii=False,
    )
    stderr = "" if bool(normalized.get("command_ok")) else str(normalized.get("message") or "")
    return SimpleNamespace(returncode=int(normalized.get("returncode") or 0), stdout=stdout, stderr=stderr, raw=send_result)
