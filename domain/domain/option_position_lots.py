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


def build_open_fields(cmd: OpenPositionCommand) -> dict[str, Any]:
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
    fields: dict[str, Any] = {
        "position_id": build_position_id(
            symbol=sym,
            expiration_ymd=cmd.expiration_ymd,
            strike=cmd.strike,
            option_type=option_type,
            side=side,
            contracts=contracts,
        ),
        "broker": broker,
        "account": account,
        "symbol": sym,
        "option_type": option_type,
        "side": side,
        "contracts": contracts,
        "contracts_open": contracts,
        "contracts_closed": 0,
        "currency": currency,
        "status": "open",
        "note": merge_note(cmd.note, note_kv) or None,
        "opened_at": opened_at,
        "last_action_at": opened_at,
    }
    if cmd.strike is not None:
        fields["strike"] = float(cmd.strike)
    fields["expiration"] = int(exp_ms)
    if cmd.premium_per_share is not None:
        fields["premium"] = float(cmd.premium_per_share)
    if multiplier is not None:
        fields["multiplier"] = int(float(multiplier)) if float(multiplier).is_integer() else float(multiplier)
    if underlying_locked is not None:
        fields["underlying_share_locked"] = int(underlying_locked)
    if cash_secured is not None:
        fields["cash_secured_amount"] = float(cash_secured)
    return fields


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
    patch: dict[str, Any] = {"last_action_at": int(as_of_ms or now_ms())}

    if contracts is not None:
        patch["contracts"] = next_contracts
        patch["contracts_closed"] = closed_contracts
        patch["contracts_open"] = 0 if status == "close" else max(0, next_contracts - closed_contracts)
    if strike is not None:
        if next_strike is None:
            raise ValueError("strike must be numeric")
        patch["strike"] = float(next_strike)
        note_updates["strike"] = None
    if premium_per_share is not None:
        patch["premium"] = float(premium_per_share)
        note_updates["premium_per_share"] = None
    if multiplier is not None:
        if next_multiplier is None or float(next_multiplier) <= 0:
            raise ValueError("multiplier must be > 0")
        if float(next_multiplier).is_integer():
            patch["multiplier"] = int(float(next_multiplier))
        else:
            patch["multiplier"] = float(next_multiplier)
        note_updates["multiplier"] = None
    if expiration_ymd is not None:
        assert parsed_exp_ms is not None
        patch["expiration"] = int(parsed_exp_ms)
        note_updates["exp"] = None
    if opened_at_ms is not None:
        patch["opened_at"] = int(opened_at_ms)

    if side == "short" and option_type == "put" and (
        contracts is not None or strike is not None or multiplier is not None
    ):
        if next_strike is None:
            raise ValueError("short put adjustment requires strike")
        if next_multiplier is None or float(next_multiplier) <= 0:
            raise ValueError("short put adjustment requires multiplier")
        patch["cash_secured_amount"] = float(calc_cash_secured(float(next_strike), float(next_multiplier), next_contracts))

    if side == "short" and option_type == "call" and (contracts is not None or multiplier is not None):
        if next_multiplier is None or float(next_multiplier) <= 0:
            raise ValueError("short call adjustment requires multiplier")
        patch["underlying_share_locked"] = int(float(next_multiplier) * int(next_contracts))

    if any(value is not None for value in (contracts, strike, expiration_ymd)):
        patch["position_id"] = build_position_id(
            symbol=symbol,
            expiration_ymd=next_expiration_ymd,
            strike=next_strike,
            option_type=option_type,
            side=side,
            contracts=next_contracts,
        )

    if note_updates:
        patch["note"] = upsert_note_kv(fields.get("note"), note_updates)
    return patch


def build_close_patch(
    fields: dict[str, Any],
    *,
    contracts_to_close: int,
    close_price: float | None = None,
    close_reason: str = "manual_close",
    close_type: str | None = None,
    as_of_ms: int | None = None,
) -> dict[str, Any]:
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

    patch: dict[str, Any] = {
        "contracts_open": remaining,
        "contracts_closed": min(closed, total) if total > 0 else closed,
        "last_action_at": ts,
        "close_type": effective_close_type,
        "close_reason": str(close_reason or "manual_close"),
    }
    if close_price is not None:
        patch["close_price"] = float(close_price)
    if remaining == 0:
        patch["status"] = "close"
        patch["closed_at"] = ts
    else:
        patch["status"] = "open"
    return patch


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


def build_expire_auto_close_patch(
    fields: dict[str, Any],
    *,
    as_of_ms: int | None = None,
    close_reason: str = "expired",
    exp_source: str | None = None,
    grace_days: int | None = None,
) -> dict[str, Any]:
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
    return {
        "contracts_open": 0,
        "contracts_closed": max(effective_contracts_closed(fields) + open_qty, total),
        "status": "close",
        "closed_at": ts,
        "last_action_at": ts,
        "close_type": EXPIRE_AUTO_CLOSE,
        "close_reason": str(close_reason or "expired"),
        "note": merge_note(fields.get("note"), note_kv),
    }
