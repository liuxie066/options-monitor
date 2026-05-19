from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from base64 import b64decode
from dataclasses import dataclass
from typing import Any, Callable, Mapping, cast

from src.application.agent_tool_contracts import AgentToolError, build_error_payload, build_response, mask_path
from src.application.inbound.feishu import handle_feishu_payload
from src.application.inbound.router import ExecuteToolFn
from src.application.secret_resolver import (
    DEFAULT_FEISHU_BOT_APP_ID_ENV,
    DEFAULT_FEISHU_BOT_APP_SECRET_ENV,
    DEFAULT_FEISHU_BOT_ENCRYPT_KEY_ENV,
    DEFAULT_FEISHU_BOT_VERIFICATION_TOKEN_ENV,
    resolve_feishu_bot_config,
)
from src.infrastructure.feishu_bot import reply_text_message


DEFAULT_FEISHU_GATEWAY_HOST = "127.0.0.1"
DEFAULT_FEISHU_GATEWAY_PORT = 8765
DEFAULT_FEISHU_GATEWAY_PATH = "/feishu/events"
DEFAULT_FEISHU_SIGNATURE_MAX_AGE_SECONDS = 600
DEFAULT_FEISHU_REPLY_MAX_CHARS = 3500

ReplyFn = Callable[..., dict[str, Any]]


@dataclass(frozen=True)
class FeishuGatewaySettings:
    host: str = DEFAULT_FEISHU_GATEWAY_HOST
    port: int = DEFAULT_FEISHU_GATEWAY_PORT
    path: str = DEFAULT_FEISHU_GATEWAY_PATH
    config_key: str | None = "us"
    config_path: str | None = None
    audit_db: str | None = None
    allowed_senders: str | None = None
    app_id: str = ""
    app_secret: str = ""
    encrypt_key: str = ""
    verification_token: str = ""
    require_signature: bool = True
    reply_enabled: bool = True
    reply_in_thread: bool = False
    max_reply_chars: int = DEFAULT_FEISHU_REPLY_MAX_CHARS
    signature_max_age_seconds: int = DEFAULT_FEISHU_SIGNATURE_MAX_AGE_SECONDS
    tls_certfile: str | None = None
    tls_keyfile: str | None = None

    @property
    def normalized_path(self) -> str:
        value = str(self.path or "").strip() or DEFAULT_FEISHU_GATEWAY_PATH
        return value if value.startswith("/") else f"/{value}"

    def validate_for_serve(self) -> None:
        if not self.allowed_senders:
            raise AgentToolError(
                code="CONFIG_ERROR",
                message="missing inbound sender allowlist for Feishu gateway",
                hint="Set OM_FEISHU_BOT_USER_OPEN_ID or OM_FEISHU_BOT_ALLOWED_OPEN_IDS.",
            )
        if self.require_signature and not self.encrypt_key:
            raise AgentToolError(
                code="CONFIG_ERROR",
                message="missing Feishu inbound encrypt key for signature verification",
                hint=f"Set {DEFAULT_FEISHU_BOT_ENCRYPT_KEY_ENV}, or pass --allow-unsigned only for local tests.",
            )
        if not self.verification_token:
            raise AgentToolError(
                code="CONFIG_ERROR",
                message="missing Feishu verification token for event callbacks",
                hint=f"Set {DEFAULT_FEISHU_BOT_VERIFICATION_TOKEN_ENV}.",
            )
        if self.reply_enabled and not (self.app_id and self.app_secret):
            raise AgentToolError(
                code="CONFIG_ERROR",
                message="missing Feishu app credentials for automatic replies",
                hint=(
                    f"Set {DEFAULT_FEISHU_BOT_APP_ID_ENV}/{DEFAULT_FEISHU_BOT_APP_SECRET_ENV}."
                ),
            )
        if bool(self.tls_certfile) != bool(self.tls_keyfile):
            raise AgentToolError(
                code="CONFIG_ERROR",
                message="tls certfile and keyfile must be configured together",
            )

    def redacted_status(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "port": self.port,
            "path": self.normalized_path,
            "config_key": self.config_key,
            "config_path": self.config_path,
            "audit_db": mask_path(self.audit_db),
            "allowed_senders_configured": bool(self.allowed_senders),
            "app_id_configured": bool(self.app_id),
            "app_secret_configured": bool(self.app_secret),
            "encrypt_key_configured": bool(self.encrypt_key),
            "verification_token_configured": bool(self.verification_token),
            "require_signature": bool(self.require_signature),
            "reply_enabled": bool(self.reply_enabled),
            "reply_in_thread": bool(self.reply_in_thread),
            "max_reply_chars": int(self.max_reply_chars),
            "signature_max_age_seconds": int(self.signature_max_age_seconds),
            "tls_enabled": bool(self.tls_certfile and self.tls_keyfile),
        }


def build_feishu_gateway_settings(
    *,
    host: str | None = None,
    port: int | None = None,
    path: str | None = None,
    config_key: str | None = "us",
    config_path: str | None = None,
    audit_db: str | None = None,
    require_signature: bool | None = None,
    reply_enabled: bool = True,
    reply_in_thread: bool | None = None,
    max_reply_chars: int | None = None,
    signature_max_age_seconds: int | None = None,
    tls_certfile: str | None = None,
    tls_keyfile: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> FeishuGatewaySettings:
    env = environ if environ is not None else os.environ
    bot_cfg = resolve_feishu_bot_config(environ=env)
    resolved_require_signature = True if require_signature is None else bool(require_signature)
    return FeishuGatewaySettings(
        host=_first_text(host, env.get("OM_FEISHU_GATEWAY_HOST")) or DEFAULT_FEISHU_GATEWAY_HOST,
        port=int(port if port is not None else _int_env(env, "OM_FEISHU_GATEWAY_PORT", DEFAULT_FEISHU_GATEWAY_PORT)),
        path=_first_text(path, env.get("OM_FEISHU_GATEWAY_PATH")) or DEFAULT_FEISHU_GATEWAY_PATH,
        config_key=str(config_key or "").strip().lower() or None,
        config_path=_first_text(config_path),
        audit_db=_first_text(audit_db, env.get("OM_INBOUND_AUDIT_DB")),
        allowed_senders=bot_cfg.default_allowed_senders(),
        app_id=bot_cfg.app_id,
        app_secret=bot_cfg.app_secret,
        encrypt_key=bot_cfg.encrypt_key,
        verification_token=bot_cfg.verification_token,
        require_signature=resolved_require_signature,
        reply_enabled=bool(reply_enabled),
        reply_in_thread=bool(reply_in_thread) if reply_in_thread is not None else _truthy(env.get("OM_FEISHU_REPLY_IN_THREAD")),
        max_reply_chars=int(max_reply_chars or _int_env(env, "OM_FEISHU_REPLY_MAX_CHARS", DEFAULT_FEISHU_REPLY_MAX_CHARS)),
        signature_max_age_seconds=int(
            signature_max_age_seconds
            if signature_max_age_seconds is not None
            else _int_env(env, "OM_FEISHU_SIGNATURE_MAX_AGE_SECONDS", DEFAULT_FEISHU_SIGNATURE_MAX_AGE_SECONDS)
        ),
        tls_certfile=_first_text(tls_certfile, env.get("OM_FEISHU_GATEWAY_TLS_CERTFILE")),
        tls_keyfile=_first_text(tls_keyfile, env.get("OM_FEISHU_GATEWAY_TLS_KEYFILE")),
    )


def build_feishu_signature(*, timestamp: str, nonce: str, encrypt_key: str, raw_body: bytes) -> str:
    body = raw_body if isinstance(raw_body, bytes) else bytes(raw_body)
    source = f"{timestamp}{nonce}{encrypt_key}".encode("utf-8") + body
    return hashlib.sha256(source).hexdigest()


def verify_feishu_signature(
    *,
    raw_body: bytes,
    headers: Mapping[str, Any],
    encrypt_key: str,
    max_age_seconds: int = DEFAULT_FEISHU_SIGNATURE_MAX_AGE_SECONDS,
    now_fn: Callable[[], float] = time.time,
) -> None:
    timestamp = _header(headers, "X-Lark-Request-Timestamp")
    nonce = _header(headers, "X-Lark-Request-Nonce")
    signature = _header(headers, "X-Lark-Signature")
    if not (timestamp and nonce and signature):
        raise AgentToolError(
            code="PERMISSION_DENIED",
            message="missing Feishu event signature headers",
        )
    if max_age_seconds > 0:
        try:
            age = abs(float(now_fn()) - float(timestamp))
        except Exception as exc:
            raise AgentToolError(
                code="PERMISSION_DENIED",
                message="invalid Feishu event signature timestamp",
            ) from exc
        if age > max_age_seconds:
            raise AgentToolError(
                code="PERMISSION_DENIED",
                message="stale Feishu event signature timestamp",
                details={"max_age_seconds": max_age_seconds},
            )
    expected = build_feishu_signature(
        timestamp=timestamp,
        nonce=nonce,
        encrypt_key=encrypt_key,
        raw_body=raw_body,
    )
    if not hmac.compare_digest(expected, signature):
        raise AgentToolError(
            code="PERMISSION_DENIED",
            message="invalid Feishu event signature",
        )


def handle_feishu_gateway_http(
    *,
    raw_body: bytes,
    headers: Mapping[str, Any],
    settings: FeishuGatewaySettings,
    reply_fn: ReplyFn = reply_text_message,
    execute_tool_fn: ExecuteToolFn | None = None,
    now_fn: Callable[[], float] = time.time,
) -> tuple[int, dict[str, Any]]:
    try:
        payload = _load_payload(raw_body)
        _verify_request(raw_body=raw_body, headers=headers, settings=settings, now_fn=now_fn)
        if _is_encrypted_payload(payload):
            payload = decrypt_feishu_event_payload(payload, settings.encrypt_key)
        _verify_token(payload, settings.verification_token)

        if _is_url_verification(payload):
            inbound = handle_feishu_payload(payload)
            data_raw = inbound.get("data")
            data = cast(dict[str, Any], data_raw) if isinstance(data_raw, dict) else {}
            response_raw = data.get("response")
            response = cast(dict[str, Any], response_raw) if isinstance(response_raw, dict) else {}
            return 200, response or {"challenge": data.get("challenge")}

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
        reply_status = _maybe_reply(
            inbound=inbound,
            settings=settings,
            reply_fn=reply_fn,
        )
        return 200, build_response(
            tool_name="inbound.feishu_gateway",
            ok=bool(inbound.get("ok", False)) and bool(reply_status.get("ok", True)),
            data={
                "event": _event_summary(payload),
                "inbound": inbound,
                "reply": reply_status,
            },
            error=inbound.get("error") if not bool(inbound.get("ok", False)) else None,
        )
    except AgentToolError as err:
        status = 401 if err.code == "PERMISSION_DENIED" else 400
        return status, build_response(
            tool_name="inbound.feishu_gateway",
            ok=False,
            error=build_error_payload(err),
        )


def create_feishu_gateway_app(
    settings: FeishuGatewaySettings,
    *,
    reply_fn: ReplyFn = reply_text_message,
    execute_tool_fn: ExecuteToolFn | None = None,
) -> Any:
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse

    app = FastAPI(title="options-monitor Feishu inbound gateway")

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {"ok": True, "service": "options-monitor-feishu-gateway"}

    @app.post(settings.normalized_path)
    async def receive(request: Request) -> JSONResponse:
        raw_body = await request.body()
        status, payload = handle_feishu_gateway_http(
            raw_body=raw_body,
            headers=dict(request.headers),
            settings=settings,
            reply_fn=reply_fn,
            execute_tool_fn=execute_tool_fn,
        )
        return JSONResponse(payload, status_code=status)

    return app


def decrypt_feishu_event_payload(payload: dict[str, Any], encrypt_key: str) -> dict[str, Any]:
    encrypted = str(payload.get("encrypt") or "").strip()
    if not encrypted:
        return payload
    key_text = str(encrypt_key or "").strip()
    if not key_text:
        raise AgentToolError(
            code="CONFIG_ERROR",
            message="missing Feishu inbound encrypt key for encrypted event payload",
        )
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes  # pyright: ignore[reportMissingImports]
    except Exception as exc:
        raise AgentToolError(
            code="CONFIG_ERROR",
            message="Feishu encrypted event support requires server dependencies",
            hint="Install requirements/server.txt before enabling encrypted Feishu events.",
        ) from exc

    try:
        key = hashlib.sha256(key_text.encode("utf-8")).digest()
        raw = b64decode(encrypted)
        cipher = Cipher(algorithms.AES(key), modes.CBC(key[:16]))
        decryptor = cipher.decryptor()
        padded = decryptor.update(raw) + decryptor.finalize()
        if not padded:
            raise ValueError("empty decrypted payload")
        pad = int(padded[-1])
        if pad <= 0 or pad > 16:
            raise ValueError("invalid PKCS7 padding")
        plaintext = padded[:-pad]
        parsed = json.loads(plaintext.decode("utf-8"))
    except Exception as exc:
        raise AgentToolError(
            code="INPUT_ERROR",
            message="failed to decrypt Feishu event payload",
            details={"error": f"{type(exc).__name__}: {exc}"},
        ) from exc
    if not isinstance(parsed, dict):
        raise AgentToolError(
            code="INPUT_ERROR",
            message="decrypted Feishu event payload must be a JSON object",
        )
    return cast(dict[str, Any], parsed)


def serve_feishu_gateway(settings: FeishuGatewaySettings) -> None:
    settings.validate_for_serve()
    try:
        import uvicorn
    except Exception as exc:
        raise AgentToolError(
            code="CONFIG_ERROR",
            message="Feishu gateway server dependencies are missing",
            hint="Install requirements/server.txt before running the gateway.",
        ) from exc

    uvicorn.run(
        create_feishu_gateway_app(settings),
        host=settings.host,
        port=int(settings.port),
        ssl_certfile=settings.tls_certfile,
        ssl_keyfile=settings.tls_keyfile,
    )


def _maybe_reply(
    *,
    inbound: dict[str, Any],
    settings: FeishuGatewaySettings,
    reply_fn: ReplyFn,
) -> dict[str, Any]:
    data_raw = inbound.get("data")
    data = cast(dict[str, Any], data_raw) if isinstance(data_raw, dict) else {}
    if data.get("kind") != "message":
        return {"attempted": False, "ok": True, "reason": "not_message"}

    inbound_result_raw = data.get("inbound_result")
    inbound_result = cast(dict[str, Any], inbound_result_raw) if isinstance(inbound_result_raw, dict) else {}
    if _inbound_error_code(inbound_result) == "PERMISSION_DENIED":
        return {"attempted": False, "ok": True, "reason": "permission_denied"}
    if not settings.reply_enabled:
        return {"attempted": False, "ok": True, "reason": "reply_disabled"}

    response_text = _trim_reply(str(data.get("response_text") or ""), max_chars=settings.max_reply_chars)
    if not response_text:
        return {"attempted": False, "ok": True, "reason": "empty_response"}
    if not (settings.app_id and settings.app_secret):
        return {"attempted": True, "ok": False, "reason": "missing_app_credentials"}

    request_raw = data.get("request")
    request = cast(dict[str, Any], request_raw) if isinstance(request_raw, dict) else {}
    message_id = str(request.get("message_id") or "").strip()
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


def _verify_request(
    *,
    raw_body: bytes,
    headers: Mapping[str, Any],
    settings: FeishuGatewaySettings,
    now_fn: Callable[[], float],
) -> None:
    if settings.require_signature:
        if not settings.encrypt_key:
            raise AgentToolError(
                code="CONFIG_ERROR",
                message="missing Feishu inbound encrypt key for signature verification",
            )
        verify_feishu_signature(
            raw_body=raw_body,
            headers=headers,
            encrypt_key=settings.encrypt_key,
            max_age_seconds=int(settings.signature_max_age_seconds),
            now_fn=now_fn,
        )


def _verify_token(payload: dict[str, Any], expected_token: str) -> None:
    expected = str(expected_token or "").strip()
    if not expected:
        return
    header = _dict(payload.get("header"))
    actual = _first_text(payload.get("token"), header.get("token"))
    if actual != expected:
        raise AgentToolError(
            code="PERMISSION_DENIED",
            message="invalid Feishu verification token",
        )


def _load_payload(raw_body: bytes) -> dict[str, Any]:
    try:
        parsed = json.loads(raw_body.decode("utf-8"))
    except Exception as exc:
        raise AgentToolError(
            code="INPUT_ERROR",
            message="failed to parse Feishu event JSON",
            details={"error": f"{type(exc).__name__}: {exc}"},
        ) from exc
    if not isinstance(parsed, dict):
        raise AgentToolError(
            code="INPUT_ERROR",
            message="Feishu event payload must be a JSON object",
        )
    return cast(dict[str, Any], parsed)


def _event_summary(payload: dict[str, Any]) -> dict[str, Any]:
    header = _dict(payload.get("header"))
    event = _dict(payload.get("event"))
    message = _dict(event.get("message"))
    return {
        "event_id": _first_text(header.get("event_id")),
        "event_type": _first_text(header.get("event_type"), event.get("type")),
        "message_id": _first_text(message.get("message_id")),
    }


def _is_url_verification(payload: dict[str, Any]) -> bool:
    return str(payload.get("type") or "").strip() == "url_verification" or bool(payload.get("challenge"))


def _is_encrypted_payload(payload: dict[str, Any]) -> bool:
    return bool(payload.get("encrypt")) and not (payload.get("event") or payload.get("challenge"))


def _inbound_error_code(inbound_result: dict[str, Any]) -> str | None:
    error_raw = inbound_result.get("error")
    error = cast(dict[str, Any], error_raw) if isinstance(error_raw, dict) else {}
    return _first_text(error.get("code"))


def _inbound_command_id(inbound_result: dict[str, Any]) -> str | None:
    data_raw = inbound_result.get("data")
    data = cast(dict[str, Any], data_raw) if isinstance(data_raw, dict) else {}
    return _first_text(data.get("command_id"))


def _trim_reply(text: str, *, max_chars: int) -> str:
    value = str(text or "").strip()
    if max_chars <= 0 or len(value) <= max_chars:
        return value
    return value[: max(0, max_chars - 20)].rstrip() + "\n...(已截断)"


def _header(headers: Mapping[str, Any], name: str) -> str:
    target = name.lower()
    for key, value in headers.items():
        if str(key).lower() == target:
            return str(value or "").strip()
    return ""


def _dict(value: Any) -> dict[str, Any]:
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


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
