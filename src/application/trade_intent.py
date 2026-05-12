from __future__ import annotations

from dataclasses import asdict, dataclass, field
import re
from typing import Any

from domain.domain.option_position_lots import normalize_currency, normalize_option_type, normalize_side
from domain.domain.trade_contract_identity import (
    canonical_contract_symbol,
    normalize_contract_expiration,
    normalize_position_effect,
    normalize_trade_side,
)


@dataclass(frozen=True)
class TradeIntent:
    source_type: str
    source_event_id: str | None
    broker: str
    account: str | None
    symbol: str | None
    option_type: str | None
    strike: float | None
    expiration_ymd: str | None
    trade_side: str | None
    position_effect: str | None
    target_position_side: str | None
    contracts: int | None
    price: float | None
    currency: str | None
    multiplier: int | None
    trade_time_ms: int | None
    record_id: str | None = None
    raw_payload: dict[str, Any] = field(default_factory=dict)
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _norm_str(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _norm_int(value: Any) -> int | None:
    try:
        if value in (None, ""):
            return None
        return int(float(value))
    except Exception:
        return None


def _norm_float(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except Exception:
        return None


def _canonical_symbol(value: Any) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    return canonical_contract_symbol(raw) or raw.upper()


def _normalized_option_type(value: Any) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return normalize_option_type(raw)
    except Exception:
        return None


def _normalized_currency(value: Any) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return normalize_currency(raw)
    except Exception:
        return None


def _infer_trade_side_from_text(raw_text: str) -> str | None:
    raw = str(raw_text or "").strip().lower()
    if re.search(r"\b(buy|bto|btc|buy_to_open|buy_to_close|buytoclose|buytoopen)\b", raw):
        return "buy"
    if "买" in raw or "買" in raw:
        return "buy"
    if re.search(r"\b(sell|sto|stc|sell_to_open|sell_to_close|selltoclose|selltoopen)\b", raw):
        return "sell"
    if "卖" in raw or "賣" in raw:
        return "sell"
    return None


def _target_position_side(*, trade_side: str | None, position_effect: str | None, parsed_side: Any) -> str | None:
    parsed_position_side = normalize_side(parsed_side) if parsed_side not in (None, "") else None
    if position_effect == "open":
        if parsed_position_side in {"short", "long"}:
            return parsed_position_side
        if trade_side == "sell":
            return "short"
        if trade_side == "buy":
            return "long"
    if position_effect == "close":
        if trade_side == "buy":
            return "short"
        if trade_side == "sell":
            return "long"
        if parsed_position_side in {"short", "long"}:
            return parsed_position_side
    return None


def _trade_side_for_position(*, position_effect: str | None, parsed_side: Any, raw_text: str) -> str | None:
    trade_side = _infer_trade_side_from_text(raw_text)
    if trade_side in {"buy", "sell"}:
        return trade_side
    parsed_position_side = normalize_side(parsed_side) if parsed_side not in (None, "") else None
    if parsed_position_side == "short":
        return "sell" if position_effect == "open" else "buy"
    if parsed_position_side == "long":
        return "buy" if position_effect == "open" else "sell"
    return None


def trade_intent_from_manual_parse(
    parsed: dict[str, Any],
    *,
    action: str,
    raw_text: str,
    broker: str,
    record_id: str | None = None,
) -> TradeIntent:
    fields = parsed.get("parsed") if isinstance(parsed.get("parsed"), dict) else {}
    position_effect = normalize_position_effect(action)
    trade_side = _trade_side_for_position(
        position_effect=position_effect,
        parsed_side=fields.get("side"),
        raw_text=raw_text,
    )
    target_position_side = _target_position_side(
        trade_side=trade_side,
        position_effect=position_effect,
        parsed_side=fields.get("side"),
    )
    diagnostics = {
        "parser_ok": bool(parsed.get("ok")),
        "missing": list(parsed.get("missing") or []),
        "raw_side": fields.get("side"),
    }
    return TradeIntent(
        source_type="manual_text",
        source_event_id=None,
        broker=str(broker or "").strip() or "富途",
        account=_norm_str(fields.get("account")),
        symbol=_canonical_symbol(fields.get("symbol")),
        option_type=_normalized_option_type(fields.get("option_type")),
        strike=_norm_float(fields.get("strike")),
        expiration_ymd=normalize_contract_expiration(fields.get("exp")),
        trade_side=trade_side,
        position_effect=position_effect,
        target_position_side=target_position_side,
        contracts=_norm_int(fields.get("contracts")),
        price=_norm_float(fields.get("premium_per_share")),
        currency=_normalized_currency(fields.get("currency")),
        multiplier=_norm_int(fields.get("multiplier")),
        trade_time_ms=_norm_int(fields.get("fill_time_ms")),
        record_id=_norm_str(record_id),
        raw_payload={"raw": parsed.get("raw"), "parsed": fields},
        diagnostics=diagnostics,
    )


def trade_intent_from_normalized_deal(deal: Any) -> TradeIntent:
    trade_side = normalize_trade_side(getattr(deal, "side", None))
    position_effect = normalize_position_effect(getattr(deal, "position_effect", None))
    return TradeIntent(
        source_type="futu_api",
        source_event_id=_norm_str(getattr(deal, "deal_id", None)),
        broker=str(getattr(deal, "broker", None) or "富途"),
        account=_norm_str(getattr(deal, "internal_account", None)),
        symbol=_canonical_symbol(getattr(deal, "symbol", None)),
        option_type=_normalized_option_type(getattr(deal, "option_type", None)),
        strike=_norm_float(getattr(deal, "strike", None)),
        expiration_ymd=normalize_contract_expiration(getattr(deal, "expiration_ymd", None)),
        trade_side=trade_side,
        position_effect=position_effect,
        target_position_side=_target_position_side(
            trade_side=trade_side,
            position_effect=position_effect,
            parsed_side=None,
        ),
        contracts=_norm_int(getattr(deal, "contracts", None)),
        price=_norm_float(getattr(deal, "price", None)),
        currency=_normalized_currency(getattr(deal, "currency", None)),
        multiplier=_norm_int(getattr(deal, "multiplier", None)),
        trade_time_ms=_norm_int(getattr(deal, "trade_time_ms", None)),
        raw_payload=dict(getattr(deal, "raw_payload", {}) or {}),
        diagnostics=dict(getattr(deal, "normalization_diagnostics", {}) or {}),
    )
