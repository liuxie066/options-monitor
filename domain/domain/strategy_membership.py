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
    source_lot_id: str | None = None
    source_wheel_branch_id: str | None = None
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
    source_lot_id: str | None = None
    source_wheel_branch_id: str | None = None
    issues: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "leg_type": self.leg_type,
            "strategy": self.strategy,
            "parent_universe": self.parent_universe,
            "leg_role": self.leg_role,
            "strategy_group_id": self.strategy_group_id,
            "source_stock_lot_id": self.source_lot_id,
            "source_wheel_branch_id": self.source_wheel_branch_id,
            "issues": list(self.issues),
        }


@dataclass(frozen=True)
class TradeAttributionResolution:
    """A proposal, never evidence that a ledger write succeeded."""

    status: str
    candidate_ids: tuple[str, ...] = ()
    selected_candidate_id: str | None = None
    reason_codes: tuple[str, ...] = ()


def strategy_metadata_has_owner(metadata: Mapping[str, Any]) -> bool:
    """CSP/CC describe a leg; only relationship metadata claims strategy ownership."""
    resolved = resolve_strategy_metadata(metadata)
    value = resolved.metadata
    return bool(resolved.issues or value.strategy not in {"", "unassigned", STRATEGY_SELL_PUT, STRATEGY_COVERED_CALL}
                or value.leg_role or value.strategy_group_id or value.source_lot_id or value.source_wheel_branch_id)


def resolve_trade_attribution(
    *,
    candidates: tuple[Mapping[str, Any], ...],
    evidence_complete: bool,
    existing: Mapping[str, Any] | None = None,
    applicable: bool = True,
) -> TradeAttributionResolution:
    """Arbitrate owner-validated proposals from one complete economic snapshot.

    Wheel/Combo owners validate identity, history, intent, quantities and policy.
    A known competing proposal remains a blocker even if it cannot auto-apply.
    The application publishes `linked` only after durable ledger readback.
    """
    by_id: dict[str, Mapping[str, Any]] = {}
    for candidate in candidates:
        candidate_id = _text(candidate.get("candidate_id"))
        if not candidate_id or candidate.get("strategy") not in {"wheel", "combo_yield"}:
            return TradeAttributionResolution("pending", reason_codes=("invalid_candidate_evidence",))
        if candidate_id in by_id and dict(by_id[candidate_id]) != dict(candidate):
            return TradeAttributionResolution("pending", reason_codes=("candidate_identity_conflict",))
        by_id[candidate_id] = candidate
    ids = tuple(sorted(by_id))
    existing = existing or {}
    if existing.get("status") in {"linked", "ordinary", "conflict"}:
        if existing.get("status") == "conflict":
            return TradeAttributionResolution("conflict", ids, reason_codes=("unresolved_attribution_conflict",))
        if existing.get("status") == "ordinary" and existing.get("origin") == "manual":
            return TradeAttributionResolution("ordinary", ids, reason_codes=("manual_ordinary_preserved",))
        if existing.get("status") == "linked":
            target = _text(existing.get("candidate_id"))
            # Missing evidence cannot disprove a durable relationship.
            acknowledged = set(existing.get("acknowledged_candidate_ids") or []) if existing.get("origin") == "manual" else set()
            conflicts = [key for key in ids if key != target and key not in acknowledged]
            if conflicts:
                return TradeAttributionResolution("conflict", ids, reason_codes=("late_competing_evidence",))
            capacity_conflicts = {"competing_fills_exceed_capacity", "competing_fills_exceed_intent_remainder",
                                  "account_stock_capacity_exceeded", "account_cash_capacity_exceeded"}
            if target in by_id and capacity_conflicts.intersection(by_id[target].get("reason_codes") or []):
                return TradeAttributionResolution("conflict", ids, reason_codes=("late_capacity_conflict",))
            return TradeAttributionResolution("linked", ids, reason_codes=("existing_attribution_preserved",))
    if not applicable:
        return TradeAttributionResolution("not_applicable")
    if not evidence_complete:
        return TradeAttributionResolution("pending", ids, reason_codes=("attribution_evidence_incomplete",))
    if not ids:
        return TradeAttributionResolution("ordinary", reason_codes=("no_strategy_candidate",))
    if len(ids) != 1:
        return TradeAttributionResolution("pending", ids, reason_codes=("multiple_strategy_candidates",))
    candidate = by_id[ids[0]]
    reasons = tuple(sorted(set(candidate.get("reason_codes") or ())))
    if candidate.get("eligible") is not True or reasons:
        return TradeAttributionResolution("pending", ids, reason_codes=reasons or ("candidate_not_eligible",))
    return TradeAttributionResolution(
        "pending", ids, selected_candidate_id=ids[0], reason_codes=("awaiting_ledger_commit",)
    )


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
        "source_wheel_branch_id": _text,
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
            source_lot_id=values["source_stock_lot_id"] or None,
            source_wheel_branch_id=values["source_wheel_branch_id"] or None,
            expiry_structure=expiry_structure,
        ),
        issues=tuple(issues),
    )


def resolve_option_strategy_membership(
    contract_key: ContractKey,
    position_side: str,
    payload: Mapping[str, Any] | None,
    *,
    valid_combo_group_ids: set[str] | frozenset[str] = frozenset(),
    source_id: str = "",
) -> OptionStrategyMembership:
    leg_type = f"{'sell' if position_side == 'short' else 'buy'}_{contract_key.option_type}"
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
            source_lot_id=(metadata.source_lot_id if keep_relationship else None),
            source_wheel_branch_id=(
                metadata.source_wheel_branch_id if keep_relationship else None
            ),
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

    if (
        strategy == "wheel"
        or role in {"wheel_call", "wheel_put"}
        or metadata.source_wheel_branch_id
    ):
        valid_wheel = (
            strategy == "wheel"
            and metadata.source_wheel_branch_id
            and (
                role == "wheel_call"
                and metadata.source_lot_id
                and leg_type == "sell_call"
                or role == "wheel_put" and leg_type == "sell_put"
            )
        )
        legacy_wheel_call = (
            strategy == "wheel"
            and role == "wheel_call"
            and metadata.source_lot_id
            and not metadata.source_wheel_branch_id
            and leg_type == "sell_call"
        )
        if valid_wheel or legacy_wheel_call:
            return result("wheel", keep_relationship=True, issues=())
        return result(issues=("strategy_attribution_conflict",))

    expected_leg = {
        STRATEGY_SELL_PUT: "sell_put",
        STRATEGY_COVERED_CALL: "sell_call",
    }.get(strategy)
    if expected_leg and leg_type != expected_leg:
        return result(issues=("strategy_attribution_conflict",))

    return result(
        keep_relationship=bool(metadata.source_lot_id),
        issues=(),
    )


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
    "TradeAttributionResolution",
    "resolve_trade_attribution",
    "StrategyMetadataResolution",
    "resolve_expiry_structure",
    "resolve_option_strategy_membership",
    "resolve_strategy_metadata",
]
