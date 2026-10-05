from __future__ import annotations

from src.application.tool_execution import execute_tool


def preview_notification(
    *,
    account: str | None = None,
    market: str | None = None,
    date: str | None = None,
    revision: int | None = None,
) -> dict:
    payload: dict[str, object] = {}
    if account:
        payload["account"] = str(account)
    if market:
        payload["market"] = str(market)
    if date:
        payload["date"] = str(date)
    if revision is not None:
        payload["revision"] = int(revision)
    return execute_tool("preview_notification", payload)
