from __future__ import annotations

import json
import socket
import urllib.error
from urllib.parse import parse_qs, urlsplit

import pytest

from src.application.agent_tool_contracts import AgentToolError
from src.application.agent_tool_registry import get_tool_definition, pure_read_toolsets
from src.application.agent_tools import portfolio
from src.application.copilot import tools as copilot_tools
from src.application.copilot.result_admission import admit_submit_answer


@pytest.fixture(autouse=True)
def _portfolio_management_enabled(monkeypatch) -> None:
    monkeypatch.setattr(
        portfolio,
        "load_runtime_config",
        lambda **_kwargs: (
            None,
            {"portfolio_management": {"enabled": True}},
        ),
    )


class _Response:
    def __init__(self, payload):
        self.body = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        self.headers = {"X-PM-API-Version": "portfolio.api.v1"}

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
        "portfolio_assignment_scenario",
    )


def test_portfolio_query_disabled_never_opens_transport(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        portfolio,
        "load_runtime_config",
        lambda **_kwargs: (
            None,
            {"portfolio_management": {"enabled": False}},
        ),
    )
    monkeypatch.setattr(
        portfolio.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: calls.append(True),
    )

    definition = get_tool_definition("portfolio_query")
    assert definition is not None
    with pytest.raises(AgentToolError) as raised:
        definition.call({"view": "health"})

    assert raised.value.code == "PORTFOLIO_MANAGEMENT_DISABLED"
    assert calls == []


def test_assignment_scenario_tool_has_accounts_only_contract(monkeypatch) -> None:
    expected = {
        "schema_version": "portfolio.assignment_scenario.v1",
        "status": "complete",
        "scope": {"accounts": ["lx"], "include_long_options": False},
        "assignments": [],
        "warnings": [],
    }
    monkeypatch.setattr(
        portfolio,
        "query_portfolio_assignment_scenario",
        lambda accounts: {**expected, "scope": {**expected["scope"], "accounts": list(accounts)}},
    )
    definition = get_tool_definition("portfolio_assignment_scenario")

    assert definition is not None
    assert definition.is_pure_read() is True
    assert definition.safe_default_input == {}
    assert definition.input_json_schema()["required"] == ["accounts"]
    assert definition.copilot_input_fields == ("accounts",)

    data, warnings, meta = definition.call({"accounts": ["lx"]})

    assert data == expected
    assert warnings == []
    assert meta == {}

    with pytest.raises(AgentToolError, match="accepts only accounts"):
        definition.call({"accounts": ["lx"], "price": 123})


def test_portfolio_query_preserves_payload_and_adds_evidence_metadata(monkeypatch) -> None:
    monkeypatch.delenv("PORTFOLIO_SERVICE_URL", raising=False)
    pm_freshness = {
        "status": "stale",
        "trust_status": "partial",
        "observed_at_utc": "2026-07-25T21:00:00Z",
        "dataset_ids": ["pm.holdings_quantity", "pm.prices"],
        "reason_codes": ["SOURCE_STALE"],
    }

    data, warnings, meta, seen = _call(
        {"view": "overview", "accounts": ["lx", "sy"], "include_details": True},
        monkeypatch,
        {
            "success": True,
            "accounts": ["lx", "sy"],
            "total_value": 123.45,
            "freshness": pm_freshness,
            "retrieved_at_utc": "2026-07-26T01:00:00Z",
        },
    )

    parsed = urlsplit(seen["request"].full_url)
    assert parsed.scheme == "http"
    assert parsed.netloc == "127.0.0.1:8765"
    assert parsed.path == "/api/v1/accounts/overview"
    assert parse_qs(parsed.query) == {"accounts": ["lx,sy"], "include_details": ["true"]}
    assert seen["request"].get_method() == "GET"
    assert data["success"] is True
    assert data["total_value"] == 123.45
    assert data["source"] == {"service": "portfolio-management", "transport": "loopback_http"}
    assert data["scope"] == {"view": "overview", "accounts": ["lx", "sy"]}
    assert data["freshness"] == pm_freshness
    assert data["retrieved_at_utc"] == "2026-07-26T01:00:00Z"
    assert warnings == []
    assert meta == {}


def test_portfolio_query_marks_business_data_unavailable_without_pm_freshness(monkeypatch) -> None:
    data, warnings, _, _ = _call(
        {"view": "cash", "account": "lx"},
        monkeypatch,
        {"success": True, "items": []},
    )

    assert data["freshness"]["status"] == "unavailable"
    assert data["freshness"]["trust_status"] == "unavailable"
    assert data["freshness"]["reason_codes"] == ["PM_FRESHNESS_EVIDENCE_MISSING"]
    assert warnings == ["PM freshness evidence is missing; data is unavailable"]


def test_portfolio_query_collection_contract_projects_real_rows(monkeypatch) -> None:
    data, warnings, _, _ = _call(
        {"view": "accounts", "include_default": False},
        monkeypatch,
        {
            "success": True,
            "accounts": ["lx", "sy"],
            "count": 2,
            "freshness": {
                "status": "fresh",
                "trust_status": "trusted",
                "observed_at_utc": "2026-08-22T09:30:00Z",
                "dataset_ids": ["pm.account_mapping"],
                "reason_codes": [],
            },
            "retrieved_at_utc": "2026-08-22T09:30:00Z",
        },
    )

    observation = copilot_tools.compact_observation(
        "portfolio_query",
        {"ok": True, "data": data, "warnings": warnings},
        {"view": "accounts", "include_default": False},
    )

    assert observation["value"]["accounts"] == ["lx", "sy"]
    assert observation["coverage"]["status"] == "complete"
    assert observation["coverage"]["complete_for"] == "requested_page"
    assert observation["coverage"]["included_count"] == 2
    assert observation["freshness"] == {
        "status": "fresh",
        "as_of": "2026-08-22T09:30:00Z",
        "trust_status": "trusted",
    }


def test_portfolio_query_untrusted_freshness_cannot_support_current_fact(monkeypatch) -> None:
    data, warnings, _, _ = _call(
        {"view": "accounts", "include_default": False},
        monkeypatch,
        {
            "success": True,
            "accounts": ["lx"],
            "count": 1,
            "freshness": {
                "status": "fresh",
                "trust_status": "untrusted",
                "observed_at_utc": "2026-08-22T09:30:00Z",
                "dataset_ids": ["pm.account_mapping"],
                "reason_codes": ["QUALITY_GATE_FAILED"],
            },
            "retrieved_at_utc": "2026-08-22T09:30:00Z",
        },
    )
    observation = copilot_tools.compact_observation(
        "portfolio_query",
        {"ok": True, "data": data, "warnings": warnings},
        {"view": "accounts", "include_default": False},
    )

    assert data["freshness"]["status"] == "unknown"
    assert observation["freshness"] == {
        "status": "unknown",
        "as_of": "2026-08-22T09:30:00Z",
        "trust_status": "untrusted",
        "reason_codes": ["QUALITY_GATE_FAILED"],
    }
    assert warnings == [
        "PM freshness evidence is not trusted; current data is unavailable"
    ]
    rejected = admit_submit_answer(
        {
            "mode": "evidence",
            "status": "complete",
            "answer_markdown": "当前账户为 lx。",
            "claims": [{
                "text": "当前账户为 lx",
                "kind": "current_fact",
                "observation_ids": ["obv_pm_untrusted"],
                "required_scope": "requested_page",
            }],
        },
        {
            "obv_pm_untrusted": {
                "ok": True,
                "authorized_read": True,
                "observation_status": observation["status"],
                "coverage": observation["coverage"],
                "freshness": observation["freshness"],
            }
        },
    )
    assert rejected["observation"]["reason"] == "claim_freshness_not_supported"


def test_portfolio_query_grouped_holdings_declares_closed_collection_coverage(monkeypatch) -> None:
    freshness = {
        "status": "fresh",
        "trust_status": "trusted",
        "observed_at_utc": "2026-08-22T09:30:00Z",
        "dataset_ids": ["pm.holdings_quantity"],
        "reason_codes": [],
    }
    data, warnings, _, _ = _call(
        {"view": "holdings", "account": "lx", "group_by_market": True},
        monkeypatch,
        {
            "success": True,
            "count": 2,
            "by_market": {"US": [{"code": "NVDA"}], "HK": [{"code": "0700.HK"}]},
            "freshness": freshness,
            "retrieved_at_utc": "2026-08-22T09:30:00Z",
        },
    )

    observation = copilot_tools.compact_observation(
        "portfolio_query",
        {"ok": True, "data": data, "warnings": warnings},
        {"view": "holdings", "account": "lx", "group_by_market": True},
    )

    assert observation["coverage"] == {
        "status": "complete",
        "complete_for": "full_query",
        "included_count": 2,
        "total_count": 2,
        "omitted_count": 0,
        "has_more": False,
        "scope": {"view": "holdings", "account": "lx"},
    }


@pytest.mark.parametrize(
    ("payload", "path"),
    [
        ({"view": "health"}, "/health"),
        ({"view": "accounts", "include_default": False}, "/api/v1/accounts"),
        ({"view": "holdings", "account": "lx"}, "/api/v1/holdings"),
        ({"view": "cash", "account": "lx"}, "/api/v1/cash"),
        ({"view": "nav", "account": "lx", "days": 14}, "/api/v1/nav"),
        ({"view": "distribution", "accounts": ["lx", "sy"]}, "/api/v1/distribution"),
        ({"view": "full_report", "account": "lx"}, "/api/v1/report/full"),
    ],
)
def test_portfolio_query_maps_supported_views_to_get_endpoints(monkeypatch, payload, path) -> None:
    _, _, _, seen = _call(payload, monkeypatch, {"success": True})

    assert urlsplit(seen["request"].full_url).path == path
    assert seen["request"].get_method() == "GET"


def test_portfolio_query_rejects_non_loopback_service_url(monkeypatch) -> None:
    monkeypatch.setenv("PORTFOLIO_SERVICE_URL", "http://portfolio.internal:8765")
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

    assert exc_info.value.code == "PORTFOLIO_MANAGEMENT_UNAVAILABLE"


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

    assert exc_info.value.code == "PORTFOLIO_MANAGEMENT_UNAVAILABLE"


def test_portfolio_query_rejects_invalid_json(monkeypatch) -> None:
    definition = get_tool_definition("portfolio_query")
    assert definition is not None
    monkeypatch.setattr(portfolio.urllib.request, "urlopen", lambda request, timeout: _Response(b"not-json"))

    with pytest.raises(AgentToolError, match="invalid JSON") as exc_info:
        definition.call({"view": "health"})

    assert exc_info.value.code == "PORTFOLIO_MANAGEMENT_INCOMPATIBLE"



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


def test_portfolio_cash_bridge_reports_cash_facts_not_onboarded_without_http(monkeypatch) -> None:
    monkeypatch.setattr(
        portfolio.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("HTTP must not be called")),
    )

    definition = get_tool_definition("portfolio_cash_bridge")
    assert definition is not None
    data, warnings, meta = definition.call(
        {"period": "mtd", "as_of_month": "2026-07", "accounts": ["lx"]}
    )

    assert data["status"] == "unavailable"
    assert data["reason"] == "portfolio_cash_facts_not_onboarded"
    assert warnings == []
    assert meta == {}


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


def test_primary_bridges_use_only_their_authoritative_sources(monkeypatch) -> None:
    pnl = get_tool_definition("portfolio_pnl_bridge")
    cash = get_tool_definition("portfolio_cash_bridge")
    assert pnl is not None
    assert cash is not None
    calls = []

    def fake_capital(*, account, period, as_of_month):
        calls.append(("capital", account, period, as_of_month))
        return _bridge_facts(account)

    monkeypatch.setattr(portfolio, "_read_capital_facts", fake_capital)
    monkeypatch.setattr(
        portfolio.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("cash bridge must not call HTTP")),
    )

    pnl_data, pnl_warnings, pnl_meta = pnl.call(
        {"period": "mtd", "as_of_month": "2026-07", "accounts": ["lx"]}
    )
    cash_data, cash_warnings, cash_meta = cash.call(
        {"period": "mtd", "as_of_month": "2026-07", "accounts": ["lx"]}
    )

    assert calls == [("capital", "lx", "mtd", "2026-07")]
    assert pnl_data["status"] == "partial"
    assert pnl_data["accounts"][0]["option_pnl_evidence"]["status"] == "unavailable"
    assert cash_data["status"] == "unavailable"
    assert cash_data["accounts"][0]["option_cash_evidence"]["status"] == "unavailable"
    assert pnl_warnings == cash_warnings == []
    assert pnl_meta == cash_meta == {}


def test_cash_bridge_is_unavailable_without_opening_transport(monkeypatch) -> None:
    definition = get_tool_definition("portfolio_cash_bridge")
    assert definition is not None

    monkeypatch.setattr(
        portfolio.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: pytest.fail("cash bridge must not open portfolio transport"),
    )

    data, warnings, meta = definition.call(
        {"period": "mtd", "as_of_month": "2026-07", "accounts": ["lx"]}
    )

    assert data["success"] is True
    assert data["status"] == "unavailable"
    assert data["accounts"][0]["reason"] == "portfolio_cash_facts_not_onboarded"
    assert data["accounts"][0]["steps"] == []
    assert data["combined"]["reason"] == "portfolio_cash_facts_not_onboarded"
    assert warnings == []
    assert meta == {}
