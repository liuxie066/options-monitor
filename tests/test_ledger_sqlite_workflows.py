from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest  # pyright: ignore[reportMissingImports]

import src.application.ledger.bootstrap as bootstrap
import src.application.ledger.bootstrap as ledger_bootstrap
import src.application.ledger.interventions as ledger_interventions
import src.application.ledger.manual_trades as ledger_manual_trades
from src.application.ledger.position_records import PositionLotRecord
import src.application.ledger.repository as ledger_repository
from src.application.ledger.store_resolution import resolve_ledger_store
import src.application.ledger.writer as ledger_writer

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))


def _write_data_config(
    path: Path,
    *,
    sqlite_path: Path,
    with_feishu: bool = True,
    bootstrap_from_feishu_enabled: bool = False,
    copy_legacy_to_standard: bool = True,
) -> Path:
    payload: dict[str, object] = {
        "option_positions": {
            "sqlite_path": str(sqlite_path),
            "bootstrap_from_feishu": {"enabled": bool(bootstrap_from_feishu_enabled)},
        },
    }
    if with_feishu:
        payload["feishu"] = {
            "app_id": "app_id",
            "app_secret": "app_secret",
            "tables": {"option_positions": "app_token/table_id"},
        }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    standard_db = path.parent / "output_shared" / "state" / "option_positions.sqlite3"
    if copy_legacy_to_standard and sqlite_path.exists() and sqlite_path.resolve() != standard_db.resolve():
        standard_db.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(sqlite_path, standard_db)
        for suffix in ("-wal", "-shm"):
            sidecar = sqlite_path.with_name(sqlite_path.name + suffix)
            if sidecar.exists():
                shutil.copy2(sidecar, standard_db.with_name(standard_db.name + suffix))
    return path


def test_resolve_ledger_store_ignores_sqlite_path_for_standard_runtime_config(tmp_path: Path) -> None:
    runtime_root = tmp_path / "options-monitor-prod-runtime"
    data_config = runtime_root / "portfolio.runtime.json"
    data_config.parent.mkdir(parents=True, exist_ok=True)
    legacy_db = tmp_path / "wrong" / "option_positions.sqlite3"
    data_config.write_text(
        json.dumps({"option_positions": {"sqlite_path": str(legacy_db)}}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    resolution = resolve_ledger_store(data_config)

    assert resolution.runtime_root == runtime_root.resolve()
    assert resolution.runtime_root_source == "data_config_parent"
    assert resolution.sqlite_path == (runtime_root / "output_shared" / "state" / "option_positions.sqlite3").resolve()
    assert resolution.sqlite_path_source == "runtime_root"
    assert resolution.legacy_sqlite_path == legacy_db.resolve()
    assert any("ignored" in item for item in resolution.warnings)


def test_resolve_ledger_store_ignores_legacy_sqlite_path_for_nonstandard_test_config(tmp_path: Path) -> None:
    data_config = tmp_path / "data.json"
    legacy_db = tmp_path / "option_positions.sqlite3"
    data_config.write_text(
        json.dumps({"option_positions": {"sqlite_path": str(legacy_db)}}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    resolution = resolve_ledger_store(data_config)

    assert resolution.runtime_root == tmp_path.resolve()
    assert resolution.sqlite_path == (tmp_path / "output_shared" / "state" / "option_positions.sqlite3").resolve()
    assert resolution.sqlite_path_source == "runtime_root"
    assert resolution.legacy_sqlite_path == legacy_db.resolve()
    assert any("ignored" in item for item in resolution.warnings)


def test_load_option_positions_repo_ignores_retired_feishu_bootstrap_opt_in(tmp_path: Path) -> None:

    data_config = _write_data_config(
        tmp_path / "data.json",
        sqlite_path=tmp_path / "option_positions.sqlite3",
        bootstrap_from_feishu_enabled=True,
    )
    repo = ledger_bootstrap.load_option_positions_repo(data_config)

    records = repo.list_records(page_size=10)
    assert records == []
    assert repo.count_position_lots() == 0
    assert repo.count_trade_events() == 0
    assert repo.bootstrap_status == "sqlite_only_feishu_bootstrap_retired"
    assert "retired" in str(repo.bootstrap_message)


def test_load_option_positions_repo_does_not_bootstrap_from_feishu_by_default(tmp_path: Path) -> None:

    data_config = _write_data_config(tmp_path / "data.json", sqlite_path=tmp_path / "option_positions.sqlite3")
    repo = ledger_bootstrap.load_option_positions_repo(data_config)

    assert repo.count_trade_events() == 0
    assert repo.count_position_lots() == 0
    assert repo.bootstrap_status == "sqlite_only_no_feishu_bootstrap"
    assert "source of truth" in str(repo.bootstrap_message)


def test_normalize_bootstrap_records_accepts_market_only_rows() -> None:

    records = bootstrap._normalize_bootstrap_records(  # type: ignore[attr-defined]
        [
            {
                "record_id": "rec_1",
                "fields": {
                    "account": "lx",
                    "market": "富途证券（香港）",
                    "symbol": "NVDA",
                    "status": "open",
                    "contracts": 1,
                    "contracts_open": 1,
                },
            }
        ]
    )

    assert len(records) == 1
    assert records[0]["fields"]["broker"] == "富途"


def test_normalize_bootstrap_records_skips_incomplete_option_rows() -> None:

    records = bootstrap._normalize_bootstrap_records(  # type: ignore[attr-defined]
        [
            {
                "record_id": "rec_bad_option",
                "fields": {
                    "account": "lx",
                    "broker": "富途",
                    "symbol": "0700.HK",
                    "option_type": "put",
                    "side": "short",
                    "status": "open",
                    "contracts": 2,
                    "contracts_open": 2,
                    "expiration": "",
                    "strike": None,
                },
            },
            {
                "record_id": "rec_good_option",
                "fields": {
                    "account": "lx",
                    "broker": "富途",
                    "symbol": "0700.HK",
                    "option_type": "put",
                    "side": "short",
                    "status": "open",
                    "contracts": 2,
                    "contracts_open": 2,
                    "expiration": 1782691200000,
                    "strike": 480,
                },
            },
        ]
    )

    assert len(records) == 1
    assert records[0]["record_id"] == "rec_good_option"


def test_bootstrap_trade_events_skips_invalid_timestamp_rows_without_degrading_bootstrap() -> None:

    events = bootstrap._bootstrap_trade_events(  # type: ignore[attr-defined]
        [
            {
                "record_id": "rec_bad_time",
                "fields": {
                    "account": "lx",
                    "broker": "富途",
                    "symbol": "0700.HK",
                    "status": "open",
                    "contracts": 1,
                    "contracts_open": 1,
                    "opened_at": "not-a-number",
                    "last_action_at": "",
                },
            },
            {
                "record_id": "rec_good_time",
                "fields": {
                    "account": "lx",
                    "broker": "富途",
                    "symbol": "NVDA",
                    "status": "open",
                    "contracts": 1,
                    "contracts_open": 1,
                    "opened_at": 1000,
                    "last_action_at": 1000,
                },
            },
        ],
        source_name="test_bootstrap",
    )

    assert len(events) == 1
    event = events[0]
    lot_id = event.get("raw_payload", {}).get("lot_record_id") if isinstance(event, dict) else getattr(event, "lot_id")
    assert lot_id == "rec_good_time"


def test_load_option_positions_repo_skips_legacy_rows_without_broker_or_market(tmp_path: Path) -> None:

    db_path = tmp_path / "option_positions.sqlite3"
    repo = ledger_repository.SQLiteOptionPositionsRepository(db_path)
    with repo._connect() as conn:  # type: ignore[attr-defined]
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS option_positions (
              record_id TEXT PRIMARY KEY,
              fields_json TEXT NOT NULL,
              created_at_ms INTEGER NOT NULL,
              updated_at_ms INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO option_positions (record_id, fields_json, created_at_ms, updated_at_ms)
            VALUES (?, ?, ?, ?)
            """,
            (
                "legacy_1",
                json.dumps({"symbol": "AAPL", "status": "open", "contracts_open": 1}, ensure_ascii=False),
                1000,
                1000,
            ),
        )
        conn.commit()

    data_config = _write_data_config(tmp_path / "data.json", sqlite_path=db_path, with_feishu=False)
    loaded = ledger_bootstrap.load_option_positions_repo(data_config)

    rows = loaded.list_records(page_size=10)
    assert rows == []
    assert loaded.count_trade_events() == 0
    assert loaded.bootstrap_status == "sqlite_only_no_feishu_bootstrap"


def test_load_option_positions_repo_does_not_migrate_legacy_rows_by_default(tmp_path: Path) -> None:

    db_path = tmp_path / "option_positions.sqlite3"
    repo = ledger_repository.SQLiteOptionPositionsRepository(db_path)
    with repo._connect() as conn:  # type: ignore[attr-defined]
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS option_positions (
              record_id TEXT PRIMARY KEY,
              fields_json TEXT NOT NULL,
              created_at_ms INTEGER NOT NULL,
              updated_at_ms INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO option_positions (record_id, fields_json, created_at_ms, updated_at_ms)
            VALUES (?, ?, ?, ?)
            """,
            (
                "legacy_1",
                json.dumps({"symbol": "AAPL", "market": "富途证券", "status": "open", "contracts_open": 1}, ensure_ascii=False),
                1000,
                1000,
            ),
        )
        conn.commit()

    data_config = _write_data_config(
        tmp_path / "data.json",
        sqlite_path=db_path,
        with_feishu=False,
        copy_legacy_to_standard=False,
    )
    loaded = ledger_bootstrap.load_option_positions_repo(data_config)

    rows = loaded.list_records(page_size=10)
    assert rows == []
    assert loaded.count_trade_events() == 0
    assert loaded.db_path != db_path.resolve()
    assert loaded.bootstrap_status == "sqlite_only_no_feishu_bootstrap"
    assert "source of truth" in str(loaded.bootstrap_message)


def test_migrate_legacy_sqlite_imports_legacy_option_positions_explicitly(tmp_path: Path) -> None:

    db_path = tmp_path / "option_positions.sqlite3"
    repo = ledger_repository.SQLiteOptionPositionsRepository(db_path)
    with repo._connect() as conn:  # type: ignore[attr-defined]
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS option_positions (
              record_id TEXT PRIMARY KEY,
              fields_json TEXT NOT NULL,
              created_at_ms INTEGER NOT NULL,
              updated_at_ms INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO option_positions (record_id, fields_json, created_at_ms, updated_at_ms)
            VALUES (?, ?, ?, ?)
            """,
            (
                "legacy_1",
                json.dumps({"symbol": "AAPL", "market": "富途证券", "status": "open", "contracts_open": 1}, ensure_ascii=False),
                1000,
                1000,
            ),
        )
        conn.commit()

    data_config = _write_data_config(
        tmp_path / "data.json",
        sqlite_path=db_path,
        with_feishu=False,
        copy_legacy_to_standard=False,
    )
    loaded = ledger_bootstrap.load_option_positions_repo(data_config)
    dry_run = ledger_bootstrap.migrate_legacy_sqlite_to_repo(loaded, legacy_path=db_path, apply=False)

    assert dry_run["applied"] is False
    assert dry_run["source_table"] == "option_positions"
    assert loaded.count_trade_events() == 0

    result = ledger_bootstrap.migrate_legacy_sqlite_to_repo(loaded, legacy_path=db_path, apply=True)

    rows = loaded.list_records(page_size=10)
    assert len(rows) == 1
    assert rows[0]["record_id"] == "legacy_1"
    assert rows[0]["fields"]["broker"] == "富途"
    assert loaded.db_path != db_path.resolve()
    assert loaded.count_trade_events() == 1
    assert loaded.bootstrap_status == "migrated_legacy_option_positions"
    assert result["applied"] is True
    assert result["migrated_count"] == 1


def test_migrate_legacy_sqlite_prefers_legacy_trade_events_explicitly(tmp_path: Path) -> None:
    from tests.ledger_legacy_helpers import LegacyTradeEvent as TradeEvent

    legacy_db = tmp_path / "legacy" / "option_positions.sqlite3"
    legacy_repo = ledger_repository.SQLiteOptionPositionsRepository(legacy_db)
    legacy_repo.upsert_trade_event(
        TradeEvent(
            event_id="deal-open-legacy",
            source_type="broker_trade_event",
            source_name="legacy_sqlite",
            broker="富途",
            account="sy",
            symbol="AAPL",
            option_type="put",
            side="sell",
            position_effect="open",
            contracts=2,
            price=1.25,
            strike=150.0,
            multiplier=100,
            expiration_ymd="2026-06-19",
            currency="USD",
            trade_time_ms=1000,
            order_id="order-legacy",
            multiplier_source="payload",
            raw_payload={"deal_id": "deal-open-legacy"},
        )
    )
    with legacy_repo._connect() as conn:  # type: ignore[attr-defined]
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS option_positions (
              record_id TEXT PRIMARY KEY,
              fields_json TEXT NOT NULL,
              created_at_ms INTEGER NOT NULL,
              updated_at_ms INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO option_positions (record_id, fields_json, created_at_ms, updated_at_ms)
            VALUES (?, ?, ?, ?)
            """,
            (
                "legacy_snapshot_should_not_win",
                json.dumps(
                    {"symbol": "MSFT", "market": "富途证券", "status": "open", "contracts_open": 1},
                    ensure_ascii=False,
                ),
                1000,
                1000,
            ),
        )
        conn.commit()

    data_config = _write_data_config(
        tmp_path / "data.json",
        sqlite_path=legacy_db,
        with_feishu=False,
        copy_legacy_to_standard=False,
    )

    loaded = ledger_bootstrap.load_option_positions_repo(data_config)

    assert loaded.db_path != legacy_db.resolve()
    assert loaded.count_trade_events() == 0

    result = ledger_bootstrap.migrate_legacy_sqlite_to_repo(loaded, legacy_path=legacy_db, apply=True)

    assert loaded.count_trade_events() == 1
    assert loaded.list_trade_events()[0]["event_id"] == "deal-open-legacy"
    lots = loaded.list_position_lots()
    assert len(lots) == 1
    assert lots[0]["fields"]["symbol"] == "AAPL"
    assert loaded.bootstrap_status == "migrated_legacy_trade_events"
    assert result["source_table"] == "trade_events"


def test_migrate_legacy_sqlite_reports_missing_legacy_sqlite_explicitly(tmp_path: Path) -> None:
    legacy_db = tmp_path / "missing" / "option_positions.sqlite3"
    data_config = _write_data_config(
        tmp_path / "data.json",
        sqlite_path=legacy_db,
        with_feishu=False,
        copy_legacy_to_standard=False,
    )

    loaded = ledger_bootstrap.load_option_positions_repo(data_config)
    result = ledger_bootstrap.migrate_legacy_sqlite_to_repo(loaded, legacy_path=legacy_db, apply=True)

    assert loaded.count_trade_events() == 0
    assert loaded.count_position_lots() == 0
    assert result["ok"] is False
    assert result["message"] == "legacy SQLite database not found"
    assert result["legacy_sqlite_path"] == str(legacy_db.resolve())


def test_load_option_positions_repo_does_not_migrate_existing_position_lots_by_default(tmp_path: Path) -> None:

    db_path = tmp_path / "option_positions.sqlite3"
    repo = ledger_repository.SQLiteOptionPositionsRepository(db_path)
    repo.replace_position_lots(
        [
            PositionLotRecord(
                record_id="rec_bootstrap_1",
                fields={
                    "account": "sy",
                    "broker": "富途",
                    "symbol": "TSLA",
                    "option_type": "put",
                    "side": "short",
                    "contracts": 2,
                    "contracts_open": 2,
                    "contracts_closed": 0,
                    "status": "open",
                    "currency": "USD",
                    "strike": 180.0,
                    "expiration": 1781827200000,
                    "opened_at": 1000,
                    "last_action_at": 1000,
                    "position_id": "TSLA_20260619_180P_short",
                    "note": "exp=2026-06-19;premium_per_share=1.2",
                    "premium": 1.2,
                },
            )
        ]
    )
    data_config = _write_data_config(tmp_path / "data.json", sqlite_path=db_path, with_feishu=False)

    loaded = ledger_bootstrap.load_option_positions_repo(data_config)

    assert loaded.count_trade_events() == 0
    assert loaded.bootstrap_status == "sqlite_only_legacy_position_lots_present"
    assert "migrate-legacy" in str(loaded.bootstrap_message)
    rows = loaded.list_position_lots()
    assert len(rows) == 1
    assert rows[0]["record_id"] == "rec_bootstrap_1"
    assert rows[0]["fields"]["symbol"] == "TSLA"


def test_bootstrap_seed_lot_survives_later_trade_event_projection(tmp_path: Path) -> None:
    from src.application.trades.normalizer import NormalizedTradeDeal

    db_path = tmp_path / "output_shared" / "state" / "option_positions.sqlite3"
    repo_seed = ledger_repository.SQLiteOptionPositionsRepository(db_path)
    repo_seed.replace_position_lots(
        [
            PositionLotRecord(
                record_id="rec_sy_seed",
                fields={
                    "account": "sy",
                    "broker": "富途",
                    "symbol": "AAPL",
                    "option_type": "put",
                    "side": "short",
                    "status": "open",
                    "contracts": 1,
                    "contracts_open": 1,
                    "contracts_closed": 0,
                    "currency": "USD",
                    "strike": 150.0,
                    "expiration": 1781827200000,
                    "opened_at": 1000,
                    "last_action_at": 1000,
                    "position_id": "AAPL_20260619_150P_short",
                    "note": "exp=2026-06-19;premium_per_share=1.0",
                    "premium": 1.0,
                },
            )
        ]
    )
    data_config = _write_data_config(tmp_path / "data.json", sqlite_path=db_path, with_feishu=False)
    repo = ledger_bootstrap.load_option_positions_repo(data_config)
    migrate_result = ledger_bootstrap.migrate_legacy_sqlite_to_repo(repo, legacy_path=db_path, apply=True)

    assert migrate_result["ok"] is True
    assert migrate_result["applied"] is True
    assert migrate_result["source_table"] == "position_lots"
    assert repo.count_trade_events() == 1
    open_deal = NormalizedTradeDeal(
        broker="富途",
        futu_account_id="REAL_1",
        internal_account="lx",
        deal_id="deal-open-2",
        order_id="order-2",
        symbol="0700.HK",
        option_type="put",
        side="sell",
        position_effect="open",
        contracts=1,
        price=3.2,
        strike=420.0,
        multiplier=100,
        multiplier_source="payload",
        expiration_ymd="2026-04-29",
        currency="HKD",
        trade_time_ms=2000,
        raw_payload={"deal_id": "deal-open-2"},
    )

    ledger_writer.persist_trade_event(repo, open_deal)

    lots = repo.list_position_lots()
    record_ids = {row["record_id"] for row in lots}
    assert "rec_sy_seed" in record_ids
    assert "lot_deal-open-2" in record_ids


def test_load_option_positions_repo_supports_sqlite_only_mode(tmp_path: Path) -> None:
    from domain.domain.option_position_lots import OpenPositionCommand

    data_config = _write_data_config(
        tmp_path / "data.json",
        sqlite_path=tmp_path / "option_positions.sqlite3",
        with_feishu=False,
    )
    repo = ledger_bootstrap.load_option_positions_repo(data_config)
    created = ledger_manual_trades.persist_manual_open_event(
        repo,
        OpenPositionCommand(
            broker="富途",
            account="lx",
            symbol="TSLA",
            option_type="put",
            side="short",
            contracts=1,
            currency="USD",
            strike=100.0,
            multiplier=100,
            expiration_ymd="2026-06-19",
            premium_per_share=1.23,
            opened_at_ms=1000,
        ),
    )

    records = repo.list_records(page_size=10)
    assert len(records) == 1
    assert records[0]["fields"]["symbol"] == "TSLA"
    assert created["created"] is True
    assert repo.bootstrap_status == "sqlite_only_no_feishu_bootstrap"


def test_load_option_positions_repo_treats_holdings_only_feishu_as_sqlite_only(tmp_path: Path) -> None:
    import json

    data_config = tmp_path / "data.json"
    data_config.write_text(
        json.dumps(
            {
                "option_positions": {"sqlite_path": str(tmp_path / "option_positions.sqlite3")},
                "feishu": {
                    "app_id": "cli_xxx",
                    "app_secret": "secret_xxx",
                    "tables": {"holdings": "app_token/table_id"},
                },
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    repo = ledger_bootstrap.load_option_positions_repo(data_config)

    assert repo.bootstrap_status == "sqlite_only_no_feishu_bootstrap"
    assert repo.bootstrap_message == "feishu option_positions bootstrap is not used; local trade_events remain source of truth"


def test_load_option_positions_repo_does_not_degrade_when_retired_feishu_bootstrap_config_exists(tmp_path: Path) -> None:

    data_config = _write_data_config(
        tmp_path / "data.json",
        sqlite_path=tmp_path / "option_positions.sqlite3",
        bootstrap_from_feishu_enabled=True,
    )
    repo = ledger_bootstrap.load_option_positions_repo(data_config)

    assert repo.count_trade_events() == 0
    assert repo.bootstrap_status == "sqlite_only_feishu_bootstrap_retired"
    assert "source of truth" in str(repo.bootstrap_message)


def test_load_option_positions_repo_does_not_validate_legacy_position_lots_until_explicit_migration(tmp_path: Path) -> None:
    import json

    db_path = tmp_path / "option_positions.sqlite3"
    repo = ledger_repository.SQLiteOptionPositionsRepository(db_path)
    bad_fields = {
        "account": "lx",
        "broker": "富途",
        "symbol": "NVDA",
        "option_type": "put",
        "side": "short",
        "contracts": 1,
        "contracts_open": 1,
        "currency": "USD",
    }
    with repo._connect() as conn:  # type: ignore[attr-defined]
        conn.execute(
            """
            INSERT INTO position_lots (record_id, fields_json, updated_at_ms)
            VALUES (?, ?, ?)
            """,
            ("lot_bad_option", json.dumps(bad_fields, ensure_ascii=False, sort_keys=True), 1000),
        )
        conn.commit()

    data_config = _write_data_config(
        tmp_path / "data.json",
        sqlite_path=db_path,
        with_feishu=False,
    )
    loaded = ledger_bootstrap.load_option_positions_repo(data_config)

    assert loaded.bootstrap_status == "sqlite_only_legacy_position_lots_present"
    assert "migrate-legacy" in str(loaded.bootstrap_message)
    assert loaded.count_trade_events() == 0
    lots = loaded.list_position_lots()
    assert [row["record_id"] for row in lots] == ["lot_bad_option"]


def test_sqlite_repo_enables_wal_and_busy_timeout(tmp_path: Path) -> None:

    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    with repo._connect() as conn:  # type: ignore[attr-defined]
        journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        busy_timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]

    assert str(journal_mode).lower() == "wal"
    assert int(busy_timeout) == 5000


def test_sqlite_trade_event_upsert_is_idempotent_and_rejects_conflicting_payload(tmp_path: Path) -> None:
    from dataclasses import replace
    from tests.ledger_legacy_helpers import LegacyTradeEvent as TradeEvent

    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    event = TradeEvent(
        event_id="deal-open-1",
        source_type="broker_trade_event",
        source_name="opend_push",
        broker="富途",
        account="lx",
        symbol="AAPL",
        option_type="put",
        side="sell",
        position_effect="open",
        contracts=1,
        price=1.0,
        strike=150.0,
        multiplier=100,
        expiration_ymd="2026-06-19",
        currency="USD",
        trade_time_ms=1000,
        order_id="order-1",
        multiplier_source="payload",
        raw_payload={"deal_id": "deal-open-1"},
    )

    assert repo.upsert_trade_event(event) is True
    assert repo.upsert_trade_event(event) is False
    with pytest.raises(ValueError, match="trade event conflict"):
        repo.upsert_trade_event(replace(event, price=2.0))

    events = repo.list_trade_events()
    assert len(events) == 1
    assert events[0]["event_id"] == "deal-open-1"
    assert events[0]["price"] == 1.0


def test_persist_trade_event_builds_position_lots_projection(tmp_path: Path) -> None:
    from src.application.trades.normalizer import NormalizedTradeDeal

    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    open_deal = NormalizedTradeDeal(
        broker="富途",
        futu_account_id="REAL_1",
        internal_account="lx",
        deal_id="deal-open-1",
        order_id="order-1",
        symbol="0700.HK",
        option_type="put",
        side="sell",
        position_effect="open",
        contracts=2,
        price=3.93,
        strike=480.0,
        multiplier=100,
        multiplier_source="payload",
        expiration_ymd="2026-04-29",
        currency="HKD",
        trade_time_ms=1000,
        raw_payload={"deal_id": "deal-open-1"},
    )
    close_deal = NormalizedTradeDeal(
        broker="富途",
        futu_account_id="REAL_1",
        internal_account="lx",
        deal_id="deal-close-1",
        order_id="order-2",
        symbol="0700.HK",
        option_type="put",
        side="buy",
        position_effect="close",
        contracts=1,
        price=1.2,
        strike=480.0,
        multiplier=100,
        multiplier_source="payload",
        expiration_ymd="2026-04-29",
        currency="HKD",
        trade_time_ms=2000,
        raw_payload={"deal_id": "deal-close-1"},
    )

    first = ledger_writer.persist_trade_event(repo, open_deal)
    second = ledger_writer.persist_trade_event(repo, close_deal)

    assert first["created"] is True
    assert second["created"] is True
    events = repo.list_trade_events()
    assert [row["event_id"] for row in events] == ["deal-open-1", "deal-close-1"]

    lots = repo.list_position_lots()
    assert len(lots) == 1
    fields = lots[0]["fields"]
    assert fields["source_event_id"] == "deal-open-1"
    assert fields["contracts"] == 2
    assert fields["contracts_open"] == 1
    assert fields["contracts_closed"] == 1
    assert fields["status"] == "open"
    assert fields["last_close_event_id"] == "deal-close-1"
    assert fields["strike"] == 480.0
    assert fields["expiration"] == 1777420800000
    assert fields["multiplier"] == 100
    with repo._connect() as conn:  # type: ignore[attr-defined]
        row = conn.execute(
            "SELECT expiration, strike, multiplier FROM position_lots WHERE record_id = ?",
            (lots[0]["record_id"],),
        ).fetchone()
    assert row is not None
    assert row["expiration"] == 1777420800000
    assert row["strike"] == 480.0
    assert row["multiplier"] == 100.0


def test_sqlite_repo_migrates_and_backfills_position_lot_contract_columns(tmp_path: Path) -> None:
    import sqlite3

    db_path = tmp_path / "option_positions.sqlite3"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE position_lots (
              record_id TEXT PRIMARY KEY,
              fields_json TEXT NOT NULL,
              source_event_id TEXT,
              updated_at_ms INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO position_lots (record_id, fields_json, source_event_id, updated_at_ms)
            VALUES (?, ?, ?, ?)
            """,
            (
                "lot_legacy_1",
                json.dumps(
                    {
                        "broker": "富途",
                        "account": "lx",
                        "symbol": "TSLA",
                        "option_type": "put",
                        "side": "short",
                        "contracts": 1,
                        "contracts_open": 1,
                        "strike": 100.0,
                        "expiration": 1781827200000,
                        "note": "multiplier=100",
                    },
                    ensure_ascii=False,
                ),
                "manual-open-legacy",
                1000,
            ),
        )
        conn.commit()

    repo = ledger_repository.SQLiteOptionPositionsRepository(db_path)
    lot = repo.list_position_lots()[0]
    assert lot["fields"]["expiration"] == 1781827200000
    assert lot["fields"]["strike"] == 100.0
    assert lot["fields"]["multiplier"] == 100

    with repo._connect() as conn:  # type: ignore[attr-defined]
        cols = {str(row["name"]) for row in conn.execute("PRAGMA table_info(position_lots)").fetchall()}
        row = conn.execute(
            "SELECT expiration, strike, multiplier FROM position_lots WHERE record_id = ?",
            ("lot_legacy_1",),
        ).fetchone()
    assert {"expiration", "strike", "multiplier"} <= cols
    assert row is not None
    assert row["expiration"] == 1781827200000
    assert row["strike"] == 100.0
    assert row["multiplier"] == 100.0


def test_rebuild_position_lots_applies_legacy_manual_close_to_bootstrap_seed(tmp_path: Path) -> None:
    from tests.ledger_legacy_helpers import LegacyTradeEvent as TradeEvent

    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    ledger_writer.persist_trade_event_object(
        repo,
        TradeEvent(
            event_id="bootstrap:lx:seed",
            source_type="bootstrap_snapshot",
            source_name="feishu_bootstrap",
            broker="富途",
            account="lx",
            symbol="AAPL",
            option_type="put",
            side="sell",
            position_effect="open",
            contracts=1,
            price=1.0,
            strike=150.0,
            multiplier=100,
            expiration_ymd="2026-06-19",
            currency="USD",
            trade_time_ms=1000,
            order_id=None,
            multiplier_source="bootstrap_snapshot",
            raw_payload={
                "lot_record_id": "rec_lx_seed",
                "fields": {
                    "broker": "富途",
                    "account": "lx",
                    "symbol": "AAPL",
                    "option_type": "put",
                    "side": "short",
                    "contracts": 1,
                    "contracts_open": 1,
                    "contracts_closed": 0,
                    "status": "open",
                    "currency": "USD",
                    "strike": 150.0,
                    "expiration": 1781827200000,
                    "opened_at": 1000,
                    "last_action_at": 1000,
                    "position_id": "AAPL_20260619_150P_short",
                    "note": "exp=2026-06-19;premium_per_share=1.0",
                    "premium": 1.0,
                },
            },
        )
    )
    ledger_writer.persist_trade_event_object(
        repo,
        TradeEvent(
            event_id="manual-close-rec-lx-seed",
            source_type="manual_trade_event",
            source_name="cli_manual_close",
            broker="富途",
            account="lx",
            symbol="AAPL",
            option_type="put",
            side="buy",
            position_effect="close",
            contracts=1,
            price=0.0,
            strike=150.0,
            multiplier=100,
            expiration_ymd="2026-06-19",
            currency="USD",
            trade_time_ms=2000,
            order_id=None,
            multiplier_source="payload",
            raw_payload={
                "source": "option_positions.py",
                "mode": "manual_close",
                "record_id": "rec_lx_seed",
                "close_reason": "expired",
            },
        )
    )

    result = ledger_writer.rebuild_position_lots_from_trade_events(repo)

    lots = repo.list_position_lots()
    assert result["trade_event_count"] == 2
    assert result["position_lot_count"] == 1
    assert lots[0]["record_id"] == "rec_lx_seed"
    assert lots[0]["fields"]["contracts_open"] == 0
    assert lots[0]["fields"]["contracts_closed"] == 1
    assert lots[0]["fields"]["status"] == "close"
    assert lots[0]["fields"]["last_close_event_id"] == "manual-close-rec-lx-seed"
    assert result["unmatched_explicit_close_count"] == 0


def test_rebuild_position_lots_closes_bootstrap_seed_by_record_id_even_if_live_projection_source_event_id_drifted(
    tmp_path: Path,
) -> None:
    from tests.ledger_legacy_helpers import LegacyTradeEvent as TradeEvent

    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    ledger_writer.persist_trade_event_object(
        repo,
        TradeEvent(
            event_id="bootstrap:lx:seed",
            source_type="bootstrap_snapshot",
            source_name="feishu_bootstrap",
            broker="富途",
            account="lx",
            symbol="AAPL",
            option_type="put",
            side="sell",
            position_effect="open",
            contracts=1,
            price=1.0,
            strike=150.0,
            multiplier=100,
            expiration_ymd="2026-06-19",
            currency="USD",
            trade_time_ms=1000,
            order_id=None,
            multiplier_source="bootstrap_snapshot",
            raw_payload={
                "lot_record_id": "rec_lx_seed",
                "fields": {
                    "broker": "富途",
                    "account": "lx",
                    "symbol": "AAPL",
                    "option_type": "put",
                    "side": "short",
                    "contracts": 1,
                    "contracts_open": 1,
                    "contracts_closed": 0,
                    "status": "open",
                    "currency": "USD",
                    "strike": 150.0,
                    "expiration": 1781827200000,
                    "opened_at": 1000,
                    "last_action_at": 1000,
                    "position_id": "AAPL_20260619_150P_short",
                    "note": "exp=2026-06-19;premium_per_share=1.0",
                    "premium": 1.0,
                },
            },
        ),
    )
    ledger_writer.persist_trade_event_object(
        repo,
        TradeEvent(
            event_id="manual-close-rec-lx-seed",
            source_type="manual_trade_event",
            source_name="cli_manual_close",
            broker="富途",
            account="lx",
            symbol="AAPL",
            option_type="put",
            side="buy",
            position_effect="close",
            contracts=1,
            price=0.0,
            strike=150.0,
            multiplier=100,
            expiration_ymd="2026-06-19",
            currency="USD",
            trade_time_ms=2000,
            order_id=None,
            multiplier_source="payload",
            raw_payload={
                "source": "option_positions.py",
                "mode": "manual_close",
                "record_id": "rec_lx_seed",
                "close_target_source_event_id": "bootstrap:lx:seed",
                "close_reason": "expired",
            },
        ),
    )
    with repo._connect() as conn:  # type: ignore[attr-defined]
        conn.execute(
            """
            UPDATE position_lots
            SET fields_json = json_set(fields_json, '$.source_event_id', 'legacy-drifted-open-event'),
                source_event_id = 'legacy-drifted-open-event'
            WHERE record_id = 'rec_lx_seed'
            """
        )
        conn.commit()

    result = ledger_writer.rebuild_position_lots_from_trade_events(repo)

    lot = repo.list_position_lots()[0]
    assert lot["record_id"] == "rec_lx_seed"
    assert lot["fields"]["contracts_open"] == 0
    assert lot["fields"]["contracts_closed"] == 1
    assert lot["fields"]["status"] == "close"
    assert result["unmatched_explicit_close_count"] == 0


def test_close_projection_does_not_cross_match_other_account_seed_lot(tmp_path: Path) -> None:
    from tests.ledger_legacy_helpers import LegacyTradeEvent as TradeEvent, project_position_lot_records

    events = [
        TradeEvent(
            event_id="bootstrap:sy:seed",
            source_type="bootstrap_snapshot",
            source_name="feishu_bootstrap",
            broker="富途",
            account="sy",
            symbol="AAPL",
            option_type="put",
            side="sell",
            position_effect="open",
            contracts=1,
            price=1.0,
            strike=150.0,
            multiplier=100,
            expiration_ymd="2026-06-19",
            currency="USD",
            trade_time_ms=1000,
            order_id=None,
            multiplier_source="bootstrap_snapshot",
            raw_payload={
                "lot_record_id": "rec_sy_seed",
                "fields": {
                    "broker": "富途",
                    "account": "sy",
                    "symbol": "AAPL",
                    "option_type": "put",
                    "side": "short",
                    "contracts": 1,
                    "contracts_open": 1,
                    "contracts_closed": 0,
                    "status": "open",
                    "currency": "USD",
                    "strike": 150.0,
                    "expiration": 1781827200000,
                    "opened_at": 1000,
                    "last_action_at": 1000,
                    "position_id": "AAPL_20260619_150P_short",
                    "note": "exp=2026-06-19;premium_per_share=1.0",
                    "premium": 1.0,
                },
            },
        ),
        TradeEvent(
            event_id="deal-close-lx-only",
            source_type="broker_trade_event",
            source_name="opend_push",
            broker="富途",
            account="lx",
            symbol="AAPL",
            option_type="put",
            side="buy",
            position_effect="close",
            contracts=1,
            price=0.5,
            strike=150.0,
            multiplier=100,
            expiration_ymd="2026-06-19",
            currency="USD",
            trade_time_ms=2000,
            order_id="order-close-lx",
            multiplier_source="payload",
            raw_payload={"deal_id": "deal-close-lx-only"},
        ),
    ]

    lots = project_position_lot_records(events)

    assert len(lots) == 1
    assert lots[0].record_id == "rec_sy_seed"
    assert lots[0].fields["account"] == "sy"
    assert lots[0].fields["contracts_open"] == 1
    assert lots[0].fields["contracts_closed"] == 0
    assert lots[0].fields["status"] == "open"


def test_close_projection_prefers_structured_expiration_over_missing_note_exp() -> None:
    from tests.ledger_legacy_helpers import LegacyTradeEvent as TradeEvent, project_position_lot_records

    events = [
        TradeEvent(
            event_id="bootstrap:lx:seed",
            source_type="bootstrap_snapshot",
            source_name="sqlite_position_lots",
            broker="富途",
            account="lx",
            symbol="AAPL",
            option_type="put",
            side="sell",
            position_effect="open",
            contracts=2,
            price=1.0,
            strike=150.0,
            multiplier=100,
            expiration_ymd="2026-06-19",
            currency="USD",
            trade_time_ms=1000,
            order_id=None,
            multiplier_source="bootstrap_snapshot",
            raw_payload={
                "lot_record_id": "rec_lx_seed",
                "fields": {
                    "broker": "富途",
                    "account": "lx",
                    "symbol": "AAPL",
                    "option_type": "put",
                    "side": "short",
                    "contracts": 2,
                    "contracts_open": 2,
                    "contracts_closed": 0,
                    "status": "open",
                    "currency": "USD",
                    "strike": 150.0,
                    "expiration": 1781827200000,
                    "opened_at": 1000,
                    "last_action_at": 1000,
                    "position_id": "AAPL_20260619_150P_short",
                    "note": "premium_per_share=1.0",
                    "premium": 1.0,
                },
            },
        ),
        TradeEvent(
            event_id="deal-close-lx-exp-structured",
            source_type="broker_trade_event",
            source_name="opend_push",
            broker="富途",
            account="lx",
            symbol="AAPL",
            option_type="put",
            side="buy",
            position_effect="close",
            contracts=1,
            price=0.5,
            strike=150.0,
            multiplier=100,
            expiration_ymd="2026-06-19",
            currency="USD",
            trade_time_ms=2000,
            order_id="order-close-lx-exp-structured",
            multiplier_source="payload",
            raw_payload={"deal_id": "deal-close-lx-exp-structured"},
        ),
    ]

    lots = project_position_lot_records(events)

    assert len(lots) == 1
    assert lots[0].record_id == "rec_lx_seed"
    assert lots[0].fields["contracts_open"] == 1
    assert lots[0].fields["contracts_closed"] == 1
    assert lots[0].fields["last_close_event_id"] == "deal-close-lx-exp-structured"


def test_close_projection_buy_side_marks_buy_to_close_type() -> None:
    from domain.domain.option_position_lots import BUY_TO_CLOSE
    from tests.ledger_legacy_helpers import LegacyTradeEvent as TradeEvent, project_position_lot_records

    events = [
        TradeEvent(
            event_id="open-1",
            source_type="broker_trade_event",
            source_name="opend_push",
            broker="富途",
            account="lx",
            symbol="AAPL",
            option_type="put",
            side="sell",
            position_effect="open",
            contracts=1,
            price=1.0,
            strike=150.0,
            multiplier=100,
            expiration_ymd="2026-06-19",
            currency="USD",
            trade_time_ms=1000,
            order_id="order-open-1",
            multiplier_source="payload",
            raw_payload={"deal_id": "open-1"},
        ),
        TradeEvent(
            event_id="close-1",
            source_type="broker_trade_event",
            source_name="opend_push",
            broker="富途",
            account="lx",
            symbol="AAPL",
            option_type="put",
            side="buy",
            position_effect="close",
            contracts=1,
            price=0.5,
            strike=150.0,
            multiplier=100,
            expiration_ymd="2026-06-19",
            currency="USD",
            trade_time_ms=2000,
            order_id="order-close-1",
            multiplier_source="payload",
            raw_payload={"deal_id": "close-1"},
        ),
    ]

    lots = project_position_lot_records(events)

    assert len(lots) == 1
    assert lots[0].fields["contracts_open"] == 0
    assert lots[0].fields["status"] == "close"
    assert lots[0].fields["close_type"] == BUY_TO_CLOSE


def test_close_projection_matches_bootstrap_lot_by_legacy_record_id() -> None:
    from tests.ledger_legacy_helpers import LegacyTradeEvent as TradeEvent, project_position_lot_records

    events = [
        TradeEvent(
            event_id="bootstrap:lx:seed",
            source_type="bootstrap_snapshot",
            source_name="sqlite_position_lots",
            broker="富途",
            account="lx",
            symbol="AAPL",
            option_type="put",
            side="sell",
            position_effect="open",
            contracts=2,
            price=1.0,
            strike=150.0,
            multiplier=100,
            expiration_ymd="2026-06-19",
            currency="USD",
            trade_time_ms=1000,
            order_id=None,
            multiplier_source="bootstrap_snapshot",
            raw_payload={
                "lot_record_id": "rec_lx_seed",
                "fields": {
                    "broker": "富途",
                    "account": "lx",
                    "symbol": "AAPL",
                    "option_type": "put",
                    "side": "short",
                    "contracts": 2,
                    "contracts_open": 2,
                    "contracts_closed": 0,
                    "status": "open",
                    "currency": "USD",
                    "strike": 150.0,
                    "expiration": 1781827200000,
                    "opened_at": 1000,
                    "last_action_at": 1000,
                    "position_id": "AAPL_20260619_150P_short",
                    "note": "premium_per_share=1.0",
                    "premium": 1.0,
                },
            },
        ),
        TradeEvent(
            event_id="manual-close-rec-lx-seed",
            source_type="manual_trade_event",
            source_name="cli_manual_close",
            broker="富途",
            account="lx",
            symbol="AAPL",
            option_type="put",
            side="buy",
            position_effect="close",
            contracts=2,
            price=0.0,
            strike=150.0,
            multiplier=100,
            expiration_ymd="2026-06-19",
            currency="USD",
            trade_time_ms=2000,
            order_id=None,
            multiplier_source="payload",
            raw_payload={
                "source": "option_positions.py",
                "mode": "manual_close",
                "record_id": "rec_lx_seed",
                "close_reason": "expired",
            },
        ),
    ]

    lots = project_position_lot_records(events)

    assert len(lots) == 1
    assert lots[0].record_id == "rec_lx_seed"
    assert lots[0].fields["contracts_open"] == 0
    assert lots[0].fields["contracts_closed"] == 2
    assert lots[0].fields["status"] == "close"
    assert lots[0].fields["last_close_event_id"] == "manual-close-rec-lx-seed"


def test_close_projection_prefers_explicit_source_event_target() -> None:
    from tests.ledger_legacy_helpers import LegacyTradeEvent as TradeEvent, project_position_lot_records

    events = [
        TradeEvent(
            event_id="open-1",
            source_type="broker_trade_event",
            source_name="opend_push",
            broker="富途",
            account="lx",
            symbol="AAPL",
            option_type="put",
            side="sell",
            position_effect="open",
            contracts=1,
            price=1.0,
            strike=150.0,
            multiplier=100,
            expiration_ymd="2026-06-19",
            currency="USD",
            trade_time_ms=1000,
            order_id="order-1",
            multiplier_source="payload",
            raw_payload={"deal_id": "open-1"},
        ),
        TradeEvent(
            event_id="open-2",
            source_type="broker_trade_event",
            source_name="opend_push",
            broker="富途",
            account="lx",
            symbol="AAPL",
            option_type="put",
            side="sell",
            position_effect="open",
            contracts=1,
            price=1.1,
            strike=150.0,
            multiplier=100,
            expiration_ymd="2026-06-19",
            currency="USD",
            trade_time_ms=1100,
            order_id="order-2",
            multiplier_source="payload",
            raw_payload={"deal_id": "open-2"},
        ),
        TradeEvent(
            event_id="manual-close-target-open-2",
            source_type="manual_trade_event",
            source_name="cli_manual_close",
            broker="富途",
            account="lx",
            symbol="AAPL",
            option_type="put",
            side="buy",
            position_effect="close",
            contracts=1,
            price=0.2,
            strike=150.0,
            multiplier=100,
            expiration_ymd="2026-06-19",
            currency="USD",
            trade_time_ms=2000,
            order_id=None,
            multiplier_source="payload",
            raw_payload={
                "source": "option_positions.py",
                "mode": "manual_close",
                "record_id": "lot_open-2",
                "close_target_source_event_id": "open-2",
                "close_reason": "expired",
            },
        ),
    ]

    lots = project_position_lot_records(events)
    lots_by_id = {record.record_id: record.fields for record in lots}

    assert lots_by_id["lot_open-1"]["contracts_open"] == 1
    assert lots_by_id["lot_open-1"]["contracts_closed"] == 0
    assert lots_by_id["lot_open-2"]["contracts_open"] == 0
    assert lots_by_id["lot_open-2"]["contracts_closed"] == 1
    assert lots_by_id["lot_open-2"]["last_close_event_id"] == "manual-close-target-open-2"


def test_close_projection_does_not_fallback_when_explicit_target_is_missing() -> None:
    from tests.ledger_legacy_helpers import LegacyTradeEvent as TradeEvent, project_position_lot_records

    events = [
        TradeEvent(
            event_id="open-1",
            source_type="broker_trade_event",
            source_name="opend_push",
            broker="富途",
            account="lx",
            symbol="AAPL",
            option_type="put",
            side="sell",
            position_effect="open",
            contracts=1,
            price=1.0,
            strike=150.0,
            multiplier=100,
            expiration_ymd="2026-06-19",
            currency="USD",
            trade_time_ms=1000,
            order_id="order-1",
            multiplier_source="payload",
            raw_payload={"deal_id": "open-1"},
        ),
        TradeEvent(
            event_id="manual-close-missing-target",
            source_type="manual_trade_event",
            source_name="cli_manual_close",
            broker="富途",
            account="lx",
            symbol="AAPL",
            option_type="put",
            side="buy",
            position_effect="close",
            contracts=1,
            price=0.2,
            strike=150.0,
            multiplier=100,
            expiration_ymd="2026-06-19",
            currency="USD",
            trade_time_ms=2000,
            order_id=None,
            multiplier_source="payload",
            raw_payload={
                "source": "option_positions.py",
                "mode": "manual_close",
                "record_id": "lot_does_not_exist",
                "close_target_source_event_id": "open-missing",
                "close_reason": "expired",
            },
        ),
    ]

    lots = project_position_lot_records(events)

    assert len(lots) == 1
    assert lots[0].record_id == "lot_open-1"
    assert lots[0].fields["contracts_open"] == 1
    assert lots[0].fields["contracts_closed"] == 0


def test_close_projection_does_not_partially_apply_oversized_explicit_target() -> None:
    from tests.ledger_legacy_helpers import LegacyTradeEvent as TradeEvent, project_position_lot_records

    events = [
        TradeEvent(
            event_id="open-1",
            source_type="broker_trade_event",
            source_name="opend_push",
            broker="富途",
            account="lx",
            symbol="AAPL",
            option_type="put",
            side="sell",
            position_effect="open",
            contracts=1,
            price=1.0,
            strike=150.0,
            multiplier=100,
            expiration_ymd="2026-06-19",
            currency="USD",
            trade_time_ms=1000,
            order_id="order-1",
            multiplier_source="payload",
            raw_payload={"deal_id": "open-1"},
        ),
        TradeEvent(
            event_id="manual-close-oversized-target",
            source_type="manual_trade_event",
            source_name="cli_manual_close",
            broker="富途",
            account="lx",
            symbol="AAPL",
            option_type="put",
            side="buy",
            position_effect="close",
            contracts=2,
            price=0.2,
            strike=150.0,
            multiplier=100,
            expiration_ymd="2026-06-19",
            currency="USD",
            trade_time_ms=2000,
            order_id=None,
            multiplier_source="payload",
            raw_payload={
                "source": "option_positions.py",
                "mode": "manual_close",
                "record_id": "lot_open-1",
                "close_target_source_event_id": "open-1",
                "close_reason": "expired",
            },
        ),
    ]

    lots = project_position_lot_records(events)

    assert len(lots) == 1
    assert lots[0].record_id == "lot_open-1"
    assert lots[0].fields["contracts_open"] == 1
    assert lots[0].fields["contracts_closed"] == 0
    assert "last_close_event_id" not in lots[0].fields


def test_close_projection_uses_record_id_when_legacy_source_event_target_disagrees() -> None:
    from tests.ledger_legacy_helpers import LegacyTradeEvent as TradeEvent, project_position_lot_records_with_diagnostics

    events = [
        TradeEvent(
            event_id="open-1",
            source_type="broker_trade_event",
            source_name="opend_push",
            broker="富途",
            account="lx",
            symbol="AAPL",
            option_type="put",
            side="sell",
            position_effect="open",
            contracts=1,
            price=1.0,
            strike=150.0,
            multiplier=100,
            expiration_ymd="2026-06-19",
            currency="USD",
            trade_time_ms=1000,
            order_id="order-1",
            multiplier_source="payload",
            raw_payload={"deal_id": "open-1"},
        ),
        TradeEvent(
            event_id="open-2",
            source_type="broker_trade_event",
            source_name="opend_push",
            broker="富途",
            account="lx",
            symbol="AAPL",
            option_type="put",
            side="sell",
            position_effect="open",
            contracts=1,
            price=1.1,
            strike=150.0,
            multiplier=100,
            expiration_ymd="2026-06-19",
            currency="USD",
            trade_time_ms=1100,
            order_id="order-2",
            multiplier_source="payload",
            raw_payload={"deal_id": "open-2"},
        ),
        TradeEvent(
            event_id="manual-close-conflict",
            source_type="manual_trade_event",
            source_name="cli_manual_close",
            broker="富途",
            account="lx",
            symbol="AAPL",
            option_type="put",
            side="buy",
            position_effect="close",
            contracts=1,
            price=0.2,
            strike=150.0,
            multiplier=100,
            expiration_ymd="2026-06-19",
            currency="USD",
            trade_time_ms=2000,
            order_id=None,
            multiplier_source="payload",
            raw_payload={
                "source": "option_positions.py",
                "mode": "manual_close",
                "record_id": "lot_open-2",
                "close_target_source_event_id": "open-1",
                "close_reason": "expired",
            },
        ),
    ]

    projection = project_position_lot_records_with_diagnostics(events)
    lots_by_id = {record.record_id: record.fields for record in projection.lots}

    assert lots_by_id["lot_open-1"]["contracts_open"] == 1
    assert lots_by_id["lot_open-2"]["contracts_open"] == 0
    assert projection.diagnostics == []


def test_repository_rejects_unresolved_heuristic_close_contracts(tmp_path: Path) -> None:
    from tests.ledger_legacy_helpers import LegacyTradeEvent as TradeEvent

    def make_event(**overrides: object) -> TradeEvent:
        payload: dict[str, Any] = {
            "event_id": "open-1",
            "source_type": "broker_trade_event",
            "source_name": "opend_push",
            "broker": "富途",
            "account": "lx",
            "symbol": "AAPL",
            "option_type": "put",
            "side": "sell",
            "position_effect": "open",
            "contracts": 1,
            "price": 1.0,
            "strike": 150.0,
            "multiplier": 100,
            "expiration_ymd": "2026-06-19",
            "currency": "USD",
            "trade_time_ms": 1000,
            "order_id": "order-1",
            "multiplier_source": "payload",
            "raw_payload": {"deal_id": "open-1"},
        }
        payload.update(overrides)
        return TradeEvent(**payload)  # type: ignore[arg-type]

    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    repo.upsert_trade_event(make_event())
    with pytest.raises(ValueError, match="target_lot_id_required"):
        repo.upsert_trade_event(
            make_event(
                event_id="close-oversized-heuristic",
                side="buy",
                position_effect="close",
                contracts=2,
                price=0.2,
                trade_time_ms=2000,
                order_id="order-close",
                raw_payload={"deal_id": "close-oversized-heuristic"},
            )
        )


def test_persist_manual_open_event_builds_position_lot(tmp_path: Path) -> None:
    from domain.domain.option_position_lots import OpenPositionCommand

    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    result = ledger_manual_trades.persist_manual_open_event(
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

    assert result["created"] is True
    assert result["record_id"] is not None
    assert str(result["record_id"]).startswith("lot_manual-open-")
    lots = repo.list_position_lots()
    assert len(lots) == 1
    assert lots[0]["record_id"] == result["record_id"]
    assert lots[0]["fields"]["contracts_open"] == 2
    assert lots[0]["fields"]["status"] == "open"


def test_persist_manual_open_event_is_idempotent_on_retry(tmp_path: Path) -> None:
    """Retrying manual-open with identical parameters must not create duplicate lots."""
    from domain.domain.option_position_lots import OpenPositionCommand

    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    command = OpenPositionCommand(
        broker="富途",
        account="sy",
        symbol="9992.HK",
        option_type="put",
        side="short",
        contracts=1,
        currency="HKD",
        strike=145.0,
        multiplier=100,
        expiration_ymd="2026-07-30",
        premium_per_share=6.0,
        opened_at_ms=1_700_000_000_000,
    )

    result1 = ledger_manual_trades.persist_manual_open_event(repo, command)
    result2 = ledger_manual_trades.persist_manual_open_event(repo, command)

    assert result1["created"] is True
    assert result2["created"] is False
    assert result1["event_id"] == result2["event_id"]
    lots = repo.list_position_lots()
    assert len(lots) == 1
    events = repo.list_trade_events()
    assert len(events) == 1


def test_persist_manual_close_event_updates_position_lot(tmp_path: Path) -> None:
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

    lot = repo.list_position_lots()[0]
    fields = dict(lot["fields"])
    fields["account"] = " LX "
    fields["currency"] = "港币"
    result = ledger_manual_trades.persist_manual_close_event(
        repo,
        record_id=lot["record_id"],
        fields=fields,
        contracts_to_close=1,
        close_price=1.2,
        close_reason="manual_buy_to_close",
        as_of_ms=2000,
    )

    assert result["created"] is True
    lots = repo.list_position_lots()
    assert len(lots) == 1
    assert lots[0]["fields"]["contracts_open"] == 1
    assert lots[0]["fields"]["contracts_closed"] == 1
    events = repo.list_trade_events()
    assert events[-1]["raw_payload"]["record_id"] == lot["record_id"]
    assert events[-1]["raw_payload"]["close_target_source_event_id"] == lots[0]["fields"]["source_event_id"]
    assert events[-1]["account"] == "lx"
    assert events[-1]["currency"] == "HKD"
    assert events[-1]["raw_payload"]["close_target_account"] == "lx"
    assert events[-1]["raw_payload"]["close_target_broker"] == "富途"


def test_persist_manual_close_event_is_idempotent_on_retry(tmp_path: Path) -> None:
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

    lot = repo.list_position_lots()[0]
    result1 = ledger_manual_trades.persist_manual_close_event(
        repo,
        record_id=lot["record_id"],
        fields=lot["fields"],
        contracts_to_close=1,
        close_price=1.2,
        close_reason="manual_buy_to_close",
        as_of_ms=2000,
    )
    result2 = ledger_manual_trades.persist_manual_close_event(
        repo,
        record_id=lot["record_id"],
        fields=repo.get_position_lot_fields(lot["record_id"]),
        contracts_to_close=1,
        close_price=1.2,
        close_reason="manual_buy_to_close",
        as_of_ms=3000,
    )

    assert result1["created"] is True
    assert result2["created"] is False
    assert result1["event_id"] == result2["event_id"]
    lots = repo.list_position_lots()
    assert lots[0]["fields"]["contracts_open"] == 1
    assert lots[0]["fields"]["contracts_closed"] == 1
    assert len(repo.list_trade_events()) == 2


def test_persist_manual_close_event_requires_broker_on_position_lot(tmp_path: Path) -> None:

    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")

    with pytest.raises(ValueError, match="position lot missing broker"):
        ledger_manual_trades.persist_manual_close_event(
            repo,
            record_id="lot_market_only",
            fields={
                "market": "富途",
                "account": "lx",
                "symbol": "0700.HK",
                "option_type": "put",
                "side": "short",
                "contracts": 1,
                "contracts_open": 1,
                "currency": "HKD",
                "strike": 480.0,
                "multiplier": 100,
                "expiration": 1777420800000,
            },
            contracts_to_close=1,
            close_price=1.2,
            close_reason="manual_buy_to_close",
            as_of_ms=2000,
        )


def test_persist_manual_close_event_rejects_mismatched_record_id_and_fields(tmp_path: Path) -> None:
    from domain.domain.option_position_lots import OpenPositionCommand

    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    ledger_manual_trades.persist_manual_open_event(
        repo,
        OpenPositionCommand(
            broker="富途",
            account="lx",
            symbol="AAPL",
            option_type="put",
            side="short",
            contracts=1,
            currency="USD",
            strike=150.0,
            multiplier=100,
            expiration_ymd="2026-06-19",
            premium_per_share=1.0,
            opened_at_ms=1000,
        ),
    )
    ledger_manual_trades.persist_manual_open_event(
        repo,
        OpenPositionCommand(
            broker="富途",
            account="lx",
            symbol="NVDA",
            option_type="put",
            side="short",
            contracts=1,
            currency="USD",
            strike=100.0,
            multiplier=100,
            expiration_ymd="2026-06-19",
            premium_per_share=1.1,
            opened_at_ms=1100,
        ),
    )
    lots = repo.list_position_lots()

    with pytest.raises(ValueError, match="manual_close target fields do not match current lot state"):
        ledger_manual_trades.persist_manual_close_event(
            repo,
            record_id=lots[0]["record_id"],
            fields=lots[1]["fields"],
            contracts_to_close=1,
            close_price=0.5,
            close_reason="manual_buy_to_close",
            as_of_ms=2000,
        )


def test_persist_manual_void_event_removes_open_lot_from_projection(tmp_path: Path) -> None:
    from domain.domain.option_position_lots import OpenPositionCommand

    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    open_result = ledger_manual_trades.persist_manual_open_event(
        repo,
        OpenPositionCommand(
            broker="富途",
            account="lx",
            symbol="TSLA",
            option_type="put",
            side="short",
            contracts=1,
            currency="USD",
            strike=100.0,
            multiplier=100,
            expiration_ymd="2026-06-19",
            premium_per_share=1.23,
            opened_at_ms=1000,
        ),
    )

    void_result = ledger_interventions.persist_manual_void_event(
        repo,
        target_event_id=str(open_result["event_id"]),
        void_reason="opened_by_mistake",
        as_of_ms=2000,
    )

    assert repo.list_position_lots() == []
    events = repo.list_trade_events()
    assert len(events) == 2
    assert events[-1]["position_effect"] == "void"
    assert events[-1]["raw_payload"]["void_target_event_id"] == open_result["event_id"]
    assert void_result["position_lot_count"] == 0


def test_persist_manual_void_event_restores_lot_when_voiding_close_event(tmp_path: Path) -> None:
    from domain.domain.option_position_lots import OpenPositionCommand

    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    open_result = ledger_manual_trades.persist_manual_open_event(
        repo,
        OpenPositionCommand(
            broker="富途",
            account="lx",
            symbol="TSLA",
            option_type="put",
            side="short",
            contracts=2,
            currency="USD",
            strike=100.0,
            multiplier=100,
            expiration_ymd="2026-06-19",
            premium_per_share=1.23,
            opened_at_ms=1000,
        ),
    )
    lot = repo.list_position_lots()[0]
    close_result = ledger_manual_trades.persist_manual_close_event(
        repo,
        record_id=lot["record_id"],
        fields=lot["fields"],
        contracts_to_close=1,
        close_price=0.5,
        close_reason="manual_buy_to_close",
        as_of_ms=1500,
    )

    void_result = ledger_interventions.persist_manual_void_event(
        repo,
        target_event_id=str(close_result["event_id"]),
        void_reason="close_recorded_by_mistake",
        as_of_ms=2000,
    )

    rebuilt_lot = repo.list_position_lots()[0]
    assert rebuilt_lot["record_id"] == f"lot_{open_result['event_id']}"
    assert rebuilt_lot["fields"]["contracts_open"] == 2
    assert rebuilt_lot["fields"]["contracts_closed"] == 0
    assert rebuilt_lot["fields"]["status"] == "open"
    assert void_result["position_lot_count"] == 1


def test_persist_manual_adjust_event_updates_position_lot_projection(tmp_path: Path) -> None:
    from domain.domain.option_position_lots import OpenPositionCommand

    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    ledger_manual_trades.persist_manual_open_event(
        repo,
        OpenPositionCommand(
            broker="富途",
            account="lx",
            symbol="NVDA",
            option_type="put",
            side="short",
            contracts=1,
            currency="USD",
            strike=100.0,
            multiplier=100,
            expiration_ymd="2026-06-19",
            premium_per_share=2.5,
            opened_at_ms=1000,
        ),
    )
    lot = repo.list_position_lots()[0]

    result = ledger_manual_trades.persist_manual_adjust_event(
        repo,
        record_id=lot["record_id"],
        fields=lot["fields"],
        contracts=2,
        strike=105.0,
        expiration_ymd="2026-07-17",
        premium_per_share=3.1,
        multiplier=100,
        opened_at_ms=2000,
        as_of_ms=3000,
    )

    adjusted = repo.get_position_lot_fields(lot["record_id"])
    assert result["created"] is True
    assert adjusted["contracts"] == 2
    assert adjusted["contracts_open"] == 2
    assert adjusted["strike"] == 105.0
    assert adjusted["premium"] == 3.1
    assert adjusted["opened_at"] == 2000
    assert adjusted["position_id"] == "NVDA_20260717_105P_short"
    assert adjusted["cash_secured_amount"] == 21000.0


def test_persist_manual_adjust_event_is_idempotent_on_retry(tmp_path: Path) -> None:
    from domain.domain.option_position_lots import OpenPositionCommand

    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    ledger_manual_trades.persist_manual_open_event(
        repo,
        OpenPositionCommand(
            broker="富途",
            account="lx",
            symbol="NVDA",
            option_type="put",
            side="short",
            contracts=1,
            currency="USD",
            strike=100.0,
            multiplier=100,
            expiration_ymd="2026-06-19",
            premium_per_share=2.5,
            opened_at_ms=1000,
        ),
    )
    lot = repo.list_position_lots()[0]

    result1 = ledger_manual_trades.persist_manual_adjust_event(
        repo,
        record_id=lot["record_id"],
        fields=lot["fields"],
        premium_per_share=3.1,
        as_of_ms=2000,
    )
    result2 = ledger_manual_trades.persist_manual_adjust_event(
        repo,
        record_id=lot["record_id"],
        fields=repo.get_position_lot_fields(lot["record_id"]),
        premium_per_share=3.1,
        as_of_ms=3000,
    )

    assert result1["created"] is True
    assert result2["created"] is False
    assert result1["event_id"] == result2["event_id"]
    assert repo.get_position_lot_fields(lot["record_id"])["premium"] == 3.1
    assert len(repo.list_trade_events()) == 2


def test_persist_manual_adjust_event_rejects_mismatched_record_id_and_fields(tmp_path: Path) -> None:
    from domain.domain.option_position_lots import OpenPositionCommand

    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    ledger_manual_trades.persist_manual_open_event(
        repo,
        OpenPositionCommand(
            broker="富途",
            account="lx",
            symbol="AAPL",
            option_type="put",
            side="short",
            contracts=1,
            currency="USD",
            strike=150.0,
            multiplier=100,
            expiration_ymd="2026-06-19",
            premium_per_share=1.0,
            opened_at_ms=1000,
        ),
    )
    ledger_manual_trades.persist_manual_open_event(
        repo,
        OpenPositionCommand(
            broker="富途",
            account="lx",
            symbol="NVDA",
            option_type="put",
            side="short",
            contracts=1,
            currency="USD",
            strike=100.0,
            multiplier=100,
            expiration_ymd="2026-06-19",
            premium_per_share=1.1,
            opened_at_ms=1100,
        ),
    )
    lots = repo.list_position_lots()

    with pytest.raises(ValueError, match="manual_adjust target fields do not match current lot state"):
        ledger_manual_trades.persist_manual_adjust_event(
            repo,
            record_id=lots[0]["record_id"],
            fields=lots[1]["fields"],
            premium_per_share=2.0,
            as_of_ms=2000,
        )


def test_voiding_adjust_event_restores_prior_projection_state(tmp_path: Path) -> None:
    from domain.domain.option_position_lots import OpenPositionCommand

    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    ledger_manual_trades.persist_manual_open_event(
        repo,
        OpenPositionCommand(
            broker="富途",
            account="lx",
            symbol="NVDA",
            option_type="put",
            side="short",
            contracts=1,
            currency="USD",
            strike=100.0,
            multiplier=100,
            expiration_ymd="2026-06-19",
            premium_per_share=2.5,
            opened_at_ms=1000,
        ),
    )
    lot = repo.list_position_lots()[0]
    adjust_result = ledger_manual_trades.persist_manual_adjust_event(
        repo,
        record_id=lot["record_id"],
        fields=lot["fields"],
        premium_per_share=3.1,
        as_of_ms=2000,
    )
    ledger_interventions.persist_manual_void_event(
        repo,
        target_event_id=str(adjust_result["event_id"]),
        void_reason="adjustment_was_wrong",
        as_of_ms=3000,
    )

    restored = repo.get_position_lot_fields(lot["record_id"])
    assert restored["premium"] == 2.5
    assert restored["contracts"] == 1


def test_load_option_positions_repo_ignores_incomplete_feishu_config_when_bootstrap_disabled(tmp_path: Path) -> None:

    data_config = tmp_path / "data.json"
    data_config.write_text(
        json.dumps(
            {
                "option_positions": {"sqlite_path": str(tmp_path / "option_positions.sqlite3")},
                "feishu": {"app_id": "app_only"},
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    repo = ledger_bootstrap.load_option_positions_repo(data_config)

    assert repo.bootstrap_status == "sqlite_only_no_feishu_bootstrap"


def test_load_option_positions_repo_ignores_malformed_feishu_config_when_retired_bootstrap_enabled(tmp_path: Path) -> None:

    data_config = tmp_path / "data.json"
    data_config.write_text(
        json.dumps(
            {
                "option_positions": {
                    "sqlite_path": str(tmp_path / "option_positions.sqlite3"),
                    "bootstrap_from_feishu": {"enabled": True},
                },
                "feishu": {"app_id": "app_only"},
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    repo = ledger_bootstrap.load_option_positions_repo(data_config)

    assert repo.bootstrap_status == "sqlite_only_feishu_bootstrap_retired"


def test_load_option_positions_repo_ignores_non_object_feishu_config_when_bootstrap_disabled(tmp_path: Path) -> None:

    data_config = tmp_path / "data.json"
    data_config.write_text(
        json.dumps(
            {
                "option_positions": {"sqlite_path": str(tmp_path / "option_positions.sqlite3")},
                "feishu": "invalid",
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    repo = ledger_bootstrap.load_option_positions_repo(data_config)

    assert repo.bootstrap_status == "sqlite_only_no_feishu_bootstrap"


def test_load_option_positions_repo_ignores_non_object_feishu_config_when_retired_bootstrap_enabled(tmp_path: Path) -> None:

    data_config = tmp_path / "data.json"
    data_config.write_text(
        json.dumps(
            {
                "option_positions": {
                    "sqlite_path": str(tmp_path / "option_positions.sqlite3"),
                    "bootstrap_from_feishu": {"enabled": True},
                },
                "feishu": "invalid",
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    repo = ledger_bootstrap.load_option_positions_repo(data_config)

    assert repo.bootstrap_status == "sqlite_only_feishu_bootstrap_retired"


def test_option_positions_bootstrap_from_feishu_enabled_defaults_false(tmp_path: Path) -> None:

    data_config = _write_data_config(tmp_path / "data.json", sqlite_path=tmp_path / "option_positions.sqlite3")

    assert ledger_repository.option_positions_bootstrap_from_feishu_enabled(data_config) is False


def test_option_positions_bootstrap_from_feishu_enabled_reads_boolean(tmp_path: Path) -> None:

    data_config = _write_data_config(
        tmp_path / "data.json",
        sqlite_path=tmp_path / "option_positions.sqlite3",
        bootstrap_from_feishu_enabled=True,
    )

    assert ledger_repository.option_positions_bootstrap_from_feishu_enabled(data_config) is False


def test_option_positions_bootstrap_from_feishu_enabled_ignores_retired_config_shape(tmp_path: Path) -> None:

    data_config = tmp_path / "data.json"
    data_config.write_text(
        json.dumps(
            {
                "option_positions": {
                    "sqlite_path": str(tmp_path / "option_positions.sqlite3"),
                    "bootstrap_from_feishu": {"enabled": "yes"},
                }
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    assert ledger_repository.option_positions_bootstrap_from_feishu_enabled(data_config) is False


def test_replace_position_lots_rejects_incomplete_option_lots_atomically(tmp_path: Path) -> None:

    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    repo.replace_position_lots(
        [
            PositionLotRecord(
                record_id="lot_existing",
                fields={
                    "account": "lx",
                    "broker": "富途",
                    "symbol": "0700.HK",
                    "option_type": "put",
                    "side": "short",
                    "contracts": 1,
                    "contracts_open": 1,
                    "expiration": 1782691200000,
                    "strike": 470.0,
                },
            ),
        ]
    )

    try:
        repo.replace_position_lots(
            [
                PositionLotRecord(
                    record_id="lot_bad_option",
                    fields={
                        "account": "lx",
                        "broker": "富途",
                        "symbol": "0700.HK",
                        "option_type": "put",
                        "side": "short",
                        "contracts": 2,
                        "contracts_open": 2,
                        "expiration": "",
                        "strike": None,
                    },
                ),
                PositionLotRecord(
                    record_id="lot_good_option",
                    fields={
                        "account": "lx",
                        "broker": "富途",
                        "symbol": "0700.HK",
                        "option_type": "put",
                        "side": "short",
                        "contracts": 2,
                        "contracts_open": 2,
                        "expiration": 1782691200000,
                        "strike": 480.0,
                    },
                ),
            ]
        )
        raise AssertionError("expected replace_position_lots to reject incomplete option lots")
    except ValueError as exc:
        assert "missing expiration, strike" in str(exc)

    lots = repo.list_position_lots()
    record_ids = {row["record_id"] for row in lots}
    assert record_ids == {"lot_existing"}


def test_replace_position_lots_requires_typed_position_lot_records(tmp_path: Path) -> None:
    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    raw_record: Any = {
        "record_id": "lot_raw",
        "fields": {
            "account": "lx",
            "broker": "富途",
            "symbol": "0700.HK",
            "option_type": "put",
            "side": "short",
            "contracts": 1,
            "contracts_open": 1,
            "expiration": 1782691200000,
            "strike": 470.0,
        },
    }

    with pytest.raises(TypeError, match="requires PositionLotRecord records"):
        repo.replace_position_lots([raw_record])

    assert repo.list_position_lots() == []


def test_projection_replay_fixture_closes_lot_and_excludes_it_from_open_context(tmp_path: Path) -> None:
    from src.application.positions.context_builder import build_context
    from tests.ledger_legacy_helpers import LegacyTradeEvent as TradeEvent

    fixture_path = BASE / "tests" / "fixtures" / "option_positions_projection_replay_case.json"
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    for raw_event in fixture["events"]:
        ledger_writer.persist_trade_event_object(repo, TradeEvent(**raw_event))

    rebuild_result = ledger_writer.rebuild_position_lots_from_trade_events(repo)

    lots = repo.list_position_lots()
    assert len(lots) == 1
    assert lots[0]["record_id"] == fixture["expected"]["record_id"]
    assert lots[0]["fields"]["contracts_open"] == 0
    assert lots[0]["fields"]["contracts_closed"] == 2
    assert lots[0]["fields"]["status"] == "close"
    assert rebuild_result["unmatched_explicit_close_count"] == 0

    context = build_context(lots, broker="富途", account="sy", rates={})
    assert context["open_positions_min"] == []


def test_projection_matches_explicit_close_with_legacy_hk_symbol_alias() -> None:
    from tests.ledger_legacy_helpers import LegacyTradeEvent as TradeEvent, project_position_lot_records_with_diagnostics

    events = [
        TradeEvent(
            event_id="bootstrap:hk:legacy-700",
            source_type="bootstrap_snapshot",
            source_name="feishu_bootstrap",
            broker="富途",
            account="lx",
            symbol="00700.HK",
            option_type="put",
            side="sell",
            position_effect="open",
            contracts=1,
            price=1.0,
            strike=480.0,
            multiplier=100,
            expiration_ymd="2026-04-29",
            currency="HKD",
            trade_time_ms=1000,
            order_id=None,
            multiplier_source="bootstrap_snapshot",
            raw_payload={
                "lot_record_id": "rec_legacy_700",
                "fields": {
                    "broker": "富途",
                    "account": "lx",
                    "symbol": "00700.HK",
                    "option_type": "put",
                    "side": "short",
                    "contracts": 1,
                    "contracts_open": 1,
                    "contracts_closed": 0,
                    "status": "open",
                    "currency": "HKD",
                    "strike": 480.0,
                    "expiration": 1777420800000,
                    "opened_at": 1000,
                    "last_action_at": 1000,
                    "position_id": "00700.HK_20260429_480P_short",
                    "note": "exp=2026-04-29;premium_per_share=1.0",
                    "premium": 1.0,
                },
            },
        ),
        TradeEvent(
            event_id="manual-close-legacy-700",
            source_type="manual_trade_event",
            source_name="cli_manual_close",
            broker="富途",
            account="lx",
            symbol="00700.HK",
            option_type="put",
            side="buy",
            position_effect="close",
            contracts=1,
            price=0.2,
            strike=480.0,
            multiplier=100,
            expiration_ymd="2026-04-29",
            currency="HKD",
            trade_time_ms=2000,
            order_id=None,
            multiplier_source="payload",
            raw_payload={
                "source": "option_positions.py",
                "mode": "manual_close",
                "record_id": "rec_legacy_700",
                "close_reason": "manual_buy_to_close",
            },
        ),
    ]

    projection = project_position_lot_records_with_diagnostics(events)

    assert projection.lots[0].fields["contracts_open"] == 0
    assert projection.lots[0].fields["contracts_closed"] == 1
    assert projection.lots[0].fields["status"] == "close"
    assert [item.code for item in projection.diagnostics] == []
