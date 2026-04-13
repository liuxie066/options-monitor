from __future__ import annotations

from scripts.multi_tick.misc import AccountResult


def test_decide_opend_degrade_to_yahoo_keeps_existing_gate() -> None:
    from domain.domain.engine import decide_opend_degrade_to_yahoo

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
    from domain.domain.engine import filter_notify_candidates, rank_notify_candidates

    results = [
        AccountResult('a', True, True, True, 'ok', 'msg-a'),
        AccountResult('b', True, True, False, 'ok', 'msg-b'),
        AccountResult('c', True, False, True, 'ok', 'msg-c'),
        AccountResult('d', True, True, True, 'ok', '   '),
    ]

    filtered = filter_notify_candidates(results)
    ranked = rank_notify_candidates(filtered)
    assert [r.account for r in ranked] == ['a']


def test_apply_opend_degrade_to_yahoo_keeps_existing_symbol_scope() -> None:
    from domain.domain.engine import apply_opend_degrade_to_yahoo

    symbols = [
        {'market': 'US', 'fetch': {'source': 'opend', 'host': '127.0.0.1', 'port': 11111}},
        {'market': 'HK', 'fetch': {'source': 'opend', 'host': '127.0.0.1', 'port': 11111}},
        {'market': 'US', 'fetch': {'source': 'yahoo'}},
    ]
    degraded = apply_opend_degrade_to_yahoo(
        symbols=symbols,
        allow_downgrade=True,
        has_hk_opend=False,
        watchdog_timed_out=False,
    )

    assert degraded is True
    assert symbols[0]['fetch']['source'] == 'yahoo'
    assert 'host' not in symbols[0]['fetch']
    assert 'port' not in symbols[0]['fetch']
    assert symbols[1]['fetch']['source'] == 'opend'
    assert symbols[2]['fetch']['source'] == 'yahoo'


def test_decide_notify_threshold_met_min_one_account_keeps_legacy_behavior() -> None:
    from domain.domain.engine import decide_notify_threshold_met

    assert decide_notify_threshold_met({'lx': 'hello'}, min_accounts=1) is True
    assert decide_notify_threshold_met({'lx': '   '}, min_accounts=1) is False
    assert decide_notify_threshold_met({}, min_accounts=1) is False
