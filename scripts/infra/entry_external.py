from __future__ import annotations

"""兼容适配层：保留旧导入路径，实际实现统一委托给 service。"""

from scripts.infra.service import (
    run_command,
    run_opend_watchdog,
    run_pipeline_script,
    run_scan_scheduler_cli,
    send_openclaw_message,
    trading_day_via_futu,
)

__all__ = [
    "run_command",
    "run_scan_scheduler_cli",
    "run_pipeline_script",
    "run_opend_watchdog",
    "send_openclaw_message",
    "trading_day_via_futu",
]
