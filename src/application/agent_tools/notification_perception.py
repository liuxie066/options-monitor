from __future__ import annotations

from typing import Any

from src.application.agent_tool_contracts import AgentToolError
from src.application.agent_tools.base import AgentTool, build_agent_tool
from src.application.agent_tool_contracts import mask_path
from src.application.agent_tool_config import repo_base
from src.application.notification_perception_read import read_notification_perception_events
from src.application.runtime_paths import resolve_runtime_root


_OUTPUT_CONTRACT: dict[str, Any] = {
    "schema_version": "notification_perception_read.output.v1",
    "source_label": "OM tick audit assistant_perception events",
    "primary_rows": "events",
    "fact_fields": [
        "summary.total_count",
        "summary.returned_count",
        "summary.status",
        "summary.malformed_count",
        "summary.unreadable_count",
        "read_statuses[].status",
        "read_statuses[].malformed_count",
        "events[].run_id",
        "events[].event_kind",
        "events[].threshold_met",
        "events[].delivery.action",
        "events[].delivery.reason",
        "events[].send_summary.send_confirmed_count",
        "coverage.total_count",
        "coverage.returned_count",
    ],
    "freshness_fields": ["freshness.kind", "freshness.latest_event_at_utc"],
    "model_preview_fields": [
        "scope",
        "coverage",
        "freshness",
        "read_statuses",
        "events",
    ],
}


def _notification_perception_read_tool(
    payload: dict[str, Any],
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    authenticated_conversation = str(payload.get("authenticated_conversation_id") or "").strip()
    explicit_conversation = str(payload.get("conversation_id") or "").strip()
    if authenticated_conversation and explicit_conversation and authenticated_conversation != explicit_conversation:
        raise AgentToolError(
            code="PERMISSION_DENIED",
            message="notification perception scope cannot override the authenticated conversation",
        )
    conversation_id = authenticated_conversation or explicit_conversation or None
    runtime_resolution = resolve_runtime_root(
        repo_root=repo_base(),
        runtime_root=payload.get("runtime_root"),
    )
    data = read_notification_perception_events(
        repo_root=runtime_resolution.runtime_root,
        run_id=payload.get("run_id"),
        conversation_id=conversation_id,
        event_kind=payload.get("event_kind"),
        limit=int(payload.get("limit") or 10),
    )
    summary = data.get("summary") if isinstance(data.get("summary"), dict) else {}
    events = data.get("events") if isinstance(data.get("events"), list) else []
    data["source"] = {"label": "OM tick audit notification perception events", "kind": "audit_snapshot"}
    data["runtime_root"] = {
        "path": mask_path(runtime_resolution.runtime_root),
        "source": runtime_resolution.source,
    }
    data["scope"] = {
        "run_id": summary.get("run_id"),
        "conversation_ref": summary.get("conversation_ref"),
        "event_kind": summary.get("event_kind"),
    }
    data["coverage"] = {
        "total_count": summary.get("total_count", 0),
        "returned_count": summary.get("returned_count", 0),
        "limit": summary.get("limit"),
        "malformed_count": summary.get("malformed_count", 0),
        "unreadable_count": summary.get("unreadable_count", 0),
    }
    data["freshness"] = {
        "kind": "audit_snapshot",
        "latest_event_at_utc": events[0].get("created_at_utc") if events and isinstance(events[0], dict) else None,
    }
    warnings: list[str] = []
    if summary.get("status") == "failed":
        warnings.append("Notification perception audit is unreadable.")
    elif summary.get("status") == "partial":
        warnings.append(
            "Notification perception audit is partially corrupt; "
            f"malformed_rows={summary.get('malformed_count', 0)}."
        )
    return data, warnings, {
        "audit_paths": [
            mask_path(path) for path in data.get("audit_paths") or []
        ],
        "runtime_root_source": runtime_resolution.source,
    }


def _notification_perception_input_validator(payload: dict[str, Any]) -> None:
    if str(payload.get("audit_path") or "").strip():
        raise AgentToolError(
            code="INPUT_ERROR",
            message="audit_path is not accepted by notification_perception_read; use run_id, conversation_id, event_kind, and limit",
        )


NOTIFICATION_PERCEPTION_READ_TOOL = build_agent_tool(
    name="notification_perception_read",
    description=(
        "Read notification decision and delivery evidence from tick audit artifacts. Use to explain whether a "
        "notification threshold was met, skipped, attempted, or confirmed; this tool never sends a notification."
    ),
    requires=("runtime_artifacts",),
    capabilities=("notification_perception", "audit_tail", "read_only", "runtime_artifacts"),
    input_schema={
        "run_id": "optional output_runs id; omitted reads output_shared/state/audit_events.jsonl",
        "conversation_id": "optional assistant conversation scope such as wechat:<chat_key>",
        "authenticated_conversation_id": "host-injected authenticated conversation scope",
        "event_kind": "optional event kind filter",
        "limit": "optional number of events to return; defaults to 10",
        "runtime_root": (
            "optional canonical runtime root; defaults to "
            "OM_RUNTIME_ROOT then repository fallback"
        ),
    },
    handler=_notification_perception_read_tool,
    pure_read=True,
    safe_default_input={"limit": 10},
    input_validator=_notification_perception_input_validator,
    examples=(
        {"input": {"limit": 10}},
        {"input": {"run_id": "20260515T182459Z-474761"}},
    ),
    output_contract=_OUTPUT_CONTRACT,
    copilot_input_fields=(
        "run_id",
        "event_kind",
        "limit",
        "runtime_root",
    ),
)


TOOLS: tuple[AgentTool, ...] = (NOTIFICATION_PERCEPTION_READ_TOOL,)


__all__ = ["NOTIFICATION_PERCEPTION_READ_TOOL", "TOOLS"]
