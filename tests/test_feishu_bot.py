from __future__ import annotations

import hashlib
import json

import pytest

from src.infrastructure import feishu_bot
from tests.notification_format_assertions import assert_mobile_flat_markdown


def _post_request_body_bytes(markdown: str, *, uuid: str | None = None) -> int:
    payload = {
        "receive_id": "ou_1",
        "msg_type": "post",
        "content": json.dumps(
            {"zh_cn": {"content": feishu_bot._post_md_paragraphs(markdown)}},
            ensure_ascii=False,
        ),
    }
    if uuid:
        payload["uuid"] = uuid
    return len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))


def test_reply_message_sends_card_json_v2_and_serializes_content_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []
    card = {
        "schema": "2.0",
        "body": {
            "elements": [
                {
                    "tag": "markdown",
                    "element_id": "reply_body",
                    "content": "| 项目 | CNY |\n|---|---:|\n| 权利金 | ¥1,000 |",
                }
            ]
        },
    }

    monkeypatch.setattr(
        feishu_bot,
        "with_tenant_token_retry",
        lambda _app_id, _app_secret, fn: fn("tenant_token"),
    )

    def _http_json(method: str, url: str, payload: dict, headers: dict, **kwargs) -> dict:
        calls.append(
            {
                "method": method,
                "url": url,
                "payload": payload,
                "headers": headers,
                "kwargs": kwargs,
            }
        )
        return {"code": 0, "data": {"message_id": "om_reply"}}

    out = feishu_bot.reply_message(
        app_id="app_1",
        app_secret="secret_1",
        message_id="msg/1",
        msg_type="interactive",
        content=card,
        uuid="feishu:cmd_1",
        reply_in_thread=True,
        http_json_fn=_http_json,
    )

    assert out["code"] == 0
    assert calls[0]["url"].endswith("/messages/msg%2F1/reply")
    assert calls[0]["payload"] == {
        "msg_type": "interactive",
        "content": json.dumps(card, ensure_ascii=False),
        "uuid": "feishu:cmd_1",
        "reply_in_thread": True,
    }
    assert json.loads(calls[0]["payload"]["content"]) == card


def test_reply_text_message_remains_backward_compatible(monkeypatch: pytest.MonkeyPatch) -> None:
    payloads: list[dict] = []
    monkeypatch.setattr(
        feishu_bot,
        "with_tenant_token_retry",
        lambda _app_id, _app_secret, fn: fn("tenant_token"),
    )

    feishu_bot.reply_text_message(
        app_id="app_1",
        app_secret="secret_1",
        message_id="msg_1",
        text="当前没有待确认操作。",
        http_json_fn=lambda _method, _url, payload, **_kwargs: payloads.append(payload) or {"code": 0},
    )

    assert payloads == [
        {
            "msg_type": "text",
            "content": json.dumps({"text": "当前没有待确认操作。"}, ensure_ascii=False),
        }
    ]


def test_send_message_sends_proactive_card_with_uuid_and_retry_logging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []
    card = {
        "schema": "2.0",
        "body": {
            "elements": [
                {
                    "tag": "markdown",
                    "element_id": "notification_body",
                    "content": "| 项目 | 数值 |\n|---|---:|\n| 现金 | ¥1,000 |",
                }
            ]
        },
    }
    monkeypatch.setattr(
        feishu_bot,
        "with_tenant_token_retry",
        lambda _app_id, _app_secret, fn: fn("tenant_token"),
    )

    out = feishu_bot.send_message(
        app_id="app_1",
        app_secret="secret_1",
        open_id="ou_1",
        msg_type="interactive",
        content=card,
        uuid="om-card-1",
        log_fn=lambda item: calls.append({"log": item}),
        http_json_fn=lambda method, url, payload, headers, **kwargs: calls.append(
            {
                "method": method,
                "url": url,
                "payload": payload,
                "headers": headers,
                "kwargs": kwargs,
            }
        )
        or {"code": 0, "data": {"message_id": "om_card"}},
    )

    request = calls[0]
    assert out["data"]["message_id"] == "om_card"
    assert request["url"].endswith("/open-apis/im/v1/messages?receive_id_type=open_id")
    assert request["payload"] == {
        "receive_id": "ou_1",
        "msg_type": "interactive",
        "content": json.dumps(card, ensure_ascii=False),
        "uuid": "om-card-1",
    }
    assert request["kwargs"]["retry_max_attempts"] == 3
    assert request["kwargs"]["log_success_attempts"] is True


def test_send_message_rejects_oversized_card_before_token_or_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        feishu_bot,
        "with_tenant_token_retry",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not request token")),
    )
    card = {
        "schema": "2.0",
        "body": {
            "elements": [
                {
                    "tag": "markdown",
                    "element_id": "notification_body",
                    "content": "中" * feishu_bot.FEISHU_SEND_REQUEST_BUDGET_BYTES,
                }
            ]
        },
    }

    with pytest.raises(feishu_bot.FeishuPermanentError) as exc_info:
        feishu_bot.send_message(
            app_id="app_1",
            app_secret="secret_1",
            open_id="ou_1",
            msg_type="interactive",
            content=card,
            uuid="om-card-too-large",
        )

    assert exc_info.value.response["local_error_code"] == feishu_bot.FEISHU_SEND_TOO_LARGE
    assert exc_info.value.response["request_body_bytes"] > feishu_bot.FEISHU_SEND_REQUEST_BUDGET_BYTES


def test_reply_message_rejects_request_over_local_budget_without_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        feishu_bot,
        "with_tenant_token_retry",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not request token")),
    )
    card = {
        "schema": "2.0",
        "body": {
            "elements": [
                {
                    "tag": "markdown",
                    "element_id": "reply_body",
                    "content": "中🙂" * feishu_bot.FEISHU_REPLY_REQUEST_BUDGET_BYTES,
                }
            ]
        },
    }

    with pytest.raises(feishu_bot.FeishuPermanentError) as raised:
        feishu_bot.reply_message(
            app_id="app_1",
            app_secret="secret_1",
            message_id="msg_1",
            msg_type="interactive",
            content=card,
        )

    assert raised.value.response["local_error_code"] == feishu_bot.FEISHU_REPLY_TOO_LARGE
    assert raised.value.response["request_body_bytes"] > feishu_bot.FEISHU_REPLY_REQUEST_BUDGET_BYTES
    assert "content_sha256" in raised.value.response


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


def test_send_post_message_posts_md_paragraphs_without_title(monkeypatch: pytest.MonkeyPatch) -> None:
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
        "zh_cn": {
            "content": [
                [{"tag": "md", "text": "# 决策简报"}],
                [{"tag": "text", "text": "\u00a0"}],
                [{"tag": "md", "text": '> 中文 "quote" 🙂'}],
                [{"tag": "text", "text": "\u00a0"}],
                [{"tag": "md", "text": "- **NVDA**\n  - 合约: 1"}],
            ]
        }
    }
    assert "title" not in json.loads(payload["content"])["zh_cn"]
    assert calls[0]["kwargs"]["retry_max_attempts"] == 3


def test_send_post_message_maps_zero_width_spacer_lines_to_paragraph_breaks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []
    markdown = "# OM · 决策简报 · lx\n\u200b\n状态｜扫描完成\n\u200b\n**1｜NVDA｜Sell Put**\n指标｜权利金 $5.25"

    monkeypatch.setattr(
        feishu_bot,
        "with_tenant_token_retry",
        lambda app_id, app_secret, fn: fn("tenant_token"),
    )

    def _http_json(method: str, url: str, payload: dict, headers: dict, **kwargs) -> dict:
        calls.append({"payload": payload})
        return {"code": 0, "data": {"message_id": "om_1"}}

    feishu_bot.send_post_message(
        app_id="app_1",
        app_secret="secret_1",
        open_id="ou_1",
        markdown=markdown,
        http_json_fn=_http_json,
    )

    assert json.loads(calls[0]["payload"]["content"]) == {
        "zh_cn": {
            "content": [
                [{"tag": "md", "text": "# OM · 决策简报 · lx"}],
                [{"tag": "text", "text": "\u00a0"}],
                [{"tag": "md", "text": "状态｜扫描完成"}],
                [{"tag": "text", "text": "\u00a0"}],
                [{"tag": "md", "text": "**1｜NVDA｜Sell Put**\n指标｜权利金 $5.25"}],
            ]
        }
    }
    paragraphs = json.loads(calls[0]["payload"]["content"])["zh_cn"]["content"]
    assert [paragraph[0]["tag"] for paragraph in paragraphs] == ["md", "text", "md", "text", "md"]
    for paragraph in paragraphs:
        assert "\u200b" not in paragraph[0]["text"]


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


def test_real_notification_renderers_preserve_content_and_visible_blank_paragraphs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.application.daily_decision_brief_renderer import render_full_brief
    from src.application.positions.maintenance_receipt import build_auto_close_receipt_message
    from src.application.scheduled_notification import build_notify_failure_summary_message
    from src.application.trades.receipt import build_trade_intake_receipt_message

    daily_brief = render_full_brief(
        {
            "market": "US",
            "account": "lx",
            "actionability": "live_actionable",
            "data_as_of_utc": "2026-07-21T14:03:00+00:00",
            "candidates": {
                "sell_put": [
                    {
                        "rank": 1,
                        "symbol": "NVDA",
                        "option_type": "put",
                        "expiration": "2026-08-21",
                        "strike": 100,
                        "priority": "P1",
                        "metrics": {
                            "mid": 5.25,
                            "annualized_net_return_on_cash_basis": 0.181,
                            "delta": -0.24,
                            "dte": 32,
                            "net_income": 480,
                        },
                        "capacity": {"contracts_available": 1, "reason": "cash_supported"},
                    }
                ],
                "covered_call": [],
                "combo_yield": [],
            },
            "positions": [
                {
                    "symbol": "NVDA",
                    "strategy_family": "sell_put",
                    "expiration": "2026-08-21",
                    "strike": 100,
                    "option_type": "put",
                    "close_action": "close",
                    "evaluation_status": "evaluable",
                    "quote_status": "priced",
                    "metrics": {
                        "close_mid": 0.52,
                        "realized_if_close": 474.5,
                        "remaining_annualized_return": 0.042,
                    },
                }
            ],
            "capacity": {"sell_put": {"contracts_available": 1, "reason": "cash_supported"}},
        }
    )
    trade_receipt = build_trade_intake_receipt_message(
        deal=None,
        result={
            "status": "unresolved",
            "reason": "ambiguous_assigned_stock_sale",
            "deal_id": "deal-1",
            "account": "lx",
            "action": "close",
            "diagnostics": {
                "candidates": [
                    {
                        "symbol": "NVDA",
                        "currency": "USD",
                        "shares_remaining": 100,
                        "stock_cost_per_share": 100,
                        "stock_lot_id": "lot-1",
                    }
                ]
            },
        },
        payload={
            "symbol": "NVDA",
            "option_type": "put",
            "expiration_ymd": "2026-08-21",
            "strike": 100,
            "qty": 1,
            "price": 5.25,
            "side": "sell",
        },
    )
    maintenance_receipt = build_auto_close_receipt_message(
        dry_run=False,
        result={
            "mode": "applied",
            "account": "lx",
            "broker": "富途",
            "grace_days": 2,
            "applied_closed": 1,
            "candidates_should_close": 2,
            "as_of_utc": "2026-07-21T14:03:00+00:00",
            "applied": [
                {"record_id": "rec_1", "position_id": "pos_1", "expiration_ymd": "2026-07-18"}
            ],
            "errors": ["rec_2 pos_2: sqlite locked"],
        },
    )
    failure_recovery = build_notify_failure_summary_message(
        run_id="run-1",
        sent_accounts=["sy"],
        notify_failures=[
            {
                "account": "lx",
                "error_code": "FEISHU_POST_TOO_LARGE",
                "attempts": 1,
                "delivery_confirmed": False,
                "message_id": None,
            }
        ],
    )
    messages = {
        "daily-brief": daily_brief,
        "trade-receipt": trade_receipt,
        "maintenance-receipt": maintenance_receipt,
        "failure-recovery": failure_recovery,
    }

    assert daily_brief.startswith("# OM · 决策简报 · lx")
    assert "状态｜当前简报" in daily_brief
    assert "\n## Sell Put\n" in daily_brief
    assert "合约｜2026-08-21 100 Put" in trade_receipt
    assert "## 可选批次" in trade_receipt
    assert "## 已完成" in maintenance_receipt and "## 失败" in maintenance_receipt
    assert "FEISHU_POST_TOO_LARGE · 尝试 1 次" in failure_recovery
    for message in messages.values():
        assert_mobile_flat_markdown(message)

    payloads: list[dict] = []
    monkeypatch.setattr(
        feishu_bot,
        "with_tenant_token_retry",
        lambda app_id, app_secret, fn: fn("tenant_token"),
    )

    def _http_json(method: str, url: str, payload: dict, headers: dict, **kwargs) -> dict:
        payloads.append(payload)
        return {"code": 0, "data": {"message_id": f"om_{len(payloads)}"}}

    for name, message in messages.items():
        feishu_bot.send_post_message(
            app_id="app_1",
            app_secret="secret_1",
            open_id="ou_1",
            markdown=message,
            uuid=f"fixture-{name}",
            http_json_fn=_http_json,
        )

    assert len(payloads) == len(messages)
    for payload, expected in zip(payloads, messages.values(), strict=True):
        content = json.loads(payload["content"])
        paragraphs = content["zh_cn"]["content"]
        assert content == {"zh_cn": {"content": feishu_bot._post_md_paragraphs(expected.strip())}}
        # Canonical content lines stay unchanged; canonical separators become
        # dedicated plain-text spacer paragraphs, never md prefixes.
        flattened = [
            line
            for paragraph in paragraphs
            for node in paragraph
            if node["tag"] == "md"
            for line in node["text"].split("\n")
        ]
        assert flattened == [line for line in expected.strip().split("\n") if line.strip(" \t\u200b")]
        assert all("\u200b" not in line for line in flattened)
        expected_blank_lines = sum(not line.strip(" \t\u200b") for line in expected.strip().split("\n"))
        spacer_paragraphs = [paragraph for paragraph in paragraphs if paragraph == [{"tag": "text", "text": "\u00a0"}]]
        assert len(spacer_paragraphs) == expected_blank_lines
        assert "title" not in content["zh_cn"]
