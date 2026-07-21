from __future__ import annotations

import hashlib
import json

import pytest

from src.infrastructure import feishu_bot


def _post_request_body_bytes(markdown: str, *, uuid: str | None = None) -> int:
    payload = {
        "receive_id": "ou_1",
        "msg_type": "post",
        "content": json.dumps(
            {"zh_cn": {"content": [[{"tag": "md", "text": markdown}]]}},
            ensure_ascii=False,
        ),
    }
    if uuid:
        payload["uuid"] = uuid
    return len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))


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


def test_send_post_message_posts_single_md_node_without_title(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict] = []
    markdown = '# 决策简报\n\n> 中文 "quote" 🙂\n\n- **NVDA**\n  - 合约: 1'

    monkeypatch.setattr(
        feishu_bot,
        "with_tenant_token_retry",
        lambda app_id, app_secret, fn: fn("tenant_token"),
    )

    def _http_json(method: str, url: str, payload: dict, headers: dict, **kwargs) -> dict:
        calls.append({"method": method, "url": url, "payload": payload, "headers": headers, "kwargs": kwargs})
        return {"code": 0, "data": {"message_id": "om_1"}}

    out = feishu_bot.send_post_message(
        app_id="app_1",
        app_secret="secret_1",
        open_id="  ou_1  ",
        markdown=f"  {markdown}  ",
        uuid="idem-1",
        http_json_fn=_http_json,
    )

    assert out["code"] == 0
    payload = calls[0]["payload"]
    assert payload["receive_id"] == "ou_1"
    assert payload["msg_type"] == "post"
    assert payload["uuid"] == "idem-1"
    assert json.loads(payload["content"]) == {
        "zh_cn": {"content": [[{"tag": "md", "text": markdown}]]}
    }
    assert "title" not in json.loads(payload["content"])["zh_cn"]
    assert calls[0]["kwargs"]["retry_max_attempts"] == 3


def test_send_post_message_without_uuid_disables_ambiguous_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict] = []

    monkeypatch.setattr(
        feishu_bot,
        "with_tenant_token_retry",
        lambda app_id, app_secret, fn: fn("tenant_token"),
    )

    def _http_json(method: str, url: str, payload: dict, headers: dict, **kwargs) -> dict:
        calls.append({"payload": payload, "kwargs": kwargs})
        return {"code": 0, "data": {"message_id": "om_1"}}

    feishu_bot.send_post_message(
        app_id="app_1",
        app_secret="secret_1",
        open_id="ou_1",
        markdown="# hello",
        http_json_fn=_http_json,
    )

    assert "uuid" not in calls[0]["payload"]
    assert calls[0]["kwargs"]["retry_max_attempts"] == 1


@pytest.mark.parametrize("open_id", ["", "   ", None])
def test_send_post_message_requires_open_id(open_id: object) -> None:
    with pytest.raises(ValueError, match="open_id is required"):
        feishu_bot.send_post_message(
            app_id="app_1",
            app_secret="secret_1",
            open_id=open_id,  # type: ignore[arg-type]
            markdown="# hello",
        )


@pytest.mark.parametrize("markdown", ["", "   ", None])
def test_send_post_message_requires_markdown(markdown: object) -> None:
    with pytest.raises(ValueError, match="markdown is required"):
        feishu_bot.send_post_message(
            app_id="app_1",
            app_secret="secret_1",
            open_id="ou_1",
            markdown=markdown,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("uuid", [None, "idem-1"])
def test_send_post_message_rejects_oversized_final_request_before_token_or_http(
    monkeypatch: pytest.MonkeyPatch,
    uuid: str | None,
) -> None:
    token_calls: list[object] = []
    http_calls: list[object] = []
    markdown = ("中🙂\"\n" * feishu_bot.FEISHU_POST_REQUEST_BUDGET_BYTES).strip()

    monkeypatch.setattr(
        feishu_bot,
        "with_tenant_token_retry",
        lambda *args, **kwargs: token_calls.append((args, kwargs)),
    )

    with pytest.raises(feishu_bot.FeishuPermanentError) as raised:
        feishu_bot.send_post_message(
            app_id="app_1",
            app_secret="secret_1",
            open_id="ou_1",
            markdown=markdown,
            uuid=uuid,
            http_json_fn=lambda *args, **kwargs: http_calls.append((args, kwargs)),
        )

    assert token_calls == []
    assert http_calls == []
    response = raised.value.response
    assert response == {
        "local_error_code": feishu_bot.FEISHU_POST_TOO_LARGE,
        "http_status": None,
        "feishu_code": None,
        "http_attempts": [],
        "request_body_bytes": response["request_body_bytes"],
        "request_body_budget_bytes": feishu_bot.FEISHU_POST_REQUEST_BUDGET_BYTES,
        "normalized_markdown_chars": len(markdown),
        "normalized_markdown_sha256": hashlib.sha256(markdown.encode("utf-8")).hexdigest(),
    }
    assert response["request_body_bytes"] > response["request_body_budget_bytes"]
    assert markdown not in repr(response)


def test_send_post_message_measures_full_outer_request_and_sends_under_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []
    markdown = ("中🙂\"\n" * 1000).strip()

    monkeypatch.setattr(
        feishu_bot,
        "with_tenant_token_retry",
        lambda app_id, app_secret, fn: fn("tenant_token"),
    )

    def _http_json(method: str, url: str, payload: dict, headers: dict, **kwargs) -> dict:
        calls.append({"payload": payload, "kwargs": kwargs})
        return {"code": 0, "data": {"message_id": "om_1"}}

    feishu_bot.send_post_message(
        app_id="app_1",
        app_secret="secret_1",
        open_id="ou_1",
        markdown=markdown,
        uuid="idem-1",
        http_json_fn=_http_json,
    )

    payload = calls[0]["payload"]
    assert len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) < feishu_bot.FEISHU_POST_REQUEST_BUDGET_BYTES
    assert json.loads(payload["content"])["zh_cn"]["content"][0][0]["text"] == markdown


@pytest.mark.parametrize("uuid", [None, "idem-1"])
def test_send_post_message_enforces_exact_final_request_boundary(
    monkeypatch: pytest.MonkeyPatch,
    uuid: str | None,
) -> None:
    token_calls: list[tuple[str, str]] = []
    http_calls: list[dict] = []
    one_char_overhead = _post_request_body_bytes("a", uuid=uuid) - 1
    exact_markdown = "a" * (feishu_bot.FEISHU_POST_REQUEST_BUDGET_BYTES - one_char_overhead)

    assert _post_request_body_bytes(exact_markdown, uuid=uuid) == feishu_bot.FEISHU_POST_REQUEST_BUDGET_BYTES
    assert _post_request_body_bytes(exact_markdown + "a", uuid=uuid) == (
        feishu_bot.FEISHU_POST_REQUEST_BUDGET_BYTES + 1
    )

    def _with_token(app_id: str, app_secret: str, fn):  # type: ignore[no-untyped-def]
        token_calls.append((app_id, app_secret))
        return fn("tenant_token")

    def _http_json(method: str, url: str, payload: dict, headers: dict, **kwargs) -> dict:
        http_calls.append(payload)
        return {"code": 0, "data": {"message_id": "om_1"}}

    monkeypatch.setattr(feishu_bot, "with_tenant_token_retry", _with_token)

    feishu_bot.send_post_message(
        app_id="app_1",
        app_secret="secret_1",
        open_id="ou_1",
        markdown=exact_markdown,
        uuid=uuid,
        http_json_fn=_http_json,
    )
    with pytest.raises(feishu_bot.FeishuPermanentError):
        feishu_bot.send_post_message(
            app_id="app_1",
            app_secret="secret_1",
            open_id="ou_1",
            markdown=exact_markdown + "a",
            uuid=uuid,
            http_json_fn=_http_json,
        )

    assert len(token_calls) == 1
    assert len(http_calls) == 1
