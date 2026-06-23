from __future__ import annotations

from typing import Any

from src.application.assistant.contracts import AssistantRequest
from src.application.assistant.operation_store import operation_summary


PERMISSION_REQUEST_SCHEMA_VERSION = "om-agent-permission-request-v1"


def build_permission_request(
    *,
    operation: dict[str, Any],
    request: AssistantRequest,
    confirm_hint: str | None = None,
    cancel_hint: str | None = None,
) -> dict[str, Any]:
    operation_id = str(operation.get("operation_id") or "").strip()
    operation_type = str(operation.get("operation_type") or "").strip()
    risk_class = _risk_class(operation_type)
    return {
        "schema_version": PERMISSION_REQUEST_SCHEMA_VERSION,
        "operation_id": operation_id,
        "operation_type": operation_type,
        "risk_class": risk_class,
        "safety_class": _safety_class(risk_class),
        "status": str(operation.get("status") or "previewed"),
        "confirm_required": True,
        "apply_allowed": False,
        "created_at": operation.get("created_at"),
        "expires_at": operation.get("expires_at"),
        "scope": {
            "channel": request.channel,
            "sender": request.sender_id,
            "conversation": request.conversation_id,
            "config_key": request.config_key,
        },
        "target_summary": _target_summary(operation),
        "evidence_refs": _evidence_refs(operation),
        "confirm_hint": confirm_hint or _confirm_hint(operation_type, operation_id),
        "cancel_hint": cancel_hint or _cancel_hint(operation_type, operation_id),
    }


def _risk_class(operation_type: str) -> str:
    if operation_type in {"upgrade_now", "monitor_run_now"}:
        return "preview_admin"
    return "preview_write"


def _safety_class(risk_class: str) -> str:
    return "admin_preview" if risk_class == "preview_admin" else "write_preview"


def _target_summary(operation: dict[str, Any]) -> str:
    summary = operation_summary(operation).get("summary")
    text = str(summary or "").strip()
    return text or str(operation.get("operation_type") or "-")


def _evidence_refs(operation: dict[str, Any]) -> list[str]:
    refs = []
    operation_id = str(operation.get("operation_id") or "").strip()
    if operation_id:
        refs.append(f"pending_operation:{operation_id}")
    operation_type = str(operation.get("operation_type") or "").strip()
    if operation_type:
        refs.append(f"preview:{operation_type}")
    return refs


def _confirm_hint(operation_type: str, operation_id: str) -> str:
    command = _operation_command_family(operation_type)
    return f"/confirm {command} {operation_id}".strip()


def _cancel_hint(operation_type: str, operation_id: str) -> str:
    command = _operation_command_family(operation_type)
    return f"/cancel {command} {operation_id}".strip()


def _operation_command_family(operation_type: str) -> str:
    if operation_type.startswith("manual_"):
        return "trade"
    if operation_type.startswith("symbol_"):
        return "symbol"
    if operation_type == "upgrade_now":
        return "upgrade"
    if operation_type == "model_use":
        return "model"
    if operation_type == "monitor_run_now":
        return "monitor-run"
    return "operation"


__all__ = [
    "PERMISSION_REQUEST_SCHEMA_VERSION",
    "build_permission_request",
]
