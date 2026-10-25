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


def test_failed_tick_record_retains_account_outcomes_for_replay(tmp_path):
    from src.application.tick_run_context import complete_tick_idempotency, tick_account_outcomes
    outcomes = tick_account_outcomes({"scan_outcomes": {"lx": True, "sy": False}, "scan_failure_notified_accounts": ["sy"]})
    saved = []
    complete_tick_idempotency(base=tmp_path, key="key", run_id="child", market_config="us", accounts=["lx", "sy"],
        status="failed", ok=False, error_code="SCAN_FAILED", account_outcomes=outcomes,
        write_record_fn=lambda *args, **kwargs: saved.append(kwargs["payload"]))
    assert saved[0]["account_outcomes"] == outcomes
    assert outcomes[1] == {"account": "sy", "scan_ok": False, "notify_failed": False, "failure_notification_confirmed": True}


def test_complete_tick_real_owner_rejects_old_run_and_preserves_successor(tmp_path):
    import json
    import pytest
    from domain.storage.repositories import state_repo
    from src.application.tick_run_context import complete_tick_idempotency

    state_repo.claim_idempotency_record(tmp_path, scope='tick_execution', key='same-key',
                                       payload={'run_id': 'old', 'pid': 99999999})
    successor = state_repo.claim_idempotency_record(tmp_path, scope='tick_execution', key='same-key',
                                                   payload={'run_id': 'successor'})
    before = successor['path'].read_bytes()
    with pytest.raises(RuntimeError, match='ownership'):
        complete_tick_idempotency(base=tmp_path, key='same-key', run_id='old', market_config='us', accounts=['lx'])
    assert successor['path'].read_bytes() == before
    complete_tick_idempotency(base=tmp_path, key='same-key', run_id='successor', market_config='us', accounts=['lx'])
    saved = json.loads(successor['path'].read_text())
    assert saved['run_id'] == 'successor' and saved['status'] == 'completed' and saved['ok']
