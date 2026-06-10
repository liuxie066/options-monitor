from __future__ import annotations

from typing import Any

from src.application.agent_tools.base import AgentTool, AgentToolContext, build_agent_tool


_CLOSE_ADVICE_READ_OUTPUT_CONTRACT: dict[str, Any] = {
    "schema_version": "close_advice_read.output.v1",
    "canonical_renderer": "position_exit_analysis",
    "source_label": "OM 本地 Close Advice 报告",
    "guard_profile": "position_exit_analysis",
    "primary_rows": "rows",
    "row_count_field": "row_count",
    "fact_fields": [
        "query",
        "source.run_id",
        "source.paths[]",
        "rows[].account",
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
    ],
}


def _close_advice_read_tool(
    ctx: AgentToolContext,
    payload: dict[str, Any],
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    from src.application.agent_tool_close_advice_read import close_advice_read_tool

    return close_advice_read_tool(
        payload,
        load_runtime_config=ctx.load_runtime_config,
        resolve_output_root=ctx.resolve_output_root,
        repo_base=ctx.repo_base,
        mask_path=ctx.mask_path,
    )


CLOSE_ADVICE_READ_TOOL = build_agent_tool(
    name="close_advice_read",
    description="Read existing close-advice report rows and filter them without refreshing market data or writing reports.",
    requires=("local_close_advice_report",),
    capabilities=("close_advice", "position_exit_analysis", "read_only"),
    input_schema={
        "config_key": "us|hk",
        "config_path": "optional explicit config path",
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
    answer_policy="facts_then_analysis",
    output_contract=_CLOSE_ADVICE_READ_OUTPUT_CONTRACT,
)

TOOLS: tuple[AgentTool, ...] = (CLOSE_ADVICE_READ_TOOL,)


__all__ = ["CLOSE_ADVICE_READ_TOOL", "TOOLS"]
