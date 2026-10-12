"""Actual sender scope proofs use synthetic state, clients and durable temp receipts."""
import json
import os
from dataclasses import replace
from types import SimpleNamespace

import pytest

from src.application.channels.wechat_clawbot import notification as wechat
from src.application.channels.wechat_clawbot.state import (
    WechatClawbotState, resolve_wechat_clawbot_state_dir,
)
from src.application.daily_decision_brief_repository import (
    read_confirmed_attribution_render_context, read_daily_decision_brief_delivery_state,
)
from src.application.notification_delivery_route import notification_reminder_route_key
from test_attribution_reminders import _next_fixed_request, _patch_pending_tick


def _wechat_fixture(tmp_path, monkeypatch):
    flow, fixture = _patch_pending_tick(monkeypatch)
    config = fixture._config()
    state_dir = resolve_wechat_clawbot_state_dir(base=tmp_path, label="default")
    state_dir.mkdir(parents=True)
    path = state_dir / "bindings.json"

    def bind(recipient):
        path.write_text(json.dumps({"bindings": {"ops": {
            "to_user_id": recipient, "context_token": "fixture-context",
        }}}), encoding="utf-8")

    bind("fixture-A")
    monkeypatch.setattr(wechat, "load_wechat_clawbot_state", lambda **_: WechatClawbotState(
        label="default", state_dir=state_dir, base_url="https://example.invalid",
        bot_token="fixture-in-memory-auth",
    ))
    monkeypatch.setattr(flow, "finalize_multi_tick_run", lambda **kw: 1 if kw.get("notify_failures") else 0)
    return flow, fixture, config, bind


def _install_real_wechat_adapter(flow, monkeypatch, client_factory, before_send=None):
    def send(**kwargs):
        if before_send:
            before_send(kwargs)
        return wechat.send_wechat_clawbot_message(**{
            key: kwargs[key] for key in ("base", "channel", "target", "message", "notifications", "idempotency_key")
        }, client_factory=client_factory)

    monkeypatch.setattr(flow, "select_notification_delivery_adapter", lambda _: SimpleNamespace(
        send_fn=send, normalize_fn=wechat.normalize_wechat_clawbot_send_output,
        failure_stage="synthetic-real-wechat-send",
    ))


def _fixed_envelopes(base, fixture, account="lx"):
    return read_daily_decision_brief_delivery_state(base=base, account=account, market="US")["state"]["days"][fixture.MARKET_DATE]["fixed_reports"]


@pytest.mark.parametrize("aba", [False, True])
def test_real_wechat_binding_switch_persists_actual_send_not_pre_or_post_metadata(tmp_path, monkeypatch, aba):
    flow, fixture, config, bind = _wechat_fixture(tmp_path, monkeypatch)
    prepared_a = notification_reminder_route_key(config=config, base=tmp_path)
    calls = []
    actual_b = []

    class FakeClient:
        def __init__(self, **kwargs):
            assert kwargs["bot_token"] == "fixture-in-memory-auth"

        def send_text_message(self, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1 and aba:
                bind("fixture-A")
            return {"ret": 0, "message_id": f"fixture-message-{len(calls)}"}

    def before_send(kwargs):
        if not calls:
            bind("fixture-B")
            actual_b.append(notification_reminder_route_key(config=config, base=tmp_path))

    _install_real_wechat_adapter(flow, monkeypatch, FakeClient, before_send)
    assert flow.run_tick_notification_flow(fixture._request(tmp_path, run_id="prepared-A-sent-B", config=config).request) == 0
    envelope = _fixed_envelopes(tmp_path, fixture)[fixture.FIXED_TARGET]
    assert calls[0]["to_user_id"] == "fixture-B"
    assert envelope["render_context"]["attribution_route_key"] == prepared_a
    assert envelope["confirmed_attribution_route_key"] == actual_b[0] != prepared_a
    assert read_confirmed_attribution_render_context(base=tmp_path, account="lx", market="US") == {}
    bind("fixture-A")
    assert flow.run_tick_notification_flow(_next_fixed_request(
        fixture, tmp_path, run_id="A-never-saw-detail", config=config)) == 0
    assert calls[1]["to_user_id"] == "fixture-A"
    assert "TCOM0 2026" in calls[1]["text"]
    assert "1 笔未变化" not in calls[1]["text"]


def test_two_accounts_persist_their_own_actual_sender_context(tmp_path, monkeypatch):
    flow, fixture, config, bind = _wechat_fixture(tmp_path, monkeypatch)
    key_a = notification_reminder_route_key(config=config, base=tmp_path)
    calls = []
    keys = []

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def send_text_message(self, **kwargs):
            calls.append(kwargs)
            return {"ret": 0, "message_id": f"fixture-{len(calls)}"}

    def before_send(kwargs):
        bind("fixture-A" if not calls else "fixture-B")
        keys.append(notification_reminder_route_key(config=config, base=tmp_path))

    _install_real_wechat_adapter(flow, monkeypatch, FakeClient, before_send)
    assert flow.run_tick_notification_flow(fixture._request(
        tmp_path, run_id="two-account-context", config=config, accounts=("lx", "sy")).request) == 0
    assert [c["to_user_id"] for c in calls] == ["fixture-A", "fixture-B"]
    assert keys[0] == key_a != keys[1]
    for account, actual in zip(("lx", "sy"), keys):
        envelope = _fixed_envelopes(tmp_path, fixture, account)[fixture.FIXED_TARGET]
        assert envelope["confirmed_attribution_route_key"] == actual
        assert envelope["render_context"]["attribution_route_key"] == key_a
    assert read_confirmed_attribution_render_context(base=tmp_path, account="lx", market="US")
    assert read_confirmed_attribution_render_context(base=tmp_path, account="sy", market="US") == {}


@pytest.mark.parametrize("outcome", ["failed", "ambiguous", "confirm_write", "unknown_success"])
def test_unconfirmed_or_unproven_actual_context_never_advances_seen(tmp_path, monkeypatch, outcome):
    flow, fixture, config, _ = _wechat_fixture(tmp_path, monkeypatch)
    result = {"ok": outcome in {"confirm_write", "unknown_success"},
              "command_ok": outcome in {"confirm_write", "unknown_success"},
              "delivery_confirmed": outcome in {"confirm_write", "unknown_success"},
              "returncode": 0 if outcome in {"confirm_write", "unknown_success"} else 1,
              "message_id": "fixture-message", "ambiguous_send": outcome == "ambiguous",
              "error_code": "SEND_UNCONFIRMED" if outcome == "ambiguous" else "SEND_FAILED"}
    if outcome != "unknown_success":
        result["attribution_route_key"] = notification_reminder_route_key(config=config, base=tmp_path)
    fixture._patch_sender(monkeypatch, result=result)
    original_execute = flow.execute_per_account_delivery
    monkeypatch.setattr(flow, "execute_per_account_delivery", lambda **kw: original_execute(**kw, sleep_fn=lambda _: None))
    if outcome == "confirm_write":
        def fail_write(**kwargs):
            raise OSError("fixture receipt write failure")
        monkeypatch.setattr(flow, "confirm_daily_decision_brief_delivery_v2", fail_write)
    assert flow.run_tick_notification_flow(fixture._request(tmp_path, run_id=outcome, config=config).request) == (
        0 if outcome == "unknown_success" else 1)
    assert read_confirmed_attribution_render_context(base=tmp_path, account="lx", market="US") == {}
    envelope = _fixed_envelopes(tmp_path, fixture)[fixture.FIXED_TARGET]
    assert envelope["confirmed_attribution_route_key"] is None
    assert envelope["status"] == ({"failed": "pending", "ambiguous": "ambiguous",
                                    "confirm_write": "ambiguous", "unknown_success": "confirmed"}[outcome])


@pytest.mark.parametrize("final_key", [None, "b" * 64])
def test_retry_uses_only_final_success_context_never_previous_failed_known_context(tmp_path, final_key):
    from src.application.scheduled_notification import send_account_message_with_retry

    outcomes = iter([
        {"ok": False, "command_ok": False, "delivery_confirmed": False, "returncode": 1,
         "error_code": "SEND_FAILED", "attribution_route_key": "a" * 64},
        {"ok": True, "command_ok": True, "delivery_confirmed": True, "returncode": 0,
         "message_id": "fixture-success", "attribution_route_key": final_key},
    ])
    result = send_account_message_with_retry(
        base=tmp_path, channel="wechat_clawbot", target="wechat:ops", account="lx",
        message="fixture", run_id="final-context-only", runlog=SimpleNamespace(safe_event=lambda *a, **k: None),
        audit_fn=lambda *a, **k: None, send_fn=lambda **_: next(outcomes),
        normalize_fn=lambda *, send_result: send_result, safe_data_fn=lambda x: x,
        failure_fields_builder=lambda **_: {}, sleep_fn=lambda _: None, max_attempts=2,
    )
    assert result["ok"] and result["attempts"] == 2
    assert result["attribution_route_key"] == final_key
    assert result["attempt_records"][0]["attribution_route_key"] == "a" * 64
    assert result["attempt_records"][1]["attribution_route_key"] == final_key


def test_real_wechat_exact_retry_preserves_frozen_payload_and_records_current_actual_context(tmp_path, monkeypatch):
    flow, fixture, config, bind = _wechat_fixture(tmp_path, monkeypatch)
    calls = []
    response = {"ret": -9}

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def send_text_message(self, **kwargs):
            calls.append(kwargs)
            return response

    _install_real_wechat_adapter(flow, monkeypatch, FakeClient)
    original_execute = flow.execute_per_account_delivery
    monkeypatch.setattr(flow, "execute_per_account_delivery", lambda **kw: original_execute(**kw, sleep_fn=lambda _: None))
    assert flow.run_tick_notification_flow(fixture._request(tmp_path, run_id="failed-A", config=config).request) == 1
    pending = _fixed_envelopes(tmp_path, fixture)[fixture.FIXED_TARGET]
    bind("fixture-B")
    actual_b = notification_reminder_route_key(config=config, base=tmp_path)
    response.update(ret=0, message_id="fixture-retry-B")
    assert flow.run_tick_notification_flow(fixture._request(
        tmp_path, run_id="retry-B", config=config, delivery_only=True).request) == 0
    confirmed = _fixed_envelopes(tmp_path, fixture)[fixture.FIXED_TARGET]
    assert calls[-1]["to_user_id"] == "fixture-B"
    assert all(call["text"] == pending["rendered_message"] for call in calls)
    assert len({call["client_id"] for call in calls}) == 1
    for field in ("delivery_key", "source_digest", "message_sha256", "rendered_message", "render_context"):
        assert confirmed[field] == pending[field]
    assert confirmed["confirmed_attribution_route_key"] == actual_b
    assert read_confirmed_attribution_render_context(base=tmp_path, account="lx", market="US") == {}


def test_wechat_process_normalization_and_fallback_keep_immutable_configured_scope(tmp_path, monkeypatch):
    _, _, config, bind = _wechat_fixture(tmp_path, monkeypatch)
    initial = notification_reminder_route_key(config=config, base=tmp_path)
    calls = []

    class FakeClient:
        def __init__(self, **kwargs):
            assert kwargs["bot_token"] == "fixture-in-memory-auth"

        def send_text_message(self, **kwargs):
            calls.append(kwargs)
            bind("fixture-B")
            return {"ret": -2} if len(calls) == 1 else {"ret": 0, "message_id": "fixture-fallback"}

    real_send = wechat.send_wechat_clawbot_message
    monkeypatch.setattr(wechat, "send_wechat_clawbot_message", lambda **kw: real_send(**kw, client_factory=FakeClient))
    process = wechat.send_wechat_clawbot_message_process(
        base=tmp_path, channel="wechat_clawbot", target="wechat:ops", message="fixture",
        notifications=config["notifications"], idempotency_key="fixture-key",
    )
    normalized = wechat.normalize_wechat_clawbot_send_output(send_result=process.raw)
    assert normalized["delivery_confirmed"]
    assert process.raw["attribution_route_key"] == normalized["attribution_route_key"] == initial
    from test_multi_tick_contract_batch2 import _FakeRunLogger, _notify_retry
    facade = _notify_retry(runlog=_FakeRunLogger(), audit_fn=lambda *a, **k: None,
                          send_fn=lambda **_: process, normalize_fn=wechat.normalize_wechat_clawbot_send_output,
                          sleep_fn=lambda _: None, base=tmp_path)
    assert facade["ok"] and facade["attribution_route_key"] == initial
    assert [c["to_user_id"] for c in calls] == ["fixture-A", "fixture-A"]
    assert [c["context_token"] for c in calls] == ["fixture-context", ""]
    assert initial != notification_reminder_route_key(config=config, base=tmp_path)
    assert "fixture-in-memory-auth" not in json.dumps(process.raw)


def test_real_wechat_provider_rejection_keeps_actual_scope_but_is_not_confirmed(tmp_path, monkeypatch):
    _, _, config, _ = _wechat_fixture(tmp_path, monkeypatch)

    class RejectClient:
        def __init__(self, **kwargs):
            pass

        def send_text_message(self, **kwargs):
            return {"ret": -9, "errmsg": "fixture rejection"}

    raw = wechat.send_wechat_clawbot_message(
        base=tmp_path, channel="wechat_clawbot", target="wechat:ops", message="fixture",
        notifications=config["notifications"], idempotency_key="fixture-key", client_factory=RejectClient,
    )
    normalized = wechat.normalize_wechat_clawbot_send_output(send_result=raw)
    assert raw["ok"] is False
    assert normalized["delivery_confirmed"] is False
    assert normalized["ok"] is False
    assert normalized["attribution_route_key"] == raw["attribution_route_key"]


@pytest.mark.parametrize("render", ["post", "card", "fallback"])
def test_real_feishu_sender_and_process_use_single_frozen_bot_identity_even_during_fallback(tmp_path, monkeypatch, render):
    from domain.domain.multi_tick import notification_route_identity_hash
    from src.application import notification_delivery_adapter as adapter
    from src.infrastructure.feishu_bitable import FeishuPermanentError
    from test_feishu_notification_sender import _fallback_envelope

    identity = {"app_id": "fixture-app-A", "user_open_id": "fixture-user-A"}
    resolve_calls = []
    transport_calls = []

    def resolve_bot(*args, **kwargs):
        resolve_calls.append(dict(identity))
        return SimpleNamespace(**identity, app_secret="fixture-in-memory-secret", credential_missing_fields=[])

    def transport(kind, kwargs):
        transport_calls.append((kind, kwargs))
        identity.update(app_id="fixture-app-B", user_open_id="fixture-user-B")
        if render == "fallback" and kind == "card":
            raise FeishuPermanentError("fixture safe card rejection", code=230001,
                                       response={"http_status": 400, "feishu_code": 230001})
        return {"code": 0, "data": {"message_id": f"fixture-{kind}"}}

    monkeypatch.setattr(adapter, "resolve_feishu_bot_config", resolve_bot)
    monkeypatch.setattr(adapter, "send_message", lambda **kwargs: transport("card", kwargs))
    monkeypatch.setattr(adapter, "send_post_message", lambda **kwargs: transport("post", kwargs))
    message, card = _fallback_envelope()
    process = adapter.send_feishu_app_message_process(
        base=tmp_path, channel="feishu_app", target="unused-authoring-hint", message=message,
        idempotency_key="fixture-idem", transport_envelope=None if render == "post" else card,
    )
    normalized = adapter.normalize_feishu_app_send_output(send_result=process.raw)
    expected = notification_route_identity_hash(provider="feishu_app", channel="feishu_app",
                                              sender="fixture-app-A", recipient="fixture-user-A")
    assert len(resolve_calls) == 1
    assert normalized["delivery_confirmed"]
    assert process.raw["attribution_route_key"] == normalized["attribution_route_key"] == expected
    from test_multi_tick_contract_batch2 import _FakeRunLogger, _notify_retry
    facade = _notify_retry(runlog=_FakeRunLogger(), audit_fn=lambda *a, **k: None,
                          send_fn=lambda **_: process, normalize_fn=adapter.normalize_feishu_app_send_output,
                          sleep_fn=lambda _: None, base=tmp_path, channel="feishu_app")
    assert facade["ok"] and facade["attribution_route_key"] == expected
    assert all(kwargs["app_id"] == "fixture-app-A" and kwargs["open_id"] == "fixture-user-A"
               and kwargs["app_secret"] == "fixture-in-memory-secret" for _, kwargs in transport_calls)
    assert [kind for kind, _ in transport_calls] == (["card", "post"] if render == "fallback" else [render])
    if render == "fallback":
        assert transport_calls[1][1]["uuid"] == "fixture-idem:fallback"
        assert normalized["fallback_used"] is True
    assert "fixture-in-memory-secret" not in json.dumps(process.raw)
    assert "fixture-app-B" not in json.dumps(process.raw)


def test_same_label_directory_auth_replacement_is_only_configured_sender_scope(tmp_path, monkeypatch):
    _, _, config, _ = _wechat_fixture(tmp_path, monkeypatch)
    state_dir = resolve_wechat_clawbot_state_dir(base=tmp_path, label="default")
    principal = {"auth": "fixture-auth-A"}
    used = []
    monkeypatch.setattr(wechat, "load_wechat_clawbot_state", lambda **_: WechatClawbotState(
        label="default", state_dir=state_dir, base_url="https://example.invalid", bot_token=principal["auth"]))

    class FakeClient:
        def __init__(self, **kwargs):
            used.append(kwargs["bot_token"])

        def send_text_message(self, **kwargs):
            return {"ret": 0, "message_id": "fixture-configured-scope"}

    keys = []
    for auth in ("fixture-auth-A", "fixture-auth-B"):
        principal["auth"] = auth
        raw = wechat.send_wechat_clawbot_message(
            base=tmp_path, channel="wechat_clawbot", target="wechat:ops", message="fixture",
            notifications=config["notifications"], client_factory=FakeClient,
        )
        keys.append(raw["attribution_route_key"])
        assert auth not in json.dumps(raw)
    assert used == ["fixture-auth-A", "fixture-auth-B"]
    assert keys[0] == keys[1] == notification_reminder_route_key(config=config, base=tmp_path)
    # Fake token labels prove only our synthetic client inputs, not a provider's physical account identity.


@pytest.mark.parametrize("proof, confirmed", [
    ({}, False),
    ({"ok": False, "response_json": {"ret": 0}}, False),
    ({"ok": None, "response_json": {"ret": 0}}, False),
    ({"response_json": {}}, True),
])
def test_wechat_normalizer_distinguishes_absent_body_explicit_failure_and_real_empty_body(proof, confirmed):
    normalized = wechat.normalize_wechat_clawbot_send_output(send_result={
        "http_status": 200, "local_receipt_id": "fixture-local-receipt",
        "attribution_route_key": "a" * 64, **proof,
    })
    assert normalized["delivery_confirmed"] is confirmed
    assert normalized["ok"] is confirmed
    assert normalized["attribution_route_key"] == "a" * 64


# Real StateStore reads, synthetic authentication, and fake transport only.
def _directory_fixture(tmp_path, monkeypatch):
    flow, fixture = _patch_pending_tick(monkeypatch)
    config = fixture._config()
    directories = []
    for marker in ("A", "B"):
        directory = tmp_path / ("source-" + marker) / "sender"
        directory.mkdir(parents=True)
        (directory / "bindings.json").write_text(json.dumps({"bindings": {"ops": {
            "to_user_id": "fixture-same-recipient", "context_token": "fixture-context",
        }}}))
        (directory / "state.json").write_text(json.dumps({
            "bot_token": "synthetic-" + marker, "base_url": "https://example.invalid",
        }))
        directories.append(directory)
    monkeypatch.setattr(flow, "finalize_multi_tick_run", lambda **kw: 1 if kw.get("notify_failures") else 0)
    return flow, fixture, config, directories


def _directory_client(calls):
    class FakeClient:
        def __init__(self, **kwargs):
            self.source = kwargs["bot_token"]

        def send_text_message(self, **kwargs):
            calls.append({"auth": self.source, "detail": "TCOM0 2026" in kwargs["text"],
                          "summary": "未变化" in kwargs["text"]})
            return {"ret": 0, "message_id": f"fixture-directory-{len(calls)}"}
    return FakeClient


@pytest.mark.parametrize("switch_after", ["state.json", "bindings.json"])
def test_pinned_directory_never_confirms_late_alias_or_suppresses_B_first_details(tmp_path, monkeypatch, switch_after):
    from src.application.channels.wechat_clawbot.state_store import WechatClawbotStateStore
    flow, fixture, config, (directory_a, directory_b) = _directory_fixture(tmp_path, monkeypatch)
    alias = tmp_path / "location"
    alias.symlink_to(directory_b.parent, target_is_directory=True)
    config["notifications"]["wechat_clawbot_state_dir"] = str(alias / "sender")
    expected_b = notification_reminder_route_key(config=config, base=tmp_path)
    calls, source_reads, descriptors = [], [], []
    original = WechatClawbotStateStore._read_json_object

    def read(store, path, **kwargs):
        value = original(store, path, **kwargs)
        if path.name == "state.json":
            source_reads.append(value["bot_token"])
            descriptors.append(store._read_directory_fd)
        if path.name == switch_after and store.state_dir == directory_a and not calls:
            alias.unlink()
            alias.symlink_to(directory_b.parent, target_is_directory=True)
        return value

    monkeypatch.setattr(WechatClawbotStateStore, "_read_json_object", read)
    def before_send(_):
        if not calls:
            alias.unlink()
            alias.symlink_to(directory_a.parent, target_is_directory=True)

    _install_real_wechat_adapter(flow, monkeypatch, _directory_client(calls), before_send)
    assert flow.run_tick_notification_flow(fixture._request(tmp_path, run_id="prepare-B-auth-A", config=config).request) == 0
    envelope = _fixed_envelopes(tmp_path, fixture)[fixture.FIXED_TARGET]
    assert source_reads == ["synthetic-A"]
    assert envelope["render_context"]["attribution_route_key"] == expected_b
    with WechatClawbotStateStore(directory_a).read_directory() as store:
        from domain.domain.multi_tick import notification_route_identity_hash
        key_a = notification_route_identity_hash(provider="wechat_clawbot", channel="wechat_clawbot",
            sender=["default", *store.sender_directory_scope()],
            recipient=["fixture-same-recipient", None, None])
    assert envelope["confirmed_attribution_route_key"] == key_a != expected_b
    assert not read_confirmed_attribution_render_context(base=tmp_path, account="lx", market="US")
    assert flow.run_tick_notification_flow(_next_fixed_request(fixture, tmp_path, run_id="B-first", config=config)) == 0
    assert calls == [{"auth": "synthetic-A", "detail": True, "summary": False},
                     {"auth": "synthetic-B", "detail": True, "summary": False}]
    for descriptor in descriptors:
        with pytest.raises(OSError):
            os.fstat(descriptor)


@pytest.mark.parametrize("restore", [False, True])
def test_directory_replacement_keeps_same_auth_snapshot_or_unknown_proof(tmp_path, monkeypatch, restore):
    from src.application.channels.wechat_clawbot.state_store import WechatClawbotStateStore
    flow, fixture, config, (directory_a, directory_b) = _directory_fixture(tmp_path, monkeypatch)
    config["notifications"]["wechat_clawbot_state_dir"] = str(directory_a)
    prepared_a = notification_reminder_route_key(config=config, base=tmp_path)
    calls, source_reads = [], []
    original = WechatClawbotStateStore._read_json_object
    old = directory_a.parent / "retired"
    moved = False

    def read(store, path, **kwargs):
        nonlocal moved
        value = original(store, path, **kwargs)
        if path.name == "bindings.json" and store.state_dir == directory_a and not moved:
            directory_a.rename(old)
            directory_b.rename(directory_a)
            moved = True
        if path.name == "state.json":
            source_reads.append(value["bot_token"])
            if restore and old.exists():
                directory_a.rename(directory_b)
                old.rename(directory_a)
        return value

    # Prepare metadata must remain stable; switch starts only inside actual send.
    def before_send(_):
        monkeypatch.setattr(WechatClawbotStateStore, "_read_json_object", read)

    _install_real_wechat_adapter(flow, monkeypatch, _directory_client(calls), before_send)
    assert flow.run_tick_notification_flow(fixture._request(tmp_path, run_id="directory-swap", config=config).request) == 0
    assert source_reads == ["synthetic-A"]
    envelope = _fixed_envelopes(tmp_path, fixture)[fixture.FIXED_TARGET]
    assert envelope["confirmed_attribution_route_key"] == (prepared_a if restore else None)
    assert bool(read_confirmed_attribution_render_context(base=tmp_path, account="lx", market="US")) is restore
    if restore:
        directory_a.rename(old)
        directory_b.rename(directory_a)
    replacement_key = notification_reminder_route_key(config=config, base=tmp_path)
    assert replacement_key and replacement_key != prepared_a
    assert flow.run_tick_notification_flow(_next_fixed_request(fixture, tmp_path, run_id="replacement-first", config=config)) == 0
    assert calls[1] == {"auth": "synthetic-B", "detail": True, "summary": False}


@pytest.mark.parametrize("file_name", ["state.json", "bindings.json"])
@pytest.mark.parametrize("kind", ["symlink", "directory", "fifo", "malformed", "nonobject", "fdopen"])
def test_unsafe_snapshot_child_rejects_without_send_and_closes_directory(tmp_path, monkeypatch, file_name, kind):
    from src.application.channels.wechat_clawbot.state_store import WechatClawbotStateStore
    _, _, config, (directory_a, directory_b) = _directory_fixture(tmp_path, monkeypatch)
    config["notifications"]["wechat_clawbot_state_dir"] = str(directory_a)
    path = directory_a / file_name
    path.unlink()
    if kind == "symlink":
        path.symlink_to(directory_b / file_name)
    elif kind == "directory":
        path.mkdir()
    elif kind == "fifo":
        os.mkfifo(path)
    elif kind == "malformed":
        path.write_text("{")
    elif kind == "nonobject":
        path.write_text("[]")
    else:
        path.write_text((directory_b / file_name).read_text())
        def fail_fdopen(*args, **kwargs):
            raise OSError("fixture fdopen failure")
        monkeypatch.setattr(os, "fdopen", fail_fdopen)
    descriptors = []
    original_open = os.open

    def open_file(*args, **kwargs):
        descriptor = original_open(*args, **kwargs)
        descriptors.append(descriptor)
        return descriptor

    monkeypatch.setattr(os, "open", open_file)
    calls = []
    with pytest.raises(ValueError):
        wechat.send_wechat_clawbot_message(base=tmp_path, channel="wechat_clawbot", target="wechat:ops",
            message="fixture", notifications=config["notifications"], client_factory=_directory_client(calls))
    assert not calls
    if file_name == "bindings.json":
        assert notification_reminder_route_key(config=config, base=tmp_path) is None
    for descriptor in descriptors:
        with pytest.raises(OSError):
            os.fstat(descriptor)


def test_prepare_reads_only_binding_metadata_from_pinned_directory(tmp_path, monkeypatch):
    from src.application.channels.wechat_clawbot.state_store import WechatClawbotStateStore
    _, _, config, (directory_a, _) = _directory_fixture(tmp_path, monkeypatch)
    config["notifications"]["wechat_clawbot_state_dir"] = str(directory_a)
    original = WechatClawbotStateStore._read_json_object
    reads = []

    def read(store, path, **kwargs):
        reads.append(path.name)
        assert path.name == "bindings.json"
        assert store._read_directory_fd is not None
        return original(store, path, **kwargs)

    monkeypatch.setattr(WechatClawbotStateStore, "_read_json_object", read)
    assert notification_reminder_route_key(config=config, base=tmp_path)
    assert reads == ["bindings.json"]
