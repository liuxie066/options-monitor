"""Deliver a finished report through an existing notification channel.

Reports stay generation-first: the artifacts are written before delivery is
attempted, and a delivery problem never removes the local report.  Outbound
delivery reuses the same Feishu app channel and bot settings as the daily
brief; this module adds no second credential set and no HTTP client.

Outcome contract (recorded in ``run.json`` under ``delivery``):

* ``delivered`` — provider confirmed the message (``message_id`` present).
* ``skipped`` — no channel was declared, ``--no-deliver`` was passed, or the
  channel is unavailable in this environment (plugin missing / credentials
  missing).  The report run still succeeds; the reason is explicit.
* ``failed`` — a send was attempted and did not confirm delivery.  The report
  run still succeeds locally, but the CLI surfaces a non-zero exit code.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.application.agent_tool_contracts import AgentToolError
from src.application.channels.feishu_notification import (
    normalize_feishu_app_send_output,
    send_feishu_app_message,
)
from src.application.channels.plugins import channel_plugin_installed
from src.application.notification_delivery_adapter import (
    build_notification_transport_key,
    notification_target_reference,
)
from src.application.reports.registry import (
    DELIVERY_FEISHU_APP,
    DELIVERY_NONE,
)
from src.application.secret_resolver import resolve_feishu_bot_config


DELIVERY_SCHEMA_VERSION = "report_delivery.v1"


def deliver_report(
    *,
    report_id: str,
    run_id: str,
    channel: str,
    markdown: str,
    base: Path,
    enabled: bool = True,
) -> dict[str, Any]:
    """Attempt one delivery; never raise and never touch the report artifacts."""

    if channel == DELIVERY_NONE:
        return _skipped(channel, "no_channel", "报告未声明投递渠道，跳过投递")
    if not enabled:
        return _skipped(channel, "delivery_disabled", "已按 --no-deliver 跳过投递")
    if channel == DELIVERY_FEISHU_APP:
        return _deliver_feishu(report_id=report_id, run_id=run_id, markdown=markdown, base=base)
    return _failed(channel, "unsupported_delivery_channel", f"不支持的投递渠道：{channel}")


def delivery_failed(delivery: dict[str, Any] | None) -> bool:
    return isinstance(delivery, dict) and delivery.get("status") == "failed"


def _deliver_feishu(*, report_id: str, run_id: str, markdown: str, base: Path) -> dict[str, Any]:
    channel = DELIVERY_FEISHU_APP
    if not channel_plugin_installed(channel):
        return _skipped(
            channel,
            "channel_plugin_not_installed",
            "飞书渠道插件未安装，跳过投递",
            hint="运行 om plugins install feishu --confirm 安装后再投递",
        )
    try:
        bot = resolve_feishu_bot_config()
    except Exception as exc:
        return _skipped(
            channel,
            "channel_not_configured",
            f"渠道配置不可用，跳过投递：{type(exc).__name__}: {exc}",
        )
    if not bot.send_ready:
        missing = list(bot.send_missing_fields)
        return _skipped(
            channel,
            "channel_not_configured",
            "未配置渠道，跳过投递",
            missing=missing,
            hint="设置 OM_FEISHU_BOT_APP_ID / OM_FEISHU_BOT_APP_SECRET / OM_FEISHU_BOT_USER_OPEN_ID",
        )

    idempotency_key = build_notification_transport_key(f"report:{report_id}:{run_id}")
    target_reference = notification_target_reference(bot.user_open_id)
    try:
        raw = send_feishu_app_message(
            base=base,
            channel=channel,
            target=bot.user_open_id,
            message=markdown,
            notifications=None,
            idempotency_key=idempotency_key,
        )
    except AgentToolError as exc:
        if exc.code == "CHANNEL_PLUGIN_NOT_INSTALLED":
            return _skipped(channel, "channel_plugin_not_installed", "飞书渠道插件未安装，跳过投递")
        return _failed(channel, exc.code or "delivery_failed", exc.message or str(exc), idempotency_key)
    except ValueError as exc:
        return _skipped(channel, "channel_not_configured", f"未配置渠道，跳过投递：{exc}")
    except Exception as exc:  # pragma: no cover - defensive: unexpected transport failure
        return _failed(
            channel,
            "delivery_failed",
            f"{type(exc).__name__}: {exc}",
            idempotency_key,
        )

    normalized = normalize_feishu_app_send_output(send_result=raw)
    if normalized.get("ok"):
        return {
            **_base(channel, "delivered", True),
            "message": "已投递到飞书",
            "message_id": normalized.get("message_id"),
            "target_reference": target_reference,
            "http_status": normalized.get("http_status"),
            "idempotency_key": idempotency_key,
        }
    return {
        **_base(channel, "failed", False),
        "message": _delivery_error_message(normalized),
        "error_code": normalized.get("error_code") or "delivery_failed",
        "message_id": normalized.get("message_id"),
        "target_reference": target_reference,
        "http_status": normalized.get("http_status"),
        "idempotency_key": idempotency_key,
        "ambiguous_send": bool(normalized.get("ambiguous_send")),
        "duplicate_risk": bool(normalized.get("duplicate_risk")),
    }


def _delivery_error_message(normalized: dict[str, Any]) -> str:
    return str(
        normalized.get("message")
        or normalized.get("error_code")
        or normalized.get("local_error_code")
        or "飞书投递未确认"
    )


def _base(channel: str, status: str, ok: bool) -> dict[str, Any]:
    return {
        "schema_version": DELIVERY_SCHEMA_VERSION,
        "channel": channel,
        "status": status,
        "ok": ok,
        "attempted_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
    }


def _skipped(channel: str, reason: str, message: str, **extra: Any) -> dict[str, Any]:
    return {
        **_base(channel, "skipped", False),
        "reason": reason,
        "message": message,
        **{key: value for key, value in extra.items() if value is not None},
    }


def _failed(channel: str, reason: str, message: str, idempotency_key: str | None = None) -> dict[str, Any]:
    return {
        **_base(channel, "failed", False),
        "reason": reason,
        "message": message,
        "error_code": reason,
        **({"idempotency_key": idempotency_key} if idempotency_key else {}),
    }


__all__ = [
    "DELIVERY_SCHEMA_VERSION",
    "deliver_report",
    "delivery_failed",
]
