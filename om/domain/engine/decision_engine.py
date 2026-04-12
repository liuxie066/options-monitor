from __future__ import annotations

from typing import Any


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
