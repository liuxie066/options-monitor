from __future__ import annotations

from collections.abc import Callable
from typing import Any

from src.application.agent_tool_contracts import AgentToolError, build_response, mask_path
from src.application.assistant.contracts import AssistantRequest
from src.application.assistant.operation_status_text import cannot_repeat_message
from src.application.assistant.operation_store import InboundOperationStore


CandidateHintFn = Callable[[str, Any], str]


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
            "result": result,
            "response_text": response_text,
        },
        meta={"audit_db": mask_path(store.path)},
    )


def _operation_resolution_details(resolution: dict[str, Any]) -> dict[str, Any]:
    return {
        "operation_resolution": resolution.get("operation_resolution"),
        "resolved_operation_id": resolution.get("operation_id"),
        "candidate_operations": resolution.get("candidate_operations") or [],
    }
