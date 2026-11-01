from __future__ import annotations

from typing import Any

from src.application.agent_tools.base import build_agent_tool
from src.application.agent_tool_config import load_runtime_config
from src.application.agent_tool_contracts import mask_path
from src.application.agent_tool_config import repo_base
from src.application.agent_tool_config import resolve_output_root


_CLOSE_ADVICE_READ_OUTPUT_CONTRACT: dict[str, Any] = {
    "schema_version": "close_advice_read.output.v1",
    "evidence_type": "collection",
    "bounded_projection": "contract_fields",
    "coverage": "primary_rows",
    "freshness": "source_declared",
    "pagination": {"mode": "none"},
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
        "rows[].policy_version",
        "rows[].recommendation_state",
        "rows[].decision_basis",
        "rows[].decision_evidence_status",
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
    payload: dict[str, Any],
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    from src.application.agent_tools.close_advice_read_impl import close_advice_read_tool

    return close_advice_read_tool(
        payload,
        load_runtime_config=load_runtime_config,
        resolve_output_root=resolve_output_root,
        repo_base=repo_base,
        mask_path=mask_path,
    )


CLOSE_ADVICE_READ_TOOL = build_agent_tool(
    name="close_advice_read",
    catalog_summary="读取已生成的平仓建议与依据。",
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
    examples=({"input": {"config_key": "us", "query": {"option_type": "put", "side": "short"}}},),
    output_contract=_CLOSE_ADVICE_READ_OUTPUT_CONTRACT,
    bot_input_fields=("config_key", "market_scope", "query", "run_id"),
)



def _position_advice_evaluate_tool(payload: dict[str, Any]):
    from src.application.agent_tool_contracts import AgentToolError
    from src.application.position_advice import evaluate_position_advice
    from src.application.runtime_paths import resolve_runtime_root, runtime_root_scope
    from src.application.quality.gate import QualityGateBlocked

    path, cfg = load_runtime_config(config_key=payload.get("config_key"), config_path=payload.get("config_path"))
    base = repo_base()
    resolved = resolve_runtime_root(repo_root=base)
    runtime_root = path.expanduser().resolve().parent if getattr(resolved, "source", None) == "repo_default" else resolved.runtime_root
    try:
        with runtime_root_scope(runtime_root):
            result = evaluate_position_advice(payload, config=cfg, config_path=path, base_dir=base, runtime_root=runtime_root)
    except ValueError as exc:
        raise AgentToolError(code="INPUT_ERROR", message=str(exc)) from exc
    except QualityGateBlocked as exc:
        raise AgentToolError(code="QUALITY_GATE_BLOCKED", message=str(exc)) from exc
    return result, [], {"source": "position_advice", "notifications_sent": False}


POSITION_ADVICE_EVALUATE_TOOL = build_agent_tool(
    name="position_advice_evaluate",
    catalog_summary="主动评估账本或临时输入的多笔期权持仓。",
    description=("Assess selected existing holdings or a temporary manual positions list using the enabled optional Position Advice plugin. "
                 "Fetches on-demand market evidence and writes isolated request reports; never changes the ledger or sends notifications. "
                 "Closed sessions return compatible sealed historical references or explicit unavailable results. Hold only means this policy has no close signal."),
    requires=("runtime_config", "position_advice_plugin", "market_data_provider"),
    capabilities=("position_advice", "position_exit_analysis"),
    read_only=True,
    risk_level="local_write",
    side_effects=("fetches_market_data", "writes_isolated_request_reports"),
    input_schema={
        "config_key": {"type": "string", "enum": ["us", "hk"]},
        "config_path": {"type": "string"},
        "account": {"type": "string", "required": True, "minLength": 1},
        "symbols": {"type": "array", "items": {"type": "string", "minLength": 1}, "maxItems": 100},
        "lot_ids": {"type": "array", "items": {"type": "string", "minLength": 1}, "maxItems": 100},
        "contracts": {"type": "array", "maxItems": 100, "items": {"type": "object", "required": ["symbol", "option_type", "expiration", "strike"], "additionalProperties": False, "properties": {
            "symbol": {"type": "string"}, "option_type": {"type": "string", "enum": ["put", "call"]}, "expiration": {"type": "string"}, "strike": {"type": "number", "exclusiveMinimum": 0},
        }}},
        "positions": {"type": "array", "minItems": 1, "maxItems": 100, "items": {"type": "object", "required": ["input_id", "input_as_of_utc", "symbol", "option_type", "expiration", "strike"], "additionalProperties": False, "properties": {
            "input_id": {"type": "string", "minLength": 1, "maxLength": 128}, "input_as_of_utc": {"type": "string"},
            "account": {"type": "string"}, "broker": {"type": "string"}, "symbol": {"type": "string"},
            "option_type": {"type": "string", "enum": ["put", "call"]}, "side": {"type": "string", "enum": ["short"]},
            "expiration": {"type": "string"}, "strike": {"type": "number", "exclusiveMinimum": 0},
            "contracts_open": {"type": "integer", "minimum": 1}, "multiplier": {"type": "integer", "minimum": 1},
            "currency": {"type": "string", "enum": ["USD", "HKD"]}, "premium": {"type": "number", "exclusiveMinimum": 0}, "opened_at": {"type": "string"},
        }}},
    },
    handler=_position_advice_evaluate_tool,
    allow_additional_input=False,
    output_contract={"schema_version": "position_advice.output.v1", "evidence_type": "collection", "primary_rows": "rows", "row_count_field": "returned_count", "freshness": "source_declared", "fact_fields": ["rows[].input_source", "rows[].input_id", "rows[].position_lot_id", "rows[].symbol", "rows[].assessment_mode", "rows[].assessment_as_of_utc", "rows[].recommendation_state", "rows[].reference_recommendation_state", "rows[].reason"], "missing_data_fields": ["rows[].evaluation_status", "rows[].quote_status"], "freshness_fields": ["rows[].assessment_mode", "rows[].assessment_as_of_utc"], "model_preview_fields": ["status", "interpretation", "rows"]},
    bot_input_fields=("config_key", "account", "symbols", "lot_ids", "contracts", "positions"),
    examples=({"input": {"config_key": "us", "account": "sample", "symbols": ["NVDA", "AAPL"]}},),
)

TOOLS = (CLOSE_ADVICE_READ_TOOL, POSITION_ADVICE_EVALUATE_TOOL)
__all__ = ["CLOSE_ADVICE_READ_TOOL", "POSITION_ADVICE_EVALUATE_TOOL", "TOOLS"]
