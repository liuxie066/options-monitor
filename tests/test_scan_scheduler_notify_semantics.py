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
        'monitor_off_hours': True,
        'market_dense_interval_min': 30,
        'market_sparse_interval_min': 30,
        'notify_cooldown_min': 60,
        'beijing_timezone': 'Asia/Shanghai',
        'sparse_after_beijing': '02:00',
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
