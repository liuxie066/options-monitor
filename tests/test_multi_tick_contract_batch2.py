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
    assert "resolve_multi_tick_engine_entrypoint(" in src
    assert "account scheduler decision view must be valid" in src
    assert "stage='account_scheduler_decision'" in src


def test_multi_tick_trading_day_guard_decision_delegates_to_engine() -> None:
    base = Path(__file__).resolve().parents[1]
    src = (base / "scripts" / "multi_tick" / "main.py").read_text(encoding="utf-8")
    assert "decide_trading_day_guard(" in src
    assert "opend_unhealthy={" in src
    assert "decide_notification_delivery(" in src


def test_multi_tick_io_and_decision_failure_audit_fields_are_distinguishable() -> None:
    base = Path(__file__).resolve().parents[1]
    src = (base / "scripts" / "multi_tick" / "main.py").read_text(encoding="utf-8")
    assert "normalize_subprocess_adapter_payload(" in src
    assert "normalize_pipeline_subprocess_output(" in src
    assert "normalize_notify_subprocess_output(" in src
    assert "failure_kind='io_error'" in src
    assert "failure_kind='decision_error'" in src


def test_multi_tick_pipeline_calls_share_context_dir() -> None:
    base = Path(__file__).resolve().parents[1]
    src = (base / "scripts" / "multi_tick" / "main.py").read_text(encoding="utf-8")
    assert "shared_context_dir=run_repo.get_run_state_dir(base, run_id)" in src
