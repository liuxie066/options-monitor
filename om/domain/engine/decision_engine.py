from __future__ import annotations

from typing import Any, Callable, Mapping


def decide_opend_degrade_to_yahoo(
    *,
    allow_downgrade: bool,
    has_hk_opend: bool,
    watchdog_timed_out: bool,
) -> bool:
    """Keep current fallback gating semantics unchanged."""
    return bool(allow_downgrade and (not has_hk_opend) and (not watchdog_timed_out))


def filter_notify_candidates(results: list[Any]) -> list[Any]:
    return [r for r in results if r.should_notify and r.meaningful and bool(r.notification_text.strip())]


def rank_notify_candidates(results: list[Any]) -> list[Any]:
    """Placeholder ranking entry for stepwise extraction; preserve original order."""
    return list(results)


def build_scheduler_decision_dto(
    scheduler_raw: Any,
    *,
    normalize_fn: Callable[[Any], Mapping[str, Any]],
) -> dict[str, Any]:
    """Build scheduler decision DTO with legacy-compatible fallback shape."""
    try:
        normalized = normalize_fn(scheduler_raw)
        if isinstance(normalized, Mapping):
            return dict(normalized)
    except Exception:
        pass
    raw = scheduler_raw if isinstance(scheduler_raw, Mapping) else {}
    return {
        'schema_kind': 'scheduler_decision',
        'schema_version': '1.0',
        'should_run_scan': bool(raw.get('should_run_scan')),
        'is_notify_window_open': bool(raw.get('is_notify_window_open', raw.get('should_notify'))),
        'reason': str(raw.get('reason') or ''),
        **raw,
    }


def build_account_scheduler_decision_dto(
    account_scheduler_raw: Any,
    *,
    scheduler_decision: Mapping[str, Any],
) -> dict[str, Any]:
    """Build account-level scheduler decision DTO with global fallback."""
    account_raw = account_scheduler_raw if isinstance(account_scheduler_raw, Mapping) else {}
    return {
        'schema_kind': 'scheduler_decision_account',
        'schema_version': '1.0',
        'is_notify_window_open': bool(
            account_raw.get(
                'is_notify_window_open',
                account_raw.get(
                    'should_notify',
                    scheduler_decision.get(
                        'is_notify_window_open',
                        scheduler_decision.get('should_notify'),
                    ),
                ),
            )
        ),
        **account_raw,
    }


def decide_notify_window_open(
    *,
    scheduler_decision: Mapping[str, Any],
    account_scheduler_decision: Mapping[str, Any] | None = None,
) -> bool:
    payload = account_scheduler_decision if account_scheduler_decision is not None else scheduler_decision
    return bool(payload.get('is_notify_window_open', payload.get('should_notify')))


def decide_account_notify_window_open(
    *,
    scheduler_decision: Mapping[str, Any],
    account_scheduler_decision: Mapping[str, Any] | None = None,
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
