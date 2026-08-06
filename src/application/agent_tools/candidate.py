from __future__ import annotations

from typing import Any

from domain.domain.strategy_vocab import (
    STRATEGY_CLOSE_ADVICE,
    STRATEGY_COVERED_CALL,
    STRATEGY_SELL_PUT,
    STRATEGY_YIELD_ENHANCEMENT,
    strategy_key_help,
)
from src.application.agent_tools.candidate_filter_impl import candidate_filter_explain_tool
from src.application.agent_tools.candidate_rank_impl import candidate_rank_explain_tool
from src.application.agent_tools.base import AgentTool, build_agent_tool
from src.application.agent_tool_config import load_runtime_config
from src.application.agent_tool_contracts import mask_path
from src.application.agent_tool_config import repo_base
from src.application.agent_tool_config import resolve_output_root
from src.application.symbol_aliases import symbol_aliases_from_config


_CANDIDATE_FILTER_OUTPUT_CONTRACT: dict[str, Any] = {
    "schema_version": "candidate_filter_explain.output.v1",
    "source_label": "OM sealed opening candidate snapshot",
    "primary_rows": "functions",
    "row_count_field": "trace_count",
    "fact_fields": [
        "symbol",
        "canonical_symbol",
        "raw_symbol",
        "scope.account",
        "scope.account_semantics",
        "evidence_status",
        "conclusion_status",
        "trace_count",
        "status_counts",
        "function_counts",
        "functions[].function",
        "functions[].status",
        "functions[].reason_counts",
        "functions[].reason_labels",
        "functions[].rejection_reason_counts",
        "functions[].rejection_reasons[].rule",
        "functions[].rejection_reasons[].label",
        "functions[].rejection_reasons[].count",
        "functions[].events[].rule",
        "functions[].events[].rule_label",
        "functions[].events[].is_rejection",
        "functions[].events[].metric_value",
        "functions[].events[].threshold",
        "functions[].events[].message",
    ],
    "missing_data_fields": [
        "evidence_status",
        "conclusion_status",
        "trace_count",
    ],
}

_CANDIDATE_RANK_OUTPUT_CONTRACT: dict[str, Any] = {
    "schema_version": "candidate_rank_explain.output.v1",
    "source_label": "OM sealed opening candidate snapshot",
    "primary_rows": "ranked",
    "row_count_field": "row_count",
    "fact_fields": [
        "mode",
        "top_n",
        "groups[].mode",
        "groups[].ranking_policy",
        "groups[].row_count",
        "ranked[].rank",
        "ranked[].symbol",
        "ranked[].contract_symbol",
        "ranked[].strategy_score",
        "ranked[].annualized_return",
        "ranked[].net_income",
        "ranked[].rank_reason",
        "ranked[].risk_notes",
        "ranked[].score_warnings",
    ],
}


def _candidate_rank_explain_tool(
    payload: dict[str, Any],
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    return candidate_rank_explain_tool(
        payload,
        repo_base=repo_base,
        resolve_output_root=resolve_output_root,
        mask_path=mask_path,
    )


def _candidate_filter_explain_tool(
    payload: dict[str, Any],
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    config_path = None
    symbol_aliases = None
    if str(payload.get("config_key") or "").strip() or str(payload.get("config_path") or "").strip():
        config_path, cfg = load_runtime_config(
            config_key=payload.get("config_key"),
            config_path=payload.get("config_path"),
        )
        symbol_aliases = symbol_aliases_from_config(cfg)

    tool_payload = dict(payload)
    if config_path is not None:
        tool_payload["config_path"] = str(config_path)

    data, warnings, meta = candidate_filter_explain_tool(
        tool_payload,
        repo_base=repo_base,
        mask_path=mask_path,
        symbol_aliases=symbol_aliases,
    )
    if config_path is not None:
        meta = dict(meta)
        meta["config_path"] = mask_path(config_path)
    return data, warnings, meta


CANDIDATE_RANK_EXPLAIN_TOOL = build_agent_tool(
    name="candidate_rank_explain",
    description=(
        "Explain the recorded order in a sealed account opening-candidate snapshot. The tool never re-ranks rows."
    ),
    requires=("opening_candidate_snapshot",),
    capabilities=("ranking_explain", "read_only"),
    input_schema={
        "mode": "optional put|call|all; defaults to all",
        "top_n": "optional int, max 100; defaults to 10",
        "run_id": "optional output_runs run id; omitted resolves the latest sealed snapshot",
        "account": {
            "type": "string",
            "required": True,
            "description": "Logical account label bound to the physical OpenD account snapshot",
        },
    },
    handler=_candidate_rank_explain_tool,
    pure_read=True,
    safe_default_input={"mode": "all", "top_n": 10},
    examples=(
        {"input": {"account": "lx", "mode": "put", "top_n": 5}},
        {"input": {"account": "sy", "run_id": "20260514T100000Z", "mode": "call"}},
    ),
    output_contract=_CANDIDATE_RANK_OUTPUT_CONTRACT,
    copilot_input_fields=("mode", "top_n", "run_id", "account"),
)

CANDIDATE_FILTER_EXPLAIN_TOOL = build_agent_tool(
    name="candidate_filter_explain",
    description=(
        "Explain the recorded opening decision for a symbol from a sealed account snapshot. The tool never re-filters rows."
    ),
    requires=("opening_candidate_snapshot",),
    capabilities=("opening_candidate_snapshot", "filter_explain", "read_only"),
    input_schema={
        "symbol": {
            "type": "string",
            "required": True,
            "description": "Canonical symbol, company name, or alias, for example NVDA, 0700.HK, or 泡泡玛特",
        },
        "config_key": {
            "type": "string",
            "enum": ["us", "hk"],
            "description": "Optional market config; when present, include runtime-config symbol aliases",
        },
        "config_path": "optional explicit config path",
        "runtime_root": "optional explicit runtime root; defaults from config_path, OM_RUNTIME_ROOT, service profile, and repo root",
        "account": {
            "type": "string",
            "required": True,
            "description": "Logical account label bound to the physical OpenD account snapshot",
        },
        "function": (
            "optional "
            + strategy_key_help(
                (
                    STRATEGY_SELL_PUT,
                    STRATEGY_COVERED_CALL,
                )
            )
        ),
        "run_id": "optional output_runs run id; omitted resolves the latest sealed snapshot",
    },
    handler=_candidate_filter_explain_tool,
    pure_read=True,
    safe_default_input={},
    examples=(
        {"input": {"run_id": "20260514T100000Z", "account": "lx", "symbol": "NVDA"}},
        {"input": {"run_id": "20260514T100000Z", "account": "sy", "symbol": "泡泡玛特"}},
    ),
    output_contract=_CANDIDATE_FILTER_OUTPUT_CONTRACT,
    copilot_input_fields=("config_key", "symbol", "account", "function", "run_id"),
)

TOOLS: tuple[AgentTool, ...] = (
    CANDIDATE_RANK_EXPLAIN_TOOL,
    CANDIDATE_FILTER_EXPLAIN_TOOL,
)


__all__ = ["CANDIDATE_FILTER_EXPLAIN_TOOL", "CANDIDATE_RANK_EXPLAIN_TOOL", "TOOLS"]
