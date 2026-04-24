from __future__ import annotations

from scripts.multi_tick.misc import AccountResult


def test_notify_candidate_filter_and_rank_keep_semantics() -> None:
    from domain.domain.engine import filter_notify_candidates, rank_notify_candidates

    results = [
        AccountResult('a', True, True, 'ok', 'msg-a'),
        AccountResult('b', True, True, 'ok', '今日无需要主动提醒的内容。'),
        AccountResult('c', True, False, 'ok', 'msg-c'),
        AccountResult('d', True, True, 'ok', '   '),
    ]

    filtered = filter_notify_candidates(results)
    ranked = rank_notify_candidates(filtered)
    assert [r.account for r in ranked] == ['a']

def test_decide_notify_threshold_met_min_one_account_keeps_legacy_behavior() -> None:
    from domain.domain.engine import decide_notify_threshold_met

    assert decide_notify_threshold_met({'lx': 'hello'}, min_accounts=1) is True
    assert decide_notify_threshold_met({'lx': '   '}, min_accounts=1) is False
    assert decide_notify_threshold_met({}, min_accounts=1) is False
