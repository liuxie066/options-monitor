from __future__ import annotations

import io
import json
import logging
from pathlib import Path
from types import SimpleNamespace
import urllib.error
import urllib.request

import pytest

from src.application.channels.wechat_clawbot import inbound, ilink_client
from src.application.channels.wechat_clawbot.state_store import WechatClawbotStateStore
from src.interfaces.cli.channel_ops import handle_channel_command


NON_TEXT = {"message_id": "fixture-message", "from_user_id": "fixture-sender", "item_list": []}
FAILURE_LOG = "failed to process WeChat ClawBot inbound batch"


class PollCancelled(BaseException):
    pass


def _case(tmp_path: Path, monkeypatch, responses, *, blocked_requests=()):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    store = WechatClawbotStateStore(state_dir)
    store.save_state({"bot_token": "fixture-token", "base_url": "https://fixture.invalid",
                      "get_updates_buf": "cursor-original", "marker": tmp_path.name})
    before = store.state_path.read_bytes()
    requests, order, paths, logs = [], [], [], []
    response_iter = iter(responses)

    def urlopen(request, *, timeout):
        assert request.full_url == "https://fixture.invalid/ilink/bot/getupdates"
        assert request.get_method() == "POST" and timeout == 1
        requests.append(json.loads(request.data))
        order.append("http")
        response = next(response_iter)
        if response == "http-503":
            raise urllib.error.HTTPError(request.full_url, 503, "fixture unavailable", {}, io.BytesIO(b"fixture HTTP error"))
        if isinstance(response, BaseException):
            raise response
        return io.BytesIO(response if isinstance(response, bytes) else json.dumps(response).encode())

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    original_retry = inbound._retry_pending_wechat_reply

    def retry(**kwargs):
        order.append("retry")
        result = original_retry(**kwargs)
        assert result == {"attempted": False, "ok": True, "reason": "reply_disabled"}
        return result

    monkeypatch.setattr(inbound, "_retry_pending_wechat_reply", retry)

    def observe(name, original):
        def forward(*args, **kwargs):
            paths.append((len(requests), name))
            assert len(requests) not in blocked_requests, f"failed poll entered {name}"
            return original(*args, **kwargs)
        return forward

    for name in ("extract_first_string", "extract_messages", "_resolve_binding_refresh_route",
                 "_maybe_start_typing", "_maybe_stop_typing", "_maybe_reply",
                 "_refresh_bound_context_from_inbound", "_refresh_bound_context_from_reply",
                 "_maybe_keepalive_bound_context"):
        monkeypatch.setattr(inbound, name, observe(name, getattr(inbound, name)))
    monkeypatch.setattr(inbound.ChannelService, "handle_inbound", observe("service", inbound.ChannelService.handle_inbound))

    class MemoryLog(logging.Handler):
        def emit(self, record):
            logs.append(record)

    monkeypatch.setattr(inbound.LOG, "handlers", [MemoryLog()])
    monkeypatch.setattr(inbound.LOG, "level", logging.DEBUG)
    monkeypatch.setattr(inbound.LOG, "propagate", False)
    kwargs = dict(base=tmp_path, label="fixture", state_dir=str(state_dir),
                  config_path=str(tmp_path / "absent-runtime.json"), bot_config_path=str(tmp_path / "absent-bot.json"),
                  audit_db=str(tmp_path / "absent-audit.sqlite3"), allowed_senders="wechat:fixture-sender",
                  reply_enabled=False, keepalive_interval_sec=0.0, timeout_sec=1)
    return SimpleNamespace(store=store, before=before, requests=requests, order=order, paths=paths,
                           logs=logs, kwargs=kwargs, state_dir=state_dir)


def _assert_no_auxiliary_state(case):
    assert not case.store.bindings_path.exists()
    assert not case.store.outbound_receipts_path.exists()
    assert not case.store.pending_login_path.exists()
    assert not Path(case.kwargs["audit_db"]).exists()
    assert not Path(case.kwargs["config_path"]).exists()
    assert not Path(case.kwargs["bot_config_path"]).exists()


@pytest.mark.parametrize("status,code", [({"ret": -2}, -2), ({"errcode": "-2"}, -2),
                                         ({"code": 5}, 5), ({"ret": -2, "code": 0}, -2)])
@pytest.mark.parametrize("with_message", [False, True])
@pytest.mark.parametrize("with_cursor", [False, True])
def test_poll_explicit_failure_preserves_state_before_any_consumption(tmp_path, monkeypatch, status, code, with_message, with_cursor):
    response = {**status, "errmsg": "PRIVATE_PROVIDER_TEXT", "msgs": []}
    if with_message:
        response["msgs"] = [{"message_id": "private-message", "from_user_id": "fixture-sender",
                             "context_token": "PRIVATE_CONTEXT", "text": "PRIVATE_MESSAGE_TEXT"}]
    if with_cursor:
        response["get_updates_buf"] = "PRIVATE_FAILED_CURSOR"
    case = _case(tmp_path, monkeypatch, [response], blocked_requests={1})
    result = inbound.poll_wechat_clawbot_once(**case.kwargs)
    assert result["ok"] is False
    assert result["error"] == {"code": "POLL_FAILED", "message": "WeChat ClawBot getupdates failed"}
    assert result["meta"] == {"provider_response_code": code}
    assert result["data"] == {
        "label": "fixture", "update_count": 0, "processed_count": 0, "reply_count": 0,
        "cursor_present": True, "cursor_updated": False, "results": [],
        "keepalive": {"attempted": False, "reason": "poll_failed"},
        "outbox_retry": {"attempted": False, "ok": True, "reason": "reply_disabled"},
    }
    assert "PRIVATE_" not in json.dumps(result) and "fixture-token" not in json.dumps(result)
    assert case.store.state_path.read_bytes() == case.before
    assert WechatClawbotStateStore(case.state_dir).load_state()["get_updates_buf"] == "cursor-original"
    assert case.paths == [] and case.order == ["retry", "http"] and case.logs == []
    _assert_no_auxiliary_state(case)


@pytest.mark.parametrize("response,count,cursor,code", [
    ({"ret": 0, "msgs": [NON_TEXT], "get_updates_buf": "next"}, 1, "next", 0),
    ({"errcode": 0, "msgs": [], "get_updates_buf": "next"}, 0, "next", 0),
    ({"code": 0, "msgs": []}, 0, "cursor-original", 0),
    ({"ret": "-0", "msgs": []}, 0, "cursor-original", 0),
    ({}, 0, "cursor-original", None),
    ({"ok": True, "ret": -2, "msgs": [NON_TEXT], "get_updates_buf": "next"}, 1, "next", -2),
    ({"data": {"message_list": [NON_TEXT], "get_updates_buf": "next"}}, 1, "next", None),
    ({"ret": 0, "errcode": -2, "msgs": []}, 0, "cursor-original", 0),
    ({"ok": False}, 0, "cursor-original", None),
    ({"ret": "unparsed"}, 0, "cursor-original", None),
    ({"ret": "+2"}, 0, "cursor-original", None),
    ({"data": {"ret": -2, "message_list": [NON_TEXT], "get_updates_buf": "next"}}, 1, "next", None),
])
def test_poll_preserves_existing_response_compatibility(tmp_path, monkeypatch, response, count, cursor, code):
    case = _case(tmp_path, monkeypatch, [response])
    result = inbound.poll_wechat_clawbot_once(**case.kwargs)
    assert result["ok"] is True and result["error"] is None
    assert result["meta"]["provider_response_code"] == code
    assert result["data"]["update_count"] == count
    assert result["data"]["processed_count"] == result["data"]["reply_count"] == 0
    assert len(result["data"]["results"]) == count
    assert sum(name == "service" for _, name in case.paths) == count
    assert all(item["kind"] == "ignored_message" and item["ok"] for item in result["data"]["results"])
    assert WechatClawbotStateStore(case.state_dir).load_state()["get_updates_buf"] == cursor
    if cursor == "cursor-original":
        assert case.store.state_path.read_bytes() == case.before
    assert case.order == ["retry", "http"] and not case.logs
    _assert_no_auxiliary_state(case)


@pytest.mark.parametrize("recovers", [False, True])
def test_serve_failure_is_visible_and_preserves_next_request_cursor(tmp_path, monkeypatch, recovers):
    failure = {"ret": -2, "msgs": [NON_TEXT], "get_updates_buf": "PRIVATE_FAILED_CURSOR", "errmsg": "PRIVATE_PROVIDER_TEXT"}
    second = {"ret": 0, "msgs": [NON_TEXT], "get_updates_buf": "next"} if recovers else failure
    case = _case(tmp_path, monkeypatch, [failure, second], blocked_requests={1} if recovers else {1, 2})
    settings = inbound.WechatClawbotServeSettings(**case.kwargs, poll_interval_sec=0.25)
    sleeps = []
    inbound.serve_wechat_clawbot(settings, sleep_fn=sleeps.append, lock_path=None, stop_after_batches=2)
    assert sleeps == [0.25]
    assert [request["get_updates_buf"] for request in case.requests] == ["cursor-original", "cursor-original"]
    assert case.order == ["retry", "http", "retry", "http"]
    assert len(case.logs) == (1 if recovers else 2)
    assert all(record.levelname == "ERROR" and record.getMessage() == FAILURE_LOG and record.exc_info is None for record in case.logs)
    assert WechatClawbotStateStore(case.state_dir).load_state()["get_updates_buf"] == ("next" if recovers else "cursor-original")
    assert all(index == 2 for index, _ in case.paths) if recovers else case.paths == []
    if not recovers:
        assert case.store.state_path.read_bytes() == case.before
    _assert_no_auxiliary_state(case)


@pytest.mark.parametrize("serve", [False, True])
def test_poll_cancellation_propagates_without_state_change(tmp_path, monkeypatch, serve):
    case = _case(tmp_path, monkeypatch, [PollCancelled("fixture cancellation")], blocked_requests={1})
    with pytest.raises(PollCancelled, match="fixture cancellation"):
        if serve:
            settings = inbound.WechatClawbotServeSettings(**case.kwargs)
            inbound.serve_wechat_clawbot(settings, lock_path=None, stop_after_batches=1)
        else:
            inbound.poll_wechat_clawbot_once(**case.kwargs)
    assert case.store.state_path.read_bytes() == case.before
    assert not case.logs and not case.paths and case.order == ["retry", "http"]
    _assert_no_auxiliary_state(case)


@pytest.mark.parametrize("serve", [False, True])
@pytest.mark.parametrize("response,expected", [("http-503", "HTTP error 503"), (b"not-json", "invalid JSON"), (b"[]", "JSON response must be an object")])
def test_poll_transport_errors_retain_existing_exception_path(tmp_path, monkeypatch, serve, response, expected):
    case = _case(tmp_path, monkeypatch, [response], blocked_requests={1})
    if serve:
        settings = inbound.WechatClawbotServeSettings(**case.kwargs)
        inbound.serve_wechat_clawbot(settings, lock_path=None, stop_after_batches=1)
        assert len(case.logs) == 1 and case.logs[0].getMessage() == FAILURE_LOG
        assert case.logs[0].exc_info[0] is ilink_client.WechatClawbotError
        assert expected in str(case.logs[0].exc_info[1])
    else:
        with pytest.raises(ilink_client.WechatClawbotError, match=expected):
            inbound.poll_wechat_clawbot_once(**case.kwargs)
    assert case.store.state_path.read_bytes() == case.before
    assert case.paths == [] and case.order == ["retry", "http"]
    _assert_no_auxiliary_state(case)


@pytest.mark.parametrize("failed", [False, True])
def test_cli_dispatch_preserves_real_poll_result(tmp_path, monkeypatch, failed):
    case = _case(tmp_path, monkeypatch, [{"ret": -2 if failed else 0, "msgs": []}], blocked_requests={1} if failed else ())
    args = SimpleNamespace(channel_command="wechat-clawbot", wechat_clawbot_command="poll-once", label="fixture",
                           state_dir=str(case.state_dir), config_key=None, config_path=case.kwargs["config_path"],
                           bot_config=case.kwargs["bot_config_path"], audit_db=case.kwargs["audit_db"],
                           allowed_senders=case.kwargs["allowed_senders"], no_reply=True, max_reply_chars=100, timeout_sec=1)
    result = handle_channel_command(args, repo_base_fn=lambda: tmp_path)
    assert result["ok"] is (not failed)
    assert (result["error"] or {}).get("code") == ("POLL_FAILED" if failed else None)
    assert result["tool_name"] == "wechat_clawbot.poll_once"
    assert case.store.state_path.read_bytes() == case.before
    assert case.order == ["retry", "http"]
    _assert_no_auxiliary_state(case)
