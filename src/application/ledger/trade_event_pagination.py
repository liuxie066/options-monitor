"""Stateless cursor primitives for the canonical trade-event stream."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import time
import uuid
from typing import Any, Mapping


CURSOR_VERSION = 1
CURSOR_TTL_SECONDS = 30 * 60
DEFAULT_LIMIT = 10
MAX_LIMIT = 20
TRADE_EVENT_ORDER = "trade_time_ms_desc,event_id_desc"
TRADE_EVENT_TOOL = "option_positions_read"
TRADE_EVENT_ACTION = "events"

_QUERY_FIELDS = (
    "account",
    "broker",
    "symbol",
    "option_type",
    "strike",
    "expiration_ymd",
    "market",
    "position_effect",
)


class TradeEventPaginationError(ValueError):
    """A safe, explicit validation failure for event pagination."""

    def __init__(self, message: str, *, code: str = "invalid_cursor"):
        self.code = code
        super().__init__(message)


def normalize_event_limit(value: Any) -> int:
    if isinstance(value, str) and value.strip().lower() in {"all", "*"}:
        raise TradeEventPaginationError(
            "unbounded event detail requires narrower filters or a canonical aggregate",
            code="needs_narrowing",
        )
    if isinstance(value, bool):
        raise TradeEventPaginationError(
            "events limit must be an integer",
            code="invalid_limit",
        )
    if isinstance(value, float) and not value.is_integer():
        raise TradeEventPaginationError(
            "events limit must be an integer",
            code="invalid_limit",
        )
    try:
        limit = int(value)
    except (TypeError, ValueError) as exc:
        raise TradeEventPaginationError(
            "events limit must be an integer",
            code="invalid_limit",
        ) from exc
    if limit < 1 or limit > MAX_LIMIT:
        raise TradeEventPaginationError(
            f"events limit must be between 1 and {MAX_LIMIT}",
            code="invalid_limit",
        )
    return limit


def normalize_event_query(
    payload: Mapping[str, Any],
    *,
    account: str | None,
    market: str,
) -> dict[str, Any]:
    normalized_market = str(market or "").strip().upper()
    if normalized_market not in {"US", "HK"}:
        raise TradeEventPaginationError(
            "events market must be US or HK",
            code="invalid_query",
        )

    position_effect = str(payload.get("position_effect") or "").strip().lower() or None
    if position_effect not in (None, "close"):
        raise TradeEventPaginationError(
            "position_effect must be close when provided",
            code="invalid_query",
        )

    option_type = str(payload.get("option_type") or "").strip().lower() or None
    if option_type not in (None, "put", "call"):
        raise TradeEventPaginationError(
            "option_type must be put or call when provided",
            code="invalid_query",
        )

    strike: float | None = None
    if payload.get("strike") not in (None, ""):
        try:
            strike = float(payload["strike"])
        except (TypeError, ValueError) as exc:
            raise TradeEventPaginationError(
                "strike must be numeric",
                code="invalid_query",
            ) from exc
        if not math.isfinite(strike):
            raise TradeEventPaginationError(
                "strike must be finite",
                code="invalid_query",
            )

    return {
        "account": str(account or "").strip().lower() or None,
        "broker": str(payload.get("broker") or "").strip() or None,
        "symbol": str(payload.get("symbol") or "").strip().upper() or None,
        "option_type": option_type,
        "strike": strike,
        "expiration_ymd": str(payload.get("exp") or payload.get("expiration_ymd") or "").strip() or None,
        "market": normalized_market,
        "position_effect": position_effect,
    }


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def encode_cursor(
    state: Mapping[str, Any],
    key: str,
    *,
    now: int | None = None,
) -> str:
    secret = str(key or "").strip()
    if not secret:
        raise TradeEventPaginationError(
            "cursor signing key is unavailable",
            code="cursor_key_unavailable",
        )
    issued = int(time.time() if now is None else now)
    body = dict(state)
    body["iat"] = issued
    body["exp"] = issued + CURSOR_TTL_SECONDS
    raw = json.dumps(
        body,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    signature = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).digest()
    return f"{_b64(raw)}.{_b64(signature)}"


def decode_cursor(
    cursor: str,
    key: str,
    *,
    now: int | None = None,
) -> dict[str, Any]:
    secret = str(key or "").strip()
    if not secret:
        raise TradeEventPaginationError(
            "cursor signing key is unavailable",
            code="cursor_key_unavailable",
        )
    try:
        encoded, encoded_signature = str(cursor).split(".", 1)
        raw = _unb64(encoded)
        supplied_signature = _unb64(encoded_signature)
        expected_signature = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).digest()
        if not hmac.compare_digest(expected_signature, supplied_signature):
            raise TradeEventPaginationError(
                "invalid cursor signature",
                code="invalid_cursor_signature",
            )
        state = json.loads(raw)
    except TradeEventPaginationError:
        raise
    except Exception as exc:
        raise TradeEventPaginationError(
            "invalid cursor",
            code="invalid_cursor",
        ) from exc
    if not isinstance(state, dict):
        raise TradeEventPaginationError(
            "invalid cursor state",
            code="invalid_cursor",
        )

    current = int(time.time() if now is None else now)
    issued = _required_int(state, "iat")
    expires = _required_int(state, "exp")
    if issued > current:
        raise TradeEventPaginationError(
            "cursor issued-at time is in the future",
            code="invalid_cursor",
        )
    if current >= expires:
        raise TradeEventPaginationError(
            "cursor expired; start a new query (new results may overlap)",
            code="cursor_expired",
        )
    return state


def new_stream_state(
    *,
    query: Mapping[str, Any],
    authority_scope: Mapping[str, Any],
    snapshot_max_ingest_seq: int,
    as_of: str,
) -> dict[str, Any]:
    canonical_query = {name: query.get(name) for name in _QUERY_FIELDS}
    return {
        "version": CURSOR_VERSION,
        "tool": TRADE_EVENT_TOOL,
        "action": TRADE_EVENT_ACTION,
        "query": canonical_query,
        "authority_scope": dict(authority_scope),
        "order": TRADE_EVENT_ORDER,
        "snapshot_max_ingest_seq": int(snapshot_max_ingest_seq),
        "last_trade_time_ms": None,
        "last_event_id": None,
        "stream_id": f"tev_{uuid.uuid4().hex}",
        "as_of": str(as_of),
    }


def validate_cursor_state(
    state: Mapping[str, Any],
    *,
    authority_scope: Mapping[str, Any],
) -> dict[str, Any]:
    if state.get("version") != CURSOR_VERSION:
        raise _cursor_mismatch("cursor version mismatch")
    if state.get("tool") != TRADE_EVENT_TOOL or state.get("action") != TRADE_EVENT_ACTION:
        raise _cursor_mismatch("cursor tool mismatch")
    if state.get("order") != TRADE_EVENT_ORDER:
        raise _cursor_mismatch("cursor order mismatch")
    if state.get("authority_scope") != dict(authority_scope):
        raise TradeEventPaginationError(
            "cursor authority mismatch",
            code="cursor_authority_mismatch",
        )

    query = state.get("query")
    if not isinstance(query, dict) or set(query) != set(_QUERY_FIELDS):
        raise _cursor_mismatch("cursor query is invalid")
    fence = _required_int(state, "snapshot_max_ingest_seq")
    if fence < 0:
        raise _cursor_mismatch("cursor snapshot boundary is invalid")
    last_trade_time_ms = state.get("last_trade_time_ms")
    last_event_id = state.get("last_event_id")
    if (last_trade_time_ms is None) != (last_event_id is None):
        raise _cursor_mismatch("cursor last key is incomplete")
    if last_trade_time_ms is not None:
        _required_int(state, "last_trade_time_ms")
        if not isinstance(last_event_id, str) or not last_event_id:
            raise _cursor_mismatch("cursor last event id is invalid")
    if not isinstance(state.get("stream_id"), str) or not state.get("stream_id"):
        raise _cursor_mismatch("cursor stream id is invalid")
    if not isinstance(state.get("as_of"), str) or not state.get("as_of"):
        raise _cursor_mismatch("cursor as-of is invalid")
    return {name: query.get(name) for name in _QUERY_FIELDS}


def resolve_continuation_query(
    state_query: Mapping[str, Any],
    payload: Mapping[str, Any],
    *,
    account: str | None,
    market: str,
) -> dict[str, Any]:
    """Reuse signed filters and reject only explicitly repeated conflicts."""

    candidate = normalize_event_query(payload, account=account, market=market)
    explicit_fields: set[str] = {"market"}
    if "account" in payload:
        explicit_fields.add("account")
    if "broker" in payload:
        explicit_fields.add("broker")
    if "symbol" in payload:
        explicit_fields.add("symbol")
    if "option_type" in payload:
        explicit_fields.add("option_type")
    if "strike" in payload:
        explicit_fields.add("strike")
    if "exp" in payload or "expiration_ymd" in payload:
        explicit_fields.add("expiration_ymd")
    if "position_effect" in payload:
        explicit_fields.add("position_effect")

    for field in explicit_fields:
        if candidate.get(field) != state_query.get(field):
            raise TradeEventPaginationError(
                f"cursor filter mismatch: {field}",
                code="cursor_query_mismatch",
            )
    return {name: state_query.get(name) for name in _QUERY_FIELDS}


def _required_int(state: Mapping[str, Any], field: str) -> int:
    value = state.get(field)
    if isinstance(value, bool) or not isinstance(value, int):
        raise _cursor_mismatch(f"cursor {field} is invalid")
    return int(value)


def _cursor_mismatch(message: str) -> TradeEventPaginationError:
    return TradeEventPaginationError(message, code="invalid_cursor")


__all__ = [
    "CURSOR_TTL_SECONDS",
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "TRADE_EVENT_ORDER",
    "TradeEventPaginationError",
    "decode_cursor",
    "encode_cursor",
    "new_stream_state",
    "normalize_event_limit",
    "normalize_event_query",
    "resolve_continuation_query",
    "validate_cursor_state",
]
