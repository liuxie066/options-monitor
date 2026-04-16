from __future__ import annotations

import importlib
from types import SimpleNamespace
from pathlib import Path


class _FakeRunLogger:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def safe_event(self, step: str, status: str, **kwargs) -> None:
        rec = {"step": step, "status": status}
        rec.update(kwargs)
        self.events.append(rec)


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
    assert "normalize_notify_subprocess_output" in src
    assert "failure_kind='io_error'" in src
    assert "failure_kind='decision_error'" in src


def test_multi_tick_pipeline_calls_share_context_dir() -> None:
    base = Path(__file__).resolve().parents[1]
    src = (base / "scripts" / "multi_tick" / "main.py").read_text(encoding="utf-8")
    assert "shared_context_dir=run_repo.get_run_state_dir(base, run_id)" in src


def test_multi_tick_notify_failure_is_account_isolated() -> None:
    base = Path(__file__).resolve().parents[1]
    src = (base / "scripts" / "multi_tick" / "main.py").read_text(encoding="utf-8")
    assert "notify_failures: list[dict[str, object]] = []" in src
    assert "NOTIFY_SEND_MAX_ATTEMPTS = 3" in src
    assert "NOTIFY_SEND_RETRY_DELAYS_SEC = (1.0, 3.0)" in src
    assert "notify_failures.append(" in src
    assert "continue" in src[src.index("notify_failures.append(") : src.index("sent_accounts.append(acct)")]
    assert "'final_returncode': int(send_result.get('final_returncode') or 0)" in src
    assert "for acct in sent_accounts:" in src
    assert "mark_notified=True" in src
    assert "NOTIFY_PARTIAL_FAILED" in src
    assert "'notify_summary': notify_summary" in src


def test_multi_tick_notify_retry_recovers_unconfirmed_send() -> None:
    mt = importlib.import_module("scripts.multi_tick.main")

    sends = [
        SimpleNamespace(returncode=0, stdout='{"ok":true}', stderr=""),
        SimpleNamespace(returncode=0, stdout='{"messageId":"lx-2"}', stderr=""),
    ]
    send_calls: list[dict] = []
    audit_events: list[dict] = []
    sleeps: list[float] = []
    runlog = _FakeRunLogger()

    def _send(**kwargs):
        send_calls.append(dict(kwargs))
        return sends.pop(0)

    def _audit(event_type, action, **kwargs):
        audit_events.append({"event_type": event_type, "action": action, **kwargs})

    result = mt._send_account_message_with_retry(
        base=Path("/tmp/options-monitor-test"),
        channel="feishu",
        target="user:test",
        account="lx",
        message="hello",
        run_id="run-1",
        runlog=runlog,
        audit_fn=_audit,
        send_fn=_send,
        sleep_fn=lambda seconds: sleeps.append(seconds),
    )

    assert result["ok"] is True
    assert result["attempts"] == 2
    assert len(send_calls) == 2
    assert sleeps == [1.0]
    assert [e["status"] for e in audit_events] == ["unconfirmed", "ok"]
    assert audit_events[0]["extra"]["delivery_confirmed"] is False
    assert audit_events[1]["extra"]["message_id"] == "lx-2"
    assert [e["status"] for e in runlog.events] == ["error", "ok"]


def test_multi_tick_notify_retry_exhausts_unconfirmed_without_success() -> None:
    mt = importlib.import_module("scripts.multi_tick.main")

    audit_events: list[dict] = []
    sleeps: list[float] = []
    runlog = _FakeRunLogger()

    def _send(**_kwargs):
        return SimpleNamespace(returncode=0, stdout='{"ok":true}', stderr="")

    def _audit(event_type, action, **kwargs):
        audit_events.append({"event_type": event_type, "action": action, **kwargs})

    result = mt._send_account_message_with_retry(
        base=Path("/tmp/options-monitor-test"),
        channel="feishu",
        target="user:test",
        account="lx",
        message="hello",
        run_id="run-1",
        runlog=runlog,
        audit_fn=_audit,
        send_fn=_send,
        sleep_fn=lambda seconds: sleeps.append(seconds),
    )

    assert result["ok"] is False
    assert result["error_code"] == "SEND_UNCONFIRMED"
    assert result["attempts"] == 3
    assert result["final_returncode"] == 0
    assert result["command_ok"] is True
    assert result["delivery_confirmed"] is False
    assert sleeps == [1.0, 3.0]
    assert [e["status"] for e in audit_events] == ["unconfirmed", "unconfirmed", "unconfirmed"]
    assert all(e["extra"]["attempt"] in {1, 2, 3} for e in audit_events)


def test_multi_tick_notify_retry_exhausts_failed_send() -> None:
    mt = importlib.import_module("scripts.multi_tick.main")

    audit_events: list[dict] = []
    sleeps: list[float] = []
    runlog = _FakeRunLogger()

    def _send(**_kwargs):
        return SimpleNamespace(returncode=2, stdout="", stderr="boom")

    def _audit(event_type, action, **kwargs):
        audit_events.append({"event_type": event_type, "action": action, **kwargs})

    result = mt._send_account_message_with_retry(
        base=Path("/tmp/options-monitor-test"),
        channel="feishu",
        target="user:test",
        account="sy",
        message="hello",
        run_id="run-1",
        runlog=runlog,
        audit_fn=_audit,
        send_fn=_send,
        sleep_fn=lambda seconds: sleeps.append(seconds),
    )

    assert result["ok"] is False
    assert result["error_code"] == "SEND_FAILED"
    assert result["attempts"] == 3
    assert result["final_returncode"] == 2
    assert result["command_ok"] is False
    assert result["delivery_confirmed"] is False
    assert sleeps == [1.0, 3.0]
    assert [e["status"] for e in audit_events] == ["error", "error", "error"]
