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
