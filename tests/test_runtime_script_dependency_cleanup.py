from __future__ import annotations

from pathlib import Path


def test_cash_summary_footer_appends_without_script_subprocess(tmp_path: Path, monkeypatch) -> None:
    import src.application.cash_summary_footer as footer

    config_path = tmp_path / "config.json"
    data_config = tmp_path / "portfolio.json"
    notification = tmp_path / "symbols_notification.txt"
    config_path.write_text('{"accounts":["lx","sy"]}', encoding="utf-8")
    data_config.write_text("{}", encoding="utf-8")
    notification.write_text("主体内容\n\n现金（CNY）:\nOLD: holding ¥1 | free ¥1\n", encoding="utf-8")

    def _fake_query(**kwargs):  # type: ignore[no-untyped-def]
        account = kwargs["account"]
        return {
            "cash_available_cny": 1000 if account == "lx" else 2000,
            "cash_free_cny": 800 if account == "lx" else 1500,
        }

    monkeypatch.setattr(footer, "query_sell_put_cash", _fake_query)

    footer.append_cash_summary_footer(
        base=tmp_path,
        notification=notification,
        config=config_path,
        data_config=data_config,
        market="富途",
    )

    text = notification.read_text(encoding="utf-8")
    assert "主体内容" in text
    assert "OLD:" not in text
    assert "LX: holding" in text
    assert "SY: holding" in text


def test_futu_doctor_runtime_returns_structured_payload(monkeypatch) -> None:
    import src.application.futu_doctor as doctor

    class _Health:
        def to_payload(self) -> dict:
            return {"ok": True, "message": "OpenD 健康"}

    monkeypatch.setattr(doctor, "sdk_status", lambda: {"ok": True, "futu_sdk_importable": True})
    monkeypatch.setattr(doctor, "run_watchdog_check", lambda **_kwargs: _Health())
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


def test_futu_doctor_skips_field_probe_when_sdk_missing(monkeypatch) -> None:
    import src.application.futu_doctor as doctor

    class _Health:
        def to_payload(self) -> dict:
            return {"ok": True, "message": "OpenD 健康"}

    monkeypatch.setattr(doctor, "sdk_status", lambda: {"ok": False, "futu_sdk_importable": False})
    monkeypatch.setattr(doctor, "run_watchdog_check", lambda **_kwargs: _Health())
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
