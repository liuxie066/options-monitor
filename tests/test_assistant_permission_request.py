from __future__ import annotations

from src.application.assistant.contracts import AssistantRequest
from src.application.assistant.permission_request import build_permission_request


def test_permission_request_marks_upgrade_preview_as_admin() -> None:
    request = AssistantRequest(
        text="立即升级",
        sender_id="ou_1",
        channel="feishu",
        message_id="msg_1",
        conversation_id="chat_1",
        config_key="us",
    )
    operation = {
        "operation_id": "in_upgrade_1",
        "operation_type": "upgrade_now",
        "status": "previewed",
        "created_at": "2026-06-13T10:00:00+00:00",
        "expires_at": "2026-06-13T10:05:00+00:00",
        "payload": {"operation_type": "upgrade_now", "arguments": {"target_version": "1.2.200"}},
        "preview": {"summary": {"current_version": "1.2.199", "target_version": "1.2.200", "status": "upgrade_available"}},
    }

    permission_request = build_permission_request(operation=operation, request=request)

    assert permission_request == {
        "schema_version": "om-agent-permission-request-v1",
        "operation_id": "in_upgrade_1",
        "operation_type": "upgrade_now",
        "risk_class": "preview_admin",
        "safety_class": "admin_preview",
        "status": "previewed",
        "confirm_required": True,
        "apply_allowed": False,
        "created_at": "2026-06-13T10:00:00+00:00",
        "expires_at": "2026-06-13T10:05:00+00:00",
        "scope": {
            "channel": "feishu",
            "sender": "ou_1",
            "conversation": "chat_1",
            "config_key": "us",
        },
        "target_summary": "1.2.199 -> 1.2.200 status upgrade_available",
        "evidence_refs": ["pending_operation:in_upgrade_1", "preview:upgrade_now"],
        "confirm_hint": "/confirm upgrade in_upgrade_1",
        "cancel_hint": "/cancel upgrade in_upgrade_1",
    }
