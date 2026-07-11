from __future__ import annotations

from typing import Any

from src.application.agent_tool_contracts import AgentToolError
from src.application.agent_tools.base import AgentTool, AgentToolContext, build_agent_tool
from src.application.notification_perception_read import read_notification_perception_events


_OUTPUT_CONTRACT: dict[str, Any] = {
    "schema_version": "notification_perception_read.output.v1",
    "source_label": "OM tick audit assistant_perception events",
    "primary_rows": "events",
    "fact_fields": [
        "summary.total_count",
        "summary.returned_count",
        "events[].run_id",
        "events[].event_kind",
        "events[].threshold_met",
        "events[].delivery.action",
        "events[].delivery.reason",
        "events[].send_summary.send_confirmed_count",
    ],
}


def _notification_perception_read_tool(
    ctx: AgentToolContext,
    payload: dict[str, Any],
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    data = read_notification_perception_events(
        repo_root=ctx.repo_base(),
        run_id=payload.get("run_id"),
        conversation_id=payload.get("conversation_id"),
        event_kind=payload.get("event_kind"),
        limit=int(payload.get("limit") or 10),
    )
    return data, [], {"audit_paths": [ctx.mask_path(path) for path in data.get("audit_paths") or []]}


def _notification_perception_input_validator(payload: dict[str, Any]) -> None:
    if str(payload.get("audit_path") or "").strip():
        raise AgentToolError(
            code="INPUT_ERROR",
            message="audit_path is not accepted by notification_perception_read; use run_id, conversation_id, event_kind, and limit",
        )


NOTIFICATION_PERCEPTION_READ_TOOL = build_agent_tool(
    name="notification_perception_read",
    description="Read compressed notification perception events from tick audit artifacts without sending notifications.",
    requires=("runtime_artifacts",),
    capabilities=("notification_perception", "audit_tail", "read_only", "runtime_artifacts"),
    input_schema={
        "run_id": "optional output_runs id; omitted reads output_shared/state/audit_events.jsonl",
        "conversation_id": "optional assistant conversation scope such as wechat:<chat_key>",
        "event_kind": "optional event kind filter",
        "limit": "optional number of events to return; defaults to 10",
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
)


TOOLS: tuple[AgentTool, ...] = (NOTIFICATION_PERCEPTION_READ_TOOL,)


__all__ = ["NOTIFICATION_PERCEPTION_READ_TOOL", "TOOLS"]
