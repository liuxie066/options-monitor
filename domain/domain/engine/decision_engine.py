from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Sequence
from domain.domain.tool_boundary import (
    normalize_notify_window_aliases,
    normalize_scheduler_decision_payload,
    resolve_notify_window_open,
)


@dataclass(frozen=True)
class SchedulerDecisionView:
    should_run_scan: bool
    is_notify_window_open: bool
    reason: str

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any] | Any) -> 'SchedulerDecisionView':
        src = normalize_notify_window_aliases(payload)
        return cls(
            should_run_scan=bool(src.get('should_run_scan')),
            is_notify_window_open=resolve_notify_window_open(src),
            reason=str(src.get('reason') or ''),
        )


@dataclass(frozen=True)
class AccountSchedulerDecisionView:
    is_notify_window_open: bool

    @classmethod
    def from_payload(
        cls,
        payload: Mapping[str, Any] | Any,
        *,
        scheduler_decision: Mapping[str, Any] | SchedulerDecisionView,
    ) -> 'AccountSchedulerDecisionView':
        scheduler_view = (
            scheduler_decision
            if isinstance(scheduler_decision, SchedulerDecisionView)
            else SchedulerDecisionView.from_payload(scheduler_decision)
        )
        src = normalize_notify_window_aliases(
            payload,
            default=bool(scheduler_view.is_notify_window_open),
        )
        return cls(
            is_notify_window_open=resolve_notify_window_open(
                src,
                default=bool(scheduler_view.is_notify_window_open),
            ),
        )


def decide_opend_degrade_to_yahoo(
    *,
    allow_downgrade: bool,
    has_hk_opend: bool,
    watchdog_timed_out: bool,
) -> bool:
    """Keep current fallback gating semantics unchanged."""
    return bool(allow_downgrade and (not has_hk_opend) and (not watchdog_timed_out))


def apply_opend_degrade_to_yahoo(
    *,
    symbols: Sequence[Any],
    allow_downgrade: bool,
    has_hk_opend: bool,
    watchdog_timed_out: bool,
) -> bool:
    """Apply per-run OpenD->Yahoo fallback with unchanged legacy predicates."""
    if not decide_opend_degrade_to_yahoo(
        allow_downgrade=allow_downgrade,
        has_hk_opend=has_hk_opend,
        watchdog_timed_out=watchdog_timed_out,
    ):
        return False

    degraded = False
    for sym in (symbols or []):
        if not isinstance(sym, dict):
            continue
        if str(sym.get('market') or '').upper() != 'US':
            continue
        fetch = sym.get('fetch')
        if not isinstance(fetch, dict):
            continue
        if str(fetch.get('source') or '').lower() != 'opend':
            continue
        fetch['source'] = 'yahoo'
        for k in ('host', 'port', 'spot_from_portfolio_management'):
            fetch.pop(k, None)
        sym['fetch'] = fetch
        degraded = True
    return degraded


def score_notify_candidate(result: Any) -> int:
    """Keep v1 scoring neutral to preserve ordering semantics."""
    return 1 if bool(str(getattr(result, 'notification_text', '') or '').strip()) else 0


def filter_notify_candidates(results: list[Any]) -> list[Any]:
    return [r for r in results if r.should_notify and r.meaningful and bool(r.notification_text.strip())]


def rank_notify_candidates(results: list[Any]) -> list[Any]:
    """Compute candidate scores but keep legacy stable order for v1 semantics."""
    scored = [(idx, score_notify_candidate(r), r) for idx, r in enumerate(results or [])]
    scored.sort(key=lambda it: (-int(it[1]), int(it[0])))
    return [it[2] for it in scored]


def decide_notify_threshold_met(
    account_messages: Mapping[str, str] | Any,
    *,
    min_accounts: int = 1,
) -> bool:
    try:
        required = max(1, int(min_accounts))
    except Exception:
        required = 1
    if not isinstance(account_messages, Mapping):
        return False
    count = sum(1 for _acct, msg in account_messages.items() if bool(str(msg or '').strip()))
    return count >= required


def build_scheduler_decision_dto(
    scheduler_raw: Any,
    *,
    normalize_fn: Callable[[Any], Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build scheduler decision DTO with legacy-compatible fallback shape."""
    try:
        resolved_normalize_fn = normalize_fn or normalize_scheduler_decision_payload
        normalized = resolved_normalize_fn(scheduler_raw)
        if isinstance(normalized, Mapping):
            return dict(normalized)
    except Exception:
        pass
    raw = normalize_notify_window_aliases(scheduler_raw)
    return {
        'schema_kind': 'scheduler_decision',
        'schema_version': '1.0',
        'should_run_scan': bool(raw.get('should_run_scan')),
        'is_notify_window_open': bool(raw.get('is_notify_window_open')),
        'reason': str(raw.get('reason') or ''),
        **raw,
    }


def resolve_scheduler_decision(
    scheduler_raw: Any,
    *,
    normalize_fn: Callable[[Any], Mapping[str, Any]] | None = None,
) -> tuple[dict[str, Any], SchedulerDecisionView]:
    """Single entrypoint for scheduler legacy/alias reads at domain boundary."""
    scheduler_decision = build_scheduler_decision_dto(
        scheduler_raw,
        normalize_fn=normalize_fn,
    )
    scheduler_view = SchedulerDecisionView.from_payload(scheduler_decision)
    return scheduler_decision, scheduler_view


def build_account_scheduler_decision_dto(
    account_scheduler_raw: Any,
    *,
    scheduler_decision: Mapping[str, Any] | SchedulerDecisionView,
) -> dict[str, Any]:
    """Build account-level scheduler decision DTO with global fallback."""
    scheduler_view = (
        scheduler_decision
        if isinstance(scheduler_decision, SchedulerDecisionView)
        else SchedulerDecisionView.from_payload(scheduler_decision)
    )
    if isinstance(account_scheduler_raw, bool):
        account_scheduler_raw = {'is_notify_window_open': bool(account_scheduler_raw)}
    account_raw = normalize_notify_window_aliases(
        account_scheduler_raw,
        default=bool(scheduler_view.is_notify_window_open),
    )
    account_view = AccountSchedulerDecisionView.from_payload(account_raw, scheduler_decision=scheduler_view)
    return {
        'schema_kind': 'scheduler_decision_account',
        'schema_version': '1.0',
        'is_notify_window_open': bool(account_view.is_notify_window_open),
        **account_raw,
    }


def decide_notify_window_open(
    *,
    scheduler_decision: Mapping[str, Any] | SchedulerDecisionView,
    account_scheduler_decision: Mapping[str, Any] | AccountSchedulerDecisionView | None = None,
) -> bool:
    scheduler_view = (
        scheduler_decision
        if isinstance(scheduler_decision, SchedulerDecisionView)
        else SchedulerDecisionView.from_payload(scheduler_decision)
    )
    if account_scheduler_decision is None:
        return bool(scheduler_view.is_notify_window_open)
    if isinstance(account_scheduler_decision, AccountSchedulerDecisionView):
        return bool(account_scheduler_decision.is_notify_window_open)
    account_view = AccountSchedulerDecisionView.from_payload(
        account_scheduler_decision,
        scheduler_decision=scheduler_view,
    )
    return bool(account_view.is_notify_window_open)


def decide_account_notify_window_open(
    *,
    scheduler_decision: Mapping[str, Any] | SchedulerDecisionView,
    account_scheduler_decision: Mapping[str, Any] | AccountSchedulerDecisionView | None = None,
) -> bool:
    """Single decision entry used by orchestrator with explicit DTO input."""
    return decide_notify_window_open(
        scheduler_decision=scheduler_decision,
        account_scheduler_decision=account_scheduler_decision,
    )


def decide_notification_meaningful(
    notification_text: str,
    *,
    empty_placeholder: str = '今日无需要主动提醒的内容。',
) -> bool:
    return bool(notification_text) and (notification_text != empty_placeholder)


def decide_scheduler_timing(
    *,
    now_utc: datetime,
    last_scan_utc: datetime | None,
    last_notify_utc: datetime | None,
    in_window: bool,
    monitor_off_hours: bool,
    interval_min: int,
    notify_cooldown_min: int,
    schedule_v2_enabled: bool = False,
    force_final_scan: bool = False,
    off_window_notify: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    """Pure scheduler timing decision; callers keep state IO and config parsing."""
    now = now_utc.astimezone(timezone.utc)
    interval = int(interval_min)
    cooldown = int(notify_cooldown_min)

    if bool(force):
        return {
            'should_run_scan': True,
            'is_notify_window_open': True,
            'reason': 'force 模式：忽略频率控制直接执行。',
            'next_run_utc': now,
        }

    if (not bool(in_window)) and (not bool(monitor_off_hours)):
        should_run_scan = False
        reason = '窗口外：不扫描。'
    elif last_scan_utc is None:
        should_run_scan = True
        reason = '首次运行，无历史扫描记录。'
    else:
        elapsed = now - last_scan_utc.astimezone(timezone.utc)
        if elapsed >= timedelta(minutes=interval):
            should_run_scan = True
            reason = f'距离上次扫描已超过 {interval} 分钟。'
        elif bool(schedule_v2_enabled) and bool(force_final_scan):
            should_run_scan = True
            reason = '窗口收盘前最后一跳：强制扫描。'
        else:
            should_run_scan = False
            reason = f'距离上次扫描不足 {interval} 分钟。'

    if bool(schedule_v2_enabled):
        if (not bool(in_window)) and (not bool(off_window_notify)):
            is_notify_window_open = False
        elif last_notify_utc is None:
            is_notify_window_open = True
        else:
            is_notify_window_open = (
                now - last_notify_utc.astimezone(timezone.utc)
            ) >= timedelta(minutes=cooldown)
    elif (not bool(in_window)) and (not bool(monitor_off_hours)):
        is_notify_window_open = False
    elif last_notify_utc is None:
        is_notify_window_open = True
    else:
        is_notify_window_open = (
            now - last_notify_utc.astimezone(timezone.utc)
        ) >= timedelta(minutes=cooldown)

    if not should_run_scan:
        if last_scan_utc is None or interval >= 10**8:
            next_run = now + timedelta(hours=24)
        else:
            next_run = last_scan_utc.astimezone(timezone.utc) + timedelta(minutes=interval)
    else:
        next_run = now

    return {
        'should_run_scan': bool(should_run_scan),
        'is_notify_window_open': bool(is_notify_window_open),
        'reason': str(reason),
        'next_run_utc': next_run,
    }


def decide_notify_dispatch_gate(
    *,
    dispatch_decision: Mapping[str, Any] | Any,
    dnd_decision: Mapping[str, Any] | Any = None,
) -> dict[str, Any]:
    """Centralize notification dispatch gate branching from orchestrator."""
    dispatch = dispatch_decision if isinstance(dispatch_decision, Mapping) else {}
    dnd = dnd_decision if isinstance(dnd_decision, Mapping) else {}
    reason = str(dispatch.get('reason') or '')
    config_error = dispatch.get('config_error')
    should_send = bool(dispatch.get('should_send'))
    effective_target = dispatch.get('effective_target')
    quiet_window = str(dnd.get('quiet_window') or '')

    if reason == 'quiet_hours':
        return {
            'action': 'skip_quiet_hours',
            'reason': reason,
            'should_send': False,
            'effective_target': effective_target,
            'config_error': None,
            'quiet_window': quiet_window,
        }

    if config_error:
        return {
            'action': 'config_error',
            'reason': reason,
            'should_send': False,
            'effective_target': effective_target,
            'config_error': config_error,
            'quiet_window': quiet_window,
        }

    if should_send:
        return {
            'action': 'send',
            'reason': reason,
            'should_send': True,
            'effective_target': effective_target,
            'config_error': None,
            'quiet_window': quiet_window,
        }

    return {
        'action': 'skip',
        'reason': reason,
        'should_send': False,
        'effective_target': effective_target,
        'config_error': None,
        'quiet_window': quiet_window,
    }


def decide_notify_delivery_action(
    *,
    dispatch_gate: Mapping[str, Any] | Any,
) -> dict[str, Any]:
    """Map notify dispatch gate into orchestration actions without inline policy checks."""
    gate = dispatch_gate if isinstance(dispatch_gate, Mapping) else {}
    action = str(gate.get('action') or '')
    if action == 'skip_quiet_hours':
        return {
            'action': 'skip_quiet_hours',
            'should_send': False,
            'config_error': None,
            'effective_target': gate.get('effective_target'),
            'reason': str(gate.get('reason') or ''),
            'quiet_window': str(gate.get('quiet_window') or ''),
        }
    if action == 'config_error':
        return {
            'action': 'config_error',
            'should_send': False,
            'config_error': gate.get('config_error'),
            'effective_target': gate.get('effective_target'),
            'reason': str(gate.get('reason') or ''),
            'quiet_window': str(gate.get('quiet_window') or ''),
        }
    if action == 'send':
        return {
            'action': 'send',
            'should_send': True,
            'config_error': None,
            'effective_target': gate.get('effective_target'),
            'reason': str(gate.get('reason') or ''),
            'quiet_window': str(gate.get('quiet_window') or ''),
        }
    return {
        'action': 'skip',
        'should_send': False,
        'config_error': None,
        'effective_target': gate.get('effective_target'),
        'reason': str(gate.get('reason') or ''),
        'quiet_window': str(gate.get('quiet_window') or ''),
    }


def decide_notification_delivery(
    *,
    should_notify_window: bool,
    notification_text: str,
    target: Any,
    no_send: bool = False,
    is_quiet: bool = False,
    quiet_window: str = '',
    empty_placeholder: str = '今日无需要主动提醒的内容。',
) -> dict[str, Any]:
    """Single delivery policy entrypoint for notification orchestration."""
    meaningful = decide_notification_meaningful(
        str(notification_text or ''),
        empty_placeholder=empty_placeholder,
    )
    effective_target = target

    if bool(is_quiet):
        return {
            'action': 'skip_quiet_hours',
            'should_send': False,
            'meaningful': bool(meaningful),
            'effective_target': effective_target,
            'config_error': None,
            'reason': 'quiet_hours',
            'quiet_window': str(quiet_window or ''),
        }

    if bool(no_send):
        return {
            'action': 'skip',
            'should_send': False,
            'meaningful': bool(meaningful),
            'effective_target': None,
            'config_error': None,
            'reason': 'no_send',
        }

    if not target:
        return {
            'action': 'config_error',
            'should_send': False,
            'meaningful': bool(meaningful),
            'effective_target': effective_target,
            'config_error': 'notifications.target is required',
            'reason': 'config_error',
        }

    if not bool(should_notify_window):
        return {
            'action': 'skip',
            'should_send': False,
            'meaningful': bool(meaningful),
            'effective_target': effective_target,
            'config_error': None,
            'reason': 'notify_window_closed',
        }

    if not bool(meaningful):
        return {
            'action': 'skip',
            'should_send': False,
            'meaningful': False,
            'effective_target': effective_target,
            'config_error': None,
            'reason': 'not_meaningful',
        }

    return {
        'action': 'send',
        'should_send': True,
        'meaningful': True,
        'effective_target': effective_target,
        'config_error': None,
        'reason': 'send',
    }


def decide_opend_unhealthy_action(
    *,
    error_code: str,
    degraded: bool,
) -> dict[str, Any]:
    code = str(error_code or 'OPEND_API_ERROR')
    if code == 'OPEND_NEEDS_PHONE_VERIFY':
        return {
            'action': 'pause_phone_verify',
            'terminal': True,
            'fallback_used': False,
        }
    if bool(degraded):
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


def build_opend_unhealthy_execution_plan(
    *,
    error_code: str,
    degraded: bool,
    message_text: str,
    detail_text: str,
    host: Any,
    port: Any,
) -> dict[str, Any]:
    """Build side-effect plan for OpenD unhealthy handling without IO."""
    action = decide_opend_unhealthy_action(
        error_code=error_code,
        degraded=degraded,
    )
    action_name = str(action.get('action') or '')
    is_pause = action_name == 'pause_phone_verify'
    is_degrade = action_name == 'degrade_continue'
    detail = (
        f'{host}:{port} {detail_text}'
        if host is not None and port is not None
        else str(detail_text or '')
    )
    message = str(message_text or '')
    if is_pause:
        message = message + '（已暂停：等待你在飞书确认后再继续）'
    return {
        **action,
        'alert_message_text': message,
        'alert_detail': detail,
        'should_mark_phone_verify_pending': is_pause,
        'should_write_account_last_run': (not is_pause),
        'should_continue': is_degrade,
    }


def decide_account_scan_gate(
    *,
    should_run: bool,
    has_symbols: bool,
    reason: str,
    no_symbols_suffix: str = '本时段无对应市场标的',
) -> dict[str, Any]:
    if not bool(should_run):
        return {
            'run_pipeline': False,
            'ran_scan': False,
            'meaningful': False,
            'result_reason': str(reason or ''),
        }
    if not bool(has_symbols):
        reason0 = str(reason or '').strip()
        msg = f'{reason0} | {no_symbols_suffix}'.strip(' |')
        return {
            'run_pipeline': False,
            'ran_scan': False,
            'meaningful': False,
            'result_reason': msg,
        }
    return {
        'run_pipeline': True,
        'ran_scan': True,
        'meaningful': None,
        'result_reason': str(reason or ''),
    }


def decide_pipeline_execution_result(
    *,
    returncode: int,
    failed_reason: str = 'pipeline failed',
) -> dict[str, Any]:
    ok = int(returncode) == 0
    if ok:
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
        'reason': str(failed_reason or 'pipeline failed'),
    }


def build_failure_audit_fields(
    *,
    failure_kind: str,
    failure_stage: str,
    failure_adapter: str | None = None,
) -> dict[str, Any]:
    kind = str(failure_kind or '').strip().lower()
    if kind not in {'io_error', 'decision_error'}:
        kind = 'io_error'
    out: dict[str, Any] = {
        'failure_kind': kind,
        'failure_stage': str(failure_stage or '').strip(),
    }
    if failure_adapter:
        out['failure_adapter'] = str(failure_adapter).strip()
    return out


def decide_trading_day_guard(
    *,
    markets_to_run: list[str],
    guard_markets: list[str],
    check_trading_day_for_market: Callable[[str], tuple[bool | None, str]],
    reduce_guard_fn: Callable[..., Mapping[str, Any]],
) -> dict[str, Any]:
    """Centralize trading-day guard decision while keeping legacy semantics."""
    guard_results: list[dict[str, Any]] = []
    for gm in (guard_markets or []):
        is_td, gm_used = check_trading_day_for_market(str(gm))
        guard_results.append({'market': gm_used, 'is_trading_day': is_td})

    reduced = reduce_guard_fn(
        markets_to_run=list(markets_to_run or []),
        guard_results=guard_results,
    )
    return {
        'guard_results': guard_results,
        'markets_to_run': list(reduced.get('markets_to_run') or []),
        'should_skip': bool(reduced.get('should_skip')),
        'skip_message': str(reduced.get('skip_message') or ''),
    }


def resolve_multi_tick_engine_entrypoint(
    *,
    scheduler_raw: Any | None = None,
    account_scheduler_raw_by_account: Mapping[str, Any] | None = None,
    opend_unhealthy: Mapping[str, Any] | None = None,
    notify_dispatch: Mapping[str, Any] | Any = None,
    dnd_decision: Mapping[str, Any] | Any = None,
    notify_account_messages: Mapping[str, str] | Any = None,
    notify_min_accounts: int | Any = 1,
    notify_dispatch_gate: Mapping[str, Any] | Any = None,
    normalize_fn: Callable[[Any], Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Single engine entrypoint for scheduler/watchdog/notify decision resolution."""
    out: dict[str, Any] = {}
    account_scheduler_map = (
        account_scheduler_raw_by_account if isinstance(account_scheduler_raw_by_account, Mapping) else {}
    )
    opend_unhealthy_map = opend_unhealthy if isinstance(opend_unhealthy, Mapping) else {}
    has_opend_unhealthy = opend_unhealthy is not None

    if scheduler_raw is not None:
        scheduler_decision, scheduler_view = resolve_scheduler_decision(
            scheduler_raw,
            normalize_fn=normalize_fn,
        )
        scheduler_bundle: dict[str, Any] = {
            'scheduler_decision': scheduler_decision,
            'scheduler_view': scheduler_view,
            'account_scheduler_decisions': {},
            'account_scheduler_views': {},
        }
        for acct, raw in account_scheduler_map.items():
            acct_key = str(acct)
            account_decision_dto = build_account_scheduler_decision_dto(
                raw,
                scheduler_decision=scheduler_view,
            )
            account_view = AccountSchedulerDecisionView.from_payload(
                account_decision_dto,
                scheduler_decision=scheduler_view,
            )
            scheduler_bundle['account_scheduler_decisions'][acct_key] = account_decision_dto
            scheduler_bundle['account_scheduler_views'][acct_key] = account_view
        out['scheduler'] = scheduler_bundle

    if has_opend_unhealthy:
        out['watchdog'] = build_opend_unhealthy_execution_plan(
            error_code=str(opend_unhealthy_map.get('error_code') or 'OPEND_API_ERROR'),
            degraded=bool(opend_unhealthy_map.get('degraded')),
            message_text=str(opend_unhealthy_map.get('message_text') or ''),
            detail_text=str(opend_unhealthy_map.get('detail_text') or ''),
            host=opend_unhealthy_map.get('host'),
            port=opend_unhealthy_map.get('port'),
        )

    if (notify_dispatch is not None) or (dnd_decision is not None):
        out['notify'] = decide_notify_dispatch_gate(
            dispatch_decision=notify_dispatch or {},
            dnd_decision=dnd_decision or {},
        )

    if notify_account_messages is not None:
        out['notify_threshold'] = {
            'threshold_met': decide_notify_threshold_met(
                notify_account_messages,
                min_accounts=notify_min_accounts,
            ),
            'min_accounts': notify_min_accounts,
        }
    if notify_dispatch_gate is not None:
        out['notify_delivery'] = decide_notify_delivery_action(
            dispatch_gate=notify_dispatch_gate,
        )

    return out
