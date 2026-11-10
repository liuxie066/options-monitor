"""Strict request contracts for the five fixed group-query methods.

The outer envelope, parameter whitelists and canonical values are frozen in
the design (sections 6.6 and 6.6.7).  Nothing here performs I/O, reads
configuration or trusts model-authored routing fields.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
import math
import re
from typing import Any

from domain.domain.symbol_identity import canonical_symbol, symbol_market


REQUEST_SCHEMA = "group_query_request.v1"
RESPONSE_SCHEMA = "group_query_response.v1"

CAPABILITY_MARKET_READ = "market:read"
CAPABILITY_CANDIDATES = "candidates:evaluate"
CAPABILITY_CLOSE = "scenario:close"
CAPABILITY_ROLL_DOWN = "scenario:roll-down"
CAPABILITY_ROLL_UP = "scenario:roll-up"
ALL_CAPABILITIES = frozenset({
    CAPABILITY_MARKET_READ, CAPABILITY_CANDIDATES, CAPABILITY_CLOSE,
    CAPABILITY_ROLL_DOWN, CAPABILITY_ROLL_UP,
})
ALL_MARKETS = frozenset({"US", "HK"})

# method -> (path suffix, required capability)
ROUTES: dict[str, tuple[str, str]] = {
    "query_quotes": ("/quotes", CAPABILITY_MARKET_READ),
    "query_candidates": ("/candidates", CAPABILITY_CANDIDATES),
    "evaluate_close": ("/scenarios/close", CAPABILITY_CLOSE),
    "evaluate_roll_down": ("/scenarios/roll-down", CAPABILITY_ROLL_DOWN),
    "evaluate_roll_up": ("/scenarios/roll-up", CAPABILITY_ROLL_UP),
}
PATH_TO_METHOD = {f"/api/group-query-bot/v1{suffix}": method
                  for method, (suffix, _cap) in ROUTES.items()}

SCENARIO_METHODS = frozenset({"evaluate_close", "evaluate_roll_down", "evaluate_roll_up"})
QUERY_METHODS = frozenset({"query_quotes", "query_candidates"})

ERROR_STATUS = {
    "INPUT_ERROR": 400,
    "UNAUTHORIZED": 401,
    "PERMISSION_DENIED": 403,
    "NOT_FOUND": 404,
    "METHOD_NOT_ALLOWED": 405,
    "RATE_LIMITED": 429,
    "DEPENDENCY_UNAVAILABLE": 503,
    "UPSTREAM_TIMEOUT": 504,
    "INTERNAL_ERROR": 500,
}

_MAX_TEXT = 200
_MAX_CONTRACTS = 100_000


class ContractError(ValueError):
    """A controlled transport or business-input rejection."""

    def __init__(self, code: str, message: str, *, retry_after_seconds: int | None = None):
        if code not in ERROR_STATUS:
            raise ValueError(f"unknown group-query error code {code!r}")
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = ERROR_STATUS[code]
        self.retry_after_seconds = retry_after_seconds


@dataclass(frozen=True)
class VerifiedPrincipal:
    """The immutable server-side authorization record for the fixed bot."""

    principal_id: str
    instance_id: str
    app_id: str
    capabilities: frozenset[str]
    markets: frozenset[str]
    account_scope: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.principal_id or not self.instance_id or not self.app_id:
            raise ValueError("verified principal identity is incomplete")
        if self.account_scope:
            raise ValueError("group-query principal must not carry account scope")
        if not self.capabilities <= ALL_CAPABILITIES:
            raise ValueError("verified principal has unsupported capability")
        if not self.markets <= ALL_MARKETS:
            raise ValueError("verified principal has unsupported market")

    def allows(self, capability: str, market: str) -> bool:
        return capability in self.capabilities and market in self.markets


@dataclass(frozen=True)
class RequestContext:
    tenant_key: str
    chat_id: str
    sender_open_id: str
    thread_id: str | None

    def public_dict(self) -> dict[str, Any]:
        return {
            "tenant_key": self.tenant_key,
            "chat_id": self.chat_id,
            "sender_open_id": self.sender_open_id,
            "thread_id": self.thread_id,
        }


@dataclass(frozen=True)
class NormalizedRequest:
    method: str
    request_id: str
    deadline_utc: datetime
    context: RequestContext
    parameters: dict[str, Any] = field(default_factory=dict)
    input_id: str | None = None
    input_as_of_utc: datetime | None = None

    @property
    def symbol(self) -> str:
        direct = self.parameters.get("symbol")
        if direct is not None:
            return str(direct)
        position = self.parameters.get("position")
        if isinstance(position, dict):
            return str(position.get("symbol") or "")
        return ""

    @property
    def market(self) -> str:
        symbol = self.symbol
        return symbol.split(".", 1)[0] if symbol else ""


def _reject(message: str) -> ContractError:
    return ContractError("INPUT_ERROR", message)


def _object(value: Any, keys: set[str], name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _reject(f"{name} must be an object")
    unknown = sorted(set(value) - keys)
    missing = sorted(key for key in keys if key not in value)
    if unknown:
        raise _reject(f"{name} contains unsupported fields: {', '.join(unknown)}")
    if missing:
        raise _reject(f"{name} is missing fields: {', '.join(missing)}")
    return dict(value)


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise _reject(f"{name} must be a string")
    if not value or value != value.strip():
        raise _reject(f"{name} must be a nonempty string without surrounding whitespace")
    if len(value) > _MAX_TEXT:
        raise _reject(f"{name} is too long")
    return value


_REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")


def _request_identity(value: Any) -> str:
    text = _text(value, "request_id")
    if not _REQUEST_ID.fullmatch(text):
        raise _reject("request_id must be a bounded identifier")
    return text


def _optional_text(value: Any, name: str) -> str | None:
    if value is None:
        return None
    return _text(value, name)


def _finite(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _reject(f"{name} must be a finite number")
    parsed = float(value)
    if not math.isfinite(parsed):
        raise _reject(f"{name} must be a finite number")
    return parsed


def _positive(value: Any, name: str) -> float:
    parsed = _finite(value, name)
    if parsed <= 0:
        raise _reject(f"{name} must be positive")
    return parsed


def _integer(value: Any, name: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or type(value) is not int:
        raise _reject(f"{name} must be an integer")
    if not minimum <= value <= maximum:
        raise _reject(f"{name} must be between {minimum} and {maximum}")
    return value


def _iso_date(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise _reject(f"{name} must use YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise _reject(f"{name} must use YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise _reject(f"{name} must use YYYY-MM-DD")
    return value


def utc_datetime(value: Any, name: str) -> datetime:
    if not isinstance(value, str):
        raise _reject(f"{name} must be an ISO-8601 timestamp with an offset")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise _reject(f"{name} must be an ISO-8601 timestamp with an offset") from exc
    if parsed.tzinfo is None:
        raise _reject(f"{name} must be an ISO-8601 timestamp with an offset")
    return parsed.astimezone(timezone.utc)


def canonical_market_symbol(value: Any) -> str:
    """Return ``{US|HK}.{canonical code}`` or reject the symbol."""
    raw = _text(value, "symbol")
    if raw.count(".") != 1:
        raise _reject("symbol must use a US. or HK. market prefix")
    prefix, code = raw.split(".", 1)
    market = prefix.upper()
    if market not in ALL_MARKETS or not code:
        raise _reject("symbol must use a US. or HK. market prefix")
    canonical = canonical_symbol(raw)
    if not canonical:
        raise _reject("symbol is not a recognized security")
    if symbol_market(canonical) != market:
        raise _reject("symbol market prefix contradicts its canonical identity")
    bare = canonical[:-3] if canonical.endswith(".HK") else canonical
    return f"{market}.{bare}"


def _option_contract(value: Any) -> dict[str, Any]:
    body = _object(value, {"option_type", "expiration", "strike"}, "contract")
    option_type = body["option_type"]
    if option_type not in {"put", "call"}:
        raise _reject("contract.option_type must be put or call")
    return {
        "option_type": str(option_type),
        "expiration": _iso_date(body["expiration"], "contract.expiration"),
        "strike": _positive(body["strike"], "contract.strike"),
    }


def _scenario_position(value: Any, *, option_type: str) -> dict[str, Any]:
    body = _object(value, {"symbol", "contract", "side", "contracts"}, "position")
    if body["side"] != "short":
        raise _reject("position.side must be short")
    contract = _option_contract(body["contract"])
    if contract["option_type"] != option_type:
        raise _reject(f"position.contract.option_type must be {option_type}")
    return {
        "symbol": canonical_market_symbol(body["symbol"]),
        "contract": contract,
        "side": "short",
        "contracts": _integer(body["contracts"], "position.contracts", minimum=1,
                              maximum=_MAX_CONTRACTS),
    }


def _opening_premium(value: Any) -> dict[str, Any]:
    body = _object(value, {"amount", "unit", "currency"}, "opening_premium")
    unit = body["unit"]
    if unit not in {"per_share", "per_contract", "total"}:
        raise _reject("opening_premium.unit must be per_share, per_contract or total")
    currency = body["currency"]
    if currency not in {"USD", "HKD"}:
        raise _reject("opening_premium.currency must be USD or HKD")
    return {
        "amount": _positive(body["amount"], "opening_premium.amount"),
        "unit": str(unit),
        "currency": str(currency),
    }


def _dte_range(value: Any) -> list[int]:
    if not isinstance(value, list) or len(value) != 2:
        raise _reject("dte must be a two-element [min_dte, max_dte] array")
    minimum = _integer(value[0], "dte[0]", minimum=1, maximum=365)
    maximum = _integer(value[1], "dte[1]", minimum=1, maximum=365)
    if minimum > maximum:
        raise _reject("dte must satisfy min_dte <= max_dte")
    return [minimum, maximum]


def _delta_range(value: Any) -> list[float]:
    if not isinstance(value, list) or len(value) != 2:
        raise _reject("abs_delta_range must be a two-element [min, max] array")
    minimum = _finite(value[0], "abs_delta_range[0]")
    maximum = _finite(value[1], "abs_delta_range[1]")
    if not 0 <= minimum <= maximum <= 1:
        raise _reject("abs_delta_range must satisfy 0 <= min <= max <= 1")
    return [minimum, maximum]


def _search_bounds(value: Any, *, strike_key: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _reject("search_bounds must be an object")
    unknown = sorted(set(value) - {"max_new_expiration", strike_key})
    if unknown:
        raise _reject(f"search_bounds contains unsupported fields: {', '.join(unknown)}")
    if "max_new_expiration" not in value:
        raise _reject("search_bounds is missing fields: max_new_expiration")
    result: dict[str, Any] = {
        "max_new_expiration": _iso_date(value["max_new_expiration"],
                                        "search_bounds.max_new_expiration"),
    }
    if strike_key in value:
        result[strike_key] = _positive(value[strike_key], f"search_bounds.{strike_key}")
    return result


def _parameters(method: str, value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _reject("parameters must be an object")
    if method == "query_quotes":
        body = _object(value, {"symbol", "contract"} if "contract" in value
                       else {"symbol"}, "parameters")
        result: dict[str, Any] = {"symbol": canonical_market_symbol(body["symbol"])}
        if "contract" in body:
            result["contract"] = _option_contract(body["contract"])
        return result
    if method == "query_candidates":
        allowed = {"symbol", "strategy", "dte", "strike", "abs_delta_range", "limit"}
        body = {key: item for key, item in value.items() if key in allowed}
        unknown = sorted(set(value) - allowed)
        if unknown:
            raise _reject(f"parameters contains unsupported fields: {', '.join(unknown)}")
        required = {"symbol", "strategy"}
        missing = sorted(required - set(body))
        if missing:
            raise _reject(f"parameters is missing fields: {', '.join(missing)}")
        strategy = body["strategy"]
        if strategy not in {"short_put", "short_call"}:
            raise _reject("strategy must be short_put or short_call")
        result = {"symbol": canonical_market_symbol(body["symbol"]),
                  "strategy": str(strategy)}
        if "dte" in body:
            result["dte"] = _dte_range(body["dte"])
        if "strike" in body:
            result["strike"] = _positive(body["strike"], "strike")
        if "abs_delta_range" in body:
            result["abs_delta_range"] = _delta_range(body["abs_delta_range"])
        result["limit"] = _integer(body.get("limit", 10), "limit", minimum=1, maximum=50)
        return result
    if method == "evaluate_close":
        allowed = {"position", "opening_premium", "opened_at"}
        unknown = sorted(set(value) - allowed)
        if unknown:
            raise _reject(f"parameters contains unsupported fields: {', '.join(unknown)}")
        missing = sorted({"position", "opening_premium"} - set(value))
        if missing:
            raise _reject(f"parameters is missing fields: {', '.join(missing)}")
        position_body = value["position"]
        if not isinstance(position_body, dict):
            raise _reject("position must be an object")
        contract = position_body.get("contract")
        if not isinstance(contract, dict) or contract.get("option_type") not in {"put", "call"}:
            raise _reject("position.contract.option_type must be put or call")
        result = {
            "position": _scenario_position(position_body,
                                           option_type=str(contract["option_type"])),
            "opening_premium": _opening_premium(value["opening_premium"]),
        }
        if "opened_at" in value:
            result["opened_at"] = _iso_date(value["opened_at"], "opened_at")
        return result
    if method in {"evaluate_roll_down", "evaluate_roll_up"}:
        allowed = {"position", "search_bounds", "limit"}
        unknown = sorted(set(value) - allowed)
        if unknown:
            raise _reject(f"parameters contains unsupported fields: {', '.join(unknown)}")
        missing = sorted({"position", "search_bounds"} - set(value))
        if missing:
            raise _reject(f"parameters is missing fields: {', '.join(missing)}")
        option_type = "put" if method == "evaluate_roll_down" else "call"
        strike_key = "min_new_strike" if option_type == "put" else "max_new_strike"
        position = _scenario_position(value["position"], option_type=option_type)
        bounds = _search_bounds(value["search_bounds"], strike_key=strike_key)
        boundary = bounds.get(strike_key)
        old_strike = float(position["contract"]["strike"])
        if boundary is not None:
            if option_type == "put" and boundary >= old_strike:
                raise _reject("search_bounds.min_new_strike must be below the original strike")
            if option_type == "call" and boundary <= old_strike:
                raise _reject("search_bounds.max_new_strike must be above the original strike")
        return {
            "position": position,
            "search_bounds": bounds,
            "limit": _integer(value.get("limit", 10), "limit", minimum=1, maximum=50),
        }
    raise ContractError("NOT_FOUND", "unsupported group-query method")


def _request_context(value: Any) -> RequestContext:
    body = _object(value, {"tenant_key", "chat_id", "sender_open_id", "thread_id"},
                   "request_context")
    return RequestContext(
        tenant_key=_text(body["tenant_key"], "request_context.tenant_key"),
        chat_id=_text(body["chat_id"], "request_context.chat_id"),
        sender_open_id=_text(body["sender_open_id"], "request_context.sender_open_id"),
        thread_id=_optional_text(body["thread_id"], "request_context.thread_id"),
    )


def normalize_request(method: str, body: Any, *, now: datetime | None = None) -> NormalizedRequest:
    """Validate the outer envelope and every method parameter without I/O."""
    if method not in ROUTES:
        raise ContractError("NOT_FOUND", "unsupported group-query method")
    if not isinstance(body, dict):
        raise _reject("request body must be an object")
    scenario = method in SCENARIO_METHODS
    outer = {"schema_version", "request_id", "request_deadline_utc",
             "request_context", "parameters"}
    if scenario:
        outer = outer | {"input_id", "input_as_of_utc"}
    unknown = sorted(set(body) - outer)
    if unknown:
        raise _reject(f"request body contains unsupported fields: {', '.join(unknown)}")
    missing = sorted(outer - set(body))
    if missing:
        raise _reject(f"request body is missing fields: {', '.join(missing)}")
    if body["schema_version"] != REQUEST_SCHEMA:
        raise _reject(f"schema_version must be {REQUEST_SCHEMA}")
    request_id = _request_identity(body["request_id"])
    deadline = utc_datetime(body["request_deadline_utc"], "request_deadline_utc")
    context = _request_context(body["request_context"])
    parameters = _parameters(method, body["parameters"])
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if deadline <= current:
        raise ContractError("UPSTREAM_TIMEOUT",
                            "request deadline has already elapsed")
    input_id = None
    input_as_of = None
    if scenario:
        input_id = _text(body["input_id"], "input_id")
        input_as_of = utc_datetime(body["input_as_of_utc"], "input_as_of_utc")
        if input_as_of > current:
            raise _reject("input_as_of_utc must not be later than the evaluation time")
    return NormalizedRequest(
        method=method, request_id=request_id, deadline_utc=deadline,
        context=context, parameters=parameters,
        input_id=input_id, input_as_of_utc=input_as_of,
    )


RETRYABLE_ERRORS = frozenset({"RATE_LIMITED", "DEPENDENCY_UNAVAILABLE", "UPSTREAM_TIMEOUT"})


def success_envelope(request_id: str, data: dict[str, Any]) -> dict[str, Any]:
    return {"schema_version": RESPONSE_SCHEMA, "request_id": request_id,
            "ok": True, "data": data, "error": None, "warnings": []}


def error_envelope(request_id: str | None, code: str, message: str, *,
                   retry_after_seconds: int | None = None) -> dict[str, Any]:
    return {"schema_version": RESPONSE_SCHEMA, "request_id": request_id,
            "ok": False, "data": None, "warnings": [],
            "error": {"code": code, "message": message,
                      "retryable": code in RETRYABLE_ERRORS,
                      "retry_after_seconds": retry_after_seconds}}


__all__ = [
    "ALL_CAPABILITIES",
    "ALL_MARKETS",
    "CAPABILITY_CANDIDATES",
    "CAPABILITY_CLOSE",
    "CAPABILITY_MARKET_READ",
    "CAPABILITY_ROLL_DOWN",
    "CAPABILITY_ROLL_UP",
    "ContractError",
    "ERROR_STATUS",
    "NormalizedRequest",
    "PATH_TO_METHOD",
    "QUERY_METHODS",
    "REQUEST_SCHEMA",
    "RESPONSE_SCHEMA",
    "ROUTES",
    "SCENARIO_METHODS",
    "VerifiedPrincipal",
    "canonical_market_symbol",
    "error_envelope",
    "normalize_request",
    "success_envelope",
    "utc_datetime",
]
