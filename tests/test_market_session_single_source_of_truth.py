"""Regression: market session detection should honor the shared schedule contract."""

from __future__ import annotations

from datetime import datetime, timezone


def test_select_markets_to_run_hk_break_respected() -> None:
    from domain.domain import select_markets_to_run

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
    out = select_markets_to_run(t, cfg, 'auto')
    assert out == []


def test_select_markets_to_run_schedule_only_hk_timezone_resolves_to_hk() -> None:
    """When schedule_hk is absent but schedule uses Asia/Hong_Kong timezone,
    market selection must resolve to HK, not US (bug: silent false negative)."""
    from domain.domain import select_markets_to_run

    # Config with only `schedule` (no `schedule_hk`), HK timezone.
    cfg = {
        'schedule': {
            'enabled': True,
            'market_timezone': 'Asia/Hong_Kong',
            'market_open': '09:30',
            'market_close': '16:00',
            'monitor_off_hours': False,
            'beijing_timezone': 'Asia/Shanghai',
            'sparse_after_beijing': '02:00',
        },
    }

    # 13:00 HKT = 05:00 UTC (UTC+8). Inside HK market hours, no break.
    t_in_hours = datetime(2026, 4, 1, 5, 0, 0, tzinfo=timezone.utc)
    out = select_markets_to_run(t_in_hours, cfg, 'auto')
    assert out == ['HK'], f"Expected ['HK'], got {out}"

    # 08:00 HKT = 00:00 UTC. Before market open: should return [].
    t_before_open = datetime(2026, 4, 1, 0, 0, 0, tzinfo=timezone.utc)
    out2 = select_markets_to_run(t_before_open, cfg, 'auto')
    assert out2 == [], f"Expected [], got {out2}"
