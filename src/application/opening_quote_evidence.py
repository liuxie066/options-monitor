from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import math
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from src.application.opend_normalize import normalize_iv


OPENING_UNDERLIER_OBSERVATION_SCHEMA = "opening_underlier_observation.v1"
OPENING_OPTION_OBSERVATION_SCHEMA = "opening_option_observation.v1"
OPENING_QUOTE_MAX_AGE_SECONDS = 300

_MARKET_TIMEZONES = {
    "US": ZoneInfo("America/New_York"),
    "HK": ZoneInfo("Asia/Hong_Kong"),
}
_CONTINUOUS_MARKET_STATES = frozenset({"MORNING", "AFTERNOON"})
_NON_CONTINUOUS_MARKET_STATES = frozenset(
    {
        "NONE",
        "AUCTION",
        "WAITING_OPEN",
        "REST",
        "CLOSED",
        "PRE_MARKET_BEGIN",
        "PRE_MARKET_END",
        "AFTER_HOURS_BEGIN",
        "AFTER_HOURS_END",
        "NIGHT_OPEN",
        "NIGHT_END",
        "HK_CAS",
        "CLOSE_AUCTION",
        "AFTERNOON_END",
        "NIGHT",
        "OVERNIGHT_BEGIN",
        "OVERNIGHT_END",
        "OVERNIGHT",
        "TRADE_AT_LAST",
        "TRADE_AUCTION",
    }
)
_MISSING_ENUM_VALUES = frozenset({"", "N/A", "NONE", "UNKNOWN"})
_OPEND_OPTION_SECURITY_TYPE = "DRVT"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _enum(value: Any) -> str:
    return _text(value).upper()


def _finite_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _positive_int(value: Any) -> int | None:
    number = _finite_float(value)
    if number is None or number <= 0 or not number.is_integer():
        return None
    return int(number)


def _optional_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value in (0, 1):
        return bool(value)
    raw = _text(value).lower()
    if raw in {"true", "yes"}:
        return True
    if raw in {"false", "no"}:
        return False
    return None


def _parse_opend_time(value: Any, *, market: str) -> datetime | None:
    timezone_value = _MARKET_TIMEZONES.get(market)
    if timezone_value is None:
        return None
    raw = _text(value)
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone_value)
    return parsed.astimezone(timezone.utc)


def _utc_now(now_utc: datetime | None) -> datetime:
    value = now_utc or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class OpeningUnderlierObservation:
    schema_version: str
    code: str
    market: str
    last_price: float | None
    update_time: str | None
    observed_at_utc: str | None
    age_seconds: float | None
    market_state: str | None
    sec_status: str | None
    suspension: bool | None
    status: str
    reason_code: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "OpeningUnderlierObservation":
        if value.get("schema_version") != OPENING_UNDERLIER_OBSERVATION_SCHEMA:
            raise ValueError("underlier observation schema mismatch")
        return cls(
            schema_version=OPENING_UNDERLIER_OBSERVATION_SCHEMA,
            code=_text(value.get("code")).upper(),
            market=_enum(value.get("market")),
            last_price=_finite_float(value.get("last_price")),
            update_time=_text(value.get("update_time")) or None,
            observed_at_utc=_text(value.get("observed_at_utc")) or None,
            age_seconds=_finite_float(value.get("age_seconds")),
            market_state=_enum(value.get("market_state")) or None,
            sec_status=_enum(value.get("sec_status")) or None,
            suspension=_optional_bool(value.get("suspension")),
            status=_text(value.get("status")),
            reason_code=_text(value.get("reason_code")) or None,
        )


def normalize_underlier_observation(
    *,
    code: str,
    market: str,
    snapshot_row: Mapping[str, Any] | None,
    market_state_row: Mapping[str, Any] | None,
    now_utc: datetime | None = None,
    max_age_seconds: int = OPENING_QUOTE_MAX_AGE_SECONDS,
) -> OpeningUnderlierObservation:
    market_code = _enum(market)
    expected_code = _text(code).upper()
    snapshot = dict(snapshot_row or {})
    state = dict(market_state_row or {})
    snapshot_code = _text(snapshot.get("code")).upper()
    state_code = _text(state.get("code")).upper()
    last_price = _finite_float(snapshot.get("last_price"))
    raw_update_time = _text(snapshot.get("update_time")) or None
    market_state = _enum(state.get("market_state")) or None
    sec_status = _enum(snapshot.get("sec_status")) or None
    suspension = _optional_bool(snapshot.get("suspension"))
    observed = _parse_opend_time(raw_update_time, market=market_code)
    age_seconds = (
        (_utc_now(now_utc) - observed).total_seconds()
        if observed is not None
        else None
    )

    status = "ready"
    reason_code: str | None = None
    if market_code not in _MARKET_TIMEZONES:
        status, reason_code = "data_unavailable", "underlier_market_invalid"
    elif snapshot_code != expected_code or state_code != expected_code:
        status, reason_code = "data_unavailable", "underlier_identity_mismatch"
    elif market_state in _NON_CONTINUOUS_MARKET_STATES:
        status, reason_code = "market_closed", "market_closed"
    elif market_state not in _CONTINUOUS_MARKET_STATES:
        status, reason_code = "data_unavailable", "market_state_missing_or_invalid"
    elif sec_status in _MISSING_ENUM_VALUES or sec_status is None:
        status, reason_code = "data_unavailable", "underlier_sec_status_missing"
    elif sec_status != "NORMAL" or suspension is True:
        status, reason_code = "data_unavailable", "underlier_security_not_normal"
    elif last_price is None or last_price <= 0:
        status, reason_code = "data_unavailable", "underlier_last_price_invalid"
    elif observed is None or age_seconds is None:
        status, reason_code = "data_unavailable", "underlier_update_time_missing_or_invalid"
    elif age_seconds < 0:
        status, reason_code = "data_unavailable", "underlier_update_time_in_future"
    elif age_seconds > int(max_age_seconds):
        status, reason_code = "data_unavailable", "underlier_quote_stale"

    return OpeningUnderlierObservation(
        schema_version=OPENING_UNDERLIER_OBSERVATION_SCHEMA,
        code=expected_code,
        market=market_code,
        last_price=last_price,
        update_time=raw_update_time,
        observed_at_utc=(observed.isoformat() if observed is not None else None),
        age_seconds=(round(age_seconds, 3) if age_seconds is not None else None),
        market_state=market_state,
        sec_status=sec_status,
        suspension=suspension,
        status=status,
        reason_code=reason_code,
    )


@dataclass(frozen=True)
class OpeningOptionObservation:
    schema_version: str
    contract_symbol: str
    status: str
    reason_codes: tuple[str, ...]
    bid: float | None
    ask: float | None
    last_price: float | None
    quote_update_time: str | None
    quote_observed_at_utc: str | None
    quote_age_seconds: float | None
    price_tick: float | None
    implied_volatility: float | None
    delta: float | None
    open_interest: float | None
    volume: float | None
    option_standard_type: str | None
    stock_owner: str | None
    stock_type: str | None
    sec_status: str | None
    suspension: bool | None
    currency: str
    chain_multiplier: int | None
    snapshot_multiplier: int | None
    multiplier: int | None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["reason_codes"] = list(self.reason_codes)
        return payload


def normalize_option_observation(
    *,
    expected_owner: str,
    market: str,
    currency: str,
    chain_row: Mapping[str, Any],
    snapshot_row: Mapping[str, Any] | None,
    underlier_observation: OpeningUnderlierObservation,
    now_utc: datetime | None = None,
    max_age_seconds: int = OPENING_QUOTE_MAX_AGE_SECONDS,
) -> OpeningOptionObservation:
    chain = dict(chain_row)
    snapshot = dict(snapshot_row or {})
    contract_symbol = _text(chain.get("code")).upper()
    snapshot_code = _text(snapshot.get("code")).upper()
    standard_type = _enum(chain.get("option_standard_type")) or None
    stock_owner = _text(chain.get("stock_owner")).upper() or None
    stock_type = _enum(chain.get("stock_type")) or None
    chain_suspension = _optional_bool(chain.get("suspension"))
    snapshot_suspension = _optional_bool(snapshot.get("suspension"))
    suspension = (
        True
        if chain_suspension is True or snapshot_suspension is True
        else False
        if chain_suspension is False and snapshot_suspension is False
        else None
    )
    sec_status = _enum(snapshot.get("sec_status")) or None
    bid = _finite_float(snapshot.get("bid_price", snapshot.get("bid")))
    ask = _finite_float(snapshot.get("ask_price", snapshot.get("ask")))
    last_price = _finite_float(snapshot.get("last_price"))
    raw_update_time = _text(snapshot.get("update_time")) or None
    observed = _parse_opend_time(raw_update_time, market=_enum(market))
    age_seconds = (
        (_utc_now(now_utc) - observed).total_seconds()
        if observed is not None
        else None
    )
    price_tick = _finite_float(snapshot.get("price_spread"))
    raw_iv = _finite_float(
        snapshot.get("option_implied_volatility", snapshot.get("implied_volatility"))
    )
    implied_volatility = normalize_iv(raw_iv)
    delta = _finite_float(snapshot.get("option_delta", snapshot.get("delta")))
    open_interest = _finite_float(
        snapshot.get("option_open_interest", snapshot.get("open_interest"))
    )
    volume = _finite_float(snapshot.get("volume"))
    chain_multiplier = _positive_int(chain.get("lot_size"))
    snapshot_multiplier_values = {
        value
        for value in (
            _positive_int(snapshot.get("option_contract_size")),
            _positive_int(snapshot.get("option_contract_multiplier")),
        )
        if value is not None
    }
    snapshot_multiplier = (
        next(iter(snapshot_multiplier_values))
        if len(snapshot_multiplier_values) == 1
        else None
    )
    multiplier = (
        chain_multiplier
        if chain_multiplier is not None and chain_multiplier == snapshot_multiplier
        else None
    )

    unavailable: list[str] = []
    ineligible: list[str] = []
    if underlier_observation.status == "market_closed":
        status = "market_closed"
        reason_codes = ("market_closed",)
    else:
        if underlier_observation.status != "ready":
            unavailable.append("underlier_observation_unavailable")
        if not contract_symbol or snapshot_code != contract_symbol:
            unavailable.append("option_snapshot_identity_mismatch")
        if standard_type in _MISSING_ENUM_VALUES or standard_type is None:
            unavailable.append("option_standard_type_missing")
        elif standard_type != "STANDARD":
            ineligible.append("option_non_standard")
        if stock_owner is None:
            unavailable.append("option_stock_owner_missing")
        elif stock_owner != _text(expected_owner).upper():
            ineligible.append("option_stock_owner_mismatch")
        if stock_type in _MISSING_ENUM_VALUES or stock_type is None:
            unavailable.append("option_stock_type_missing")
        elif stock_type != _OPEND_OPTION_SECURITY_TYPE:
            ineligible.append("option_stock_type_mismatch")
        if suspension is None:
            unavailable.append("option_suspension_status_missing")
        elif suspension:
            ineligible.append("option_suspended")
        if sec_status in _MISSING_ENUM_VALUES or sec_status is None:
            unavailable.append("option_sec_status_missing")
        elif sec_status != "NORMAL":
            ineligible.append("option_security_not_normal")
        if chain_multiplier is None:
            unavailable.append("option_chain_multiplier_missing")
        if len(snapshot_multiplier_values) > 1:
            unavailable.append("option_snapshot_multiplier_conflict")
        elif snapshot_multiplier is None:
            unavailable.append("option_snapshot_multiplier_missing")
        elif chain_multiplier is not None and chain_multiplier != snapshot_multiplier:
            unavailable.append("option_multiplier_conflict")
        if price_tick is None or price_tick <= 0:
            unavailable.append("option_price_tick_missing_or_invalid")
        if bid is None or bid <= 0:
            unavailable.append("option_bid_missing_or_invalid")
        if ask is None or ask <= 0 or (bid is not None and ask < bid):
            unavailable.append("option_ask_missing_or_invalid")
        if observed is None or age_seconds is None:
            unavailable.append("option_quote_time_missing_or_invalid")
        elif age_seconds < 0:
            unavailable.append("option_quote_time_in_future")
        elif age_seconds > int(max_age_seconds):
            unavailable.append("option_quote_stale")
        if unavailable:
            status = "data_unavailable"
            reason_codes = tuple(dict.fromkeys(unavailable + ineligible))
        elif ineligible:
            status = "ineligible"
            reason_codes = tuple(dict.fromkeys(ineligible))
        else:
            status = "ready"
            reason_codes = ()

    return OpeningOptionObservation(
        schema_version=OPENING_OPTION_OBSERVATION_SCHEMA,
        contract_symbol=contract_symbol,
        status=status,
        reason_codes=reason_codes,
        bid=bid,
        ask=ask,
        last_price=last_price,
        quote_update_time=raw_update_time,
        quote_observed_at_utc=(observed.isoformat() if observed is not None else None),
        quote_age_seconds=(round(age_seconds, 3) if age_seconds is not None else None),
        price_tick=price_tick,
        implied_volatility=implied_volatility,
        delta=delta,
        open_interest=open_interest,
        volume=volume,
        option_standard_type=standard_type,
        stock_owner=stock_owner,
        stock_type=stock_type,
        sec_status=sec_status,
        suspension=suspension,
        currency=_enum(currency),
        chain_multiplier=chain_multiplier,
        snapshot_multiplier=snapshot_multiplier,
        multiplier=multiplier,
    )
