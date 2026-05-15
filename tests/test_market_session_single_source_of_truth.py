"""Regression: market session detection should honor the shared schedule contract."""

from __future__ import annotations

from datetime import datetime, timezone


def test_select_markets_to_run_hk_break_respected() -> None:
    from domain.domain import select_markets_to_run

    cfg = {
        'schedule_hk': {
            'enabled': True,
            'timezone': 'Asia/Hong_Kong',
            'run_window': {
                'start': '09:30',
                'end': '16:00',
                'breaks': [
                    {'start': '12:00', 'end': '13:00'},
                ],
            },
            'beijing_timezone': 'Asia/Shanghai',
        },
        'schedule': {
            'enabled': True,
            'timezone': 'America/New_York',
            'run_window': {
                'start': '09:30',
                'end': '16:00',
                'breaks': [],
            },
            'beijing_timezone': 'Asia/Shanghai',
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
            'timezone': 'Asia/Hong_Kong',
            'run_window': {
                'start': '09:30',
                'end': '16:00',
                'breaks': [],
            },
            'beijing_timezone': 'Asia/Shanghai',
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
            'timezone': 'America/New_York',
            'run_window': {
                'start': '09:30',
                'end': '16:00',
                'breaks': [],
            },
        },
    }

    t_in_hours = datetime(2026, 4, 1, 14, 0, 0, tzinfo=timezone.utc)
    out = select_markets_to_run(t_in_hours, cfg, 'auto')
    assert out == ['US']


def test_select_markets_to_run_us_beijing_gate_blocks_auto_after_cutoff() -> None:
    from domain.domain import select_markets_to_run

    cfg = {
        'schedule': {
            'enabled': True,
            'timezone': 'America/New_York',
            'run_window': {
                'start': '09:30',
                'end': '16:00',
                'breaks': [],
            },
            'gates': [
                {
                    'type': 'before',
                    'timezone': 'Asia/Shanghai',
                    'time': '02:00',
                    'day_offset_from_window_start': 1,
                },
            ],
        },
    }

    # Summer time: 13:00 EDT is 01:00 Beijing next day, still allowed.
    assert select_markets_to_run(
        datetime(2026, 7, 1, 17, 0, 0, tzinfo=timezone.utc),
        cfg,
        'auto',
    ) == ['US']
    # 14:00 EDT is exactly 02:00 Beijing next day, so the before-gate closes.
    assert select_markets_to_run(
        datetime(2026, 7, 1, 18, 0, 0, tzinfo=timezone.utc),
        cfg,
        'auto',
    ) == []

    # Winter time follows the same Beijing cutoff without a separate config.
    assert select_markets_to_run(
        datetime(2026, 1, 5, 17, 0, 0, tzinfo=timezone.utc),
        cfg,
        'auto',
    ) == ['US']
    assert select_markets_to_run(
        datetime(2026, 1, 5, 18, 0, 0, tzinfo=timezone.utc),
        cfg,
        'auto',
    ) == []


def test_select_markets_to_run_prefers_schedule_hk_when_both_markets_are_open() -> None:
    from domain.domain import select_markets_to_run

    cfg = {
        'schedule_hk': {
            'enabled': True,
            'timezone': 'Asia/Hong_Kong',
            'run_window': {
                'start': '00:00',
                'end': '23:59',
                'breaks': [],
            },
        },
        'schedule': {
            'enabled': True,
            'timezone': 'America/New_York',
            'run_window': {
                'start': '00:00',
                'end': '23:59',
                'breaks': [],
            },
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
            'timezone': 'Asia/Hong_Kong',
            'run_window': {
                'start': '09:30',
                'end': '16:00',
                'breaks': [],
            },
        },
    }

    t_in_hours = datetime(2026, 4, 1, 5, 0, 0, tzinfo=timezone.utc)
    rules = mod._evaluate_auto_market_rules(t_in_hours, cfg)

    assert [rule.schedule_key for rule in rules] == ['schedule_hk', 'schedule']
    assert rules[0].configured is False
    assert rules[0].in_run_window is False
    assert rules[0].resolved_market is None

    assert rules[1].configured is True
    assert rules[1].in_run_window is True
    assert rules[1].inferred_market_from_timezone == 'HK'
    assert rules[1].resolved_market == 'HK'
