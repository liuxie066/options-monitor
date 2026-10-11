from argparse import Namespace
from contextlib import contextmanager
from dataclasses import replace
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import socket
from threading import Thread

import pytest

from src.application.agent_tool_contracts import AgentToolError
from src.application.agent_tools import materialization_impl, positions
from src.application.bot import tools as bot_tools
from src.application.ledger.repository import SQLiteOptionPositionsRepository
from src.application.performance import query, query_http
from src.infrastructure import option_performance_client as client
from src.interfaces.cli import option_performance
from test_performance_service_v2 import _event


TOKEN = "isolated-performance-test-token"
QUERY = {"period": "month", "month": "2026-09", "config_key": "us"}


@contextmanager
def _running(server):
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()
        assert not thread.is_alive()


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setenv("OM_RUNTIME_ROOT", str(tmp_path))
    monkeypatch.delenv("OM_PERFORMANCE_SERVICE_URL", raising=False)
    path = tmp_path / "output_shared/state/option_positions.sqlite3"
    writer = SQLiteOptionPositionsRepository(path)
    for n in range(3):
        event = _event(f"open-{n}", "open", "2026-09-01T10:00:00")
        writer.upsert_trade_event(
            replace(event, lot_id=f"lot-{n}", contract_key=replace(event.contract_key, underlying_symbol=f"TEST{n}"))
        )
    cfg = {"accounts": ["lx", "sy"], "portfolio": {}}
    monkeypatch.setattr(query_http, "load_runtime_config", lambda **kw: (tmp_path / "config.us.json", cfg))
    monkeypatch.setattr(
        query_http, "resolve_public_data_config_path", lambda *args: tmp_path / "portfolio.runtime.json"
    )
    server = query_http.create_performance_query_server(
        config_key="us", config_path=tmp_path / "config.us.json", token=TOKEN, port=0
    )
    with _running(server) as url:
        yield url, writer, path, cfg


def _request(url, payload, *, token=TOKEN, method="POST", path=client.QUERY_PATH, headers=None):
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    connection = HTTPConnection("127.0.0.1", int(url.rsplit(":", 1)[1]), timeout=3)
    try:
        connection.request(
            method,
            path,
            body=body,
            headers=headers or {"Authorization": "Bearer " + token, "Content-Type": "application/json"},
        )
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


def _remote(url, payload=QUERY):
    return client.query_option_performance(payload, url=url, token=TOKEN)


def _forbid(*args, **kwargs):
    raise AssertionError("service consumer entered a local dependency")


def test_real_client_cli_tool_and_bot_use_the_canonical_result(service, monkeypatch):
    url, writer, path, cfg = service
    data, warnings, meta = _remote(url, {**QUERY, "include_rows": True})
    with writer._connect() as conn:
        before = list(conn.iterdump())
    expected, _, _ = query.option_performance_report_tool(
        {**QUERY, "include_rows": True},
        load_runtime_config=lambda **kw: (path.parent / "config.us.json", cfg),
        resolve_public_data_config_path=lambda *args: path.parent / "portfolio.runtime.json",
        normalize_broker=positions.normalize_broker,
        resolve_option_positions_repo=lambda **kw: (path, writer),
        build_option_period_performance=positions.build_option_period_performance,
        repo_base=lambda: path.parent,
        mask_path=lambda p: "<bound-runtime>",
    )
    assert data == expected and warnings == []
    assert str(path) not in json.dumps(meta)
    monkeypatch.setenv("OM_PERFORMANCE_SERVICE_URL", url)
    monkeypatch.setenv("OM_PERFORMANCE_SERVICE_TOKEN", TOKEN)
    for owner in (positions, option_performance):
        for name in (
            "load_runtime_config",
            "resolve_public_data_config_path",
            "open_performance_query_ledger_from_data_config",
            "build_option_period_performance",
            "repo_base",
        ):
            monkeypatch.setattr(owner, name, _forbid)
    assert positions.OPTION_PERFORMANCE_REPORT_TOOL.call({**QUERY, "include_rows": True})[0] == expected
    args = Namespace(
        option_performance_command="report",
        config_key="us",
        config_path=None,
        data_config=None,
        account=None,
        broker=None,
        period="month",
        month="2026-09",
        year=None,
        as_of_date=None,
        include_rows=True,
    )
    assert option_performance.handle_option_performance_command(args) == expected
    payload, error = bot_tools.build_tool_payload(
        "option_performance_report", QUERY, fixed_input={"config_key": "us", "config_path": "/operator/trusted.json"}
    )
    assert error is None and "config_path" not in payload
    response = bot_tools.call_read_tool(
        "option_performance_report", payload, allowed_tools=("option_performance_report",), now_ms=1
    )
    assert response["ok"] and response["data"]["option_net_cashflow"] == expected["option_net_cashflow"]
    rejected, error = bot_tools.build_tool_payload("option_performance_report", {"config_path": "/bad"})
    assert rejected is None and "unsupported" in error
    with writer._connect() as conn:
        assert list(conn.iterdump()) == before


@pytest.mark.parametrize(
    "payload,status,code",
    [
        ({**QUERY, "config_key": "hk"}, 403, "SCOPE_ERROR"),
        ({**QUERY, "account": "unknown"}, 403, "SCOPE_ERROR"),
        ({**QUERY, "config_path": "/private/data"}, 400, "INPUT_ERROR"),
        ({**QUERY, "data_config": "/private/data"}, 400, "INPUT_ERROR"),
        ({**QUERY, "now_ms": 1}, 400, "INPUT_ERROR"),
        ({**QUERY, "include_rows": "true"}, 400, "INPUT_ERROR"),
        ({**QUERY, "account": []}, 400, "INPUT_ERROR"),
        ({**QUERY, "limit": True}, 400, "INPUT_ERROR"),
        ([], 400, "INPUT_ERROR"),
        (b"{", 400, "INPUT_ERROR"),
        (b'{"year": NaN}', 400, "INPUT_ERROR"),
        (b'{"account":"lx","account":"sy"}', 400, "INPUT_ERROR"),
        (b" " * (client.MAX_REQUEST_BYTES + 1), 400, "INPUT_ERROR"),
    ],
)
def test_server_rejects_invalid_requests_before_ledger_reads(service, monkeypatch, payload, status, code):
    url, _, _, _ = service
    monkeypatch.setattr(query_http, "build_option_period_performance", _forbid)
    actual, response = _request(url, payload)
    assert actual == status and response["error"]["code"] == code
    assert TOKEN not in json.dumps(response)


@pytest.mark.parametrize("token", ["", "wrong"])
def test_invalid_auth_rejects_without_query(service, monkeypatch, token):
    monkeypatch.setattr(query_http, "build_option_period_performance", _forbid)
    status, response = _request(service[0], QUERY, token=token)
    assert status == 401 and response["error"]["code"] == "AUTH_ERROR"


@pytest.mark.parametrize("method,path,status", [("GET", client.QUERY_PATH, 405), ("POST", "/other", 404)])
def test_exact_route_and_method(service, method, path, status):
    assert _request(service[0], QUERY, method=method, path=path)[0] == status


def test_read_failures_and_unhandled_errors_are_distinct_and_sanitized(service, monkeypatch):
    url, _, path, _ = service
    monkeypatch.setattr(
        query_http,
        "build_option_period_performance",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError(str(path) + TOKEN)),
    )
    status, body = _request(url, QUERY)
    assert status == 500 and body["error"]["code"] == "INTERNAL_ERROR"
    assert str(path) not in json.dumps(body) and TOKEN not in json.dumps(body)


def test_missing_ledger_returns_read_error_and_does_not_create(service):
    url, _, path, _ = service
    path.unlink()
    with pytest.raises(AgentToolError) as exc:
        _remote(url)
    assert exc.value.code == "READ_ERROR" and exc.value.details["reason_codes"] == ["ledger_read_failed"]
    assert not path.exists()


def test_cursor_keeps_server_time_and_detects_source_change(service, monkeypatch):
    from datetime import datetime, timezone

    url, writer, _, _ = service
    fixed = int(datetime(2026, 9, 2, 12, tzinfo=timezone.utc).timestamp() * 1000)

    class Clock(datetime):
        instant = fixed

        @classmethod
        def now(cls, tz=None):
            return datetime.fromtimestamp(cls.instant / 1000, tz=tz)

    monkeypatch.setattr(query, "datetime", Clock)
    from src.application.agent_tools import project_reader

    monkeypatch.setattr(project_reader, "_key", lambda: "isolated-cursor-key")
    request = {**QUERY, "view": "rows", "limit": 1}
    first = _remote(url, request)[0]
    assert first["pagination"]["has_more"] and first["next_cursor"]
    Clock.instant += 86400000
    second = _remote(url, {**request, "cursor": first["next_cursor"]})[0]
    assert first["period"] == second["period"]
    assert first["rows"][0]["fact_id"] != second["rows"][0]["fact_id"]
    writer.upsert_trade_event(replace(_event("new", "open", "2026-09-02T10:00:00"), lot_id="lot-new"))
    with pytest.raises(AgentToolError) as exc:
        _remote(url, {**request, "cursor": second["next_cursor"]})
    assert exc.value.code == "READ_ERROR" and exc.value.message == "source_changed"


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:1",
        "http://example.com:1",
        "http://127.0.0.1:1/",
        "http://test-user@127.0.0.1:1",
        "http://127.0.0.1:1?q=x",
        "http://127.0.0.1:1#x",
        "http://127.0.0.1",
    ],
)
def test_client_rejects_non_loopback_or_ambiguous_origins(url):
    with pytest.raises(AgentToolError) as exc:
        _remote(url)
    assert exc.value.code == "INPUT_ERROR"


def test_unavailable_service_never_falls_back(monkeypatch):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    monkeypatch.setenv("OM_PERFORMANCE_SERVICE_URL", f"http://127.0.0.1:{port}")
    monkeypatch.setenv("OM_PERFORMANCE_SERVICE_TOKEN", TOKEN)
    deps = {
        name: _forbid
        for name in (
            "load_runtime_config",
            "resolve_public_data_config_path",
            "normalize_broker",
            "resolve_option_positions_repo",
            "build_option_period_performance",
            "repo_base",
            "mask_path",
        )
    }
    with pytest.raises(AgentToolError) as exc:
        materialization_impl.option_performance_report_tool(QUERY, **deps)
    assert exc.value.code == "SERVICE_ERROR"
    assert exc.value.details["reason_codes"] == ["service_unavailable"]


@pytest.mark.parametrize(
    "body,status",
    [
        (b"not json", 200),
        (b"{}", 200),
        (json.dumps({"schema_version": "old", "ok": True, "data": {}, "meta": {}, "warnings": []}).encode(), 200),
        (
            json.dumps(
                {"schema_version": client.API_VERSION, "ok": True, "data": {}, "meta": {}, "warnings": []}
            ).encode(),
            503,
        ),
        (
            json.dumps(
                {"schema_version": client.API_VERSION, "ok": False, "error": {"code": "READ_ERROR", "message": "x"}}
            ).encode(),
            200,
        ),
        (b"x" * (client.MAX_RESPONSE_BYTES + 1), 200),
        (b"{}", 302),
    ],
    ids=["invalid-json", "missing-schema", "legacy-schema", "status-mismatch",
         "error-status-mismatch", "oversized-response", "redirect"],
)
def test_client_rejects_bad_envelopes_and_redirects(body, status):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Location", "http://example.com/")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    with _running(HTTPServer(("127.0.0.1", 0), Handler)) as url:
        with pytest.raises(AgentToolError) as exc:
            _remote(url)
        assert exc.value.details["reason_codes"] == ["service_protocol_invalid"]


def test_client_timeout_is_explicit(service, monkeypatch):
    monkeypatch.setattr(client, "CLIENT_TIMEOUT_SECONDS", 0.001)
    import time

    original = query_http.build_option_period_performance

    def slow(*args, **kw):
        time.sleep(0.05)
        return original(*args, **kw)

    monkeypatch.setattr(query_http, "build_option_period_performance", slow)
    with pytest.raises(AgentToolError) as exc:
        _remote(service[0])
    assert exc.value.details["reason_codes"] == ["service_unavailable"]


def test_missing_token_prevents_listen(monkeypatch):
    monkeypatch.setattr(query_http, "load_runtime_config", _forbid)
    with pytest.raises(AgentToolError, match="TOKEN"):
        query_http.create_performance_query_server(config_key="us", config_path=Path("/never"), token="", port=0)


def test_integer_year_and_client_path_rejection(service):
    assert _remote(service[0], {"period": "year", "year": 2025})[0]["period"]["kind"] == "year"
    with pytest.raises(AgentToolError) as exc:
        _remote(service[0], {**QUERY, "data_config": "/private/server-path"})
    assert exc.value.code == "INPUT_ERROR"


def test_partial_pagination_without_existing_cursor_key(service):
    data = _remote(service[0], {**QUERY, "view": "rows", "limit": 1})[0]
    assert data["next_cursor"] is None
    assert data["continuation_status"] == "continuation_unavailable"
    assert data["coverage"]["status"] == "partial"


def test_corrupt_service_ledger_is_read_error(service):
    url, writer, _, _ = service
    with writer._connect() as conn:
        conn.execute("DROP TABLE trade_events")
    with pytest.raises(AgentToolError) as exc:
        _remote(url)
    assert exc.value.code == "READ_ERROR"


def test_bot_trusted_account_fence_is_preserved(service, monkeypatch):
    from src.application import agent_tool_config

    monkeypatch.setenv("OM_PERFORMANCE_SERVICE_URL", service[0])
    monkeypatch.setattr(
        agent_tool_config, "load_runtime_config", lambda **kw: (Path("/trusted/config.json"), service[3])
    )
    fixed = {"config_key": "us", "config_path": "/trusted/config.json"}
    payload, error = bot_tools.build_tool_payload(
        "option_performance_report", {**QUERY, "account": "lx"}, fixed_input=fixed
    )
    assert error is None and payload["account"] == "lx" and "config_path" not in payload
    payload, error = bot_tools.build_tool_payload(
        "option_performance_report", {**QUERY, "account": "unknown"}, fixed_input=fixed
    )
    assert payload is None and "outside" in error


def test_public_cli_entry_routes_without_local_report_dependencies(service, monkeypatch, capsys):
    from src.interfaces.cli.main import main

    monkeypatch.setenv("OM_PERFORMANCE_SERVICE_URL", service[0])
    monkeypatch.setenv("OM_PERFORMANCE_SERVICE_TOKEN", TOKEN)
    monkeypatch.setattr(option_performance, "load_runtime_config", _forbid)
    monkeypatch.setattr(option_performance, "open_performance_query_ledger_from_data_config", _forbid)
    assert main(["option-performance", "report", "--period", "month", "--month", "2026-09"]) == 0
    assert json.loads(capsys.readouterr().out)["period"]["kind"] == "month"


def test_serve_public_entry_binds_operator_config_and_closes(monkeypatch):
    from src.interfaces.cli.main import main

    captured = {}

    class Server:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            captured["closed"] = True

        def serve_forever(self):
            raise KeyboardInterrupt

    def create(**kw):
        captured.update(kw)
        return Server()

    monkeypatch.setenv("OM_PERFORMANCE_SERVICE_TOKEN", TOKEN)
    monkeypatch.setattr(query_http, "create_performance_query_server", create)
    assert (
        main(
            [
                "option-performance",
                "serve",
                "--config-key",
                "hk",
                "--config-path",
                "/isolated/config.hk.json",
                "--data-config",
                "/isolated/portfolio.runtime.json",
                "--port",
                "0",
            ]
        )
        == 0
    )
    assert captured == {
        "config_key": "hk",
        "config_path": "/isolated/config.hk.json",
        "data_config": "/isolated/portfolio.runtime.json",
        "port": 0,
        "token": TOKEN,
        "closed": True,
    }


def test_response_limit_is_a_read_failure(service, monkeypatch):
    monkeypatch.setattr(query_http, "MAX_RESPONSE_BYTES", 1000)
    with pytest.raises(AgentToolError) as exc:
        _remote(service[0])
    assert exc.value.code == "READ_ERROR" and "paged" in exc.value.message


def test_existing_generated_config_startup_is_read_only(example_config_path, monkeypatch):
    monkeypatch.setenv("OM_RUNTIME_ROOT", str(example_config_path.parent))
    server = query_http.create_performance_query_server(
        config_key="us", config_path=example_config_path, token=TOKEN, port=0
    )
    with _running(server) as url:
        with pytest.raises(AgentToolError) as exc:
            _remote(url)
        assert exc.value.code == "READ_ERROR"
    assert not (example_config_path.parent / "output_shared/state/option_positions.sqlite3").exists()


def test_request_header_framing_rejects_before_read(service):
    url = service[0]
    for extra in ({"Content-Length": "9" * 5000}, {"Transfer-Encoding": "chunked"}, {"Content-Type": "text/plain"}):
        headers = {"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json", **extra}
        status, body = _request(url, QUERY, headers=headers)
        assert status == 400 and body["error"]["code"] == "INPUT_ERROR"


@pytest.mark.parametrize("raw", [b'{"x":1e999}', b'{"x":1,"x":2}', b'{"x":Infinity}'])
def test_strict_wire_json_rejects_duplicate_and_nonfinite_numbers(raw):
    with pytest.raises(ValueError):
        client.decode_protocol_json(raw)


def test_income_control_projects_trusted_paths_and_rejects_identity_mismatch(service, monkeypatch):
    from src.application.bot import config_scope
    from src.application.bot.control.inbound_control import execute_explicit_control
    from src.application.bot.control.contracts import BotInboundRequest, ControlCommand

    monkeypatch.setenv("OM_PERFORMANCE_SERVICE_URL", service[0])
    monkeypatch.setenv("OM_PERFORMANCE_SERVICE_TOKEN", TOKEN)
    monkeypatch.setattr(
        config_scope, "resolve_trusted_config_scope", lambda **kw: ("us", "/trusted/config.us.json", "test-authority")
    )
    command = ControlCommand("option_performance_report", {"period": "month", "month": "2026-09"})
    request = BotInboundRequest(text="/income 2026-09", sender_id="test", config_path="/trusted/config.us.json")
    result = execute_explicit_control(command, request=request, command_id="isolated-test", operation_store=None)
    assert result.ok and result.payload == QUERY
    with pytest.raises(AgentToolError) as exc:
        execute_explicit_control(
            command, request=replace(request, config_key="hk"), command_id="isolated-test", operation_store=None
        )
    assert exc.value.code == "SCOPE_ERROR"

    def fail(**kw):
        raise config_scope.BotConfigScopeError("config_missing")

    monkeypatch.setattr(config_scope, "resolve_trusted_config_scope", fail)
    with pytest.raises(AgentToolError) as exc:
        execute_explicit_control(command, request=request, command_id="isolated-test", operation_store=None)
    assert exc.value.code == "SCOPE_ERROR" and "/trusted/" not in exc.value.message
