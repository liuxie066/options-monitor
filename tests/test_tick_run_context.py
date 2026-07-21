from __future__ import annotations

from datetime import datetime, timezone


def test_tick_idempotency_context_normalizes_inputs_and_sorts_accounts(tmp_path) -> None:
    from src.application.tick_run_context import build_tick_idempotency_context

    cfg = tmp_path / "config.us.json"
    cfg.write_text("{}", encoding="utf-8")
    now = datetime(2026, 5, 13, 1, 2, 3, tzinfo=timezone.utc)

    first = build_tick_idempotency_context(
        cfg_path=cfg,
        market_config=" US ",
        accounts=["SY", "lx"],
        trigger_kind=" Scheduled ",
        now_utc=now,
    )
    second = build_tick_idempotency_context(
        cfg_path=cfg,
        market_config="us",
        accounts=["lx", "sy"],
        trigger_kind="scheduled",
        now_utc=now,
    )

    assert first.bucket == "20260513T0102"
    assert first.market_config == "us"
    assert first.accounts == ["sy", "lx"]
    assert first.trigger_kind == "scheduled"
    assert first.key == second.key

    manual = build_tick_idempotency_context(
        cfg_path=cfg,
        market_config="us",
        accounts=["lx", "sy"],
        trigger_kind="manual",
        now_utc=now,
    )
    force = build_tick_idempotency_context(
        cfg_path=cfg,
        market_config="us",
        accounts=["lx", "sy"],
        trigger_kind="force",
        now_utc=now,
    )
    assert len({first.key, manual.key, force.key}) == 3


def test_complete_tick_idempotency_writes_tick_execution_record(tmp_path) -> None:
    from src.application.tick_run_context import complete_tick_idempotency

    calls: list[dict] = []

    def write_record(base, *, scope, key, payload):
        calls.append({"base": base, "scope": scope, "key": key, "payload": payload})

    complete_tick_idempotency(
        base=tmp_path,
        key="key-1",
        run_id="run-1",
        market_config="us",
        accounts=["lx"],
        trigger_kind="scheduled",
        status="skipped",
        message="quiet_hours",
        write_record_fn=write_record,
    )

    assert len(calls) == 1
    assert calls[0]["base"] == tmp_path
    assert calls[0]["scope"] == "tick_execution"
    assert calls[0]["key"] == "key-1"
    assert calls[0]["payload"]["finished_at_utc"]
    payload = dict(calls[0]["payload"])
    payload.pop("finished_at_utc")
    assert payload == {
        "ok": True,
        "status": "skipped",
        "run_id": "run-1",
        "market_config": "us",
        "accounts": ["lx"],
        "trigger_kind": "scheduled",
        "message": "quiet_hours",
    }


def test_missing_tick_trigger_defaults_to_manual_fail_safe(tmp_path) -> None:
    from src.application.tick_run_context import build_tick_idempotency_context

    context = build_tick_idempotency_context(
        cfg_path=tmp_path / "config.us.json",
        market_config="us",
        accounts=["lx"],
    )

    assert context.trigger_kind == "manual"


def test_complete_tick_idempotency_records_terminal_failure(tmp_path) -> None:
    from src.application.tick_run_context import complete_tick_idempotency

    calls: list[dict] = []
    complete_tick_idempotency(
        base=tmp_path,
        key="key-failed",
        run_id="run-failed",
        market_config="all",
        accounts=["lx"],
        trigger_kind="scheduled",
        status="unsupported_failed",
        message="daily_brief_multi_market_delivery_unsupported",
        ok=False,
        error_code="daily_brief_multi_market_delivery_unsupported",
        write_record_fn=lambda base, *, scope, key, payload: calls.append(payload),
    )

    payload = calls[0]
    assert payload["ok"] is False
    assert payload["status"] == "unsupported_failed"
    assert payload["trigger_kind"] == "scheduled"
    assert payload["error_code"] == "daily_brief_multi_market_delivery_unsupported"
