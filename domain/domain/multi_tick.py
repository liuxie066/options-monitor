from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .engine import (
    AccountSchedulerDecisionView,
    SchedulerDecisionView,
    build_account_scheduler_decision_dto,
    build_scheduler_decision_dto,
    decide_account_notify_window_open,
    filter_notify_candidates as filter_notify_candidates_engine,
)

OPENCLAW_NOTIFICATION_PROVIDER = 'openclaw'
FEISHU_APP_NOTIFICATION_PROVIDER = 'feishu_app'
DEFAULT_NOTIFICATION_PROVIDER = OPENCLAW_NOTIFICATION_PROVIDER
WECHAT_CLAWBOT_NOTIFICATION_CHANNEL = 'wechat_clawbot'
OPENCLAW_WEIXIN_TRANSPORT_CHANNEL = 'openclaw-weixin'
SUPPORTED_NOTIFICATION_PROVIDERS = (
    OPENCLAW_NOTIFICATION_PROVIDER,
    FEISHU_APP_NOTIFICATION_PROVIDER,
)
SUPPORTED_NOTIFICATION_CHANNELS = (
    OPENCLAW_WEIXIN_TRANSPORT_CHANNEL,
    WECHAT_CLAWBOT_NOTIFICATION_CHANNEL,
)
OPENCLAW_NOTIFICATION_CHANNELS = (
    OPENCLAW_WEIXIN_TRANSPORT_CHANNEL,
    WECHAT_CLAWBOT_NOTIFICATION_CHANNEL,
)
OPENCLAW_TRANSPORT_CHANNEL_BY_NOTIFICATION_CHANNEL = {
    OPENCLAW_WEIXIN_TRANSPORT_CHANNEL: OPENCLAW_WEIXIN_TRANSPORT_CHANNEL,
    WECHAT_CLAWBOT_NOTIFICATION_CHANNEL: OPENCLAW_WEIXIN_TRANSPORT_CHANNEL,
}


def _resolve_watchlist_config(cfg: dict | None) -> list[dict[str, Any]]:
    data = cfg if isinstance(cfg, dict) else {}
    symbols = data.get("symbols")
    if not isinstance(symbols, list):
        return []
    return [item for item in symbols if isinstance(item, dict)]


def _parse_hhmm(value: str) -> time:
    hour, minute = str(value or "").split(":", 1)
    return time(hour=int(hour), minute=int(minute))


def _time_in_range(value: time, start: time, end: time) -> bool:
    if start <= end:
        return start <= value <= end
    return value >= start or value <= end


def _gate_cutoff_dt(*, gate: dict[str, Any], window_start_dt: datetime) -> datetime | None:
    if str(gate.get('type') or '').strip().lower() != 'before':
        return None
    gate_tz = ZoneInfo(str(gate.get('timezone') or 'Asia/Shanghai'))
    gate_time = _parse_hhmm(str(gate.get('time') or '02:00'))
    try:
        day_offset = int(gate.get('day_offset_from_window_start') or 0)
    except Exception:
        day_offset = 0
    base_date = window_start_dt.astimezone(gate_tz).date() + timedelta(days=day_offset)
    return datetime.combine(base_date, gate_time, tzinfo=gate_tz)


def _allowed_by_gates(*, now_market: datetime, window_start_dt: datetime, gates: object) -> bool:
    if not isinstance(gates, list):
        return True
    for gate in gates:
        if not isinstance(gate, dict):
            continue
        if str(gate.get('type') or '').strip().lower() != 'before':
            continue
        cutoff = _gate_cutoff_dt(gate=gate, window_start_dt=window_start_dt)
        if cutoff is not None and not (now_market.astimezone(cutoff.tzinfo) < cutoff):
            return False
    return True


_SCHEDULE_TZ_HK: tuple[str, ...] = (
    'Asia/Hong_Kong',
)


@dataclass(frozen=True)
class AutoMarketRuleEvaluation:
    schedule_key: str
    default_market: str
    configured: bool
    in_run_window: bool
    inferred_market_from_timezone: str | None
    resolved_market: str | None


def _collect_schedule_configs_for_auto_market(cfg: dict[str, Any]) -> tuple[tuple[str, dict[str, Any] | None, str], ...]:
    data = cfg if isinstance(cfg, dict) else {}
    out: list[tuple[str, dict[str, Any] | None, str]] = []
    for schedule_key, default_market in (('schedule_hk', 'HK'), ('schedule', 'US')):
        schedule_cfg = data.get(schedule_key)
        out.append((schedule_key, schedule_cfg if isinstance(schedule_cfg, dict) else None, default_market))
    return tuple(out)


def _infer_timezone_market_override(schedule_cfg: dict[str, Any]) -> str | None:
    """Return a timezone-specific market override for auto selection.

    This handles the canonical single-`schedule` case where the configured
    timezone identifies the market.
    """
    tz = str(schedule_cfg.get('timezone', '')).strip()
    if tz in _SCHEDULE_TZ_HK:
        return 'HK'
    return None


def _resolve_auto_market_for_schedule(
    *,
    schedule_key: str,
    inferred_market_from_timezone: str | None,
    default_market: str,
) -> str:
    if schedule_key == 'schedule_hk':
        return 'HK'
    if inferred_market_from_timezone:
        return inferred_market_from_timezone
    return default_market


def _evaluate_auto_market_rules(now_utc: datetime, cfg: dict[str, Any]) -> tuple[AutoMarketRuleEvaluation, ...]:
    out: list[AutoMarketRuleEvaluation] = []
    for schedule_key, schedule_cfg, default_market in _collect_schedule_configs_for_auto_market(cfg):
        configured = isinstance(schedule_cfg, dict)
        in_run_window = _is_run_window_for_schedule(now_utc, schedule_cfg) if configured else False
        inferred_market_from_timezone = (
            _infer_timezone_market_override(schedule_cfg)
            if configured else None
        )
        resolved_market = (
            _resolve_auto_market_for_schedule(
                schedule_key=schedule_key,
                inferred_market_from_timezone=inferred_market_from_timezone,
                default_market=default_market,
            )
            if in_run_window else None
        )
        out.append(
            AutoMarketRuleEvaluation(
                schedule_key=schedule_key,
                default_market=default_market,
                configured=configured,
                in_run_window=in_run_window,
                inferred_market_from_timezone=inferred_market_from_timezone,
                resolved_market=resolved_market,
            )
        )
    return tuple(out)


def _is_run_window_for_schedule(now_utc: datetime, schedule_cfg: dict[str, Any]) -> bool:
    if not isinstance(schedule_cfg, dict) or not bool(schedule_cfg.get("enabled", True)):
        return False
    market_tz = ZoneInfo(str(schedule_cfg.get("timezone") or "America/New_York"))
    now_market = now_utc.astimezone(market_tz)
    if now_market.weekday() >= 5:
        return False

    run_window = schedule_cfg.get("run_window") if isinstance(schedule_cfg.get("run_window"), dict) else {}
    run_start = _parse_hhmm(str(run_window.get("start") or "09:30"))
    run_end = _parse_hhmm(str(run_window.get("end") or "16:00"))
    current = now_market.time()
    if not (run_start <= current <= run_end):
        return False

    window_start_dt = datetime.combine(now_market.date(), run_start, tzinfo=market_tz)
    if not _allowed_by_gates(
        now_market=now_market,
        window_start_dt=window_start_dt,
        gates=schedule_cfg.get('gates'),
    ):
        return False

    breaks = run_window.get("breaks")
    if not isinstance(breaks, list):
        breaks = []
    for item in breaks:
        if not isinstance(item, dict) or not item.get("start") or not item.get("end"):
            continue
        break_start = _parse_hhmm(str(item.get("start")))
        break_end = _parse_hhmm(str(item.get("end")))
        if _time_in_range(current, break_start, break_end) and current != break_end:
            return False
    return True


def select_markets_to_run(now_utc: datetime, cfg: dict, market_config: str) -> list[str]:
    mc = str(market_config or 'auto').lower()
    if mc == 'hk':
        return ['HK']
    if mc == 'us':
        return ['US']
    if mc == 'all':
        return ['HK', 'US']

    for evaluation in _evaluate_auto_market_rules(now_utc, cfg):
        if evaluation.resolved_market:
            return [evaluation.resolved_market]

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
    if no_send:
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
    default_channel: str = OPENCLAW_WEIXIN_TRANSPORT_CHANNEL,
    default_provider: str = DEFAULT_NOTIFICATION_PROVIDER,
) -> dict[str, Any]:
    """Resolve channel/target with compat defaults at domain boundary."""
    notif_cfg = notifications if isinstance(notifications, dict) else {}
    cli_provider = normalize_notification_provider(cli_channel, default_provider=None)
    cfg_channel_provider = normalize_notification_provider(notif_cfg.get('channel'), default_provider=None)
    provider = normalize_notification_provider(
        notif_cfg.get('provider')
        or (cli_provider if cli_provider in SUPPORTED_NOTIFICATION_PROVIDERS else None)
        or (cfg_channel_provider if cfg_channel_provider in SUPPORTED_NOTIFICATION_PROVIDERS else None)
        or default_provider
    )
    raw_channel = cli_channel or notif_cfg.get('transport_channel') or notif_cfg.get('channel') or default_channel
    if provider == OPENCLAW_NOTIFICATION_PROVIDER:
        channel = resolve_openclaw_transport_channel(raw_channel)
    else:
        channel = FEISHU_APP_NOTIFICATION_PROVIDER
    return {
        'provider': provider,
        'channel': channel,
        'target': (cli_target or notif_cfg.get('target')),
    }


def normalize_notification_provider(provider: Any, *, default_provider: str | None = DEFAULT_NOTIFICATION_PROVIDER) -> str:
    value = str(provider or default_provider or '').strip().lower()
    if value in OPENCLAW_NOTIFICATION_CHANNELS or value == OPENCLAW_NOTIFICATION_PROVIDER:
        return OPENCLAW_NOTIFICATION_PROVIDER
    if value == FEISHU_APP_NOTIFICATION_PROVIDER:
        return FEISHU_APP_NOTIFICATION_PROVIDER
    return value


def normalize_notification_channel(channel: Any, *, default_channel: str | None = None) -> str:
    value = str(channel or default_channel or '').strip().lower()
    return value


def is_supported_notification_provider(provider: Any) -> bool:
    return normalize_notification_provider(provider) in SUPPORTED_NOTIFICATION_PROVIDERS


def is_supported_notification_channel(channel: Any) -> bool:
    return normalize_notification_channel(channel) in SUPPORTED_NOTIFICATION_CHANNELS


def is_openclaw_notification_channel(channel: Any) -> bool:
    return normalize_notification_channel(channel) in OPENCLAW_NOTIFICATION_CHANNELS


def resolve_openclaw_transport_channel(channel: Any) -> str:
    value = normalize_notification_channel(channel)
    if value in OPENCLAW_TRANSPORT_CHANNEL_BY_NOTIFICATION_CHANNEL:
        return OPENCLAW_TRANSPORT_CHANNEL_BY_NOTIFICATION_CHANNEL[value]
    return str(channel or '').strip()


def resolve_notification_route_from_config(
    *,
    config: Any,
    cli_channel: Any = None,
    cli_target: Any = None,
    default_channel: str = OPENCLAW_WEIXIN_TRANSPORT_CHANNEL,
    default_provider: str = DEFAULT_NOTIFICATION_PROVIDER,
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
        default_provider=default_provider,
    )
    return {
        'notifications': notif_cfg,
        'provider': route.get('provider'),
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
        resolved = state if state.is_absolute() else (base_dir / state)
        # Guard against path traversal: resolved path must stay within base_dir
        base_resolved = base_dir if base_dir.is_absolute() else base_dir.absolute()
        resolved_real = resolved if resolved.is_absolute() else resolved.absolute()
        try:
            resolved_real.relative_to(base_resolved)
        except ValueError:
            raise ValueError(f"state_override escapes base_dir: {state_override}")
        return resolved

    state_root = Path(state_dir)
    if not state_root.is_absolute():
        state_root = (base_dir / state_root)
    return state_root / str(filename)
