from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from domain.domain.combo_identity import (
    FUNDING_PUT_ROLES,
    PARTICIPATION_CALL_ROLES,
)
from domain.domain.decision_state_fingerprint import canonical_sha256
from src.application.ledger.event_codec import valid_void_target_event_id


COMBO_GROUP_MEMBERSHIP_SCHEMA = "account_combo_group_membership.v1"


@dataclass(frozen=True)
class ComboMembershipResolution:
    fact: dict[str, Any]
    global_current_record_ids: tuple[str, ...]
    global_live_record_ids: tuple[str, ...]
    global_historical_record_ids: tuple[str, ...]
    retag_events: tuple[tuple[str, str, str, str], ...]
    generation_hash: str


@dataclass(frozen=True)
class ComboMembershipValidation:
    status: str
    membership_hash: str | None
    reason_codes: tuple[str, ...] = ()


def resolve_combo_group_membership(
    *,
    group_id: str,
    account: str,
    trade_events: Iterable[Mapping[str, Any]],
    projected_position_lots: Iterable[Any],
    expected_symbol: str | None = None,
) -> ComboMembershipResolution:
    group_value = _group_id(group_id)
    account_value = _text(account, lower=True)
    symbol_value = _text(expected_symbol, upper=True)
    if not group_value or not account_value:
        raise ValueError("combo membership requires group_id and account")

    history = _effective_group_history(trade_events)
    current_rows = _current_lot_rows(projected_position_lots)
    current_members = {
        record_id: item
        for record_id, item in current_rows.items()
        if _group_id(item.get("strategy_group_id")) == group_value
    }
    live_ids = {
        record_id
        for record_id, item in current_members.items()
        if _nonnegative_integer(item.get("contracts_open")) not in (None, 0)
    }
    historical_ids = set(history.historical_by_group.get(group_value, ()))
    retag_events = tuple(
        sorted(history.retag_by_group.get(group_value, ()))
    )
    known_rows = {**history.open_bindings, **current_rows}
    current_account_ids = sorted(
        record_id
        for record_id, item in current_members.items()
        if _text(item.get("account"), lower=True) == account_value
    )
    occurrence_ids = set(current_members) | historical_ids
    external_ids = sorted(
        record_id
        for record_id in occurrence_ids
        if _text((known_rows.get(record_id) or {}).get("account"), lower=True)
        != account_value
    )
    cross_symbol = any(
        symbol_value
        and _text((known_rows.get(record_id) or {}).get("symbol"), upper=True)
        != symbol_value
        for record_id in occurrence_ids
    )
    bindings = [
        _allowlisted_binding(record_id, current_members[record_id])
        for record_id in current_account_ids
    ]
    bindings.sort(
        key=lambda item: (
            item["record_id"],
            item["role"],
            item["open_event_id"],
        )
    )
    reasons: set[str] = set()
    if len(current_members) != 2:
        reasons.add("combo_group_current_member_count_invalid")
    if len(historical_ids) != 2:
        reasons.add("combo_group_historical_member_count_invalid")
    if set(current_members) != historical_ids:
        reasons.add("combo_group_current_history_mismatch")
    if external_ids:
        reasons.add("combo_group_cross_account_member")
    if cross_symbol:
        reasons.add("combo_group_cross_symbol_member")
    if retag_events:
        reasons.add("combo_group_retag_history_present")
    if len(current_account_ids) != 2 or len(bindings) != 2:
        reasons.add("combo_group_account_binding_count_invalid")
    roles = {item["role"] for item in bindings}
    if not (
        len(roles.intersection(FUNDING_PUT_ROLES)) == 1
        and len(roles.intersection(PARTICIPATION_CALL_ROLES)) == 1
    ):
        reasons.add("combo_group_roles_invalid")
    if any(item["strategy"] != "combo_yield" for item in bindings):
        reasons.add("combo_group_strategy_invalid")
    if any(item["account"] != account_value for item in bindings):
        reasons.add("combo_group_account_binding_invalid")
    if symbol_value and any(
        item["symbol"] != symbol_value for item in bindings
    ):
        reasons.add("combo_group_symbol_binding_invalid")

    external_tuples = sorted(
        (
            record_id,
            _text((known_rows.get(record_id) or {}).get("account"), lower=True),
            _text((known_rows.get(record_id) or {}).get("symbol"), upper=True),
        )
        for record_id in external_ids
    )
    fact = {
        "membership_schema_version": COMBO_GROUP_MEMBERSHIP_SCHEMA,
        "group_id": group_value,
        "status": "exact" if not reasons else "conflict",
        "current_account_member_record_ids": current_account_ids,
        "global_current_member_count": len(current_members),
        "global_historical_member_count": len(historical_ids),
        "external_member_count": len(external_ids),
        "external_membership_hash": canonical_sha256(external_tuples),
        "retag_event_count": len(retag_events),
        "retag_history_hash": canonical_sha256(retag_events),
        "cross_account_member_present": bool(external_ids),
        "cross_symbol_member_present": bool(cross_symbol),
        "member_bindings_for_current_account": bindings,
        "reason_codes": sorted(reasons),
    }
    fact["membership_hash"] = canonical_sha256(fact)
    generation_payload = {
        "schema_version": "combo_membership_generation.v1",
        "group_id": group_value,
        "global_current_record_ids": sorted(current_members),
        "global_live_record_ids": sorted(live_ids),
        "global_historical_record_ids": sorted(historical_ids),
        "retag_events": retag_events,
        "fact_hash": fact["membership_hash"],
    }
    return ComboMembershipResolution(
        fact=fact,
        global_current_record_ids=tuple(sorted(current_members)),
        global_live_record_ids=tuple(sorted(live_ids)),
        global_historical_record_ids=tuple(sorted(historical_ids)),
        retag_events=retag_events,
        generation_hash=canonical_sha256(generation_payload),
    )


def resolve_account_combo_memberships(
    *,
    account: str,
    trade_events: Iterable[Mapping[str, Any]],
    projected_position_lots: Iterable[Any],
    identities: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    events = [dict(item) for item in trade_events]
    lots = list(projected_position_lots)
    account_value = _text(account, lower=True)
    identity_rows = [dict(item) for item in identities]
    group_symbols = {
        _group_id(item.get("group_id")): _text(item.get("symbol"), upper=True)
        for item in identity_rows
        if _text(item.get("account"), lower=True) == account_value
        and _group_id(item.get("group_id"))
    }
    for record_id, item in _current_lot_rows(lots).items():
        del record_id
        if _text(item.get("account"), lower=True) != account_value:
            continue
        group_value = _group_id(item.get("strategy_group_id"))
        if group_value:
            group_symbols.setdefault(
                group_value,
                _text(item.get("symbol"), upper=True),
            )
    return [
        resolve_combo_group_membership(
            group_id=group_id,
            account=account_value,
            trade_events=events,
            projected_position_lots=lots,
            expected_symbol=group_symbols[group_id],
        ).fact
        for group_id in sorted(group_symbols)
    ]


def validate_combo_group_membership(
    payload: Mapping[str, Any],
) -> ComboMembershipValidation:
    item = dict(payload or {})
    reasons: set[str] = set()
    required_keys = {
        "membership_schema_version",
        "group_id",
        "status",
        "current_account_member_record_ids",
        "global_current_member_count",
        "global_historical_member_count",
        "external_member_count",
        "external_membership_hash",
        "retag_event_count",
        "retag_history_hash",
        "cross_account_member_present",
        "cross_symbol_member_present",
        "member_bindings_for_current_account",
        "reason_codes",
        "membership_hash",
    }
    if set(item) != required_keys:
        reasons.add("combo_group_membership_shape_invalid")
    if item.get("membership_schema_version") != COMBO_GROUP_MEMBERSHIP_SCHEMA:
        reasons.add("combo_group_membership_schema_invalid")
    group_id = item.get("group_id")
    if not isinstance(group_id, str) or not group_id or group_id != group_id.strip():
        reasons.add("combo_group_id_invalid")
    record_ids = item.get("current_account_member_record_ids")
    bindings = item.get("member_bindings_for_current_account")
    reason_codes = item.get("reason_codes")
    if not _canonical_text_list(record_ids):
        reasons.add("combo_group_member_ids_noncanonical")
    if not _canonical_text_list(reason_codes):
        reasons.add("combo_group_reason_codes_noncanonical")
    binding_rows: list[dict[str, Any]] = []
    if not isinstance(bindings, list) or bindings != sorted(
        bindings,
        key=lambda binding: (
            str(binding.get("record_id") or "")
            if isinstance(binding, Mapping)
            else "",
            str(binding.get("role") or "")
            if isinstance(binding, Mapping)
            else "",
            str(binding.get("open_event_id") or "")
            if isinstance(binding, Mapping)
            else "",
        ),
    ):
        reasons.add("combo_group_bindings_noncanonical")
    elif any(
        not isinstance(binding, Mapping)
        or set(binding)
        != {
            "record_id",
            "role",
            "open_event_id",
            "strategy",
            "account",
            "symbol",
        }
        for binding in bindings
    ):
        reasons.add("combo_group_binding_shape_invalid")
    else:
        binding_rows = [dict(binding) for binding in bindings]
        canonical_bindings = [
            {
                "record_id": _text(binding.get("record_id")),
                "role": _text(binding.get("role"), lower=True),
                "open_event_id": _text(binding.get("open_event_id")),
                "strategy": _text(
                    binding.get("strategy"), lower=True
                ),
                "account": _text(binding.get("account"), lower=True),
                "symbol": _text(binding.get("symbol"), upper=True),
            }
            for binding in binding_rows
        ]
        if binding_rows != canonical_bindings or any(
            not all(binding.values())
            for binding in canonical_bindings
        ):
            reasons.add("combo_group_binding_values_invalid")
        binding_record_ids = [
            binding["record_id"] for binding in canonical_bindings
        ]
        if (
            not isinstance(record_ids, list)
            or binding_record_ids != record_ids
        ):
            reasons.add("combo_group_binding_record_ids_mismatch")
        if len(
            {
                binding["open_event_id"]
                for binding in canonical_bindings
            }
        ) != len(canonical_bindings):
            reasons.add("combo_group_open_event_ids_duplicate")
    for field in (
        "global_current_member_count",
        "global_historical_member_count",
        "external_member_count",
        "retag_event_count",
    ):
        if _nonnegative_integer(item.get(field)) is None:
            reasons.add("combo_group_membership_count_invalid")
    for field in (
        "cross_account_member_present",
        "cross_symbol_member_present",
    ):
        if not isinstance(item.get(field), bool):
            reasons.add("combo_group_membership_flag_invalid")
    for field in (
        "external_membership_hash",
        "retag_history_hash",
        "membership_hash",
    ):
        if not _sha256_text(item.get(field)):
            reasons.add("combo_group_membership_digest_invalid")
    supplied_hash = _text(item.get("membership_hash"))
    expected_hash = canonical_sha256(
        {
            key: value
            for key, value in item.items()
            if key != "membership_hash"
        }
    )
    if supplied_hash != expected_hash:
        reasons.add("combo_group_membership_hash_mismatch")
    if item.get("status") == "exact":
        funding_bindings = [
            binding
            for binding in binding_rows
            if binding.get("role") in FUNDING_PUT_ROLES
        ]
        participation_bindings = [
            binding
            for binding in binding_rows
            if binding.get("role") in PARTICIPATION_CALL_ROLES
        ]
        if (
            item.get("global_current_member_count") != 2
            or item.get("global_historical_member_count") != 2
            or item.get("external_member_count") != 0
            or item.get("retag_event_count") != 0
            or item.get("cross_account_member_present") is not False
            or item.get("cross_symbol_member_present") is not False
            or not isinstance(record_ids, list)
            or len(record_ids) != 2
            or not isinstance(bindings, list)
            or len(bindings) != 2
            or len(funding_bindings) != 1
            or len(participation_bindings) != 1
            or any(
                binding.get("strategy") != "combo_yield"
                for binding in binding_rows
            )
            or len(
                {binding.get("account") for binding in binding_rows}
            )
            != 1
            or len(
                {binding.get("symbol") for binding in binding_rows}
            )
            != 1
            or item.get("external_membership_hash")
            != canonical_sha256([])
            or item.get("retag_history_hash")
            != canonical_sha256([])
            or reason_codes != []
        ):
            reasons.add("combo_group_exact_membership_invalid")
    elif item.get("status") == "conflict":
        if not reason_codes:
            reasons.add("combo_group_conflict_reasons_missing")
    else:
        reasons.add("combo_group_membership_status_invalid")
    return ComboMembershipValidation(
        status="valid" if not reasons else "conflict",
        membership_hash=expected_hash if not reasons else None,
        reason_codes=tuple(sorted(reasons)),
    )


@dataclass(frozen=True)
class _GroupHistory:
    historical_by_group: dict[str, set[str]]
    retag_by_group: dict[str, list[tuple[str, str, str, str]]]
    open_bindings: dict[str, dict[str, Any]]


def _effective_group_history(
    trade_events: Iterable[Mapping[str, Any]],
) -> _GroupHistory:
    events = [dict(item) for item in trade_events]
    voided_event_ids = {
        target
        for item in events
        for target in [valid_void_target_event_id(item)]
        if target
    }
    effective = sorted(
        (
            item
            for item in events
            if _text(item.get("event_id")) not in voided_event_ids
            and _text(item.get("event_type"), lower=True) != "void"
        ),
        key=lambda item: (
            _integer(item.get("event_time_ms")) or 0,
            _text(item.get("event_id")),
        ),
    )
    group_by_record: dict[str, str] = {}
    historical: dict[str, set[str]] = {}
    retags: dict[str, list[tuple[str, str, str, str]]] = {}
    open_bindings: dict[str, dict[str, Any]] = {}
    for item in effective:
        event_type = _text(item.get("event_type"), lower=True)
        event_id = _text(item.get("event_id"))
        raw = item.get("raw_payload")
        payload = dict(raw) if isinstance(raw, Mapping) else {}
        if event_type == "open":
            record_id = _text(
                item.get("lot_id")
                or payload.get("record_id")
                or f"lot_{event_id}"
            )
            fields = (
                dict(payload.get("fields") or {})
                if isinstance(payload.get("fields"), Mapping)
                else {}
            )
            contract = (
                dict(item.get("contract_key") or {})
                if isinstance(item.get("contract_key"), Mapping)
                else {}
            )
            binding = {
                "record_id": record_id,
                "open_event_id": event_id,
                "account": _text(
                    fields.get("account") or contract.get("account"),
                    lower=True,
                ),
                "symbol": _text(
                    fields.get("symbol")
                    or contract.get("underlying_symbol"),
                    upper=True,
                ),
                "role": _text(
                    fields.get("leg_role")
                    or payload.get("leg_role")
                    or _snapshot_value(payload, "leg_role"),
                    lower=True,
                ),
                "strategy": _text(
                    fields.get("strategy")
                    or payload.get("strategy")
                    or _snapshot_value(payload, "strategy"),
                    lower=True,
                ),
            }
            open_bindings[record_id] = binding
            group_value = _group_id(
                fields.get("strategy_group_id")
                or payload.get("strategy_group_id")
                or _snapshot_value(payload, "strategy_group_id")
            )
            group_by_record[record_id] = group_value
            if group_value:
                historical.setdefault(group_value, set()).add(record_id)
            continue
        if event_type != "adjust":
            continue
        record_id = _text(
            item.get("target_lot_id") or payload.get("target_lot_id")
        )
        patch = payload.get("patch")
        if not record_id or not isinstance(patch, Mapping):
            continue
        if "strategy_group_id" not in patch:
            continue
        before = group_by_record.get(record_id, "")
        after = _group_id(patch.get("strategy_group_id"))
        group_by_record[record_id] = after
        if after:
            historical.setdefault(after, set()).add(record_id)
        if before and after and before != after:
            occurrence = (event_id, record_id, before, after)
            retags.setdefault(before, []).append(occurrence)
            retags.setdefault(after, []).append(occurrence)
    return _GroupHistory(
        historical_by_group=historical,
        retag_by_group=retags,
        open_bindings=open_bindings,
    )


def _current_lot_rows(
    projected_position_lots: Iterable[Any],
) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for raw in projected_position_lots:
        if isinstance(raw, Mapping):
            record_id = _text(raw.get("record_id") or raw.get("lot_id"))
            fields_raw = raw.get("fields")
            fields = (
                dict(fields_raw)
                if isinstance(fields_raw, Mapping)
                else dict(raw)
            )
        else:
            record_id = _text(
                getattr(raw, "record_id", None)
                or getattr(raw, "lot_id", None)
            )
            fields = dict(getattr(raw, "fields", {}) or {})
        if not record_id:
            raise ValueError("projected combo membership lot requires record_id")
        if record_id in out:
            raise ValueError(f"duplicate projected combo lot: {record_id}")
        out[record_id] = fields
    return out


def _allowlisted_binding(
    record_id: str,
    fields: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "record_id": record_id,
        "role": _text(fields.get("leg_role"), lower=True),
        "open_event_id": _text(fields.get("source_event_id")),
        "strategy": _text(fields.get("strategy"), lower=True),
        "account": _text(fields.get("account"), lower=True),
        "symbol": _text(fields.get("symbol"), upper=True),
    }


def _snapshot_value(payload: Mapping[str, Any], field: str) -> Any:
    snapshot = payload.get("strategy_snapshot")
    return snapshot.get(field) if isinstance(snapshot, Mapping) else None


def _canonical_text_list(value: Any) -> bool:
    return (
        isinstance(value, list)
        and all(isinstance(item, str) and item for item in value)
        and value == sorted(set(value))
    )


def _group_id(value: Any) -> str:
    return str(value or "").strip()


def _text(
    value: Any,
    *,
    lower: bool = False,
    upper: bool = False,
) -> str:
    text = str(value or "").strip()
    if lower:
        return text.lower()
    if upper:
        return text.upper()
    return text


def _integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _sha256_text(value: Any) -> bool:
    text = _text(value, lower=True)
    return len(text) == 64 and all(
        character in "0123456789abcdef" for character in text
    )


def _nonnegative_integer(value: Any) -> int | None:
    parsed = _integer(value)
    return parsed if parsed is not None and parsed >= 0 else None


__all__ = [
    "COMBO_GROUP_MEMBERSHIP_SCHEMA",
    "ComboMembershipResolution",
    "ComboMembershipValidation",
    "resolve_account_combo_memberships",
    "resolve_combo_group_membership",
    "validate_combo_group_membership",
]
