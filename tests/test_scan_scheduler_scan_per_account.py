"""Regression: scan_scheduler scan clock should be per-account in multi-account mode."""

from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta


def test_scan_scheduler_scan_is_per_account() -> None:
    from src.application.scan_scheduler import decide

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


def test_scheduler_decision_payload_uses_account_scan_clock(tmp_path) -> None:
    from src.application.scan_scheduler import build_scheduler_decision_payload

    schedule_cfg = {
        'enabled': True,
        'market_timezone': 'Asia/Hong_Kong',
        'market_open': '09:30',
        'market_close': '16:00',
        'first_notify_after_open_min': 30,
        'notify_interval_min': 60,
        'final_notify_before_close_min': 10,
        'beijing_timezone': 'Asia/Shanghai',
    }
    t0 = datetime(2026, 4, 1, 2, 0, 0, tzinfo=timezone.utc)
    t1 = t0 + timedelta(minutes=10)
    config = tmp_path / 'config.us.json'
    state = tmp_path / 'scheduler_state.json'
    config.write_text(json.dumps({'schedule': schedule_cfg}), encoding='utf-8')
    state.write_text(
        json.dumps(
            {
                'last_scan_utc': None,
                'last_scan_utc_by_account': {'lx': t0.isoformat()},
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
    assert data['last_scan_utc'] == t0.isoformat()
    assert data['last_scan_utc_by_account'] == {
        'lx': t0.isoformat(),
        'sy': t0.isoformat(),
    }
