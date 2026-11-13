"""Offline native-I/O and sender regressions for Tick's shared delivery budget."""
from copy import deepcopy
import json
import socket
import threading
from time import monotonic
import urllib.error
import urllib.request

import pytest

from src.application import notification_delivery_adapter as adapters
from src.application.channels import feishu_notification
from src.application.channels.wechat_clawbot import ilink_client
from src.application.channels.plugins import load_channel_plugin
from src.infrastructure import feishu_bitable as feishu_http
from tests.test_feishu_notification_sender import _fallback_envelope, _wechat_state


@pytest.fixture(autouse=True)
def isolated_feishu_auth(monkeypatch):
    monkeypatch.setenv("OM_SECRET_BACKEND", "env")
    monkeypatch.setenv("OM_FEISHU_BOT_APP_ID", "offline_app")
    monkeypatch.setenv("OM_FEISHU_BOT_APP_SECRET", "offline_secret")
    monkeypatch.setenv("OM_FEISHU_BOT_USER_OPEN_ID", "ou_1")
    monkeypatch.setattr(feishu_http, "_token_cache", {})
    monkeypatch.setattr(feishu_http, "_token_lock", threading.Lock())


class JsonResponse:
    status = 200

    def __init__(self, value, before_read=None):
        self.value = value
        self.before_read = before_read

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass

    def read(self):
        if self.before_read is not None:
            self.before_read()
        return json.dumps(self.value).encode()


def sender_arguments(tmp_path, provider, *, card=False):
    if provider == "feishu_app":
        kwargs = {"base": tmp_path, "channel": provider, "target": "ou_1",
                  "message": "frozen body", "notifications": {"_explicit_route_target": True},
                  "idempotency_key": "frozen-delivery-key"}
        if card:
            kwargs["message"], kwargs["transport_envelope"] = _fallback_envelope()
        return kwargs
    state_dir = _wechat_state(tmp_path)
    return {"base": tmp_path, "channel": provider, "target": "ops",
            "message": "frozen body", "idempotency_key": "frozen-delivery-key",
            "notifications": {"wechat_clawbot_state_dir": str(state_dir), "send_timeout_sec": 12}}


def provider_success(provider):
    if provider == "feishu_app":
        return {"code": 0, "data": {"message_id": "offline_message"}}
    return {"ret": 0, "data": {"message_id": "offline_message"}}


def auth_response():
    return {"code": 0, "tenant_access_token": "offline_token", "expire": 7200}


def is_auth(request):
    return "/auth/" in request.full_url


@pytest.mark.parametrize("provider", ["feishu_app", "wechat_clawbot"])
@pytest.mark.parametrize("cancel_in_flight", [False, True])
def test_real_socket_read_uses_remaining_timeout_and_is_not_instantly_cancelled(
    monkeypatch, provider, cancel_in_flight, record_property,
):
    # Only urlopen is replaced. read() blocks in the OS on a private socketpair;
    # no server, network connection or provider request is created.
    if provider == "wechat_clawbot":
        load_channel_plugin("wechat_clawbot", module="ilink_client")
    cancelled = threading.Event()
    attempts = []
    native_waits = []

    class BlockingResponse:
        status = 200

        def __init__(self, timeout):
            self.reader, self.writer = socket.socketpair()
            self.reader.settimeout(timeout)
            self.timer = None

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.reader.close()
            self.writer.close()
            if self.timer is not None:
                self.timer.cancel()
                self.timer.join(timeout=1)

        def read(self):
            if cancel_in_flight:
                self.timer = threading.Timer(0.01, cancelled.set)
                self.timer.start()
            started = monotonic()
            try:
                return self.reader.recv(1)
            finally:
                native_waits.append(monotonic() - started)

    def blocked_urlopen(request, *, timeout):
        attempts.append((request, timeout))
        return BlockingResponse(timeout)

    monkeypatch.setattr(urllib.request, "urlopen", blocked_urlopen)
    deadline = monotonic() + 0.12
    http = feishu_http.http_json if provider == "feishu_app" else ilink_client.http_json
    started = monotonic()
    with pytest.raises(TimeoutError):
        http("POST", "https://offline.invalid/send", {"message": "fixed"},
             timeout=20, deadline=deadline, cancelled=cancelled.is_set)
    elapsed = monotonic() - started
    assert len(attempts) == 1
    timeout = attempts[0][1]
    assert 0 < timeout <= 0.12
    assert native_waits[0] >= timeout * 0.7
    assert elapsed < 1.0
    assert cancelled.is_set() is cancel_in_flight
    record_property("provider", provider)
    record_property("cancel_in_flight", str(cancel_in_flight))
    record_property("native_socket_timeout_seconds", timeout)
    record_property("native_read_elapsed_seconds", native_waits[0])
    record_property("total_elapsed_seconds", elapsed)
    record_property("provider_attempts", len(attempts))


@pytest.mark.parametrize("provider", ["feishu_app", "wechat_clawbot"])
def test_adapter_controls_do_not_change_frozen_payload_or_delivery_identity(tmp_path, monkeypatch, provider):
    kwargs = sender_arguments(tmp_path, provider)
    frozen = deepcopy(kwargs)
    requests = []

    def urlopen(request, *, timeout):
        requests.append((request, timeout))
        return JsonResponse(auth_response() if is_auth(request) else provider_success(provider))

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    adapter = adapters.select_notification_delivery_adapter(provider)
    old = adapter.send_fn(**kwargs)
    baseline = [request.data for request, _ in requests if not is_auth(request)]
    requests.clear()
    controlled = adapter.send_fn(**kwargs, deadline=monotonic() + 0.5, cancelled=lambda: False)
    payloads = [request.data for request, _ in requests if not is_auth(request)]
    assert old.returncode == controlled.returncode == 0
    assert kwargs == frozen
    assert payloads == baseline
    assert old.raw == controlled.raw
    assert requests and all(0 < timeout <= 0.5 for _, timeout in requests)
    assert b"deadline" not in payloads[0] and b"cancelled" not in payloads[0]


@pytest.mark.parametrize("provider", ["feishu_app", "wechat_clawbot"])
def test_fallback_keeps_the_same_deadline_and_frozen_inputs(tmp_path, monkeypatch, provider):
    kwargs = sender_arguments(tmp_path, provider, card=provider == "feishu_app")
    frozen = deepcopy(kwargs)
    calls = []

    def urlopen(request, *, timeout):
        if is_auth(request):
            return JsonResponse(auth_response())
        calls.append((json.loads(request.data), timeout))
        if len(calls) == 1:
            rejected = {"code": 230001, "msg": "invalid card"} if provider == "feishu_app" else {"data": {"ret": -2}}
            return JsonResponse(rejected, lambda: threading.Event().wait(0.02))
        return JsonResponse(provider_success(provider))

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    result = adapters.select_notification_delivery_adapter(provider).send_fn(
        **kwargs, deadline=monotonic() + 0.5, cancelled=lambda: False,
    )
    assert result.returncode == 0
    assert kwargs == frozen
    assert len(calls) == 2
    assert 0 < calls[1][1] < calls[0][1] <= 0.5
    if provider == "feishu_app":
        key = kwargs["idempotency_key"]
        assert [payload["uuid"] for payload, _ in calls] == [key, f"{key}:fallback"]
    else:
        assert calls[0][0]["msg"]["client_id"] == calls[1][0]["msg"]["client_id"]
        assert [payload["msg"]["context_token"] for payload, _ in calls] == ["ctx_1", ""]


@pytest.mark.parametrize("provider", ["feishu_app", "wechat_clawbot"])
def test_cancellation_after_http_response_does_not_start_fallback(tmp_path, monkeypatch, provider):
    kwargs = sender_arguments(tmp_path, provider, card=provider == "feishu_app")
    cancelled = threading.Event()
    calls = []

    def urlopen(request, *, timeout):
        if is_auth(request):
            return JsonResponse(auth_response())
        calls.append(request)
        rejected = {"code": 230001, "msg": "invalid card"} if provider == "feishu_app" else {"ret": -2}
        return JsonResponse(rejected, cancelled.set)

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    with pytest.raises(TimeoutError):
        adapters.select_notification_delivery_adapter(provider).send_fn(
            **kwargs, deadline=monotonic() + 0.5, cancelled=cancelled.is_set,
        )
    assert len(calls) == 1


@pytest.mark.parametrize("provider", ["feishu_app", "wechat_clawbot"])
def test_cancelled_adapter_does_not_start_provider_io(tmp_path, monkeypatch, provider):
    monkeypatch.setattr(urllib.request, "urlopen", lambda *_a, **_k: pytest.fail("cancelled send opened HTTP"))
    with pytest.raises(TimeoutError):
        adapters.select_notification_delivery_adapter(provider).send_fn(
            base=tmp_path, channel=provider, target="unused", message="frozen body",
            deadline=monotonic() + 1, cancelled=lambda: True,
        )


@pytest.mark.parametrize("stop", ["deadline", "cancelled", "local_lock_cap"])
def test_tenant_lock_wait_consumes_parent_budget_and_remains_owned(monkeypatch, stop, record_property):
    lock = feishu_http._token_lock
    lock.acquire()
    cancelled = threading.Event()
    timer = threading.Timer(0.02, cancelled.set) if stop == "cancelled" else None
    monkeypatch.setattr(urllib.request, "urlopen", lambda *_a, **_k: pytest.fail("busy token lock reached HTTP"))
    if timer is not None:
        timer.start()
    started = monotonic()
    try:
        kwargs = {"deadline": monotonic() + (0.06 if stop == "deadline" else 1), "cancelled": cancelled.is_set}
        if stop == "local_lock_cap":
            kwargs["lock_timeout"] = 0.01
        error = feishu_http.FeishuTransientError if stop == "local_lock_cap" else TimeoutError
        with pytest.raises(error):
            feishu_http.get_tenant_access_token("offline_app", "offline_secret", **kwargs)
        assert lock.locked()
    finally:
        lock.release()
        if timer is not None:
            timer.cancel()
            timer.join(timeout=1)
    elapsed = monotonic() - started
    assert elapsed < 0.5
    record_property("stop", stop)
    record_property("lock_wait_elapsed_seconds", elapsed)


def test_auth_refresh_does_not_reset_delivery_deadline(tmp_path, monkeypatch):
    calls = []
    message_attempts = 0

    def urlopen(request, *, timeout):
        nonlocal message_attempts
        calls.append((request, timeout))
        if is_auth(request):
            return JsonResponse(auth_response(), lambda: threading.Event().wait(0.01))
        message_attempts += 1
        if message_attempts == 1:
            return JsonResponse({"code": 99991663, "msg": "token expired"})
        return JsonResponse(provider_success("feishu_app"))

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    kwargs = sender_arguments(tmp_path, "feishu_app")
    result = adapters.select_notification_delivery_adapter("feishu_app").send_fn(
        **kwargs, deadline=monotonic() + 0.5, cancelled=lambda: False,
    )
    assert result.returncode == 0
    assert len(calls) == 4
    assert [is_auth(request) for request, _ in calls] == [True, False, True, False]
    assert all(later < earlier for (_, earlier), (_, later) in zip(calls, calls[1:]))
    assert [json.loads(request.data)["uuid"] for request, _ in calls if not is_auth(request)] == [kwargs["idempotency_key"]] * 2


@pytest.mark.parametrize("stop", ["deadline", "cancelled"])
def test_http_backoff_stops_without_another_provider_attempt(monkeypatch, stop, record_property):
    attempts = []
    cancelled = threading.Event()

    def urlopen(request, *, timeout):
        attempts.append(request)
        raise urllib.error.URLError("offline transient failure")

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    timer = threading.Timer(0.02, cancelled.set) if stop == "cancelled" else None
    if timer is not None:
        timer.start()
    started = monotonic()
    try:
        with pytest.raises(TimeoutError):
            feishu_http.http_json(
                "POST", "https://offline.invalid/send", {},
                deadline=monotonic() + (0.06 if stop == "deadline" else 1),
                cancelled=cancelled.is_set, log_fn=lambda _: None,
            )
    finally:
        if timer is not None:
            timer.cancel()
            timer.join(timeout=1)
    elapsed = monotonic() - started
    assert len(attempts) == 1
    assert elapsed < 0.5
    record_property("stop", stop)
    record_property("backoff_elapsed_seconds", elapsed)
    record_property("provider_attempts", len(attempts))
