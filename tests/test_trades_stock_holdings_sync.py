from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace

from src.application.trades.intake import process_trade_payload
from src.application.trades.stock_holdings_sync import StockHoldingsSyncDispatcher


def _context(
    deal_id: str,
    *,
    account: str = "lx",
    option_type: str | None = None,
    source: str = "push",
    apply_changes: bool = True,
) -> dict:
    return {
        "deal": SimpleNamespace(
            deal_id=deal_id,
            internal_account=account,
            option_type=option_type,
        ),
        "source": source,
        "apply_changes": apply_changes,
    }


def test_option_deal_never_calls_portfolio_sync(tmp_path: Path) -> None:
    calls: list[str] = []
    dispatcher = StockHoldingsSyncDispatcher(
        accounts=["lx"],
        state_dir=tmp_path,
        sync_fn=lambda account: calls.append(account) or {"success": True},
        debounce_sec=0,
    )

    result = dispatcher.handle_normalized_deal(
        _context("option-1", option_type="put")
    )

    assert result == {
        "status": "skipped",
        "reason": "option_deal",
        "deal_id": "option-1",
    }
    assert dispatcher.close()
    assert calls == []


def test_stock_fills_are_coalesced_and_persisted_once(tmp_path: Path) -> None:
    calls: list[str] = []
    dispatcher = StockHoldingsSyncDispatcher(
        accounts=["lx"],
        state_dir=tmp_path,
        sync_fn=lambda account: calls.append(account) or {
            "success": True,
            "account": account,
            "write_applied": True,
        },
        debounce_sec=0.05,
    )

    first = dispatcher.handle_normalized_deal(_context("stock-1"))
    duplicate = dispatcher.handle_normalized_deal(_context("stock-1"))
    second = dispatcher.handle_normalized_deal(_context("stock-2"))

    assert first["status"] == "queued"
    assert duplicate["status"] == "coalesced"
    assert second["status"] == "queued"
    assert dispatcher.wait_until_idle()
    assert dispatcher.close()
    assert calls == ["lx"]

    state = json.loads(
        (tmp_path / "lx" / "state.json").read_text(encoding="utf-8")
    )
    assert set(state["recent_succeeded_deal_ids"]) == {"stock-1", "stock-2"}
    assert state["last_batch"]["status"] == "succeeded"
    assert state["last_batch"]["deal_ids"] == ["stock-1", "stock-2"]
    events = [
        json.loads(line)
        for line in (tmp_path / "lx" / "audit.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert [event["phase"] for event in events] == [
        "stock_holdings_sync_started",
        "stock_holdings_sync_succeeded",
    ]


def test_succeeded_deal_is_deduplicated_after_restart(tmp_path: Path) -> None:
    calls: list[str] = []
    first = StockHoldingsSyncDispatcher(
        accounts=["lx"],
        state_dir=tmp_path,
        sync_fn=lambda account: calls.append(account) or {"success": True},
        debounce_sec=0,
    )
    assert first.handle_normalized_deal(_context("stock-1"))["status"] == "queued"
    assert first.close()

    second = StockHoldingsSyncDispatcher(
        accounts=["lx"],
        state_dir=tmp_path,
        sync_fn=lambda account: calls.append(account) or {"success": True},
        debounce_sec=0,
    )
    result = second.handle_normalized_deal(
        _context("stock-1", source="backfill")
    )

    assert result["status"] == "skipped"
    assert result["reason"] == "already_synchronized"
    assert second.close()
    assert calls == ["lx"]


def test_failed_sync_retries_without_rolling_back_intent(tmp_path: Path) -> None:
    attempts: list[str] = []

    def _sync(account: str) -> dict:
        attempts.append(account)
        if len(attempts) == 1:
            raise RuntimeError("temporary failure")
        return {"success": True, "account": account}

    dispatcher = StockHoldingsSyncDispatcher(
        accounts=["lx"],
        state_dir=tmp_path,
        sync_fn=_sync,
        debounce_sec=0,
        max_attempts=3,
        retry_backoff_sec=0,
    )

    assert dispatcher.handle_normalized_deal(_context("stock-1"))["status"] == "queued"
    assert dispatcher.close()
    assert attempts == ["lx", "lx"]
    state = json.loads(
        (tmp_path / "lx" / "state.json").read_text(encoding="utf-8")
    )
    assert state["last_batch"]["status"] == "succeeded"
    assert state["last_batch"]["attempts"] == 2


def test_accounts_execute_independently(tmp_path: Path) -> None:
    release_lx = threading.Event()
    sy_finished = threading.Event()

    def _sync(account: str) -> dict:
        if account == "lx":
            assert release_lx.wait(2)
            raise RuntimeError("lx unavailable")
        else:
            sy_finished.set()
        return {"success": True, "account": account}

    dispatcher = StockHoldingsSyncDispatcher(
        accounts=["lx", "sy"],
        state_dir=tmp_path,
        sync_fn=_sync,
        debounce_sec=0,
        max_attempts=1,
    )
    assert dispatcher.handle_normalized_deal(_context("lx-stock", account="lx"))["status"] == "queued"
    assert dispatcher.handle_normalized_deal(_context("sy-stock", account="sy"))["status"] == "queued"

    assert sy_finished.wait(0.5), "sy was blocked by lx"
    release_lx.set()
    assert dispatcher.close()
    lx_state = json.loads(
        (tmp_path / "lx" / "state.json").read_text(encoding="utf-8")
    )
    sy_state = json.loads(
        (tmp_path / "sy" / "state.json").read_text(encoding="utf-8")
    )
    assert lx_state["last_batch"]["status"] == "failed"
    assert sy_state["last_batch"]["status"] == "succeeded"


def test_account_queue_is_bounded_and_enqueue_does_not_wait_for_http(
    tmp_path: Path,
) -> None:
    sync_started = threading.Event()
    release_sync = threading.Event()

    def _sync(account: str) -> dict:
        sync_started.set()
        assert release_sync.wait(2)
        return {"success": True, "account": account}

    dispatcher = StockHoldingsSyncDispatcher(
        accounts=["lx"],
        state_dir=tmp_path,
        sync_fn=_sync,
        debounce_sec=0,
        queue_capacity=1,
    )
    assert dispatcher.handle_normalized_deal(_context("stock-1"))["status"] == "queued"
    assert sync_started.wait(0.5)
    assert dispatcher.handle_normalized_deal(_context("stock-2"))["status"] == "queued"
    rejected = dispatcher.handle_normalized_deal(_context("stock-3"))
    assert rejected["status"] == "rejected"
    assert rejected["reason"] == "queue_full"
    release_sync.set()
    assert dispatcher.close()


def test_dispatcher_callback_failure_does_not_change_om_result(tmp_path: Path) -> None:
    deal = SimpleNamespace(
        deal_id="stock-1",
        internal_account="lx",
        option_type=None,
        position_effect=None,
        to_dict=lambda: {
            "deal_id": "stock-1",
            "internal_account": "lx",
            "option_type": None,
        },
    )

    class _Result:
        status = "skipped"
        action = None
        reason = "not_option_deal"
        deal_id = "stock-1"
        account = "lx"
        operations: list = []

        def to_dict(self) -> dict:
            return {
                "status": self.status,
                "action": self.action,
                "reason": self.reason,
                "deal_id": self.deal_id,
                "account": self.account,
                "operations": [],
            }

    events: list[dict] = []
    out = process_trade_payload(
        {"deal_id": "stock-1"},
        repo=object(),
        state_path=tmp_path / "intake-state.json",
        audit_path=tmp_path / "intake-audit.jsonl",
        account_mapping={"REAL_1": "lx"},
        apply_changes=True,
        load_trade_intake_state_fn=lambda _path: {},
        write_trade_intake_state_fn=lambda *_args, **_kwargs: None,
        upsert_deal_state_fn=lambda state, **_kwargs: state,
        append_trade_intake_audit_fn=lambda _path, event: events.append(
            dict(event)
        ),
        enrich_trade_payload_fn=None,
        normalize_trade_deal_fn=lambda *_args, **_kwargs: deal,
        resolve_trade_deal_fn=lambda *_args, **_kwargs: _Result(),
        on_stock_holdings_sync_fn=lambda _context: (_ for _ in ()).throw(
            RuntimeError("PM unavailable")
        ),
    )

    assert out["status"] == "skipped"
    assert out["reason"] == "not_option_deal"
    assert out["stock_holdings_sync"]["status"] == "failed"
    assert out["stock_holdings_sync"]["reason"] == "dispatcher_callback_exception"
    assert any(
        event.get("phase") == "stock_holdings_sync_intent"
        for event in events
    )


def test_ordinary_stock_skips_om_ledger_and_triggers_pm_sync(
    tmp_path: Path,
) -> None:
    calls: list[str] = []
    dispatcher = StockHoldingsSyncDispatcher(
        accounts=["lx"],
        state_dir=tmp_path / "pm-sync",
        sync_fn=lambda account: calls.append(account) or {
            "success": True,
            "account": account,
        },
        debounce_sec=0,
    )
    deal = SimpleNamespace(
        deal_id="stock-1",
        internal_account="lx",
        option_type=None,
        position_effect=None,
        to_dict=lambda: {
            "deal_id": "stock-1",
            "internal_account": "lx",
            "option_type": None,
        },
    )

    class _Result:
        status = "skipped"
        action = None
        reason = "not_option_deal"
        deal_id = "stock-1"
        account = "lx"
        operations: list = []

        def to_dict(self) -> dict:
            return {
                "status": self.status,
                "action": self.action,
                "reason": self.reason,
                "deal_id": self.deal_id,
                "account": self.account,
                "operations": [],
            }

    out = process_trade_payload(
        {"deal_id": "stock-1"},
        repo=object(),
        state_path=tmp_path / "intake-state.json",
        audit_path=tmp_path / "intake-audit.jsonl",
        account_mapping={"REAL_1": "lx"},
        apply_changes=True,
        load_trade_intake_state_fn=lambda _path: {},
        write_trade_intake_state_fn=lambda *_args, **_kwargs: None,
        upsert_deal_state_fn=lambda state, **_kwargs: state,
        append_trade_intake_audit_fn=lambda *_args, **_kwargs: None,
        enrich_trade_payload_fn=None,
        normalize_trade_deal_fn=lambda *_args, **_kwargs: deal,
        resolve_trade_deal_fn=lambda *_args, **_kwargs: _Result(),
        on_stock_holdings_sync_fn=dispatcher.handle_normalized_deal,
    )

    assert out["status"] == "skipped"
    assert out["reason"] == "not_option_deal"
    assert out["stock_holdings_sync"]["status"] == "queued"
    assert dispatcher.close()
    assert calls == ["lx"]


def test_assignment_updates_om_and_pm_independently(tmp_path: Path) -> None:
    calls: list[str] = []
    dispatcher = StockHoldingsSyncDispatcher(
        accounts=["lx"],
        state_dir=tmp_path / "pm-sync",
        sync_fn=lambda account: calls.append(account) or {
            "success": True,
            "account": account,
        },
        debounce_sec=0,
    )
    deal = SimpleNamespace(
        deal_id="assignment-stock-1",
        internal_account="lx",
        option_type=None,
        position_effect=None,
        to_dict=lambda: {
            "deal_id": "assignment-stock-1",
            "internal_account": "lx",
            "option_type": None,
        },
    )

    class _Result:
        status = "applied"
        action = "assignment"
        reason = "assignment_recorded"
        deal_id = "assignment-stock-1"
        account = "lx"
        operations: list = []

        def to_dict(self) -> dict:
            return {
                "status": self.status,
                "action": self.action,
                "reason": self.reason,
                "deal_id": self.deal_id,
                "account": self.account,
                "operations": [],
            }

    out = process_trade_payload(
        {"deal_id": "assignment-stock-1"},
        repo=object(),
        state_path=tmp_path / "intake-state.json",
        audit_path=tmp_path / "intake-audit.jsonl",
        account_mapping={"REAL_1": "lx"},
        apply_changes=True,
        load_trade_intake_state_fn=lambda _path: {},
        write_trade_intake_state_fn=lambda *_args, **_kwargs: None,
        upsert_deal_state_fn=lambda state, **_kwargs: state,
        append_trade_intake_audit_fn=lambda *_args, **_kwargs: None,
        enrich_trade_payload_fn=None,
        normalize_trade_deal_fn=lambda *_args, **_kwargs: deal,
        resolve_trade_deal_fn=lambda *_args, **_kwargs: _Result(),
        on_stock_holdings_sync_fn=dispatcher.handle_normalized_deal,
    )

    assert out["status"] == "applied"
    assert out["action"] == "assignment"
    assert out["stock_holdings_sync"]["status"] == "queued"
    assert dispatcher.close()
    assert calls == ["lx"]


def test_auto_intake_factory_wires_apply_mode_to_portfolio_client(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from src.application.trades import auto_intake

    calls: list[tuple[str, float]] = []
    monkeypatch.setattr(
        auto_intake,
        "sync_portfolio_holdings",
        lambda account, *, timeout_sec: calls.append(
            (account, timeout_sec)
        )
        or {"success": True, "account": account},
    )
    dispatcher = auto_intake._build_stock_holdings_sync_dispatcher(
        intake_cfg={
            "holdings_sync": {
                "enabled": True,
                "state_dir": "pm-sync",
                "debounce_sec": 0,
                "request_timeout_sec": 45,
                "max_attempts": 1,
                "retry_backoff_sec": 0,
                "queue_capacity": 10,
                "recent_deal_limit": 20,
            }
        },
        sources=[{"account_mapping": {"REAL_1": "lx"}}],
        runtime_root=tmp_path,
        apply_changes=True,
    )

    assert dispatcher is not None
    assert dispatcher.handle_normalized_deal(_context("stock-1"))["status"] == "queued"
    assert dispatcher.close()
    assert calls == [("lx", 45.0)]
    assert (tmp_path / "pm-sync" / "lx" / "state.json").exists()
