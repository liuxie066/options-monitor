from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from tests.notification_format_assertions import assert_mobile_flat_markdown

from src.application.channels.wechat_clawbot import notification as wechat_notification
from src.application.positions import maintenance_receipt
from src.application.positions.maintenance_receipt import (
    build_auto_close_receipt_message,
    build_auto_close_receipt_identity,
    decide_auto_close_receipt,
    persist_auto_close_receipt_state,
    resolve_auto_close_receipt_config,
    send_auto_close_receipt,
)


def _receipt_result(**overrides) -> dict:
    values = {
        "mode": "applied",
        "account": "lx",
        "broker": "富途",
        "grace_days": 1,
        "as_of_utc": "2026-05-15T16:10:00+00:00",
        "applied_closed": 1,
        "candidates_should_close": 1,
        "errors": [],
        "applied": [{"record_id": "rec_1", "position_key": "pos_1"}],
    }
    return {**values, **overrides}


def _minimal_result() -> dict:
    return {"mode": "applied", "account": "lx", "applied_closed": 1, "candidates_should_close": 1, "errors": []}


def _sender(calls: list[dict], message_id: str):
    def _send(**kwargs):
        calls.append(dict(kwargs))
        return {"command_ok": True, "delivery_confirmed": True, "message_id": message_id, "returncode": 0}
    return _send


def test_auto_close_receipt_decision_defaults_send_applied_and_failed() -> None:
    applied = decide_auto_close_receipt(
        receipt_config={"notifications": {"enabled": True}},
        dry_run=False,
        result={"mode": "applied", "applied_closed": 1, "candidates_should_close": 1, "errors": []},
    )
    failed = decide_auto_close_receipt(
        receipt_config={"notifications": {"enabled": True}},
        dry_run=False,
        result={"mode": "applied", "applied_closed": 0, "candidates_should_close": 1, "errors": ["boom"]},
    )
    partial = decide_auto_close_receipt(
        receipt_config={"notifications": {"enabled": True}},
        dry_run=False,
        result={"mode": "applied", "applied_closed": 1, "candidates_should_close": 2, "errors": ["boom"]},
    )

    assert applied == {"should_send": True, "reason": "applied"}
    assert failed == {"should_send": True, "reason": "failed"}
    assert partial == {"should_send": True, "reason": "partial_failed"}


def test_auto_close_receipt_decision_skips_dry_run_and_noop_by_default() -> None:
    dry_run = decide_auto_close_receipt(
        receipt_config={"notifications": {"enabled": True}},
        dry_run=True,
        result={"mode": "dry_run", "applied_closed": 0, "candidates_should_close": 1, "errors": []},
    )
    noop = decide_auto_close_receipt(
        receipt_config={"notifications": {"enabled": True}},
        dry_run=False,
        result={"mode": "applied", "applied_closed": 0, "candidates_should_close": 0, "errors": []},
    )

    assert dry_run == {"should_send": False, "reason": "dry_run"}
    assert noop == {"should_send": False, "reason": "noop"}


def test_auto_close_receipt_config_rejects_non_boolean_flag() -> None:
    with pytest.raises(ValueError, match="option_positions.auto_close.receipt.enabled must be a boolean"):
        resolve_auto_close_receipt_config({"enabled": "yes"})


def test_auto_close_receipt_config_defaults_retry_unconfirmed() -> None:
    assert resolve_auto_close_receipt_config({})["retry_unconfirmed"] is True


def test_auto_close_receipt_decision_skips_confirmed_duplicate() -> None:
    identity = build_auto_close_receipt_identity(
        config={"schedule": {"timezone": "Asia/Hong_Kong"}},
        result=_receipt_result(),
    )

    out = decide_auto_close_receipt(
        receipt_config={"notifications": {"enabled": True}},
        dry_run=False,
        result={"mode": "applied", "applied_closed": 1, "candidates_should_close": 1, "errors": []},
        prior_receipt={"status": "sent", "delivery_confirmed": True, "attempt_count": 1},
        receipt_key=identity["receipt_key"],
    )

    assert out == {"should_send": False, "reason": "skipped_duplicate_confirmed"}


def test_auto_close_receipt_decision_retries_unconfirmed_duplicate() -> None:
    out = decide_auto_close_receipt(
        receipt_config={"notifications": {"enabled": True}},
        dry_run=False,
        result={"mode": "applied", "applied_closed": 1, "candidates_should_close": 1, "errors": []},
        prior_receipt={"status": "failed", "delivery_confirmed": False, "attempt_count": 2},
        receipt_key="receipt-key-1",
    )

    assert out == {"should_send": True, "reason": "applied_retry_unconfirmed_receipt"}


def test_send_auto_close_receipt_skips_without_route(tmp_path: Path) -> None:
    out = send_auto_close_receipt(
        base=tmp_path,
        config={"notifications": {"enabled": True, "provider": "wechat_clawbot"}},
        receipt_config={"notifications": {"enabled": True}},
        dry_run=False,
        result={"mode": "applied", "applied_closed": 1, "candidates_should_close": 1, "errors": []},
    )

    assert out["status"] == "skipped"
    assert out["reason"] == "skipped_no_route"


def test_send_auto_close_receipt_uses_existing_route_and_sender(tmp_path: Path) -> None:
    calls: list[dict] = []
    _send = _sender(calls, "msg-auto-1")

    out = send_auto_close_receipt(
        base=tmp_path,
        config={"notifications": {"enabled": True, "provider": "wechat_clawbot", "target": "wechat:ops"}},
        receipt_config={"notifications": {"enabled": True}},
        dry_run=False,
        result=_receipt_result(
            as_of_utc="2026-10-18T01:07:28+00:00",
            applied_closed=3,
            candidates_should_close=3,
            applied=[
                {
                    "record_id": f"lot_futu:lx:fixture:{index}",
                    "position_key": "富途|lx|FUTU|2026-10-16|100P|short",
                    "expiration_ymd": "2026-10-16",
                }
                for index in range(3)
            ],
        ),
        send_fn=_send,
        normalize_fn=lambda send_result: send_result,
    )

    assert out["status"] == "sent"
    assert out["delivery_confirmed"] is True
    assert out["message_id"] == "msg-auto-1"
    assert calls[0]["target"] == "wechat:ops"
    assert calls[0]["message"] == "\n".join([
        "# OM · 回执 · lx",
        "",
        "类型｜持仓维护",
        "状态｜✅ 已完成",
        "券商｜富途",
        "规则｜到期后 1 天",
        "结果｜成功 3 · 候选 3 · 错误 0",
        "时间｜2026-10-18 09:07:28 北京时间",
        "",
        "## 已完成",
        "富途|lx|FUTU|2026-10-16|100P|short",
        "富途|lx|FUTU|2026-10-16|100P|short",
        "富途|lx|FUTU|2026-10-16|100P|short",
    ])
    assert_mobile_flat_markdown(calls[0]["message"])
    assert out["attempt_count"] == 1


def test_send_auto_close_receipt_uses_feishu_bot_target(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OM_FEISHU_BOT_USER_OPEN_ID", "ou_bot")
    calls: list[dict] = []
    _send = _sender(calls, "msg-auto-1")

    out = send_auto_close_receipt(
        base=tmp_path,
        config={"notifications": {"enabled": True, "provider": "feishu_app"}},
        receipt_config={"notifications": {"enabled": True}},
        dry_run=False,
        result=_minimal_result(),
        send_fn=_send,
        normalize_fn=lambda send_result: send_result,
    )

    assert out["status"] == "sent"
    assert calls[0]["target"] == "ou_bot"
    assert calls[0]["notifications"] == {"enabled": True, "provider": "feishu_app"}


def test_send_auto_close_receipt_increments_retry_attempt_count(tmp_path: Path) -> None:
    calls: list[dict] = []
    _send = _sender(calls, "msg-auto-2")

    out = send_auto_close_receipt(
        base=tmp_path,
        config={"notifications": {"enabled": True, "provider": "wechat_clawbot", "target": "wechat:ops"}},
        receipt_config={"notifications": {"enabled": True}},
        dry_run=False,
        result={"mode": "applied", "applied_closed": 1, "candidates_should_close": 1, "errors": []},
        prior_receipt={"status": "failed", "delivery_confirmed": False, "attempt_count": 2},
        receipt_key="receipt-key-1",
        send_fn=_send,
        normalize_fn=lambda send_result: send_result,
    )

    assert calls
    assert out["status"] == "sent"
    assert out["reason"] == "applied_retry_unconfirmed_receipt"
    assert out["attempt_count"] == 3


def test_persist_auto_close_receipt_state_keeps_daily_receipt_by_key(tmp_path: Path) -> None:
    result = {
        "mode": "applied",
        "account": "lx",
        "broker": "富途",
        "as_of_utc": "2026-05-15T16:10:00+00:00",
        "candidates_should_close": 1,
        "applied_closed": 1,
        "errors": [],
        "applied": [{"record_id": "rec_1"}],
    }
    identity = build_auto_close_receipt_identity(config={"schedule": {"timezone": "Asia/Hong_Kong"}}, result=result)
    receipt = {
        "status": "sent",
        "reason": "applied",
        "delivery_confirmed": True,
        "message_id": "msg-auto-1",
        "attempt_count": 1,
        **identity,
    }

    state = persist_auto_close_receipt_state(base=tmp_path, account="lx", result=result, receipt=receipt)

    assert state is not None
    item = state["receipts"][identity["receipt_key"]]
    assert item["receipt"]["message_id"] == "msg-auto-1"
    assert item["receipt_key_fields"]["business_date"] == "2026-05-16"


def test_build_auto_close_receipt_message_marks_partial_failure() -> None:
    msg = build_auto_close_receipt_message(
        dry_run=False,
        result={
            "mode": "applied",
            "account": "lx",
            "broker": "富途",
            "grace_days": 2,
            "applied_closed": 1,
            "candidates_should_close": 2,
            "errors": ["rec_2 pos_2: sqlite locked\n    - retry later"],
            "applied": [{"record_id": "rec_1", "position_key": "pos_1", "expiration_ymd": "2026-05-01"}],
        },
    )

    assert "状态｜⚠️ 部分完成" in msg
    assert msg.startswith("# OM · 回执 · lx")
    assert "类型｜持仓维护" in msg
    assert "结果｜成功 1 · 候选 2 · 错误 1" in msg
    assert "sqlite locked · - retry later" in msg
    assert_mobile_flat_markdown(msg)


@pytest.mark.parametrize(
    ("dry_run", "result", "expected_status"),
    [
        (True, {"mode": "dry_run", "applied_closed": 0, "candidates_should_close": 1}, "ℹ️ 预览"),
        (False, {"mode": "applied", "applied_closed": 0, "candidates_should_close": 1, "errors": ["boom"]}, "❌ 失败"),
        (False, {"mode": "applied", "applied_closed": 0, "candidates_should_close": 0, "errors": []}, "ℹ️ 无变更"),
    ],
)
def test_build_auto_close_receipt_message_preserves_status_variants(
    dry_run: bool,
    result: dict[str, object],
    expected_status: str,
) -> None:
    msg = build_auto_close_receipt_message(
        dry_run=dry_run,
        result={"account": "lx", "broker": "富途", "grace_days": 2, **result},
    )

    assert msg.startswith("# OM · 回执 · lx")
    assert "类型｜持仓维护" in msg
    assert f"状态｜{expected_status}" in msg
    assert_mobile_flat_markdown(msg)


def test_auto_close_receipt_preserves_normalized_feishu_size_error(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OM_FEISHU_BOT_USER_OPEN_ID", "ou_bot")
    out = send_auto_close_receipt(
        base=tmp_path,
        config={"notifications": {"enabled": True, "provider": "feishu_app"}},
        receipt_config={"notifications": {"enabled": True}},
        dry_run=False,
        result=_minimal_result(),
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


def _safe_receipt_fixture(monkeypatch, *, confirmed_outcomes=(True,)):
    calls: list[dict] = []
    outcomes = iter(confirmed_outcomes)

    def send(**kwargs):
        calls.append(dict(kwargs))
        return {
            "ok": True,
            "http_status": 200,
            "provider_response_code": 0,
            "message_id": f"synthetic-receipt-{len(calls)}" if next(outcomes) else None,
        }

    monkeypatch.setattr(wechat_notification, "send_wechat_clawbot_message", send)
    cfg = {
        "notifications": {"enabled": True, "provider": "wechat_clawbot", "target": "wechat:fixture"},
        "schedule": {"timezone": "UTC"},
        "option_positions": {"auto_close": {"receipt": {}}},
    }
    result = _receipt_result(account="fixture", market_filter="US", applied_closed=0,
                             errors=["synthetic maintenance failure"], applied=[])
    return cfg, result, calls


def _disabled_receipt_policy(cfg, policy):
    disabled = deepcopy(cfg)
    if policy == "notifications":
        disabled["notifications"]["enabled"] = False
    else:
        disabled["option_positions"]["auto_close"]["receipt"][policy] = False
    return disabled


def _saved_receipt(base, result, key):
    path = maintenance_receipt._auto_close_receipt_state_path(base=base, account=result["account"])
    return maintenance_receipt._load_auto_close_receipt_state(path)["receipts"][key]


def _assert_confirmed_receipt_preserved(base, result, original, current):
    assert current["receipt"] == original["receipt"]
    assert current["result_summary"] == original["result_summary"]
    assert current["receipt_key"] == original["receipt_key"]
    assert current["receipt_key_fields"] == original["receipt_key_fields"]
    rows = maintenance_receipt.query_maintenance_receipts(
        base=base, account=result["account"], query={"account": result["account"], "market": "US"})
    assert len(rows) == 1
    assert rows[0]["delivery_state"] == "confirmed"
    assert rows[0]["body_provenance"] == "frozen"
    assert rows[0]["receipt_body"] == original["receipt"]["rendered_message"]
    assert rows[0]["business_result"] == original["result_summary"]
    assert rows[0]["market"] == "US"


@pytest.mark.parametrize(("policy", "reason"), [
    ("notifications", "notifications_disabled"), ("enabled", "skipped_disabled"),
    ("notify_failed", "failed"),
])
def test_safe_auto_close_receipt_keeps_confirmation_across_policy_skips(monkeypatch, tmp_path, policy, reason):
    cfg, result, calls = _safe_receipt_fixture(monkeypatch)
    send = maintenance_receipt.safe_send_auto_close_receipt
    first = send(base=tmp_path, config=cfg, dry_run=False, result=result)
    key = first["receipt_key"]
    original = _saved_receipt(tmp_path, result, key)
    assert first["delivery_confirmed"] is True and first["attempt_count"] == 1
    duplicate = send(base=tmp_path, config=cfg, dry_run=False, result=result)
    assert duplicate["reason"] == "skipped_duplicate_confirmed"
    assert len(calls) == 1
    _assert_confirmed_receipt_preserved(tmp_path, result, original, _saved_receipt(tmp_path, result, key))

    skipped = send(base=tmp_path, config=_disabled_receipt_policy(cfg, policy), dry_run=False, result=result)
    assert skipped["receipt_key"] == key
    assert skipped["status"] == "skipped" and skipped["reason"] == reason
    assert skipped["delivery_confirmed"] is False and skipped["message_id"] is None
    assert len(calls) == 1
    stored = _saved_receipt(tmp_path, result, key)
    _assert_confirmed_receipt_preserved(tmp_path, result, original, stored)
    assert stored["last_skip"] == skipped

    restored = send(base=tmp_path, config=cfg, dry_run=False, result=result)
    assert restored["receipt_key"] == key and restored["reason"] == "skipped_duplicate_confirmed"
    assert len(calls) == 1
    stored = _saved_receipt(tmp_path, result, key)
    _assert_confirmed_receipt_preserved(tmp_path, result, original, stored)
    assert stored["last_skip"] == restored


@pytest.mark.parametrize("policy", ["notifications", "enabled", "notify_failed"])
def test_safe_auto_close_receipt_policy_skip_does_not_confirm_prior_unconfirmed(monkeypatch, tmp_path, policy):
    cfg, result, calls = _safe_receipt_fixture(monkeypatch, confirmed_outcomes=(False, True))
    send = maintenance_receipt.safe_send_auto_close_receipt
    first = send(base=tmp_path, config=cfg, dry_run=False, result=result)
    key = first["receipt_key"]
    assert first["status"] == "unconfirmed" and first["delivery_confirmed"] is False
    skipped = send(base=tmp_path, config=_disabled_receipt_policy(cfg, policy), dry_run=False, result=result)
    assert skipped["receipt_key"] == key and skipped["delivery_confirmed"] is False
    assert _saved_receipt(tmp_path, result, key)["receipt"]["delivery_confirmed"] is False
    assert len(calls) == 1
    restored = send(base=tmp_path, config=cfg, dry_run=False, result=result)
    assert restored["receipt_key"] == key and restored["delivery_confirmed"] is True
    assert len(calls) == 2
    assert _saved_receipt(tmp_path, result, key)["receipt"] == restored


def test_persist_auto_close_receipt_preserves_confirmed_on_other_skip(monkeypatch, tmp_path):
    cfg, result, calls = _safe_receipt_fixture(monkeypatch)
    first = maintenance_receipt.safe_send_auto_close_receipt(base=tmp_path, config=cfg, dry_run=False, result=result)
    key = first["receipt_key"]
    original = _saved_receipt(tmp_path, result, key)
    skipped = {"status": "skipped", "reason": "skipped_no_route", "enabled": True,
               "delivery_confirmed": False, "message_id": None,
               "receipt_key": key, "receipt_key_fields": first["receipt_key_fields"]}
    persist_auto_close_receipt_state(base=tmp_path, account=result["account"], result=result, receipt=skipped)
    stored = _saved_receipt(tmp_path, result, key)
    _assert_confirmed_receipt_preserved(tmp_path, result, original, stored)
    assert stored["last_skip"] == skipped and len(calls) == 1
