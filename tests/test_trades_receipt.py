from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.notification_format_assertions import assert_mobile_flat_markdown

from src.application.trades.lifecycle_outbox import (
    BATCH_RENDERER_VERSION,
    build_notification_batch_route,
)
from src.application.trades.receipt import (
    build_trade_lifecycle_notification_batch_message,
    build_trade_lifecycle_notification_message,
    build_trade_intake_receipt_message,
    classify_trade_lifecycle_delivery_result,
    decide_trade_intake_receipt,
    send_trade_lifecycle_outbox_payload,
    send_trade_intake_receipt,
)
from src.application.channels.wechat_clawbot.notification import (
    normalize_wechat_clawbot_send_output,
)


def _lifecycle_batch_member(
    index: int,
    *,
    case_id: str | None = None,
    resolution_revision: int = 1,
    transition_type: str = "needs_review",
    account: str = "lx",
    symbol: str | None = None,
) -> dict:
    resolved_case_id = case_id or f"case-{index:02d}"
    payload = {
        "account": account,
        "case_id": resolved_case_id,
        "transition_type": transition_type,
        "symbol": symbol or f"SYM{index:02d}",
        "expiration_ymd": "2026-08-21",
        "strike": 100 + index,
        "option_type": "put",
    }
    return {
        "outbox_id": f"outbox-{index:02d}",
        "case_id": resolved_case_id,
        "transition_type": transition_type,
        "resolution_revision": resolution_revision,
        "delivery_revision": 0,
        "transition_key": (
            f"lifecycle:{resolved_case_id}:{transition_type}"
        ),
        "state_fingerprint": f"state-{index:02d}",
        "payload_hash": f"payload-{index:02d}",
        "created_at_ms": 1_000 + index,
        "payload": payload,
    }


def _lifecycle_batch_payload(
    members: list[dict],
    *,
    target: str = "wechat:ops",
) -> dict:
    route = build_notification_batch_route(
        provider="wechat_clawbot",
        channel="wechat_clawbot",
        target=target,
    )
    return {
        "schema_version": BATCH_RENDERER_VERSION,
        "batch_id": "tlb_0123456789abcdef0123456789abcdef",
        "route": {
            key: route[key]
            for key in (
                "provider",
                "channel",
                "target_fingerprint",
                "route_fingerprint",
            )
        },
        "members": members,
    }


def test_lifecycle_batch_single_member_preserves_existing_message() -> None:
    member = _lifecycle_batch_member(1)

    assert build_trade_lifecycle_notification_batch_message(
        _lifecycle_batch_payload([member])
    ) == build_trade_lifecycle_notification_message(member["payload"])


def test_lifecycle_batch_digest_is_deterministic_and_top_twelve_only() -> None:
    members = [
        _lifecycle_batch_member(
            index,
            account="lx" if index < 15 else "sy",
        )
        for index in range(24)
    ]

    forward = build_trade_lifecycle_notification_batch_message(
        _lifecycle_batch_payload(members)
    )
    reverse = build_trade_lifecycle_notification_batch_message(
        _lifecycle_batch_payload(list(reversed(members)))
    )

    assert forward == reverse
    assert "范围｜24 条意图 · 24 个案件" in forward
    assert "另有 12 个案件未展开" in forward
    assert forward.count("`case-") == 12
    assert "wechat:ops" not in forward


def test_lifecycle_batch_digest_collapses_case_to_latest_revision() -> None:
    older = _lifecycle_batch_member(
        1,
        case_id="case-shared",
        resolution_revision=1,
        symbol="OLD",
    )
    newer = _lifecycle_batch_member(
        2,
        case_id="case-shared",
        resolution_revision=2,
        transition_type="conflict",
        symbol="NEW",
    )
    another = _lifecycle_batch_member(3, symbol="OTHER")

    message = build_trade_lifecycle_notification_batch_message(
        _lifecycle_batch_payload([older, another, newer])
    )

    assert "范围｜3 条意图 · 2 个案件" in message
    assert "NEW" in message
    assert "OLD" not in message
    assert message.index("NEW") < message.index("OTHER")


def test_lifecycle_batch_single_representative_preserves_message() -> None:
    older = _lifecycle_batch_member(
        1,
        case_id="case-shared",
        resolution_revision=1,
        symbol="OLD",
    )
    newer = _lifecycle_batch_member(
        2,
        case_id="case-shared",
        resolution_revision=2,
        transition_type="conflict",
        symbol="NEW",
    )

    assert build_trade_lifecycle_notification_batch_message(
        _lifecycle_batch_payload([older, newer])
    ) == build_trade_lifecycle_notification_message(newer["payload"])


def test_lifecycle_batch_route_mismatch_fails_before_send(
    tmp_path: Path,
) -> None:
    calls: list[dict] = []
    payload = _lifecycle_batch_payload(
        [_lifecycle_batch_member(1)],
        target="wechat:old",
    )

    result = send_trade_lifecycle_outbox_payload(
        base=tmp_path,
        config={
            "notifications": {
                "provider": "wechat_clawbot",
                "target": "wechat:new",
            }
        },
        receipt_config={},
        payload=payload,
        send_fn=lambda **kwargs: calls.append(dict(kwargs)),
        normalize_fn=lambda send_result: send_result,
    )

    assert result["status"] == "explicit_failed"
    assert result["explicit_pre_acceptance_failure"] is True
    assert result["classification_evidence"]["preflight"] == (
        "route_fingerprint_mismatch"
    )
    assert calls == []
    assert "wechat:old" not in str(result)
    assert "wechat:new" not in str(result)


def test_lifecycle_batch_sender_reuses_batch_idempotency_key(
    tmp_path: Path,
) -> None:
    calls: list[dict] = []
    payload = _lifecycle_batch_payload(
        [_lifecycle_batch_member(1), _lifecycle_batch_member(2)]
    )

    def _send(**kwargs):
        calls.append(dict(kwargs))
        return {
            "command_ok": True,
            "delivery_confirmed": True,
            "message_id": f"message-{len(calls)}",
            "idempotency_key": kwargs["idempotency_key"],
        }

    for _attempt in range(2):
        result = send_trade_lifecycle_outbox_payload(
            base=tmp_path,
            config={
                "notifications": {
                    "provider": "wechat_clawbot",
                    "target": "wechat:ops",
                }
            },
            receipt_config={},
            payload=payload,
            send_fn=_send,
            normalize_fn=lambda send_result: send_result,
        )
        assert result["status"] == "confirmed"

    assert [call["idempotency_key"] for call in calls] == [
        payload["batch_id"],
        payload["batch_id"],
    ]
    assert all("期权平仓批次" in call["message"] for call in calls)


@pytest.mark.parametrize(
    ("normalized", "expected"),
    (
        (
            {
                "command_ok": False,
                "delivery_confirmed": False,
                "http_status": 400,
                "provider_response_code": 230001,
            },
            "explicit_failed",
        ),
        (
            {
                "command_ok": True,
                "delivery_confirmed": False,
                "http_status": 200,
                "provider_response_code": 230001,
            },
            "explicit_failed",
        ),
        (
            {
                "command_ok": False,
                "delivery_confirmed": False,
                "http_status": 500,
                "provider_response_code": 999,
            },
            "unknown",
        ),
        (
            {
                "command_ok": False,
                "delivery_confirmed": False,
                "http_status": 400,
                "ambiguous_send": True,
            },
            "unknown",
        ),
        (
            {
                "command_ok": True,
                "delivery_confirmed": False,
                "fallback_used": True,
            },
            "unknown",
        ),
        (
            {
                "command_ok": True,
                "delivery_confirmed": False,
            },
            "accepted",
        ),
    ),
)
def test_lifecycle_delivery_classifier_is_fail_closed(
    normalized: dict,
    expected: str,
) -> None:
    assert classify_trade_lifecycle_delivery_result(normalized)[
        "outcome"
    ] == expected


def test_lifecycle_classifier_retries_real_wechat_business_rejection() -> None:
    normalized = normalize_wechat_clawbot_send_output(
        send_result={
            "ok": False,
            "http_status": 200,
            "response_json": {"code": 230001, "message": "rejected"},
            "response_tail": '{"code":230001}',
        }
    )

    assert normalized["command_ok"] is True
    assert normalized["delivery_confirmed"] is False
    assert normalized["provider_response_code"] == 230001
    assert classify_trade_lifecycle_delivery_result(normalized)[
        "outcome"
    ] == "explicit_failed"


def test_receipt_decision_defaults_send_applied() -> None:
    out = decide_trade_intake_receipt(
        receipt_config={},
        apply_changes=True,
        state={},
        deal_id="deal-1",
        result={"status": "applied", "reason": "applied_open"},
    )

    assert out == {"should_send": True, "reason": "applied"}


def test_receipt_decision_defaults_send_unresolved_and_failed() -> None:
    for status in ("unresolved", "failed"):
        out = decide_trade_intake_receipt(
            receipt_config={},
            apply_changes=True,
            state={},
            deal_id="deal-1",
            result={"status": status, "reason": status},
        )

        assert out == {"should_send": True, "reason": status}


def test_receipt_decision_skips_repeated_confirmed_unresolved() -> None:
    out = decide_trade_intake_receipt(
        receipt_config={},
        apply_changes=True,
        state={
            "unresolved_deal_ids": {
                "deal-1": {
                    "status": "unresolved",
                    "receipt": {"status": "sent", "delivery_confirmed": True},
                }
            }
        },
        deal_id="deal-1",
        result={"status": "unresolved", "reason": "waiting_settlement_evidence"},
    )

    assert out == {"should_send": False, "reason": "skipped_unresolved_already_notified"}


def test_receipt_decision_retries_unconfirmed_unresolved() -> None:
    out = decide_trade_intake_receipt(
        receipt_config={},
        apply_changes=True,
        state={
            "unresolved_deal_ids": {
                "deal-1": {
                    "status": "unresolved",
                    "receipt": {"status": "failed", "delivery_confirmed": False},
                }
            }
        },
        deal_id="deal-1",
        result={"status": "unresolved", "reason": "waiting_settlement_evidence"},
    )

    assert out == {"should_send": True, "reason": "unresolved_retry_unconfirmed_receipt"}


def test_receipt_decision_skips_dry_run() -> None:
    out = decide_trade_intake_receipt(
        receipt_config={},
        apply_changes=False,
        state={},
        deal_id="deal-1",
        result={"status": "dry_run", "reason": "preview_open"},
    )

    assert out == {"should_send": False, "reason": "skipped_dry_run"}


def test_receipt_decision_skips_confirmed_duplicate_by_default() -> None:
    out = decide_trade_intake_receipt(
        receipt_config={},
        apply_changes=True,
        state={
            "processed_deal_ids": {
                "deal-1": {
                    "status": "applied",
                    "receipt": {"status": "sent", "delivery_confirmed": True},
                }
            }
        },
        deal_id="deal-1",
        result={"status": "skipped", "reason": "duplicate_deal_id"},
    )

    assert out == {"should_send": False, "reason": "skipped_duplicate"}


def test_receipt_decision_retries_explicit_failed_duplicate() -> None:
    out = decide_trade_intake_receipt(
        receipt_config={},
        apply_changes=True,
        state={
            "processed_deal_ids": {
                "deal-1": {
                    "status": "applied",
                    "receipt": {"status": "failed", "delivery_confirmed": False},
                }
            }
        },
        deal_id="deal-1",
        result={"status": "skipped", "reason": "duplicate_deal_id"},
    )

    assert out == {"should_send": True, "reason": "duplicate_retry_unconfirmed_receipt"}


def test_trade_receipt_does_not_resend_provider_unconfirmed_duplicate(
    tmp_path: Path,
) -> None:
    calls: list[dict] = []
    deal = SimpleNamespace(
        deal_id="deal-1",
        internal_account="lx",
        futu_account_id="REAL_1",
        position_effect="open",
        side="sell",
        symbol="NVDA",
        option_type="put",
        expiration_ymd="2026-06-19",
        strike=120,
        contracts=1,
        price=1.23,
        multiplier=100,
        currency="USD",
        trade_time_ms=1779167311000,
    )

    def _send(**kwargs):
        calls.append(dict(kwargs))
        return {
            "command_ok": True,
            "delivery_confirmed": False,
            "message_id": None,
            "returncode": 0,
        }

    first = send_trade_intake_receipt(
        base=tmp_path,
        config={
            "notifications": {
                "provider": "wechat_clawbot",
                "target": "wechat:ops",
            }
        },
        receipt_config={},
        apply_changes=True,
        state={},
        deal=deal,
        result={
            "status": "applied",
            "reason": "applied_open",
            "deal_id": "deal-1",
            "account": "lx",
            "action": "open",
        },
        payload={},
        send_fn=_send,
        normalize_fn=lambda send_result: send_result,
    )
    assert first["status"] == "unconfirmed"

    duplicate = send_trade_intake_receipt(
        base=tmp_path,
        config={
            "notifications": {
                "provider": "wechat_clawbot",
                "target": "wechat:ops",
            }
        },
        receipt_config={},
        apply_changes=True,
        state={
            "processed_deal_ids": {
                "futu:lx:REAL_1:deal-1": {
                    "status": "applied",
                    "receipt": first,
                }
            }
        },
        deal=deal,
        result={
            "status": "skipped",
            "reason": "duplicate_deal_id",
            "deal_id": "deal-1",
            "account": "lx",
        },
        payload={},
        send_fn=_send,
        normalize_fn=lambda send_result: send_result,
    )

    assert duplicate["status"] == "skipped"
    assert duplicate["reason"] == "skipped_duplicate_delivery_unknown"
    assert len(calls) == 1


def test_receipt_decision_skips_non_option_deal() -> None:
    out = decide_trade_intake_receipt(
        receipt_config={},
        apply_changes=True,
        state={},
        deal_id="deal-stock-1",
        result={"status": "skipped", "reason": "not_option_deal"},
    )

    assert out == {"should_send": False, "reason": "skipped_not_option_deal"}


def test_send_trade_intake_receipt_skips_without_route(tmp_path: Path) -> None:
    out = send_trade_intake_receipt(
        base=tmp_path,
        config={"notifications": {"provider": "wechat_clawbot"}},
        receipt_config={},
        apply_changes=True,
        state={},
        deal=None,
        result={"status": "applied", "reason": "applied_open", "deal_id": "deal-1"},
        payload={"deal_id": "deal-1"},
    )

    assert out["status"] == "skipped"
    assert out["reason"] == "skipped_no_route"


def test_send_trade_intake_receipt_uses_existing_route_and_sender(tmp_path: Path) -> None:
    calls: list[dict] = []
    deal = SimpleNamespace(
        deal_id="deal-1",
        internal_account="lx",
        position_effect="open",
        side="sell",
        symbol="NVDA",
        option_type="put",
        expiration_ymd="2026-06-19",
        strike=120,
        contracts=1,
        price=1.23,
        multiplier=100,
        currency="USD",
        trade_time_ms=1779167311000,
    )

    def _send(**kwargs):
        calls.append(dict(kwargs))
        return {"command_ok": True, "delivery_confirmed": True, "message_id": "msg-1", "returncode": 0}

    out = send_trade_intake_receipt(
        base=tmp_path,
        config={"notifications": {"provider": "wechat_clawbot", "target": "wechat:ops"}},
        receipt_config={},
        apply_changes=True,
        state={},
        deal=deal,
        result={"status": "applied", "reason": "applied_open", "deal_id": "deal-1", "account": "lx", "action": "open"},
        payload={},
        send_fn=_send,
        normalize_fn=lambda send_result: send_result,
    )

    assert out["status"] == "sent"
    assert out["delivery_confirmed"] is True
    assert out["message_id"] == "msg-1"
    assert calls[0]["target"] == "wechat:ops"
    assert calls[0]["message"].startswith("# OM · 回执 · lx")
    assert "类型｜成交" in calls[0]["message"]
    assert "状态｜✅ 已完成" in calls[0]["message"]
    assert "资金｜权利金毛流入 USD 123.00" in calls[0]["message"]
    assert "时间｜2026-05-19 13:08:31 北京时间" in calls[0]["message"]
    assert "编号｜`deal-1`" in calls[0]["message"]
    assert_mobile_flat_markdown(calls[0]["message"])


def test_send_trade_intake_receipt_uses_feishu_bot_target(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OM_FEISHU_BOT_USER_OPEN_ID", "ou_bot")
    calls: list[dict] = []

    def _send(**kwargs):
        calls.append(dict(kwargs))
        return {"command_ok": True, "delivery_confirmed": True, "message_id": "msg-1", "returncode": 0}

    out = send_trade_intake_receipt(
        base=tmp_path,
        config={"notifications": {"provider": "feishu_app"}},
        receipt_config={},
        apply_changes=True,
        state={},
        deal=None,
        result={"status": "applied", "reason": "applied_open", "deal_id": "deal-1", "account": "lx", "action": "open"},
        payload={"deal_id": "deal-1"},
        send_fn=_send,
        normalize_fn=lambda send_result: send_result,
    )

    assert out["status"] == "sent"
    assert calls[0]["target"] == "ou_bot"
    assert calls[0]["notifications"] == {"provider": "feishu_app"}


def test_build_trade_intake_receipt_message_marks_unresolved() -> None:
    msg = build_trade_intake_receipt_message(
        deal=None,
        result={
            "status": "unresolved",
            "reason": "missing_required_fields:multiplier",
            "deal_id": "deal-1",
            "account": "lx",
            "action": "open",
        },
        payload={"symbol": "9992.HK", "qty": 1, "price": 6.3},
    )

    assert "状态｜❌ 未记录" in msg
    assert msg.startswith("# OM · 回执 · lx")
    assert "类型｜成交" in msg
    assert "missing_required_fields:multiplier" in msg
    assert "9992.HK" in msg


def test_build_trade_intake_receipt_message_marks_ambiguous_assigned_stock_sale_as_pending_confirmation() -> None:
    msg = build_trade_intake_receipt_message(
        deal=None,
        result={
            "status": "unresolved",
            "reason": "ambiguous_assigned_stock_sale",
            "deal_id": "deal-stock-1",
            "account": "lx",
            "action": "assigned_stock_sale",
            "diagnostics": {
                "candidates": [
                    {
                        "stock_lot_id": "assigned-stock-a",
                        "symbol": "FUTU",
                        "currency": "USD",
                        "shares_remaining": 100,
                        "stock_cost_per_share": 120.0,
                        "opened_at_ms": 1779167311000,
                        "reject_reasons": [],
                    },
                    {
                        "stock_lot_id": "assigned-stock-b",
                        "symbol": "FUTU",
                        "currency": "USD",
                        "shares_remaining": 100,
                        "stock_cost_per_share": 117.45,
                        "opened_at_ms": 1779253711000,
                        "reject_reasons": [],
                    },
                ]
            },
        },
        payload={"symbol": "FUTU", "qty": 100, "price": 100.0},
    )

    assert "状态｜⚠️ 待确认" in msg
    assert msg.startswith("# OM · 回执 · lx")
    assert "类型｜成交" in msg
    assert "确认前不会自动写入" in msg
    assert "A｜FUTU USD；剩余 100 股；成本 120.0/股" in msg
    assert "B｜FUTU USD；剩余 100 股；成本 117.45/股" in msg
    assert "下一步｜回复“选择 A”" in msg
    assert msg.index("下一步｜回复“选择 A”") < msg.index("编号｜`deal-stock-1`")
    assert "状态｜❌ 未记录" not in msg
    assert_mobile_flat_markdown(msg)


def test_build_trade_intake_receipt_message_marks_projection_verification_failure() -> None:
    msg = build_trade_intake_receipt_message(
        deal=None,
        result={
            "status": "failed",
            "reason": "projection_verification_failed",
            "deal_id": "deal-1",
            "account": "lx",
            "action": "close",
        },
        payload={"symbol": "0700.HK", "qty": 2, "price": 1.2},
    )

    assert "状态｜❌ 写入异常" in msg
    assert "状态｜✅ 已完成" not in msg
    assert "projection_verification_failed" in msg


def test_build_trade_intake_receipt_message_marks_staggered_combo_relation_pending() -> None:
    msg = build_trade_intake_receipt_message(
        deal=None,
        result={
            "status": "applied",
            "reason": "applied_open",
            "deal_id": "deal-staggered-call-1",
            "account": "lx",
            "action": "open",
            "diagnostics": {
                "combo_yield_enrichment": {
                    "structure_mode": "staggered_expiry_pair",
                    "pair_intent_id": None,
                    "combination_relation_pending": True,
                }
            },
        },
        payload={
            "symbol": "PDD",
            "option_type": "call",
            "side": "buy",
            "expiration_ymd": "2026-10-16",
            "strike": 100,
            "qty": 1,
            "price": 0.73,
            "multiplier": 100,
            "currency": "USD",
        },
    )

    assert "状态｜✅ 已完成" in msg
    assert "资金｜权利金毛流出 USD 73.00" in msg
    assert "组合｜关系待确认" in msg
    assert "未自动归入 Combo Yield 组" in msg


def test_trade_receipt_preserves_normalized_feishu_size_error(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OM_FEISHU_BOT_USER_OPEN_ID", "ou_bot")
    out = send_trade_intake_receipt(
        base=tmp_path,
        config={"notifications": {"provider": "feishu_app"}},
        receipt_config={},
        apply_changes=True,
        state={},
        deal=None,
        result={"status": "applied", "reason": "applied_open", "deal_id": "deal-1", "account": "lx"},
        payload={"deal_id": "deal-1"},
        send_fn=lambda **_kwargs: {
            "command_ok": False,
            "delivery_confirmed": False,
            "returncode": 1,
            "error_code": "FEISHU_POST_TOO_LARGE",
        },
        normalize_fn=lambda send_result: send_result,
    )

    assert out["status"] == "failed"
    assert out["delivery_confirmed"] is False
    assert out["error_code"] == "FEISHU_POST_TOO_LARGE"
