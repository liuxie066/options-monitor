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
    def __init__(
        self,
        payload: object,
        *,
        version: str = API_VERSION,
        status: int = 200,
    ) -> None:
        self._body = json.dumps(payload).encode()
        self.headers = {"X-PM-API-Version": version}
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, _size: int) -> bytes:
        return self._body



def _valuation_receipt(accounts=None) -> dict:
    resolved = list(accounts or ["lx"])
    return {
        "success": True,
        "schema_version": "portfolio.valuation_evidence.v1",
        "status": "complete",
        "freshness": {
            "status": "fresh",
            "trust_status": "trusted",
            "observed_at_utc": "2026-07-24T01:00:00Z",
            "dataset_ids": [
                "pm.holdings_quantity",
                "pm.prices",
                "pm.fx",
            ],
            "reason_codes": [],
        },
        "retrieved_at_utc": "2026-07-24T01:00:01Z",
        "scope": {"accounts": resolved},
        "snapshot": {
            "snapshot_id": "valuation-1",
            "observed_at": "2026-07-24T01:00:00Z",
        },
        "holdings": [],
        "quotes": [],
        "account_status": [
            {"account": account, "status": "complete"}
            for account in resolved
        ],
        "warnings": [],
    }


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


def test_generic_distribution_view_remains_available() -> None:
    seen = {}

    def open_ok(request, *, timeout):
        seen.update(url=request.full_url, method=request.method, timeout=timeout)
        return _Response({"success": True, "accounts": ["lx"], "by_asset": []})

    result = PortfolioManagementClient(urlopen_fn=open_ok).read_view(
        "distribution",
        query={"account": "lx", "by_asset": "true"},
        timeout=12,
    )

    assert seen == {
        "url": "http://127.0.0.1:8765/api/v1/distribution?account=lx&by_asset=true",
        "method": "GET",
        "timeout": 12.0,
    }
    assert result == {"success": True, "accounts": ["lx"], "by_asset": []}


def test_client_maps_http_error_without_retry() -> None:
    calls = 0

    def fail(request, *, timeout):
        nonlocal calls
        calls += 1
        raise urllib.error.HTTPError(
            request.full_url,
            503,
            "unavailable",
            {"X-PM-API-Version": API_VERSION},
            io.BytesIO(
                json.dumps(
                    {
                        "success": False,
                        "error_code": "PM_SERVICE_UNAVAILABLE",
                        "message": "broker unavailable",
                        "request_id": "request-1",
                        "details": {},
                    }
                ).encode()
            ),
        )

    with pytest.raises(PortfolioManagementHTTPError) as raised:
        PortfolioManagementClient(urlopen_fn=fail).read_view("holdings", timeout=10)
    assert raised.value.status == 503
    assert raised.value.error_code == "PM_SERVICE_UNAVAILABLE"
    assert calls == 1


@pytest.mark.parametrize(
    ("headers", "payload", "error"),
    [
        ({"X-PM-API-Version": "portfolio.api.v2"}, {
            "success": False,
            "error_code": "INPUT_VALIDATION_ERROR",
            "message": "invalid input",
            "request_id": "request-1",
            "details": {},
        }, "version mismatch"),
        ({"X-PM-API-Version": API_VERSION}, {
            "success": False,
            "error_code": "INPUT_VALIDATION_ERROR",
            "message": "invalid input",
        }, "missing required fields"),
    ],
)
def test_versioned_http_error_requires_version_and_public_schema(
    headers, payload, error
) -> None:
    def fail(request, *, timeout):
        raise urllib.error.HTTPError(
            request.full_url,
            422,
            "invalid",
            headers,
            io.BytesIO(json.dumps(payload).encode()),
        )

    with pytest.raises(PortfolioManagementProtocolError, match=error):
        PortfolioManagementClient(urlopen_fn=fail).request_holdings_refresh(
            account="lx",
            request_id="stock-refresh:abc",
            timeout=2,
        )


def test_versioned_success_status_rejects_public_error_envelope() -> None:
    client = PortfolioManagementClient(
        urlopen_fn=lambda *_args, **_kwargs: _Response(
            {
                "success": False,
                "error_code": "PM_SERVICE_UNAVAILABLE",
                "message": "unavailable",
                "request_id": "request-1",
                "details": {},
            }
        )
    )

    with pytest.raises(PortfolioManagementProtocolError, match="success=true"):
        client.read_view("holdings", timeout=2)


def test_refresh_request_is_accepted_only_and_not_retried() -> None:
    seen = {}

    def open_ok(request, *, timeout):
        seen["url"] = request.full_url
        seen["body"] = json.loads(request.data)
        return _Response(
            {
                "success": True,
                "status": "accepted",
                "account": "lx",
                "request_id": "stock-refresh:abc",
            },
            status=202,
        )

    result = PortfolioManagementClient(urlopen_fn=open_ok).request_holdings_refresh(
        account="lx",
        request_id="stock-refresh:abc",
        timeout=2,
    )
    assert result["account"] == "lx"
    assert result["status"] == "accepted"
    assert seen["url"].endswith("/api/v1/futu/holdings/refresh-requests")
    assert seen["body"] == {
        "account": "lx",
        "request_id": "stock-refresh:abc",
    }


@pytest.mark.parametrize(
    ("mutator", "error"),
    [
        (lambda item: item.pop("request_id"), "missing required fields"),
        (lambda item: item.update(account="sy"), "account mismatch"),
        (lambda item: item.update(status="written"), "not accepted"),
        (lambda item: item.update(request_id="other"), "request id mismatch"),
    ],
)
def test_refresh_request_rejects_misbound_acceptance(mutator, error) -> None:
    receipt = {
        "success": True,
        "status": "accepted",
        "account": "lx",
        "request_id": "stock-refresh:abc",
    }
    mutator(receipt)
    client = PortfolioManagementClient(
        urlopen_fn=lambda *_args, **_kwargs: _Response(receipt, status=202)
    )

    with pytest.raises(PortfolioManagementProtocolError, match=error):
        client.request_holdings_refresh(
            account="lx",
            request_id="stock-refresh:abc",
            timeout=2,
        )


def test_refresh_request_rejects_200_success_status() -> None:
    client = PortfolioManagementClient(
        urlopen_fn=lambda *_args, **_kwargs: _Response(
            {
                "success": True,
                "status": "accepted",
                "account": "lx",
                "request_id": "stock-refresh:abc",
            },
            status=200,
        )
    )

    with pytest.raises(PortfolioManagementProtocolError, match="expected 202"):
        client.request_holdings_refresh(
            account="lx",
            request_id="stock-refresh:abc",
            timeout=2,
        )


@pytest.mark.parametrize(
    ("mutator", "error"),
    [
        (lambda item: item.pop("freshness"), "missing required fields"),
        (
            lambda item: item["scope"].update(accounts=["sy"]),
            "account scope mismatch",
        ),
        (
            lambda item: item["snapshot"].update(snapshot_id=""),
            "snapshot id",
        ),
        (
            lambda item: item["snapshot"].update(observed_at="not-a-time"),
            "snapshot observed_at",
        ),
        (
            lambda item: item["freshness"].update(status="FRESH"),
            "freshness evidence is invalid",
        ),
    ],
)
def test_valuation_evidence_rejects_missing_or_misbound_contract(
    mutator,
    error,
) -> None:
    receipt = _valuation_receipt()
    mutator(receipt)
    client = PortfolioManagementClient(
        urlopen_fn=lambda *_args, **_kwargs: _Response(receipt)
    )

    with pytest.raises(PortfolioManagementProtocolError, match=error):
        client.read_valuation_evidence(
            accounts=["lx"],
            supplemental_codes=[],
            price_timeout=10,
        )
