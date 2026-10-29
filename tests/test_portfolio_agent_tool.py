from __future__ import annotations

import json
import socket
import urllib.error
from urllib.parse import parse_qs, urlsplit

import pytest

from src.application.agent_tool_contracts import AgentToolError
from src.application.agent_tool_registry import get_tool_definition, pure_read_toolsets
from src.application.agent_tools import portfolio
from src.application.bot import tools as bot_tools


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
    definition = _tool("portfolio_query")
    data, warnings, meta = definition.call(payload)
    return data, warnings, meta, seen


def _tool(name: str):
    """Look up a registered tool definition, failing the test when it is absent."""
    definition = get_tool_definition(name)
    assert definition is not None
    return definition


def test_portfolio_tools_share_one_pure_read_toolset() -> None:
    definition = _tool("portfolio_query")

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

    definition = _tool("portfolio_query")
    with pytest.raises(AgentToolError) as raised:
        definition.call({"view": "health"})

    assert raised.value.code == "PORTFOLIO_MANAGEMENT_DISABLED"
    assert calls == []


def test_assignment_scenario_tool_has_optional_scenario_contract(monkeypatch) -> None:
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
        lambda accounts, *, scenario: {**expected, "scope": {**expected["scope"], "accounts": list(accounts)}},
    )
    definition = _tool("portfolio_assignment_scenario")

    assert definition.is_pure_read() is True
    assert definition.safe_default_input == {}
    assert definition.input_json_schema()["required"] == ["accounts"]
    assert definition.bot_input_fields == ("accounts", "scenario")
    assert definition.output_contract["source_label"] == (
        "Futu account + OM SQLite position_lots; PM non-Futu Holdings optional"
    )

    data, warnings, meta = definition.call({"accounts": ["lx"]})

    assert data == expected
    assert warnings == []
    assert meta == {}

    with pytest.raises(AgentToolError, match="accepts only accounts"):
        definition.call({"accounts": ["lx"], "price": 123})


@pytest.mark.parametrize("scenario", ["short_put", "short_call", "all_short"])
def test_assignment_tool_forwards_assumption(monkeypatch, scenario):
    from src.application.bot.tools import build_tool_payload

    calls = []
    monkeypatch.setattr(portfolio, "query_portfolio_assignment_scenario", lambda accounts, **kwargs: (
        calls.append((accounts, kwargs)) or {"warnings": []}
    ))
    payload, error = build_tool_payload(
        "portfolio_assignment_scenario", {"accounts": ["lx"], "scenario": scenario}, fixed_input={},
    )
    assert error is None
    _tool("portfolio_assignment_scenario").call(payload)
    assert calls == [(["lx"], {"scenario": scenario})]


def test_assignment_tool_rejects_unknown_assumption(monkeypatch):
    monkeypatch.setattr(portfolio, "query_portfolio_assignment_scenario", lambda *args, **kwargs: pytest.fail("unexpected read"))
    with pytest.raises(AgentToolError):
        _tool("portfolio_assignment_scenario").call({"accounts": ["lx"], "scenario": "put"})


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


@pytest.mark.parametrize(
    ("payload", "service_url", "match", "code"),
    [
        ({"view": "health"}, "http://portfolio.internal:8765", "loopback", "CONFIG_ERROR"),
        ({"view": "health", "service_url": "http://127.0.0.1:9999"}, None, "endpoint fields", "INPUT_ERROR"),
        ({"view": "holdings"}, None, "account is required", "INPUT_ERROR"),
    ],
    ids=["non-loopback-service-url", "model-provided-endpoint-field", "account-required-for-scoped-view"],
)
def test_portfolio_query_rejects_invalid_request_before_transport(monkeypatch, payload, service_url, match, code) -> None:
    if service_url is not None:
        monkeypatch.setenv("PORTFOLIO_SERVICE_URL", service_url)
    definition = _tool("portfolio_query")

    with pytest.raises(AgentToolError, match=match) as exc_info:
        definition.call(payload)

    assert exc_info.value.code == code


@pytest.mark.parametrize(
    ("response", "match", "code"),
    [
        ({"success": False, "error": "missing holdings table"}, "missing holdings table", "PORTFOLIO_MANAGEMENT_UNAVAILABLE"),
        (b"not-json", "invalid JSON", "PORTFOLIO_MANAGEMENT_INCOMPATIBLE"),
    ],
    ids=["service-reported-failure", "invalid-json-body"],
)
def test_portfolio_query_converts_bad_response_to_agent_tool_error(monkeypatch, response, match, code) -> None:
    definition = _tool("portfolio_query")
    monkeypatch.setattr(portfolio.urllib.request, "urlopen", lambda request, timeout: _Response(response))

    with pytest.raises(AgentToolError, match=match) as exc_info:
        definition.call({"view": "health"})

    assert exc_info.value.code == code


@pytest.mark.parametrize(
    "success_fields",
    [{}, {"success": None}, {"success": False}, {"success": "true"},
     {"success": "false"}, {"success": 0}, {"success": 1},
     {"success": []}, {"success": {}}],
    ids=["missing", "null", "false", "true-string", "false-string",
         "zero", "one", "list", "object"],
)
def test_portfolio_accounts_rejects_malformed_success_before_freshness(
    monkeypatch, success_fields
) -> None:
    response = {
        **success_fields,
        "accounts": ["sample"],
        "count": 1,
        "retrieved_at_utc": "2026-10-20T00:00:00Z",
        "freshness": {
            "status": "fresh",
            "trust_status": "trusted",
            "observed_at_utc": "2026-10-20T00:00:00Z",
            "dataset_ids": ["pm.accounts"],
            "reason_codes": [],
        },
    }
    calls = []

    def respond(request, timeout):
        calls.append(request)
        return _Response(response)

    monkeypatch.setattr(portfolio.urllib.request, "urlopen", respond)
    with pytest.raises(AgentToolError, match="success=true") as raised:
        _tool("portfolio_query").call({"view": "accounts"})

    assert raised.value.code == "PORTFOLIO_MANAGEMENT_INCOMPATIBLE"
    assert len(calls) == 1


def test_portfolio_health_preserves_unversioned_success_contract(monkeypatch) -> None:
    data, warnings, _, seen = _call(
        {"view": "health"}, monkeypatch, {"status": "ok"}
    )

    assert data["status"] == "ok"
    assert data["freshness"]["status"] == "fresh"
    assert warnings == []
    assert urlsplit(seen["request"].full_url).path == "/health"


@pytest.mark.parametrize(
    "failure",
    [
        urllib.error.URLError("connection refused"),
        socket.timeout("read timed out"),
        urllib.error.HTTPError("http://127.0.0.1:8765/health", 503, "unavailable", None, None),
    ],
)
def test_portfolio_query_converts_transport_failures_to_agent_tool_error(monkeypatch, failure) -> None:
    definition = _tool("portfolio_query")

    def fail(request, timeout):
        raise failure

    monkeypatch.setattr(portfolio.urllib.request, "urlopen", fail)

    with pytest.raises(AgentToolError) as exc_info:
        definition.call({"view": "health"})

    assert exc_info.value.code == "PORTFOLIO_MANAGEMENT_UNAVAILABLE"


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

    definition = _tool("portfolio_cash_bridge")
    data, warnings, meta = definition.call(
        {"period": "mtd", "as_of_month": "2026-07", "accounts": ["lx"]}
    )

    assert data["status"] == "unavailable"
    assert data["reason"] == "portfolio_cash_facts_not_onboarded"
    assert warnings == []
    assert meta == {}


@pytest.mark.parametrize("tool_name", ["portfolio_pnl_bridge", "portfolio_cash_bridge"])
def test_primary_portfolio_bridges_are_pure_read_and_require_explicit_scope(tool_name) -> None:
    definition = _tool(tool_name)

    assert definition.is_pure_read() is True
    assert definition.side_effects == ()
    assert definition.requires_confirm is False
    assert definition.safe_default_input == {}
    schema = definition.input_json_schema()
    assert schema["required"] == ["period", "as_of_month", "accounts"]
    assert "url" not in schema["properties"]


def test_primary_bridges_use_only_their_authoritative_sources(monkeypatch) -> None:
    pnl = _tool("portfolio_pnl_bridge")
    cash = _tool("portfolio_cash_bridge")
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
    definition = _tool("portfolio_cash_bridge")

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


@pytest.mark.parametrize("view", ["health", "accounts", "overview", "holdings", "cash", "nav", "full_report", "distribution"])
@pytest.mark.parametrize("selector", [{}, {"account": "lx"}, {"accounts": ["lx"]}, {"account": "lx", "accounts": ["lx"]}])
def test_portfolio_closed_account_matrix_before_transport(monkeypatch, view, selector):
    from src.application.tool_execution import execute_tool
    required = {"holdings", "cash", "nav", "full_report"}
    allowed = (not selector if view in {"health", "accounts"}
               else "account" not in selector if view == "overview"
               else selector == {"account": "lx"} if view in required
               else len(selector) <= 1)
    calls = []
    class Client:
        def read_view(self, *args, **kwargs):
            calls.append((args, kwargs))
            return {}
    monkeypatch.setattr(portfolio, "_portfolio_client", lambda: Client())
    response = execute_tool("portfolio_query", {"view": view, **selector})
    assert response["ok"] is allowed, response
    assert bool(calls) is allowed
    if not allowed:
        assert response["error"]["code"] == "INPUT_ERROR"


@pytest.mark.parametrize("view", ["health", "accounts", "overview", "distribution"])
def test_portfolio_empty_optional_account_keeps_unscoped_default(monkeypatch, view):
    data, _, _, seen = _call({"view": view, "account": " "}, monkeypatch, {"success": True})
    assert data["scope"] == {"view": view}
    assert "account" not in parse_qs(urlsplit(seen["request"].full_url).query)


def _cash_evidence():
    # Synthetic replay of the incident's response shape, not real account data.
    return {
        "success": True, "count": 2,
        "items": [
            {"code": "CNY-CASH", "name": "账户余额", "amount": 1000, "currency": "CNY", "type": "cash"},
            {"code": "CNY-MMF", "name": "货币基金", "amount": 2000, "currency": "CNY", "type": "mmf"},
        ],
        "by_currency": {"CNY": 3000},
        "retrieved_at_utc": "2026-10-26T02:16:29Z",
        "freshness": {"status": "fresh", "trust_status": "trusted",
                      "observed_at_utc": "2026-10-26T00:10:32Z",
                      "dataset_ids": ["pm.cash_aggregate", "pm.fund_mmf"], "reason_codes": []},
    }


def _cash_gateway(monkeypatch, response, payload=None):
    from src.application.tool_execution import execute_tool
    monkeypatch.setattr(portfolio.urllib.request, "urlopen", lambda *_args, **_kwargs: _Response(response))
    return execute_tool("portfolio_query", payload or {"view": "cash", "account": "sample"})


def test_account_evidence_public_gateway_to_bot_preserves_fact_boundaries(monkeypatch):
    source = _cash_evidence()
    response = _cash_gateway(monkeypatch, source)
    observation = bot_tools.model_observation("portfolio_query", response)
    assert response["ok"] is True
    data = observation["data"]
    assert data["scope"] == {"view": "cash", "account": "sample"}
    assert data["source"]["service"] == "portfolio-management"
    assert data["freshness"] == source["freshness"]
    assert data["items"] == source["items"]  # No broker invention, currency relabeling or FX.
    assert data["coverage"]["status"] == "complete"
    assert data["assessment"]["status"] == "reported_facts_only"
    assert data["assessment"]["scope_binding"] == "request"
    assert data["assessment"]["supported_claims"] == ["pm_reported_cash_as_of"]
    assert "available_to_trade" in data["assessment"]["unsupported_claims"]
    assert "additional_short_contracts" in data["assessment"]["unsupported_claims"]


@pytest.mark.parametrize("change", [
    {"account": "other"}, {"scope": {"accounts": ["other"]}},
    {"items": [{"account": "other", "currency": "CNY", "amount": 3000}]},
    {"accounts": ["sample", "other"]},
])
def test_wrong_account_response_never_becomes_requested_account_facts(monkeypatch, change):
    response = _cash_gateway(monkeypatch, {**_cash_evidence(), **change})
    observation = bot_tools.model_observation("portfolio_query", response)
    assert observation["ok"] is False
    assert observation["error"]["code"] == "PORTFOLIO_MANAGEMENT_INCOMPATIBLE"
    assert not observation.get("data")


@pytest.mark.parametrize("change", [
    {"status": "stale"}, {"trust_status": "partial"},
    {"observed_at_utc": None}, {"observed_at_utc": "not-a-time"},
    {"observed_at_utc": "2026-10-26T00:10:32"},
    {"observed_at_utc": "2026-10-26T03:00:00Z"},
])
def test_unusable_source_time_or_trust_does_not_support_current_facts(monkeypatch, change):
    source = _cash_evidence()
    source["freshness"].update(change)
    observation = bot_tools.model_observation("portfolio_query", _cash_gateway(monkeypatch, source))
    assert observation["ok"] is True  # Transport succeeded, evidence did not.
    assert observation["data"]["assessment"]["status"] == "unable"
    assert observation["data"]["assessment"]["supported_claims"] == []
    assert observation["data"]["items"] == source["items"]  # Kept as unqualified source records.


@pytest.mark.parametrize("change", [
    {"count": 3}, {"count": None}, {"count": True}, {"items": None},
    {"items": [None, {}]}, {"by_currency": None},
    {"by_currency": {}}, {"by_currency": {"CNY": None}},
    {"by_currency": {"HKD": 3000}}, {"status": []},
])
def test_partial_or_malformed_cash_cannot_prove_complete_account(monkeypatch, change):
    result = _cash_gateway(monkeypatch, {**_cash_evidence(), **change})
    assert result["ok"] is True
    assert result["data"]["coverage"]["status"] != "complete"
    assert result["data"]["assessment"]["status"] == "unable"
    assert "PM_QUERY_INCOMPLETE" in result["data"]["assessment"]["reason_codes"]


def test_observed_empty_cash_is_distinct_from_missing_or_failed_query(monkeypatch):
    result = _cash_gateway(monkeypatch, {**_cash_evidence(), "items": [], "by_currency": {}, "count": 0})
    assert result["data"]["coverage"]["status"] == "complete"
    assert result["data"]["assessment"]["status"] == "reported_facts_only"
    assert "available_to_trade" in result["data"]["assessment"]["unsupported_claims"]


def test_account_query_failure_cannot_be_projected_as_zero_cash(monkeypatch):
    from src.application.tool_execution import execute_tool
    def unavailable(*_args, **_kwargs):
        raise socket.timeout("synthetic timeout")
    monkeypatch.setattr(portfolio.urllib.request, "urlopen", unavailable)
    result = bot_tools.model_observation("portfolio_query", execute_tool("portfolio_query", {"view": "cash", "account": "sample"}))
    assert result["ok"] is False
    assert result["error"]["code"] == "PORTFOLIO_MANAGEMENT_UNAVAILABLE"
    assert not result.get("data")


def test_account_query_missing_scope_does_not_default_or_open_transport(monkeypatch):
    from src.application.tool_execution import execute_tool
    monkeypatch.setattr(portfolio.urllib.request, "urlopen", lambda *_a, **_k: pytest.fail("must clarify account"))
    result = execute_tool("portfolio_query", {"view": "cash"})
    assert result["ok"] is False
    assert result["error"]["code"] == "INPUT_ERROR"


def test_partial_overview_does_not_qualify_all_account_totals(monkeypatch):
    source = {**_cash_evidence(), "accounts": ["sample", "other"], "account_count": 2,
              "successful_count": 1, "failed_count": 1, "summary": {"cash_value": 1000},
              "items": [{"account": "sample", "success": True}, {"account": "other", "success": False}]}
    result = _cash_gateway(monkeypatch, source, {"view": "overview", "accounts": ["sample", "other"]})
    assert result["ok"] is True
    assert result["data"]["coverage"]["status"] == "partial"
    assert result["data"]["assessment"]["supported_claims"] == []


def test_pm_cash_scope_echo_is_optional_but_never_independently_claimed(monkeypatch):
    for extra in ({}, {"account": "sample"}):
        result = _cash_gateway(monkeypatch, {**_cash_evidence(), **extra})
        assert result["ok"] is True
        assert result["data"]["assessment"]["scope_binding"] == "request"


@pytest.mark.parametrize("labels, expected_ok", [({"sample": 3}, True), ({"other": 3}, False)])
def test_asset_distribution_account_quantity_map_respects_scope(monkeypatch, labels, expected_ok):
    source = {**_cash_evidence(), "accounts": ["sample"], "by_asset": [
        {"code": "SYNTHETIC", "accounts": labels, "quantity": 3, "value": 30},
    ], "total_value": 30}
    result = _cash_gateway(monkeypatch, source, {"view": "distribution", "account": "sample", "by_asset": True})
    assert result["ok"] is expected_ok
    if expected_ok:
        assert result["data"]["assessment"]["status"] == "reported_facts_only"
        assert result["data"]["by_asset"][0]["accounts"] == labels
    else:
        assert result["error"]["code"] == "PORTFOLIO_MANAGEMENT_INCOMPATIBLE"


@pytest.mark.parametrize("view, fields, payload", [
    ("accounts", {"accounts": ["sample"], "count": 1}, {}),
    ("holdings", {"holdings": [{"code": "SYNTHETIC"}], "count": 1}, {"account": "sample"}),
    ("holdings", {"by_market": {"US": [{"code": "SYNTHETIC"}]}, "count": 1}, {"account": "sample", "group_by_market": True}),
    ("overview", {"accounts": ["sample"], "account_count": 1, "successful_count": 1, "failed_count": 0,
                  "summary": {"total_value": 3000}, "items": [{"account": "sample", "success": True}]}, {"accounts": ["sample"]}),
    ("nav", {"latest": {"nav": 1}, "history": [{"nav": 1}]}, {"account": "sample"}),
    ("full_report", {"overview": {"total_value": 3000}, "returns": {}, "distribution": [],
                     "top_holdings": [], "nav": None, "generated_at": "2026-10-26T10:16:29"}, {"account": "sample"}),
])
def test_supported_pm_view_shapes_keep_source_facts_qualified(monkeypatch, view, fields, payload):
    metadata = {key: value for key, value in _cash_evidence().items()
                if key in {"success", "freshness", "retrieved_at_utc"}}
    result = _cash_gateway(monkeypatch, {**metadata, **fields}, {"view": view, **payload})
    assert result["ok"] is True
    assert result["data"]["assessment"]["status"] == "reported_facts_only"
    if view == "full_report":
        assert result["data"]["coverage"]["complete_for"] == "report_summary"


@pytest.mark.parametrize("declared_count", [None, 2])
@pytest.mark.parametrize("grouped", [False, True])
def test_returned_holdings_do_not_establish_source_completeness(monkeypatch, declared_count, grouped):
    source = {key: value for key, value in _cash_evidence().items()
              if key in {"success", "freshness", "retrieved_at_utc"}}
    rows = [{"code": "SYNTHETIC", "account": "sample"}]
    source.update({"by_market": {"US": rows}} if grouped else {"holdings": rows})
    if declared_count is not None:
        source["count"] = declared_count
    result = _cash_gateway(monkeypatch, source,
                           {"view": "holdings", "account": "sample", "group_by_market": grouped})
    observed = bot_tools.model_observation("portfolio_query", result)
    assert observed["ok"] is True
    assert observed["data"]["coverage"]["status"] != "complete"
    assert observed["data"]["assessment"]["supported_claims"] == []
    assert "PM_QUERY_INCOMPLETE" in observed["data"]["assessment"]["reason_codes"]


@pytest.mark.parametrize("tool", ["query_cash_headroom", "get_portfolio_context"])
@pytest.mark.parametrize("selector", [{}, {"account": ""}, {"account": " "}, {"account": "sample other"}])
def test_broker_account_tools_reject_missing_or_blank_scope_before_handler(monkeypatch, tool, selector):
    from src.application.agent_tools import materialization
    from src.application.tool_execution import execute_tool
    monkeypatch.setattr(materialization, "load_runtime_config", lambda **_k: pytest.fail("no runtime read"))
    result = execute_tool(tool, {"config_key": "us", **selector})
    assert result["ok"] is False
    assert result["error"]["code"] == "INPUT_ERROR"
