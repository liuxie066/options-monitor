from __future__ import annotations

import ipaddress
import json
import os
import socket
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable


DEFAULT_SERVICE_URL = "http://127.0.0.1:8765"
SERVICE_URL_ENV = "PORTFOLIO_SERVICE_URL"
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


class PortfolioHoldingsSyncError(RuntimeError):
    """Raised when the local portfolio holdings sync cannot be confirmed."""


def resolve_portfolio_service_origin(value: str | None = None) -> str:
    raw = str(value or os.environ.get(SERVICE_URL_ENV) or DEFAULT_SERVICE_URL).strip()
    try:
        parsed = urllib.parse.urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise PortfolioHoldingsSyncError(f"invalid {SERVICE_URL_ENV}: {exc}") from exc

    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise PortfolioHoldingsSyncError(
            f"{SERVICE_URL_ENV} must be an http(s) loopback URL"
        )
    if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
        raise PortfolioHoldingsSyncError(
            f"{SERVICE_URL_ENV} must contain only a loopback origin"
        )

    hostname = parsed.hostname.rstrip(".").lower()
    if hostname != "localhost":
        try:
            is_loopback = ipaddress.ip_address(hostname).is_loopback
        except ValueError:
            is_loopback = False
        if not is_loopback:
            raise PortfolioHoldingsSyncError(
                f"{SERVICE_URL_ENV} must use localhost or a loopback IP address"
            )

    host = f"[{hostname}]" if ":" in hostname else hostname
    authority = f"{host}:{port}" if port is not None else host
    return urllib.parse.urlunsplit((parsed.scheme, authority, "", "", ""))


def sync_portfolio_holdings(
    account: str,
    *,
    timeout_sec: float = 120.0,
    service_url: str | None = None,
    urlopen_fn: Callable[..., Any] = urllib.request.urlopen,
) -> dict[str, Any]:
    account_label = str(account or "").strip().lower()
    if not account_label:
        raise PortfolioHoldingsSyncError("account is required")

    body = json.dumps(
        {
            "account": account_label,
            "dry_run": False,
            "confirm": True,
            "allow_empty_stock_snapshot": False,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{resolve_portfolio_service_origin(service_url)}/futu/holdings/sync",
        data=body,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen_fn(request, timeout=float(timeout_sec)) as response:
            response_body = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read(MAX_RESPONSE_BYTES).decode("utf-8", errors="replace")
        except Exception:
            detail = ""
        suffix = f": {detail}" if detail else ""
        raise PortfolioHoldingsSyncError(
            f"portfolio-management HTTP {exc.code}{suffix}"
        ) from exc
    except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as exc:
        raise PortfolioHoldingsSyncError(
            f"portfolio-management request failed: {exc}"
        ) from exc

    if len(response_body) > MAX_RESPONSE_BYTES:
        raise PortfolioHoldingsSyncError(
            "portfolio-management response exceeds 8 MiB"
        )
    try:
        decoded = json.loads(response_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PortfolioHoldingsSyncError(
            "portfolio-management returned invalid JSON"
        ) from exc
    if not isinstance(decoded, dict):
        raise PortfolioHoldingsSyncError(
            "portfolio-management JSON response must be an object"
        )
    if decoded.get("success") is not True:
        message = str(
            decoded.get("error")
            or decoded.get("message")
            or decoded.get("detail")
            or "portfolio holdings sync did not confirm success=true"
        )
        raise PortfolioHoldingsSyncError(message)
    return decoded
