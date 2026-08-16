from __future__ import annotations

from typing import Any

from src.application.agent_tool_contracts import AgentToolError
from src.application.agent_tools.base import AgentTool, build_agent_tool
from src.application.quality.service import OMQualityService


def _quality_status_tool(
    payload: dict[str, Any],
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    account = str(payload.get("account") or "").strip().lower()
    market = str(payload.get("market") or "").strip().lower()
    dataset_id = str(payload.get("dataset_id") or "").strip()
    status = OMQualityService().read_published(
        consumer="quality_status",
        account=account or None,
        market=market or None,
        lifecycle_rows_requested=(
            not dataset_id
            or dataset_id
            in {"om.lifecycle_evidence", "om.lifecycle_history"}
        ),
    )
    if status is None:
        raise AgentToolError(
            code="QUALITY_STATUS_UNAVAILABLE",
            message="No valid published OM quality status is available.",
        )
    if not any((account, market, dataset_id)):
        return status, [], {"artifact": "om-quality-status"}
    projected = dict(status)
    projected["datasets"] = [
        item
        for item in status.get("datasets") or []
        if isinstance(item, dict)
        and (not dataset_id or item.get("dataset_id") == dataset_id)
        and (not account or str((item.get("scope") or {}).get("account") or "").lower() == account)
        and (not market or str((item.get("scope") or {}).get("market") or "").lower() == market)
    ]
    return projected, [], {"artifact": "om-quality-status"}


QUALITY_STATUS_TOOL = build_agent_tool(
    name="quality_status",
    description="Read the latest schema-validated OM runtime and data-quality artifact without refreshing OpenD or writing state.",
    requires=("published_quality_artifact",),
    capabilities=("runtime_quality", "data_quality", "read_only"),
    input_schema={
        "account": "optional lowercase account filter",
        "market": {"type": "string", "enum": ["us", "hk"], "description": "optional market filter"},
        "dataset_id": "optional exact dataset id",
    },
    handler=_quality_status_tool,
    pure_read=True,
    safe_default_input={},
    examples=({"input": {}}, {"input": {"account": "lx", "market": "us"}}),
    output_contract={
        "schema_version": "investment.quality_status.v1",
        "source_label": "OM published quality artifact",
        "primary_rows": "datasets",
        "freshness_fields": ["observed_at_utc", "datasets[].as_of_utc"],
        "missing_data_fields": ["datasets[].required_evidence_complete"],
    },
)

TOOLS: tuple[AgentTool, ...] = (QUALITY_STATUS_TOOL,)

__all__ = ["QUALITY_STATUS_TOOL", "TOOLS"]
