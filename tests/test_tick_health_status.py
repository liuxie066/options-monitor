from __future__ import annotations

from datetime import datetime, timezone
from subprocess import CompletedProcess

from src.application.agent_tools.runtime_status_impl import _tick_health_from_artifacts
from src.application.quality.runtime_checks import build_runtime_checks


def test_tick_health_reason_is_shared_with_quality_and_recovery(tmp_path) -> None:
    state = tmp_path / "output_shared" / "state"
    current = state / "current" / "tick_cron_last_result.hk.current.json"
    pending = state / "opend_phone_verify_pending.json"
    values = {
        str(current): {"status": "failed", "error_code": "TICK_EXEC_FAILED",
                       "event_at_utc": "2026-09-23T04:51:00Z", "run_id": "wrapper-1"},
        str(pending): {},
    }
    accounts = {
        "lx": {"last_run": {"json": {"error_code": "OPEND_NEEDS_PIC_VERIFY",
                                      "last_run_utc": "2026-09-23T04:50:30Z"}}}
    }

    def read(path):
        return values.get(str(path), {})

    failed = _tick_health_from_artifacts(
        shared_state_dir=state, market="hk", account_status=accounts,
        read_json_object_or_empty=read,
    )
    assert failed["reason_code"] == "OPEND_NEEDS_PIC_VERIFY"
    checks = build_runtime_checks(
        runtime_statuses=[{"tick_health": failed}],
        observed_at_utc="2026-09-23T04:52:00Z",
        now=datetime(2026, 9, 23, 4, 52, tzinfo=timezone.utc),
    )
    tick_check = next(check for check in checks if check["check_id"] == "RT-OM-005")
    assert tick_check["status"] == "fail"
    assert tick_check["reason_code"] == failed["reason_code"]

    values[str(pending)] = {"pending": True}
    assert _tick_health_from_artifacts(
        shared_state_dir=state, market="hk", account_status=accounts,
        read_json_object_or_empty=read,
    )["reason_code"] == "OPEND_NEEDS_PHONE_VERIFY"

    values[str(pending)] = {}
    values[str(current)] = {"status": "ok", "event_at_utc": "2026-09-23T05:00:00Z"}
    assert _tick_health_from_artifacts(
        shared_state_dir=state, market="hk", account_status=accounts,
        read_json_object_or_empty=read,
    )["status"] == "ok"


def test_tick_unit_exit_code_overrides_success_result(monkeypatch) -> None:
    from src.application.agent_tools import runtime_status_impl

    monkeypatch.setattr(runtime_status_impl.subprocess, "run",
                        lambda command, **kwargs: CompletedProcess(command, 0, "Result=success\nExecMainStatus=2\n", ""))
    assert runtime_status_impl._tick_unit_execution_status("hk") == "failed"
