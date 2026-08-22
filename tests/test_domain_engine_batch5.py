from __future__ import annotations

from pathlib import Path


def test_resolve_multi_tick_engine_entrypoint_notify_threshold_matches_legacy() -> None:
    from domain.domain.engine import resolve_multi_tick_engine_entrypoint

    cases = [
        ({'lx': 'hello', 'sy': ''}, 1, True),
        ({'lx': 'hello', 'sy': 'world'}, 2, True),
        ({'lx': '   '}, 1, False),
        ({}, 1, False),
        ('invalid', 1, False),
        ({'lx': 'hello'}, 0, True),
        ({'lx': 'hello'}, 'x', True),
    ]
    for account_messages, min_accounts, expected in cases:
        actual_bundle = resolve_multi_tick_engine_entrypoint(
            notify_account_messages=account_messages,
            notify_min_accounts=min_accounts,
        ).get('notify_threshold') or {}
        assert bool(actual_bundle.get('threshold_met')) is expected


def test_scheduled_notification_uses_daily_brief_authority_without_legacy_threshold_path() -> None:
    base = Path(__file__).resolve().parents[1]
    notification_flow_src = (base / 'src' / 'application' / 'tick_notification_flow.py').read_text(encoding='utf-8')
    notification_src = (base / 'src' / 'application' / 'scheduled_notification.py').read_text(encoding='utf-8')

    assert '_prepare_daily_brief_notification(request)' in notification_flow_src
    assert 'prepare_multi_account_notification(' not in notification_flow_src
    assert 'prepare_per_account_messages(' not in notification_src
    assert 'mark_no_candidate_notification_metrics(' not in notification_src


def test_resolve_multi_tick_engine_entrypoint_shape_guard_for_account_scheduler_map() -> None:
    from domain.domain.engine import resolve_multi_tick_engine_entrypoint

    out = resolve_multi_tick_engine_entrypoint(
        scheduler_raw={
            'should_run_scan': True,
            'is_notify_window_open': True,
            'reason': 'ok',
        },
        account_scheduler_raw_by_account=['not-a-mapping'],
    )
    scheduler = out.get('scheduler') or {}
    assert scheduler.get('account_scheduler_decisions') == {}
    assert scheduler.get('account_scheduler_views') == {}


def test_resolve_multi_tick_engine_entrypoint_shape_guard_for_opend_payload() -> None:
    from domain.domain.engine import resolve_multi_tick_engine_entrypoint

    out = resolve_multi_tick_engine_entrypoint(opend_unhealthy='invalid-shape')
    watchdog = out.get('watchdog') or {}
    assert watchdog.get('action') == 'abort'
    assert watchdog.get('fallback_used') is False


def test_notify_delivery_action_matches_legacy_branching_batch5() -> None:
    from domain.domain.engine import decide_notify_delivery_action

    cases = [
        ({'action': 'skip_quiet_hours', 'effective_target': 'u1', 'reason': 'quiet_hours', 'quiet_window': '23:00-06:00'}, 'skip_quiet_hours', False, None),
        ({'action': 'config_error', 'effective_target': '', 'reason': 'config_error', 'config_error': 'missing target'}, 'config_error', False, 'missing target'),
        ({'action': 'send', 'effective_target': 'u2', 'reason': 'send'}, 'send', True, None),
        ({'action': 'skip', 'effective_target': None, 'reason': 'no_send'}, 'skip', False, None),
        ({'action': 'unknown', 'effective_target': None, 'reason': 'x'}, 'skip', False, None),
    ]
    for gate, action, should_send, config_error in cases:
        assert decide_notify_delivery_action(dispatch_gate=gate) == {
            'action': action,
            'should_send': should_send,
            'config_error': config_error,
            'effective_target': gate.get('effective_target'),
            'reason': gate['reason'],
            'quiet_window': gate.get('quiet_window', ''),
        }


def test_decide_notification_delivery_centralizes_single_entry_policy() -> None:
    from domain.domain.engine import decide_notification_delivery

    assert decide_notification_delivery(
        should_notify_window=True,
        notification_text='hello',
        target='',
    ) == {
        'action': 'config_error',
        'should_send': False,
        'meaningful': True,
        'effective_target': '',
        'config_error': 'notifications.target is required',
        'reason': 'config_error',
    }
    assert decide_notification_delivery(
        should_notify_window=True,
        notification_text='hello',
        target='user:test',
        is_quiet=True,
    )['action'] == 'skip_quiet_hours'
    assert decide_notification_delivery(
        should_notify_window=True,
        notification_text='hello',
        target='user:test',
        no_send=True,
    )['reason'] == 'no_send'
    assert decide_notification_delivery(
        should_notify_window=False,
        notification_text='hello',
        target='user:test',
    )['reason'] == 'notify_window_closed'
    assert decide_notification_delivery(
        should_notify_window=True,
        notification_text='今日无需要主动提醒的内容。',
        target='user:test',
    )['reason'] == 'not_meaningful'
    assert decide_notification_delivery(
        should_notify_window=True,
        notification_text='hello',
        target='user:test',
    ) == {
        'action': 'send',
        'should_send': True,
        'meaningful': True,
        'effective_target': 'user:test',
        'config_error': None,
        'reason': 'send',
    }
