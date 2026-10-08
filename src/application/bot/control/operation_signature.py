from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

from src.application.agent_tool_contracts import AgentToolError
from src.application.secret_store import INBOUND_OPERATION_HMAC_KEY, SecretProvider, resolve_secret


OPERATION_SIGNATURE_VERSION = "hmac-sha256-v1"
OPERATION_HMAC_KEY_ENV = "OM_INBOUND_OPERATION_HMAC_KEY"


def operation_hmac_key(*, secret_provider: SecretProvider | None = None) -> str:
    return str(
        resolve_secret(
            INBOUND_OPERATION_HMAC_KEY,
            provider=secret_provider,
            legacy_env_name=OPERATION_HMAC_KEY_ENV,
        )
        or ""
    )


def require_operation_hmac_key(*, secret_provider: SecretProvider | None = None) -> str:
    key = operation_hmac_key(secret_provider=secret_provider)
    if not key:
        raise AgentToolError(
            code="CONFIG_ERROR",
            message="inbound operation HMAC key is not configured",
            hint=f"Provision {INBOUND_OPERATION_HMAC_KEY} before enabling inbound write operations.",
        )
    return key


def hash_operation_payload(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def sign_operation_fields(*, operation: dict[str, Any], key: str | None = None) -> dict[str, str]:
    effective_key = key if key is not None else operation_hmac_key()
    if not effective_key:
        return {"signature_version": "", "signature": ""}
    message = _signature_message(operation)
    signature = hmac.new(effective_key.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()
    return {
        "signature_version": OPERATION_SIGNATURE_VERSION,
        "signature": signature,
    }


def verify_operation_signature(operation: dict[str, Any]) -> None:
    key = require_operation_hmac_key()
    version = str(operation.get("signature_version") or "").strip()
    actual = str(operation.get("signature") or "").strip()
    if version != OPERATION_SIGNATURE_VERSION or not actual:
        raise AgentToolError(
            code="PERMISSION_DENIED",
            message="pending operation signature is missing or invalid",
            details={
                "operation_id": operation.get("operation_id"),
                "signature_version": version or None,
            },
        )
    expected = sign_operation_fields(operation=operation, key=key)["signature"]
    if not hmac.compare_digest(actual, expected):
        raise AgentToolError(
            code="PERMISSION_DENIED",
            message="pending operation signature mismatch; refusing to confirm",
            details={"operation_id": operation.get("operation_id")},
        )


def _signature_message(operation: dict[str, Any]) -> str:
    payload = {
        "operation_id": str(operation.get("operation_id") or ""),
        "command_id": str(operation.get("command_id") or ""),
        "channel": str(operation.get("channel") or ""),
        "sender_id": str(operation.get("sender_id") or ""),
        "conversation_id": str(operation.get("conversation_id") or ""),
        "operation_type": str(operation.get("operation_type") or ""),
        "payload_hash": str(operation.get("payload_hash") or ""),
        "created_at": str(operation.get("created_at") or ""),
        "expires_at": str(operation.get("expires_at") or ""),
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


__all__ = [
    "OPERATION_HMAC_KEY_ENV",
    "OPERATION_SIGNATURE_VERSION",
    "hash_operation_payload",
    "operation_hmac_key",
    "require_operation_hmac_key",
    "sign_operation_fields",
    "verify_operation_signature",
]
