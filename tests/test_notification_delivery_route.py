from __future__ import annotations

from src.application.notification_delivery_route import resolve_notification_delivery_route


def test_resolve_notification_delivery_route_preserves_openclaw_target() -> None:
    route = resolve_notification_delivery_route(
        config={"notifications": {"provider": "openclaw", "channel": "wechat_clawbot", "target": "user:test"}}
    )

    assert route["provider"] == "openclaw"
    assert route["channel"] == "openclaw-weixin"
    assert route["target"] == "user:test"


def test_resolve_notification_delivery_route_uses_feishu_bot_open_id(monkeypatch) -> None:
    monkeypatch.setenv("OM_FEISHU_BOT_USER_OPEN_ID", "ou_bot")

    route = resolve_notification_delivery_route(
        config={"notifications": {"provider": "feishu_app", "target": "ou_config"}}
    )

    assert route["provider"] == "feishu_app"
    assert route["channel"] == "feishu_app"
    assert route["target"] == "ou_bot"

