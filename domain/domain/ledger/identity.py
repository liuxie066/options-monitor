from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from domain.domain.option_position_identity import (
    normalize_account,
    normalize_broker,
    normalize_option_type,
    normalize_side,
)
from domain.domain.trade_contract_identity import (
    canonical_contract_symbol,
    normalize_contract_expiration,
)


def _normalize_strike(value: Any) -> float:
    if value in (None, ""):
        raise ValueError("strike is required")
    numeric = float(value)
    return round(numeric, 6)


@dataclass(frozen=True)
class ContractKey:
    broker: str
    account: str
    underlying_symbol: str
    option_type: str
    position_side: str
    strike: float
    expiration_ymd: str

    @classmethod
    def from_values(
        cls,
        *,
        broker: Any,
        account: Any,
        underlying_symbol: Any,
        option_type: Any,
        position_side: Any,
        strike: Any,
        expiration_ymd: Any,
    ) -> "ContractKey":
        normalized_broker = normalize_broker(str(broker or ""))
        normalized_account = normalize_account(account)
        normalized_symbol = canonical_contract_symbol(underlying_symbol)
        normalized_option_type = normalize_option_type(option_type, strict=True)
        normalized_position_side = normalize_side(position_side, strict=True)
        normalized_expiration = normalize_contract_expiration(expiration_ymd)
        if not normalized_broker:
            raise ValueError("broker is required")
        if not normalized_account:
            raise ValueError("account is required")
        if not normalized_symbol:
            raise ValueError("underlying_symbol is required")
        if not normalized_expiration:
            raise ValueError("expiration_ymd is required")
        return cls(
            broker=normalized_broker,
            account=normalized_account,
            underlying_symbol=normalized_symbol,
            option_type=normalized_option_type,
            position_side=normalized_position_side,
            strike=_normalize_strike(strike),
            expiration_ymd=normalized_expiration,
        )

    @property
    def position_key(self) -> str:
        strike = f"{self.strike:.6f}".rstrip("0").rstrip(".")
        option_suffix = "P" if self.option_type == "put" else "C"
        return (
            f"{self.broker}|{self.account}|{self.underlying_symbol}|"
            f"{self.expiration_ymd}|{strike}{option_suffix}|{self.position_side}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "broker": self.broker,
            "account": self.account,
            "underlying_symbol": self.underlying_symbol,
            "option_type": self.option_type,
            "position_side": self.position_side,
            "strike": self.strike,
            "expiration_ymd": self.expiration_ymd,
            "position_key": self.position_key,
        }
