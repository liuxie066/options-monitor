from __future__ import annotations

from pathlib import Path
from typing import Any, Callable


def preview_notification_tool(payload: dict[str, Any], *, build_notification: Callable[[str, str], str]) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    alerts_text = str(payload.get("alerts_text") or "").strip()
    changes_text = str(payload.get("changes_text") or "").strip()
    account_label = str(payload.get("account_label") or "当前账户").strip() or "当前账户"
    if not alerts_text and payload.get("alerts_path"):
        alerts_text = Path(str(payload.get("alerts_path"))).read_text(encoding="utf-8")
    if not changes_text and payload.get("changes_path"):
        changes_text = Path(str(payload.get("changes_path"))).read_text(encoding="utf-8")
    preview = build_notification(changes_text, alerts_text, account_label=account_label)
    return {"account_label": account_label, "notification_text": preview}, [], {}
