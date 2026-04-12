from __future__ import annotations


def test_apply_scan_run_decision_force_and_smoke_keep_existing_semantics() -> None:
    from om.domain.multi_tick import apply_scan_run_decision

    should_run, reason = apply_scan_run_decision(
        should_run_global=False,
        reason_global='interval_not_due',
        force_mode=True,
        smoke=True,
    )

    assert should_run is False
    assert reason == 'interval_not_due | force | force: bypass guard | smoke_skip_pipeline'


def test_decide_should_notify_prefers_account_and_fallbacks_to_scheduler_fields() -> None:
    from om.domain.multi_tick import decide_should_notify

    assert (
        decide_should_notify(
            account='lx',
            notify_decision_by_account={'lx': True},
            scheduler_decision={'should_notify': False, 'is_notify_window_open': False},
        )
        is True
    )

    assert (
        decide_should_notify(
            account='sy',
            notify_decision_by_account={},
            scheduler_decision={'should_notify': True, 'is_notify_window_open': False},
        )
        is False
    )

    assert (
        decide_should_notify(
            account='sy',
            notify_decision_by_account={},
            scheduler_decision={'should_notify': True},
        )
        is True
    )

    assert (
        decide_should_notify(
            account='sy',
            notify_decision_by_account={'sy': False},
            scheduler_decision={'is_notify_window_open': True},
        )
        is False
    )


def test_decide_should_notify_accepts_scheduler_view() -> None:
    from om.domain.engine import SchedulerDecisionView
    from om.domain.multi_tick import decide_should_notify

    assert (
        decide_should_notify(
            account='sy',
            notify_decision_by_account={},
            scheduler_decision=SchedulerDecisionView(
                should_run_scan=True,
                is_notify_window_open=True,
                reason='ok',
            ),
        )
        is True
    )


def test_decide_should_notify_accepts_account_scheduler_dto() -> None:
    from om.domain.multi_tick import decide_should_notify

    assert (
        decide_should_notify(
            account='sy',
            notify_decision_by_account={'sy': {'should_notify': True}},
            scheduler_decision={'is_notify_window_open': False},
        )
        is True
    )


def test_decide_should_notify_accepts_account_scheduler_view() -> None:
    from om.domain.engine import AccountSchedulerDecisionView
    from om.domain.multi_tick import decide_should_notify

    assert (
        decide_should_notify(
            account='sy',
            notify_decision_by_account={'sy': AccountSchedulerDecisionView(is_notify_window_open=True)},
            scheduler_decision={'is_notify_window_open': False},
        )
        is True
    )


def test_filter_notify_candidates_matches_existing_predicate() -> None:
    from om.domain.multi_tick import filter_notify_candidates
    from scripts.multi_tick.misc import AccountResult

    results = [
        AccountResult('a', True, True, True, 'ok', 'x'),
        AccountResult('b', True, True, False, 'ok', 'x'),
        AccountResult('c', True, False, True, 'ok', 'x'),
        AccountResult('d', True, True, True, 'ok', '   '),
    ]

    selected = filter_notify_candidates(results)
    assert [r.account for r in selected] == ['a']


def test_build_account_messages_aggregates_non_empty_messages() -> None:
    from om.domain.multi_tick_result import build_account_messages
    from scripts.multi_tick.misc import AccountResult

    def _cash_footer_for_account(lines: list[str], account: str) -> list[str]:
        return [f"{account}:{len(lines)}"]

    def _build_account_message(result, *, now_bj, cash_footer_lines: list[str]) -> str:
        if result.account == 'a':
            return f"{result.account}@{now_bj}|{','.join(cash_footer_lines)}"
        return ''

    results = [
        AccountResult('a', True, True, True, 'ok', 'x'),
        AccountResult('b', True, True, True, 'ok', 'y'),
    ]

    out = build_account_messages(
        notify_candidates=results,
        now_bj='BJ_NOW',
        cash_footer_lines=['line1'],
        cash_footer_for_account_fn=_cash_footer_for_account,
        build_account_message_fn=_build_account_message,
    )
    assert out == {'a': 'a@BJ_NOW|a:1'}


def test_build_no_account_notification_payloads_keeps_existing_fields() -> None:
    from om.domain.multi_tick_result import build_no_account_notification_payloads
    from scripts.multi_tick.misc import AccountResult

    calls = {'n': 0}

    def _now() -> str:
        calls['n'] += 1
        return f'2026-04-11T11:44:0{calls["n"]}Z'

    results = [
        AccountResult('a', True, True, True, 'ok', 'x'),
        AccountResult('b', False, False, False, 'skip', ''),
    ]

    shared, per_account = build_no_account_notification_payloads(
        now_utc_fn=_now,
        results=results,
        run_dir='/tmp/run',
    )

    assert shared['reason'] == 'no_account_notification'
    assert shared['accounts'] == ['a', 'b']
    assert shared['results'][0]['account'] == 'a'
    assert per_account['a']['account'] == 'a'
    assert per_account['a']['result']['decision_reason'] == 'ok'
    assert per_account['b']['run_dir'] == '/tmp/run'
    assert calls['n'] == 3


def test_build_shared_last_run_payload_merges_prev_and_caps_history() -> None:
    from om.domain.multi_tick_result import build_shared_last_run_payload

    prev = {
        'legacy': 1,
        'history': [{'id': 1}, {'id': 2}],
    }
    run_meta = {'id': 3, 'sent': True}

    out = build_shared_last_run_payload(prev_payload=prev, run_meta=run_meta, history_limit=2)
    assert out['legacy'] == 1
    assert out['id'] == 3
    assert out['history'] == [{'id': 2}, {'id': 3, 'sent': True}]
