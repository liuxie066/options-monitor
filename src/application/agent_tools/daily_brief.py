from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from domain.domain.daily_decision_brief import effective_daily_brief_actionability
from src.application.agent_tool_config import repo_base
from src.application.agent_tool_contracts import AgentToolError, mask_path
from src.application.agent_tools.base import AgentTool, build_agent_tool
from src.application.daily_decision_brief_renderer import render_full_brief
from src.application.daily_decision_brief_repository import (
    read_daily_decision_brief,
    read_latest_daily_decision_brief,
)
from src.application.runtime_paths import resolve_runtime_root


_OUTPUT_CONTRACT: dict[str, Any] = {
    "schema_version": "daily_decision_brief_read.output.v1",
    "source_label": "OM local daily_decision_brief.v1 state",
    "result_shape": "single_brief_or_unavailable",
    "fact_fields": [
        "available",
        "reason",
        "query",
        "brief.schema_version",
        "brief.brief_id",
        "brief.market",
        "brief.market_trading_date",
        "brief.account",
        "brief.revision",
        "brief.actionability",
        "effective_actionability",
        "coverage",
        "source",
        "freshness",
        "brief.actions[]",
        "brief.positions[]",
        "brief.capacity",
        "brief.candidates",
        "brief.rejections",
        "brief.events[]",
        "brief.data_gaps[]",
    ],
    "freshness_fields": [
        "freshness.data_as_of_utc",
        "freshness.valid_until_utc",
        "freshness.effective_actionability",
    ],
    "missing_data_fields": ["brief.data_gaps[]", "reason"],
    "model_preview_fields": ["query", "available", "effective_actionability", "rendered_markdown"],
}


def read_daily_brief_view(
    *,
    base: Path,
    account: str,
    market: str,
    market_trading_date: str | None = None,
    revision: int | None = None,
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    account_norm = str(account or "").strip().lower()
    market_norm = str(market or "").strip().upper()
    date_norm = str(market_trading_date or "").strip() or None
    if not account_norm:
        raise ValueError("account is required")
    if not market_norm:
        raise ValueError("market is required")
    if revision is not None and date_norm is None:
        raise ValueError("market_trading_date is required when revision is provided")

    if date_norm is None:
        result = read_latest_daily_decision_brief(
            base=base,
            account=account_norm,
            market=market_norm,
        )
        mode = "latest"
    else:
        result = read_daily_decision_brief(
            base=base,
            account=account_norm,
            market=market_norm,
            market_trading_date=date_norm,
            revision=revision,
        )
        mode = "revision" if revision is not None else "day_latest"

    query = {
        "mode": mode,
        "account": account_norm,
        "market": market_norm,
        "market_trading_date": date_norm,
        "revision": revision,
    }
    if not bool(result.get("available")):
        reason = str(result.get("reason") or "unavailable")
        effective = "unavailable"
        return {
            "schema_version": "daily_decision_brief_read.output.v1",
            "available": False,
            "reason": reason,
            "query": query,
            "brief": None,
            "effective_actionability": effective,
            "coverage": _coverage(None, reason=reason),
            "source": _source(result),
            "freshness": _freshness(None, effective_actionability=effective),
            "rendered_markdown": _render_unavailable(query=query, reason=reason),
        }

    brief = dict(result["brief"])
    effective = effective_daily_brief_actionability(brief, now_utc=now_utc)
    rendered_brief = dict(brief)
    rendered_brief["actionability"] = effective
    return {
        "schema_version": "daily_decision_brief_read.output.v1",
        "available": True,
        "reason": "ok",
        "query": query,
        "brief": brief,
        "effective_actionability": effective,
        "coverage": _coverage(brief, reason="ok"),
        "source": _source(result),
        "freshness": _freshness(brief, effective_actionability=effective),
        "rendered_markdown": render_full_brief(rendered_brief),
    }


def _coverage(brief: dict[str, Any] | None, *, reason: str) -> dict[str, Any]:
    payload = brief or {}
    return {
        "status": str(payload.get("status") or ("unavailable" if brief is None else "unknown")),
        "reason": reason,
        "action_count": _list_count(payload.get("actions")),
        "position_count": _list_count(payload.get("positions")),
        "data_gap_count": _list_count(payload.get("data_gaps")),
        "source_artifact_count": _list_count(payload.get("source_artifacts")),
    }


def _source(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "label": "OM local daily_decision_brief.v1 state",
        "state_path": mask_path(result.get("path")),
    }


def _freshness(
    brief: dict[str, Any] | None,
    *,
    effective_actionability: str,
) -> dict[str, Any]:
    payload = brief or {}
    return {
        "data_as_of_utc": payload.get("data_as_of_utc"),
        "valid_until_utc": payload.get("valid_until_utc"),
        "effective_actionability": effective_actionability,
    }


def _list_count(value: Any) -> int:
    return len(value) if isinstance(value, list) else 0


def _render_unavailable(*, query: dict[str, Any], reason: str) -> str:
    date_text = query.get("market_trading_date") or "latest"
    revision = query.get("revision")
    revision_text = "latest" if revision is None else str(revision)
    return "\n".join(
        [
            "# 每日决策简报 · 不可用",
            f"- 账号：`{query['account']}` | 市场：`{query['market']}`",
            f"- 交易日：`{date_text}` | revision：`{revision_text}`",
            f"- 原因：`{reason}`",
        ]
    )


def _validate_daily_brief_input(payload: dict[str, Any]) -> None:
    if payload.get("revision") is None:
        return
    if not str(payload.get("date") or "").strip():
        raise AgentToolError(code="INPUT_ERROR", message="date is required when revision is provided")


def _daily_brief_read_tool(payload: dict[str, Any]) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    revision_value = payload.get("revision")
    revision = None if revision_value is None else int(revision_value)
    repo_root = repo_base()
    runtime_root = resolve_runtime_root(repo_root=repo_root).runtime_root
    try:
        data = read_daily_brief_view(
            base=runtime_root,
            account=str(payload.get("account") or ""),
            market=str(payload.get("market") or "US"),
            market_trading_date=(str(payload.get("date") or "").strip() or None),
            revision=revision,
        )
    except ValueError as exc:
        raise AgentToolError(code="INPUT_ERROR", message=str(exc)) from exc
    warnings = [] if data["available"] else [f"daily brief unavailable: {data['reason']}"]
    return data, warnings, {"read_only": True, "state_path": data["source"]["state_path"]}


DAILY_DECISION_BRIEF_READ_TOOL = build_agent_tool(
    name="daily_decision_brief_read",
    description=(
        "Read the canonical local Daily Decision Brief by latest, trading day, or exact revision. "
        "Returns structured JSON plus bounded Chinese Markdown and never sends notifications or refreshes data."
    ),
    requires=("daily_decision_brief_state",),
    capabilities=("daily_brief", "decision_support", "read_only", "runtime_artifacts"),
    input_schema={
        "account": {
            "type": "string",
            "minLength": 1,
            "required": True,
            "description": "Lowercase account label such as lx",
        },
        "market": {
            "type": "string",
            "enum": ["US", "HK", "us", "hk"],
            "description": "Optional market; defaults to US",
        },
        "date": {
            "type": "string",
            "pattern": r"^\d{4}-\d{2}-\d{2}$",
            "description": "Optional market trading date in YYYY-MM-DD; omitted reads latest",
        },
        "revision": {
            "type": "integer",
            "minimum": 0,
            "description": "Optional exact revision; requires date",
        },
    },
    handler=_daily_brief_read_tool,
    pure_read=True,
    safe_default_input={"market": "US"},
    input_validator=_validate_daily_brief_input,
    examples=(
        {"input": {"account": "lx", "market": "US"}},
        {"input": {"account": "lx", "market": "US", "date": "2026-07-19", "revision": 0}},
    ),
    output_contract=_OUTPUT_CONTRACT,
    copilot_input_fields=("account", "market", "date", "revision"),
)

TOOLS: tuple[AgentTool, ...] = (DAILY_DECISION_BRIEF_READ_TOOL,)


__all__ = ["DAILY_DECISION_BRIEF_READ_TOOL", "TOOLS", "read_daily_brief_view"]
