"""第5步收口回归：Entry 仅编排，外部调用下沉到 infra/service。"""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_entry_imports_service_module() -> None:
    multi_tick = _read("scripts/multi_tick/main.py")
    send_if_needed = _read("scripts/send_if_needed.py")

    assert "from scripts.infra.service import (" in multi_tick
    assert "from src.application.multi_account_tick import current_run_id, run_tick" in send_if_needed


def test_entry_external_is_compat_reexport() -> None:
    from scripts.infra import entry_external, service

    assert entry_external.run_command is service.run_command
    assert entry_external.run_scan_scheduler_cli is service.run_scan_scheduler_cli
    assert entry_external.run_pipeline_script is service.run_pipeline_script
    assert entry_external.run_opend_watchdog is service.run_opend_watchdog
    assert entry_external.send_openclaw_message is service.send_openclaw_message
    assert entry_external.trading_day_via_futu is service.trading_day_via_futu
