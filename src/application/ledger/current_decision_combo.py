from __future__ import annotations

from .current_decision_assigned_stock import (
    _COMBO_GROUP_KEYS,
    _COMBO_MEMBER_KEYS,
    validate_assigned_stock_fact,
)

from .current_decision_common import (
    Any,
    CURRENT_COMBO_GROUP_FACT_SCHEMA,
    CURRENT_COMBO_SCHEMA,
    CurrentDecisionProjectionError,
    FUNDING_PUT_ROLES,
    Mapping,
    PARTICIPATION_CALL_ROLES,
    Sequence,
    _fact_hash,
    _hash_without,
    _integer,
    _position_lot_fields,
    _sha256,
    _text,
    _text_list,
    canonical_sha256,
    classify_combo_structure,
    validate_combo_identity,
)

def build_current_combo_facts(
    *,
    account: str,
    current_position_lots: Sequence[Mapping[str, Any]],
    identities: Sequence[Mapping[str, Any]],
    assigned_stock: Mapping[str, Any],
) -> dict[str, Any]:
    account_value = str(account or "").strip().lower()
    assigned = validate_assigned_stock_fact(assigned_stock)
    if assigned["account"] != account_value:
        raise CurrentDecisionProjectionError("combo assigned-stock account mismatch")
    lots_by_id = _position_lot_fields(current_position_lots)
    assigned_by_group: dict[str, list[str]] = {}
    for lot in assigned["lots"]:
        group_id = str(lot.get("strategy_group_id") or "").strip()
        if group_id:
            assigned_by_group.setdefault(group_id, []).append(lot["stock_lot_id"])

    groups: list[dict[str, Any]] = []
    seen_groups: set[str] = set()
    for raw_identity in sorted(
        identities,
        key=lambda item: str(item.get("group_id") or ""),
    ):
        identity = dict(raw_identity)
        if str(identity.get("account") or "").strip().lower() != account_value:
            raise CurrentDecisionProjectionError("combo identity account mismatch")
        validation = validate_combo_identity(identity)
        if (
            validation.status != "valid"
            or validation.identity_hash != identity.get("identity_hash")
        ):
            raise CurrentDecisionProjectionError("combo identity is invalid")
        group_id = str(identity.get("group_id") or "").strip()
        if group_id in seen_groups:
            raise CurrentDecisionProjectionError("duplicate combo group identity")
        seen_groups.add(group_id)
        expected = (
            (
                str(identity["funding_put_record_id"]),
                str(identity["funding_put_open_event_id"]),
                "funding_put",
            ),
            (
                str(identity["participation_call_record_id"]),
                str(identity["participation_call_open_event_id"]),
                "participation_call",
            ),
        )
        bindings: list[dict[str, Any]] = []
        reasons: set[str] = set()
        put_open = 0
        call_open = 0
        put_terminal = 0
        call_terminal = 0
        original_contracts = int(identity["original_contracts"])
        for record_id, expected_event_id, expected_role in expected:
            fields = lots_by_id.get(record_id)
            if fields is None:
                continue
            contracts_open = _integer(
                fields.get("contracts_open"),
                field="combo contracts_open",
            )
            role = str(fields.get("leg_role") or "").strip().lower()
            open_event_id = str(
                fields.get("source_event_id") or fields.get("open_event_id") or ""
            ).strip()
            if (
                str(fields.get("account") or "").strip().lower() != account_value
                or str(fields.get("symbol") or "").strip().upper()
                != str(identity["symbol"])
                or str(fields.get("strategy_group_id") or "").strip() != group_id
                or open_event_id != expected_event_id
                or (
                    expected_role == "funding_put"
                    and role not in FUNDING_PUT_ROLES
                )
                or (
                    expected_role == "participation_call"
                    and role not in PARTICIPATION_CALL_ROLES
                )
            ):
                reasons.add("combo_current_member_binding_invalid")
            if contracts_open > original_contracts:
                reasons.add("combo_current_member_quantity_invalid")
            if contracts_open > 0:
                bindings.append(
                    {
                        "record_id": record_id,
                        "role": role,
                        "open_event_id": open_event_id,
                        "account": account_value,
                        "symbol": str(fields.get("symbol") or "").strip().upper(),
                        "contracts_open": contracts_open,
                    }
                )
            if expected_role == "funding_put":
                put_open = contracts_open
                put_terminal = max(0, original_contracts - contracts_open)
            else:
                call_open = contracts_open
                call_terminal = max(0, original_contracts - contracts_open)
        assigned_ids = sorted(assigned_by_group.get(group_id, ()))
        if not bindings and not assigned_ids:
            continue
        status = classify_combo_structure(
            identity=identity,
            funding_put_contracts_open=put_open,
            participation_call_contracts_open=call_open,
            funding_put_terminal_allocated=put_terminal,
            participation_call_terminal_allocated=call_terminal,
            assigned_stock_contracts=1 if assigned_ids else 0,
            evidence_conflict=bool(reasons),
        )
        fact = {
            "schema_version": CURRENT_COMBO_GROUP_FACT_SCHEMA,
            "group_id": group_id,
            "identity_hash": str(identity["identity_hash"]),
            "account": account_value,
            "symbol": str(identity["symbol"]),
            "strategy": str(identity["strategy"]),
            "original_contracts": int(identity["original_contracts"]),
            "expected_roles": ["funding_put", "participation_call"],
            "active_member_bindings": sorted(
                bindings,
                key=lambda item: item["record_id"],
            ),
            "assigned_stock_lot_ids": assigned_ids,
            "status": status,
            "reason_codes": sorted(reasons),
        }
        fact["fact_sha256"] = _fact_hash(fact)
        groups.append(validate_current_combo_group_fact(fact))
    result = {
        "schema_version": CURRENT_COMBO_SCHEMA,
        "current_groups": groups,
    }
    result["current_groups_hash"] = canonical_sha256(result)
    return validate_current_combo_facts(result)

def validate_current_combo_group_fact(
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(payload, Mapping) or set(payload) != _COMBO_GROUP_KEYS:
        raise CurrentDecisionProjectionError("current combo group shape is invalid")
    item = dict(payload)
    if item["schema_version"] != CURRENT_COMBO_GROUP_FACT_SCHEMA:
        raise CurrentDecisionProjectionError("current combo group schema is invalid")
    _text(item["group_id"], field="combo group_id")
    _sha256(item["identity_hash"], field="combo identity_hash")
    _text(item["account"], field="combo account", lower=True)
    _text(item["symbol"], field="combo symbol", upper=True)
    _text(item["strategy"], field="combo strategy", lower=True)
    _integer(item["original_contracts"], field="combo original_contracts", minimum=1)
    if item["expected_roles"] != ["funding_put", "participation_call"]:
        raise CurrentDecisionProjectionError("combo expected roles are invalid")
    bindings = item["active_member_bindings"]
    if not isinstance(bindings, list):
        raise CurrentDecisionProjectionError("combo member bindings must be a list")
    binding_ids: list[str] = []
    for raw in bindings:
        if not isinstance(raw, Mapping) or set(raw) != _COMBO_MEMBER_KEYS:
            raise CurrentDecisionProjectionError("combo member binding shape is invalid")
        binding = dict(raw)
        binding_ids.append(_text(binding["record_id"], field="combo record_id"))
        _text(binding["role"], field="combo role", lower=True)
        _text(binding["open_event_id"], field="combo open_event_id")
        if binding["account"] != item["account"]:
            raise CurrentDecisionProjectionError("combo binding account mismatch")
        if binding["symbol"] != item["symbol"]:
            raise CurrentDecisionProjectionError("combo binding symbol mismatch")
        _integer(binding["contracts_open"], field="combo contracts_open", minimum=1)
    if binding_ids != sorted(set(binding_ids)):
        raise CurrentDecisionProjectionError("combo member bindings are not canonical")
    _text_list(item["assigned_stock_lot_ids"], field="assigned_stock_lot_ids")
    _text(item["status"], field="combo status", lower=True)
    _text_list(item["reason_codes"], field="combo reason_codes")
    if _sha256(item["fact_sha256"], field="combo fact_sha256") != _fact_hash(item):
        raise CurrentDecisionProjectionError("combo group fact hash mismatch")
    return item

def validate_current_combo_facts(payload: Mapping[str, Any]) -> dict[str, Any]:
    required = {"schema_version", "current_groups", "current_groups_hash"}
    if not isinstance(payload, Mapping) or set(payload) != required:
        raise CurrentDecisionProjectionError("current combo facts shape is invalid")
    item = dict(payload)
    if item["schema_version"] != CURRENT_COMBO_SCHEMA:
        raise CurrentDecisionProjectionError("current combo facts schema is invalid")
    groups = item["current_groups"]
    if not isinstance(groups, list):
        raise CurrentDecisionProjectionError("current combo groups must be a list")
    group_ids = [
        validate_current_combo_group_fact(group)["group_id"]
        for group in groups
    ]
    if group_ids != sorted(set(group_ids)):
        raise CurrentDecisionProjectionError("current combo groups are not canonical")
    if (
        _sha256(item["current_groups_hash"], field="current_groups_hash")
        != _hash_without(item, "current_groups_hash")
    ):
        raise CurrentDecisionProjectionError("current combo facts hash mismatch")
    return item
