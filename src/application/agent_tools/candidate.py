from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Any

from domain.domain.strategy_vocab import (
    STRATEGY_CLOSE_ADVICE,
    STRATEGY_COVERED_CALL,
    STRATEGY_SELL_PUT,
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


def _normalize_candidate_filter_copilot_input(payload: Mapping[str, Any]) -> dict[str, Any]:
    normalized = dict(payload)
    selector = normalized.get("run_selector")
    if selector is not None:
        selector_value = str(selector).strip().lower()
        if selector_value not in {"latest", "latest_notification"}:
            raise ValueError("run_selector must be latest or latest_notification")
        normalized["run_selector"] = selector_value
    raw_date = normalized.get("notification_date")
    if raw_date is not None:
        date_text = str(raw_date).strip()
        try:
            date.fromisoformat(date_text)
        except ValueError as exc:
            raise ValueError("notification_date must be ISO YYYY-MM-DD") from exc
        if normalized.get("run_selector") != "latest_notification":
            raise ValueError("notification_date requires run_selector=latest_notification")
        normalized["notification_date"] = date_text
    return normalized


_CANDIDATE_FILTER_OUTPUT_CONTRACT: dict[str, Any] = {
    "schema_version": "candidate_filter_explain.output.v1",
    "evidence_type": "collection",
    "bounded_projection": "contract_fields",
    "coverage": "primary_rows",
    "freshness": "source_declared",
    "pagination": {"mode": "none"},
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
    "evidence_type": "collection",
    "bounded_projection": "contract_fields",
    "coverage": "primary_rows",
    "freshness": "source_declared",
    "pagination": {"mode": "none"},
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
        "ranked[].period_net_return",
        "ranked[].annualized_return",
        "ranked[].net_income",
        "ranked[].rank_reason",
        "ranked[].primary_drivers",
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
    catalog_summary="解释候选机会的排序依据与证据。",
    description=(
        "Explain the recorded order in a terminal manifest-bound account opening-candidate snapshot. The tool never re-ranks rows."
    ),
    requires=("candidate_snapshot_manifest", "opening_candidate_snapshot"),
    capabilities=("ranking_explain", "read_only"),
    input_schema={
        "mode": "optional put|call|all; defaults to all",
        "top_n": "optional int, max 100; defaults to 10",
        "run_id": "optional output_runs run id; omitted resolves the latest terminal manifest-bound run",
        "runtime_root": "optional explicit runtime root; defaults to OM_RUNTIME_ROOT then repo root",
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
    catalog_summary="解释候选机会的筛选结果与排除原因。",
    description=(
        "Explain the recorded opening decision for a symbol from a terminal manifest-bound account snapshot. The tool never re-filters rows. "
        "With run_selector=latest_notification it resolves the run that produced the most recent notification actually delivered "
        "to the account on notification_date (default: today, runtime host local timezone), so a user can ask why a symbol was "
        "filtered right after a monitoring notification arrives."
    ),
    requires=("candidate_snapshot_manifest", "opening_candidate_snapshot"),
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
        "runtime_root": "optional explicit runtime root; defaults to OM_RUNTIME_ROOT then repo root",
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
        "run_id": "optional output_runs run id; omitted resolves the latest terminal manifest-bound run",
        "run_selector": (
            "optional latest|latest_notification; latest_notification resolves the most recent run whose "
            "notification was actually delivered to the account on notification_date"
        ),
        "notification_date": (
            "optional ISO YYYY-MM-DD used with run_selector=latest_notification; "
            "defaults to today in the runtime host local timezone"
        ),
    },
    handler=_candidate_filter_explain_tool,
    pure_read=True,
    safe_default_input={},
    examples=(
        {"input": {"run_id": "20260514T100000Z", "account": "lx", "symbol": "NVDA"}},
        {"input": {"run_id": "20260514T100000Z", "account": "sy", "symbol": "泡泡玛特"}},
        {"input": {"account": "sy", "symbol": "0700.HK", "run_selector": "latest_notification"}},
    ),
    output_contract=_CANDIDATE_FILTER_OUTPUT_CONTRACT,
    copilot_input_fields=(
        "config_key",
        "symbol",
        "account",
        "function",
        "run_id",
        "run_selector",
        "notification_date",
    ),
    copilot_input_normalizer=_normalize_candidate_filter_copilot_input,
)

TOOLS: tuple[AgentTool, ...] = (
    CANDIDATE_RANK_EXPLAIN_TOOL,
    CANDIDATE_FILTER_EXPLAIN_TOOL,
)


__all__ = ["CANDIDATE_FILTER_EXPLAIN_TOOL", "CANDIDATE_RANK_EXPLAIN_TOOL", "TOOLS"]
