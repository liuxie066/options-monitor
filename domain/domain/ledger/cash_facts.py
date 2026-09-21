from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any, Mapping

from domain.domain.ledger.economics import fee_fact_for_event, fee_fact_from_persisted_evidence
from domain.domain.ledger.events import CLOSE_EVENT_TYPES, TradeEvent, persisted_stock_settlement
from domain.domain.ledger.fees import FeeBasis, FeeComponent
from domain.domain.money import quantize_money, to_decimal
from domain.domain.trade_contract_identity import contract_share_quantity
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


def assignment_principal_anchor(
    event: TradeEvent,
    direction: str,
) -> tuple[str | None, str | None, str | None, tuple[str, ...]]:
    facts = {
        fact.fact_kind: fact
        for fact in cash_facts_for_trade_event(event)
        if fact.fact_kind.startswith("stock_settlement_")
    }
    gross = facts.get("stock_settlement_cash_gross")
    fee = facts.get("stock_settlement_fee_cash")
    fact_ids = tuple(sorted(fact.fact_id for fact in facts.values()))
    if (
        gross is None
        or fee is None
        or gross.amount is None
        or fee.amount is None
        or not gross.currency
        or gross.currency != fee.currency
    ):
        currency = (
            gross.currency
            if gross is not None
            else fee.currency
            if fee is not None
            else None
        )
        return None, currency, "assignment_cash_facts_unavailable", fact_ids
    net = Decimal(gross.amount) + Decimal(fee.amount)
    anchor = -net if direction == "call" else net
    if anchor < 0:
        return None, gross.currency, "assignment_cash_facts_invalid", fact_ids
    return format(anchor, "f"), gross.currency, None, fact_ids


def broker_settlement_multiplier_evidence(event: TradeEvent) -> dict[str, Any] | None:
    raw = event.raw_payload if isinstance(event.raw_payload, Mapping) else {}
    stock = raw.get("stock_settlement")
    stock = stock if isinstance(stock, Mapping) else {}
    source_event_id = str(stock.get("source_event_id") or "").strip()
    if (
        event.event_type not in {"assignment", "exercise"}
        or str(raw.get("source_type") or "").strip().lower()
        != "broker_settlement_pair"
        or not source_event_id
        or source_event_id not in str(raw.get("source_event_id") or "").split("|")
        or not str(stock.get("futu_account_id") or "").strip()
        or not str(stock.get("order_id") or "").strip()
        or str(stock.get("symbol") or "").strip().upper()
        != event.contract_key.underlying_symbol
    ):
        return None
    try:
        shares = to_decimal(
            stock.get("shares"), field_name="stock settlement shares"
        )
        contracts = Decimal(event.contracts)
        multiplier = shares / contracts
        recorded_multiplier = to_decimal(event.multiplier, field_name="multiplier")
    except (ArithmeticError, TypeError, ValueError):
        return None
    if (
        shares <= 0
        or contracts <= 0
        or multiplier != multiplier.to_integral_value()
        or multiplier != recorded_multiplier
    ):
        return None
    return {
        "schema_version": "wheel_assignment_multiplier_evidence.v1",
        "source_assignment_event_id": event.event_id,
        "source_event_id": source_event_id,
        "futu_account_id": str(stock["futu_account_id"]),
        "order_id": str(stock["order_id"]),
        "symbol": event.contract_key.underlying_symbol,
        "contracts": event.contracts,
        "shares": int(shares),
        "multiplier": int(multiplier),
    }


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
        gross = quantize_money(price * contract_share_quantity(event.contracts, multiplier))
    except (TypeError, ValueError) as exc:
        return None, f"option cash unavailable: {exc}"
    positive = (event.position_side == "short") == (event.event_type == "open")
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
    raw = persisted_stock_settlement(raw)
    currency = _currency_or_none(raw.get("currency") or event.currency)
    common["currency"] = currency
    try:
        shares = to_decimal(raw.get("shares"), field_name="stock settlement shares")
        price = to_decimal(raw.get("price"), field_name="stock settlement price")
        side = str(raw.get("side") or "").strip().lower()
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
        compatibility_amount=raw.get("fees", 0),
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


__all__ = [
    "TradeCashFact",
    "assignment_principal_anchor",
    "broker_settlement_multiplier_evidence",
    "cash_facts_for_trade_event",
]
