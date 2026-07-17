from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from enum import Enum
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Mapping
from urllib.parse import quote, unquote

if TYPE_CHECKING:
    from domain.domain.ledger.identity import ContractKey
from domain.domain.option_position_identity import normalize_option_type
from domain.domain.trade_contract_identity import canonical_contract_symbol, normalize_contract_expiration

MONEY_QUANTUM = Decimal("0.000001")
CAPITAL_DAYS_QUANTUM = Decimal("0.000000000001")
MILLISECONDS_PER_DAY = Decimal("86400000")
_CURRENCY_RE = re.compile(r"^[A-Z][A-Z0-9]{2,9}$")


def to_decimal(value: Any, *, field_name: str = "value") -> Decimal:
    if isinstance(value, bool) or value in (None, ""):
        raise ValueError(f"{field_name} is required")
    try:
        out = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field_name} must be a finite decimal") from exc
    if not out.is_finite():
        raise ValueError(f"{field_name} must be a finite decimal")
    return out


def canonical_decimal_text(value: Any, *, field_name: str = "value") -> str:
    decimal_value = to_decimal(value, field_name=field_name)
    if decimal_value == 0:
        return "0"
    rendered = format(decimal_value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered


def quantize_money(value: Any) -> Decimal:
    return to_decimal(value, field_name="amount").quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)


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
    NOT_OBSERVED = "not_observed"
    NOT_APPLICABLE = "not_applicable"


@dataclass(frozen=True)
class MetricQuality:
    status: MetricStatus
    missing: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    evidence_fact_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", MetricStatus(self.status))
        object.__setattr__(self, "missing", tuple(sorted({str(item) for item in self.missing if str(item)})))
        object.__setattr__(self, "warnings", tuple(str(item) for item in self.warnings if str(item)))
        object.__setattr__(
            self,
            "evidence_fact_ids",
            tuple(dict.fromkeys(str(item) for item in self.evidence_fact_ids if str(item))),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "missing": list(self.missing),
            "warnings": list(self.warnings),
            "evidence_fact_ids": list(self.evidence_fact_ids),
        }


@dataclass(frozen=True)
class DecimalAmountEnvelope:
    by_currency: Mapping[str, Decimal] = field(default_factory=dict)
    cny: Decimal | None = None
    quality: MetricQuality = field(default_factory=lambda: MetricQuality(MetricStatus.NOT_OBSERVED))
    fx_fact_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        normalized: dict[str, Decimal] = {}
        for currency, amount in self.by_currency.items():
            canonical_currency = normalize_currency(currency)
            if canonical_currency in normalized:
                raise ValueError(f"duplicate canonical currency: {canonical_currency}")
            normalized[canonical_currency] = quantize_money(amount)
        object.__setattr__(self, "by_currency", MappingProxyType(dict(sorted(normalized.items()))))
        object.__setattr__(self, "cny", None if self.cny is None else quantize_money(self.cny))
        object.__setattr__(self, "fx_fact_ids", tuple(dict.fromkeys(str(x) for x in self.fx_fact_ids if str(x))))

    def to_dict(self) -> dict[str, Any]:
        return {
            "by_currency": {currency: float(amount) for currency, amount in self.by_currency.items()},
            "cny": None if self.cny is None else float(self.cny),
            "status": self.quality.status.value,
            "missing": list(self.quality.missing),
            "fx_fact_ids": list(self.fx_fact_ids),
        }


class FeeBasis(str, Enum):
    ACTUAL = "actual"
    ESTIMATED = "estimated"
    MISSING = "missing"


class FeeComponent(str, Enum):
    OPTION_OPEN = "option_open"
    OPTION_CLOSE = "option_close"
    ASSIGNMENT_OPTION = "assignment_option"
    STOCK_SETTLEMENT = "stock_settlement"
    STOCK_SALE = "stock_sale"


@dataclass(frozen=True)
class FeeFact:
    amount: Decimal | None
    basis: FeeBasis
    component: FeeComponent
    source_event_id: str
    source: str | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        basis = FeeBasis(self.basis)
        component = FeeComponent(self.component)
        source_event_id = str(self.source_event_id or "").strip()
        if not source_event_id:
            raise ValueError("source_event_id is required")
        if basis == FeeBasis.MISSING:
            if self.amount is not None:
                raise ValueError("missing fee must not have an amount")
            amount = None
        else:
            amount = quantize_money(self.amount)
            if amount < 0:
                raise ValueError("fee amount cannot be negative")
        object.__setattr__(self, "basis", basis)
        object.__setattr__(self, "component", component)
        object.__setattr__(self, "source_event_id", source_event_id)
        object.__setattr__(self, "amount", amount)
        object.__setattr__(self, "source", str(self.source).strip() if self.source not in (None, "") else None)
        object.__setattr__(self, "reason", str(self.reason).strip() if self.reason not in (None, "") else None)

    @property
    def is_complete(self) -> bool:
        return self.basis in {FeeBasis.ACTUAL, FeeBasis.ESTIMATED}

    def to_dict(self) -> dict[str, Any]:
        return {
            "amount": None if self.amount is None else float(self.amount),
            "basis": self.basis.value,
            "component": self.component.value,
            "source": self.source,
            "reason": self.reason,
            "source_event_id": self.source_event_id,
        }


@dataclass(frozen=True)
class CapitalExposureSegment:
    account: str
    broker: str
    symbol: str
    currency: str
    exposure_kind: str
    source_id: str
    start_at_ms: int
    end_at_ms: int
    notional: Decimal
    quantity: Decimal
    incremental: bool = True

    def __post_init__(self) -> None:
        account = str(self.account or "").strip().lower()
        broker = str(self.broker or "").strip().lower()
        symbol = str(self.symbol or "").strip().upper()
        exposure_kind = str(self.exposure_kind or "").strip()
        source_id = str(self.source_id or "").strip()
        start_at_ms = int(self.start_at_ms)
        end_at_ms = int(self.end_at_ms)
        notional = to_decimal(self.notional, field_name="notional")
        quantity = to_decimal(self.quantity, field_name="quantity")
        if not account or not broker or not symbol or not exposure_kind or not source_id:
            raise ValueError("capital exposure requires account, broker, symbol, kind, and source_id")
        if start_at_ms <= 0 or end_at_ms < start_at_ms:
            raise ValueError("capital exposure interval is invalid")
        if notional < 0 or quantity < 0:
            raise ValueError("capital exposure notional and quantity cannot be negative")
        if not self.incremental and notional != 0:
            raise ValueError("zero-incremental capital exposure must have zero notional")
        object.__setattr__(self, "account", account)
        object.__setattr__(self, "broker", broker)
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "currency", normalize_currency(self.currency))
        object.__setattr__(self, "exposure_kind", exposure_kind)
        object.__setattr__(self, "source_id", source_id)
        object.__setattr__(self, "start_at_ms", start_at_ms)
        object.__setattr__(self, "end_at_ms", end_at_ms)
        object.__setattr__(self, "notional", notional)
        object.__setattr__(self, "quantity", quantity)

    def overlap_ms(self, *, period_start_at_ms: int, period_end_exclusive_at_ms: int) -> int:
        return max(
            0,
            min(self.end_at_ms, int(period_end_exclusive_at_ms))
            - max(self.start_at_ms, int(period_start_at_ms)),
        )

    def capital_days(self, *, period_start_at_ms: int, period_end_exclusive_at_ms: int) -> Decimal:
        overlap_ms = self.overlap_ms(
            period_start_at_ms=period_start_at_ms,
            period_end_exclusive_at_ms=period_end_exclusive_at_ms,
        )
        if overlap_ms <= 0 or not self.incremental:
            return Decimal(0)
        return self.notional * Decimal(overlap_ms) / MILLISECONDS_PER_DAY

    def to_dict(self, *, period_start_at_ms: int, period_end_exclusive_at_ms: int) -> dict[str, Any]:
        overlap_ms = self.overlap_ms(
            period_start_at_ms=period_start_at_ms,
            period_end_exclusive_at_ms=period_end_exclusive_at_ms,
        )
        return {
            "account": self.account,
            "broker": self.broker,
            "symbol": self.symbol,
            "currency": self.currency,
            "exposure_kind": self.exposure_kind,
            "source_id": self.source_id,
            "start_at_ms": self.start_at_ms,
            "end_at_ms": self.end_at_ms,
            "notional": float(self.notional),
            "quantity": float(self.quantity),
            "incremental": self.incremental,
            "overlap_ms": overlap_ms,
            "capital_days": float(
                self.capital_days(
                    period_start_at_ms=period_start_at_ms,
                    period_end_exclusive_at_ms=period_end_exclusive_at_ms,
                )
            ),
        }


__all__ = [
    "CAPITAL_DAYS_QUANTUM",
    "MILLISECONDS_PER_DAY",
    "CapitalExposureSegment",
    "DecimalAmountEnvelope",
    "FeeBasis",
    "FeeComponent",
    "FeeFact",
    "MONEY_QUANTUM",
    "MetricQuality",
    "MetricStatus",
    "EvidenceEnvelope",
    "EvidenceSelection",
    "FXRateFact",
    "OptionInstrumentKey",
    "OptionValuationPosition",
    "StockInstrumentKey",
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
