from __future__ import annotations

import src.application.ledger.manual_trades as ledger_manual_trades
import src.application.ledger.repository as ledger_repository

from domain.domain.ledger import ContractKey, TradeEvent
from src.application.ledger.writer import persist_trade_event_object
from src.application.trades.normalizer import NormalizedTradeDeal
from src.application.trades.resolver import (
    load_close_candidate_records,
    match_close_positions,
    match_close_targets,
    resolve_trade_deal,
)


class FakeRepo:
    def __init__(self, records: list[dict]) -> None:
        self.records = records
        self.updated: list[dict] = []

    def list_records(self, *, page_size: int = 500) -> list[dict]:
        return list(self.records)

    def list_position_lots(self) -> list[dict]:
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


def _record_with_expiration(record_id: str, opened_at: int, contracts_open: int, expiration: int) -> dict:
    row = _record(record_id, opened_at, contracts_open)
    row["fields"]["expiration"] = expiration
    return row


def _long_record(record_id: str, opened_at: int, contracts_open: int) -> dict:
    row = _record(record_id, opened_at, contracts_open)
    row["fields"]["side"] = "long"
    return row


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


def test_match_close_targets_exposes_strict_resolution_contract() -> None:
    repo = FakeRepo([_record("rec1", 100, 1), _record("rec2", 200, 2)])

    resolution = match_close_targets(repo, _deal())

    assert resolution.source == "broker_trade_close"
    assert resolution.strategy == "strict_exact_fifo"
    assert resolution.selector["expiration_ymd"] == "2026-04-29"
    assert resolution.record_ids == ("rec1", "rec2")
    assert resolution.to_dict()["contracts_to_close"] == 3


def test_broker_close_target_resolution_does_not_cross_same_strike_different_expiry() -> None:
    may_exp = 1777420800000
    jun_exp = 1782691200000
    repo = FakeRepo(
        [
            _record_with_expiration("may_put", 100, 1, may_exp),
            _record_with_expiration("jun_put", 200, 3, jun_exp),
        ]
    )

    resolution = match_close_targets(repo, _deal(contracts=1, expiration_ymd="2026-04-29"))

    assert resolution.record_ids == ("may_put",)
    assert resolution.to_dict()["targets"][0]["candidate"]["expiration_ymd"] == "2026-04-29"


def test_match_close_positions_ignores_market_only_persisted_rows() -> None:
    market_only = _record("rec1", 100, 1)
    market_only["fields"].pop("broker", None)
    market_only["fields"]["market"] = "富途"
    repo = FakeRepo([market_only, _record("rec2", 200, 3)])

    matches = match_close_positions(repo, _deal())

    assert [(m.record_id, m.contracts_to_close) for m in matches] == [("rec2", 3)]


def test_match_close_positions_canonicalizes_candidate_and_deal_symbols() -> None:
    raw_alias = _record("rec-pop", 100, 1)
    raw_alias["fields"]["symbol"] = "POP"
    repo = FakeRepo([raw_alias])

    matches = match_close_positions(repo, _deal(symbol="HK.09992", contracts=1))

    assert [(m.record_id, m.contracts_to_close) for m in matches] == [("rec-pop", 1)]


def test_resolve_trade_close_dry_run_builds_patches() -> None:
    repo = FakeRepo([_record("rec1", 100, 1), _record("rec2", 200, 2)])

    result = resolve_trade_deal(_deal(), repo=repo, state={}, apply_changes=False)

    assert result.status == "dry_run"
    assert result.action == "close"
    assert result.diagnostics["close_target_resolution"]["record_ids"] == ["rec1", "rec2"]
    assert len(result.operations) == 2
    assert result.operations[0]["close_target_resolution"]["record_ids"] == ["rec1", "rec2"]
    assert result.operations[0]["action"] == "buy_close"
    assert result.operations[0]["patch"]["contracts_open"] == 0
    assert result.operations[0]["patch"]["close_type"] == "buy_to_close"


def test_resolve_trade_long_close_dry_run_builds_patches() -> None:
    repo = FakeRepo([_long_record("rec1", 100, 1), _long_record("rec2", 200, 2)])

    result = resolve_trade_deal(_deal(side="sell"), repo=repo, state={}, apply_changes=False)

    assert result.status == "dry_run"
    assert result.action == "close"
    assert len(result.operations) == 2
    assert result.operations[0]["action"] == "sell_close"
    assert result.operations[0]["patch"]["contracts_open"] == 0
    assert result.operations[0]["patch"]["close_type"] == "sell_to_close"


def test_resolve_trade_close_apply_updates_records() -> None:
    repo = FakeRepo([_record("rec1", 100, 1), _record("rec2", 200, 2)])
    result = resolve_trade_deal(
        _deal(),
        repo=repo,
        state={},
        apply_changes=True,
        persist_trade_event_fn=lambda repo, deal: {"event_id": deal.deal_id, "created": True},
    )

    assert result.status == "applied"
    assert [row["record_id"] for row in result.operations] == ["rec1", "rec2"]
    assert result.diagnostics["close_target_resolution"]["strategy"] == "strict_exact_fifo"
    assert repo.updated == []


def test_resolve_trade_long_close_apply_updates_records() -> None:
    repo = FakeRepo([_long_record("rec1", 100, 1), _long_record("rec2", 200, 2)])
    result = resolve_trade_deal(
        _deal(side="sell"),
        repo=repo,
        state={},
        apply_changes=True,
        persist_trade_event_fn=lambda repo, deal: {"event_id": deal.deal_id, "created": True},
    )

    assert result.status == "applied"
    assert [row["record_id"] for row in result.operations] == ["rec1", "rec2"]
    assert [row["action"] for row in result.operations] == ["sell_close", "sell_close"]
    assert repo.updated == []


def test_resolve_trade_close_apply_persists_per_lot_target_events(tmp_path) -> None:
    from domain.domain.option_position_lots import OpenPositionCommand

    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    for opened_at, contracts in ((100, 1), (200, 2)):
        ledger_manual_trades.persist_manual_open_event(
            repo,
            OpenPositionCommand(
                broker="富途",
                account="lx",
                symbol="0700.HK",
                option_type="put",
                side="short",
                contracts=contracts,
                currency="HKD",
                strike=480.0,
                multiplier=100,
                expiration_ymd="2026-04-29",
                premium_per_share=3.93,
                opened_at_ms=opened_at,
            ),
        )
    open_lot_ids = [row["record_id"] for row in repo.list_position_lots()]

    result = resolve_trade_deal(
        _deal(contracts=3, trade_time_ms=5000),
        repo=repo,
        state={},
        apply_changes=True,
    )

    assert result.status == "applied"
    assert [row["record_id"] for row in result.operations] == open_lot_ids
    assert [row["contracts_to_close"] for row in result.operations] == [1, 2]
    assert {row["ledger_preflight"]["event_type"] for row in result.operations} == {"close"}
    close_events = [item for item in repo.list_trade_events() if item["position_effect"] == "close"]
    assert {item["raw_payload"]["record_id"] for item in close_events} == set(open_lot_ids)
    assert {tuple(item["raw_payload"]["close_target_resolution"]["record_ids"]) for item in close_events} == {
        tuple(open_lot_ids)
    }
    assert {item["raw_payload"]["source_deal_id"] for item in close_events} == {"deal-close-1"}
    assert all(str(item["event_id"]).startswith("deal-close-1:close:") for item in close_events)
    lots = repo.list_position_lots()
    assert all(item["fields"]["status"] == "close" for item in lots)
    assert all(item["fields"]["contracts_open"] == 0 for item in lots)


def test_resolve_trade_close_rejects_missing_trade_time_before_write() -> None:
    repo = FakeRepo([_record("rec1", 100, 1)])

    result = resolve_trade_deal(_deal(contracts=1, trade_time_ms=None), repo=repo, state={}, apply_changes=True)

    assert result.status == "unresolved"
    assert result.reason == "missing_required_fields:trade_time_ms"


def test_resolve_trade_close_reports_failed_when_post_write_projection_does_not_close_lot(tmp_path) -> None:
    from domain.domain.option_position_lots import OpenPositionCommand

    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    ledger_manual_trades.persist_manual_open_event(
        repo,
        OpenPositionCommand(
            broker="富途",
            account="lx",
            symbol="0700.HK",
            option_type="put",
            side="short",
            contracts=2,
            currency="HKD",
            strike=480.0,
            multiplier=100,
            expiration_ymd="2026-04-29",
            premium_per_share=3.93,
            opened_at_ms=1000,
        ),
    )
    lot_id = repo.list_position_lots()[0]["record_id"]

    def _persist_bad_zero_time_close(repo, deal):  # type: ignore[no-untyped-def]
        record_id = str((deal.raw_payload or {}).get("record_id") or "")
        event = TradeEvent(
            event_id=f"{deal.deal_id}:close:{record_id}",
            event_type="close",
            event_time_ms=0,
            contract_key=ContractKey.from_values(
                broker="富途",
                account=deal.internal_account,
                underlying_symbol=deal.symbol,
                option_type=deal.option_type,
                position_side="short",
                strike=deal.strike,
                expiration_ymd=deal.expiration_ymd,
            ),
            contracts=int(deal.contracts or 0),
            price=float(deal.price or 0),
            currency=deal.currency,
            source="opend_push",
            multiplier=float(deal.multiplier or 100),
            target_lot_id=record_id,
            raw_payload={"record_id": record_id, "target_lot_id": record_id},
        )
        return persist_trade_event_object(repo, event)

    result = resolve_trade_deal(
        _deal(contracts=2, trade_time_ms=5000),
        repo=repo,
        state={},
        apply_changes=True,
        persist_trade_event_fn=_persist_bad_zero_time_close,
    )

    assert result.status == "failed"
    assert result.reason == "projection_verification_failed"
    verification = result.diagnostics["post_write_projection_verification"]
    assert verification["errors"][0]["code"] == "projection_unmatched_close"
    assert repo.get_record_fields(lot_id)["contracts_open"] == 2


def test_resolve_trade_close_rejects_insufficient_contracts() -> None:
    repo = FakeRepo([_record("rec1", 100, 1)])

    result = resolve_trade_deal(_deal(), repo=repo, state={}, apply_changes=False)

    assert result.status == "unresolved"
    assert "close_match_insufficient_contracts" in result.reason


def test_resolve_trade_close_rejects_unknown_side() -> None:
    repo = FakeRepo([_record("rec1", 100, 3)])

    result = resolve_trade_deal(_deal(side="hold"), repo=repo, state={}, apply_changes=False)

    assert result.status == "unresolved"
    assert result.reason == "unsupported_close_side"


def test_match_close_positions_matches_long_lots_for_sell_close() -> None:
    repo = FakeRepo([_long_record("rec1", 100, 1), _long_record("rec2", 200, 2)])

    matches = match_close_positions(repo, _deal(side="sell"))

    assert [(m.record_id, m.contracts_to_close) for m in matches] == [("rec1", 1), ("rec2", 2)]


def test_load_close_candidate_records_prefers_position_lots_projection() -> None:
    class _PrimaryRepo:
        def list_position_lots(self) -> list[dict]:
            return [_record("lot1", 100, 2)]

    class _Repo(FakeRepo):
        primary_repo = _PrimaryRepo()

    repo = _Repo([_record("rec1", 100, 1)])

    rows = load_close_candidate_records(repo)

    assert [row["record_id"] for row in rows] == ["lot1"]
