from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from domain.domain.daily_decision_brief import effective_daily_brief_actionability
from src.application.agent_tool_config import load_runtime_config, repo_base
from src.application.agent_tool_contracts import AgentToolError, mask_path
from src.application.agent_tools.base import AgentTool, build_agent_tool
from src.application.daily_decision_brief_renderer import render_query_brief
from src.application.daily_decision_brief_repository import (
    read_daily_decision_brief,
    read_latest_daily_decision_brief,
)
from src.application.runtime_paths import resolve_runtime_root


_MARKETS = ("US", "HK")
_MARKET_LABELS = {"US": "美股", "HK": "港股"}
_OUTPUT_CONTRACT: dict[str, Any] = {
    "evidence_type": "collection",
    "bounded_projection": "contract_fields",
    "coverage": "primary_rows",
    "freshness": "source_declared",
    "pagination": {"mode": "none"},
    "schema_version": "daily_decision_brief_read.output.v1",
    "source_label": "OM local daily_decision_brief.v1 successful current state",
    "result_shape": "single_brief_or_aggregate_sections",
    "fact_fields": [
        "available",
        "reason",
        "query",
        "sections[]",
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
        "brief.funds",
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
    "missing_data_fields": ["brief.data_gaps[]", "reason", "sections[].reason"],
    "model_preview_fields": ["query", "available", "effective_actionability", "sections", "rendered_markdown"],
}


def read_daily_brief_view(
    *,
    base: Path,
    account: str | None = None,
    market: str | None = None,
    market_trading_date: str | None = None,
    revision: int | None = None,
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    account_norm = str(account or "").strip().lower() or None
    market_norm = str(market or "").strip().upper() or None
    date_norm = str(market_trading_date or "").strip() or None
    now = _utc_datetime(now_utc)
    if market_norm is not None and market_norm not in _MARKETS:
        raise ValueError("market must be US or HK")
    if revision is not None and revision < 0:
        raise ValueError("revision must be non-negative")
    if revision is not None and date_norm is None:
        raise ValueError("market_trading_date is required when revision is provided")
    if date_norm is not None and (account_norm is None or market_norm is None):
        raise ValueError("account and market are required for day or revision queries")

    if date_norm is not None or (account_norm is not None and market_norm is not None):
        assert account_norm is not None and market_norm is not None
        return _read_single_daily_brief_view(
            base=base,
            account=account_norm,
            market=market_norm,
            market_trading_date=date_norm,
            revision=revision,
            now_utc=now,
        )

    scopes = _enabled_daily_brief_scopes(account=account_norm, market=market_norm)
    sections = [
        _read_single_daily_brief_view(
            base=base,
            account=scope_account,
            market=scope_market,
            market_trading_date=None,
            revision=None,
            now_utc=now,
            heading_level=2,
        )
        for scope_account, scope_market in scopes
    ]
    return _aggregate_daily_brief_view(
        sections=sections,
        account=account_norm,
        market=market_norm,
        now_utc=now,
    )


def _read_single_daily_brief_view(
    *,
    base: Path,
    account: str,
    market: str,
    market_trading_date: str | None,
    revision: int | None,
    now_utc: datetime,
    heading_level: int = 1,
) -> dict[str, Any]:
    if market_trading_date is None:
        result = read_latest_daily_decision_brief(base=base, account=account, market=market)
        mode = "latest"
    else:
        result = read_daily_decision_brief(
            base=base,
            account=account,
            market=market,
            market_trading_date=market_trading_date,
            revision=revision,
        )
        mode = "revision" if revision is not None else "day_latest"

    query = {
        "mode": mode,
        "account": account,
        "market": market,
        "market_trading_date": market_trading_date,
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
            "rendered_markdown": _render_unavailable(query=query, reason=reason, heading_level=heading_level),
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
        "rendered_markdown": render_query_brief(
            rendered_brief,
            context={"query_time_utc": now_utc.isoformat()},
            heading_level=heading_level,
        ),
    }


def _enabled_daily_brief_scopes(*, account: str | None, market: str | None) -> list[tuple[str, str]]:
    markets = (market,) if market is not None else _MARKETS
    scopes: set[tuple[str, str]] = set()
    for market_name in markets:
        _path, cfg = load_runtime_config(config_key=market_name.lower(), expected_market=market_name.lower())
        accounts = [str(item or "").strip().lower() for item in cfg.get("accounts") or []]
        for account_name in accounts:
            if account_name and (account is None or account_name == account):
                scopes.add((account_name, market_name))
    return sorted(scopes)


def _aggregate_daily_brief_view(
    *,
    sections: list[dict[str, Any]],
    account: str | None,
    market: str | None,
    now_utc: datetime,
) -> dict[str, Any]:
    available_count = sum(bool(item.get("available")) for item in sections)
    if not sections:
        reason = "scope_not_enabled"
    elif available_count == len(sections):
        reason = "ok"
    elif available_count:
        reason = "partial"
    else:
        reason = "not_found"
    query = {
        "mode": "latest",
        "account": account,
        "market": market,
        "market_trading_date": None,
        "revision": None,
        "scope": "enabled_runtime_config",
    }
    warning = ""
    if reason == "partial":
        warning = "提醒｜部分账户或市场的成功扫描快照暂不可用。"
    elif reason == "not_found":
        warning = "提醒｜当前启用范围还没有可用的成功扫描快照。"
    elif reason == "scope_not_enabled":
        warning = "提醒｜没有匹配的启用账户或市场。"
    rendered_parts = ["# 期权监控"]
    if warning:
        rendered_parts.extend(["", warning])
    for section in sections:
        rendered_parts.extend(["", "---", "", str(section["rendered_markdown"]).strip()])
    return {
        "schema_version": "daily_decision_brief_read.output.v1",
        "available": bool(available_count),
        "reason": reason,
        "query": query,
        "brief": None,
        "sections": sections,
        "effective_actionability": "mixed" if len(sections) > 1 else (
            str(sections[0].get("effective_actionability") or "unavailable") if sections else "unavailable"
        ),
        "coverage": {
            "status": "ready" if reason == "ok" else ("partial" if reason == "partial" else "unavailable"),
            "reason": reason,
            "section_count": len(sections),
            "available_section_count": available_count,
            "unavailable_section_count": len(sections) - available_count,
        },
        "source": {
            "label": "OM local daily_decision_brief.v1 successful current state",
            "state_paths": [item["source"]["state_path"] for item in sections],
        },
        "freshness": {
            "query_time_utc": now_utc.isoformat(),
            "effective_actionability": "mixed" if len(sections) > 1 else (
                str(sections[0].get("effective_actionability") or "unavailable") if sections else "unavailable"
            ),
        },
        "rendered_markdown": "\n".join(rendered_parts).strip(),
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
        "label": "OM local daily_decision_brief.v1 successful current state",
        "state_path": mask_path(result.get("path")),
    }


def _freshness(brief: dict[str, Any] | None, *, effective_actionability: str) -> dict[str, Any]:
    payload = brief or {}
    return {
        "data_as_of_utc": payload.get("data_as_of_utc"),
        "valid_until_utc": payload.get("valid_until_utc"),
        "effective_actionability": effective_actionability,
    }


def _list_count(value: Any) -> int:
    return len(value) if isinstance(value, list) else 0


def _render_unavailable(*, query: dict[str, Any], reason: str, heading_level: int) -> str:
    title_mark = "#" * max(1, min(int(heading_level), 5))
    date_text = query.get("market_trading_date") or "最近成功扫描"
    return "\n".join(
        [
            f"{title_mark} OM · 决策简报 · {query['account']}",
            "",
            "状态｜当前查询",
            f"市场｜{_MARKET_LABELS.get(str(query['market']), '市场')}",
            f"范围｜{date_text}",
            "",
            f"结论｜当前查询不可用：暂时没有成功扫描快照（{reason}）。",
        ]
    )


def _utc_datetime(value: datetime | None) -> datetime:
    now = value or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


_BRIEF_SECTIONS = ("rendered_markdown", "brief", "actions", "positions", "funds", "capacity", "candidates", "rejections", "events", "data_gaps")
_PAGE_FIELDS = ["available", "reason", "query", "section", "text", "body_range", "body_complete", "next_cursor", "continuation_status", "pagination", "brief_identity", "effective_actionability"]
_PAGE_CONTRACT = {
    "schema_version": "daily_decision_brief_read.page_output.v1", "evidence_type": "aggregate",
    "bounded_projection": "contract_fields", "coverage": "source_declared", "freshness": "source_declared",
    "pagination": {"mode": "keyset"}, "source_label": "OM retained daily_decision_brief.v1 snapshot",
    "fact_fields": _PAGE_FIELDS, "model_value_fields": _PAGE_FIELDS,
    "missing_data_fields": ["continuation_status"], "freshness_fields": ["freshness.as_of"],
}


def _daily_brief_output_contract(payload: dict[str, Any]) -> dict[str, Any]:
    return _PAGE_CONTRACT if payload.get("section") is not None or payload.get("cursor") else _OUTPUT_CONTRACT


def _daily_brief_bot_input(payload: dict[str, Any]) -> dict[str, Any]:
    return {"section": "rendered_markdown", **payload}


def _page_daily_brief(data: dict[str, Any], payload: dict[str, Any], *, binding: str,
                      state: dict[str, Any] | None, now_utc: datetime) -> dict[str, Any]:
    from src.application.agent_tools.project_reader import ProjectReaderError, digest, page_text, set_continuation

    section = str(payload.get("section") or "rendered_markdown")
    sections = data.get("sections") or [data]
    briefs = [item["brief"] for item in sections if isinstance(item.get("brief"), dict)]
    source_hash = digest([{"query": item.get("query"), "brief": item.get("brief"), "reason": item.get("reason")} for item in sections])
    if state and state.get("source_hash") != source_hash:
        raise AgentToolError("READ_ERROR", "source_changed", hint="Discard the old cursor and read the new report revision.")
    identities = [{key: brief.get(key) for key in ("brief_id", "account", "market", "run_id", "revision", "market_trading_date", "data_as_of_utc")} for brief in briefs]
    result = {key: data[key] for key in ("available", "reason", "query", "effective_actionability")}
    result.update(section=section, brief_identity=identities,
                  source={"label": "OM retained daily brief", "content_hash": source_hash},
                  scope={"account": payload.get("account"), "market": payload.get("market"), "section": section,
                         "brief_identity": identities, "content_hash": source_hash},
                  freshness={"status": "historical" if briefs else "unknown", "as_of": min((str(brief.get("data_as_of_utc") or "") for brief in briefs), default=None)})
    if not briefs:
        result.update(read_status="not_found" if data["reason"] == "not_found" else "unavailable",
                      coverage={"status": "unknown", "complete_for": "point"}, next_cursor=None,
                      pagination={"total_count": None, "matched_count": None, "returned_count": 0, "scanned_count": len(sections), "has_more": False})
        return result
    selected = [brief if section in {"brief", "rendered_markdown"} else brief.get(section) for brief in briefs]
    missing_sections = sum(item is None or (section == "funds" and isinstance(item, dict) and item.get("available") is False) for item in selected)
    if missing_sections == len(selected):
        reason = str(selected[0].get("reason") or "section_unavailable") if len(selected) == 1 and isinstance(selected[0], dict) else "section_unavailable"
        result.update(available=False, reason=reason, read_status="unavailable",
                      coverage={"status": "unknown", "complete_for": "point"}, next_cursor=None,
                      pagination={"total_count": len(sections), "matched_count": 0, "returned_count": 0,
                                  "scanned_count": len(sections), "has_more": False})
        return result
    if section == "rendered_markdown":
        raw = str(data["rendered_markdown"]).encode()
    else:
        raw = json.dumps(selected[0] if len(selected) == 1 else selected, ensure_ascii=False, sort_keys=True).encode()
    try:
        fragment = page_text(raw, relative_name=section, resource="daily_decision_brief_read",
                             scope={"query_binding": binding, "source_hash": source_hash},
                             cursor=state.get("body_cursor") if state else None)
        result.update({name: fragment[name] for name in ("text", "body_range", "body_complete", "coverage")})
        if fragment.get("continuation_status"):
            result.update(continuation_status=fragment["continuation_status"], next_cursor=None)
        else:
            set_continuation(result, {"binding": binding, "source_hash": source_hash, "body_cursor": fragment["next_cursor"],
                                      "now_utc": now_utc.isoformat()} if fragment.get("next_cursor") else None)
    except ProjectReaderError as exc:
        raise AgentToolError("READ_ERROR", exc.code, hint="Keep the same report filters when continuing; changed sources require a new read.") from exc
    result["pagination"] = {"total_count": len(sections), "matched_count": len(briefs), "returned_count": len(briefs),
                             "scanned_count": len(sections), "has_more": not result["body_complete"], "next_cursor": result.get("next_cursor")}
    if len(briefs) != len(sections) or missing_sections:
        result["coverage"]["status"] = "partial"
    if missing_sections:
        result["reason"] = "section_unavailable"
        result["pagination"]["returned_count"] -= missing_sections
    return result


def _validate_daily_brief_input(payload: dict[str, Any]) -> None:
    if payload.get("section") is not None and payload["section"] not in _BRIEF_SECTIONS:
        raise AgentToolError(code="INPUT_ERROR", message="Unknown report section")
    has_date = bool(str(payload.get("date") or "").strip())
    has_revision = payload.get("revision") is not None
    if has_revision and not has_date:
        raise AgentToolError(code="INPUT_ERROR", message="date is required when revision is provided")
    if (has_date or has_revision) and not str(payload.get("account") or "").strip():
        raise AgentToolError(code="INPUT_ERROR", message="account is required for day or revision queries")


def _daily_brief_read_tool(payload: dict[str, Any]) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    revision_value = payload.get("revision")
    revision = None if revision_value is None else int(revision_value)
    date = str(payload.get("date") or "").strip() or None
    market = str(payload.get("market") or "").strip() or ("US" if date is not None else None)
    repo_root = repo_base()
    runtime_root = resolve_runtime_root(repo_root=repo_root).runtime_root
    paged = payload.get("section") is not None or bool(payload.get("cursor"))
    state, binding, now = None, "", datetime.now(timezone.utc)
    if paged:
        from src.application.agent_tools.project_reader import ProjectReaderError, decode_cursor, digest

        binding = digest({"tool": "daily_decision_brief_read", "root": str(runtime_root),
                          "account": str(payload.get("account") or "").strip().lower(), "market": str(market or "").upper(),
                          "date": date, "revision": revision, "section": payload.get("section", "rendered_markdown")})
        try:
            state = decode_cursor(payload.get("cursor"), binding)
            if state:
                now = datetime.fromisoformat(state["now_utc"])
                if now.tzinfo is None or now > datetime.now(timezone.utc):
                    raise ValueError("invalid cursor time")
        except (ProjectReaderError, ValueError, KeyError, TypeError) as exc:
            raise AgentToolError("INPUT_ERROR", getattr(exc, "code", "cursor_invalidated")) from exc
    try:
        data = read_daily_brief_view(
            base=runtime_root,
            account=(str(payload.get("account") or "").strip() or None),
            market=market,
            market_trading_date=date,
            revision=revision,
            now_utc=now,
        )
    except ValueError as exc:
        raise AgentToolError(code="INPUT_ERROR", message=str(exc)) from exc
    warnings = [] if data["reason"] == "ok" else [f"daily brief query status: {data['reason']}"]
    state_paths = data["source"].get("state_paths")
    meta = {"read_only": True}
    if isinstance(state_paths, list):
        meta["state_paths"] = state_paths
    else:
        meta["state_path"] = data["source"]["state_path"]
    if paged:
        data = _page_daily_brief(data, payload, binding=binding, state=state, now_utc=now)
    return data, warnings, meta


DAILY_DECISION_BRIEF_READ_TOOL = build_agent_tool(
    name="daily_decision_brief_read",
    catalog_summary="读取指定账户的每日决策简报。",
    description=(
        "Read the latest successful option-monitor snapshot, a trading day, or an exact revision. "
        "Use for queries such as 期权监控, 最新期权报告, 港股期权, 美股期权, or lx/sy 期权. "
        "Omitting account and market returns all enabled scopes. The tool returns structured JSON plus "
        "readable Chinese Markdown and never scans, sends, or changes delivery state. "
        "Use section for bounded report text or existing structured details, then next_cursor with unchanged filters. "
        "A fragment only covers its body_range; even the last fragment does not prove earlier fragments were read."
    ),
    requires=("daily_decision_brief_state",),
    capabilities=("daily_brief", "decision_support", "read_only", "runtime_artifacts"),
    input_schema={
        "section": {"type": "string", "enum": list(_BRIEF_SECTIONS), "description": "Bounded original report section; brief reads structured details"},
        "cursor": {"type": "string", "minLength": 1, "maxLength": 8192},
        "account": {
            "type": "string",
            "minLength": 1,
            "description": "Optional lowercase account label; omitted queries all enabled accounts",
        },
        "market": {
            "type": "string",
            "enum": ["US", "HK", "us", "hk"],
            "description": "Optional market; omitted latest queries all enabled markets and day queries default to US",
        },
        "date": {
            "type": "string",
            "pattern": r"^\d{4}-\d{2}-\d{2}$",
            "description": "Optional market trading date; requires account; market defaults to US",
        },
        "revision": {
            "type": "integer",
            "minimum": 0,
            "description": "Optional exact revision; requires date and account; market defaults to US",
        },
    },
    handler=_daily_brief_read_tool,
    pure_read=True,
    safe_default_input={},
    input_validator=_validate_daily_brief_input,
    examples=(
        {"input": {}},
        {"input": {"market": "HK"}},
        {"input": {"account": "lx"}},
        {"input": {"account": "lx", "market": "US", "date": "2026-07-19", "revision": 0}},
    ),
    output_contract=_OUTPUT_CONTRACT,
    output_contract_resolver=_daily_brief_output_contract,
    bot_input_fields=("account", "market", "date", "revision", "section", "cursor"),
    bot_input_normalizer=_daily_brief_bot_input,
)

TOOLS: tuple[AgentTool, ...] = (DAILY_DECISION_BRIEF_READ_TOOL,)


__all__ = ["DAILY_DECISION_BRIEF_READ_TOOL", "TOOLS", "read_daily_brief_view"]
