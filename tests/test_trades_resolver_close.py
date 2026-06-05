from __future__ import annotations

import src.application.ledger.manual_trades as ledger_manual_trades
import src.application.ledger.repository as ledger_repository

from domain.domain.ledger import ContractKey, TradeEvent
from domain.domain.ledger.position_fields import effective_expiration_ymd
from src.application.ledger.writer import persist_trade_event_object
from src.application.trades.normalizer import NormalizedTradeDeal
from src.application.trades.resolver import (
    load_close_candidate_records,
    match_close_positions,
    match_close_targets,
    resolve_trade_deal,
)
from src.application.trades.lifecycle import resolve_lifecycle_expired_unassigned


class FakeRepo:
    def __init__(self, records: list[dict]) -> None:
        self.records = records
        self.updated: list[dict] = []

    def list_records(self, *, page_size: int = 500) -> list[dict]:
        return list(self.records)

    def list_position_lots(self) -> list[dict]:
        return list(self.records)

    def list_trade_events(self) -> list[dict]:
        return [_open_event_from_record(item) for item in self.records]

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
            "currency": "HKD",
            "multiplier": 100,
            "expiration": 1777420800000,
            "opened_at": opened_at,
        },
    }


def _open_event_from_record(record: dict) -> dict:
    fields = dict(record["fields"])
    return TradeEvent(
        event_id=f"seed-{record['record_id']}",
        event_type="open",
        event_time_ms=int(fields.get("opened_at") or 1),
        contract_key=ContractKey.from_values(
            broker=fields.get("broker"),
            account=fields.get("account"),
            underlying_symbol=fields.get("symbol"),
            option_type=fields.get("option_type"),
            position_side=fields.get("side"),
            strike=fields.get("strike"),
            expiration_ymd=effective_expiration_ymd(fields),
        ),
        contracts=int(fields.get("contracts") or fields.get("contracts_open") or 0),
        price=1.0,
        currency=str(fields.get("currency") or "HKD"),
        source="test_seed_open_lot",
        multiplier=float(fields.get("multiplier") or 100),
        lot_id=str(record["record_id"]),
        raw_payload={"source_type": "test_seed"},
    ).to_dict()


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


def test_resolve_trade_close_dry_run_routes_zero_price_expiry_leg_to_lifecycle_pending() -> None:
    repo = FakeRepo([_record("rec1", 100, 3)])

    result = resolve_trade_deal(
        _deal(
            contracts=3,
            price=0.0,
            expiration_ymd="2026-04-29",
            trade_time_ms=1777420800000,
        ),
        repo=repo,
        state={},
        apply_changes=False,
    )

    assert result.status == "dry_run"
    assert result.action == "lifecycle"
    assert result.reason == "waiting_settlement_evidence"
    assert result.operations[0]["action"] == "lifecycle_pending"
    assert result.diagnostics["decision"]["decision_type"] == "needs_review"


def test_resolve_trade_close_skips_failed_deal_by_default() -> None:
    repo = FakeRepo([_record("rec1", 100, 3)])

    result = resolve_trade_deal(
        _deal(),
        repo=repo,
        state={"failed_deal_ids": {"deal-close-1": {"status": "failed", "reason": "exception:LedgerPreflightError"}}},
        apply_changes=False,
    )

    assert result.status == "skipped"
    assert result.reason == "duplicate_deal_id"


def test_resolve_trade_close_retries_failed_deal_when_explicitly_allowed() -> None:
    repo = FakeRepo([_record("rec1", 100, 3)])

    result = resolve_trade_deal(
        _deal(),
        repo=repo,
        state={"failed_deal_ids": {"deal-close-1": {"status": "failed", "reason": "exception:LedgerPreflightError"}}},
        apply_changes=False,
        retry_failed_deal=True,
    )

    assert result.status == "dry_run"
    assert result.action == "close"
    assert result.reason == "preview_close"


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


def test_resolve_trade_close_apply_keeps_zero_price_option_leg_pending_without_stock_settlement(tmp_path) -> None:
    from domain.domain.option_position_lots import OpenPositionCommand

    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    ledger_manual_trades.persist_manual_open_event(
        repo,
        OpenPositionCommand(
            broker="富途",
            account="lx",
            symbol="TIGR",
            option_type="put",
            side="short",
            contracts=10,
            currency="USD",
            strike=6.0,
            multiplier=100,
            expiration_ymd="2026-05-22",
            premium_per_share=0.2,
            opened_at_ms=1779129617118,
        ),
    )
    lot_id = repo.list_position_lots()[0]["record_id"]

    result = resolve_trade_deal(
        _deal(
            deal_id="5646137975909129735",
            order_id="FH1C8FA7239D5FA000",
            symbol="TIGR",
            contracts=10,
            price=0.0,
            strike=6.0,
            expiration_ymd="2026-05-22",
            currency="USD",
            trade_time_ms=1779468493916,
            raw_payload={"deal_id": "5646137975909129735", "code": "US.TIGR260522P6000"},
        ),
        repo=repo,
        state={},
        apply_changes=True,
    )

    assert result.status == "unresolved"
    assert result.action == "lifecycle"
    assert result.reason == "waiting_settlement_evidence"
    close_events = [item for item in repo.list_trade_events() if item["position_effect"] == "close"]
    assert close_events == []
    cases = repo.list_trade_lifecycle_cases()
    assert cases[0]["status"] == "waiting_settlement_evidence"
    assert repo.get_record_fields(lot_id)["contracts_open"] == 10


def test_confirm_lifecycle_expired_unassigned_records_expire_close(tmp_path) -> None:
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
            strike=440.0,
            multiplier=100,
            expiration_ymd="2026-06-05",
            premium_per_share=0.86,
            opened_at_ms=1780354364000,
        ),
    )
    lot_id = repo.list_position_lots()[0]["record_id"]
    option_result = resolve_trade_deal(
        _deal(
            deal_id="775828694842258876",
            symbol="0700.HK",
            contracts=2,
            price=0.0,
            strike=440.0,
            expiration_ymd="2026-06-05",
            currency="HKD",
            trade_time_ms=1780657845000,
            raw_payload={"deal_id": "775828694842258876", "code": "HK.TCH260605P440000"},
        ),
        repo=repo,
        state={},
        apply_changes=True,
    )
    assert option_result.reason == "waiting_settlement_evidence"

    result = resolve_lifecycle_expired_unassigned(
        repo,
        deal_id="775828694842258876",
        apply_changes=True,
    )

    assert result.status == "applied"
    assert result.action == "expire_close"
    assert result.reason == "expire_close_recorded"
    assert result.operations[0]["ledger_preflight"]["event_type"] == "expire_close"
    close_events = [item for item in repo.list_trade_events() if item["event_type"] == "expire_close"]
    assert len(close_events) == 1
    assert close_events[0]["target_lot_id"] == lot_id
    assert close_events[0]["raw_payload"]["close_type"] == "expire_auto_close"
    assert close_events[0]["raw_payload"]["close_reason"] == "expired_unassigned"
    assert close_events[0]["raw_payload"]["evidence_ids"]
    cases = repo.list_trade_lifecycle_cases()
    assert cases[0]["status"] == "ledger_written"
    assert cases[0]["decision_type"] == "expire_close"
    assert cases[0]["target_lot_ids"] == [lot_id]
    fields = repo.get_record_fields(lot_id)
    assert fields["status"] == "close"
    assert fields["contracts_open"] == 0
    assert fields["close_type"] == "expire_auto_close"


def test_resolve_trade_close_retry_failed_routes_early_zero_price_assignment_to_lifecycle_pending(tmp_path) -> None:
    from domain.domain.option_position_lots import OpenPositionCommand

    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    ledger_manual_trades.persist_manual_open_event(
        repo,
        OpenPositionCommand(
            broker="富途",
            account="lx",
            symbol="FUTU",
            option_type="put",
            side="short",
            contracts=1,
            currency="USD",
            strike=120.0,
            multiplier=100,
            expiration_ymd="2026-06-05",
            premium_per_share=3.6,
            opened_at_ms=1779129615442,
        ),
    )
    lot_id = repo.list_position_lots()[0]["record_id"]

    result = resolve_trade_deal(
        _deal(
            deal_id="3254612655429789712",
            order_id="FH1C9F208E1EAE8000",
            symbol="FUTU",
            contracts=1,
            price=0.0,
            strike=120.0,
            expiration_ymd="2026-06-05",
            currency="USD",
            trade_time_ms=1780506955360,
            raw_payload={
                "deal_id": "3254612655429789712",
                "order_id": "FH1C9F208E1EAE8000",
                "code": "US.FUTU260605P120000",
                "trd_side": "BUY_BACK",
                "status": "OK",
            },
        ),
        repo=repo,
        state={
            "failed_deal_ids": {
                "3254612655429789712": {
                    "status": "failed",
                    "reason": "exception:LedgerPreflightError",
                }
            }
        },
        apply_changes=True,
        retry_failed_deal=True,
    )

    assert result.status == "unresolved"
    assert result.action == "lifecycle"
    assert result.reason == "waiting_settlement_evidence"
    close_events = [item for item in repo.list_trade_events() if item["position_effect"] == "close"]
    assert close_events == []
    cases = repo.list_trade_lifecycle_cases()
    assert cases[0]["symbol"] == "FUTU"
    assert cases[0]["status"] == "waiting_settlement_evidence"
    assert cases[0]["decision_type"] == "needs_review"
    evidence = repo.list_trade_lifecycle_evidence(case_id=cases[0]["case_id"])
    assert evidence[0]["source_event_id"] == "3254612655429789712"
    assert evidence[0]["evidence_type"] == "option_zero_price_close"
    assert repo.get_record_fields(lot_id)["contracts_open"] == 1


def test_resolve_trade_lifecycle_option_first_stock_settlement_records_assignment(tmp_path) -> None:
    from domain.domain.option_position_lots import OpenPositionCommand

    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    ledger_manual_trades.persist_manual_open_event(
        repo,
        OpenPositionCommand(
            broker="富途",
            account="lx",
            symbol="TIGR",
            option_type="put",
            side="short",
            contracts=10,
            currency="USD",
            strike=6.0,
            multiplier=100,
            expiration_ymd="2026-05-22",
            premium_per_share=0.2,
            opened_at_ms=1779129617118,
        ),
    )
    lot_id = repo.list_position_lots()[0]["record_id"]

    option_result = resolve_trade_deal(
        _deal(
            deal_id="option-leg-1",
            symbol="TIGR",
            contracts=10,
            price=0.0,
            strike=6.0,
            expiration_ymd="2026-05-22",
            currency="USD",
            trade_time_ms=1779468493916,
            raw_payload={"deal_id": "option-leg-1", "code": "US.TIGR260522P6000"},
        ),
        repo=repo,
        state={},
        apply_changes=True,
    )

    assert option_result.status == "unresolved"

    stock_result = resolve_trade_deal(
        _deal(
            deal_id="stock-leg-1",
            order_id="stock-order-1",
            symbol="TIGR",
            option_type=None,
            side="buy",
            position_effect=None,
            contracts=1000,
            price=6.0,
            strike=None,
            multiplier=None,
            expiration_ymd=None,
            currency="USD",
            trade_time_ms=1779468500000,
            raw_payload={"deal_id": "stock-leg-1", "code": "US.TIGR"},
        ),
        repo=repo,
        state={},
        apply_changes=True,
    )

    assert stock_result.status == "applied"
    assert stock_result.action == "assignment"
    assert stock_result.operations[0]["ledger_preflight"]["event_type"] == "assignment"
    assignment_events = [item for item in repo.list_trade_events() if item.get("event_type") == "assignment"]
    assert len(assignment_events) == 1
    assert assignment_events[0]["raw_payload"]["record_id"] == lot_id
    assert assignment_events[0]["raw_payload"]["stock_settlement"]["shares"] == 1000
    assert repo.get_record_fields(lot_id)["contracts_open"] == 0
    assert repo.get_record_fields(lot_id)["close_type"] == "assignment"


def test_resolve_trade_lifecycle_duplicate_option_leg_after_assignment_is_idempotent(tmp_path) -> None:
    from domain.domain.option_position_lots import OpenPositionCommand

    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    ledger_manual_trades.persist_manual_open_event(
        repo,
        OpenPositionCommand(
            broker="富途",
            account="lx",
            symbol="TIGR",
            option_type="put",
            side="short",
            contracts=10,
            currency="USD",
            strike=6.0,
            multiplier=100,
            expiration_ymd="2026-05-22",
            premium_per_share=0.2,
            opened_at_ms=1779129617118,
        ),
    )
    option_deal = _deal(
        deal_id="option-leg-dup",
        symbol="TIGR",
        contracts=10,
        price=0.0,
        strike=6.0,
        expiration_ymd="2026-05-22",
        currency="USD",
        trade_time_ms=1779468493916,
        raw_payload={"deal_id": "option-leg-dup", "code": "US.TIGR260522P6000"},
    )

    assert resolve_trade_deal(option_deal, repo=repo, state={}, apply_changes=True).status == "unresolved"
    assert resolve_trade_deal(
        _deal(
            deal_id="stock-leg-dup",
            symbol="TIGR",
            option_type=None,
            side="buy",
            position_effect=None,
            contracts=1000,
            price=6.0,
            strike=None,
            multiplier=None,
            expiration_ymd=None,
            currency="USD",
            trade_time_ms=1779468500000,
            raw_payload={"deal_id": "stock-leg-dup", "code": "US.TIGR"},
        ),
        repo=repo,
        state={},
        apply_changes=True,
    ).status == "applied"

    duplicate = resolve_trade_deal(option_deal, repo=repo, state={}, apply_changes=True)

    assert duplicate.status == "skipped"
    assert duplicate.reason == "lifecycle_already_written"
    assert duplicate.action == "assignment"
    assert len([item for item in repo.list_trade_events() if item.get("event_type") == "assignment"]) == 1
    cases = repo.list_trade_lifecycle_cases()
    assert cases[0]["status"] == "ledger_written"


def test_resolve_trade_lifecycle_long_call_exercise_records_exercise(tmp_path) -> None:
    from domain.domain.option_position_lots import OpenPositionCommand

    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    ledger_manual_trades.persist_manual_open_event(
        repo,
        OpenPositionCommand(
            broker="富途",
            account="lx",
            symbol="AAPL",
            option_type="call",
            side="long",
            contracts=2,
            currency="USD",
            strike=200.0,
            multiplier=100,
            expiration_ymd="2026-05-22",
            premium_per_share=1.5,
            opened_at_ms=1779129617118,
        ),
    )
    lot_id = repo.list_position_lots()[0]["record_id"]

    option_result = resolve_trade_deal(
        _deal(
            deal_id="long-call-option-leg",
            symbol="AAPL",
            option_type="call",
            side="sell",
            position_effect="close",
            contracts=2,
            price=0.0,
            strike=200.0,
            expiration_ymd="2026-05-22",
            currency="USD",
            trade_time_ms=1779468493916,
            raw_payload={"deal_id": "long-call-option-leg", "code": "US.AAPL260522C200000"},
        ),
        repo=repo,
        state={},
        apply_changes=True,
    )
    assert option_result.status == "unresolved"

    stock_result = resolve_trade_deal(
        _deal(
            deal_id="long-call-stock-leg",
            symbol="AAPL",
            option_type=None,
            side="buy",
            position_effect=None,
            contracts=200,
            price=200.0,
            strike=None,
            multiplier=None,
            expiration_ymd=None,
            currency="USD",
            trade_time_ms=1779468500000,
            raw_payload={"deal_id": "long-call-stock-leg", "code": "US.AAPL"},
        ),
        repo=repo,
        state={},
        apply_changes=True,
    )

    assert stock_result.status == "applied"
    assert stock_result.action == "exercise"
    assert stock_result.operations[0]["ledger_preflight"]["event_type"] == "exercise"
    exercise_events = [item for item in repo.list_trade_events() if item.get("event_type") == "exercise"]
    assert len(exercise_events) == 1
    assert exercise_events[0]["raw_payload"]["record_id"] == lot_id
    assert exercise_events[0]["raw_payload"]["stock_settlement"]["shares"] == 200
    assert repo.get_record_fields(lot_id)["contracts_open"] == 0
    assert repo.get_record_fields(lot_id)["close_type"] == "exercise"


def test_resolve_trade_lifecycle_stock_first_then_long_put_exercise_records_exercise(tmp_path) -> None:
    from domain.domain.option_position_lots import OpenPositionCommand

    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    ledger_manual_trades.persist_manual_open_event(
        repo,
        OpenPositionCommand(
            broker="富途",
            account="lx",
            symbol="AAPL",
            option_type="put",
            side="long",
            contracts=1,
            currency="USD",
            strike=180.0,
            multiplier=100,
            expiration_ymd="2026-05-22",
            premium_per_share=1.5,
            opened_at_ms=1779129617118,
        ),
    )

    stock_result = resolve_trade_deal(
        _deal(
            deal_id="long-put-stock-leg-first",
            symbol="AAPL",
            option_type=None,
            side="sell",
            position_effect=None,
            contracts=100,
            price=180.0,
            strike=None,
            multiplier=None,
            expiration_ymd=None,
            currency="USD",
            trade_time_ms=1779468400000,
            raw_payload={"deal_id": "long-put-stock-leg-first", "code": "US.AAPL"},
        ),
        repo=repo,
        state={},
        apply_changes=True,
    )
    assert stock_result.status == "unresolved"
    assert stock_result.reason == "stock_settlement_waiting_option_leg"

    option_result = resolve_trade_deal(
        _deal(
            deal_id="long-put-option-leg-after-stock",
            symbol="AAPL",
            option_type="put",
            side="sell",
            position_effect="close",
            contracts=1,
            price=0.0,
            strike=180.0,
            expiration_ymd="2026-05-22",
            currency="USD",
            trade_time_ms=1779468493916,
            raw_payload={"deal_id": "long-put-option-leg-after-stock", "code": "US.AAPL260522P180000"},
        ),
        repo=repo,
        state={},
        apply_changes=True,
    )

    assert option_result.status == "applied"
    assert option_result.action == "exercise"
    assert len([item for item in repo.list_trade_events() if item.get("event_type") == "exercise"]) == 1


def test_resolve_trade_lifecycle_stock_first_then_option_leg_records_assignment(tmp_path) -> None:
    from domain.domain.option_position_lots import OpenPositionCommand

    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    ledger_manual_trades.persist_manual_open_event(
        repo,
        OpenPositionCommand(
            broker="富途",
            account="lx",
            symbol="TIGR",
            option_type="put",
            side="short",
            contracts=10,
            currency="USD",
            strike=6.0,
            multiplier=100,
            expiration_ymd="2026-05-22",
            premium_per_share=0.2,
            opened_at_ms=1779129617118,
        ),
    )

    stock_result = resolve_trade_deal(
        _deal(
            deal_id="stock-leg-first",
            symbol="TIGR",
            option_type=None,
            side="buy",
            position_effect=None,
            contracts=1000,
            price=6.0,
            strike=None,
            multiplier=None,
            expiration_ymd=None,
            currency="USD",
            trade_time_ms=1779468400000,
            raw_payload={"deal_id": "stock-leg-first", "code": "US.TIGR"},
        ),
        repo=repo,
        state={},
        apply_changes=True,
    )

    assert stock_result.status == "unresolved"
    assert stock_result.reason == "stock_settlement_waiting_option_leg"

    option_result = resolve_trade_deal(
        _deal(
            deal_id="option-leg-after-stock",
            symbol="TIGR",
            contracts=10,
            price=0.0,
            strike=6.0,
            expiration_ymd="2026-05-22",
            currency="USD",
            trade_time_ms=1779468493916,
            raw_payload={"deal_id": "option-leg-after-stock", "code": "US.TIGR260522P6000"},
        ),
        repo=repo,
        state={},
        apply_changes=True,
    )

    assert option_result.status == "applied"
    assert option_result.action == "assignment"
    assignment_events = [item for item in repo.list_trade_events() if item.get("event_type") == "assignment"]
    assert len(assignment_events) == 1


def test_resolve_trade_lifecycle_late_assignment_after_expire_close_marks_conflict(tmp_path) -> None:
    from domain.domain.option_position_lots import OpenPositionCommand

    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    ledger_manual_trades.persist_manual_open_event(
        repo,
        OpenPositionCommand(
            broker="富途",
            account="lx",
            symbol="TIGR",
            option_type="put",
            side="short",
            contracts=10,
            currency="USD",
            strike=6.0,
            multiplier=100,
            expiration_ymd="2026-05-22",
            premium_per_share=0.2,
            opened_at_ms=1779129617118,
        ),
    )
    lot_id = repo.list_position_lots()[0]["record_id"]
    persist_trade_event_object(
        repo,
        TradeEvent(
            event_id="expire-close-before-assignment",
            event_type="expire_close",
            event_time_ms=1779468400000,
            contract_key=ContractKey.from_values(
                broker="富途",
                account="lx",
                underlying_symbol="TIGR",
                option_type="put",
                position_side="short",
                strike=6.0,
                expiration_ymd="2026-05-22",
            ),
            contracts=10,
            price=0.0,
            currency="USD",
            source="test_expire_close",
            multiplier=100,
            target_lot_id=lot_id,
            raw_payload={"record_id": lot_id, "target_lot_id": lot_id, "close_type": "expire_auto_close"},
        ),
    )
    assert repo.get_record_fields(lot_id)["status"] == "close"

    stock_result = resolve_trade_deal(
        _deal(
            deal_id="late-stock-leg",
            symbol="TIGR",
            option_type=None,
            side="buy",
            position_effect=None,
            contracts=1000,
            price=6.0,
            strike=None,
            multiplier=None,
            expiration_ymd=None,
            currency="USD",
            trade_time_ms=1779468500000,
            raw_payload={"deal_id": "late-stock-leg", "code": "US.TIGR"},
        ),
        repo=repo,
        state={},
        apply_changes=True,
    )
    assert stock_result.status == "unresolved"

    option_result = resolve_trade_deal(
        _deal(
            deal_id="late-option-leg",
            symbol="TIGR",
            contracts=10,
            price=0.0,
            strike=6.0,
            expiration_ymd="2026-05-22",
            currency="USD",
            trade_time_ms=1779468600000,
            raw_payload={"deal_id": "late-option-leg", "code": "US.TIGR260522P6000"},
        ),
        repo=repo,
        state={},
        apply_changes=True,
    )

    assert option_result.status == "unresolved"
    assert option_result.reason == "assignment_after_expire_close_conflict"
    cases = repo.list_trade_lifecycle_cases()
    assert cases[0]["status"] == "conflict"
    assert option_result.diagnostics["conflict_event"]["event_id"] == "expire-close-before-assignment"
    assert [item for item in repo.list_trade_events() if item.get("event_type") == "assignment"] == []


def test_resolve_trade_close_retry_failed_keeps_zero_price_option_leg_pending(tmp_path) -> None:
    from domain.domain.option_position_lots import OpenPositionCommand

    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    ledger_manual_trades.persist_manual_open_event(
        repo,
        OpenPositionCommand(
            broker="富途",
            account="lx",
            symbol="TIGR",
            option_type="put",
            side="short",
            contracts=10,
            currency="USD",
            strike=6.0,
            multiplier=100,
            expiration_ymd="2026-05-22",
            premium_per_share=0.2,
            opened_at_ms=1779129617118,
        ),
    )
    lot_id = repo.list_position_lots()[0]["record_id"]

    result = resolve_trade_deal(
        _deal(
            deal_id="5646137975909129735",
            order_id="FH1C8FA7239D5FA000",
            symbol="TIGR",
            contracts=10,
            price=0.0,
            strike=6.0,
            expiration_ymd="2026-05-22",
            currency="USD",
            trade_time_ms=1779468493916,
            raw_payload={"deal_id": "5646137975909129735", "code": "US.TIGR260522P6000"},
        ),
        repo=repo,
        state={"failed_deal_ids": {"5646137975909129735": {"status": "failed", "reason": "exception:LedgerPreflightError"}}},
        apply_changes=True,
        retry_failed_deal=True,
    )

    assert result.status == "unresolved"
    assert result.reason == "waiting_settlement_evidence"
    close_events = [item for item in repo.list_trade_events() if item["position_effect"] == "close"]
    assert close_events == []
    assert repo.get_record_fields(lot_id)["contracts_open"] == 10


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
