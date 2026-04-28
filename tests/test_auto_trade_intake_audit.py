from __future__ import annotations

from pathlib import Path

from scripts.trade_event_normalizer import NormalizedTradeDeal
from src.application.trade_intake import build_trade_intake_audit_event


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


def test_process_payload_appends_ledger_persist_audit_on_applied(monkeypatch, tmp_path: Path) -> None:
    import scripts.auto_trade_intake as intake

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


def test_process_payload_appends_enriched_audit_when_lookup_adds_account(monkeypatch, tmp_path: Path) -> None:
    import scripts.auto_trade_intake as intake

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
