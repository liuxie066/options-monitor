from __future__ import annotations

from typing import Any

from src.application.agent_tools.base import AgentTool, build_agent_tool
from src.application.agent_tools.daily_brief import (
    DAILY_BRIEF_SELECTOR_INPUT_SCHEMA,
    query_daily_brief_tool_view,
    validate_daily_brief_query_input,
)
from src.application.agent_tools.notifications_impl import preview_notification_tool


_PREVIEW_NOTIFICATION_OUTPUT_CONTRACT: dict[str, Any] = {
    "evidence_type": "diagnostic",
    "bounded_projection": "contract_fields",
    "coverage": "primary_rows",
    "freshness": "source_declared",
    "pagination": {"mode": "none"},
    "schema_version": "preview_notification.output.v2",
    "source_label": "OM local daily_decision_brief.v1 successful current state",
    "result_shape": "scalar",
    "fact_fields": [
        "schema_version",
        "available",
        "reason",
        "query",
        "notification_text",
        "effective_actionability",
        "coverage",
        "source",
        "freshness",
        "renderer",
        "authority",
        "delivery_evidence",
    ],
    "freshness_fields": [
        "freshness.data_as_of_utc",
        "freshness.valid_until_utc",
        "freshness.effective_actionability",
    ],
    "missing_data_fields": ["reason"],
    "model_preview_fields": [
        "query",
        "available",
        "notification_text",
        "effective_actionability",
        "renderer",
        "authority",
        "delivery_evidence",
    ],
}


def _preview_notification_tool(
    payload: dict[str, Any],
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    return preview_notification_tool(
        payload,
        query_daily_brief=query_daily_brief_tool_view,
    )


PREVIEW_NOTIFICATION_TOOL = build_agent_tool(
    name="preview_notification",
    catalog_summary="预览已持久化的每日决策简报，不执行投递。",
    description=(
        "Project the persisted Daily Decision Brief through its canonical query renderer without "
        "scanning, sending, or changing delivery state."
    ),
    requires=("daily_decision_brief_state",),
    capabilities=("notification_preview", "daily_brief", "read_only", "runtime_artifacts"),
    input_schema=DAILY_BRIEF_SELECTOR_INPUT_SCHEMA,
    handler=_preview_notification_tool,
    pure_read=True,
    safe_default_input={},
    input_validator=validate_daily_brief_query_input,
    examples=(
        {"input": {}},
        {"input": {"account": "lx", "market": "US"}},
        {"input": {"account": "lx", "market": "US", "date": "2026-07-19", "revision": 0}},
    ),
    output_contract=_PREVIEW_NOTIFICATION_OUTPUT_CONTRACT,
    bot_input_fields=("account", "market", "date", "revision"),
    allow_additional_input=False,
)

TOOLS: tuple[AgentTool, ...] = (PREVIEW_NOTIFICATION_TOOL,)


__all__ = ["PREVIEW_NOTIFICATION_TOOL", "TOOLS"]
