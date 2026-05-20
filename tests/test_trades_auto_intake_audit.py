from __future__ import annotations

from pathlib import Path

from src.application.ledger.store_resolution import LedgerStoreResolution
from src.application.trades.normalizer import NormalizedTradeDeal
from src.application.trades.state import upsert_deal_state
from src.application.trades.intake import build_trade_intake_audit_event, process_trade_payload


def test_build_audit_event_promotes_multiplier_source_to_top_level() -> None:
    deal = NormalizedTradeDeal(
        broker="富途",
        futu_account_id="REAL_1",
        internal_account="lx",
        deal_id="deal-1",
        order_id="order-1",
        symbol="0700.HK",
        option_type="put",
        side="sell",
        position_effect="open",
        contracts=2,
        price=3.93,
        strike=480.0,
        multiplier=100,
        multiplier_source="cache",
        expiration_ymd="2026-04-29",
        currency="HKD",
        trade_time_ms=1000,
        raw_payload={"deal_id": "deal-1"},
        visible_account_fields={"trade_acc_id": "REAL_1"},
        account_mapping_keys=["REAL_1"],
        normalization_diagnostics={"multiplier_resolution": {"selected_source": "cache"}},
    )

    event = build_trade_intake_audit_event("normalized", deal=deal)

    assert event["phase"] == "normalized"
    assert event["deal_id"] == "deal-1"
    assert event["account"] == "lx"
    assert event["futu_account_id"] == "REAL_1"
    assert event["multiplier"] == 100
    assert event["multiplier_source"] == "cache"
    assert event["deal"]["multiplier_source"] == "cache"
    assert event["visible_account_fields"] == {"trade_acc_id": "REAL_1"}
    assert event["account_mapping_keys"] == ["REAL_1"]
    assert event["normalization_diagnostics"]["multiplier_resolution"]["selected_source"] == "cache"


def test_process_payload_appends_ledger_persist_audit_on_applied(monkeypatch, tmp_path: Path) -> None:
    import src.application.trades.auto_intake as intake

    deal = NormalizedTradeDeal(
        broker="富途",
        futu_account_id="REAL_1",
        internal_account="lx",
        deal_id="deal-1",
        order_id="order-1",
        symbol="0700.HK",
        option_type="put",
        side="sell",
        position_effect="open",
        contracts=2,
        price=3.93,
        strike=480.0,
        multiplier=100,
        multiplier_source="cache",
        expiration_ymd="2026-04-29",
        currency="HKD",
        trade_time_ms=1000,
        raw_payload={"deal_id": "deal-1"},
    )

    events: list[dict] = []

    class _Result:
        status = "applied"
        action = "open"
        reason = "applied_open"
        deal_id = "deal-1"
        account = "lx"
        operations = [{"record_id": "rec_1"}]

        def to_dict(self) -> dict:
            return {
                "status": self.status,
                "action": self.action,
                "reason": self.reason,
                "deal_id": self.deal_id,
                "account": self.account,
                "operations": self.operations,
            }

    monkeypatch.setattr(intake, "load_trade_intake_state", lambda _path: {})
    monkeypatch.setattr(intake, "write_trade_intake_state", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(intake, "upsert_deal_state", lambda state, **_kwargs: state)
    monkeypatch.setattr(intake, "append_trade_intake_audit", lambda _path, event: events.append(dict(event)))
    class _EnrichResult:
        payload = {"deal_id": "deal-1"}
        diagnostics = {"matched_via": "not_found"}

    monkeypatch.setattr(intake, "enrich_trade_push_payload_with_account_id", lambda payload, **kwargs: _EnrichResult())
    monkeypatch.setattr(intake, "normalize_trade_deal", lambda payload, futu_account_mapping=None: deal)
    monkeypatch.setattr(intake, "resolve_trade_deal", lambda *args, **kwargs: _Result())
    out = intake._process_payload(
        {"deal_id": "deal-1"},
        repo=object(),
        state_path=tmp_path / "state.json",
        audit_path=tmp_path / "audit.jsonl",
        account_mapping={"REAL_1": "lx"},
        futu_account_ids=["REAL_1"],
        apply_changes=True,
        host="127.0.0.1",
        port=11111,
    )

    assert out["status"] == "applied"
    assert any(event.get("phase") == "ledger_persisted" for event in events)


def test_process_payload_close_invalidates_context_and_attaches_projection_diagnostics(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    account_ctx = runtime_root / "output_accounts" / "lx" / "state" / "option_positions_context.json"
    shared_ctx = runtime_root / "output_shared" / "state" / "option_positions_context.shared.json"
    account_ctx.parent.mkdir(parents=True, exist_ok=True)
    shared_ctx.parent.mkdir(parents=True, exist_ok=True)
    account_ctx.write_text("{}", encoding="utf-8")
    shared_ctx.write_text("{}", encoding="utf-8")

    deal = NormalizedTradeDeal(
        broker="富途",
        futu_account_id="REAL_1",
        internal_account="lx",
        deal_id="deal-close-1",
        order_id="order-1",
        symbol="0700.HK",
        option_type="call",
        side="buy",
        position_effect="close",
        contracts=2,
        price=1.2,
        strike=510.0,
        multiplier=100,
        multiplier_source="cache",
        expiration_ymd="2026-05-28",
        currency="HKD",
        trade_time_ms=1779260747577,
        raw_payload={"deal_id": "deal-close-1"},
    )

    class _Repo:
        ledger_store = LedgerStoreResolution(
            runtime_root=runtime_root,
            data_config_path=runtime_root / "portfolio.runtime.json",
            sqlite_path=runtime_root / "output_shared" / "state" / "option_positions.sqlite3",
            runtime_root_source="argument",
            sqlite_path_source="runtime_root",
            db_exists=True,
            db_size_bytes=1,
            trade_event_count=2,
            position_lot_count=1,
        )

    class _Result:
        status = "applied"
        action = "close"
        reason = "applied_close"
        deal_id = "deal-close-1"
        account = "lx"
        operations = []

        def to_dict(self) -> dict:
            return {
                "status": self.status,
                "action": self.action,
                "reason": self.reason,
                "deal_id": self.deal_id,
                "account": self.account,
                "operations": self.operations,
                "diagnostics": {
                    "post_write_projection_verification": {
                        "ok": True,
                        "checks": [
                            {
                                "record_id": "lot_1",
                                "contracts_open_before": 2,
                                "contracts_to_close": 2,
                                "expected_contracts_open_after": 0,
                                "actual_contracts_open_after": 0,
                            }
                        ],
                        "errors": [],
                    }
                },
            }

    events: list[dict] = []
    out = process_trade_payload(
        {"deal_id": "deal-close-1"},
        repo=_Repo(),
        state_path=tmp_path / "state.json",
        audit_path=tmp_path / "audit.jsonl",
        account_mapping={"REAL_1": "lx"},
        apply_changes=True,
        load_trade_intake_state_fn=lambda _path: {},
        write_trade_intake_state_fn=lambda *_args, **_kwargs: None,
        upsert_deal_state_fn=lambda state, **_kwargs: state,
        append_trade_intake_audit_fn=lambda _path, event: events.append(dict(event)),
        enrich_trade_payload_fn=None,
        normalize_trade_deal_fn=lambda _payload, futu_account_mapping=None: deal,
        resolve_trade_deal_fn=lambda *_args, **_kwargs: _Result(),
    )

    assert out["projection_status"] == "recorded_and_projected"
    assert out["ledger_store"]["sqlite_path"].endswith("option_positions.sqlite3")
    assert out["contracts_open_before"] == 2
    assert out["contracts_open_after"] == 0
    assert out["context_invalidation"]["ok"] is True
    assert not account_ctx.exists()
    assert not shared_ctx.exists()
    resolved = [event for event in events if event.get("phase") == "resolved"][-1]
    assert resolved["result"]["projection_status"] == "recorded_and_projected"


def test_process_payload_appends_enriched_audit_when_lookup_adds_account(monkeypatch, tmp_path: Path) -> None:
    import src.application.trades.auto_intake as intake

    events: list[dict] = []

    monkeypatch.setattr(intake, "load_trade_intake_state", lambda _path: {})
    monkeypatch.setattr(intake, "write_trade_intake_state", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(intake, "upsert_deal_state", lambda state, **_kwargs: state)
    monkeypatch.setattr(intake, "append_trade_intake_audit", lambda _path, event: events.append(dict(event)))
    class _EnrichResult:
        payload = {"deal_id": "deal-1", "futu_account_id": "123"}
        diagnostics = {"matched_via": "deal_lookup_by_acc_id"}

    monkeypatch.setattr(intake, "enrich_trade_push_payload_with_account_id", lambda payload, **kwargs: _EnrichResult())

    deal = NormalizedTradeDeal(
        broker="富途",
        futu_account_id="123",
        internal_account="lx",
        deal_id="deal-1",
        order_id="order-1",
        symbol="0700.HK",
        option_type="put",
        side="sell",
        position_effect="open",
        contracts=2,
        price=3.93,
        strike=480.0,
        multiplier=100,
        multiplier_source="cache",
        expiration_ymd="2026-04-29",
        currency="HKD",
        trade_time_ms=1000,
        raw_payload={"deal_id": "deal-1"},
    )
    monkeypatch.setattr(intake, "normalize_trade_deal", lambda payload, futu_account_mapping=None: deal)

    class _Result:
        status = "dry_run"
        action = "open"
        reason = "preview_open"
        deal_id = "deal-1"
        account = "lx"
        operations = []

        def to_dict(self) -> dict:
            return {
                "status": self.status,
                "action": self.action,
                "reason": self.reason,
                "deal_id": self.deal_id,
                "account": self.account,
                "operations": self.operations,
            }

    monkeypatch.setattr(intake, "resolve_trade_deal", lambda *args, **kwargs: _Result())

    intake._process_payload(
        {"deal_id": "deal-1"},
        repo=object(),
        state_path=tmp_path / "state.json",
        audit_path=tmp_path / "audit.jsonl",
        account_mapping={"123": "lx"},
        futu_account_ids=["123"],
        apply_changes=False,
        host="127.0.0.1",
        port=11111,
    )

    assert any(event.get("phase") == "enriched" and event.get("payload", {}).get("futu_account_id") == "123" for event in events)
    assert any(event.get("phase") == "enrichment_lookup" and event.get("enrichment", {}).get("matched_via") == "deal_lookup_by_acc_id" for event in events)


def test_build_audit_event_promotes_missing_account_mapping_diagnostics() -> None:
    deal = NormalizedTradeDeal(
        broker="富途",
        futu_account_id="281756479859383816",
        internal_account=None,
        deal_id="deal-2",
        order_id="order-2",
        symbol="0700.HK",
        option_type="put",
        side="sell",
        position_effect="open",
        contracts=1,
        price=1.0,
        strike=480.0,
        multiplier=100,
        multiplier_source="cache",
        expiration_ymd="2026-04-29",
        currency="HKD",
        trade_time_ms=1000,
        raw_payload={"deal_id": "deal-2", "trade_acc_id": "281756479859383816"},
        visible_account_fields={"trade_acc_id": "281756479859383816"},
        account_mapping_keys=["999999999999999999"],
    )

    event = build_trade_intake_audit_event(
        "resolved",
        deal=deal,
        result={
            "status": "unresolved",
            "action": None,
            "reason": "missing_account_mapping:futu_account_id=281756479859383816",
            "deal_id": "deal-2",
            "account": None,
            "operations": [],
            "diagnostics": {
                "futu_account_id": "281756479859383816",
                "visible_account_fields": {"trade_acc_id": "281756479859383816"},
                "account_mapping_keys": ["999999999999999999"],
            },
        },
    )

    assert event["futu_account_id"] == "281756479859383816"
    assert event["diagnostics"]["account_mapping_keys"] == ["999999999999999999"]


def test_build_audit_event_keeps_shared_visible_account_fields_after_normalization() -> None:
    deal = NormalizedTradeDeal(
        broker="富途",
        futu_account_id="FUTU_1",
        internal_account="lx",
        deal_id="deal-3",
        order_id="order-3",
        symbol="NVDA",
        option_type="call",
        side="sell",
        position_effect="open",
        contracts=1,
        price=1.0,
        strike=100.0,
        multiplier=100,
        multiplier_source="payload",
        expiration_ymd="2026-06-18",
        currency="USD",
        trade_time_ms=1000,
        raw_payload={"trade_acc_id": "TRADE_1", "account_id": "ACCOUNT_1", "futu_account_id": "FUTU_1"},
        visible_account_fields={
            "futu_account_id": "FUTU_1",
            "account_id": "ACCOUNT_1",
            "trade_acc_id": "TRADE_1",
        },
        account_mapping_keys=["FUTU_1"],
    )

    event = build_trade_intake_audit_event("normalized", deal=deal)

    assert event["futu_account_id"] == "FUTU_1"
    assert event["visible_account_fields"] == {
        "futu_account_id": "FUTU_1",
        "account_id": "ACCOUNT_1",
        "trade_acc_id": "TRADE_1",
    }


def test_process_payload_records_retryable_unresolved_diagnostics(tmp_path: Path) -> None:
    deal = NormalizedTradeDeal(
        broker="富途",
        futu_account_id="REAL_1",
        internal_account="lx",
        deal_id="deal-retry-1",
        order_id="order-retry-1",
        symbol="9992.HK",
        option_type="put",
        side="sell",
        position_effect="open",
        contracts=1,
        price=6.3,
        strike=150.0,
        multiplier=None,
        multiplier_source=None,
        expiration_ymd="2026-05-28",
        currency="HKD",
        trade_time_ms=1000,
        raw_payload={"deal_id": "deal-retry-1"},
        normalization_diagnostics={
            "symbol": {"canonical": "9992.HK", "raw_fields": {"code": "HK.POP260528P150000"}},
            "multiplier_resolution": {"canonical_symbol": "9992.HK", "attempted_sources": []},
        },
    )

    class _Result:
        status = "unresolved"
        action = "open"
        reason = "missing_required_fields:multiplier"
        deal_id = "deal-retry-1"
        account = "lx"
        operations: list[dict] = []
        diagnostics = {
            "retryable": True,
            "missing_fields": ["multiplier"],
            "multiplier_resolution": {"canonical_symbol": "9992.HK"},
        }

        def to_dict(self) -> dict:
            return {
                "status": self.status,
                "action": self.action,
                "reason": self.reason,
                "deal_id": self.deal_id,
                "account": self.account,
                "operations": self.operations,
                "diagnostics": self.diagnostics,
            }

    events: list[dict] = []
    writes: list[dict] = []
    initial_state = {
        "processed_deal_ids": {},
        "failed_deal_ids": {},
        "unresolved_deal_ids": {"deal-retry-1": {"status": "unresolved", "retryable": True, "attempt_count": 2}},
    }

    out = process_trade_payload(
        {"deal_id": "deal-retry-1"},
        repo=object(),
        state_path=tmp_path / "state.json",
        audit_path=tmp_path / "audit.jsonl",
        account_mapping={"REAL_1": "lx"},
        apply_changes=True,
        load_trade_intake_state_fn=lambda _path: initial_state,
        write_trade_intake_state_fn=lambda _path, state: writes.append(dict(state)),
        upsert_deal_state_fn=upsert_deal_state,
        append_trade_intake_audit_fn=lambda _path, event: events.append(dict(event)),
        enrich_trade_payload_fn=None,
        normalize_trade_deal_fn=lambda payload, futu_account_mapping=None: deal,
        resolve_trade_deal_fn=lambda *_args, **_kwargs: _Result(),
    )

    assert out["status"] == "unresolved"
    state_item = writes[-1]["unresolved_deal_ids"]["deal-retry-1"]
    assert state_item["retryable"] is True
    assert state_item["attempt_count"] == 3
    assert state_item["diagnostics"]["missing_fields"] == ["multiplier"]
    assert any(event.get("phase") == "resolved" and event.get("diagnostics", {}).get("retryable") is True for event in events)


def test_process_payload_records_failed_state_when_resolver_raises(tmp_path: Path) -> None:
    deal = NormalizedTradeDeal(
        broker="富途",
        futu_account_id="REAL_1",
        internal_account="lx",
        deal_id="deal-failed-1",
        order_id="order-failed-1",
        symbol="0700.HK",
        option_type="put",
        side="sell",
        position_effect="open",
        contracts=1,
        price=3.93,
        strike=480.0,
        multiplier=100,
        multiplier_source="payload",
        expiration_ymd="2026-04-29",
        currency="HKD",
        trade_time_ms=1000,
        raw_payload={"deal_id": "deal-failed-1"},
    )
    events: list[dict] = []
    writes: list[dict] = []

    out = process_trade_payload(
        {"deal_id": "deal-failed-1"},
        repo=object(),
        state_path=tmp_path / "state.json",
        audit_path=tmp_path / "audit.jsonl",
        account_mapping={"REAL_1": "lx"},
        apply_changes=True,
        load_trade_intake_state_fn=lambda _path: {"processed_deal_ids": {}, "failed_deal_ids": {}, "unresolved_deal_ids": {}},
        write_trade_intake_state_fn=lambda _path, state: writes.append(dict(state)),
        upsert_deal_state_fn=upsert_deal_state,
        append_trade_intake_audit_fn=lambda _path, event: events.append(dict(event)),
        enrich_trade_payload_fn=None,
        normalize_trade_deal_fn=lambda payload, futu_account_mapping=None: deal,
        resolve_trade_deal_fn=lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    assert out["status"] == "failed"
    assert out["reason"] == "exception:RuntimeError"
    failed = writes[-1]["failed_deal_ids"]["deal-failed-1"]
    assert failed["status"] == "failed"
    assert failed["diagnostics"]["exception_type"] == "RuntimeError"
    assert any(event.get("phase") == "failed" and event.get("deal_id") == "deal-failed-1" for event in events)


def test_process_payload_records_receipt_state_after_applied(tmp_path: Path) -> None:
    deal = NormalizedTradeDeal(
        broker="富途",
        futu_account_id="REAL_1",
        internal_account="lx",
        deal_id="deal-receipt-1",
        order_id="order-receipt-1",
        symbol="NVDA",
        option_type="put",
        side="sell",
        position_effect="open",
        contracts=1,
        price=1.23,
        strike=120.0,
        multiplier=100,
        multiplier_source="payload",
        expiration_ymd="2026-06-19",
        currency="USD",
        trade_time_ms=1000,
        raw_payload={"deal_id": "deal-receipt-1"},
    )

    class _Result:
        status = "applied"
        action = "open"
        reason = "applied_open"
        deal_id = "deal-receipt-1"
        account = "lx"
        operations = [{"record_id": "lot_deal-receipt-1"}]

        def to_dict(self) -> dict:
            return {
                "status": self.status,
                "action": self.action,
                "reason": self.reason,
                "deal_id": self.deal_id,
                "account": self.account,
                "operations": self.operations,
            }

    writes: list[dict] = []
    events: list[dict] = []

    out = process_trade_payload(
        {"deal_id": "deal-receipt-1"},
        repo=object(),
        state_path=tmp_path / "state.json",
        audit_path=tmp_path / "audit.jsonl",
        account_mapping={"REAL_1": "lx"},
        apply_changes=True,
        load_trade_intake_state_fn=lambda _path: {"processed_deal_ids": {}, "failed_deal_ids": {}, "unresolved_deal_ids": {}},
        write_trade_intake_state_fn=lambda _path, state: writes.append(dict(state)),
        upsert_deal_state_fn=upsert_deal_state,
        append_trade_intake_audit_fn=lambda _path, event: events.append(dict(event)),
        enrich_trade_payload_fn=None,
        normalize_trade_deal_fn=lambda payload, futu_account_mapping=None: deal,
        resolve_trade_deal_fn=lambda *_args, **_kwargs: _Result(),
        on_result_fn=lambda _context: {"status": "sent", "delivery_confirmed": True, "message_id": "msg-1"},
    )

    assert out["receipt"]["status"] == "sent"
    receipt = writes[-1]["processed_deal_ids"]["deal-receipt-1"]["receipt"]
    assert receipt["delivery_confirmed"] is True
    assert receipt["message_id"] == "msg-1"
    assert receipt["attempt_count"] == 1
    assert any(event.get("phase") == "receipt_sent" and event.get("deal_id") == "deal-receipt-1" for event in events)


def test_process_payload_preserves_confirmed_receipt_on_duplicate_skip(tmp_path: Path) -> None:
    deal = NormalizedTradeDeal(
        broker="富途",
        futu_account_id="REAL_1",
        internal_account="lx",
        deal_id="deal-duplicate-1",
        order_id="order-duplicate-1",
        symbol="NVDA",
        option_type="put",
        side="sell",
        position_effect="open",
        contracts=1,
        price=1.23,
        strike=120.0,
        multiplier=100,
        multiplier_source="payload",
        expiration_ymd="2026-06-19",
        currency="USD",
        trade_time_ms=1000,
        raw_payload={"deal_id": "deal-duplicate-1"},
    )

    class _Result:
        status = "skipped"
        action = None
        reason = "duplicate_deal_id"
        deal_id = "deal-duplicate-1"
        account = "lx"
        operations: list[dict] = []

        def to_dict(self) -> dict:
            return {
                "status": self.status,
                "action": self.action,
                "reason": self.reason,
                "deal_id": self.deal_id,
                "account": self.account,
                "operations": self.operations,
            }

    initial_state = {
        "processed_deal_ids": {
            "deal-duplicate-1": {
                "status": "applied",
                "action": "open",
                "account": "lx",
                "reason": "applied_open",
                "receipt": {
                    "status": "sent",
                    "delivery_confirmed": True,
                    "message_id": "msg-confirmed",
                    "attempt_count": 1,
                },
            }
        },
        "failed_deal_ids": {},
        "unresolved_deal_ids": {},
    }
    writes: list[dict] = []
    events: list[dict] = []

    out = process_trade_payload(
        {"deal_id": "deal-duplicate-1"},
        repo=object(),
        state_path=tmp_path / "state.json",
        audit_path=tmp_path / "audit.jsonl",
        account_mapping={"REAL_1": "lx"},
        apply_changes=True,
        load_trade_intake_state_fn=lambda _path: initial_state,
        write_trade_intake_state_fn=lambda _path, state: writes.append(dict(state)),
        upsert_deal_state_fn=upsert_deal_state,
        append_trade_intake_audit_fn=lambda _path, event: events.append(dict(event)),
        enrich_trade_payload_fn=None,
        normalize_trade_deal_fn=lambda payload, futu_account_mapping=None: deal,
        resolve_trade_deal_fn=lambda *_args, **_kwargs: _Result(),
        on_result_fn=lambda _context: {"status": "skipped", "reason": "skipped_duplicate", "delivery_confirmed": False},
    )

    assert out["receipt"]["reason"] == "skipped_duplicate"
    receipt = writes[-1]["processed_deal_ids"]["deal-duplicate-1"]["receipt"]
    assert receipt["status"] == "sent"
    assert receipt["delivery_confirmed"] is True
    assert receipt["message_id"] == "msg-confirmed"
    assert receipt["attempt_count"] == 1
    assert any(event.get("phase") == "receipt_skipped" and event.get("deal_id") == "deal-duplicate-1" for event in events)
