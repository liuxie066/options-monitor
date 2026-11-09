"""Transport for the explicitly enabled loopback performance query service."""

from __future__ import annotations

import ipaddress
from http.client import HTTPException
import json
import math
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from src.application.performance.query_contracts import PerformanceQueryError

API_VERSION = "option_performance.api.v1"
QUERY_PATH = "/api/v1/option-performance/query"
MAX_REQUEST_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
CLIENT_TIMEOUT_SECONDS = 10
ERROR_STATUS = {"INPUT_ERROR": 400, "AUTH_ERROR": 401, "SCOPE_ERROR": 403, "READ_ERROR": 503, "INTERNAL_ERROR": 500}


def decode_protocol_json(raw: bytes):
    def object_pairs(pairs):
        value = dict(pairs)
        if len(value) != len(pairs):
            raise ValueError("duplicate JSON field")
        return value

    def invalid_constant(value):
        raise ValueError("nonfinite JSON value")

    def finite_float(raw_value):
        value = float(raw_value)
        if not math.isfinite(value):
            raise ValueError("nonfinite JSON value")
        return value

    return json.loads(raw, parse_constant=invalid_constant, parse_float=finite_float, object_pairs_hook=object_pairs)


def validate_service_origin(url: str) -> str:
    try:
        parts = urlsplit(url)
        host = parts.hostname
        loopback = host == "localhost" or (host is not None and ipaddress.ip_address(host).is_loopback)
        if (
            parts.scheme != "http"
            or not loopback
            or parts.username is not None
            or parts.password is not None
            or parts.path
            or parts.query
            or parts.fragment
            or not parts.port
            or "?" in url
            or "#" in url
        ):
            raise ValueError
    except ValueError as exc:
        raise PerformanceQueryError(
            "INPUT_ERROR", "Performance service URL must be an http loopback origin with a port."
        ) from exc
    return url


def validate_service_token(token: str) -> None:
    if not token or any(ord(char) < 33 or ord(char) > 126 for char in token):
        raise PerformanceQueryError("INPUT_ERROR", "OM_PERFORMANCE_SERVICE_TOKEN must be a nonempty printable bearer token.")


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _validate_business_page(data: dict, payload: dict) -> None:
    paged = any(name in payload for name in ("view", "group_by", "symbol", "limit", "cursor"))
    view = (
        payload.get("view")
        or ("breakdowns" if any(name in payload for name in ("group_by", "symbol", "limit", "cursor")) else "summary")
    ) if paged else None
    if data.get("view") != view:
        raise ValueError
    required_objects = ("period", "scope", "coverage", "freshness", "quality", "breakdowns")
    if view in (None, "summary"):
        required_objects += (
            "option_net_cashflow", "sell_option_win_rate", "buy_option_win_rate", "option_return", "daily_occupied_capital",
        )
    if any(not isinstance(data.get(name), dict) for name in required_objects):
        raise ValueError
    # Additive report fields may be absent in older compatible business bodies.
    for name in ("period_option_net_cashflow", "metric_semantics"):
        if name in data and not isinstance(data[name], dict):
            raise ValueError
    if any(
        not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows)
        for rows in data["breakdowns"].values()
    ):
        raise ValueError
    if view in ("rows", "breakdowns") or "rows" in data or (view is None and payload.get("include_rows")):
        rows = data.get("rows")
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError
    if view not in ("rows", "breakdowns"):
        return
    if not isinstance(data.get("pagination"), dict) or not isinstance(data.get("source"), dict):
        raise ValueError
    pagination = data["pagination"]
    for name in ("offset", "matched_count"):
        if type(pagination.get(name)) is not int or pagination[name] < 0:
            raise ValueError
    for name in ("total_count", "returned_count", "scanned_count"):
        if name in pagination and (type(pagination[name]) is not int or pagination[name] < 0):
            raise ValueError
    if type(pagination.get("has_more")) is not bool or "page_cursor" not in pagination or "next_cursor" not in data:
        raise ValueError
    # Null cursors are valid when continuation signing is unavailable.
    for cursor in (pagination["page_cursor"], data["next_cursor"], pagination.get("next_cursor")):
        if cursor is not None and (not isinstance(cursor, str) or not cursor):
            raise ValueError
    if not isinstance(data["source"].get("content_hash"), str) or not data["source"]["content_hash"]:
        raise ValueError
    if view == "breakdowns":
        group = data.get("group_by")
        if not isinstance(group, str) or group not in data["breakdowns"]:
            raise ValueError


def query_option_performance(payload: dict, *, url: str, token: str) -> tuple[dict, list[str], dict]:
    origin = validate_service_origin(url)
    validate_service_token(token)
    if any(payload.get(name) not in (None, "") for name in ("config_path", "data_config")):
        raise PerformanceQueryError("INPUT_ERROR", "Service mode does not accept config_path or data_config overrides.")
    try:
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise PerformanceQueryError("INPUT_ERROR", "Performance query must be valid JSON.") from exc
    if len(body) > MAX_REQUEST_BYTES:
        raise PerformanceQueryError("INPUT_ERROR", "Performance query is too large.")
    request = Request(
        origin + QUERY_PATH,
        data=body,
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
        method="POST",
    )
    opener = build_opener(ProxyHandler({}), _NoRedirect())
    try:
        try:
            response = opener.open(request, timeout=CLIENT_TIMEOUT_SECONDS)
        except HTTPError as exc:
            response = exc
        with response:
            status = response.code
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except (URLError, OSError, HTTPException) as exc:
        raise PerformanceQueryError(
            "SERVICE_ERROR",
            "Performance query service is unavailable.",
            details={"reason_codes": ["service_unavailable"]},
        ) from exc
    try:
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ValueError
        result = decode_protocol_json(raw)
        if (
            not isinstance(result, dict)
            or result.get("schema_version") != API_VERSION
            or type(result.get("ok")) is not bool
        ):
            raise ValueError
        if result["ok"]:
            if (
                status != 200
                or not isinstance(result.get("data"), dict)
                or not isinstance(result.get("meta"), dict)
                or not isinstance(result.get("warnings"), list)
                or any(not isinstance(v, str) for v in result["warnings"])
                or result.get("error") is not None
            ):
                raise ValueError
            _validate_business_page(result["data"], payload)
            return result["data"], result["warnings"], result["meta"]
        error = result.get("error")
        if (
            not isinstance(error, dict)
            or error.get("code") not in ERROR_STATUS
            or status != ERROR_STATUS[error["code"]]
            or not isinstance(error.get("message"), str)
            or not error["message"]
            or ("hint" in error and not isinstance(error["hint"], str))
            or ("details" in error and not isinstance(error["details"], dict))
        ):
            raise ValueError
    except (ValueError, TypeError, KeyError, RecursionError) as exc:
        raise PerformanceQueryError(
            "SERVICE_ERROR",
            "Performance query service returned an invalid response.",
            details={"reason_codes": ["service_protocol_invalid"]},
        ) from exc
    raise PerformanceQueryError(error["code"], error["message"], hint=error.get("hint"), details=error.get("details"))
