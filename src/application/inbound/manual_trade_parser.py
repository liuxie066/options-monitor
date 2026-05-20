from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from domain.domain.symbol_identity import canonical_symbol
from src.application.account_config import DEFAULT_ACCOUNTS, normalize_accounts
from src.application.multiplier_cache import resolve_multiplier_with_source_and_diagnostics
from src.application.parse_option_message import parse_option_message_text
from src.application.symbol_aliases import symbol_aliases_from_config


_ACCOUNT_RE_TEMPLATE = r"(?<![a-z0-9_])({accounts})(?![a-z0-9_])"
_DATE_RE = re.compile(r"(?<!\d)(20\d{2})[-/.](0[1-9]|1[0-2])[-/.](0[1-9]|[12]\d|3[01])(?!\d)")
_NUMBER_RE = re.compile(r"(?<![A-Za-z0-9.])(\d+(?:\.\d+)?)(?![A-Za-z0-9.])")
_SYMBOL_RE = re.compile(r"(?<![A-Za-z0-9_.])([A-Za-z]{1,8}(?:\.[A-Za-z]{1,4})?|[A-Za-z]{2}\.\d{4,5}|\d{3,5}(?:\.HK)?|[\u4e00-\u9fff]{2,8})(?![A-Za-z0-9_.])")


def build_manual_trade_draft(
    operation_type: str,
    *,
    raw_text: str,
    accounts: list[str] | tuple[str, ...] | None,
    config_key: str | None,
    config_path: str | Path | None,
    runtime_config: dict[str, Any] | None,
    repo_base: Path,
    allow_opend_refresh: bool = False,
) -> dict[str, Any]:
    """Build a normalized manual trade draft and auditable parse diagnostics."""
    if operation_type == "manual_open":
        arguments, diagnostics = _build_open_draft(
            raw_text,
            accounts=accounts,
            config_key=config_key,
            config_path=config_path,
            runtime_config=runtime_config,
            repo_base=repo_base,
            allow_opend_refresh=allow_opend_refresh,
        )
    elif operation_type == "manual_close":
        arguments, diagnostics = _build_close_draft(
            raw_text,
            accounts=accounts,
            config_key=config_key,
            config_path=config_path,
            runtime_config=runtime_config,
            repo_base=repo_base,
            allow_opend_refresh=allow_opend_refresh,
        )
    else:
        raise ValueError(f"unsupported manual trade operation_type: {operation_type}")
    return {"arguments": arguments, "diagnostics": diagnostics}


def _build_open_draft(
    text: str,
    *,
    accounts: list[str] | tuple[str, ...] | None,
    config_key: str | None,
    config_path: str | Path | None,
    runtime_config: dict[str, Any] | None,
    repo_base: Path,
    allow_opend_refresh: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    labeled = _extract_labeled_values(text)
    fill = _extract_futu_fill_values(text, accounts=accounts)
    raw_symbol, symbol_source = _first_value_with_source(
        (labeled.get("symbol"), "labeled"),
        (fill.get("raw_symbol") or fill.get("symbol"), "fill_parser"),
        (_extract_symbol(text, accounts=accounts), "text"),
    )
    canonical = _canonicalize_symbol(raw_symbol, runtime_config=runtime_config)
    multiplier, multiplier_source, multiplier_diagnostics = _resolve_multiplier(
        symbol=canonical or raw_symbol,
        explicit_multiplier=(
            _parse_float_value(labeled, ("multiplier",))
            or fill.get("multiplier")
            or _extract_after_label(text, ("multiplier", "乘数"))
        ),
        runtime_config=runtime_config,
        config_path=config_path,
        repo_base=repo_base,
        allow_opend_refresh=allow_opend_refresh,
    )
    args: dict[str, Any] = {
        "account": labeled.get("account") or fill.get("account") or _extract_account(text, accounts=accounts),
        "symbol": canonical or raw_symbol,
        "option_type": _parse_option_type(str(labeled.get("option_type") or "")) or fill.get("option_type") or _parse_option_type(text),
        "side": _parse_position_side(str(labeled.get("side") or "")) or fill.get("side") or _parse_position_side(text),
        "contracts": _parse_int_value(labeled, ("contracts", "qty")) or fill.get("contracts") or _extract_contracts(text),
        "strike": _parse_float_value(labeled, ("strike",)) or fill.get("strike") or _extract_after_label(text, ("strike", "行权价")),
        "expiration_ymd": _parse_date(str(labeled.get("expiration_ymd") or labeled.get("exp") or "")) or fill.get("expiration_ymd") or _parse_date(text),
        "multiplier": multiplier,
        "premium_per_share": (
            _parse_float_value(labeled, ("premium_per_share", "premium"))
            or fill.get("premium_per_share")
            or _extract_after_label(text, ("premium", "权利金"))
        ),
    }
    locked = _parse_int_value(labeled, ("underlying_share_locked", "locked"))
    if locked is not None:
        args["underlying_share_locked"] = locked
    currency = labeled.get("currency") or fill.get("currency")
    if currency:
        args["currency"] = str(currency).upper()
    broker = fill.get("broker")
    if broker:
        args["broker"] = str(broker)
    opened_at_ms = fill.get("fill_time_ms")
    if opened_at_ms is not None:
        args["opened_at_ms"] = opened_at_ms
    note = labeled.get("note")
    if note:
        args["note"] = str(note)
    args = _compact_args(args)
    diagnostics = _base_diagnostics(
        operation_type="manual_open",
        text=text,
        accounts=accounts,
        config_key=config_key,
        config_path=config_path,
        raw_symbol=raw_symbol,
        canonical_symbol=canonical,
        symbol_source=symbol_source,
        fill=fill,
        trade_side_raw=fill.get("side") or labeled.get("side") or _parse_position_side(text),
        position_side=args.get("side"),
        multiplier=multiplier,
        multiplier_source=multiplier_source,
        multiplier_diagnostics=multiplier_diagnostics,
        allow_opend_refresh=allow_opend_refresh,
    )
    diagnostics["missing_fields"] = _missing_fields(
        args,
        ("account", "symbol", "option_type", "side", "contracts", "strike", "expiration_ymd", "multiplier", "premium_per_share"),
    )
    return args, diagnostics


def _build_close_draft(
    text: str,
    *,
    accounts: list[str] | tuple[str, ...] | None,
    config_key: str | None,
    config_path: str | Path | None,
    runtime_config: dict[str, Any] | None,
    repo_base: Path,
    allow_opend_refresh: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    labeled = _extract_labeled_values(text)
    fill = _extract_futu_fill_values(text, accounts=accounts)
    raw_symbol, symbol_source = _first_value_with_source(
        (labeled.get("symbol"), "labeled"),
        (fill.get("raw_symbol") or fill.get("symbol"), "fill_parser"),
        (_extract_symbol(text, accounts=accounts), "text"),
    )
    canonical = _canonicalize_symbol(raw_symbol, runtime_config=runtime_config)
    trade_side_raw = fill.get("side") or labeled.get("side") or _parse_position_side(text)
    position_side = (
        _parse_position_side(str(labeled.get("side") or ""))
        or _close_position_side_from_fill_side(str(fill.get("side") or ""))
        or _parse_position_side(text)
    )
    args: dict[str, Any] = {
        "record_id": labeled.get("record_id") or _extract_record_id(text),
        "account": labeled.get("account") or fill.get("account") or _extract_account(text, accounts=accounts),
        "symbol": canonical or raw_symbol,
        "option_type": _parse_option_type(str(labeled.get("option_type") or "")) or fill.get("option_type") or _parse_option_type(text),
        "side": position_side,
        "contracts_to_close": _parse_int_value(labeled, ("contracts_to_close", "contracts", "qty")) or fill.get("contracts") or _extract_contracts(text),
        "strike": _parse_float_value(labeled, ("strike",)) or fill.get("strike") or _extract_after_label(text, ("strike", "行权价")),
        "expiration_ymd": _parse_date(str(labeled.get("expiration_ymd") or labeled.get("exp") or "")) or fill.get("expiration_ymd") or _parse_date(text),
        "close_price": (
            _parse_float_value(labeled, ("close_price", "close"))
            or fill.get("premium_per_share")
            or _extract_after_label(text, ("close", "平仓价", "价格"))
        ),
    }
    broker = fill.get("broker")
    if broker:
        args["broker"] = str(broker)
    as_of_ms = fill.get("fill_time_ms")
    if as_of_ms is not None:
        args["as_of_ms"] = as_of_ms
    close_reason = labeled.get("close_reason")
    if close_reason:
        args["close_reason"] = str(close_reason)
    args = _compact_args(args)
    diagnostics = _base_diagnostics(
        operation_type="manual_close",
        text=text,
        accounts=accounts,
        config_key=config_key,
        config_path=config_path,
        raw_symbol=raw_symbol,
        canonical_symbol=canonical,
        symbol_source=symbol_source,
        fill=fill,
        trade_side_raw=trade_side_raw,
        position_side=position_side,
        multiplier=None,
        multiplier_source=None,
        multiplier_diagnostics={"attempted_sources": []},
        allow_opend_refresh=allow_opend_refresh,
    )
    required = (
        ("contracts_to_close", "close_price")
        if args.get("record_id")
        else ("account", "symbol", "option_type", "side", "contracts_to_close", "strike", "expiration_ymd", "close_price")
    )
    diagnostics["missing_fields"] = _missing_fields(args, required)
    return args, diagnostics


def _base_diagnostics(
    *,
    operation_type: str,
    text: str,
    accounts: list[str] | tuple[str, ...] | None,
    config_key: str | None,
    config_path: str | Path | None,
    raw_symbol: Any,
    canonical_symbol: str | None,
    symbol_source: str | None,
    fill: dict[str, Any],
    trade_side_raw: Any,
    position_side: Any,
    multiplier: Any,
    multiplier_source: str | None,
    multiplier_diagnostics: dict[str, Any],
    allow_opend_refresh: bool,
) -> dict[str, Any]:
    return {
        "operation_type": operation_type,
        "raw_symbol": raw_symbol,
        "canonical_symbol": canonical_symbol,
        "symbol_source": symbol_source,
        "multiplier": multiplier,
        "multiplier_source": multiplier_source,
        "multiplier_resolution_attempts": multiplier_diagnostics.get("attempted_sources") or [],
        "multiplier_resolution_message": multiplier_diagnostics.get("message"),
        "multiplier_cache_path": multiplier_diagnostics.get("cache_path"),
        "fill_parser_source": "futu_fill_alert" if fill else "manual_fields",
        "fill_time_ms": fill.get("fill_time_ms"),
        "trade_side_raw": trade_side_raw,
        "position_side": position_side,
        "config_key": config_key,
        "config_path": str(config_path) if config_path else None,
        "accounts": list(normalize_accounts(accounts, fallback=DEFAULT_ACCOUNTS)),
        "multiplier_source_policy": {
            "mode": "cache_only" if not allow_opend_refresh else "cache_opend",
            "allow_opend_refresh": bool(allow_opend_refresh),
        },
        "raw_text": text,
    }


def _extract_futu_fill_values(text: str, *, accounts: list[str] | tuple[str, ...] | None) -> dict[str, Any]:
    if "成交提醒" not in text and "$" not in text:
        return {}
    parsed = parse_option_message_text(text, accounts=accounts, resolve_multiplier=False)
    values = parsed.get("parsed")
    if not isinstance(values, dict):
        return {}
    symbol = values.get("symbol")
    if not symbol:
        return {}
    out: dict[str, Any] = {
        "raw_symbol": values.get("underlying") or symbol,
        "symbol": symbol,
        "expiration_ymd": values.get("exp"),
        "option_type": values.get("option_type"),
        "side": values.get("side"),
        "contracts": values.get("contracts"),
        "strike": values.get("strike"),
        "premium_per_share": values.get("premium_per_share"),
        "account": values.get("account"),
        "currency": values.get("currency"),
        "broker": values.get("market"),
        "fill_time_ms": values.get("fill_time_ms"),
    }
    return {key: value for key, value in out.items() if value not in (None, "")}


def _resolve_multiplier(
    *,
    symbol: Any,
    explicit_multiplier: Any,
    runtime_config: dict[str, Any] | None,
    config_path: str | Path | None,
    repo_base: Path,
    allow_opend_refresh: bool,
) -> tuple[float | None, str | None, dict[str, Any]]:
    multiplier, source, diagnostics = resolve_multiplier_with_source_and_diagnostics(
        repo_base=repo_base,
        symbol=str(symbol or "").strip() or None,
        multiplier=explicit_multiplier,
        config_path=config_path,
        allow_opend_refresh=allow_opend_refresh,
        config=runtime_config,
    )
    return (float(multiplier) if multiplier else None), source, diagnostics


def _canonicalize_symbol(symbol: Any, *, runtime_config: dict[str, Any] | None) -> str | None:
    text = str(symbol or "").strip()
    if not text:
        return None
    aliases = symbol_aliases_from_config(runtime_config)
    return canonical_symbol(text, symbol_aliases=aliases) or text


def _extract_account(text: str, *, accounts: list[str] | tuple[str, ...] | None) -> str | None:
    candidates = normalize_accounts(accounts, fallback=DEFAULT_ACCOUNTS)
    if not candidates:
        return None
    pattern = _ACCOUNT_RE_TEMPLATE.format(accounts="|".join(re.escape(account) for account in candidates))
    match = re.search(pattern, text, flags=re.IGNORECASE)
    return match.group(1).lower() if match else None


def _extract_labeled_values(text: str) -> dict[str, str]:
    aliases = {
        "account": "account",
        "账户": "account",
        "symbol": "symbol",
        "标的": "symbol",
        "type": "option_type",
        "option_type": "option_type",
        "side": "side",
        "方向": "side",
        "strike": "strike",
        "行权价": "strike",
        "exp": "exp",
        "expiration": "expiration_ymd",
        "expiration_ymd": "expiration_ymd",
        "到期日": "expiration_ymd",
        "contracts": "contracts",
        "contracts_to_close": "contracts_to_close",
        "qty": "qty",
        "数量": "contracts",
        "multiplier": "multiplier",
        "乘数": "multiplier",
        "locked": "locked",
        "locked_shares": "underlying_share_locked",
        "premium": "premium",
        "权利金": "premium",
        "close": "close",
        "close_price": "close_price",
        "record_id": "record_id",
        "currency": "currency",
        "note": "note",
        "close_reason": "close_reason",
    }
    out: dict[str, str] = {}
    for match in re.finditer(r"([A-Za-z_][A-Za-z0-9_]*|[\u4e00-\u9fff]{2,8})\s*[:=：]\s*([^\s,，]+)", text):
        key = aliases.get(match.group(1).strip().lower()) or aliases.get(match.group(1).strip())
        if key:
            out[key] = match.group(2).strip()
    return out


def _extract_symbol(text: str, *, accounts: list[str] | tuple[str, ...] | None) -> str | None:
    skip = {
        "记录",
        "记录开仓",
        "记录平仓",
        "开仓",
        "平仓",
        "确认记录",
        "取消记录",
        "成交提醒",
        "short",
        "long",
        "sell",
        "buy",
        "put",
        "call",
        "strike",
        "exp",
        "premium",
        "multiplier",
        "close",
        "record_id",
    }
    skip.update(normalize_accounts(accounts, fallback=DEFAULT_ACCOUNTS))
    for match in _SYMBOL_RE.finditer(text):
        raw = match.group(1).strip()
        if not raw:
            continue
        lowered = raw.lower()
        if lowered in skip or _DATE_RE.fullmatch(raw) or raw.startswith("in_"):
            continue
        if raw.isdigit() and len(raw) < 3:
            continue
        return raw
    return None


def _extract_record_id(text: str) -> str | None:
    for match in re.finditer(r"\b(lot|evt|rec|record)[A-Za-z0-9_.:-]*\b", text, flags=re.IGNORECASE):
        token = match.group(0)
        if token.lower() not in {"lot", "evt", "rec", "record"}:
            return token
    return None


def _parse_date(text: str) -> str | None:
    match = _DATE_RE.search(text)
    return f"{match.group(1)}-{match.group(2)}-{match.group(3)}" if match else None


def _parse_option_type(text: str) -> str | None:
    lower = text.lower()
    if "put" in lower or "看跌" in text or "沽" in text:
        return "put"
    if "call" in lower or "看涨" in text or "购" in text:
        return "call"
    return None


def _parse_position_side(text: str) -> str | None:
    lower = text.lower()
    if "short" in lower or "sell" in lower or "卖出" in text:
        return "short"
    if "long" in lower or "buy" in lower or "买入" in text:
        return "long"
    return None


def _close_position_side_from_fill_side(fill_side: str) -> str | None:
    if fill_side == "long":
        return "short"
    if fill_side == "short":
        return "long"
    return None


def _parse_int_value(values: dict[str, str], keys: tuple[str, ...]) -> int | None:
    for key in keys:
        raw = values.get(key)
        if raw not in (None, ""):
            return int(float(str(raw)))
    return None


def _parse_float_value(values: dict[str, str], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        raw = values.get(key)
        if raw not in (None, ""):
            return float(str(raw))
    return None


def _extract_contracts(text: str) -> int | None:
    match = re.search(r"(?<!\d)(\d+)\s*(?:张|手|份|contracts?|合约)(?![A-Za-z])", text, flags=re.IGNORECASE)
    if match:
        return int(match.group(1))
    return None


def _extract_after_label(text: str, labels: tuple[str, ...]) -> float | None:
    for label in labels:
        pattern = rf"{re.escape(label)}\s*[:=：]?\s*{_NUMBER_RE.pattern}"
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return float(match.group(1))
    return None


def _first_value_with_source(*items: tuple[Any, str]) -> tuple[Any, str | None]:
    for value, source in items:
        if value not in (None, ""):
            return value, source
    return None, None


def _missing_fields(args: dict[str, Any], required: tuple[str, ...]) -> list[str]:
    return [key for key in required if args.get(key) in (None, "")]


def _compact_args(args: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in args.items() if value not in (None, "")}
