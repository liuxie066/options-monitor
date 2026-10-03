from __future__ import annotations

from domain.domain.ledger.events import lot_id_for_open_event
from domain.domain.trade_contract_identity import require_option_multiplier

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from domain.domain.combo_identity import (
    FUNDING_PUT_ROLES,
    PARTICIPATION_CALL_ROLES,
    validate_combo_identity,
    build_combo_identity_intent, identity_from_intent,
)
from domain.domain.decision_state_fingerprint import canonical_sha256
from domain.domain.ledger import TradeEvent, ContractKey
from domain.domain.ledger.position_fields import effective_contracts, effective_multiplier
from domain.domain.ledger.projection import _valid_combo_pair
from domain.domain.strategy_membership import resolve_strategy_metadata
from domain.domain.wheel import lot_strategy_metadata_from_trade_events
from src.application.ledger.event_codec import valid_void_target_event_id
from src.application.ledger.queries import project_trade_event_log
from src.application.payload_helpers import text as _group_id


COMBO_GROUP_MEMBERSHIP_SCHEMA = "account_combo_group_membership.v1"


def _lot_contract_value(
    fields: Mapping[str, Any],
    nested_key: str,
    *flat_keys: str,
) -> Any:
    """One contract identity value: nested ``contract_key`` first, flat after.

    ``position_lots.fields_json`` carries the contract under ``contract_key``
    now (``write-side-definition.md`` §2); the flat siblings it replaced stay
    readable for a row that predates the shape switch.
    """
    contract_key = fields.get("contract_key")
    contract_key = contract_key if isinstance(contract_key, Mapping) else {}
    value = contract_key.get(nested_key)
    if value not in (None, ""):
        return value
    for flat_key in flat_keys:
        value = fields.get(flat_key)
        if value not in (None, ""):
            return value
    return None


@dataclass(frozen=True)
class ComboMembershipResolution:
    fact: dict[str, Any]
    global_current_lot_ids: tuple[str, ...]
    global_live_lot_ids: tuple[str, ...]
    global_historical_lot_ids: tuple[str, ...]
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
    # ``strategy_group_id`` left the lot payload (write-side-definition.md §2
    # RECONSTRUCTIBLE; §7 moves the family to the strategy/event side). The
    # event-layer replay ``_effective_group_history`` already derives answers
    # the live binding; the retired flat key stays readable for a row written
    # before the shape switch.
    current_members = {
        lot_id: item
        for lot_id, item in current_rows.items()
        if _group_id(
            history.group_by_record.get(lot_id)
            or item.get("strategy_group_id")
        )
        == group_value
    }
    live_ids = {
        lot_id
        for lot_id, item in current_members.items()
        if _nonnegative_integer(item.get("contracts_open")) not in (None, 0)
    }
    historical_ids = set(history.historical_by_group.get(group_value, ()))
    retag_events = tuple(
        sorted(history.retag_by_group.get(group_value, ()))
    )
    known_rows = {**history.open_bindings, **current_rows}
    current_account_ids = sorted(
        lot_id
        for lot_id, item in current_members.items()
        if _text(_lot_contract_value(item, "account", "account"), lower=True)
        == account_value
    )
    occurrence_ids = set(current_members) | historical_ids
    external_ids = sorted(
        lot_id
        for lot_id in occurrence_ids
        if _text(
            _lot_contract_value(known_rows.get(lot_id) or {}, "account", "account"),
            lower=True,
        )
        != account_value
    )
    cross_symbol = any(
        symbol_value
        and _text(
            _lot_contract_value(
                known_rows.get(lot_id) or {}, "underlying_symbol", "symbol"
            ),
            upper=True,
        )
        != symbol_value
        for lot_id in occurrence_ids
    )
    bindings = [
        _allowlisted_binding(
            lot_id,
            current_members[lot_id],
            binding=history.binding_by_record.get(lot_id) or {},
        )
        for lot_id in current_account_ids
    ]
    bindings.sort(
        key=lambda item: (
            item["record_id"],
            item["role"],
            item["open_event_id"],
        )
    )
    reasons: set[str] = set()
    released = not current_members and group_value in history.released_groups
    if len(current_members) != 2 and not released:
        reasons.add("combo_group_current_member_count_invalid")
    if len(historical_ids) != 2:
        reasons.add("combo_group_historical_member_count_invalid")
    if set(current_members) != historical_ids and not released:
        reasons.add("combo_group_current_history_mismatch")
    if external_ids:
        reasons.add("combo_group_cross_account_member")
    if cross_symbol:
        reasons.add("combo_group_cross_symbol_member")
    if retag_events:
        reasons.add("combo_group_retag_history_present")
    if (len(current_account_ids) != 2 or len(bindings) != 2) and not released:
        reasons.add("combo_group_account_binding_count_invalid")
    roles = {item["role"] for item in bindings}
    sp_lc_roles = (
        len(roles.intersection(FUNDING_PUT_ROLES)) == 1
        and len(roles.intersection(PARTICIPATION_CALL_ROLES)) == 1
    )
    if not sp_lc_roles and roles != {"short_call", "long_put"} and not released:
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
            lot_id,
            _text(
                _lot_contract_value(
                    known_rows.get(lot_id) or {}, "account", "account"
                ),
                lower=True,
            ),
            _text(
                _lot_contract_value(
                    known_rows.get(lot_id) or {}, "underlying_symbol", "symbol"
                ),
                upper=True,
            ),
        )
        for lot_id in external_ids
    )
    fact = {
        "membership_schema_version": COMBO_GROUP_MEMBERSHIP_SCHEMA,
        "group_id": group_value,
        "status": "released" if released and not reasons else "exact" if not reasons else "conflict",
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
        global_current_lot_ids=tuple(sorted(current_members)),
        global_live_lot_ids=tuple(sorted(live_ids)),
        global_historical_lot_ids=tuple(sorted(historical_ids)),
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
        _group_id(item.get("group_id")): _text(
            _lot_contract_value(item, "underlying_symbol", "symbol"), upper=True
        )
        for item in identity_rows
        if _text(_lot_contract_value(item, "account", "account"), lower=True)
        == account_value
        and _group_id(item.get("group_id"))
    }
    for lot_id, item in _current_lot_rows(lots).items():
        del lot_id
        if _text(_lot_contract_value(item, "account", "account"), lower=True) != account_value:
            continue
        group_value = _group_id(item.get("strategy_group_id"))
        if group_value:
            group_symbols.setdefault(
                group_value,
                _text(
                    _lot_contract_value(item, "underlying_symbol", "symbol"),
                    upper=True,
                ),
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
    lot_ids = item.get("current_account_member_record_ids")
    bindings = item.get("member_bindings_for_current_account")
    reason_codes = item.get("reason_codes")
    if not _canonical_text_list(lot_ids):
        reasons.add("combo_group_member_ids_noncanonical")
    if not _canonical_text_list(reason_codes):
        reasons.add("combo_group_reason_codes_noncanonical")
    binding_rows: list[dict[str, Any]] = []
    cc_lp_roles = False
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
        binding_lot_ids = [
            binding["record_id"] for binding in canonical_bindings
        ]
        cc_lp_roles = {
            binding.get("role") for binding in binding_rows
        } == {"short_call", "long_put"}
        if (
            not isinstance(lot_ids, list)
            or binding_lot_ids != lot_ids
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
            or not isinstance(lot_ids, list)
            or len(lot_ids) != 2
            or not isinstance(bindings, list)
            or len(bindings) != 2
            or not (
                len(funding_bindings) == 1
                and len(participation_bindings) == 1
                or cc_lp_roles
            )
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
    elif item.get("status") == "released":
        if (item.get("global_current_member_count") != 0 or item.get("global_historical_member_count") != 2
                or item.get("external_member_count") != 0 or item.get("retag_event_count") != 0
                or item.get("cross_account_member_present") is not False
                or item.get("cross_symbol_member_present") is not False or lot_ids != [] or bindings != []
                or item.get("external_membership_hash") != canonical_sha256([])
                or item.get("retag_history_hash") != canonical_sha256([]) or reason_codes != []):
            reasons.add("combo_group_released_membership_invalid")
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
    released_groups: set[str]
    historical_by_group: dict[str, set[str]]
    retag_by_group: dict[str, list[tuple[str, str, str, str]]]
    open_bindings: dict[str, dict[str, Any]]
    # lot_id -> the group currently bound to it, replayed from the open and
    # adjust events. This is the strategy side's answer now that the lot
    # payload no longer carries ``strategy_group_id``.
    group_by_record: dict[str, str]
    # lot_id -> ``strategy_group_id`` / ``leg_role`` / ``strategy`` as the
    # event layer last bound them.
    binding_by_record: dict[str, dict[str, str]]


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
    # The strategy family's home: ``strategy_group_id`` / ``leg_role`` /
    # ``strategy`` are replayed per lot from the open event and the adjust
    # patches that follow it, because the lot payload no longer carries them.
    binding_by_record: dict[str, dict[str, str]] = {}
    historical: dict[str, set[str]] = {}
    retags: dict[str, list[tuple[str, str, str, str]]] = {}
    open_bindings: dict[str, dict[str, Any]] = {}
    released: set[str] = set()
    handled: set[str] = set()
    accepted: set[str] = set()
    lot_strategy_metadata_from_trade_events(effective, accepted_proof_event_ids=accepted)
    for item in effective:
        event_type = _text(item.get("event_type"), lower=True)
        event_id = _text(item.get("event_id"))
        raw = item.get("raw_payload")
        payload = dict(raw) if isinstance(raw, Mapping) else {}
        if event_type == "open":
            lot_id = lot_id_for_open_event(item)
            if lot_id in open_bindings and open_bindings[lot_id]["open_event_id"] != event_id:
                raise ValueError("duplicate_lot_id")
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
                "record_id": lot_id,
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
            open_bindings[lot_id] = binding
            group_value = _group_id(
                fields.get("strategy_group_id")
                or payload.get("strategy_group_id")
                or _snapshot_value(payload, "strategy_group_id")
            )
            group_by_record[lot_id] = group_value
            binding_by_record[lot_id] = {
                "strategy_group_id": group_value,
                "leg_role": binding["role"],
                "strategy": binding["strategy"],
            }
            if group_value:
                historical.setdefault(group_value, set()).add(lot_id)
            continue
        if event_type != "adjust":
            continue
        lot_id = _text(
            item.get("target_lot_id") or payload.get("target_lot_id")
        )
        patch = payload.get("patch")
        if not lot_id or not isinstance(patch, Mapping):
            continue
        if "strategy_group_id" not in patch:
            continue
        decision = payload.get("attribution_decision")
        if decision is not None:
            if event_id in handled:
                continue
            try:
                if event_id not in accepted:
                    raise ValueError("attribution decision was not accepted by replay")
                members = decision["members"]
                changes = {member["lot_id"]: member for member in members}
                affected = {str(member[side].get("strategy_group_id") or "")
                            for member in members for side in ("before", "after")} - {""}
                final = {**group_by_record, **{key: str(member["after"].get("strategy_group_id") or "")
                                             for key, member in changes.items()}}
                for group in affected:
                    prior_ids = {key for key, value in group_by_record.items() if value == group}
                    final_ids = {key for key, value in final.items() if value == group}
                    if not prior_ids <= changes.keys() or len(final_ids) not in {0, 2}:
                        raise ValueError("attribution Combo member closure is incomplete")
                    history_ids = historical.get(group, set())
                    if history_ids and final_ids and history_ids != final_ids:
                        raise ValueError("attribution cannot change immutable Combo identity")
                    if final_ids:
                        roles = {changes[key]["after"].get("leg_role") for key in final_ids}
                        if roles not in ({"funding_put", "participation_call"}, {"short_call", "long_put"}):
                            raise ValueError("attribution Combo roles are invalid")
                for group in affected:
                    final_ids = {key for key, value in final.items() if value == group}
                    if final_ids:
                        historical.setdefault(group, set()).update(final_ids)
                        released.discard(group)
                    else:
                        released.add(group)
                for key, member in changes.items():
                    group_by_record[key] = final[key]
                    binding_by_record[key] = {field: _text(member["after"].get(field))
                        for field in ("strategy_group_id", "leg_role", "strategy")}
                handled.update(member["proof_event_id"] for member in members)
                continue
            except (TypeError, ValueError, KeyError, AttributeError):
                # An incomplete or forged decision cannot authorize a retag.
                for group in {_group_id(group_by_record.get(lot_id)), _group_id(patch.get("strategy_group_id"))} - {""}:
                    retags.setdefault(group, []).append((event_id, lot_id, group, "invalid_decision"))
                continue
        before = group_by_record.get(lot_id, "")
        after = _group_id(patch.get("strategy_group_id"))
        group_by_record[lot_id] = after
        recorded = binding_by_record.setdefault(
            lot_id,
            {"strategy_group_id": before, "leg_role": "", "strategy": ""},
        )
        recorded["strategy_group_id"] = after
        for key in ("leg_role", "strategy"):
            if key in patch:
                recorded[key] = _text(patch.get(key), lower=True)
        if after:
            historical.setdefault(after, set()).add(lot_id)
        if before and after and before != after:
            occurrence = (event_id, lot_id, before, after)
            retags.setdefault(before, []).append(occurrence)
            retags.setdefault(after, []).append(occurrence)
    return _GroupHistory(
        released_groups=released,
        historical_by_group=historical,
        retag_by_group=retags,
        open_bindings=open_bindings,
        group_by_record=group_by_record,
        binding_by_record=binding_by_record,
    )


def resolve_lot_group_bindings(
    trade_events: Iterable[Mapping[str, Any]],
) -> dict[str, dict[str, str]]:
    """``record_id`` -> the strategy family as the event layer last bound it.

    ``strategy_group_id`` / ``leg_role`` / ``strategy`` are no longer in the lot
    payload (``write-side-definition.md`` §2 RECONSTRUCTIBLE; §7 moves the
    family to the strategy/event side). Consumers that used to read them off a
    lot ask this instead: open-event payload, then the adjust patches that
    follow it, with voided events excluded.
    """
    return _effective_group_history(trade_events).binding_by_record


def _current_lot_rows(
    projected_position_lots: Iterable[Any],
) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for raw in projected_position_lots:
        if isinstance(raw, Mapping):
            lot_id = _text(raw.get("record_id") or raw.get("lot_id"))
            fields_raw = raw.get("fields")
            fields = (
                dict(fields_raw)
                if isinstance(fields_raw, Mapping)
                else dict(raw)
            )
        else:
            lot_id = _text(
                getattr(raw, "record_id", None)
                or getattr(raw, "lot_id", None)
            )
            fields = dict(getattr(raw, "fields", {}) or {})
        if not lot_id:
            raise ValueError("projected combo membership lot requires record_id")
        if lot_id in out:
            raise ValueError(f"duplicate projected combo lot: {lot_id}")
        out[lot_id] = fields
    return out


def _allowlisted_binding(
    lot_id: str,
    fields: Mapping[str, Any],
    *,
    binding: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    # ``leg_role`` / ``strategy`` left the lot payload; the event layer's
    # replay (``_GroupHistory.binding_by_record``) answers them, with the
    # retired flat keys kept as the legacy-row fallback.
    replay = dict(binding or {})
    return {
        "record_id": lot_id,
        "role": _text(
            replay.get("leg_role") or fields.get("leg_role"), lower=True
        ),
        "open_event_id": _text(
            fields.get("open_event_id") or fields.get("source_event_id")
        ),
        "strategy": _text(
            replay.get("strategy") or fields.get("strategy"), lower=True
        ),
        "account": _text(_lot_contract_value(fields, "account", "account"), lower=True),
        "symbol": _text(
            _lot_contract_value(fields, "underlying_symbol", "symbol"), upper=True
        ),
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


def _controlled_pair_adoption(
    rows: list[dict[str, Any]], *, binding: Mapping[str, Any],
    opening: TradeEvent, group_id: str, role: str, instant: int,
    voided_ids: set[str],
) -> tuple[str, int] | None:
    """Recognize an exact pre-assignment post-trade Combo adoption."""
    candidates = [
        row for row in rows
        if row.get("event_type") == "adjust"
        and row.get("target_lot_id") == binding["record_id"]
        and opening.event_time_ms < (_integer(row.get("event_time_ms")) or 0) < instant
        and row.get("source") in {"post_trade_combo_reconciliation", "trade_attribution"}
        and row.get("event_id") not in voided_ids
    ]
    candidates.sort(key=lambda row: (int(row["event_time_ms"]), row["event_id"]))
    if candidates and "attribution_decision" in (candidates[-1].get("raw_payload") or {}):
        decision = candidates[-1]["raw_payload"]["attribution_decision"]
        try:
            accepted: set[str] = set()
            current = lot_strategy_metadata_from_trade_events(
                [row for row in rows if int(row.get("event_time_ms") or 0) < instant],
                accepted_proof_event_ids=accepted)
            if candidates[-1]["event_id"] not in accepted:
                return None
            member = next(row for row in decision["members"] if row["lot_id"] == binding["record_id"])
            if (any(current.get(member["lot_id"], {}).get(key) != value for key, value in member["after"].items())
                    or member["open_event_id"] != opening.event_id or member["after"]["strategy"] != "combo_yield"
                    or member["after"]["strategy_group_id"] != group_id or member["after"]["leg_role"] != role):
                return None
            return decision["request_id"], int(candidates[-1]["event_time_ms"])
        except (TypeError, ValueError, KeyError, StopIteration):
            return None
    # Legacy adoption can only claim an unowned opening; complete decisions
    # above also prove an explicitly authorized transfer from an existing owner.
    metadata = resolve_strategy_metadata(opening.raw_payload, source_id=opening.event_id)
    if metadata.issues or any((metadata.metadata.strategy, metadata.metadata.strategy_group_id,
                               metadata.metadata.leg_role)):
        return None
    if len(candidates) != 1:
        return None
    row = candidates[0]
    raw = row.get("raw_payload")
    if not isinstance(raw, Mapping):
        return None
    inference_id = _text(raw.get("inference_id"))
    event_id = "combo-adopt:v1:" + canonical_sha256(
        {"inference_id": inference_id, "role": role}
    )
    patch = raw.get("patch")
    if (
        not inference_id.startswith("combo-inference:v1:")
        or row.get("event_id") != event_id
        or event_id in voided_ids
        or raw.get("source") != "post_trade_combo_reconciliation"
        or raw.get("source_type") != "combo_pair_inference"
        or raw.get("mode") != "post_trade_combo_adoption"
        or raw.get("idempotency_key") != event_id
        or raw.get("record_id") != binding["record_id"]
        or raw.get("target_lot_id") != binding["record_id"]
        or raw.get("adjust_target_source_event_id") != opening.event_id
        or not isinstance(patch, Mapping)
        or set(patch) != {"strategy", "strategy_group_id", "leg_role", "last_action_at"}
        or patch.get("strategy") != "combo_yield"
        or patch.get("strategy_group_id") != group_id
        or patch.get("leg_role") != role
        or _integer(patch.get("last_action_at")) != row.get("event_time_ms")
    ):
        return None
    try:
        adoption = TradeEvent.from_dict(row)
    except (TypeError, ValueError):
        return None
    if (
        adoption.contract_key != opening.contract_key
        or adoption.currency != opening.currency
        or adoption.multiplier != opening.multiplier
        or adoption.contracts != 0
    ):
        return None
    return inference_id, adoption.event_time_ms


def resolve_combo_assignment_proof(
    *,
    assignment: TradeEvent,
    group_id: str,
    trade_events: Iterable[Mapping[str, Any]],
    identities: Iterable[Mapping[str, Any]],
) -> tuple[str | None, str | None]:
    """Prove the pair as it existed before a short-leg assignment."""
    instant = assignment.event_time_ms
    if instant <= 0 or not assignment.target_lot_id:
        return None, "combo_assignment_source_invalid"
    rows = [dict(row) for row in trade_events]
    strategy_rows = [row for row in rows if (_integer(row.get("event_time_ms")) or 0) < instant]
    prefix = [
        row for row in rows
        if _text(row.get("event_type"), lower=True) != "void"
        and (_integer(row.get("event_time_ms")) or 0) < instant
    ]
    prefix_ids = {_text(row.get("event_id")) for row in prefix}
    prefix.extend(
        row for row in rows
        if valid_void_target_event_id(row) in prefix_ids
    )
    try:
        projected = project_trade_event_log(prefix)
        membership = resolve_combo_group_membership(
            group_id=group_id,
            account=assignment.contract_key.account,
            expected_symbol=assignment.contract_key.underlying_symbol,
            trade_events=strategy_rows,
            projected_position_lots=projected.lots,
        )
    except (TypeError, ValueError):
        return None, "combo_assignment_projection_invalid"
    if membership.fact["status"] != "exact":
        return None, "combo_assignment_membership_unresolved"

    bindings = membership.fact["member_bindings_for_current_account"]
    role_to_binding = {item["role"]: item for item in bindings}
    if len(role_to_binding) != 2:
        return None, "combo_assignment_roles_invalid"
    if set(role_to_binding) == {"short_call", "long_put"}:
        variant = "cc_lp"
        put_binding = role_to_binding["long_put"]
        call_binding = role_to_binding["short_call"]
        short_binding = call_binding
    else:
        variant = "csp_lc"
        put_binding = next((item for item in bindings if item["role"] in FUNDING_PUT_ROLES), None)
        call_binding = next((item for item in bindings if item["role"] in PARTICIPATION_CALL_ROLES), None)
        short_binding = put_binding
    if put_binding is None or call_binding is None or short_binding is None:
        return None, "combo_assignment_roles_invalid"
    if short_binding["record_id"] != assignment.target_lot_id:
        return None, "combo_assignment_short_leg_mismatch"

    lots = {lot.lot_id: lot for lot in projected.ledger_projection.lots}
    opens = {
        _text(row.get("event_id")): row
        for row in prefix
        if _text(row.get("event_type"), lower=True) == "open"
    }
    leg_events: dict[str, TradeEvent] = {}
    for label, binding in (("put", put_binding), ("call", call_binding)):
        lot = lots.get(binding["record_id"])
        raw_open = opens.get(binding["open_event_id"])
        if lot is None or raw_open is None or lot.open_event_id != binding["open_event_id"]:
            return None, "combo_assignment_open_binding_invalid"
        try:
            opening = TradeEvent.from_dict(raw_open)
        except (TypeError, ValueError):
            return None, "combo_assignment_open_binding_invalid"
        if (
            opening.contract_key != lot.contract_key
            or opening.currency != lot.currency
            or opening.multiplier != lot.multiplier
            or opening.contracts != lot.contracts_opened
            or opening.position_side != lot.position_side
        ):
            return None, "combo_assignment_open_binding_invalid"
        leg_events[label] = opening
    put_lot, call_lot = lots[put_binding["record_id"]], lots[call_binding["record_id"]]
    if not _valid_combo_pair(put_lot, call_lot, kind=variant):
        return None, "combo_assignment_structure_invalid"
    short_open = leg_events["put" if variant == "csp_lc" else "call"]
    if (
        short_open.contract_key != assignment.contract_key
        or short_open.currency != assignment.currency
        or short_open.multiplier != assignment.multiplier
    ):
        return None, "combo_assignment_short_leg_mismatch"

    expected_roles = (
        (("put", frozenset({"long_put"})), ("call", frozenset({"short_call"})))
        if variant == "cc_lp" else
        (("put", FUNDING_PUT_ROLES), ("call", PARTICIPATION_CALL_ROLES))
    )
    adopted_roles = []
    for label, roles in expected_roles:
        opening = leg_events[label]
        metadata = resolve_strategy_metadata(
            opening.raw_payload, source_id=opening.event_id,
        )
        if (
            not metadata.issues
            and metadata.metadata.strategy == "combo_yield"
            and metadata.metadata.strategy_group_id == group_id
            and metadata.metadata.leg_role in roles
        ):
            continue
        if variant != "csp_lc" or metadata.issues:
            return None, "combo_assignment_open_identity_unproven"
        adopted_roles.append((label, (put_binding if label == "put" else call_binding)["role"]))
    if adopted_roles:
        if len(adopted_roles) != 2:
            return None, "combo_assignment_open_identity_unproven"
        voided_ids = {
            target for row in strategy_rows
            for target in [valid_void_target_event_id(row)]
            if target
        }
        proofs = [
            _controlled_pair_adoption(
                strategy_rows, binding=put_binding if label == "put" else call_binding,
                opening=leg_events[label], group_id=group_id, role=role,
                instant=instant, voided_ids=voided_ids,
            )
            for label, role in adopted_roles
        ]
        if any(proof is None for proof in proofs) or len(set(proofs)) != 1:
            return None, "combo_assignment_open_identity_unproven"
    if variant == "csp_lc":
        matching = [
            dict(item) for item in identities
            if _group_id(item.get("group_id")) == group_id
        ]
        if len(matching) != 1:
            return None, "combo_assignment_identity_missing"
        identity = matching[0]
        validated = validate_combo_identity(identity)
        expected = {
            "strategy": "combo_yield",
            "account": assignment.contract_key.account,
            "symbol": assignment.contract_key.underlying_symbol,
            "funding_put_record_id": put_binding["record_id"],
            "funding_put_open_event_id": put_binding["open_event_id"],
            "participation_call_record_id": call_binding["record_id"],
            "participation_call_open_event_id": call_binding["open_event_id"],
            "original_contracts": put_lot.contracts_opened,
        }
        # Historical identities bind the stored open key, including legacy position fields.
        contract_keys_match = all(
            identity.get(key) in (
                opens[binding["open_event_id"]].get("contract_key"),
                leg_events[label].contract_key.to_dict(),
            )
            for key, label, binding in (
                ("funding_put_contract_key", "put", put_binding),
                ("participation_call_contract_key", "call", call_binding),
            )
        )
        if (
            validated.status != "valid"
            or validated.identity_hash != identity.get("identity_hash")
            or any(identity.get(key) != value for key, value in expected.items())
            or not contract_keys_match
        ):
            return None, "combo_assignment_identity_mismatch"
    return variant, None


__all__ = [
    "COMBO_GROUP_MEMBERSHIP_SCHEMA",
    "ComboMembershipResolution",
    "ComboMembershipValidation",
    "resolve_account_combo_memberships",
    "resolve_combo_assignment_proof",
    "resolve_combo_group_membership",
    "validate_combo_group_membership",
]


def _identity_leg(record: Mapping[str, Any], *, open_event_id: str, group_id: str, leg_role: str) -> dict[str, Any]:
    fields = dict(record["fields"])
    contract = ContractKey.from_values(**{key: _lot_contract_value(fields, key, flat)
        for key, flat in (("broker", "broker"), ("account", "account"), ("underlying_symbol", "symbol"),
            ("option_type", "option_type"), ("strike", "strike"), ("expiration_ymd", "expiration_ymd"))})
    return {"strategy_group_id": group_id, "strategy": "combo_yield", "leg_role": leg_role,
        "broker": contract.broker, "account": contract.account, "symbol": contract.underlying_symbol,
        "contracts": effective_contracts(fields), "open_event_id": open_event_id,
        "record_id": record["record_id"], "contract_key": contract.to_dict(),
        "currency": str(fields.get("currency") or "").strip().upper(),
        "multiplier": require_option_multiplier(fields.get("multiplier")), "strike": float(contract.strike),
        "expiration_ymd": contract.expiration_ymd}

def publish_combo_pair_identity(sqlite_repo: Any, *, conn: Any, inference: Mapping[str, Any]) -> tuple[Any, Any]:
    """Validate and persist the immutable pair after all member patches are visible."""
    group_id = str(inference["strategy_group_id"])
    events = sqlite_repo.list_trade_events(conn=conn)
    projection_lots = sqlite_repo.list_position_lots(conn=conn)
    projected_by_id = {
        str(item.get("record_id") or ""): item
        for item in projection_lots
    }
    put_leg = _identity_leg(
        projected_by_id[str(inference["put_record_id"])],
        open_event_id=str(inference["put_open_event_id"]),
        group_id=group_id,
        leg_role="funding_put",
    )
    call_leg = _identity_leg(
        projected_by_id[str(inference["call_record_id"])],
        open_event_id=str(inference["call_open_event_id"]),
        group_id=group_id,
        leg_role="participation_call",
    )
    intent = build_combo_identity_intent(first_leg=put_leg, second_leg=call_leg)
    identity = identity_from_intent(
        intent,
        first_leg=put_leg,
        second_leg=call_leg,
    )
    membership = resolve_combo_group_membership(
        group_id=group_id,
        account=str(inference["account"]),
        expected_symbol=str(inference["symbol"]),
        trade_events=events,
        projected_position_lots=projection_lots,
    )
    if (
        membership.fact.get("status") != "exact"
        or set(membership.fact.get("current_account_member_record_ids") or [])
        != {str(inference["put_record_id"]), str(inference["call_record_id"])}
    ):
        raise ValueError("post-trade Combo adoption membership is not exact")
    sqlite_repo.insert_strategy_group_identity(identity, conn=conn)
    return identity, membership
