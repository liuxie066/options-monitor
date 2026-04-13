from __future__ import annotations

from pathlib import Path


def test_multi_tick_account_messages_snapshot_contract_guard_present() -> None:
    base = Path(__file__).resolve().parents[1]
    src = (base / "scripts" / "multi_tick" / "main.py").read_text(encoding="utf-8")
    assert "snapshot_name': 'account_messages'" in src
    assert "stage='account_messages_snapshot'" in src
    assert "account_messages must be a dict" in src


def test_multi_tick_scheduler_and_account_decision_use_objectized_contract_path() -> None:
    base = Path(__file__).resolve().parents[1]
    src = (base / "scripts" / "multi_tick" / "main.py").read_text(encoding="utf-8")
    assert "snapshot_name': 'scheduler_raw'" in src
    assert "AccountSchedulerDecisionView.from_payload(" in src
    assert "build_account_scheduler_decision_dto(" in src
    assert "stage='account_scheduler_decision'" in src
