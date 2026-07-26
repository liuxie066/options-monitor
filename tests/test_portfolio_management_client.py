from __future__ import annotations

import io
import json
import urllib.error

import pytest

from src.infrastructure.portfolio_management_client import (
    API_VERSION,
    PortfolioManagementClient,
    PortfolioManagementConfigError,
    PortfolioManagementHTTPError,
    PortfolioManagementProtocolError,
    resolve_portfolio_service_origin,
)


class _Response:
    def __init__(self, payload: object, *, version: str = API_VERSION) -> None:
        self._body = json.dumps(payload).encode()
        self.headers = {"X-PM-API-Version": version}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, _size: int) -> bytes:
        return self._body


def test_origin_accepts_ipv4_ipv6_and_rejects_remote_or_embedded_paths() -> None:
    assert resolve_portfolio_service_origin("http://127.0.0.1:8765") == "http://127.0.0.1:8765"
    assert resolve_portfolio_service_origin("http://[::1]:8765") == "http://[::1]:8765"
    for value in ("https://pm.example.com", "http://127.0.0.1:8765/api", "http://user@127.0.0.1"):
        with pytest.raises(PortfolioManagementConfigError):
            resolve_portfolio_service_origin(value)


def test_client_uses_v1_path_and_requires_version_header() -> None:
    seen = {}

    def open_ok(request, *, timeout):
        seen.update(url=request.full_url, method=request.method, timeout=timeout)
        return _Response({"success": True, "accounts": ["lx"]})

    result = PortfolioManagementClient(urlopen_fn=open_ok).read_view(
        "accounts",
        query={"include_default": "false"},
        timeout=10,
    )
    assert result["accounts"] == ["lx"]
    assert seen == {
        "url": "http://127.0.0.1:8765/api/v1/accounts?include_default=false",
        "method": "GET",
        "timeout": 10.0,
    }

    with pytest.raises(PortfolioManagementProtocolError, match="version mismatch"):
        PortfolioManagementClient(
            urlopen_fn=lambda *_args, **_kwargs: _Response(
                {"success": True},
                version="",
            )
        ).read_view("accounts", timeout=10)


def test_client_maps_http_error_without_retry() -> None:
    calls = 0

    def fail(request, *, timeout):
        nonlocal calls
        calls += 1
        raise urllib.error.HTTPError(
            request.full_url,
            503,
            "unavailable",
            {},
            io.BytesIO(
                json.dumps(
                    {
                        "success": False,
                        "error_code": "PM_SERVICE_UNAVAILABLE",
                        "message": "broker unavailable",
                    }
                ).encode()
            ),
        )

    with pytest.raises(PortfolioManagementHTTPError) as raised:
        PortfolioManagementClient(urlopen_fn=fail).read_view("holdings", timeout=10)
    assert raised.value.status == 503
    assert raised.value.error_code == "PM_SERVICE_UNAVAILABLE"
    assert calls == 1


def test_sync_holdings_is_absolute_confirmed_and_not_retried() -> None:
    seen = {}

    def open_ok(request, *, timeout):
        seen["url"] = request.full_url
        seen["body"] = json.loads(request.data)
        return _Response({"success": True, "account": "lx"})

    result = PortfolioManagementClient(urlopen_fn=open_ok).sync_holdings(
        account="lx",
        timeout=30,
    )
    assert result["account"] == "lx"
    assert seen["url"].endswith("/api/v1/futu/holdings/sync")
    assert seen["body"] == {
        "account": "lx",
        "dry_run": False,
        "confirm": True,
        "allow_empty_stock_snapshot": False,
    }
