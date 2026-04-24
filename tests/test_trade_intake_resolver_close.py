from __future__ import annotations

from scripts.trade_event_normalizer import NormalizedTradeDeal
from scripts.trade_intake_resolver import load_close_candidate_records, match_close_positions, resolve_trade_deal


class FakeRepo:
    def __init__(self, records: list[dict]) -> None:
        self.records = records
        self.updated: list[dict] = []

    def list_records(self, *, page_size: int = 500) -> list[dict]:
        return list(self.records)

    def get_record_fields(self, record_id: str) -> dict:
        for item in self.records:
            if item["record_id"] == record_id:
                return dict(item["fields"])
        raise KeyError(record_id)

    def update_record(self, record_id: str, fields: dict) -> dict:
        self.updated.append({"record_id": record_id, "fields": fields})
        return {"record": {"record_id": record_id}}


def _record(record_id: str, opened_at: int, contracts_open: int) -> dict:
    return {
        "record_id": record_id,
        "fields": {
            "record_id": record_id,
            "broker": "富途",
            "account": "lx",
            "symbol": "0700.HK",
            "option_type": "put",
            "side": "short",
            "status": "open",
            "contracts": contracts_open,
            "contracts_open": contracts_open,
            "contracts_closed": 0,
            "strike": 480.0,
            "expiration": 1777420800000,
            "opened_at": opened_at,
        },
    }


def _deal(**overrides: object) -> NormalizedTradeDeal:
    base = {
        "broker": "富途",
        "futu_account_id": "REAL_1",
        "internal_account": "lx",
        "deal_id": "deal-close-1",
        "order_id": "order-1",
        "symbol": "0700.HK",
        "option_type": "put",
        "side": "buy",
        "position_effect": "close",
        "contracts": 3,
        "price": 1.2,
        "strike": 480.0,
        "multiplier": 100,
        "multiplier_source": "cache",
        "expiration_ymd": "2026-04-29",
        "currency": "HKD",
        "trade_time_ms": 1000,
        "raw_payload": {},
    }
    base.update(overrides)
    return NormalizedTradeDeal(**base)


def test_match_close_positions_uses_fifo() -> None:
    repo = FakeRepo([_record("rec1", 100, 1), _record("rec2", 200, 2)])

    matches = match_close_positions(repo, _deal())

    assert [(m.record_id, m.contracts_to_close) for m in matches] == [("rec1", 1), ("rec2", 2)]


def test_resolve_trade_close_dry_run_builds_patches() -> None:
    repo = FakeRepo([_record("rec1", 100, 1), _record("rec2", 200, 2)])

    result = resolve_trade_deal(_deal(), repo=repo, state={}, apply_changes=False)

    assert result.status == "dry_run"
    assert result.action == "close"
    assert len(result.operations) == 2
    assert result.operations[0]["patch"]["contracts_open"] == 0


def test_resolve_trade_close_apply_updates_records() -> None:
    repo = FakeRepo([_record("rec1", 100, 1), _record("rec2", 200, 2)])
    import scripts.trade_intake_resolver as tir

    old_persist = tir.persist_trade_event
    try:
        tir.persist_trade_event = lambda repo, deal: {"event_id": deal.deal_id, "created": True}  # type: ignore[assignment]
        result = resolve_trade_deal(_deal(), repo=repo, state={}, apply_changes=True)
    finally:
        tir.persist_trade_event = old_persist  # type: ignore[assignment]

    assert result.status == "applied"
    assert [row["record_id"] for row in result.operations] == ["rec1", "rec2"]
    assert repo.updated == []


def test_resolve_trade_close_rejects_insufficient_contracts() -> None:
    repo = FakeRepo([_record("rec1", 100, 1)])

    result = resolve_trade_deal(_deal(), repo=repo, state={}, apply_changes=False)

    assert result.status == "unresolved"
    assert "close_match_insufficient_contracts" in result.reason


def test_resolve_trade_close_rejects_non_buy_side() -> None:
    repo = FakeRepo([_record("rec1", 100, 3)])

    result = resolve_trade_deal(_deal(side="sell"), repo=repo, state={}, apply_changes=False)

    assert result.status == "unresolved"
    assert result.reason == "unsupported_close_side"


def test_load_close_candidate_records_prefers_position_lots_projection() -> None:
    class _PrimaryRepo:
        def list_position_lots(self) -> list[dict]:
            return [_record("lot1", 100, 2)]

    class _Repo(FakeRepo):
        primary_repo = _PrimaryRepo()

    repo = _Repo([_record("rec1", 100, 1)])

    rows = load_close_candidate_records(repo)

    assert [row["record_id"] for row in rows] == ["lot1"]
