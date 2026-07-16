from __future__ import annotations

from typing import Any

from src.application.agent_tool_contracts import AgentToolError
from src.application.agent_tools.base import AgentTool, build_agent_tool
from src.application.agent_tool_contracts import mask_path
from src.application.agent_tool_config import repo_base
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
        "coverage.total_count",
        "coverage.returned_count",
    ],
    "freshness_fields": ["freshness.kind", "freshness.latest_event_at_utc"],
    "model_preview_fields": ["scope", "coverage", "freshness", "events"],
}


def _notification_perception_read_tool(
    payload: dict[str, Any],
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    data = read_notification_perception_events(
        repo_root=repo_base(),
        run_id=payload.get("run_id"),
        conversation_id=payload.get("conversation_id"),
        event_kind=payload.get("event_kind"),
        limit=int(payload.get("limit") or 10),
    )
    summary = data.get("summary") if isinstance(data.get("summary"), dict) else {}
    events = data.get("events") if isinstance(data.get("events"), list) else []
    data["source"] = {"label": "OM tick audit notification perception events", "kind": "audit_snapshot"}
    data["scope"] = {
        "run_id": summary.get("run_id"),
        "conversation_id": summary.get("conversation_id"),
        "event_kind": summary.get("event_kind"),
    }
    data["coverage"] = {
        "total_count": summary.get("total_count", 0),
        "returned_count": summary.get("returned_count", 0),
        "limit": summary.get("limit"),
    }
    data["freshness"] = {
        "kind": "audit_snapshot",
        "latest_event_at_utc": events[0].get("created_at_utc") if events and isinstance(events[0], dict) else None,
    }
    return data, [], {"audit_paths": [mask_path(path) for path in data.get("audit_paths") or []]}


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
    copilot_input_fields=("run_id", "conversation_id", "event_kind", "limit"),
)


TOOLS: tuple[AgentTool, ...] = (NOTIFICATION_PERCEPTION_READ_TOOL,)


__all__ = ["NOTIFICATION_PERCEPTION_READ_TOOL", "TOOLS"]
