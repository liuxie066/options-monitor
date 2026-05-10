from __future__ import annotations

import importlib
import subprocess

import pytest


@pytest.mark.parametrize(
    ("watchdog_cfg", "expected_retry_enabled", "expected_timeout_sec"),
    [
        (None, True, 60),
        ({"retry_enabled": False}, False, 35),
    ],
)
def test_watchdog_retry_defaults_to_enabled_but_allows_explicit_disable(
    fake_runlog_factory,
    tmp_path,
    watchdog_cfg,
    expected_retry_enabled,
    expected_timeout_sec,
) -> None:
    from src.application.multi_tick_watchdog import run_multi_tick_watchdog

    calls: list[dict] = []
    base_cfg = {} if watchdog_cfg is None else {"watchdog": watchdog_cfg}

    def _run_opend_watchdog(**kwargs):
        calls.append(kwargs)
        return subprocess.CompletedProcess(
            args=["opend_watchdog"],
            returncode=0,
            stdout='{"ok": true}',
            stderr="",
        )

    outcome = run_multi_tick_watchdog(
        base=tmp_path,
        base_cfg=base_cfg,
        accounts=[],
        no_send=True,
        vpy=tmp_path / ".venv" / "bin" / "python",
        runlog=fake_runlog_factory([]),
        safe_data_fn=lambda data: data,
        utc_now_fn=lambda: "2026-05-10T00:00:00Z",
        audit_fn=lambda *args, **kwargs: None,
        on_guard_failure=lambda *_args, **_kwargs: None,
        run_opend_watchdog=_run_opend_watchdog,
        parse_last_json_obj=lambda _text: {"ok": True},
        classify_failure=lambda **_kwargs: {},
        resolve_watchlist_config=lambda _cfg: [{"fetch": {"source": "futu", "host": "127.0.0.1", "port": 11111}}],
        is_futu_fetch_source=lambda _source: True,
        resolve_multi_tick_engine_entrypoint=lambda **_kwargs: {},
        build_opend_unhealthy_execution_plan=lambda **_kwargs: {},
        mark_opend_phone_verify_pending=lambda *_args, **_kwargs: None,
        send_opend_alert=lambda *_args, **_kwargs: None,
        state_repo=object(),
    )

    assert outcome.should_continue is True
    assert len(calls) == 1
    assert calls[0]["retry_enabled"] is expected_retry_enabled
    assert calls[0]["timeout_sec"] == expected_timeout_sec


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
    monkeypatch.setattr(mt.state_repo, "claim_idempotency_record", lambda *a, **k: {"claimed": True})
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
