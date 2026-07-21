from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from typing import Any, Mapping

from domain.domain.symbol_identity import canonical_symbol


IMPORTANT_EVENT_TYPES = ("earnings", "ex_dividend", "split")
EVENT_USER_STATES = frozenset({"confirmed_event", "confirmed_none", "unknown"})


def build_candidate_event_risk(
    *,
    symbol: Any,
    market_trading_date: Any,
    expirations: Mapping[str, Any],
    snapshot_item: Mapping[str, Any] | None,
    snapshot_reason: str = "",
) -> dict[str, Any]:
    """Project one run-snapshot symbol item into candidate-scoped event semantics."""

    candidate_symbol = canonical_symbol(symbol) or str(symbol or "").strip().upper()
    as_of = _date(market_trading_date)
    normalized_expirations = {
        str(label): parsed
        for label, raw in expirations.items()
        if str(label).strip() and (parsed := _date(raw)) is not None
    }
    if not candidate_symbol or as_of is None:
        return _unknown("candidate_context_invalid", candidate_symbol, normalized_expirations)
    if snapshot_reason:
        return _unknown(snapshot_reason, candidate_symbol, normalized_expirations)
    if not isinstance(snapshot_item, Mapping):
        return _unknown("snapshot_symbol_missing", candidate_symbol, normalized_expirations)

    item_symbol = canonical_symbol(snapshot_item.get("symbol")) or str(snapshot_item.get("symbol") or "").strip().upper()
    if item_symbol and item_symbol != candidate_symbol:
        return _unknown("snapshot_symbol_mismatch", candidate_symbol, normalized_expirations)

    source_status = str(snapshot_item.get("source_status") or "").strip().lower()
    selected_provider = str(snapshot_item.get("selected_provider") or snapshot_item.get("provider") or "").strip().lower()
    coverage, coverage_malformed = _coverage(snapshot_item.get("coverage"))
    events, events_malformed, events_conflict = _events(
        snapshot_item.get("events"),
        symbol=candidate_symbol,
        as_of=as_of,
    )
    provider_conflict = _provider_conflict(snapshot_item, as_of=as_of)
    evidence_chain_id = _evidence_chain_id(selected_provider, coverage)

    if source_status not in {"ok", "ok_with_fallback"}:
        return _unknown(
            "event_source_" + (source_status or "unavailable"),
            candidate_symbol,
            normalized_expirations,
            selected_provider=selected_provider,
            evidence_chain_id=evidence_chain_id,
            coverage=coverage,
        )
    if coverage_malformed or events_malformed:
        return _unknown(
            "event_evidence_malformed",
            candidate_symbol,
            normalized_expirations,
            selected_provider=selected_provider,
            evidence_chain_id=evidence_chain_id,
            coverage=coverage,
        )
    if events_conflict or provider_conflict:
        return _unknown(
            "event_evidence_conflict",
            candidate_symbol,
            normalized_expirations,
            selected_provider=selected_provider,
            evidence_chain_id=evidence_chain_id,
            coverage=coverage,
        )
    if not all(coverage.get(event_type) == "complete" for event_type in IMPORTANT_EVENT_TYPES):
        return _unknown(
            "event_evidence_incomplete",
            candidate_symbol,
            normalized_expirations,
            selected_provider=selected_provider,
            evidence_chain_id=evidence_chain_id,
            coverage=coverage,
        )

    if events:
        nearest = dict(events[0])
        event_date = _date(nearest.get("event_date"))
        assert event_date is not None
        relations = _expiration_relations(event_date, normalized_expirations)
        return {
            "user_state": "confirmed_event",
            "reason_code": "confirmed_event",
            "reliable": True,
            "symbol": candidate_symbol,
            "selected_provider": selected_provider,
            "evidence_chain_id": evidence_chain_id,
            "coverage": coverage,
            "nearest_event": nearest,
            "events": events,
            "days_to_event": (event_date - as_of).days,
            "expiration_relations": relations,
            "in_attention_window": any(
                item.get("relation") in {"before_expiration", "on_expiration"}
                for item in relations.values()
            ),
        }

    if source_status == "ok":
        return {
            "user_state": "confirmed_none",
            "reason_code": "confirmed_no_upcoming_event",
            "reliable": True,
            "symbol": candidate_symbol,
            "selected_provider": selected_provider,
            "evidence_chain_id": evidence_chain_id,
            "coverage": coverage,
            "nearest_event": None,
            "events": [],
            "days_to_event": None,
            "expiration_relations": {},
            "in_attention_window": False,
        }

    return _unknown(
        "fallback_absence_unconfirmed",
        candidate_symbol,
        normalized_expirations,
        selected_provider=selected_provider,
        evidence_chain_id=evidence_chain_id,
        coverage=coverage,
    )


def normalize_candidate_event_risk(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return _unknown("event_risk_not_observed", "", {})
    state = str(value.get("user_state") or "unknown").strip().lower()
    if state not in EVENT_USER_STATES:
        state = "unknown"
    out = dict(value)
    out["user_state"] = state
    out["reason_code"] = str(value.get("reason_code") or "event_risk_not_observed").strip()
    out["reliable"] = bool(value.get("reliable")) and state != "unknown"
    out["selected_provider"] = str(value.get("selected_provider") or "").strip().lower()
    out["evidence_chain_id"] = str(value.get("evidence_chain_id") or "").strip()
    out["coverage"] = {
        event_type: str(status or "unknown").strip().lower()
        for event_type, status in (value.get("coverage") or {}).items()
        if str(event_type).strip()
    } if isinstance(value.get("coverage"), Mapping) else {}
    out["nearest_event"] = dict(value["nearest_event"]) if isinstance(value.get("nearest_event"), Mapping) else None
    out["events"] = [dict(item) for item in value.get("events") or [] if isinstance(item, Mapping)]
    out["expiration_relations"] = {
        str(key): dict(item)
        for key, item in (value.get("expiration_relations") or {}).items()
        if isinstance(item, Mapping)
    } if isinstance(value.get("expiration_relations"), Mapping) else {}
    out["in_attention_window"] = bool(value.get("in_attention_window"))
    return out


def _unknown(
    reason_code: str,
    symbol: str,
    expirations: Mapping[str, date],
    *,
    selected_provider: str = "",
    evidence_chain_id: str = "",
    coverage: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    return {
        "user_state": "unknown",
        "reason_code": str(reason_code or "event_evidence_unavailable"),
        "reliable": False,
        "symbol": symbol,
        "selected_provider": selected_provider,
        "evidence_chain_id": evidence_chain_id,
        "coverage": dict(coverage or {}),
        "nearest_event": None,
        "events": [],
        "days_to_event": None,
        "expiration_relations": {
            label: {"expiration": value.isoformat(), "relation": "unknown", "days_before_expiration": None}
            for label, value in expirations.items()
        },
        "in_attention_window": False,
    }


def _coverage(value: Any) -> tuple[dict[str, str], bool]:
    if not isinstance(value, Mapping):
        return {}, True
    out: dict[str, str] = {}
    malformed = False
    for event_type in IMPORTANT_EVENT_TYPES:
        item = value.get(event_type)
        if not isinstance(item, Mapping):
            out[event_type] = "unknown"
            malformed = True
            continue
        status = str(item.get("status") or "unknown").strip().lower()
        if status not in {"complete", "partial", "unsupported", "unknown"}:
            status = "unknown"
            malformed = True
        out[event_type] = status
    return out, malformed


def _events(value: Any, *, symbol: str, as_of: date) -> tuple[list[dict[str, Any]], bool, bool]:
    if not isinstance(value, list):
        return [], True, False
    out: list[dict[str, Any]] = []
    malformed = False
    dates_by_id: dict[str, set[str]] = {}
    seen: set[tuple[str, str]] = set()
    for raw in value:
        if not isinstance(raw, Mapping):
            malformed = True
            continue
        event_type = str(raw.get("type") or raw.get("event_type") or "").strip().lower()
        if event_type not in IMPORTANT_EVENT_TYPES:
            continue
        event_date = _date(raw.get("date") or raw.get("event_date"))
        if event_date is None:
            malformed = True
            continue
        normalized = _normalized_event(symbol=symbol, event_type=event_type, event_date=event_date, raw=raw)
        dates_by_id.setdefault(normalized["event_id"], set()).add(normalized["event_date"])
        dedupe_key = (normalized["event_id"], normalized["event_date"])
        if dedupe_key in seen or event_date < as_of:
            continue
        seen.add(dedupe_key)
        out.append(normalized)
    out.sort(key=lambda item: (item["event_date"], item["event_type"], item["event_id"]))
    conflict = any(len(dates) > 1 for dates in dates_by_id.values())
    return out, malformed, conflict


def _normalized_event(*, symbol: str, event_type: str, event_date: date, raw: Mapping[str, Any]) -> dict[str, Any]:
    series_id = "event-series-" + _digest({"symbol": symbol, "event_type": event_type})[:24]
    anchor = _occurrence_anchor(event_type, raw)
    event_id = "event-" + _digest(
        {
            "series_id": series_id,
            "occurrence": anchor or event_date.isoformat(),
        }
    )[:24]
    return {
        "event_id": event_id,
        "event_series_id": series_id,
        "event_type": event_type,
        "event_date": event_date.isoformat(),
        "occurrence_anchor": anchor,
        "anchored": bool(anchor),
    }


def _occurrence_anchor(event_type: str, event: Mapping[str, Any]) -> str:
    raw = event.get("raw") if isinstance(event.get("raw"), Mapping) else {}
    fields_by_type = {
        "earnings": ("fiscal_year", "financial_type", "period_text"),
        "ex_dividend": ("record_date", "dividend_payable_date"),
        "split": ("rate", "reform_type"),
    }
    parts = [str(raw.get(field) or "").strip() for field in fields_by_type.get(event_type, ())]
    values = [value for value in parts if value]
    return "|".join(values)


def _expiration_relations(event_date: date, expirations: Mapping[str, date]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for label, expiration in expirations.items():
        delta = (expiration - event_date).days
        relation = "before_expiration" if delta > 0 else ("on_expiration" if delta == 0 else "after_expiration")
        out[label] = {
            "expiration": expiration.isoformat(),
            "relation": relation,
            "days_before_expiration": delta,
        }
    return out


def _provider_conflict(item: Mapping[str, Any], *, as_of: date) -> bool:
    raw_results = item.get("source_results")
    if not isinstance(raw_results, Mapping):
        return False
    comparable: list[tuple[dict[str, str], dict[str, str]]] = []
    for raw in raw_results.values():
        if not isinstance(raw, Mapping) or str(raw.get("source_status") or "").strip().lower() != "ok":
            continue
        coverage, malformed = _coverage(raw.get("coverage"))
        events, events_malformed, events_conflict = _events(raw.get("events"), symbol=str(item.get("symbol") or ""), as_of=as_of)
        if malformed or events_malformed or events_conflict:
            continue
        nearest_by_type: dict[str, str] = {}
        for event in events:
            nearest_by_type.setdefault(event["event_type"], event["event_date"])
        comparable.append((coverage, nearest_by_type))
    for index, (left_coverage, left_events) in enumerate(comparable):
        for right_coverage, right_events in comparable[index + 1 :]:
            for event_type in IMPORTANT_EVENT_TYPES:
                if left_coverage.get(event_type) != "complete" or right_coverage.get(event_type) != "complete":
                    continue
                if left_events.get(event_type) != right_events.get(event_type):
                    return True
    return False


def _evidence_chain_id(provider: str, coverage: Mapping[str, str]) -> str:
    if not provider:
        return ""
    return "event-chain-" + _digest(
        {
            "provider": provider,
            "coverage": {event_type: coverage.get(event_type, "unknown") for event_type in IMPORTANT_EVENT_TYPES},
        }
    )[:24]


def _date(value: Any) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text[:10]).date()
    except ValueError:
        return None


def _digest(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


__all__ = [
    "EVENT_USER_STATES",
    "IMPORTANT_EVENT_TYPES",
    "build_candidate_event_risk",
    "normalize_candidate_event_risk",
]
