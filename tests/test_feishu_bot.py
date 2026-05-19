from __future__ import annotations

import pytest

from src.infrastructure import feishu_bot


def test_add_message_reaction_posts_feishu_reaction_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict] = []

    monkeypatch.setattr(
        feishu_bot,
        "with_tenant_token_retry",
        lambda app_id, app_secret, fn: fn("tenant_token"),
    )

    def _http_json(method: str, url: str, payload: dict, headers: dict) -> dict:
        calls.append({"method": method, "url": url, "payload": payload, "headers": headers})
        return {"code": 0, "data": {"reaction_id": "r_1"}}

    out = feishu_bot.add_message_reaction(
        app_id="app_1",
        app_secret="secret_1",
        message_id="msg/1",
        emoji_type="smile",
        http_json_fn=_http_json,
    )

    assert out["code"] == 0
    assert calls == [
        {
            "method": "POST",
            "url": "https://open.feishu.cn/open-apis/im/v1/messages/msg%2F1/reactions",
            "payload": {"reaction_type": {"emoji_type": "SMILE"}},
            "headers": {
                "Authorization": "Bearer tenant_token",
                "Content-Type": "application/json; charset=utf-8",
            },
        }
    ]


def test_add_message_reaction_requires_message_and_emoji() -> None:
    with pytest.raises(ValueError, match="message_id is required"):
        feishu_bot.add_message_reaction(app_id="app_1", app_secret="secret_1", message_id="", emoji_type="SMILE")

    with pytest.raises(ValueError, match="emoji_type is required"):
        feishu_bot.add_message_reaction(app_id="app_1", app_secret="secret_1", message_id="msg_1", emoji_type="")
