from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence
from om.domain.tool_boundary import (
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
