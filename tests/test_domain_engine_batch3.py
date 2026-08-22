from __future__ import annotations

from pathlib import Path


def test_decide_opend_unhealthy_action_matches_legacy_branching() -> None:
    from domain.domain.engine import decide_opend_unhealthy_action

    cases = (
        ('OPEND_NEEDS_PHONE_VERIFY', False, 'pause_phone_verify', True, False),
        ('OPEND_NEEDS_PHONE_VERIFY', True, 'pause_phone_verify', True, False),
        ('OPEND_API_ERROR', False, 'abort', True, False),
        ('OPEND_API_ERROR', True, 'degrade_continue', False, True),
    )
    for error_code, degraded, action, terminal, fallback_used in cases:
        assert decide_opend_unhealthy_action(error_code=error_code, degraded=degraded) == {
            'action': action,
            'terminal': terminal,
            'fallback_used': fallback_used,
        }


def test_decide_account_scan_gate_matches_legacy_branching() -> None:
    from domain.domain.engine import decide_account_scan_gate

    cases = (
        (False, True, 'interval_not_due', False, False, False, 'interval_not_due'),
        (True, False, 'ok', False, False, False, 'ok | 本时段无对应市场标的'),
        (True, True, 'ok', True, True, None, 'ok'),
    )
    for should_run, has_symbols, reason, run_pipeline, ran_scan, meaningful, result_reason in cases:
        assert decide_account_scan_gate(
            should_run=should_run,
            has_symbols=has_symbols,
            reason=reason,
        ) == {
            'run_pipeline': run_pipeline,
            'ran_scan': ran_scan,
            'meaningful': meaningful,
            'result_reason': result_reason,
        }


def test_decide_pipeline_execution_result_matches_legacy_branching() -> None:
    from domain.domain.engine import decide_pipeline_execution_result

    for returncode, ok, meaningful, reason in (
        (0, True, None, ''),
        (1, False, False, 'pipeline failed'),
        (2, False, False, 'pipeline failed'),
    ):
        assert decide_pipeline_execution_result(returncode=returncode) == {
            'ok': ok,
            'ran_scan': True,
            'meaningful': meaningful,
            'reason': reason,
        }


def test_main_uses_engine_decision_entrypoints_batch3() -> None:
    base = Path(__file__).resolve().parents[1]
    scheduler_context_src = (base / 'src' / 'application' / 'tick_scheduler_context.py').read_text(encoding='utf-8')
    account_run_src = (base / 'src' / 'application' / 'account_run.py').read_text(encoding='utf-8')
    watchdog_src = (base / 'src' / 'application' / 'multi_tick_watchdog.py').read_text(encoding='utf-8')

    # Batch-3 accepted direct decision calls; later batches may route watchdog via
    # unified engine entrypoint. Keep this guard compatible with both forms.
    assert 'engine_entrypoint=resolve_multi_tick_engine_entrypoint' in scheduler_context_src
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
