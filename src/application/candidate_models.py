from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd

from domain.domain.option_position_identity import normalize_currency


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
    market: str
    option_type: str
    expiration: str
    contract_symbol: str
    currency: str
    dte: int | None
    strike: float | None
    spot: float | None
    spot_update_time: str
    spot_observed_at_utc: str
    spot_age_seconds: float | None
    market_state: str
    underlier_observation_status: str
    underlier_observation_reason_code: str
    bid: float | None
    ask: float | None
    last_price: float | None
    mid: float | None
    quote_observed_at_utc: str
    quote_age_seconds: float | None
    price_tick: float | None
    open_interest: float | None
    volume: float | None
    implied_volatility: float | None
    realized_volatility_20: float | None
    realized_volatility_60: float | None
    realized_volatility_120: float | None
    realized_volatility_estimate: float | None
    term_matched_rv: float | None
    term_matched_rv_status: str
    term_matched_rv_reason: str
    term_matched_rv_input_hash: str
    delta: float | None
    option_standard_type: str
    stock_owner: str
    stock_type: str
    option_sec_status: str
    option_suspension: bool | None
    chain_multiplier: float | None
    snapshot_multiplier: float | None
    multiplier: float | None
    quote_update_time: str
    opening_contract_status: str
    opening_contract_reason_codes: str

    @classmethod
    def from_row(cls, row: pd.Series, *, mode: str) -> "CandidateContractInput":
        return cls(
            mode=str(mode),
            symbol=str(row.get("symbol") or "").strip().upper(),
            market=str(row.get("market") or "").strip().upper(),
            option_type=str(row.get("option_type") or "").strip().lower(),
            expiration=str(row.get("expiration") or "").strip(),
            contract_symbol=str(row.get("contract_symbol") or "").strip(),
            currency=normalize_currency(row.get("currency")),
            dte=_as_int(row.get("dte")),
            strike=_as_float(row.get("strike")),
            spot=_as_float(row.get("spot")),
            spot_update_time=str(row.get("spot_update_time") or "").strip(),
            spot_observed_at_utc=str(row.get("spot_observed_at_utc") or "").strip(),
            spot_age_seconds=_as_float(row.get("spot_age_seconds")),
            market_state=str(row.get("market_state") or "").strip(),
            underlier_observation_status=str(row.get("underlier_observation_status") or "").strip(),
            underlier_observation_reason_code=str(row.get("underlier_observation_reason_code") or "").strip(),
            bid=_as_float(row.get("bid")),
            ask=_as_float(row.get("ask")),
            last_price=_as_float(row.get("last_price")),
            mid=_as_float(row.get("mid")),
            quote_observed_at_utc=str(row.get("quote_observed_at_utc") or "").strip(),
            quote_age_seconds=_as_float(row.get("quote_age_seconds")),
            price_tick=_as_float(row.get("price_tick")),
            open_interest=_as_float(row.get("open_interest")),
            volume=_as_float(row.get("volume")),
            implied_volatility=_as_float(row.get("implied_volatility")),
            realized_volatility_20=_as_float(row.get("realized_volatility_20")),
            realized_volatility_60=_as_float(row.get("realized_volatility_60")),
            realized_volatility_120=_as_float(row.get("realized_volatility_120")),
            realized_volatility_estimate=_as_float(row.get("realized_volatility_estimate")),
            term_matched_rv=_as_float(row.get("term_matched_rv")),
            term_matched_rv_status=str(row.get("term_matched_rv_status") or "").strip(),
            term_matched_rv_reason=str(row.get("term_matched_rv_reason") or "").strip(),
            term_matched_rv_input_hash=str(row.get("term_matched_rv_input_hash") or "").strip(),
            delta=_as_float(row.get("delta")),
            option_standard_type=str(row.get("option_standard_type") or "").strip(),
            stock_owner=str(row.get("stock_owner") or "").strip(),
            stock_type=str(row.get("stock_type") or "").strip(),
            option_sec_status=str(row.get("option_sec_status") or "").strip(),
            option_suspension=(None if pd.isna(row.get("option_suspension")) else bool(row.get("option_suspension"))),
            chain_multiplier=_as_float(row.get("chain_multiplier")),
            snapshot_multiplier=_as_float(row.get("snapshot_multiplier")),
            multiplier=_as_float(row.get("multiplier")),
            quote_update_time=str(row.get("quote_update_time") or "").strip(),
            opening_contract_status=str(row.get("opening_contract_status") or "").strip(),
            opening_contract_reason_codes=str(row.get("opening_contract_reason_codes") or "").strip(),
        )

    def to_gate_payload(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "market": self.market,
            "option_type": self.option_type,
            "expiration": self.expiration,
            "contract_symbol": self.contract_symbol,
            "currency": self.currency,
            "dte": self.dte,
            "strike": self.strike,
            "spot": self.spot,
            "spot_update_time": self.spot_update_time,
            "spot_observed_at_utc": self.spot_observed_at_utc,
            "spot_age_seconds": self.spot_age_seconds,
            "market_state": self.market_state,
            "underlier_observation_status": self.underlier_observation_status,
            "underlier_observation_reason_code": self.underlier_observation_reason_code,
            "bid": self.bid,
            "ask": self.ask,
            "last_price": self.last_price,
            "mid": self.mid,
            "quote_observed_at_utc": self.quote_observed_at_utc,
            "quote_age_seconds": self.quote_age_seconds,
            "price_tick": self.price_tick,
            "open_interest": self.open_interest,
            "volume": self.volume,
            "implied_volatility": self.implied_volatility,
            "realized_volatility_20": self.realized_volatility_20,
            "realized_volatility_60": self.realized_volatility_60,
            "realized_volatility_120": self.realized_volatility_120,
            "realized_volatility_estimate": self.realized_volatility_estimate,
            "term_matched_rv": self.term_matched_rv,
            "term_matched_rv_status": self.term_matched_rv_status,
            "term_matched_rv_reason": self.term_matched_rv_reason,
            "term_matched_rv_input_hash": self.term_matched_rv_input_hash,
            "delta": self.delta,
            "option_standard_type": self.option_standard_type,
            "stock_owner": self.stock_owner,
            "stock_type": self.stock_type,
            "option_sec_status": self.option_sec_status,
            "option_suspension": self.option_suspension,
            "chain_multiplier": self.chain_multiplier,
            "snapshot_multiplier": self.snapshot_multiplier,
            "multiplier": self.multiplier,
            "quote_update_time": self.quote_update_time,
            "opening_contract_status": self.opening_contract_status,
            "opening_contract_reason_codes": self.opening_contract_reason_codes,
        }


@dataclass(frozen=True)
class CandidateBaseValues:
    dte: int
    strike: float
    open_interest: float | None
    volume: float | None
    spread: float | None
    spread_ratio: float | None
