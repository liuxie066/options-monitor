from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

from domain.domain.ledger.cash_facts import cash_facts_for_trade_event
from domain.domain.ledger.economics import fee_fact_from_persisted_evidence
from domain.domain.ledger.events import TradeEvent
from domain.domain.ledger.fees import FeeComponent
from domain.domain.money import quantize_money, to_decimal
from domain.domain.option_position_identity import normalize_currency
from domain.domain.performance.cash_conversion import (
    DAILY_CASH_FX_METHOD,
    DAILY_CASH_FX_POLICY,
    HISTORICAL_BUSINESS_DAY_FX_CARRY_FORWARD_METHOD,
    MAX_HISTORICAL_CARRY_FORWARD_DISTANCE_MS,
    MAX_BOOKING_RATE_DISTANCE_MS,
    cash_fx_date,
    cash_conversion_id,
    cash_conversion_identity,
    select_cash_fx_rate,
)
from domain.domain.performance.models import FXRateFact
from src.infrastructure.exchange_rates import get_cached_exchange_rates
from src.infrastructure.performance_evidence_sqlite import PerformanceEvidenceSQLiteRepository


def load_cash_fx_payload(repo: Any, *, conn: Any | None = None, persist: bool = True) -> dict[str, Any] | None:
    candidate = getattr(repo, "primary_repo", repo)
    db_path = getattr(candidate, "db_path", None)
    if db_path in (None, ""):
        return None
    evidence_repo = PerformanceEvidenceSQLiteRepository(db_path)
    if not persist:
        return {"fx_rate_facts": evidence_repo.read_fx_rates().fx_rates}
    observation = get_cached_exchange_rates(
        cache_path=(Path(db_path).expanduser().resolve().parent / "rate_cache.json"),
        max_age_hours=None,
    )
    now_ms = utc_now_ms()
    try:
        candidates = cash_fx_observation_facts(observation, observed_at_ms=now_ms) if observation else ()
    except (TypeError, ValueError):
        candidates = ()
    try:
        rates = evidence_repo.freeze_cash_fx_daily_rates(candidates, migrated_at_ms=now_ms, conn=conn)
    except ValueError:
        # Invalid FX evidence must leave CNY pending, without rejecting native cash.
        rates = ()
    return {"fx_rate_facts": rates}


def cash_fx_observation_facts(
    observation: Mapping[str, Any],
    *,
    observed_at_ms: int,
    observation_status: str = "ready",
) -> tuple[FXRateFact, ...]:
    provider = str(observation.get("source") or "").strip()
    rates = observation.get("rates")
    timestamps = observation.get("quote_timestamps")
    timestamps = timestamps if isinstance(timestamps, Mapping) else {}
    if not provider or not isinstance(rates, Mapping) or any(rates.get(pair) in (None, "") for pair in ("USDCNY", "HKDCNY")):
        raise ValueError("FX evidence requires a provider and both currency pairs")
    captured = _payload_timestamp_ms({"timestamp": observation.get("observed_at")}) or int(observed_at_ms)
    if captured > int(observed_at_ms):
        raise ValueError("FX observation capture time is in the future")
    facts = []
    for pair in ("USDCNY", "HKDCNY"):
        timestamp = timestamps.get(pair) or observation.get("timestamp")
        effective = _payload_timestamp_ms({"timestamp": timestamp})
        if effective is None or effective > captured:
            raise ValueError("FX source timestamp is missing or in the future")
        quality = {
            "capture_path": "scheduled_tick",
            "provider_source": provider,
            "source_timestamp_verified": pair in timestamps,
        }
        if observation_status == "unavailable_stale":
            quality["stale_cache_fallback"] = True
        facts.append(FXRateFact(
            fact_id=None,
            base_currency=pair[:3], quote_currency="CNY", rate=rates[pair], rate_kind="spot",
            effective_at_ms=effective, observed_at_ms=captured,
            source="cache_snapshot" if observation_status == "unavailable_stale" else "realtime_snapshot",
            source_id=f"{provider}:{pair}:{effective}", quality=quality, raw=dict(observation),
        ))
    return tuple(facts)


def attach_trade_event_cash_conversions(
    event: TradeEvent,
    *,
    fx_payload: Mapping[str, Any] | None,
    observed_at_ms: int,
) -> TradeEvent:
    from dataclasses import replace

    conversions: dict[str, dict[str, Any]] = {}
    for fact in cash_facts_for_trade_event(event):
        if fact.amount is None:
            continue
        conversions[fact.fact_kind] = build_cash_conversion(
            cash_fact_id=fact.fact_id,
            amount=fact.amount,
            currency=str(fact.currency or ""),
            fx_payload=fx_payload,
            effective_at_ms=fact.effective_at_ms,
            observed_at_ms=observed_at_ms,
        )
    if not conversions:
        return event
    raw_payload = dict(event.raw_payload or {})
    raw_payload["cash_conversions"] = conversions
    return replace(event, raw_payload=raw_payload)


def attach_assigned_stock_sale_cash_conversions(
    event: Mapping[str, Any],
    *,
    fx_payload: Mapping[str, Any] | None,
    observed_at_ms: int,
) -> dict[str, Any]:
    out = dict(event)
    stock_event_id = str(out.get("stock_event_id") or out.get("event_id") or "").strip()
    currency = str(out.get("currency") or "")
    shares = int(out.get("shares") or 0)
    price = to_decimal(out.get("price"), field_name="assigned stock sale price")
    gross = quantize_money(price * Decimal(shares))
    conversions = {
        "assigned_stock_sale_cash_gross": build_cash_conversion(
            cash_fact_id=f"assigned_stock_sale_cash_gross:{stock_event_id}",
            amount=gross,
            currency=currency,
            fx_payload=fx_payload,
            effective_at_ms=int(out.get("trade_time_ms") or out.get("event_time_ms") or observed_at_ms),
            observed_at_ms=observed_at_ms,
        )
    }
    fee = fee_fact_from_persisted_evidence(
        event_id=stock_event_id,
        component=FeeComponent.STOCK_SALE,
        provenance=out.get("fee_provenance"),
        compatibility_amount=out.get("fees") or 0,
    )
    if fee.is_complete and fee.amount is not None:
        conversions["assigned_stock_sale_fee_cash"] = build_cash_conversion(
            cash_fact_id=f"assigned_stock_sale_fee_cash:{stock_event_id}",
            amount=-quantize_money(fee.amount),
            currency=currency,
            fx_payload=fx_payload,
            effective_at_ms=int(out.get("trade_time_ms") or out.get("event_time_ms") or observed_at_ms),
            observed_at_ms=observed_at_ms,
        )
    out["cash_conversions"] = conversions
    return out


def build_cash_conversion(
    *,
    cash_fact_id: str,
    amount: Decimal | float | int | str,
    currency: str,
    fx_payload: Mapping[str, Any] | None,
    effective_at_ms: int,
    observed_at_ms: int,
    rate_source: str | None = None,
    rate_source_id: str | None = None,
    rate_evidence_fact_id: str | None = None,
    method: str | None = None,
    max_rate_distance_ms: int = MAX_BOOKING_RATE_DISTANCE_MS,
) -> dict[str, Any]:
    native_amount = quantize_money(to_decimal(amount, field_name="cash conversion amount"))
    native_currency = normalize_currency(currency)
    daily_selection = isinstance(fx_payload, Mapping) and "fx_rate_facts" in fx_payload
    selected_rate = None
    if daily_selection:
        selection = select_cash_fx_rate(fx_payload["fx_rate_facts"], base_currency=native_currency, at_ms=int(effective_at_ms))
        selected_rate = selection.fact
        fx_payload = None
        method = DAILY_CASH_FX_METHOD
        if isinstance(selected_rate, FXRateFact):
            fx_payload = {
                "rates": {f"{native_currency}CNY": str(selected_rate.rate)},
                "timestamp": datetime.fromtimestamp(selected_rate.effective_at_ms / 1000, tz=timezone.utc).isoformat(),
            }
            rate_source = selected_rate.source
            rate_source_id = selected_rate.source_id
            rate_evidence_fact_id = str(selected_rate.fact_id)
            if (
                selected_rate.quality.get("cash_fx_policy") != DAILY_CASH_FX_POLICY
                or selected_rate.supersedes_fact_id is not None
            ):
                method = "historical_fx_evidence_backfill"
                if cash_fx_date(selected_rate.effective_at_ms) != cash_fx_date(effective_at_ms):
                    method = HISTORICAL_BUSINESS_DAY_FX_CARRY_FORWARD_METHOD
                    max_rate_distance_ms = MAX_HISTORICAL_CARRY_FORWARD_DISTANCE_MS
    rate: Decimal | None = None
    conversion_method = str(method or "booking_fx_snapshot").strip()
    conversion_rate_source = str(rate_source or "rate_cache").strip()
    rate_timestamp = _payload_timestamp(fx_payload)
    rate_timestamp_ms = _payload_timestamp_ms(fx_payload)
    missing_reason: str | None = None
    if native_amount == 0:
        amount_cny = Decimal(0)
        conversion_method = "zero_identity"
    elif native_currency == "CNY":
        rate = Decimal(1)
        amount_cny = native_amount
        conversion_method = "cny_identity"
        conversion_rate_source = "identity"
    else:
        rates = fx_payload.get("rates") if isinstance(fx_payload, Mapping) and isinstance(fx_payload.get("rates"), Mapping) else {}
        try:
            rate = to_decimal(rates.get(f"{native_currency}CNY"), field_name="cash conversion FX rate")
            if rate <= 0:
                raise ValueError("cash conversion FX rate must be positive")
        except (TypeError, ValueError):
            rate = None
            missing_reason = f"{native_currency}CNY booking FX unavailable"
        if rate is not None and rate_timestamp_ms is None:
            rate = None
            missing_reason = f"{native_currency}CNY booking FX timestamp unavailable"
        if rate is not None and abs(rate_timestamp_ms - int(effective_at_ms)) > int(
            max_rate_distance_ms
        ):
            rate = None
            missing_reason = f"{native_currency}CNY booking FX outside 24h event window"
        amount_cny = quantize_money(native_amount * rate) if rate is not None else None
    status = "observed" if amount_cny is not None else "pending"
    source_id = str(rate_source_id or "").strip() or f"{native_currency}CNY:{rate_timestamp or int(observed_at_ms)}"
    identity = cash_conversion_identity(
        cash_fact_id=str(cash_fact_id),
        native_amount=native_amount,
        native_currency=native_currency,
        fx_rate=rate,
        amount_cny=amount_cny,
        rate_source_id=source_id,
        effective_at_ms=int(effective_at_ms),
    )
    result = {
        "schema_version": "cash_conversion.v1",
        "conversion_id": cash_conversion_id(identity),
        **identity,
        "quote_currency": "CNY",
        "status": status,
        "method": conversion_method,
        "rate_source": conversion_rate_source if rate is not None else None,
        "rate_evidence_fact_id": str(rate_evidence_fact_id or "").strip() or None,
        "rate_timestamp": rate_timestamp,
        "observed_at_ms": int(observed_at_ms),
        "missing_reason": None if status == "observed" else missing_reason or f"{native_currency}CNY booking FX unavailable",
    }
    if conversion_method == DAILY_CASH_FX_METHOD:
        result.update(fx_policy=DAILY_CASH_FX_POLICY, cash_fx_date=cash_fx_date(effective_at_ms))
    if isinstance(selected_rate, FXRateFact):
        result["rate_observed_at_ms"] = selected_rate.observed_at_ms
    return result


def utc_now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _payload_timestamp(payload: Mapping[str, Any] | None) -> str | None:
    if not isinstance(payload, Mapping):
        return None
    raw = str(payload.get("timestamp") or "").strip()
    return raw or None


def _payload_timestamp_ms(payload: Mapping[str, Any] | None) -> int | None:
    raw = _payload_timestamp(payload)
    if raw is None:
        return None
    try:
        value = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return int(value.astimezone(timezone.utc).timestamp() * 1000)


__all__ = [
    "attach_assigned_stock_sale_cash_conversions",
    "attach_trade_event_cash_conversions",
    "build_cash_conversion",
    "load_cash_fx_payload",
    "utc_now_ms",
]
