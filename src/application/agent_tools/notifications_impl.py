from __future__ import annotations

from typing import Any, Callable


def preview_notification_tool(
    payload: dict[str, Any],
    *,
    query_daily_brief: Callable[
        [dict[str, Any]],
        tuple[dict[str, Any], list[str], dict[str, Any]],
    ],
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    view, warnings, meta = query_daily_brief(payload)
    return {
        "schema_version": "preview_notification.output.v2",
        "available": bool(view.get("available")),
        "reason": str(view.get("reason") or "unavailable"),
        "query": dict(view.get("query") or {}),
        "notification_text": str(view.get("rendered_markdown") or ""),
        "effective_actionability": str(
            view.get("effective_actionability") or "unavailable"
        ),
        "coverage": dict(view.get("coverage") or {}),
        "source": dict(view.get("source") or {}),
        "freshness": dict(view.get("freshness") or {}),
        "renderer": "daily_decision_brief.query",
        "authority": "daily_decision_brief",
        "delivery_evidence": False,
    }, warnings, meta
