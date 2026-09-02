from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Mapping
from urllib.parse import quote, unquote

from domain.domain.ledger.fees import FeeBasis, FeeComponent, FeeFact
from domain.domain.money import (
    MONEY_QUANTUM,
    canonical_decimal_text,
    quantize_money,
    to_decimal,
)
if TYPE_CHECKING:
    from domain.domain.ledger.identity import ContractKey
from domain.domain.option_position_identity import normalize_option_type
from domain.domain.trade_contract_identity import canonical_contract_symbol, normalize_contract_expiration

CAPITAL_DAYS_QUANTUM = Decimal("0.000000000001")
MILLISECONDS_PER_DAY = Decimal("86400000")
_CURRENCY_RE = re.compile(r"^[A-Z][A-Z0-9]{2,9}$")


def normalize_currency(value: Any) -> str:
    currency = str(value or "").strip().upper()
    if not _CURRENCY_RE.fullmatch(currency):
        raise ValueError("currency must be an uppercase ISO-like code")
    return currency


def _encode_part(value: str) -> str:
    return quote(value, safe="-._~")


def _decode_part(value: str) -> str:
    return unquote(value)


@dataclass(frozen=True)
class OptionInstrumentKey:
    symbol: str
    option_type: str
    strike: Decimal
    expiration_ymd: str
    currency: str
    multiplier: Decimal

    def __post_init__(self) -> None:
        symbol = canonical_contract_symbol(self.symbol)
        option_type = normalize_option_type(self.option_type, strict=True)
        strike = to_decimal(self.strike, field_name="strike")
        expiration = normalize_contract_expiration(self.expiration_ymd)
        currency = normalize_currency(self.currency)
        multiplier = to_decimal(self.multiplier, field_name="multiplier")
        if not symbol:
            raise ValueError("symbol is required")
        if strike <= 0:
            raise ValueError("strike must be positive")
        if not expiration:
            raise ValueError("expiration_ymd must be YYYY-MM-DD")
        if multiplier <= 0:
            raise ValueError("multiplier must be positive")
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "option_type", option_type)
        object.__setattr__(self, "strike", strike)
        object.__setattr__(self, "expiration_ymd", expiration)
        object.__setattr__(self, "currency", currency)
        object.__setattr__(self, "multiplier", multiplier)

    @property
    def instrument_key(self) -> str:
        parts = (
            "option:v1",
            _encode_part(self.symbol),
            self.option_type,
            canonical_decimal_text(self.strike, field_name="strike"),
            self.expiration_ymd,
            self.currency,
            canonical_decimal_text(self.multiplier, field_name="multiplier"),
        )
        return "|".join(parts)

    @classmethod
    def decode(cls, value: str) -> "OptionInstrumentKey":
        parts = str(value or "").split("|")
        if len(parts) != 7 or parts[0] != "option:v1":
            raise ValueError("invalid option instrument key version or field count")
        decoded = cls(
            symbol=_decode_part(parts[1]),
            option_type=parts[2],
            strike=to_decimal(parts[3], field_name="strike"),
            expiration_ymd=parts[4],
            currency=parts[5],
            multiplier=to_decimal(parts[6], field_name="multiplier"),
        )
        if decoded.instrument_key != value:
            raise ValueError("option instrument key is not canonically encoded")
        return decoded

    @classmethod
    def from_contract_key(
        cls,
        contract_key: ContractKey,
        *,
        currency: Any,
        multiplier: Any,
    ) -> "OptionInstrumentKey":
        return cls(
            symbol=contract_key.underlying_symbol,
            option_type=contract_key.option_type,
            strike=Decimal(str(contract_key.strike)),
            expiration_ymd=contract_key.expiration_ymd,
            currency=normalize_currency(currency),
            multiplier=to_decimal(multiplier, field_name="multiplier"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "option",
            "instrument_key": self.instrument_key,
            "symbol": self.symbol,
            "option_type": self.option_type,
            "strike": canonical_decimal_text(self.strike, field_name="strike"),
            "expiration_ymd": self.expiration_ymd,
            "currency": self.currency,
            "multiplier": canonical_decimal_text(self.multiplier, field_name="multiplier"),
        }


@dataclass(frozen=True)
class StockInstrumentKey:
    symbol: str
    currency: str

    def __post_init__(self) -> None:
        symbol = canonical_contract_symbol(self.symbol)
        if not symbol:
            raise ValueError("symbol is required")
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "currency", normalize_currency(self.currency))

    @property
    def instrument_key(self) -> str:
        return f"stock:v1|{_encode_part(self.symbol)}|{self.currency}"

    @classmethod
    def decode(cls, value: str) -> "StockInstrumentKey":
        parts = str(value or "").split("|")
        if len(parts) != 3 or parts[0] != "stock:v1":
            raise ValueError("invalid stock instrument key version or field count")
        decoded = cls(symbol=_decode_part(parts[1]), currency=parts[2])
        if decoded.instrument_key != value:
            raise ValueError("stock instrument key is not canonically encoded")
        return decoded

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "stock",
            "instrument_key": self.instrument_key,
            "symbol": self.symbol,
            "currency": self.currency,
        }


class MetricStatus(str, Enum):
    OBSERVED = "observed"
    PARTIAL = "partial"
    NOT_APPLICABLE = "not_applicable"




@dataclass(frozen=True)
class StrategyAttribution:
    strategy: str
    leg_role: str
    strategy_group_id: str
    lifecycle_id: str
    expiry_structure: str | None = None

    def __post_init__(self) -> None:
        for field_name in ("strategy", "leg_role", "strategy_group_id", "lifecycle_id"):
            value = _required_text(getattr(self, field_name), field_name=field_name)
            object.__setattr__(self, field_name, value)
        object.__setattr__(self, "expiry_structure", _optional_text(self.expiry_structure))

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "leg_role": self.leg_role,
            "strategy_group_id": self.strategy_group_id,
            "lifecycle_id": self.lifecycle_id,
            "expiry_structure": self.expiry_structure,
        }


__all__ = [
    "CAPITAL_DAYS_QUANTUM",
    "MILLISECONDS_PER_DAY",
    "FeeBasis",
    "FeeComponent",
    "FeeFact",
    "MONEY_QUANTUM",
    "MetricStatus",
    "EvidenceEnvelope",
    "EvidenceSelection",
    "FXRateFact",
    "OptionInstrumentKey",
    "OptionValuationPosition",
    "StockInstrumentKey",
    "StrategyAttribution",
    "ValuationMarkFact",
    "canonical_decimal_text",
    "normalize_currency",
    "parse_evidence_envelope",
    "quantize_money",
    "select_fx_rate",
    "select_valuation_mark",
    "to_decimal",
    "validate_evidence_facts",
]


_EVIDENCE_SOURCE_PRIORITY = {
    "cache_snapshot": 1,
    "realtime_snapshot": 2,
    "broker_snapshot": 3,
    "official_close": 4,
    "manual_correction": 5,
}
_EVIDENCE_SCHEMA_VERSION = "option_performance_evidence.v1"
_EVIDENCE_MAX_STALENESS_MS = 7 * 86_400_000


def _mapping(value: Any, *, field_name: str) -> Mapping[str, Any]:
    if value in (None, ""):
        return MappingProxyType({})
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} must be an object")
    return MappingProxyType(dict(value))


def _positive_ms(value: Any, *, field_name: str) -> int:
    try:
        out = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a positive integer") from exc
    if out <= 0:
        raise ValueError(f"{field_name} must be a positive integer")
    return out


def _revision(value: Any) -> int:
    try:
        out = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("revision must be a positive integer") from exc
    if out <= 0:
        raise ValueError("revision must be a positive integer")
    return out


def _optional_text(value: Any) -> str | None:
    raw = str(value or "").strip()
    return raw or None


def _required_text(value: Any, *, field_name: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError(f"{field_name} is required")
    return raw


def _canonical_json(value: Mapping[str, Any]) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _generated_fact_id(prefix: str, payload: Mapping[str, Any]) -> str:
    import hashlib

    digest = hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()[:32]
    return f"{prefix}_{digest}"


@dataclass(frozen=True)
class ValuationMarkFact:
    fact_id: str | None
    instrument: OptionInstrumentKey | StockInstrumentKey
    price: Decimal
    mark_kind: str
    effective_at_ms: int
    observed_at_ms: int
    source: str
    source_id: str
    revision: int = 1
    supersedes_fact_id: str | None = None
    quality: Mapping[str, Any] = field(default_factory=dict)
    raw: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        price = to_decimal(self.price, field_name="price")
        if price < 0:
            raise ValueError("price cannot be negative")
        mark_kind = _required_text(self.mark_kind, field_name="mark_kind")
        source = _required_text(self.source, field_name="source")
        source_id = _required_text(self.source_id, field_name="source_id")
        effective_at_ms = _positive_ms(self.effective_at_ms, field_name="effective_at_ms")
        observed_at_ms = _positive_ms(self.observed_at_ms, field_name="observed_at_ms")
        revision = _revision(self.revision)
        supersedes = _optional_text(self.supersedes_fact_id)
        quality = _mapping(self.quality, field_name="quality")
        raw = _mapping(self.raw, field_name="raw")
        object.__setattr__(self, "price", price)
        object.__setattr__(self, "mark_kind", mark_kind)
        object.__setattr__(self, "effective_at_ms", effective_at_ms)
        object.__setattr__(self, "observed_at_ms", observed_at_ms)
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "source_id", source_id)
        object.__setattr__(self, "revision", revision)
        object.__setattr__(self, "supersedes_fact_id", supersedes)
        object.__setattr__(self, "quality", quality)
        object.__setattr__(self, "raw", raw)
        payload = self.normalized_payload(include_fact_id=False)
        object.__setattr__(self, "fact_id", _optional_text(self.fact_id) or _generated_fact_id("mark", payload))

    @property
    def instrument_key(self) -> str:
        return self.instrument.instrument_key

    @property
    def instrument_type(self) -> str:
        return "option" if isinstance(self.instrument, OptionInstrumentKey) else "stock"

    @property
    def identity(self) -> tuple[str, str]:
        return ("valuation_mark", self.instrument_key)

    @property
    def source_identity(self) -> tuple[str, str, int]:
        return (self.source, self.source_id, self.revision)

    def normalized_payload(self, *, include_fact_id: bool = True) -> dict[str, Any]:
        out = {
            "instrument": self.instrument.to_dict(),
            "price": canonical_decimal_text(self.price, field_name="price"),
            "mark_kind": self.mark_kind,
            "effective_at_ms": self.effective_at_ms,
            "observed_at_ms": self.observed_at_ms,
            "source": self.source,
            "source_id": self.source_id,
            "revision": self.revision,
            "supersedes_fact_id": self.supersedes_fact_id,
            "quality": dict(self.quality),
            "raw": dict(self.raw),
        }
        if include_fact_id:
            out["fact_id"] = self.fact_id
        return out


@dataclass(frozen=True)
class FXRateFact:
    fact_id: str | None
    base_currency: str
    quote_currency: str
    rate: Decimal
    rate_kind: str
    effective_at_ms: int
    observed_at_ms: int
    source: str
    source_id: str
    revision: int = 1
    supersedes_fact_id: str | None = None
    quality: Mapping[str, Any] = field(default_factory=dict)
    raw: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        base = normalize_currency(self.base_currency)
        quote = normalize_currency(self.quote_currency)
        if base == quote:
            raise ValueError("FX base_currency and quote_currency must differ")
        rate = to_decimal(self.rate, field_name="rate")
        if rate <= 0:
            raise ValueError("rate must be positive")
        rate_kind = _required_text(self.rate_kind, field_name="rate_kind")
        source = _required_text(self.source, field_name="source")
        source_id = _required_text(self.source_id, field_name="source_id")
        effective_at_ms = _positive_ms(self.effective_at_ms, field_name="effective_at_ms")
        observed_at_ms = _positive_ms(self.observed_at_ms, field_name="observed_at_ms")
        revision = _revision(self.revision)
        supersedes = _optional_text(self.supersedes_fact_id)
        quality = _mapping(self.quality, field_name="quality")
        raw = _mapping(self.raw, field_name="raw")
        object.__setattr__(self, "base_currency", base)
        object.__setattr__(self, "quote_currency", quote)
        object.__setattr__(self, "rate", rate)
        object.__setattr__(self, "rate_kind", rate_kind)
        object.__setattr__(self, "effective_at_ms", effective_at_ms)
        object.__setattr__(self, "observed_at_ms", observed_at_ms)
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "source_id", source_id)
        object.__setattr__(self, "revision", revision)
        object.__setattr__(self, "supersedes_fact_id", supersedes)
        object.__setattr__(self, "quality", quality)
        object.__setattr__(self, "raw", raw)
        payload = self.normalized_payload(include_fact_id=False)
        object.__setattr__(self, "fact_id", _optional_text(self.fact_id) or _generated_fact_id("fx", payload))

    @property
    def identity(self) -> tuple[str, str, str]:
        return ("fx_rate", self.base_currency, self.quote_currency)

    @property
    def source_identity(self) -> tuple[str, str, int]:
        return (self.source, self.source_id, self.revision)

    def normalized_payload(self, *, include_fact_id: bool = True) -> dict[str, Any]:
        out = {
            "base_currency": self.base_currency,
            "quote_currency": self.quote_currency,
            "rate": canonical_decimal_text(self.rate, field_name="rate"),
            "rate_kind": self.rate_kind,
            "effective_at_ms": self.effective_at_ms,
            "observed_at_ms": self.observed_at_ms,
            "source": self.source,
            "source_id": self.source_id,
            "revision": self.revision,
            "supersedes_fact_id": self.supersedes_fact_id,
            "quality": dict(self.quality),
            "raw": dict(self.raw),
        }
        if include_fact_id:
            out["fact_id"] = self.fact_id
        return out


@dataclass(frozen=True)
class EvidenceEnvelope:
    valuation_marks: tuple[ValuationMarkFact, ...] = ()
    fx_rates: tuple[FXRateFact, ...] = ()
    schema_version: str = _EVIDENCE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != _EVIDENCE_SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {_EVIDENCE_SCHEMA_VERSION}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "valuation_marks": [item.normalized_payload() for item in self.valuation_marks],
            "fx_rates": [item.normalized_payload() for item in self.fx_rates],
        }


@dataclass(frozen=True)
class EvidenceSelection:
    fact: ValuationMarkFact | FXRateFact | None
    status: str
    at_ms: int
    staleness_ms: int | None = None
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        fact = self.fact
        return {
            "status": self.status,
            "fact_id": fact.fact_id if fact is not None else None,
            "effective_at_ms": fact.effective_at_ms if fact is not None else None,
            "observed_at_ms": fact.observed_at_ms if fact is not None else None,
            "source": fact.source if fact is not None else None,
            "staleness_days": None if self.staleness_ms is None else self.staleness_ms / 86_400_000,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class OptionValuationPosition:
    lot_id: str
    account: str
    broker: str
    instrument: OptionInstrumentKey
    position_side: str
    contracts_open: int
    open_price: Decimal
    open_fee_remaining: Decimal | None
    open_fee_quality: str
    opened_at_ms: int
    market_code: str | None = None
    attribution: StrategyAttribution | None = None
    attribution_issues: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        lot_id = _required_text(self.lot_id, field_name="lot_id")
        account = _required_text(self.account, field_name="account").lower()
        broker = _required_text(self.broker, field_name="broker")
        side = str(self.position_side or "").strip().lower()
        if side not in {"short", "long"}:
            raise ValueError("position_side must be short or long")
        contracts = int(self.contracts_open)
        if contracts <= 0:
            raise ValueError("contracts_open must be positive")
        open_price = to_decimal(self.open_price, field_name="open_price")
        if open_price < 0:
            raise ValueError("open_price cannot be negative")
        fee = None if self.open_fee_remaining is None else quantize_money(self.open_fee_remaining)
        if fee is not None and fee < 0:
            raise ValueError("open_fee_remaining cannot be negative")
        quality = str(self.open_fee_quality or "").strip().lower()
        if quality not in {item.value for item in FeeBasis}:
            raise ValueError("open_fee_quality must be actual, estimated, or missing")
        object.__setattr__(self, "lot_id", lot_id)
        object.__setattr__(self, "account", account)
        object.__setattr__(self, "broker", broker)
        object.__setattr__(self, "position_side", side)
        object.__setattr__(self, "contracts_open", contracts)
        object.__setattr__(self, "open_price", open_price)
        object.__setattr__(self, "open_fee_remaining", fee)
        object.__setattr__(self, "open_fee_quality", quality)
        object.__setattr__(self, "opened_at_ms", _positive_ms(self.opened_at_ms, field_name="opened_at_ms"))
        object.__setattr__(self, "market_code", _optional_text(self.market_code))
        object.__setattr__(
            self,
            "attribution_issues",
            tuple(sorted({str(item) for item in self.attribution_issues if str(item)})),
        )

    @property
    def symbol(self) -> str:
        return self.instrument.symbol

    @property
    def currency(self) -> str:
        return self.instrument.currency


def _instrument_from_payload(value: Any) -> OptionInstrumentKey | StockInstrumentKey:
    if not isinstance(value, Mapping):
        raise ValueError("instrument must be an object")
    kind = str(value.get("type") or "").strip().lower()
    if kind == "option":
        instrument = OptionInstrumentKey(
            symbol=value.get("symbol"),
            option_type=value.get("option_type"),
            strike=value.get("strike"),
            expiration_ymd=value.get("expiration_ymd"),
            currency=value.get("currency"),
            multiplier=value.get("multiplier"),
        )
    elif kind == "stock":
        instrument = StockInstrumentKey(symbol=value.get("symbol"), currency=value.get("currency"))
    else:
        raise ValueError("instrument.type must be option or stock")
    supplied_key = _optional_text(value.get("instrument_key"))
    if supplied_key and supplied_key != instrument.instrument_key:
        raise ValueError("instrument structured fields do not match instrument_key")
    return instrument


def parse_evidence_envelope(value: Any) -> EvidenceEnvelope:
    if not isinstance(value, Mapping):
        raise ValueError("evidence envelope must be an object")
    schema_version = str(value.get("schema_version") or "").strip()
    if schema_version != _EVIDENCE_SCHEMA_VERSION:
        raise ValueError(f"schema_version must be {_EVIDENCE_SCHEMA_VERSION}")
    raw_marks = value.get("valuation_marks") or []
    raw_rates = value.get("fx_rates") or []
    if not isinstance(raw_marks, list) or not isinstance(raw_rates, list):
        raise ValueError("valuation_marks and fx_rates must be arrays")
    marks: list[ValuationMarkFact] = []
    rates: list[FXRateFact] = []
    for index, item in enumerate(raw_marks):
        if not isinstance(item, Mapping):
            raise ValueError(f"valuation_marks[{index}] must be an object")
        marks.append(
            ValuationMarkFact(
                fact_id=item.get("fact_id"),
                instrument=_instrument_from_payload(item.get("instrument")),
                price=item.get("price"),
                mark_kind=item.get("mark_kind"),
                effective_at_ms=item.get("effective_at_ms"),
                observed_at_ms=item.get("observed_at_ms"),
                source=item.get("source"),
                source_id=item.get("source_id"),
                revision=item.get("revision", 1),
                supersedes_fact_id=item.get("supersedes_fact_id"),
                quality=item.get("quality") or {},
                raw=item.get("raw") or {},
            )
        )
    for index, item in enumerate(raw_rates):
        if not isinstance(item, Mapping):
            raise ValueError(f"fx_rates[{index}] must be an object")
        rates.append(
            FXRateFact(
                fact_id=item.get("fact_id"),
                base_currency=item.get("base_currency"),
                quote_currency=item.get("quote_currency"),
                rate=item.get("rate"),
                rate_kind=item.get("rate_kind"),
                effective_at_ms=item.get("effective_at_ms"),
                observed_at_ms=item.get("observed_at_ms"),
                source=item.get("source"),
                source_id=item.get("source_id"),
                revision=item.get("revision", 1),
                supersedes_fact_id=item.get("supersedes_fact_id"),
                quality=item.get("quality") or {},
                raw=item.get("raw") or {},
            )
        )
    envelope = EvidenceEnvelope(valuation_marks=tuple(marks), fx_rates=tuple(rates))
    validate_evidence_facts(envelope.valuation_marks, envelope.fx_rates)
    return envelope


def validate_evidence_facts(
    valuation_marks: tuple[ValuationMarkFact, ...] | list[ValuationMarkFact],
    fx_rates: tuple[FXRateFact, ...] | list[FXRateFact],
    *,
    existing_marks: tuple[ValuationMarkFact, ...] | list[ValuationMarkFact] = (),
    existing_rates: tuple[FXRateFact, ...] | list[FXRateFact] = (),
) -> None:
    _validate_evidence_kind([*existing_marks], [*valuation_marks])
    _validate_evidence_kind([*existing_rates], [*fx_rates])


def _validate_evidence_kind(existing: list[Any], incoming: list[Any]) -> None:
    by_id: dict[str, Any] = {}
    by_source: dict[tuple[str, str, int], Any] = {}
    for item in existing:
        fact_id = str(item.fact_id)
        same_id = by_id.get(fact_id)
        if same_id is not None and same_id.normalized_payload() != item.normalized_payload():
            raise ValueError(f"fact_id conflict: {fact_id}")
        same_source = by_source.get(item.source_identity)
        if same_source is not None and same_source.normalized_payload() != item.normalized_payload():
            raise ValueError(f"source identity conflict: {item.source_identity}")
        by_id[fact_id] = item
        by_source[item.source_identity] = item
    _validate_correction_graph(by_id)

    for item in incoming:
        fact_id = str(item.fact_id)
        same_id = by_id.get(fact_id)
        if same_id is not None and same_id.normalized_payload() != item.normalized_payload():
            raise ValueError(f"fact_id conflict: {fact_id}")
        same_source = by_source.get(item.source_identity)
        if same_source is not None and same_source.normalized_payload() != item.normalized_payload():
            raise ValueError(f"source identity conflict: {item.source_identity}")
        target_id = item.supersedes_fact_id
        if target_id:
            if target_id == fact_id:
                raise ValueError("evidence fact cannot supersede itself")
            target = by_id.get(target_id)
            if target is None:
                raise ValueError(f"supersedes_fact_id must reference an existing or earlier fact: {target_id}")
            if target.identity != item.identity:
                raise ValueError("superseding evidence must preserve exact identity")
        by_id[fact_id] = item
        by_source[item.source_identity] = item
        _validate_correction_graph(by_id)


def _validate_correction_graph(by_id: Mapping[str, Any]) -> None:
    for item in by_id.values():
        cursor = item
        seen: set[str] = set()
        while cursor.supersedes_fact_id:
            cursor_id = str(cursor.fact_id)
            if cursor_id in seen:
                raise ValueError("evidence correction cycle detected")
            seen.add(cursor_id)
            target = by_id.get(str(cursor.supersedes_fact_id))
            if target is None:
                raise ValueError(f"supersedes_fact_id does not exist: {cursor.supersedes_fact_id}")
            if target.identity != cursor.identity:
                raise ValueError("superseding evidence must preserve exact identity")
            cursor = target


def _select_evidence(facts: list[Any], *, at_ms: int, max_staleness_ms: int) -> EvidenceSelection:
    instant = int(at_ms)
    eligible = [item for item in facts if item.effective_at_ms <= instant]
    superseded = {str(item.supersedes_fact_id) for item in eligible if item.supersedes_fact_id}
    candidates = [item for item in eligible if item.fact_id not in superseded]
    if not candidates:
        return EvidenceSelection(None, "missing", instant, reason="no evidence at or before requested instant")
    selected = max(
        candidates,
        key=lambda item: (
            item.effective_at_ms,
            _EVIDENCE_SOURCE_PRIORITY.get(item.source, 0),
            item.revision,
            str(item.fact_id),
        ),
    )
    staleness_ms = max(0, instant - selected.effective_at_ms)
    if staleness_ms > int(max_staleness_ms):
        return EvidenceSelection(
            None, "stale", instant, staleness_ms=staleness_ms, reason="evidence exceeds maximum staleness"
        )
    return EvidenceSelection(selected, "selected", instant, staleness_ms=staleness_ms)


def select_valuation_mark(
    facts: tuple[ValuationMarkFact, ...] | list[ValuationMarkFact],
    *,
    instrument_key: str,
    at_ms: int,
    max_staleness_ms: int = _EVIDENCE_MAX_STALENESS_MS,
) -> EvidenceSelection:
    matches = [item for item in facts if item.instrument_key == instrument_key]
    return _select_evidence(matches, at_ms=at_ms, max_staleness_ms=max_staleness_ms)


def select_fx_rate(
    facts: tuple[FXRateFact, ...] | list[FXRateFact],
    *,
    base_currency: str,
    quote_currency: str = "CNY",
    at_ms: int,
    max_staleness_ms: int = _EVIDENCE_MAX_STALENESS_MS,
) -> EvidenceSelection:
    base = normalize_currency(base_currency)
    quote = normalize_currency(quote_currency)
    matches = [item for item in facts if item.base_currency == base and item.quote_currency == quote]
    return _select_evidence(matches, at_ms=at_ms, max_staleness_ms=max_staleness_ms)
