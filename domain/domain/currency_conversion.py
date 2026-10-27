"""Pure legacy FX arithmetic and normalized-row supplements; no rate fetching."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from domain.domain.option_position_identity import normalize_currency
from domain.domain.symbol_identity import symbol_currency
from domain.domain.short_vol_assessment import _float


@dataclass(frozen=True)
class ExchangeRates:
    usd_per_cny: float | None = None
    cny_per_hkd: float | None = None


@dataclass(frozen=True)
class CurrencyConverter:
    """Convert between base CNY and option native currencies (USD/HKD)."""

    rates: ExchangeRates

    def cny_to_usd(self, cny: float) -> float | None:
        r = self.rates.usd_per_cny
        if r is None or r <= 0:
            return None
        return float(cny) * float(r)

    def usd_to_cny(self, usd: float) -> float | None:
        r = self.rates.usd_per_cny
        if r is None or r <= 0:
            return None
        return float(usd) / float(r)

    def cny_to_hkd(self, cny: float) -> float | None:
        cny_per_hkd_exchange_rate = self.rates.cny_per_hkd
        if cny_per_hkd_exchange_rate is None or cny_per_hkd_exchange_rate <= 0:
            return None
        return float(cny) / float(cny_per_hkd_exchange_rate)

    def hkd_to_cny(self, hkd: float) -> float | None:
        cny_per_hkd_exchange_rate = self.rates.cny_per_hkd
        if cny_per_hkd_exchange_rate is None or cny_per_hkd_exchange_rate <= 0:
            return None
        return float(hkd) * float(cny_per_hkd_exchange_rate)

    def cny_to_native(self, cny: float, *, native_ccy: str) -> float | None:
        c = str(native_ccy or '').upper()
        if c == 'USD':
            return self.cny_to_usd(cny)
        if c == 'HKD':
            return self.cny_to_hkd(cny)
        return None

    def native_to_cny(self, amount: float, *, native_ccy: str) -> float | None:
        c = str(native_ccy or '').upper()
        if c == 'USD':
            return self.usd_to_cny(amount)
        if c == 'HKD':
            return self.hkd_to_cny(amount)
        if c == 'CNY':
            return float(amount)
        return None

    def convert(self, amount: float, *, from_ccy: str, to_ccy: str) -> float | None:
        source = str(from_ccy or '').strip().upper()
        target = str(to_ccy or '').strip().upper()
        if source == 'RMB':
            source = 'CNY'
        if target == 'RMB':
            target = 'CNY'
        if not source or not target:
            return None
        if source == target:
            return float(amount)
        amount_cny = self.native_to_cny(float(amount), native_ccy=source)
        if amount_cny is None:
            return None
        if target == 'CNY':
            return amount_cny
        return self.cny_to_native(amount_cny, native_ccy=target)


def amount_to_cny(
    value: Any,
    ccy: Any,
    *,
    exchange_rate_converter: CurrencyConverter,
) -> float | None:
    amount = _float(value)
    if amount is None:
        return None
    currency = normalize_currency(ccy)
    if currency in {"CNY", "RMB"}:
        return float(amount)
    if not currency:
        return None
    converted = exchange_rate_converter.native_to_cny(float(amount), native_ccy=currency)
    return float(converted) if converted is not None else None


def enrich_short_vol_contract_cny_fields(
    row: dict[str, Any],
    *,
    exchange_rate_converter: CurrencyConverter,
) -> dict[str, float]:
    ccy = normalize_currency(row.get("currency") or row.get("option_ccy")) or symbol_currency(row.get("symbol"))
    fields: dict[str, float] = {}
    if _float(row.get("net_income_cny")) is None:
        net_income = _first_float(row, "net_income", "net_credit")
        net_income_cny = amount_to_cny(net_income, ccy, exchange_rate_converter=exchange_rate_converter)
        if net_income_cny is not None:
            fields["net_income_cny"] = net_income_cny
    if _float(row.get("option_contract_point_value_cny")) is None:
        multiplier = _first_float(row, "multiplier", "option_contract_multiplier", "option_contract_size")
        point_value_cny = amount_to_cny(multiplier, ccy, exchange_rate_converter=exchange_rate_converter)
        if point_value_cny is not None:
            fields["option_contract_point_value_cny"] = point_value_cny
    return fields


def _first_float(row: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = _float(row.get(key))
        if value is not None:
            return value
    return None


def covered_notional_cny(
    row: dict[str, Any],
    *,
    exchange_rate_converter: CurrencyConverter,
) -> float | None:
    spot = _float(row.get("spot"))
    multiplier = _float(row.get("multiplier"))
    if spot is None or multiplier is None or spot <= 0 or multiplier <= 0:
        return None
    ccy = row.get("currency") or symbol_currency(row.get("symbol"))
    return amount_to_cny(spot * multiplier, ccy, exchange_rate_converter=exchange_rate_converter)
