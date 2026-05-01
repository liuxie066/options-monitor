from __future__ import annotations

from pathlib import Path


def _legacy_opend_unhealthy_action(error_code: str, degraded: bool) -> dict[str, object]:
    code = str(error_code or 'OPEND_API_ERROR')
    if code == 'OPEND_NEEDS_PHONE_VERIFY':
        return {
            'action': 'pause_phone_verify',
            'terminal': True,
            'fallback_used': False,
        }
    if degraded:
        return {
            'action': 'degrade_continue',
            'terminal': False,
            'fallback_used': True,
        }
    return {
        'action': 'abort',
        'terminal': True,
        'fallback_used': False,
    }


def _legacy_account_scan_gate(
    *,
    should_run: bool,
    markets_to_run: list[str],
    symbols: list[object],
    reason: str,
) -> dict[str, object]:
    if not should_run:
        return {
            'run_pipeline': False,
            'ran_scan': False,
            'meaningful': False,
            'result_reason': reason,
        }
    if markets_to_run and (not symbols):
        return {
            'run_pipeline': False,
            'ran_scan': False,
            'meaningful': False,
            'result_reason': f'{reason} | 本时段无对应市场标的',
        }
    return {
        'run_pipeline': True,
        'ran_scan': True,
        'meaningful': None,
        'result_reason': reason,
    }


def _legacy_pipeline_execution_result(returncode: int) -> dict[str, object]:
    if int(returncode) == 0:
        return {
            'ok': True,
            'ran_scan': True,
            'meaningful': None,
            'reason': '',
        }
    return {
        'ok': False,
        'ran_scan': True,
        'meaningful': False,
        'reason': 'pipeline failed',
    }


def test_decide_opend_unhealthy_action_matches_legacy_branching() -> None:
    from domain.domain.engine import decide_opend_unhealthy_action

    for error_code in ('OPEND_NEEDS_PHONE_VERIFY', 'OPEND_API_ERROR'):
        for degraded in (False, True):
            expected = _legacy_opend_unhealthy_action(error_code, degraded)
            actual = decide_opend_unhealthy_action(error_code=error_code, degraded=degraded)
            assert actual == expected


def test_decide_account_scan_gate_matches_legacy_branching() -> None:
    from domain.domain.engine import decide_account_scan_gate

    cases = [
        {
            'should_run': False,
            'markets_to_run': ['US'],
            'symbols': [{'symbol': 'AAPL'}],
            'reason': 'interval_not_due',
        },
        {
            'should_run': True,
            'markets_to_run': ['US'],
            'symbols': [],
            'reason': 'ok',
        },
        {
            'should_run': True,
            'markets_to_run': [],
            'symbols': [],
            'reason': 'ok',
        },
    ]

    for case in cases:
        expected = _legacy_account_scan_gate(
            should_run=bool(case['should_run']),
            markets_to_run=list(case['markets_to_run']),
            symbols=list(case['symbols']),
            reason=str(case['reason']),
        )
        actual = decide_account_scan_gate(
            should_run=bool(case['should_run']),
            has_symbols=((not list(case['markets_to_run'])) or bool(list(case['symbols']))),
            reason=str(case['reason']),
        )
        assert actual == expected


def test_decide_pipeline_execution_result_matches_legacy_branching() -> None:
    from domain.domain.engine import decide_pipeline_execution_result

    for returncode in (0, 1, 2):
        expected = _legacy_pipeline_execution_result(returncode)
        actual = decide_pipeline_execution_result(returncode=returncode)
        assert actual == expected


def test_main_uses_engine_decision_entrypoints_batch3() -> None:
    base = Path(__file__).resolve().parents[1]
    main_src = (base / 'scripts' / 'multi_tick' / 'main.py').read_text(encoding='utf-8')
    account_run_src = (base / 'src' / 'application' / 'account_run.py').read_text(encoding='utf-8')
    watchdog_src = (base / 'src' / 'application' / 'multi_tick_watchdog.py').read_text(encoding='utf-8')

    # Batch-3 accepted direct decision calls; later batches may route watchdog via
    # unified engine entrypoint. Keep this guard compatible with both forms.
    assert 'resolve_multi_tick_engine_entrypoint=resolve_multi_tick_engine_entrypoint' in main_src
    assert (
        'decide_opend_unhealthy_action' in watchdog_src
        or 'resolve_multi_tick_engine_entrypoint(' in watchdog_src
    )
    assert 'decide_account_scan_gate' in account_run_src
    assert 'decide_pipeline_execution_result' in account_run_src


def test_engine_package_exports_batch3_entrypoints() -> None:
    from domain.domain.engine import (
        build_opend_unhealthy_execution_plan,
        decide_notify_dispatch_gate,
        decide_trading_day_guard,
    )

    assert callable(build_opend_unhealthy_execution_plan)
    assert callable(decide_notify_dispatch_gate)
    assert callable(decide_trading_day_guard)
