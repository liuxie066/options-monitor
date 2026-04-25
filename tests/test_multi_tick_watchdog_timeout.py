from __future__ import annotations

import importlib
import subprocess


def test_watchdog_timeout_should_not_degrade_and_should_skip_pipeline(
    argv_scope,
    fake_runlog_factory,
    monkeypatch,
    runtime_config_copy,
) -> None:
    mt = importlib.import_module("scripts.multi_tick.main")

    events: list[dict] = []
    scheduler_called = {"value": 0}

    monkeypatch.setattr(mt, "RunLogger", lambda base: fake_runlog_factory(events))
    monkeypatch.setattr(
        mt,
        "run_opend_watchdog",
        lambda **_kwargs: (_ for _ in ()).throw(subprocess.TimeoutExpired(cmd="opend_watchdog", timeout=35)),
    )

    def _scheduler_should_not_run(**_kwargs):
        scheduler_called["value"] += 1
        raise AssertionError("scheduler should not run when watchdog times out")

    monkeypatch.setattr(mt, "run_scan_scheduler_cli", _scheduler_should_not_run)
    monkeypatch.setattr(mt, "send_opend_alert", lambda *a, **k: None)
    monkeypatch.setattr(mt, "admit_project_run", lambda *_a, **_k: {"allowed": True})
    monkeypatch.setattr(mt.state_repo, "write_account_last_run", lambda *a, **k: None)
    monkeypatch.setattr(mt.state_repo, "put_idempotency_success", lambda *a, **k: {"created": True})
    monkeypatch.setattr(mt.state_repo, "append_audit_event", lambda *a, **k: None)
    monkeypatch.setattr(mt, "is_opend_phone_verify_pending", lambda _base: False)

    argv_scope(
        [
            "send_if_needed_multi.py",
            "--config",
            str(runtime_config_copy),
            "--accounts",
            "lx",
            "--market-config",
            "us",
            "--no-send",
        ]
    )
    rc = mt.main()

    assert rc == 0
    assert scheduler_called["value"] == 0
    assert any(e.get("step") == "watchdog" and e.get("status") == "error" for e in events)
    assert any(e.get("step") == "run_end" and e.get("status") == "error" for e in events)
