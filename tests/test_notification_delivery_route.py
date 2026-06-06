from __future__ import annotations

from src.application.notification_delivery_route import resolve_notification_delivery_route


def test_resolve_notification_delivery_route_preserves_wechat_clawbot_target() -> None:
    route = resolve_notification_delivery_route(
        config={"notifications": {"channel": "wechat_clawbot", "target": "wechat:ops"}}
    )

    assert route["provider"] == "wechat_clawbot"
    assert route["channel"] == "wechat_clawbot"
    assert route["target"] == "wechat:ops"


def test_resolve_notification_delivery_route_uses_feishu_bot_open_id(monkeypatch) -> None:
    monkeypatch.setenv("OM_FEISHU_BOT_USER_OPEN_ID", "ou_bot")

    route = resolve_notification_delivery_route(
        config={"notifications": {"provider": "feishu_app", "target": "ou_config"}}
    )

    assert route["provider"] == "feishu_app"
    assert route["channel"] == "feishu_app"
    assert route["target"] == "ou_bot"
