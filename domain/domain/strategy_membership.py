from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Mapping

from domain.domain.strategy_vocab import (
    STRATEGY_COMBO_YIELD,
    STRATEGY_COVERED_CALL,
    STRATEGY_SELL_PUT,
    canonical_strategy_id,
)

if TYPE_CHECKING:
    from domain.domain.ledger.identity import ContractKey


@dataclass(frozen=True)
class StrategyMetadata:
    strategy: str = ""
    leg_role: str = ""
    strategy_group_id: str | None = None
    source_stock_lot_id: str | None = None
    expiry_structure: str | None = None


@dataclass(frozen=True)
class StrategyMetadataResolution:
    metadata: StrategyMetadata
    issues: tuple[str, ...] = ()


@dataclass(frozen=True)
class OptionStrategyMembership:
    leg_type: str
    strategy: str
    parent_universe: str | None
    leg_role: str | None = None
    strategy_group_id: str | None = None
    source_stock_lot_id: str | None = None
    issues: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "leg_type": self.leg_type,
            "strategy": self.strategy,
            "parent_universe": self.parent_universe,
            "leg_role": self.leg_role,
            "strategy_group_id": self.strategy_group_id,
            "source_stock_lot_id": self.source_stock_lot_id,
            "issues": list(self.issues),
        }


def resolve_strategy_metadata(
    payload: Mapping[str, Any] | None,
    *,
    source_id: str = "",
) -> StrategyMetadataResolution:
    raw = payload if isinstance(payload, Mapping) else {}
    snapshot = raw.get("strategy_snapshot")
    snapshot = snapshot if isinstance(snapshot, Mapping) else {}
    issues: list[str] = []
    values: dict[str, str] = {}
    normalizers = {
        "strategy": _strategy,
        "leg_role": _lower,
        "strategy_group_id": _text,
        "source_stock_lot_id": _text,
        "expiry_structure": _lower,
    }
    for key, normalize in normalizers.items():
        top = normalize(raw.get(key))
        nested = normalize(snapshot.get(key))
        if top and nested and top != nested:
            prefix = f":{source_id}" if source_id else ""
            issues.append(f"strategy_metadata_conflict{prefix}:{key}")
        values[key] = nested or top

    expiry_structure = values["expiry_structure"] or resolve_expiry_structure(
        snapshot,
        raw,
    )
    return StrategyMetadataResolution(
        metadata=StrategyMetadata(
            strategy=values["strategy"],
            leg_role=values["leg_role"],
            strategy_group_id=values["strategy_group_id"] or None,
            source_stock_lot_id=values["source_stock_lot_id"] or None,
            expiry_structure=expiry_structure,
        ),
        issues=tuple(issues),
    )


def resolve_option_strategy_membership(
    contract_key: ContractKey,
    payload: Mapping[str, Any] | None,
    *,
    valid_combo_group_ids: set[str] | frozenset[str] = frozenset(),
    source_id: str = "",
) -> OptionStrategyMembership:
    leg_type = f"{'sell' if contract_key.position_side == 'short' else 'buy'}_{contract_key.option_type}"
    default = "csp" if leg_type == "sell_put" else "cc" if leg_type == "sell_call" else "unassigned"
    parent = "csp" if leg_type == "sell_put" else "cc" if leg_type == "sell_call" else None
    resolved = resolve_strategy_metadata(payload, source_id=source_id)
    metadata = resolved.metadata

    def result(
        strategy: str = default,
        *,
        keep_relationship: bool = False,
        issues: tuple[str, ...] = resolved.issues,
    ) -> OptionStrategyMembership:
        return OptionStrategyMembership(
            leg_type=leg_type,
            strategy=strategy,
            parent_universe=parent,
            leg_role=metadata.leg_role or None,
            strategy_group_id=(metadata.strategy_group_id if keep_relationship else None),
            source_stock_lot_id=(metadata.source_stock_lot_id if keep_relationship else None),
            issues=issues,
        )

    if resolved.issues:
        return result(issues=("strategy_attribution_conflict",))

    strategy = metadata.strategy
    group_id = metadata.strategy_group_id
    role = metadata.leg_role
    if not strategy and str(group_id or "").startswith(f"{STRATEGY_COMBO_YIELD}:"):
        strategy = STRATEGY_COMBO_YIELD

    if strategy == STRATEGY_COMBO_YIELD or str(group_id or "").startswith(
        f"{STRATEGY_COMBO_YIELD}:"
    ):
        combo = {
            ("funding_put", "sell_put"): "csp_lc",
            ("participation_call", "buy_call"): "csp_lc",
            ("short_call", "sell_call"): "cc_lp",
            ("long_put", "buy_put"): "cc_lp",
        }.get((role, leg_type))
        if combo and group_id in valid_combo_group_ids:
            return result(combo, keep_relationship=True, issues=())
        return result(issues=("strategy_attribution_conflict",))

    if strategy == "wheel" or role == "wheel_call" or metadata.source_stock_lot_id:
        if strategy == "wheel" and role == "wheel_call" and metadata.source_stock_lot_id and leg_type == "sell_call":
            return result("wheel", keep_relationship=True, issues=())
        return result(issues=("strategy_attribution_conflict",))

    expected_leg = {
        STRATEGY_SELL_PUT: "sell_put",
        STRATEGY_COVERED_CALL: "sell_call",
    }.get(strategy)
    if expected_leg and leg_type != expected_leg:
        return result(issues=("strategy_attribution_conflict",))

    return result(issues=())


def resolve_expiry_structure(
    snapshot: Mapping[str, Any] | None,
    payload: Mapping[str, Any] | None,
) -> str | None:
    snapshot = snapshot if isinstance(snapshot, Mapping) else {}
    payload = payload if isinstance(payload, Mapping) else {}
    explicit = _lower(snapshot.get("expiry_structure")) or _lower(
        payload.get("expiry_structure")
    )
    if explicit:
        return explicit
    mode = _lower(snapshot.get("structure_mode")) or _lower(
        payload.get("structure_mode")
    )
    if mode == "same_expiry_pair":
        return "same_expiry"
    return "unknown" if mode else None


def _strategy(value: Any) -> str:
    text = _text(value)
    return canonical_strategy_id(text) if text else ""


def _lower(value: Any) -> str:
    return _text(value).lower()


def _text(value: Any) -> str:
    return str(value or "").strip()


__all__ = [
    "OptionStrategyMembership",
    "StrategyMetadata",
    "StrategyMetadataResolution",
    "resolve_expiry_structure",
    "resolve_option_strategy_membership",
    "resolve_strategy_metadata",
]
