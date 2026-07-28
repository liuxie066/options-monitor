from __future__ import annotations

import json
import urllib.error

from src.infrastructure.portfolio_holdings_sync_client import (
    PortfolioHoldingsSyncError,
    PortfolioHoldingsSyncUnknownError,
    resolve_portfolio_service_origin,
    sync_portfolio_holdings,
)


class _Response:
    def __init__(self, payload: dict) -> None:
        self._body = json.dumps(payload).encode("utf-8")
        self.headers = {"X-PM-API-Version": "portfolio.api.v1"}

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


def test_sync_portfolio_holdings_posts_fail_closed_absolute_sync() -> None:
    observed: dict = {}

    def _urlopen(request, *, timeout):
        observed["url"] = request.full_url
        observed["method"] = request.method
        observed["timeout"] = timeout
        observed["body"] = json.loads(request.data.decode("utf-8"))
        return _Response(_sync_receipt())

    out = sync_portfolio_holdings(
        "LX",
        timeout_sec=30,
        service_url="http://127.0.0.1:8765",
        urlopen_fn=_urlopen,
    )

    assert out == _sync_receipt()
    assert observed == {
        "url": "http://127.0.0.1:8765/api/v1/futu/holdings/sync",
        "method": "POST",
        "timeout": 30.0,
        "body": {
            "account": "lx",
            "dry_run": False,
            "confirm": True,
            "allow_empty_stock_snapshot": False,
        },
    }


def test_portfolio_service_url_must_be_loopback() -> None:
    try:
        resolve_portfolio_service_origin("https://portfolio.example.com")
    except PortfolioHoldingsSyncError as exc:
        assert "loopback" in str(exc)
    else:
        raise AssertionError("expected PortfolioHoldingsSyncError")


def test_sync_portfolio_holdings_rejects_success_false() -> None:
    def _urlopen(_request, *, timeout):
        assert timeout == 5.0
        return _Response({"success": False, "error": "invalid average_cost"})

    try:
        sync_portfolio_holdings(
            "lx",
            timeout_sec=5,
            urlopen_fn=_urlopen,
        )
    except PortfolioHoldingsSyncError as exc:
        assert "invalid average_cost" in str(exc)
    else:
        raise AssertionError("expected PortfolioHoldingsSyncError")


def test_sync_portfolio_holdings_requires_explicit_success_confirmation() -> None:
    def _urlopen(_request, *, timeout):
        assert timeout == 5.0
        return _Response({"status": "written"})

    try:
        sync_portfolio_holdings(
            "lx",
            timeout_sec=5,
            urlopen_fn=_urlopen,
        )
    except PortfolioHoldingsSyncError as exc:
        assert "missing required fields" in str(exc)
    else:
        raise AssertionError("expected PortfolioHoldingsSyncError")


def test_transport_failure_is_reported_as_unknown_not_safe_to_retry() -> None:
    def _urlopen(request, *, timeout):
        raise urllib.error.URLError("response lost")

    try:
        sync_portfolio_holdings(
            "lx",
            timeout_sec=5,
            urlopen_fn=_urlopen,
        )
    except PortfolioHoldingsSyncUnknownError as exc:
        assert "response lost" in str(exc)
    else:
        raise AssertionError("expected PortfolioHoldingsSyncUnknownError")
