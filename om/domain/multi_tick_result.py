from __future__ import annotations

from typing import Any, Callable


def build_account_messages(
    *,
    notify_candidates: list,
    now_bj,
    cash_footer_lines: list[str],
    cash_footer_for_account_fn: Callable[[list[str], str], list[str]],
    build_account_message_fn: Callable[..., str],
) -> dict[str, str]:
    out: dict[str, str] = {}
    for r in (notify_candidates or []):
        msg = build_account_message_fn(
            r,
            now_bj=now_bj,
            cash_footer_lines=cash_footer_for_account_fn(cash_footer_lines, r.account),
        )
        if msg:
            out[str(r.account)] = msg
    return out


def build_no_account_notification_payloads(
    *,
    now_utc_fn: Callable[[], str],
    results: list,
    run_dir: str,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    shared_now = now_utc_fn()
    shared_payload = {
        'last_run_utc': shared_now,
        'sent': False,
        'reason': 'no_account_notification',
        'accounts': [r.account for r in results],
        'results': [r.__dict__ for r in results],
    }
    account_payloads: dict[str, dict[str, Any]] = {}
    for r in results:
        account_payloads[str(r.account)] = {
            'last_run_utc': now_utc_fn(),
            'sent': False,
            'reason': 'no_account_notification',
            'account': r.account,
            'result': r.__dict__,
            'run_dir': str(run_dir),
        }
    return shared_payload, account_payloads


def build_shared_last_run_payload(
    *,
    prev_payload: dict[str, Any] | Any,
    run_meta: dict[str, Any],
    history_limit: int = 20,
) -> dict[str, Any]:
    prev = prev_payload if isinstance(prev_payload, dict) else {}
    hist = prev.get('history')
    if not isinstance(hist, list):
        hist = []
    hist.append(run_meta)
    if history_limit > 0:
        hist = hist[-int(history_limit):]
    return {
        **prev,
        **run_meta,
        'history': hist,
    }
