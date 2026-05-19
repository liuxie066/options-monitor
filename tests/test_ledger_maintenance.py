from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from domain.domain.option_position_lots import EXPIRE_AUTO_CLOSE, parse_exp_to_ms
import src.application.ledger.bootstrap as ledger_bootstrap
import src.application.ledger.manual_trades as ledger_manual_trades
from src.application.ledger.maintenance import auto_close_expired_positions, build_expired_close_decisions
from src.application.ledger.position_records import PositionLotRecord
import src.application.ledger.repository as ledger_repository


def _migrate_seed_position_lots_explicitly(repo: ledger_repository.SQLiteOptionPositionsRepository) -> None:
    result = ledger_bootstrap.migrate_legacy_sqlite_to_repo(repo, legacy_path=repo.db_path, apply=True)
    assert result["ok"] is True
    assert result["applied"] is True
    assert result["source_table"] == "position_lots"


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
    assert decisions[0]["should_close"] is True
    assert decisions[0]["record_id"] == "rec_1"
    patch = decisions[0]["patch"]
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

    assert decisions[0]["should_close"] is False
    assert decisions[0]["reason"] == "missing record_id"
    assert decisions[0]["patch"] is None


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

    assert decisions[0]["should_close"] is False


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

    assert decisions[0]["should_close"] is True
    assert decisions[0]["expiration_ymd"] == "2026-05-01"


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

    assert decisions[0]["should_close"] is False
    assert decisions[0]["skip_reason"] == "already_closed_or_zero_open"
    assert decisions[0]["contracts_open"] == 0
    assert decisions[0]["patch"] is None


def test_auto_close_expired_positions_uses_effective_contracts_open_fallback(tmp_path: Path) -> None:

    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    inserted = repo.replace_position_lots(
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
                    "expiration": parse_exp_to_ms("2026-04-17"),
                    "note": "",
                },
            )
        ]
    )
    assert inserted == 1
    _migrate_seed_position_lots_explicitly(repo)

    as_of_ms = parse_exp_to_ms("2026-04-20")
    assert as_of_ms is not None

    positions = [dict(item["fields"], record_id=item["record_id"]) for item in repo.list_position_lots()]

    decisions, applied, errors = auto_close_expired_positions(
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

    decisions, applied, errors = auto_close_expired_positions(
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

    decisions, applied, errors = auto_close_expired_positions(
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
    may_exp = parse_exp_to_ms("2026-05-28")
    jun_exp = parse_exp_to_ms("2026-06-29")
    assert may_exp is not None
    assert jun_exp is not None
    repo.replace_position_lots(
        [
            PositionLotRecord(
                record_id="lot_0700_call_510_20260528",
                fields={
                    "record_id": "lot_0700_call_510_20260528",
                    "position_id": "0700.HK_20260528_510C_short",
                    "status": "open",
                    "contracts": 2,
                    "contracts_open": 2,
                    "contracts_closed": 0,
                    "broker": "富途",
                    "account": "sy",
                    "symbol": "0700.HK",
                    "option_type": "call",
                    "side": "short",
                    "currency": "HKD",
                    "strike": 510,
                    "multiplier": 100,
                    "expiration": may_exp,
                    "note": "",
                },
            ),
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
                    "expiration": may_exp,
                    "note": "",
                },
            ),
            PositionLotRecord(
                record_id="lot_0700_put_450_20260629",
                fields={
                    "record_id": "lot_0700_put_450_20260629",
                    "position_id": "0700.HK_20260629_450P_short",
                    "status": "open",
                    "contracts": 3,
                    "contracts_open": 3,
                    "contracts_closed": 0,
                    "broker": "富途",
                    "account": "sy",
                    "symbol": "0700.HK",
                    "option_type": "put",
                    "side": "short",
                    "currency": "HKD",
                    "strike": 450,
                    "multiplier": 100,
                    "expiration": jun_exp,
                    "note": "",
                },
            ),
        ]
    )
    _migrate_seed_position_lots_explicitly(repo)
    as_of_ms = parse_exp_to_ms("2026-05-31")
    assert as_of_ms is not None
    positions = [dict(item["fields"], record_id=item["record_id"]) for item in repo.list_position_lots()]

    decisions, applied, errors = auto_close_expired_positions(
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

    decisions, applied, errors = auto_close_expired_positions(
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


def test_position_maintenance_requires_explicit_legacy_migration_before_closing_legacy_lot(tmp_path: Path) -> None:
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
    assert "explicit legacy migration required before auto-close" in result["errors"][0]
    assert "migrate-legacy --apply" in result["errors"][0]
    lots = ledger_repository.SQLiteOptionPositionsRepository(db_path).list_position_lots()
    assert len(lots) == 1
    assert lots[0]["fields"]["status"] == "open"
    assert "close_type" not in lots[0]["fields"]
