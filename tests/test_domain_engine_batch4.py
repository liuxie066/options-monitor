from __future__ import annotations

from pathlib import Path


def test_build_opend_unhealthy_execution_plan_matches_legacy_branching() -> None:
    from domain.domain.engine import build_opend_unhealthy_execution_plan

    cases = (
        ('OPEND_NEEDS_PHONE_VERIFY', False, 'pause_phone_verify', True, False, True, False, False),
        ('OPEND_NEEDS_PHONE_VERIFY', True, 'pause_phone_verify', True, False, True, False, False),
        ('OPEND_API_ERROR', False, 'abort', True, False, False, True, False),
        ('OPEND_API_ERROR', True, 'degrade_continue', False, True, False, True, True),
    )
    for error_code, degraded, action, terminal, fallback, pending, write_last_run, should_continue in cases:
        for host, port, detail in ((None, None, 'detail'), ('127.0.0.1', 11111, '127.0.0.1:11111 detail')):
            actual = build_opend_unhealthy_execution_plan(
                error_code=error_code,
                degraded=degraded,
                message_text='msg',
                detail_text='detail',
                host=host,
                port=port,
            )
            assert actual == {
                'action': action,
                'terminal': terminal,
                'fallback_used': fallback,
                'alert_message_text': (
                    'msg（已暂停：等待你在飞书确认后再继续）'
                    if pending
                    else 'msg'
                ),
                'alert_detail': detail,
                'should_mark_phone_verify_pending': pending,
                'should_write_account_last_run': write_last_run,
                'should_continue': should_continue,
            }


def test_main_uses_opend_unhealthy_execution_plan_batch4() -> None:
    base = Path(__file__).resolve().parents[1]
    src = (base / 'src' / 'application' / 'tick_guard_flow.py').read_text(encoding='utf-8')
    assert 'build_opend_unhealthy_execution_plan' in src


def test_decide_trading_day_guard_matches_legacy_semantics() -> None:
    from domain.domain.engine import decide_trading_day_guard
    from domain.domain.multi_tick import reduce_trading_day_guard

    def _check(gm: str) -> tuple[bool | None, str]:
        table = {
            'US': (False, 'US'),
            'HK': (True, 'HK'),
            'CN': (None, 'CN'),
        }
        return table[gm]

    actual = decide_trading_day_guard(
        markets_to_run=['US', 'HK'],
        guard_markets=['US', 'HK', 'CN'],
        check_trading_day_for_market=_check,
        reduce_guard_fn=reduce_trading_day_guard,
    )
    assert actual == {
        'guard_results': [
            {'market': 'US', 'is_trading_day': False},
            {'market': 'HK', 'is_trading_day': True},
            {'market': 'CN', 'is_trading_day': None},
        ],
        'markets_to_run': ['HK'],
        'should_skip': False,
        'skip_message': '',
    }


def test_decide_notify_dispatch_gate_matches_legacy_branching() -> None:
    from domain.domain.engine import decide_notify_dispatch_gate

    cases = [
        (
            {
                'should_send': False,
                'effective_target': 'chat-id',
                'config_error': None,
                'reason': 'quiet_hours',
            },
            {'quiet_window': '23:00-06:00'},
            'skip_quiet_hours',
        ),
        (
            {
                'should_send': False,
                'effective_target': '',
                'config_error': 'notifications.target is required',
                'reason': 'config_error',
            },
            {'quiet_window': ''},
            'config_error',
        ),
        (
            {
                'should_send': True,
                'effective_target': 'chat-id',
                'config_error': None,
                'reason': 'send',
            },
            {'quiet_window': ''},
            'send',
        ),
        (
            {
                'should_send': False,
                'effective_target': None,
                'config_error': None,
                'reason': 'no_send',
            },
            {'quiet_window': ''},
            'skip',
        ),
    ]

    for dispatch_decision, dnd_decision, action in cases:
        actual = decide_notify_dispatch_gate(
            dispatch_decision=dispatch_decision,
            dnd_decision=dnd_decision,
        )
        assert actual == {
            'action': action,
            'reason': dispatch_decision['reason'],
            'should_send': action == 'send',
            'effective_target': dispatch_decision['effective_target'],
            'config_error': dispatch_decision['config_error'] if action == 'config_error' else None,
            'quiet_window': dnd_decision['quiet_window'],
        }


def test_main_uses_notify_dispatch_gate_entrypoint_batch4() -> None:
    base = Path(__file__).resolve().parents[1]
    notification_flow_src = (base / 'src' / 'application' / 'tick_notification_flow.py').read_text(encoding='utf-8')
    for entrypoint in (
        'assemble_daily_decision_briefs(',
        'persist_daily_decision_brief_success(',
        'prepare_daily_decision_brief_delivery(',
        'render_fixed_report(',
        'render_candidate_alert(',
        'render_fixed_failure(',
        'build_per_account_delivery_batch(',
        'decision_builder=decide_notification_delivery',
    ):
        assert entrypoint in notification_flow_src


def test_main_orchestrator_guard_batch4_no_legacy_rule_reflow() -> None:
    base = Path(__file__).resolve().parents[1]
    src = (base / 'src' / 'application' / 'multi_account_tick.py').read_text(encoding='utf-8')
    scheduler_context_src = (base / 'src' / 'application' / 'tick_scheduler_context.py').read_text(encoding='utf-8')
    guard_flow_src = (base / 'src' / 'application' / 'tick_guard_flow.py').read_text(encoding='utf-8')
    notification_flow_src = (base / 'src' / 'application' / 'tick_notification_flow.py').read_text(encoding='utf-8')

    # Keep main.py as orchestration-only and check delegated owner modules for key Batch-4 decisions.
    assert 'TickSchedulerRequest(' in src

    for entrypoint in (
        'decide_trading_day_guard=decide_trading_day_guard',
        'engine_entrypoint=resolve_multi_tick_engine_entrypoint',
    ):
        assert entrypoint in scheduler_context_src

    assert 'build_opend_unhealthy_execution_plan=build_opend_unhealthy_execution_plan' in guard_flow_src
    assert 'resolve_multi_tick_engine_entrypoint=resolve_multi_tick_engine_entrypoint' in guard_flow_src

    for entrypoint in (
        'assemble_daily_decision_briefs(',
        'persist_daily_decision_brief_success(',
        'prepare_daily_decision_brief_delivery(',
        'render_fixed_report(',
        'render_candidate_alert(',
        'render_fixed_failure(',
        'build_per_account_delivery_batch(',
    ):
        assert entrypoint in notification_flow_src

    for legacy_notification_fragment in (
        'resolve_multi_tick_engine_entrypoint',
        'engine_filter_notify_candidates',
        'rank_notify_candidates',
        'filter_notify_candidates_fn=',
        'rank_notify_candidates_fn=',
        'build_account_message(',
        'build_account_message_compact(',
        'prepare_multi_account_notification(',
    ):
        assert legacy_notification_fragment not in notification_flow_src

    # Guard against legacy business predicates drifting back into main.py.
    for legacy_fragment in (
        "allow_downgrade and (not has_hk_opend) and (not watchdog_timed_out)",
        "false_markets = [str(r.get('market')) for r in guard_results if r.get('is_trading_day') is False]",
        "if reason == 'quiet_hours':",
        "if str(dispatch_gate.get('action') or '') == 'skip_quiet_hours':",
        'decide_notify_dispatch(',
        'decide_notify_delivery_action(',
        "if should_send:",
    ):
        assert legacy_fragment not in src
