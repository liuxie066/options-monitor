from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path


BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))


class _FakeRunLogger:
    def __init__(self, _base: Path):
        self.run_id = "test-run"
        self.events: list[dict] = []

    def safe_event(self, step: str, status: str, **kwargs) -> None:
        rec = {"step": step, "status": status}
        rec.update(kwargs)
        self.events.append(rec)


def test_watchdog_timeout_should_not_degrade_and_should_skip_pipeline() -> None:
    mt = importlib.import_module("scripts.multi_tick.main")

    events: list[dict] = []
    scheduler_called = {"value": 0}
    old = {
        "RunLogger": mt.RunLogger,
        "run_opend_watchdog": mt.run_opend_watchdog,
        "run_scan_scheduler_cli": mt.run_scan_scheduler_cli,
        "send_opend_alert": mt.send_opend_alert,
        "write_account_last_run": mt.state_repo.write_account_last_run,
        "put_idempotency_success": mt.state_repo.put_idempotency_success,
        "append_audit_event": mt.state_repo.append_audit_event,
        "is_opend_phone_verify_pending": mt.is_opend_phone_verify_pending,
        "argv": sys.argv[:],
    }

    try:
        def _mk_logger(base: Path):
            lg = _FakeRunLogger(base)

            def _safe_event(step: str, status: str, **kwargs) -> None:
                rec = {"step": step, "status": status}
                rec.update(kwargs)
                events.append(rec)

            lg.safe_event = _safe_event  # type: ignore[assignment]
            return lg

        def _raise_timeout(**_kwargs):
            raise subprocess.TimeoutExpired(cmd="opend_watchdog", timeout=35)

        def _scheduler_should_not_run(**_kwargs):
            scheduler_called["value"] += 1
            raise AssertionError("scheduler should not run when watchdog times out")

        mt.RunLogger = _mk_logger  # type: ignore[assignment]
        mt.run_opend_watchdog = _raise_timeout  # type: ignore[assignment]
        mt.run_scan_scheduler_cli = _scheduler_should_not_run  # type: ignore[assignment]
        mt.send_opend_alert = lambda *a, **k: None  # type: ignore[assignment]
        mt.state_repo.write_account_last_run = lambda *a, **k: None  # type: ignore[assignment]
        mt.state_repo.put_idempotency_success = lambda *a, **k: {"created": True}  # type: ignore[assignment]
        mt.state_repo.append_audit_event = lambda *a, **k: None  # type: ignore[assignment]
        mt.is_opend_phone_verify_pending = lambda _base: False  # type: ignore[assignment]

        cfg_path = (BASE / "config.us.json").resolve()
        sys.argv = [
            "send_if_needed_multi.py",
            "--config",
            str(cfg_path),
            "--accounts",
            "lx",
            "--market-config",
            "us",
            "--no-send",
        ]
        rc = mt.main()

        assert rc == 0
        assert scheduler_called["value"] == 0
        assert any(e.get("step") == "watchdog" and e.get("status") == "error" for e in events)
        assert any(e.get("step") == "run_end" and e.get("status") == "error" for e in events)
    finally:
        mt.RunLogger = old["RunLogger"]  # type: ignore[assignment]
        mt.run_opend_watchdog = old["run_opend_watchdog"]  # type: ignore[assignment]
        mt.run_scan_scheduler_cli = old["run_scan_scheduler_cli"]  # type: ignore[assignment]
        mt.send_opend_alert = old["send_opend_alert"]  # type: ignore[assignment]
        mt.state_repo.write_account_last_run = old["write_account_last_run"]  # type: ignore[assignment]
        mt.state_repo.put_idempotency_success = old["put_idempotency_success"]  # type: ignore[assignment]
        mt.state_repo.append_audit_event = old["append_audit_event"]  # type: ignore[assignment]
        mt.is_opend_phone_verify_pending = old["is_opend_phone_verify_pending"]  # type: ignore[assignment]
        sys.argv = old["argv"]


def main() -> None:
    test_watchdog_timeout_should_not_degrade_and_should_skip_pipeline()
    print("OK (watchdog-timeout)")


if __name__ == "__main__":
    main()
