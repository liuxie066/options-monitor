from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from domain.domain.daily_decision_event_risk import (
    candidate_event_risk_transitions,
    normalize_candidate_event_risk,
)
from domain.domain.strategy_vocab import canonical_strategy_id
from domain.domain.symbol_identity import canonical_symbol, symbol_market


DAILY_DECISION_BRIEF_SCHEMA_VERSION = "daily_decision_brief.v1"
DAILY_DECISION_BRIEF_DIFF_SCHEMA_VERSION = "daily_decision_brief_diff.v1"

ACTIONABILITIES = frozenset({"live_actionable", "planning_only", "blocked"})
ACTION_PRIORITIES = ("P0", "P1", "P2")
ACTION_STATES = frozenset({"active", "invalidated", "blocked", "observe"})
_CANDIDATE_STRATEGY_FAMILIES = frozenset(
    {"sell_put", "covered_call", "combo_yield", "wheel"}
)
_CANDIDATE_REPRESENTATIVE_FIELDS = (
    "rank",
    "symbol",
    "strategy_family",
    "option_type",
    "contract_symbol",
    "expiration",
    "strike",
    "strategy_group_id",
    "position_lot_id",
    "wheel_branch_id",
    "candidate_pair_id",
    "structure_mode",
    "put_contract_symbol",
    "call_contract_symbol",
    "put_expiration",
    "call_expiration",
    "put_strike",
    "call_strike",
    "currency",
    "multiplier",
    "put_sell_reference",
    "call_buy_reference",
    "priority",
    "metrics",
    "capacity",
    "event_risk",
)

# Retired fields are never part of the current normalized/write contract.  Raw
# persisted values are consulted only when reconstructing an exact historical
# digest candidate for immutable overlay-era revisions.
RETIRED_DAILY_BRIEF_FIELDS = (
    "ai_decision_advice",
    "ai_decision_advice_evidence_index",
)

_STABLE_ACTION_ID_FIELDS = (
    "action_type",
    "strategy_family",
    "account",
    "symbol",
    "option_type",
    "side",
    "expiration",
    "strike",
    "contract_symbol",
    "position_lot_id",
    "strategy_group_id",
    "leg_role",
)

_EVIDENCE_HOLD_MUTABLE_FIELDS = frozenset(
    {"state", "evidence_state", "evidence_gap_key", "evidence_reason"}
)


def build_daily_brief_id(*, market: Any, market_trading_date: Any, account: Any) -> str:
    identity = {
        "market": _upper(market),
        "market_trading_date": str(market_trading_date or "").strip(),
        "account": _lower(account),
    }
    if not all(identity.values()):
        raise ValueError("market, market_trading_date, and account are required")
    return "daily-brief-" + _digest(identity)[:24]


def build_daily_brief_candidate_identity(
    *,
    account: Any,
    market: Any,
    symbol: Any,
    strategy_family: Any,
    position_lot_id: Any = None,
    wheel_branch_id: Any = None,
) -> str:
    account_norm = _lower(account)
    market_norm = _upper(market)
    symbol_norm = canonical_symbol(symbol)
    family_norm = canonical_strategy_id(str(strategy_family or ""))
    if family_norm == "sell_call":
        family_norm = "covered_call"
    if not account_norm or ":" in account_norm:
        raise ValueError("valid account is required for candidate identity")
    if market_norm not in {"US", "HK", "CN"}:
        raise ValueError(f"unsupported candidate market: {market_norm}")
    if not symbol_norm or symbol_market(symbol_norm) != market_norm:
        raise ValueError(f"candidate symbol does not belong to market {market_norm}: {symbol!r}")
    if family_norm not in _CANDIDATE_STRATEGY_FAMILIES:
        raise ValueError(f"unsupported candidate strategy family: {strategy_family!r}")
    identity = f"candidate:v1:{account_norm}:{market_norm}:{symbol_norm}:{family_norm}"
    if family_norm != "wheel":
        return identity
    branch_id = str(wheel_branch_id or position_lot_id or "").strip()
    if not branch_id or ":" in branch_id:
        raise ValueError(
            "valid wheel_branch_id or position_lot_id is required for Wheel candidate identity"
        )
    return f"{identity}:{branch_id}"


def decide_daily_brief_notification(
    *,
    ran_scan: bool,
    pipeline_reliable: bool,
    fixed_due: bool,
    pending_candidate_identities: list[str] | tuple[str, ...],
    retryable_envelope_kind: str | None = None,
) -> dict[str, Any]:
    """Choose the one allowed Daily Brief delivery action for this tick."""

    retry_kind = str(retryable_envelope_kind or "").strip().lower() or None
    if not ran_scan:
        return {
            "action": "retry_exact" if retry_kind else "none",
            "reason": "retryable_envelope" if retry_kind else "no_scan_no_retry",
        }
    if fixed_due:
        if pipeline_reliable:
            return {
                "action": "fixed_report",
                "reason": "fixed_report_due",
            }
        return {
            "action": "fixed_failure",
            "reason": "fixed_scan_failed",
        }
    if not pipeline_reliable:
        return {"action": "none", "reason": "nonfixed_scan_failed"}
    if pending_candidate_identities:
        return {"action": "candidate_alert", "reason": "pending_candidates"}
    return {"action": "none", "reason": "no_pending_candidates"}


def build_daily_brief_action_id(action: Mapping[str, Any]) -> str:
    identity = _legacy_action_identity(action)
    if identity["action_type"] == "open_combo_yield":
        candidate_pair_id = str(action.get("candidate_pair_id") or "").strip()
        if not candidate_pair_id:
            raise ValueError("candidate_pair_id is required for Combo action identity")
        identity["candidate_pair_id"] = candidate_pair_id
    return "action-" + _digest(identity)[:24]


def _build_legacy_combo_action_id(action: Mapping[str, Any]) -> str:
    identity = _legacy_action_identity(action)
    if identity["action_type"] != "open_combo_yield":
        raise ValueError("legacy Combo action identity requires open_combo_yield")
    return "action-" + _digest(identity)[:24]


def _legacy_action_identity(action: Mapping[str, Any]) -> dict[str, str]:
    identity = {
        field: _normalize_action_identity_value(field, action.get(field))
        for field in _STABLE_ACTION_ID_FIELDS
    }
    if not identity["action_type"]:
        raise ValueError("action_type is required for daily brief action identity")
    if not identity["account"]:
        raise ValueError("account is required for daily brief action identity")
    wheel_branch_id = str(action.get("wheel_branch_id") or "").strip()
    position_lot_id = str(action.get("position_lot_id") or "").strip()
    if (
        identity["strategy_family"] == "wheel"
        and wheel_branch_id
        and wheel_branch_id != position_lot_id
    ):
        identity["wheel_branch_id"] = wheel_branch_id
    return identity


def _validate_legacy_hold_action_source(
    action: Mapping[str, Any],
    source: Mapping[str, Any],
) -> None:
    action_fixed = {
        key: value
        for key, value in dict(action).items()
        if key not in _EVIDENCE_HOLD_MUTABLE_FIELDS
    }
    source_fixed = {
        key: value
        for key, value in dict(source).items()
        if key not in _EVIDENCE_HOLD_MUTABLE_FIELDS
    }
    if _json_safe(action_fixed) != _json_safe(source_fixed):
        raise ValueError("evidence hold action differs from its persisted source")


def normalize_daily_brief_action(action: Mapping[str, Any]) -> dict[str, Any]:
    return _normalize_daily_brief_action(action, persisted=False)


def _normalize_daily_brief_action(
    action: Mapping[str, Any],
    *,
    persisted: bool,
    legacy_hold_source: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    src = dict(action or {})
    priority = str(src.get("priority") or "P2").strip().upper()
    if priority not in ACTION_PRIORITIES:
        raise ValueError(f"unsupported daily brief action priority: {priority}")
    state = str(src.get("state") or "active").strip().lower()
    if state not in ACTION_STATES:
        raise ValueError(f"unsupported daily brief action state: {state}")

    out = dict(src)
    out["priority"] = priority
    out["state"] = state
    out["action_type"] = _lower(src.get("action_type"))
    out["strategy_family"] = _lower(src.get("strategy_family"))
    out["account"] = _lower(src.get("account"))
    out["symbol"] = _upper(src.get("symbol"))
    out["option_type"] = _lower(src.get("option_type"))
    out["side"] = _lower(src.get("side"))
    out["expiration"] = str(src.get("expiration") or "").strip()
    out["strike"] = _canonical_number(src.get("strike"))
    out["contract_symbol"] = _upper(src.get("contract_symbol"))
    out["position_lot_id"] = str(src.get("position_lot_id") or "").strip()
    wheel_branch_id = str(src.get("wheel_branch_id") or "").strip()
    if out["strategy_family"] == "wheel" and wheel_branch_id:
        out["wheel_branch_id"] = wheel_branch_id
    else:
        out.pop("wheel_branch_id", None)
    out["strategy_group_id"] = str(src.get("strategy_group_id") or "").strip()
    out["leg_role"] = _lower(src.get("leg_role"))
    if out["action_type"] == "open_combo_yield":
        candidate_pair_id = str(src.get("candidate_pair_id") or "").strip()
        if candidate_pair_id or "candidate_pair_id" in src:
            out["candidate_pair_id"] = candidate_pair_id
        else:
            out.pop("candidate_pair_id", None)
    if out["action_type"] in {"open_candidate", "open_combo_yield"}:
        out["event_risk"] = normalize_candidate_event_risk(src.get("event_risk"))

    supplied_id = str(src.get("action_id") or "").strip()
    if legacy_hold_source is not None:
        _validate_legacy_hold_action_source(src, legacy_hold_source)
        persisted = True
    if not persisted or not supplied_id:
        expected_id = build_daily_brief_action_id(out)
        if (
            out["action_type"] == "open_combo_yield"
            and supplied_id
            and supplied_id != expected_id
        ):
            raise ValueError(
                f"daily brief action_id mismatch: {supplied_id!r} != {expected_id!r}"
            )
        out["action_id"] = expected_id
        return out

    if out["action_type"] == "open_combo_yield":
        current_id = (
            build_daily_brief_action_id(out)
            if str(out.get("candidate_pair_id") or "").strip()
            else None
        )
        legacy_id = _build_legacy_combo_action_id(out)
        if supplied_id == current_id:
            pass
        elif supplied_id == legacy_id:
            if "candidate_pair_id" in src:
                out["candidate_pair_id"] = src["candidate_pair_id"]
            else:
                out.pop("candidate_pair_id", None)
        else:
            raise ValueError("persisted Combo action_id does not match a supported algorithm")
    else:
        expected_id = "action-" + _digest(_legacy_action_identity(out))[:24]
        if supplied_id != expected_id:
            raise ValueError(
                f"persisted daily brief action_id mismatch: {supplied_id!r} != {expected_id!r}"
            )
    out["action_id"] = supplied_id
    return out


def normalize_daily_decision_brief(payload: Mapping[str, Any]) -> dict[str, Any]:
    return _normalize_daily_decision_brief(payload, persisted=False)


def normalize_persisted_daily_decision_brief(
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate stored action identities while preserving their saved IDs."""

    return _normalize_daily_decision_brief(payload, persisted=True)


def _normalize_daily_decision_brief(
    payload: Mapping[str, Any],
    *,
    persisted: bool,
    legacy_hold_actions: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    src = dict(payload or {})
    schema_version = str(src.get("schema_version") or DAILY_DECISION_BRIEF_SCHEMA_VERSION).strip()
    if schema_version != DAILY_DECISION_BRIEF_SCHEMA_VERSION:
        raise ValueError(f"unsupported daily brief schema version: {schema_version}")

    market = _upper(src.get("market"))
    market_date = str(src.get("market_trading_date") or "").strip()
    account = _lower(src.get("account"))
    revision = _nonnegative_int(src.get("revision"), field="revision")
    actionability = str(src.get("actionability") or "blocked").strip().lower()
    if actionability not in ACTIONABILITIES:
        raise ValueError(f"unsupported daily brief actionability: {actionability}")

    normalized_actions = []
    for item in _mapping_list(src.get("actions"), field="actions"):
        supplied_id = str(item.get("action_id") or "").strip()
        hold_source = (
            legacy_hold_actions.get(supplied_id)
            if supplied_id and legacy_hold_actions is not None
            else None
        )
        normalized_actions.append(
            _normalize_daily_brief_action(
                item,
                persisted=persisted,
                legacy_hold_source=hold_source,
            )
        )
    action_ids: set[str] = set()
    for action in normalized_actions:
        action_id = str(action["action_id"])
        if action_id in action_ids:
            raise ValueError(f"duplicate daily brief action_id: {action_id}")
        action_ids.add(action_id)

    out = {
        key: value
        for key, value in src.items()
        if key not in RETIRED_DAILY_BRIEF_FIELDS
    }
    out.update(
        {
            "schema_version": schema_version,
            "brief_id": build_daily_brief_id(
                market=market,
                market_trading_date=market_date,
                account=account,
            ),
            "market": market,
            "market_trading_date": market_date,
            "account": account,
            "revision": revision,
            "run_id": str(src.get("run_id") or "").strip(),
            "generated_at_utc": _iso_or_empty(src.get("generated_at_utc")),
            "data_as_of_utc": _iso_or_empty(src.get("data_as_of_utc")),
            "valid_until_utc": _iso_or_empty(src.get("valid_until_utc")),
            "status": str(src.get("status") or "unknown").strip().lower(),
            "actionability": actionability,
            "strategy_summary": str(src.get("strategy_summary") or "").strip(),
            "actions": normalized_actions,
            "positions": _mapping_list(src.get("positions"), field="positions"),
            "capacity": _mapping(src.get("capacity"), field="capacity"),
            "funds": _normalize_daily_brief_funds(src.get("funds")),
            "candidates": _normalize_candidate_groups(src.get("candidates")),
            "candidate_index": _normalize_candidate_index(
                src.get("candidate_index"),
                account=account,
                market=market,
                actionability=actionability,
                actions=normalized_actions,
                persisted=persisted,
            ),
            "rejections": _mapping(src.get("rejections"), field="rejections"),
            "events": _mapping_list(src.get("events"), field="events"),
            "data_gaps": _mapping_list(src.get("data_gaps"), field="data_gaps"),
            "source_artifacts": _mapping_list(src.get("source_artifacts"), field="source_artifacts"),
        }
    )
    if "wheel_batches" in src:
        out["wheel_batches"] = _mapping_list(
            src.get("wheel_batches"), field="wheel_batches"
        )
    return out


def reconcile_daily_decision_brief_evidence(
    previous: Mapping[str, Any],
    current: Mapping[str, Any],
) -> dict[str, Any]:
    """Carry candidate identity across a run with typed family-level data gaps."""

    prev = normalize_persisted_daily_decision_brief(previous)
    cur = normalize_daily_decision_brief(current)
    _ensure_same_brief_identity(prev, cur)
    gaps = {
        (
            _upper(item.get("symbol")),
            _lower(item.get("strategy_family")),
        ): dict(item)
        for item in cur.get("data_gaps") or []
        if isinstance(item, Mapping)
        and _upper(item.get("symbol"))
        and _lower(item.get("strategy_family"))
    }
    current_actions = {
        str(action.get("action_id") or ""): action
        for action in cur.get("actions") or []
        if isinstance(action, Mapping)
    }
    aligned_current_to_previous = _align_combo_action_ids(prev, cur)
    aligned_previous_ids = set(aligned_current_to_previous.values())
    additions: list[dict[str, Any]] = []
    legacy_hold_actions: dict[str, dict[str, Any]] = {}
    for prior in prev.get("actions") or []:
        if not isinstance(prior, Mapping) or not _is_opening_candidate_action(prior):
            continue
        action_id = str(prior.get("action_id") or "")
        if (
            not action_id
            or action_id in current_actions
            or action_id in aligned_previous_ids
        ):
            continue
        active_candidate = (
            prior.get("state") == "active"
            and prior.get("priority") in {"P0", "P1"}
        )
        evidence_hold = (
            prior.get("state") == "observe"
            and _lower(prior.get("evidence_state")) == "unavailable"
        )
        if not (active_candidate or evidence_hold):
            continue
        key = (
            _upper(prior.get("symbol")),
            _lower(prior.get("strategy_family")),
        )
        gap = gaps.get(key)
        if gap is None:
            continue
        held = dict(prior)
        held.update(
            {
                "state": "observe",
                "evidence_state": "unavailable",
                "evidence_gap_key": (
                    f"{cur['market']}:{key[0]}:{key[1]}:"
                    f"{str(gap.get('reason') or 'source_unavailable').strip()}"
                ),
                "evidence_reason": str(
                    gap.get("reason") or "source_unavailable"
                ).strip(),
            }
        )
        additions.append(held)
        legacy_hold_actions[action_id] = dict(prior)
    if not additions:
        return cur
    candidate = dict(cur)
    candidate["actions"] = [*cur["actions"], *additions]
    return _normalize_daily_decision_brief(
        candidate,
        persisted=False,
        legacy_hold_actions=legacy_hold_actions,
    )


def _combo_action_generation(action: Mapping[str, Any]) -> str | None:
    if _lower(action.get("action_type")) != "open_combo_yield":
        return None
    saved_id = str(action.get("action_id") or "").strip()
    if not saved_id:
        return None
    pair_id = str(action.get("candidate_pair_id") or "").strip()
    if pair_id and saved_id == build_daily_brief_action_id(action):
        return "current"
    if saved_id == _build_legacy_combo_action_id(action):
        return "legacy"
    return None


def _combo_action_legs(action: Mapping[str, Any]) -> tuple[str, str] | None:
    top_put = _upper(action.get("put_contract_symbol"))
    top_call = _upper(action.get("call_contract_symbol"))
    metrics = action.get("metrics")
    metric_put = _upper(metrics.get("put_contract_symbol")) if isinstance(metrics, Mapping) else ""
    metric_call = _upper(metrics.get("call_contract_symbol")) if isinstance(metrics, Mapping) else ""
    if bool(top_put) != bool(top_call) or bool(metric_put) != bool(metric_call):
        return None
    if top_put and metric_put and (top_put, top_call) != (metric_put, metric_call):
        return None
    legs = (top_put, top_call) if top_put else (metric_put, metric_call)
    return legs if all(legs) else None


def _combo_candidate_pair_claims(
    brief: Mapping[str, Any],
    *,
    symbol: str,
    legs: tuple[str, str],
    action_pair_id: str,
) -> tuple[frozenset[str], bool]:
    pair_bindings: dict[str, set[tuple[str, str, str]]] = {}
    for item in brief.get("candidate_index") or []:
        if not isinstance(item, Mapping) or _lower(item.get("strategy_family")) != "combo_yield":
            continue
        representative = item.get("representative")
        if not isinstance(representative, Mapping):
            continue
        pair_id = str(representative.get("candidate_pair_id") or "").strip()
        if not pair_id:
            continue
        rep_symbol = _upper(representative.get("symbol") or item.get("symbol"))
        rep_legs = (
            _upper(representative.get("put_contract_symbol")),
            _upper(representative.get("call_contract_symbol")),
        )
        pair_bindings.setdefault(pair_id, set()).add(
            (rep_symbol, rep_legs[0], rep_legs[1])
        )
    target = (symbol, legs[0], legs[1])
    claims = {
        pair_id
        for pair_id, bindings in pair_bindings.items()
        if target in bindings
    }
    relevant = set(claims)
    if action_pair_id:
        relevant.add(action_pair_id)
    valid = len(claims) <= 1 and all(
        pair_bindings.get(pair_id, {target}) == {target}
        for pair_id in relevant
    )
    return frozenset(claims), valid


def _combo_alignment_evidence(
    brief: Mapping[str, Any],
    action: Mapping[str, Any],
) -> tuple[tuple[str, str, str, str, str], str, frozenset[str]] | None:
    generation = _combo_action_generation(action)
    legs = _combo_action_legs(action)
    account = _lower(action.get("account"))
    market = _upper(brief.get("market"))
    symbol = _upper(action.get("symbol"))
    if (
        generation is None
        or legs is None
        or not account
        or account != _lower(brief.get("account"))
        or not market
        or not symbol
    ):
        return None
    pair_id = str(action.get("candidate_pair_id") or "").strip()
    if generation == "current" and not pair_id:
        return None
    claims, valid = _combo_candidate_pair_claims(
        brief,
        symbol=symbol,
        legs=legs,
        action_pair_id=pair_id,
    )
    if not valid:
        return None
    combined_claims = set(claims)
    if pair_id:
        combined_claims.add(pair_id)
    if len(combined_claims) > 1:
        return None
    return (
        (account, market, symbol, legs[0], legs[1]),
        generation,
        frozenset(combined_claims),
    )


def _align_combo_action_ids(
    previous: Mapping[str, Any],
    current: Mapping[str, Any],
) -> dict[str, str]:
    previous_actions = {
        str(item.get("action_id") or ""): item
        for item in previous.get("actions") or []
        if isinstance(item, Mapping) and str(item.get("action_id") or "")
    }
    current_actions = {
        str(item.get("action_id") or ""): item
        for item in current.get("actions") or []
        if isinstance(item, Mapping) and str(item.get("action_id") or "")
    }
    exact_ids = set(previous_actions) & set(current_actions)
    previous_by_key: dict[
        tuple[str, str, str, str, str],
        list[tuple[str, str, frozenset[str]]],
    ] = {}
    current_by_key: dict[
        tuple[str, str, str, str, str],
        list[tuple[str, str, frozenset[str]]],
    ] = {}
    for action_id, action in previous_actions.items():
        if action_id in exact_ids:
            continue
        evidence = _combo_alignment_evidence(previous, action)
        if evidence is not None:
            key, generation, claims = evidence
            previous_by_key.setdefault(key, []).append((action_id, generation, claims))
    for action_id, action in current_actions.items():
        if action_id in exact_ids:
            continue
        evidence = _combo_alignment_evidence(current, action)
        if evidence is not None:
            key, generation, claims = evidence
            current_by_key.setdefault(key, []).append((action_id, generation, claims))

    aligned: dict[str, str] = {}
    for key in sorted(set(previous_by_key) & set(current_by_key)):
        prior_rows = previous_by_key[key]
        current_rows = current_by_key[key]
        if len(prior_rows) != 1 or len(current_rows) != 1:
            continue
        prior_id, prior_generation, prior_claims = prior_rows[0]
        current_id, current_generation, current_claims = current_rows[0]
        if {prior_generation, current_generation} != {"legacy", "current"}:
            continue
        if len(set(prior_claims) | set(current_claims)) > 1:
            continue
        aligned[current_id] = prior_id
    return aligned


def _normalize_daily_brief_funds(value: Any) -> dict[str, Any]:
    if value is None:
        return {
            "as_of_utc": "",
            "cash_total_by_currency": {},
            "option_opening_available_by_currency": {},
            "available": False,
            "reason": "not_recorded",
        }
    funds = _mapping(value, field="funds")
    available = bool(funds.get("available"))
    out = {
        "as_of_utc": _iso_or_empty(funds.get("as_of_utc")),
        "cash_total_by_currency": _normalize_currency_amounts(
            funds.get("cash_total_by_currency"),
            field="funds.cash_total_by_currency",
        ),
        "option_opening_available_by_currency": _normalize_currency_amounts(
            funds.get("option_opening_available_by_currency"),
            field="funds.option_opening_available_by_currency",
        ),
        "available": available,
        "reason": str(funds.get("reason") or ("ok" if available else "unavailable")).strip(),
    }
    for key in ("cash_total_cny", "cash_secured_total_cny", "option_opening_available_cny"):
        raw = funds.get(key)
        if raw is None:
            continue
        if isinstance(raw, bool):
            raise ValueError(f"funds.{key} must be a number")
        try:
            out[key] = float(raw)
        except (TypeError, ValueError):
            raise ValueError(f"funds.{key} must be a number") from None
    return out


def _normalize_currency_amounts(value: Any, *, field: str) -> dict[str, float]:
    amounts = _mapping(value, field=field)
    out: dict[str, float] = {}
    for raw_currency, raw_amount in amounts.items():
        currency = _upper(raw_currency)
        if not currency or isinstance(raw_amount, bool):
            raise ValueError(f"{field} contains an invalid currency or amount")
        try:
            amount = float(raw_amount)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"{field}.{currency} must be a finite number") from exc
        if not math.isfinite(amount):
            raise ValueError(f"{field}.{currency} must be a finite number")
        out[currency] = amount
    return {currency: out[currency] for currency in sorted(out)}


def _normalize_candidate_index(
    value: Any,
    *,
    account: str,
    market: str,
    actionability: str,
    actions: list[dict[str, Any]],
    persisted: bool,
) -> list[dict[str, Any]]:
    if actionability != "live_actionable":
        if value not in (None, []):
            raise ValueError("candidate_index is only valid for live_actionable briefs")
        return []
    if value is None:
        derived = _derive_candidate_index_from_actions(
            actions,
            account=account,
            market=market,
        )
        if not persisted:
            for item in derived:
                if item["strategy_family"] != "combo_yield":
                    continue
                _validate_candidate_representative(
                    item["representative"],
                    family=str(item["strategy_family"]),
                    persisted=False,
                )
        return derived
    items = _mapping_list(value, field="candidate_index")
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        representative = _mapping(item.get("representative"), field="candidate_index.representative")
        symbol = canonical_symbol(item.get("symbol") or representative.get("symbol"))
        family = item.get("strategy_family") or representative.get("strategy_family")
        identity = build_daily_brief_candidate_identity(
            account=account,
            market=market,
            symbol=symbol,
            strategy_family=family,
            position_lot_id=representative.get("position_lot_id"),
            wheel_branch_id=representative.get("wheel_branch_id"),
        )
        supplied_identity = str(item.get("identity") or "").strip()
        if supplied_identity and supplied_identity != identity:
            raise ValueError(f"candidate identity mismatch: {supplied_identity!r} != {identity!r}")
        if identity in seen:
            raise ValueError(f"duplicate candidate identity: {identity}")
        seen.add(identity)
        contract_count = _nonnegative_int(item.get("contract_count"), field="contract_count")
        if contract_count < 1:
            raise ValueError("candidate_index contract_count must be positive")
        family_norm = canonical_strategy_id(str(family or ""))
        if family_norm == "sell_call":
            family_norm = "covered_call"
        representative_view = _candidate_representative_view(
            representative,
            symbol=symbol,
            strategy_family=family_norm,
        )
        _validate_candidate_representative(
            representative_view,
            family=family_norm,
            persisted=persisted,
        )
        out.append(
            {
                "identity": identity,
                "symbol": symbol,
                "strategy_family": family_norm,
                "representative": representative_view,
                "contract_count": contract_count,
            }
        )
    return sorted(out, key=lambda item: item["identity"])


def _validate_candidate_representative(
    representative: Mapping[str, Any],
    *,
    family: str,
    persisted: bool,
) -> None:
    capacity = representative.get("capacity")
    contracts = capacity.get("contracts_available") if isinstance(capacity, Mapping) else None
    if contracts is None or _nonnegative_int(contracts, field="contracts_available") < 1:
        raise ValueError("candidate representative capacity must be at least one contract")
    if family == "combo_yield":
        required = (
            "put_contract_symbol",
            "call_contract_symbol",
            "put_expiration",
            "call_expiration",
            "put_strike",
            "call_strike",
        )
    else:
        required = ("contract_symbol", "expiration", "strike")
    if any(representative.get(field) in (None, "") for field in required):
        raise ValueError("candidate representative contract fields are incomplete")
    if family == "combo_yield" and not str(
        representative.get("candidate_pair_id")
        or (representative.get("strategy_group_id") if persisted else "")
        or ""
    ).strip():
        raise ValueError("candidate representative pair identity is incomplete")


def _derive_candidate_index_from_actions(
    actions: list[dict[str, Any]],
    *,
    account: str,
    market: str,
) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for action in actions:
        if action.get("state") != "active" or action.get("action_type") not in {
            "open_candidate",
            "open_combo_yield",
        }:
            continue
        contracts = _candidate_capacity_contracts(action)
        if contracts is None or contracts < 1:
            continue
        try:
            identity = build_daily_brief_candidate_identity(
                account=account,
                market=market,
                symbol=action.get("symbol"),
                strategy_family=action.get("strategy_family"),
                position_lot_id=action.get("position_lot_id"),
                wheel_branch_id=action.get("wheel_branch_id"),
            )
        except ValueError:
            continue
        item = grouped.get(identity)
        if item is None:
            family = canonical_strategy_id(str(action.get("strategy_family") or ""))
            if family == "sell_call":
                family = "covered_call"
            symbol = canonical_symbol(action.get("symbol"))
            grouped[identity] = {
                "identity": identity,
                "symbol": symbol,
                "strategy_family": family,
                "representative": _candidate_representative_view(
                    action,
                    symbol=symbol,
                    strategy_family=family,
                ),
                "contract_count": 1,
            }
        else:
            item["contract_count"] += 1
    return [grouped[identity] for identity in sorted(grouped)]


def _candidate_representative_view(
    value: Mapping[str, Any],
    *,
    symbol: str | None,
    strategy_family: str,
) -> dict[str, Any]:
    out = {
        field: _json_safe(value.get(field))
        for field in _CANDIDATE_REPRESENTATIVE_FIELDS
        if value.get(field) is not None
        and (
            field not in {"position_lot_id", "wheel_branch_id"}
            or strategy_family == "wheel"
        )
    }
    if "capacity" not in out:
        metrics = out.get("metrics")
        if isinstance(metrics, Mapping) and isinstance(metrics.get("capacity"), Mapping):
            out["capacity"] = _json_safe(metrics["capacity"])
    out["symbol"] = symbol or ""
    out["strategy_family"] = strategy_family
    return out


def _normalize_candidate_groups(value: Any) -> dict[str, Any]:
    groups = _mapping(value, field="candidates")
    out: dict[str, Any] = {}
    for family, items in groups.items():
        if not isinstance(items, list):
            out[family] = items
            continue
        out[family] = [
            {**dict(item), "event_risk": normalize_candidate_event_risk(item.get("event_risk"))}
            if isinstance(item, Mapping)
            else item
            for item in items
        ]
    return out


def effective_daily_brief_actionability(
    brief: Mapping[str, Any],
    *,
    now_utc: datetime | None = None,
) -> str:
    actionability = str(brief.get("actionability") or "blocked").strip().lower()
    if actionability not in ACTIONABILITIES:
        return "blocked"
    if actionability == "blocked":
        return "blocked"
    if actionability == "planning_only":
        return "planning_only"

    valid_until = _parse_datetime(brief.get("valid_until_utc"))
    if valid_until is None:
        return "planning_only"
    effective_now = now_utc or datetime.now(timezone.utc)
    if effective_now.tzinfo is None:
        effective_now = effective_now.replace(tzinfo=timezone.utc)
    if effective_now.astimezone(timezone.utc) >= valid_until.astimezone(timezone.utc):
        return "planning_only"
    return "live_actionable"


def diff_daily_decision_briefs(
    previous: Mapping[str, Any],
    current: Mapping[str, Any],
) -> dict[str, Any]:
    prev = normalize_persisted_daily_decision_brief(previous)
    cur = normalize_persisted_daily_decision_brief(current)
    _ensure_same_brief_identity(prev, cur)

    changes: list[dict[str, Any]] = []
    prev_actionability = str(prev["actionability"])
    cur_actionability = str(cur["actionability"])
    if prev_actionability != cur_actionability:
        if cur_actionability == "blocked":
            changes.append(_change("blocked", priority="P0", material=True, before=prev_actionability, after=cur_actionability))
        elif prev_actionability == "blocked":
            changes.append(_change("recovered", priority="P0", material=True, before=prev_actionability, after=cur_actionability))
        elif _has_active_high_priority_actions(prev) or _has_active_high_priority_actions(cur):
            changes.append(
                _change(
                    "actionability_changed",
                    priority="P1",
                    material=True,
                    before=prev_actionability,
                    after=cur_actionability,
                )
            )

    prev_actions = {str(item["action_id"]): item for item in prev["actions"]}
    cur_actions = {str(item["action_id"]): item for item in cur["actions"]}
    aligned_current_to_previous = _align_combo_action_ids(prev, cur)
    aligned_previous_ids = set(aligned_current_to_previous.values())

    for action_id, action in sorted(cur_actions.items()):
        prior = prev_actions.get(action_id)
        if prior is None:
            prior = prev_actions.get(aligned_current_to_previous.get(action_id, ""))
        action_id_transition = _action_id_transition(prior, action)
        opening_candidate = _is_opening_candidate_action(action)
        if prior is None:
            if action["priority"] in {"P0", "P1"} and action["state"] == "active":
                changes.append(
                    _change(
                        "candidate_added"
                        if opening_candidate
                        else ("p0_added" if action["priority"] == "P0" else "action_added"),
                        priority=action["priority"],
                        material=True,
                        action=_action_change_view(action),
                        **action_id_transition,
                    )
                )
            continue

        prior_was_active_high_priority = (
            prior["priority"] in {"P0", "P1"} and prior["state"] == "active"
        )
        current_is_active_high_priority = (
            action["priority"] in {"P0", "P1"} and action["state"] == "active"
        )
        priority_rank = {"P0": 0, "P1": 1, "P2": 2}

        if opening_candidate:
            prior_evidence_unavailable = (
                prior["state"] == "observe"
                and _lower(prior.get("evidence_state")) == "unavailable"
            )
            current_evidence_unavailable = (
                action["state"] == "observe"
                and _lower(action.get("evidence_state")) == "unavailable"
            )
            if prior_evidence_unavailable and current_is_active_high_priority:
                changes.append(
                    _change(
                        "candidate_evidence_recovered",
                        priority=action["priority"],
                        material=True,
                        action=_action_change_view(action),
                        **action_id_transition,
                    )
                )
            elif prior_was_active_high_priority and current_evidence_unavailable:
                changes.append(
                    _change(
                        "candidate_evidence_unavailable",
                        priority=prior["priority"],
                        material=True,
                        action=_action_change_view(action),
                        **action_id_transition,
                    )
                )
            elif prior_evidence_unavailable and current_evidence_unavailable:
                continue
            elif current_is_active_high_priority and not prior_was_active_high_priority:
                changes.append(
                    _change(
                        "candidate_added",
                        priority=action["priority"],
                        material=True,
                        action=_action_change_view(action),
                        **action_id_transition,
                    )
                )
            elif prior_was_active_high_priority and not current_is_active_high_priority:
                if action["state"] == "active":
                    changes.append(
                        _change(
                            "candidate_priority_downgraded",
                            priority=prior["priority"],
                            material=True,
                            before=prior["priority"],
                            after=action["priority"],
                            action=_action_change_view(action),
                            **action_id_transition,
                        )
                    )
                else:
                    changes.append(
                        _change(
                            "candidate_invalidated",
                            priority=prior["priority"],
                            material=True,
                            before=prior["state"],
                            after=action["state"],
                            action=_action_change_view(action),
                            **action_id_transition,
                        )
                    )
            elif prior_was_active_high_priority and current_is_active_high_priority:
                if prior["priority"] != "P0" and action["priority"] == "P0":
                    changes.append(
                        _change(
                            "candidate_priority_upgraded_to_p0",
                            priority="P0",
                            material=True,
                            before=prior["priority"],
                            after=action["priority"],
                            action=_action_change_view(action),
                            **action_id_transition,
                        )
                    )
                elif priority_rank[action["priority"]] > priority_rank[prior["priority"]]:
                    changes.append(
                        _change(
                            "candidate_priority_downgraded",
                            priority=prior["priority"],
                            material=True,
                            before=prior["priority"],
                            after=action["priority"],
                            action=_action_change_view(action),
                            **action_id_transition,
                        )
                    )
                else:
                    before_capacity = _candidate_capacity_contracts(prior)
                    after_capacity = _candidate_capacity_contracts(action)
                    if (
                        before_capacity is not None
                        and after_capacity is not None
                        and before_capacity != after_capacity
                    ):
                        changes.append(
                            _change(
                                "candidate_capacity_changed",
                                priority=action["priority"],
                                material=True,
                                before=before_capacity,
                                after=after_capacity,
                                action=_action_change_view(action),
                                **action_id_transition,
                            )
                        )
            if prior_was_active_high_priority or current_is_active_high_priority:
                for transition in candidate_event_risk_transitions(
                    prior.get("event_risk"),
                    action.get("event_risk"),
                    market_trading_date=cur["market_trading_date"],
                ):
                    changes.append(
                        _change(
                            str(transition["change_type"]),
                            priority=action["priority"] if current_is_active_high_priority else prior["priority"],
                            material=True,
                            action=_action_change_view(action),
                            before_event_risk=transition["before_event_risk"],
                            after_event_risk=transition["after_event_risk"],
                            **action_id_transition,
                        )
                    )
            continue

        upgraded_to_p0 = prior["priority"] != "P0" and action["priority"] == "P0"
        if upgraded_to_p0:
            changes.append(
                _change(
                    "priority_upgraded_to_p0",
                    priority="P0",
                    material=True,
                    before=prior["priority"],
                    after=action["priority"],
                    action=_action_change_view(action),
                )
            )
        if (
            current_is_active_high_priority
            and not prior_was_active_high_priority
            and not upgraded_to_p0
        ):
            changes.append(
                _change(
                    "action_added",
                    priority=action["priority"],
                    material=True,
                    action=_action_change_view(action),
                )
            )
        if (
            prior["priority"] in {"P0", "P1"}
            and priority_rank[action["priority"]] > priority_rank[prior["priority"]]
        ):
            changes.append(
                _change(
                    "priority_downgraded",
                    priority=prior["priority"],
                    material=True,
                    before=prior["priority"],
                    after=action["priority"],
                    action=_action_change_view(action),
                )
            )
        if (
            prior["state"] == "active"
            and action["state"] != "active"
            and prior["priority"] in {"P0", "P1"}
        ):
            changes.append(
                _change(
                    "action_invalidated",
                    priority=prior["priority"],
                    material=True,
                    before=prior["state"],
                    after=action["state"],
                    action=_action_change_view(action),
                )
            )

    for action_id, action in sorted(prev_actions.items()):
        if action_id in cur_actions or action_id in aligned_previous_ids:
            continue
        evidence_hold = (
            action["priority"] in {"P0", "P1"}
            and action["state"] == "observe"
            and _lower(action.get("evidence_state")) == "unavailable"
        )
        if (
            action["priority"] in {"P0", "P1"}
            and action["state"] == "active"
        ) or evidence_hold:
            changes.append(
                _change(
                    "candidate_invalidated"
                    if _is_opening_candidate_action(action)
                    else "action_invalidated",
                    priority=action["priority"],
                    material=True,
                    before=("evidence_unavailable" if evidence_hold else "active"),
                    after="missing",
                    action=_action_change_view(action),
                )
            )

    changes.sort(key=_change_sort_key)
    material = any(bool(item.get("material")) for item in changes)
    canonical_changes = [_canonical_change(item) for item in changes]
    return {
        "schema_version": DAILY_DECISION_BRIEF_DIFF_SCHEMA_VERSION,
        "brief_id": cur["brief_id"],
        "market": cur["market"],
        "market_trading_date": cur["market_trading_date"],
        "account": cur["account"],
        "from_revision": prev["revision"],
        "to_revision": cur["revision"],
        "material": material,
        "changes": changes,
        "material_diff_digest": _digest(canonical_changes),
    }


def daily_brief_digest(brief: Mapping[str, Any]) -> str:
    return daily_brief_compatible_digests(brief)[0]


def daily_brief_compatible_digests(brief: Mapping[str, Any]) -> tuple[str, ...]:
    """Return the current digest plus an exact overlay-era digest candidate.

    New writes always use the stripped current contract.  When immutable raw
    input contains retired fields, their exact values are reattached only for
    historical integrity verification; they are never normalized or exposed.
    """

    source = dict(brief or {})
    normalized = normalize_persisted_daily_decision_brief(brief)
    payload = {
        key: value
        for key, value in normalized.items()
        if key not in {"generated_at_utc", "data_as_of_utc", "run_id"}
    }
    current = _digest(payload)
    legacy_payload = dict(payload)
    for field in RETIRED_DAILY_BRIEF_FIELDS:
        if field in source:
            legacy_payload[field] = source[field]
    legacy = _digest(legacy_payload)
    return (current,) if legacy == current else (current, legacy)


def _ensure_same_brief_identity(previous: Mapping[str, Any], current: Mapping[str, Any]) -> None:
    prev_identity = (previous.get("market"), previous.get("market_trading_date"), previous.get("account"))
    cur_identity = (current.get("market"), current.get("market_trading_date"), current.get("account"))
    if prev_identity != cur_identity:
        raise ValueError(f"daily brief identity mismatch: {prev_identity!r} != {cur_identity!r}")


def _has_active_high_priority_actions(brief: Mapping[str, Any]) -> bool:
    return any(
        item.get("priority") in {"P0", "P1"} and item.get("state") == "active"
        for item in brief.get("actions") or []
        if isinstance(item, Mapping)
    )


def _is_opening_candidate_action(action: Mapping[str, Any]) -> bool:
    return str(action.get("action_type") or "").strip().lower() in {
        "open_candidate",
        "open_combo_yield",
    }


def _candidate_capacity_contracts(action: Mapping[str, Any]) -> int | None:
    metrics = action.get("metrics")
    if not isinstance(metrics, Mapping):
        return None
    capacity = metrics.get("capacity")
    if not isinstance(capacity, Mapping):
        return None
    raw = capacity.get("contracts_available")
    if raw is None:
        return None
    try:
        return max(0, int(float(raw)))
    except (TypeError, ValueError, OverflowError):
        return None


def _action_change_view(action: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: action.get(key)
        for key in (
            "action_id",
            "priority",
            "state",
            "action_type",
            "strategy_family",
            "symbol",
            "option_type",
            "expiration",
            "strike",
            "contract_symbol",
            "position_lot_id",
            "wheel_branch_id",
            "strategy_group_id",
            "candidate_pair_id",
            "leg_role",
            "title",
            "reason",
        )
        if action.get(key) not in (None, "")
    }


def _action_id_transition(
    previous: Mapping[str, Any] | None,
    current: Mapping[str, Any],
) -> dict[str, str]:
    if previous is None:
        return {}
    before_id = str(previous.get("action_id") or "").strip()
    after_id = str(current.get("action_id") or "").strip()
    if not before_id or not after_id or before_id == after_id:
        return {}
    return {"before_action_id": before_id, "after_action_id": after_id}


def _change(change_type: str, *, priority: str, material: bool, **fields: Any) -> dict[str, Any]:
    return {"change_type": change_type, "priority": priority, "material": bool(material), **fields}


def _canonical_change(change: Mapping[str, Any]) -> dict[str, Any]:
    out = {
        key: value
        for key, value in change.items()
        if key not in {"action", "to_revision", "generated_at_utc", "data_as_of_utc"}
    }
    action = change.get("action")
    if isinstance(action, Mapping):
        out["action"] = {
            key: action.get(key)
            for key in (
                "action_id",
                "priority",
                "state",
                "action_type",
                "strategy_family",
                "symbol",
                "option_type",
                "expiration",
                "strike",
                "contract_symbol",
                "position_lot_id",
                "wheel_branch_id",
                "strategy_group_id",
                "candidate_pair_id",
                "leg_role",
            )
            if action.get(key) not in (None, "")
        }
    return out


def _change_sort_key(change: Mapping[str, Any]) -> tuple[int, str, str]:
    priority = str(change.get("priority") or "P2")
    priority_rank = {"P0": 0, "P1": 1, "P2": 2}.get(priority, 3)
    action = change.get("action") if isinstance(change.get("action"), Mapping) else {}
    return priority_rank, str(change.get("change_type") or ""), str(action.get("action_id") or "")


def _normalize_action_identity_value(field: str, value: Any) -> str:
    if field in {"account", "action_type", "strategy_family", "option_type", "side", "leg_role"}:
        return _lower(value)
    if field in {"symbol", "contract_symbol"}:
        return _upper(value)
    if field == "strike":
        return _canonical_number(value)
    return str(value or "").strip()


def _canonical_number(value: Any) -> str:
    if value is None or isinstance(value, bool):
        return ""
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return str(value or "").strip()
    if not number.is_finite():
        return ""
    normalized = number.normalize()
    if normalized == normalized.to_integral():
        return format(normalized, "f").split(".", 1)[0]
    return format(normalized, "f")


def _nonnegative_int(value: Any, *, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a non-negative integer")
    try:
        out = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{field} must be a non-negative integer") from exc
    if out < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    return out


def _mapping(value: Any, *, field: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be an object")
    return dict(value)


def _mapping_list(value: Any, *, field: str) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list")
    out: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise ValueError(f"{field} items must be objects")
        out.append(dict(item))
    return out


def _iso_or_empty(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    parsed = _parse_datetime(text)
    if parsed is None:
        raise ValueError(f"invalid ISO datetime: {text}")
    return parsed.astimezone(timezone.utc).isoformat()


def _parse_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _digest(value: Any) -> str:
    raw = json.dumps(
        _json_safe(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _json_safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, Decimal):
        return _canonical_number(value)
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _lower(value: Any) -> str:
    return str(value or "").strip().lower()


def _upper(value: Any) -> str:
    return str(value or "").strip().upper()


__all__ = [
    "ACTIONABILITIES",
    "ACTION_PRIORITIES",
    "ACTION_STATES",
    "DAILY_DECISION_BRIEF_DIFF_SCHEMA_VERSION",
    "DAILY_DECISION_BRIEF_SCHEMA_VERSION",
    "RETIRED_DAILY_BRIEF_FIELDS",
    "build_daily_brief_action_id",
    "build_daily_brief_candidate_identity",
    "decide_daily_brief_notification",
    "build_daily_brief_id",
    "daily_brief_compatible_digests",
    "daily_brief_digest",
    "diff_daily_decision_briefs",
    "effective_daily_brief_actionability",
    "normalize_daily_brief_action",
    "normalize_daily_decision_brief",
    "normalize_persisted_daily_decision_brief",
    "reconcile_daily_decision_brief_evidence",
]
