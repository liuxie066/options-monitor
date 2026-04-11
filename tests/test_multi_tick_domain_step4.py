from __future__ import annotations

from datetime import time


def test_evaluate_dnd_quiet_hours_cross_midnight_window() -> None:
    from om.domain.multi_tick import evaluate_dnd_quiet_hours

    out = evaluate_dnd_quiet_hours(
        schedule_v2_enabled=False,
        quiet_hours={'start': '23:00', 'end': '06:00'},
        no_send=False,
        now_bj_time=time(0, 30),
        parse_hhmm_fn=lambda s: time.fromisoformat(s),
    )

    assert out['enabled'] is True
    assert out['quiet_window'] == '23:00-06:00'
    assert out['is_quiet'] is True
    assert out['parse_error'] is None


def test_evaluate_dnd_quiet_hours_parse_error_keeps_non_blocking_behavior() -> None:
    from om.domain.multi_tick import evaluate_dnd_quiet_hours

    out = evaluate_dnd_quiet_hours(
        schedule_v2_enabled=False,
        quiet_hours={'start': 'BAD', 'end': '06:00'},
        no_send=False,
        now_bj_time=time(3, 0),
        parse_hhmm_fn=lambda s: time.fromisoformat(s),
    )

    assert out['enabled'] is True
    assert out['is_quiet'] is False
    assert isinstance(out['parse_error'], str) and bool(out['parse_error'])


def test_decide_notify_dispatch_preserves_route_and_target_rules() -> None:
    from om.domain.multi_tick import decide_notify_dispatch

    assert decide_notify_dispatch(no_send=True, target='chat-id', dnd_is_quiet=False) == {
        'should_send': False,
        'effective_target': None,
        'config_error': None,
        'reason': 'no_send',
    }

    assert decide_notify_dispatch(no_send=False, target='', dnd_is_quiet=False) == {
        'should_send': False,
        'effective_target': '',
        'config_error': 'notifications.target is required',
        'reason': 'config_error',
    }

    assert decide_notify_dispatch(no_send=False, target='chat-id', dnd_is_quiet=True) == {
        'should_send': False,
        'effective_target': 'chat-id',
        'config_error': None,
        'reason': 'quiet_hours',
    }
