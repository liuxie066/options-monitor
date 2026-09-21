from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
from typing import Any, Iterable

from domain.domain.trade_contract_identity import (
    canonical_contract_symbol, derive_position_side, normalize_contract_expiration,
    normalize_contract_option_type, normalize_position_effect, normalize_trade_side,
)
from domain.domain.trade_account_identity import extract_primary_account_id
from domain.domain.option_position_identity import normalize_broker
from domain.domain.symbol_identity import (
    OPTION_CODE_RE,
    pick_first_normalized_symbol,
    symbol_currency,
    symbol_market,
)


EXECUTION_INPUT_VERSION = "trade_execution.v1"
_INSTANT_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:\d{2})$"
)


@dataclass(frozen=True)
class ExecutionIdentity:
    broker_id: str
    external_account_id: str
    environment: str
    external_id_namespace: str
    external_execution_id: str

    @classmethod
    def from_input(cls, execution: Any) -> "ExecutionIdentity | None":
        if not isinstance(execution, Mapping):
            return None
        account = execution.get("broker_account_ref")
        if not isinstance(account, Mapping):
            return None
        values = (
            str(account.get("broker_id") or "").strip().lower(),
            str(account.get("external_account_id") or "").strip(),
            str(account.get("environment") or "").strip().upper(),
            str(execution.get("external_id_namespace") or "").strip(),
            str(execution.get("external_execution_id") or "").strip(),
        )
        return cls(*values) if all(values) else None

    @property
    def stable_id(self) -> str:
        digest = hashlib.sha256(
            json.dumps(
                [
                    self.broker_id,
                    self.external_account_id,
                    self.environment,
                    self.external_id_namespace,
                    self.external_execution_id,
                ],
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode()
        ).hexdigest()
        return f"execution:v1:{digest}"


def execution_identity_from_input(execution: Any) -> str:
    identity = ExecutionIdentity.from_input(execution)
    return identity.stable_id if identity is not None else ""


def execution_source_namespaces(payload: Mapping[str, Any], execution: Any) -> frozenset[str]:
    """Resolve declared source namespaces, retaining the legacy Futu deal contract."""
    identity = ExecutionIdentity.from_input(execution)
    namespaces = {
        str(payload[field]).strip()
        for field in ("external_id_namespace", "execution_id_namespace")
        if payload.get(field) not in (None, "")
    }
    if not namespaces and ((identity is not None and identity.broker_id == "futu") or payload.get("futu_account_id")):
        namespaces = {"futu.deal"}
    return frozenset(namespaces)


def execution_source_identity_conflicts(payload: Mapping[str, Any], execution: Any) -> bool:
    """Compare declared source deal IDs only within the same execution namespace."""
    identity = ExecutionIdentity.from_input(execution)
    if identity is None:
        return False
    namespaces = execution_source_namespaces(payload, execution)
    if len(namespaces) > 1:
        return True
    if identity.external_id_namespace not in namespaces:
        return False
    values = [payload.get(field) for field in ("source_deal_id", "deal_id", "futu_deal_id", "external_execution_id")]
    completion = payload.get("broker_deal_completion")
    if isinstance(completion, Mapping):
        values.append(completion.get("source_deal_id"))
    return any(str(value).strip() != identity.external_execution_id for value in values if value not in (None, ""))


def canonical_decimal(value: Any) -> str | None:
    """Encode a supplied decimal exactly; missing values never become zero."""
    if value is None or value == "":
        return None
    if isinstance(value, (bool, float)) or not isinstance(value, (str, int, Decimal)):
        raise ValueError("decimal_string_required")
    if len(str(value)) > 128:
        raise ValueError("decimal_precision_exceeded")
    try:
        number = Decimal(value)
    except (InvalidOperation, ValueError):
        raise ValueError("invalid_decimal") from None
    if not number.is_finite():
        raise ValueError("non_finite_decimal")
    if abs(number.as_tuple().exponent) > 128 or number.adjusted() > 128:
        raise ValueError("decimal_precision_exceeded")
    if number.is_zero():
        return "0"
    text = format(number, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def canonical_utc_instant(value: Any) -> str | None:
    """Keep all source fractional digits, including precision beyond microseconds."""
    if value is None or value == "":
        return None
    match = _INSTANT_RE.fullmatch(str(value).strip())
    if not match:
        raise ValueError("timestamp_with_seconds_and_timezone_required")
    day, clock, fraction, offset = match.groups()
    try:
        instant = datetime.fromisoformat(f"{day}T{clock}{offset.replace('Z', '+00:00')}")
        utc = instant.astimezone(timezone.utc)
    except ValueError:
        raise ValueError("invalid_timestamp") from None
    suffix = (fraction or "").rstrip("0")
    return utc.strftime("%Y-%m-%dT%H:%M:%S") + (f".{suffix}" if suffix else "") + "Z"


def epoch_milliseconds_instant(value: Any) -> str | None:
    text = canonical_decimal(value)
    if text is None:
        return None
    decimal_places = len(text.partition(".")[2])
    scale = 10 ** (decimal_places + 3)
    whole, fraction = divmod(int(text.replace(".", "")), scale)
    instant = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=whole)
    fractional_digits = str(fraction).zfill(decimal_places + 3).rstrip("0")
    return instant.strftime("%Y-%m-%dT%H:%M:%S") + (f".{fractional_digits}" if fractional_digits else "") + "Z"


def normalize_execution_input(
    payload: Mapping[str, Any], *, source_payload: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate source-independent, per-execution input without external lookups."""
    errors: list[str] = []
    if payload.get("schema_version") not in (None, EXECUTION_INPUT_VERSION):
        errors.append("unsupported:schema_version")

    def text(value: Any) -> str | None:
        return str(value).strip() or None if value is not None else None

    def required(value: Any, name: str) -> Any:
        if value is None or value == "":
            errors.append(f"missing:{name}")
        return value

    def decimal(value: Any, name: str, *, positive: bool = False, integer: bool = False) -> str | None:
        try:
            result = canonical_decimal(value)
        except ValueError as exc:
            errors.append(f"invalid:{name}:{exc}")
            return None
        required(result, name)
        if result is not None:
            number = Decimal(result)
            if number < 0 or (positive and number == 0):
                errors.append(f"invalid:{name}:must_be_{'positive' if positive else 'nonnegative'}")
            if integer and number != number.to_integral_value():
                errors.append(f"invalid:{name}:integer_required")
        return result

    account_source = payload.get("broker_account_ref")
    account_source = account_source if isinstance(account_source, Mapping) else {}
    account = {
        name: required(text(account_source.get(name)), f"broker_account_ref.{name}")
        for name in ("broker_account_id", "broker_id", "external_account_id", "environment")
    }
    if isinstance(account_source.get("external_account_id"), (float, bool)):
        errors.append("invalid:broker_account_ref.external_account_id:string_required")
    account["account_label"] = text(account_source.get("account_label"))
    if account["environment"]:
        account["environment"] = account["environment"].upper()
    if account["broker_id"]:
        account["broker_id"] = account["broker_id"].lower()

    source = payload.get("instrument_ref")
    source = source if isinstance(source, Mapping) else {}
    asset_type = text(source.get("asset_type"))
    asset_type = asset_type.lower() if asset_type else None
    if asset_type not in {"stock", "option"}:
        errors.append("unsupported:instrument_ref.asset_type" if asset_type else "missing:instrument_ref.asset_type")
    instrument = {
        "asset_type": asset_type,
        "symbol": required(text(source.get("symbol")), "instrument_ref.symbol"),
        "market": required(text(source.get("market")), "instrument_ref.market"),
        "currency": required(text(source.get("currency")), "instrument_ref.currency"),
        "source_code": text(source.get("source_code")),
        "source_security_type": text(source.get("source_security_type")),
    }
    for name in ("symbol", "market", "currency"):
        if instrument[name]:
            instrument[name] = instrument[name].upper()
    if asset_type == "option":
        option_type = text(source.get("option_type"))
        option_type = {"p": "put", "c": "call"}.get((option_type or "").lower(), (option_type or "").lower()) or None
        if option_type not in {"put", "call"}:
            errors.append("invalid:instrument_ref.option_type" if option_type else "missing:instrument_ref.option_type")
        expiration = required(text(source.get("expiration_ymd")), "instrument_ref.expiration_ymd")
        if expiration:
            try:
                date.fromisoformat(expiration)
            except ValueError:
                errors.append("invalid:instrument_ref.expiration_ymd")
        deliverable = source.get("deliverable")
        if deliverable is not None:
            if not isinstance(deliverable, Mapping):
                errors.append("invalid:instrument_ref.deliverable")
                deliverable = None
            else:
                deliverable = dict(deliverable)
                for name in ("quantity", "amount", "multiplier", "ratio"):
                    if name in deliverable:
                        deliverable[name] = decimal(deliverable[name], f"instrument_ref.deliverable.{name}")
        instrument.update(
            option_type=option_type,
            strike=decimal(source.get("strike"), "instrument_ref.strike", positive=True),
            expiration_ymd=expiration,
            multiplier=decimal(source.get("multiplier"), "instrument_ref.multiplier", positive=True, integer=True),
            deliverable=deliverable,
        )
    elif source.get("option_type") not in (None, ""):
        errors.append("invalid:instrument_ref.asset_type_option_mismatch")

    side = normalize_trade_side(payload.get("side"))
    if side is None:
        errors.append("invalid:side" if payload.get("side") else "missing:side")
    try:
        occurred_at = canonical_utc_instant(payload.get("occurred_at_utc"))
    except ValueError as exc:
        occurred_at = None
        errors.append(f"invalid:occurred_at_utc:{exc}")
    if not any(error.startswith("invalid:occurred_at_utc") for error in errors):
        required(occurred_at, "occurred_at_utc")
    granularity = text(payload.get("data_type")) or "execution"
    if granularity != "execution":
        errors.append("unsupported:data_type:execution_required")
    currency = required(text(payload.get("currency")), "currency")
    currency = currency.upper() if currency else None
    if currency and instrument["currency"] and currency != instrument["currency"]:
        errors.append("invalid:currency:instrument_mismatch")
    evidence_refs = payload.get("evidence_refs") or []
    if not isinstance(evidence_refs, list) or any(not isinstance(ref, str) for ref in evidence_refs):
        errors.append("invalid:evidence_refs")
        evidence_refs = []
    result = {
        "schema_version": EXECUTION_INPUT_VERSION,
        "broker_account_ref": account,
        "instrument_ref": instrument,
        "data_type": granularity,
        "external_id_namespace": required(text(payload.get("external_id_namespace")), "external_id_namespace"),
        "external_execution_id": required(text(payload.get("external_execution_id")), "external_execution_id"),
        "external_order_namespace": text(payload.get("external_order_namespace")),
        "external_order_id": text(payload.get("external_order_id")),
        "side": side,
        "quantity": decimal(payload.get("quantity"), "quantity", positive=True, integer=asset_type == "option"),
        "quantity_unit": text(payload.get("quantity_unit")) or ({"stock": "share", "option": "contract"}.get(asset_type)),
        "price": decimal(payload.get("price"), "price"),
        "currency": currency,
        "occurred_at_utc": occurred_at,
        "position_effect": normalize_position_effect(payload.get("position_effect")),
        "evidence_refs": list(evidence_refs),
        "source_time": payload.get("source_time"),
        "source_timezone": text(payload.get("source_timezone")),
        "errors": errors,
    }
    expected_unit = {"stock": "share", "option": "contract"}.get(asset_type)
    if expected_unit and result["quantity_unit"] != expected_unit:
        errors.append("invalid:quantity_unit")
    if result["external_order_id"] and not result["external_order_namespace"]:
        errors.append("missing:external_order_namespace")
    if result["external_order_namespace"] and not result["external_order_id"]:
        errors.append("missing:external_order_id")
    if execution_source_identity_conflicts(payload if source_payload is None else source_payload, result):
        errors.append("invalid:source_execution_identity")
    return result


def execution_economic_content(execution: Mapping[str, Any]) -> dict[str, Any]:
    instrument = dict(execution.get("instrument_ref") or {})
    instrument.pop("source_code", None)
    instrument.pop("source_security_type", None)
    account = dict(execution.get("broker_account_ref") or {})
    account.pop("account_label", None)
    return {
        "version": EXECUTION_INPUT_VERSION,
        "economic": {
            "account": account,
            "instrument": instrument,
            **{name: execution.get(name) for name in ("side", "quantity", "price", "currency", "occurred_at_utc")},
        },
        "associations": {
            name: execution.get(name)
            for name in ("external_order_namespace", "external_order_id", "position_effect")
        },
        "errors": list(execution.get("errors") or []),
    }


def conflicting_execution_associations(left: Mapping[str, Any], right: Mapping[str, Any]) -> list[str]:
    old = left.get("associations") or {}
    new = right.get("associations") or {}
    return [name for name in ("external_order_namespace", "external_order_id", "position_effect") if old.get(name) is not None and new.get(name) is not None and old[name] != new[name]]


def futu_order_namespace_issue(payload: Mapping[str, Any]) -> str | None:
    """Require explicit Futu order scope for standard executions, retaining legacy rows."""
    execution = payload.get("execution_input")
    namespaces = {
        str(value).strip()
        for value in (
            payload.get("external_order_namespace"),
            execution.get("external_order_namespace") if isinstance(execution, Mapping) else None,
        )
        if value is not None and str(value).strip()
    }
    if namespaces and namespaces != {"futu.order"}:
        return "unsupported_order_namespace"
    standard = (
        isinstance(execution, Mapping)
        or payload.get("schema_version") == "trade_execution.v1"
        or isinstance(payload.get("broker_account_ref"), Mapping)
        or isinstance(payload.get("instrument_ref"), Mapping)
    )
    if standard:
        if not namespaces:
            return "order_namespace_missing"
        refs = [
            ref for ref in (
                payload.get("broker_account_ref"),
                execution.get("broker_account_ref") if isinstance(execution, Mapping) else None,
            )
            if isinstance(ref, Mapping)
        ]
        source = payload.get("_trade_intake_source")
        physical_ids = {
            str(value).strip()
            for value in (
                payload.get("futu_account_id"),
                source.get("futu_account_id") if isinstance(source, Mapping) else None,
            )
            if value is not None and str(value).strip()
        }
        if not refs or not physical_ids:
            return "order_account_identity_missing"
        for ref in refs:
            if str(ref.get("broker_id") or "").strip().lower() != "futu":
                return "unsupported_order_broker"
            if str(ref.get("environment") or "").strip().upper() != "REAL":
                return "unsupported_order_environment"
            physical_id = ref.get("external_account_id")
            if isinstance(physical_id, (float, bool)) or not str(physical_id or "").strip():
                return "order_account_identity_missing"
            if physical_ids != {str(physical_id).strip()}:
                return "order_physical_account_mismatch"
    # Legacy callers still require the Futu broker, physical account and order ID.
    return None


_SYMBOL_KEYS = (
    "symbol",
    "underlying_symbol",
    "owner_symbol",
    "owner_stock_code",
    "owner_stock_code_full",
    "underlying_stock_code",
    "owner_code",
    "underlying_code",
    "stock_code",
    "code",
    "owner_stock_name",
    "underlying_stock_name",
    "owner_name",
    "stock_name",
    "name",
    "underlying",
)


def _pick(src: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in src and src.get(key) not in (None, ""):
            return src.get(key)
    return None


def _parse_futu_option_code(code: Any) -> dict[str, Any]:
    raw = str(code or "").strip().upper()
    match = OPTION_CODE_RE.match(raw)
    if not match:
        return {}
    strike_digits = match.group("strike")
    strike_value = None
    if strike_digits:
        try:
            decimal_digits = strike_digits.zfill(4)
            strike_value = canonical_decimal(decimal_digits[:-3] + "." + decimal_digits[-3:])
        except Exception:
            strike_value = None
    expiration = f"20{match.group('yy')}-{match.group('mm')}-{match.group('dd')}"
    option_type = "call" if match.group("cp") == "C" else "put"
    return {
        "option_code_market": match.group("market"),
        "option_code_root": match.group("root"),
        "option_type": option_type,
        "expiration_ymd": expiration,
        "strike": strike_value,
        "currency": ("HKD" if match.group("market") == "HK" else "USD" if match.group("market") == "US" else None),
    }


def _futu_asset_type(src: dict[str, Any], option_info: dict[str, Any]) -> str | None:
    raw_type = _pick(src, "asset_type", "security_type", "stock_type", "sec_type")
    if raw_type is not None:
        kind = str(raw_type).strip().lower().rsplit(".", 1)[-1]
        return {"stock": "stock", "etf": "stock", "equity": "stock", "option": "option"}.get(kind)
    if option_info or _pick(src, "option_type", "put_call", "call_or_put") is not None:
        return "option"
    code = str(_pick(src, "code", "stock_code") or "").strip().upper()
    if re.fullmatch(r"US\.[A-Z][A-Z.\-]{0,9}|HK\.\d{4,5}|\d{4,5}\.HK", code):
        return "stock"
    return None


def _source_multiplier_errors(src: dict[str, Any]) -> list[str]:
    supplied = {}
    for key in ("multiplier", "contract_multiplier", "lot_size"):
        if src.get(key) in (None, ""):
            continue
        try:
            value = canonical_decimal(str(src[key]) if isinstance(src[key], float) else src[key])
        except (ValueError, TypeError):
            value = None
        supplied[key] = _positive_int(value)
    errors = [f"invalid:instrument_ref.multiplier:source_field:{key}" for key, value in supplied.items() if value is None or value <= 0]
    if len({value for value in supplied.values() if value is not None and value > 0}) > 1:
        errors.append("invalid:instrument_ref.multiplier:source_alias_conflict")
    return errors


def _futu_execution_input(src: dict[str, Any]) -> dict[str, Any]:
    """Map Futu execution fields only; SDK enrichment belongs to the adapter caller."""
    code = _pick(src, "code", "stock_code", "symbol")
    option_info = _parse_futu_option_code(code)
    symbol = pick_first_normalized_symbol(src, *_SYMBOL_KEYS)
    asset_type = _futu_asset_type(src, option_info)
    raw_time = _pick(src, "occurred_at_utc", "trade_time_ms", "create_time", "updated_time")
    occurred_at = raw_time
    try:
        if "occurred_at_utc" in src:
            occurred_at = canonical_utc_instant(raw_time)
        elif "trade_time_ms" in src:
            occurred_at = epoch_milliseconds_instant(str(raw_time) if isinstance(raw_time, float) else raw_time)
        elif raw_time is not None:
            time_text = str(raw_time).strip().replace("/", "-")
            if not re.search(r"Z$|[+-]\d{2}:\d{2}$", time_text):
                time_text += "+08:00"
            occurred_at = canonical_utc_instant(time_text)
    except (ValueError, OverflowError):
        pass

    def decimal_source(*keys: str) -> Any:
        value = _pick(src, *keys)
        return str(value) if isinstance(value, float) else value

    option_type = _pick(src, "option_type", "put_call", "call_or_put") or option_info.get("option_type")
    currency = (
        _pick(src, "currency", "currency_code", "ccy")
        or option_info.get("currency")
        or symbol_currency(symbol)
    )
    data_type = str(_pick(src, "data_type", "record_type", "granularity") or "execution").strip().lower()
    if data_type in {"deal", "fill", "trade"}:
        data_type = "execution"
    if any(key in src for key in ("dealt_avg_price", "avg_price", "dealt_qty")) and not all(
        _pick(src, *keys) is not None for keys in (("qty", "quantity", "contracts"), ("price", "execution_price", "dealt_price"))
    ):
        data_type = "order_summary"
    execution = normalize_execution_input({
        "broker_account_ref": {
            "broker_account_id": src.get("broker_account_id"),
            "broker_id": "futu",
            "external_account_id": extract_primary_account_id(src),
            "environment": _pick(src, "environment", "trd_env"),
            "account_label": _pick(src, "internal_account", "account_label"),
        },
        "instrument_ref": {
            "asset_type": asset_type,
            "symbol": symbol,
            "market": src.get("market") or option_info.get("option_code_market") or symbol_market(symbol),
            "currency": currency,
            "source_code": code,
            "source_security_type": _pick(src, "security_type", "stock_type", "sec_type"),
            "option_type": option_type,
            "strike": decimal_source("strike", "strike_price") if _pick(src, "strike", "strike_price") is not None else option_info.get("strike"),
            "expiration_ymd": normalize_contract_expiration(_pick(src, "expiration", "expiration_ymd", "expiry", "expiry_date")) or option_info.get("expiration_ymd"),
            "multiplier": decimal_source("multiplier", "contract_multiplier", "lot_size"),
            "deliverable": src.get("deliverable"),
        },
        "data_type": data_type,
        "external_id_namespace": _pick(src, "external_id_namespace", "execution_id_namespace"),
        "external_execution_id": _pick(src, "external_execution_id", "deal_id", "dealID", "id"),
        "external_order_namespace": src.get("external_order_namespace"),
        "external_order_id": _pick(src, "external_order_id", "order_id", "orderID"),
        "side": _pick(src, "side", "trd_side", "trade_side"),
        "position_effect": _pick(src, "position_effect", "position_side", "offset_type", "open_close", "trd_side", "trade_side", "side"),
        "quantity": decimal_source("contracts", "qty", "quantity"),
        "price": decimal_source("price", "execution_price", "dealt_price"),
        "currency": currency,
        "occurred_at_utc": occurred_at,
        "source_time": raw_time,
        "source_timezone": src.get("source_timezone") or ("UTC" if "occurred_at_utc" in src or "trade_time_ms" in src else "Asia/Shanghai"),
        "evidence_refs": src.get("evidence_refs") or [],
    }, source_payload=src)
    if asset_type == "option":
        execution["errors"].extend(_source_multiplier_errors(src))
    return execution


def canonical_trade_execution_content(payload: dict[str, Any]) -> dict[str, Any]:
    nested = payload.get("execution_input")
    if isinstance(nested, dict):
        execution = normalize_execution_input(nested, source_payload=payload)
    elif _is_standard_execution(payload):
        execution = normalize_execution_input(payload)
    else:
        execution = _futu_execution_input(payload)
    return execution_economic_content(execution)


def _is_standard_execution(src: dict[str, Any]) -> bool:
    broker = str(_pick(src, "broker_id", "broker") or "futu").strip().lower()
    return any(name in src for name in ("execution_input", "instrument_ref", "broker_account_ref")) or broker not in {"futu", "富途"}


DEAL_ID_FIELDS = ("source_deal_id", "deal_id", "futu_deal_id")


def structured_deal_ids_from_ledger_event(event: dict[str, Any]) -> set[str]:
    """Return only broker deal identifiers stored in authoritative fields."""

    raw = event.get("raw_payload")
    raw_payload = raw if isinstance(raw, dict) else {}
    out = _normalized_values(raw_payload.get(key) for key in DEAL_ID_FIELDS)
    stock_settlement = raw_payload.get("stock_settlement")
    if isinstance(stock_settlement, dict):
        out.update(_normalized_values([stock_settlement.get("source_event_id")]))
    return out


def structured_deal_keys_from_ledger_event(
    event: dict[str, Any], *, include_legacy_execution_identity: bool = False,
) -> set[str]:
    """Return proven scoped identities; an unscoped deal ID is never a key."""

    raw = event.get("raw_payload")
    raw_payload = raw if isinstance(raw, dict) else {}
    execution = raw_payload.get("execution_input") or {}
    execution_id = execution_identity_from_input(execution)
    if execution and (not execution_id or execution_source_identity_conflicts(raw_payload, execution)):
        return set()
    keys = {execution_id} if execution_id else set()
    deal_ids = structured_deal_ids_from_ledger_event(event)
    contract = event.get("contract_key") if isinstance(event.get("contract_key"), dict) else {}
    account = str(
        event.get("account")
        or contract.get("account")
        or raw_payload.get("internal_account")
        or raw_payload.get("account")
        or ""
    ).strip().lower()
    futu_account_id = str(raw_payload.get("futu_account_id") or "").strip()
    if execution_id:
        ref = execution["broker_account_ref"]
        if not (
            ref.get("broker_id") == "futu"
            and ref.get("external_account_id") == futu_account_id
            and ref.get("environment") == "REAL"
            and execution.get("external_id_namespace") == "futu.deal"
            and str(execution.get("external_execution_id")) in deal_ids
        ):
            return keys
    # The legacy alias denotes REAL Futu deals only; explicit scope wins.
    if any(
        raw_payload.get(field) not in (None, "", expected)
        for field, expected in (
            ("environment", "REAL"), ("trd_env", "REAL"),
            ("external_id_namespace", "futu.deal"),
            ("execution_id_namespace", "futu.deal"),
        )
    ):
        return keys
    if any(
        str(value).strip().lower() not in {"futu", "富途"}
        for value in (event.get("broker"), raw_payload.get("broker"), raw_payload.get("broker_id"))
        if value not in (None, "")
    ):
        return keys
    if account and futu_account_id:
        keys.update({
            f"futu:{account}:{futu_account_id}:{deal_id}"
            for deal_id in deal_ids
        })
        if include_legacy_execution_identity and not execution_id:
            # This is the physical identity of the already-proven legacy alias,
            # not an admission default for an incomplete incoming execution.
            keys.update(
                execution_identity_from_input({
                    "broker_account_ref": {
                        "broker_id": "futu", "external_account_id": futu_account_id,
                        "environment": "REAL",
                    },
                    "external_id_namespace": "futu.deal", "external_execution_id": deal_id,
                })
                for deal_id in deal_ids
            )
    return keys


def ledger_execution_event_set_is_complete(rows: list[dict[str, Any]]) -> bool:
    """Prove one active execution's complete allocation, source and target evidence."""
    if not rows:
        return False
    metadata_rows = [value for row in rows if (value := _deal_completion_payload(row)) is not None]
    if not metadata_rows:
        has_resolution = any("close_target_resolution" in (row.get("raw_payload") or {}) for row in rows)
        return (len(rows) == 1 and not has_resolution) or (
            _split_events_are_coherent(rows) and _legacy_split_set_is_complete(rows)
        )
    if len(metadata_rows) != len(rows):
        return False
    expected_split_count = _consistent_positive_int(metadata_rows, "split_count")
    expected_contracts = _consistent_positive_int(metadata_rows, "expected_contracts")
    if expected_split_count is None or expected_contracts is None:
        return False
    split_indexes = {_positive_int(item.get("split_index")) for item in metadata_rows}
    allocations = [_positive_int(item.get("allocated_contracts")) for item in metadata_rows]
    actual_contracts = [_positive_int(row.get("contracts")) for row in rows]
    return (
        None not in allocations
        and allocations == actual_contracts
        and _split_events_are_coherent(rows)
        and (
            not any("close_target_resolution" in (row.get("raw_payload") or {}) for row in rows)
            or _legacy_split_set_is_complete(rows)
        )
        and all(_positive_int(value) == expected_contracts for row in rows for value in _declared_execution_quantities(row))
        and len(rows) == expected_split_count
        and split_indexes == set(range(1, expected_split_count + 1))
        and sum(allocations) == expected_contracts
    )


def _deal_completion_payload(event: dict[str, Any]) -> dict[str, Any] | None:
    raw = event.get("raw_payload")
    raw_payload = raw if isinstance(raw, dict) else {}
    value = raw_payload.get("broker_deal_completion")
    return dict(value) if isinstance(value, dict) else ({} if "broker_deal_completion" in raw_payload else None)


def _legacy_split_set_is_complete(rows: list[dict[str, Any]]) -> bool:
    resolutions: list[dict[str, Any]] = []
    target_lot_ids: set[str] = set()
    for event in rows:
        raw = event.get("raw_payload")
        raw_payload = raw if isinstance(raw, dict) else {}
        resolution = raw_payload.get("close_target_resolution")
        if not isinstance(resolution, dict):
            return False
        if not isinstance(resolution.get("record_ids"), (list, tuple)):
            return False
        resolutions.append(dict(resolution))
        target_lot_id = str(
            event.get("target_lot_id")
            or raw_payload.get("target_lot_id")
            or raw_payload.get("record_id")
            or ""
        ).strip()
        if not target_lot_id:
            return False
        target_lot_ids.add(target_lot_id)

    declared_target_sets = {
        tuple(
            sorted(
                str(value or "").strip()
                for value in item["record_ids"]
            )
        )
        for item in resolutions
    }
    declared_contracts = {
        _positive_int(item.get("contracts_to_close"))
        for item in resolutions
    }
    if len(declared_target_sets) != 1 or len(declared_contracts) != 1:
        return False
    declared_targets = next(iter(declared_target_sets))
    expected_targets = set(declared_targets)
    if len(declared_targets) != len(expected_targets):
        return False
    expected_contracts = next(iter(declared_contracts))
    if expected_contracts is None:
        return False
    return (
        expected_targets == target_lot_ids
        and len(rows) == len(target_lot_ids)
        and all(_positive_int(event.get("contracts")) is not None for event in rows)
        and sum(_positive_int(event.get("contracts")) for event in rows) == expected_contracts
        and all(
            _positive_int(value) == expected_contracts
            for row in rows
            for value in _declared_execution_quantities(row)
        )
    )


def _consistent_positive_int(rows: list[dict[str, Any]], key: str) -> int | None:
    values = {_positive_int(item.get(key)) for item in rows}
    if None in values or len(values) != 1:
        return None
    return next(iter(values))


def _positive_int(value: Any) -> int | None:
    number = _finite_decimal(value)
    if number is None or number <= 0 or number != number.to_integral_value():
        return None
    return int(number)


def _finite_decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        return None
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def _stored_event_economic_fields(event: dict[str, Any]) -> dict[str, Any]:
    """Compatibility read boundary for flat and nested historical ledger rows."""
    key = event.get("contract_key") if isinstance(event.get("contract_key"), dict) else {}
    raw = event.get("raw_payload") if isinstance(event.get("raw_payload"), dict) else {}
    side = event.get("side") or raw.get("side")
    return {
        **event,
        "broker": event.get("broker") or key.get("broker"),
        "account": event.get("account") or key.get("account") or raw.get("internal_account"),
        "symbol": event.get("symbol") or key.get("underlying_symbol"),
        "option_type": event.get("option_type") or key.get("option_type"),
        "position_side": event.get("position_side") or key.get("position_side")
        or derive_position_side(event.get("event_type"), side),
        "side": side,
        "strike": event.get("strike", key.get("strike")),
        "expiration_ymd": event.get("expiration_ymd") or key.get("expiration_ymd"),
        "multiplier": event.get("multiplier", raw.get("multiplier")),
        "currency": event.get("currency") or raw.get("currency"),
    }


def ledger_event_economic_fingerprint(event: dict[str, Any]) -> tuple[Any, ...]:
    """Compare allocated event economics independently of the per-lot quantity."""
    fields = _stored_event_economic_fields(event)
    return (
        str(fields.get("event_type") or "").lower(),
        str(fields.get("broker") or "").lower(),
        str(fields.get("account") or "").lower(),
        str(fields.get("symbol") or "").upper(),
        str(fields.get("option_type") or "").lower(),
        str(fields.get("position_side") or "").lower(),
        str(fields.get("side") or "").lower(),
        _finite_decimal(fields.get("strike")),
        str(fields.get("expiration_ymd") or ""),
        _positive_int(fields.get("multiplier")),
        _finite_decimal(fields.get("price")),
        str(fields.get("currency") or "").upper(),
    )


def _declared_execution_quantities(event: dict[str, Any]) -> list[Any]:
    raw = event.get("raw_payload") or {}
    execution = raw.get("execution_input") or {}
    return ([raw[field] for field in ("qty", "quantity", "contracts") if field in raw]
            + ([execution["quantity"]] if "quantity" in execution else []))


def _source_economics_match_event(event: dict[str, Any]) -> bool:
    raw = event.get("raw_payload") or {}
    if not _declared_execution_quantities(event):
        return False
    actual = _stored_event_economic_fields(event)
    effect = str(event.get("event_type") or "").lower()
    if effect in {"expire_close", "assignment", "exercise"}:
        effect = "close"
    if effect in {"open", "close"}:
        position_side = actual.get("position_side")
        if position_side not in {"long", "short"}:
            # §9.2 step 3: stored contract keys no longer carry the position side,
            # so derive it from the trade side this event declares.
            position_side = derive_position_side(effect, actual["side"])
        if position_side not in {"long", "short"}:
            return False
        expected_side = {("open", "short"): "sell", ("open", "long"): "buy",
                         ("close", "short"): "buy", ("close", "long"): "sell"}[(effect, position_side)]
        if actual["side"] is not None and normalize_trade_side(actual["side"]) != expected_side:
            return False
        actual["side"] = expected_side
    for field, aliases, normalize in (
        ("price", ("price", "execution_price", "dealt_price"), _finite_decimal),
        ("strike", ("strike", "strike_price"), _finite_decimal),
        ("multiplier", ("multiplier", "contract_multiplier", "lot_size"), _positive_int),
        ("expiration_ymd", ("expiration_ymd", "expiration", "expiry", "expiry_date"), normalize_contract_expiration),
        ("side", ("side", "trd_side", "trade_side"), normalize_trade_side),
    ):
        for alias in aliases:
            if alias in raw and (normalize(raw[alias]) is None or normalize(raw[alias]) != normalize(actual.get(field))):
                return False
    sources = [(canonical_trade_execution_content(raw), bool(raw.get("execution_input")))]
    if raw.get("execution_input"):
        sources.append((canonical_trade_execution_content({k: v for k, v in raw.items() if k != "execution_input"}), False))
    for source, standard in sources:
        if (standard and source["errors"]) or any(error.startswith("invalid:") for error in source["errors"]):
            return False
        economic = source["economic"]
        instrument = economic["instrument"]
        declared = {**instrument, "side": economic["side"], "price": economic["price"],
                    "currency": economic["currency"]}
        for field, normalize in (
            ("symbol", canonical_contract_symbol), ("option_type", normalize_contract_option_type),
            ("expiration_ymd", normalize_contract_expiration), ("side", normalize_trade_side),
            ("strike", _finite_decimal), ("multiplier", _positive_int), ("price", _finite_decimal),
            ("currency", lambda value: str(value or "").upper()),
        ):
            if declared.get(field) is not None and normalize(declared[field]) != normalize(actual.get(field)):
                return False
        if standard and normalize_broker(economic["account"]["broker_id"]) != normalize_broker(actual.get("broker")):
            return False
        if source["associations"].get("position_effect") not in (None, effect):
            return False
    return True


def _split_events_are_coherent(rows: list[dict[str, Any]]) -> bool:
    keys = [structured_deal_keys_from_ledger_event(row) for row in rows]
    if not set.intersection(*keys):
        return False
    execution_ids = {
        execution_identity_from_input((row.get("raw_payload") or {}).get("execution_input"))
        for row in rows
    } - {""}
    if len(execution_ids) > 1:
        return False
    source_namespaces = {
        execution_source_namespaces(row.get("raw_payload") or {}, (row.get("raw_payload") or {}).get("execution_input"))
        for row in rows
    }
    if len(source_namespaces) > 1 or any(len(namespaces) > 1 for namespaces in source_namespaces):
        return False
    for row in rows:
        if not _source_economics_match_event(row):
            return False
        raw = row.get("raw_payload") or {}
        deal_ids = structured_deal_ids_from_ledger_event(row)
        contract = row.get("contract_key") if isinstance(row.get("contract_key"), dict) else {}
        accounts = _normalized_values(
            str(value or "").strip().lower()
            for value in (row.get("account"), contract.get("account"), raw.get("internal_account"), raw.get("account"))
        )
        if len(accounts) > 1:
            return False
        completion = _deal_completion_payload(row) or {}
        execution = raw.get("execution_input") or {}
        ref = execution.get("broker_account_ref") or {}
        if execution_identity_from_input(execution) and any(
            value not in (None, "", expected)
            for value, expected in (
                (raw.get("futu_account_id"), ref.get("external_account_id")),
                (raw.get("environment"), ref.get("environment")),
                (raw.get("trd_env"), ref.get("environment")),
            )
        ):
            return False
        if len(deal_ids) > 1 or (
            completion.get("source_deal_id") not in (None, "")
            and str(completion["source_deal_id"]) not in deal_ids
        ):
            return False
    if len(rows) == 1:
        return True
    fingerprints = {ledger_event_economic_fingerprint(row) for row in rows}
    if len(fingerprints) != 1 or any(value in (None, "") for value in next(iter(fingerprints))):
        return False
    if next(iter(fingerprints))[0] not in {"close", "expire_close", "assignment", "exercise"}:
        return False
    event_ids = {str(row.get("event_id") or "").strip() for row in rows}
    if "" in event_ids or len(event_ids) != len(rows):
        return False
    targets = [str(row.get("target_lot_id") or (row.get("raw_payload") or {}).get("target_lot_id")
                   or (row.get("raw_payload") or {}).get("record_id") or "").strip() for row in rows]
    if "" in targets or len(set(targets)) != len(rows):
        return False
    executions = [execution_economic_content((row.get("raw_payload") or {})["execution_input"])
                  for row in rows if execution_identity_from_input((row.get("raw_payload") or {}).get("execution_input"))]
    return all(
        not item["errors"] and item["economic"] == executions[0]["economic"]
        and not conflicting_execution_associations(item, executions[0])
        for item in executions
    )


def _normalized_values(values: Iterable[Any]) -> set[str]:
    return {
        text
        for value in values
        for text in [str(value or "").strip()]
        if text
    }


def require_same_execution(stored: dict[str, Any], incoming: dict[str, Any]) -> None:
    if execution_identity_from_input(stored) != execution_identity_from_input(incoming):
        raise ValueError("trade_execution_identity_conflict")
    before = execution_economic_content(stored)
    after = execution_economic_content(incoming)
    require_same_execution_content(before, after)


def require_same_execution_content(before: Mapping[str, Any], after: Mapping[str, Any]) -> None:
    if before["errors"] or after["errors"]:
        raise ValueError("legacy_execution_evidence_required")
    if before["economic"] != after["economic"] or conflicting_execution_associations(before, after):
        raise ValueError("trade_execution_economic_conflict")


def execution_application_conflicts(
    execution_id: str, content: Mapping[str, Any], events: Iterable[Mapping[str, Any]],
    *, allow_legacy_identity: bool = False,
) -> list[str]:
    """Arbitrate durable application associations without repository access."""
    incoming = content.get("associations") or {}
    conflicts: set[str] = set()
    for event in events:
        if not isinstance(event, Mapping):
            continue
        raw = event if event.get("stock_event_id") else event.get("raw_payload") or {}
        if not isinstance(raw, Mapping):
            continue
        execution = raw.get("execution_input") or {}
        stored_id = execution_identity_from_input(execution)
        if stored_id != execution_id and not (allow_legacy_identity and not stored_id):
            continue
        effect = str(event.get("event_type") or "").lower()
        if effect in {"close", "expire_close", "assignment", "exercise", "sale"}:
            effect = "close"
        if effect in {"open", "close"} and incoming.get("position_effect") not in (None, effect):
            conflicts.add("position_effect")
        associations = {
            "external_order_id": raw.get("order_id") or execution.get("external_order_id"),
            "external_order_namespace": raw.get("external_order_namespace") or execution.get("external_order_namespace"),
        }
        conflicts.update(conflicting_execution_associations(
            {"associations": associations}, content,
        ))
    return sorted(conflicts)
