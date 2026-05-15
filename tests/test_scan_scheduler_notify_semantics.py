"""Regression: scan_scheduler should expose explicit notify-window semantics."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


def test_scan_scheduler_emits_notify_window_for_downstream_delivery() -> None:
    base = Path(__file__).resolve().parents[1]

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
    state = {
        'last_run_utc_by_account': {},
        'last_notify_utc': None,
        'last_notify_utc_by_account': {},
    }
    now_utc = datetime(2026, 4, 1, 1, 0, 0, tzinfo=timezone.utc)

    decision = decide(schedule_cfg, state, now_utc, account='lx', schedule_key='schedule_hk')
    payload = json.loads(json.dumps(decision.__dict__, ensure_ascii=False))

    assert payload['schedule_key'] == 'schedule_hk'
    assert 'is_notify_window_open' in payload


def test_scan_scheduler_uses_simple_market_day_targets() -> None:
    from src.application.scan_scheduler import decide

    schedule_cfg = {
        'enabled': True,
        'timezone': 'Asia/Hong_Kong',
        'cron_interval_min': 10,
        'run_window': {
            'start': '09:30',
            'end': '16:00',
            'breaks': [
                {'start': '12:00', 'end': '13:00'},
            ],
        },
        'run_points': {
            'start_plus_min': 10,
            'hourly_minute': 0,
            'end_minus_min': 10,
        },
        'beijing_timezone': 'Asia/Shanghai',
    }
    empty_state = {
        'last_run_utc_by_account': {},
        'last_notify_utc': None,
        'last_notify_utc_by_account': {},
    }

    before_first = decide(
        schedule_cfg,
        empty_state,
        datetime(2026, 4, 1, 1, 35, 0, tzinfo=timezone.utc),  # 09:35 HKT
        account='lx',
        schedule_key='schedule_hk',
    )
    assert before_first.should_run_scan is False
    assert before_first.is_notify_window_open is False
    assert before_first.next_run_market.endswith('09:40:00+08:00')

    first = decide(
        schedule_cfg,
        empty_state,
        datetime(2026, 4, 1, 1, 40, 0, tzinfo=timezone.utc),  # 09:40 HKT
        account='lx',
        schedule_key='schedule_hk',
    )
    assert first.should_run_scan is True
    assert first.is_notify_window_open is True

    already_scanned = {
        **empty_state,
        'last_run_utc_by_account': {
            'lx': datetime(2026, 4, 1, 1, 40, 1, tzinfo=timezone.utc).isoformat(),
        },
    }
    duplicate = decide(
        schedule_cfg,
        already_scanned,
        datetime(2026, 4, 1, 1, 45, 0, tzinfo=timezone.utc),  # 09:45 HKT
        account='lx',
        schedule_key='schedule_hk',
    )
    assert duplicate.should_run_scan is False
    assert duplicate.is_notify_window_open is False
    assert duplicate.next_run_market.endswith('10:00:00+08:00')

    hourly = decide(
        schedule_cfg,
        already_scanned,
        datetime(2026, 4, 1, 3, 0, 0, tzinfo=timezone.utc),  # 11:00 HKT
        account='lx',
        schedule_key='schedule_hk',
    )
    assert hourly.should_run_scan is True
    assert hourly.is_notify_window_open is True

    during_break = decide(
        schedule_cfg,
        already_scanned,
        datetime(2026, 4, 1, 4, 0, 0, tzinfo=timezone.utc),  # 12:00 HKT
        account='lx',
        schedule_key='schedule_hk',
    )
    assert during_break.should_run_scan is False
    assert during_break.is_notify_window_open is False
    assert during_break.next_run_market.endswith('13:00:00+08:00')

    final = decide(
        schedule_cfg,
        already_scanned,
        datetime(2026, 4, 1, 7, 50, 0, tzinfo=timezone.utc),  # 15:50 HKT
        account='lx',
        schedule_key='schedule_hk',
    )
    assert final.should_run_scan is True
    assert final.is_notify_window_open is True


def test_scan_scheduler_us_beijing_before_2am_gate_handles_dst() -> None:
    from src.application.scan_scheduler import decide

    schedule_cfg = {
        'enabled': True,
        'timezone': 'America/New_York',
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
        'gates': [
            {
                'type': 'before',
                'timezone': 'Asia/Shanghai',
                'time': '02:00',
                'day_offset_from_window_start': 1,
            }
        ],
        'beijing_timezone': 'Asia/Shanghai',
    }
    empty_state = {
        'last_run_utc_by_account': {},
        'last_notify_utc': None,
        'last_notify_utc_by_account': {},
    }

    summer_allowed = decide(
        schedule_cfg,
        empty_state,
        datetime(2026, 7, 1, 17, 0, 0, tzinfo=timezone.utc),  # 13:00 EDT / 01:00 Beijing next day
        account='lx',
    )
    assert summer_allowed.should_run_scan is True

    summer_cutoff = decide(
        schedule_cfg,
        empty_state,
        datetime(2026, 7, 1, 18, 0, 0, tzinfo=timezone.utc),  # 14:00 EDT / 02:00 Beijing next day
        account='lx',
    )
    assert summer_cutoff.should_run_scan is False

    winter_allowed = decide(
        schedule_cfg,
        empty_state,
        datetime(2026, 1, 5, 17, 0, 0, tzinfo=timezone.utc),  # 12:00 EST / 01:00 Beijing next day
        account='lx',
    )
    assert winter_allowed.should_run_scan is True

    winter_cutoff = decide(
        schedule_cfg,
        empty_state,
        datetime(2026, 1, 5, 18, 0, 0, tzinfo=timezone.utc),  # 13:00 EST / 02:00 Beijing next day
        account='lx',
    )
    assert winter_cutoff.should_run_scan is False
