"""Loopback transport admission, authorization and error mapping."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import http.client
import json
from pathlib import Path
import threading

import pytest

from om_group_query_bot.api import (
    MAX_BODY_BYTES,
    APIService,
    AuthenticationError,
    create_server,
)
from om_group_query_bot.budget import BudgetExhausted
from om_group_query_bot.business import BusinessUnavailable, Dependencies
from om_group_query_bot.config import load_api_settings
from om_group_query_bot.store import DeliveryStore

NOW = datetime(2026, 11, 10, 6, 0, tzinfo=timezone.utc)
TOKEN = "dedicated-query-token"


def _settings(tmp_path, *, capabilities=None, principal_per_minute=None):
    payload = {
        "instance_id": "gq",
        "data_dir": str(tmp_path / "data"),
        "api": {"bind_host": "127.0.0.1", "bind_port": 8765,
                "token_ref": "group_query_bot.query_api.token", "app_id": "cli_group"},
        "principal": {"principal_id": "group-query-bot",
                      "capabilities": capabilities or [
                          "market:read", "candidates:evaluate", "scenario:close",
                          "scenario:roll-down", "scenario:roll-up"],
                      "markets": ["us"], "account_scope": []},
        "quote": {"source": "futu", "host": "127.0.0.1", "port": 11111},
        "max_budget_seconds": 45,
        "public_policy": {"policy": {"sell_put": {"min_dte": 20}},
                          "fee_model_id": "futu_option_fee",
                          "fee_model_version": "futu_option_sell_fee.v1",
                          "strategy_version": "remaining_yield_capture.v3"},
    }
    if principal_per_minute is not None:
        payload["principal_requests_per_minute"] = principal_per_minute
    path = tmp_path / "api.json"
    path.write_text(json.dumps(payload))
    return load_api_settings(path)


class _Recorder:
    def __init__(self):
        self.calls = []

    def fetch_symbol(self, base: Path, request):
        self.calls.append(("fetch", str(base)))
        raise AssertionError("quote I/O must not run in this test")

    def exchange_rates(self, settings, now, budget):
        self.calls.append(("rates", now.isoformat()))
        raise AssertionError("rate I/O must not run in this test")


def _service(tmp_path, *, capabilities=None, principal_per_minute=None):
    recorder = _Recorder()
    service = APIService(
        settings=_settings(tmp_path, capabilities=capabilities,
                           principal_per_minute=principal_per_minute),
        token=TOKEN,
        store=DeliveryStore(tmp_path / "data"),
        dependencies=Dependencies(fetch_symbol=recorder.fetch_symbol,
                                  exchange_rates=recorder.exchange_rates),
        now=lambda: NOW,
    )
    return service, recorder


def _body(parameters, *, deadline=60, request_id="req-1", **extra):
    body = {
        "schema_version": "group_query_request.v1",
        "request_id": request_id,
        "request_deadline_utc": (NOW + timedelta(seconds=deadline)).isoformat(),
        "request_context": {"tenant_key": "tenant", "chat_id": "chat",
                            "sender_open_id": "sender", "thread_id": None},
        "parameters": parameters,
    }
    body.update(extra)
    return body


def _scenario_body(**extra):
    body = _body({"position": {
        "symbol": "US.NVDA",
        "contract": {"option_type": "put", "expiration": "2026-12-18",
                     "strike": 180.0},
        "side": "short", "contracts": 2},
        "opening_premium": {"amount": 2.5, "unit": "per_share", "currency": "USD"}},
        **extra)
    body["input_id"] = "input-1"
    body["input_as_of_utc"] = (NOW - timedelta(seconds=30)).isoformat()
    return body


def test_bearer_authentication_is_strict(tmp_path):
    service, _ = _service(tmp_path)
    service.authenticate(f"Bearer {TOKEN}")
    for header in (None, "", TOKEN, f"bearer {TOKEN}", f"Bearer {TOKEN} ",
                   "Bearer other"):
        with pytest.raises(AuthenticationError):
            service.authenticate(header)


def test_ungranted_capability_is_denied_before_body_or_io(tmp_path):
    service, recorder = _service(tmp_path, capabilities=["market:read"])
    payload, status = service.handle("evaluate_close", {"request_id": "r"})
    assert status == 403
    assert payload["error"]["code"] == "PERMISSION_DENIED"
    assert payload["request_id"] == "r"
    assert recorder.calls == []


def test_contract_rejections_are_enveloped_before_io(tmp_path):
    service, recorder = _service(tmp_path)
    payload, status = service.handle(
        "query_quotes", _body({"symbol": "NVDA", "account": "lx"}))
    assert status == 400
    assert payload["ok"] is False and payload["data"] is None
    assert payload["error"]["code"] == "INPUT_ERROR"
    assert recorder.calls == []


def test_expired_deadline_maps_to_gateway_timeout(tmp_path):
    service, recorder = _service(tmp_path)
    payload, status = service.handle(
        "query_quotes", _body({"symbol": "US.NVDA"}, deadline=-1))
    assert status == 504
    assert payload["error"]["code"] == "UPSTREAM_TIMEOUT"
    assert recorder.calls == []


def test_principal_request_rate_is_bounded_before_business_io(tmp_path):
    service, recorder = _service(tmp_path, principal_per_minute=1)
    assert service._slots.acquire(blocking=False)
    assert service._slots.acquire(blocking=False)
    try:
        first, first_status = service.handle(
            "query_quotes", _body({"symbol": "US.NVDA"}))
    finally:
        service._slots.release()
        service._slots.release()
    # The first request is stopped by the concurrency gate, but it still counts
    # against the principal's one request in the window.
    assert first_status == 429
    assert first["error"]["message"] == "too many concurrent group-query requests"

    payload, status = service.handle(
        "query_quotes", _body({"symbol": "US.NVDA"}, request_id="req-2"))
    assert status == 429
    assert payload["error"]["code"] == "RATE_LIMITED"
    assert "principal request rate exceeded" in payload["error"]["message"]
    assert recorder.calls == []


def test_admission_returns_rate_limited_when_slots_are_full(tmp_path):
    service, recorder = _service(tmp_path)
    assert service._slots.acquire(blocking=False)
    assert service._slots.acquire(blocking=False)
    try:
        payload, status = service.handle("query_quotes", _body({"symbol": "US.NVDA"}))
    finally:
        service._slots.release()
        service._slots.release()
    assert status == 429
    assert payload["error"]["code"] == "RATE_LIMITED"
    assert payload["request_id"] == "req-1"
    assert recorder.calls == []


def test_business_errors_collapse_to_bounded_envelopes(tmp_path):
    service, _ = _service(tmp_path)
    payload, status = service.business.execute_error("r", BusinessUnavailable("db down"))
    assert (status, payload["error"]["code"]) == (503, "DEPENDENCY_UNAVAILABLE")
    assert "db down" not in payload["error"]["message"]
    payload, status = service.business.execute_error("r", BudgetExhausted("deadline"))
    assert (status, payload["error"]["code"]) == (504, "UPSTREAM_TIMEOUT")
    payload, status = service.business.execute_error("r", RuntimeError("secret detail"))
    assert (status, payload["error"]["code"]) == (500, "INTERNAL_ERROR")
    assert "secret detail" not in payload["error"]["message"]


@pytest.fixture()
def live_server(tmp_path):
    service, recorder = _service(tmp_path)
    server = create_server(_settings(tmp_path), token=TOKEN,
                           store=DeliveryStore(tmp_path / "data"),
                           dependencies=Dependencies(
                               fetch_symbol=recorder.fetch_symbol,
                               exchange_rates=recorder.exchange_rates),
                           now=lambda: NOW, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1], recorder
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _post(port, path, *, headers=None, body=b""):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        connection.request("POST", path, body=body, headers=headers or {})
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


def test_transport_rejects_unknown_route_and_non_post_methods(live_server):
    port, recorder = live_server
    status, payload = _post(port, "/unknown", body=b"{}")
    assert status == 404 and payload["error"]["code"] == "NOT_FOUND"
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        connection.request("GET", "/api/group-query-bot/v1/quotes")
        response = connection.getresponse()
        assert response.status == 405
        assert json.loads(response.read())["error"]["code"] == "METHOD_NOT_ALLOWED"
    finally:
        connection.close()
    assert recorder.calls == []


def test_transport_requires_bearer_before_body_or_io(live_server):
    port, recorder = live_server
    status, payload = _post(port, "/api/group-query-bot/v1/quotes", body=b"{}")
    assert status == 401 and payload["error"]["code"] == "UNAUTHORIZED"
    status, payload = _post(port, "/api/group-query-bot/v1/quotes", headers={
        "Authorization": "Bearer wrong", "Content-Type": "application/json"},
        body=b"{}")
    assert status == 401
    assert recorder.calls == []


def test_transport_bounds_body_size_and_requires_json(live_server):
    port, recorder = live_server
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        connection.putrequest("POST", "/api/group-query-bot/v1/quotes")
        connection.putheader("Authorization", f"Bearer {TOKEN}")
        connection.putheader("Content-Length", str(MAX_BODY_BYTES + 1))
        connection.endheaders()
        response = connection.getresponse()
        assert response.status == 400
        assert json.loads(response.read())["error"]["code"] == "INPUT_ERROR"
    finally:
        connection.close()
    status, payload = _post(port, "/api/group-query-bot/v1/quotes", headers={
        "Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
        body=b"{not-json")
    assert status == 400 and payload["error"]["code"] == "INPUT_ERROR"
    assert recorder.calls == []


def test_transport_serves_the_contract_error_envelope(live_server):
    port, recorder = live_server
    body = _scenario_body()
    del body["input_id"]
    status, payload = _post(port, "/api/group-query-bot/v1/scenarios/close", headers={
        "Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
        body=json.dumps(body).encode())
    assert status == 400
    assert payload["ok"] is False
    assert payload["request_id"] == "req-1"
    assert recorder.calls == []


class _GatedBusiness:
    """Blocks every call until its own event is released, recording admission."""

    def __init__(self):
        self.lock = threading.Lock()
        self.admitted = []
        self.events = {}

    def execute(self, request, principal, budget):
        event = threading.Event()
        with self.lock:
            self.admitted.append(request.request_id)
            self.events[request.request_id] = event
        assert event.wait(timeout=10), "gated call was never released"
        return {"schema_version": "group_query_response.v1",
                "request_id": request.request_id, "ok": True, "data": {},
                "error": None}

    def execute_error(self, request_id, exc):
        return ({"schema_version": "group_query_response.v1", "request_id": request_id,
                 "ok": False, "data": None,
                 "error": {"code": "INPUT_ERROR", "message": "rejected"}}, 400)

    def release(self, request_id):
        self.events[request_id].set()


def _gated_call(service, results, request_id):
    body = _body({"symbol": "US.NVDA"}, request_id=request_id)
    results[request_id] = service.handle("query_quotes", body)


def test_only_an_actually_finished_request_frees_one_slot(tmp_path):
    service, _ = _service(tmp_path)
    gated = _GatedBusiness()
    service.business = gated
    results = {}
    threads = {}
    for request_id in ("r1", "r2"):
        threads[request_id] = threading.Thread(
            target=_gated_call, args=(service, results, request_id), daemon=True)
        threads[request_id].start()
    for _ in range(200):
        with gated.lock:
            if len(gated.admitted) == 2:
                break
        threading.Event().wait(0.01)
    with gated.lock:
        assert sorted(gated.admitted) == ["r1", "r2"]
    payload, status = service.handle(
        "query_quotes", _body({"symbol": "US.NVDA"}, request_id="r3"))
    assert status == 429 and payload["request_id"] == "r3"
    gated.release("r1")
    threads["r1"].join(timeout=10)
    assert results["r1"][1] == 200
    threads["r4"] = threading.Thread(
        target=_gated_call, args=(service, results, "r4"), daemon=True)
    threads["r4"].start()
    for _ in range(200):
        with gated.lock:
            if "r4" in gated.admitted:
                break
        threading.Event().wait(0.01)
    with gated.lock:
        assert "r4" in gated.admitted
        assert "r2" in gated.admitted
    payload, status = service.handle(
        "query_quotes", _body({"symbol": "US.NVDA"}, request_id="r5"))
    assert status == 429 and payload["request_id"] == "r5"
    gated.release("r2")
    gated.release("r4")
    threads["r2"].join(timeout=10)
    threads["r4"].join(timeout=10)
    assert results["r2"][1] == 200
    assert results["r4"][1] == 200
