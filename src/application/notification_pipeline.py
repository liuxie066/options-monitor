from __future__ import annotations

from src.application.agent_tools import run_agent_tool


def preview_notification(
    *,
    alerts_path: str | None = None,
    changes_path: str | None = None,
    alerts_text: str | None = None,
    changes_text: str | None = None,
    account_label: str | None = None,
) -> dict:
    payload: dict[str, object] = {}
    if alerts_path:
        payload["alerts_path"] = str(alerts_path)
    if changes_path:
        payload["changes_path"] = str(changes_path)
    if alerts_text:
        payload["alerts_text"] = str(alerts_text)
    if changes_text:
        payload["changes_text"] = str(changes_text)
    if account_label:
        payload["account_label"] = str(account_label)
    return run_agent_tool("preview_notification", payload)

