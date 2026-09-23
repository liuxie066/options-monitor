from __future__ import annotations

import importlib
import subprocess
import sys
import time

import pytest


def _shared_watchdog_kwargs() -> dict:
    """Collaborators every run_multi_tick_watchdog call in this module stubs identically."""
    return {
        "accounts": [],
        "no_send": True,
        "safe_data_fn": lambda data: data,
        "audit_fn": lambda *args, **kwargs: None,
        "on_guard_failure": lambda *_args, **_kwargs: None,
        "parse_last_json_obj": lambda _text: {"ok": True},
        "classify_failure": lambda **_kwargs: {},
        "is_futu_fetch_source": lambda _source: True,
        "resolve_multi_tick_engine_entrypoint": lambda **_kwargs: {},
        "build_opend_unhealthy_execution_plan": lambda **_kwargs: {},
        "mark_opend_phone_verify_pending": lambda *_args, **_kwargs: None,
        "send_opend_alert": lambda *_args, **_kwargs: None,
        "send_opend_recovery_notice": lambda *_args, **_kwargs: None,
        "state_repo": object(),
    }


@pytest.mark.parametrize(
    (
        "watchdog_cfg",
        "allow_operational_side_effects",
        "expected_retry_enabled",
        "expected_timeout_sec",
        "expected_ensure",
    ),
    [
        (None, True, True, 60, True),
        ({"retry_enabled": False}, True, False, 35, True),
        (None, False, True, 60, False),
    ],
)
def test_watchdog_retry_defaults_to_enabled_but_allows_explicit_disable(
    fake_runlog_factory,
    tmp_path,
    watchdog_cfg,
    allow_operational_side_effects,
    expected_retry_enabled,
    expected_timeout_sec,
    expected_ensure,
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
        vpy=tmp_path / ".venv" / "bin" / "python",
        runlog=fake_runlog_factory([]),
        utc_now_fn=lambda: "2026-05-10T00:00:00Z",
        run_opend_watchdog=_run_opend_watchdog,
        resolve_watchlist_config=lambda _cfg: [{"fetch": {"source": "futu", "host": "127.0.0.1", "port": 11111}}],
        allow_operational_side_effects=allow_operational_side_effects,
        **_shared_watchdog_kwargs(),
    )

    assert outcome.should_continue is True
    assert len(calls) == 1
    assert calls[0]["retry_enabled"] is expected_retry_enabled
    assert calls[0]["timeout_sec"] == expected_timeout_sec
    assert calls[0]["ensure"] is expected_ensure


def test_watchdog_timeout_should_not_degrade_and_should_skip_pipeline(
    argv_scope,
    example_config_path,
    fake_runlog_factory,
    monkeypatch,
) -> None:
    mt = importlib.import_module("src.application.multi_account_tick")

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
            "om",
            "--config",
            str(example_config_path),
            "--accounts",
            "lx",
            "--market-config",
            "us",
            "--no-send",
        ]
    )
    rc = mt.main()

    assert rc == 2
    assert scheduler_called["value"] == 0
    assert any(e.get("step") == "watchdog" and e.get("status") == "error" for e in events)
    assert any(e.get("step") == "run_end" and e.get("status") == "error" for e in events)


def test_phone_verify_pending_fails_without_reprobe(
    argv_scope, example_config_path, fake_runlog_factory, monkeypatch,
) -> None:
    mt = importlib.import_module("src.application.multi_account_tick")
    events = []
    monkeypatch.setattr(mt, "RunLogger", lambda base: fake_runlog_factory(events))
    monkeypatch.setattr(mt, "is_opend_phone_verify_pending", lambda _base: True)
    monkeypatch.setattr(mt, "run_opend_watchdog", lambda **_kwargs: (_ for _ in ()).throw(
        AssertionError("pending login must not be reprobed")))
    monkeypatch.setattr(mt, "admit_project_run", lambda *_a, **_k: {"allowed": True})
    monkeypatch.setattr(mt.state_repo, "claim_idempotency_record", lambda *a, **k: {"claimed": True})
    monkeypatch.setattr(mt.state_repo, "append_audit_event", lambda *a, **k: None)
    argv_scope(["om", "--config", str(example_config_path), "--accounts", "lx",
                "--market-config", "us", "--no-send"])

    assert mt.main() == 2
    assert any(e.get("step") == "run_end" and e.get("status") == "error"
               and e.get("error_code") == "OPEND_NEEDS_PHONE_VERIFY" for e in events)


def test_watchdog_outer_exception_fails_closed_without_ok_event(
    fake_runlog_factory,
    tmp_path,
) -> None:
    from src.application.multi_tick_watchdog import run_multi_tick_watchdog

    events: list[dict] = []
    outcome = run_multi_tick_watchdog(
        base=tmp_path,
        base_cfg={},
        vpy=tmp_path / ".venv" / "bin" / "python",
        runlog=fake_runlog_factory(events),
        utc_now_fn=lambda: "2026-07-29T00:00:00Z",
        run_opend_watchdog=lambda **_kwargs: {"ok": True},
        resolve_watchlist_config=lambda _cfg: (_ for _ in ()).throw(
            ValueError("invalid watchlist")
        ),
        **_shared_watchdog_kwargs(),
    )

    assert outcome.should_continue is False
    assert outcome.return_code == 2
    assert any(
        event.get("step") == "watchdog" and event.get("status") == "error"
        for event in events
    )
    assert not any(
        event.get("step") == "watchdog" and event.get("status") == "ok"
        for event in events
    )


def test_login_invalid_is_classified_and_alerts_on_first_failure(fake_runlog_factory, tmp_path) -> None:
    from src.application.multi_tick_watchdog import run_multi_tick_watchdog

    alerts = []
    events = []
    outcome = run_multi_tick_watchdog(
        base=tmp_path,
        base_cfg={},
        accounts=[],
        no_send=False,
        vpy=tmp_path / "python",
        runlog=fake_runlog_factory(events),
        safe_data_fn=lambda data: data,
        utc_now_fn=lambda: "2026-09-23T05:00:00Z",
        audit_fn=lambda *args, **kwargs: None,
        on_guard_failure=lambda *_args: None,
        run_opend_watchdog=lambda **_kwargs: {
            "ok": False,
            "error_code": "OPEND_LOGIN_INVALID",
            "message": "OpenD 登录已失效，需人工重新登录",
        },
        parse_last_json_obj=lambda _text: {},
        classify_failure=lambda **_kwargs: {},
        resolve_watchlist_config=lambda _cfg: [{"fetch": {"source": "futu", "port": 11111}}],
        is_futu_fetch_source=lambda _source: True,
        resolve_multi_tick_engine_entrypoint=lambda **_kwargs: {},
        build_opend_unhealthy_execution_plan=lambda **_kwargs: {},
        mark_opend_phone_verify_pending=lambda *_args: None,
        send_opend_alert=lambda *_args, **kwargs: alerts.append(kwargs),
        send_opend_recovery_notice=lambda *_args, **_kwargs: None,
        state_repo=object(),
    )

    assert outcome.should_continue is False
    assert outcome.return_code == 2
    assert alerts[0]["error_code"] == "OPEND_LOGIN_INVALID"
    assert alerts[0]["skip_consecutive_gate"] is True
    assert any(e.get("step") == "run_end" and e.get("data", {}).get("alert_submitted") is False for e in events)


def test_phone_verify_stays_a_failure_and_records_account_reason(fake_runlog_factory, tmp_path) -> None:
    from domain.domain.engine.decision_engine import build_opend_unhealthy_execution_plan
    from src.application.multi_tick_watchdog import run_multi_tick_watchdog

    class State:
        writes = []

        @staticmethod
        def write_account_last_run(_base, account, payload):
            State.writes.append((account, payload))

    events = []
    pending = []
    outcome = run_multi_tick_watchdog(
        base=tmp_path,
        base_cfg={},
        accounts=["lx"],
        no_send=False,
        vpy=tmp_path / "python",
        runlog=fake_runlog_factory(events),
        safe_data_fn=lambda data: data,
        utc_now_fn=lambda: "2026-09-23T05:00:00Z",
        audit_fn=lambda *args, **kwargs: None,
        on_guard_failure=lambda *_args: None,
        run_opend_watchdog=lambda **_kwargs: {
            "ok": False, "error_code": "OPEND_NEEDS_PHONE_VERIFY", "message": "需要手机验证码",
        },
        parse_last_json_obj=lambda _text: {},
        classify_failure=lambda **_kwargs: {},
        resolve_watchlist_config=lambda _cfg: [{"fetch": {"source": "futu", "port": 11111}}],
        is_futu_fetch_source=lambda _source: True,
        resolve_multi_tick_engine_entrypoint=lambda **_kwargs: {},
        build_opend_unhealthy_execution_plan=build_opend_unhealthy_execution_plan,
        mark_opend_phone_verify_pending=lambda *_args, **_kwargs: pending.append(True),
        send_opend_alert=lambda *_args, **_kwargs: True,
        send_opend_recovery_notice=lambda *_args, **_kwargs: None,
        state_repo=State,
    )

    assert outcome.return_code == 2
    assert pending == [True]
    assert State.writes[0][1]["error_code"] == "OPEND_NEEDS_PHONE_VERIFY"
    assert any(e.get("step") == "run_end" and e.get("status") == "error" for e in events)


def test_watchdog_probe_hard_timeout_and_login_classification(monkeypatch, tmp_path) -> None:
    from src.infrastructure import external_services
    from src.infrastructure.opend_watchdog import classify_watchdog_result

    calls = []

    def timed_out(command, **kwargs):
        calls.append((command, kwargs))
        raise subprocess.TimeoutExpired(command, kwargs["timeout_sec"],
            output="init connect fail: msg=登录密码被修改,已退出登录".encode())

    monkeypatch.setattr(external_services, "run_command", timed_out)
    result = external_services.run_opend_watchdog(
        vpy=tmp_path / "python", base=tmp_path, host="127.0.0.1", port=11111,
        timeout_sec=4, required_capability="quote",
    )
    assert result == {"ok": False, "error_code": "OPEND_LOGIN_INVALID",
        "message": "OpenD 登录已失效，需人工重新登录"}
    assert calls[0][1]["timeout_sec"] == 4
    assert calls[0][0][-1] == "--ensure"
    assert calls[0][0][calls[0][0].index("--required-capability") + 1] == "quote"
    assert classify_watchdog_result(None, "登录密码被修改,已退出登录")[0] == "OPEND_LOGIN_INVALID"


def test_watchdog_probe_kills_stuck_sdk_process(tmp_path) -> None:
    from src.infrastructure.external_services import run_opend_watchdog

    fake_python = tmp_path / "stuck-sdk"
    fake_python.write_text(
        f"#!{sys.executable}\nimport time\nprint('init connect fail: msg=登录密码被修改,已退出登录', flush=True)\ntime.sleep(30)\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o700)
    started = time.monotonic()
    result = run_opend_watchdog(
        vpy=fake_python, base=tmp_path, host="127.0.0.1", port=11111,
        timeout_sec=1, required_capability="quote",
    )
    assert time.monotonic() - started < 4
    assert result["error_code"] == "OPEND_LOGIN_INVALID"
