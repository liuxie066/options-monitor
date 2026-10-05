from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Mapping, Sequence

from domain.domain.trade_execution import execution_identity_from_input, legacy_open_execution_input_from_event

from domain.domain.strategy_vocab import (
    STRATEGY_COMBO_YIELD,
    STRATEGY_COVERED_CALL,
    STRATEGY_SELL_PUT,
    canonical_strategy_id,
)
from domain.domain.wheel_call_allocation import parse_wheel_call_allocations

if TYPE_CHECKING:
    from domain.domain.ledger.identity import ContractKey


POSITION_LOT_STRATEGY_PATCH_FIELDS = (
    "strategy", "leg_role", "strategy_group_id", "source_stock_lot_id",
    "source_wheel_branch_id", "wheel_call_allocations", "strategy_snapshot",
)


def validate_attribution_decision(
    decision: Mapping[str, Any], *, events: Sequence[Mapping[str, Any]],
    opening_lot_ids: Mapping[str, str], as_of_ms: int,
) -> tuple[Mapping[str, Any], ...]:
    """Validate a complete durable decision against active, time-bounded facts.

    The caller supplies canonical opening IDs and excludes validated voids. This
    owner depends on source facts only, never on Wheel or Combo read models.
    """
    from domain.domain.ledger.events import TradeEvent

    if (decision.get("schema_version") != "attribution_decision.v1"
            or any(not decision.get(key) for key in
                   ("account", "request_id", "actor", "input_hash", "members"))):
        raise ValueError("attribution decision identity is incomplete")
    members = decision["members"]
    conflicts = decision.get("conflict_event_ids")
    if (not isinstance(members, list) or any(not isinstance(member, Mapping) for member in members)
            or not isinstance(conflicts, list)
            or any(not isinstance(value, str) or not value for value in conflicts)
            or len(set(conflicts)) != len(conflicts)
            or not isinstance(decision.get("branch_generations"), Mapping)
            or not isinstance(decision.get("manual"), bool)
            or not decision.get("policy_version")):
        raise ValueError("attribution decision members or conflicts are invalid")
    by_id: dict[str, Mapping[str, Any]] = {}
    for event in events:
        if int(event.get("event_time_ms") or 0) > as_of_ms:
            continue
        key = str(event.get("event_id") or "")
        if key in by_id and by_id[key] != event:
            raise ValueError("attribution source event identity conflicts")
        by_id[key] = event
    for field in ("execution_key", "open_event_id", "lot_id", "proof_event_id"):
        values = [member.get(field) for member in members]
        if any(not isinstance(value, str) or not value for value in values) or len(set(values)) != len(values):
            raise ValueError("attribution decision member identity is not unique")
    proofs = []
    physical_accounts = []
    for member in members:
        opening = by_id.get(member["open_event_id"], {})
        proof = by_id.get(member["proof_event_id"], {})
        raw = proof.get("raw_payload") or {}
        patch = raw.get("patch") or {}
        after = member.get("after")
        before = member.get("before")
        opening_execution = ((opening.get("raw_payload") or {}).get("execution_input")
            or legacy_open_execution_input_from_event(opening))
        if (not isinstance(patch, Mapping) or not isinstance(after, Mapping) or not isinstance(before, Mapping)
                or set(after) != set(POSITION_LOT_STRATEGY_PATCH_FIELDS)
                or set(before) != set(POSITION_LOT_STRATEGY_PATCH_FIELDS)
                or set(patch) - {*POSITION_LOT_STRATEGY_PATCH_FIELDS, "last_action_at"}
                or any(patch.get(key) != after[key] for key in after)):
            raise ValueError("attribution decision patch differs from its member")
        if (opening.get("event_type") != "open" or proof.get("event_type") != "adjust"
                or opening_lot_ids.get(member["open_event_id"]) != member["lot_id"]
                or list(opening_lot_ids.values()).count(member["lot_id"]) != 1
                or proof.get("target_lot_id") != member["lot_id"]
                or (opening.get("contract_key") or {}).get("account") != decision["account"]
                or TradeEvent.from_dict(proof).contract_key != TradeEvent.from_dict(opening).contract_key
                or proof.get("currency") != opening.get("currency")
                or proof.get("multiplier") != opening.get("multiplier")
                or int(opening.get("event_time_ms") or 0) > int(proof.get("event_time_ms") or 0)
                or proof.get("contracts") != 0 or float(proof.get("price") or 0) != 0
                or float(proof.get("fees") or 0) != 0
                or raw.get("attribution_decision") != decision
                or raw.get("attribution_request_id") != decision["request_id"]
                or raw.get("adjust_target_source_event_id") != member["open_event_id"]
                or raw.get("attribution_policy_version") != decision["policy_version"]
                or raw.get("actor") != decision["actor"]
                or (raw.get("attribution_origin") != "manual" if decision.get("manual", True)
                    else raw.get("attribution_origin") not in {"rule", "intent"})
                or proof.get("source") not in {"trade_attribution", "wheel_linkage", "post_trade_combo_reconciliation"}
                or execution_identity_from_input(opening_execution) != member["execution_key"]):
            raise ValueError("attribution decision proof or opening is invalid")
        resolved = resolve_strategy_metadata({key: value for key, value in after.items() if value is not None})
        if resolved.issues:
            raise ValueError("attribution decision strategy metadata conflicts")
        membership = resolve_option_strategy_membership(opening["contract_key"], opening.get("position_side") or
            ("short" if (opening.get("raw_payload") or {}).get("side") == "sell" else "long"),
            {key: value for key, value in after.items() if value is not None},
            valid_combo_group_ids={str(after.get("strategy_group_id") or "")})
        if membership.issues or (resolved.metadata.wheel_call_allocations and
                sum(row[2] for row in resolved.metadata.wheel_call_allocations) != opening.get("contracts")):
            raise ValueError("attribution decision relationship or quantity is invalid")
        if not decision.get("manual", True) and (conflicts or strategy_metadata_has_owner(
                {key: value for key, value in before.items() if value is not None})):
            raise ValueError("automatic attribution cannot transfer an existing relationship")
        proofs.append(proof)
        ref = opening_execution.get("broker_account_ref") or {}
        physical_accounts.append(tuple(ref.get(key) for key in ("broker_id", "external_account_id", "environment")))
    if any(not all(ref) for ref in physical_accounts) or len(set(physical_accounts)) != 1:
        raise ValueError("attribution decision physical accounts differ")
    if len({int(proof["event_time_ms"]) for proof in proofs}) != 1:
        raise ValueError("attribution decision proof times differ")
    return tuple(proofs)


@dataclass(frozen=True)
class StrategyMetadata:
    strategy: str = ""
    leg_role: str = ""
    strategy_group_id: str | None = None
    source_lot_id: str | None = None
    source_wheel_branch_id: str | None = None
    wheel_call_allocations: tuple[tuple[str, str, int], ...] = ()
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
    wheel_call_allocations: tuple[tuple[str, str, int], ...] = ()
    issues: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "leg_type": self.leg_type,
            "strategy": self.strategy,
            "parent_universe": self.parent_universe,
            "leg_role": self.leg_role,
            "strategy_group_id": self.strategy_group_id,
            "source_stock_lot_id": self.source_lot_id,
            "source_wheel_branch_id": self.source_wheel_branch_id,
            "issues": list(self.issues),
        }
        if self.wheel_call_allocations:
            payload["wheel_call_allocations"] = [
                {"stock_lot_id": stock, "wheel_branch_id": branch, "contracts": contracts}
                for stock, branch, contracts in self.wheel_call_allocations
            ]
        return payload


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
                or value.leg_role or value.strategy_group_id or value.source_lot_id or value.source_wheel_branch_id
                or value.wheel_call_allocations)


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
            # A manual Wheel assignment already chooses among stock branches.
            # New branch possibilities alone do not contradict that choice;
            # explicit intent/Combo evidence and real capacity conflicts still do.
            conflicts = [key for key in ids if key != target and key not in acknowledged
                and not (existing.get("origin") == "manual" and existing.get("strategy") == "wheel"
                    and by_id[key].get("strategy") == "wheel" and not by_id[key].get("intent_id")
                    and not {"multiple_or_invalid_wheel_intents", "wheel_intent_fill_mismatch_or_consumed"}
                        .intersection(by_id[key].get("reason_codes") or []))]
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

    allocations: tuple[tuple[str, str, int], ...] = ()
    for source in (raw, snapshot):
        if "wheel_call_allocations" not in source:
            continue
        try:
            parsed = parse_wheel_call_allocations(source["wheel_call_allocations"])
        except ValueError:
            issues.append("wheel_call_allocations_invalid")
            continue
        if allocations and allocations != parsed:
            issues.append("wheel_call_allocations_conflict")
        allocations = parsed

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
            wheel_call_allocations=allocations,
            expiry_structure=expiry_structure,
        ),
        issues=tuple(issues),
    )


def resolve_option_strategy_membership(
    contract_key: ContractKey | Mapping[str, Any],
    position_side: str,
    payload: Mapping[str, Any] | None,
    *,
    valid_combo_group_ids: set[str] | frozenset[str] = frozenset(),
    source_id: str = "",
) -> OptionStrategyMembership:
    option_type = contract_key.get("option_type") if isinstance(contract_key, Mapping) else contract_key.option_type
    leg_type = f"{'sell' if position_side == 'short' else 'buy'}_{option_type}"
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
            wheel_call_allocations=(metadata.wheel_call_allocations if keep_relationship else ()),
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
        multi_wheel_call = (
            strategy == "wheel" and role == "wheel_call" and leg_type == "sell_call"
            and metadata.wheel_call_allocations and not metadata.source_lot_id
            and not metadata.source_wheel_branch_id and not group_id
        )
        if ((valid_wheel or legacy_wheel_call) and not metadata.wheel_call_allocations) or multi_wheel_call:
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
