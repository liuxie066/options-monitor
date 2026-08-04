from __future__ import annotations

from pathlib import Path


def test_futu_doctor_runtime_returns_structured_payload(monkeypatch) -> None:
    import src.application.futu_doctor as doctor

    class _Health:
        def to_payload(self) -> dict:
            return {"ok": True, "message": "OpenD 健康"}

    monkeypatch.setattr(doctor, "sdk_status", lambda: {"ok": True, "futu_sdk_importable": True})
    monkeypatch.setattr(doctor, "run_watchdog_check", lambda **_kwargs: _Health())
    monkeypatch.setattr(doctor, "port_open", lambda host, port, timeout=0.8: host == "127.0.0.1" and port == 22222)
    monkeypatch.setattr(
        doctor,
        "check_required_option_fields",
        lambda **_kwargs: {"results": [{"symbol": "NVDA", "ok": True}]},
    )

    payload = doctor.run_futu_doctor_checks(host="127.0.0.1", port=11111, symbols=["NVDA"])

    assert payload["ok"] is True
    assert payload["watchdog_ok"] is True
    assert payload["required_fields_ok"] is True
    assert payload["required_fields"]["results"][0]["symbol"] == "NVDA"
    assert payload["telnet"]["ok"] is True


def test_futu_doctor_skips_field_probe_when_sdk_missing(monkeypatch) -> None:
    import src.application.futu_doctor as doctor

    class _Health:
        def to_payload(self) -> dict:
            return {"ok": True, "message": "OpenD 健康"}

    monkeypatch.setattr(doctor, "sdk_status", lambda: {"ok": False, "futu_sdk_importable": False})
    monkeypatch.setattr(doctor, "run_watchdog_check", lambda **_kwargs: _Health())
    monkeypatch.setattr(doctor, "port_open", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        doctor,
        "check_required_option_fields",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("field probe should not run without futu sdk")),
    )

    payload = doctor.run_futu_doctor_checks(host="127.0.0.1", port=11111, symbols=["NVDA"])

    assert payload["ok"] is False
    assert payload["watchdog_ok"] is True
    assert payload["required_fields_ok"] is False
    assert payload["required_fields"] is None
    assert payload["telnet"]["ok"] is False


def test_opend_watchdog_requires_trade_login() -> None:
    from src.infrastructure.opend_watchdog import classify_watchdog_result

    code, message = classify_watchdog_result(
        {"program_status_type": "READY", "qot_logined": True, "trd_logined": False},
        None,
    )

    assert code == "OPEND_TRD_NOT_LOGINED"
    assert "交易未登录" in message


def test_quote_watchdog_ignores_unrequested_trade_login(monkeypatch) -> None:
    import src.infrastructure.opend_watchdog as watchdog

    monkeypatch.setattr(watchdog, "port_open", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        watchdog,
        "get_global_state",
        lambda *_args, **_kwargs: (
            {
                "program_status_type": "READY",
                "qot_logined": True,
                "trd_logined": False,
            },
            None,
            None,
        ),
    )

    health = watchdog.run_watchdog_check(required_capability="quote")

    assert health.ok is True
    assert health.error is None


def test_multi_tick_watchdog_accepts_structured_watchdog_payload(fake_runlog_factory, tmp_path: Path) -> None:
    from src.application.multi_tick_watchdog import run_multi_tick_watchdog

    calls: list[dict] = []

    def _run_opend_watchdog(**kwargs):  # type: ignore[no-untyped-def]
        calls.append(kwargs)
        return {"ok": True, "message": "OpenD 健康"}

    outcome = run_multi_tick_watchdog(
        base=tmp_path,
        base_cfg={"watchdog": {"retry_enabled": False}},
        accounts=[],
        no_send=True,
        vpy=tmp_path / ".venv" / "bin" / "python",
        runlog=fake_runlog_factory([]),
        safe_data_fn=lambda data: data,
        utc_now_fn=lambda: "2026-05-10T00:00:00Z",
        audit_fn=lambda *args, **kwargs: None,
        on_guard_failure=lambda *_args, **_kwargs: None,
        run_opend_watchdog=_run_opend_watchdog,
        parse_last_json_obj=lambda _text: (_ for _ in ()).throw(AssertionError("json stdout parser should not run")),
        classify_failure=lambda **_kwargs: {},
        resolve_watchlist_config=lambda _cfg: [{"fetch": {"source": "futu", "host": "127.0.0.1", "port": 11111}}],
        is_futu_fetch_source=lambda _source: True,
        resolve_multi_tick_engine_entrypoint=lambda **_kwargs: {},
        build_opend_unhealthy_execution_plan=lambda **_kwargs: {},
        mark_opend_phone_verify_pending=lambda *_args, **_kwargs: None,
        send_opend_alert=lambda *_args, **_kwargs: None,
        send_opend_recovery_notice=lambda *_args, **_kwargs: None,
        state_repo=object(),
    )

    assert outcome.should_continue is True
    assert len(calls) == 1
    assert calls[0]["required_capability"] == "quote"
