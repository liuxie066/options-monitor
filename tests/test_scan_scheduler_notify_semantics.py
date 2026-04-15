"""Regression: scan_scheduler should expose explicit notify-window semantics."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


def test_scan_scheduler_emits_is_notify_window_open_and_backcompat_should_notify() -> None:
    base = Path(__file__).resolve().parents[1]

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
    state = {
        'last_scan_utc': None,
        'last_notify_utc': None,
        'last_notify_utc_by_account': {},
    }
    now_utc = datetime(2026, 4, 1, 1, 0, 0, tzinfo=timezone.utc)

    decision = decide(schedule_cfg, state, now_utc, account='lx', schedule_key='schedule_hk')
    payload = json.loads(json.dumps(decision.__dict__, ensure_ascii=False))

    assert payload['schedule_key'] == 'schedule_hk'
    assert 'is_notify_window_open' in payload


def test_scan_scheduler_uses_simple_market_day_targets() -> None:
    from scripts.scan_scheduler import decide

    schedule_cfg = {
        'enabled': True,
        'market_timezone': 'Asia/Hong_Kong',
        'market_open': '09:30',
        'market_close': '16:00',
        'market_break_start': '12:00',
        'market_break_end': '13:00',
        'first_notify_after_open_min': 30,
        'notify_interval_min': 60,
        'final_notify_before_close_min': 10,
        'beijing_timezone': 'Asia/Shanghai',
    }
    empty_state = {
        'last_scan_utc': None,
        'last_scan_utc_by_account': {},
        'last_notify_utc': None,
        'last_notify_utc_by_account': {},
    }

    before_first = decide(
        schedule_cfg,
        empty_state,
        datetime(2026, 4, 1, 1, 50, 0, tzinfo=timezone.utc),  # 09:50 HKT
        account='lx',
        schedule_key='schedule_hk',
    )
    assert before_first.should_run_scan is False
    assert before_first.is_notify_window_open is False
    assert before_first.next_run_market.endswith('10:00:00+08:00')

    first = decide(
        schedule_cfg,
        empty_state,
        datetime(2026, 4, 1, 2, 0, 0, tzinfo=timezone.utc),  # 10:00 HKT
        account='lx',
        schedule_key='schedule_hk',
    )
    assert first.should_run_scan is True
    assert first.is_notify_window_open is True

    already_scanned = {
        **empty_state,
        'last_scan_utc_by_account': {
            'lx': datetime(2026, 4, 1, 2, 0, 1, tzinfo=timezone.utc).isoformat(),
        },
    }
    duplicate = decide(
        schedule_cfg,
        already_scanned,
        datetime(2026, 4, 1, 2, 5, 0, tzinfo=timezone.utc),  # 10:05 HKT
        account='lx',
        schedule_key='schedule_hk',
    )
    assert duplicate.should_run_scan is False
    assert duplicate.is_notify_window_open is False
    assert duplicate.next_run_market.endswith('11:00:00+08:00')

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
