from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from decimal import Decimal, DecimalException
from typing import Any, Mapping
from dataclasses import replace
from zoneinfo import ZoneInfo

from domain.domain.fx_quote_policy import CALENDAR_EVIDENCE, quote_quality, quote_session_start
from domain.domain.decision_state_fingerprint import canonical_sha256

from domain.domain.performance.models import (
    canonical_decimal_text,
    EvidenceSelection,
    FXRateFact,
    normalize_currency,
    quantize_money,
    to_decimal,
    select_fx_rate,
)


OPEND_EXCHANGE_RATE_SOURCE = "opend_account_funds_conversion"
TENCENT_EXCHANGE_RATE_SOURCE = "tencent_quote"
SINA_EXCHANGE_RATE_SOURCE = "sina_quote"
_REQUIRED_RATES = ("USDCNY", "HKDCNY")


def _valid_rate(raw: Any) -> float | None:
    if isinstance(raw, bool):
        return None
    try:
        number = float(raw)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and 0 < number < 1000 else None


def _strict_timestamp(raw: Any) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value.astimezone(timezone.utc) if value.tzinfo is not None else None


def _verified_pair(row: Any, *, now: datetime) -> dict[str, Any] | None:
    if not isinstance(row, Mapping):
        return None
    source = row.get("source")
    rate = _valid_rate(row.get("rate"))
    quoted = _strict_timestamp(row.get("quote_at_utc"))
    observed = _strict_timestamp(row.get("observed_at_utc"))
    if (
        source not in {TENCENT_EXCHANGE_RATE_SOURCE, SINA_EXCHANGE_RATE_SOURCE}
        or rate is None
        or quoted is None
        or observed is None
        or quoted > observed
        or observed > now
        or quote_session_start(quoted) is None
    ):
        return None
    return {
        "rate": rate,
        "source": source,
        "quote_at_utc": quoted.isoformat(),
        "observed_at_utc": observed.isoformat(),
    }


def _verified_pairs(payload: Any, *, now: datetime) -> dict[str, dict[str, Any]]:
    if not isinstance(payload, Mapping):
        return {}
    pairs = payload.get("pairs")
    if isinstance(pairs, Mapping):
        return {
            pair: verified
            for pair in _REQUIRED_RATES
            if (verified := _verified_pair(pairs.get(pair), now=now)) is not None
        }
    # Older observations qualify only when each pair has its own source time.
    source = payload.get("source")
    rates = payload.get("rates")
    timestamps = payload.get("quote_timestamps")
    observed = payload.get("observed_at")
    if not isinstance(rates, Mapping) or not isinstance(timestamps, Mapping):
        return {}
    return {
        pair: verified
        for pair in _REQUIRED_RATES
        if (
            verified := _verified_pair(
                {
                    "rate": rates.get(pair),
                    "source": source,
                    "quote_at_utc": timestamps.get(pair),
                    "observed_at_utc": observed,
                },
                now=now,
            )
        )
        is not None
    }


def verified_exchange_rate_pairs(payload: Any, *, observed_at: datetime) -> dict[str, dict[str, Any]]:
    """Original provider evidence, independent of its eligibility today."""
    return _verified_pairs(payload, now=observed_at)


def cash_fx_observation_facts(
    observation: Mapping[str, Any],
    *,
    observed_at_ms: int,
    observation_status: str = "ready",
) -> tuple[FXRateFact, ...]:
    pairs = verified_exchange_rate_pairs(
        observation,
        observed_at=datetime.fromtimestamp(observed_at_ms / 1000, tz=timezone.utc),
    )
    facts = []
    for pair, row in pairs.items():
        effective = int(_strict_timestamp(row["quote_at_utc"]).timestamp() * 1000)
        captured = int(_strict_timestamp(row["observed_at_utc"]).timestamp() * 1000)
        provider = row["source"]
        facts.append(
            FXRateFact(
                fact_id=None,
                base_currency=pair[:3],
                quote_currency="CNY",
                rate=row["rate"],
                rate_kind="spot",
                effective_at_ms=effective,
                observed_at_ms=captured,
                source="realtime_snapshot",
                source_id=f"verified_quote:{provider}:{pair}:{effective}:{captured}",
                quality={"provider_source": provider, "source_timestamp_verified": True},
                raw=dict(row),
            )
        )
    if not facts:
        raise ValueError("FX evidence has no pair with verified source and quote time")
    return tuple(facts)


class RunFXError(RuntimeError):
    pass


def decode_run_fx_snapshot(raw: bytes, *, run_id: str) -> tuple[dict[str, Any], str]:
    try:
        envelope = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise RunFXError("sealed FX artifact is invalid") from exc
    if not isinstance(envelope, dict) or envelope.get("schema_version") != 1 or envelope.get("run_id") != run_id:
        raise RunFXError("sealed FX artifact identity mismatch")
    snapshot = envelope.get("snapshot")
    digest = envelope.get("snapshot_sha256")
    if (
        not isinstance(snapshot, dict)
        or not isinstance(snapshot.get("pairs"), dict)
        or not isinstance(digest, str)
        or canonical_sha256(snapshot) != digest
    ):
        raise RunFXError("sealed FX artifact hash mismatch")
    return snapshot, digest


def project_run_fx_facts(raw: bytes, *, run_id: str) -> dict[str, Any]:
    """Project one sealed native observation with its own historical clock, without I/O."""
    if not isinstance(run_id, str) or not run_id or run_id.strip() != run_id:
        raise RunFXError("sealed FX projection requires an explicit run id")
    snapshot, digest = decode_run_fx_snapshot(raw, run_id=run_id)
    return _project_fx_observation(snapshot, run_id=run_id, digest=digest)


def project_candidate_fx_facts(raw: bytes, *, run_id: str) -> dict[str, Any]:
    """Use an anchored candidate's embedded observation; do not reconstruct a run artifact."""
    if not isinstance(run_id, str) or not run_id or run_id.strip() != run_id:
        raise RunFXError("sealed FX projection requires an explicit run id")
    try:
        candidate = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise RunFXError("sealed candidate FX artifact is invalid") from exc
    if (
        not isinstance(candidate, dict)
        or candidate.get("schema_version") != "opening_candidate_snapshot.v3"
        or candidate.get("run_id") != run_id
        or not isinstance(candidate.get("dependencies"), list)
        or not isinstance(candidate.get("capacity_fx_evidence"), dict)
    ):
        raise RunFXError("sealed candidate FX identity mismatch")
    evidence = candidate["capacity_fx_evidence"]
    snapshot = evidence.get("observation")
    digest = evidence.get("observation_sha256")
    reference = evidence.get("run_fx_snapshot_sha256")
    dependencies = [
        row.get("sha256") for row in candidate["dependencies"] if isinstance(row, dict) and row.get("kind") == "fx"
    ]
    if (
        not isinstance(snapshot, dict)
        or not isinstance(snapshot.get("pairs"), dict)
        or not isinstance(digest, str)
        or canonical_sha256(snapshot) != digest
        or evidence.get("evaluated_at_utc") != snapshot.get("evaluated_at_utc")
        or not isinstance(reference, str)
        or len(reference) != 64
        or any(c not in "0123456789abcdef" for c in reference)
        or dependencies != [reference]
    ):
        raise RunFXError("sealed candidate FX observation binding mismatch")
    return {
        **_project_fx_observation(snapshot, run_id=run_id, digest=digest),
        "source_kind": "candidate_snapshot",
        "run_fx_snapshot_sha256": reference,
        "run_fx_artifact_verification": "reference_only",
    }


def _project_fx_observation(snapshot: dict[str, Any], *, run_id: str, digest: str) -> dict[str, Any]:
    evaluated = _strict_timestamp(snapshot.get("evaluated_at_utc"))
    if evaluated is None:
        raise RunFXError("sealed FX artifact requires a zoned evaluation time")
    pairs = verified_exchange_rate_pairs(snapshot, observed_at=evaluated)
    # Verify at the original microsecond before projecting to the owner's millisecond API.
    facts = (
        cash_fx_observation_facts(
            {"pairs": pairs},
            observed_at_ms=math.ceil(evaluated.timestamp() * 1000),
        )
        if pairs
        else ()
    )
    missing = [pair for pair in _REQUIRED_RATES if pair not in pairs]
    return {
        "schema_version": "om.run-fx-projection.v1",
        "run_id": run_id,
        "snapshot_sha256": digest,
        "evaluated_at_utc": snapshot["evaluated_at_utc"],
        "status": "incomplete" if missing else "complete",
        "missing_pairs": missing,
        "facts": [fact.normalized_payload() for fact in facts],
    }


MAX_BOOKING_RATE_DISTANCE_MS = 24 * 60 * 60 * 1000
MAX_HISTORICAL_CARRY_FORWARD_DISTANCE_MS = 7 * 24 * 60 * 60 * 1000
HISTORICAL_BUSINESS_DAY_FX_CARRY_FORWARD_METHOD = "historical_business_day_fx_carry_forward"
OFFICIAL_CARRY_FORWARD_SOURCES = frozenset({"pbc_central_parity", "manual_correction"})
FOREIGN_METHODS = {
    "booking_fx_snapshot",
    "historical_fx_evidence_backfill",
    HISTORICAL_BUSINESS_DAY_FX_CARRY_FORWARD_METHOD,
}
DAILY_CASH_FX_POLICY = "shanghai_first_observed.v1"
DAILY_CASH_FX_METHOD = "shanghai_daily_fx"
MARKET_CASH_FX_METHOD = "event_time_market_fx"
FOREIGN_METHODS.update({DAILY_CASH_FX_METHOD, MARKET_CASH_FX_METHOD})
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


def select_cash_fx_rate(
    facts: list[FXRateFact] | tuple[FXRateFact, ...],
    *,
    base_currency: str,
    at_ms: int,
) -> EvidenceSelection:
    """Select market evidence with the same session policy as current consumers.

    Explicit official evidence remains an operator-controlled override. Existing
    daily snapshots remain readable; new bookings never use a future quote.
    """
    day = cash_fx_date(at_ms)
    matching = [
        fact
        for fact in facts
        if fact.base_currency == normalize_currency(base_currency) and fact.quote_currency == "CNY"
    ]
    official = [fact for fact in matching if fact.source in OFFICIAL_CARRY_FORWARD_SOURCES]
    same_day = [fact for fact in official if cash_fx_date(fact.effective_at_ms) == day]
    if same_day:
        return select_fx_rate(
            same_day,
            base_currency=base_currency,
            at_ms=max(fact.effective_at_ms for fact in same_day),
            max_staleness_ms=MAX_BOOKING_RATE_DISTANCE_MS,
        )
    carried = [
        fact
        for fact in official
        if fact.quality.get("official") is True
        and isinstance(fact.quality.get("carry_forward_dates"), (list, tuple))
        and day in fact.quality["carry_forward_dates"]
    ]
    if carried:
        return select_fx_rate(
            carried,
            base_currency=base_currency,
            at_ms=int(at_ms),
            max_staleness_ms=MAX_HISTORICAL_CARRY_FORWARD_DISTANCE_MS,
        )
    # Later-effective corrections cannot revoke the quote for an earlier cash event.
    # Official same-day and explicitly carried evidence retain their rules above.
    superseded = {
        fact.supersedes_fact_id for fact in matching if fact.supersedes_fact_id and fact.effective_at_ms <= int(at_ms)
    }
    market = [
        fact
        for fact in matching
        if fact.fact_id not in superseded and market_cash_fx_quality(fact, at_ms=at_ms) in {"fresh", "holiday_carried"}
    ]
    if not market:
        return EvidenceSelection(None, "missing", int(at_ms), reason="no verified quote eligible at cash event time")
    selected = max(
        market,
        key=lambda fact: (
            fact.effective_at_ms,
            fact.quality.get("provider_source", fact.source) == "tencent_quote",
            fact.quality.get("cash_fx_policy") != DAILY_CASH_FX_POLICY,
            -fact.observed_at_ms,
            str(fact.fact_id),
        ),
    )
    return EvidenceSelection(
        selected,
        "selected",
        int(at_ms),
        staleness_ms=int(at_ms) - selected.effective_at_ms,
        reason=MARKET_CASH_FX_METHOD,
    )


def market_cash_fx_quality(fact: FXRateFact, *, at_ms: int) -> str:
    if (
        fact.source not in _MARKET_FX_SOURCES | {"realtime_snapshot", "cache_snapshot"}
        or fact.quality.get("provider_source", fact.source) not in _MARKET_FX_SOURCES
        or fact.quality.get("source_timestamp_verified") is not True
        or fact.effective_at_ms > fact.observed_at_ms
        or not 0 < fact.rate < 1000
    ):
        return "unavailable"
    return quote_quality(
        datetime.fromtimestamp(fact.effective_at_ms / 1000, tz=timezone.utc),
        at=datetime.fromtimestamp(at_ms / 1000, tz=timezone.utc),
    )[0]


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
    amount = quantize_money(to_decimal(native_amount, field_name="native_amount"))
    rate = to_decimal(fx_rate, field_name="fx_rate") if fx_rate is not None else None
    converted = quantize_money(to_decimal(amount_cny, field_name="amount_cny")) if amount_cny is not None else None
    return {
        "cash_fact_id": str(cash_fact_id),
        "native_amount": canonical_decimal_text(
            amount,
            field_name="native_amount",
        ),
        "native_currency": normalize_currency(native_currency),
        "fx_rate": (canonical_decimal_text(rate, field_name="fx_rate") if rate is not None else None),
        "amount_cny": (canonical_decimal_text(converted, field_name="amount_cny") if converted is not None else None),
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
        expected_amount = quantize_money(to_decimal(native_amount, field_name="native_amount"))
        expected_currency = normalize_currency(native_currency)
        amount = quantize_money(to_decimal(conversion.get("native_amount"), field_name="native_amount"))
        amount_cny = quantize_money(to_decimal(conversion.get("amount_cny"), field_name="amount_cny"))
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
        if method != "cny_identity" or rate != Decimal(1) or amount_cny != amount or rate_source != "identity":
            return None, "cny_identity_mismatch"
    else:
        if method not in FOREIGN_METHODS or not rate_source:
            return None, "fx_provenance_invalid"
        if rate is None or rate <= 0:
            return None, "fx_rate_invalid"
        if amount_cny != quantize_money(amount * rate):
            return None, "fx_arithmetic_mismatch"
        rate_timestamp_ms = cash_conversion_timestamp_ms(conversion.get("rate_timestamp"))
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
        if method == MARKET_CASH_FX_METHOD:
            quoted = datetime.fromtimestamp(rate_timestamp_ms / 1000, tz=timezone.utc)
            quality, _ = quote_quality(
                quoted, at=datetime.fromtimestamp(conversion_effective_at_ms / 1000, tz=timezone.utc)
            )
            if (
                rate_source not in _MARKET_FX_SOURCES
                or not str(conversion.get("rate_evidence_fact_id") or "").strip()
                or conversion.get("source_timestamp_verified") is not True
                or conversion.get("fx_calendar") != CALENDAR_EVIDENCE
                or quality not in {"fresh", "holiday_carried"}
                or conversion.get("rate_quote_quality") != quality
                or not rate_timestamp_ms <= int(conversion.get("rate_observed_at_ms") or 0) <= observed_at_ms
                or rate >= 1000
            ):
                return None, "fx_provenance_invalid"
        elif abs(rate_timestamp_ms - conversion_effective_at_ms) > max_rate_distance_ms:
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
    if str(conversion.get("conversion_id") or "").strip() != cash_conversion_id(identity):
        return None, "conversion_id_mismatch"
    return amount_cny, None


def cash_conversion_timestamp_ms(value: Any) -> int | None:
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
    "MARKET_CASH_FX_METHOD",
    "market_cash_fx_quality",
    "DAILY_CASH_FX_METHOD",
    "DAILY_CASH_FX_POLICY",
    "HISTORICAL_BUSINESS_DAY_FX_CARRY_FORWARD_METHOD",
    "MAX_BOOKING_RATE_DISTANCE_MS",
    "MAX_HISTORICAL_CARRY_FORWARD_DISTANCE_MS",
    "OFFICIAL_CARRY_FORWARD_SOURCES",
    "cash_conversion_timestamp_ms",
    "cash_conversion_id",
    "cash_conversion_identity",
    "cash_fx_date",
    "fixed_cash_fx_fact",
    "select_cash_fx_rate",
    "validate_observed_cash_conversion",
]
