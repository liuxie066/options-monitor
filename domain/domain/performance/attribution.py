from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from domain.domain.ledger.events import TradeEvent
from domain.domain.performance.models import StrategyAttribution
from domain.domain.strategy_membership import (
    resolve_expiry_structure,
    resolve_strategy_metadata,
)

_COMBO_YIELD = "combo_yield"
_FUNDING_PUT = "funding_put"
_PARTICIPATION_CALL = "participation_call"


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
    resolved = resolve_strategy_metadata(payload, source_id=event.event_id)
    if resolved.issues:
        return AttributionResolution(None, resolved.issues)
    metadata = resolved.metadata
    strategy = metadata.strategy
    leg_role = metadata.leg_role
    group_id = metadata.strategy_group_id or ""
    if not strategy and group_id.startswith(f"{_COMBO_YIELD}:"):
        strategy = _COMBO_YIELD
    if not any((strategy, leg_role, group_id)):
        return AttributionResolution(None)
    combo_indicated = strategy == _COMBO_YIELD or group_id.startswith(f"{_COMBO_YIELD}:")
    if not combo_indicated:
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
            expiry_structure=metadata.expiry_structure,
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


def resolve_expiry_structure_from_fields(
    snapshot: Mapping[str, Any] | None,
    payload: Mapping[str, Any] | None,
) -> str | None:
    """Resolve Combo expiry structure with the same fallback as the lifecycle layer.

    Explicit ``expiry_structure`` wins; otherwise ``structure_mode`` maps
    ``same_expiry_pair`` to ``same_expiry`` and any other explicit value to
    ``unknown`` (fail-closed: unsupported structures must not be treated as
    same-expiry). Missing structure metadata stays ``None`` so production
    same-expiry rows keep their serialized shape.
    """
    return resolve_expiry_structure(snapshot, payload)


__all__ = [
    "AttributionResolution",
    "resolve_allocation_attribution",
    "resolve_event_attribution",
    "resolve_expiry_structure_from_fields",
]
