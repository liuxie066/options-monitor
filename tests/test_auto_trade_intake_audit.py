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
    )

    event = build_trade_intake_audit_event("normalized", deal=deal)

    assert event["phase"] == "normalized"
    assert event["deal_id"] == "deal-1"
    assert event["account"] == "lx"
    assert event["multiplier"] == 100
    assert event["multiplier_source"] == "cache"
    assert event["deal"]["multiplier_source"] == "cache"


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
    monkeypatch.setattr(intake, "normalize_trade_deal", lambda payload, futu_account_mapping=None: deal)
    monkeypatch.setattr(intake, "resolve_trade_deal", lambda *args, **kwargs: _Result())
    out = intake._process_payload(
        {"deal_id": "deal-1"},
        repo=object(),
        state_path=tmp_path / "state.json",
        audit_path=tmp_path / "audit.jsonl",
        account_mapping={"REAL_1": "lx"},
        apply_changes=True,
    )

    assert out["status"] == "applied"
    assert any(event.get("phase") == "ledger_persisted" for event in events)
