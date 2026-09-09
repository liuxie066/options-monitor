from __future__ import annotations

import json
import hashlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

import src.application.trades.backfill as backfill_module
import src.application.trades.auto_intake as auto_intake
from src.application.ledger.api import record_normalized_trade_event
from src.application.ledger.repository import SQLiteOptionPositionsRepository
from src.application.trades.auto_intake import _process_payload
from src.application.trades.backfill import run_history_backfill
from src.application.trades.deal_identity import broker_deal_key_from_payload
from src.application.trades.inbox import (
    enqueue_trade_payload,
    list_retryable_trade_payloads,
    read_trade_payload,
    record_trade_payload_refresh_intent,
    trade_inbox_summary,
)
from src.application.trades.inbox_authority import resolve_execution_inbox_path
from src.application.trades.normalizer import normalize_trade_deal
from src.application.trades.state import load_trade_intake_state, write_trade_intake_state


class _FakeRepo:
    def __init__(self, events: list[dict[str, Any]] | None = None) -> None:
        self.events = list(events or [])

    def list_trade_events(self) -> list[dict[str, Any]]:
        return list(self.events)


@pytest.fixture(autouse=True)
def _healthy_lifecycle_discovery(monkeypatch) -> None:
    def _discover(_repo, *, account, observed_at_ms, apply_changes):
        return {
            "schema_version": "lifecycle_discovery_result.v2",
            "observed_at_ms": observed_at_ms,
            "account": account,
            "apply_changes": apply_changes,
            "created_case_ids": [],
            "would_create_case_ids": [],
            "discovered_case_ids": [],
            "refreshed_case_ids": [],
            "would_refresh_case_ids": [],
            "skipped_targeted_lot_ids": [],
        }

    monkeypatch.setattr(
        backfill_module,
        "discover_lifecycle_cases",
        _discover,
    )


def _backfill_kwargs(tmp_path: Path) -> dict[str, Any]:
    return {
        "repo": _FakeRepo(),
        "state_path": tmp_path / "state.json",
        "audit_path": tmp_path / "audit.jsonl",
        "account_mapping": {"REAL_1": "lx"},
        "futu_account_ids": ["REAL_1"],
        "apply_changes": True,
        "host": "127.0.0.1",
        "port": 11111,
        "config": {},
        "config_path": tmp_path / "config.json",
        "runtime_root": tmp_path,
        "backfill_config": {"lookback_hours": 6},
        "on_result_fn": None,
        "now_fn": lambda: datetime(2026, 6, 3, 6, 0, tzinfo=timezone.utc),
    }


def _audit_events(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _standard_execution(*, price: str = "2.50") -> dict[str, Any]:
    return {
        "schema_version": "trade_execution.v1",
        "broker_account_ref": {
            "broker_id": "futu", "external_account_id": "123", "environment": "REAL",
            "broker_account_id": "futu:REAL:123", "account_label": "lx",
        },
        "instrument_ref": {
            "asset_type": "option", "market": "US", "symbol": "NVDA", "currency": "USD",
            "option_type": "put", "strike": "100", "expiration_ymd": "2026-09-18", "multiplier": "100",
        },
        "external_id_namespace": "futu.deal", "external_execution_id": "fill-1",
        "external_order_namespace": "futu.order", "external_order_id": "order-1",
        "side": "sell", "position_effect": "open", "quantity": "1", "price": price,
        "currency": "USD", "occurred_at_utc": "2026-09-07T02:30:00Z",
    }


def _run_standard_backfill(tmp_path: Path, repo, payload, *, on_result_fn=None, dispatch_portfolio_refresh_fn=None, config=None):
    kwargs = _backfill_kwargs(tmp_path)
    kwargs.update(repo=repo, account_mapping={"123": "lx"}, futu_account_ids=["123"], on_result_fn=on_result_fn)
    if config is not None:
        kwargs["config"] = config
    return run_history_backfill(
        **kwargs,
        dispatch_portfolio_refresh_fn=dispatch_portfolio_refresh_fn,
        history_deals_fn=lambda **_kwargs: ([payload], {
            "account_results": [{"futu_account_id": "123", "ret": 0, "coverage_status": "complete",
                                 "coverage_complete": True, "pagination_complete": True}],
        }),
        process_payload_fn=lambda row, **context: _process_payload(row, **context, allow_external_lookup=False),
    )


@pytest.mark.parametrize("saved_state", [False, True])
def test_canonical_backfill_checks_economics_even_when_ledger_or_state_is_processed(tmp_path: Path, saved_state: bool) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    original_payload = _standard_execution()
    record_normalized_trade_event(repo, normalize_trade_deal(original_payload))
    original_events = repo.list_trade_events()
    key = broker_deal_key_from_payload(original_payload, account_mapping={"123": "lx"})
    if saved_state:
        write_trade_intake_state(tmp_path / "state.json", {
            "processed_deal_ids": {key: {"status": "applied", "action": "open", "account": "lx"}},
            "failed_deal_ids": {}, "unresolved_deal_ids": {},
        })

    result = _run_standard_backfill(tmp_path, repo, _standard_execution(price="3"))

    assert result["unresolved_count"] == 1
    assert result["skipped_duplicate_count"] == 0
    assert result["last_result"]["reason"] == "trade_execution_economic_conflict"
    assert repo.list_trade_events() == original_events


def test_canonical_backfill_recovers_pending_receipt_after_ledger_commit(tmp_path: Path) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    payload = _standard_execution()
    key = broker_deal_key_from_payload(payload, account_mapping={"123": "lx"})
    inbox = resolve_execution_inbox_path(repo, tmp_path / "legacy.sqlite3")
    inbox_id = enqueue_trade_payload(inbox, payload=payload, source="push", broker_deal_key=key, repo=repo)
    record_normalized_trade_event(repo, normalize_trade_deal(payload))
    original_events = repo.list_trade_events()
    write_trade_intake_state(tmp_path / "state.json", {
        "processed_deal_ids": {key: {"status": "applied", "action": "open", "account": "lx"}},
        "failed_deal_ids": {}, "unresolved_deal_ids": {},
    })
    receipts = []

    def receipt(context):
        receipts.append(context)
        return {"status": "sent", "delivery_confirmed": True}

    first = _run_standard_backfill(tmp_path, repo, payload, on_result_fn=receipt)
    replay = _run_standard_backfill(tmp_path, repo, payload, on_result_fn=receipt)

    assert first["last_result"]["reason"] == "applied_open"
    assert len(receipts) == 1
    assert replay["skipped_duplicate_count"] == 1
    assert repo.list_trade_events() == original_events
    saved = read_trade_payload(inbox, inbox_id=inbox_id)
    assert saved["status"] == "handled"
    assert saved["result"]["operations"]


def test_backfill_assigned_stock_completion_keys_require_physical_scope() -> None:
    class StockRepo(_FakeRepo):
        def list_assigned_stock_events(self):
            return [
                {"event_type": "sale", "account": label, "futu_account_id": physical, "source_deal_id": "same-id"}
                for label, physical in (("lx", "123"), ("sy", "456"), ("lx", ""))
            ]

    assert backfill_module._ledger_recorded_deal_keys(StockRepo()) == {
        "futu:lx:123:same-id", "futu:sy:456:same-id",
    }


def test_run_history_backfill_processes_missing_deal_through_pipeline(tmp_path: Path) -> None:
    processed: list[dict[str, Any]] = []
    dispatched: list[dict[str, str]] = []

    def _history_deals_fn(**_kwargs):
        return (
            [{"deal_id": "deal-1", "code": "HK.TCH260605P440000"}],
            {"window_start_utc": "2026-06-03T00:00:00+00:00", "window_end_utc": "2026-06-03T06:00:00+00:00",
             "account_results": [{"futu_account_id": "REAL_1", "ret": 0, "coverage_status": "complete",
                                  "coverage_complete": True, "pagination_complete": True}]},
        )

    def _process_payload_fn(payload: dict[str, Any], **kwargs):
        processed.append(
            {
                "payload": payload,
                "source": kwargs.get("source"),
            }
        )
        return {
            "status": "applied",
            "action": "open",
            "reason": "applied_open",
            "deal_id": payload["deal_id"],
            "account": "lx",
        }

    out = run_history_backfill(
        **_backfill_kwargs(tmp_path),
        history_deals_fn=_history_deals_fn,
        process_payload_fn=_process_payload_fn,
        dispatch_portfolio_refresh_fn=dispatched.append,
    )

    assert out["ok"] is True
    assert out["deal_count"] == 1
    assert out["applied_count"] == 1
    assert len(processed) == 1
    assert processed[0]["payload"]["deal_id"] == "deal-1"
    assert processed[0]["payload"]["code"] == "HK.TCH260605P440000"
    assert processed[0]["payload"]["futu_account_id"] == "REAL_1"
    assert processed[0]["payload"]["internal_account"] == "lx"
    assert processed[0]["payload"]["_trade_intake_source"]["transport"] == "poll"
    assert processed[0]["payload"]["_trade_intake_source"]["opend_port"] == 11111
    assert processed[0]["source"] == "backfill"
    assert dispatched == []
    phases = [event["phase"] for event in _audit_events(tmp_path / "audit.jsonl")]
    assert phases == [
        "backfill_check_started",
        "backfill_lifecycle_discovery_before",
        "backfill_received",
        "backfill_applied",
        "backfill_lifecycle_reconciliation_after",
        "backfill_check_finished",
    ]
    with sqlite3.connect(tmp_path / "trade_intake_inbox.sqlite3") as conn:
        envelope = json.loads(
            conn.execute(
                "SELECT evidence_json FROM trade_inbox_evidence"
            ).fetchone()[0]
        )
    assert envelope["adapter_version"] == "om.trade-intake.history.v1"


def test_push_lookup_persists_only_exact_deal_economics_once(tmp_path, monkeypatch) -> None:
    class FakeGateway:
        def get_deal_list(self, **kwargs):
            assert kwargs == {"acc_id": 123}
            return [
                {
                    "deal_id": "other-fill",
                    "order_id": "shared-order",
                    "acc_id": "123",
                    "qty": "9",
                    "price": "99",
                    "create_time": "2026-09-07 09:00:00",
                },
                {
                    "deal_id": "target-fill",
                    "order_id": "shared-order",
                    "acc_id": "123",
                    "qty": "2",
                    "price": "2.50",
                    "create_time": "2026-09-07 10:30:01",
                },
            ]

        def get_order_list(self, **kwargs):
            pytest.fail("complete exact deal must not trigger an order lookup")

        def close(self):
            return None

    monkeypatch.setattr(
        "src.application.trades.futu_detail_lookup.build_ready_futu_broker_gateway",
        lambda **kwargs: FakeGateway(),
    )
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    payload = {
        "futu_account_id": "123",
        "deal_id": "target-fill",
        "order_id": "shared-order",
        "code": "US.NVDA260918P00100000",
        "trd_side": "SELL_SHORT",
        "contract_multiplier": "100",
    }
    kwargs = {
        "repo": repo,
        "state_path": tmp_path / "state.json",
        "audit_path": tmp_path / "audit.jsonl",
        "account_mapping": {"123": "lx"},
        "futu_account_ids": ["123"],
        "apply_changes": True,
        "host": "127.0.0.1",
        "port": 11111,
        "config": {},
        "config_path": tmp_path / "config.json",
        "runtime_root": tmp_path,
        "source": "push",
    }

    first = _process_payload(payload, **kwargs)
    replay = _process_payload(payload, **kwargs)

    assert (first["status"], first["action"], first["reason"]) == ("applied", "open", "applied_open")
    assert replay["reason"] == "duplicate"
    events = repo.list_trade_events()
    assert len(events) == 1
    assert events[0]["contracts"] == 2
    assert events[0]["price"] == 2.5
    assert events[0]["event_time_ms"] == int(datetime(2026, 9, 7, 2, 30, 1, tzinfo=timezone.utc).timestamp() * 1000)


def test_push_lookup_account_mismatch_keeps_missing_economics_in_review(tmp_path, monkeypatch) -> None:
    class FakeGateway:
        def get_deal_list(self, **kwargs):
            return [{
                "deal_id": "target-fill",
                "order_id": "shared-order",
                "acc_id": "456",
                "qty": "9",
                "price": "99",
                "create_time": "2026-09-07 09:00:00",
            }]

        def get_order_list(self, **kwargs):
            return []

        def close(self):
            return None

    monkeypatch.setattr(
        "src.application.trades.futu_detail_lookup.build_ready_futu_broker_gateway",
        lambda **kwargs: FakeGateway(),
    )
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    result = _process_payload(
        {
            "futu_account_id": "123",
            "deal_id": "target-fill",
            "order_id": "shared-order",
            "code": "US.NVDA260918P00100000",
            "trd_side": "SELL_SHORT",
            "contract_multiplier": "100",
        },
        repo=repo,
        state_path=tmp_path / "state.json",
        audit_path=tmp_path / "audit.jsonl",
        account_mapping={"123": "lx"},
        futu_account_ids=["123", "456"],
        apply_changes=True,
        host="127.0.0.1",
        port=11111,
        config={},
        config_path=tmp_path / "config.json",
        runtime_root=tmp_path,
        source="push",
    )

    assert result["status"] == "unresolved"
    assert result["reason"] == "missing_required_fields:contracts,price,trade_time_ms"
    assert result["diagnostics"]["missing_fields"] == ["contracts", "price", "trade_time_ms"]
    assert repo.list_trade_events() == []


def _standard_stock_execution(execution_id="stock-1"):
    payload = _standard_execution()
    payload["instrument_ref"] = {"asset_type": "stock", "market": "US", "symbol": "NVDA", "currency": "USD"}
    payload.update(external_execution_id=execution_id, side="buy", quantity="100", price="100")
    return payload


def test_backfill_dispatches_once_per_account_after_all_inbox_settlements(tmp_path, monkeypatch):
    order = []
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    monkeypatch.setattr(auto_intake, "is_portfolio_management_enabled", lambda _: True)
    payloads = [_standard_stock_execution(value) for value in ("stock-1", "stock-2")]
    intents = []

    def process(payload, **kwargs):
        result = _process_payload(payload, **kwargs, allow_external_lookup=False)
        order.append(payload["external_execution_id"])
        intents.append(result["portfolio_refresh_intent"])
        return result

    kwargs = _backfill_kwargs(tmp_path)
    kwargs.update(repo=repo, account_mapping={"123": "lx"}, futu_account_ids=["123"],
                  config={"portfolio_management": {"enabled": True}})
    run_history_backfill(
        **kwargs, history_deals_fn=lambda **_: (payloads, {}), process_payload_fn=process,
        dispatch_portfolio_refresh_fn=lambda intent: order.append(intent),
    )
    assert order == ["stock-1", "stock-2", intents[0]]
    inbox = resolve_execution_inbox_path(repo, tmp_path / "legacy.sqlite3")
    assert trade_inbox_summary(inbox)["handled_count"] == 2


def test_backfill_dispatches_stored_refresh_after_duplicate_recovery(
    tmp_path: Path,
) -> None:
    inbox_path = tmp_path / "trade_intake_inbox.sqlite3"
    payload = {
        "deal_id": "stock-1",
        "code": "HK.00700",
        "futu_account_id": "REAL_1",
        "internal_account": "lx",
    }
    inbox_id = enqueue_trade_payload(
        inbox_path,
        payload=payload,
        source="push",
        broker_deal_key="futu:lx:REAL_1:stock-1",
    )
    record_trade_payload_refresh_intent(
        inbox_path,
        inbox_id=inbox_id,
        intent={"account": "lx", "request_id": "stock-refresh:stored"},
    )
    write_trade_intake_state(
        tmp_path / "state.json",
        {
            "processed_deal_ids": {
                "futu:lx:REAL_1:stock-1": {
                    "status": "skipped",
                    "reason": "not_option_deal",
                }
            },
            "failed_deal_ids": {},
            "unresolved_deal_ids": {},
        },
    )
    dispatched: list[dict[str, str]] = []

    out = run_history_backfill(
        **{**_backfill_kwargs(tmp_path), "config": {"portfolio_management": {"enabled": True}}},
        inbox_path=inbox_path,
        history_deals_fn=lambda **_kwargs: ([payload], {}),
        process_payload_fn=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("duplicate should not enter process pipeline")
        ),
        dispatch_portfolio_refresh_fn=dispatched.append,
    )

    assert out["skipped_duplicate_count"] == 1
    assert dispatched == [
        {"account": "lx", "request_id": "stock-refresh:stored"}
    ]


def test_backfill_lifecycle_discovery_is_scoped_to_single_mapped_account(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls: list[str | None] = []

    def _discover(_repo, *, account, observed_at_ms, apply_changes):
        calls.append(account)
        return {
            "schema_version": "lifecycle_discovery_result.v2",
            "observed_at_ms": observed_at_ms,
            "account": account,
            "apply_changes": apply_changes,
            "created_case_ids": [],
            "would_create_case_ids": [],
            "discovered_case_ids": [],
            "refreshed_case_ids": [],
            "would_refresh_case_ids": [],
            "skipped_targeted_lot_ids": [],
        }

    monkeypatch.setattr(
        backfill_module,
        "discover_lifecycle_cases",
        _discover,
    )
    out = run_history_backfill(
        **_backfill_kwargs(tmp_path),
        history_deals_fn=lambda **_kwargs: ([], {}),
        process_payload_fn=lambda *_args, **_kwargs: {},
    )

    assert calls == ["lx", "lx"]
    lifecycle = out["diagnostics"]["lifecycle_reconciliation"]
    assert lifecycle["before"]["accounts"] == ["lx"]
    assert lifecycle["after"]["accounts"] == ["lx"]
    assert lifecycle["before"]["schema_version"] == (
        "lifecycle_discovery_result.v2"
    )
    assert lifecycle["after"]["schema_version"] == (
        "lifecycle_discovery_result.v2"
    )
    assert lifecycle["before"]["account_results"][0]["account"] == "lx"
    assert lifecycle["after"]["account_results"][0]["account"] == "lx"
    audits = _audit_events(tmp_path / "audit.jsonl")
    lifecycle_audits = [
        event
        for event in audits
        if event["phase"]
        in {
            "backfill_lifecycle_discovery_before",
            "backfill_lifecycle_reconciliation_after",
        }
    ]
    assert all(event["ok"] is True for event in lifecycle_audits)
    assert all(event["result"]["accounts"] == ["lx"] for event in lifecycle_audits)
    assert all(
        event["result"]["schema_version"]
        == "lifecycle_discovery_result.v2"
        for event in lifecycle_audits
    )
    assert all("accounts" not in event for event in lifecycle_audits)


def test_backfill_lifecycle_discovery_scopes_legacy_source_per_account(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls: list[str | None] = []

    def _discover(_repo, *, account, observed_at_ms, apply_changes):
        calls.append(account)
        return {
            "schema_version": "lifecycle_discovery_result.v2",
            "observed_at_ms": observed_at_ms,
            "account": account,
            "apply_changes": apply_changes,
            "created_case_ids": [f"created-{account}"],
            "would_create_case_ids": [],
            "discovered_case_ids": [f"discovered-{account}"],
            "refreshed_case_ids": [],
            "would_refresh_case_ids": [],
            "skipped_targeted_lot_ids": [f"lot-{account}"],
        }

    monkeypatch.setattr(
        backfill_module,
        "discover_lifecycle_cases",
        _discover,
    )
    kwargs = _backfill_kwargs(tmp_path)
    kwargs["account_mapping"] = {
        "REAL_2": "sy",
        "REAL_1": "LX",
    }
    kwargs["futu_account_ids"] = ["REAL_2", "REAL_1"]
    out = run_history_backfill(
        **kwargs,
        history_deals_fn=lambda **_kwargs: ([], {}),
        process_payload_fn=lambda *_args, **_kwargs: {},
    )

    assert calls == ["lx", "sy", "lx", "sy"]
    before = out["diagnostics"]["lifecycle_reconciliation"]["before"]
    assert before["ok"] is True
    assert before["accounts"] == ["lx", "sy"]
    assert [item["account"] for item in before["account_results"]] == [
        "lx",
        "sy",
    ]
    assert before["created_case_ids"] == ["created-lx", "created-sy"]
    assert before["discovered_case_ids"] == [
        "discovered-lx",
        "discovered-sy",
    ]
    assert before["skipped_targeted_lot_ids"] == ["lot-lx", "lot-sy"]


def test_backfill_lifecycle_discovery_rejects_incomplete_account_scope_without_partial_scan(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        backfill_module,
        "discover_lifecycle_cases",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("incomplete account scope must not scan any account")
        ),
    )
    kwargs = _backfill_kwargs(tmp_path)
    kwargs["account_mapping"] = {"REAL_1": "lx"}
    kwargs["futu_account_ids"] = ["REAL_1", "REAL_2"]
    out = run_history_backfill(
        **kwargs,
        history_deals_fn=lambda **_kwargs: (
            [
                {
                    "deal_id": "deal-unmapped",
                    "futu_account_id": "REAL_2",
                }
            ],
            {"account_results": [
                {"futu_account_id": physical, "ret": 0, "coverage_status": "complete",
                 "coverage_complete": True, "pagination_complete": True}
                for physical in ("REAL_1", "REAL_2")
            ]},
        ),
        process_payload_fn=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("unmapped payload must remain unresolved")
        ),
    )

    assert out["ok"] is False
    assert out["error"] == "lifecycle_discovery_incomplete"
    assert out["deal_count"] == 1
    assert out["unresolved_count"] == 1
    assert out["diagnostics"]["lifecycle_discovery_complete"] is False
    lifecycle = out["diagnostics"]["lifecycle_reconciliation"]
    for phase in ("before", "after"):
        assert lifecycle[phase]["ok"] is False
        assert lifecycle[phase]["reason"] == (
            "lifecycle_account_scope_incomplete"
        )
        assert lifecycle[phase]["accounts"] == []
        assert lifecycle[phase]["account_results"] == []
        assert "REAL_2" in lifecycle[phase]["error"]
    lifecycle_audits = [
        event
        for event in _audit_events(tmp_path / "audit.jsonl")
        if event["phase"]
        in {
            "backfill_lifecycle_discovery_before",
            "backfill_lifecycle_reconciliation_after",
        }
    ]
    assert all(event["ok"] is False for event in lifecycle_audits)
    assert all("REAL_2" in event["error"] for event in lifecycle_audits)
    assert all(event["result"]["accounts"] == [] for event in lifecycle_audits)
    phases = [
        event["phase"]
        for event in _audit_events(tmp_path / "audit.jsonl")
    ]
    assert "backfill_received" in phases
    assert "backfill_identity_needs_review" in phases


def test_run_history_backfill_skips_processed_outbox_managed_duplicate_before_pipeline(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "state.json"
    write_trade_intake_state(
        state_path,
        {
            "processed_deal_ids": {
                "futu:lx:REAL_1:deal-1": {
                    "status": "applied",
                    "reason": "applied_open",
                    "receipt": {
                        "status": "outbox_managed",
                        "reason": "transactional_outbox",
                        "delivery_confirmed": False,
                    },
                }
            },
            "failed_deal_ids": {},
            "unresolved_deal_ids": {},
        },
    )

    def _history_deals_fn(**_kwargs):
        return ([{"deal_id": "deal-1", "order_id": "order-1"}], {})

    def _process_payload_fn(_payload: dict[str, Any], **_kwargs):
        raise AssertionError("duplicate should not enter process pipeline")

    kwargs = _backfill_kwargs(tmp_path)
    kwargs["state_path"] = state_path
    kwargs["on_result_fn"] = lambda _context: (_ for _ in ()).throw(
        AssertionError("duplicate backfill must not invoke receipt callback")
    )
    fee_targets: list[tuple[str, str, str, str]] = []
    out = run_history_backfill(
        **kwargs,
        history_deals_fn=_history_deals_fn,
        process_payload_fn=_process_payload_fn,
        enqueue_fee_target_fn=fee_targets.append,
    )

    assert out["applied_count"] == 0
    assert out["skipped_duplicate_count"] == 1
    assert fee_targets == [("富途", "lx", "REAL_1", "order-1")]
    events = _audit_events(tmp_path / "audit.jsonl")
    skipped = [event for event in events if event["phase"] == "backfill_skipped_duplicate"]
    assert skipped == [{"phase": "backfill_skipped_duplicate", "source": "backfill", "deal_id": "deal-1", "reason": "state:processed_deal_ids"}]


def test_history_backfill_does_not_enqueue_processed_non_option_fee_target(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "state.json"
    write_trade_intake_state(
        state_path,
        {
            "processed_deal_ids": {
                "futu:lx:REAL_1:deal-stock": {
                    "status": "skipped",
                    "reason": "not_option_deal",
                }
            },
            "failed_deal_ids": {},
            "unresolved_deal_ids": {},
        },
    )
    fee_targets: list[tuple[str, str, str, str]] = []
    kwargs = _backfill_kwargs(tmp_path)
    kwargs["state_path"] = state_path

    out = run_history_backfill(
        **kwargs,
        history_deals_fn=lambda **_kwargs: (
            [{"deal_id": "deal-stock", "order_id": "order-stock"}],
            {},
        ),
        process_payload_fn=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("duplicate should not enter process pipeline")
        ),
        enqueue_fee_target_fn=fee_targets.append,
    )

    assert out["skipped_duplicate_count"] == 1
    assert fee_targets == []
    assert out["diagnostics"]["fee_target_count"] == 0


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        ("unresolved", "waiting_settlement_evidence"),
        ("failed", "projection_verification_failed"),
    ],
)
def test_history_backfill_does_not_enqueue_non_durable_fee_target(
    tmp_path: Path,
    status: str,
    reason: str,
) -> None:
    fee_targets: list[tuple[str, str, str, str]] = []

    out = run_history_backfill(
        **_backfill_kwargs(tmp_path),
        history_deals_fn=lambda **_kwargs: (
            [{"deal_id": "deal-1", "order_id": "order-1"}],
            {},
        ),
        process_payload_fn=lambda payload, **_kwargs: {
            "status": status,
            "action": None,
            "reason": reason,
            "deal_id": payload["deal_id"],
            "account": "lx",
        },
        enqueue_fee_target_fn=fee_targets.append,
    )

    assert fee_targets == []
    assert out["diagnostics"]["fee_target_count"] == 0


def test_run_history_backfill_retries_retryable_unresolved_state(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    write_trade_intake_state(
        state_path,
        {
            "processed_deal_ids": {},
            "failed_deal_ids": {},
            "unresolved_deal_ids": {
                "deal-1": {
                    "status": "unresolved",
                    "reason": "missing_account_mapping",
                    "retryable": True,
                    "attempt_count": 1,
                }
            },
        },
    )
    processed: list[dict[str, Any]] = []

    def _history_deals_fn(**_kwargs):
        return ([{"deal_id": "deal-1"}], {})

    def _process_payload_fn(payload: dict[str, Any], **kwargs):
        processed.append({"payload": payload, "source": kwargs.get("source")})
        return {
            "status": "applied",
            "action": "open",
            "reason": "applied_open",
            "deal_id": payload["deal_id"],
            "account": "lx",
        }

    kwargs = _backfill_kwargs(tmp_path)
    kwargs["state_path"] = state_path
    out = run_history_backfill(
        **kwargs,
        history_deals_fn=_history_deals_fn,
        process_payload_fn=_process_payload_fn,
    )

    assert out["applied_count"] == 1
    assert out["skipped_duplicate_count"] == 0
    assert len(processed) == 1
    assert processed[0]["payload"]["deal_id"] == "deal-1"
    assert processed[0]["payload"]["futu_account_id"] == "REAL_1"
    assert processed[0]["payload"]["internal_account"] == "lx"
    assert processed[0]["source"] == "backfill"


def test_run_history_backfill_marks_ledger_duplicate_processed_without_pipeline(tmp_path: Path) -> None:
    def _history_deals_fn(**_kwargs):
        return ([{"deal_id": "deal-1"}], {})

    def _process_payload_fn(_payload: dict[str, Any], **_kwargs):
        raise AssertionError("ledger duplicate should not enter process pipeline")

    kwargs = _backfill_kwargs(tmp_path)
    kwargs["repo"] = _FakeRepo([{"event_id": "deal-1", "account": "lx",
                                 "raw_payload": {"source_deal_id": "deal-1", "futu_account_id": "REAL_1"}}])
    out = run_history_backfill(
        **kwargs,
        history_deals_fn=_history_deals_fn,
        process_payload_fn=_process_payload_fn,
    )

    assert out["applied_count"] == 0
    assert out["skipped_duplicate_count"] == 1
    state = load_trade_intake_state(tmp_path / "state.json")
    assert state["processed_deal_ids"][
        "futu:lx:REAL_1:deal-1"
    ]["status"] == "reconciled"
    assert state["processed_deal_ids"][
        "futu:lx:REAL_1:deal-1"
    ]["reason"] == "ledger_event_already_recorded"


def test_backfill_does_not_dedupe_same_deal_id_across_accounts(
    tmp_path: Path,
) -> None:
    processed: list[dict[str, Any]] = []

    def _history_deals_fn(**_kwargs):
        return (
            [{"deal_id": "same-id", "futu_account_id": "REAL_2"}],
            {},
        )

    def _process_payload_fn(payload: dict[str, Any], **_kwargs):
        processed.append(dict(payload))
        return {
            "status": "applied",
            "action": "open",
            "reason": "applied_open",
            "deal_id": payload["deal_id"],
            "account": "sy",
        }

    kwargs = _backfill_kwargs(tmp_path)
    kwargs["account_mapping"] = {"REAL_1": "lx", "REAL_2": "sy"}
    kwargs["futu_account_ids"] = ["REAL_1", "REAL_2"]
    kwargs["repo"] = _FakeRepo(
        [
            {
                "event_id": "futu:lx:REAL_1:same-id",
                "event_type": "open",
                "account": "lx",
                "raw_payload": {
                    "external_event_key": "futu:lx:REAL_1:same-id",
                    "source_deal_id": "same-id",
                    "futu_account_id": "REAL_1",
                    "broker_deal_completion": {
                        "split_count": 1,
                        "split_index": 1,
                        "expected_contracts": 1,
                        "allocated_contracts": 1,
                    },
                },
            }
        ]
    )
    out = run_history_backfill(
        **kwargs,
        history_deals_fn=_history_deals_fn,
        process_payload_fn=_process_payload_fn,
    )

    assert out["applied_count"] == 1
    assert out["skipped_duplicate_count"] == 0
    assert len(processed) == 1
    assert processed[0]["deal_id"] == "same-id"
    assert processed[0]["futu_account_id"] == "REAL_2"
    assert processed[0]["internal_account"] == "sy"
    assert processed[0]["_trade_intake_source"]["transport"] == "poll"


def test_run_history_backfill_does_not_treat_numeric_lot_lineage_as_deal_id(
    tmp_path: Path,
) -> None:
    processed: list[str] = []
    opening_deal_id = "9162790356868244299"
    closing_deal_id = "495287541148725639"

    def _history_deals_fn(**_kwargs):
        return ([{"deal_id": opening_deal_id}], {})

    def _process_payload_fn(payload: dict[str, Any], **_kwargs):
        processed.append(str(payload["deal_id"]))
        return {
            "status": "applied",
            "action": "open",
            "reason": "applied_open",
            "deal_id": payload["deal_id"],
            "account": "lx",
        }

    kwargs = _backfill_kwargs(tmp_path)
    kwargs["repo"] = _FakeRepo(
        [
            {
                "event_id": (
                    "futu:lx:999000000000000001:"
                    f"{closing_deal_id}:close:lot_futu:lx:999000000000000001:{opening_deal_id}"
                ),
                "event_type": "close",
                "raw_payload": {"source_deal_id": closing_deal_id},
            }
        ]
    )

    out = run_history_backfill(
        **kwargs,
        history_deals_fn=_history_deals_fn,
        process_payload_fn=_process_payload_fn,
    )

    assert out["applied_count"] == 1
    assert processed == [opening_deal_id]


def test_history_backfill_does_not_skip_incomplete_broker_close_split(
    tmp_path: Path,
) -> None:
    processed: list[str] = []

    kwargs = _backfill_kwargs(tmp_path)
    kwargs["repo"] = _FakeRepo(
        [
            {
                "event_id": "broker-close-deal-split-lot-1",
                "event_type": "close",
                "contracts": 1,
                "target_lot_id": "lot-1",
                "raw_payload": {
                    "source_deal_id": "deal-split",
                    "broker_deal_completion": {
                        "source_deal_id": "deal-split",
                        "expected_contracts": 2,
                        "split_count": 2,
                        "split_index": 1,
                        "allocated_contracts": 1,
                    },
                },
            }
        ]
    )

    out = run_history_backfill(
        **kwargs,
        history_deals_fn=lambda **_kwargs: ([{"deal_id": "deal-split"}], {}),
        process_payload_fn=lambda payload, **_kwargs: (
            processed.append(str(payload["deal_id"]))
            or {
                "status": "applied",
                "action": "close",
                "reason": "applied_close",
                "deal_id": payload["deal_id"],
                "account": "lx",
            }
        ),
    )

    assert out["applied_count"] == 1
    assert processed == ["deal-split"]


def test_history_backfill_extends_window_from_persisted_checkpoint(
    tmp_path: Path,
) -> None:
    captured: dict[str, Any] = {}
    checkpoint_path = tmp_path / "backfill_checkpoint.json"
    seed = _backfill_kwargs(tmp_path)
    seed["now_fn"] = lambda: datetime(2026, 6, 1, tzinfo=timezone.utc)
    run_history_backfill(
        **seed, checkpoint_path=checkpoint_path,
        history_deals_fn=lambda **_: ([], {"account_results": [_complete_account("REAL_1")]}),
        process_payload_fn=lambda *_args, **_kwargs: {},
    )

    def _history_deals_fn(**kwargs):
        captured.update(kwargs)
        return (
            [],
            {
                "window_start_utc": "2026-05-31T23:00:00+00:00",
                "window_end_utc": "2026-06-03T06:00:00+00:00",
                "account_results": [
                    {
                        "futu_account_id": "REAL_1",
                        "ret": 0,
                        "row_count": 0,
                        "coverage_status": "complete",
                        "coverage_complete": True,
                        "pagination_complete": True,
                    }
                ],
            },
        )

    out = run_history_backfill(
        **_backfill_kwargs(tmp_path),
        checkpoint_path=checkpoint_path,
        history_deals_fn=_history_deals_fn,
        process_payload_fn=lambda *_args, **_kwargs: {},
    )

    assert captured["lookback_hours"] == 55.0
    assert out["ok"] is True
    assert out["diagnostics"]["checkpoint_advanced"] is True
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    assert (
        next(iter(checkpoint["scopes"].values()))["last_successful_window_end_utc"]
        == "2026-06-03T06:00:00+00:00"
    )


def test_history_backfill_does_not_advance_checkpoint_on_partial_account_query(
    tmp_path: Path,
) -> None:
    checkpoint_path = tmp_path / "backfill_checkpoint.json"
    original = {
        "last_successful_window_end_utc": "2026-06-01T00:00:00+00:00"
    }
    checkpoint_path.write_text(json.dumps(original), encoding="utf-8")

    def _history_deals_fn(**_kwargs):
        return (
            [],
            {
                "window_end_utc": "2026-06-03T06:00:00+00:00",
                "account_results": [
                    {
                        "futu_account_id": "REAL_1",
                        "ret": -1,
                        "row_count": 0,
                        "error": "trade context unavailable",
                    }
                ],
            },
        )

    out = run_history_backfill(
        **_backfill_kwargs(tmp_path),
        checkpoint_path=checkpoint_path,
        history_deals_fn=_history_deals_fn,
        process_payload_fn=lambda *_args, **_kwargs: {},
    )

    assert out["ok"] is False
    assert out["error"] == "history_query_incomplete"
    assert out["diagnostics"]["checkpoint_advanced"] is False
    assert json.loads(checkpoint_path.read_text(encoding="utf-8")) == original


def test_history_backfill_keeps_unexpected_pipeline_exception_in_durable_inbox(
    tmp_path: Path,
) -> None:
    inbox_path = tmp_path / "trade_inbox.sqlite3"

    def _history_deals_fn(**_kwargs):
        return ([{"deal_id": "deal-crash"}], {})

    out = run_history_backfill(
        **_backfill_kwargs(tmp_path),
        inbox_path=inbox_path,
        history_deals_fn=_history_deals_fn,
        process_payload_fn=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("unexpected pipeline crash")
        ),
    )

    assert out["failed_count"] == 1
    assert trade_inbox_summary(inbox_path)["pending_count"] == 1
    retry_rows = list_retryable_trade_payloads(
        inbox_path,
        retry_delay_sec=0,
    )
    assert retry_rows[0]["payload"]["deal_id"] == "deal-crash"
    assert retry_rows[0]["attempt_count"] == 0
    failed = [event for event in _audit_events(tmp_path / "audit.jsonl") if event["phase"] == "backfill_pipeline_failed"]
    assert "unexpected pipeline crash" in failed[0]["error"]


def test_history_backfill_handles_lifecycle_pending_in_durable_inbox(tmp_path, monkeypatch):
    from src.application.trades.resolver import IntakeResolution
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    monkeypatch.setattr(auto_intake, "resolve_trade_deal", lambda deal, **_: IntakeResolution(
        status="unresolved", action="lifecycle", reason="waiting_settlement_evidence",
        deal_id=deal.deal_id, account="lx", operations=[],
        diagnostics={"retryable": True, "broker_evidence_accepted": True},
    ))
    out = _run_standard_backfill(tmp_path, repo, _standard_execution())
    inbox = resolve_execution_inbox_path(repo, tmp_path / "legacy.sqlite3")
    assert out["unresolved_count"] == 1
    assert trade_inbox_summary(inbox)["pending_count"] == 0
    assert trade_inbox_summary(inbox)["handled_count"] == 1
    assert list_retryable_trade_payloads(inbox, retry_delay_sec=0) == []


def test_history_backfill_fee_target_enqueue_failure_redacts_exception_message(
    tmp_path: Path,
) -> None:
    out = run_history_backfill(
        **_backfill_kwargs(tmp_path),
        history_deals_fn=lambda **_kwargs: (
            [{"deal_id": "deal-1", "order_id": "order-1"}],
            {},
        ),
        process_payload_fn=lambda payload, **_kwargs: {
            "status": "applied",
            "action": "open",
            "reason": "applied_open",
            "deal_id": payload["deal_id"],
            "account": "lx",
        },
        enqueue_fee_target_fn=lambda _target: (_ for _ in ()).throw(
            RuntimeError("secret-order-id=/private/path")
        ),
    )

    assert out["diagnostics"]["fee_target_count"] == 1
    assert out["diagnostics"]["fee_target_enqueue_failed_count"] == 1
    event = next(
        event
        for event in _audit_events(tmp_path / "audit.jsonl")
        if event["phase"] == "backfill_fee_target_enqueue_failed"
    )
    assert event["error_type"] == "RuntimeError"
    assert "secret-order-id" not in str(event)


def _complete_account(account_id):
    return {"futu_account_id": account_id, "ret": 0, "coverage_status": "complete",
            "coverage_complete": True, "pagination_complete": True}


def _scoped_history_run(tmp_path, *, account_ids, now, host="127.0.0.1", port=11111,
                        complete_ids=None, payloads=()):
    captured = {}
    def history(**kwargs):
        captured.update(kwargs)
        return list(payloads), {"account_results": [
            _complete_account(value) if complete_ids is None or value in complete_ids else
            {"futu_account_id": value, "ret": -1, "error": "query unavailable"}
            for value in account_ids
        ]}
    kwargs = _backfill_kwargs(tmp_path)
    kwargs.update(account_mapping={value: "lx" for value in account_ids}, futu_account_ids=account_ids,
                  host=host, port=port, now_fn=lambda: now)
    out = run_history_backfill(**kwargs, history_deals_fn=history, process_payload_fn=lambda *_args, **_: {})
    return out, captured


@pytest.mark.parametrize("replacement", ["physical", "host", "port"])
def test_checkpoint_scope_switch_back_retains_original_gap(tmp_path, replacement):
    t0 = datetime(2026, 6, 1, tzinfo=timezone.utc)
    t1 = datetime(2026, 6, 3, tzinfo=timezone.utc)
    t2 = datetime(2026, 6, 3, 6, tzinfo=timezone.utc)
    _scoped_history_run(tmp_path, account_ids=["A"], now=t0)
    other = {"account_ids": ["B"]} if replacement == "physical" else {replacement: "opend-b" if replacement == "host" else 11112}
    _scoped_history_run(tmp_path, **{"account_ids": ["A"], **other}, now=t1)
    out, query = _scoped_history_run(tmp_path, account_ids=["A"], now=t2)
    assert query["lookback_hours"] == 55
    assert out["diagnostics"]["checkpoint_advanced_accounts"] == ["A"]
    checkpoint = json.loads((tmp_path / "trade_intake_backfill_checkpoint.json").read_text())
    assert len(checkpoint["scopes"]) == 2


def test_checkpoint_legacy_cursor_is_preserved_as_unverified(tmp_path):
    path = tmp_path / "trade_intake_backfill_checkpoint.json"
    original = {"last_successful_window_end_utc": "2026-01-01T00:00:00Z"}
    path.write_text(json.dumps(original))
    out, query = _scoped_history_run(tmp_path, account_ids=["A"], now=datetime(2026, 6, 3, tzinfo=timezone.utc))
    assert query["lookback_hours"] == 6
    assert out["diagnostics"]["legacy_checkpoint_unverified"] is True
    saved = json.loads(path.read_text())
    assert saved["legacy_unverified"] == original
    assert len(saved["scopes"]) == 1


@pytest.mark.parametrize("failure", ["query", "durable"])
def test_checkpoint_advances_only_complete_durable_account(tmp_path, monkeypatch, failure):
    t0 = datetime(2026, 6, 1, tzinfo=timezone.utc)
    t1 = datetime(2026, 6, 3, tzinfo=timezone.utc)
    _scoped_history_run(tmp_path, account_ids=["A", "B"], now=t0)
    original = backfill_module.enqueue_trade_payload
    def enqueue(path, **kwargs):
        if kwargs["payload"].get("futu_account_id") == "B":
            raise OSError("temporary Inbox write failure")
        return original(path, **kwargs)
    if failure == "durable":
        monkeypatch.setattr(backfill_module, "enqueue_trade_payload", enqueue)
    out, query = _scoped_history_run(
        tmp_path, account_ids=["A", "B"], now=t1,
        complete_ids=["A"] if failure == "query" else None,
        payloads=[{"deal_id": "fill-b", "futu_account_id": "B"}] if failure == "durable" else [],
    )
    assert query["lookback_hours"] == 49
    assert out["ok"] is False
    assert out["diagnostics"]["checkpoint_advanced_accounts"] == ["A"]
    saved = json.loads((tmp_path / "trade_intake_backfill_checkpoint.json").read_text())
    cursors = {value["scope"]["physical_account_id"]: value["last_successful_window_end_utc"]
               for value in saved["scopes"].values()}
    assert cursors == {"A": t1.isoformat(), "B": t0.isoformat()}


def _process_stock(tmp_path, repo, payload, *, source="push"):
    return _process_payload(
        payload, repo=repo, state_path=tmp_path / "state.json", audit_path=tmp_path / "audit.jsonl",
        account_mapping={"123": "lx"}, futu_account_ids=["123"], apply_changes=True,
        host="127.0.0.1", port=11111, source=source, allow_external_lookup=False,
    )


@pytest.mark.parametrize("crash_at", ["before_state", "after_state"])
def test_pm_intent_survives_stock_state_crash_and_is_claimed_once(tmp_path, monkeypatch, crash_at):
    from src.application.trades.inbox import claim_trade_payload_refresh_intent
    class Crash(BaseException):
        pass
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    monkeypatch.setattr(auto_intake, "is_portfolio_management_enabled", lambda _: True)
    original = auto_intake.update_trade_intake_state_entries
    def crash(path, state, **kwargs):
        if crash_at == "after_state":
            original(path, state, **kwargs)
        raise Crash()
    monkeypatch.setattr(auto_intake, "update_trade_intake_state_entries", crash)
    payload = _standard_stock_execution()
    with pytest.raises(Crash):
        _process_stock(tmp_path, repo, payload)
    inbox = resolve_execution_inbox_path(repo, tmp_path / "legacy.sqlite3")
    after_lease = auto_intake.time.time() + 121
    monkeypatch.setattr(auto_intake.time, "time", lambda: after_lease)
    pending = list_retryable_trade_payloads(inbox, retry_delay_sec=0)[0]
    saved = read_trade_payload(inbox, inbox_id=pending["inbox_id"])
    intent = saved["result"]["portfolio_refresh_intent"]
    assert json.loads(saved["portfolio_refresh_intent_json"]) == intent
    assert saved["portfolio_refresh_attempted_at_ms"] is None
    monkeypatch.setattr(auto_intake, "update_trade_intake_state_entries", original)
    monkeypatch.setattr("src.application.trades.inbox.time.time", lambda: (saved["claim_until_ms"] + 1) / 1000)
    _process_stock(tmp_path, repo, payload, source="backfill")
    assert read_trade_payload(inbox, inbox_id=saved["inbox_id"])["status"] == "handled"
    assert claim_trade_payload_refresh_intent(inbox, inbox_id=saved["inbox_id"]) == intent
    assert claim_trade_payload_refresh_intent(inbox, inbox_id=saved["inbox_id"]) is None


def test_backfill_disabled_preserves_pending_refresh_for_enabled_replay(tmp_path, monkeypatch):
    from src.application.trades.inbox import resume_trade_payload

    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    monkeypatch.setattr(auto_intake, "is_portfolio_management_enabled", lambda _: True)
    payload = _standard_stock_execution()
    class Crash(BaseException):
        pass
    original = auto_intake.update_trade_intake_state_entries
    def interrupt(path, state, **kwargs):
        original(path, state, **kwargs)
        raise Crash()
    monkeypatch.setattr(auto_intake, "update_trade_intake_state_entries", interrupt)
    with pytest.raises(Crash):
        _process_stock(tmp_path, repo, payload)
    monkeypatch.setattr(auto_intake, "update_trade_intake_state_entries", original)
    path = resolve_execution_inbox_path(repo, tmp_path / "unused.sqlite3")
    after_lease = auto_intake.time.time() + 121
    monkeypatch.setattr(auto_intake.time, "time", lambda: after_lease)
    pending = list_retryable_trade_payloads(path, retry_delay_sec=0)[0]
    intent = json.loads(read_trade_payload(path, inbox_id=pending["inbox_id"])["portfolio_refresh_intent_json"])
    resume_trade_payload(path, inbox_id=pending["inbox_id"], operator="offline-test", repo=repo)
    calls = []
    _run_standard_backfill(tmp_path, repo, payload, dispatch_portfolio_refresh_fn=calls.append,
                           config={"portfolio_management": {"enabled": False}})
    saved = read_trade_payload(path, inbox_id=pending["inbox_id"])
    assert saved["status"] == "handled" and saved["portfolio_refresh_attempted_at_ms"] is None
    assert calls == []
    for _ in range(2):
        _run_standard_backfill(tmp_path, repo, payload, dispatch_portfolio_refresh_fn=calls.append,
                               config={"portfolio_management": {"enabled": True}})
    assert calls == [intent]


@pytest.mark.parametrize("replay_source", ["push", "backfill"])
def test_historical_stock_reception_cannot_gain_live_pm_intent(tmp_path, monkeypatch, replay_source):
    from src.application.trades.inbox import claim_trade_payload_refresh_intent
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    monkeypatch.setattr(auto_intake, "is_portfolio_management_enabled", lambda _: True)
    payload = _standard_stock_execution()
    inbox = resolve_execution_inbox_path(repo, tmp_path / "legacy.sqlite3")
    inbox_id = enqueue_trade_payload(inbox, payload=payload, source="file", repo=repo,
                                     broker_deal_key=broker_deal_key_from_payload(payload, account_mapping={"123": "lx"}))
    _process_stock(tmp_path, repo, payload, source=replay_source)
    saved = read_trade_payload(inbox, inbox_id=inbox_id)
    assert saved["delivery_purpose"] == "historical"
    assert "portfolio_refresh_intent" not in saved["result"]
    assert saved["portfolio_refresh_intent_json"] is None
    assert claim_trade_payload_refresh_intent(inbox, inbox_id=inbox_id) is None


@pytest.mark.parametrize("failure", ["unresolved", "exception"])
def test_backfill_counts_one_attempt_per_real_core_attempt(tmp_path, monkeypatch, failure):
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    monkeypatch.setattr("src.application.trades.normalizer.resolve_multiplier_with_source_and_diagnostics",
                        lambda **_: (None, None, {}))
    payload = {"acc_id": "123", "broker_account_id": "futu:REAL:123", "environment": "REAL",
               "external_id_namespace": "futu.deal", "deal_id": "missing-multiplier",
               "code": "US.NVDA260918P00100000", "qty": "1", "price": "2.50",
               "trd_side": "SELL_SHORT", "create_time": "2026-09-07 10:30:00"}
    if failure == "exception":
        monkeypatch.setattr(auto_intake, "save_trade_payload_result",
                            lambda *_, **__: (_ for _ in ()).throw(OSError("result storage unavailable")))
    inbox = resolve_execution_inbox_path(repo, tmp_path / "legacy.sqlite3")
    key = broker_deal_key_from_payload(payload, account_mapping={"123": "lx"})
    inbox_id = hashlib.sha256(key.encode()).hexdigest()
    clock = [auto_intake.time.time()]
    monkeypatch.setattr(auto_intake.time, "time", lambda: clock[0])
    for expected_attempts in (1, 2):
        result = _run_standard_backfill(tmp_path, repo, payload)
        rows = list_retryable_trade_payloads(inbox, retry_delay_sec=0)
        if failure == "unresolved":
            assert len(rows) == 1
            assert rows[0]["attempt_count"] == expected_attempts
            assert result["last_result"]["reason"] == "missing_required_fields:multiplier"
        else:
            assert rows == []
            saved = read_trade_payload(inbox, inbox_id=inbox_id)
            assert saved["attempt_count"] == 1
            assert saved["result"]["receipt_kind"] == "verification_pending"
            assert "result storage unavailable" in saved["last_error"]
        clock[0] += 61
    assert repo.list_trade_events() == []


def test_checkpoint_repeated_or_older_window_does_not_regress_or_report_advance(tmp_path):
    current = datetime(2026, 6, 3, tzinfo=timezone.utc)
    first, _ = _scoped_history_run(tmp_path, account_ids=["A"], now=current)
    assert first["diagnostics"]["checkpoint_advanced"] is True
    checkpoint = tmp_path / "trade_intake_backfill_checkpoint.json"
    original = checkpoint.read_bytes()
    for observed in (current, datetime(2026, 6, 2, tzinfo=timezone.utc)):
        replay, _ = _scoped_history_run(tmp_path, account_ids=["A"], now=observed)
        assert replay["diagnostics"]["checkpoint_advanced"] is False
        assert replay["diagnostics"]["checkpoint_advanced_accounts"] == []
        assert checkpoint.read_bytes() == original
