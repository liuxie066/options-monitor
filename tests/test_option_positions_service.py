from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from scripts.option_positions_core.domain import EXPIRE_AUTO_CLOSE, parse_exp_to_ms
from scripts.option_positions_core.service import auto_close_expired_positions, build_expired_close_decisions


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
    import scripts.option_positions_core.service as svc

    repo = svc.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    inserted = repo.replace_position_lots(
        [
            {
                "record_id": "rec_nvda",
                "fields": {
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
            }
        ]
    )
    assert inserted == 1

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
    import scripts.option_positions_core.service as svc

    repo = svc.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    expiration = parse_exp_to_ms("2026-05-01")
    assert expiration is not None
    repo.replace_position_lots(
        [
            {
                "record_id": "rec_nvda",
                "fields": {
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
            }
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


def test_position_maintenance_closes_legacy_sqlite_lot_after_load_repo_bootstrap(tmp_path: Path) -> None:
    import scripts.option_positions_core.service as svc
    from src.application.position_maintenance import run_expired_position_maintenance_for_account

    db_path = tmp_path / "option_positions.sqlite3"
    data_config = tmp_path / "portfolio.sqlite.json"
    data_config.write_text(
        json.dumps({"option_positions": {"sqlite_path": str(db_path)}}),
        encoding="utf-8",
    )
    repo = svc.SQLiteOptionPositionsRepository(db_path)
    repo.replace_position_lots(
        [
            {
                "record_id": "rec_nvda",
                "fields": {
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
            }
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

    assert result["applied_closed"] == 1
    assert result["errors"] == []
    lots = svc.SQLiteOptionPositionsRepository(db_path).list_position_lots()
    assert len(lots) == 1
    assert lots[0]["fields"]["status"] == "close"
    assert lots[0]["fields"]["close_type"] == EXPIRE_AUTO_CLOSE
