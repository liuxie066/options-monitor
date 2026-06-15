from __future__ import annotations

from typing import Any

from domain.domain.strategy_vocab import (
    STRATEGY_CLOSE_ADVICE,
    STRATEGY_COVERED_CALL,
    STRATEGY_SELL_PUT,
    STRATEGY_YIELD_ENHANCEMENT,
    strategy_key_help,
)
from src.application.agent_tool_candidate_filter import candidate_filter_explain_tool
from src.application.agent_tool_candidate_rank import candidate_rank_explain_tool
from src.application.agent_tools.base import AgentTool, AgentToolContext, build_agent_tool
from src.application.symbol_aliases import symbol_aliases_from_config


_CANDIDATE_FILTER_OUTPUT_CONTRACT: dict[str, Any] = {
    "schema_version": "candidate_filter_explain.output.v1",
    "canonical_renderer": "candidate_filter_explain",
    "source_label": "OM candidate filter trace",
    "guard_profile": "candidate_filter",
    "primary_rows": "functions",
    "row_count_field": "trace_count",
    "fact_fields": [
        "symbol",
        "canonical_symbol",
        "raw_symbol",
        "scope.account",
        "scope.account_semantics",
        "trace_count",
        "status_counts",
        "function_counts",
        "functions[].function",
        "functions[].status",
        "functions[].reason_counts",
        "functions[].events[].rule",
        "functions[].events[].metric_value",
        "functions[].events[].threshold",
        "functions[].events[].message",
    ],
    "missing_data_fields": [
        "trace_count",
    ],
}


def _candidate_rank_explain_tool(
    ctx: AgentToolContext,
    payload: dict[str, Any],
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    return candidate_rank_explain_tool(
        payload,
        repo_base=ctx.repo_base,
        resolve_output_root=ctx.resolve_output_root,
        mask_path=ctx.mask_path,
    )


def _candidate_filter_explain_tool(
    ctx: AgentToolContext,
    payload: dict[str, Any],
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    config_path = None
    symbol_aliases = None
    if str(payload.get("config_key") or "").strip() or str(payload.get("config_path") or "").strip():
        config_path, cfg = ctx.load_runtime_config(
            config_key=payload.get("config_key"),
            config_path=payload.get("config_path"),
        )
        symbol_aliases = symbol_aliases_from_config(cfg)

    data, warnings, meta = candidate_filter_explain_tool(
        payload,
        repo_base=ctx.repo_base,
        mask_path=ctx.mask_path,
        symbol_aliases=symbol_aliases,
    )
    if config_path is not None:
        meta = dict(meta)
        meta["config_path"] = ctx.mask_path(config_path)
    return data, warnings, meta


CANDIDATE_RANK_EXPLAIN_TOOL = build_agent_tool(
    name="candidate_rank_explain",
    description="Explain existing candidate CSV ranking scores without running scans, sending notifications, or writing reports.",
    requires=("local_candidate_reports",),
    capabilities=("ranking_explain", "read_only"),
    input_schema={
        "mode": "optional put|call|all; defaults to all",
        "top_n": "optional int, max 100; defaults to 10",
        "report_dir": "optional report dir; defaults to output_shared/reports then output_shared/agent_tools/reports",
        "output_dir": "optional output root; uses <output_dir>/reports",
        "run_id": "optional output_runs run id; searches run account report dirs",
        "run_dir": "optional explicit run dir; searches run account report dirs",
        "account": "optional account label when run_id/run_dir is supplied",
        "candidate_path": "optional explicit candidate CSV path",
        "candidate_paths": "optional list of candidate CSV paths",
        "score_weights": (
            "optional object: annualized_return, net_income, liquidity, risk_distance, "
            "vol_edge, delta_target, concentration, path_risk"
        ),
        "compare_baseline": "optional bool; compare against return-then-income baseline",
    },
    handler=_candidate_rank_explain_tool,
    pure_read=True,
    safe_default_input={"mode": "all", "top_n": 10},
    examples=(
        {"input": {"mode": "put", "top_n": 5}},
        {"input": {"candidate_path": "output_shared/reports/sell_put_candidates_labeled.csv", "mode": "put"}},
    ),
)

CANDIDATE_FILTER_EXPLAIN_TOOL = build_agent_tool(
    name="candidate_filter_explain",
    description=(
        "Explain why a symbol was rejected, post-filtered, accepted, or not observed across candidate "
        "filter functions from existing trace rows."
    ),
    requires=("candidate_filter_trace",),
    capabilities=("candidate_filter_trace", "filter_explain", "read_only"),
    input_schema={
        "symbol": "required canonical symbol, company name, or alias, for example NVDA, 0700.HK, or 泡泡玛特",
        "config_key": "optional us|hk; when present, include runtime-config symbol aliases",
        "config_path": "optional explicit config path; system-injected for Inbound planner calls",
        "account": "optional scan-scope account label; not part of symbol identity",
        "function": (
            "optional "
            + strategy_key_help(
                (
                    STRATEGY_SELL_PUT,
                    STRATEGY_COVERED_CALL,
                    STRATEGY_CLOSE_ADVICE,
                    STRATEGY_YIELD_ENHANCEMENT,
                )
            )
            + "|cash_reserve|share_coverage"
        ),
        "run_id": "optional output_runs run id; use latest externally when desired",
        "run_dir": "optional explicit output_runs/<run_id> directory",
        "report_dir": "optional report dir containing candidate_filter_trace.jsonl",
        "trace_path": "optional explicit candidate_filter_trace.jsonl path",
        "trace_paths": "optional list of trace jsonl paths",
    },
    handler=_candidate_filter_explain_tool,
    pure_read=True,
    safe_default_input={"symbol": "NVDA"},
    examples=(
        {"input": {"run_id": "20260514T100000Z", "account": "lx", "symbol": "NVDA"}},
        {"input": {"run_id": "20260514T100000Z", "symbol": "泡泡玛特"}},
        {"input": {"trace_path": "output_shared/reports/candidate_filter_trace.jsonl", "symbol": "NVDA"}},
    ),
    output_contract=_CANDIDATE_FILTER_OUTPUT_CONTRACT,
)

TOOLS: tuple[AgentTool, ...] = (
    CANDIDATE_RANK_EXPLAIN_TOOL,
    CANDIDATE_FILTER_EXPLAIN_TOOL,
)


__all__ = ["CANDIDATE_FILTER_EXPLAIN_TOOL", "CANDIDATE_RANK_EXPLAIN_TOOL", "TOOLS"]
