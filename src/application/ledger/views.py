from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from domain.domain.symbol_identity import canonical_symbol


def _text_or_none(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _int_or_zero(value: Any) -> int:
    try:
        return int(float(value or 0))
    except Exception:
        return 0


@dataclass(frozen=True)
class PositionLotSnapshot:
    record_id: str | None
    fields: dict[str, Any]

    @classmethod
    def from_record(cls, item: dict[str, Any]) -> "PositionLotSnapshot":
        fields = item.get("fields") if isinstance(item, dict) else None
        fields_dict = dict(fields) if isinstance(fields, dict) else {}
        record_id = _text_or_none(item.get("record_id") if isinstance(item, dict) else None)
        if record_id is None:
            record_id = _text_or_none(fields_dict.get("record_id"))
        return cls(record_id=record_id, fields=fields_dict)

    def as_record(self) -> dict[str, Any]:
        return {"record_id": self.record_id, "fields": dict(self.fields)}


@dataclass(frozen=True)
class RiskPositionView:
    record_id: str | None
    fields: dict[str, Any]
    position_id: str | None
    broker: str | None
    account: str | None
    symbol: str | None
    option_type: str | None
    side: str | None
    status: str | None
    strike: Any
    multiplier: Any
    expiration: Any
    expiration_ymd: str | None
    expiration_date: date | None
    contracts: int
    contracts_open: int
    contracts_closed: int
    currency: str | None
    cash_secured_amount: Any
    underlying_share_locked: Any
    premium: Any
    opened_at: Any
    last_action_at: Any
    close_type: str | None
    close_reason: Any
    note: Any

    @classmethod
    def from_view(cls, view: dict[str, Any]) -> "RiskPositionView":
        fields = view.get("fields") if isinstance(view, dict) else None
        fields_dict = dict(fields) if isinstance(fields, dict) else {}
        return cls(
            record_id=_text_or_none(view.get("record_id")) if isinstance(view, dict) else None,
            fields=fields_dict,
            position_id=_text_or_none(view.get("position_id")) if isinstance(view, dict) else None,
            broker=_text_or_none(view.get("broker")) if isinstance(view, dict) else None,
            account=_text_or_none(view.get("account")) if isinstance(view, dict) else None,
            symbol=_text_or_none(view.get("symbol")) if isinstance(view, dict) else None,
            option_type=_text_or_none(view.get("option_type")) if isinstance(view, dict) else None,
            side=_text_or_none(view.get("side")) if isinstance(view, dict) else None,
            status=_text_or_none(view.get("status")) if isinstance(view, dict) else None,
            strike=view.get("strike") if isinstance(view, dict) else None,
            multiplier=view.get("multiplier") if isinstance(view, dict) else None,
            expiration=view.get("expiration") if isinstance(view, dict) else None,
            expiration_ymd=_text_or_none(view.get("expiration_ymd")) if isinstance(view, dict) else None,
            expiration_date=(
                view.get("expiration_date")
                if isinstance(view, dict) and isinstance(view.get("expiration_date"), date)
                else None
            ),
            contracts=_int_or_zero(view.get("contracts")) if isinstance(view, dict) else 0,
            contracts_open=_int_or_zero(view.get("contracts_open")) if isinstance(view, dict) else 0,
            contracts_closed=_int_or_zero(view.get("contracts_closed")) if isinstance(view, dict) else 0,
            currency=_text_or_none(view.get("currency")) if isinstance(view, dict) else None,
            cash_secured_amount=view.get("cash_secured_amount") if isinstance(view, dict) else None,
            underlying_share_locked=view.get("underlying_share_locked") if isinstance(view, dict) else None,
            premium=view.get("premium") if isinstance(view, dict) else None,
            opened_at=view.get("opened_at") if isinstance(view, dict) else None,
            last_action_at=view.get("last_action_at") if isinstance(view, dict) else None,
            close_type=_text_or_none(view.get("close_type")) if isinstance(view, dict) else None,
            close_reason=fields_dict.get("close_reason"),
            note=view.get("note") if isinstance(view, dict) else None,
        )

    @property
    def canonical_underlying_symbol(self) -> str:
        raw_symbol = str(self.symbol or "").strip().upper()
        return canonical_symbol(raw_symbol) or raw_symbol

    @property
    def is_open(self) -> bool:
        return not self.status or self.status == "open"

    def as_shadow_record(self) -> dict[str, Any] | None:
        if not self.record_id:
            return None
        return {"record_id": self.record_id, "fields": dict(self.fields)}

    def as_open_position_min(self, *, as_of_date: date) -> dict[str, Any]:
        days_to_expiration = (
            (self.expiration_date - as_of_date).days if self.expiration_date is not None else None
        )
        return {
            "record_id": self.record_id,
            "position_id": self.position_id,
            "broker": self.broker,
            "account": self.account,
            "symbol": self.canonical_underlying_symbol,
            "option_type": self.option_type,
            "side": self.side,
            "status": "open",
            "contracts": self.contracts,
            "contracts_open": self.contracts_open,
            "contracts_closed": self.contracts_closed,
            "currency": self.currency,
            "cash_secured_amount": self.cash_secured_amount,
            "underlying_share_locked": self.underlying_share_locked,
            "strike": self.strike,
            "multiplier": self.multiplier,
            "premium": self.premium,
            "expiration": self.expiration,
            "expiration_ymd": self.expiration_ymd,
            "days_to_expiration": days_to_expiration,
            "opened_at": self.opened_at,
            "last_action_at": self.last_action_at,
            "close_type": self.close_type,
            "close_reason": self.close_reason,
            "note": self.note,
        }


__all__ = ["PositionLotSnapshot", "RiskPositionView"]
