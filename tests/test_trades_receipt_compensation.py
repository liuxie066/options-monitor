from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from src.application.trades.receipt_compensation import (
    LEGACY_FALSE_OUTBOX_REASON,
    SKIPPED_NO_ROUTE_REASON,
    compensate_trade_intake_receipts,
    receipt_compensation_takeover,
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
    assert "动作｜CSP 开仓" in out["message"]
    assert "标的｜0700.HK" in out["message"]
    assert "合约｜2026-09-29 430 Put" in out["message"]
    assert "数量｜2 笔 · 2 张" in out["message"]
    assert "资金｜权利金毛流入 HKD 1,276.00" in out["message"]
    assert "本消息仅补充历史回执，不会重复记账" in out["message"]
    assert not Path(out["record_path"]).exists()
    assert not Path(out["audit_path"]).exists()


def test_receipt_compensation_formats_float_transport_noise_as_broker_price(
    tmp_path: Path,
) -> None:
    sources, repo = _fixture(tmp_path)
    state_path = Path(sources[0]["state_path"])
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["processed_deal_ids"] = {
        DEAL_IDS[0]: state["processed_deal_ids"][DEAL_IDS[0]]
    }
    state_path.write_text(json.dumps(state), encoding="utf-8")
    repo.events = [repo.events[0]]
    repo.events[0]["price"] = 1.5699999999999998

    out = compensate_trade_intake_receipts(
        base=tmp_path,
        config={},
        sources=sources,
        repo=repo,
        account=ACCOUNT,
        deal_ids=[DEAL_IDS[0]],
        apply_changes=False,
        reason=LEGACY_FALSE_OUTBOX_REASON,
        route_resolver=_route,
    )

    assert "成交｜1.57 HKD" in out["message"]
    assert "权利金毛流入 HKD 157.00" in out["message"]
    assert "1.5699999999999998" not in out["message"]


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


def test_receipt_compensation_accepts_explicit_unsent_no_route_marker(
    tmp_path: Path,
) -> None:
    sources, repo = _fixture(tmp_path)
    state_path = Path(sources[0]["state_path"])
    state = json.loads(state_path.read_text(encoding="utf-8"))
    for deal_id in DEAL_IDS:
        state["processed_deal_ids"][deal_id]["receipt"] = {
            "enabled": True,
            "status": "skipped",
            "reason": SKIPPED_NO_ROUTE_REASON,
            "target_set": False,
            "delivery_confirmed": False,
            "message_id": None,
        }
    state_path.write_text(json.dumps(state), encoding="utf-8")

    out = compensate_trade_intake_receipts(
        base=tmp_path,
        config={},
        sources=sources,
        repo=repo,
        account=ACCOUNT,
        deal_ids=list(DEAL_IDS),
        apply_changes=False,
        reason=SKIPPED_NO_ROUTE_REASON,
        route_resolver=_route,
    )

    assert out["status"] == "ready"
    assert out["reason"] == SKIPPED_NO_ROUTE_REASON


def test_receipt_compensation_rejects_no_route_reason_without_exact_marker(
    tmp_path: Path,
) -> None:
    sources, repo = _fixture(tmp_path)

    with pytest.raises(ValueError, match="unsent no-route marker"):
        compensate_trade_intake_receipts(
            base=tmp_path,
            config={},
            sources=sources,
            repo=repo,
            account=ACCOUNT,
            deal_ids=list(DEAL_IDS),
            apply_changes=False,
            reason=SKIPPED_NO_ROUTE_REASON,
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


def _core_compensation_input(tmp_path, monkeypatch, *, namespace="futu.deal", outcome="no_route"):
    from src.application.ledger.repository import SQLiteOptionPositionsRepository
    from tests.test_trade_receipt_recovery import _payload, _processor, _successful_sender

    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    calls = []
    payload = {**_payload("777"), "external_id_namespace": namespace}
    def sender(**kwargs):
        result = _successful_sender(calls)(**kwargs)
        if outcome == "unknown":
            result.update(delivery_confirmed=False, message_id=None, error_code="SEND_UNCONFIRMED")
        return result

    result = _processor(tmp_path, repo, monkeypatch, sender, routed=outcome != "no_route")(payload)
    assert result["status"] == "applied"
    assert len(calls) == int(outcome != "no_route")
    return {
        "base": tmp_path, "config": {}, "repo": repo, "account": "lx",
        "sources": [{"id": "lx", "account": "lx", "state_path": tmp_path / "state.json",
                     "audit_path": tmp_path / "audit.jsonl", "receipt": {"enabled": True}}],
        "deal_ids": ["futu:lx:123:777"], "reason": SKIPPED_NO_ROUTE_REASON,
        "route_resolver": _route, "normalize_fn": lambda send_result: send_result,
    }


def test_real_core_no_route_receipt_compensation_defers_to_inbox_owner(tmp_path, monkeypatch):
    kwargs = _core_compensation_input(tmp_path, monkeypatch)
    state_before = (tmp_path / "state.json").read_bytes()
    state = json.loads(state_before)
    assert len(state["processed_deal_ids"]) == 1
    assert next(iter(state["processed_deal_ids"])).startswith("execution:v1:")
    repo = kwargs["repo"]
    economics_before = repo.list_trade_events(), repo.list_position_lots()
    calls = []

    def sender(**kw):
        calls.append(kw)
        return {"command_ok": True, "delivery_confirmed": True, "message_id": "offline-compensation", "returncode": 0}

    preview = compensate_trade_intake_receipts(**kwargs, apply_changes=False)
    assert preview["status"] == "inbox_managed"
    assert preview["inbox_receipts"][0]["current_result_key"] == "recorded"
    assert (tmp_path / "state.json").read_bytes() == state_before
    result = compensate_trade_intake_receipts(**kwargs, apply_changes=True,
                                            expected_payload_hash="old-preview", send_fn=sender)
    assert result["status"] == "inbox_managed"
    replay = compensate_trade_intake_receipts(**kwargs, apply_changes=True,
                                            expected_payload_hash="old-preview", send_fn=sender)
    assert replay["status"] == "inbox_managed"
    assert calls == []
    assert not (tmp_path / "receipt_compensations").exists()
    assert (repo.list_trade_events(), repo.list_position_lots()) == economics_before
    assert (tmp_path / "state.json").read_bytes() == state_before


@pytest.mark.parametrize("case", ["namespace", "physical_account", "already_sent", "unknown", "ambiguous_alias"])
def test_compensation_does_not_guess_execution_alias_or_override_receipt_evidence(tmp_path, monkeypatch, case):
    kwargs = _core_compensation_input(tmp_path, monkeypatch,
                                     namespace="file.deal" if case == "namespace" else "futu.deal",
                                     outcome=case if case in {"already_sent", "unknown"} else "no_route")
    if case == "physical_account":
        kwargs["deal_ids"] = ["futu:lx:124:777"]
    if case == "ambiguous_alias":
        path = tmp_path / "state.json"
        state = json.loads(path.read_text())
        state["processed_deal_ids"]["futu:lx:123:777"] = next(iter(state["processed_deal_ids"].values()))
        path.write_text(json.dumps(state))
    if case in {"already_sent", "unknown", "ambiguous_alias"}:
        assert compensate_trade_intake_receipts(**kwargs, apply_changes=False)["status"] == "inbox_managed"
    else:
        with pytest.raises(ValueError, match="missing deal_id"):
            compensate_trade_intake_receipts(**kwargs, apply_changes=False)
    assert not (tmp_path / "receipt_compensations").exists()


@pytest.mark.parametrize("status,blocked", [
    ("send_started", True), ("unknown", True), ("confirmed", True),
    ("prepared", True), ("explicit_failed", False),
])
def test_takeover_checks_every_member_of_legacy_combined_receipt(tmp_path, status, blocked):
    preview = _run(tmp_path, apply_changes=False)
    record = {**preview, "status": status, "explicit_pre_acceptance_failure": status == "explicit_failed"}
    path = Path(preview["record_path"])
    path.parent.mkdir()
    path.write_text(json.dumps(record))
    source = {"state_path": preview["state_path"], "account": ACCOUNT}
    for deal_id in DEAL_IDS:
        with receipt_compensation_takeover(source=source, account=ACCOUNT, canonical_deal_id=deal_id) as evidence:
            assert evidence["blocked"] is blocked
            assert evidence["result_key"] == "recorded"
            assert evidence["records"][0]["deal_ids"] == list(DEAL_IDS)
            assert evidence["status"] == ("confirmed" if status == "confirmed" else "unknown" if blocked else "clear")


@pytest.mark.parametrize("broken", [{}, {"schema_version": "unknown"}, "invalid json"])
def test_takeover_fails_closed_for_unattributable_legacy_evidence(tmp_path, broken):
    source = {"state_path": tmp_path / "state.json", "account": ACCOUNT}
    directory = tmp_path / "receipt_compensations"
    directory.mkdir()
    (directory / "unproven.json").write_text(broken if isinstance(broken, str) else json.dumps(broken))
    with pytest.raises(ValueError, match="unverifiable receipt compensation evidence"):
        with receipt_compensation_takeover(source=source, account=ACCOUNT, canonical_deal_id=DEAL_IDS[0]):
            pytest.fail("unproven legacy evidence must not grant takeover")


def test_manual_apply_blocks_overlap_with_combined_legacy_send(tmp_path):
    preview = _run(tmp_path, apply_changes=False)
    path = Path(preview["record_path"])
    path.parent.mkdir()
    path.write_text(json.dumps({**preview, "status": "send_started"}))
    sources, repo = _fixture(tmp_path)
    kwargs = dict(base=tmp_path, config={}, sources=sources, repo=repo, account=ACCOUNT,
                  deal_ids=[DEAL_IDS[0]], route_resolver=_route)
    single = compensate_trade_intake_receipts(**kwargs, apply_changes=False)
    result = compensate_trade_intake_receipts(
        **kwargs, apply_changes=True, expected_payload_hash=single["payload_hash"],
        send_fn=lambda **_: pytest.fail("overlap must never send"),
    )
    assert result["status"] == "duplicate_suppressed"
    assert result["suppression_reason"] == "overlapping_compensation"
    assert not Path(single["record_path"]).exists()


def test_manual_apply_rechecks_inbox_takeover_after_preview_under_shared_lock(tmp_path):
    import fcntl
    import threading
    from src.application.trades.inbox import enqueue_trade_payload, prepare_trade_receipt_result
    from src.application.ledger.repository import SQLiteOptionPositionsRepository
    from src.application.trades.deal_identity import broker_deal_key_from_payload
    from tests.test_trade_receipt_recovery import _payload

    sources, repo = _fixture(tmp_path)
    source = sources[0]
    inbox_path = Path(source["state_path"]).with_name("trade_intake_inbox.sqlite3")
    kwargs = dict(base=tmp_path, config={}, sources=sources, repo=repo, account=ACCOUNT,
                  deal_ids=list(DEAL_IDS), route_resolver=_route)
    preview = compensate_trade_intake_receipts(**kwargs, apply_changes=False)
    payload = _payload(DEAL_IDS[0].rsplit(":", 1)[1])
    payload["broker_account_ref"].update(external_account_id=FUTU_ACCOUNT_ID,
                                          broker_account_id=f"futu:REAL:{FUTU_ACCOUNT_ID}")
    repo.events[0]["raw_payload"]["execution_input"] = payload
    key = broker_deal_key_from_payload(payload, account_mapping=None)
    result = []
    started = threading.Event()

    def apply():
        started.set()
        result.append(compensate_trade_intake_receipts(
            **kwargs, apply_changes=True, expected_payload_hash=preview["payload_hash"],
            send_fn=lambda **_: pytest.fail("Inbox owns this multi-deal request")))

    with receipt_compensation_takeover(source=source, account=ACCOUNT, canonical_deal_id=DEAL_IDS[0]) as evidence:
        assert evidence["blocked"] is False
        with Path(source["state_path"]).with_name("receipt_compensations.lock").open("a+") as second:
            with pytest.raises(BlockingIOError):
                fcntl.flock(second.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        worker = threading.Thread(target=apply)
        worker.start()
        assert started.wait(2)
        inbox_id = enqueue_trade_payload(inbox_path, payload=payload, source="push", broker_deal_key=key,
                                        repo=SQLiteOptionPositionsRepository(tmp_path / "empty-ledger.sqlite3"))
        prepare_trade_receipt_result(inbox_path, inbox_id=inbox_id,
                                     result={"status": "applied", "reason": "applied_open"},
                                     expected_payload_version=1)
        assert not result
    worker.join(2)
    assert not worker.is_alive()
    assert result[0]["status"] == "inbox_managed"
    assert result[0]["inbox_receipts"][0]["current_result_key"] == "recorded"
    assert not Path(preview["record_path"]).exists()
