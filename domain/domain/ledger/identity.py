from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from domain.domain.money import canonical_decimal_text, to_decimal
from domain.domain.option_position_identity import (
    normalize_account,
    normalize_broker,
    normalize_option_type,
)
from domain.domain.trade_contract_identity import (
    canonical_contract_symbol,
    normalize_asset_type,
    normalize_contract_expiration,
)


def _normalize_strike(value: Any) -> Decimal:
    if value in (None, ""):
        raise ValueError("strike is required")
    decimal_value = to_decimal(value, field_name="strike")
    if decimal_value <= 0:
        raise ValueError("strike must be finite and > 0")
    return decimal_value


@dataclass(frozen=True)
class ContractKey:
    broker: str
    account: str
    underlying_symbol: str
    option_type: str
    strike: Decimal
    expiration_ymd: str
    asset_type: str = "option"

    @classmethod
    def from_values(
        cls,
        *,
        broker: Any,
        account: Any,
        underlying_symbol: Any,
        option_type: Any,
        strike: Any,
        expiration_ymd: Any,
        asset_type: Any = "option",
    ) -> "ContractKey":
        normalized_broker = normalize_broker(str(broker or ""))
        normalized_account = normalize_account(account)
        normalized_symbol = canonical_contract_symbol(underlying_symbol)
        normalized_asset_type = normalize_asset_type(asset_type) or "option"
        if not normalized_broker:
            raise ValueError("broker is required")
        if not normalized_account:
            raise ValueError("account is required")
        if not normalized_symbol:
            raise ValueError("underlying_symbol is required")
        if normalized_asset_type == "stock":
            return cls(
                broker=normalized_broker,
                account=normalized_account,
                underlying_symbol=normalized_symbol,
                option_type="",
                strike=Decimal("0"),
                expiration_ymd="",
                asset_type="stock",
            )
        normalized_option_type = normalize_option_type(option_type, strict=True)
        normalized_expiration = normalize_contract_expiration(expiration_ymd)
        if not normalized_expiration:
            raise ValueError("expiration_ymd is required")
        return cls(
            broker=normalized_broker,
            account=normalized_account,
            underlying_symbol=normalized_symbol,
            option_type=normalized_option_type,
            strike=_normalize_strike(strike),
            expiration_ymd=normalized_expiration,
            asset_type=normalized_asset_type,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "broker": self.broker,
            "account": self.account,
            "underlying_symbol": self.underlying_symbol,
            "option_type": self.option_type,
            "strike": canonical_decimal_text(self.strike),
            "expiration_ymd": self.expiration_ymd,
            "asset_type": self.asset_type,
        }


def position_key_for(contract_key: ContractKey, position_side: str) -> str:
    """Aggregation/display key: contract identity + derived side (§9.2 step 3)."""
    if contract_key.asset_type == "stock":
        return (
            f"{contract_key.broker}|{contract_key.account}|{contract_key.underlying_symbol}|"
            f"stock|{position_side}"
        )
    strike = f"{contract_key.strike:.6f}".rstrip("0").rstrip(".")
    option_suffix = "P" if contract_key.option_type == "put" else "C"
    return (
        f"{contract_key.broker}|{contract_key.account}|{contract_key.underlying_symbol}|"
        f"{contract_key.expiration_ymd}|{strike}{option_suffix}|{position_side}"
    )
