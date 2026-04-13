from __future__ import annotations

from pathlib import Path


def _legacy_plan(
    *,
    error_code: str,
    degraded: bool,
    message_text: str,
    detail_text: str,
    host,
    port,
) -> dict[str, object]:
    code = str(error_code or 'OPEND_API_ERROR')
    if code == 'OPEND_NEEDS_PHONE_VERIFY':
        action = {
            'action': 'pause_phone_verify',
            'terminal': True,
            'fallback_used': False,
        }
        return {
            **action,
            'alert_message_text': str(message_text or '') + '（已暂停：等待你在飞书确认后再继续）',
            'alert_detail': (f'{host}:{port} {detail_text}' if host is not None and port is not None else str(detail_text or '')),
            'should_mark_phone_verify_pending': True,
            'should_write_account_last_run': False,
            'should_continue': False,
        }
    if bool(degraded):
        action = {
            'action': 'degrade_continue',
            'terminal': False,
            'fallback_used': True,
        }
        return {
            **action,
            'alert_message_text': str(message_text or ''),
            'alert_detail': (f'{host}:{port} {detail_text}' if host is not None and port is not None else str(detail_text or '')),
            'should_mark_phone_verify_pending': False,
            'should_write_account_last_run': True,
            'should_continue': True,
        }
    action = {
        'action': 'abort',
        'terminal': True,
        'fallback_used': False,
    }
    return {
        **action,
        'alert_message_text': str(message_text or ''),
        'alert_detail': (f'{host}:{port} {detail_text}' if host is not None and port is not None else str(detail_text or '')),
        'should_mark_phone_verify_pending': False,
        'should_write_account_last_run': True,
        'should_continue': False,
    }


def test_build_opend_unhealthy_execution_plan_matches_legacy_branching() -> None:
    from om.domain.engine import build_opend_unhealthy_execution_plan

    for error_code in ('OPEND_NEEDS_PHONE_VERIFY', 'OPEND_API_ERROR'):
        for degraded in (False, True):
            for host, port in ((None, None), ('127.0.0.1', 11111)):
                expected = _legacy_plan(
                    error_code=error_code,
                    degraded=degraded,
                    message_text='msg',
                    detail_text='detail',
                    host=host,
                    port=port,
                )
                actual = build_opend_unhealthy_execution_plan(
                    error_code=error_code,
                    degraded=degraded,
                    message_text='msg',
                    detail_text='detail',
                    host=host,
                    port=port,
                )
                assert actual == expected


def test_main_uses_opend_unhealthy_execution_plan_batch4() -> None:
    base = Path(__file__).resolve().parents[1]
    src = (base / 'scripts' / 'multi_tick' / 'main.py').read_text(encoding='utf-8')
    assert 'build_opend_unhealthy_execution_plan' in src


def _legacy_trading_day_guard_decision(
    *,
    markets_to_run: list[str],
    guard_markets: list[str],
    check_fn,
) -> dict[str, object]:
    guard_results: list[dict[str, object]] = []
    for gm in guard_markets:
        is_td, gm_used = check_fn(gm)
        guard_results.append({'market': gm_used, 'is_trading_day': is_td})

    false_markets = [str(r.get('market')) for r in guard_results if r.get('is_trading_day') is False]
    true_markets = [str(r.get('market')) for r in guard_results if r.get('is_trading_day') is True]

    if false_markets:
        if markets_to_run:
            narrowed = [m for m in markets_to_run if m not in set(false_markets)]
            if not narrowed:
                return {
                    'guard_results': guard_results,
                    'markets_to_run': [],
                    'should_skip': True,
                    'skip_message': f"non-trading day: {','.join(false_markets)}",
                }
            return {
                'guard_results': guard_results,
                'markets_to_run': narrowed,
                'should_skip': False,
                'skip_message': '',
            }
        if true_markets:
            return {
                'guard_results': guard_results,
                'markets_to_run': sorted({m for m in true_markets if m in ('HK', 'US', 'CN')}),
                'should_skip': False,
                'skip_message': '',
            }
        return {
            'guard_results': guard_results,
            'markets_to_run': [],
            'should_skip': True,
            'skip_message': f"non-trading day: {','.join(false_markets)}",
        }

    return {
        'guard_results': guard_results,
        'markets_to_run': list(markets_to_run or []),
        'should_skip': False,
        'skip_message': '',
    }


def test_decide_trading_day_guard_matches_legacy_semantics() -> None:
    from om.domain.engine import decide_trading_day_guard
    from om.domain.multi_tick import reduce_trading_day_guard

    def _check(gm: str) -> tuple[bool | None, str]:
        table = {
            'US': (False, 'US'),
            'HK': (True, 'HK'),
            'CN': (None, 'CN'),
        }
        return table[gm]

    expected = _legacy_trading_day_guard_decision(
        markets_to_run=['US', 'HK'],
        guard_markets=['US', 'HK', 'CN'],
        check_fn=_check,
    )
    actual = decide_trading_day_guard(
        markets_to_run=['US', 'HK'],
        guard_markets=['US', 'HK', 'CN'],
        check_trading_day_for_market=_check,
        reduce_guard_fn=reduce_trading_day_guard,
    )
    assert actual == expected
