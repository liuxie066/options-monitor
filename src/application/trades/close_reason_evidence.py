from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, time, timedelta
from typing import Any, Iterable
from zoneinfo import ZoneInfo


TIMING_POLICY_SCHEMA = "lifecycle_timing_policy.v1"
SETTLEMENT_OBSERVATION_SCHEMA = "broker_settlement_observation.v2"
PAIRING_WINDOW_MS = 15 * 60 * 1000
REQUIRED_SETTLEMENT_SOURCES = (
    "anchor_option_close",
    "history_deals",
    "history_orders",
    "fresh_positions",
    "trading_calendar",
    "contract_metadata",
)
MARKET_TIMEZONES = {
    "US": "America/New_York",
    "HK": "Asia/Hong_Kong",
}
SUPPORTED_LAST_TRADE_SOURCES = {
    "broker_contract_metadata",
    "instrument_policy_registry",
}


def canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_lifecycle_timing_policy(
    *,
    case_id: str,
    market: str,
    expiration_ymd: str,
    contract_metadata: dict[str, Any],
    trading_days: Iterable[dict[str, Any] | str],
    calendar_source: str,
    calendar_observed_at_ms: int,
) -> dict[str, Any]:
    case_value = str(case_id or "").strip()
    market_value = str(market or "").strip().upper()
    timezone_name = MARKET_TIMEZONES.get(market_value)
    metadata = dict(contract_metadata or {})
    settlement_style = str(
        metadata.get("settlement_style") or ""
    ).strip().lower()
    security_type = str(
        metadata.get("underlying_security_type") or ""
    ).strip().lower()
    cutoff_source = str(
        metadata.get("last_trade_cutoff_source") or ""
    ).strip().lower()
    cutoff_ms = int(metadata.get("last_trade_cutoff_ms") or 0)
    observed_at_ms = int(calendar_observed_at_ms or 0)
    if not case_value or timezone_name is None:
        raise ValueError("lifecycle timing identity is incomplete")
    if settlement_style != "physical" or security_type != "equity":
        raise ValueError("unsupported lifecycle settlement metadata")
    if cutoff_source not in SUPPORTED_LAST_TRADE_SOURCES or cutoff_ms <= 0:
        raise ValueError("authoritative last trade cutoff is unavailable")
    if observed_at_ms <= 0:
        raise ValueError("lifecycle timing observation times are invalid")
    expiration = date.fromisoformat(str(expiration_ymd or "").strip())
    normalized_days = _normalize_trading_days(trading_days)
    following = [
        item
        for item in normalized_days
        if date.fromisoformat(item["date"]) > expiration
        and item["type"] in {"WHOLE", "TRADING"}
    ]
    if len(following) < 2:
        raise ValueError("two following broker business days are unavailable")
    selected = following[:2]
    timezone = ZoneInfo(timezone_name)
    deadline_local = datetime.combine(
        date.fromisoformat(selected[1]["date"]) + timedelta(days=1),
        time.min,
        tzinfo=timezone,
    )
    calendar_hash = canonical_hash(normalized_days)
    return {
        "policy_schema": TIMING_POLICY_SCHEMA,
        "case_id": case_value,
        "market": market_value,
        "timezone": timezone_name,
        "settlement_style": settlement_style,
        "underlying_security_type": security_type,
        "last_trade_cutoff_ms": cutoff_ms,
        "last_trade_cutoff_source": cutoff_source,
        "settlement_deadline_ms": int(deadline_local.timestamp() * 1000),
        "trading_days": normalized_days,
        "calendar_source": str(calendar_source or "").strip(),
        "calendar_observed_at_ms": observed_at_ms,
        "calendar_hash": calendar_hash,
    }


def derive_effective_lifecycle_timing(
    *,
    policy: dict[str, Any],
    option_close_evidence: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    timing = dict(policy or {})
    if str(timing.get("policy_schema") or "") != TIMING_POLICY_SCHEMA:
        raise ValueError("lifecycle timing policy is unavailable")
    accepted: list[int] = []
    for item in option_close_evidence:
        if (
            not isinstance(item, dict)
            or str(item.get("evidence_type") or "").strip().lower()
            != "option_zero_price_close"
        ):
            continue
        try:
            received_at_ms = int(item.get("received_at_ms") or 0)
        except (TypeError, ValueError, OverflowError):
            continue
        if received_at_ms > 0:
            accepted.append(received_at_ms)
    if not accepted:
        raise ValueError("first accepted option close receipt is unavailable")
    received_at_ms = min(accepted)
    return {
        "schema_version": "effective_lifecycle_timing.v1",
        "case_id": str(timing.get("case_id") or "").strip(),
        "last_trade_cutoff_ms": int(
            timing.get("last_trade_cutoff_ms") or 0
        ),
        "pairing_until_ms": received_at_ms + PAIRING_WINDOW_MS,
        "settlement_deadline_ms": int(
            timing.get("settlement_deadline_ms") or 0
        ),
        "settlement_style": str(
            timing.get("settlement_style") or ""
        ).strip().lower(),
        "timing_policy_hash": canonical_hash(timing),
        "first_option_close_received_at_ms": received_at_ms,
    }


def build_settlement_source_receipt(
    *,
    source: str,
    query_input: dict[str, Any],
    rows: Iterable[dict[str, Any]],
    observed_at_ms: int,
    retcode: Any,
    coverage_complete: bool,
    pagination_complete: bool = True,
    stale: bool = False,
    fallback_cache: bool = False,
    error: str | None = None,
) -> dict[str, Any]:
    canonical_rows = [
        dict(item)
        for item in rows
        if isinstance(item, dict)
    ]
    complete = (
        retcode in {0, "0", "OK", "ok", None}
        and bool(coverage_complete)
        and bool(pagination_complete)
        and not stale
        and not fallback_cache
        and not str(error or "").strip()
    )
    return {
        "source": str(source or "").strip(),
        "query_input": dict(query_input or {}),
        "retcode": retcode,
        "row_count": len(canonical_rows),
        "coverage_complete": bool(coverage_complete),
        "pagination_complete": bool(pagination_complete),
        "stale": bool(stale),
        "fallback_cache": bool(fallback_cache),
        "observed_at_ms": int(observed_at_ms or 0),
        "rows": canonical_rows,
        "payload_hash": canonical_hash(canonical_rows),
        "status": "complete" if complete else "incomplete",
        "error": str(error or "").strip() or None,
    }


def build_broker_settlement_observation(
    *,
    case_id: str,
    account: str,
    futu_account_id: str,
    market: str,
    contract_identity: dict[str, Any],
    target_contracts_by_lot: dict[str, int],
    frozen_preterminal_remaining_by_lot: dict[str, int],
    anchor_option_deal_key: str,
    anchor_execution_time_ms: int,
    observed_at_ms: int,
    settlement_deadline_ms: int,
    query_window: dict[str, Any],
    source_receipts: dict[str, dict[str, Any]],
    calendar_hash: str,
    broker_option_position_absent: bool,
    projection_matches_frozen_remaining: bool,
    reservation_exclusive: bool,
    competing_effective_consumption: bool,
    stock_settlement_present: bool,
    stock_settlement_candidates: Iterable[
        dict[str, Any]
    ] = (),
    normal_order_present: bool,
    additional_incomplete_reason_codes: Iterable[str] = (),
) -> dict[str, Any]:
    incomplete: set[str] = {
        str(item or "").strip()
        for item in additional_incomplete_reason_codes
        if str(item or "").strip()
    }
    receipts = {
        str(name): dict(receipt or {})
        for name, receipt in dict(source_receipts or {}).items()
    }
    for name in REQUIRED_SETTLEMENT_SOURCES:
        receipt = receipts.get(name)
        if not isinstance(receipt, dict):
            incomplete.add(f"{name}_missing")
            continue
        if str(receipt.get("status") or "") != "complete":
            incomplete.add(f"{name}_incomplete")
        if int(receipt.get("observed_at_ms") or 0) <= 0:
            incomplete.add(f"{name}_observed_at_missing")
        rows = list(receipt.get("rows") or [])
        if str(receipt.get("payload_hash") or "") != canonical_hash(rows):
            incomplete.add(f"{name}_payload_hash_mismatch")
    if int(observed_at_ms or 0) < int(settlement_deadline_ms or 0):
        incomplete.add("settlement_deadline_not_reached")
    if not broker_option_position_absent:
        incomplete.add("broker_option_position_present")
    if not projection_matches_frozen_remaining:
        incomplete.add("projection_frozen_remaining_mismatch")
    if not reservation_exclusive:
        incomplete.add("reservation_not_exclusive")
    if competing_effective_consumption:
        incomplete.add("competing_effective_consumption")
    if stock_settlement_present:
        incomplete.add("stock_settlement_present")
    if normal_order_present:
        incomplete.add("normal_order_present")
    if set(target_contracts_by_lot) != set(
        frozen_preterminal_remaining_by_lot
    ):
        incomplete.add("frozen_target_manifest_mismatch")
    if not str(calendar_hash or "").strip():
        incomplete.add("calendar_hash_missing")

    payload = {
        "schema_version": SETTLEMENT_OBSERVATION_SCHEMA,
        "case_id": str(case_id or "").strip(),
        "account": str(account or "").strip().lower(),
        "futu_account_id": str(futu_account_id or "").strip(),
        "market": str(market or "").strip().upper(),
        "contract_identity": dict(contract_identity or {}),
        "target_contracts_by_lot": dict(target_contracts_by_lot or {}),
        "frozen_preterminal_remaining_by_lot": dict(
            frozen_preterminal_remaining_by_lot or {}
        ),
        "anchor_option_deal_key": str(
            anchor_option_deal_key or ""
        ).strip(),
        "anchor_execution_time_ms": int(anchor_execution_time_ms or 0),
        "observed_at_ms": int(observed_at_ms or 0),
        "query_window": dict(query_window or {}),
        "required_sources": list(REQUIRED_SETTLEMENT_SOURCES),
        "source_results": {
            name: {
                "status": receipt.get("status"),
                "row_count": receipt.get("row_count"),
                "payload_hash": receipt.get("payload_hash"),
            }
            for name, receipt in sorted(receipts.items())
        },
        "relevant_rows": {
            name: list(receipt.get("rows") or [])
            for name, receipt in sorted(receipts.items())
        },
        "source_payload_hashes": {
            name: receipt.get("payload_hash")
            for name, receipt in sorted(receipts.items())
        },
        "source_receipts": receipts,
        "calendar_hash": str(calendar_hash or "").strip(),
        "broker_option_position_absent": bool(
            broker_option_position_absent
        ),
        "projection_matches_frozen_remaining": bool(
            projection_matches_frozen_remaining
        ),
        "reservation_exclusive": bool(reservation_exclusive),
        "competing_effective_consumption": bool(
            competing_effective_consumption
        ),
        "stock_settlement_present": bool(stock_settlement_present),
        "stock_settlement_candidates": [
            dict(item)
            for item in stock_settlement_candidates
            if isinstance(item, dict)
        ],
        "normal_order_present": bool(normal_order_present),
        "complete": not incomplete,
        "incomplete_reason_codes": sorted(incomplete),
    }
    observation_id = "observation_" + canonical_hash(payload)
    return {"observation_id": observation_id, **payload}


def _normalize_trading_days(
    rows: Iterable[dict[str, Any] | str],
) -> list[dict[str, str]]:
    normalized: dict[str, str] = {}
    for item in rows:
        if isinstance(item, str):
            day = item.strip()
            day_type = "TRADING"
        elif isinstance(item, dict):
            day = str(
                item.get("date")
                or item.get("time")
                or item.get("trade_date")
                or ""
            ).strip()
            day_type = str(
                item.get("type")
                or item.get("trade_date_type")
                or item.get("trade_type")
                or ""
            ).strip().upper()
        else:
            continue
        if not day:
            continue
        date.fromisoformat(day)
        normalized[day] = day_type
    return [
        {"date": day, "type": normalized[day]}
        for day in sorted(normalized)
    ]


__all__ = [
    "MARKET_TIMEZONES",
    "PAIRING_WINDOW_MS",
    "REQUIRED_SETTLEMENT_SOURCES",
    "SETTLEMENT_OBSERVATION_SCHEMA",
    "TIMING_POLICY_SCHEMA",
    "build_broker_settlement_observation",
    "derive_effective_lifecycle_timing",
    "build_lifecycle_timing_policy",
    "build_settlement_source_receipt",
    "canonical_hash",
]
