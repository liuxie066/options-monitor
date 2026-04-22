from __future__ import annotations

from scripts.trade_event_normalizer import NormalizedTradeDeal
from scripts.trade_intake_resolver import resolve_trade_deal


class FakeRepo:
    def __init__(self) -> None:
        self.created: list[dict] = []

    def list_records(self, *, page_size: int = 500) -> list[dict]:
        return []

    def get_record_fields(self, record_id: str) -> dict:
        raise KeyError(record_id)

    def create_record(self, fields: dict) -> dict:
        self.created.append(fields)
        return {"record": {"record_id": "rec_open_1"}}


def _deal(**overrides: object) -> NormalizedTradeDeal:
    base = {
        "broker": "富途",
        "futu_account_id": "REAL_1",
        "internal_account": "lx",
        "deal_id": "deal-open-1",
        "order_id": "order-1",
        "symbol": "0700.HK",
        "option_type": "put",
        "side": "sell",
        "position_effect": "open",
        "contracts": 2,
        "price": 3.93,
        "strike": 480.0,
        "multiplier": 100,
        "multiplier_source": "builtin.hk.common",
        "expiration_ymd": "2026-04-29",
        "currency": "HKD",
        "trade_time_ms": 1000,
        "raw_payload": {},
    }
    base.update(overrides)
    return NormalizedTradeDeal(**base)


def test_resolve_trade_open_dry_run_returns_fields_preview() -> None:
    result = resolve_trade_deal(_deal(), repo=FakeRepo(), state={}, apply_changes=False)

    assert result.status == "dry_run"
    assert result.action == "open"
    assert result.operations[0]["fields"]["account"] == "lx"
    assert "multiplier_source=builtin.hk.common" in result.operations[0]["fields"]["note"]


def test_resolve_trade_open_apply_creates_record() -> None:
    repo = FakeRepo()

    result = resolve_trade_deal(_deal(), repo=repo, state={}, apply_changes=True)

    assert result.status == "applied"
    assert result.operations[0]["record_id"] == "rec_open_1"
    assert repo.created[0]["symbol"] == "0700.HK"


def test_resolve_trade_open_rejects_duplicate_deal_id() -> None:
    result = resolve_trade_deal(
        _deal(),
        repo=FakeRepo(),
        state={"processed_deal_ids": {"deal-open-1": {"status": "applied"}}},
        apply_changes=False,
    )

    assert result.status == "skipped"
    assert result.reason == "duplicate_deal_id"


def test_resolve_trade_open_rejects_non_sell_side() -> None:
    result = resolve_trade_deal(_deal(side="buy"), repo=FakeRepo(), state={}, apply_changes=False)

    assert result.status == "unresolved"
    assert result.reason == "unsupported_open_side"
