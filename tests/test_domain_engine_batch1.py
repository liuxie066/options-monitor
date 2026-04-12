from __future__ import annotations

from scripts.multi_tick.misc import AccountResult


def test_decide_opend_degrade_to_yahoo_keeps_existing_gate() -> None:
    from om.domain.engine import decide_opend_degrade_to_yahoo

    assert (
        decide_opend_degrade_to_yahoo(
            allow_downgrade=True,
            has_hk_opend=False,
            watchdog_timed_out=False,
        )
        is True
    )
    assert (
        decide_opend_degrade_to_yahoo(
            allow_downgrade=False,
            has_hk_opend=False,
            watchdog_timed_out=False,
        )
        is False
    )
    assert (
        decide_opend_degrade_to_yahoo(
            allow_downgrade=True,
            has_hk_opend=True,
            watchdog_timed_out=False,
        )
        is False
    )
    assert (
        decide_opend_degrade_to_yahoo(
            allow_downgrade=True,
            has_hk_opend=False,
            watchdog_timed_out=True,
        )
        is False
    )


def test_notify_candidate_filter_and_rank_keep_semantics() -> None:
    from om.domain.engine import filter_notify_candidates, rank_notify_candidates

    results = [
        AccountResult('a', True, True, True, 'ok', 'msg-a'),
        AccountResult('b', True, True, False, 'ok', 'msg-b'),
        AccountResult('c', True, False, True, 'ok', 'msg-c'),
        AccountResult('d', True, True, True, 'ok', '   '),
    ]

    filtered = filter_notify_candidates(results)
    ranked = rank_notify_candidates(filtered)
    assert [r.account for r in ranked] == ['a']
