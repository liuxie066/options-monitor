from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any, Mapping

from domain.domain.ledger.economics import fee_fact_for_event, fee_fact_from_persisted_evidence
from domain.domain.ledger.events import CLOSE_EVENT_TYPES, TradeEvent
from domain.domain.ledger.fees import FeeBasis, FeeComponent
from domain.domain.money import quantize_money, to_decimal
from domain.domain.option_position_identity import normalize_currency


_CASH_KINDS = frozenset(
    {
        "option_trade_cash_gross",
        "option_fee_cash",
        "stock_settlement_cash_gross",
        "stock_settlement_fee_cash",
    }
)


@dataclass(frozen=True)
class TradeCashFact:
    fact_kind: str
    effective_at_ms: int
    currency: str | None
    source_event_id: str
    amount: Decimal | None = None
    missing_reason: str | None = None
    cash_conversion: Mapping[str, Any] | None = None

    @property
    def fact_id(self) -> str:
        return f"{self.fact_kind}:{self.source_event_id or self.effective_at_ms}"


def cash_facts_for_trade_event(event: TradeEvent) -> list[TradeCashFact]:
    if event.event_type == "open":
        facts = _option_trade_cash_facts(event)
    elif event.event_type in CLOSE_EVENT_TYPES:
        facts = [*_option_trade_cash_facts(event), *_stock_settlement_cash_facts(event)]
    else:
        return []
    conversions = event.raw_payload.get("cash_conversions") if isinstance(event.raw_payload, dict) else None
    if not isinstance(conversions, Mapping):
        return facts
    return [
        replace(fact, cash_conversion=dict(conversions[fact.fact_kind]))
        if fact.fact_kind in _CASH_KINDS and isinstance(conversions.get(fact.fact_kind), Mapping)
        else fact
        for fact in facts
    ]


def _option_trade_cash_facts(event: TradeEvent) -> list[TradeCashFact]:
    currency = _currency_or_none(event.currency)
    amount, reason = _option_amount(event)
    if currency is None:
        amount = None
        reason = "option event currency unavailable"
    common = {
        "effective_at_ms": event.event_time_ms,
        "currency": currency,
        "source_event_id": event.event_id,
    }
    fee = fee_fact_for_event(event)
    fee_amount = -fee.amount if fee.is_complete and fee.amount is not None and currency else None
    fee_reason = None if fee_amount is not None else fee.reason or f"{fee.basis.value} option fee is unavailable"
    return [
        TradeCashFact(
            fact_kind="option_trade_cash_gross",
            amount=amount,
            missing_reason=reason,
            **common,
        ),
        TradeCashFact(
            fact_kind="option_fee_cash",
            amount=fee_amount,
            missing_reason=fee_reason,
            **common,
        ),
    ]


def _option_amount(event: TradeEvent) -> tuple[Decimal | None, str | None]:
    try:
        price = to_decimal(event.price, field_name="price")
        multiplier = to_decimal(event.multiplier, field_name="multiplier")
        if price < 0 or multiplier <= 0:
            raise ValueError("price must be non-negative and multiplier must be positive")
        gross = quantize_money(price * multiplier * Decimal(event.contracts))
    except (TypeError, ValueError) as exc:
        return None, f"option cash unavailable: {exc}"
    positive = (event.contract_key.position_side == "short") == (event.event_type == "open")
    return (gross if positive else -gross), None


def _stock_settlement_cash_facts(event: TradeEvent) -> list[TradeCashFact]:
    if event.event_type not in {"assignment", "exercise"}:
        return []
    raw = event.raw_payload.get("stock_settlement") if isinstance(event.raw_payload, dict) else None
    common = {
        "effective_at_ms": event.event_time_ms,
        "currency": _currency_or_none(event.currency),
        "source_event_id": event.event_id,
    }
    if not isinstance(raw, dict):
        return [
            TradeCashFact(
                fact_kind=kind,
                amount=None,
                missing_reason="assignment/exercise stock_settlement is missing",
                **common,
            )
            for kind in ("stock_settlement_cash_gross", "stock_settlement_fee_cash")
        ]
    currency = _currency_or_none(raw.get("currency") or event.currency)
    common["currency"] = currency
    try:
        shares = to_decimal(raw.get("shares", raw.get("stock_qty")), field_name="stock settlement shares")
        price = to_decimal(raw.get("price", raw.get("stock_price")), field_name="stock settlement price")
        side = str(raw.get("side") or raw.get("stock_side") or "").strip().lower()
        if shares != shares.to_integral_value() or shares <= 0 or price < 0 or side not in {"buy", "sell"}:
            raise ValueError("stock settlement values are invalid")
        principal = quantize_money(price * shares)
        cash_amount = (principal if side == "sell" else -principal) if currency else None
        cash_reason = None if currency else "stock settlement currency unavailable"
    except (TypeError, ValueError) as exc:
        cash_amount = None
        cash_reason = f"stock settlement cash unavailable: {exc}"
    fee = fee_fact_from_persisted_evidence(
        event_id=f"{event.event_id}:stock_settlement",
        component=FeeComponent.STOCK_SETTLEMENT,
        provenance=raw.get("fee_provenance"),
        compatibility_amount=raw.get("fees", raw.get("fee", 0)),
    )
    fee_amount = -fee.amount if fee.basis == FeeBasis.ACTUAL and fee.amount is not None and currency else None
    fee_reason = None if fee_amount is not None else fee.reason or "stock settlement fee is unavailable"
    return [
        TradeCashFact(
            fact_kind="stock_settlement_cash_gross",
            amount=cash_amount,
            missing_reason=cash_reason,
            **common,
        ),
        TradeCashFact(
            fact_kind="stock_settlement_fee_cash",
            amount=fee_amount,
            missing_reason=fee_reason,
            **common,
        ),
    ]


def _currency_or_none(value: Any) -> str | None:
    try:
        return normalize_currency(value)
    except ValueError:
        return None


__all__ = ["TradeCashFact", "cash_facts_for_trade_event"]
