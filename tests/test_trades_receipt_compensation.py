from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from src.application.trades.receipt_compensation import (
    LEGACY_FALSE_OUTBOX_REASON,
    compensate_trade_intake_receipts,
)


ACCOUNT = "lx"
FUTU_ACCOUNT_ID = "100000000000000001"
DEAL_IDS = (
    f"futu:{ACCOUNT}:{FUTU_ACCOUNT_ID}:2000000000000000001",
    f"futu:{ACCOUNT}:{FUTU_ACCOUNT_ID}:2000000000000000002",
)


class _Repo:
    def __init__(self, events: list[dict[str, Any]]) -> None:
        self.events = [dict(item) for item in events]

    def list_trade_events(self) -> list[dict[str, Any]]:
        return [dict(item) for item in self.events]


def _event(deal_id: str, *, trade_time_ms: int) -> dict[str, Any]:
    source_deal_id = deal_id.rsplit(":", 1)[-1]
    return {
        "event_id": deal_id,
        "trade_time_ms": trade_time_ms,
        "source_type": "broker_trade_event",
        "source_name": "opend_push",
        "broker": "富途",
        "account": ACCOUNT,
        "symbol": "0700.HK",
        "option_type": "put",
        "side": "sell",
        "position_effect": "open",
        "contracts": 1,
        "price": 6.38,
        "strike": 430.0,
        "multiplier": 100,
        "expiration_ymd": "2026-09-29",
        "currency": "HKD",
        "raw_payload": {
            "external_event_key": deal_id,
            "source_deal_id": source_deal_id,
            "futu_account_id": FUTU_ACCOUNT_ID,
        },
    }


def _state_row(deal_id: str) -> dict[str, Any]:
    return {
        "status": "applied",
        "action": "open",
        "account": ACCOUNT,
        "source_deal_id": deal_id.rsplit(":", 1)[-1],
        "futu_account_id": FUTU_ACCOUNT_ID,
        "broker_deal_key": deal_id,
        "reason": "applied_open",
        "receipt": {
            "enabled": True,
            "status": "outbox_managed",
            "reason": "transactional_outbox",
            "delivery_confirmed": False,
            "message_id": None,
            "attempt_count": 1,
        },
    }


def _fixture(tmp_path: Path) -> tuple[list[dict[str, Any]], _Repo]:
    state_path = tmp_path / "trade_intake" / ACCOUNT / "state.json"
    audit_path = state_path.with_name("audit.jsonl")
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {
                "processed_deal_ids": {
                    deal_id: _state_row(deal_id) for deal_id in DEAL_IDS
                },
                "failed_deal_ids": {},
                "unresolved_deal_ids": {},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    source = {
        "id": ACCOUNT,
        "account": ACCOUNT,
        "state_path": state_path,
        "audit_path": audit_path,
        "receipt": {"enabled": True},
    }
    repo = _Repo(
        [
            _event(DEAL_IDS[0], trade_time_ms=1785735451733),
            _event(DEAL_IDS[1], trade_time_ms=1785735488447),
        ]
    )
    return [source], repo


def _route(**_kwargs: Any) -> dict[str, Any]:
    return {
        "provider": "wechat_clawbot",
        "channel": "wechat_clawbot",
        "target": "wechat:ops",
        "notifications": {"provider": "wechat_clawbot"},
    }


def _run(
    tmp_path: Path,
    *,
    apply_changes: bool,
    send_fn: Any = None,
) -> dict[str, Any]:
    sources, repo = _fixture(tmp_path)
    kwargs = {
        "base": tmp_path,
        "config": {},
        "sources": sources,
        "repo": repo,
        "account": ACCOUNT,
        "deal_ids": list(DEAL_IDS),
        "reason": LEGACY_FALSE_OUTBOX_REASON,
        "send_fn": send_fn,
        "normalize_fn": (lambda send_result: send_result),
        "route_resolver": _route,
        "now_fn": (lambda: "2026-08-04T10:00:00+00:00"),
    }
    if apply_changes:
        preview = compensate_trade_intake_receipts(
            **kwargs,
            apply_changes=False,
        )
        return compensate_trade_intake_receipts(
            **kwargs,
            apply_changes=True,
            expected_payload_hash=preview["payload_hash"],
        )
    return compensate_trade_intake_receipts(
        **kwargs,
        apply_changes=False,
    )


def test_receipt_compensation_preview_combines_two_ledger_trades_without_writes(
    tmp_path: Path,
) -> None:
    out = _run(tmp_path, apply_changes=False)

    assert out["ok"] is True
    assert out["status"] == "ready"
    assert out["dry_run"] is True
    assert out["write_applied"] is False
    assert out["deal_ids"] == sorted(DEAL_IDS)
    assert len(out["members"]) == 2
    assert out["route"]["provider"] == "wechat_clawbot"
    assert "target" not in out["route"]
    assert "类型｜历史成交补充" in out["message"]
    assert "状态｜✅ 已入账" in out["message"]
    assert "动作｜Sell Put 开仓" in out["message"]
    assert "标的｜0700.HK" in out["message"]
    assert "合约｜2026-09-29 430 Put" in out["message"]
    assert "数量｜2 笔 · 2 张" in out["message"]
    assert "资金｜权利金毛流入 HKD 1,276.00" in out["message"]
    assert "本消息仅补充历史回执，不会重复记账" in out["message"]
    assert not Path(out["record_path"]).exists()
    assert not Path(out["audit_path"]).exists()


def test_receipt_compensation_sends_once_and_suppresses_confirmed_duplicate(
    tmp_path: Path,
) -> None:
    calls: list[dict[str, Any]] = []

    def _send(**kwargs: Any) -> dict[str, Any]:
        calls.append(dict(kwargs))
        return {
            "command_ok": True,
            "delivery_confirmed": True,
            "message_id": "om_msg_1",
            "returncode": 0,
            "idempotency_key": kwargs["idempotency_key"],
        }

    first = _run(tmp_path, apply_changes=True, send_fn=_send)

    assert first["ok"] is True
    assert first["status"] == "confirmed"
    assert first["delivery_confirmed"] is True
    assert first["message_id"] == "om_msg_1"
    assert len(calls) == 1

    preview_after_send = _run(tmp_path, apply_changes=False)
    assert preview_after_send["status"] == "duplicate_suppressed"
    assert preview_after_send["dry_run"] is True
    assert preview_after_send["suppression_reason"] == "already_confirmed"
    assert calls[0]["idempotency_key"] == first["transport_idempotency_key"]
    record = json.loads(Path(first["record_path"]).read_text(encoding="utf-8"))
    assert record["status"] == "confirmed"
    assert record["message_id"] == "om_msg_1"
    assert record["attempt_count"] == 1
    audit_rows = [
        json.loads(line)
        for line in Path(first["audit_path"]).read_text(encoding="utf-8").splitlines()
    ]
    assert [item["phase"] for item in audit_rows] == [
        "receipt_compensation_prepared",
        "receipt_compensation_confirmed",
    ]
    assert audit_rows[-1]["deal_ids"] == sorted(DEAL_IDS)

    second = _run(tmp_path, apply_changes=True, send_fn=_send)

    assert second["ok"] is True
    assert second["status"] == "duplicate_suppressed"
    assert second["prior_status"] == "confirmed"
    assert second["suppression_reason"] == "already_confirmed"
    assert len(calls) == 1


def test_receipt_compensation_freezes_unconfirmed_delivery_without_retry(
    tmp_path: Path,
) -> None:
    calls = 0

    def _send(**_kwargs: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return {
            "command_ok": True,
            "delivery_confirmed": False,
            "message_id": None,
            "ambiguous_send": True,
            "returncode": 0,
        }

    first = _run(tmp_path, apply_changes=True, send_fn=_send)
    second = _run(tmp_path, apply_changes=True, send_fn=_send)

    assert first["ok"] is False
    assert first["status"] == "unknown"
    assert first["delivery_confirmed"] is False
    assert second["ok"] is False
    assert second["status"] == "duplicate_suppressed"
    assert second["prior_status"] == "unknown"
    assert second["suppression_reason"] == (
        "existing_nonterminal_or_unconfirmed_compensation"
    )
    assert calls == 1


def test_receipt_compensation_rejects_real_outbox_evidence(
    tmp_path: Path,
) -> None:
    sources, repo = _fixture(tmp_path)
    state_path = Path(sources[0]["state_path"])
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["processed_deal_ids"][DEAL_IDS[0]]["receipt"].update(
        {
            "outbox_id": "outbox-1",
            "outbox_readback_confirmed": True,
        }
    )
    state_path.write_text(json.dumps(state), encoding="utf-8")

    with pytest.raises(ValueError, match="durable outbox evidence"):
        compensate_trade_intake_receipts(
            base=tmp_path,
            config={},
            sources=sources,
            repo=repo,
            account=ACCOUNT,
            deal_ids=list(DEAL_IDS),
            apply_changes=False,
            route_resolver=_route,
        )


def test_receipt_compensation_requires_canonical_account_scoped_ids(
    tmp_path: Path,
) -> None:
    sources, repo = _fixture(tmp_path)

    with pytest.raises(ValueError, match="canonical IDs"):
        compensate_trade_intake_receipts(
            base=tmp_path,
            config={},
            sources=sources,
            repo=repo,
            account=ACCOUNT,
            deal_ids=["2000000000000000001"],
            apply_changes=False,
            route_resolver=_route,
        )


def test_receipt_compensation_apply_requires_matching_dry_run_hash(
    tmp_path: Path,
) -> None:
    sources, repo = _fixture(tmp_path)

    with pytest.raises(ValueError, match="payload_hash from dry-run"):
        compensate_trade_intake_receipts(
            base=tmp_path,
            config={},
            sources=sources,
            repo=repo,
            account=ACCOUNT,
            deal_ids=list(DEAL_IDS),
            apply_changes=True,
            route_resolver=_route,
        )

    with pytest.raises(ValueError, match="payload changed after dry-run"):
        compensate_trade_intake_receipts(
            base=tmp_path,
            config={},
            sources=sources,
            repo=repo,
            account=ACCOUNT,
            deal_ids=list(DEAL_IDS),
            apply_changes=True,
            expected_payload_hash="0" * 64,
            route_resolver=_route,
        )
