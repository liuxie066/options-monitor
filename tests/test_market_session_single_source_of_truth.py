"""Regression: market session detection should rely on scan_scheduler (single source of truth)."""

from __future__ import annotations

from datetime import datetime, timezone


def test_select_markets_to_run_hk_break_respected() -> None:
    from scripts.send_if_needed_multi import _select_markets_to_run

    cfg = {
        'schedule_hk': {
            'enabled': True,
            'market_timezone': 'Asia/Hong_Kong',
            'market_open': '09:30',
            'market_close': '16:00',
            'monitor_off_hours': False,
            'market_dense_interval_min': 30,
            'market_sparse_interval_min': 30,
            'notify_cooldown_min': 60,
            'market_break_start': '12:00',
            'market_break_end': '13:00',
            'beijing_timezone': 'Asia/Shanghai',
            'sparse_after_beijing': '02:00',
        },
        'schedule': {
            'enabled': True,
            'market_timezone': 'America/New_York',
            'market_open': '09:30',
            'market_close': '16:00',
            'monitor_off_hours': False,
            'beijing_timezone': 'Asia/Shanghai',
            'sparse_after_beijing': '02:00',
        },
    }

    # 12:30 HKT => lunch break => should NOT select HK.
    t = datetime(2026, 4, 1, 4, 30, 0, tzinfo=timezone.utc)
    out = _select_markets_to_run(t, cfg, 'auto')
    assert out == []
