"""Regression: scan_scheduler scan clock should be per-account in multi-account mode."""

from __future__ import annotations

from datetime import datetime, timezone, timedelta


def test_scan_scheduler_scan_is_per_account() -> None:
    from scripts.scan_scheduler import decide

    schedule_cfg = {
        'enabled': True,
        'market_timezone': 'Asia/Hong_Kong',
        'market_open': '09:30',
        'market_close': '16:00',
        'monitor_off_hours': False,
        'first_notify_after_open_min': 30,
        'notify_interval_min': 60,
        'final_notify_before_close_min': 10,
        'beijing_timezone': 'Asia/Shanghai',
    }

    t0 = datetime(2026, 4, 1, 2, 0, 0, tzinfo=timezone.utc)  # 10:00 HKT target
    t1 = t0 + timedelta(minutes=10)

    state = {
        'last_scan_utc': None,
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
