from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from domain.domain.option_position_identity import (
    BUY_TO_CLOSE,
    EXPIRE_AUTO_CLOSE,
    SELL_TO_CLOSE,
    exp_ms_to_datetime,
    exp_ms_to_ymd,
    infer_currency_from_symbol,
    norm_symbol,
    normalize_account,
    normalize_broker,
    normalize_close_type,
    normalize_currency,
    normalize_option_type,
    normalize_side,
    normalize_status,
    now_ms,
    parse_exp_to_ms,
    resolve_open_currency,
)


class _Unset:
    pass


_UNSET = _Unset()
_PatchValue = int | float | str | None | _Unset

POSITION_LOT_PATCH_FIELDS = (
    "contracts_open",
    "contracts_closed",
    "last_action_at",
    "close_type",
    "close_reason",
    "close_price",
    "status",
    "closed_at",
    "contracts",
    "strike",
    "premium",
    "multiplier",
    "expiration",
    "expiration_ymd",
    "opened_at",
    "cash_secured_amount",
    "underlying_share_locked",
    "position_id",
    "currency",
    "note",
)


def safe_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except Exception:
        return None


def parse_note_kv(note: str, key: str) -> str:
    if not note:
        return ""
    for part in str(note).replace(",", ";").split(";"):
        part = part.strip()
        if not part:
            continue
        if part.startswith(f"{key}="):
            return part.split("=", 1)[1].strip()
    return ""


def merge_note(note: str | None, kv: dict[str, str]) -> str:
    parts: list[str] = []
    base = str(note or "").strip()
    if base:
        parts.append(base)
    for key, value in kv.items():
        if value in (None, ""):
            continue
        parts.append(f"{key}={value}")
    return ";".join(parts)


def calc_cash_secured(strike: float, multiplier: float, contracts: int | float) -> float:
    return float(strike) * float(multiplier) * int(float(contracts))


def effective_contracts(fields: dict[str, Any]) -> int:
    v = safe_float(fields.get("contracts"))
    return max(0, int(v or 0))


def effective_contracts_open(fields: dict[str, Any]) -> int:
    status = normalize_status(fields.get("status"))
    if status == "close":
        return 0
    open_v = safe_float(fields.get("contracts_open"))
    if open_v is not None:
        return max(0, int(open_v))
    closed_v = safe_float(fields.get("contracts_closed"))
    total = effective_contracts(fields)
    if closed_v is not None:
        return max(0, total - int(closed_v))
    return total


def effective_contracts_closed(fields: dict[str, Any]) -> int:
    closed_v = safe_float(fields.get("contracts_closed"))
    if closed_v is not None:
        return max(0, int(closed_v))
    total = effective_contracts(fields)
    open_v = effective_contracts_open(fields)
    return max(0, total - open_v)


def effective_expiration(fields: dict[str, Any]) -> tuple[int | None, str]:
    exp_ms = fields.get("expiration")
    parsed_exp = exp_ms_to_datetime(exp_ms)
    if parsed_exp is not None:
        return int(parsed_exp.timestamp() * 1000), "expiration"
    exp_note = parse_note_kv(fields.get("note") or "", "exp")
    exp_ms2 = parse_exp_to_ms(exp_note)
    if exp_ms2 is not None:
        return exp_ms2, "note.exp"
    return None, "none"


def effective_expiration_ymd(fields: dict[str, Any]) -> str | None:
    exp_ms, _source = effective_expiration(fields)
    return exp_ms_to_ymd(exp_ms)


def effective_strike(fields: dict[str, Any]) -> float | None:
    strike = safe_float(fields.get("strike"))
    if strike is not None:
        return float(strike)
    return safe_float(parse_note_kv(fields.get("note") or "", "strike"))


def effective_multiplier(fields: dict[str, Any]) -> float | None:
    multiplier = safe_float(fields.get("multiplier"))
    if multiplier is not None:
        return float(multiplier)
    return safe_float(parse_note_kv(fields.get("note") or "", "multiplier"))


def _fmt_strike(value: float | None) -> str:
    if value is None:
        return "NA"
    if float(value).is_integer():
        return str(int(value))
    return str(value).replace(".", "p")


def build_position_id(
    *,
    symbol: str,
    expiration_ymd: str | None,
    strike: float | None,
    option_type: str,
    side: str,
    contracts: int,
) -> str:
    sym = norm_symbol(symbol)
    exp_compact = str(expiration_ymd or "").replace("-", "") or "NA"
    pc = "P" if str(option_type).strip().lower() == "put" else "C"
    base = sym.replace(".", "_")
    return f"{base}_{exp_compact}_{_fmt_strike(strike)}{pc}_{str(side).strip().lower()}"


@dataclass(frozen=True)
class OpenPositionCommand:
    broker: str
    account: str
    symbol: str
    option_type: str
    side: str
    contracts: int
    currency: str | None
    strike: float | None = None
    multiplier: float | None = None
    expiration_ymd: str | None = None
    premium_per_share: float | None = None
    underlying_share_locked: int | None = None
    note: str | None = None
    opened_at_ms: int | None = None


@dataclass(frozen=True)
class PositionLotFields:
    position_id: str
    broker: str
    account: str
    symbol: str
    option_type: str
    side: str
    contracts: int
    contracts_open: int
    contracts_closed: int
    currency: str
    status: str
    note: str | None
    opened_at: int
    last_action_at: int
    strike: float
    expiration: int
    premium: float | None = None
    multiplier: int | float | None = None
    underlying_share_locked: int | None = None
    cash_secured_amount: float | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "position_id": self.position_id,
            "broker": self.broker,
            "account": self.account,
            "symbol": self.symbol,
            "option_type": self.option_type,
            "side": self.side,
            "contracts": self.contracts,
            "contracts_open": self.contracts_open,
            "contracts_closed": self.contracts_closed,
            "currency": self.currency,
            "status": self.status,
            "note": self.note,
            "opened_at": self.opened_at,
            "last_action_at": self.last_action_at,
            "strike": self.strike,
            "expiration": self.expiration,
        }
        if self.premium is not None:
            payload["premium"] = self.premium
        if self.multiplier is not None:
            payload["multiplier"] = self.multiplier
        if self.underlying_share_locked is not None:
            payload["underlying_share_locked"] = self.underlying_share_locked
        if self.cash_secured_amount is not None:
            payload["cash_secured_amount"] = self.cash_secured_amount
        return payload


@dataclass(frozen=True)
class PositionLotPatch:
    contracts_open: _PatchValue = _UNSET
    contracts_closed: _PatchValue = _UNSET
    last_action_at: _PatchValue = _UNSET
    close_type: _PatchValue = _UNSET
    close_reason: _PatchValue = _UNSET
    close_price: _PatchValue = _UNSET
    status: _PatchValue = _UNSET
    closed_at: _PatchValue = _UNSET
    contracts: _PatchValue = _UNSET
    strike: _PatchValue = _UNSET
    premium: _PatchValue = _UNSET
    multiplier: _PatchValue = _UNSET
    expiration: _PatchValue = _UNSET
    expiration_ymd: _PatchValue = _UNSET
    opened_at: _PatchValue = _UNSET
    cash_secured_amount: _PatchValue = _UNSET
    underlying_share_locked: _PatchValue = _UNSET
    position_id: _PatchValue = _UNSET
    currency: _PatchValue = _UNSET
    note: _PatchValue = _UNSET

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        for key in POSITION_LOT_PATCH_FIELDS:
            value = getattr(self, key)
            if value is _UNSET:
                continue
            payload[key] = value
        return payload

    def has(self, key: str) -> bool:
        if key not in POSITION_LOT_PATCH_FIELDS:
            raise KeyError(f"unsupported position lot patch field: {key}")
        return getattr(self, key) is not _UNSET

    def value(self, key: str) -> int | float | str | None:
        if key not in POSITION_LOT_PATCH_FIELDS:
            raise KeyError(f"unsupported position lot patch field: {key}")
        value = getattr(self, key)
        if value is _UNSET:
            raise KeyError(f"position lot patch field is unset: {key}")
        return value


def decode_position_lot_patch(payload: Any) -> PositionLotPatch:
    if not isinstance(payload, dict) or not payload:
        raise ValueError("adjust event requires non-empty raw_payload.patch")
    unsupported = sorted(str(key) for key in payload if str(key) not in POSITION_LOT_PATCH_FIELDS)
    if unsupported:
        raise ValueError(f"adjust patch contains unsupported fields: {', '.join(unsupported)}")
    return PositionLotPatch(
        contracts_open=payload.get("contracts_open", _UNSET),
        contracts_closed=payload.get("contracts_closed", _UNSET),
        last_action_at=payload.get("last_action_at", _UNSET),
        close_type=payload.get("close_type", _UNSET),
        close_reason=payload.get("close_reason", _UNSET),
        close_price=payload.get("close_price", _UNSET),
        status=payload.get("status", _UNSET),
        closed_at=payload.get("closed_at", _UNSET),
        contracts=payload.get("contracts", _UNSET),
        strike=payload.get("strike", _UNSET),
        premium=payload.get("premium", _UNSET),
        multiplier=payload.get("multiplier", _UNSET),
        expiration=payload.get("expiration", _UNSET),
        expiration_ymd=payload.get("expiration_ymd", _UNSET),
        opened_at=payload.get("opened_at", _UNSET),
        cash_secured_amount=payload.get("cash_secured_amount", _UNSET),
        underlying_share_locked=payload.get("underlying_share_locked", _UNSET),
        position_id=payload.get("position_id", _UNSET),
        currency=payload.get("currency", _UNSET),
        note=payload.get("note", _UNSET),
    )


def build_position_lot_fields(cmd: OpenPositionCommand) -> PositionLotFields:
    sym = norm_symbol(cmd.symbol)
    broker = normalize_broker(cmd.broker)
    account = normalize_account(cmd.account)
    side = normalize_side(cmd.side, strict=True)
    option_type = normalize_option_type(cmd.option_type, strict=True)
    currency = resolve_open_currency(sym, cmd.currency)
    contracts = int(cmd.contracts)
    if contracts <= 0:
        raise ValueError("contracts must be > 0")

    if cmd.strike is None:
        raise ValueError(f"{option_type} option requires strike")
    exp_ms = parse_exp_to_ms(cmd.expiration_ymd)
    if exp_ms is None:
        raise ValueError(f"{option_type} option requires expiration_ymd")

    multiplier = cmd.multiplier
    cash_secured = None
    if side == "short" and option_type == "put":
        if multiplier is None:
            raise ValueError("short put requires multiplier")
        cash_secured = calc_cash_secured(float(cmd.strike), float(multiplier), contracts)

    underlying_locked = cmd.underlying_share_locked
    if side == "short" and option_type == "call" and underlying_locked is None:
        if multiplier is None:
            raise ValueError("short call requires multiplier or underlying_share_locked")
        underlying_locked = int(float(multiplier) * contracts)

    note_kv: dict[str, str] = {}

    opened_at = int(cmd.opened_at_ms or now_ms())
    normalized_multiplier = None
    if multiplier is not None:
        normalized_multiplier = int(float(multiplier)) if float(multiplier).is_integer() else float(multiplier)
    return PositionLotFields(
        position_id=build_position_id(
            symbol=sym,
            expiration_ymd=cmd.expiration_ymd,
            strike=cmd.strike,
            option_type=option_type,
            side=side,
            contracts=contracts,
        ),
        broker=broker,
        account=account,
        symbol=sym,
        option_type=option_type,
        side=side,
        contracts=contracts,
        contracts_open=contracts,
        contracts_closed=0,
        currency=currency,
        status="open",
        note=merge_note(cmd.note, note_kv) or None,
        opened_at=opened_at,
        last_action_at=opened_at,
        strike=float(cmd.strike),
        expiration=int(exp_ms),
        premium=(float(cmd.premium_per_share) if cmd.premium_per_share is not None else None),
        multiplier=normalized_multiplier,
        underlying_share_locked=(int(underlying_locked) if underlying_locked is not None else None),
        cash_secured_amount=(float(cash_secured) if cash_secured is not None else None),
    )


def build_open_fields(cmd: OpenPositionCommand) -> dict[str, Any]:
    return build_position_lot_fields(cmd).to_dict()


def upsert_note_kv(note: str | None, kv: dict[str, Any]) -> str:
    raw = str(note or "").strip()
    pairs: list[tuple[str, str]] = []
    replaced_keys = {str(key).strip() for key in kv if str(key).strip()}
    for part in raw.replace(",", ";").split(";"):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            pairs.append((part, ""))
            continue
        key, value = part.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key in replaced_keys:
            continue
        pairs.append((key, value))
    for key, value in kv.items():
        clean_key = str(key).strip()
        if not clean_key or value in (None, ""):
            continue
        pairs.append((clean_key, str(value).strip()))
    out: list[str] = []
    for key, value in pairs:
        out.append(key if value == "" else f"{key}={value}")
    return ";".join(out)


def build_open_adjustment_patch_contract(
    fields: dict[str, Any],
    *,
    contracts: int | None = None,
    strike: float | None = None,
    expiration_ymd: str | None = None,
    premium_per_share: float | None = None,
    multiplier: float | None = None,
    opened_at_ms: int | None = None,
    as_of_ms: int | None = None,
) -> PositionLotPatch:
    if all(value is None for value in (contracts, strike, expiration_ymd, premium_per_share, multiplier, opened_at_ms)):
        raise ValueError("at least one adjustment field is required")

    symbol = norm_symbol(fields.get("symbol") or "")
    option_type = normalize_option_type(fields.get("option_type"), strict=True)
    side = normalize_side(fields.get("side"), strict=True)
    status = normalize_status(fields.get("status"), strict=True)
    total_contracts = effective_contracts(fields)
    closed_contracts = effective_contracts_closed(fields)

    next_contracts = int(contracts) if contracts is not None else total_contracts
    if next_contracts <= 0:
        raise ValueError("contracts must be > 0")
    if closed_contracts > next_contracts:
        raise ValueError(f"contracts must be >= contracts_closed: {next_contracts} < {closed_contracts}")
    if status == "close" and contracts is not None and next_contracts != closed_contracts:
        raise ValueError("cannot change contracts on a closed lot unless it equals contracts_closed")

    next_strike = strike if strike is not None else effective_strike(fields)
    next_multiplier = multiplier
    if next_multiplier is None:
        next_multiplier = effective_multiplier(fields)

    next_expiration_ymd = expiration_ymd or effective_expiration_ymd(fields) or None
    parsed_exp_ms: int | None = None
    if expiration_ymd is not None:
        parsed_exp_ms = parse_exp_to_ms(expiration_ymd)
        if parsed_exp_ms is None:
            raise ValueError("expiration_ymd must be YYYY-MM-DD")

    note_updates: dict[str, Any] = {}
    patch_contracts: _PatchValue = _UNSET
    patch_contracts_open: _PatchValue = _UNSET
    patch_contracts_closed: _PatchValue = _UNSET
    patch_strike: _PatchValue = _UNSET
    patch_premium: _PatchValue = _UNSET
    patch_multiplier: _PatchValue = _UNSET
    patch_expiration: _PatchValue = _UNSET
    patch_opened_at: _PatchValue = _UNSET
    patch_cash_secured: _PatchValue = _UNSET
    patch_underlying_locked: _PatchValue = _UNSET
    patch_position_id: _PatchValue = _UNSET
    patch_note: _PatchValue = _UNSET

    if contracts is not None:
        patch_contracts = next_contracts
        patch_contracts_closed = closed_contracts
        patch_contracts_open = 0 if status == "close" else max(0, next_contracts - closed_contracts)
    if strike is not None:
        if next_strike is None:
            raise ValueError("strike must be numeric")
        patch_strike = float(next_strike)
        note_updates["strike"] = None
    if premium_per_share is not None:
        patch_premium = float(premium_per_share)
        note_updates["premium_per_share"] = None
    if multiplier is not None:
        if next_multiplier is None or float(next_multiplier) <= 0:
            raise ValueError("multiplier must be > 0")
        if float(next_multiplier).is_integer():
            patch_multiplier = int(float(next_multiplier))
        else:
            patch_multiplier = float(next_multiplier)
        note_updates["multiplier"] = None
    if expiration_ymd is not None:
        assert parsed_exp_ms is not None
        patch_expiration = int(parsed_exp_ms)
        note_updates["exp"] = None
    if opened_at_ms is not None:
        patch_opened_at = int(opened_at_ms)

    if side == "short" and option_type == "put" and (
        contracts is not None or strike is not None or multiplier is not None
    ):
        if next_strike is None:
            raise ValueError("short put adjustment requires strike")
        if next_multiplier is None or float(next_multiplier) <= 0:
            raise ValueError("short put adjustment requires multiplier")
        patch_cash_secured = float(calc_cash_secured(float(next_strike), float(next_multiplier), next_contracts))

    if side == "short" and option_type == "call" and (contracts is not None or multiplier is not None):
        if next_multiplier is None or float(next_multiplier) <= 0:
            raise ValueError("short call adjustment requires multiplier")
        patch_underlying_locked = int(float(next_multiplier) * int(next_contracts))

    if any(value is not None for value in (contracts, strike, expiration_ymd)):
        patch_position_id = build_position_id(
            symbol=symbol,
            expiration_ymd=next_expiration_ymd,
            strike=next_strike,
            option_type=option_type,
            side=side,
            contracts=next_contracts,
        )

    if note_updates:
        patch_note = upsert_note_kv(fields.get("note"), note_updates)
    return PositionLotPatch(
        contracts=patch_contracts,
        contracts_open=patch_contracts_open,
        contracts_closed=patch_contracts_closed,
        last_action_at=int(as_of_ms or now_ms()),
        strike=patch_strike,
        premium=patch_premium,
        multiplier=patch_multiplier,
        expiration=patch_expiration,
        opened_at=patch_opened_at,
        cash_secured_amount=patch_cash_secured,
        underlying_share_locked=patch_underlying_locked,
        position_id=patch_position_id,
        note=patch_note,
    )


def build_open_adjustment_patch(
    fields: dict[str, Any],
    *,
    contracts: int | None = None,
    strike: float | None = None,
    expiration_ymd: str | None = None,
    premium_per_share: float | None = None,
    multiplier: float | None = None,
    opened_at_ms: int | None = None,
    as_of_ms: int | None = None,
) -> dict[str, Any]:
    return build_open_adjustment_patch_contract(
        fields,
        contracts=contracts,
        strike=strike,
        expiration_ymd=expiration_ymd,
        premium_per_share=premium_per_share,
        multiplier=multiplier,
        opened_at_ms=opened_at_ms,
        as_of_ms=as_of_ms,
    ).to_dict()


def build_close_patch_contract(
    fields: dict[str, Any],
    *,
    contracts_to_close: int,
    close_price: float | None = None,
    close_reason: str = "manual_close",
    close_type: str | None = None,
    as_of_ms: int | None = None,
) -> PositionLotPatch:
    qty = int(contracts_to_close)
    if qty <= 0:
        raise ValueError("contracts_to_close must be > 0")
    open_qty = effective_contracts_open(fields)
    if qty > open_qty:
        raise ValueError(f"contracts_to_close exceeds open contracts: {qty} > {open_qty}")

    total = effective_contracts(fields)
    closed = effective_contracts_closed(fields) + qty
    remaining = max(0, open_qty - qty)
    ts = int(as_of_ms or now_ms())
    if close_type:
        effective_close_type = str(close_type).strip().lower()
    else:
        normalized_side = normalize_side(fields.get("side"), strict=True)
        effective_close_type = (BUY_TO_CLOSE if normalized_side == "short" else SELL_TO_CLOSE)

    close_price_value: _PatchValue = _UNSET
    if close_price is not None:
        close_price_value = float(close_price)
    if remaining == 0:
        status = "close"
        closed_at: _PatchValue = ts
    else:
        status = "open"
        closed_at = _UNSET
    return PositionLotPatch(
        contracts_open=remaining,
        contracts_closed=min(closed, total) if total > 0 else closed,
        last_action_at=ts,
        close_type=effective_close_type,
        close_reason=str(close_reason or "manual_close"),
        close_price=close_price_value,
        status=status,
        closed_at=closed_at,
    )


def build_close_patch(
    fields: dict[str, Any],
    *,
    contracts_to_close: int,
    close_price: float | None = None,
    close_reason: str = "manual_close",
    close_type: str | None = None,
    as_of_ms: int | None = None,
) -> dict[str, Any]:
    return build_close_patch_contract(
        fields,
        contracts_to_close=contracts_to_close,
        close_price=close_price,
        close_reason=close_reason,
        close_type=close_type,
        as_of_ms=as_of_ms,
    ).to_dict()


def build_buy_to_close_patch(
    fields: dict[str, Any],
    *,
    contracts_to_close: int,
    close_price: float | None = None,
    close_reason: str = "manual_buy_to_close",
    as_of_ms: int | None = None,
) -> dict[str, Any]:
    return build_close_patch(
        fields,
        contracts_to_close=contracts_to_close,
        close_price=close_price,
        close_reason=close_reason,
        close_type=BUY_TO_CLOSE,
        as_of_ms=as_of_ms,
    )


def build_expire_auto_close_patch_contract(
    fields: dict[str, Any],
    *,
    as_of_ms: int | None = None,
    close_reason: str = "expired",
    exp_source: str | None = None,
    grace_days: int | None = None,
) -> PositionLotPatch:
    open_qty = effective_contracts_open(fields)
    total = effective_contracts(fields)
    ts = int(as_of_ms or now_ms())
    note_kv = {
        "auto_close_at": datetime.fromtimestamp(ts / 1000, tz=timezone.utc).isoformat(),
        "auto_close_reason": close_reason,
        "close_reason": close_reason,
    }
    if grace_days is not None:
        note_kv["auto_close_grace_days"] = str(grace_days)
    if exp_source:
        note_kv["auto_close_exp_src"] = str(exp_source)
    return PositionLotPatch(
        contracts_open=0,
        contracts_closed=max(effective_contracts_closed(fields) + open_qty, total),
        status="close",
        closed_at=ts,
        last_action_at=ts,
        close_type=EXPIRE_AUTO_CLOSE,
        close_reason=str(close_reason or "expired"),
        note=merge_note(fields.get("note"), note_kv),
    )


def build_expire_auto_close_patch(
    fields: dict[str, Any],
    *,
    as_of_ms: int | None = None,
    close_reason: str = "expired",
    exp_source: str | None = None,
    grace_days: int | None = None,
) -> dict[str, Any]:
    return build_expire_auto_close_patch_contract(
        fields,
        as_of_ms=as_of_ms,
        close_reason=close_reason,
        exp_source=exp_source,
        grace_days=grace_days,
    ).to_dict()
