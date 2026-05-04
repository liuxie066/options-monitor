from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd

from scripts.option_positions_core.domain import normalize_currency


def _as_float(value: Any) -> float | None:
    try:
        coerced = pd.to_numeric(value, errors="coerce")
    except Exception:
        return None
    try:
        return None if pd.isna(coerced) else float(coerced)
    except Exception:
        return None


def _as_int(value: Any) -> int | None:
    numeric = _as_float(value)
    if numeric is None:
        return None
    try:
        return int(numeric)
    except Exception:
        return None


@dataclass(frozen=True)
class CandidateContractInput:
    mode: str
    symbol: str
    option_type: str
    expiration: str
    contract_symbol: str
    currency: str
    dte: int | None
    strike: float | None
    spot: float | None
    bid: float | None
    ask: float | None
    last_price: float | None
    mid: float | None
    open_interest: float | None
    volume: float | None
    implied_volatility: float | None
    delta: float | None
    multiplier: float | None

    @classmethod
    def from_row(cls, row: pd.Series, *, mode: str) -> "CandidateContractInput":
        return cls(
            mode=str(mode),
            symbol=str(row.get("symbol") or "").strip().upper(),
            option_type=str(row.get("option_type") or "").strip().lower(),
            expiration=str(row.get("expiration") or "").strip(),
            contract_symbol=str(row.get("contract_symbol") or "").strip(),
            currency=normalize_currency(row.get("currency")),
            dte=_as_int(row.get("dte")),
            strike=_as_float(row.get("strike")),
            spot=_as_float(row.get("spot")),
            bid=_as_float(row.get("bid")),
            ask=_as_float(row.get("ask")),
            last_price=_as_float(row.get("last_price")),
            mid=_as_float(row.get("mid")),
            open_interest=_as_float(row.get("open_interest")),
            volume=_as_float(row.get("volume")),
            implied_volatility=_as_float(row.get("implied_volatility")),
            delta=_as_float(row.get("delta")),
            multiplier=_as_float(row.get("multiplier")),
        )

    def to_gate_payload(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "option_type": self.option_type,
            "expiration": self.expiration,
            "contract_symbol": self.contract_symbol,
            "currency": self.currency,
            "dte": self.dte,
            "strike": self.strike,
            "spot": self.spot,
            "bid": self.bid,
            "ask": self.ask,
            "last_price": self.last_price,
            "mid": self.mid,
            "open_interest": self.open_interest,
            "volume": self.volume,
            "implied_volatility": self.implied_volatility,
            "delta": self.delta,
            "multiplier": self.multiplier,
        }


@dataclass(frozen=True)
class CandidateBaseValues:
    dte: int
    strike: float
    open_interest: float
    volume: float
    spread: float | None
    spread_ratio: float | None
