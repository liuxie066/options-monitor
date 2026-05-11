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


def test_select_markets_to_run_schedule_only_us_timezone_resolves_to_us() -> None:
    from domain.domain import select_markets_to_run

    cfg = {
        'schedule': {
            'enabled': True,
            'market_timezone': 'America/New_York',
            'market_open': '09:30',
            'market_close': '16:00',
        },
    }

    t_in_hours = datetime(2026, 4, 1, 14, 0, 0, tzinfo=timezone.utc)
    out = select_markets_to_run(t_in_hours, cfg, 'auto')
    assert out == ['US']


def test_select_markets_to_run_prefers_schedule_hk_when_both_markets_are_open() -> None:
    from domain.domain import select_markets_to_run

    cfg = {
        'schedule_hk': {
            'enabled': True,
            'market_timezone': 'Asia/Hong_Kong',
            'market_open': '00:00',
            'market_close': '23:59',
        },
        'schedule': {
            'enabled': True,
            'market_timezone': 'America/New_York',
            'market_open': '00:00',
            'market_close': '23:59',
        },
    }

    t_both_open = datetime(2026, 4, 1, 14, 0, 0, tzinfo=timezone.utc)
    out = select_markets_to_run(t_both_open, cfg, 'auto')
    assert out == ['HK']


def test_evaluate_auto_market_rules_makes_hk_us_resolution_explicit() -> None:
    from domain.domain import multi_tick as mod

    cfg = {
        'schedule': {
            'enabled': True,
            'market_timezone': 'Asia/Hong_Kong',
            'market_open': '09:30',
            'market_close': '16:00',
        },
    }

    t_in_hours = datetime(2026, 4, 1, 5, 0, 0, tzinfo=timezone.utc)
    rules = mod._evaluate_auto_market_rules(t_in_hours, cfg)

    assert [rule.schedule_key for rule in rules] == ['schedule_hk', 'schedule']
    assert rules[0].configured is False
    assert rules[0].in_market_hours is False
    assert rules[0].resolved_market is None

    assert rules[1].configured is True
    assert rules[1].in_market_hours is True
    assert rules[1].inferred_market_from_timezone == 'HK'
    assert rules[1].resolved_market == 'HK'
