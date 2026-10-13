"""Tool-only display pages over complete, authenticated performance query pages."""
from __future__ import annotations

import json
from typing import Any, Callable

from src.application.agent_tool_contracts import AgentToolError
from src.application.agent_tools.project_reader import page_text
from src.application.query_cursor import CursorError, decode_cursor, digest, encode_cursor

_KIND = "performance_tool_page.v1"


def _size(value: dict[str, Any]) -> int:
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))


def _summary(data: dict[str, Any]) -> dict[str, Any]:
    if _size(data) <= 6000:
        return data
    result = {**data, "breakdowns": {},
              "detail_query": {"view": "breakdowns", "group_by": "symbols", "limit": 20}}
    if _size(result) > 6000:
        raise AgentToolError("READ_ERROR", "scope_too_broad")
    for group in ("currencies", "leg_types", "accounts", "symbols", "opening_months",
                  "opening_years", "attribution_strategies", "parent_universes"):
        candidate = {**result, "breakdowns": {**result["breakdowns"], group: data["breakdowns"].get(group, [])}}
        if _size(candidate) <= 6000:
            result = candidate
    return result


def _page(data: dict[str, Any], state: dict[str, Any] | None, binding: str) -> dict[str, Any]:
    rows = data["rows"]
    index = state["index"] if state else 0
    if type(index) is not int or not 0 <= index <= len(rows) or (state and rows and index == len(rows)):
        raise AgentToolError("INPUT_ERROR", "cursor_invalidated")
    body_cursor = state.get("body_cursor") if state else None
    if body_cursor is not None and (not isinstance(body_cursor, str) or not body_cursor or index >= len(rows)):
        raise AgentToolError("INPUT_ERROR", "cursor_invalidated")

    def result_for(end: int, fragment: dict[str, Any] | None = None) -> dict[str, Any]:
        selected = [] if fragment else rows[index:end]
        result = {**data, "rows": selected,
                  "breakdowns": {data["group_by"]: selected} if data["view"] == "breakdowns" and not fragment else {},
                  "pagination": {k: v for k, v in data["pagination"].items() if k not in {"page_cursor", "next_cursor"}},
                  "coverage": {**data["coverage"], "status": "complete",
                               "complete_for": "point" if fragment else "requested_page",
                               "included_count": len(selected), "omitted_count": data["pagination"]["matched_count"] - len(selected)}}
        result.pop("continuation_status", None)
        if fragment:
            result.update({name: fragment[name] for name in ("text", "body_range", "body_complete")})
        within_page = end < len(rows)
        has_more = within_page or data["pagination"]["has_more"]
        result["pagination"].update(returned_count=len(selected), has_more=has_more)
        result["coverage"]["has_more"] = has_more
        result["next_cursor"] = None
        if has_more:
            replay = data["pagination"]["page_cursor"] if within_page else data["next_cursor"]
            try:
                if not replay or (fragment and fragment.get("continuation_status")):
                    raise CursorError("continuation_unavailable")
                result["next_cursor"] = encode_cursor({
                    "kind": _KIND, "binding": binding, "page_cursor": replay,
                    "index": end if within_page else 0,
                    "body_cursor": fragment.get("next_cursor") if fragment and not fragment["body_complete"] else None,
                })
            except CursorError as exc:
                if exc.code != "continuation_unavailable":
                    raise AgentToolError("READ_ERROR", exc.code) from exc
                result["continuation_status"] = exc.code
                result["coverage"].update(status="partial", complete=False)
                if fragment:
                    result["body_complete"] = False
        result["pagination"]["next_cursor"] = result["next_cursor"]
        return result

    if body_cursor is None:
        for end in range(len(rows), index, -1):
            result = result_for(end)
            if _size(result) <= 5000:
                return result
        if index == len(rows):
            result = result_for(index)
            if _size(result) <= 5000:
                return result
            raise AgentToolError("READ_ERROR", "scope_too_broad")
    raw = json.dumps(rows[index], ensure_ascii=False, sort_keys=True).encode("utf-8")
    max_chars = 4000
    while True:
        try:
            fragment = page_text(raw, relative_name=data["view"] + "/" + str(data["pagination"]["offset"] + index),
                                 resource="option_performance_report",
                                 scope={**data["scope"], "source_hash": data["source"]["content_hash"]},
                                 cursor=body_cursor, max_chars=max_chars)
        except CursorError as exc:
            raise AgentToolError("READ_ERROR", exc.code) from exc
        end = index + 1 if fragment["body_complete"] else index
        result = result_for(end, fragment)
        if _size(result) <= 5000:
            return result
        if max_chars == 1:
            raise AgentToolError("READ_ERROR", "scope_too_broad")
        max_chars = max(1, max_chars // 2)


def present_option_performance(
    payload: dict[str, Any], *, query: Callable[[dict[str, Any]], tuple[dict[str, Any], list[str], dict[str, Any]]]
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    if not any(name in payload for name in ("view", "group_by", "symbol", "limit", "cursor")):
        return query(payload)
    request = {name: value for name, value in payload.items() if name != "cursor"}
    binding = digest({"kind": _KIND, "query": request})
    try:
        state = decode_cursor(payload.get("cursor"), binding)
    except CursorError as exc:
        raise AgentToolError("INPUT_ERROR", exc.code) from exc
    if state:
        if (state.get("kind") != _KIND or not isinstance(state.get("page_cursor"), str)
                or not state["page_cursor"] or type(state.get("index")) is not int):
            raise AgentToolError("INPUT_ERROR", "cursor_invalidated")
        request["cursor"] = state["page_cursor"]
    data, warnings, meta = query(request)
    return (_summary(data) if data["view"] == "summary" else _page(data, state, binding)), warnings, meta
