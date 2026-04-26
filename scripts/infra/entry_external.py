from __future__ import annotations

"""兼容适配层：保留旧导入路径，实际实现统一委托给 service。"""

from scripts.infra.service import (
    load_feishu_notification_app_config,
    normalize_feishu_app_send_output,
    run_command,
    run_opend_watchdog,
    run_pipeline_script,
    run_scan_scheduler_cli,
    send_feishu_app_message,
    send_feishu_app_message_process,
    send_openclaw_message,
    trading_day_via_futu,
)

__all__ = [
    "run_command",
    "run_scan_scheduler_cli",
    "run_pipeline_script",
    "run_opend_watchdog",
    "load_feishu_notification_app_config",
    "send_feishu_app_message",
    "send_feishu_app_message_process",
    "normalize_feishu_app_send_output",
    "send_openclaw_message",
    "trading_day_via_futu",
]
