from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
from typing import Any

from domain.domain.trade_contract_identity import normalize_position_effect, normalize_trade_side


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


def normalize_execution_input(payload: Mapping[str, Any]) -> dict[str, Any]:
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
