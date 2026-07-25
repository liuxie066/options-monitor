from __future__ import annotations

import json

from src.infrastructure.portfolio_holdings_sync_client import (
    PortfolioHoldingsSyncError,
    resolve_portfolio_service_origin,
    sync_portfolio_holdings,
)


class _Response:
    def __init__(self, payload: dict) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, _size: int) -> bytes:
        return self._body


def test_sync_portfolio_holdings_posts_fail_closed_absolute_sync() -> None:
    observed: dict = {}

    def _urlopen(request, *, timeout):
        observed["url"] = request.full_url
        observed["method"] = request.method
        observed["timeout"] = timeout
        observed["body"] = json.loads(request.data.decode("utf-8"))
        return _Response({"success": True, "account": "lx"})

    out = sync_portfolio_holdings(
        "LX",
        timeout_sec=30,
        service_url="http://127.0.0.1:8765",
        urlopen_fn=_urlopen,
    )

    assert out == {"success": True, "account": "lx"}
    assert observed == {
        "url": "http://127.0.0.1:8765/futu/holdings/sync",
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
        assert "success=true" in str(exc)
    else:
        raise AssertionError("expected PortfolioHoldingsSyncError")
