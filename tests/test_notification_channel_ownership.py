"""Shared routes consume channel-owned facts and keep authority/hash policy."""
from pathlib import Path

import pytest

from domain.domain.multi_tick import notification_route_identity_hash
from src.application import notification_delivery_route as routes
from src.application.channels.contracts import ChannelCapabilities
from src.application.channels.service import ChannelAdapter, ChannelRegistry


def _config():
    return {"notifications": {"enabled": True, "provider": "feishu_app", "target": "authoring-hint"}}


def test_shared_route_dispatches_target_and_identity_to_channel(monkeypatch, tmp_path):
    calls = []

    def target(**kwargs):
        calls.append(("target", kwargs))
        return "effective-target"

    def identity(**kwargs):
        calls.append(("identity", kwargs))
        return {"sender": "adapter-sender", "recipient": "adapter-recipient"}

    adapter = ChannelAdapter(channel="feishu_app", capabilities=ChannelCapabilities(outbound=True),
                             resolve_target_fn=target, resolve_identity_fn=identity)
    monkeypatch.setattr(routes, "build_notification_channel_registry", lambda: ChannelRegistry((adapter,)))
    config = _config()
    route = routes.resolve_notification_delivery_route(config=config)
    assert route["target"] == "effective-target"
    key = routes.notification_reminder_route_key(config=config, base=tmp_path)
    assert key == notification_route_identity_hash(provider="feishu_app", channel="feishu_app",
                                                    sender="adapter-sender", recipient="adapter-recipient")
    assert [call[0] for call in calls] == ["target", "identity"]
    assert calls[0][1]["target"] == "authoring-hint"
    assert calls[1][1]["base"] == tmp_path
    assert calls[1][1]["route"]["provider"] == "feishu_app"


@pytest.mark.parametrize("evidence", [None, "error"])
def test_unknown_channel_identity_cannot_prove_seen_recipient(monkeypatch, tmp_path, evidence):
    calls = []

    def identity(**kwargs):
        calls.append(kwargs)
        if evidence == "error":
            raise RuntimeError("identity unavailable")
        return None

    adapter = ChannelAdapter(channel="feishu_app", resolve_identity_fn=identity)
    monkeypatch.setattr(routes, "build_notification_channel_registry", lambda: ChannelRegistry((adapter,)))
    assert routes.notification_reminder_route_key(config=_config(), base=tmp_path) is None
    assert len(calls) == 1


def test_disabled_notifications_never_construct_or_probe_adapters(monkeypatch, tmp_path):
    calls = []

    def forbidden():
        calls.append("adapter")
        raise AssertionError("disabled delivery reached adapter")

    monkeypatch.setattr(routes, "build_notification_channel_registry", forbidden)
    config = {"notifications": {"enabled": False, "provider": "feishu_app"}}
    assert routes.resolve_notification_delivery_route(config=config)["enabled"] is False
    assert routes.resolve_notification_delivery_routes(config=config, account="lx", base=tmp_path) == []
    assert routes.notification_reminder_route_key(config=config, base=tmp_path) is None
    assert calls == []


def test_official_builders_supply_transport_and_compatibility_exports():
    from src.application import notification_delivery_adapter as facade
    from src.application.channels import feishu_notification
    from src.application.channels.feishu import build_feishu_channel_adapter
    from src.application.channels.wechat_clawbot.adapter import build_wechat_clawbot_channel_adapter
    from src.application.channels.wechat_clawbot import notification as wechat

    feishu = build_feishu_channel_adapter()
    assert feishu.send_fn is facade.send_feishu_app_message_process is feishu_notification.send_feishu_app_message_process
    assert feishu.normalize_fn is facade.normalize_feishu_app_send_output
    assert facade.send_feishu_app_message is feishu_notification.send_feishu_app_message
    assert facade.resolve_feishu_bot_send_target is feishu_notification.resolve_feishu_bot_send_target
    clawbot = build_wechat_clawbot_channel_adapter()
    assert clawbot.send_fn is wechat.send_wechat_clawbot_message_process
    assert clawbot.normalize_fn is wechat.normalize_wechat_clawbot_send_output
    assert clawbot.resolve_target_fn(target="wechat:ops", notifications={}) == "wechat:ops"
    assert feishu.resolve_identity_fn is not None and clawbot.resolve_identity_fn is not None
    assert Path(feishu.send_fn.__code__.co_filename).parent.name == "channels"
