from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from scripts.feishu_bitable import merge_note, parse_note_kv, safe_float
from scripts.opend_utils import resolve_underlier_alias


BUY_TO_CLOSE = "buy_to_close"
EXPIRE_AUTO_CLOSE = "expire_auto_close"


def norm_symbol(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    return resolve_underlier_alias(raw)


def _compact_choice(value: Any) -> str:
    return (
        str(value or "")
        .strip()
        .replace("（", "(")
        .replace("）", ")")
        .replace(" ", "")
        .replace("\u3000", "")
        .replace("-", "_")
        .lower()
    )


def _normalize_choice(value: Any, aliases: dict[str, str], field_name: str, *, strict: bool = False) -> str:
    raw = str(value or "").strip()
    compact = _compact_choice(value)
    if compact in aliases:
        return aliases[compact]
    if strict:
        allowed = sorted(set(aliases.values()))
        raise ValueError(f"{field_name} must be one of: {', '.join(allowed)}")
    return raw.lower()


def normalize_broker(value: str | None) -> str:
    raw = str(value or "").strip()
    compact = (
        raw.replace("（", "(")
        .replace("）", ")")
        .replace(" ", "")
        .replace("\u3000", "")
        .lower()
    )
    futu_aliases = {
        "富途",
        "富途证券",
        "富途证券(香港)",
        "富途證券",
        "富途證券(香港)",
        "富途牛牛",
        "futu",
        "futuhk",
        "futusecurities",
        "futusecurities(hongkong)",
        "futusecuritieshk",
    }
    if compact in futu_aliases or compact.startswith("futu"):
        return "富途"
    return raw


def normalize_account(value: Any) -> str:
    return str(value or "").strip().lower()


def normalize_option_type(value: Any, *, strict: bool = False) -> str:
    return _normalize_choice(
        value,
        {
            "put": "put",
            "p": "put",
            "认沽": "put",
            "認沽": "put",
            "沽": "put",
            "call": "call",
            "c": "call",
            "认购": "call",
            "認購": "call",
            "购": "call",
            "購": "call",
        },
        "option_type",
        strict=strict,
    )


def normalize_side(value: Any, *, strict: bool = False) -> str:
    return _normalize_choice(
        value,
        {
            "short": "short",
            "sell": "short",
            "sell_to_open": "short",
            "selltoopen": "short",
            "sto": "short",
            "卖出": "short",
            "賣出": "short",
            "卖开": "short",
            "賣開": "short",
            "long": "long",
            "buy": "long",
            "buy_to_open": "long",
            "buytoopen": "long",
            "bto": "long",
            "买入": "long",
            "買入": "long",
            "买开": "long",
            "買開": "long",
        },
        "side",
        strict=strict,
    )


def normalize_status(value: Any, *, strict: bool = False) -> str:
    return _normalize_choice(
        value,
        {
            "open": "open",
            "opened": "open",
            "active": "open",
            "持仓": "open",
            "未平": "open",
            "未平仓": "open",
            "未平倉": "open",
            "close": "close",
            "closed": "close",
            "平仓": "close",
            "平倉": "close",
            "已平": "close",
            "已平仓": "close",
            "已平倉": "close",
        },
        "status",
        strict=strict,
    )


def normalize_currency(value: Any, *, strict: bool = False) -> str:
    raw = str(value or "").strip().upper()
    compact = _compact_choice(raw)
    aliases = {
        "USD": "USD",
        "US$": "USD",
        "$": "USD",
        "美元": "USD",
        "HKD": "HKD",
        "HK$": "HKD",
        "港币": "HKD",
        "港幣": "HKD",
        "CNY": "CNY",
        "CNH": "CNY",
        "RMB": "CNY",
        "人民币": "CNY",
        "人民幣": "CNY",
    }
    if raw in aliases:
        return aliases[raw]
    if compact in aliases:
        return aliases[compact]
    if strict:
        raise ValueError("currency must be one of: CNY, HKD, USD")
    return raw


def normalize_close_type(value: Any, *, strict: bool = False) -> str:
    return _normalize_choice(
        value,
        {
            BUY_TO_CLOSE: BUY_TO_CLOSE,
            "btc": BUY_TO_CLOSE,
            "buytoclose": BUY_TO_CLOSE,
            "buyclose": BUY_TO_CLOSE,
            "买入平仓": BUY_TO_CLOSE,
            "買入平倉": BUY_TO_CLOSE,
            "买平": BUY_TO_CLOSE,
            "買平": BUY_TO_CLOSE,
            EXPIRE_AUTO_CLOSE: EXPIRE_AUTO_CLOSE,
            "expireautoclose": EXPIRE_AUTO_CLOSE,
            "expired": EXPIRE_AUTO_CLOSE,
            "expire": EXPIRE_AUTO_CLOSE,
            "到期": EXPIRE_AUTO_CLOSE,
            "到期平仓": EXPIRE_AUTO_CLOSE,
            "到期平倉": EXPIRE_AUTO_CLOSE,
            "到期自动平仓": EXPIRE_AUTO_CLOSE,
            "到期自動平倉": EXPIRE_AUTO_CLOSE,
        },
        "close_type",
        strict=strict,
    )


def now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def parse_exp_to_ms(exp_ymd: str | None) -> int | None:
    try:
        if not exp_ymd:
            return None
        y, m, d = map(int, str(exp_ymd).split("-"))
        return int(datetime(y, m, d, tzinfo=timezone.utc).timestamp() * 1000)
    except Exception:
        return None


def exp_ms_to_datetime(value: Any) -> datetime | None:
    try:
        if value in (None, "", 0):
            return None
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)
    except Exception:
        return None


def calc_cash_secured(strike: float, multiplier: float, contracts: int | float) -> float:
    return float(strike) * float(multiplier) * int(float(contracts))


def guess_multiplier(symbol: str) -> float | None:
    try:
        from pathlib import Path as _Path
        from scripts.multiplier_cache import resolve_multiplier

        return float(
            resolve_multiplier(
                repo_base=_Path(__file__).resolve().parents[2],
                symbol=norm_symbol(symbol),
                allow_opend_refresh=False,
            )
            or 0
        ) or None
    except Exception:
        return None


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
    exp_dt = exp_ms_to_datetime(exp_ms)
    return exp_dt.date().isoformat() if exp_dt is not None else None


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
    currency: str
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
    currency = normalize_currency(cmd.currency, strict=True)
    contracts = int(cmd.contracts)
    if contracts <= 0:
        raise ValueError("contracts must be > 0")

    multiplier = cmd.multiplier
    cash_secured = None
    if side == "short" and option_type == "put":
        if cmd.strike is None:
            raise ValueError("short put requires strike")
        if multiplier is None:
            multiplier = guess_multiplier(sym)
        if multiplier is None:
            raise ValueError("short put requires multiplier")
        cash_secured = calc_cash_secured(float(cmd.strike), float(multiplier), contracts)

    underlying_locked = cmd.underlying_share_locked
    if side == "short" and option_type == "call" and underlying_locked is None:
        m = multiplier if multiplier is not None else guess_multiplier(sym)
        if m is None:
            m = 100
        underlying_locked = int(float(m) * contracts)

    note_kv: dict[str, str] = {}
    if cmd.strike is not None:
        note_kv["strike"] = str(cmd.strike)
    if multiplier is not None:
        note_kv["multiplier"] = str(multiplier)
    if cmd.expiration_ymd:
        note_kv["exp"] = str(cmd.expiration_ymd)
    if cmd.premium_per_share is not None:
        note_kv["premium_per_share"] = str(cmd.premium_per_share)

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
    exp_ms = parse_exp_to_ms(cmd.expiration_ymd)
    if exp_ms is not None:
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


def upsert_note_kv(note: str | None, kv: dict[str, str]) -> str:
    raw = str(note or "").strip()
    pairs: list[tuple[str, str]] = []
    replaced_keys = {str(key).strip() for key, value in kv.items() if str(key).strip() and value not in (None, "")}
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

    next_strike = strike if strike is not None else safe_float(fields.get("strike"))
    next_multiplier = multiplier
    if next_multiplier is None:
        next_multiplier = safe_float(fields.get("multiplier"))
    if next_multiplier is None:
        next_multiplier = safe_float(parse_note_kv(fields.get("note") or "", "multiplier"))
    if next_multiplier is None and side == "short":
        next_multiplier = guess_multiplier(symbol)

    next_expiration_ymd = expiration_ymd or parse_note_kv(fields.get("note") or "", "exp") or None
    if expiration_ymd is not None:
        exp_ms = parse_exp_to_ms(expiration_ymd)
        if exp_ms is None:
            raise ValueError("expiration_ymd must be YYYY-MM-DD")
    else:
        exp_ms = fields.get("expiration")

    note_updates: dict[str, str] = {}
    patch: dict[str, Any] = {"last_action_at": int(as_of_ms or now_ms())}

    if contracts is not None:
        patch["contracts"] = next_contracts
        patch["contracts_closed"] = closed_contracts
        patch["contracts_open"] = 0 if status == "close" else max(0, next_contracts - closed_contracts)
    if strike is not None:
        if next_strike is None:
            raise ValueError("strike must be numeric")
        patch["strike"] = float(next_strike)
        note_updates["strike"] = str(next_strike)
    if premium_per_share is not None:
        patch["premium"] = float(premium_per_share)
        note_updates["premium_per_share"] = str(premium_per_share)
    if multiplier is not None:
        if next_multiplier is None or float(next_multiplier) <= 0:
            raise ValueError("multiplier must be > 0")
        if float(next_multiplier).is_integer():
            patch["multiplier"] = int(float(next_multiplier))
        else:
            patch["multiplier"] = float(next_multiplier)
        note_updates["multiplier"] = str(multiplier)
    if expiration_ymd is not None:
        patch["expiration"] = int(exp_ms)  # type: ignore[arg-type]
        note_updates["exp"] = expiration_ymd
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


def build_buy_to_close_patch(
    fields: dict[str, Any],
    *,
    contracts_to_close: int,
    close_price: float | None = None,
    close_reason: str = "manual_buy_to_close",
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

    patch: dict[str, Any] = {
        "contracts_open": remaining,
        "contracts_closed": min(closed, total) if total > 0 else closed,
        "last_action_at": ts,
        "close_type": BUY_TO_CLOSE,
        "close_reason": str(close_reason or "manual_buy_to_close"),
    }
    if close_price is not None:
        patch["close_price"] = float(close_price)
    if remaining == 0:
        patch["status"] = "close"
        patch["closed_at"] = ts
    else:
        patch["status"] = "open"
    return patch


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
