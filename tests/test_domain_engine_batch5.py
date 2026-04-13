from __future__ import annotations

from pathlib import Path


def _legacy_notify_threshold_met(
    account_messages: dict[str, str] | object,
    *,
    min_accounts: int = 1,
) -> bool:
    try:
        required = max(1, int(min_accounts))
    except Exception:
        required = 1
    if not isinstance(account_messages, dict):
        return False
    count = sum(1 for _acct, msg in account_messages.items() if bool(str(msg or '').strip()))
    return count >= required


def test_resolve_multi_tick_engine_entrypoint_notify_threshold_matches_legacy() -> None:
    from om.domain.engine import resolve_multi_tick_engine_entrypoint

    cases = [
        ({'lx': 'hello', 'sy': ''}, 1),
        ({'lx': 'hello', 'sy': 'world'}, 2),
        ({'lx': '   '}, 1),
        ({}, 1),
        ('invalid', 1),
        ({'lx': 'hello'}, 0),
        ({'lx': 'hello'}, 'x'),
    ]
    for account_messages, min_accounts in cases:
        expected = _legacy_notify_threshold_met(
            account_messages,
            min_accounts=min_accounts,
        )
        actual_bundle = resolve_multi_tick_engine_entrypoint(
            notify_account_messages=account_messages,
            notify_min_accounts=min_accounts,
        ).get('notify_threshold') or {}
        assert bool(actual_bundle.get('threshold_met')) is expected


def test_main_uses_notify_threshold_entrypoint_batch5() -> None:
    base = Path(__file__).resolve().parents[1]
    src = (base / 'scripts' / 'multi_tick' / 'main.py').read_text(encoding='utf-8')
    assert 'notify_account_messages=account_messages' in src
    assert 'notify_min_accounts=1' in src
    assert 'decide_notify_threshold_met(' not in src


def test_resolve_multi_tick_engine_entrypoint_shape_guard_for_account_scheduler_map() -> None:
    from om.domain.engine import resolve_multi_tick_engine_entrypoint

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
    from om.domain.engine import resolve_multi_tick_engine_entrypoint

    out = resolve_multi_tick_engine_entrypoint(opend_unhealthy='invalid-shape')
    watchdog = out.get('watchdog') or {}
    assert watchdog.get('action') == 'abort'
    assert watchdog.get('fallback_used') is False
