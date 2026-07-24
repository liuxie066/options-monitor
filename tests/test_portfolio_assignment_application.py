from __future__ import annotations

import json
from urllib.parse import urlsplit

import pytest

from src.application import portfolio_assignment_scenario as application


class _Response:
    def __init__(self, payload):
        self.body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self, size=-1):
        return self.body if size < 0 else self.body[:size]


def test_normalize_assignment_accounts_trims_lowercases_and_deduplicates():
    assert application.normalize_assignment_accounts([" LX ", "sy", "lx"]) == ["lx", "sy"]

    with pytest.raises(application.AssignmentScenarioInputError, match="at least one"):
        application.normalize_assignment_accounts([])
    with pytest.raises(application.AssignmentScenarioInputError, match="invalid account"):
        application.normalize_assignment_accounts(["bad account"])


def test_valuation_evidence_client_posts_to_fixed_loopback_endpoint(monkeypatch):
    seen = {}

    def fake_urlopen(request, timeout):
        seen["request"] = request
        seen["timeout"] = timeout
        return _Response(
            {
                "schema_version": "portfolio.valuation_evidence.v1",
                "success": True,
                "status": "complete",
                "holdings": [],
                "quotes": [],
            }
        )

    monkeypatch.delenv(application.SERVICE_URL_ENV, raising=False)
    monkeypatch.setattr(application.urllib.request, "urlopen", fake_urlopen)

    result = application.read_portfolio_valuation_evidence(
        accounts=["lx", "sy"],
        supplemental_codes=["NVDA"],
        price_timeout=7,
    )

    request = seen["request"]
    assert urlsplit(request.full_url).scheme == "http"
    assert urlsplit(request.full_url).netloc == "127.0.0.1:8765"
    assert urlsplit(request.full_url).path == "/analysis/valuation-evidence"
    assert request.get_method() == "POST"
    assert json.loads(request.data) == {
        "accounts": ["lx", "sy"],
        "supplemental_codes": ["NVDA"],
        "price_timeout": 7,
    }
    assert seen["timeout"] == 17
    assert result["status"] == "complete"


def test_valuation_evidence_client_rejects_non_loopback_url(monkeypatch):
    monkeypatch.setenv(
        application.SERVICE_URL_ENV,
        "https://portfolio.example.com",
    )

    with pytest.raises(application.PortfolioEvidenceReadError, match="loopback"):
        application.read_portfolio_valuation_evidence(
            accounts=["lx"],
            supplemental_codes=[],
        )


def test_query_assignment_scenario_reads_only_open_short_underlyings(monkeypatch):
    positions = [
        {
            "record_id": "p1",
            "account": "lx",
            "broker": "富途",
            "symbol": "NVDA",
            "option_type": "put",
            "side": "short",
            "status": "open",
            "contracts_open": 1,
            "multiplier": 100,
            "strike": 100,
            "currency": "USD",
            "expiration_ymd": "2026-08-28",
        }
    ]
    seen = {}
    monkeypatch.setattr(
        application,
        "_load_runtime_and_positions",
        lambda accounts: (positions, "config.us.json"),
    )

    def evidence_reader(*, accounts, supplemental_codes, price_timeout=30):
        seen["accounts"] = accounts
        seen["supplemental_codes"] = supplemental_codes
        return {
            "schema_version": "portfolio.valuation_evidence.v1",
            "success": True,
            "status": "complete",
            "snapshot": {
                "snapshot_id": "valuation-1",
                "observed_at": "2026-07-24T01:00:00+00:00",
            },
            "holdings": [],
            "quotes": [
                {
                    "code": "NVDA",
                    "currency": "USD",
                    "price_native": 120,
                    "price_cny": 864,
                    "exchange_rate_to_cny": 7.2,
                    "source": "test",
                }
            ],
            "warnings": [],
        }

    monkeypatch.setattr(
        application,
        "read_portfolio_valuation_evidence",
        evidence_reader,
    )

    result = application.query_portfolio_assignment_scenario([" LX ", "lx"])

    assert seen == {"accounts": ["lx"], "supplemental_codes": ["NVDA"]}
    assert result["scope"]["accounts"] == ["lx"]
    assert result["scope"]["include_long_options"] is False
    assert result["summary"]["assignment_count"] == 1
    assert result["snapshot"]["portfolio_snapshot_id"] == "valuation-1"
    assert result["snapshot"]["runtime_config"] == "config.us.json"


def test_query_returns_business_unavailable_when_portfolio_source_is_down(monkeypatch):
    monkeypatch.setattr(
        application,
        "_load_runtime_and_positions",
        lambda accounts: ([], "config.us.json"),
    )
    monkeypatch.setattr(
        application,
        "read_portfolio_valuation_evidence",
        lambda **_kwargs: (_ for _ in ()).throw(
            application.PortfolioEvidenceReadError("service down")
        ),
    )

    result = application.query_portfolio_assignment_scenario(["lx"])

    assert result["status"] == "unavailable"
    assert "service down" in result["warnings"]


def test_query_returns_business_unavailable_when_option_ledger_is_down(monkeypatch):
    monkeypatch.setattr(
        application,
        "_load_runtime_and_positions",
        lambda accounts: (_ for _ in ()).throw(RuntimeError("ledger down")),
    )

    result = application.query_portfolio_assignment_scenario(["lx"])

    assert result["status"] == "unavailable"
    assert result["summary"]["assignment_count"] == 0
    assert any("ledger down" in warning for warning in result["warnings"])
