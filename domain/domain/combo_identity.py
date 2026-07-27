from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from domain.domain.decision_state_fingerprint import canonical_sha256


COMBO_IDENTITY_SCHEMA = "combo_identity.v2"
COMBO_IDENTITY_INTENT_SCHEMA = "combo_identity_intent.v2"

FUNDING_PUT_ROLES = frozenset({"funding_put", "sell_put"})
PARTICIPATION_CALL_ROLES = frozenset({"participation_call", "enhancement_call"})


@dataclass(frozen=True)
class ComboIdentityValidation:
    status: str
    identity_hash: str | None
    reason_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class ComboIdentityIntentValidation:
    status: str
    intent_hash: str | None
    reason_codes: tuple[str, ...] = ()


def _text(value: Any, *, lower: bool = False, upper: bool = False) -> str:
    result = str(value or "").strip()
    if lower:
        return result.lower()
    if upper:
        return result.upper()
    return result


def _positive_contracts(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        numeric = Decimal(str(value))
        parsed = int(numeric)
    except (InvalidOperation, TypeError, ValueError, OverflowError):
        return None
    return parsed if numeric.is_finite() and parsed > 0 and numeric == parsed else None


def _canonical_identity_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": COMBO_IDENTITY_SCHEMA,
        "group_id": _text(payload.get("group_id")),
        "strategy": _text(payload.get("strategy"), lower=True),
        "account": _text(payload.get("account"), lower=True),
        "symbol": _text(payload.get("symbol"), upper=True),
        "funding_put_record_id": _text(payload.get("funding_put_record_id")),
        "funding_put_open_event_id": _text(payload.get("funding_put_open_event_id")),
        "funding_put_contract_key": payload.get("funding_put_contract_key"),
        "participation_call_record_id": _text(payload.get("participation_call_record_id")),
        "participation_call_open_event_id": _text(payload.get("participation_call_open_event_id")),
        "participation_call_contract_key": payload.get("participation_call_contract_key"),
        "original_contracts": _positive_contracts(payload.get("original_contracts")),
    }


def _canonical_intent_payload(payload: dict[str, Any]) -> dict[str, Any]:
    contract_keys = dict(payload.get("contract_keys") or {})
    return {
        "schema_version": COMBO_IDENTITY_INTENT_SCHEMA,
        "group_id": _text(payload.get("group_id")),
        "account": _text(payload.get("account"), lower=True),
        "symbol": _text(payload.get("symbol"), upper=True),
        "strategy": _text(payload.get("strategy"), lower=True),
        "first_leg_open_event_id": _text(payload.get("first_leg_open_event_id")),
        "first_leg_expected_record_id": _text(payload.get("first_leg_expected_record_id")),
        "first_leg_role": _text(payload.get("first_leg_role"), lower=True),
        "second_leg_open_event_id": _text(payload.get("second_leg_open_event_id")),
        "second_leg_expected_record_id": _text(payload.get("second_leg_expected_record_id")),
        "second_leg_role": _text(payload.get("second_leg_role"), lower=True),
        "expected_contracts": _positive_contracts(payload.get("expected_contracts")),
        "contract_keys": {
            "funding_put": contract_keys.get("funding_put"),
            "participation_call": contract_keys.get("participation_call"),
        },
    }


def validate_combo_identity(payload: dict[str, Any]) -> ComboIdentityValidation:
    canonical = _canonical_identity_payload(dict(payload or {}))
    reasons: list[str] = []
    required = (
        "group_id",
        "strategy",
        "account",
        "symbol",
        "funding_put_record_id",
        "funding_put_open_event_id",
        "funding_put_contract_key",
        "participation_call_record_id",
        "participation_call_open_event_id",
        "participation_call_contract_key",
        "original_contracts",
    )
    for field in required:
        if canonical.get(field) in (None, "", {}):
            reasons.append(f"{field}_required")
    record_ids = {
        canonical["funding_put_record_id"],
        canonical["participation_call_record_id"],
    }
    event_ids = {
        canonical["funding_put_open_event_id"],
        canonical["participation_call_open_event_id"],
    }
    if len(record_ids) != 2:
        reasons.append("leg_record_ids_must_differ")
    if len(event_ids) != 2:
        reasons.append("leg_open_event_ids_must_differ")
    if reasons:
        return ComboIdentityValidation(status="conflict", identity_hash=None, reason_codes=tuple(sorted(set(reasons))))
    return ComboIdentityValidation(status="valid", identity_hash=canonical_sha256(canonical))


def build_combo_identity(payload: dict[str, Any]) -> dict[str, Any]:
    canonical = _canonical_identity_payload(dict(payload or {}))
    validation = validate_combo_identity(canonical)
    if validation.status != "valid" or not validation.identity_hash:
        raise ValueError(f"invalid combo identity: {','.join(validation.reason_codes)}")
    return {**canonical, "identity_hash": validation.identity_hash}


def validate_combo_identity_intent(
    payload: dict[str, Any],
    *,
    require_hash: bool = True,
) -> ComboIdentityIntentValidation:
    raw = dict(payload or {})
    canonical = _canonical_intent_payload(raw)
    reasons: list[str] = []
    for field in (
        "group_id",
        "account",
        "symbol",
        "strategy",
        "first_leg_open_event_id",
        "first_leg_expected_record_id",
        "first_leg_role",
        "second_leg_open_event_id",
        "second_leg_expected_record_id",
        "second_leg_role",
        "expected_contracts",
    ):
        if canonical.get(field) in (None, ""):
            reasons.append(f"{field}_required")
    roles = {canonical["first_leg_role"], canonical["second_leg_role"]}
    if not (
        len(roles.intersection(FUNDING_PUT_ROLES)) == 1
        and len(roles.intersection(PARTICIPATION_CALL_ROLES)) == 1
    ):
        reasons.append("intent_leg_roles_invalid")
    if canonical["first_leg_open_event_id"] == canonical["second_leg_open_event_id"]:
        reasons.append("leg_open_event_ids_must_differ")
    if canonical["first_leg_expected_record_id"] == canonical["second_leg_expected_record_id"]:
        reasons.append("leg_record_ids_must_differ")
    contract_keys = canonical["contract_keys"]
    if contract_keys["funding_put"] in (None, "", {}) or contract_keys["participation_call"] in (None, "", {}):
        reasons.append("intent_contract_keys_required")
    expected_hash = canonical_sha256(canonical) if not reasons else None
    supplied_hash = _text(raw.get("intent_hash"))
    if require_hash and not supplied_hash:
        reasons.append("intent_hash_required")
    if supplied_hash and expected_hash and supplied_hash != expected_hash:
        reasons.append("intent_hash_mismatch")
    return ComboIdentityIntentValidation(
        status="valid" if not reasons else "conflict",
        intent_hash=expected_hash if not reasons else None,
        reason_codes=tuple(sorted(set(reasons))),
    )


def build_combo_identity_intent(*, first_leg: dict[str, Any], second_leg: dict[str, Any]) -> dict[str, Any]:
    legs = [dict(first_leg or {}), dict(second_leg or {})]
    roles = [_text(leg.get("leg_role"), lower=True) for leg in legs]
    put_indexes = [index for index, role in enumerate(roles) if role in FUNDING_PUT_ROLES]
    call_indexes = [index for index, role in enumerate(roles) if role in PARTICIPATION_CALL_ROLES]
    if len(put_indexes) != 1 or len(call_indexes) != 1:
        raise ValueError("combo identity intent requires one Funding Put and one Participation Call")
    put_leg = legs[put_indexes[0]]
    call_leg = legs[call_indexes[0]]
    groups = {_text(item.get("strategy_group_id") or item.get("group_id")) for item in legs}
    accounts = {_text(item.get("account"), lower=True) for item in legs}
    symbols = {_text(item.get("symbol"), upper=True) for item in legs}
    contracts = {_positive_contracts(item.get("contracts") or item.get("contracts_open")) for item in legs}
    if "" in groups or len(groups) != 1:
        raise ValueError("combo identity intent requires one explicit strategy_group_id")
    if "" in accounts or len(accounts) != 1:
        raise ValueError("combo identity intent requires matching account")
    if "" in symbols or len(symbols) != 1:
        raise ValueError("combo identity intent requires matching symbol")
    if None in contracts or len(contracts) != 1:
        raise ValueError("combo identity intent requires equal positive contracts")
    for leg in legs:
        if not _text(leg.get("open_event_id")) or not _text(leg.get("record_id")):
            raise ValueError("combo identity intent requires exact open event and record ids")
        if leg.get("contract_key") in (None, "", {}):
            raise ValueError("combo identity intent requires exact contract keys")
    strategy = _text(put_leg.get("strategy") or call_leg.get("strategy"), lower=True)
    if not strategy:
        raise ValueError("combo identity intent requires strategy")
    payload = {
        "schema_version": COMBO_IDENTITY_INTENT_SCHEMA,
        "group_id": next(iter(groups)),
        "account": next(iter(accounts)),
        "symbol": next(iter(symbols)),
        "strategy": strategy,
        "first_leg_open_event_id": _text(first_leg.get("open_event_id")),
        "first_leg_expected_record_id": _text(first_leg.get("record_id")),
        "first_leg_role": _text(first_leg.get("leg_role"), lower=True),
        "second_leg_open_event_id": _text(second_leg.get("open_event_id")),
        "second_leg_expected_record_id": _text(second_leg.get("record_id")),
        "second_leg_role": _text(second_leg.get("leg_role"), lower=True),
        "expected_contracts": next(iter(contracts)),
        "contract_keys": {
            "funding_put": put_leg.get("contract_key"),
            "participation_call": call_leg.get("contract_key"),
        },
    }
    validation = validate_combo_identity_intent(payload, require_hash=False)
    if validation.status != "valid" or not validation.intent_hash:
        raise ValueError(f"invalid combo identity intent: {','.join(validation.reason_codes)}")
    return {**payload, "intent_hash": validation.intent_hash}


def identity_from_intent(
    intent: dict[str, Any],
    *,
    first_leg: dict[str, Any],
    second_leg: dict[str, Any],
) -> dict[str, Any]:
    validation = validate_combo_identity_intent(intent)
    if validation.status != "valid" or not validation.intent_hash:
        raise ValueError(f"invalid combo identity intent: {','.join(validation.reason_codes)}")
    expected = build_combo_identity_intent(first_leg=first_leg, second_leg=second_leg)
    if _canonical_intent_payload(intent) != _canonical_intent_payload(expected):
        raise ValueError("combo identity intent hash mismatch")
    legs = [first_leg, second_leg]
    put_leg = next(item for item in legs if _text(item.get("leg_role"), lower=True) in FUNDING_PUT_ROLES)
    call_leg = next(item for item in legs if _text(item.get("leg_role"), lower=True) in PARTICIPATION_CALL_ROLES)
    return build_combo_identity(
        {
            "group_id": expected["group_id"],
            "strategy": expected["strategy"],
            "account": expected["account"],
            "symbol": expected["symbol"],
            "funding_put_record_id": put_leg.get("record_id"),
            "funding_put_open_event_id": put_leg.get("open_event_id"),
            "funding_put_contract_key": put_leg.get("contract_key"),
            "participation_call_record_id": call_leg.get("record_id"),
            "participation_call_open_event_id": call_leg.get("open_event_id"),
            "participation_call_contract_key": call_leg.get("contract_key"),
            "original_contracts": expected["expected_contracts"],
        }
    )


def classify_combo_structure(
    *,
    identity: dict[str, Any] | None,
    funding_put_contracts_open: Any = 0,
    participation_call_contracts_open: Any = 0,
    funding_put_terminal_allocated: Any = 0,
    participation_call_terminal_allocated: Any = 0,
    assigned_stock_contracts: Any = 0,
    evidence_conflict: bool = False,
) -> str:
    if evidence_conflict:
        return "review_required"
    if not identity:
        return "identity_unverified"
    validation = validate_combo_identity(identity)
    if validation.status != "valid" or validation.identity_hash != identity.get("identity_hash"):
        return "review_required"
    try:
        original = int(identity["original_contracts"])
        quantities = (
            int(funding_put_contracts_open or 0),
            int(participation_call_contracts_open or 0),
            int(funding_put_terminal_allocated or 0),
            int(participation_call_terminal_allocated or 0),
            int(assigned_stock_contracts or 0),
        )
    except (TypeError, ValueError, OverflowError):
        return "review_required"
    if any(item < 0 for item in quantities):
        return "review_required"
    put_open, call_open, put_terminal, call_terminal, assigned = quantities
    put_observed = put_open + put_terminal
    call_observed = call_open + call_terminal
    if put_observed == 0 or call_observed == 0:
        return "opening_incomplete"
    if put_observed > original or call_observed > original:
        return "review_required"
    if put_observed != original or call_observed != original:
        return "partially_decomposed"
    if put_terminal > 0 or call_terminal > 0 or put_open not in {0, original} or call_open not in {0, original}:
        if put_open > 0 or call_open > 0:
            if put_open == original and call_open == 0 and call_terminal >= original:
                return "decomposed_residual_funding_put"
            if put_open == 0 and call_open == original and put_terminal >= original:
                return "assigned_stock_with_residual_call" if assigned > 0 else "residual_call"
            return "partially_decomposed"
    if put_open == original and call_open == original:
        return "active_combo"
    if put_open == 0 and call_open == 0:
        return "assigned_stock_only" if assigned > 0 else "closed"
    return "opening_incomplete"


__all__ = [
    "COMBO_IDENTITY_INTENT_SCHEMA",
    "COMBO_IDENTITY_SCHEMA",
    "ComboIdentityIntentValidation",
    "ComboIdentityValidation",
    "build_combo_identity",
    "build_combo_identity_intent",
    "classify_combo_structure",
    "identity_from_intent",
    "validate_combo_identity",
    "validate_combo_identity_intent",
]
