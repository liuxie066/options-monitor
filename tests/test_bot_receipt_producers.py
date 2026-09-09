"""Real business producers retain source market for the public receipt reader."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from domain.domain.option_position_lots import OpenPositionCommand
from domain.storage.repositories import state_repo
from src.application.agent_tools import receipts
from src.application.ledger.manual_trades import persist_manual_open_event
from src.application.ledger.repository import SQLiteOptionPositionsRepository
from src.application.multi_tick.misc import AccountResult
from src.application.multi_tick_finalization import finalize_no_account_notification
from src.application.positions import maintenance, maintenance_receipt
from src.application.research.redaction import redact_text
from src.application.trades.lifecycle_outbox import (
    QUIET_WINDOW_MS,
    build_notification_batch_route,
    dispatch_notification_batch_once,
)
from src.application.trades.normalizer import normalize_trade_deal
from src.application.trades.resolver import resolve_trade_deal
from tests.test_trades_resolver_close import _deal


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("OM_RUNTIME_ROOT", str(tmp_path))
    cfg = {"accounts": ["lx"], "_generated": {"market": "hk", "source_format": "yaml"}}
    path = tmp_path / "config.hk.json"
    path.write_text(json.dumps(cfg))
    monkeypatch.setattr(receipts, "_cursor_key", lambda: "fixture-cursor-key")
    repo = SQLiteOptionPositionsRepository(tmp_path / "output_shared/state/option_positions.sqlite3")
    return tmp_path, cfg, path, repo


def _read(runtime, **filters):
    root, cfg, path, repo = runtime
    return receipts.RECEIPT_READ_TOOL.call({"config_path": str(path), "config_key": "hk", **filters})[0]


def _dispatch(repo, calls):
    pending = repo.list_trade_lifecycle_notifications()
    now = max(row["created_at_ms"] for row in pending) + QUIET_WINDOW_MS
    route = build_notification_batch_route(provider="fixture", channel="fixture", target="fixture")
    def send(payload):
        calls.append(payload)
        return {"status": "confirmed", "delivery_confirmed": True, "message_id": "fixture"}
    result = dispatch_notification_batch_once(repo, route=route, send_fn=send, now_ms=now, allowed_accounts={"lx"})
    dispatch_notification_batch_once(repo, route=route, send_fn=send, now_ms=now + 100000, allowed_accounts={"lx"})
    assert len(calls) == 1
    return result["batch"]


@pytest.mark.parametrize("normalized", [True, False])
def test_normal_close_producer_dispatch_and_public_read(runtime, normalized):
    root, cfg, path, repo = runtime
    for opened_at, contracts in ((100, 1), (200, 2)):
        persist_manual_open_event(repo, OpenPositionCommand(
            broker="富途", account="lx", symbol="0700.HK", option_type="put", side="short",
            contracts=contracts, currency="HKD", strike=480, multiplier=100,
            expiration_ymd="2026-04-29", premium_per_share=3.93, opened_at_ms=opened_at,
        ))
    deal = _deal(contracts=3, trade_time_ms=5000)
    if normalized:
        deal = normalize_trade_deal({
            "trd_acc_id": "REAL_1", "deal_id": "deal-close-1", "order_id": "order-1",
            "broker_account_id": "futu:REAL:REAL_1", "environment": "REAL",
            "external_id_namespace": "futu.deal", "external_order_namespace": "futu.order",
            "code": "0700.HK", "market": "HK", "option_type": "put", "side": "buy",
            "position_effect": "close", "qty": 3, "price": "1.2", "strike": "480",
            "multiplier": 100, "expiration": "20260429", "currency": "HKD", "trade_time_ms": 5000,
        }, futu_account_mapping={"REAL_1": "lx"}, repo_base=root, allow_opend_refresh=False)
        assert deal.execution_input["instrument_ref"]["market"] == "HK"
    result = resolve_trade_deal(deal, repo=repo, state={}, apply_changes=True)
    assert result.status == "applied"
    events = repo.list_trade_events()
    calls = []
    batch = _dispatch(repo, calls)
    value = _read(runtime, type="trade", event_id="trade_lifecycle_batch:" + batch["batch_id"])
    assert repo.list_trade_events() == events
    assert all(row["fields"]["contracts_open"] == 0 for row in repo.list_position_lots())
    if normalized:
        assert len(value["rows"]) == 1, value
        row = value["rows"][0]
        assert row["market"] == "HK" and row["delivery_state"] == "confirmed"
        assert row["body_provenance"] == "frozen"
        assert row["receipt_body"] == redact_text(batch["provider_receipt"]["rendered_message"])
        # Replay the real business input: no second durable economic or delivery effect.
        resolve_trade_deal(deal, repo=repo, state={}, apply_changes=True)
        assert repo.list_trade_events() == events
        assert len(repo.list_trade_lifecycle_notifications()) == 1
    else:
        assert not value["rows"] and value["status"] == "unavailable"
        assert any(item["reason"] == "receipt_market_unlinkable" for item in value["missing_sources"])


def test_actual_maintenance_filter_survives_send_persistence_and_retry(runtime, monkeypatch):
    root, cfg, path, repo = runtime
    data = root / "data.json"
    data.write_text(json.dumps({"option_positions": {"sqlite_path": str(repo.db_path)}}))
    cfg = {**cfg, "portfolio": {"data_config": str(data), "broker": "富途"},
           "notifications": {"provider": "wechat_clawbot", "target": "fixture"},
           "option_positions": {"auto_close": {"receipt": {"notify_noop": True}}}}
    sent = []
    def send(**kwargs):
        sent.append(kwargs["message"])
        return {"command_ok": True, "delivery_confirmed": True, "message_id": "fixture", "returncode": 0}
    # Only transport is mocked; route, rendering, delivery normalization and persistence execute.
    original = maintenance_receipt.send_auto_close_receipt
    monkeypatch.setattr(maintenance_receipt, "send_auto_close_receipt", lambda **kw: original(
        **kw, send_fn=send, normalize_fn=lambda value: value))
    result = maintenance.run_expired_position_maintenance_for_account(
        base=root, cfg=cfg, account="lx", report_dir=root / "reports", as_of_ms=1788934554000)
    assert result["market_filter"] == "HK" and result["applied_closed"] == 0
    assert result["receipt"]["delivery_confirmed"], result
    identity = maintenance_receipt.build_auto_close_receipt_identity(config=cfg, result=result)
    assert "market" not in identity["receipt_key_fields"]
    assert identity == maintenance_receipt.build_auto_close_receipt_identity(config=cfg, result={**result, "market_filter": "US"})
    value = _read(runtime, type="monitor", event_id="maintenance:" + identity["receipt_key"])
    assert len(value["rows"]) == 1, value
    row = value["rows"][0]
    assert row["market"] == "HK" and row["delivery_state"] == "confirmed"
    assert row["receipt_body"] == redact_text(sent[0]) and row["body_provenance"] == "frozen"
    retry = maintenance_receipt.safe_send_auto_close_receipt(base=root, config=cfg, dry_run=False, result=result)
    assert retry["reason"] == "skipped_duplicate_confirmed" and len(sent) == 1
    retried = _read(runtime, type="monitor", event_id="maintenance:" + identity["receipt_key"])["rows"][0]
    assert all(retried[key] == row[key] for key in ("source_ref", "market", "receipt_body", "delivery_state"))
    saved = root / "output_accounts/lx/state/auto_close_receipts.json"
    state = json.loads(saved.read_text())
    state["receipts"][identity["receipt_key"]]["result_summary"].pop("market_filter")
    saved.write_text(json.dumps(state))
    assert not _read(runtime, type="monitor", event_id="maintenance:" + identity["receipt_key"])["rows"]


@pytest.mark.parametrize("actual,scheduler,expected", [
    (["HK"], ["HK"], "HK"), ([], ["HK"], "HK"), (["US"], [], "US"),
    (["HK", "US"], ["HK"], None), (["HK"], ["US"], None), ([], [], None),
])
def test_real_scheduled_finalization_uses_same_run_market(runtime, actual, scheduler, expected):
    root, cfg, path, repo = runtime
    metrics = {"markets_to_run": actual, "scheduler_markets": scheduler, "run_dir": "fixture-run"}
    rc = finalize_no_account_notification(
        base=root, run_id="fixture-run", runlog=SimpleNamespace(safe_event=lambda *a, **kw: None),
        results=[AccountResult("lx", False, False, "scan_failed", "")], tick_metrics=metrics,
        no_send=True, state_repo=state_repo, utc_now_fn=lambda: "2026-09-09T08:00:00+00:00",
        audit_fn=lambda *a, **kw: None, safe_data_fn=lambda value: value, on_success=lambda: None,
        reason="scan_failed", run_end_outcome="error", return_code=1)
    assert rc == 1
    saved = root / "output_runs/fixture-run/accounts/lx/state/last_run.json"
    payload = json.loads(saved.read_text())
    assert payload.get("market") == expected
    value = _read(runtime, type="scheduled", run_id="fixture-run")
    if expected == "HK":
        assert len(value["rows"]) == 1, value
        row = value["rows"][0]
        assert row["market"] == "HK" and row["delivery_state"] == "unknown"
        assert row["body_provenance"] == "reconstructed" and '"scan_failed"' in row["receipt_body"]
    else:
        assert not value["rows"]  # Foreign, mixed and unknown actual runs never inherit today's HK config.
    payload.pop("market", None)
    saved.write_text(json.dumps(payload))
    assert not _read(runtime, type="scheduled", run_id="fixture-run")["rows"]


def test_lifecycle_case_market_survives_real_state_transition_and_dispatch(runtime):
    from src.application.ledger.writer import (
        advance_lifecycle_case_state_atomically,
        discover_expired_lifecycle_cases_atomically,
    )

    root, cfg, path, repo = runtime
    persist_manual_open_event(repo, OpenPositionCommand(
        broker="富途", account="lx", symbol="0700.HK", option_type="put", side="short",
        contracts=1, currency="HKD", strike=480, multiplier=100, expiration_ymd="2026-04-29",
        premium_per_share=3.93, opened_at_ms=1775000000000))
    discovery = discover_expired_lifecycle_cases_atomically(
        repo, account="lx", observed_at_ms=1788934554000, apply_changes=True)
    case_id = discovery["created_case_ids"][0]
    case = repo.get_trade_lifecycle_case(case_id)
    assert case["market"] == "HK"
    advance_lifecycle_case_state_atomically(repo, case_id=case_id, status="needs_review",
        derived_summary={"reason_codes": ["fixture_review"]}, public_transition="needs_review")
    batch = _dispatch(repo, [])
    value = _read(runtime, type="trade", event_id="trade_lifecycle_batch:" + batch["batch_id"])
    assert len(value["rows"]) == 1, value
    assert value["rows"][0]["market"] == case["market"]
    assert value["rows"][0]["delivery_state"] == "confirmed"
