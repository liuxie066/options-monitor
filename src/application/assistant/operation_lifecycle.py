from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from src.application.agent_tool_contracts import AgentToolError, build_response, mask_path
from src.application.assistant.contracts import AssistantRequest
from src.application.assistant.operation_signature import hash_operation_payload, verify_operation_signature
from src.application.assistant.operation_status_text import cannot_repeat_message
from src.application.assistant.operation_store import InboundOperationStore, operation_is_expired
from src.application.assistant.permission_request import build_permission_request


CandidateHintFn = Callable[[str, Any], str]
PreviewResponseTextFn = Callable[[dict[str, Any]], str]


@dataclass(frozen=True)
class ConfirmedOperation:
    operation_id: str
    operation: dict[str, Any]
    operation_resolution: dict[str, Any]
    payload: dict[str, Any]
    payload_hash: str


def resolve_pending_operation_or_raise(
    *,
    operation_id: str | None,
    request: AssistantRequest,
    store: InboundOperationStore,
    operation_types: set[str] | frozenset[str],
    allow_expired: bool,
    action: str,
    subject: str,
    expired_message: str,
    expired_hint: str,
    none_hint: str,
    wrong_family_message: str,
    not_found_message: str,
    not_found_hint: str,
    candidate_hint: CandidateHintFn,
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    resolution = store.resolve_pending_operation(
        channel=request.channel,
        sender_id=request.sender_id,
        operation_types=operation_types,
        conversation_id=request.conversation_id,
        explicit_operation_id=operation_id,
        allow_expired=allow_expired,
    )
    details = _operation_resolution_details(resolution)
    status = str(resolution.get("status") or "")
    resolved_operation_id = str(resolution.get("operation_id") or operation_id or "").strip()
    operation_raw = resolution.get("operation")
    operation = operation_raw if isinstance(operation_raw, dict) else {}
    if status == "resolved" and resolved_operation_id and operation:
        return resolved_operation_id, operation, details
    if status == "expired":
        result = {"operation_id": resolved_operation_id, "status": "expired"}
        if resolved_operation_id:
            store.mark_expired(resolved_operation_id, result=result)
        raise AgentToolError(
            code="NEEDS_CLARIFICATION",
            message=expired_message,
            hint=expired_hint,
            details={**result, **details},
        )
    if status == "ambiguous":
        raise AgentToolError(
            code="NEEDS_CLARIFICATION",
            message=f"有多条待{action}的{subject}，请带 operation_id。",
            hint=candidate_hint(action, details.get("candidate_operations")),
            details=details,
        )
    if status == "none":
        raise AgentToolError(
            code="NEEDS_CLARIFICATION",
            message=f"没有可{action}的{subject}。",
            hint=none_hint,
            details=details,
        )
    if status == "forbidden":
        raise AgentToolError(
            code="PERMISSION_DENIED",
            message=f"只能由创建该预览的同一 sender/对话 {action}。",
            details=details,
        )
    if status == "wrong_family":
        raise AgentToolError(code="INPUT_ERROR", message=wrong_family_message, details=details)
    if status == "invalid_status":
        current_status = str(operation.get("status") or "-")
        raise AgentToolError(
            code="INPUT_ERROR",
            message=cannot_repeat_message(subject, action, current_status),
            details=details,
        )
    raise AgentToolError(code="INPUT_ERROR", message=not_found_message, hint=not_found_hint, details=details)


def build_cancelled_operation_response(
    *,
    tool_name: str,
    operation_id: str,
    operation: dict[str, Any],
    operation_resolution: dict[str, Any],
    store: InboundOperationStore,
    response_text: str,
) -> dict[str, Any]:
    payload = operation.get("payload") if isinstance(operation.get("payload"), dict) else {}
    preview = operation.get("preview") if isinstance(operation.get("preview"), dict) else {}
    result = {"operation_id": operation_id, "status": "cancelled"}
    store.mark_cancelled(operation_id, result=result)
    return build_response(
        tool_name=tool_name,
        ok=True,
        data={
            "operation_id": operation_id,
            **operation_resolution,
            "operation_type": operation.get("operation_type"),
            "status": "cancelled",
            "payload_hash": operation.get("payload_hash"),
            "payload": payload,
            "preview": preview,
            "result": result,
            "response_text": response_text,
        },
        meta={"audit_db": mask_path(store.path)},
    )


def build_previewed_operation_response(
    *,
    tool_name: str,
    operation_id: str,
    request: AssistantRequest,
    store: InboundOperationStore,
    payload: dict[str, Any],
    preview: dict[str, Any],
    ttl_seconds: int,
    response_text: PreviewResponseTextFn,
) -> dict[str, Any]:
    payload_hash = hash_operation_payload(payload)
    operation = store.save_preview(
        operation_id=operation_id,
        command_id=operation_id,
        channel=request.channel,
        sender_id=request.sender_id,
        conversation_id=request.conversation_id,
        operation_type=str(payload["operation_type"]),
        payload_hash=payload_hash,
        payload=payload,
        preview=preview,
        ttl_seconds=ttl_seconds,
    )
    permission_request = build_permission_request(operation=operation, request=request)
    return build_response(
        tool_name=tool_name,
        ok=True,
        data={
            "operation_id": operation_id,
            "operation_type": payload["operation_type"],
            "status": "previewed",
            "payload_hash": payload_hash,
            "payload": payload,
            "preview": preview,
            "expires_at": operation.get("expires_at"),
            "permission_request": permission_request,
            "response_text": response_text(operation),
        },
        meta={"audit_db": mask_path(store.path)},
    )


def confirm_previewed_operation_or_raise(
    *,
    operation_id: str,
    operation: dict[str, Any],
    operation_resolution: dict[str, Any],
    store: InboundOperationStore,
    subject: str,
    expired_message: str,
    expired_hint: str,
    hash_mismatch_message: str,
    confirmed_result: dict[str, Any] | None = None,
) -> ConfirmedOperation:
    if operation_is_expired(operation):
        result = {"operation_id": operation_id, "status": "expired"}
        store.mark_expired(operation_id, result=result)
        raise AgentToolError(
            code="NEEDS_CLARIFICATION",
            message=expired_message,
            hint=expired_hint,
            details={**result, **operation_resolution},
        )
    payload = dict(operation["payload"])
    stored_hash = str(operation.get("payload_hash") or "")
    current_hash = hash_operation_payload(payload)
    if stored_hash != current_hash:
        result = {"operation_id": operation_id, "status": "failed", "reason": "payload_hash_mismatch"}
        store.mark_failed(operation_id, result=result)
        raise AgentToolError(
            code="INTERNAL_ERROR",
            message=hash_mismatch_message,
            details=result,
        )
    verify_operation_signature(operation)
    if not store.mark_confirmed(operation_id, result=confirmed_result):
        current = store.get(operation_id) or {}
        current_status = str(current.get("status") or "-")
        raise AgentToolError(
            code="INPUT_ERROR",
            message=cannot_repeat_message(subject, "确认", current_status),
            details={
                "operation_id": operation_id,
                "status": current_status,
                "reason": "operation_not_previewed",
                **operation_resolution,
            },
        )
    return ConfirmedOperation(
        operation_id=operation_id,
        operation=operation,
        operation_resolution=operation_resolution,
        payload=payload,
        payload_hash=current_hash,
    )


def _operation_resolution_details(resolution: dict[str, Any]) -> dict[str, Any]:
    return {
        "operation_resolution": resolution.get("operation_resolution"),
        "resolved_operation_id": resolution.get("operation_id"),
        "candidate_operations": resolution.get("candidate_operations") or [],
    }
