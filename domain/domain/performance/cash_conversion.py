from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping

from domain.domain.performance.models import (
    canonical_decimal_text,
    normalize_currency,
    quantize_money,
    to_decimal,
)


MAX_BOOKING_RATE_DISTANCE_MS = 24 * 60 * 60 * 1000
FOREIGN_METHODS = {"booking_fx_snapshot", "historical_fx_evidence_backfill"}


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

    if (
        conversion.get("schema_version") != "cash_conversion.v1"
        or str(conversion.get("status") or "").strip().lower() != "observed"
        or str(conversion.get("cash_fact_id") or "") != str(cash_fact_id)
        or str(conversion.get("quote_currency") or "").strip().upper() != "CNY"
        or amount != expected_amount
        or normalize_currency(conversion.get("native_currency")) != expected_currency
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
        if method != "zero_identity" or amount_cny != 0:
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
        if (
            abs(rate_timestamp_ms - conversion_effective_at_ms)
            > MAX_BOOKING_RATE_DISTANCE_MS
        ):
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
    "MAX_BOOKING_RATE_DISTANCE_MS",
    "cash_conversion_id",
    "cash_conversion_identity",
    "validate_observed_cash_conversion",
]
