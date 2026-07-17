from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping
from urllib.parse import quote, unquote

from domain.domain.ledger.identity import ContractKey
from domain.domain.option_position_identity import normalize_option_type
from domain.domain.trade_contract_identity import canonical_contract_symbol, normalize_contract_expiration

MONEY_QUANTUM = Decimal("0.000001")
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


__all__ = [
    "DecimalAmountEnvelope",
    "FeeBasis",
    "FeeComponent",
    "FeeFact",
    "MONEY_QUANTUM",
    "MetricQuality",
    "MetricStatus",
    "OptionInstrumentKey",
    "StockInstrumentKey",
    "canonical_decimal_text",
    "normalize_currency",
    "quantize_money",
    "to_decimal",
]
