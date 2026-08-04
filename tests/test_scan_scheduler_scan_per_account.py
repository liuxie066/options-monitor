"""Regression: scan_scheduler scan clock should be per-account in multi-account mode."""

from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta

import pytest


def test_scan_scheduler_scan_is_per_account() -> None:
    from src.application.scan_scheduler import decide

    schedule_cfg = {
        'enabled': True,
        'timezone': 'Asia/Hong_Kong',
        'cron_interval_min': 10,
        'run_window': {
            'start': '09:30',
            'end': '16:00',
            'breaks': [],
        },
        'run_points': {
            'start_plus_min': 10,
            'hourly_minute': 0,
            'end_minus_min': 10,
        },
        'beijing_timezone': 'Asia/Shanghai',
    }

    t0 = datetime(2026, 4, 1, 1, 40, 0, tzinfo=timezone.utc)  # 09:40 HKT target
    t1 = t0 + timedelta(minutes=10)

    state = {
        'last_run_utc_by_account': {
            'lx': t0.isoformat(),
        },
        'last_notify_utc': None,
        'last_notify_utc_by_account': {},
    }

    d_lx = decide(schedule_cfg, state, t1, account='lx', schedule_key='schedule_hk')
    d_sy = decide(schedule_cfg, state, t1, account='sy', schedule_key='schedule_hk')

    assert d_lx.should_run_scan is False
    assert d_sy.should_run_scan is True


def test_scan_scheduler_reads_legacy_per_account_scan_state() -> None:
    from src.application.scan_scheduler import decide

    schedule_cfg = {
        'enabled': True,
        'timezone': 'Asia/Hong_Kong',
        'cron_interval_min': 10,
        'run_window': {
            'start': '09:30',
            'end': '16:00',
            'breaks': [],
        },
        'run_points': {
            'start_plus_min': 10,
            'hourly_minute': 0,
            'end_minus_min': 10,
        },
        'beijing_timezone': 'Asia/Shanghai',
    }

    t0 = datetime(2026, 4, 1, 1, 40, 0, tzinfo=timezone.utc)  # 09:40 HKT target
    t1 = t0 + timedelta(minutes=10)
    state = {
        'last_scan_utc': t0.isoformat(),
        'last_scan_utc_by_account': {
            'lx': t0.isoformat(),
        },
        'last_notify_utc': None,
        'last_notify_utc_by_account': {},
    }

    d_lx = decide(schedule_cfg, state, t1, account='lx', schedule_key='schedule_hk')
    d_sy = decide(schedule_cfg, state, t1, account='sy', schedule_key='schedule_hk')

    assert d_lx.should_run_scan is False
    assert d_sy.should_run_scan is True


def test_scheduler_decision_payload_uses_account_scan_clock(tmp_path) -> None:
    from src.application.scan_scheduler import build_scheduler_decision_payload

    schedule_cfg = {
        'enabled': True,
        'timezone': 'Asia/Hong_Kong',
        'cron_interval_min': 10,
        'run_window': {
            'start': '09:30',
            'end': '16:00',
            'breaks': [],
        },
        'run_points': {
            'start_plus_min': 10,
            'hourly_minute': 0,
            'end_minus_min': 10,
        },
        'beijing_timezone': 'Asia/Shanghai',
    }
    t0 = datetime(2026, 4, 1, 1, 40, 0, tzinfo=timezone.utc)
    t1 = t0 + timedelta(minutes=10)
    config = tmp_path / 'config.us.json'
    state = tmp_path / 'scheduler_state.json'
    config.write_text(json.dumps({'schedule': schedule_cfg}), encoding='utf-8')
    state.write_text(
        json.dumps(
            {
                'last_run_utc_by_account': {'lx': t0.isoformat()},
                'last_notify_utc': None,
                'last_notify_utc_by_account': {},
            }
        ),
        encoding='utf-8',
    )

    lx = build_scheduler_decision_payload(
        config=config,
        state=state,
        schedule_key='schedule',
        account='lx',
        base_dir=tmp_path,
        now_utc=t1,
    )
    sy = build_scheduler_decision_payload(
        config=config,
        state=state,
        schedule_key='schedule',
        account='sy',
        base_dir=tmp_path,
        now_utc=t1,
    )

    assert lx['should_run_scan'] is False
    assert lx['should_notify'] is False
    assert sy['should_run_scan'] is True
    assert sy['should_notify'] is True


def test_mark_scheduler_accounts_batches_scan_state(tmp_path) -> None:
    from src.application.scan_scheduler import mark_scheduler_accounts

    t0 = datetime(2026, 4, 1, 2, 0, 0, tzinfo=timezone.utc)
    config = tmp_path / 'config.us.json'
    state = tmp_path / 'scheduler_state.json'
    config.write_text(json.dumps({'schedule': {'enabled': True}}), encoding='utf-8')

    no_op = mark_scheduler_accounts(
        config=config,
        state=state,
        schedule_key='schedule',
        accounts=[],
        mark_scanned=True,
        base_dir=tmp_path,
        now_utc=t0,
    )

    assert no_op['updated'] is False
    assert not state.exists()

    out = mark_scheduler_accounts(
        config=config,
        state=state,
        schedule_key='schedule',
        accounts=['lx', ' ', 'sy'],
        mark_scanned=True,
        base_dir=tmp_path,
        now_utc=t0,
    )

    data = json.loads(state.read_text(encoding='utf-8'))
    assert out['updated'] is True
    assert out['accounts'] == ['lx', 'sy']
    assert data['last_run_utc_by_account'] == {
        'lx': t0.isoformat(),
        'sy': t0.isoformat(),
    }


@pytest.mark.parametrize(
    "extra_args",
    [
        {},
        {"account": "lx", "mark_notified": True},
        {"account": "sy", "mark_scanned": True},
        {"account": "lx", "mark_notified": True, "mark_scanned": True, "force": True, "jsonl": True},
    ],
)
def test_run_scheduler_rejects_run_if_due_before_runtime_or_state_access(
    monkeypatch,
    tmp_path,
    extra_args,
) -> None:
    from src.application import scan_scheduler
    from src.application.agent_tool_contracts import AgentToolError

    calls: list[str] = []

    def _unexpected_call(name: str):
        def _fail(*args, **kwargs):
            calls.append(name)
            raise AssertionError(f"unexpected call: {name}")

        return _fail

    for owner in ("_resolve_base", "_resolve_state_path", "read_state", "write_state"):
        monkeypatch.setattr(scan_scheduler, owner, _unexpected_call(owner))

    with pytest.raises(AgentToolError) as exc_info:
        scan_scheduler.run_scheduler(
            config=tmp_path / "missing.json",
            state=tmp_path / "scheduler_state.json",
            run_if_due=True,
            **extra_args,
        )

    assert exc_info.value.code == "UNSUPPORTED_OPERATION"
    assert "tick" in str(exc_info.value.hint)
    assert calls == []
    assert not (tmp_path / "scheduler_state.json").exists()
