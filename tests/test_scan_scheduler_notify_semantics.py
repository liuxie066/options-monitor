"""Regression: scan_scheduler should expose explicit notify-window semantics."""

from __future__ import annotations

import json
from datetime import datetime, timezone


def test_scan_scheduler_emits_notify_window_for_downstream_delivery() -> None:
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
    assert hourly.now_beijing == '2026-04-01T11:00:00+08:00'
    assert hourly.in_run_window is True
    assert hourly.run_window_start_beijing == '2026-04-01T09:30:00+08:00'
    assert hourly.run_window_end_beijing == '2026-04-01T16:00:00+08:00'
    assert hourly.reason == '到达运行点 11:00：执行扫描并允许通知。'

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


def _us_schedule_with_beijing_cutoff() -> dict:
    return {
        'enabled': True,
        'timezone': 'America/New_York',
        'cron_interval_min': 10,
        'run_window': {'start': '09:30', 'end': '16:00', 'breaks': []},
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


def test_us_summer_schedule_keeps_0940_then_hourly_targets_and_structured_batch() -> None:
    from src.application.scan_scheduler import decide

    cfg = _us_schedule_with_beijing_cutoff()
    state = {'last_run_utc_by_account': {}}
    allowed = {
        '09:40': datetime(2026, 7, 20, 13, 40, tzinfo=timezone.utc),
        '10:00': datetime(2026, 7, 20, 14, 0, tzinfo=timezone.utc),
        '11:00': datetime(2026, 7, 20, 15, 0, tzinfo=timezone.utc),
        '12:00': datetime(2026, 7, 20, 16, 0, tzinfo=timezone.utc),
        '13:00': datetime(2026, 7, 20, 17, 0, tzinfo=timezone.utc),
    }

    for label, now_utc in allowed.items():
        decision = decide(cfg, state, now_utc, account='lx', schedule_key='schedule')
        assert decision.should_run_scan is True
        assert decision.scheduled_target_market is not None
        target = datetime.fromisoformat(decision.scheduled_target_market)
        assert target.strftime('%H:%M') == label

    cutoff = decide(
        cfg,
        state,
        datetime(2026, 7, 20, 18, 0, tzinfo=timezone.utc),
        account='lx',
        schedule_key='schedule',
    )
    assert cutoff.should_run_scan is False
    assert cutoff.scheduled_target_market is None


def test_us_winter_schedule_keeps_0940_then_hourly_until_beijing_cutoff() -> None:
    from src.application.scan_scheduler import decide

    cfg = _us_schedule_with_beijing_cutoff()
    state = {'last_run_utc_by_account': {}}
    allowed = {
        '09:40': datetime(2026, 1, 5, 14, 40, tzinfo=timezone.utc),
        '10:00': datetime(2026, 1, 5, 15, 0, tzinfo=timezone.utc),
        '11:00': datetime(2026, 1, 5, 16, 0, tzinfo=timezone.utc),
        '12:00': datetime(2026, 1, 5, 17, 0, tzinfo=timezone.utc),
    }

    for label, now_utc in allowed.items():
        decision = decide(cfg, state, now_utc, account='lx', schedule_key='schedule')
        assert decision.should_run_scan is True
        assert decision.scheduled_target_market is not None
        target = datetime.fromisoformat(decision.scheduled_target_market)
        assert target.strftime('%H:%M') == label

    cutoff = decide(
        cfg,
        state,
        datetime(2026, 1, 5, 18, 0, tzinfo=timezone.utc),
        account='lx',
        schedule_key='schedule',
    )
    assert cutoff.should_run_scan is False
    assert cutoff.scheduled_target_market is None


def test_scheduler_catchup_keeps_original_batch_and_force_has_no_batch() -> None:
    from src.application.scan_scheduler import decide

    cfg = _us_schedule_with_beijing_cutoff()
    state = {'last_run_utc_by_account': {}}
    catchup = decide(
        cfg,
        state,
        datetime(2026, 7, 20, 14, 8, tzinfo=timezone.utc),  # 10:08 EDT catches 10:00
        account='lx',
        schedule_key='schedule',
    )
    assert catchup.should_run_scan is True
    assert catchup.scheduled_target_market is not None
    target = datetime.fromisoformat(catchup.scheduled_target_market)
    assert target.strftime('%H:%M') == '10:00'

    forced = decide(
        cfg,
        state,
        datetime(2026, 7, 20, 14, 8, tzinfo=timezone.utc),
        account='lx',
        schedule_key='schedule',
        force=True,
    )
    assert forced.should_run_scan is True
    assert forced.scheduled_target_market is None


def test_scan_scheduler_adds_half_hour_candidate_targets_without_0930() -> None:
    from src.application.scan_scheduler import decide

    cfg = {
        "enabled": True,
        "timezone": "Asia/Hong_Kong",
        "cron_interval_min": 10,
        "run_window": {
            "start": "09:30",
            "end": "16:00",
            "breaks": [{"start": "12:00", "end": "13:00"}],
        },
        "run_points": {"start_plus_min": 10, "hourly_minute": 0, "end_minus_min": 10},
    }
    state = {
        "last_run_utc_by_account": {},
        "last_processed_scan_target_utc_by_account": {},
    }

    open_tick = decide(cfg, state, datetime(2026, 7, 21, 1, 30, tzinfo=timezone.utc), account="lx")
    assert open_tick.should_run_scan is False
    assert open_tick.scheduled_scan_target_market is None
    assert open_tick.next_run_market.endswith("09:40:00+08:00")

    candidate_tick = decide(cfg, state, datetime(2026, 7, 21, 2, 30, tzinfo=timezone.utc), account="lx")
    assert candidate_tick.should_run_scan is True
    assert candidate_tick.is_notify_window_open is False
    assert candidate_tick.scheduled_scan_target_market.endswith("10:30:00+08:00")
    assert candidate_tick.scheduled_target_market is None
    assert candidate_tick.reason == "到达候选检查点 10:30：执行扫描。"

    lunch_tick = decide(cfg, state, datetime(2026, 7, 21, 4, 30, tzinfo=timezone.utc), account="lx")
    assert lunch_tick.should_run_scan is False
    assert lunch_tick.scheduled_scan_target_market is None
    assert lunch_tick.next_run_market.endswith("13:00:00+08:00")


def test_processed_target_watermark_does_not_let_late_completion_swallow_next_target() -> None:
    from src.application.scan_scheduler import decide

    cfg = {
        "enabled": True,
        "timezone": "Asia/Hong_Kong",
        "cron_interval_min": 10,
        "run_window": {"start": "09:30", "end": "16:00", "breaks": []},
        "run_points": {"start_plus_min": 10, "hourly_minute": 0, "end_minus_min": 10},
    }
    state = {
        "last_run_utc_by_account": {"lx": "2026-07-21T02:01:00+00:00"},
        "last_processed_scan_target_utc_by_account": {"lx": "2026-07-21T01:40:00+00:00"},
    }

    ten_o_clock = decide(cfg, state, datetime(2026, 7, 21, 2, 1, tzinfo=timezone.utc), account="lx")
    assert ten_o_clock.should_run_scan is True
    assert ten_o_clock.scheduled_scan_target_market.endswith("10:00:00+08:00")
    assert ten_o_clock.scheduled_target_market.endswith("10:00:00+08:00")

    state["last_run_utc_by_account"]["lx"] = "2026-07-21T07:51:00+00:00"
    state["last_processed_scan_target_utc_by_account"]["lx"] = "2026-07-21T07:30:00+00:00"
    final_report = decide(cfg, state, datetime(2026, 7, 21, 7, 51, tzinfo=timezone.utc), account="lx")
    assert final_report.should_run_scan is True
    assert final_report.scheduled_scan_target_market.endswith("15:50:00+08:00")


def test_processed_target_watermark_is_account_isolated() -> None:
    from src.application.scan_scheduler import decide

    cfg = {
        "enabled": True,
        "timezone": "Asia/Hong_Kong",
        "cron_interval_min": 10,
        "run_window": {"start": "09:30", "end": "16:00", "breaks": []},
        "run_points": {"start_plus_min": 10, "hourly_minute": 0, "end_minus_min": 10},
    }
    state = {
        "last_run_utc_by_account": {"lx": "2026-07-21T02:00:05+00:00"},
        "last_processed_scan_target_utc_by_account": {"lx": "2026-07-21T02:00:00+00:00"},
    }
    now = datetime(2026, 7, 21, 2, 5, tzinfo=timezone.utc)

    assert decide(cfg, state, now, account="lx").should_run_scan is False
    assert decide(cfg, state, now, account="sy").should_run_scan is True
