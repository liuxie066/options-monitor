from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal, DecimalException
from typing import Any, Mapping
from dataclasses import replace
from zoneinfo import ZoneInfo

from domain.domain.performance.models import (
    canonical_decimal_text,
    EvidenceSelection,
    FXRateFact,
    normalize_currency,
    quantize_money,
    to_decimal,
    select_fx_rate,
)


MAX_BOOKING_RATE_DISTANCE_MS = 24 * 60 * 60 * 1000
MAX_HISTORICAL_CARRY_FORWARD_DISTANCE_MS = 7 * 24 * 60 * 60 * 1000
HISTORICAL_BUSINESS_DAY_FX_CARRY_FORWARD_METHOD = (
    "historical_business_day_fx_carry_forward"
)
OFFICIAL_CARRY_FORWARD_SOURCES = frozenset(
    {"pbc_central_parity", "manual_correction"}
)
FOREIGN_METHODS = {
    "booking_fx_snapshot",
    "historical_fx_evidence_backfill",
    HISTORICAL_BUSINESS_DAY_FX_CARRY_FORWARD_METHOD,
}
DAILY_CASH_FX_POLICY = "shanghai_first_observed.v1"
DAILY_CASH_FX_METHOD = "shanghai_daily_fx"
FOREIGN_METHODS.add(DAILY_CASH_FX_METHOD)
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_MARKET_FX_SOURCES = frozenset({"tencent_quote", "sina_quote"})


def cash_fx_date(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(int(timestamp_ms) / 1000, tz=_SHANGHAI).date().isoformat()


def fixed_cash_fx_fact(fact: FXRateFact) -> FXRateFact | None:
    """Construct a day identity only from a dated, same-day market observation."""
    provider = str(fact.quality.get("provider_source") or fact.source)
    day = cash_fx_date(fact.effective_at_ms)
    if (
        provider not in _MARKET_FX_SOURCES
        or fact.quote_currency != "CNY"
        or fact.quality.get("source_timestamp_verified") is not True
        or cash_fx_date(fact.observed_at_ms) != day
        or fact.effective_at_ms > fact.observed_at_ms
    ):
        return None
    identity = f"cashfxday:{DAILY_CASH_FX_POLICY}:{day}:{fact.base_currency}:{fact.quote_currency}"
    return replace(
        fact,
        fact_id=identity,
        source_id=identity,
        source=provider,
        revision=1,
        supersedes_fact_id=None,
        quality={**fact.quality, "cash_fx_policy": DAILY_CASH_FX_POLICY, "cash_fx_date": day},
        raw={**fact.raw, "selected_fx_fact": fact.normalized_payload()},
    )


def cash_fx_daily_facts(facts: list[FXRateFact] | tuple[FXRateFact, ...]) -> tuple[FXRateFact, ...]:
    by_id = {fact.fact_id: fact for fact in facts}
    for fact in sorted(facts, key=lambda item: (item.observed_at_ms, 0 if item.quality.get("provider_source", item.source) == "tencent_quote" else 1, str(item.fact_id))):
        if fact.quality.get("cash_fx_policy") != DAILY_CASH_FX_POLICY:
            fixed = fixed_cash_fx_fact(fact)
            if fixed is not None:
                by_id.setdefault(fixed.fact_id, fixed)
    return tuple(by_id.values())


def select_cash_fx_rate(
    facts: list[FXRateFact] | tuple[FXRateFact, ...],
    *,
    base_currency: str,
    at_ms: int,
) -> EvidenceSelection:
    """Cash uses the Shanghai day; valuation selectors retain instant semantics."""
    day = cash_fx_date(at_ms)
    matching = [fact for fact in facts if fact.base_currency == normalize_currency(base_currency) and fact.quote_currency == "CNY"]
    fixed = [
        fact for fact in matching
        if fact.quality.get("cash_fx_policy") == DAILY_CASH_FX_POLICY
        and fact.quality.get("cash_fx_date") == day
        and fact.supersedes_fact_id is None
    ]
    if fixed:
        anchor = min(fixed, key=lambda fact: (fact.observed_at_ms, str(fact.fact_id)))
        lineage = {anchor.fact_id: anchor}
        corrections = [
            fact for fact in matching
            if fact.source in OFFICIAL_CARRY_FORWARD_SOURCES
            and cash_fx_date(fact.effective_at_ms) == day
        ]
        # Later quotes cannot replace the day anchor without explicit correction lineage.
        while additions := [
            fact for fact in corrections
            if fact.fact_id not in lineage and fact.supersedes_fact_id in lineage
        ]:
            lineage.update((fact.fact_id, fact) for fact in additions)
        selection = select_fx_rate(
            list(lineage.values()), base_currency=base_currency,
            at_ms=max(fact.effective_at_ms for fact in lineage.values()),
            max_staleness_ms=MAX_BOOKING_RATE_DISTANCE_MS,
        )
        return replace(
            selection, at_ms=int(at_ms),
            staleness_ms=abs(int(at_ms) - selection.fact.effective_at_ms) if selection.fact else None,
            reason="cash_fx_daily_corrected" if selection.fact != anchor else "cash_fx_daily_fixed",
        )
    official = [fact for fact in matching if fact.source in OFFICIAL_CARRY_FORWARD_SOURCES]
    same_day = [fact for fact in official if cash_fx_date(fact.effective_at_ms) == day]
    if same_day:
        return select_fx_rate(same_day, base_currency=base_currency, at_ms=max(fact.effective_at_ms for fact in same_day), max_staleness_ms=MAX_BOOKING_RATE_DISTANCE_MS)
    carried = [
        fact for fact in official
        if fact.quality.get("official") is True
        and isinstance(fact.quality.get("carry_forward_dates"), (list, tuple))
        and day in fact.quality["carry_forward_dates"]
    ]
    if carried:
        return select_fx_rate(carried, base_currency=base_currency, at_ms=int(at_ms), max_staleness_ms=MAX_HISTORICAL_CARRY_FORWARD_DISTANCE_MS)
    return EvidenceSelection(None, "missing", int(at_ms), reason="no fixed Shanghai-day FX or explicit official carry date")


def cash_conversion_identity(
    *,
    cash_fact_id: str,
    native_amount: Any,
    native_currency: str,
    fx_rate: Any,
    amount_cny: Any,
    rate_source_id: str,
    effective_at_ms: int,
) -> dict[str, Any]:
    amount = quantize_money(
        to_decimal(native_amount, field_name="native_amount")
    )
    rate = (
        to_decimal(fx_rate, field_name="fx_rate")
        if fx_rate is not None
        else None
    )
    converted = (
        quantize_money(to_decimal(amount_cny, field_name="amount_cny"))
        if amount_cny is not None
        else None
    )
    return {
        "cash_fact_id": str(cash_fact_id),
        "native_amount": canonical_decimal_text(
            amount,
            field_name="native_amount",
        ),
        "native_currency": normalize_currency(native_currency),
        "fx_rate": (
            canonical_decimal_text(rate, field_name="fx_rate")
            if rate is not None
            else None
        ),
        "amount_cny": (
            canonical_decimal_text(converted, field_name="amount_cny")
            if converted is not None
            else None
        ),
        "rate_source_id": str(rate_source_id or "").strip(),
        "effective_at_ms": int(effective_at_ms),
    }


def cash_conversion_id(identity: Mapping[str, Any]) -> str:
    digest = hashlib.sha256(
        json.dumps(
            dict(identity),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()[:24]
    return f"cashfx_{digest}"


def validate_observed_cash_conversion(
    conversion: Mapping[str, Any],
    *,
    cash_fact_id: str,
    native_amount: Any,
    native_currency: str,
    effective_at_ms: int,
) -> tuple[Decimal | None, str | None]:
    try:
        return _validate_observed_cash_conversion(
            conversion,
            cash_fact_id=cash_fact_id,
            native_amount=native_amount,
            native_currency=native_currency,
            effective_at_ms=effective_at_ms,
        )
    except (DecimalException, OverflowError, TypeError, ValueError):
        return None, "invalid_numeric_contract"


def _validate_observed_cash_conversion(
    conversion: Mapping[str, Any],
    *,
    cash_fact_id: str,
    native_amount: Any,
    native_currency: str,
    effective_at_ms: int,
) -> tuple[Decimal | None, str | None]:
    try:
        expected_amount = quantize_money(
            to_decimal(native_amount, field_name="native_amount")
        )
        expected_currency = normalize_currency(native_currency)
        amount = quantize_money(
            to_decimal(conversion.get("native_amount"), field_name="native_amount")
        )
        amount_cny = quantize_money(
            to_decimal(conversion.get("amount_cny"), field_name="amount_cny")
        )
        rate = (
            to_decimal(conversion.get("fx_rate"), field_name="fx_rate")
            if conversion.get("fx_rate") is not None
            else None
        )
        conversion_effective_at_ms = int(conversion.get("effective_at_ms") or 0)
        observed_at_ms = int(conversion.get("observed_at_ms") or 0)
    except (TypeError, ValueError):
        return None, "invalid_numeric_contract"
    try:
        conversion_currency = normalize_currency(conversion.get("native_currency"))
    except ValueError:
        return None, "identity_contract_mismatch"

    if (
        conversion.get("schema_version") != "cash_conversion.v1"
        or str(conversion.get("status") or "").strip().lower() != "observed"
        or str(conversion.get("cash_fact_id") or "") != str(cash_fact_id)
        or str(conversion.get("quote_currency") or "").strip().upper() != "CNY"
        or amount != expected_amount
        or conversion_currency != expected_currency
        or conversion_effective_at_ms != int(effective_at_ms)
        or conversion_effective_at_ms <= 0
        or observed_at_ms <= 0
    ):
        return None, "identity_contract_mismatch"

    method = str(conversion.get("method") or "").strip()
    rate_source = str(conversion.get("rate_source") or "").strip()
    rate_source_id = str(conversion.get("rate_source_id") or "").strip()
    if not rate_source_id:
        return None, "rate_source_id_missing"
    if amount == 0:
        if method != "zero_identity" or rate is not None or amount_cny != 0:
            return None, "zero_identity_mismatch"
    elif expected_currency == "CNY":
        if (
            method != "cny_identity"
            or rate != Decimal(1)
            or amount_cny != amount
            or rate_source != "identity"
        ):
            return None, "cny_identity_mismatch"
    else:
        if method not in FOREIGN_METHODS or not rate_source:
            return None, "fx_provenance_invalid"
        if rate is None or rate <= 0:
            return None, "fx_rate_invalid"
        if amount_cny != quantize_money(amount * rate):
            return None, "fx_arithmetic_mismatch"
        rate_timestamp_ms = _timestamp_ms(conversion.get("rate_timestamp"))
        if rate_timestamp_ms is None:
            return None, "rate_timestamp_invalid"
        if method == DAILY_CASH_FX_METHOD and (
            conversion.get("fx_policy") != DAILY_CASH_FX_POLICY
            or conversion.get("cash_fx_date") != cash_fx_date(conversion_effective_at_ms)
            or cash_fx_date(rate_timestamp_ms) != cash_fx_date(conversion_effective_at_ms)
            or not str(conversion.get("rate_evidence_fact_id") or "").strip()
            or rate_source not in _MARKET_FX_SOURCES
        ):
            return None, "fx_provenance_invalid"
        if method == HISTORICAL_BUSINESS_DAY_FX_CARRY_FORWARD_METHOD and (
            rate_timestamp_ms > conversion_effective_at_ms
            or rate_source not in OFFICIAL_CARRY_FORWARD_SOURCES
            or not str(conversion.get("rate_evidence_fact_id") or "").strip()
        ):
            return None, "fx_provenance_invalid"
        max_rate_distance_ms = (
            MAX_HISTORICAL_CARRY_FORWARD_DISTANCE_MS
            if method == HISTORICAL_BUSINESS_DAY_FX_CARRY_FORWARD_METHOD
            else MAX_BOOKING_RATE_DISTANCE_MS
        )
        if abs(rate_timestamp_ms - conversion_effective_at_ms) > max_rate_distance_ms:
            return None, "rate_timestamp_outside_booking_window"

    identity = cash_conversion_identity(
        cash_fact_id=cash_fact_id,
        native_amount=amount,
        native_currency=expected_currency,
        fx_rate=rate,
        amount_cny=amount_cny,
        rate_source_id=rate_source_id,
        effective_at_ms=conversion_effective_at_ms,
    )
    if str(conversion.get("conversion_id") or "").strip() != cash_conversion_id(
        identity
    ):
        return None, "conversion_id_mismatch"
    return amount_cny, None


def _timestamp_ms(value: Any) -> int | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.astimezone(timezone.utc).timestamp() * 1000)


__all__ = [
    "DAILY_CASH_FX_METHOD",
    "DAILY_CASH_FX_POLICY",
    "HISTORICAL_BUSINESS_DAY_FX_CARRY_FORWARD_METHOD",
    "MAX_BOOKING_RATE_DISTANCE_MS",
    "MAX_HISTORICAL_CARRY_FORWARD_DISTANCE_MS",
    "OFFICIAL_CARRY_FORWARD_SOURCES",
    "cash_conversion_id",
    "cash_conversion_identity",
    "cash_fx_daily_facts",
    "cash_fx_date",
    "fixed_cash_fx_fact",
    "select_cash_fx_rate",
    "validate_observed_cash_conversion",
]
