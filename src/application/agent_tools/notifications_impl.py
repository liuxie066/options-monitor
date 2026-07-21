from __future__ import annotations

from pathlib import Path
from typing import Any, Callable


def preview_notification_tool(payload: dict[str, Any], *, build_notification: Callable[..., str]) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    alerts_text = str(payload.get("alerts_text") or "").strip()
    changes_text = str(payload.get("changes_text") or "").strip()
    account_label = str(payload.get("account_label") or "当前账户").strip() or "当前账户"
    raw_render_style = payload.get("render_style")
    render_style = "compact" if raw_render_style is None else str(raw_render_style).strip().lower()
    if not alerts_text and payload.get("alerts_path"):
        alerts_text = Path(str(payload.get("alerts_path"))).read_text(encoding="utf-8")
    if not changes_text and payload.get("changes_path"):
        changes_text = Path(str(payload.get("changes_path"))).read_text(encoding="utf-8")
    preview = build_notification(changes_text, alerts_text, account_label=account_label, render_style=render_style)
    warnings = [
        "Compact Tick preview is compatibility-only and is not scheduled delivery evidence."
        if render_style == "compact"
        else "Legacy Tick preview is deprecated, compatibility-only, and is not scheduled delivery evidence."
    ]
    return {
        "account_label": account_label,
        "notification_text": preview,
        "renderer": render_style,
        "render_style": render_style,
        "authority": "compatibility_only",
        "delivery_evidence": False,
    }, warnings, {}
