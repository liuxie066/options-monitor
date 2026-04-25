from __future__ import annotations

import importlib
from pathlib import Path
from types import SimpleNamespace


def test_finalize_no_account_notification_records_degraded_writes_and_still_succeeds(
    fake_runlog_factory,
) -> None:
    mod = importlib.import_module("src.application.multi_tick_finalization")
    events: list[dict] = []
    audit_calls: list[tuple[tuple, dict]] = []
    success = {"called": 0}
    tick_metrics = {"run_dir": "/tmp/run"}

    state_repo = SimpleNamespace(
        write_shared_last_run=lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("shared boom")),
        write_account_last_run=lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("account boom")),
        write_run_account_last_run=lambda *_args, **_kwargs: None,
        write_tick_metrics=lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("metrics boom")),
        append_tick_metrics_history=lambda *_args, **_kwargs: None,
    )

    mod.build_no_account_notification_payloads = lambda **_kwargs: (
        {"shared": True},
        {"lx": {"last_run_utc": "2026-01-01T00:00:00Z", "sent": False}},
    )
    mod.build_run_end_payload = lambda **kwargs: {"reason": kwargs.get("reason"), "sent_accounts": kwargs.get("sent_accounts")}

    rc = mod.finalize_no_account_notification(
        base=Path("/tmp/base"),
        run_id="run-1",
        runlog=fake_runlog_factory(events),
        results=[SimpleNamespace(account="lx")],
        tick_metrics=tick_metrics,
        no_send=True,
        state_repo=state_repo,
        utc_now_fn=lambda: "2026-01-01T00:00:00Z",
        audit_fn=lambda *args, **kwargs: audit_calls.append((args, kwargs)),
        safe_data_fn=lambda payload: payload,
        on_success=lambda: success.__setitem__("called", success["called"] + 1),
    )

    assert rc == 0
    assert success["called"] == 1
    assert tick_metrics["sent"] is False
    assert tick_metrics["reason"] == "no_account_notification"
    degraded_actions = [e.get("data", {}).get("action") for e in events if e.get("step") == "finalize" and e.get("status") == "degraded"]
    assert degraded_actions == ["write_shared_last_run", "write_account_last_run", "write_tick_metrics"]
    assert any(e.get("step") == "run_end" and e.get("status") == "ok" for e in events)
    assert [kwargs.get("status") for _args, kwargs in audit_calls[-3:]] == ["error", "error", "error"]


def test_finalize_multi_tick_run_logs_degraded_shared_write_and_returns_partial_failure(
    fake_runlog_factory,
    tmp_path,
) -> None:
    mod = importlib.import_module("src.application.multi_tick_finalization")
    events: list[dict] = []
    audit_calls: list[tuple[tuple, dict]] = []
    success = {"called": 0}

    state_repo = SimpleNamespace(
        write_shared_last_run=lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("shared write failed")),
    )

    mod.build_shared_last_run_meta = lambda **kwargs: {"sent_accounts": kwargs.get("sent_accounts"), "notify_summary": kwargs.get("notify_summary")}
    mod.build_shared_last_run_payload = lambda **kwargs: {"prev": kwargs.get("prev_payload"), "meta": kwargs.get("run_meta")}
    mod.build_run_end_payload = lambda **kwargs: {"notify_failures": kwargs.get("notify_failures"), "sent_accounts": kwargs.get("sent_accounts")}

    rc = mod.finalize_multi_tick_run(
        base=tmp_path,
        run_id="run-2",
        runlog=fake_runlog_factory(events),
        results=[SimpleNamespace(account="lx")],
        tick_metrics={"run_dir": str(tmp_path / "run")},
        no_send=False,
        sent_accounts=["lx"],
        notify_failures=[{"account": "sy", "error_code": "SEND_FAILED"}],
        notify_summary={"sent": 1, "failed": 1},
        channel="feishu",
        target="group://test",
        state_repo=state_repo,
        read_json_fn=lambda *_args, **_kwargs: {"history": []},
        shared_state_dir_getter=lambda _base: tmp_path,
        utc_now_fn=lambda: "2026-01-01T00:00:00Z",
        audit_fn=lambda *args, **kwargs: audit_calls.append((args, kwargs)),
        safe_data_fn=lambda payload: payload,
        on_success=lambda: success.__setitem__("called", success["called"] + 1),
    )

    assert rc == 1
    assert success["called"] == 0
    assert any(
        e.get("step") == "finalize"
        and e.get("status") == "degraded"
        and e.get("data", {}).get("action") == "write_shared_last_run"
        for e in events
    )
    assert any(e.get("step") == "run_end" and e.get("status") == "error" and e.get("error_code") == "NOTIFY_PARTIAL_FAILED" for e in events)
    assert any(kwargs.get("status") == "error" and kwargs.get("extra") == {"sent_accounts": ["lx"]} for _args, kwargs in audit_calls)


def test_finalize_multi_tick_run_success_calls_on_success(fake_runlog_factory, tmp_path) -> None:
    mod = importlib.import_module("src.application.multi_tick_finalization")
    events: list[dict] = []
    shared_payloads: list[dict] = []
    success = {"called": 0}

    state_repo = SimpleNamespace(
        write_shared_last_run=lambda _base, payload: shared_payloads.append(payload),
    )

    mod.build_shared_last_run_meta = lambda **kwargs: {"sent_accounts": kwargs.get("sent_accounts")}
    mod.build_shared_last_run_payload = lambda **kwargs: {"prev": kwargs.get("prev_payload"), "meta": kwargs.get("run_meta")}
    mod.build_run_end_payload = lambda **kwargs: {"sent_accounts": kwargs.get("sent_accounts"), "notify_summary": kwargs.get("notify_summary")}

    rc = mod.finalize_multi_tick_run(
        base=tmp_path,
        run_id="run-3",
        runlog=fake_runlog_factory(events),
        results=[SimpleNamespace(account="lx")],
        tick_metrics={"run_dir": str(tmp_path / "run")},
        no_send=False,
        sent_accounts=["lx"],
        notify_failures=[],
        notify_summary={"sent": 1, "failed": 0},
        channel="feishu",
        target="group://test",
        state_repo=state_repo,
        read_json_fn=lambda *_args, **_kwargs: {"history": [1]},
        shared_state_dir_getter=lambda _base: tmp_path,
        utc_now_fn=lambda: "2026-01-01T00:00:00Z",
        audit_fn=lambda *_args, **_kwargs: None,
        safe_data_fn=lambda payload: payload,
        on_success=lambda: success.__setitem__("called", success["called"] + 1),
    )

    assert rc == 0
    assert success["called"] == 1
    assert shared_payloads == [{"prev": {"history": [1]}, "meta": {"sent_accounts": ["lx"]}}]
    assert any(e.get("step") == "run_end" and e.get("status") == "ok" for e in events)
