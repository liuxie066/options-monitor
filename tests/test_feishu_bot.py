from __future__ import annotations

import pytest

from src.infrastructure import feishu_bot


def test_add_message_reaction_posts_feishu_reaction_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict] = []

    token_calls: list[dict] = []

    def _with_token(app_id, app_secret, fn, **kwargs):
        token_calls.append({"app_id": app_id, "app_secret": app_secret, **kwargs})
        return fn("tenant_token")

    monkeypatch.setattr(feishu_bot, "with_tenant_token_retry", _with_token)

    def _http_json(method: str, url: str, payload: dict, headers: dict, **kwargs) -> dict:
        calls.append({"method": method, "url": url, "payload": payload, "headers": headers, "kwargs": kwargs})
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
            "kwargs": {"timeout": 2, "retry_max_attempts": 1},
        }
    ]
    assert token_calls == [
        {
            "app_id": "app_1",
            "app_secret": "secret_1",
            "token_timeout": 2,
            "token_retry_max_attempts": 1,
            "token_lock_timeout": 0.0,
        }
    ]


def test_add_message_reaction_preserves_official_mixed_case_emoji(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payloads: list[dict] = []

    monkeypatch.setattr(
        feishu_bot,
        "with_tenant_token_retry",
        lambda _app_id, _app_secret, fn, **_kwargs: fn("tenant_token"),
    )

    def _http_json(
        _method: str,
        _url: str,
        payload: dict,
        headers: dict,
        **_kwargs,
    ) -> dict:
        assert headers["Authorization"] == "Bearer tenant_token"
        payloads.append(payload)
        return {"code": 0}

    feishu_bot.add_message_reaction(
        app_id="app_1",
        app_secret="secret_1",
        message_id="msg_1",
        emoji_type="Typing",
        http_json_fn=_http_json,
    )

    assert payloads == [{"reaction_type": {"emoji_type": "Typing"}}]


def test_add_message_reaction_requires_message_and_emoji() -> None:
    with pytest.raises(ValueError, match="message_id is required"):
        feishu_bot.add_message_reaction(app_id="app_1", app_secret="secret_1", message_id="", emoji_type="SMILE")

    with pytest.raises(ValueError, match="emoji_type is required"):
        feishu_bot.add_message_reaction(app_id="app_1", app_secret="secret_1", message_id="msg_1", emoji_type="")


def test_send_text_message_passes_uuid_and_enables_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict] = []

    monkeypatch.setattr(
        feishu_bot,
        "with_tenant_token_retry",
        lambda app_id, app_secret, fn: fn("tenant_token"),
    )

    def _http_json(method: str, url: str, payload: dict, headers: dict, **kwargs) -> dict:
        calls.append({"method": method, "url": url, "payload": payload, "headers": headers, "kwargs": kwargs})
        return {"code": 0, "data": {"message_id": "om_1"}}

    logs: list[dict] = []
    out = feishu_bot.send_text_message(
        app_id="app_1",
        app_secret="secret_1",
        open_id="ou_1",
        text="hello",
        uuid="idem-1",
        log_fn=logs.append,
        http_json_fn=_http_json,
    )

    assert out["code"] == 0
    assert calls[0]["payload"]["uuid"] == "idem-1"
    assert calls[0]["kwargs"]["retry_max_attempts"] == 3
    assert calls[0]["kwargs"]["log_fn"].__self__ is logs
    assert calls[0]["kwargs"]["log_success_attempts"] is True


def test_send_text_message_without_uuid_disables_ambiguous_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict] = []

    monkeypatch.setattr(
        feishu_bot,
        "with_tenant_token_retry",
        lambda app_id, app_secret, fn: fn("tenant_token"),
    )

    def _http_json(method: str, url: str, payload: dict, headers: dict, **kwargs) -> dict:
        calls.append({"payload": payload, "kwargs": kwargs})
        return {"code": 0, "data": {"message_id": "om_1"}}

    feishu_bot.send_text_message(
        app_id="app_1",
        app_secret="secret_1",
        open_id="ou_1",
        text="hello",
        http_json_fn=_http_json,
    )

    assert "uuid" not in calls[0]["payload"]
    assert calls[0]["kwargs"]["retry_max_attempts"] == 1
