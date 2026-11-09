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
    has_page_options = any(name in payload for name in ("group_by", "symbol", "limit", "cursor"))
    expected_view = str(payload.get("view") or ("breakdowns" if has_page_options else "summary"))
    # Only unpaged requests may receive a legacy report without view.
    if (
        data.get("view", "summary") != expected_view
        or ("view" not in data and ("view" in payload or has_page_options))
    ):
        raise ValueError
    if any(not isinstance(data.get(name), dict) for name in (
        "period", "scope", "coverage", "freshness", "quality", "breakdowns",
    )):
        raise ValueError
    if "metric_semantics" in data and not isinstance(data["metric_semantics"], dict):
        raise ValueError
    if "schema_version" in data and not isinstance(data["schema_version"], str):
        raise ValueError

    def object_rows(value):
        return isinstance(value, list) and all(isinstance(row, dict) for row in value)

    if any(not object_rows(rows) for rows in data["breakdowns"].values()):
        raise ValueError
    if "rows" in data and not object_rows(data["rows"]):
        raise ValueError
    if expected_view == "summary":
        if any(not isinstance(data.get(name), dict) for name in (
            "period_option_net_cashflow", "option_net_cashflow", "sell_option_win_rate",
            "buy_option_win_rate", "option_return", "daily_occupied_capital",
        )):
            raise ValueError
        return
    if expected_view not in {"rows", "breakdowns"}:
        raise ValueError
    if (
        not object_rows(data.get("rows"))
        or not isinstance(data.get("source"), dict)
        or not isinstance(data.get("pagination"), dict)
    ):
        raise ValueError
    pagination = data["pagination"]
    if any(type(pagination.get(name)) is not int for name in (
        "offset", "total_count", "matched_count", "returned_count", "scanned_count",
    )) or type(pagination.get("has_more")) is not bool:
        raise ValueError
    if (
        "next_cursor" not in data
        or (data["next_cursor"] is not None and not isinstance(data["next_cursor"], str))
        or (pagination.get("page_cursor") is not None and not isinstance(pagination["page_cursor"], str))
    ):
        raise ValueError
    if expected_view == "breakdowns" and (
        data.get("group_by") != str(payload.get("group_by") or "symbols")
        or data.get("group_by") not in data["breakdowns"]
    ):
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
