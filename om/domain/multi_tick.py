from __future__ import annotations

from datetime import datetime
from typing import Any, Callable


def select_markets_to_run(now_utc: datetime, cfg: dict, market_config: str) -> list[str]:
    mc = str(market_config or 'auto').lower()
    if mc == 'hk':
        return ['HK']
    if mc == 'us':
        return ['US']
    if mc == 'all':
        return ['HK', 'US']

    schedule_hk = (cfg.get('schedule_hk') or {}) if isinstance(cfg, dict) else {}
    schedule_us = (cfg.get('schedule') or {}) if isinstance(cfg, dict) else {}

    try:
        from scripts.scan_scheduler import decide

        state0: dict = {
            'last_scan_utc': None,
            'last_notify_utc': None,
        }

        d_hk = decide(schedule_hk, state0, now_utc, account=None, schedule_key='schedule_hk')
        if d_hk.in_market_hours:
            return ['HK']

        d_us = decide(schedule_us, state0, now_utc, account=None, schedule_key='schedule')
        if d_us.in_market_hours:
            return ['US']
    except Exception:
        pass

    return []


def markets_for_trading_day_guard(markets_to_run: list[str], cfg: dict, market_config: str) -> list[str]:
    """Infer pre-scan trading-day markets (US/HK/CN) for this run."""
    mc = str(market_config or 'auto').lower()
    if mc == 'hk':
        return ['HK']
    if mc == 'us':
        return ['US']
    if mc == 'all':
        return ['HK', 'US']

    try:
        mk0 = [str(m).upper() for m in (markets_to_run or []) if str(m).upper() in ('HK', 'US', 'CN')]
        if mk0:
            return mk0
    except Exception:
        pass

    try:
        syms = (cfg or {}).get('symbols') or []
        mk = sorted({str((it or {}).get('market') or '').upper() for it in syms if isinstance(it, dict) and (it or {}).get('market')})
        mk = [m for m in mk if m in ('HK', 'US', 'CN')]
        if mk:
            return mk
    except Exception:
        pass

    try:
        market_hint = str(((cfg or {}).get('portfolio') or {}).get('market') or '').strip()
        if ('港' in market_hint) or ('HK' in market_hint.upper()):
            return ['HK']
        if ('美' in market_hint) or ('US' in market_hint.upper()):
            return ['US']
        if ('A股' in market_hint) or ('CN' in market_hint.upper()):
            return ['CN']
    except Exception:
        pass

    return ['US']


def apply_scan_run_decision(*, should_run_global: bool, reason_global: str, force_mode: bool, smoke: bool) -> tuple[bool, str]:
    should_run = bool(should_run_global)
    reason = str(reason_global or '')

    if force_mode:
        should_run = True
        reason = (reason + ' | force | force: bypass guard').strip(' |')

    if smoke:
        should_run = False
        reason = (reason + ' | smoke_skip_pipeline').strip()

    return should_run, reason


def decide_should_notify(*, account: str, notify_decision_by_account: dict[str, bool], scheduler_decision: dict) -> bool:
    return bool(
        notify_decision_by_account.get(
            str(account),
            scheduler_decision.get('is_notify_window_open', scheduler_decision.get('should_notify')),
        )
    )


def filter_notify_candidates(results: list) -> list:
    return [r for r in results if r.should_notify and r.meaningful and bool(r.notification_text.strip())]


def is_in_quiet_hours_window(*, start_t, end_t, now_bj_time) -> bool:
    if start_t <= end_t:
        return start_t <= now_bj_time <= end_t
    return now_bj_time >= start_t or now_bj_time <= end_t


def evaluate_dnd_quiet_hours(
    *,
    schedule_v2_enabled: bool,
    quiet_hours: Any,
    no_send: bool,
    now_bj_time,
    parse_hhmm_fn: Callable[[str], Any],
) -> dict[str, Any]:
    out: dict[str, Any] = {
        'enabled': False,
        'quiet_window': '',
        'is_quiet': False,
        'parse_error': None,
    }
    if schedule_v2_enabled or no_send:
        return out
    if (not quiet_hours) or (not isinstance(quiet_hours, dict)):
        return out

    out['enabled'] = True
    try:
        start_t = parse_hhmm_fn(str(quiet_hours.get('start', '02:00')))
        end_t = parse_hhmm_fn(str(quiet_hours.get('end', '08:00')))
        out['quiet_window'] = f'{start_t.strftime("%H:%M")}-{end_t.strftime("%H:%M")}'
        out['is_quiet'] = bool(is_in_quiet_hours_window(start_t=start_t, end_t=end_t, now_bj_time=now_bj_time))
    except Exception as e:
        out['parse_error'] = str(e)
    return out


def decide_notify_dispatch(*, no_send: bool, target: Any, dnd_is_quiet: bool) -> dict[str, Any]:
    if dnd_is_quiet:
        return {
            'should_send': False,
            'effective_target': target,
            'config_error': None,
            'reason': 'quiet_hours',
        }

    if no_send:
        return {
            'should_send': False,
            'effective_target': None,
            'config_error': None,
            'reason': 'no_send',
        }

    if not target:
        return {
            'should_send': False,
            'effective_target': target,
            'config_error': 'notifications.target is required',
            'reason': 'config_error',
        }

    return {
        'should_send': True,
        'effective_target': target,
        'config_error': None,
        'reason': 'send',
    }
