from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from domain.domain.ledger.events import TradeEvent
from domain.domain.performance.models import StrategyAttribution

_COMBO_YIELD = "combo_yield"
_FUNDING_PUT = "funding_put"
_PARTICIPATION_CALL = "participation_call"
_KEYS = ("strategy", "leg_role", "strategy_group_id", "expiry_structure")


@dataclass(frozen=True)
class AttributionResolution:
    attribution: StrategyAttribution | None
    issues: tuple[str, ...] = ()


def resolve_event_attribution(
    event: TradeEvent,
    *,
    lifecycle_source_id: str | None = None,
) -> AttributionResolution:
    payload = event.raw_payload if isinstance(event.raw_payload, Mapping) else {}
    snapshot = payload.get("strategy_snapshot")
    snapshot = snapshot if isinstance(snapshot, Mapping) else {}
    values: dict[str, str] = {}
    issues: list[str] = []
    for key in _KEYS:
        snapshot_value = _text(snapshot.get(key))
        top_level_value = _text(payload.get(key))
        if snapshot_value and top_level_value and snapshot_value != top_level_value:
            issues.append(f"strategy_metadata_conflict:{event.event_id}:{key}")
        values[key] = snapshot_value or top_level_value
    if issues:
        return AttributionResolution(None, tuple(issues))
    strategy = values["strategy"]
    leg_role = values["leg_role"]
    group_id = values["strategy_group_id"]
    if not strategy and group_id.startswith(f"{_COMBO_YIELD}:"):
        strategy = _COMBO_YIELD
    if not any((strategy, leg_role, group_id)):
        return AttributionResolution(None)
    if strategy != _COMBO_YIELD or not group_id:
        return AttributionResolution(None, (f"strategy_attribution_incomplete:{event.event_id}",))
    if not _text(lifecycle_source_id):
        return AttributionResolution(None, (f"strategy_lifecycle_source_missing:{event.event_id}",))
    lifecycle_id = _lifecycle_id(leg_role=leg_role, source_id=lifecycle_source_id)
    if lifecycle_id is None:
        return AttributionResolution(None, (f"strategy_leg_role_unsupported:{event.event_id}",))
    return AttributionResolution(
        StrategyAttribution(
            strategy=strategy,
            leg_role=leg_role,
            strategy_group_id=group_id,
            lifecycle_id=lifecycle_id,
            expiry_structure=values["expiry_structure"] or None,
        )
    )


def resolve_allocation_attribution(
    *,
    strategy: Any,
    leg_role: Any,
    strategy_group_id: Any,
    target_lot_id: str,
) -> StrategyAttribution | None:
    strategy_value = _text(strategy)
    role_value = _text(leg_role)
    group_value = _text(strategy_group_id)
    if not strategy_value and group_value.startswith(f"{_COMBO_YIELD}:"):
        strategy_value = _COMBO_YIELD
    lifecycle_id = _lifecycle_id(leg_role=role_value, source_id=target_lot_id)
    if strategy_value != _COMBO_YIELD or not group_value or lifecycle_id is None:
        return None
    return StrategyAttribution(
        strategy=strategy_value,
        leg_role=role_value,
        strategy_group_id=group_value,
        lifecycle_id=lifecycle_id,
    )


def _lifecycle_id(*, leg_role: str, source_id: str) -> str | None:
    source = _text(source_id)
    if not source:
        return None
    if leg_role == _FUNDING_PUT:
        return f"funding_cycle:{source}"
    if leg_role == _PARTICIPATION_CALL:
        return f"participation:{source}"
    return None


def _text(value: Any) -> str:
    return str(value or "").strip()


__all__ = [
    "AttributionResolution",
    "resolve_allocation_attribution",
    "resolve_event_attribution",
]
