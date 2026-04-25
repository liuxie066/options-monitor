from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .engine import (
    AccountSchedulerDecisionView,
    SchedulerDecisionView,
    build_account_scheduler_decision_dto,
    build_scheduler_decision_dto,
    decide_account_notify_window_open,
    filter_notify_candidates as filter_notify_candidates_engine,
)


def _resolve_watchlist_config(cfg: dict | None) -> list[dict[str, Any]]:
    data = cfg if isinstance(cfg, dict) else {}
    symbols = data.get("symbols")
    if not isinstance(symbols, list):
        return []
    return [item for item in symbols if isinstance(item, dict)]


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
        syms = _resolve_watchlist_config(cfg)
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


def decide_should_notify(
    *,
    account: str,
    notify_decision_by_account: dict[str, bool | dict[str, Any] | AccountSchedulerDecisionView],
    scheduler_decision: dict | SchedulerDecisionView,
) -> bool:
    if isinstance(scheduler_decision, SchedulerDecisionView):
        scheduler_view = scheduler_decision
    elif isinstance(scheduler_decision, dict) and str(scheduler_decision.get('schema_kind') or '') == 'scheduler_decision':
        scheduler_view = SchedulerDecisionView.from_payload(scheduler_decision)
    else:
        # Keep scheduler legacy-compat reads centralized in decision DTO builder.
        scheduler_view = SchedulerDecisionView.from_payload(
            build_scheduler_decision_dto(scheduler_decision)
        )
    account_decision_raw = notify_decision_by_account.get(str(account))
    account_decision: AccountSchedulerDecisionView | None
    if account_decision_raw is None or isinstance(account_decision_raw, AccountSchedulerDecisionView):
        account_decision = account_decision_raw
    elif isinstance(account_decision_raw, dict) and str(account_decision_raw.get('schema_kind') or '') == 'scheduler_decision_account':
        account_decision = AccountSchedulerDecisionView.from_payload(
            account_decision_raw,
            scheduler_decision=scheduler_view,
        )
    else:
        account_decision_dto = build_account_scheduler_decision_dto(
            account_decision_raw,
            scheduler_decision=scheduler_view,
        )
        account_decision = AccountSchedulerDecisionView(
            is_notify_window_open=bool(account_decision_dto.get('is_notify_window_open')),
        )
    return bool(
        decide_account_notify_window_open(
            scheduler_decision=scheduler_view,
            account_scheduler_decision=account_decision,
        )
    )


def filter_notify_candidates(results: list) -> list:
    return filter_notify_candidates_engine(results)


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


def cash_footer_for_account(cash_footer_lines: list[str], account: str) -> list[str]:
    if not cash_footer_lines:
        return []
    acct = str(account).strip().upper()
    out: list[str] = []
    matched = False
    asof_line = ''
    for ln in cash_footer_lines:
        s = str(ln)
        if s.startswith('**💰 现金 CNY**'):
            out.append(s)
            continue
        if s.startswith('> 截至 '):
            asof_line = s
            continue
        if s.startswith(f'- **{acct}**'):
            out.append(s)
            matched = True
            continue
    if matched and asof_line:
        out.append('')
        out.append(asof_line)
    return out if matched else []


def reduce_trading_day_guard(
    *,
    markets_to_run: list[str],
    guard_results: list[dict[str, Any]],
) -> dict[str, Any]:
    false_markets = [str(r.get('market')) for r in guard_results if r.get('is_trading_day') is False]
    true_markets = [str(r.get('market')) for r in guard_results if r.get('is_trading_day') is True]

    if false_markets:
        if markets_to_run:
            narrowed = [m for m in markets_to_run if m not in set(false_markets)]
            if not narrowed:
                return {
                    'should_skip': True,
                    'markets_to_run': [],
                    'skip_message': f"non-trading day: {','.join(false_markets)}",
                }
            return {
                'should_skip': False,
                'markets_to_run': narrowed,
                'skip_message': '',
            }
        if true_markets:
            return {
                'should_skip': False,
                'markets_to_run': sorted({m for m in true_markets if m in ('HK', 'US', 'CN')}),
                'skip_message': '',
            }
        return {
            'should_skip': True,
            'markets_to_run': [],
            'skip_message': f"non-trading day: {','.join(false_markets)}",
        }

    return {
        'should_skip': False,
        'markets_to_run': list(markets_to_run or []),
        'skip_message': '',
    }


def select_scheduler_state_filename(markets_to_run: list[str]) -> str:
    if markets_to_run == ['HK']:
        return 'scheduler_state_hk.json'
    if markets_to_run == ['US']:
        return 'scheduler_state_us.json'
    return 'scheduler_state.json'


def resolve_notification_channel_target(
    *,
    notifications: Any,
    cli_channel: Any = None,
    cli_target: Any = None,
    default_channel: str = 'feishu',
) -> dict[str, Any]:
    """Resolve channel/target with compat defaults at domain boundary."""
    notif_cfg = notifications if isinstance(notifications, dict) else {}
    return {
        'channel': (cli_channel or notif_cfg.get('channel') or default_channel),
        'target': (cli_target or notif_cfg.get('target')),
    }


def resolve_notification_route_from_config(
    *,
    config: Any,
    cli_channel: Any = None,
    cli_target: Any = None,
    default_channel: str = 'feishu',
) -> dict[str, Any]:
    """Resolve notification route while centralizing config notifications fallback reads."""
    cfg = config if isinstance(config, dict) else {}
    notifications = cfg.get('notifications')
    notif_cfg = notifications if isinstance(notifications, dict) else {}
    route = resolve_notification_channel_target(
        notifications=notif_cfg,
        cli_channel=cli_channel,
        cli_target=cli_target,
        default_channel=default_channel,
    )
    return {
        'notifications': notif_cfg,
        'channel': route.get('channel'),
        'target': route.get('target'),
    }


def resolve_scheduler_state_path(
    *,
    base_dir: Path,
    state_dir: str | Path,
    state_override: str | Path | None,
    filename: str = 'scheduler_state.json',
) -> Path:
    """Resolve scheduler state path while centralizing legacy --state override."""
    if state_override:
        state = Path(state_override)
        resolved = state if state.is_absolute() else (base_dir / state).resolve()
        # Guard against path traversal: resolved path must stay within base_dir
        base_resolved = base_dir.resolve()
        resolved_real = resolved.resolve()
        try:
            resolved_real.relative_to(base_resolved)
        except ValueError:
            raise ValueError(f"state_override escapes base_dir: {state_override}")
        return resolved

    state_root = Path(state_dir)
    if not state_root.is_absolute():
        state_root = (base_dir / state_root).resolve()
    return (state_root / str(filename)).resolve()
