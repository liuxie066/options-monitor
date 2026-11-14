"""Independent source-bound QA; all writes belong to pytest temporary directories.

OM_QA_CHANNEL_TARGET overrides installed package discovery for isolated QA.
Otherwise use installed distributions, as in CI. Never load plugin source trees.
"""
from __future__ import annotations

from src.application.channels import feishu_notification as feishu_transport

import argparse
from copy import deepcopy
import hashlib
import importlib.machinery
import importlib.metadata
import importlib.util
import itertools
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
from types import SimpleNamespace
import urllib.request

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_MODULES = ("om_feishu_channel", "om_wechat_channel")


def installed_package_roots():
    override = os.environ.get("OM_QA_CHANNEL_TARGET")
    roots = {}
    for name, module in (("om-feishu-channel-plugin", PACKAGE_MODULES[0]),
                         ("om-wechat-channel-plugin", PACKAGE_MODULES[1])):
        if override:
            target = Path(override).resolve()
            assert target.is_dir(), target
            dist = next(importlib.metadata.Distribution.discover(name=name, path=[str(target)]), None)
            assert dist is not None, f"{name} is not installed in OM_QA_CHANNEL_TARGET={target}"
            spec = importlib.machinery.PathFinder.find_spec(module, [str(target)])
        else:
            dist = importlib.metadata.distribution(name)
            spec = importlib.util.find_spec(module)
        root = Path(dist.locate_file("")).resolve()
        init = Path(module) / "__init__.py"
        assert str(init) in {str(path) for path in dist.files or ()}, f"{name} must be a real installed package"
        assert spec is not None and spec.origin, f"installed module not discoverable: {module}"
        assert Path(spec.origin).resolve() == (root / init).resolve(), f"{module} resolves outside installed distribution"
        assert not (root / module).resolve().is_relative_to(ROOT / "plugins"), "plugin source fallback is forbidden"
        roots[module] = str(root)
    return roots


def switches(master=False, receive=False, notify=False):
    return dict(enabled=master, receive_enabled=receive, notify_enabled=notify)


def forbidden(calls, name):
    def reject(*args, **kwargs):
        calls.append((name, args, kwargs))
        raise AssertionError(f"forbidden QA side effect: {name}")
    return reject


@pytest.fixture(autouse=True)
def offline(monkeypatch, tmp_path):
    calls = []
    for name in list(os.environ):
        if name.startswith(("OM_", "FEISHU_", "FUTU_", "WECHAT_")) and name != "OM_QA_CHANNEL_TARGET":
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OM_SECRET_BACKEND", "env")
    monkeypatch.setenv("OM_RUNTIME_ROOT", str(tmp_path / "runtime"))
    monkeypatch.setenv("OM_FEISHU_BOT_APP_ID", "qa-sender")
    monkeypatch.setenv("OM_FEISHU_BOT_APP_SECRET", "qa-synthetic-secret")
    monkeypatch.setenv("OM_FEISHU_BOT_USER_OPEN_ID", "ou_env_wrong")
    monkeypatch.setattr(socket.socket, "connect", forbidden(calls, "socket.connect"))
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden(calls, "socket.connect_ex"))
    monkeypatch.setattr(urllib.request, "urlopen", forbidden(calls, "urlopen"))
    yield calls
    assert calls == [], calls


def authoring(tmp_path, *, channel_controls=None):
    path = tmp_path / "config.yaml"
    doc = {
        "accounts": {a: {"type": "futu", "futu_account_id": str(i)}
                     for a, i in (("alpha", 12345678), ("beta", 87654321))},
        "markets": {"us": {"accounts": ["alpha"], "symbols": ["NVDA"]},
                    "hk": {"accounts": ["beta"], "symbols": ["0700.HK"]}},
        "bot": {"enabled": False}, "notifications": {"enabled": False},
    }
    if channel_controls is not None:
        doc["channels"] = deepcopy(channel_controls)
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return path, doc


def route_config(*targets, account="alpha"):
    return {"channels": {"feishu": switches(True, False, True)},
            "notifications": {"enabled": True, "routes": [
                {"id": t, "provider": "feishu_app", "target": "ou_" + t, "accounts": [account]}
                for t in targets]}}


def identity(send_result):
    return send_result


@pytest.mark.parametrize("channel", ["feishu", "wechat_clawbot"])
@pytest.mark.parametrize("flags", list(itertools.product((False, True), repeat=3)))
def test_root_controls_generate_identically_for_bot_and_both_markets(tmp_path, channel, flags):
    from src.application.config_yaml import resolve_yaml_bot_config, resolve_yaml_runtime_config
    controls = {channel: switches(*flags)}
    path, _ = authoring(tmp_path, channel_controls=controls)
    bot, _ = resolve_yaml_bot_config(repo_root=ROOT, config_path=path)
    assert bot["channels"] == controls
    for market in ("us", "hk"):
        runtime, _ = resolve_yaml_runtime_config(repo_root=ROOT, config_path=path, market=market)
        assert runtime["channels"] == controls


def test_route_generation_filters_market_and_account_and_preserves_explicit_empty(tmp_path):
    from src.application.config_yaml import resolve_yaml_runtime_config
    from src.application.notification_delivery_route import resolve_notification_delivery_routes
    path, doc = authoring(tmp_path, channel_controls={"feishu": switches(True, False, True)})
    doc["notifications"] = route_config("shared")["notifications"]
    doc["notifications"]["routes"][0]["accounts"] = ["alpha", "beta"]
    doc["notifications"]["routes"].append({"id": "beta", "provider": "feishu_app",
        "target": "ou_beta_private", "accounts": ["beta"]})
    path.write_text(yaml.safe_dump(doc))
    us, _ = resolve_yaml_runtime_config(repo_root=ROOT, config_path=path, market="us")
    assert us["notifications"]["routes"] == [{"id": "shared", "provider": "feishu_app",
        "target": "ou_shared", "accounts": ["alpha"]}]
    assert resolve_notification_delivery_routes(config=us, account="beta", base=tmp_path) == []
    assert [r["target"] for r in resolve_notification_delivery_routes(config=us, account="alpha", base=tmp_path)] == ["ou_shared"]
    doc["notifications"]["routes"] = []
    path.write_text(yaml.safe_dump(doc))
    for market in ("us", "hk"):
        cfg, _ = resolve_yaml_runtime_config(repo_root=ROOT, config_path=path, market=market)
        assert cfg["notifications"]["routes"] == []
        assert resolve_notification_delivery_routes(config=cfg, account="alpha", base=tmp_path) == []


def test_cli_public_parser_preview_apply_cas_and_readback(tmp_path, monkeypatch):
    from src.application.agent_tool_contracts import AgentToolError
    from src.interfaces.cli import main, feature_ops
    calls = []
    monkeypatch.setattr(feature_ops, "write_feature_env", forbidden(calls, "env write"))
    controls = {"feishu": switches(), "wechat_clawbot": switches(True, False, True)}
    path, _ = authoring(tmp_path, channel_controls=controls)
    args = main.parse_args(["channel", "configure", "--config-yaml", str(path),
        "--provider", "feishu_app", "--enabled", "true", "--receive-enabled", "true",
        "--notify-enabled", "false"])
    before = path.read_bytes()
    run = lambda: feature_ops.run_feature_configure(args, repo_base_fn=lambda: ROOT,
        input_is_tty=lambda: False, secret_runner=forbidden(calls, "secret provision"))
    preview = run()["result"]
    assert path.read_bytes() == before
    args.apply = args.confirm = True
    args.expected_source_sha256 = "stale"
    with pytest.raises(AgentToolError, match="STALE_PREVIEW"):
        run()
    assert path.read_bytes() == before
    args.expected_source_sha256 = preview["source_revision"]["before_sha256"]
    applied = run()
    expected = {**controls, "feishu": switches(True, True, False)}
    assert applied["status"] == "configured"
    assert yaml.safe_load(path.read_text())["channels"] == expected
    targets = applied["result"]["verified_targets"]
    assert any(str(p).endswith("config.bot.json") for p in targets)
    assert any(str(p).endswith("config.us.json") for p in targets)
    assert any(str(p).endswith("config.hk.json") for p in targets)
    for target in targets:
        if str(target).endswith(".json"):
            assert Path(target).is_relative_to(tmp_path)
            assert json.loads(Path(target).read_text())["channels"] == expected
    assert applied["result"]["restart_performed"] is False
    assert calls == []


@pytest.mark.parametrize("installed", [(), ("feishu",), ("wechat_clawbot",), ("feishu", "wechat_clawbot")])
@pytest.mark.parametrize("flags", [(False, True, True), (True, True, False), (True, False, True)])
def test_cli_status_separates_package_presence_controls_and_availability(tmp_path, monkeypatch, installed, flags):
    from src.application.channels import status
    from src.interfaces.cli.channel_ops import handle_channel_command
    monkeypatch.setattr(status, "channel_plugin_installed", lambda channel: channel in installed)
    monkeypatch.setattr(status, "resolve_secret_status", lambda *a, **kw: SimpleNamespace(configured=True))
    state = tmp_path / "wechat"
    state.mkdir()
    (state / "state.json").write_text(json.dumps({"bot_token": "qa-token-do-not-render"}))
    config = tmp_path / "config.bot.json"
    config.write_text(json.dumps({"channels": {c: switches(*flags) for c in ("feishu", "wechat_clawbot")},
        "inbound": {"wechat_clawbot": {"allowed_senders": "qa-user-do-not-render"}}}))
    profile = tmp_path / "service.profile.json"
    profile.write_text(json.dumps({"bot_config_path": str(config), "feishu_ws": {"enabled": True},
        "wechat_clawbot": {"enabled": True, "state_dir": str(state)}}))
    result = handle_channel_command(argparse.Namespace(channel_command="status", runtime_root=str(tmp_path),
        profile_path=str(profile), bot_config=str(config), include_service_status=False), repo_base_fn=lambda: tmp_path)
    assert result["ok"] is True
    for name in ("feishu", "wechat_clawbot"):
        entry = result["data"]["channels"][name]
        assert entry["installed"] is (name in installed)
        assert entry["master_enabled"] is flags[0]
        assert entry["receive_enabled"] is flags[1]
        assert entry["notify_enabled"] is flags[2]
        assert entry["available"] is (name in installed and flags[0] and (flags[1] or flags[2]))
        assert entry["reply_enabled"] is (name in installed and flags[0] and flags[1])
    assert "qa-token-do-not-render" not in json.dumps(result)
    assert "qa-user-do-not-render" not in json.dumps(result)


def test_missing_plugin_selected_facades_explicit_no_fallback(tmp_path, monkeypatch):
    from src.application.channels import plugins
    from src.application.agent_tool_contracts import AgentToolError
    from src.infrastructure.feishu_bot import send_text_message
    from src.application.channels.wechat_clawbot.ilink_client import WechatClawbotClient
    monkeypatch.setattr(plugins, "channel_plugin_installed", lambda channel: False)
    for call in (lambda: send_text_message(app_id="qa", app_secret="qa", open_id="qa", text="qa"),
                 lambda: WechatClawbotClient(bot_token="qa")):
        with pytest.raises(AgentToolError) as caught:
            call()
        assert caught.value.code == "CHANNEL_PLUGIN_NOT_INSTALLED"


@pytest.mark.parametrize("blocked_flag", ["enabled", "receive_enabled"])
@pytest.mark.parametrize("channel", ["feishu", "wechat_clawbot"])
def test_disabled_inbound_real_facades_do_not_enter_business_or_transport(tmp_path, monkeypatch, channel, blocked_flag):
    from src.application.channels import plugins
    from src.application.agent_tool_contracts import AgentToolError
    from src.application.inbound import feishu, feishu_ws
    from src.application.channels.wechat_clawbot import inbound as wechat
    controls = switches(True, True, True)
    controls[blocked_flag] = False
    path = tmp_path / "bot.json"
    path.write_text(json.dumps({"channels": {channel: controls}}))
    monkeypatch.setattr(plugins, "channel_plugin_installed", lambda _: True)
    calls = []
    reject = forbidden(calls, "disabled inbound")
    if channel == "feishu":
        assert feishu.handle_feishu_payload({}, bot_config_path=str(path), execute_tool_fn=reject)["ok"] is False
        settings = feishu_ws.FeishuWsSettings(bot_config_path=str(path))
        result = feishu_ws.handle_feishu_ws_event({}, settings=settings, reply_fn=reject,
            reaction_fn=reject, channel_service=SimpleNamespace(handle_inbound=reject))
        assert result["error"]["code"] == "CHANNEL_RECEIVE_DISABLED"
        with pytest.raises(AgentToolError):
            feishu_ws.serve_feishu_ws(settings, start_client_fn=reject)
    else:
        assert wechat.handle_wechat_clawbot_message({}, bot_config_path=str(path), execute_tool_fn=reject)["ok"] is False
        with pytest.raises(AgentToolError):
            wechat.poll_wechat_clawbot_once(base=tmp_path, bot_config_path=str(path), client_factory=reject,
                channel_service=SimpleNamespace(handle_inbound=reject))
    assert calls == []


@pytest.mark.parametrize("channel", ["feishu", "wechat"])
def test_pending_replies_pause_without_attempt_then_reply_in_original_channel(tmp_path, monkeypatch, channel):
    from src.application.bot.config_scope import resolve_replay_authorization
    from src.application.bot.control.contracts import BotInboundRequest
    from src.application.bot.host_store import BotHostStore
    from src.application.channels import plugins
    from src.application.channels.feishu_reply_renderer import render_feishu_conversation_reply
    from src.application.inbound import feishu_ws
    from src.application.channels.wechat_clawbot import inbound as wechat
    from src.application.config_yaml import build_yaml_runtime_config_file
    path, _ = authoring(tmp_path)
    config = tmp_path / "config.us.json"
    build_yaml_runtime_config_file(repo_root=ROOT, config_path=path, market="us", output_config_path=config)
    bot = tmp_path / "bot.json"
    selected = "feishu" if channel == "feishu" else "wechat_clawbot"
    monkeypatch.setattr(plugins, "channel_plugin_installed", lambda _: True)
    monkeypatch.setattr("src.application.bot.control.config_loader.default_bot_config_path", lambda **kw: bot)
    def write(master, receive):
        bot.write_text(json.dumps({"bot": {"enabled": True},
            "channels": {selected: switches(master, receive, False)}}))
    write(True, True)
    authorization = resolve_replay_authorization(BotInboundRequest(text="", channel=channel, sender_id="qa-sender",
        message_id="qa-original-message", config_path=str(config), bot_config_path=str(bot),
        reply_context={"reply_sender_identity": "qa-app"}))
    payload = (render_feishu_conversation_reply(message_id="qa-original-message", text="qa reply",
        reply_in_thread=False, max_chars=3500, render_route="bot") if channel == "feishu" else
        {"to_user_id": "qa-sender", "context_token": "qa-context", "group_id": None, "text": "qa reply"})
    store = BotHostStore(str(tmp_path / "replies.sqlite3"))
    store.enqueue_reply(delivery_key="qa-pending", channel=channel, payload=payload, authorization=authorization)
    sent = []
    def send(**kwargs):
        sent.append(deepcopy(kwargs))
        return {"code": 0, "ret": 0, "data": {"message_id": "qa-out"}}
    def retry():
        if channel == "feishu":
            return feishu_ws._retry_pending_feishu_reply(settings=feishu_ws.FeishuWsSettings(
                config_path=str(config), bot_config_path=str(bot), audit_db=str(store.path),
                allowed_senders="qa-sender", app_id="qa-app", app_secret="qa-synthetic"), reply_fn=send)
        return wechat._retry_pending_wechat_reply(audit_db=str(store.path), client=SimpleNamespace(send_text_message=send),
            config_path=str(config), bot_config_path=str(bot), transport_identity="qa-app",
            allowed_senders_fn=lambda: "qa-sender")
    before = store.list_replies()[0]
    for master, receive in ((False, True), (True, False)):
        write(master, receive)
        assert retry()["attempted"] is False
        after = store.list_replies()[0]
        assert after["attempt_count"] == before["attempt_count"] == 0
        assert after["status"] == before["status"]
        assert json.loads(after["payload_json"]) == payload
        assert sent == []
    write(True, True)
    # Editing Bot config rotates its authorization generation, even on restore.
    assert retry()["reason"] == "authorization_blocked"
    assert store.list_replies()[0]["attempt_count"] == 0
    assert sent == []
    authorization = resolve_replay_authorization(BotInboundRequest(text="", channel=channel, sender_id="qa-sender",
        message_id="qa-original-message", config_path=str(config), bot_config_path=str(bot),
        reply_context={"reply_sender_identity": "qa-app"}))
    store = BotHostStore(str(tmp_path / "fresh-replies.sqlite3"))
    store.enqueue_reply(delivery_key="qa-fresh-authorized", channel=channel, payload=payload, authorization=authorization)
    assert retry()["ok"] is True
    assert len(sent) == 1
    if channel == "feishu":
        assert sent[0]["message_id"] == "qa-original-message"
    else:
        assert sent[0]["to_user_id"] == "qa-sender"
        assert sent[0]["context_token"] == "qa-context"
    assert store.list_replies()[0]["status"] == "delivered"


@pytest.mark.parametrize("change", ["master", "notify", "emergency", "empty", "removed", "recipient", "sender"])
def test_frozen_pending_obligation_blocks_disabled_or_rotated_route_without_attempt(tmp_path, monkeypatch, change):
    from src.application.notification_fanout_delivery import send_notification_fanout
    cfg = route_config("B")
    sent = []
    def reject(**kwargs):
        sent.append(deepcopy(kwargs))
        return {"explicit_pre_acceptance_failure": True}
    kwargs = dict(base=tmp_path, config=cfg, account="alpha", logical_key="qa:rotation", message="frozen",
        send_fn=reject, normalize_fn=identity, now_ms=1000, retry_delays_ms=(10,))
    first = send_notification_fanout(**kwargs)
    if change in ("master", "notify"):
        cfg["channels"]["feishu"]["enabled" if change == "master" else "notify_enabled"] = False
    elif change == "emergency":
        cfg["notifications"]["enabled"] = False
    elif change == "empty":
        cfg["notifications"]["routes"] = []
    elif change == "removed":
        cfg["notifications"]["routes"][0]["accounts"] = ["beta"]
    elif change == "recipient":
        cfg["notifications"]["routes"][0]["target"] = "ou_rotated"
    else:
        monkeypatch.setenv("OM_FEISHU_BOT_APP_ID", "qa-rotated-app")
    second = send_notification_fanout(**{**kwargs, "now_ms": 2000})
    assert len(sent) == 1
    assert second["fanout_id"] == first["fanout_id"]
    assert second["children"][0]["attempt_count"] == 1
    assert second["children"][0]["spec"] == first["children"][0]["spec"]
    assert second["has_retryable_children"]


def test_actual_feishu_sender_uses_explicit_target_not_environment(tmp_path, monkeypatch):
    from src.application import notification_delivery_adapter as adapter
    from src.application.notification_delivery_route import resolve_notification_delivery_routes
    cfg = route_config("explicit")
    route, = resolve_notification_delivery_routes(config=cfg, account="alpha", base=tmp_path)
    sent = []
    monkeypatch.setattr(feishu_transport, "send_post_message", lambda **kw: sent.append(kw) or
        {"code": 0, "data": {"message_id": "qa-confirmed"}})
    result = adapter.send_feishu_app_message(base=tmp_path, channel=route["channel"], target=route["target"],
        notifications=route["notifications"], message="private alpha", idempotency_key="qa-frozen")
    assert len(sent) == 1 and sent[0]["open_id"] == "ou_explicit"
    assert result["attribution_route_key"] == route["identity_key"]
    assert sent[0]["uuid"] == "qa-frozen"


@pytest.mark.parametrize("rotation", ["to_user_id", "group_id", "chat_key", "sender_directory"])
def test_wechat_binding_rotation_does_not_retarget_old_pending_obligation(tmp_path, rotation):
    from src.application.channels.wechat_clawbot.state_store import WechatClawbotStateStore
    from src.application.notification_fanout_delivery import send_notification_fanout
    state = tmp_path / "wechat"
    binding = {"to_user_id": "qa-old-user", "context_token": "qa-context", "group_id": "qa-group", "chat_key": "qa-chat"}
    store = WechatClawbotStateStore(state)
    store.save_state({"bot_token": "qa-synthetic"})
    store.save_bindings({"bindings": {"ops": binding}})
    cfg = {"channels": {"wechat_clawbot": switches(True, False, True)},
        "notifications": {"enabled": True, "wechat_clawbot_state_dir": str(state), "routes": [
            {"id": "wechat", "provider": "wechat_clawbot", "target": "wechat:qa:ops", "accounts": ["alpha"]}]}}
    calls = []
    def send(**kw):
        calls.append(deepcopy(kw))
        return {"explicit_pre_acceptance_failure": True}
    kwargs = dict(base=tmp_path, config=cfg, account="alpha", logical_key="qa:wechat-rotation", message="private alpha",
        send_fn=send, normalize_fn=identity, retry_delays_ms=(10,))
    first = send_notification_fanout(**kwargs, now_ms=1000)
    if rotation == "sender_directory":
        state.rename(tmp_path / "retained-old-sender")
        replacement = WechatClawbotStateStore(state)
        replacement.save_state({"bot_token": "qa-new-synthetic"})
        replacement.save_bindings({"bindings": {"ops": binding}})
    else:
        store.save_bindings({"bindings": {"ops": {**binding, rotation: "qa-rotated"}}})
    final = send_notification_fanout(**kwargs, now_ms=2000)
    assert len(calls) == 1
    assert final["fanout_id"] == first["fanout_id"]
    assert final["children"][0]["attempt_count"] == 1
    assert final["children"][0]["spec"] == first["children"][0]["spec"]
    assert final["has_retryable_children"]


@pytest.mark.parametrize("provider,expected", [("feishu_app", (True, False)), ("wechat_clawbot", (False, True))])
@pytest.mark.parametrize("enabled", [None, True, False])
def test_legacy_upgrade_selection_preserves_old_missing_enabled_default_without_writes(tmp_path, provider, expected, enabled):
    from src.application.service_upgrade import _legacy_message_channels_enabled
    notifications = {"provider": provider}
    if enabled is not None:
        notifications["enabled"] = enabled
    config = tmp_path / "effective.us.json"
    config.write_text(json.dumps({"notifications": notifications}))
    profile = tmp_path / "service.profile.json"
    profile.write_text(json.dumps({"config_paths": {"us": str(config)}}))
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()}
    assert _legacy_message_channels_enabled(runtime_root=tmp_path) == (expected if enabled is not False else (False, False))
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()} == before


def test_mixed_fanout_process_loss_retries_only_explicit_failure(tmp_path):
    from src.application import notification_fanout as owner
    from src.application.notification_fanout_delivery import send_notification_fanout
    cfg = route_config("A", "B", "C")
    sent = []
    def send(**kwargs):
        sent.append(deepcopy(kwargs))
        if kwargs["target"] == "ou_C":
            raise SystemExit("qa process lost after unknown external result")
        if kwargs["target"] == "ou_B":
            return {"explicit_pre_acceptance_failure": True}
        from src.application.notification_delivery_route import notification_reminder_route_key
        return {"delivery_confirmed": True, "attribution_route_key": notification_reminder_route_key(
            config={"notifications": kwargs["notifications"]}, base=tmp_path)}
    kwargs = dict(base=tmp_path, config=cfg, account="alpha", logical_key="qa:crash", message="frozen body",
        send_fn=send, normalize_fn=identity, now_ms=1000, retry_delays_ms=(10,))
    with pytest.raises(SystemExit):
        send_notification_fanout(**kwargs)
    frozen = owner.list_notification_fanouts(base=tmp_path, account="alpha")[0]
    assert [c["status"] for c in frozen["children"]] == ["confirmed", "explicit_failed", "send_started"]
    def recover(**kw):
        assert kw["target"] == "ou_B"
        assert kw == sent[1]
        sent.append(deepcopy(kw))
        return {"delivery_confirmed": True, "attribution_route_key": frozen["children"][1]["identity_key"]}
    cfg["notifications"]["routes"].append({"id": "D", "provider": "feishu_app", "target": "ou_D", "accounts": ["alpha"]})
    final = send_notification_fanout(**{**kwargs, "send_fn": recover,
        "now_ms": 1000 + owner.CLAIM_LEASE_MS, "message": "must not replace frozen body"})
    assert [c["status"] for c in final["children"]] == ["confirmed", "confirmed", "unknown"]
    assert [c["attempt_count"] for c in final["children"]] == [1, 2, 1]
    assert [c["target"] for c in sent] == ["ou_A", "ou_B", "ou_C", "ou_B"]
    assert not final["delivery_confirmed"] and not final["has_retryable_children"]
    assert final["has_unknown_children"]
    assert owner.list_notification_fanouts(base=tmp_path, account="beta") == []


@pytest.mark.parametrize("provider", ["feishu_app", "wechat_clawbot"])
@pytest.mark.parametrize("explicit_empty", [False, True])
def test_actual_tick_alloff_legacy_routes_absent_keeps_local_report_no_transport(tmp_path, monkeypatch, provider, explicit_empty):
    from src.application import tick_notification_flow as flow
    from src.application import daily_decision_brief_repository as repo
    from tests.test_daily_decision_brief_notification_flow import _request, _patch_assembler, _run_flow
    cfg = {"channels": {"feishu": switches(), "wechat_clawbot": switches()},
        "notifications": {"enabled": True, "provider": provider, "channel": provider,
                          "target": "ou_legacy" if provider == "feishu_app" else "wechat:qa:ops"},
        "schedule": {"timezone": "America/New_York"}}
    if explicit_empty:
        cfg["notifications"]["routes"] = []
    calls = []
    monkeypatch.setattr(flow, "select_notification_delivery_adapter", forbidden(calls, "tick adapter"))
    _patch_assembler(monkeypatch)
    req = _request(tmp_path, run_id="qa-disabled", config=cfg)
    result = _run_flow(req.request)
    assert result == 0
    assert repo.read_latest_daily_decision_brief(base=tmp_path, account="lx", market="US")["available"]
    assert not list(tmp_path.rglob("nf_*.json"))
    assert calls == []


@pytest.mark.parametrize("family", ["maintenance", "system_alert", "trade_receipt", "lifecycle"])
def test_proactive_family_alloff_with_legacy_routes_absent_never_selects_transport(tmp_path, monkeypatch, family):
    from src.application.positions import maintenance_receipt as maintenance
    from src.application import system_alerts
    from src.application.trades import receipt
    cfg = {"channels": {"feishu": switches(), "wechat_clawbot": switches()},
        "notifications": {"enabled": True, "provider": "feishu_app", "target": "ou_legacy"}}
    calls = []
    reject = forbidden(calls, "disabled proactive family")
    if family == "maintenance":
        result = maintenance.send_auto_close_receipt(base=tmp_path, config=cfg, receipt_config={}, dry_run=False,
            result={"account": "alpha", "applied_closed": 1, "candidates_should_close": 1},
            adapter_selector=reject)
    elif family == "system_alert":
        monkeypatch.setattr(system_alerts, "select_notification_delivery_adapter", reject)
        result = system_alerts._send(tmp_path, cfg, "qa incident", "qa-key", account="alpha")
    elif family == "trade_receipt":
        result = receipt.send_trade_intake_receipt(base=tmp_path, config=cfg, receipt_config={},
            apply_changes=True, state=None, deal=SimpleNamespace(deal_id="qa-one", internal_account="alpha"),
            result={"account": "alpha", "status": "failed", "reason": "qa"}, payload={"account": "alpha"},
            adapter_selector=reject)
    else:
        result = receipt.send_trade_lifecycle_outbox_payload(base=tmp_path, config=cfg, receipt_config={},
            payload={"account": "alpha", "transition_type": "needs_review", "_frozen_message": "qa lifecycle"},
            adapter_selector=reject)
    assert not result.get("delivery_confirmed")
    assert calls == []
    assert not list(tmp_path.rglob("nf_*.json"))


def test_actual_tick_crash_recovers_b_only_and_queries_independent_child_receipts(tmp_path, monkeypatch):
    from src.application import tick_notification_flow as flow
    from src.application import daily_decision_brief_repository as repo
    from src.application.channels import plugins
    from src.application.notification_delivery_route import notification_reminder_route_key
    from tests.test_daily_decision_brief_notification_flow import _request, _patch_assembler, _run_flow
    cfg = route_config("A", "B", "C", account="lx")
    cfg["schedule"] = {"timezone": "America/New_York"}
    clock = ["2026-07-21T14:00:30+00:00"]
    monkeypatch.setattr(flow, "utc_now", lambda: clock[0])
    monkeypatch.setattr(repo, "_utc_now_iso", lambda: clock[0])
    monkeypatch.setattr(plugins, "channel_plugin_installed", lambda _: True)
    _patch_assembler(monkeypatch)
    sent = []
    def send(**kw):
        sent.append(deepcopy(kw))
        if kw["target"] == "ou_C":
            raise SystemExit("QA tick transport process lost")
        if kw["target"] == "ou_B" and len(sent) == 2:
            return {"explicit_pre_acceptance_failure": True}
        return {"delivery_confirmed": True, "attribution_route_key": notification_reminder_route_key(
            config={"notifications": kw["notifications"]}, base=tmp_path)}
    monkeypatch.setattr(flow, "select_notification_delivery_adapter", lambda _: SimpleNamespace(
        send_fn=send, normalize_fn=identity, failure_stage="qa"))
    first = _request(tmp_path, run_id="qa-tick-crash", config=cfg)
    with pytest.raises(SystemExit, match="QA tick transport process lost"):
        _run_flow(first.request)
    assert repo.read_latest_daily_decision_brief(base=tmp_path, account="lx", market="US")["available"]
    prohibited = []
    monkeypatch.setattr("src.application.tick_brief_preparation.assemble_daily_decision_briefs", forbidden(prohibited, "rerun scan/brief business"))
    clock[0] = "2026-07-21T14:06:00+00:00"
    cfg["notifications"]["routes"].append({"id": "D", "provider": "feishu_app", "target": "ou_D", "accounts": ["lx"]})
    retry = _request(tmp_path, run_id="qa-tick-retry", delivery_only=True, config=cfg)
    assert _run_flow(retry.request) == 1
    assert [c["target"] for c in sent] == ["ou_A", "ou_B", "ou_C", "ou_B"]
    assert sent[1] == sent[3]
    assert prohibited == [] and retry.commits == []
    rows = repo.query_daily_brief_receipts(base=tmp_path, account="lx", market="US", query={"run_id": "qa-tick-retry"})
    by_route = {row["business_result"]["route_id"]: row for row in rows}
    assert set(by_route) == {"A", "B", "C"}
    assert by_route["A"]["delivery_state"] == by_route["B"]["delivery_state"] == "confirmed"
    assert by_route["C"]["delivery_state"] != "confirmed"


def test_actual_tick_account_bodies_reach_only_selected_account_target(tmp_path, monkeypatch):
    from src.application import tick_notification_flow as flow
    from src.application import daily_decision_brief_repository as repo
    from src.application.channels import plugins
    from src.application.notification_delivery_route import notification_reminder_route_key
    from tests.test_daily_decision_brief_notification_flow import _request, _patch_assembler, _run_flow
    cfg = route_config("lx", account="lx")
    cfg["notifications"]["routes"].append({"id": "sy", "provider": "feishu_app", "target": "ou_sy", "accounts": ["sy"]})
    cfg["schedule"] = {"timezone": "America/New_York"}
    monkeypatch.setattr(flow, "utc_now", lambda: "2026-07-21T14:00:30+00:00")
    monkeypatch.setattr(repo, "_utc_now_iso", lambda: "2026-07-21T14:00:30+00:00")
    monkeypatch.setattr(plugins, "channel_plugin_installed", lambda _: True)
    _patch_assembler(monkeypatch)
    sent = []
    def send(**kw):
        sent.append(deepcopy(kw))
        return {"delivery_confirmed": True, "attribution_route_key": notification_reminder_route_key(
            config={"notifications": kw["notifications"]}, base=tmp_path)}
    monkeypatch.setattr(flow, "select_notification_delivery_adapter", lambda _: SimpleNamespace(
        send_fn=send, normalize_fn=identity, failure_stage="qa"))
    assert _run_flow(_request(tmp_path, run_id="qa-accounts", accounts=("lx", "sy"), config=cfg).request) == 0
    assert len(sent) == 2
    bodies = {c["target"]: c["message"] for c in sent}
    assert set(bodies) == {"ou_lx", "ou_sy"}
    assert "· lx" in bodies["ou_lx"] and "· sy" not in bodies["ou_lx"]
    assert "· sy" in bodies["ou_sy"] and "· lx" not in bodies["ou_sy"]


def test_inbox_parent_projection_failure_recovers_only_b_without_economic_reclaim(tmp_path, monkeypatch):
    from src.application import notification_fanout as fanout
    from src.application.trades import inbox, receipt
    from src.application.ledger import commands, writer, writer_trade_events
    from src.application.notification_delivery_route import notification_reminder_route_key
    cfg = route_config("A", "B", "C")
    clock = [2_000_000_000.0]
    monkeypatch.setattr(fanout.time, "time", lambda: clock[0])
    db = tmp_path / "inbox.sqlite3"
    key = inbox.enqueue_trade_payload(db, payload={"deal_id": "qa-one", "futu_account_id": "123",
        "account": "alpha"}, source="push", broker_deal_key="futu:alpha:123:qa-one")
    with inbox._connect(db) as conn:
        conn.execute("UPDATE trade_inbox SET receipt_recovery_allowed=1 WHERE inbox_id=?", (key,))
    claim = inbox.claim_trade_payload(db, inbox_id=key)
    business = inbox.save_trade_payload_result(db, claim=claim, result={"account": "alpha", "status": "failed",
        "reason": "qa-preexisting-result", "diagnostics": {"retryable": False}, "_receipt_payload": {"account": "alpha"}})
    inbox.settle_trade_payload_result(db, inbox_id=key, claim=claim, result=business)
    economic_calls = []
    for module, name in ((inbox, "claim_trade_payload"), (commands, "record_normalized_trade_event"),
                         (writer, "persist_trade_event_object"), (writer_trade_events, "persist_trade_event_object")):
        monkeypatch.setattr(module, name, forbidden(economic_calls, name))
    calls = []
    def send(**kw):
        calls.append(deepcopy(kw))
        target = kw["target"]
        if target == "ou_B" and sum(c["target"] == target for c in calls) == 1:
            return {"explicit_pre_acceptance_failure": True}
        if target == "ou_C":
            return {"ambiguous_send": True}
        return {"delivery_confirmed": True, "attribution_route_key": notification_reminder_route_key(
            config={"notifications": kw["notifications"]}, base=tmp_path)}
    kwargs = dict(base=tmp_path, config=cfg, receipt_config={}, apply_changes=True, state=None,
        deal=SimpleNamespace(deal_id="qa-one", internal_account="alpha"), result=business,
        payload={"account": "alpha"}, inbox_path=db, inbox_id=key, send_fn=send, normalize_fn=identity)
    finish = inbox.finish_trade_receipt_attempt
    parent_writes = []
    def fail_projection(*a, **kw):
        parent_writes.append((a, kw))
        raise RuntimeError("QA parent projection write failed")
    monkeypatch.setattr(inbox, "finish_trade_receipt_attempt", fail_projection)
    with pytest.raises(RuntimeError, match="QA parent projection write failed"):
        receipt.send_trade_intake_receipt(**kwargs)
    assert len(parent_writes) == 1
    before = inbox.read_trade_payload(db, inbox_id=key)
    frozen = fanout.list_notification_fanouts(base=tmp_path, account="alpha")[0]
    assert [c["status"] for c in frozen["children"]] == ["confirmed", "explicit_failed", "unknown"]
    assert before["receipt"]["fanout_id"] == frozen["fanout_id"]
    clock[0] += 61
    recovery = inbox.list_trade_receipt_recovery_rows(db, account_ids=["123"])
    assert [row["inbox_id"] for row in recovery] == [key]
    monkeypatch.setattr(inbox, "finish_trade_receipt_attempt", finish)
    result = receipt.send_trade_intake_receipt(**kwargs)
    after = inbox.read_trade_payload(db, inbox_id=key)
    assert [c["target"] for c in calls] == ["ou_A", "ou_B", "ou_C", "ou_B"]
    assert calls[1] == calls[3]
    assert result["status"] == "partial" and result["has_unknown_children"]
    assert not result["has_retryable_children"] and not result["delivery_confirmed"]
    assert after["attempt_count"] == before["attempt_count"]
    assert after["result"] == before["result"] == business
    assert economic_calls == []
    assert inbox.list_trade_receipt_recovery_rows(db, account_ids=["123"]) == []


def test_compensation_alloff_legacy_absent_routes_rejects_before_transport(tmp_path):
    from tests.test_trades_receipt_compensation import _fixture, _call
    sources, repo = _fixture(tmp_path)
    calls = []
    cfg = {"channels": {"feishu": switches(), "wechat_clawbot": switches()},
        "notifications": {"enabled": True, "provider": "feishu_app", "target": "ou_env_wrong"}}
    before = deepcopy(repo.events)
    with pytest.raises(ValueError, match="no enabled routes"):
        _call(tmp_path, sources, repo, config=cfg, adapter_selector=forbidden(calls, "compensation adapter"))
    assert repo.events == before
    assert calls == []
    assert not list(tmp_path.rglob("nf_*.json"))


def test_real_installed_package_bytes_match_reviewed_plugin_source():
    roots = installed_package_roots()
    for package, module in (("feishu-channel", PACKAGE_MODULES[0]), ("wechat-channel", PACKAGE_MODULES[1])):
        source = ROOT / "plugins" / package / "src"
        for path in sorted((source / module).rglob("*.py")):
            installed = Path(roots[module]) / path.relative_to(source)
            assert installed.is_file(), installed
            assert hashlib.sha256(installed.read_bytes()).digest() == hashlib.sha256(path.read_bytes()).digest(), (
                f"stale installed package: {path.relative_to(source)}; fresh installation required")


@pytest.mark.parametrize("selection", [(), (0,), (1,), (0, 1)])
def test_fresh_process_package_matrix_public_startup_and_real_mock_http(tmp_path, selection):
    roots = installed_package_roots()
    blocked = tuple(m for i, m in enumerate(PACKAGE_MODULES) if i not in selection)
    code = '''
import contextlib, importlib.util, io, json, socket, sys, urllib.request
from importlib.abc import MetaPathFinder
from pathlib import Path
calls = []
def forbidden(*a, **kw):
    calls.append((a, kw))
    raise AssertionError("network or SDK forbidden")
socket.socket.connect = forbidden
socket.socket.connect_ex = forbidden
urllib.request.urlopen = forbidden
original = importlib.util.find_spec
def spec(name, *a, **kw):
    return None if name.split('.')[0] in blocked else original(name, *a, **kw)
importlib.util.find_spec = spec
class Blocked(MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in blocked or fullname.split('.')[0] == "lark_oapi":
            if fullname.split('.')[0] == "lark_oapi":
                forbidden(fullname)
            raise ModuleNotFoundError(fullname, name=fullname)
sys.meta_path.insert(0, Blocked())
from src.interfaces.cli.main import main
from src.interfaces.agent.cli import main as agent
with contextlib.redirect_stdout(io.StringIO()):
    assert main(["help"]) == 0
    assert agent(["spec"]) == 0
assert not any(m in sys.modules for m in packages)
from src.application.channels.plugins import channel_plugin_installed
assert [channel_plugin_installed(c) for c in ("feishu", "wechat_clawbot")] == [i in selected for i in (0,1)]
from src.application.agent_tool_contracts import AgentToolError
from src.infrastructure import feishu_bot
from src.application.channels.wechat_clawbot.ilink_client import WechatClawbotClient
from src.application.bot.control.policy import require_inbound_channel
from importlib.metadata import distribution
http = []
bot_path = Path("qa.bot.json")
def control(channel, enabled):
    bot_path.write_text(json.dumps({"channels": {channel: {
        "enabled": enabled, "receive_enabled": True, "notify_enabled": False}}}))
if 0 in selected:
    dist = distribution("om-feishu-channel-plugin")
    assert Path(dist.locate_file('')).resolve() == Path(roots["om_feishu_channel"]).resolve()
    feishu_bot.with_tenant_token_retry = lambda app, secret, send: send("qa-token")
    response = feishu_bot.send_text_message(app_id="qa", app_secret="qa", open_id="qa-target", text="qa",
        http_json_fn=lambda *a, **kw: http.append((a, kw)) or {"code": 0})
    assert response == {"code": 0} and http[-1][0][2]["receive_id"] == "qa-target"
    control("feishu", True)
    def token_refresh(app, secret, send, **kwargs):
        control("feishu", False)
        return send("qa-refreshed-token")
    feishu_bot.with_tenant_token_retry = token_refresh
    for operation in (lambda: feishu_bot.reply_message(app_id="qa", app_secret="qa",
            message_id="qa-original", text="qa", http_json_fn=forbidden,
            before_send=lambda: require_inbound_channel("feishu", str(bot_path))),
        lambda: feishu_bot.add_message_reaction(app_id="qa", app_secret="qa", message_id="qa-original",
            emoji_type="Typing", http_json_fn=forbidden,
            before_send=lambda: require_inbound_channel("feishu", str(bot_path)))):
        control("feishu", True)
        try:
            operation()
        except AgentToolError as e:
            assert e.code == "CHANNEL_RECEIVE_DISABLED"
        else:
            raise AssertionError("disabled Feishu request reached transport")
else:
    try:
        feishu_bot.send_text_message(app_id="qa", app_secret="qa", open_id="qa", text="qa")
    except AgentToolError as e:
        assert e.code == "CHANNEL_PLUGIN_NOT_INSTALLED"
    else:
        raise AssertionError("missing Feishu package was accepted")
if 1 in selected:
    dist = distribution("om-wechat-channel-plugin")
    assert Path(dist.locate_file('')).resolve() == Path(roots["om_wechat_channel"]).resolve()
    client = WechatClawbotClient(bot_token="qa", base_url="https://qa.invalid",
        http_json_fn=lambda *a, **kw: http.append((a, kw)) or {"ret": 0})
    assert client.send_text_message(to_user_id="qa-user", context_token="qa-context", text="qa")["ret"] == 0
    assert http[-1][0][2]["msg"]["to_user_id"] == "qa-user"
else:
    try:
        WechatClawbotClient(bot_token="qa")
    except AgentToolError as e:
        assert e.code == "CHANNEL_PLUGIN_NOT_INSTALLED"
    else:
        raise AssertionError("missing Wechat package was accepted")
assert "lark_oapi" not in sys.modules
assert calls == [], calls
print("QA_PACKAGE_MATRIX_OK", selected)
'''
    prelude = f"blocked={blocked!r}\npackages={PACKAGE_MODULES!r}\nselected={selection!r}\nroots={roots!r}\n"
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "OM_SECRET_BACKEND": "env",
           "PYTHONPATH": os.pathsep.join(dict.fromkeys([str(ROOT), *roots.values()]))}
    result = subprocess.run([sys.executable, "-c", prelude + code], cwd=tmp_path, env=env,
        capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "QA_PACKAGE_MATRIX_OK" in result.stdout


@pytest.mark.parametrize("outcome", ["missing_credentials", "explicit_failed", "unknown"])
def test_repair_legacy_alert_public_failure_recovery_never_selects_foreign_channel(
        tmp_path, monkeypatch, outcome):
    from src.application import system_alerts
    calls, selected = [], []

    def primary(**kwargs):
        calls.append(deepcopy(kwargs))
        if outcome == "missing_credentials":
            raise ValueError("WeChat ClawBot bot_token is missing")
        return {"explicit_pre_acceptance_failure": outcome == "explicit_failed"}

    def select(provider):
        selected.append(provider)
        if provider != "wechat_clawbot":
            return forbidden(selected, "foreign alert adapter")(provider)
        return SimpleNamespace(send_fn=primary, normalize_fn=identity)

    monkeypatch.setattr(system_alerts, "select_notification_delivery_adapter", select)
    cfg = {"notifications": {"enabled": True, "provider": "wechat_clawbot", "target": "wechat:qa:ops"}}
    fields = dict(base=tmp_path, config=cfg, unit="qa", market="us", account="alpha",
        failure_code="QA", stage="qa")
    assert system_alerts.report_system_failure(**fields, run_id="qa", rc=1,
        first_error_at="2026-11-01T00:00:00+00:00", opend_login_state="unknown") != "confirmed"
    assert system_alerts.report_system_recovery(**fields) != "confirmed"
    assert selected == ["wechat_clawbot", "wechat_clawbot"]
    assert len(calls) == 2
    state = json.loads((tmp_path / "output_shared/state/system_alerts.json").read_text())
    row = next(value for value in state.values() if value.get("failure_code") == "QA")
    assert row["failure_fallback_used"] is False and row["recovery_fallback_used"] is False
    assert row["failure_provider"] == row["recovery_provider"] == "wechat_clawbot"
    assert row["failure_delivery_confirmed"] is False and row["recovery_delivery_confirmed"] is False


def test_repair_opend_global_public_path_real_installed_facade_mock_http(tmp_path, monkeypatch):
    from functools import partial
    for root in installed_package_roots().values():
        monkeypatch.syspath_prepend(root)
    from src.application import notification_delivery_adapter as adapter
    from src.application.multi_tick import opend_guard
    from src.infrastructure import feishu_bot
    http = []

    def send_http(*args, **kwargs):
        http.append(deepcopy((args, kwargs)))
        return {"code": 0, "data": {"message_id": f"qa-{len(http)}"}}

    monkeypatch.setattr(feishu_bot, "with_tenant_token_retry", lambda app, secret, send: send("qa-token"))
    monkeypatch.setattr(feishu_transport, "send_post_message", partial(feishu_bot.send_post_message, http_json_fn=send_http))
    cfg = route_config("shared", account="alpha")
    cfg["accounts"] = ["alpha", "beta"]
    cfg["notifications"]["opend_alert_after_consecutive_failures"] = 1
    cfg["notifications"]["routes"][0]["accounts"] = ["alpha", "beta"]
    cfg["notifications"]["routes"] += [
        {"id": "beta", "provider": "feishu_app", "target": "ou_beta", "accounts": ["beta"]},
        {"id": "foreign", "provider": "feishu_app", "target": "ou_foreign", "accounts": ["gamma"]}]
    assert opend_guard.send_opend_alert(tmp_path, cfg, error_code="OPEND_LOGIN_INVALID", message_text="qa unavailable")
    assert not opend_guard.send_opend_alert(tmp_path, cfg, error_code="OPEND_LOGIN_INVALID", message_text="duplicate")
    assert opend_guard.send_opend_recovery_notice(tmp_path, cfg)
    assert not opend_guard.send_opend_recovery_notice(tmp_path, cfg)
    bodies = [args[2] for args, _ in http]
    assert sorted(body["receive_id"] for body in bodies) == ["ou_beta", "ou_beta", "ou_shared", "ou_shared"]
    assert len({body["uuid"] for body in bodies}) == 4
    assert all("alpha" not in body["content"] and "beta" not in body["content"] for body in bodies)


@pytest.mark.parametrize("profile_selected", [False, True])
def test_repair_connection_setup_migrates_only_bound_explicit_receiver(tmp_path, monkeypatch, profile_selected):
    from src.application.bot.control import config_loader
    from src.application.channels.wechat_clawbot.state_store import WechatClawbotStateStore
    from src.interfaces.cli import main, feature_ops
    path, _ = authoring(tmp_path)
    profile = tmp_path / "service.profile.json"
    profile.write_text(json.dumps({"runtime_root": str(tmp_path),
        "config_authoring": {"config_yaml": str(path)},
        "services": [{"name": "options-monitor-feishu-ws.service"}] if profile_selected else []}))
    state = tmp_path / "output_shared/state/channels/wechat_clawbot/named"
    WechatClawbotStateStore(state).save_bindings({"bindings": {"team": {
        "to_user_id": "qa-user", "context_token": "qa-context"}}})
    args = main.parse_args(["channel", "configure", "--config-yaml", str(path),
        "--provider", "wechat_clawbot", "--enabled", "true", "--receive-enabled", "true",
        "--notify-enabled", "false", "--target", "wechat:named:team", "--label", "named",
        "--binding-name", "team", "--allowed-senders", "wechat:qa-user"])
    calls = []
    def run():
        return feature_ops.run_feature_configure(args, repo_base_fn=lambda: ROOT, input_is_tty=lambda: False,
            secret_runner=forbidden(calls, "secret"), wechat_connector=forbidden(calls, "QR/network"))
    before, profile_before = path.read_bytes(), profile.read_bytes()
    preview = run()
    assert path.read_bytes() == before and not (tmp_path / "config.bot.json").exists()
    args.apply = args.confirm = True
    args.expected_source_sha256 = preview["result"]["source_revision"]["before_sha256"]
    args.expected_preview_sha256 = preview["result"]["preview_sha256"]
    applied = run()["result"]
    expected = {"feishu": switches(profile_selected, profile_selected, False),
        "wechat_clawbot": switches(True, True, False)}
    for target in applied["verified_targets"]:
        doc = yaml.safe_load(Path(target).read_text()) if str(target).endswith(".yaml") else json.loads(Path(target).read_text())
        assert doc["channels"] == expected
        if Path(target) == path or str(target) == applied["bot"]["output_config_path"]:
            assert doc["inbound"]["wechat_clawbot"]["allowed_senders"] == "wechat:qa-user"
    monkeypatch.setattr(config_loader.plugins, "channel_plugin_installed", lambda _: True)
    bot = applied["bot"]["output_config_path"]
    config_loader.require_inbound_channel("wechat_clawbot", bot)
    if profile_selected:
        config_loader.require_inbound_channel("feishu", bot)
    else:
        from src.application.agent_tool_contracts import AgentToolError
        with pytest.raises(AgentToolError, match="disabled"):
            config_loader.require_inbound_channel("feishu", bot)
    assert profile.read_bytes() == profile_before
    assert not (tmp_path / "options-monitor.env").exists() and calls == []


def test_repair_feishu_setup_flags_preview_env_cas_then_real_generation(tmp_path):
    from src.application.agent_tool_contracts import AgentToolError
    from src.interfaces.cli import main, feature_ops
    path, _ = authoring(tmp_path, channel_controls={"feishu": switches(), "wechat_clawbot": switches()})
    args = main.parse_args(["channel", "configure", "--config-yaml", str(path), "--provider", "feishu_app",
        "--enabled", "true", "--receive-enabled", "true", "--notify-enabled", "false",
        "--app-id", "qa-setup-app", "--recipient", "ou_setup", "--allowed-senders", "ou_setup_sender"])
    calls = []
    run = lambda: feature_ops.run_feature_configure(args, repo_base_fn=lambda: ROOT,
        input_is_tty=lambda: False, secret_runner=forbidden(calls, "secret"))
    before = path.read_bytes()
    preview = run()
    env_path = tmp_path / "options-monitor.env"
    assert path.read_bytes() == before and not env_path.exists()
    assert preview["env"]["changes"]["OM_FEISHU_BOT_ALLOWED_OPEN_IDS"] == "ou_setup_sender"
    args.apply = args.confirm = True
    args.expected_source_sha256 = preview["result"]["source_revision"]["before_sha256"]
    args.expected_env_sha256 = "stale-env"
    with pytest.raises(AgentToolError, match="STALE_PREVIEW"):
        run()
    assert path.read_bytes() == before and not env_path.exists()
    args.expected_env_sha256 = preview["env"]["source_revision"]["before_sha256"]
    applied = run()
    assert applied["env"]["verified"] is True and applied["result"]["restart_performed"] is False
    from src.application.settings import build_effective_env
    env = build_effective_env(environ={}, env_file=env_path).values
    assert env["OM_FEISHU_BOT_APP_ID"] == "qa-setup-app"
    assert env["OM_FEISHU_BOT_USER_OPEN_ID"] == "ou_setup"
    assert env["OM_FEISHU_BOT_ALLOWED_OPEN_IDS"] == "ou_setup_sender"
    for target in applied["result"]["verified_targets"]:
        doc = yaml.safe_load(Path(target).read_text()) if str(target).endswith(".yaml") else json.loads(Path(target).read_text())
        assert doc["channels"] == {"feishu": switches(True, True, False), "wechat_clawbot": switches()}
    assert calls == []


@pytest.mark.parametrize("preview_flags", [[], ["--dry-run"], ["--apply", "--yes", "--dry-run"]])
def test_repair_public_positions_preview_does_not_touch_failed_delivery(tmp_path, monkeypatch, preview_flags):
    from src.application import notification_fanout as owner
    from src.application.ledger.repository import SQLiteOptionPositionsRepository
    from src.application.positions import auto_close, maintenance_receipt
    from src.interfaces.cli import option_positions
    cfg = route_config("A", "B")
    cfg.update(accounts=["alpha"], portfolio={"broker": "futu", "data_config": str(tmp_path / "data.json")},
        option_positions={"auto_close": {"enabled": True}})
    ledger = tmp_path / "isolated.sqlite3"
    repo = SQLiteOptionPositionsRepository(ledger)
    (tmp_path / "data.json").write_text(json.dumps({"option_positions": {"sqlite_path": str(ledger)}}))
    path = tmp_path / "config.us.json"
    path.write_text(json.dumps(cfg))
    # Seed delivery facts, never business events. No worker fixture or decision helper is replaced.
    result = {"account": "alpha", "broker": "futu", "mode": "apply", "applied_closed": 1,
        "candidates_should_close": 1, "applied": [], "as_of_utc": "2026-11-01T00:00:00Z"}
    seeded = maintenance_receipt.send_auto_close_receipt(base=tmp_path, config=cfg, receipt_config={},
        dry_run=False, result=result, send_fn=lambda **_: {"explicit_pre_acceptance_failure": True}, normalize_fn=identity)
    frozen = owner.read_notification_fanout(base=tmp_path, fanout_id=seeded["fanout_id"])
    assert [child["attempt_count"] for child in frozen["children"]] == [1, 1]
    protected = {p: p.read_bytes() for p in tmp_path.rglob("*.json") if p.name != "config.us.json"}
    events = repo.list_trade_events()
    monkeypatch.setattr(owner.time, "time", lambda: 4_000_000_000.0)
    calls = []
    monkeypatch.setattr(maintenance_receipt, "select_notification_delivery_adapter", forbidden(calls, "preview adapter"))
    monkeypatch.setattr(auto_close, "load_config", lambda **_: cfg)
    args = ["auto-close-expired", "--runtime-root", str(tmp_path),
        "--config", str(path), "--accounts", "alpha", "--quiet", *preview_flags]
    output_path = tmp_path / "output_shared/state/auto_close_expired.json"
    if "--apply" in preview_flags:
        with pytest.raises(SystemExit, match="--dry-run cannot be combined"):
            option_positions.main(args)
        assert not output_path.exists()
    else:
        assert option_positions.main(args) == 0
        preview = json.loads(output_path.read_text())
        assert preview["status"] == "dry_run" and preview["summary"]["applied_closed"] == 0
    assert owner.read_notification_fanout(base=tmp_path, fanout_id=seeded["fanout_id"]) == frozen
    assert all(p.read_bytes() == data for p, data in protected.items())
    assert repo.list_trade_events() == events and calls == []


@pytest.mark.parametrize("disk_state", ["disabled", "missing"])
@pytest.mark.parametrize("family", ["intake", "lifecycle"])
def test_repair_legacy_receipt_bound_disk_authority_never_uses_cached_enablement(
        tmp_path, family, disk_state):
    from src.application.trades import receipt
    path = tmp_path / "config.us.json"
    cached = {"config_source_path": str(path), "notifications": {
        "enabled": True, "provider": "feishu_app", "channel": "feishu_app"}}
    if disk_state == "disabled":
        path.write_text(json.dumps({"notifications": {"enabled": False}}))
    before = deepcopy(cached)
    calls = []
    reject = forbidden(calls, "legacy transport after authority closed")
    if family == "intake":
        result = receipt.send_trade_intake_receipt(base=tmp_path, config=cached, receipt_config={},
            apply_changes=True, state=None, deal=SimpleNamespace(deal_id="qa-one", internal_account="alpha"),
            result={"account": "alpha", "status": "failed", "reason": "qa"}, payload={"account": "alpha"},
            send_fn=reject, normalize_fn=identity)
    else:
        result = receipt.send_trade_lifecycle_outbox_payload(base=tmp_path, config=cached, receipt_config={},
            payload={"account": "alpha", "transition_type": "needs_review", "_frozen_message": "qa lifecycle"},
            send_fn=reject, normalize_fn=identity)
    assert calls == []
    assert not result.get("delivery_confirmed")
    assert cached == before


@pytest.mark.parametrize("provider", ["feishu_app", "wechat_clawbot"])
def test_repair_setup_omitted_master_never_silently_discards_connection_fields(tmp_path, provider):
    from src.application.agent_tool_contracts import AgentToolError
    from src.application.channels.wechat_clawbot.state_store import WechatClawbotStateStore
    from src.application.settings import build_effective_env
    from src.interfaces.cli import main, feature_ops
    path, _ = authoring(tmp_path, channel_controls={"feishu": switches(), "wechat_clawbot": switches()})
    flags = ["--app-id", "qa-app", "--recipient", "ou_qa", "--allowed-senders", "ou_sender"]
    if provider == "wechat_clawbot":
        state = tmp_path / "output_shared/state/channels/wechat_clawbot/named"
        WechatClawbotStateStore(state).save_bindings({"bindings": {"ops": {
            "to_user_id": "qa-user", "context_token": "qa-context"}}})
        flags = ["--target", "wechat:named:ops", "--label", "named", "--binding-name", "ops",
            "--allowed-senders", "wechat:qa-user"]
    args = main.parse_args(["channel", "configure", "--config-yaml", str(path),
        "--provider", provider, "--receive-enabled", "true", *flags])
    calls = []
    def run():
        return feature_ops.run_feature_configure(args, repo_base_fn=lambda: ROOT, input_is_tty=lambda: False,
            secret_runner=forbidden(calls, "secret"), wechat_connector=forbidden(calls, "QR/network"))
    before = path.read_bytes()
    try:
        preview = run()
    except AgentToolError as exc:
        assert exc.code == "INPUT_ERROR"
        assert "enabled" in str(exc).lower() or "master" in str(exc).lower()
        assert path.read_bytes() == before and not (tmp_path / "options-monitor.env").exists()
    else:
        assert path.read_bytes() == before
        args.apply = args.confirm = True
        args.expected_source_sha256 = preview["result"]["source_revision"]["before_sha256"]
        args.expected_preview_sha256 = preview["result"].get("preview_sha256")
        if "env" in preview:
            args.expected_env_sha256 = preview["env"]["source_revision"]["before_sha256"]
        assert run()["status"] == "configured"
        doc = yaml.safe_load(path.read_text())
        if provider == "feishu_app":
            env = build_effective_env(environ={}, env_file=tmp_path / "options-monitor.env").values
            assert env["OM_FEISHU_BOT_APP_ID"] == "qa-app"
            assert env["OM_FEISHU_BOT_USER_OPEN_ID"] == "ou_qa"
            assert env["OM_FEISHU_BOT_ALLOWED_OPEN_IDS"] == "ou_sender"
        else:
            assert doc["notifications"]["target"] == "wechat:named:ops"
            assert doc["inbound"]["wechat_clawbot"]["allowed_senders"] == "wechat:qa-user"
    assert calls == []
