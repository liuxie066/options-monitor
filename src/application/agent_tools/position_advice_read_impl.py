from __future__ import annotations

from datetime import datetime
from typing import Any

from domain.domain.daily_decision_brief import daily_brief_position_roll, daily_brief_position_roll_is_current


def read_position_advice_view(
    view: dict[str, Any], *, action: str | None = None, now_utc: datetime | None = None,
) -> dict[str, Any]:
    """Project saved advice; the Daily Brief reader owns scope and freshness."""
    rows: list[dict[str, Any]] = []
    sections = view.get("sections") if isinstance(view.get("sections"), list) else [view]
    scopes = [{"account": (item.get("query") or {}).get("account"),
               "market": (item.get("query") or {}).get("market"),
               "status": "available" if item.get("available") else item.get("reason"),
               "brief": item.get("brief"), "freshness": item.get("freshness"),
               "effective_actionability": item.get("effective_actionability"),
               "latest_attempt": item.get("latest_attempt")} for item in sections]
    for scope in scopes:
        brief = scope.get("brief")
        if not isinstance(brief, dict):
            continue
        live = scope.get("effective_actionability") == "live_actionable"
        for position in brief.get("positions") or []:
            if not isinstance(position, dict):
                continue
            common = {
                "account": scope.get("account"), "market": scope.get("market"),
                "position_lot_id": position.get("position_lot_id"), "symbol": position.get("symbol"),
                "option_type": position.get("option_type"), "contract_symbol": position.get("contract_symbol"),
                "expiration": position.get("expiration"), "strike": position.get("strike"),
                "run_id": brief.get("run_id"), "revision": brief.get("revision"),
                "effective_actionability": scope.get("effective_actionability"),
                "freshness": scope.get("freshness"),
            }
            if action in (None, "close"):
                close = {key: value for key, value in position.items()
                         if key not in {"roll_advice", "roll_notification_eligible"}}
                rows.append({**common, "action": "close", "close_advice": close,
                    "status": position.get("evaluation_status") or "unavailable",
                    "recommendation_state": position.get("recommendation_state"),
                    "actionable": live and position.get("close_actionable") is True
                        and position.get("recommendation_state") == "close"})
            direction = {"call": "roll_up", "put": "roll_down"}.get(position.get("option_type"))
            if direction is None or action not in (None, direction):
                continue
            result = position.get("roll_advice")
            selected = daily_brief_position_roll(position)
            status = result.get("status", "unavailable") if isinstance(result, dict) else "unavailable"
            outcome = result.get("outcome") if isinstance(result, dict) else None
            reasons = list(result.get("reason_codes") or []) if isinstance(result, dict) else ["roll_advice_missing"]
            if isinstance(result, dict) and outcome == "found" and selected is None:
                status = "unavailable"
                reasons.append("roll_advice_not_eligible")
            current = daily_brief_position_roll_is_current(position, now_utc=now_utc)
            if selected is not None and not current:
                reasons.append("roll_advice_stale_or_freshness_missing")
            rows.append({**common, "action": direction, "status": status, "outcome": outcome,
                "actionable": live and selected is not None and current,
                "actionable_until_utc": result.get("actionable_until_utc") if isinstance(result, dict) else None,
                "reason_codes": reasons, "roll_advice": result})
    return {
        "schema_version": "position_advice_read.output.v1", "status": "available" if view.get("available") else view.get("reason"),
        "query": {**dict(view.get("query") or {}), "action": action},
        "scope": dict(view.get("scope") or {}), "source": dict(view.get("source") or {}),
        "scopes": [{key: item.get(key) for key in ("account", "market", "status", "freshness",
            "effective_actionability", "latest_attempt")} for item in scopes],
        "coverage": {"source_count": len(scopes), "returned_count": len(rows),
            "truncated": False}, "returned_count": len(rows),
        "actionable_count": sum(row["actionable"] for row in rows), "rows": rows,
    }


def position_advice_read_tool(payload: dict[str, Any]):
    from src.application.agent_tools.daily_brief import query_daily_brief_tool_view

    view, warnings, metadata = query_daily_brief_tool_view(
        {key: value for key, value in payload.items() if key != "action"})
    return read_position_advice_view(view, action=payload.get("action")), warnings, metadata
