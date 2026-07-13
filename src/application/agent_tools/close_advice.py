from __future__ import annotations

from typing import Any

from src.application.agent_tools.base import AgentTool, AgentToolContext, build_agent_tool


_CLOSE_ADVICE_READ_OUTPUT_CONTRACT: dict[str, Any] = {
    "schema_version": "close_advice_read.output.v1",
    "source_label": "OM 本地 Close Advice 报告",
    "primary_rows": "rows",
    "row_count_field": "returned_count",
    "fact_fields": [
        "query",
        "source.run_id",
        "source.paths[]",
        "rows[].account",
        "rows[].position_lot_id",
        "rows[].symbol",
        "rows[].side",
        "rows[].option_type",
        "rows[].expiration",
        "rows[].expiration_ymd",
        "rows[].strike",
        "rows[].close_action",
        "rows[].tier_label",
        "rows[].evaluation_status",
        "rows[].quote_status",
        "rows[].reason",
        "coverage.source_count",
        "coverage.matched_count",
        "coverage.returned_count",
        "coverage.truncated",
    ],
    "freshness_fields": ["freshness.kind", "freshness.run_ids[]"],
    "missing_data_fields": ["rows[].evaluation_status", "rows[].quote_status"],
    "model_preview_fields": ["scope", "coverage", "freshness", "summary", "rows"],
}


def _close_advice_read_tool(
    ctx: AgentToolContext,
    payload: dict[str, Any],
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    from src.application.agent_tools.close_advice_read_impl import close_advice_read_tool

    return close_advice_read_tool(
        payload,
        load_runtime_config=ctx.load_runtime_config,
        resolve_output_root=ctx.resolve_output_root,
        repo_base=ctx.repo_base,
        mask_path=ctx.mask_path,
    )


CLOSE_ADVICE_READ_TOOL = build_agent_tool(
    name="close_advice_read",
    description=(
        "Read and filter the latest existing close-advice report without refreshing market data or writing reports. "
        "Treat the returned run/source timestamp as the data freshness boundary."
    ),
    requires=("local_close_advice_report",),
    capabilities=("close_advice", "position_exit_analysis", "read_only"),
    input_schema={
        "config_key": {"type": "string", "enum": ["us", "hk"], "description": "Market config"},
        "config_path": "optional explicit config path",
        "market_scope": {
            "type": "string",
            "enum": ["us", "hk", "all"],
            "description": "Optional close-advice market filter; all searches cross-market reports",
        },
        "query": "optional object with account, symbol, option_type, side, strike, expiration, limit",
        "run_id": "optional output_runs run id",
        "runs_root": "optional output_runs root",
        "report_path": "optional explicit close_advice.csv path or containing directory",
        "csv_path": "optional explicit close_advice.csv path",
        "output_dir": "optional agent tool output root; uses <output_dir>/reports/close_advice.csv",
    },
    handler=_close_advice_read_tool,
    pure_read=True,
    safe_default_input={},
    examples=({"input": {"config_key": "us", "query": {"option_type": "call", "side": "long"}}},),
    output_contract=_CLOSE_ADVICE_READ_OUTPUT_CONTRACT,
    copilot_input_fields=("config_key", "market_scope", "query", "run_id"),
)

TOOLS: tuple[AgentTool, ...] = (CLOSE_ADVICE_READ_TOOL,)


__all__ = ["CLOSE_ADVICE_READ_TOOL", "TOOLS"]
