"""第5步收口回归：Entry 仅编排，外部调用下沉到基础设施拥有者模块。"""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_entry_imports_service_module() -> None:
    multi_tick = _read("src/application/multi_account_tick.py")
    send_if_needed = _read("scripts/send_if_needed.py")

    assert "from src.infrastructure.external_services import (" in multi_tick
    assert "from src.application.multi_account_tick import current_run_id, run_tick" in send_if_needed


def test_legacy_infra_service_wrappers_are_removed() -> None:
    assert not (ROOT / "scripts" / "infra" / "service.py").exists()
    assert not (ROOT / "scripts" / "infra" / "entry_external.py").exists()
