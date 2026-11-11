from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from domain.domain.ledger import TradeEvent
from src.application.ledger.event_codec import stored_trade_event_to_ledger_event


def lifecycle_effective_void_target_event_ids(
    events: Iterable[Any],
) -> tuple[str, ...]:
    """Void targets that still invalidate lifecycle terminal allocations.

    A controlled repair voids a terminal event and records the live
    replacement in ``repair_event_id``. While that replacement reproduces the
    original terminal result (same contract, event type, lot and contracts),
    the result still exists, so the void must not hide the lifecycle
    allocation that names the original derived event id. Voids without a live
    matching replacement keep their normal invalidating effect.

    Every void on one target must resolve to the same live replacement;
    otherwise the target stays effective. The same rule is shared by the
    lifecycle overlay, the coherent-fact queries and the current-decision
    writer so read and write sides cannot disagree about a voided close.
    """

    decoded: list[TradeEvent] = []
    for item in events:
        event, diagnostics = stored_trade_event_to_ledger_event(item)
        if event is None or any(diag.severity == "error" for diag in diagnostics):
            continue
        decoded.append(event)
    by_id = {event.event_id: event for event in decoded}

    voids_by_target: dict[str, list[TradeEvent]] = {}
    for event in decoded:
        if event.event_type != "void":
            continue
        target = str(event.target_event_id or "").strip()
        if target:
            voids_by_target.setdefault(target, []).append(event)

    def live_replacement(
        event: TradeEvent, seen: frozenset[str]
    ) -> TradeEvent | None:
        """Follow repair pointers to an event no void invalidates."""

        if event.event_id in seen:
            return None
        seen = seen | {event.event_id}
        if event.event_type == "void":
            replacement_id = str(
                event.raw_payload.get("repair_event_id") or ""
            ).strip()
            replacement = by_id.get(replacement_id) if replacement_id else None
            return live_replacement(replacement, seen) if replacement else None
        voids = voids_by_target.get(event.event_id) or []
        if not voids:
            return event
        # Several voids on one event leave no single live result to point at.
        if len(voids) > 1:
            return None
        return live_replacement(voids[0], seen)

    def replaces_the_result(original: TradeEvent, tail: TradeEvent) -> bool:
        return (
            tail.event_type == original.event_type
            and tail.contract_key == original.contract_key
            and tail.contracts == original.contracts
            and tail.target_lot_id == original.target_lot_id
        )

    targets: set[str] = set()
    for target, void_events in voids_by_target.items():
        original = by_id.get(target)
        replacements = [
            live_replacement(void_event, frozenset())
            for void_event in void_events
        ]
        superseded = (
            original is not None
            and all(tail is not None for tail in replacements)
            and len({tail.event_id for tail in replacements if tail is not None}) == 1
            and replaces_the_result(
                original,
                next(tail for tail in replacements if tail is not None),
            )
        )
        if not superseded:
            targets.add(target)
    return tuple(sorted(targets))
