from __future__ import annotations

import json
import socket
import urllib.error
from urllib.parse import parse_qs, urlsplit

import pytest

from src.application.agent_tool_contracts import AgentToolError
from src.application.agent_tool_registry import get_tool_definition, pure_read_toolsets
from src.application.agent_tools import portfolio


class _Response:
    def __init__(self, payload):
        self.body = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self, size=-1):
        return self.body if size < 0 else self.body[:size]


def _call(payload, monkeypatch, response):
    seen = {}

    def fake_urlopen(request, timeout):
        seen["request"] = request
        seen["timeout"] = timeout
        return _Response(response)

    monkeypatch.setattr(portfolio.urllib.request, "urlopen", fake_urlopen)
    definition = get_tool_definition("portfolio_query")
    assert definition is not None
    data, warnings, meta = definition.call(payload)
    return data, warnings, meta, seen


def test_portfolio_tools_share_one_pure_read_toolset() -> None:
    definition = get_tool_definition("portfolio_query")

    assert definition is not None
    assert definition.is_pure_read() is True
    assert definition.side_effects == ()
    assert definition.requires_confirm is False
    assert definition.safe_default_input == {"view": "health"}
    assert "url" not in definition.input_schema
    assert pure_read_toolsets()["portfolio"] == (
        "portfolio_query",
        "portfolio_pnl_bridge",
        "portfolio_cash_bridge",
        "portfolio_capital_bridge",
    )


def test_portfolio_query_preserves_payload_and_adds_evidence_metadata(monkeypatch) -> None:
    monkeypatch.delenv(portfolio.SERVICE_URL_ENV, raising=False)

    data, warnings, meta, seen = _call(
        {"view": "overview", "accounts": ["lx", "sy"], "include_details": True},
        monkeypatch,
        {"success": True, "accounts": ["lx", "sy"], "total_value": 123.45},
    )

    parsed = urlsplit(seen["request"].full_url)
    assert parsed.scheme == "http"
    assert parsed.netloc == "127.0.0.1:8765"
    assert parsed.path == "/accounts/overview"
    assert parse_qs(parsed.query) == {"accounts": ["lx,sy"], "include_details": ["true"]}
    assert seen["request"].get_method() == "GET"
    assert data["success"] is True
    assert data["total_value"] == 123.45
    assert data["source"] == {"service": "portfolio-management", "transport": "loopback_http"}
    assert data["scope"] == {"view": "overview", "accounts": ["lx", "sy"]}
    assert data["freshness"]["status"] == "live"
    assert data["freshness"]["observed_at"].endswith("+00:00")
    assert warnings == []
    assert meta == {}


@pytest.mark.parametrize(
    ("payload", "path"),
    [
        ({"view": "health"}, "/health"),
        ({"view": "accounts", "include_default": False}, "/accounts"),
        ({"view": "holdings", "account": "lx"}, "/holdings"),
        ({"view": "cash", "account": "lx"}, "/cash"),
        ({"view": "nav", "account": "lx", "days": 14}, "/nav"),
        ({"view": "distribution", "accounts": ["lx", "sy"]}, "/distribution"),
        ({"view": "full_report", "account": "lx"}, "/report/full"),
    ],
)
def test_portfolio_query_maps_supported_views_to_get_endpoints(monkeypatch, payload, path) -> None:
    _, _, _, seen = _call(payload, monkeypatch, {"success": True})

    assert urlsplit(seen["request"].full_url).path == path
    assert seen["request"].get_method() == "GET"


def test_portfolio_query_rejects_non_loopback_service_url(monkeypatch) -> None:
    monkeypatch.setenv(portfolio.SERVICE_URL_ENV, "http://portfolio.internal:8765")
    definition = get_tool_definition("portfolio_query")
    assert definition is not None

    with pytest.raises(AgentToolError, match="loopback") as exc_info:
        definition.call({"view": "health"})

    assert exc_info.value.code == "CONFIG_ERROR"


def test_portfolio_query_rejects_model_provided_endpoint_fields() -> None:
    definition = get_tool_definition("portfolio_query")
    assert definition is not None

    with pytest.raises(AgentToolError, match="endpoint fields") as exc_info:
        definition.call({"view": "health", "service_url": "http://127.0.0.1:9999"})

    assert exc_info.value.code == "INPUT_ERROR"


def test_portfolio_query_requires_account_for_account_scoped_views() -> None:
    definition = get_tool_definition("portfolio_query")
    assert definition is not None

    with pytest.raises(AgentToolError, match="account is required") as exc_info:
        definition.call({"view": "holdings"})

    assert exc_info.value.code == "INPUT_ERROR"


def test_portfolio_query_converts_service_failure_to_agent_tool_error(monkeypatch) -> None:
    definition = get_tool_definition("portfolio_query")
    assert definition is not None
    monkeypatch.setattr(
        portfolio.urllib.request,
        "urlopen",
        lambda request, timeout: _Response({"success": False, "error": "missing holdings table"}),
    )

    with pytest.raises(AgentToolError, match="missing holdings table") as exc_info:
        definition.call({"view": "health"})

    assert exc_info.value.code == "READ_ERROR"


@pytest.mark.parametrize(
    "failure",
    [
        urllib.error.URLError("connection refused"),
        socket.timeout("read timed out"),
        urllib.error.HTTPError("http://127.0.0.1:8765/health", 503, "unavailable", None, None),
    ],
)
def test_portfolio_query_converts_transport_failures_to_agent_tool_error(monkeypatch, failure) -> None:
    definition = get_tool_definition("portfolio_query")
    assert definition is not None

    def fail(request, timeout):
        raise failure

    monkeypatch.setattr(portfolio.urllib.request, "urlopen", fail)

    with pytest.raises(AgentToolError) as exc_info:
        definition.call({"view": "health"})

    assert exc_info.value.code == "READ_ERROR"


def test_portfolio_query_rejects_invalid_json(monkeypatch) -> None:
    definition = get_tool_definition("portfolio_query")
    assert definition is not None
    monkeypatch.setattr(portfolio.urllib.request, "urlopen", lambda request, timeout: _Response(b"not-json"))

    with pytest.raises(AgentToolError, match="invalid JSON") as exc_info:
        definition.call({"view": "health"})

    assert exc_info.value.code == "READ_ERROR"



def _bridge_facts(account: str, *, end_date: str = "2026-07-16") -> dict:
    return {
        "schema_version": "portfolio.capital_facts.v1",
        "success": True,
        "status": "ok",
        "account": account,
        "period": {
            "kind": "mtd",
            "requested_as_of_month": "2026-07",
            "calendar_start": "2026-07-01",
            "anchor_date": "2026-06-30",
            "end_date": end_date,
            "timezone": "Asia/Shanghai",
        },
        "amounts": {
            "currency": "CNY",
            "opening_assets": 1000.0,
            "external_cash_flow": 100.0,
            "period_pnl": 150.0,
            "ending_assets": 1250.0,
        },
        "reconciliation": {"status": "ok"},
    }


def _cash_bridge_facts(account: str, *, end_date: str = "2026-07-16") -> dict:
    return {
        "schema_version": "portfolio.cash_facts.v1",
        "success": True,
        "status": "ok",
        "account": account,
        "period": {
            "kind": "mtd",
            "requested_as_of_month": "2026-07",
            "calendar_start": "2026-07-01",
            "anchor_date": "2026-06-30",
            "end_date": end_date,
            "timezone": "Asia/Shanghai",
        },
        "amounts": {
            "currency": "CNY",
            "opening_cash": 500.0,
            "external_cash_flow": 100.0,
            "ending_cash": 550.0,
        },
    }


def _option_performance(account: str, *, end_date: str = "2026-07-16") -> dict:
    return {
        "period": {
            "kind": "mtd",
            "requested_end_date": end_date,
            "reporting_timezone": "Asia/Shanghai",
        },
        "scope": {"account": account},
        "quality": {"status": "observed", "evidence_fact_ids": []},
        "cash": {
            "total_cash_change_net": {"cny": -200.0, "status": "observed"},
        },
        "pnl": {
            "period_total_net": {"cny": 40.0, "status": "observed"},
        },
    }


def test_portfolio_capital_bridge_reads_capital_facts_endpoint(monkeypatch) -> None:
    seen = {}

    def fake_urlopen(request, timeout):
        seen["request"] = request
        seen["timeout"] = timeout
        return _Response(_bridge_facts("lx"))

    monkeypatch.delenv(portfolio.SERVICE_URL_ENV, raising=False)
    monkeypatch.setattr(portfolio.urllib.request, "urlopen", fake_urlopen)

    result = portfolio._read_capital_facts(account="lx", period="mtd", as_of_month="2026-07")

    parsed = urlsplit(seen["request"].full_url)
    assert parsed.path == "/analysis/capital-facts"
    assert parse_qs(parsed.query) == {"account": ["lx"], "period": ["mtd"], "as_of_month": ["2026-07"]}
    assert seen["request"].get_method() == "GET"
    assert seen["timeout"] == 30.0
    assert result["status"] == "ok"


def test_portfolio_capital_bridge_is_pure_read_and_requires_explicit_scope() -> None:
    definition = get_tool_definition("portfolio_capital_bridge")

    assert definition is not None
    assert definition.is_pure_read() is True
    assert definition.side_effects == ()
    assert definition.requires_confirm is False
    assert definition.safe_default_input == {}
    schema = definition.input_json_schema()
    assert schema["required"] == ["period", "as_of_month", "accounts"]
    assert "url" not in schema["properties"]


def test_portfolio_capital_bridge_loads_shared_ledger_once_and_reports_once_per_end_date(monkeypatch) -> None:
    definition = get_tool_definition("portfolio_capital_bridge")
    assert definition is not None
    calls: list[tuple] = []
    inputs = {
        "records": [{"fields": {"account": "lx"}}, {"fields": {"account": "sy"}}],
        "trade_events": [{"account": "lx"}, {"account": "sy"}],
        "assigned_stock_events": None,
        "broker": "futu",
        "rates": {"rates": {"USDCNY": 7.2}},
    }

    def fake_load(payload, **_kwargs):
        calls.append(("load", payload))
        return inputs, ["loader warning"], {"data_config": ".../portfolio.runtime.json"}

    def fake_facts(*, account, period, as_of_month):
        calls.append(("facts", account, period, as_of_month))
        return _bridge_facts(account)

    def fake_report(records, **kwargs):
        calls.append(("report", records, kwargs))
        return {
            "return_summary": [
                {"month": "2026-07", "account": "lx", "net_income_cny": 40.0},
                {"month": "2026-07", "account": "sy", "net_income_cny": 10.0},
            ],
            "warnings": ["report warning"],
        }

    monkeypatch.setattr(portfolio, "load_monthly_income_inputs", fake_load)
    monkeypatch.setattr(portfolio, "_read_capital_facts", fake_facts)
    monkeypatch.setattr(portfolio, "build_monthly_income_report", fake_report)

    data, warnings, meta = definition.call(
        {"period": "mtd", "as_of_month": "2026-07", "accounts": ["lx", "sy"]}
    )

    assert calls[0] == ("load", {"config_key": "us"})
    assert [item for item in calls if item[0] == "facts"] == [
        ("facts", "lx", "mtd", "2026-07"),
        ("facts", "sy", "mtd", "2026-07"),
    ]
    report_calls = [item for item in calls if item[0] == "report"]
    assert len(report_calls) == 1
    assert report_calls[0][2]["as_of_ms"] == portfolio.beijing_end_of_day_ms("2026-07-16")
    assert report_calls[0][2]["trade_events"] is inputs["trade_events"]
    assert data["status"] == "ok"
    assert data["source"]["option_cash"]["runtime_config_key"] == "us"
    assert data["source"]["option_cash"]["shared_ledger_loads"] == 1
    assert warnings == ["loader warning", "2026-07-16: report warning"]
    assert meta == {"data_config": ".../portfolio.runtime.json"}


def test_portfolio_capital_bridge_runs_separate_in_memory_reports_for_mismatched_end_dates(monkeypatch) -> None:
    definition = get_tool_definition("portfolio_capital_bridge")
    assert definition is not None
    cutoffs: list[int] = []

    monkeypatch.setattr(
        portfolio,
        "load_monthly_income_inputs",
        lambda payload, **_kwargs: (
            {
                "records": [],
                "trade_events": [{"account": "lx"}, {"account": "sy"}],
                "assigned_stock_events": None,
                "broker": "futu",
                "rates": {},
            },
            [],
            {},
        ),
    )
    monkeypatch.setattr(
        portfolio,
        "_read_capital_facts",
        lambda *, account, **_kwargs: _bridge_facts(account, end_date="2026-07-16" if account == "lx" else "2026-07-15"),
    )

    def fake_report(_records, **kwargs):
        cutoffs.append(kwargs["as_of_ms"])
        return {"return_summary": [], "warnings": []}

    monkeypatch.setattr(portfolio, "build_monthly_income_report", fake_report)

    data, _, _ = definition.call(
        {"period": "mtd", "as_of_month": "2026-07", "accounts": ["lx", "sy"]}
    )

    assert sorted(cutoffs) == sorted(
        [portfolio.beijing_end_of_day_ms("2026-07-15"), portfolio.beijing_end_of_day_ms("2026-07-16")]
    )
    assert data["combined"]["status"] == "unavailable"
    assert data["combined"]["reason"] == "account_end_date_mismatch"


def test_portfolio_capital_bridge_rejects_endpoint_fields_and_missing_required_input() -> None:
    definition = get_tool_definition("portfolio_capital_bridge")
    assert definition is not None

    with pytest.raises(AgentToolError, match="required property"):
        definition.call({"period": "mtd", "as_of_month": "2026-07"})
    with pytest.raises(AgentToolError, match="endpoint fields"):
        definition.call(
            {
                "period": "mtd",
                "as_of_month": "2026-07",
                "accounts": ["lx"],
                "service_url": "http://127.0.0.1:9999",
            }
        )


def test_portfolio_cash_bridge_reads_cash_facts_endpoint(monkeypatch) -> None:
    seen = {}

    def fake_urlopen(request, timeout):
        seen["request"] = request
        seen["timeout"] = timeout
        return _Response(_cash_bridge_facts("lx"))

    monkeypatch.delenv(portfolio.SERVICE_URL_ENV, raising=False)
    monkeypatch.setattr(portfolio.urllib.request, "urlopen", fake_urlopen)

    result = portfolio._read_cash_facts(account="lx", period="mtd", as_of_month="2026-07")

    parsed = urlsplit(seen["request"].full_url)
    assert parsed.path == "/analysis/cash-facts"
    assert parse_qs(parsed.query) == {"account": ["lx"], "period": ["mtd"], "as_of_month": ["2026-07"]}
    assert seen["request"].get_method() == "GET"
    assert seen["timeout"] == 30.0
    assert result["status"] == "ok"


@pytest.mark.parametrize("tool_name", ["portfolio_pnl_bridge", "portfolio_cash_bridge"])
def test_primary_portfolio_bridges_are_pure_read_and_require_explicit_scope(tool_name) -> None:
    definition = get_tool_definition(tool_name)

    assert definition is not None
    assert definition.is_pure_read() is True
    assert definition.side_effects == ()
    assert definition.requires_confirm is False
    assert definition.safe_default_input == {}
    schema = definition.input_json_schema()
    assert schema["required"] == ["period", "as_of_month", "accounts"]
    assert "url" not in schema["properties"]


def test_primary_bridges_read_aligned_option_performance_per_account(monkeypatch) -> None:
    pnl = get_tool_definition("portfolio_pnl_bridge")
    cash = get_tool_definition("portfolio_cash_bridge")
    assert pnl is not None
    assert cash is not None
    calls = []

    def fake_capital(*, account, period, as_of_month):
        calls.append(("capital", account, period, as_of_month))
        return _bridge_facts(account)

    def fake_cash(*, account, period, as_of_month):
        calls.append(("cash", account, period, as_of_month))
        return _cash_bridge_facts(account)

    def fake_option(*, account, period, end_date):
        calls.append(("option", account, period, end_date))
        return _option_performance(account, end_date=end_date), [], {"data_config": ".../runtime.json"}

    monkeypatch.setattr(portfolio, "_read_capital_facts", fake_capital)
    monkeypatch.setattr(portfolio, "_read_cash_facts", fake_cash)
    monkeypatch.setattr(portfolio, "_read_option_performance", fake_option)

    pnl_data, pnl_warnings, pnl_meta = pnl.call(
        {"period": "mtd", "as_of_month": "2026-07", "accounts": ["lx"]}
    )
    cash_data, cash_warnings, cash_meta = cash.call(
        {"period": "mtd", "as_of_month": "2026-07", "accounts": ["lx"]}
    )

    assert calls == [
        ("capital", "lx", "mtd", "2026-07"),
        ("option", "lx", "mtd", "2026-07-16"),
        ("cash", "lx", "mtd", "2026-07"),
        ("option", "lx", "mtd", "2026-07-16"),
    ]
    assert pnl_data["status"] == "ok"
    assert pnl_data["accounts"][0]["option_pnl_evidence"]["amount_cny"] == 40.0
    assert cash_data["status"] == "ok"
    assert cash_data["accounts"][0]["option_cash_evidence"]["amount_cny"] == -200.0
    assert pnl_warnings == cash_warnings == []
    assert pnl_meta == cash_meta == {"data_config": ".../runtime.json"}


def test_cash_facts_404_returns_structured_unavailable_without_option_fallback(monkeypatch) -> None:
    definition = get_tool_definition("portfolio_cash_bridge")
    assert definition is not None

    def fail(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 404, "missing", None, None)

    monkeypatch.setattr(portfolio.urllib.request, "urlopen", fail)
    monkeypatch.setattr(
        portfolio,
        "_read_option_performance",
        lambda **_kwargs: pytest.fail("option report must not load when cash facts are unavailable"),
    )

    data, warnings, meta = definition.call(
        {"period": "mtd", "as_of_month": "2026-07", "accounts": ["lx"]}
    )

    assert data["success"] is True
    assert data["status"] == "unavailable"
    assert data["accounts"][0]["reason"] == "portfolio_cash_facts_unavailable"
    assert data["accounts"][0]["steps"] == []
    assert data["combined"]["reason"] == "account_bridge_unavailable"
    assert warnings == []
    assert meta == {}


def test_legacy_capital_bridge_is_explicitly_deprecated() -> None:
    definition = get_tool_definition("portfolio_capital_bridge")

    assert definition is not None
    assert "DEPRECATED" in definition.description
    assert "deprecated" in definition.capabilities
