"""Real binding/transport failures must preserve state and redact public errors."""
from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path
import socket
import sys
import traceback
from types import SimpleNamespace
import urllib.error
import urllib.request

import pytest

from src.application.agent_tool_contracts import AgentToolError, build_error_payload
from src.application.channels.wechat_clawbot import binding
from src.application.channels.wechat_clawbot.ilink_client import WechatClawbotError
from src.application.channels.wechat_clawbot.state_store import WechatClawbotStateStore
from src.interfaces.cli.main import _print_error
from tests.test_channel_configure_setup import command, run, starter  # noqa: F401


PLUGIN_FILE = Path(__file__).resolve().parents[1] / "plugins/wechat-channel/src/om_wechat_channel/ilink_client.py"
PRIVATE_VALUES = ("SYNTHETIC_BOT_CREDENTIAL", "SYNTHETIC_CONTEXT_CREDENTIAL", "SYNTHETIC_PRIVATE_CURSOR")
MESSAGE = {"from_user_id": "qa-user", "message_id": "bind-1", "context_token": PRIVATE_VALUES[1],
           "text_item": {"text": "bind ops"}}


@pytest.fixture(autouse=True)
def transport(monkeypatch):
    # The installed wheel is a different snapshot; exercise this checkout's owner.
    spec = importlib.util.spec_from_file_location("om_wechat_channel.ilink_client", PLUGIN_FILE)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    assert Path(module.__file__).resolve() == PLUGIN_FILE

    def forbidden(*args, **kwargs):
        pytest.fail("regression attempted a real network connection")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    return module


@pytest.fixture
def case(tmp_path, monkeypatch):
    bot = tmp_path / "bot.json"
    bot.write_text(json.dumps({"bot": {"enabled": False}, "channels": {"wechat_clawbot": {
        "enabled": True, "receive_enabled": True, "notify_enabled": False}}}))
    store = WechatClawbotStateStore(tmp_path / "state")
    store.save_state({"bot_token": PRIVATE_VALUES[0], "base_url": "https://fixture.invalid",
                      "get_updates_buf": "original-cursor"})
    store.save_bindings({"bindings": {"existing": {"to_user_id": "old-user", "context_token": "old-context"}}})
    before = store.state_path.read_bytes(), store.bindings_path.read_bytes()
    responses, requests = [], []

    def urlopen(request, *, timeout):
        assert request.full_url == "https://fixture.invalid/ilink/bot/getupdates"
        assert request.get_method() == "POST"
        requests.append(json.loads(request.data))
        return io.BytesIO(json.dumps(responses.pop(0)).encode())

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)

    def invoke():
        return binding.bind_wechat_clawbot_target(base=tmp_path, name="ops", match_text="bind ops",
            state_dir=str(store.state_dir), bot_config_path=str(bot))

    return SimpleNamespace(store=store, before=before, responses=responses, requests=requests, invoke=invoke)


@pytest.mark.parametrize("status,code", [({"ret": -2}, -2), ({"errcode": " -2 "}, -2),
    ({"code": 5}, 5), ({"ret": -2, "code": 0}, -2), ({"ok": False, "ret": -2}, -2)])
@pytest.mark.parametrize("with_message", [False, True])
@pytest.mark.parametrize("with_cursor", [False, True])
def test_binding_explicit_failure_never_consumes_payload(case, status, code, with_message, with_cursor):
    response = {**status, "errmsg": PRIVATE_VALUES[0], "msgs": [MESSAGE] if with_message else []}
    if with_cursor:
        response["get_updates_buf"] = PRIVATE_VALUES[2]
    case.responses.append(response)
    result = case.invoke()
    assert result["ok"] is False
    assert result["error"] == {"code": "POLL_FAILED", "message": "WeChat ClawBot getupdates failed"}
    assert result["meta"] == {"provider_response_code": code}
    assert case.store.state_path.read_bytes() == case.before[0]
    assert case.store.bindings_path.read_bytes() == case.before[1]
    assert case.requests == [{"base_info": {"channel_version": "1.0.0"}, "get_updates_buf": "original-cursor"}]
    assert all(value not in json.dumps(result) for value in PRIVATE_VALUES)


@pytest.mark.parametrize("status", [{"ret": 0}, {"code": " -0 "}, {}, {"ok": True, "ret": -2},
    {"ret": 0, "errcode": -2}, {"ret": "unparsed"}, {"ok": False}])
@pytest.mark.parametrize("with_message", [False, True])
def test_binding_empty_compatibility_and_success(case, status, with_message):
    case.responses.append({**status, "data": {"msgs": [MESSAGE] if with_message else [],
                                             "get_updates_buf": "next-cursor"}})
    result = case.invoke()
    assert result["ok"] is with_message
    if with_message:
        assert result["data"]["target"] == "wechat:default:ops"
        assert case.store.load_state()["get_updates_buf"] == "next-cursor"
        assert case.store.load_bindings()["bindings"]["ops"]["context_token"] == PRIVATE_VALUES[1]
    else:
        assert result["error"]["code"] == "BINDING_MESSAGE_NOT_FOUND"
        assert case.store.state_path.read_bytes() == case.before[0]
        assert case.store.bindings_path.read_bytes() == case.before[1]


def test_binding_retry_uses_original_cursor_then_commits_success(case):
    case.responses.extend([{"ret": -2, "msgs": [MESSAGE], "get_updates_buf": PRIVATE_VALUES[2]},
                           {"ret": 0, "msgs": [MESSAGE], "get_updates_buf": "next-cursor"}])
    assert case.invoke()["ok"] is False
    assert case.invoke()["ok"] is True
    assert [request["get_updates_buf"] for request in case.requests] == ["original-cursor", "original-cursor"]
    assert case.store.load_state()["get_updates_buf"] == "next-cursor"
    assert set(case.store.load_bindings()["bindings"]) == {"existing", "ops"}


def test_cli_connect_stops_on_explicit_failure_without_publication(starter, monkeypatch):
    calls = []

    def urlopen(request, *, timeout):
        calls.append(request.full_url)
        assert len(calls) <= 3, "explicit provider failures must not enter binding retries"
        if len(calls) == 1:
            response = {"qrcode": "offline-qr"}
        elif len(calls) == 2:
            response = {"status": "confirmed", "bot_token": PRIVATE_VALUES[0], "get_updates_buf": "original-cursor"}
        else:
            response = {"ret": -2, "msgs": [MESSAGE], "get_updates_buf": PRIVATE_VALUES[2]}
        return io.BytesIO(json.dumps(response).encode())

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    before = starter.read_bytes()
    answers = iter(["wechat_clawbot", "true", "true", "false", "y"])
    result = run(command(starter, "--interactive"), input_is_tty=lambda: True,
        input_fn=lambda _: next(answers), output_fn=lambda _: None)
    assert result["ok"] is False and result["status"] == "binding_incomplete"
    step = result["completed_steps"][0]
    assert step["wechat_binding"]["error"]["code"] == "POLL_FAILED"
    assert step["wechat_binding"]["meta"] == {"provider_response_code": -2}
    assert step["readback"] == {"login_saved": True, "binding_saved": False}
    store = WechatClawbotStateStore(Path(step["state_dir"]))
    assert store.load_state()["get_updates_buf"] == "original-cursor"
    assert not store.bindings_path.exists()
    assert starter.read_bytes() == before
    assert all(value not in json.dumps(result) for value in PRIVATE_VALUES)


def _failing_urlopen(kind):
    raw = json.dumps(dict(zip(("bot_token", "context_token", "get_updates_buf"), PRIVATE_VALUES))).encode()

    def urlopen(request, *, timeout):
        if kind == "http":
            raise urllib.error.HTTPError(request.full_url, 503, PRIVATE_VALUES[0], {}, io.BytesIO(raw))
        if kind == "network":
            raise urllib.error.URLError(PRIVATE_VALUES[0])
        return io.BytesIO(raw[:-1])

    return urlopen


@pytest.mark.parametrize("kind,diagnostic", [("http", "HTTP error 503"), ("invalid_json", "invalid JSON"),
                                           ("network", "network error: URLError")])
def test_transport_errors_are_safe_including_exception_chain(transport, monkeypatch, kind, diagnostic):
    monkeypatch.setattr(urllib.request, "urlopen", _failing_urlopen(kind))
    with pytest.raises(WechatClawbotError) as caught:
        transport.http_json("GET", "https://fixture.invalid/login")
    assert diagnostic in str(caught.value)
    public = str(caught.value) + repr(vars(caught.value)) + "".join(traceback.format_exception(caught.value))
    assert all(value not in public for value in PRIVATE_VALUES)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


@pytest.mark.parametrize("kind", ["http", "invalid_json", "network"])
@pytest.mark.parametrize("stage", ["qr", "login", "binding"])
def test_cli_real_transport_error_never_exposes_credentials(starter, transport, monkeypatch, capsys, kind, stage):
    state_dir = starter.parent / "output_shared/state/channels/wechat_clawbot/default"
    calls = []
    fail = _failing_urlopen(kind)

    def urlopen(request, *, timeout):
        current = ("qr" if "/get_bot_qrcode?" in request.full_url else
                   "login" if "/get_qrcode_status?" in request.full_url else "binding")
        calls.append(current)
        if current == stage:
            return fail(request, timeout=timeout)
        response = ({"qrcode": "offline-qr"} if current == "qr" else
                    {"status": "confirmed", "bot_token": PRIVATE_VALUES[0], "get_updates_buf": "original-cursor"})
        return io.BytesIO(json.dumps(response).encode())

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    before = starter.read_bytes()
    answers = iter(["wechat_clawbot", "true", "true", "false", "y"])
    with pytest.raises(AgentToolError) as caught:
        run(command(starter, "--interactive"), input_is_tty=lambda: True,
            input_fn=lambda _: next(answers), output_fn=print)
    assert caught.value.code == "WECHAT_CONNECTION_ERROR"
    error = build_error_payload(caught.value)
    assert error["details"]["completed_steps"][0]["readback"] == {
        "login_saved": stage == "binding", "binding_saved": False}
    assert _print_error(caught.value) == 2
    public = capsys.readouterr().out + json.dumps(error) + "".join(traceback.format_exception(caught.value))
    assert all(value not in public for value in PRIVATE_VALUES)
    assert calls == ["qr", "login", "binding"][:calls.index(stage) + 1]
    assert starter.read_bytes() == before
    assert not WechatClawbotStateStore(state_dir).bindings_path.exists()
