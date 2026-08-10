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
_CANDIDATE_STRATEGY_FAMILIES = frozenset({"sell_put", "covered_call", "combo_yield"})
_CANDIDATE_REPRESENTATIVE_FIELDS = (
    "rank",
    "symbol",
    "strategy_family",
    "option_type",
    "contract_symbol",
    "expiration",
    "strike",
    "strategy_group_id",
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
    return f"candidate:v1:{account_norm}:{market_norm}:{symbol_norm}:{family_norm}"


def decide_daily_brief_notification(
    *,
    ran_scan: bool,
    pipeline_reliable: bool,
    normal_delivery_allowed: bool,
    fixed_failure_delivery_allowed: bool,
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
        if pipeline_reliable and normal_delivery_allowed:
            return {
                "action": "fixed_report",
                "reason": "fixed_report_due",
            }
        if fixed_failure_delivery_allowed:
            return {
                "action": "fixed_failure",
                "reason": (
                    "fixed_scan_failed"
                    if not pipeline_reliable
                    else "fixed_normal_authority_unavailable"
                ),
            }
        return {
            "action": "none",
            "reason": "fixed_failure_authority_unavailable",
        }
    if not pipeline_reliable:
        return {"action": "none", "reason": "nonfixed_scan_failed"}
    if not normal_delivery_allowed:
        return {"action": "none", "reason": "normal_authority_unavailable"}
    if pending_candidate_identities:
        return {"action": "candidate_alert", "reason": "pending_candidates"}
    return {"action": "none", "reason": "no_pending_candidates"}


def build_daily_brief_action_id(action: Mapping[str, Any]) -> str:
    identity = {
        field: _normalize_action_identity_value(field, action.get(field))
        for field in _STABLE_ACTION_ID_FIELDS
    }
    if not identity["action_type"]:
        raise ValueError("action_type is required for daily brief action identity")
    if not identity["account"]:
        raise ValueError("account is required for daily brief action identity")
    return "action-" + _digest(identity)[:24]


def normalize_daily_brief_action(action: Mapping[str, Any]) -> dict[str, Any]:
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
    out["strategy_group_id"] = str(src.get("strategy_group_id") or "").strip()
    out["leg_role"] = _lower(src.get("leg_role"))
    if out["action_type"] in {"open_candidate", "open_combo_yield"}:
        out["event_risk"] = normalize_candidate_event_risk(src.get("event_risk"))
    out["action_id"] = build_daily_brief_action_id(out)
    return out


def normalize_daily_decision_brief(payload: Mapping[str, Any]) -> dict[str, Any]:
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

    normalized_actions = [
        normalize_daily_brief_action(item)
        for item in _mapping_list(src.get("actions"), field="actions")
    ]
    action_ids: set[str] = set()
    for action in normalized_actions:
        action_id = str(action["action_id"])
        if action_id in action_ids:
            raise ValueError(f"duplicate daily brief action_id: {action_id}")
        action_ids.add(action_id)

    out = dict(src)
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
            ),
            "rejections": _mapping(src.get("rejections"), field="rejections"),
            "events": _mapping_list(src.get("events"), field="events"),
            "data_gaps": _mapping_list(src.get("data_gaps"), field="data_gaps"),
            "source_artifacts": _mapping_list(src.get("source_artifacts"), field="source_artifacts"),
            "ai_decision_advice": _normalize_ai_decision_advice(
                src.get("ai_decision_advice")
            ),
            "ai_decision_advice_evidence_index": _mapping(
                src.get("ai_decision_advice_evidence_index"),
                field="ai_decision_advice_evidence_index",
            ),
        }
    )
    return out


_AI_DECISION_ADVICE_STATUSES = frozenset({"completed", "unavailable", "not_applicable"})
_AI_DECISION_ADVICE_ACTIONS = frozenset({"keep", "switch", "defer", "needs_review"})


def _normalize_ai_decision_advice(value: Any) -> dict[str, Any] | None:
    """Normalize the optional AI Decision Advice section (design 14.1).

    The section is absent (``None``) for briefs assembled before this feature
    or when the module is disabled; when present, only the envelope status,
    per-scope action state, and zero-candidate flags are normalized. Rationale
    and source refs are rendering concerns and do not participate in diffs.
    """

    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("ai_decision_advice must be an object")
    status = str(value.get("status") or "").strip().lower()
    if status not in _AI_DECISION_ADVICE_STATUSES:
        raise ValueError(f"unsupported ai_decision_advice status: {status}")

    def _decision(row: Any, *, family: str) -> dict[str, Any] | None:
        if not isinstance(row, Mapping):
            return None
        action = row.get("action")
        action = str(action).strip().lower() if action is not None else None
        if action is not None and action not in _AI_DECISION_ADVICE_ACTIONS:
            raise ValueError(f"unsupported ai_decision_advice action: {action}")
        out = {
            "action": action,
            "baseline_candidate_id": row.get("baseline_candidate_id"),
            "selected_candidate_id": row.get("selected_candidate_id"),
        }
        if family == "covered_call":
            out["symbol"] = _upper(row.get("symbol"))
        rationale = row.get("rationale")
        out["rationale"] = dict(rationale) if isinstance(rationale, Mapping) else None
        source_refs = row.get("source_refs")
        out["source_refs"] = dict(source_refs) if isinstance(source_refs, Mapping) else None
        return out

    zero_candidate_raw = value.get("zero_candidate")
    zero_candidate = (
        {
            "sell_put": bool(zero_candidate_raw.get("sell_put")),
            "covered_call": bool(zero_candidate_raw.get("covered_call")),
        }
        if isinstance(zero_candidate_raw, Mapping)
        else {"sell_put": False, "covered_call": False}
    )
    covered_call_rows = value.get("covered_call")
    covered_call = (
        [
            row
            for row in (_decision(item, family="covered_call") for item in covered_call_rows)
            if row is not None
        ]
        if isinstance(covered_call_rows, list)
        else None
    )
    return {
        "status": status,
        "unavailable_reason": (
            str(value.get("unavailable_reason")).strip() or None
            if value.get("unavailable_reason") is not None
            else None
        ),
        "evidence_as_of": _iso_or_empty(value.get("evidence_as_of")) or None,
        "sell_put": _decision(value.get("sell_put"), family="sell_put"),
        "covered_call": covered_call,
        "zero_candidate": zero_candidate,
        "reused": bool(value.get("reused")),
        "advice_record_id": (
            str(value.get("advice_record_id")).strip() or None
            if value.get("advice_record_id") is not None
            else None
        ),
    }


def _ai_decision_advice_state_map(
    section: Mapping[str, Any] | None,
) -> dict[str, tuple[str, str | None]]:
    """Scope -> (action, selected candidate) map for diffing (design 14.1).

    Only ``completed`` sections contribute action state; ``unavailable`` and
    ``not_applicable`` never generate material changes.
    """

    if not isinstance(section, Mapping) or section.get("status") != "completed":
        return {}
    out: dict[str, tuple[str, str | None]] = {}
    sell_put = section.get("sell_put")
    if isinstance(sell_put, Mapping) and sell_put.get("action"):
        out["sell_put"] = (
            str(sell_put["action"]),
            str(sell_put.get("selected_candidate_id") or "").strip() or None,
        )
    for row in section.get("covered_call") or []:
        if isinstance(row, Mapping) and row.get("action"):
            symbol = _upper(row.get("symbol"))
            out[f"covered_call:{symbol}"] = (
                str(row["action"]),
                str(row.get("selected_candidate_id") or "").strip() or None,
            )
    return out


def _diff_ai_decision_advice(
    prev: Mapping[str, Any],
    cur: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Material action migrations between keep/switch/defer/needs_review.

    ``unavailable`` appearance, disappearance, or reason changes are rendered
    in receipts but never material (design 14.1).
    """

    prev_states = _ai_decision_advice_state_map(prev.get("ai_decision_advice"))
    cur_states = _ai_decision_advice_state_map(cur.get("ai_decision_advice"))
    changes: list[dict[str, Any]] = []
    for scope in sorted(set(prev_states) | set(cur_states)):
        before = prev_states.get(scope)
        after = cur_states.get(scope)
        if before is None or after is None:
            continue
        before_action, before_selected = before
        after_action, after_selected = after
        if before_action != after_action:
            changes.append(
                _change(
                    "ai_decision_advice_action_changed",
                    priority="P1",
                    material=True,
                    ai_advice_scope=scope,
                    before=before_action,
                    after=after_action,
                )
            )
            continue
        if (
            before_action == "switch"
            and before_selected != after_selected
        ):
            changes.append(
                _change(
                    "ai_decision_advice_selected_candidate_changed",
                    priority="P1",
                    material=True,
                    ai_advice_scope=scope,
                    before=before_selected,
                    after=after_selected,
                )
            )
    return changes


def reconcile_daily_decision_brief_evidence(
    previous: Mapping[str, Any],
    current: Mapping[str, Any],
) -> dict[str, Any]:
    """Carry candidate identity across a run with typed family-level data gaps."""

    prev = normalize_daily_decision_brief(previous)
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
    additions: list[dict[str, Any]] = []
    for prior in prev.get("actions") or []:
        if not isinstance(prior, Mapping) or not _is_opening_candidate_action(prior):
            continue
        action_id = str(prior.get("action_id") or "")
        if not action_id or action_id in current_actions:
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
    if not additions:
        return cur
    candidate = dict(cur)
    candidate["actions"] = [*cur["actions"], *additions]
    return normalize_daily_decision_brief(candidate)


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
) -> list[dict[str, Any]]:
    if actionability != "live_actionable":
        if value not in (None, []):
            raise ValueError("candidate_index is only valid for live_actionable briefs")
        return []
    if value is None:
        return _derive_candidate_index_from_actions(actions, account=account, market=market)
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
        family_norm = identity.rsplit(":", 1)[-1]
        representative_view = _candidate_representative_view(
            representative,
            symbol=symbol,
            strategy_family=family_norm,
        )
        _validate_candidate_representative(representative_view, family=family_norm)
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
) -> None:
    capacity = representative.get("capacity")
    contracts = capacity.get("contracts_available") if isinstance(capacity, Mapping) else None
    if contracts is None or _nonnegative_int(contracts, field="contracts_available") < 1:
        raise ValueError("candidate representative capacity must be at least one contract")
    if family == "combo_yield":
        required = (
            "strategy_group_id",
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
            )
        except ValueError:
            continue
        item = grouped.get(identity)
        if item is None:
            family = identity.rsplit(":", 1)[-1]
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
    prev = normalize_daily_decision_brief(previous)
    cur = normalize_daily_decision_brief(current)
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

    for action_id, action in sorted(cur_actions.items()):
        prior = prev_actions.get(action_id)
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
                    )
                )
            elif prior_was_active_high_priority and current_evidence_unavailable:
                changes.append(
                    _change(
                        "candidate_evidence_unavailable",
                        priority=prior["priority"],
                        material=True,
                        action=_action_change_view(action),
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
        if action_id in cur_actions:
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

    changes.extend(_diff_ai_decision_advice(prev, cur))
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
    normalized = normalize_daily_decision_brief(brief)
    payload = {
        key: value
        for key, value in normalized.items()
        if key not in {"generated_at_utc", "data_as_of_utc", "run_id"}
    }
    return _digest(payload)


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
            "strategy_group_id",
            "leg_role",
            "title",
            "reason",
        )
        if action.get(key) not in (None, "")
    }


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
                "strategy_group_id",
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
    "build_daily_brief_action_id",
    "build_daily_brief_candidate_identity",
    "decide_daily_brief_notification",
    "build_daily_brief_id",
    "daily_brief_digest",
    "diff_daily_decision_briefs",
    "effective_daily_brief_actionability",
    "normalize_daily_brief_action",
    "normalize_daily_decision_brief",
    "reconcile_daily_decision_brief_evidence",
]
