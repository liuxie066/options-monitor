from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from domain.domain.ledger import ContractKey, TradeEvent
from domain.domain.option_position_lots import EXPIRE_AUTO_CLOSE, parse_exp_to_ms
import src.application.ledger.manual_trades as ledger_manual_trades
from src.application.ledger.maintenance import auto_close_expired_positions, build_expired_close_decisions
from src.application.ledger.position_records import PositionLotRecord
import src.application.ledger.repository as ledger_repository
import src.application.ledger.writer as ledger_writer


def _seed_open_lot_event(
    repo: ledger_repository.SQLiteOptionPositionsRepository,
    *,
    record_id: str,
    account: str,
    symbol: str,
    option_type: str,
    side: str,
    contracts: int,
    currency: str,
    strike: float,
    multiplier: float,
    expiration_ymd: str,
    premium: float = 1.0,
    opened_at_ms: int = 1000,
) -> None:
    ledger_writer.persist_trade_event_object(
        repo,
        TradeEvent(
            event_id=f"seed-{record_id}",
            event_type="open",
            event_time_ms=int(opened_at_ms),
            contract_key=ContractKey.from_values(
                broker="富途",
                account=account,
                underlying_symbol=symbol,
                option_type=option_type,
                position_side=side,
                strike=float(strike),
                expiration_ymd=expiration_ymd,
            ),
            contracts=int(contracts),
            price=float(premium),
            currency=currency,
            source="test_seed_open_lot",
            multiplier=float(multiplier),
            lot_id=record_id,
            raw_payload={"source_type": "test_seed"},
        ),
    )


def _auto_close_payloads(*args, **kwargs):
    payload = auto_close_expired_positions(*args, **kwargs).to_payload()
    return payload["decisions"], payload["applied"], payload["errors"]


def test_build_expired_close_decisions_marks_expired_position() -> None:
    as_of_ms = parse_exp_to_ms("2026-04-20")
    assert as_of_ms is not None

    decisions = build_expired_close_decisions(
        [
            {
                "record_id": "rec_1",
                "position_id": "NVDA_20260417_100P_short",
                "status": "open",
                "contracts": 1,
                "contracts_open": 1,
                "expiration": parse_exp_to_ms("2026-04-17"),
                "note": "",
            }
        ],
        as_of_ms=as_of_ms,
        grace_days=1,
    )

    assert len(decisions) == 1
    assert decisions[0].to_payload()["should_close"] is True
    assert decisions[0].to_payload()["record_id"] == "rec_1"
    patch = decisions[0].to_payload()["patch"]
    assert isinstance(patch, dict)
    assert patch["contracts_open"] == 0
    assert patch["status"] == "close"
    assert patch["close_type"] == EXPIRE_AUTO_CLOSE
    assert patch["close_reason"] == "expired"


def test_build_expired_close_decisions_skips_missing_record_id() -> None:
    as_of_ms = parse_exp_to_ms("2026-04-20")
    assert as_of_ms is not None

    decisions = build_expired_close_decisions(
        [
            {
                "position_id": "missing_rid",
                "contracts": 1,
                "contracts_open": 1,
                "note": "exp=2026-04-17",
            }
        ],
        as_of_ms=as_of_ms,
        grace_days=1,
    )

    assert decisions[0].to_payload()["should_close"] is False
    assert decisions[0].to_payload()["reason"] == "missing record_id"
    assert decisions[0].to_payload()["patch"] is None


def test_build_expired_close_decisions_waits_until_expiration_plus_full_grace_day() -> None:
    exp_ms = parse_exp_to_ms("2026-05-01")
    assert exp_ms is not None

    before_threshold_ms = int(datetime(2026, 5, 1, 23, 46, tzinfo=timezone.utc).timestamp() * 1000)
    decisions = build_expired_close_decisions(
        [
            {
                "record_id": "rec_1",
                "position_id": "NVDA_20260501_100P_short",
                "status": "open",
                "contracts": 1,
                "contracts_open": 1,
                "expiration": exp_ms,
                "note": "",
            }
        ],
        as_of_ms=before_threshold_ms,
        grace_days=1,
    )

    assert decisions[0].to_payload()["should_close"] is False
    assert decisions[0].to_payload()["skip_reason"] == "grace_period_pending"
    assert decisions[0].to_payload()["eligible_after_utc"] == "2026-05-02T00:00:00+00:00"
    assert "expired but waiting grace cutoff" in decisions[0].to_payload()["reason"]


def test_build_expired_close_decisions_closes_at_expiration_plus_full_grace_day() -> None:
    exp_ms = parse_exp_to_ms("2026-05-01")
    assert exp_ms is not None

    threshold_ms = int(datetime(2026, 5, 2, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
    decisions = build_expired_close_decisions(
        [
            {
                "record_id": "rec_1",
                "position_id": "NVDA_20260501_100P_short",
                "status": "open",
                "contracts": 1,
                "contracts_open": 1,
                "expiration": exp_ms,
                "note": "",
            }
        ],
        as_of_ms=threshold_ms,
        grace_days=1,
    )

    assert decisions[0].to_payload()["should_close"] is True
    assert decisions[0].to_payload()["expiration_ymd"] == "2026-05-01"


def test_build_expired_close_decisions_uses_us_market_local_grace_cutoff() -> None:
    exp_ms = parse_exp_to_ms("2026-06-18")
    assert exp_ms is not None

    before_us_midnight_ms = int(datetime(2026, 6, 19, 1, 0, tzinfo=timezone.utc).timestamp() * 1000)
    decisions = build_expired_close_decisions(
        [
            {
                "record_id": "rec_pdd",
                "position_id": "PDD_20260618_85P_short",
                "symbol": "PDD",
                "status": "open",
                "contracts": 2,
                "contracts_open": 2,
                "expiration": exp_ms,
            }
        ],
        as_of_ms=before_us_midnight_ms,
        grace_days=1,
    )

    assert decisions[0].to_payload()["should_close"] is False
    assert decisions[0].to_payload()["skip_reason"] == "grace_period_pending"
    assert decisions[0].to_payload()["eligible_after_utc"] == "2026-06-19T04:00:00+00:00"
    assert decisions[0].to_payload()["expiration_market"] == "US"
    assert decisions[0].to_payload()["expiration_timezone"] == "America/New_York"

    at_us_midnight_ms = int(datetime(2026, 6, 19, 4, 0, tzinfo=timezone.utc).timestamp() * 1000)
    decisions = build_expired_close_decisions(
        [
            {
                "record_id": "rec_pdd",
                "position_id": "PDD_20260618_85P_short",
                "symbol": "PDD",
                "status": "open",
                "contracts": 2,
                "contracts_open": 2,
                "expiration": exp_ms,
            }
        ],
        as_of_ms=at_us_midnight_ms,
        grace_days=1,
    )

    assert decisions[0].to_payload()["should_close"] is True


def test_build_expired_close_decisions_uses_hk_market_local_grace_cutoff() -> None:
    exp_ms = parse_exp_to_ms("2026-06-18")
    assert exp_ms is not None

    beijing_morning_ms = int(datetime(2026, 6, 19, 1, 0, tzinfo=timezone.utc).timestamp() * 1000)
    decisions = build_expired_close_decisions(
        [
            {
                "record_id": "rec_0700",
                "position_id": "0700_HK_20260618_420P_short",
                "symbol": "0700.HK",
                "status": "open",
                "contracts": 1,
                "contracts_open": 1,
                "expiration": exp_ms,
            }
        ],
        as_of_ms=beijing_morning_ms,
        grace_days=1,
    )

    assert decisions[0].to_payload()["should_close"] is True


def test_build_expired_close_decisions_waits_for_short_put_assignment_when_itm() -> None:
    exp_ms = parse_exp_to_ms("2026-06-18")
    assert exp_ms is not None
    as_of_ms = int(datetime(2026, 6, 19, 4, 0, tzinfo=timezone.utc).timestamp() * 1000)

    decisions = build_expired_close_decisions(
        [
            {
                "record_id": "rec_pdd",
                "position_id": "PDD_20260618_85P_short",
                "symbol": "PDD",
                "option_type": "put",
                "side": "short",
                "strike": 85,
                "_auto_close_underlying_spot": 80,
                "status": "open",
                "contracts": 2,
                "contracts_open": 2,
                "expiration": exp_ms,
            }
        ],
        as_of_ms=as_of_ms,
        grace_days=1,
    )

    assert decisions[0].to_payload()["should_close"] is False
    assert decisions[0].to_payload()["skip_reason"] == "expiry_assignment_review_required"
    assert decisions[0].to_payload()["assignment_review"]["status"] == "itm_or_atm"
    assert decisions[0].to_payload()["assignment_review"]["spot"] == 80
    assert decisions[0].to_payload()["assignment_review"]["strike"] == 85
    assert decisions[0].to_payload()["patch"] is None


def test_build_expired_close_decisions_closes_short_put_when_otm_spot_verified() -> None:
    exp_ms = parse_exp_to_ms("2026-06-18")
    assert exp_ms is not None
    as_of_ms = int(datetime(2026, 6, 19, 4, 0, tzinfo=timezone.utc).timestamp() * 1000)

    decisions = build_expired_close_decisions(
        [
            {
                "record_id": "rec_pdd",
                "position_id": "PDD_20260618_85P_short",
                "symbol": "PDD",
                "option_type": "put",
                "side": "short",
                "strike": 85,
                "_auto_close_underlying_spot": 90,
                "status": "open",
                "contracts": 2,
                "contracts_open": 2,
                "expiration": exp_ms,
            }
        ],
        as_of_ms=as_of_ms,
        grace_days=1,
    )

    assert decisions[0].to_payload()["should_close"] is True
    assert decisions[0].to_payload()["assignment_review"]["status"] == "otm_verified"
    assert decisions[0].to_payload()["assignment_review"]["spot"] == 90


def test_build_expired_close_decisions_waits_for_short_call_assignment_when_itm() -> None:
    exp_ms = parse_exp_to_ms("2026-06-18")
    assert exp_ms is not None
    as_of_ms = int(datetime(2026, 6, 19, 4, 0, tzinfo=timezone.utc).timestamp() * 1000)

    decisions = build_expired_close_decisions(
        [
            {
                "record_id": "rec_call",
                "position_id": "AAPL_20260618_200C_short",
                "symbol": "AAPL",
                "option_type": "call",
                "side": "short",
                "strike": 200,
                "_auto_close_underlying_spot": 210,
                "status": "open",
                "contracts": 1,
                "contracts_open": 1,
                "expiration": exp_ms,
            }
        ],
        as_of_ms=as_of_ms,
        grace_days=1,
    )

    assert decisions[0].to_payload()["should_close"] is False
    assert decisions[0].to_payload()["skip_reason"] == "expiry_assignment_review_required"
    assert decisions[0].to_payload()["assignment_review"]["status"] == "itm_or_atm"


def test_build_expired_close_decisions_fail_closed_when_short_option_spot_missing() -> None:
    exp_ms = parse_exp_to_ms("2026-06-18")
    assert exp_ms is not None
    as_of_ms = int(datetime(2026, 6, 19, 4, 0, tzinfo=timezone.utc).timestamp() * 1000)

    decisions = build_expired_close_decisions(
        [
            {
                "record_id": "rec_tcom",
                "position_id": "TCOM_20260618_45P_short",
                "symbol": "TCOM",
                "option_type": "put",
                "side": "short",
                "strike": 45,
                "status": "open",
                "contracts": 1,
                "contracts_open": 1,
                "expiration": exp_ms,
            }
        ],
        as_of_ms=as_of_ms,
        grace_days=1,
    )

    assert decisions[0].to_payload()["should_close"] is False
    assert decisions[0].to_payload()["skip_reason"] == "expiry_assignment_review_required"
    assert decisions[0].to_payload()["assignment_review"]["status"] == "missing_spot"
    assert decisions[0].to_payload()["patch"] is None


def test_build_expired_close_decisions_skips_already_closed_or_zero_open() -> None:
    as_of_ms = parse_exp_to_ms("2026-05-03")
    assert as_of_ms is not None

    decisions = build_expired_close_decisions(
        [
            {
                "record_id": "rec_closed",
                "position_id": "NVDA_20260501_100P_short",
                "status": "close",
                "contracts": 1,
                "contracts_open": 0,
                "expiration": parse_exp_to_ms("2026-05-01"),
                "note": "",
            }
        ],
        as_of_ms=as_of_ms,
        grace_days=1,
    )

    assert decisions[0].to_payload()["should_close"] is False
    assert decisions[0].to_payload()["skip_reason"] == "already_closed_or_zero_open"
    assert decisions[0].to_payload()["contracts_open"] == 0
    assert decisions[0].to_payload()["patch"] is None


def test_auto_close_expired_positions_uses_effective_contracts_open_fallback(tmp_path: Path) -> None:

    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    _seed_open_lot_event(
        repo,
        record_id="rec_nvda",
        account="lx",
        symbol="NVDA",
        option_type="put",
        side="short",
        contracts=1,
        currency="USD",
        strike=100,
        multiplier=100,
        expiration_ymd="2026-04-17",
    )
    lots = repo.list_position_lots()
    assert len(lots) == 1
    fields = dict(lots[0]["fields"])
    fields["contracts_open"] = None
    repo.replace_position_lots([PositionLotRecord(record_id="rec_nvda", fields=fields)])

    as_of_ms = parse_exp_to_ms("2026-04-20")
    assert as_of_ms is not None

    positions = [dict(item["fields"], record_id=item["record_id"]) for item in repo.list_position_lots()]
    positions[0]["_auto_close_underlying_spot"] = 101

    decisions, applied, errors = _auto_close_payloads(
        repo,
        positions,
        as_of_ms=as_of_ms,
        grace_days=1,
        max_close=5,
    )

    assert len(decisions) == 1
    assert decisions[0]["should_close"] is True
    assert len(applied) == 1
    assert errors == []
    lots = repo.list_position_lots()
    assert len(lots) == 1
    fields = lots[0]["fields"]
    assert fields["status"] == "close"
    assert fields["contracts_open"] == 0
    assert fields["contracts_closed"] == 1
    assert fields["close_type"] == EXPIRE_AUTO_CLOSE
    assert fields["close_reason"] == "expired"
    events = repo.list_trade_events()
    assert len(events) == 2
    assert events[-1]["source_name"] == "auto_close_expired_positions"
    assert events[-1]["raw_payload"]["close_type"] == EXPIRE_AUTO_CLOSE


def test_auto_close_expired_positions_skips_stale_open_input_when_current_lot_closed(tmp_path: Path) -> None:

    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    expiration = parse_exp_to_ms("2026-05-01")
    assert expiration is not None
    repo.replace_position_lots(
        [
            PositionLotRecord(
                record_id="rec_nvda",
                fields={
                    "record_id": "rec_nvda",
                    "position_id": "NVDA_20260501_160P_short",
                    "status": "close",
                    "contracts": 1,
                    "contracts_open": 0,
                    "contracts_closed": 1,
                    "broker": "富途",
                    "account": "lx",
                    "symbol": "NVDA",
                    "option_type": "put",
                    "side": "short",
                    "currency": "USD",
                    "strike": 160,
                    "multiplier": 100,
                    "expiration": expiration,
                    "note": "",
                },
            )
        ]
    )

    stale_positions = [
        {
            "record_id": "rec_nvda",
            "position_id": "NVDA_20260501_160P_short",
            "status": "open",
            "contracts": 1,
            "contracts_open": 1,
            "contracts_closed": 0,
            "broker": "富途",
            "account": "lx",
            "symbol": "NVDA",
            "option_type": "put",
            "side": "short",
            "currency": "USD",
            "strike": 160,
            "multiplier": 100,
            "expiration": expiration,
            "note": "",
        }
    ]
    as_of_ms = parse_exp_to_ms("2026-05-03")
    assert as_of_ms is not None

    decisions, applied, errors = _auto_close_payloads(
        repo,
        stale_positions,
        as_of_ms=as_of_ms,
        grace_days=1,
        max_close=5,
    )

    assert applied == []
    assert errors == []
    assert decisions[0]["should_close"] is False
    assert decisions[0]["skip_reason"] == "already_closed_or_zero_open"
    assert decisions[0]["contracts_open"] == 0
    assert repo.count_trade_events() == 0


def test_auto_close_expired_positions_skips_non_current_candidate_record_id(tmp_path: Path) -> None:

    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    expiration = parse_exp_to_ms("2026-05-28")
    assert expiration is not None
    repo.replace_position_lots(
        [
            PositionLotRecord(
                record_id="lot_0700_put_450_20260528",
                fields={
                    "record_id": "lot_0700_put_450_20260528",
                    "position_id": "0700.HK_20260528_450P_short",
                    "status": "open",
                    "contracts": 6,
                    "contracts_open": 6,
                    "contracts_closed": 0,
                    "broker": "富途",
                    "account": "sy",
                    "symbol": "0700.HK",
                    "option_type": "put",
                    "side": "short",
                    "currency": "HKD",
                    "strike": 450,
                    "multiplier": 100,
                    "expiration": expiration,
                    "note": "",
                },
            )
        ]
    )
    compat_position = {
        "record_id": "compat_0700_put_450_20260528",
        "position_id": "0700.HK_20260528_450P_short",
        "status": "open",
        "contracts": 6,
        "contracts_open": 6,
        "contracts_closed": 0,
        "broker": "富途",
        "account": "sy",
        "symbol": "0700.HK",
        "option_type": "put",
        "side": "short",
        "currency": "HKD",
        "strike": 450,
        "multiplier": 100,
        "expiration": expiration,
        "note": "",
    }
    as_of_ms = parse_exp_to_ms("2026-05-31")
    assert as_of_ms is not None

    decisions, applied, errors = _auto_close_payloads(
        repo,
        [compat_position],
        as_of_ms=as_of_ms,
        grace_days=1,
        max_close=5,
    )

    assert applied == []
    assert errors == []
    assert decisions[0]["should_close"] is False
    assert decisions[0]["skip_reason"] == "not_current_position_lot"
    lot = repo.list_position_lots()[0]
    assert lot["record_id"] == "lot_0700_put_450_20260528"
    assert lot["fields"]["status"] == "open"
    assert lot["fields"]["contracts_open"] == 6
    assert repo.count_trade_events() == 0


def test_auto_close_expired_positions_closes_same_expiry_without_crossing_later_expiry(tmp_path: Path) -> None:

    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    _seed_open_lot_event(
        repo,
        record_id="lot_0700_call_510_20260528",
        account="sy",
        symbol="0700.HK",
        option_type="call",
        side="short",
        contracts=2,
        currency="HKD",
        strike=510,
        multiplier=100,
        expiration_ymd="2026-05-28",
        opened_at_ms=1000,
    )
    _seed_open_lot_event(
        repo,
        record_id="lot_0700_put_450_20260528",
        account="sy",
        symbol="0700.HK",
        option_type="put",
        side="short",
        contracts=6,
        currency="HKD",
        strike=450,
        multiplier=100,
        expiration_ymd="2026-05-28",
        opened_at_ms=2000,
    )
    _seed_open_lot_event(
        repo,
        record_id="lot_0700_put_450_20260629",
        account="sy",
        symbol="0700.HK",
        option_type="put",
        side="short",
        contracts=3,
        currency="HKD",
        strike=450,
        multiplier=100,
        expiration_ymd="2026-06-29",
        opened_at_ms=3000,
    )
    as_of_ms = parse_exp_to_ms("2026-05-31")
    assert as_of_ms is not None
    positions = [dict(item["fields"], record_id=item["record_id"]) for item in repo.list_position_lots()]
    for item in positions:
        item["_auto_close_underlying_spot"] = 500

    decisions, applied, errors = _auto_close_payloads(
        repo,
        positions,
        as_of_ms=as_of_ms,
        grace_days=1,
        max_close=5,
    )

    assert errors == []
    assert {item["record_id"] for item in applied} == {
        "lot_0700_call_510_20260528",
        "lot_0700_put_450_20260528",
    }
    assert {item["ledger_preflight"]["event_type"] for item in applied} == {"expire_close"}
    assert {item["ledger_preflight"]["target_lot_id"] for item in applied} == {
        "lot_0700_call_510_20260528",
        "lot_0700_put_450_20260528",
    }
    assert {item["close_target_resolution"]["strategy"] for item in applied} == {"explicit_record_id_current_lot"}
    assert {tuple(item["close_target_resolution"]["record_ids"]) for item in applied} == {
        ("lot_0700_call_510_20260528",),
        ("lot_0700_put_450_20260528",),
    }
    assert {item["record_id"] for item in decisions if item["should_close"]} == {
        "lot_0700_call_510_20260528",
        "lot_0700_put_450_20260528",
    }
    close_events = [item for item in repo.list_trade_events() if item["source_name"] == "auto_close_expired_positions"]
    assert {item["raw_payload"]["close_target_resolution"]["strategy"] for item in close_events} == {
        "explicit_record_id_current_lot"
    }
    lots_by_id = {item["record_id"]: item["fields"] for item in repo.list_position_lots()}
    assert lots_by_id["lot_0700_call_510_20260528"]["status"] == "close"
    assert lots_by_id["lot_0700_put_450_20260528"]["status"] == "close"
    assert lots_by_id["lot_0700_put_450_20260629"]["status"] == "open"
    assert lots_by_id["lot_0700_put_450_20260629"]["contracts_open"] == 3


def test_auto_close_expired_positions_skips_when_lifecycle_assignment_pending(tmp_path: Path) -> None:
    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    _seed_open_lot_event(
        repo,
        record_id="lot_tigr_put_6_20260522",
        account="lx",
        symbol="TIGR",
        option_type="put",
        side="short",
        contracts=10,
        currency="USD",
        strike=6,
        multiplier=100,
        expiration_ymd="2026-05-22",
        opened_at_ms=1000,
    )
    repo.upsert_trade_lifecycle_case(
        {
            "case_id": "lc_tigr_assignment",
            "case_key": "富途|lx|TIGR|put|short|6|2026-05-22",
            "account": "lx",
            "symbol": "TIGR",
            "option_type": "put",
            "position_side": "short",
            "strike": 6,
            "expiration_ymd": "2026-05-22",
            "contracts": 10,
            "status": "waiting_settlement_evidence",
            "decision_type": "needs_review",
            "target_lot_ids": [],
        }
    )
    as_of_ms = parse_exp_to_ms("2026-05-25")
    assert as_of_ms is not None
    positions = [dict(item["fields"], record_id=item["record_id"]) for item in repo.list_position_lots()]
    positions[0]["_auto_close_underlying_spot"] = 7

    decisions, applied, errors = _auto_close_payloads(
        repo,
        positions,
        as_of_ms=as_of_ms,
        grace_days=1,
        max_close=5,
    )

    assert errors == []
    assert applied == []
    assert decisions[0]["should_close"] is False
    assert decisions[0]["skip_reason"] == "lifecycle_assignment_pending"
    assert decisions[0]["lifecycle_blocker"]["case_id"] == "lc_tigr_assignment"
    assert [item for item in repo.list_trade_events() if item["event_type"] == "expire_close"] == []
    assert repo.get_record_fields("lot_tigr_put_6_20260522")["status"] == "open"


def test_auto_close_expired_positions_records_lifecycle_expire_close_for_otm_pending_case(tmp_path: Path) -> None:
    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    _seed_open_lot_event(
        repo,
        record_id="lot_tigr_put_6_20260522",
        account="lx",
        symbol="TIGR",
        option_type="put",
        side="short",
        contracts=10,
        currency="USD",
        strike=6,
        multiplier=100,
        expiration_ymd="2026-05-22",
        opened_at_ms=1000,
    )
    repo.upsert_trade_lifecycle_case(
        {
            "case_id": "lc_tigr_expire",
            "case_key": "富途|lx|TIGR|put|short|6|2026-05-22",
            "account": "lx",
            "symbol": "TIGR",
            "option_type": "put",
            "position_side": "short",
            "strike": 6,
            "expiration_ymd": "2026-05-22",
            "contracts": 10,
            "status": "waiting_settlement_evidence",
            "decision_type": "needs_review",
            "target_lot_ids": [],
        }
    )
    repo.upsert_trade_lifecycle_evidence(
        {
            "evidence_id": "ev_tigr_zero_close",
            "case_id": "lc_tigr_expire",
            "source_type": "opend_deal",
            "source_event_id": "deal-zero-close",
            "evidence_type": "option_zero_price_close",
            "account": "lx",
            "symbol": "TIGR",
        }
    )
    as_of_ms = parse_exp_to_ms("2026-05-25")
    assert as_of_ms is not None
    positions = [dict(item["fields"], record_id=item["record_id"]) for item in repo.list_position_lots()]
    positions[0]["_auto_close_underlying_spot"] = 7

    decisions, applied, errors = _auto_close_payloads(
        repo,
        positions,
        as_of_ms=as_of_ms,
        grace_days=1,
        max_close=5,
    )

    assert errors == []
    assert len(applied) == 1
    assert decisions[0]["should_close"] is True
    assert repo.get_record_fields("lot_tigr_put_6_20260522")["status"] == "close"
    events = [item for item in repo.list_trade_events() if item["event_type"] == "expire_close"]
    assert len(events) == 1
    assert events[0]["source"] == "option_lifecycle_decision"
    assert events[0]["raw_payload"]["case_id"] == "lc_tigr_expire"
    assert events[0]["raw_payload"]["evidence_ids"] == ["ev_tigr_zero_close"]
    case = repo.get_trade_lifecycle_case("lc_tigr_expire")
    assert case is not None
    assert case["status"] == "ledger_written"
    assert case["decision_type"] == "expire_close"
    assert case["target_lot_ids"] == ["lot_tigr_put_6_20260522"]
    allocations = repo.list_trade_lifecycle_allocations(case_id="lc_tigr_expire")
    assert len(allocations) == 1
    assert allocations[0]["evidence_id"] == "ev_tigr_zero_close"
    assert allocations[0]["target_lot_id"] == "lot_tigr_put_6_20260522"
    assert allocations[0]["canonical_terminal_event_id"] == events[0]["event_id"]


def test_lifecycle_auto_expire_rolls_back_event_lot_and_allocation_when_case_write_fails(
    tmp_path: Path,
) -> None:
    class FailingCaseRepo(ledger_repository.SQLiteOptionPositionsRepository):
        def upsert_trade_lifecycle_case(self, case, *, conn=None):  # type: ignore[no-untyped-def]
            if str(case.get("status") or "") == "ledger_written":
                raise RuntimeError("injected lifecycle case write failure")
            return super().upsert_trade_lifecycle_case(case, conn=conn)

    repo = FailingCaseRepo(tmp_path / "option_positions.sqlite3")
    _seed_open_lot_event(
        repo,
        record_id="lot_tigr_put_6_20260522",
        account="lx",
        symbol="TIGR",
        option_type="put",
        side="short",
        contracts=10,
        currency="USD",
        strike=6,
        multiplier=100,
        expiration_ymd="2026-05-22",
        opened_at_ms=1000,
    )
    repo.upsert_trade_lifecycle_case(
        {
            "case_id": "lc_tigr_expire",
            "case_key": "富途|lx|TIGR|put|short|6|2026-05-22",
            "account": "lx",
            "symbol": "TIGR",
            "option_type": "put",
            "position_side": "short",
            "strike": 6,
            "expiration_ymd": "2026-05-22",
            "contracts": 10,
            "status": "waiting_settlement_evidence",
            "decision_type": "needs_review",
            "target_lot_ids": [],
        }
    )
    repo.upsert_trade_lifecycle_evidence(
        {
            "evidence_id": "ev_tigr_zero_close",
            "case_id": "lc_tigr_expire",
            "source_type": "opend_deal",
            "source_event_id": "deal-zero-close",
            "evidence_type": "option_zero_price_close",
            "account": "lx",
            "symbol": "TIGR",
        }
    )
    as_of_ms = parse_exp_to_ms("2026-05-25")
    assert as_of_ms is not None
    positions = [
        dict(item["fields"], record_id=item["record_id"])
        for item in repo.list_position_lots()
    ]
    positions[0]["_auto_close_underlying_spot"] = 7

    _decisions, applied, errors = _auto_close_payloads(
        repo,
        positions,
        as_of_ms=as_of_ms,
        grace_days=1,
        max_close=5,
    )

    assert applied == []
    assert errors and "injected lifecycle case write failure" in errors[0]
    assert repo.get_record_fields("lot_tigr_put_6_20260522")["status"] == "open"
    assert [row for row in repo.list_trade_events() if row["event_type"] == "expire_close"] == []
    assert repo.list_trade_lifecycle_allocations(case_id="lc_tigr_expire") == []
    case = repo.get_trade_lifecycle_case("lc_tigr_expire")
    assert case is not None
    assert case["status"] == "waiting_settlement_evidence"


def test_auto_close_expired_positions_skips_when_exercise_stock_evidence_seen(tmp_path: Path) -> None:
    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    _seed_open_lot_event(
        repo,
        record_id="lot_aapl_call_200_20260522",
        account="lx",
        symbol="AAPL",
        option_type="call",
        side="long",
        contracts=2,
        currency="USD",
        strike=200,
        multiplier=100,
        expiration_ymd="2026-05-22",
        opened_at_ms=1000,
    )
    repo.upsert_trade_lifecycle_evidence(
        {
            "evidence_id": "ev_aapl_exercise_stock",
            "case_id": None,
            "source_type": "futu_trade_push",
            "source_event_id": "deal-aapl-stock",
            "evidence_type": "stock_settlement_leg",
            "account": "lx",
            "symbol": "AAPL",
            "side": "buy",
            "stock_qty": 200,
            "stock_price": 200,
            "trade_time_ms": parse_exp_to_ms("2026-05-23"),
            "raw": {"broker": "富途", "deal_id": "deal-aapl-stock"},
        }
    )
    as_of_ms = parse_exp_to_ms("2026-05-25")
    assert as_of_ms is not None
    positions = [dict(item["fields"], record_id=item["record_id"]) for item in repo.list_position_lots()]

    decisions, applied, errors = _auto_close_payloads(
        repo,
        positions,
        as_of_ms=as_of_ms,
        grace_days=1,
        max_close=5,
    )

    assert errors == []
    assert applied == []
    assert decisions[0]["should_close"] is False
    assert decisions[0]["skip_reason"] == "lifecycle_stock_settlement_evidence_seen"
    assert [item for item in repo.list_trade_events() if item["event_type"] == "expire_close"] == []
    assert repo.get_record_fields("lot_aapl_call_200_20260522")["status"] == "open"


def test_auto_close_ignores_nested_broker_stock_evidence_for_other_contract(tmp_path: Path) -> None:
    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    _seed_open_lot_event(
        repo,
        record_id="lot_0700_put_440_20260730",
        account="lx",
        symbol="0700.HK",
        option_type="put",
        side="short",
        contracts=1,
        currency="HKD",
        strike=440,
        multiplier=100,
        expiration_ymd="2026-07-30",
        opened_at_ms=1000,
    )
    repo.upsert_trade_lifecycle_evidence(
        {
            "evidence_id": "ev_0700_old_450_put_assignment",
            "case_id": "lc_0700_old_450_put_assignment",
            "source_type": "futu_trade_push",
            "source_event_id": "deal-0700-old-450-put-stock",
            "evidence_type": "stock_settlement_leg",
            "account": "lx",
            "symbol": "0700.HK",
            "side": "buy",
            "stock_qty": 100,
            "stock_price": 450,
            "trade_time_ms": parse_exp_to_ms("2026-06-29"),
            "raw": {
                "broker": "富途",
                "deal_id": "deal-0700-old-450-put-stock",
            },
        }
    )
    as_of_ms = parse_exp_to_ms("2026-08-01")
    assert as_of_ms is not None
    positions = [dict(item["fields"], record_id=item["record_id"]) for item in repo.list_position_lots()]
    positions[0]["_auto_close_underlying_spot"] = 500

    decisions, applied, errors = _auto_close_payloads(
        repo,
        positions,
        as_of_ms=as_of_ms,
        grace_days=1,
        max_close=5,
    )

    assert errors == []
    assert [item["record_id"] for item in applied] == ["lot_0700_put_440_20260730"]
    assert decisions[0]["should_close"] is True
    assert decisions[0].get("lifecycle_blocker") is None
    assert repo.get_record_fields("lot_0700_put_440_20260730")["status"] == "close"


def test_auto_close_expired_positions_fail_closed_on_ledger_identity_mismatch(tmp_path: Path) -> None:
    from domain.domain.option_position_lots import OpenPositionCommand

    class MismatchedSnapshotRepo(ledger_repository.SQLiteOptionPositionsRepository):
        def list_position_lots(self):  # type: ignore[no-untyped-def]
            rows = super().list_position_lots()
            patched = []
            for row in rows:
                fields = dict(row["fields"])
                fields["strike"] = 451
                patched.append({"record_id": row["record_id"], "fields": fields})
            return patched

    repo = MismatchedSnapshotRepo(tmp_path / "option_positions.sqlite3")
    ledger_manual_trades.persist_manual_open_event(
        repo,
        OpenPositionCommand(
            broker="富途",
            account="sy",
            symbol="0700.HK",
            option_type="put",
            side="short",
            contracts=6,
            currency="HKD",
            strike=450,
            multiplier=100,
            expiration_ymd="2026-05-28",
            premium_per_share=1.0,
            opened_at_ms=1000,
        ),
    )
    as_of_ms = parse_exp_to_ms("2026-05-31")
    assert as_of_ms is not None
    positions = [dict(item["fields"], record_id=item["record_id"]) for item in repo.list_position_lots()]
    positions[0]["_auto_close_underlying_spot"] = 500

    decisions, applied, errors = _auto_close_payloads(
        repo,
        positions,
        as_of_ms=as_of_ms,
        grace_days=1,
        max_close=5,
    )

    assert applied == []
    assert len(errors) == 1
    assert "target identity differs" in errors[0]
    assert decisions[0]["ledger_preflight"]["status"] == "blocked"
    assert decisions[0]["ledger_preflight"]["fail_closed"] is True
    assert decisions[0]["ledger_preflight"]["code"] == "target_identity_mismatch"
    record_id = str(decisions[0]["record_id"])
    fields = repo.get_record_fields(record_id)
    assert fields["status"] == "open"
    assert fields["contracts_open"] == 6


def test_position_maintenance_requires_active_ledger_repair_before_closing_position_lots_without_events(
    tmp_path: Path,
) -> None:
    from src.application.positions.maintenance import run_expired_position_maintenance_for_account

    runtime_root = tmp_path / "runtime"
    db_path = runtime_root / "output_shared" / "state" / "option_positions.sqlite3"
    data_config = runtime_root / "portfolio.runtime.json"
    data_config.parent.mkdir(parents=True, exist_ok=True)
    data_config.write_text(
        json.dumps({"option_positions": {}}),
        encoding="utf-8",
    )
    repo = ledger_repository.SQLiteOptionPositionsRepository(db_path)
    repo.replace_position_lots(
        [
            PositionLotRecord(
                record_id="rec_nvda",
                fields={
                    "record_id": "rec_nvda",
                    "position_id": "NVDA_20260417_100P_short",
                    "status": "open",
                    "contracts": 1,
                    "contracts_open": None,
                    "contracts_closed": 0,
                    "broker": "富途",
                    "account": "lx",
                    "symbol": "NVDA",
                    "option_type": "put",
                    "side": "short",
                    "currency": "USD",
                    "strike": 100,
                    "multiplier": 100,
                    "_auto_close_underlying_spot": 101,
                    "expiration": parse_exp_to_ms("2026-04-17"),
                    "note": "",
                },
            )
        ]
    )

    as_of_ms = parse_exp_to_ms("2026-04-20")
    assert as_of_ms is not None
    result = run_expired_position_maintenance_for_account(
        base=tmp_path,
        cfg={"portfolio": {"data_config": str(data_config)}},
        account="lx",
        report_dir=tmp_path / "reports",
        as_of_ms=as_of_ms,
    )

    assert result["applied_closed"] == 0
    assert len(result["errors"]) == 1
    assert "active ledger repair required before auto-close" in result["errors"][0]
    assert "repair the active ledger" in result["errors"][0]
    lots = ledger_repository.SQLiteOptionPositionsRepository(db_path).list_position_lots()
    assert len(lots) == 1
    assert lots[0]["fields"]["status"] == "open"
    assert "close_type" not in lots[0]["fields"]
