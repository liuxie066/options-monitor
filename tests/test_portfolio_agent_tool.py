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


def test_portfolio_query_is_one_pure_read_toolset() -> None:
    definition = get_tool_definition("portfolio_query")

    assert definition is not None
    assert definition.is_pure_read() is True
    assert definition.side_effects == ()
    assert definition.requires_confirm is False
    assert definition.safe_default_input == {"view": "health"}
    assert "url" not in definition.input_schema
    assert pure_read_toolsets()["portfolio"] == ("portfolio_query",)


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
