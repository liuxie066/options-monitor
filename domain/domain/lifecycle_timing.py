"""Pure lifecycle receipt timing and its stable JSON identity."""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, time
from typing import Any, Iterable
from zoneinfo import ZoneInfo

TIMING_POLICY_SCHEMA = "lifecycle_timing_policy.v1"
EFFECTIVE_TIMING_SCHEMA = "effective_lifecycle_timing.v2"
PAIRING_WINDOW_MS = 15 * 60 * 1000

INSTRUMENT_POLICY_SCHEMA = "lifecycle_instrument_policy.v1"
INSTRUMENT_POLICY_REGISTRY: dict[str, dict[str, Any]] = {
    "US:standard_equity_option": {
        "schema_version": INSTRUMENT_POLICY_SCHEMA,
        "policy_id": "us_standard_equity_option.v1",
        "market": "US",
        "contract_class": "standard_equity_option",
        "underlying_security_type": "equity",
        "settlement_style": "physical",
        "timezone": "America/New_York",
        "last_trade_session_close": "16:00:00",
        "half_trade_session_close": "13:00:00",
    },
    "HK:standard_equity_option": {
        "schema_version": INSTRUMENT_POLICY_SCHEMA,
        "policy_id": "hk_standard_equity_option.v1",
        "market": "HK",
        "contract_class": "standard_equity_option",
        "underlying_security_type": "equity",
        "settlement_style": "physical",
        "timezone": "Asia/Hong_Kong",
        "last_trade_session_close": "16:00:00",
        "half_trade_session_close": "12:00:00",
    },
}


def canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def effective_last_trade_cutoff_ms(
    policy: dict[str, Any], *, expiration_ymd: str | None = None,
) -> int:
    """Apply the expiration session to registry cutoffs, including stored v1 rows."""
    cutoff = int(policy.get("last_trade_cutoff_ms") or 0)
    if policy.get("last_trade_cutoff_source") != "instrument_policy_registry" or cutoff <= 0:
        return cutoff
    registry = INSTRUMENT_POLICY_REGISTRY.get(
        f"{str(policy.get('market') or '').upper()}:standard_equity_option"
    )
    if registry is None:
        return cutoff
    zone = ZoneInfo(str(registry["timezone"]))
    expiration = (
        date.fromisoformat(expiration_ymd)
        if expiration_ymd
        else datetime.fromtimestamp(cutoff / 1000, zone).date()
    )
    session = next((
        str(row.get("type") or "").upper()
        for row in policy.get("trading_days") or []
        if isinstance(row, dict) and row.get("date") == expiration.isoformat()
    ), None)
    # Some older policies only froze following days. Retain their authoritative
    # cutoff when the expiration session is absent; never roll the contract date.
    if session is None:
        return cutoff
    if session not in {"WHOLE", "MORNING", "AFTERNOON", "TRADING"}:
        raise ValueError("contract expiration is not a calendar trading day")
    close_key = "half_trade_session_close" if session == "MORNING" else "last_trade_session_close"
    return int(datetime.combine(
        expiration, time.fromisoformat(str(registry[close_key])), tzinfo=zone,
    ).timestamp() * 1000)


def derive_effective_lifecycle_timing(
    *,
    policy: dict[str, Any],
    option_close_evidence: Iterable[dict[str, Any]],
    expiration_ymd: str | None = None,
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
    cutoff = effective_last_trade_cutoff_ms(timing, expiration_ymd=expiration_ymd)
    pairing_until = received_at_ms + PAIRING_WINDOW_MS
    return {
        "schema_version": EFFECTIVE_TIMING_SCHEMA,
        "case_id": str(timing.get("case_id") or "").strip(),
        "last_trade_cutoff_ms": cutoff,
        "pairing_until_ms": pairing_until,
        "review_after_ms": max(cutoff, pairing_until),
        # This persisted boundary remains the final negative-evidence gate and
        # stock-leg matching upper bound, not the first time we may query evidence.
        "settlement_deadline_ms": int(timing.get("settlement_deadline_ms") or 0),
        "settlement_style": str(timing.get("settlement_style") or "").strip().lower(),
        # Preserve the immutable policy's audit identity. Scheduling separately
        # includes EFFECTIVE_TIMING_SCHEMA so old cached attempts are replanned.
        "timing_policy_hash": canonical_hash(timing),
        "first_option_close_received_at_ms": received_at_ms,
        "last_option_close_received_at_ms": max(accepted),
    }
