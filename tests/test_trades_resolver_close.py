from __future__ import annotations

from dataclasses import replace

import src.application.ledger.manual_trades as ledger_manual_trades
import src.application.ledger.repository as ledger_repository
import pytest

from domain.domain.ledger import ContractKey, TradeEvent
from domain.domain.trade_contract_identity import derive_trade_side
from domain.domain.ledger.position_fields import effective_expiration_ymd
from domain.domain.option_lifecycle import expiration_observation_start_ms
from src.application.ledger.api import (
    find_unique_open_position_lot,
    summarize_broker_trade_close_candidates,
)
from src.application.ledger.writer import persist_trade_event_object
from src.application.ledger.current_decision_projection import (
    build_current_decision_projection,
    current_decision_projection_row,
    empty_assigned_stock_fact,
    read_current_decision_projection,
)
from src.application.trades.normalizer import NormalizedTradeDeal
from src.application.trades.resolver import (
    load_close_candidate_records,
    match_close_positions,
    match_close_targets,
    resolve_trade_deal,
)
from src.application.trades.lifecycle import (
    _stock_matches_lifecycle_close,
    lifecycle_deal_economic_hash,
    resolve_lifecycle_expired_unassigned,
)
from src.application.trades.lifecycle_reconciliation import (
    discover_lifecycle_cases,
    lifecycle_case_read_model,
    reconcile_lifecycle_evidence,
)


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

    def get_record_fields(self, lot_id: str) -> dict:
        for item in self.records:
            if item["record_id"] == lot_id:
                return dict(item["fields"])
        raise KeyError(lot_id)

    def update_record(self, lot_id: str, fields: dict) -> dict:
        self.updated.append({"record_id": lot_id, "fields": fields})
        return {"record": {"record_id": lot_id}}


def _record(lot_id: str, opened_at: int, contracts_open: int) -> dict:
    return {
        "record_id": lot_id,
        "fields": {
            "record_id": lot_id,
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
            strike=fields.get("strike"),
            expiration_ymd=effective_expiration_ymd(fields),
                ),
        contracts=int(fields.get("contracts") or fields.get("contracts_open") or 0),
        price=1.0,
        currency=str(fields.get("currency") or "HKD"),
        source="test_seed_open_lot",
        multiplier=float(fields.get("multiplier") or 100),
        lot_id=str(record["record_id"]),
        raw_payload={
            # §9.2 step 3: the contract key no longer carries the position
            # side, so translate the record's side into the trade side.
            "side": derive_trade_side("open", fields.get("side")) or "",
            "source_type": "test_seed",
        },
    ).to_dict()


def _record_with_expiration(lot_id: str, opened_at: int, contracts_open: int, expiration: int) -> dict:
    row = _record(lot_id, opened_at, contracts_open)
    row["fields"]["expiration"] = expiration
    return row


def _long_record(lot_id: str, opened_at: int, contracts_open: int) -> dict:
    row = _record(lot_id, opened_at, contracts_open)
    row["fields"]["side"] = "long"
    return row


def _persist_lot(repo: object, **overrides: object) -> None:
    """Persist one manual open lot into ``repo``.

    The defaults are the TIGR short put this module repeats most often, so a
    call site spells out only the fields that differ from it.
    """
    base: dict[str, object] = {
        "broker": "富途",
        "account": "lx",
        "symbol": "TIGR",
        "option_type": "put",
        "side": "short",
        "contracts": 10,
        "currency": "USD",
        "strike": 6.0,
        "multiplier": 100,
        "expiration_ymd": "2026-05-22",
        "premium_per_share": 0.2,
        "opened_at_ms": 1779129617118,
    }
    base.update(overrides)
    ledger_manual_trades.persist_manual_open_event(repo, **base)


def _open_lot(tmp_path, **overrides: object):
    """Build a fresh SQLite position repo holding a single manual open lot."""
    repo = ledger_repository.SQLiteOptionPositionsRepository(
        tmp_path / "option_positions.sqlite3"
    )
    _persist_lot(repo, **overrides)
    return repo


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


def _lot_close_type(repo, lot_id: str) -> str:
    """The lot's close type, read from its closing event's payload.

    ``close_type`` left ``fields_json`` in the convergence batch
    (``write-side-definition.md`` §2); its home is the closing trade event, and
    the lot payload keeps only the id (``last_event_id``).
    """
    fields = repo.get_record_fields(lot_id)
    wanted = str(fields.get("last_event_id") or "").strip()
    for event in repo.list_trade_events():
        if str(event.get("event_id") or "").strip() != wanted:
            continue
        return (
            str((event.get("raw_payload") or {}).get("close_type") or "")
            .strip()
            .lower()
        )
    return ""


def test_match_close_positions_uses_fifo() -> None:
    repo = FakeRepo([_record("rec1", 100, 1), _record("rec2", 200, 2)])

    matches = match_close_positions(repo, _deal())

    assert [(m.lot_id, m.contracts_to_close) for m in matches] == [("rec1", 1), ("rec2", 2)]


def test_match_close_targets_exposes_strict_resolution_contract() -> None:
    repo = FakeRepo([_record("rec1", 100, 1), _record("rec2", 200, 2)])

    resolution = match_close_targets(repo, _deal())

    assert resolution.source == "broker_trade_close"
    assert resolution.strategy == "strict_exact_fifo"
    assert resolution.selector["expiration_ymd"] == "2026-04-29"
    assert resolution.lot_ids == ("rec1", "rec2")
    assert resolution.to_dict()["contracts_to_close"] == 3


@pytest.mark.parametrize("apply_changes", [False, True])
def test_close_rejects_same_contract_with_different_multiplier(apply_changes):
    repo = FakeRepo([_record("rec1", 100, 1)])
    result = resolve_trade_deal(_deal(contracts=1, multiplier=10), repo=repo,
                                state={}, apply_changes=apply_changes)
    assert result.status == "unresolved"
    assert "unsupported_contract_multiplier" in str(result.to_dict())
    assert repo.updated == []


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

    assert resolution.lot_ids == ("may_put",)
    assert resolution.to_dict()["targets"][0]["candidate"]["expiration_ymd"] == "2026-04-29"


def test_match_close_positions_ignores_market_only_persisted_rows() -> None:
    market_only = _record("rec1", 100, 1)
    market_only["fields"].pop("broker", None)
    market_only["fields"]["market"] = "富途"
    repo = FakeRepo([market_only, _record("rec2", 200, 3)])

    matches = match_close_positions(repo, _deal())

    assert [(m.lot_id, m.contracts_to_close) for m in matches] == [("rec2", 3)]


def test_match_close_positions_canonicalizes_candidate_and_deal_symbols() -> None:
    raw_alias = _record("rec-pop", 100, 1)
    raw_alias["fields"]["symbol"] = "POP"
    repo = FakeRepo([raw_alias])

    matches = match_close_positions(repo, _deal(symbol="HK.09992", contracts=1))

    assert [(m.lot_id, m.contracts_to_close) for m in matches] == [("rec-pop", 1)]


def test_ledger_close_helpers_canonicalize_aliases_and_summarize_candidates() -> None:
    raw_alias = _record("rec-pop", 100, 2)
    raw_alias["fields"]["symbol"] = "POP"
    repo = FakeRepo([raw_alias])

    candidate = find_unique_open_position_lot(
        repo,
        account="lx",
        symbol="HK.09992",
        option_type="put",
        side="short",
        expiration_ymd="2026-04-29",
    )
    summary = summarize_broker_trade_close_candidates(repo, deal=_deal(symbol="HK.09992", contracts=1))

    assert candidate is not None
    assert candidate["record_id"] == "rec-pop"
    assert summary == {
        "semantic_count": 1,
        "exact_contract_count": 1,
        "exact_open_contracts": 2,
        "requested_contracts": 1,
    }


@pytest.mark.parametrize(
    ("side", "close_action", "close_type"),
    [
        pytest.param("buy", "buy_close", "buy_to_close", id="close_dry_run_builds_patches"),
        pytest.param("sell", "sell_close", "sell_to_close", id="long_close_dry_run_builds_patches"),
    ],
)
def test_resolve_trade_close_dry_run_builds_patches(side, close_action, close_type) -> None:
    record = _long_record if side == "sell" else _record
    repo = FakeRepo([record("rec1", 100, 1), record("rec2", 200, 2)])

    result = resolve_trade_deal(_deal(side=side), repo=repo, state={}, apply_changes=False)

    assert result.status == "dry_run"
    assert result.action == "close"
    assert result.diagnostics["close_target_resolution"]["record_ids"] == ["rec1", "rec2"]
    assert len(result.operations) == 2
    assert result.operations[0].to_payload()["close_target_resolution"]["record_ids"] == ["rec1", "rec2"]
    assert result.operations[0].to_payload()["action"] == close_action
    assert result.operations[0].to_payload()["patch"]["contracts_open"] == 0
    assert result.operations[0].to_payload()["patch"]["close_type"] == close_type


def test_resolve_unknown_buy_call_prefers_existing_short_call_close() -> None:
    short_call = _record("short-call", 100, 1)
    short_call["fields"]["option_type"] = "call"
    repo = FakeRepo([short_call])

    result = resolve_trade_deal(
        _deal(option_type="call", side="buy", position_effect=None, contracts=1),
        repo=repo,
        state={},
        apply_changes=False,
    )

    assert result.status == "dry_run"
    assert result.action == "close"
    assert result.operations[0].to_payload()["record_id"] == "short-call"
    assert result.diagnostics["position_effect_inference"]["decision"] == "close"


def test_resolve_trade_close_dry_run_routes_zero_price_expiry_leg_to_lifecycle_pending(tmp_path) -> None:
    repo = _open_lot(tmp_path, symbol="0700.HK", contracts=3, strike=480, currency="HKD",
                     expiration_ymd="2026-04-29", opened_at_ms=100)

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
    assert result.operations[0].to_payload()["action"] == "lifecycle_pending"
    assert result.diagnostics["decision"]["decision_type"] == "needs_review"


def test_resolve_trade_close_skips_failed_deal_by_default() -> None:
    repo = FakeRepo([_record("rec1", 100, 3)])

    result = resolve_trade_deal(
        _deal(),
        repo=repo,
        state={"failed_deal_ids": {"futu:lx:REAL_1:deal-close-1": {"status": "failed", "account": "lx", "reason": "exception:LedgerPreflightError"}}},
        apply_changes=False,
    )

    assert result.status == "skipped"
    assert result.reason == "duplicate_deal_id"


def test_resolve_trade_close_retries_failed_deal_when_explicitly_allowed() -> None:
    repo = FakeRepo([_record("rec1", 100, 3)])

    result = resolve_trade_deal(
        _deal(),
        repo=repo,
        state={"failed_deal_ids": {"deal-close-1": {"status": "failed", "account": "lx", "reason": "exception:LedgerPreflightError"}}},
        apply_changes=False,
        retry_failed_deal=True,
    )

    assert result.status == "dry_run"
    assert result.action == "close"
    assert result.reason == "preview_close"


def test_resolve_trade_open_accepts_futu_float_transport_noise() -> None:
    result = resolve_trade_deal(
        _deal(
            deal_id="deal-float-transport-noise",
            order_id="order-float-transport-noise",
            symbol="3690.HK",
            side="sell",
            position_effect="open",
            contracts=1,
            price=1.5699999999999998,
            strike=80.0,
            multiplier=500,
            expiration_ymd="2026-09-29",
            trade_time_ms=1_786_512_490_000,
        ),
        repo=FakeRepo([]),
        state={},
        apply_changes=False,
    )

    assert result.status == "dry_run"
    assert result.action == "open"
    assert result.reason == "preview_open"
    assert result.operations[0].to_payload()["fields"]["premium"] == 1.57


@pytest.mark.parametrize(
    ("side", "close_action"),
    [
        pytest.param("buy", "buy_close", id="close_apply_updates_records"),
        pytest.param("sell", "sell_close", id="long_close_apply_updates_records"),
    ],
)
def test_resolve_trade_close_apply_updates_records(side, close_action) -> None:
    record = _long_record if side == "sell" else _record
    repo = FakeRepo([record("rec1", 100, 1), record("rec2", 200, 2)])
    result = resolve_trade_deal(
        _deal(side=side),
        repo=repo,
        state={},
        apply_changes=True,
        persist_trade_event_fn=lambda repo, deal: {"event_id": deal.deal_id, "created": True},
    )

    assert result.status == "applied"
    assert [row.lot_id for row in result.operations] == ["rec1", "rec2"]
    assert [row.action for row in result.operations] == [close_action, close_action]
    assert result.diagnostics["close_target_resolution"]["strategy"] == "strict_exact_fifo"
    assert repo.updated == []


# Kept as a by-name entry point for the merged long-lot cases above.
def test_resolve_trade_long_close_dry_run_builds_patches() -> None:
    test_resolve_trade_close_dry_run_builds_patches("sell", "sell_close", "sell_to_close")


def test_resolve_trade_long_close_apply_updates_records() -> None:
    test_resolve_trade_close_apply_updates_records("sell", "sell_close")


def test_resolve_trade_close_apply_persists_per_lot_target_events(tmp_path) -> None:

    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    for opened_at, contracts in ((100, 1), (200, 2)):
        _persist_lot(
            repo,
            symbol="0700.HK",
            contracts=contracts,
            currency="HKD",
            strike=480.0,
            expiration_ymd="2026-04-29",
            premium_per_share=3.93,
            opened_at_ms=opened_at,
        )
    open_lot_ids = [row["record_id"] for row in repo.list_position_lots()]

    result = resolve_trade_deal(
        _deal(contracts=3, trade_time_ms=5000),
        repo=repo,
        state={},
        apply_changes=True,
    )

    assert result.status == "applied"
    assert [row.lot_id for row in result.operations] == open_lot_ids
    assert [row.contracts_to_close for row in result.operations] == [1, 2]
    assert {row.ledger_preflight.event_type for row in result.operations} == {"close"}
    close_events = [item for item in repo.list_trade_events() if item["position_effect"] == "close"]
    assert {item["raw_payload"]["record_id"] for item in close_events} == set(open_lot_ids)
    assert {tuple(item["raw_payload"]["close_target_resolution"]["record_ids"]) for item in close_events} == {
        tuple(open_lot_ids)
    }
    assert {item["raw_payload"]["source_deal_id"] for item in close_events} == {"deal-close-1"}
    assert all(str(item["event_id"]).startswith("futu:lx:REAL_1:deal-close-1:close:") for item in close_events)
    outbox_ids = {
        str(operation.result.to_dict().get("notification_outbox_id") or "")
        for operation in result.operations
        if operation.result is not None
    }
    assert len(outbox_ids) == 1
    outbox_id = next(iter(outbox_ids))
    assert outbox_id
    assert repo.get_trade_lifecycle_notification(outbox_id)["outbox_id"] == outbox_id
    lots = repo.list_position_lots()
    assert all(item["fields"]["status"] == "close" for item in lots)
    assert all(item["fields"]["contracts_open"] == 0 for item in lots)


def test_multi_lot_broker_close_rolls_back_every_split_when_second_write_fails(
    tmp_path,
    monkeypatch,
) -> None:

    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    for opened_at, contracts in ((100, 1), (200, 2)):
        _persist_lot(
            repo,
            symbol="0700.HK",
            contracts=contracts,
            currency="HKD",
            strike=480.0,
            expiration_ymd="2026-04-29",
            premium_per_share=3.93,
            opened_at_ms=opened_at,
        )

    original_upsert = repo.upsert_trade_event
    close_write_count = 0

    def _fail_second_close(event, *, conn=None):
        nonlocal close_write_count
        if str(getattr(event, "event_type", "")) == "close":
            close_write_count += 1
            if close_write_count == 2:
                raise RuntimeError("injected second split failure")
        return original_upsert(event, conn=conn)

    monkeypatch.setattr(repo, "upsert_trade_event", _fail_second_close)

    with pytest.raises(RuntimeError, match="injected second split failure"):
        resolve_trade_deal(
            _deal(contracts=3, trade_time_ms=5000),
            repo=repo,
            state={},
            apply_changes=True,
        )

    close_events = [
        item for item in repo.list_trade_events()
        if item["position_effect"] == "close"
    ]
    assert close_events == []
    assert [item["fields"]["contracts_open"] for item in repo.list_position_lots()] == [1, 2]


def test_multi_lot_broker_close_declares_complete_deal_split_metadata(tmp_path) -> None:
    from src.application.trades.deal_identity import completed_ledger_deal_ids

    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    for opened_at, contracts in ((100, 1), (200, 2)):
        _persist_lot(
            repo,
            symbol="0700.HK",
            contracts=contracts,
            currency="HKD",
            strike=480.0,
            expiration_ymd="2026-04-29",
            premium_per_share=3.93,
            opened_at_ms=opened_at,
        )

    result = resolve_trade_deal(
        _deal(contracts=3, trade_time_ms=5000, raw_payload={"qty": 3}),
        repo=repo,
        state={},
        apply_changes=True,
    )

    assert result.status == "applied"
    close_events = [
        item for item in repo.list_trade_events()
        if item["position_effect"] == "close"
    ]
    assert sorted(
        item["raw_payload"]["broker_deal_completion"]["split_index"]
        for item in close_events
    ) == [1, 2]
    assert {
        item["raw_payload"]["broker_deal_completion"]["expected_contracts"]
        for item in close_events
    } == {3}
    assert "deal-close-1" in completed_ledger_deal_ids(close_events)


def test_late_zero_price_evidence_does_not_adopt_unbound_expire_close(
    tmp_path,
) -> None:
    repo = _open_lot(tmp_path, contracts=1)
    lot_id = repo.list_position_lots()[0]["record_id"]
    persist_trade_event_object(
        repo,
        TradeEvent(
            event_id="generic-expire-close-before-broker-evidence",
            event_type="expire_close",
            event_time_ms=1779468400000,
            contract_key=ContractKey.from_values(
                broker="富途",
                account="lx",
                underlying_symbol="TIGR",
                option_type="put",
                strike=6.0,
                expiration_ymd="2026-05-22",
                        ),
            contracts=1,
            price=0.0,
            currency="USD",
            source="auto_close_expired",
            multiplier=100,
            target_lot_id=lot_id,
            raw_payload={
                "record_id": lot_id,
                "target_lot_id": lot_id,
                "close_type": "expire_auto_close",
            },
        ),
    )
    event_count_before = len(repo.list_trade_events())

    result = resolve_trade_deal(
        _deal(
            deal_id="late-option-zero-price",
            symbol="TIGR",
            contracts=1,
            price=0.0,
            strike=6.0,
            expiration_ymd="2026-05-22",
            currency="USD",
            trade_time_ms=1779468600000,
            raw_payload={
                "deal_id": "late-option-zero-price",
                "code": "US.TIGR260522P6000",
            },
        ),
        repo=repo,
        state={},
        apply_changes=True,
    )

    assert result.status == "unresolved"
    assert result.reason == "lifecycle_close_target_not_found"
    assert len(repo.list_trade_events()) == event_count_before
    assert repo.list_trade_lifecycle_cases() == []
    assert repo.list_trade_lifecycle_evidence() == []


def test_resolve_trade_close_apply_records_zero_price_option_leg_pending_reason(tmp_path) -> None:
    repo = _open_lot(tmp_path)
    lot_id = repo.list_position_lots()[0]["record_id"]

    deal = _deal(
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
        )
    result = resolve_trade_deal(
        deal,
        repo=repo,
        state={},
        apply_changes=True,
    )

    assert result.status == "applied"
    assert result.action == "lifecycle"
    assert result.reason == "close_reason_pending"
    close_events = [item for item in repo.list_trade_events() if item["position_effect"] == "close"]
    assert len(close_events) == 1
    assert close_events[0]["event_time_ms"] == 1779468493916
    assert close_events[0]["price"] == "0"
    assert close_events[0]["raw_payload"]["close_type"] == "cause_pending"
    cases = repo.list_trade_lifecycle_cases()
    assert cases[0]["status"] == "ledger_written"
    assert repo.get_record_fields(lot_id)["contracts_open"] == 0
    model = lifecycle_case_read_model(repo, case_id=cases[0]["case_id"])
    assert model["reason_state"] == "cause_pending"
    assert model["pending_close_contracts_by_lot"] == {lot_id: 10}
    replay = resolve_trade_deal(deal, repo=repo, state={}, apply_changes=True)
    assert replay.status == "skipped"
    assert len([item for item in repo.list_trade_events() if item["position_effect"] == "close"]) == 1
    changed = resolve_trade_deal(
        replace(deal, trade_time_ms=deal.trade_time_ms + 1),
        repo=repo, state={}, apply_changes=True,
    )
    assert changed.status == "unresolved"
    assert changed.reason == "lifecycle_source_event_already_consumed"


@pytest.mark.parametrize("failure_point", ["after_claim", "after_projection"])
def test_zero_price_close_failure_rolls_back_anchor_and_position(
    tmp_path, monkeypatch: pytest.MonkeyPatch, failure_point: str,
) -> None:
    import src.application.ledger.writer_lifecycle_allocation as allocation_writer

    repo = _open_lot(tmp_path)
    original = allocation_writer.apply_lifecycle_allocation_atomically
    before_events = len(repo.list_trade_events())
    lot_id = repo.list_position_lots()[0]["record_id"]

    def fail(*args, **kwargs):
        if failure_point == "after_projection":
            original(*args, **kwargs)
        raise RuntimeError(failure_point)

    monkeypatch.setattr(allocation_writer, "apply_lifecycle_allocation_atomically", fail)
    with pytest.raises(RuntimeError, match=failure_point):
        resolve_trade_deal(
            _deal(
                deal_id=failure_point, symbol="TIGR", contracts=10,
                price=0, strike=6, expiration_ymd="2026-05-22",
                currency="USD",
                trade_time_ms=1779468493916,
                raw_payload={"deal_id": failure_point, "code": "US.TIGR260522P6000"},
            ),
            repo=repo, state={}, apply_changes=True,
        )
    assert len(repo.list_trade_events()) == before_events
    assert repo.list_trade_lifecycle_cases() == []
    assert repo.list_trade_lifecycle_evidence() == []
    assert repo.list_trade_lifecycle_source_consumptions() == []
    assert repo.get_record_fields(lot_id)["contracts_open"] == 10


def test_zero_price_close_updates_trusted_decision_snapshot(tmp_path) -> None:
    repo = _open_lot(tmp_path)
    with repo._connect() as conn:
        conn.execute(
            "INSERT INTO current_decision_input_generations ("
            "account, generation, case_generation, evidence_generation, "
            "allocation_generation, source_consumption_generation, "
            "timing_generation, combo_identity_generation, "
            "assigned_stock_generation, updated_at_ms) "
            "VALUES ('lx', 0, 0, 0, 0, 0, 0, 0, 0, 1)"
        )
    payload = build_current_decision_projection(
        repo, account="lx", updated_at_ms=1779468493910,
        assigned_stock_after=empty_assigned_stock_fact("lx"),
        all_quality_case_facts=[],
    )
    repo.upsert_current_decision_projection(current_decision_projection_row(payload))
    result = resolve_trade_deal(
        _deal(
            deal_id="snapshot-close", symbol="TIGR", contracts=10,
            price=0, strike=6, expiration_ymd="2026-05-22",
            currency="USD", trade_time_ms=1779468493916,
            raw_payload={"deal_id": "snapshot-close", "code": "US.TIGR260522P6000"},
        ),
        repo=repo, state={}, apply_changes=True,
    )
    assert result.status == "applied"
    snapshot = read_current_decision_projection(
        repo, account="lx", now_ms=1779468493920,
    )
    assert snapshot["status"] == "trusted", (
        snapshot,
        result.diagnostics["lifecycle_adoption"]["economic_close"]["decision_projection"],
    )
    case = repo.list_trade_lifecycle_cases()[0]
    assert snapshot["lifecycle_by_case"].get(case["case_id"], {}).get("reason_state") == "cause_pending", (
        snapshot["lifecycle_by_case"],
        result.diagnostics["lifecycle_adoption"]["economic_close"]["decision_projection"],
    )
    settled = resolve_trade_deal(
        _deal(
            deal_id="snapshot-stock", symbol="TIGR", option_type=None,
            side="buy", position_effect=None, contracts=1000, price=6,
            strike=None, multiplier=None, expiration_ymd=None, currency="USD",
            trade_time_ms=1779468500000,
            raw_payload={"deal_id": "snapshot-stock", "code": "US.TIGR"},
        ),
        repo=repo, state={}, apply_changes=True,
    )
    assert settled.status == "applied"
    snapshot_after = read_current_decision_projection(
        repo, account="lx", now_ms=1779468500010,
    )
    assert snapshot_after["status"] == "trusted", snapshot_after
    assert snapshot_after["lifecycle_by_case"][case["case_id"]]["reason_state"] == "resolved"


def test_zero_price_close_reopened_same_contract_uses_new_case(tmp_path) -> None:
    repo = _open_lot(tmp_path)

    def close(deal_id: str, trade_time_ms: int):
        return resolve_trade_deal(
            _deal(
                deal_id=deal_id, symbol="TIGR", contracts=10,
                price=0, strike=6, expiration_ymd="2026-05-22",
                currency="USD", trade_time_ms=trade_time_ms,
                raw_payload={"deal_id": deal_id, "code": "US.TIGR260522P6000"},
            ), repo=repo, state={}, apply_changes=True,
        )

    assert close("first-close", 1779382093916).status == "applied"
    first_case = repo.list_trade_lifecycle_cases()[0]["case_id"]
    _persist_lot(repo, opened_at_ms=1779382094000)
    assert close("second-close", 1779382095000).status == "applied"
    case_ids = {case["case_id"] for case in repo.list_trade_lifecycle_cases()}
    assert len(case_ids) == 2
    assert first_case in case_ids
    assert all(repo.get_record_fields(lot["record_id"])["contracts_open"] == 0
               for lot in repo.list_position_lots())


def test_zero_price_partial_deals_close_only_confirmed_contracts(tmp_path) -> None:
    repo = _open_lot(tmp_path)
    lot_id = repo.list_position_lots()[0]["record_id"]

    for deal_id, contracts, time_ms, remaining in (
        ("partial-4", 4, 1779382093916, 6),
        ("partial-6", 6, 1779382095000, 0),
    ):
        result = resolve_trade_deal(
            _deal(
                deal_id=deal_id, symbol="TIGR", contracts=contracts,
                price=0, strike=6, expiration_ymd="2026-05-22",
                currency="USD", trade_time_ms=time_ms,
                raw_payload={"deal_id": deal_id, "code": "US.TIGR260522P6000"},
            ), repo=repo, state={}, apply_changes=True,
        )
        assert result.status == "applied"
        assert repo.get_record_fields(lot_id)["contracts_open"] == remaining

    closes = [event for event in repo.list_trade_events()
              if event["position_effect"] == "close"]
    assert [(event["contracts"], event["event_time_ms"]) for event in closes] == [
        (4, 1779382093916), (6, 1779382095000),
    ]


def test_confirm_lifecycle_expired_unassigned_fails_closed_without_broker_observation(
    tmp_path,
) -> None:
    repo = _open_lot(
        tmp_path,
        symbol="0700.HK",
        contracts=2,
        currency="HKD",
        strike=440.0,
        expiration_ymd="2026-06-05",
        premium_per_share=0.86,
        opened_at_ms=1780354364000,
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
    assert option_result.reason == "close_reason_pending"
    status_before = repo.list_trade_lifecycle_cases()[0]["status"]

    result = resolve_lifecycle_expired_unassigned(
        repo,
        deal_id="775828694842258876",
        apply_changes=True,
    )

    assert result.status == "unresolved"
    assert result.action == "expire_close"
    assert result.reason == "manual_expiration_confirmation_retired"
    close_events = [item for item in repo.list_trade_events() if item["event_type"] == "expire_close"]
    assert close_events == []
    cases = repo.list_trade_lifecycle_cases()
    assert cases[0]["status"] == status_before
    fields = repo.get_record_fields(lot_id)
    assert fields["status"] == "close"
    assert fields["contracts_open"] == 0


def test_resolve_trade_close_retry_failed_routes_early_zero_price_assignment_to_lifecycle_pending(tmp_path) -> None:
    repo = _open_lot(
        tmp_path,
        symbol="FUTU",
        contracts=1,
        strike=120.0,
        expiration_ymd="2026-06-05",
        premium_per_share=3.6,
        opened_at_ms=1779129615442,
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

    assert result.status == "applied"
    assert result.action == "lifecycle"
    assert result.reason == "close_reason_pending"
    close_events = [item for item in repo.list_trade_events() if item["position_effect"] == "close"]
    assert len(close_events) == 1
    cases = repo.list_trade_lifecycle_cases()
    assert cases[0]["symbol"] == "FUTU"
    assert cases[0]["status"] == "ledger_written"
    assert result.diagnostics["lifecycle_schema_version"] == "lifecycle_case.v2"
    evidence = repo.list_trade_lifecycle_evidence(case_id=cases[0]["case_id"])
    assert evidence[0]["source_event_id"] == "futu:lx:REAL_1:3254612655429789712"
    assert evidence[0]["evidence_type"] == "option_zero_price_close"
    assert repo.get_record_fields(lot_id)["contracts_open"] == 0


def test_resolve_trade_lifecycle_retry_without_open_target_fails_closed(
    tmp_path,
) -> None:
    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    deal_kwargs = {
        "deal_id": "deal-lifecycle-retry-1",
        "order_id": "order-lifecycle-retry-1",
        "symbol": "FUTU",
        "contracts": 1,
        "price": 0.0,
        "strike": 120.0,
        "expiration_ymd": "2026-06-05",
        "currency": "USD",
        "trade_time_ms": 1780506955360,
        "raw_payload": {"deal_id": "deal-lifecycle-retry-1", "code": "US.FUTU260605P120000"},
    }

    first = resolve_trade_deal(
        _deal(
            **deal_kwargs,
            normalization_diagnostics={
                "multiplier_resolution": {
                    "attempted_sources": [
                        {"source": "payload", "status": "missing"},
                        {"source": "cache", "status": "miss"},
                        {"source": "opend", "status": "resolved", "value": 100},
                    ]
                }
            },
        ),
        repo=repo,
        state={},
        apply_changes=True,
    )
    assert first.status == "unresolved"
    assert first.reason == "lifecycle_close_target_not_found"

    retry = resolve_trade_deal(
        _deal(
            **deal_kwargs,
            normalization_diagnostics={
                "multiplier_resolution": {
                    "attempted_sources": [
                        {"source": "payload", "status": "missing"},
                        {"source": "cache", "status": "resolved", "value": 100},
                    ]
                }
            },
        ),
        repo=repo,
        state={
            "unresolved_deal_ids": {
                "deal-lifecycle-retry-1": {
                    "status": "unresolved",
                    "account": "lx",
                    "retryable": True,
                }
            }
        },
        apply_changes=True,
    )

    assert retry.status == "unresolved"
    assert retry.reason == "lifecycle_close_target_not_found"
    assert repo.list_trade_lifecycle_cases() == []
    assert repo.list_trade_lifecycle_evidence() == []


@pytest.mark.parametrize(
    ("first_leg", "pending_reason"),
    [
        pytest.param(
            "option",
            "waiting_settlement_evidence",
            id="option_first_records_early_assignment_before_expiration",
        ),
        pytest.param(
            "stock",
            "stock_settlement_waiting_option_leg",
            id="stock_first_records_early_assignment_before_expiration",
        ),
    ],
)
def test_resolve_trade_lifecycle_records_early_assignment_before_expiration(
    tmp_path, first_leg: str, pending_reason: str
) -> None:
    repo = _open_lot(
        tmp_path,
        symbol="FUTU",
        contracts=1,
        strike=117.45,
        expiration_ymd="2026-06-18",
        premium_per_share=5.2,
        opened_at_ms=1779129615891,
    )
    lot_id = repo.list_position_lots()[0]["record_id"]
    legs = {
        "option": _deal(
            deal_id="6182783325760874067",
            order_id="FH1CA6D913A3AE8000",
            symbol="FUTU",
            contracts=1,
            price=0.0,
            strike=117.45,
            expiration_ymd="2026-06-18",
            currency="USD",
            trade_time_ms=1781025088633,
            raw_payload={"deal_id": "6182783325760874067", "code": "US.FUTU260618P117450"},
        ),
        "stock": _deal(
            deal_id="8433576313500456302",
            order_id="FH1CA6D9142E648000",
            symbol="FUTU",
            option_type=None,
            side="buy",
            position_effect=None,
            contracts=100,
            price=117.45,
            strike=None,
            multiplier=None,
            expiration_ymd=None,
            currency="USD",
            trade_time_ms=1781025089183,
            raw_payload={"deal_id": "8433576313500456302", "code": "US.FUTU"},
        ),
    }
    other_leg = "stock" if first_leg == "option" else "option"

    first_result = resolve_trade_deal(legs[first_leg], repo=repo, state={}, apply_changes=True)

    assert first_result.status == ("applied" if first_leg == "option" else "unresolved")
    assert first_result.reason == ("close_reason_pending" if first_leg == "option" else pending_reason)

    second_result = resolve_trade_deal(legs[other_leg], repo=repo, state={}, apply_changes=True)

    assert second_result.status == "applied"
    assert second_result.action == "assignment"
    assert second_result.reason == "assignment_recorded"
    assignment_events = [item for item in repo.list_trade_events() if item.get("event_type") == "assignment"]
    assert len(assignment_events) == 1
    assert assignment_events[0]["raw_payload"]["record_id"] == lot_id
    assert (
        assignment_events[0]["raw_payload"]["stock_settlement"]["source_event_id"]
        == "futu:lx:REAL_1:8433576313500456302"
    )
    assert assignment_events[0]["raw_payload"]["stock_settlement"]["shares"] == 100
    assert assignment_events[0]["raw_payload"]["stock_settlement"]["price"] == 117.45
    assert repo.get_record_fields(lot_id)["contracts_open"] == 0
    assert _lot_close_type(repo, lot_id) == "assignment"


# These two names are a by-name contract: tests/quality/test_om_quality_checks.py
# reaches them with getattr() to reuse the fixture they build, so the merged
# parametrized body above keeps thin entries under the original names.
def test_resolve_trade_lifecycle_option_first_records_early_assignment_before_expiration(tmp_path) -> None:
    test_resolve_trade_lifecycle_records_early_assignment_before_expiration(
        tmp_path, "option", "waiting_settlement_evidence"
    )


def test_resolve_trade_lifecycle_stock_first_records_early_assignment_before_expiration(tmp_path) -> None:
    test_resolve_trade_lifecycle_records_early_assignment_before_expiration(
        tmp_path, "stock", "stock_settlement_waiting_option_leg"
    )


@pytest.mark.parametrize("readback_failure", [False, True])
def test_stock_first_reason_failure_retries_without_reclosing(
    tmp_path, monkeypatch, readback_failure: bool,
) -> None:
    import src.application.trades.lifecycle as lifecycle_module

    repo = _open_lot(tmp_path, contracts=1)
    lot_id = repo.list_position_lots()[0]["record_id"]
    stock = _deal(
        deal_id="stock-before-retry", symbol="TIGR", option_type=None,
        side="buy", position_effect=None, contracts=100, price=6,
        strike=None, multiplier=None, expiration_ymd=None, currency="USD",
        trade_time_ms=1779468500000,
        raw_payload={"deal_id": "stock-before-retry", "code": "US.TIGR"},
    )
    option = _deal(
        deal_id="option-after-stock", symbol="TIGR", contracts=1,
        price=0, strike=6, expiration_ymd="2026-05-22", currency="USD",
        trade_time_ms=1779468493916,
        raw_payload={"deal_id": "option-after-stock", "code": "US.TIGR260522P6000"},
    )
    assert resolve_trade_deal(stock, repo=repo, state={}, apply_changes=True).status == "unresolved"

    original = lifecycle_module._write_lifecycle_close_from_case
    original_list_events = repo.list_trade_events

    def fail_reason(*_args, **_kwargs):
        if readback_failure:
            monkeypatch.setattr(
                repo, "list_trade_events",
                lambda: (_ for _ in ()).throw(RuntimeError("readback unavailable")),
            )
        raise RuntimeError("reason correction unavailable")

    monkeypatch.setattr(lifecycle_module, "_write_lifecycle_close_from_case", fail_reason)
    first = resolve_trade_deal(option, repo=repo, state={}, apply_changes=True)
    assert first.status == "applied"
    assert first.reason == "close_reason_pending"
    assert first.diagnostics["reason_correction_error"] == "RuntimeError: reason correction unavailable"
    if readback_failure:
        assert first.diagnostics["reason_correction_readback_error"] == "RuntimeError: readback unavailable"
        monkeypatch.setattr(repo, "list_trade_events", original_list_events)
    assert repo.get_record_fields(lot_id)["contracts_open"] == 0
    assert _lot_close_type(repo, lot_id) == "cause_pending"

    monkeypatch.setattr(lifecycle_module, "_write_lifecycle_close_from_case", original)
    retry = resolve_trade_deal(option, repo=repo, state={}, apply_changes=True)
    assert retry.status == "applied"
    assert retry.action == "assignment"
    assert _lot_close_type(repo, lot_id) == "assignment"
    assert repo.get_record_fields(lot_id)["contracts_open"] == 0
    assert len([event for event in repo.list_trade_events() if event["event_type"] == "assignment"]) == 1
    assert len([row for row in repo.list_trade_lifecycle_source_consumptions()
                if row["source_role"] == "stock_settlement"]) == 1
    duplicate = resolve_trade_deal(option, repo=repo, state={}, apply_changes=True)
    assert duplicate.status == "skipped"
    assert duplicate.action == "assignment"
    assert len([event for event in repo.list_trade_events()
                if event["event_type"] == "assignment"]) == 1


@pytest.mark.parametrize("fee_timing", ["before_reason", "after_reason"])
def test_resolve_trade_lifecycle_option_first_stock_settlement_records_assignment(tmp_path, monkeypatch, fee_timing) -> None:
    repo = _open_lot(tmp_path)
    lot_id = repo.list_position_lots()[0]["record_id"]
    fx = {"rates": {"USDCNY": 7.2}, "timestamp": "2026-05-22T16:48:13+00:00"}
    monkeypatch.setattr("src.application.ledger.writer_lifecycle_evidence.load_cash_fx_payload", lambda *_args, **_kwargs: fx)
    monkeypatch.setattr("src.application.ledger.writer_lifecycle_allocation.load_cash_fx_payload", lambda *_args, **_kwargs: fx)
    monkeypatch.setattr("src.application.ledger.order_fee_migration.load_cash_fx_payload", lambda *_args, **_kwargs: fx)

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

    assert option_result.status == "applied"
    assert repo.get_record_fields(lot_id)["contracts_open"] == 0
    assert _lot_close_type(repo, lot_id) == "cause_pending"
    from src.application.trades.order_fee_sync import recover_order_fee_targets, sync_order_fees

    class FeeProvider:
        def fetch_terminal_orders(self, **kwargs):
            return {"order-1": {
                "status": "terminal_with_fill", "dealt_qty": "10", "currency": "USD",
            }}, {}

        def fetch_order_fees(self, **kwargs):
            return {"order-1": {"fee_amount": "2.00", "fee_details": {}}}, {}

    def sync_fee():
        fee_targets = recover_order_fee_targets(repo, account="lx")["targets"]
        assert ("富途", "lx", "REAL_1", "order-1") in fee_targets
        result = sync_order_fees(
            repo, account="lx", provider=FeeProvider(), apply=True,
            observed_at_ms=1779468501000, futu_account_id="REAL_1",
            target_identity=("富途", "lx", "REAL_1", "order-1"),
        )
        assert result["migration"]["status_counts"].get("committed") == 1

    if fee_timing == "before_reason":
        sync_fee()
        prior_fee_conversion = next(
            row for row in repo.list_trade_events() if row.get("event_type") == "close"
        )["raw_payload"]["cash_conversions"]["option_fee_cash"]
        assert prior_fee_conversion["status"] == "observed"

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

    assert stock_result.status == "applied", stock_result
    assert stock_result.action == "assignment"
    assignment_events = [item for item in repo.list_trade_events() if item.get("event_type") == "assignment"]
    assert len(assignment_events) == 1
    assert assignment_events[0]["raw_payload"]["record_id"] == lot_id
    assert assignment_events[0]["raw_payload"]["stock_settlement"]["shares"] == 1000
    assert repo.get_record_fields(lot_id)["contracts_open"] == 0
    assert _lot_close_type(repo, lot_id) == "assignment"
    if fee_timing == "after_reason":
        sync_fee()
    assignment_after_fee = next(
        row for row in repo.list_trade_events() if row.get("event_type") == "assignment"
    )
    assert assignment_after_fee["raw_payload"]["fee_provenance"]["amount"] == "2"
    fee_conversion = assignment_after_fee["raw_payload"]["cash_conversions"]["option_fee_cash"]
    assert fee_conversion["status"] == "observed"
    assert fee_conversion["amount_cny"] == "-14.4"
    if fee_timing == "before_reason":
        assert fee_conversion["fx_rate"] == prior_fee_conversion["fx_rate"]
        assert fee_conversion["rate_source_id"] == prior_fee_conversion["rate_source_id"]


def test_reason_correction_projection_failure_keeps_pending_close(tmp_path, monkeypatch) -> None:
    repo = _open_lot(tmp_path, contracts=1)
    option = _deal(
        deal_id="rollback-option", symbol="TIGR", contracts=1, price=0,
        strike=6, expiration_ymd="2026-05-22", currency="USD",
        trade_time_ms=1779468493916,
        raw_payload={"deal_id": "rollback-option", "code": "US.TIGR260522P6000"},
    )
    assert resolve_trade_deal(option, repo=repo, state={}, apply_changes=True).status == "applied"
    original_events = repo.list_trade_events()
    original_allocations = repo.list_trade_lifecycle_allocations()
    lot_id = repo.list_position_lots()[0]["record_id"]
    stock = _deal(
        deal_id="rollback-stock", symbol="TIGR", option_type=None,
        side="buy", position_effect=None, contracts=100, price=6,
        strike=None, multiplier=None, expiration_ymd=None, currency="USD",
        trade_time_ms=1779468500000,
        raw_payload={"deal_id": "rollback-stock", "code": "US.TIGR"},
    )
    monkeypatch.setattr(
        "src.application.ledger.writer_lifecycle_allocation.run_position_projection_in_transaction",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("projection_failed")),
    )
    with pytest.raises(RuntimeError, match="projection_failed"):
        resolve_trade_deal(stock, repo=repo, state={}, apply_changes=True)
    assert repo.list_trade_events() == original_events
    assert repo.list_trade_lifecycle_allocations() == original_allocations
    assert repo.get_record_fields(lot_id)["contracts_open"] == 0
    assert _lot_close_type(repo, lot_id) == "cause_pending"


def test_one_stock_settlement_cannot_correct_two_pending_option_deals(tmp_path) -> None:
    repo = _open_lot(tmp_path, contracts=2)
    for index in range(2):
        option = _deal(
            deal_id=f"split-option-{index}", symbol="TIGR", contracts=1,
            price=0, strike=6, expiration_ymd="2026-05-22",
            currency="USD", trade_time_ms=1779468493916 + index,
            raw_payload={"deal_id": f"split-option-{index}", "code": "US.TIGR260522P6000"},
        )
        assert resolve_trade_deal(option, repo=repo, state={}, apply_changes=True).status == "applied"
    stock = _deal(
        deal_id="split-stock", symbol="TIGR", option_type=None,
        side="buy", position_effect=None, contracts=200, price=6,
        strike=None, multiplier=None, expiration_ymd=None, currency="USD",
        trade_time_ms=1779468500000,
        raw_payload={"deal_id": "split-stock", "code": "US.TIGR"},
    )
    settled = resolve_trade_deal(stock, repo=repo, state={}, apply_changes=True)
    assert settled.status == "unresolved"
    assert not [item for item in repo.list_trade_events() if item["event_type"] == "assignment"]
    assert len([item for item in repo.list_trade_events()
                if item["event_type"] == "close" and item["raw_payload"].get("close_type") == "cause_pending"]) == 2
    assert repo.list_position_lots()[0]["fields"]["contracts_open"] == 0


def test_resolve_trade_lifecycle_option_and_stock_pair_uses_frozen_v2_case(tmp_path) -> None:
    repo = _open_lot(tmp_path)
    lot_id = repo.list_position_lots()[0]["record_id"]
    observation_start = expiration_observation_start_ms("2026-05-22", "US")
    assert observation_start is not None

    discovery = discover_lifecycle_cases(
        repo,
        account="lx",
        observed_at_ms=observation_start,
        apply_changes=True,
    )
    assert len(discovery["created_case_ids"]) == 1
    v2_case_id = discovery["created_case_ids"][0]

    option_result = resolve_trade_deal(
        _deal(
            deal_id="option-leg-v2",
            symbol="TIGR",
            contracts=10,
            price=0.0,
            strike=6.0,
            expiration_ymd="2026-05-22",
            currency="USD",
            trade_time_ms=observation_start + 1_000,
            raw_payload={"deal_id": "option-leg-v2", "code": "US.TIGR260522P6000"},
        ),
        repo=repo,
        state={},
        apply_changes=True,
    )
    assert option_result.status == "applied"

    stock_result = resolve_trade_deal(
        _deal(
            deal_id="stock-leg-v2",
            order_id="stock-order-v2",
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
            trade_time_ms=observation_start + 2_000,
            raw_payload={"deal_id": "stock-leg-v2", "code": "US.TIGR"},
        ),
        repo=repo,
        state={},
        apply_changes=True,
    )

    assert stock_result.status == "applied"
    assert stock_result.action == "assignment"
    assert stock_result.reason == "assignment_recorded"
    allocations = repo.list_trade_lifecycle_allocations(case_id=v2_case_id)
    assert len(allocations) == 2
    terminal_allocation = next(item for item in allocations if item["terminal_type"] == "assignment")
    assert terminal_allocation["target_lot_id"] == lot_id
    assert terminal_allocation["contracts_allocated"] == 10
    terminal_event = next(
        item
        for item in repo.list_trade_events()
        if item.get("event_id") == terminal_allocation["canonical_terminal_event_id"]
    )
    assert terminal_event["event_type"] == "assignment"
    assert terminal_event["raw_payload"]["schema_version"] == "lifecycle_terminal_event.v2"
    v2_case = repo.get_trade_lifecycle_case(v2_case_id)
    assert v2_case is not None
    assert v2_case["status"] == "ledger_written"
    assert repo.get_record_fields(lot_id)["contracts_open"] == 0
    assert _lot_close_type(repo, lot_id) == "assignment"


def test_broker_lifecycle_adapter_accumulates_partial_stock_settlement(
    tmp_path,
) -> None:
    repo = _open_lot(tmp_path, contracts=2)
    lot_id = repo.list_position_lots()[0]["record_id"]
    observation_start = expiration_observation_start_ms("2026-05-22", "US")
    assert observation_start is not None
    v2_case_id = discover_lifecycle_cases(
        repo,
        account="lx",
        observed_at_ms=observation_start,
        apply_changes=True,
    )["created_case_ids"][0]

    option = _deal(
        deal_id="partial-option",
        symbol="TIGR",
        contracts=2,
        price=0.0,
        strike=6.0,
        expiration_ymd="2026-05-22",
        currency="USD",
        trade_time_ms=observation_start + 1_000,
        raw_payload={"deal_id": "partial-option", "code": "US.TIGR260522P6000"},
    )
    assert resolve_trade_deal(
        option,
        repo=repo,
        state={},
        apply_changes=True,
    ).status == "applied"
    assert repo.get_record_fields(lot_id)["contracts_open"] == 0

    def stock_leg(deal_id: str, offset: int) -> NormalizedTradeDeal:
        return _deal(
            deal_id=deal_id,
            order_id=f"order-{deal_id}",
            symbol="TIGR",
            option_type=None,
            side="buy",
            position_effect=None,
            contracts=100,
            price=6.0,
            strike=None,
            multiplier=None,
            expiration_ymd=None,
            currency="USD",
            trade_time_ms=observation_start + offset,
            raw_payload={"deal_id": deal_id, "code": "US.TIGR"},
        )

    first = resolve_trade_deal(
        stock_leg("partial-stock-1", 2_000),
        repo=repo,
        state={},
        apply_changes=True,
    )
    assert first.status == "unresolved", first
    assert first.reason == "pending_close_settlement_not_exact"
    assert repo.get_record_fields(lot_id)["contracts_open"] == 0
    assert not [event for event in repo.list_trade_events() if event["event_type"] == "assignment"]

    before_events = len(repo.list_trade_events())
    replay = resolve_trade_deal(
        stock_leg("partial-stock-1", 2_000),
        repo=repo,
        state={},
        apply_changes=True,
    )
    assert replay.status == "unresolved"
    assert repo.get_record_fields(lot_id)["contracts_open"] == 0
    assert len(repo.list_trade_events()) == before_events

    completed = resolve_trade_deal(
        stock_leg("partial-stock-2", 3_000),
        repo=repo,
        state={},
        apply_changes=True,
    )
    assert completed.status == "unresolved"
    assert completed.reason == "pending_close_settlement_not_exact"
    assert repo.get_record_fields(lot_id)["contracts_open"] == 0
    assert repo.get_trade_lifecycle_case(v2_case_id)["status"] == "needs_review"
    assert len(repo.list_trade_lifecycle_allocations(case_id=v2_case_id)) == 1


def test_broker_lifecycle_adapter_creates_v2_case_for_partial_stock_settlement(
    tmp_path,
) -> None:
    repo = _open_lot(tmp_path, contracts=2)
    lot_id = repo.list_position_lots()[0]["record_id"]
    observation_start = expiration_observation_start_ms("2026-05-22", "US")
    assert observation_start is not None
    option = _deal(
        deal_id="partial-option-no-v2",
        symbol="TIGR",
        contracts=2,
        price=0.0,
        strike=6.0,
        expiration_ymd="2026-05-22",
        currency="USD",
        trade_time_ms=observation_start + 1_000,
        raw_payload={
            "deal_id": "partial-option-no-v2",
            "code": "US.TIGR260522P6000",
        },
    )
    assert resolve_trade_deal(
        option,
        repo=repo,
        state={},
        apply_changes=True,
    ).status == "applied"

    partial = resolve_trade_deal(
        _deal(
            deal_id="partial-stock-no-v2",
            order_id="order-partial-stock-no-v2",
            symbol="TIGR",
            option_type=None,
            side="buy",
            position_effect=None,
            contracts=100,
            price=6.0,
            strike=None,
            multiplier=None,
            expiration_ymd=None,
            currency="USD",
            trade_time_ms=observation_start + 2_000,
            raw_payload={"deal_id": "partial-stock-no-v2", "code": "US.TIGR"},
        ),
        repo=repo,
        state={},
        apply_changes=True,
    )

    assert partial.status == "unresolved"
    assert partial.reason == "pending_close_settlement_not_exact"
    assert repo.get_record_fields(lot_id)["contracts_open"] == 0
    cases = repo.list_trade_lifecycle_cases()
    assert len(cases) == 1
    assert cases[0]["status"] == "needs_review"


def test_lifecycle_evidence_identity_is_scoped_by_broker_account(tmp_path) -> None:
    from src.application.trades.lifecycle import _evidence_from_deal

    repo = ledger_repository.SQLiteOptionPositionsRepository(
        tmp_path / "option_positions.sqlite3"
    )
    lx = _evidence_from_deal(
        _deal(deal_id="same-id"),
        evidence_type="option_zero_price_close",
        case_id=None,
    )
    sy = _evidence_from_deal(
        _deal(
            deal_id="same-id",
            internal_account="sy",
            futu_account_id="REAL_2",
        ),
        evidence_type="option_zero_price_close",
        case_id=None,
    )
    repo.upsert_trade_lifecycle_evidence(lx)
    repo.upsert_trade_lifecycle_evidence(sy)

    rows = repo.list_trade_lifecycle_evidence()
    assert {item["source_event_id"] for item in rows} == {
        "futu:lx:REAL_1:same-id",
        "futu:sy:REAL_2:same-id",
    }
    assert len({item["evidence_id"] for item in rows}) == 2


def test_resolve_trade_lifecycle_option_first_ignores_pre_expiration_stock_trade(tmp_path) -> None:
    repo = _open_lot(tmp_path)
    option_result = resolve_trade_deal(
        _deal(
            deal_id="option-leg-pre-exp-stock",
            symbol="TIGR",
            contracts=10,
            price=0.0,
            strike=6.0,
            expiration_ymd="2026-05-22",
            currency="USD",
            trade_time_ms=1779468493916,
            raw_payload={"deal_id": "option-leg-pre-exp-stock", "code": "US.TIGR260522P6000"},
        ),
        repo=repo,
        state={},
        apply_changes=True,
    )
    assert option_result.status == "applied"

    stock_result = resolve_trade_deal(
        _deal(
            deal_id="pre-expiration-stock-leg",
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
            trade_time_ms=1779321600000,
            raw_payload={"deal_id": "pre-expiration-stock-leg", "code": "US.TIGR"},
        ),
        repo=repo,
        state={},
        apply_changes=True,
    )

    assert stock_result.status == "skipped"
    assert stock_result.reason == "not_option_deal"
    assert [item for item in repo.list_trade_events() if item.get("event_type") == "assignment"] == []
    assert repo.list_trade_lifecycle_cases()[0]["status"] == "ledger_written"


def test_resolve_trade_lifecycle_option_leg_ignores_pre_expiration_stock_evidence(tmp_path) -> None:
    repo = _open_lot(tmp_path)
    repo.upsert_trade_lifecycle_evidence(
        {
            "evidence_id": "pre_expiration_stock_evidence",
            "case_id": None,
            "source_type": "test",
            "source_event_id": "pre-expiration-stock-evidence",
            "evidence_type": "stock_settlement_leg",
            "account": "lx",
            "symbol": "TIGR",
            "side": "buy",
            "trade_time_ms": 1779321600000,
            "stock_qty": 1000,
            "stock_price": 6.0,
            "raw": {"deal_id": "pre-expiration-stock-evidence", "code": "US.TIGR"},
        }
    )

    option_result = resolve_trade_deal(
        _deal(
            deal_id="option-leg-ignores-old-stock",
            symbol="TIGR",
            contracts=10,
            price=0.0,
            strike=6.0,
            expiration_ymd="2026-05-22",
            currency="USD",
            trade_time_ms=1779468493916,
            raw_payload={"deal_id": "option-leg-ignores-old-stock", "code": "US.TIGR260522P6000"},
        ),
        repo=repo,
        state={},
        apply_changes=True,
    )

    assert option_result.status == "applied"
    assert option_result.reason == "close_reason_pending"
    assert [item for item in repo.list_trade_events() if item.get("event_type") == "assignment"] == []
    assert repo.list_trade_lifecycle_cases()[0]["status"] == "ledger_written"


def test_resolve_trade_lifecycle_duplicate_option_leg_after_assignment_is_idempotent(tmp_path) -> None:
    repo = _open_lot(tmp_path)
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

    assert resolve_trade_deal(option_deal, repo=repo, state={}, apply_changes=True).status == "applied"
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
    assert duplicate.reason == "lifecycle_already_written_v2"
    assert duplicate.action == "assignment"
    assert len([item for item in repo.list_trade_events() if item.get("event_type") == "assignment"]) == 1
    cases = repo.list_trade_lifecycle_cases()
    assert cases[0]["status"] == "ledger_written"


def test_resolve_trade_lifecycle_long_call_exercise_records_exercise(tmp_path) -> None:
    repo = _open_lot(
        tmp_path,
        symbol="AAPL",
        option_type="call",
        side="long",
        contracts=2,
        strike=200.0,
        premium_per_share=1.5,
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
    assert option_result.status == "applied"

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
    exercise_events = [item for item in repo.list_trade_events() if item.get("event_type") == "exercise"]
    assert len(exercise_events) == 1
    assert exercise_events[0]["raw_payload"]["record_id"] == lot_id
    assert exercise_events[0]["raw_payload"]["stock_settlement"]["shares"] == 200
    assert repo.get_record_fields(lot_id)["contracts_open"] == 0
    assert _lot_close_type(repo, lot_id) == "exercise"


def test_resolve_trade_lifecycle_stock_first_then_long_put_exercise_records_exercise(tmp_path) -> None:
    repo = _open_lot(tmp_path, symbol="AAPL", side="long", contracts=1, strike=180.0, premium_per_share=1.5)

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
    repo = _open_lot(tmp_path)

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


def test_resolve_trade_lifecycle_late_assignment_does_not_adopt_unbound_expire_close(
    tmp_path,
) -> None:
    repo = _open_lot(tmp_path)
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
            raw_payload={
                "deal_id": "late-option-leg",
                "code": "US.TIGR260522P6000",
            },
        ),
        repo=repo,
        state={},
        apply_changes=True,
    )

    assert option_result.status == "unresolved"
    assert option_result.reason == "lifecycle_close_target_not_found"
    assert repo.list_trade_lifecycle_cases() == []
    assert [
        item
        for item in repo.list_trade_events()
        if item.get("event_type") == "assignment"
    ] == []


def test_stock_evidence_same_key_economic_drift_fails_closed(
    tmp_path,
) -> None:

    repo = _open_lot(tmp_path, contracts=1)
    first = _deal(
        deal_id="drift-stock-1",
        symbol="TIGR",
        option_type=None,
        side="buy",
        position_effect=None,
        contracts=100,
        price=6.0,
        strike=None,
        multiplier=None,
        expiration_ymd=None,
        currency="USD",
        trade_time_ms=1779468400000,
        raw_payload={
            "deal_id": "drift-stock-1",
            "code": "US.TIGR",
        },
    )
    assert resolve_trade_deal(
        first,
        repo=repo,
        state={},
        apply_changes=True,
    ).status == "unresolved"

    drifted = resolve_trade_deal(
        replace(first, price=6.01),
        repo=repo,
        state={},
        apply_changes=True,
    )

    assert drifted.status == "unresolved"
    assert drifted.reason == "broker_evidence_economic_conflict"
    stored = repo.list_trade_lifecycle_evidence()[0]
    assert stored["stock_price"] == 6.0


def test_processed_lifecycle_replay_audits_economic_hash() -> None:
    original = _deal(
        deal_id="processed-stock-1",
        symbol="NVDA",
        option_type=None,
        side="buy",
        position_effect=None,
        contracts=100,
        price=100.0,
        strike=None,
        multiplier=None,
        expiration_ymd=None,
        currency="USD",
        trade_time_ms=1_800_000_000_000,
    )
    source_key = "futu:lx:REAL_1:processed-stock-1"
    original_hash = lifecycle_deal_economic_hash(original)
    assert original_hash

    result = resolve_trade_deal(
        replace(original, contracts=200),
        repo=FakeRepo([]),
        state={
            "processed_deal_ids": {
                source_key: {
                    "status": "applied",
                    "account": "lx",
                    "economic_payload_hash": original_hash,
                }
            }
        },
        apply_changes=True,
    )

    assert result.status == "failed"
    assert result.reason == "broker_deal_economic_conflict"


def test_push_stock_after_expire_close_reaches_conflict_writer(
    tmp_path,
) -> None:

    repo = _open_lot(tmp_path, contracts=1)
    lot_id = repo.list_position_lots()[0]["record_id"]
    option_time_ms = 1779468493916
    option = _deal(
        deal_id="late-conflict-option",
        symbol="TIGR",
        contracts=1,
        price=0.0,
        strike=6.0,
        expiration_ymd="2026-05-22",
        currency="USD",
        trade_time_ms=option_time_ms,
        raw_payload={
            "deal_id": "late-conflict-option",
            "code": "US.TIGR260522P6000",
        },
    )
    assert resolve_trade_deal(
        option,
        repo=repo,
        state={},
        apply_changes=True,
    ).status == "applied"
    case_id = repo.list_trade_lifecycle_cases()[0]["case_id"]
    expiry = reconcile_lifecycle_evidence(
        repo,
        evidence={
            "evidence_id": "late-conflict-expiry",
            "source_type": "broker_settlement_observation",
            "source_event_id": "observation:late-conflict",
            "evidence_type": "expire_close",
            "account": "lx",
            "symbol": "TIGR",
            "option_type": "put",
            "position_side": "short",
            "strike": 6,
            "expiration_ymd": "2026-05-22",
            "contracts": 1,
            "event_time_ms": option_time_ms + 1,
            "target_lot_id": lot_id,
            "pending_close_anchor_evidence_id": repo.list_trade_lifecycle_evidence(case_id=case_id)[0]["evidence_id"],
        },
        case_id=case_id,
        apply_changes=True,
        now_ms=option_time_ms + 1,
    )
    assert expiry.status == "applied"

    result = resolve_trade_deal(
        _deal(
            deal_id="late-conflict-stock",
            symbol="TIGR",
            option_type=None,
            side="buy",
            position_effect=None,
            contracts=100,
            price=6.0,
            strike=None,
            multiplier=None,
            expiration_ymd=None,
            currency="USD",
            trade_time_ms=option_time_ms + 2,
            raw_payload={
                "deal_id": "late-conflict-stock",
                "code": "US.TIGR",
            },
        ),
        repo=repo,
        state={},
        apply_changes=True,
    )

    assert result.status == "unresolved"
    assert result.reason == (
        "late_settlement_conflicts_with_expire_close"
    )


def test_option_anchor_cannot_rebind_case_to_other_futu_account(
    tmp_path,
) -> None:

    repo = _open_lot(tmp_path, contracts=1)
    first = _deal(
        deal_id="account-one-option",
        symbol="TIGR",
        contracts=1,
        price=0.0,
        strike=6.0,
        expiration_ymd="2026-05-22",
        currency="USD",
        trade_time_ms=1779468493916,
        raw_payload={
            "deal_id": "account-one-option",
            "code": "US.TIGR260522P6000",
        },
    )
    assert resolve_trade_deal(
        first,
        repo=repo,
        state={},
        apply_changes=True,
    ).status == "applied"

    second = resolve_trade_deal(
        replace(
            first,
            futu_account_id="REAL_2",
            deal_id="account-two-option",
            order_id="account-two-order",
            raw_payload={
                "deal_id": "account-two-option",
                "code": "US.TIGR260522P6000",
            },
        ),
        repo=repo,
        state={},
        apply_changes=True,
    )

    assert second.status == "unresolved"
    assert second.reason == "lifecycle_close_target_not_found"
    assert len(repo.list_trade_lifecycle_evidence()) == 1


def test_resolve_trade_close_retry_failed_keeps_zero_price_option_leg_pending(tmp_path) -> None:
    repo = _open_lot(tmp_path)
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
        state={"failed_deal_ids": {"5646137975909129735": {"status": "failed", "account": "lx", "reason": "exception:LedgerPreflightError"}}},
        apply_changes=True,
        retry_failed_deal=True,
    )

    assert result.status == "applied"
    assert result.reason == "close_reason_pending"
    close_events = [item for item in repo.list_trade_events() if item["position_effect"] == "close"]
    assert len(close_events) == 1
    assert repo.get_record_fields(lot_id)["contracts_open"] == 0


def test_resolve_trade_close_rejects_missing_trade_time_before_write() -> None:
    repo = FakeRepo([_record("rec1", 100, 1)])

    result = resolve_trade_deal(_deal(contracts=1, trade_time_ms=None), repo=repo, state={}, apply_changes=True)

    assert result.status == "unresolved"
    assert result.reason == "missing_required_fields:trade_time_ms"


def test_resolve_trade_close_reports_failed_when_post_write_projection_does_not_close_lot(tmp_path) -> None:
    repo = _open_lot(
        tmp_path,
        symbol="0700.HK",
        contracts=2,
        currency="HKD",
        strike=480.0,
        expiration_ymd="2026-04-29",
        premium_per_share=3.93,
        opened_at_ms=1000,
    )
    lot_id = repo.list_position_lots()[0]["record_id"]

    def _persist_bad_zero_time_close(repo, deal):  # type: ignore[no-untyped-def]
        lot_id = str((deal.raw_payload or {}).get("record_id") or "")
        event = TradeEvent(
            event_id=f"{deal.deal_id}:close:{lot_id}",
            event_type="close",
            event_time_ms=0,
            contract_key=ContractKey.from_values(
                broker="富途",
                account=deal.internal_account,
                underlying_symbol=deal.symbol,
                option_type=deal.option_type,
                strike=deal.strike,
                expiration_ymd=deal.expiration_ymd,
                        ),
            contracts=int(deal.contracts or 0),
            price=float(deal.price or 0),
            currency=deal.currency,
            source="opend_push",
            multiplier=float(deal.multiplier or 100),
            target_lot_id=lot_id,
            raw_payload={"record_id": lot_id, "target_lot_id": lot_id},
        )
        return persist_trade_event_object(repo, event)

    with pytest.raises(ValueError, match="event_time_must_be_positive"):
        resolve_trade_deal(
            _deal(contracts=2, trade_time_ms=5000),
            repo=repo,
            state={},
            apply_changes=True,
            persist_trade_event_fn=_persist_bad_zero_time_close,
        )
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

    assert [(m.lot_id, m.contracts_to_close) for m in matches] == [("rec1", 1), ("rec2", 2)]


def test_load_close_candidate_records_prefers_position_lots_projection() -> None:
    class _PrimaryRepo:
        def list_position_lots(self) -> list[dict]:
            return [_record("lot1", 100, 2)]

    class _Repo(FakeRepo):
        primary_repo = _PrimaryRepo()

    repo = _Repo([_record("rec1", 100, 1)])

    rows = load_close_candidate_records(repo)

    assert [row["record_id"] for row in rows] == ["lot1"]


def test_unknown_effect_cannot_close_a_lot_opened_after_the_fill():
    row = _record("future-lot", opened_at=9999999999999, contracts_open=1)
    result = resolve_trade_deal(_deal(position_effect=None, contracts=1), repo=FakeRepo([row]), state={}, apply_changes=False)
    assert result.status == "unresolved"
    assert result.reason == "unknown_position_effect:later_or_same_time_ledger_event"
    assert result.operations == []

@pytest.mark.parametrize("first_leg", ["stock", "option"])
def test_lifecycle_three_hk_put_lots_match_one_1500_share_settlement(tmp_path, first_leg):
    repo = ledger_repository.SQLiteOptionPositionsRepository(
        tmp_path / "option_positions.sqlite3"
    )
    for index in range(3):
        _persist_lot(
            repo,
            symbol="3690.HK",
            contracts=1,
            strike=77.5,
            expiration_ymd="2026-09-29",
            currency="HKD",
            multiplier=500,
            opened_at_ms=1790000000000 + index,
        )
    lot_ids = [row["record_id"] for row in repo.list_position_lots()]
    assert len(lot_ids) == 3
    legs = {
        "option": _deal(
            deal_id="meituan-option-3",
            symbol="3690.HK",
            contracts=3,
            price=0,
            strike=77.5,
            multiplier=500,
            expiration_ymd="2026-09-29",
            currency="HKD",
            trade_time_ms=1790683024000,
            raw_payload={"deal_id": "meituan-option-3", "code": "HK.03690"},
        ),
        "stock": _deal(
            deal_id="meituan-stock-1500",
            symbol="3690.HK",
            option_type=None,
            side="buy",
            position_effect=None,
            contracts=1500,
            price=77.5,
            strike=None,
            multiplier=None,
            expiration_ymd=None,
            currency="HKD",
            trade_time_ms=1790683025000,
            raw_payload={"deal_id": "meituan-stock-1500", "code": "HK.03690"},
        ),
    }
    other_leg = "option" if first_leg == "stock" else "stock"
    first = resolve_trade_deal(legs[first_leg], repo=repo, state={}, apply_changes=True)
    assert first.status == ("applied" if first_leg == "option" else "unresolved")
    second = resolve_trade_deal(legs[other_leg], repo=repo, state={}, apply_changes=True)
    assert second.status == "applied", second.to_dict()
    assert second.action == "assignment"
    assert all(repo.get_record_fields(lot_id)["contracts_open"] == 0 for lot_id in lot_ids)
    assignments = [row for row in repo.list_trade_events() if row.get("event_type") == "assignment"]
    assert len(assignments) == 3


def test_final_lifecycle_case_cannot_claim_stock_after_its_deadline():
    old_case = {
        "status": "ledger_written",
        "account": "lx",
        "symbol": "3690.HK",
        "option_type": "put",
        "position_side": "short",
        "strike": 80,
        "contracts": 1,
        "multiplier": 500,
        "futu_account_id": "REAL_1",
        "observation_start_ms": 1770000000000,
        "settlement_deadline_ms": 1771000000000,
        "event_time_ms": 1770999999000,
    }
    stock = {
        "side": "buy",
        "futu_account_id": "REAL_1",
        "stock_qty": 500,
        "stock_price": 80,
        "trade_time_ms": 1790683025000,
    }
    assert not _stock_matches_lifecycle_close(old_case, stock)
    assert not _stock_matches_lifecycle_close(
        {**old_case, "event_time_ms": stock["trade_time_ms"]}, stock
    )


def test_option_anchor_does_not_choose_between_two_waiting_stock_sources(tmp_path):
    repo = _open_lot(
        tmp_path,
        symbol="3690.HK",
        contracts=1,
        strike=80,
        expiration_ymd="2026-09-29",
        currency="HKD",
        multiplier=500,
        opened_at_ms=1790000000000,
    )
    for index in range(2):
        result = resolve_trade_deal(
            _deal(
                deal_id=f"stock-candidate-{index}",
                symbol="3690.HK",
                option_type=None,
                side="buy",
                position_effect=None,
                contracts=500,
                price=80,
                strike=None,
                multiplier=None,
                expiration_ymd=None,
                currency="HKD",
                trade_time_ms=1790683025000 + index,
                raw_payload={"deal_id": f"stock-candidate-{index}", "code": "HK.03690"},
            ),
            repo=repo,
            state={},
            apply_changes=True,
        )
        assert result.status == "unresolved"
    option = resolve_trade_deal(
        _deal(
            deal_id="option-after-two-stocks",
            symbol="3690.HK",
            contracts=1,
            price=0,
            strike=80,
            multiplier=500,
            expiration_ymd="2026-09-29",
            currency="HKD",
            trade_time_ms=1790683024000,
            raw_payload={"deal_id": "option-after-two-stocks", "code": "HK.03690"},
        ),
        repo=repo,
        state={},
        apply_changes=True,
    )
    assert option.status == "applied"
    assert not [row for row in repo.list_trade_events() if row.get("event_type") == "assignment"]
    retry = resolve_trade_deal(
        _deal(
            deal_id="stock-candidate-0", symbol="3690.HK", option_type=None,
            side="buy", position_effect=None, contracts=500, price=80,
            strike=None, multiplier=None, expiration_ymd=None, currency="HKD",
            trade_time_ms=1790683025000,
            raw_payload={"deal_id": "stock-candidate-0", "code": "HK.03690"},
        ), repo=repo, state={}, apply_changes=True,
    )
    assert (retry.status, retry.reason) == ("unresolved", "ambiguous_stock_settlement_evidence")
    assert not [row for row in repo.list_trade_events() if row.get("event_type") == "assignment"]
    assert not [row for row in repo.list_trade_lifecycle_notifications()
                if row["transition_type"] == "resolution_confirmed" and row["status"] != "suppressed"]
    from src.application.trades.lifecycle import _write_v2_lifecycle_close_from_case
    case = repo.list_trade_lifecycle_cases()[0]
    evidences = repo.list_trade_lifecycle_evidence()
    option_evidence = next(row for row in evidences if row["evidence_type"] == "option_zero_price_close")
    stock_evidence = next(row for row in evidences if row.get("raw", {}).get("deal_id") == "stock-candidate-0")
    with pytest.raises(ValueError, match="ambiguous_stock_settlement_evidence"):
        _write_v2_lifecycle_close_from_case(
            repo, case=case, decision_type="assignment",
            option_evidence=option_evidence, stock_evidence=stock_evidence,
            event_time_ms=stock_evidence["trade_time_ms"],
        )


def test_inbox_retry_cannot_choose_between_two_waiting_stock_sources(tmp_path):
    from src.application.trades.auto_intake import _process_payload

    repo = _open_lot(
        tmp_path, symbol="3690.HK", contracts=1, strike=80,
        expiration_ymd="2026-09-29", currency="HKD", multiplier=500,
        opened_at_ms=1790000000000,
    )
    kwargs = dict(
        repo=repo, state_path=tmp_path / "state.json", audit_path=tmp_path / "audit.jsonl",
        account_mapping={"REAL_1": "lx"}, futu_account_ids=["REAL_1"],
        apply_changes=True, host="127.0.0.1", port=11111,
        source="push", allow_external_lookup=False,
    )
    stocks = [
        {
            "deal_id": f"raw-stock-{index}", "code": "HK.03690",
            "futu_account_id": "REAL_1", "trd_side": "BUY", "qty": 500,
            "price": 80, "trade_time_ms": 1790683025000 + index,
            "external_id_namespace": "futu.deal", "environment": "REAL", "status": "OK",
            "_trade_intake_source": {"account": "lx", "futu_account_id": "REAL_1"},
        }
        for index in range(2)
    ]
    for stock in stocks:
        assert _process_payload(stock, **kwargs)["status"] == "unresolved"
    option = _deal(
        deal_id="option-after-inbox-stocks", symbol="3690.HK", contracts=1,
        price=0, strike=80, multiplier=500, expiration_ymd="2026-09-29",
        currency="HKD", trade_time_ms=1790683024000,
        raw_payload={"deal_id": "option-after-inbox-stocks", "code": "HK.03690"},
    )
    assert resolve_trade_deal(option, repo=repo, state={}, apply_changes=True).reason == "close_reason_pending"
    retry = _process_payload(stocks[0], retry_failed_deal=True, **kwargs)
    assert (retry["status"], retry["reason"]) == ("unresolved", "ambiguous_stock_settlement_evidence")
    assert not [row for row in repo.list_trade_events() if row["event_type"] == "assignment"]
    assert not [row for row in repo.list_trade_lifecycle_notifications()
                if row["transition_type"] == "resolution_confirmed" and row["status"] != "suppressed"]


def test_option_retry_keeps_partial_stock_pending_without_reassigning(tmp_path):
    from src.application.trades.lifecycle import _evidence_from_deal

    repo = _open_lot(tmp_path, contracts=2)
    start = expiration_observation_start_ms("2026-05-22", "US")
    assert start is not None
    option = _deal(
        deal_id="partial-option-retry", symbol="TIGR", contracts=2, price=0,
        strike=6, expiration_ymd="2026-05-22", currency="USD",
        trade_time_ms=start + 1000,
        raw_payload={"deal_id": "partial-option-retry", "code": "US.TIGR260522P6000"},
    )
    assert resolve_trade_deal(option, repo=repo, state={}, apply_changes=True).status == "applied"

    def stock(index):
        return _deal(
            deal_id=f"partial-stock-retry-{index}", symbol="TIGR",
            option_type=None, side="buy", position_effect=None,
            contracts=100, price=6, strike=None, multiplier=None,
            expiration_ymd=None, currency="USD", trade_time_ms=start + 2000 + index,
            raw_payload={"deal_id": f"partial-stock-retry-{index}", "code": "US.TIGR"},
        )

    assert resolve_trade_deal(stock(0), repo=repo, state={}, apply_changes=True).status == "unresolved"
    assert repo.insert_trade_lifecycle_evidence_once(
        _evidence_from_deal(stock(1), evidence_type="stock_settlement_leg", case_id=None)
    )
    retry = resolve_trade_deal(option, repo=repo, state={}, apply_changes=True)
    assert retry.status == "skipped"
    assert not [row for row in repo.list_trade_events() if row["event_type"] == "assignment"]
    assert resolve_trade_deal(option, repo=repo, state={}, apply_changes=True).status == "skipped"


def test_broker_assigned_stock_sale_keeps_assignment_physical_account(tmp_path):
    repo = _open_lot(
        tmp_path,
        symbol="3690.HK",
        contracts=1,
        strike=80,
        expiration_ymd="2026-09-29",
        currency="HKD",
        multiplier=500,
        opened_at_ms=1790000000000,
    )
    option = _deal(
        deal_id="physical-option",
        symbol="3690.HK",
        contracts=1,
        price=0,
        strike=80,
        multiplier=500,
        expiration_ymd="2026-09-29",
        currency="HKD",
        trade_time_ms=1790683024000,
        raw_payload={"deal_id": "physical-option", "code": "HK.03690"},
    )
    stock = _deal(
        deal_id="physical-stock",
        symbol="3690.HK",
        option_type=None,
        side="buy",
        position_effect=None,
        contracts=500,
        price=80,
        strike=None,
        multiplier=None,
        expiration_ymd=None,
        currency="HKD",
        trade_time_ms=1790683025000,
        raw_payload={"deal_id": "physical-stock", "code": "HK.03690"},
    )
    assert resolve_trade_deal(option, repo=repo, state={}, apply_changes=True).status == "applied"
    assert resolve_trade_deal(stock, repo=repo, state={}, apply_changes=True).status == "applied"
    sale = _deal(
        deal_id="wrong-physical-sale",
        futu_account_id="REAL_2",
        symbol="3690.HK",
        option_type=None,
        side="sell",
        position_effect=None,
        contracts=500,
        price=85,
        strike=None,
        multiplier=None,
        expiration_ymd=None,
        currency="HKD",
        trade_time_ms=1790683125000,
        raw_payload={"deal_id": "wrong-physical-sale", "code": "HK.03690"},
    )
    result = resolve_trade_deal(sale, repo=repo, state={}, apply_changes=True)
    assert result.status == "unresolved"
    assert result.reason == "assigned_stock_sale_physical_account_unverified"
    assert repo.list_assigned_stock_events() == []


def test_historical_stock_recovery_suppresses_new_lifecycle_outbox(tmp_path):
    repo = _open_lot(
        tmp_path,
        symbol="3690.HK", contracts=1, strike=80,
        expiration_ymd="2026-09-29", currency="HKD", multiplier=500,
        opened_at_ms=1790000000000,
    )
    option = _deal(
        deal_id="suppressed-option", symbol="3690.HK", contracts=1,
        price=0, strike=80, multiplier=500, expiration_ymd="2026-09-29",
        currency="HKD", trade_time_ms=1790683024000,
        raw_payload={"deal_id": "suppressed-option", "code": "HK.03690"},
    )
    stock = _deal(
        deal_id="suppressed-stock", symbol="3690.HK", option_type=None,
        side="buy", position_effect=None, contracts=500, price=80,
        strike=None, multiplier=None, expiration_ymd=None, currency="HKD",
        trade_time_ms=1790683025000,
        raw_payload={"deal_id": "suppressed-stock", "code": "HK.03690"},
    )
    assert resolve_trade_deal(option, repo=repo, state={}, apply_changes=True).status == "applied"
    before_outbox = {row["outbox_id"] for row in repo.list_trade_lifecycle_notifications()}
    result = resolve_trade_deal(
        stock, repo=repo, state={}, apply_changes=True,
        notification_status="suppressed",
    )
    assert result.status == "applied"
    outbox = repo.list_trade_lifecycle_notifications()
    assert outbox
    assert all(row["status"] == "suppressed" for row in outbox
               if row["outbox_id"] not in before_outbox)


def test_skipped_stock_source_recovery_writes_assignment_once_without_delivery(tmp_path, monkeypatch):
    import src.application.trades.auto_intake as auto_intake
    from src.application.trades.auto_intake import _process_payload, _skipped_recovery_snapshot
    from src.application.trades.inbox import (read_trade_payload, resume_skipped_trade_payload,
                                              list_retryable_trade_payloads,
                                              list_trade_receipt_recovery_rows)
    from src.application.trades.inbox_authority import resolve_execution_inbox_path

    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    raw_stock = {"deal_id": "recovered-stock", "code": "HK.03690",
                 "futu_account_id": "REAL_1", "trd_side": "BUY", "qty": 1500,
                 "price": 77.5, "trade_time_ms": 1790683025000,
                 "external_id_namespace": "futu.deal", "environment": "REAL", "status": "OK",
                 "_trade_intake_source": {"account": "lx", "futu_account_id": "REAL_1"}}
    state_path = tmp_path / "state.json"
    kwargs = dict(repo=repo, state_path=state_path, audit_path=tmp_path / "audit.jsonl",
                  account_mapping={"REAL_1": "lx"}, futu_account_ids=["REAL_1"],
                  apply_changes=True, host="127.0.0.1", port=11111,
                  source="push", allow_external_lookup=False)
    skipped = _process_payload(raw_stock, **kwargs)
    assert (skipped["status"], skipped["reason"]) == ("skipped", "not_option_deal")
    for index in range(3):
        _persist_lot(
            repo, symbol="3690.HK", contracts=1, strike=77.5,
            expiration_ymd="2026-09-29", currency="HKD", multiplier=500,
            opened_at_ms=1790000000000 + index,
        )
    option = _deal(
        deal_id="recovered-option", symbol="3690.HK", contracts=3,
        price=0, strike=77.5, multiplier=500, expiration_ymd="2026-09-29",
        currency="HKD", trade_time_ms=1790683024000,
        raw_payload={"deal_id": "recovered-option", "code": "HK.03690"},
    )
    assert resolve_trade_deal(option, repo=repo, state={}, apply_changes=True).status == "applied"
    before_outbox = {row["outbox_id"] for row in repo.list_trade_lifecycle_notifications()}
    inbox = resolve_execution_inbox_path(repo, tmp_path / "unused.sqlite3")
    preview = _skipped_recovery_snapshot(
        inbox_path=inbox, inbox_id=skipped["inbox_id"], state_path=state_path,
        ledger_path=repo.db_path,
    )
    assert resume_skipped_trade_payload(
        inbox, inbox_id=skipped["inbox_id"], operator="operator",
        economic_payload_hash=preview["economic_payload_hash"], repo=repo,
    )
    assert list_retryable_trade_payloads(inbox, retry_delay_sec=0) == []
    assert list_trade_receipt_recovery_rows(inbox, account_ids=["REAL_1"]) == []
    assert _skipped_recovery_snapshot(
        inbox_path=inbox, inbox_id=skipped["inbox_id"], state_path=state_path,
        ledger_path=repo.db_path,
    )["recovery_hash"] != preview["recovery_hash"]
    original_update = auto_intake.update_trade_intake_state_entries
    class SimulatedCrash(BaseException):
        pass
    monkeypatch.setattr(auto_intake, "update_trade_intake_state_entries",
                        lambda *args, **kwargs: (_ for _ in ()).throw(SimulatedCrash()))
    with pytest.raises(SimulatedCrash):
        _process_payload(raw_stock, recover_skipped=True, **kwargs)
    monkeypatch.setattr(auto_intake, "update_trade_intake_state_entries", original_update)
    assert list_retryable_trade_payloads(inbox, retry_delay_sec=0) == []
    assert list_trade_receipt_recovery_rows(inbox, account_ids=["REAL_1"]) == []
    assert len([row for row in repo.list_trade_events() if row.get("event_type") == "assignment"]) == 3
    interrupted = _skipped_recovery_snapshot(
        inbox_path=inbox, inbox_id=skipped["inbox_id"], state_path=state_path,
        ledger_path=repo.db_path,
    )
    assert resume_skipped_trade_payload(
        inbox, inbox_id=skipped["inbox_id"], operator="operator",
        economic_payload_hash=interrupted["economic_payload_hash"], repo=repo,
    )
    recovered = _process_payload(raw_stock, recover_skipped=True, **kwargs)
    assert recovered["reason"] == "lifecycle_already_written_v2", recovered
    assert recovered["receipt_notification_owner"] == "lifecycle_outbox"
    assert recovered["receipt_suppression_reason"] == "historical_recovery"
    assert list_trade_receipt_recovery_rows(inbox, account_ids=["REAL_1"]) == []
    assert all(row["status"] == "suppressed" for row in repo.list_trade_lifecycle_notifications()
               if row["outbox_id"] not in before_outbox)
    assert len([row for row in repo.list_trade_events() if row.get("event_type") == "assignment"]) == 3
    assert read_trade_payload(inbox, inbox_id=skipped["inbox_id"])["status"] == "handled"
    duplicate = _process_payload(raw_stock, recover_skipped=True, **kwargs)
    assert (duplicate["status"], duplicate["reason"]) == ("skipped", "duplicate")
    assert len([row for row in repo.list_trade_events() if row.get("event_type") == "assignment"]) == 3


def test_manual_required_stock_ambiguity_uses_guarded_recovery_without_delivery(tmp_path):
    import json
    from src.application.trades.auto_intake import _process_payload, _skipped_recovery_snapshot
    from src.application.trades.inbox import _connect, read_trade_payload, resume_skipped_trade_payload
    from src.application.trades.inbox_authority import resolve_execution_inbox_path

    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    stock = {"deal_id": "old-ambiguous-stock", "code": "HK.03690",
             "futu_account_id": "REAL_1", "trd_side": "BUY", "qty": 500,
             "price": 80, "trade_time_ms": 1790682392448,
             "external_id_namespace": "futu.deal", "environment": "REAL", "status": "OK",
             "_trade_intake_source": {"account": "lx", "futu_account_id": "REAL_1"}}
    state_path = tmp_path / "state.json"
    kwargs = dict(repo=repo, state_path=state_path, audit_path=tmp_path / "audit.jsonl",
                  account_mapping={"REAL_1": "lx"}, futu_account_ids=["REAL_1"],
                  apply_changes=True, host="127.0.0.1", port=11111,
                  source="backfill", allow_external_lookup=False)
    original = _process_payload(stock, **kwargs)
    _persist_lot(repo, symbol="3690.HK", contracts=1, strike=80,
                 expiration_ymd="2026-09-29", currency="HKD", multiplier=500,
                 opened_at_ms=1790000000000)
    inbox = resolve_execution_inbox_path(repo, tmp_path / "unused.sqlite3")
    saved = read_trade_payload(inbox, inbox_id=original["inbox_id"])
    key = saved["broker_deal_key"]
    state = json.loads(state_path.read_text())
    entry = (state.get("unresolved_deal_ids") or {}).get(key)
    if entry is None:
        entry = state["processed_deal_ids"].pop(key)
    entry.update(status="unresolved", reason="ambiguous_lifecycle_case_match", retryable=False)
    state.setdefault("unresolved_deal_ids", {})[key] = entry
    state_path.write_text(json.dumps(state))
    with _connect(inbox) as conn:
        conn.execute("""UPDATE trade_inbox SET status='handled', result_status='unresolved',
            result_reason='ambiguous_lifecycle_case_match',
            result_json=json_set(result_json, '$.status', 'unresolved',
                                 '$.reason', 'ambiguous_lifecycle_case_match',
                                 '$.receipt_kind', 'manual_required') WHERE inbox_id=?""",
                     (original["inbox_id"],))
    option = _deal(deal_id="old-ambiguous-option", symbol="3690.HK", contracts=1,
                   price=0, strike=80, multiplier=500, expiration_ymd="2026-09-29",
                   currency="HKD", trade_time_ms=1790682391907,
                   raw_payload={"deal_id": "old-ambiguous-option", "code": "HK.MET260929P80000"})
    assert resolve_trade_deal(option, repo=repo, state={}, apply_changes=True).status == "applied"
    preview = _skipped_recovery_snapshot(
        inbox_path=inbox, inbox_id=original["inbox_id"], state_path=state_path,
        ledger_path=repo.db_path, account_mapping={"REAL_1": "lx"},
    )
    assert preview["receipt_suppressed"] and preview["portfolio_refresh_suppressed"]
    before_outbox = {row["outbox_id"] for row in repo.list_trade_lifecycle_notifications()}
    assert resume_skipped_trade_payload(
        inbox, inbox_id=original["inbox_id"], operator="operator",
        economic_payload_hash=preview["economic_payload_hash"], repo=repo,
    )
    recovered = _process_payload(stock, recover_skipped=True, **kwargs)
    assert (recovered["status"], recovered["action"]) == ("applied", "assignment")
    assert recovered["receipt_suppression_reason"] == "historical_recovery"
    new_outbox = [row for row in repo.list_trade_lifecycle_notifications()
                  if row["outbox_id"] not in before_outbox]
    assert new_outbox and all(row["status"] == "suppressed" for row in new_outbox)
    assert len([row for row in repo.list_trade_events() if row.get("event_type") == "assignment"]) == 1
    assert _process_payload(stock, recover_skipped=True, **kwargs)["reason"] == "duplicate"


@pytest.mark.parametrize("raw", [None, "", True, 0, -1, 100.5, "100.00000000000000001"])
@pytest.mark.parametrize("price", [0, 0.1])
def test_close_multiplier_invalid_preview_and_write_leave_database_unchanged(tmp_path, raw, price):
    from src.application.ledger.api import record_normalized_trade_event

    repo = _open_lot(tmp_path, multiplier=500)
    deal = _deal(symbol="TIGR", contracts=1, strike=6, expiration_ymd="2026-05-22",
                 trade_time_ms=1779468493916, currency="USD", price=price, multiplier=raw)
    with repo._connect() as conn:
        before = tuple(conn.iterdump())
    for apply in (False, True):
        result = resolve_trade_deal(deal, repo=repo, state={}, apply_changes=apply)
        assert result.status == "unresolved"
        assert "multiplier" in result.reason
        assert result.operations == []
    with pytest.raises(ValueError, match="event_multiplier_invalid"):
        record_normalized_trade_event(repo, deal)
    with repo._connect() as conn:
        assert tuple(conn.iterdump()) == before


@pytest.mark.parametrize("price", [0, 0.1])
def test_close_source_target_multiplier_conflict_blocks_preview_and_apply(tmp_path, price):
    repo = _open_lot(tmp_path, multiplier=500)
    deal = _deal(symbol="TIGR", contracts=1, strike=6, expiration_ymd="2026-05-22",
                 trade_time_ms=1779468493916, currency="USD", price=price, multiplier=100)
    with repo._connect() as conn:
        before = tuple(conn.iterdump())
    for apply in (False, True):
        result = resolve_trade_deal(deal, repo=repo, state={}, apply_changes=apply)
        assert result.status == "unresolved"
        assert result.reason == "unsupported_contract_multiplier"
    with repo._connect() as conn:
        assert tuple(conn.iterdump()) == before


@pytest.mark.parametrize("multiplier", [500, 1000])
def test_zero_close_uses_actual_multiplier_in_case_and_events(tmp_path, multiplier):
    repo = _open_lot(tmp_path, multiplier=multiplier)
    deal = _deal(symbol="TIGR", contracts=1, strike=6, expiration_ymd="2026-05-22",
                 trade_time_ms=1779468493916, currency="USD", price=0, multiplier=multiplier)
    assert resolve_trade_deal(deal, repo=repo, state={}, apply_changes=False).status == "dry_run"
    assert resolve_trade_deal(deal, repo=repo, state={}, apply_changes=True).status == "applied"
    assert repo.list_trade_lifecycle_cases()[0]["multiplier"] == multiplier
    assert all(event["multiplier"] == multiplier for event in repo.list_trade_events())
    stock = _deal(deal_id="actual-unit-stock", order_id="actual-unit-stock-order", symbol="TIGR",
                  option_type=None, side="buy", position_effect=None, contracts=multiplier,
                  price=6, strike=None, multiplier=None, expiration_ymd=None, currency="USD",
                  trade_time_ms=1779468500000, raw_payload={"deal_id": "actual-unit-stock", "code": "US.TIGR"})
    result = resolve_trade_deal(stock, repo=repo, state={}, apply_changes=True)
    assert (result.status, result.action) == ("applied", "assignment")
    assignment = next(event for event in repo.list_trade_events() if event["event_type"] == "assignment")
    assert assignment["multiplier"] == multiplier
    assert assignment["raw_payload"]["stock_settlement"]["shares"] == multiplier
    # A successful retry refreshes only projection verification timestamps.
    def durable_snapshot():
        metadata_tables = ("position_projection_heads", "position_projection_source_state")
        with repo._connect() as conn:
            metadata = tuple(tuple(tuple(row)[:-1] for row in conn.execute(f"SELECT * FROM {table}"))
                             for table in metadata_tables)
            economic = tuple(line for line in conn.iterdump()
                             if not line.startswith(tuple(f'INSERT INTO "{table}"' for table in metadata_tables)))
        return metadata, economic

    after = durable_snapshot()
    resolve_trade_deal(stock, repo=repo, state={}, apply_changes=True)
    assert durable_snapshot() == after


def test_zero_close_rechecks_multiplier_after_preview(tmp_path):
    import json

    repo = _open_lot(tmp_path, multiplier=500)
    deal = _deal(symbol="TIGR", contracts=1, strike=6, expiration_ymd="2026-05-22",
                 trade_time_ms=1779468493916, currency="USD", price=0, multiplier=500)
    assert resolve_trade_deal(deal, repo=repo, state={}, apply_changes=False).status == "dry_run"
    row = repo.list_position_lots()[0]
    fields = dict(row["fields"])
    fields.pop("multiplier")
    with repo._connect() as conn:
        conn.execute("UPDATE position_lots SET fields_json=? WHERE lot_id=?", (json.dumps(fields), row["record_id"]))
        conn.commit()
        before = tuple(conn.iterdump())
    result = resolve_trade_deal(deal, repo=repo, state={}, apply_changes=True)
    assert result.status == "unresolved" and "multiplier" in result.reason
    with repo._connect() as conn:
        assert tuple(conn.iterdump()) == before


@pytest.mark.parametrize("raw", [None, True, "100.00000000000000001"])
def test_strict_execution_multiplier_cannot_be_repaired_from_dto_or_lot(tmp_path, raw):
    from src.application.trades.normalizer import normalize_trade_deal
    from src.application.ledger.api import record_normalized_trade_event

    repo = _open_lot(tmp_path, multiplier=500)
    payload = {
        "broker_account_ref": {"broker_account_id": "account-1", "broker_id": "futu",
                               "external_account_id": "REAL_1", "environment": "REAL", "account_label": "lx"},
        "instrument_ref": {"asset_type": "option", "symbol": "TIGR", "market": "US", "currency": "USD",
                           "option_type": "put", "strike": "6", "expiration_ymd": "2026-05-22", "multiplier": raw},
        "external_id_namespace": "futu-us-deals", "external_execution_id": "strict-unit",
        "external_order_namespace": "futu-orders", "external_order_id": "strict-order",
        "side": "buy", "position_effect": "close", "quantity": "1", "price": "0.1", "currency": "USD",
        "occurred_at_utc": "2026-05-22T16:00:00Z", "evidence_refs": ["synthetic-test"],
    }
    if raw is None:
        payload["instrument_ref"].pop("multiplier")
    deal = replace(normalize_trade_deal(payload), multiplier=500)
    with repo._connect() as conn:
        before = tuple(conn.iterdump())
    for apply in (False, True):
        result = resolve_trade_deal(deal, repo=repo, state={}, apply_changes=apply)
        assert result.status == "unresolved" and result.reason == "execution_admission_failed"
        assert any("multiplier" in error for error in result.diagnostics["errors"])
        assert result.operations == []
    with pytest.raises(ValueError, match="multiplier"):
        record_normalized_trade_event(repo, deal)
    with repo._connect() as conn:
        assert tuple(conn.iterdump()) == before
