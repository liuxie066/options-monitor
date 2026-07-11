from __future__ import annotations

from typing import Any

from src.application.agent_tools.notifications_impl import preview_notification_tool
from src.application.agent_tools.base import AgentTool, AgentToolContext, build_agent_tool


_PREVIEW_NOTIFICATION_OUTPUT_CONTRACT: dict[str, Any] = {
    "schema_version": "preview_notification.output.v1",
    "source_label": "OM notification formatter",
    "result_shape": "scalar",
    "fact_fields": ["account_label", "notification_text"],
    "model_preview_fields": ["account_label", "notification_text"],
}


def _preview_notification_tool(
    ctx: AgentToolContext,
    payload: dict[str, Any],
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    return preview_notification_tool(payload, build_notification=ctx.build_notification)


PREVIEW_NOTIFICATION_TOOL = build_agent_tool(
    name="preview_notification",
    description="Build final notification text from alerts/changes without sending it.",
    requires=("alerts_or_changes_input",),
    capabilities=("notification_preview", "read_only"),
    input_schema={
        "alerts_text": "raw alert markdown text",
        "changes_text": "raw changes markdown text",
        "alerts_path": "optional file path when alerts_text omitted",
        "changes_path": "optional file path when changes_text omitted",
        "account_label": "optional account label",
    },
    handler=_preview_notification_tool,
    pure_read=True,
    safe_default_input={"alerts_text": "", "changes_text": ""},
    examples=(
        {
            "input": {
                "alerts_path": "output_shared/reports/symbols_alerts.txt",
                "changes_path": "output_shared/reports/symbols_changes.txt",
            }
        },
    ),
    output_contract=_PREVIEW_NOTIFICATION_OUTPUT_CONTRACT,
    copilot_input_fields=("alerts_text", "changes_text", "account_label"),
)

TOOLS: tuple[AgentTool, ...] = (PREVIEW_NOTIFICATION_TOOL,)


__all__ = ["PREVIEW_NOTIFICATION_TOOL", "TOOLS"]
