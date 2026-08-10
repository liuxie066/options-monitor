from __future__ import annotations

import io
import json
import urllib.error

import pytest

from src.infrastructure.portfolio_management_client import (
    API_VERSION,
    DISTRIBUTION_VALUATION_CURRENCY,
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


def _sync_receipt(account: str = "lx") -> dict:
    return {
        "success": True,
        "status": "written",
        "account": account,
        "broker": "futu",
        "dry_run": False,
        "source": "futu-openapi",
        "source_snapshot_id": f"snapshot-{account}",
        "sync_run_id": f"sync-{account}",
        "receipt_persisted": True,
        "partial_write_possible": False,
        "stages": {
            name: {
                "status": "succeeded",
                "partial_write_possible": False,
            }
            for name in ("positions", "securities_cash", "fund_mmf")
        },
        "positions": [],
    }


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


def _distribution_receipt(account: str = "lx") -> dict:
    return {
        "success": True,
        "accounts": [account],
        "freshness": {
            "status": "fresh",
            "trust_status": "trusted",
            "observed_at_utc": "2026-08-09T10:00:00Z",
            "dataset_ids": ["pm.holdings", "pm.prices", "pm.fx"],
            "reason_codes": [],
        },
        "retrieved_at_utc": "2026-08-09T10:00:01Z",
        "by_asset": [
            {
                "code": "NVDA",
                "normalized_type": "stock",
                "currency": "USD",
                "quantity": 10,
                "value": 12345.0,
            }
        ],
        "errors": [],
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


def test_distribution_uses_fixed_single_account_query_and_contract() -> None:
    seen = {}

    def open_ok(request, *, timeout):
        seen.update(url=request.full_url, method=request.method, timeout=timeout)
        return _Response(_distribution_receipt("Feishu EXT"))

    result = PortfolioManagementClient(urlopen_fn=open_ok).read_distribution(
        account=" Feishu EXT ",
        timeout=12,
    )

    assert seen == {
        "url": (
            "http://127.0.0.1:8765/api/v1/distribution?"
            "account=Feishu+EXT&by_asset=true&include_value=true&group_cash=false"
        ),
        "method": "GET",
        "timeout": 12.0,
    }
    assert result["accounts"] == ["Feishu EXT"]
    assert result["by_asset"][0]["value"] == 12345.0
    assert DISTRIBUTION_VALUATION_CURRENCY == "CNY"


@pytest.mark.parametrize("account", ["", " ", "all", "ALL", "lx,sy"])
def test_distribution_rejects_non_single_account_without_request(account: str) -> None:
    calls = 0

    def open_never(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return _Response(_distribution_receipt())

    with pytest.raises(PortfolioManagementProtocolError, match="account"):
        PortfolioManagementClient(urlopen_fn=open_never).read_distribution(
            account=account,
            timeout=10,
        )
    assert calls == 0


@pytest.mark.parametrize(
    ("mutator", "error"),
    [
        (lambda item: item.pop("accounts"), "missing required fields"),
        (lambda item: item.update(accounts=["sy"]), "account scope mismatch"),
        (lambda item: item.update(accounts=["LX"]), "account scope mismatch"),
        (lambda item: item.update(accounts=["lx", "lx"]), "account scope mismatch"),
        (
            lambda item: item["freshness"].update(status="FRESH"),
            "freshness evidence is invalid",
        ),
        (
            lambda item: item["freshness"].update(trust_status="TRUSTED"),
            "freshness evidence is invalid",
        ),
        (
            lambda item: item["freshness"].update(observed_at_utc="bad"),
            "freshness observed_at_utc",
        ),
        (lambda item: item.update(by_asset=None), "by_asset must be an array"),
        (lambda item: item.update(errors={}), "errors must be an array"),
    ],
)
def test_distribution_rejects_missing_or_misbound_wire_contract(
    mutator,
    error,
) -> None:
    receipt = _distribution_receipt()
    mutator(receipt)
    client = PortfolioManagementClient(
        urlopen_fn=lambda *_args, **_kwargs: _Response(receipt)
    )

    with pytest.raises(PortfolioManagementProtocolError, match=error):
        client.read_distribution(account="lx", timeout=10)


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
        return _Response(_sync_receipt())

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


@pytest.mark.parametrize(
    ("mutator", "error"),
    [
        (lambda item: item.pop("source_snapshot_id"), "missing required fields"),
        (lambda item: item.update(account="sy"), "account mismatch"),
        (lambda item: item.update(dry_run=True), "real write"),
        (
            lambda item: item["stages"]["positions"].update(status="failed"),
            "stage positions",
        ),
    ],
)
def test_sync_holdings_rejects_unconfirmed_receipts(mutator, error) -> None:
    receipt = _sync_receipt()
    mutator(receipt)
    client = PortfolioManagementClient(
        urlopen_fn=lambda *_args, **_kwargs: _Response(receipt)
    )

    with pytest.raises(PortfolioManagementProtocolError, match=error):
        client.sync_holdings(account="lx", timeout=30)


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
