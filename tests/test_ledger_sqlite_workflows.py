from __future__ import annotations

import json
import shutil
import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest  # pyright: ignore[reportMissingImports]

from domain.domain.ledger import ContractKey, TradeEvent, fee_fact_for_event
from src.application.trades.normalizer import NormalizedTradeDeal
from tests.ledger_legacy_helpers import LegacyTradeEvent
import src.application.ledger.bootstrap as bootstrap
import src.application.ledger.bootstrap as ledger_bootstrap
import src.application.ledger.interventions as ledger_interventions
import src.application.ledger.manual_trades as ledger_manual_trades
from src.application.ledger.position_records import PositionLotRecord
import src.application.ledger.repository as ledger_repository
from src.application.ledger.store_resolution import ledger_store_write_guard, resolve_ledger_store
import src.application.ledger.writer as ledger_writer

BASE = Path(__file__).resolve().parents[1]


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


def _contract_key(**overrides: Any) -> ContractKey:
    base: dict[str, Any] = {
        "broker": "富途",
        "account": "lx",
        "underlying_symbol": "NVDA",
        "option_type": "put",
        "strike": 100,
        "expiration_ymd": "2026-08-21",
    }
    base.update(overrides)
    return ContractKey.from_values(**base)


def _trade_event(**overrides: Any) -> TradeEvent:
    base: dict[str, Any] = {
        "event_id": "open-aapl",
        "event_type": "open",
        "event_time_ms": 1000,
        "contract_key": _contract_key(),
        "contracts": 1,
        "price": 1.0,
        "currency": "USD",
        "source": "test",
        "multiplier": 100,
        "lot_id": "lot-aapl",
        # §9.2 step 3: the default event is a short put open, so the side
        # travels in raw_payload.
        "raw_payload": {"side": "sell"},
    }
    base.update(overrides)
    return TradeEvent(**base)


def _seed_fields(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
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
        "position_key": "AAPL_20260619_150P_short",
        "note": "exp=2026-06-19;premium_per_share=1.0",
        "premium": 1.0,
    }
    base.update(overrides)
    return base


def _legacy_event(**overrides: Any) -> LegacyTradeEvent:
    base: dict[str, Any] = {
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
    base.update(overrides)
    return LegacyTradeEvent(**base)


def _bootstrap_event(**overrides: Any) -> LegacyTradeEvent:
    base: dict[str, Any] = {
        "event_id": "bootstrap:lx:seed",
        "source_type": "bootstrap_snapshot",
        "source_name": "feishu_bootstrap",
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
        "order_id": None,
        "multiplier_source": "bootstrap_snapshot",
        "raw_payload": {"lot_record_id": "rec_lx_seed", "fields": _seed_fields()},
    }
    base.update(overrides)
    return LegacyTradeEvent(**base)


def _manual_close_event(**overrides: Any) -> LegacyTradeEvent:
    base: dict[str, Any] = {
        "event_id": "manual-close-rec-lx-seed",
        "source_type": "manual_trade_event",
        "source_name": "cli_manual_close",
        "broker": "富途",
        "account": "lx",
        "symbol": "AAPL",
        "option_type": "put",
        "side": "buy",
        "position_effect": "close",
        "contracts": 1,
        "price": 0.0,
        "strike": 150.0,
        "multiplier": 100,
        "expiration_ymd": "2026-06-19",
        "currency": "USD",
        "trade_time_ms": 2000,
        "order_id": None,
        "multiplier_source": "payload",
        "raw_payload": {
            "source": "option_positions.py",
            "mode": "manual_close",
            "record_id": "rec_lx_seed",
            "close_reason": "expired",
        },
    }
    base.update(overrides)
    return LegacyTradeEvent(**base)


def _open_kwargs(**overrides: Any) -> dict[str, Any]:
    """Keyword arguments for ``persist_manual_open_event`` (its retired command object is gone)."""

    base: dict[str, Any] = {
        "broker": "富途",
        "account": "lx",
        "symbol": "NVDA",
        "option_type": "put",
        "side": "short",
        "contracts": 1,
        "currency": "USD",
        "strike": 100.0,
        "multiplier": 100,
        "expiration_ymd": "2026-06-19",
        "premium_per_share": 2.5,
        "opened_at_ms": 1000,
    }
    base.update(overrides)
    return base


def _deal(**overrides: Any) -> NormalizedTradeDeal:
    base: dict[str, Any] = {
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
        "multiplier_source": "payload",
        "expiration_ymd": "2026-04-29",
        "currency": "HKD",
        "trade_time_ms": 1000,
        "raw_payload": {"deal_id": "deal-open-1"},
    }
    base.update(overrides)
    return NormalizedTradeDeal(**base)


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
    assert not hasattr(resolution, "legacy_sqlite_path")
    assert resolution.warnings == ()


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
    assert not hasattr(resolution, "legacy_sqlite_path")
    assert resolution.warnings == ()


def test_ledger_store_write_guard_fails_when_active_empty_but_systemd_default_populated(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import src.application.ledger.store_resolution as store_resolution

    data_config = tmp_path / "release" / "portfolio.runtime.json"
    data_config.parent.mkdir(parents=True, exist_ok=True)
    data_config.write_text("{}", encoding="utf-8")
    systemd_root = tmp_path / "var-lib-options-monitor"
    monkeypatch.setattr(store_resolution, "REPO_BASE", data_config.parent)
    monkeypatch.setattr(store_resolution, "SYSTEMD_DEFAULT_RUNTIME_ROOT", systemd_root)
    repo = ledger_repository.SQLiteOptionPositionsRepository(
        systemd_root / "output_shared" / "state" / "option_positions.sqlite3"
    )
    ledger_manual_trades.persist_manual_open_event(
        repo,
        **_open_kwargs(
            symbol="0700.HK",
            option_type="call",
            contracts=2,
            currency="HKD",
            strike=510.0,
            expiration_ymd="2026-05-28",
            premium_per_share=1.2,
        ),
    )

    guard = ledger_store_write_guard(data_config)

    assert guard["ok"] is False
    assert any("active ledger SQLite is empty" in item for item in guard["errors"])
    assert guard["summary"]["active_empty_but_other_populated"] is True


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
                    "option_type": "call",
                    "side": "short",
                    "strike": 500,
                    "expiration_ymd": "2026-04-29",
                    "status": "open",
                    "contracts": 1,
                    "contracts_open": 1,
                    "premium": 3.5,
                    "currency": "HKD",
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


def test_bootstrap_seeds_the_strategy_family_onto_the_import_event() -> None:
    """An imported lot's family has to survive on the event it was imported as.

    The family is the one fact this batch declares RECONSTRUCTIBLE from the
    event layer (``write-side-definition.md`` §2), and the read side reads it
    off the payload's top level
    (``wheel.lot_strategy_metadata_from_trade_events``). Seeding the whole
    legacy row is what the payload note rules out — an open set of historical
    spellings would ride into a new payload — and this is not that: the family
    is a declared key set, and without it an imported lot has no carrier left
    for its family once the payload keys are dropped.
    """

    from domain.domain.wheel import lot_strategy_metadata_from_trade_events

    events = bootstrap._bootstrap_trade_events(  # type: ignore[attr-defined]
        [
            {
                "record_id": "rec_wheel",
                "fields": {
                    "account": "lx",
                    "broker": "futu",
                    "symbol": "NVDA",
                    "option_type": "put",
                    "side": "short",
                    "strike": 100.0,
                    "expiration_ymd": "2026-06-19",
                    "status": "open",
                    "contracts": 1,
                    "contracts_open": 1,
                    "premium": 2.5,
                    "currency": "USD",
                    "opened_at": 1000,
                    # The retired flat vocabulary rides along, and the payload
                    # must still not carry it verbatim.
                    "exp": "20260619",
                    "underlying_shares_locked": 100,
                    "strategy": "wheel",
                    "leg_role": "short_put",
                    "strategy_group_id": "group-a",
                    "strategy_snapshot": {"strategy": "wheel", "leg_role": "short_put"},
                },
            }
        ],
        source_name="test_bootstrap",
    )

    assert len(events) == 1
    payload = events[0].raw_payload
    assert "fields" not in payload
    assert "exp" not in payload and "underlying_shares_locked" not in payload
    assert payload["strategy"] == "wheel"
    assert payload["leg_role"] == "short_put"
    assert payload["strategy_group_id"] == "group-a"
    assert payload["strategy_snapshot"] == {"strategy": "wheel", "leg_role": "short_put"}
    # The read side is the half that has to find it again.
    metadata = lot_strategy_metadata_from_trade_events(
        [
            {
                "event_id": events[0].event_id,
                "event_type": "open",
                "lot_id": events[0].lot_id,
                "raw_payload": payload,
            }
        ]
    )
    assert metadata["rec_wheel"]["strategy"] == "wheel"
    assert metadata["rec_wheel"]["strategy_snapshot"] == {
        "strategy": "wheel",
        "leg_role": "short_put",
    }


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
                    json.dumps(
                        {
                            "account": "lx",
                            "broker": "富途",
                            "symbol": "AAPL",
                            "option_type": "put",
                            "side": "short",
                            "strike": 100,
                            "expiration_ymd": "2026-06-19",
                            "status": "open",
                            "contracts": 1,
                            "contracts_open": 1,
                            "premium": 1.2,
                            "currency": "USD",
                            "opened_at": 1000,
                        },
                        ensure_ascii=False,
                    ),
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
                json.dumps(
                    {
                        "account": "lx",
                        "broker": "富途",
                        "symbol": "AAPL",
                        "option_type": "put",
                        "side": "short",
                        "strike": 100,
                        "expiration_ymd": "2026-06-19",
                        "status": "open",
                        "contracts": 1,
                        "contracts_open": 1,
                        "premium": 1.2,
                        "currency": "USD",
                        "opened_at": 1000,
                    },
                    ensure_ascii=False,
                ),
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

    assert not hasattr(ledger_bootstrap, "migrate_legacy_sqlite_to_repo")
    assert rows == []
    assert loaded.db_path != db_path.resolve()
    assert loaded.count_trade_events() == 0
    assert loaded.bootstrap_status == "sqlite_only_no_feishu_bootstrap"


def test_migrate_legacy_sqlite_prefers_legacy_trade_events_explicitly(tmp_path: Path) -> None:
    legacy_db = tmp_path / "legacy" / "option_positions.sqlite3"
    legacy_repo = ledger_repository.SQLiteOptionPositionsRepository(legacy_db)
    legacy_event = _legacy_event(
        event_id="deal-open-legacy",
        source_name="legacy_sqlite",
        account="sy",
        contracts=2,
        price=1.25,
        order_id="order-legacy",
        raw_payload={"deal_id": "deal-open-legacy"},
    )
    with legacy_repo._connect() as conn:  # type: ignore[attr-defined]
        for trigger_name in ledger_repository.TRADE_EVENT_PAGINATION_TRIGGERS:
            conn.execute(f"DROP TRIGGER IF EXISTS {trigger_name}")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS trade_events (
              event_id TEXT PRIMARY KEY,
              trade_time_ms INTEGER NOT NULL,
              event_json TEXT NOT NULL,
              created_at_ms INTEGER NOT NULL,
              updated_at_ms INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO trade_events (event_id, trade_time_ms, event_json, created_at_ms, updated_at_ms)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                legacy_event.event_id,
                legacy_event.trade_time_ms,
                json.dumps(legacy_event.to_legacy_dict(), ensure_ascii=False),
                1000,
                1000,
            ),
        )
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
    assert not hasattr(ledger_bootstrap, "migrate_legacy_sqlite_to_repo")
    assert loaded.list_trade_events() == []
    assert loaded.list_position_lots() == []
    assert loaded.bootstrap_status == "sqlite_only_no_feishu_bootstrap"


def test_migrate_legacy_sqlite_reports_missing_legacy_sqlite_explicitly(tmp_path: Path) -> None:
    legacy_db = tmp_path / "missing" / "option_positions.sqlite3"
    data_config = _write_data_config(
        tmp_path / "data.json",
        sqlite_path=legacy_db,
        with_feishu=False,
        copy_legacy_to_standard=False,
    )

    loaded = ledger_bootstrap.load_option_positions_repo(data_config)

    assert not hasattr(ledger_bootstrap, "migrate_legacy_sqlite_to_repo")
    assert loaded.count_trade_events() == 0
    assert loaded.count_position_lots() == 0


def test_load_option_positions_repo_reports_position_lots_without_trade_events(tmp_path: Path) -> None:
    db_path = tmp_path / "option_positions.sqlite3"
    repo = ledger_repository.SQLiteOptionPositionsRepository(db_path)
    repo.replace_position_lots(
        [
            PositionLotRecord(
                lot_id="rec_bootstrap_1",
                # §1/§3: a seeded row carries the converged payload, contract under
                # ``contract_key``.
                fields={
                    "lot_id": "rec_bootstrap_1",
                    "open_event_id": "bootstrap:sy:rec_bootstrap_1",
                    "contract_key": {
                        "broker": "富途",
                        "account": "sy",
                        "underlying_symbol": "TSLA",
                        "option_type": "put",
                        "strike": "180",
                        "expiration_ymd": "2026-06-19",
                        "asset_type": "option",
                    },
                    "position_side": "short",
                    "position_key": "富途|sy|TSLA|2026-06-19|180P|short",
                    "opened_at_ms": 1000,
                    "contracts_opened": 2,
                    "contracts_open": 2,
                    "contracts_closed": 0,
                    "status": "open",
                    "currency": "USD",
                    "premium_open": "1.2",
                    "multiplier": 100,
                    "realized_pnl": "0",
                    "last_event_id": "bootstrap:sy:rec_bootstrap_1",
                    "close_event_ids": [],
                    "asset_type": "option",
                },
            )
        ]
    )
    data_config = _write_data_config(tmp_path / "data.json", sqlite_path=db_path, with_feishu=False)

    loaded = ledger_bootstrap.load_option_positions_repo(data_config)

    assert loaded.count_trade_events() == 0
    assert loaded.bootstrap_status == "sqlite_only_position_lots_without_trade_events"
    assert "repair the active ledger" in str(loaded.bootstrap_message)
    rows = loaded.list_position_lots()
    assert len(rows) == 1
    assert rows[0]["record_id"] == "rec_bootstrap_1"
    assert rows[0]["fields"]["contract_key"]["underlying_symbol"] == "TSLA"


def test_canonical_seed_lot_survives_later_trade_event_projection(tmp_path: Path) -> None:
    db_path = tmp_path / "output_shared" / "state" / "option_positions.sqlite3"
    data_config = _write_data_config(tmp_path / "data.json", sqlite_path=db_path, with_feishu=False)
    repo = ledger_bootstrap.load_option_positions_repo(data_config)
    ledger_writer.persist_trade_event_object(
        repo,
        _trade_event(
            event_id="seed-rec-sy",
            contract_key=_contract_key(
                account="sy",
                underlying_symbol="AAPL",
                strike=150.0,
                expiration_ymd="2026-06-19",
            ),
            source="test_seed_open_lot",
            lot_id="rec_sy_seed",
            # §9.2 step 3: the short put side travels as the trade side.
            raw_payload={"side": "sell"},
        ),
    )

    assert repo.count_trade_events() == 1
    open_deal = _deal(
        deal_id="deal-open-2",
        order_id="order-2",
        contracts=1,
        price=3.2,
        strike=420.0,
        trade_time_ms=2000,
        raw_payload={"deal_id": "deal-open-2"},
    )

    ledger_writer.persist_trade_event(repo, open_deal)

    lots = repo.list_position_lots()
    lot_ids = {row["record_id"] for row in lots}
    assert "rec_sy_seed" in lot_ids
    assert "lot_futu:lx:REAL_1:deal-open-2" in lot_ids


def test_load_option_positions_repo_supports_sqlite_only_mode(tmp_path: Path) -> None:
    data_config = _write_data_config(
        tmp_path / "data.json",
        sqlite_path=tmp_path / "option_positions.sqlite3",
        with_feishu=False,
    )
    repo = ledger_bootstrap.load_option_positions_repo(data_config)
    created = ledger_manual_trades.persist_manual_open_event(
        repo,
        **_open_kwargs(symbol="TSLA", premium_per_share=1.23),
    )

    records = repo.list_records(page_size=10)
    assert len(records) == 1
    assert records[0]["fields"]["contract_key"]["underlying_symbol"] == "TSLA"
    assert created.created is True
    assert repo.bootstrap_status == "sqlite_only_no_feishu_bootstrap"


def test_load_option_positions_repo_treats_holdings_only_feishu_as_sqlite_only(tmp_path: Path) -> None:
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


def test_load_option_positions_repo_does_not_validate_position_lots_without_trade_events(tmp_path: Path) -> None:
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

    assert loaded.bootstrap_status == "sqlite_only_position_lots_without_trade_events"
    assert "repair the active ledger" in str(loaded.bootstrap_message)
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
    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    event = _legacy_event(event_id="deal-open-1", raw_payload={"deal_id": "deal-open-1"})

    assert repo.upsert_trade_event(event) is True
    assert repo.upsert_trade_event(event) is False
    with pytest.raises(ValueError, match="trade event conflict"):
        repo.upsert_trade_event(replace(event, price=2.0))

    events = repo.list_trade_events()
    assert len(events) == 1
    assert events[0]["event_id"] == "deal-open-1"
    assert events[0]["price"] == "1"


def test_persist_trade_event_builds_position_lots_projection(tmp_path: Path) -> None:
    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    open_deal = _deal()
    close_deal = _deal(
        deal_id="deal-close-1",
        order_id="order-2",
        side="buy",
        position_effect="close",
        contracts=1,
        price=1.2,
        trade_time_ms=2000,
        raw_payload={"deal_id": "deal-close-1"},
    )

    first = ledger_writer.persist_trade_event(repo, open_deal)
    second = ledger_writer.persist_trade_event(repo, close_deal)

    assert first.created is True
    assert second.created is True
    events = repo.list_trade_events()
    open_event_id = "futu:lx:REAL_1:deal-open-1"
    close_event_id = "futu:lx:REAL_1:deal-close-1"
    assert [row["event_id"] for row in events] == [open_event_id, close_event_id]
    assert [row["raw_payload"]["source_deal_id"] for row in events] == ["deal-open-1", "deal-close-1"]

    lots = repo.list_position_lots()
    assert len(lots) == 1
    fields = lots[0]["fields"]
    assert fields["open_event_id"] == open_event_id
    assert fields["contracts_opened"] == 2
    assert fields["contracts_open"] == 1
    assert fields["contracts_closed"] == 1
    assert fields["status"] == "open"
    # §2: ``last_close_event_id`` converged onto ``close_event_ids`` /
    # ``last_event_id``. The close transition itself retains the closing ids
    # (``retain_close_event_ids=True``), so a partially closed lot publishes them
    # and the retired flat spelling is not resurrected either way.
    assert "last_close_event_id" not in fields
    assert fields["close_event_ids"] == [close_event_id]
    assert fields["last_event_id"] == close_event_id
    # §7.4: the published row carries money as decimal text.
    assert fields["contract_key"]["strike"] == "480"
    assert fields["contract_key"]["expiration_ymd"] == "2026-04-29"
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


def test_generic_event_writer_rolls_back_event_when_projection_is_invalid(tmp_path: Path) -> None:
    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    key = _contract_key(underlying_symbol="AAPL", strike=150.0, expiration_ymd="2026-06-19")
    ledger_writer.persist_trade_event_object(
        repo,
        _trade_event(contract_key=key),
    )
    ledger_writer.persist_trade_event_object(
        repo,
        _trade_event(
            event_id="close-aapl",
            event_type="close",
            event_time_ms=2000,
            contract_key=key,
            price=0.5,
            lot_id=None,
            target_lot_id="lot-aapl",
            # §9.2 step 3: the short put side travels as the trade side.
            raw_payload={"side": "buy"},
        ),
    )

    with pytest.raises(ValueError, match="target_lot_already_closed"):
        ledger_writer.persist_trade_event_object(
            repo,
            _trade_event(
                event_id="duplicate-close-aapl",
                event_type="close",
                event_time_ms=3000,
                contract_key=key,
                price=0.4,
                lot_id=None,
                target_lot_id="lot-aapl",
                # §9.2 step 3: the short put side travels as the trade side.
                raw_payload={"side": "buy"},
            ),
        )

    assert [event["event_id"] for event in repo.list_trade_events()] == [
        "open-aapl",
        "close-aapl",
    ]
    assert repo.list_position_lots()[0]["fields"]["status"] == "close"


def test_rebuild_preserves_existing_lots_when_event_history_is_invalid(tmp_path: Path) -> None:
    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    key = _contract_key(underlying_symbol="AAPL", strike=150.0, expiration_ymd="2026-06-19")
    ledger_writer.persist_trade_event_object(
        repo,
        _trade_event(contract_key=key),
    )
    original_lots = repo.list_position_lots()
    repo.upsert_trade_event(
        _trade_event(
            event_id="orphan-close-aapl",
            event_type="close",
            event_time_ms=2000,
            contract_key=key,
            price=0.5,
            source="legacy-corruption-fixture",
            lot_id=None,
            target_lot_id="lot-missing",
            # §9.2 step 3: the short put side travels as the trade side.
            raw_payload={"side": "buy"},
        )
    )

    with pytest.raises(ValueError, match="target_lot_not_found"):
        ledger_writer.rebuild_position_lots_from_trade_events(repo)

    assert repo.list_position_lots() == original_lots


def test_persist_trade_event_keys_api_deals_by_account_and_futu_account(tmp_path: Path) -> None:
    from dataclasses import replace

    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    base_deal = _deal(
        deal_id="same-deal-id",
        symbol="NVDA",
        contracts=1,
        price=1.2,
        strike=100.0,
        expiration_ymd="2026-06-19",
        currency="USD",
        raw_payload={"deal_id": "same-deal-id"},
    )

    ledger_writer.persist_trade_event(repo, base_deal)
    ledger_writer.persist_trade_event(
        repo,
        replace(
            base_deal,
            futu_account_id="REAL_2",
            internal_account="sy",
            order_id="order-2",
            trade_time_ms=2000,
        ),
    )

    events = repo.list_trade_events()
    assert [item["event_id"] for item in events] == [
        "futu:lx:REAL_1:same-deal-id",
        "futu:sy:REAL_2:same-deal-id",
    ]
    assert {item["raw_payload"]["source_deal_id"] for item in events} == {"same-deal-id"}


def test_sqlite_repo_adds_position_lot_contract_columns_without_startup_backfill(tmp_path: Path) -> None:
    """The columns are added empty, then filled by an explicit backfill.

    A legacy flat payload migrates through the nested-first-plus-flat-fallback
    read (``repository_common._position_lot_contract_scalars`` derives
    ``expiration``/``strike`` from the flat siblings when ``contract_key`` is
    absent). A note-only scalar does not migrate: ``note`` is display text, not
    a fact source, so ``multiplier=100`` leaves the column NULL and the
    migration gate flags the row as a blocker instead
    (``tests/test_lot_identity_migration.py::test_a_note_only_scalar_blocks_even_with_a_populated_column``
    pins that half). A converged row is unaffected
    (``tests/test_position_projection_publication.py`` pins that half).
    """

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
    assert "multiplier" not in lot["fields"]

    with repo._connect() as conn:  # type: ignore[attr-defined]
        cols = {str(row["name"]) for row in conn.execute("PRAGMA table_info(position_lots)").fetchall()}
        row = conn.execute(
            "SELECT expiration, strike, multiplier FROM position_lots WHERE record_id = ?",
            ("lot_legacy_1",),
        ).fetchone()
    assert {"expiration", "strike", "multiplier"} <= cols
    assert row is not None
    assert row["expiration"] is None
    assert row["strike"] is None
    assert row["multiplier"] is None

    assert repo.backfill_position_lot_contract_columns() == 1
    with repo._connect() as conn:  # type: ignore[attr-defined]
        migrated = conn.execute(
            "SELECT expiration, strike, multiplier FROM position_lots WHERE record_id = ?",
            ("lot_legacy_1",),
        ).fetchone()
    assert migrated is not None
    assert migrated["expiration"] == 1781827200000
    assert migrated["strike"] == 100.0
    assert migrated["multiplier"] is None


def test_reopening_replaces_a_stale_flat_only_account_guard(tmp_path: Path) -> None:
    """A pre-convergence account-guard body must not outlive a reopen.

    ``CREATE TRIGGER IF NOT EXISTS`` is a no-op on a store that already
    materialized the flat-only body, so without the drop-if-stale replacement
    a converged payload (account only under ``contract_key``) makes the flat
    extract coalesce to ``''`` and every write ABORTs with 'position lot
    account is required' -- the deployment blocker the schema guard fixes.
    """

    converged_fields = json.dumps(
        {
            "contract_key": {
                "broker": "futu",
                "account": "lx",
                "underlying_symbol": "TSLA",
                "option_type": "put",
                "strike": "100",
                "expiration_ymd": "2026-06-18",
                "asset_type": "option",
            },
            "position_side": "short",
            "position_key": "futu:lx:TSLA:put:100:2026-06-18:short",
            "status": "open",
        },
        ensure_ascii=False,
    )
    old_guard_body = """
        CREATE TRIGGER trg_position_lots_account_insert_guard
        BEFORE INSERT ON position_lots
        BEGIN
          SELECT CASE
            WHEN json_valid(NEW.fields_json) = 0 THEN RAISE(ABORT, 'invalid position lot JSON')
            WHEN coalesce(trim(CAST(json_extract(NEW.fields_json, '$.account') AS TEXT)), '') = ''
              THEN RAISE(ABORT, 'position lot account is required')
          END;
        END
    """
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
        conn.execute(old_guard_body)
        conn.commit()
        # Pre-condition: the stale flat-only body rejects the converged payload.
        with pytest.raises(sqlite3.IntegrityError, match="position lot account is required"):
            conn.execute(
                """
                INSERT INTO position_lots (record_id, fields_json, source_event_id, updated_at_ms)
                VALUES ('lot_new_1', ?, 'evt-open-1', 1000)
                """,
                (converged_fields,),
            )

    repo = ledger_repository.SQLiteOptionPositionsRepository(db_path)
    with repo._connect() as conn:  # type: ignore[attr-defined]
        body = str(
            conn.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = ?",
                ("trg_position_lots_account_insert_guard",),
            ).fetchone()["sql"]
        )
        assert "$.contract_key.account" in body
        conn.execute(
            """
            INSERT INTO position_lots (record_id, fields_json, source_event_id, updated_at_ms)
            VALUES ('lot_new_1', ?, 'evt-open-1', 1000)
            """,
            (converged_fields,),
        )
        # The same replacement covers the update guard: a converged rewrite passes.
        update_body = str(
            conn.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = ?",
                ("trg_position_lots_account_update_guard",),
            ).fetchone()["sql"]
        )
        assert "$.contract_key.account" in update_body
        conn.execute(
            "UPDATE position_lots SET fields_json = ? WHERE record_id = 'lot_new_1'",
            (converged_fields,),
        )
    # The replacement fires once: a second reopen of the converged store must
    # not drop/recreate anything and leave the schema cookie untouched (the
    # migration inventory fingerprint keys off it).
    cookie = repo.position_projection_schema_cookie()
    reopened = ledger_repository.SQLiteOptionPositionsRepository(db_path)
    assert reopened.position_projection_schema_cookie() == cookie


def test_rebuild_position_lots_applies_legacy_manual_close_to_bootstrap_seed(tmp_path: Path) -> None:
    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    ledger_writer.persist_trade_event_object(
        repo,
        _bootstrap_event()
    )
    ledger_writer.persist_trade_event_object(
        repo,
        _manual_close_event()
    )

    result = ledger_writer.rebuild_position_lots_from_trade_events(repo)

    lots = repo.list_position_lots()
    assert result.trade_event_count == 2
    assert result.position_lot_count == 1
    assert lots[0]["record_id"] == "rec_lx_seed"
    assert lots[0]["fields"]["contracts_open"] == 0
    assert lots[0]["fields"]["contracts_closed"] == 1
    assert lots[0]["fields"]["status"] == "close"
    assert lots[0]["fields"]["last_event_id"] == "manual-close-rec-lx-seed"
    assert result.unmatched_explicit_close_count == 0


def test_rebuild_position_lots_closes_bootstrap_seed_by_record_id_even_if_live_projection_source_event_id_drifted(
    tmp_path: Path,
) -> None:
    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    ledger_writer.persist_trade_event_object(
        repo,
        _bootstrap_event(),
    )
    ledger_writer.persist_trade_event_object(
        repo,
        _manual_close_event(
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
    assert result.unmatched_explicit_close_count == 0


def test_close_projection_does_not_cross_match_other_account_seed_lot(tmp_path: Path) -> None:
    from tests.ledger_legacy_helpers import project_position_lot_records

    events = [
        _bootstrap_event(
            event_id="bootstrap:sy:seed",
            account="sy",
            raw_payload={"lot_record_id": "rec_sy_seed", "fields": _seed_fields(account="sy")},
        ),
        _legacy_event(
            event_id="deal-close-lx-only",
            side="buy",
            position_effect="close",
            price=0.5,
            trade_time_ms=2000,
            order_id="order-close-lx",
            raw_payload={"deal_id": "deal-close-lx-only"},
        ),
    ]

    lots = project_position_lot_records(events)

    assert len(lots) == 1
    assert lots[0].lot_id == "rec_sy_seed"
    assert lots[0].fields["contract_key"]["account"] == "sy"
    assert lots[0].fields["contracts_open"] == 1
    assert lots[0].fields["contracts_closed"] == 0
    assert lots[0].fields["status"] == "open"


def test_close_projection_prefers_structured_expiration_over_missing_note_exp() -> None:
    from tests.ledger_legacy_helpers import project_position_lot_records

    events = [
        _bootstrap_event(
            source_name="sqlite_position_lots",
            contracts=2,
            raw_payload={
                "lot_record_id": "rec_lx_seed",
                "fields": _seed_fields(contracts=2, contracts_open=2, note="premium_per_share=1.0"),
            },
        ),
        _legacy_event(
            event_id="deal-close-lx-exp-structured",
            side="buy",
            position_effect="close",
            price=0.5,
            trade_time_ms=2000,
            order_id="order-close-lx-exp-structured",
            raw_payload={"deal_id": "deal-close-lx-exp-structured", "record_id": "rec_lx_seed"},
        ),
    ]

    lots = project_position_lot_records(events)

    assert len(lots) == 1
    assert lots[0].lot_id == "rec_lx_seed"
    assert lots[0].fields["contracts_open"] == 1
    assert lots[0].fields["contracts_closed"] == 1
    assert lots[0].fields["last_event_id"] == "deal-close-lx-exp-structured"


def test_close_projection_buy_side_closes_the_lot_without_publishing_close_type() -> None:
    from tests.ledger_legacy_helpers import LegacyTradeEvent as TradeEvent, project_position_lot_records

    events = [
        _legacy_event(order_id="order-open-1"),
        _legacy_event(
            event_id="close-1",
            side="buy",
            position_effect="close",
            price=0.5,
            trade_time_ms=2000,
            order_id="order-close-1",
            raw_payload={"deal_id": "close-1", "record_id": "lot_open-1"},
        ),
    ]

    lots = project_position_lot_records(events)

    assert len(lots) == 1
    assert lots[0].fields["contracts_open"] == 0
    assert lots[0].fields["contracts_closed"] == 1
    assert lots[0].fields["status"] == "close"
    assert lots[0].fields["last_event_id"] == "close-1"
    # §2 RECONSTRUCTIBLE: ``close_type`` left the payload; the close event's trade
    # side is its home now (``normalize_close_type`` in
    # ``tests/test_option_positions_domain.py`` pins the mapping itself).
    assert "close_type" not in lots[0].fields


def test_close_projection_matches_bootstrap_lot_by_legacy_record_id() -> None:
    from tests.ledger_legacy_helpers import project_position_lot_records

    events = [
        _bootstrap_event(
            source_name="sqlite_position_lots",
            contracts=2,
            raw_payload={
                "lot_record_id": "rec_lx_seed",
                "fields": _seed_fields(contracts=2, contracts_open=2, note="premium_per_share=1.0"),
            },
        ),
        _manual_close_event(contracts=2),
    ]

    lots = project_position_lot_records(events)

    assert len(lots) == 1
    assert lots[0].lot_id == "rec_lx_seed"
    assert lots[0].fields["contracts_open"] == 0
    assert lots[0].fields["contracts_closed"] == 2
    assert lots[0].fields["status"] == "close"
    assert lots[0].fields["last_event_id"] == "manual-close-rec-lx-seed"


def test_close_projection_prefers_explicit_source_event_target() -> None:
    from tests.ledger_legacy_helpers import project_position_lot_records

    events = [
        _legacy_event(),
        _legacy_event(
            event_id="open-2",
            price=1.1,
            trade_time_ms=1100,
            order_id="order-2",
            raw_payload={"deal_id": "open-2"},
        ),
        _manual_close_event(
            event_id="manual-close-target-open-2",
            price=0.2,
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
    lots_by_id = {record.lot_id: record.fields for record in lots}

    assert lots_by_id["lot_open-1"]["contracts_open"] == 1
    assert lots_by_id["lot_open-1"]["contracts_closed"] == 0
    assert lots_by_id["lot_open-2"]["contracts_open"] == 0
    assert lots_by_id["lot_open-2"]["contracts_closed"] == 1
    assert lots_by_id["lot_open-2"]["last_event_id"] == "manual-close-target-open-2"


def test_close_projection_does_not_fallback_when_explicit_target_is_missing() -> None:
    from tests.ledger_legacy_helpers import project_position_lot_records

    events = [
        _legacy_event(),
        _manual_close_event(
            event_id="manual-close-missing-target",
            price=0.2,
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
    assert lots[0].lot_id == "lot_open-1"
    assert lots[0].fields["contracts_open"] == 1
    assert lots[0].fields["contracts_closed"] == 0


def test_close_projection_does_not_partially_apply_oversized_explicit_target() -> None:
    from tests.ledger_legacy_helpers import project_position_lot_records

    events = [
        _legacy_event(),
        _manual_close_event(
            event_id="manual-close-oversized-target",
            contracts=2,
            price=0.2,
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
    assert lots[0].lot_id == "lot_open-1"
    assert lots[0].fields["contracts_open"] == 1
    assert lots[0].fields["contracts_closed"] == 0
    assert "last_close_event_id" not in lots[0].fields


def test_close_projection_uses_record_id_when_legacy_source_event_target_disagrees() -> None:
    from tests.ledger_legacy_helpers import project_position_lot_records_with_diagnostics

    events = [
        _legacy_event(),
        _legacy_event(
            event_id="open-2",
            price=1.1,
            trade_time_ms=1100,
            order_id="order-2",
            raw_payload={"deal_id": "open-2"},
        ),
        _manual_close_event(
            event_id="manual-close-conflict",
            price=0.2,
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
    lots_by_id = {record.lot_id: record.fields for record in projection.lots}

    assert lots_by_id["lot_open-1"]["contracts_open"] == 1
    assert lots_by_id["lot_open-2"]["contracts_open"] == 0
    assert projection.diagnostics == []


def test_repository_rejects_unresolved_heuristic_close_contracts(tmp_path: Path) -> None:
    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    repo.upsert_trade_event(_legacy_event())
    with pytest.raises(ValueError, match="target_lot_id_required"):
        repo.upsert_trade_event(
            _legacy_event(
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
    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    result = ledger_manual_trades.persist_manual_open_event(
        repo,
        **_open_kwargs(
            symbol="0700.HK",
            contracts=2,
            currency="HKD",
            strike=480.0,
            expiration_ymd="2026-04-29",
            premium_per_share=3.93,
        ),
    )

    assert result.created is True
    assert result.lot_id is not None
    assert str(result.lot_id).startswith("lot_manual-open-")
    lots = repo.list_position_lots()
    assert len(lots) == 1
    assert lots[0]["record_id"] == result.lot_id
    assert lots[0]["fields"]["contracts_open"] == 2
    assert lots[0]["fields"]["status"] == "open"


def test_persist_manual_open_event_is_idempotent_on_retry(tmp_path: Path) -> None:
    """Retrying manual-open with identical parameters must not create duplicate lots."""

    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    command = _open_kwargs(
        account="sy",
        symbol="9992.HK",
        currency="HKD",
        strike=145.0,
        expiration_ymd="2026-07-30",
        premium_per_share=6.0,
        opened_at_ms=1_700_000_000_000,
    )

    result1 = ledger_manual_trades.persist_manual_open_event(repo, **command)
    result2 = ledger_manual_trades.persist_manual_open_event(repo, **command)

    assert result1.created is True
    assert result2.created is False
    assert result1.event_id == result2.event_id
    lots = repo.list_position_lots()
    assert len(lots) == 1
    events = repo.list_trade_events()
    assert len(events) == 1


@pytest.mark.parametrize(
    ("legacy_snapshot", "retry_snapshot"),
    [
        (
            {
                "strategy": "yield_enhancement",
                "yield_enhancement_mode": "vol_convexity_enhancement",
            },
            {"strategy": "combo_yield"},
        ),
        ({"yield_enhancement_mode": "vol_convexity_enhancement"}, None),
    ],
)
def test_manual_open_request_id_is_stable_without_explicit_timestamp_and_rejects_reuse(
    tmp_path: Path,
    legacy_snapshot: dict[str, str],
    retry_snapshot: dict[str, str] | None,
) -> None:
    from src.application.ledger.commands import persist_manual_open_event_with_ledger

    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    command = _open_kwargs(
        expiration_ymd="2027-08-21",
        opened_at_ms=None,
        strategy_snapshot=legacy_snapshot,
        request_id="manual-open-request-001",
    )

    first = persist_manual_open_event_with_ledger(repo, **command)
    stored_event = repo.list_trade_events()[0]
    stored_event["raw_payload"]["strategy_snapshot"] = dict(command["strategy_snapshot"] or {})
    open_fields = ledger_manual_trades.build_position_lot_fields(
        **{key: value for key, value in command.items() if key != "request_id"}
    )
    stored_event["raw_payload"]["manual_request_intent_hash"] = (
        ledger_manual_trades._manual_open_request_intent_hash(
            broker=command["broker"],
            account=command["account"],
            symbol=command["symbol"],
            option_type=command["option_type"],
            side=command["side"],
            contracts=command["contracts"],
            currency=command["currency"],
            strike=command["strike"],
            expiration_ymd=command["expiration_ymd"],
            underlying_share_locked=command.get("underlying_share_locked"),
            note=command.get("note"),
            fields=open_fields,
            strategy_snapshot=dict(command["strategy_snapshot"] or {}),
        )
    )
    with repo._connect() as conn:
        conn.execute(
            "UPDATE trade_events SET event_json=? WHERE event_id=?",
            (json.dumps(stored_event, ensure_ascii=False), stored_event["event_id"]),
        )
    second = persist_manual_open_event_with_ledger(
        repo,
        **{**command, "strategy_snapshot": retry_snapshot},
    )

    assert first.result.created is True
    assert second.result.created is False
    assert first.result.event_id == second.result.event_id
    assert len(repo.list_trade_events()) == 1
    with pytest.raises(ValueError, match="manual request conflict"):
        persist_manual_open_event_with_ledger(
            repo,
            **{**command, "strike": 101.0},
        )


def test_persist_manual_open_event_id_distinguishes_multiplier(tmp_path: Path) -> None:
    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    command = _open_kwargs(
        account="sy",
        symbol="9992.HK",
        currency="HKD",
        strike=145.0,
        expiration_ymd="2026-07-30",
        premium_per_share=6.0,
        opened_at_ms=1_700_000_000_000,
    )

    result1 = ledger_manual_trades.persist_manual_open_event(repo, **command)
    result2 = ledger_manual_trades.persist_manual_open_event(repo, **{**command, "multiplier": 1000})

    assert result1.created is True
    assert result2.created is True
    assert result1.event_id != result2.event_id
    events = repo.list_trade_events()
    assert len(events) == 2
    lots = repo.list_position_lots()
    assert sorted(lot["fields"]["multiplier"] for lot in lots) == [100.0, 1000.0]


def test_persist_manual_close_event_updates_position_lot(tmp_path: Path) -> None:
    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    ledger_manual_trades.persist_manual_open_event(
        repo,
        **_open_kwargs(
            symbol="0700.HK",
            contracts=2,
            currency="HKD",
            strike=480.0,
            expiration_ymd="2026-04-29",
            premium_per_share=3.93,
        ),
    )

    lot = repo.list_position_lots()[0]
    fields = dict(lot["fields"])
    # The contract identity lives under ``contract_key`` now (§3); the dirty
    # spellings below are what the close path has to normalize.
    fields["contract_key"] = {**fields["contract_key"], "account": " LX "}
    fields["currency"] = "港币"
    result = ledger_manual_trades.persist_manual_close_event(
        repo,
        lot_id=lot["record_id"],
        fields=fields,
        contracts_to_close=1,
        close_price=1.2,
        close_reason="manual_buy_to_close",
        as_of_ms=2000,
    )

    assert result.created is True
    lots = repo.list_position_lots()
    assert len(lots) == 1
    assert lots[0]["fields"]["contracts_open"] == 1
    assert lots[0]["fields"]["contracts_closed"] == 1
    events = repo.list_trade_events()
    assert events[-1]["raw_payload"]["record_id"] == lot["record_id"]
    assert events[-1]["raw_payload"]["close_target_source_event_id"] == lots[0]["fields"]["open_event_id"]
    assert events[-1]["account"] == "lx"
    assert events[-1]["currency"] == "HKD"
    assert events[-1]["raw_payload"]["close_target_account"] == "lx"
    assert events[-1]["raw_payload"]["close_target_broker"] == "富途"


def test_persist_manual_close_event_is_idempotent_on_retry(tmp_path: Path) -> None:
    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    ledger_manual_trades.persist_manual_open_event(
        repo,
        **_open_kwargs(
            symbol="0700.HK",
            contracts=2,
            currency="HKD",
            strike=480.0,
            expiration_ymd="2026-04-29",
            premium_per_share=3.93,
        ),
    )

    lot = repo.list_position_lots()[0]
    result1 = ledger_manual_trades.persist_manual_close_event(
        repo,
        lot_id=lot["record_id"],
        fields=lot["fields"],
        contracts_to_close=1,
        close_price=1.2,
        close_reason="manual_buy_to_close",
        as_of_ms=2000,
    )
    result2 = ledger_manual_trades.persist_manual_close_event(
        repo,
        lot_id=lot["record_id"],
        fields=repo.get_position_lot_fields(lot["record_id"]),
        contracts_to_close=1,
        close_price=1.2,
        close_reason="manual_buy_to_close",
        as_of_ms=3000,
    )

    assert result1.created is True
    assert result2.created is False
    assert result1.event_id == result2.event_id
    lots = repo.list_position_lots()
    assert lots[0]["fields"]["contracts_open"] == 1
    assert lots[0]["fields"]["contracts_closed"] == 1
    assert len(repo.list_trade_events()) == 2


def test_persist_manual_close_event_requires_broker_on_position_lot(tmp_path: Path) -> None:
    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")

    with pytest.raises(ValueError, match="position lot missing broker"):
        ledger_manual_trades.persist_manual_close_event(
            repo,
            lot_id="lot_market_only",
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
    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    ledger_manual_trades.persist_manual_open_event(
        repo,
        **_open_kwargs(symbol="AAPL", strike=150.0, premium_per_share=1.0),
    )
    ledger_manual_trades.persist_manual_open_event(
        repo,
        **_open_kwargs(premium_per_share=1.1, opened_at_ms=1100),
    )
    lots = repo.list_position_lots()

    with pytest.raises(ValueError, match="manual_close target fields do not match current lot state"):
        ledger_manual_trades.persist_manual_close_event(
            repo,
            lot_id=lots[0]["record_id"],
            fields=lots[1]["fields"],
            contracts_to_close=1,
            close_price=0.5,
            close_reason="manual_buy_to_close",
            as_of_ms=2000,
        )


def test_lifecycle_close_rejects_resolution_quantity_mismatch_before_write(tmp_path: Path) -> None:
    from src.application.ledger.lifecycle import persist_expire_close_events
    from src.application.ledger.lot_resolver import LotCloseSelector, resolve_fifo_close_targets

    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    ledger_manual_trades.persist_manual_open_event(
        repo,
        **_open_kwargs(contracts=2),
    )
    selector = LotCloseSelector.from_values(
        broker="富途",
        account="lx",
        symbol="NVDA",
        option_type="put",
        position_side="short",
        strike=100.0,
        expiration_ymd="2026-06-19",
        contracts_to_close=2,
    )
    resolution = resolve_fifo_close_targets(repo, selector, source="test")

    with pytest.raises(ValueError, match="contracts_to_close does not match resolved close targets"):
        persist_expire_close_events(
            repo,
            close_target_resolution=resolution,
            contracts_to_close=1,
            event_time_ms=2000,
            case_id="case-mismatch",
        )

    assert [item for item in repo.list_trade_events() if item.get("event_type") == "expire_close"] == []


def test_stock_settlement_allocator_conserves_fees_and_provenance_per_evidence() -> None:
    from domain.domain.lifecycle_allocation import (
        allocate_stock_settlement,
        validate_stock_settlement_allocation_group,
    )

    source = {
        "side": "buy",
        "shares": 300,
        "price": 100.0,
        "fees": 0.0,
        "fee_provenance": {
            "basis": "estimated",
            "amount": 0.7,
            "source": "frozen_schedule",
            "reason": "actual_fee_unavailable",
        },
    }
    allocated = allocate_stock_settlement(
        source,
        [
            {"target_lot_id": "lot-b", "contracts_allocated": 1, "multiplier": 100},
            {"target_lot_id": "lot-a", "contracts_allocated": 2, "multiplier": 100},
        ],
    )

    assert allocated["lot-a"]["shares"] == 200
    assert allocated["lot-b"]["shares"] == 100
    assert sum(Decimal(str(item["fees"])) for item in allocated.values()) == Decimal("0")
    assert sum(
        Decimal(str(item["fee_provenance"]["amount"]))
        for item in allocated.values()
    ) == Decimal("0.7")
    assert {
        (item["fee_provenance"]["basis"], item["fee_provenance"]["source"], item["fee_provenance"]["reason"])
        for item in allocated.values()
    } == {("estimated", "frozen_schedule", "actual_fee_unavailable")}
    assert source["fees"] == 0.0
    assert source["fee_provenance"]["amount"] == 0.7

    actual_source = {
        **source,
        "fees": 1.0,
        "fee_provenance": {
            "basis": "actual",
            "amount": 1.0,
            "source": "broker",
            "reason": "broker_reported",
        },
    }
    actual_allocated = allocate_stock_settlement(
        actual_source,
        [
            {"target_lot_id": "lot-b", "contracts_allocated": 1, "multiplier": 100},
            {"target_lot_id": "lot-a", "contracts_allocated": 2, "multiplier": 100},
        ],
    )
    assert sum(Decimal(str(item["fees"])) for item in actual_allocated.values()) == Decimal("1.0")
    assert sum(
        Decimal(str(item["fee_provenance"]["amount"]))
        for item in actual_allocated.values()
    ) == Decimal("1.0")

    dust = allocate_stock_settlement(
        {"side": "buy", "shares": 400, "price": 100.0, "fees": "0.000002"},
        [
            {"target_lot_id": f"lot-{index}", "contracts_allocated": 1, "multiplier": 100}
            for index in range(4)
        ],
    )
    dust_fees = [Decimal(str(item["fees"])) for item in dust.values()]
    assert dust_fees == [Decimal("0.000001"), Decimal("0.000001"), Decimal("0"), Decimal("0")]
    assert all(amount >= 0 for amount in dust_fees)
    assert sum(dust_fees) == Decimal("0.000002")

    def terminal_event(evidence_id: str, lot_id: str, contracts: int) -> TradeEvent:
        return _trade_event(
            event_id=f"terminal-{evidence_id}-{lot_id}",
            event_type="assignment",
            event_time_ms=2000,
            contract_key=_contract_key(expiration_ymd="2026-06-19"),
            contracts=contracts,
            price=0,
            lot_id=None,
            target_lot_id=lot_id,
            raw_payload={
                # §9.2 step 3: the short put side travels as the trade side.
                "side": "buy",
                "target_lot_id": lot_id,
                "case_id": "case-1",
                "evidence_id": evidence_id,
                "stock_settlement_source": source,
                "stock_settlement": allocated[lot_id],
            },
        )

    first_evidence = [terminal_event("evidence-1", "lot-a", 2), terminal_event("evidence-1", "lot-b", 1)]
    assert validate_stock_settlement_allocation_group(first_evidence) == source
    second_evidence = [terminal_event("evidence-2", "lot-a", 2), terminal_event("evidence-2", "lot-b", 1)]
    assert validate_stock_settlement_allocation_group(second_evidence) == source
    with pytest.raises(ValueError, match="context conflicts"):
        validate_stock_settlement_allocation_group([first_evidence[0], second_evidence[1]])
    old = TradeEvent.from_dict(first_evidence[0].to_dict())
    old.raw_payload.pop("stock_settlement_source")
    with pytest.raises(ValueError, match="mixes old and new"):
        validate_stock_settlement_allocation_group([old, first_evidence[1]])
    old_group = [TradeEvent.from_dict(item.to_dict()) for item in first_evidence]
    for item in old_group:
        item.raw_payload.pop("stock_settlement_source")
        item.raw_payload["stock_settlement"] = source
    assert validate_stock_settlement_allocation_group(old_group) == source
    for item in old_group:
        item.raw_payload["stock_settlement"] = {**source, "shares": 400}
    with pytest.raises(ValueError, match="allocated stock shares"):
        validate_stock_settlement_allocation_group(old_group)
    for item in old_group:
        item.raw_payload["stock_settlement"] = {}
    with pytest.raises(ValueError, match="non-empty"):
        validate_stock_settlement_allocation_group(old_group)


def test_multi_lot_assignment_rolls_back_all_settlement_events_on_second_write_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from src.application.ledger.lifecycle import persist_assignment_events
    from src.application.ledger.lot_resolver import (
        LotCloseSelector,
        resolve_fifo_close_targets,
    )

    repo = ledger_repository.SQLiteOptionPositionsRepository(
        tmp_path / "option_positions.sqlite3"
    )
    for opened_at_ms in (1000, 1100):
        ledger_manual_trades.persist_manual_open_event(
            repo,
            **_open_kwargs(opened_at_ms=opened_at_ms),
        )
    resolution = resolve_fifo_close_targets(
        repo,
        LotCloseSelector.from_values(
            broker="富途",
            account="lx",
            symbol="NVDA",
            option_type="put",
            position_side="short",
            strike=100.0,
            expiration_ymd="2026-06-19",
            contracts_to_close=2,
        ),
        source="test",
    )
    original_upsert = repo.upsert_trade_event
    close_write_count = 0

    def _fail_second_close(event, *, conn=None):
        nonlocal close_write_count
        if str(getattr(event, "event_type", "")) == "assignment":
            close_write_count += 1
            if close_write_count == 2:
                raise RuntimeError("injected lifecycle split failure")
        return original_upsert(event, conn=conn)

    monkeypatch.setattr(repo, "upsert_trade_event", _fail_second_close)

    with pytest.raises(RuntimeError, match="injected lifecycle split failure"):
        persist_assignment_events(
            repo,
            close_target_resolution=resolution,
            contracts_to_close=2,
            event_time_ms=2000,
            case_id="case-atomic",
            evidence_ids=["evidence-atomic"],
            stock_settlement={
                "side": "buy",
                "shares": 200,
                "price": 100.0,
                "fees": 1.0,
                "fee_provenance": {
                    "basis": "actual",
                    "amount": 1.0,
                    "source": "broker",
                    "reason": "broker_reported",
                },
            },
        )

    assert [
        item
        for item in repo.list_trade_events()
        if item.get("event_type") == "assignment"
    ] == []
    assert [
        item["fields"]["contracts_open"]
        for item in repo.list_position_lots()
    ] == [1, 1]


def test_single_lifecycle_assignment_rejects_multi_target_resolution_before_write(tmp_path: Path) -> None:
    from src.application.ledger.lifecycle import persist_assignment_event
    from src.application.ledger.lot_resolver import LotCloseSelector, resolve_fifo_close_targets

    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    for opened_at_ms in (1000, 1100):
        ledger_manual_trades.persist_manual_open_event(
            repo,
            **_open_kwargs(opened_at_ms=opened_at_ms),
        )
    selector = LotCloseSelector.from_values(
        broker="富途",
        account="lx",
        symbol="NVDA",
        option_type="put",
        position_side="short",
        strike=100.0,
        expiration_ymd="2026-06-19",
        contracts_to_close=2,
    )
    resolution = resolve_fifo_close_targets(repo, selector, source="test")
    assert len(resolution.matches) == 2

    with pytest.raises(ValueError, match="expected one assignment target"):
        persist_assignment_event(
            repo,
            close_target_resolution=resolution,
            contracts_to_close=2,
            event_time_ms=2000,
            case_id="case-multi",
            stock_settlement={"shares": 200},
        )

    assert [item for item in repo.list_trade_events() if item.get("event_type") == "assignment"] == []


def test_persist_manual_void_event_removes_open_lot_from_projection(tmp_path: Path) -> None:
    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    open_result = ledger_manual_trades.persist_manual_open_event(
        repo,
        **_open_kwargs(symbol="TSLA", premium_per_share=1.23),
    )

    void_result = ledger_interventions.persist_manual_void_event(
        repo,
        target_event_id=str(open_result.event_id),
        void_reason="opened_by_mistake",
        as_of_ms=2000,
    )

    assert repo.list_position_lots() == []
    events = repo.list_trade_events()
    assert len(events) == 2
    assert events[-1]["position_effect"] == "void"
    assert events[-1]["raw_payload"]["void_target_event_id"] == open_result.event_id
    assert void_result.position_lot_count == 0


def test_persist_manual_void_event_restores_lot_when_voiding_close_event(tmp_path: Path) -> None:
    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    open_result = ledger_manual_trades.persist_manual_open_event(
        repo,
        **_open_kwargs(symbol="TSLA", contracts=2, premium_per_share=1.23),
    )
    lot = repo.list_position_lots()[0]
    close_result = ledger_manual_trades.persist_manual_close_event(
        repo,
        lot_id=lot["record_id"],
        fields=lot["fields"],
        contracts_to_close=1,
        close_price=0.5,
        close_reason="manual_buy_to_close",
        as_of_ms=1500,
    )

    void_result = ledger_interventions.persist_manual_void_event(
        repo,
        target_event_id=str(close_result.event_id),
        void_reason="close_recorded_by_mistake",
        as_of_ms=2000,
    )

    rebuilt_lot = repo.list_position_lots()[0]
    assert rebuilt_lot["record_id"] == f"lot_{open_result.event_id}"
    assert rebuilt_lot["fields"]["contracts_open"] == 2
    assert rebuilt_lot["fields"]["contracts_closed"] == 0
    assert rebuilt_lot["fields"]["status"] == "open"
    assert void_result.position_lot_count == 1


def test_persist_manual_adjust_event_updates_position_lot_projection(tmp_path: Path) -> None:
    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    ledger_manual_trades.persist_manual_open_event(
        repo,
        **_open_kwargs(),
    )
    lot = repo.list_position_lots()[0]

    result = ledger_manual_trades.persist_manual_adjust_event(
        repo,
        lot_id=lot["record_id"],
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
    assert result.created is True
    assert adjusted["contracts_opened"] == 2
    assert adjusted["contracts_open"] == 2
    # §7.4: the published row carries money as decimal text.
    assert adjusted["contract_key"]["strike"] == "105"
    assert adjusted["premium_open"] == "3.1"
    assert adjusted["opened_at_ms"] == 2000
    # §7.1: ``position_id`` is retired; the adjust patch no longer carries a
    # display id, and the derived ``position_key`` is published instead. The
    # literal pins the derivation itself -- a truthiness check would pass on any
    # string, including one still keyed on the pre-adjust contract.
    assert "position_id" not in adjusted
    assert adjusted["position_key"] == "富途|lx|NVDA|2026-07-17|105P|short"
    # §2 RECONSTRUCTIBLE: the derived cash-secured amount is no longer a payload
    # key -- it is recomputed from the contract under ``contract_key``.
    assert "cash_secured_amount" not in adjusted


def test_manual_adjust_group_id_collision_comes_from_the_event_layer(tmp_path: Path) -> None:
    """A group id belongs to one lot, and the check reads the binding, not the payload.

    ``strategy_group_id`` left the lot payload (``write-side-definition.md`` §2
    RECONSTRUCTIBLE), so the guard's flat SQL over ``position_lots`` can no longer
    see a group a lot is bound to -- the binding lives on the adjust event that
    carried it. It has to answer from there, or one group id names two lots and the
    combo identity resolves to whichever row is read first.
    """

    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    ledger_manual_trades.persist_manual_open_event(repo, **_open_kwargs())
    ledger_manual_trades.persist_manual_open_event(
        repo, **_open_kwargs(strike=110.0, expiration_ymd="2026-08-21")
    )
    first_id, second_id = sorted(row["record_id"] for row in repo.list_position_lots())

    def adjust(lot_id: str, group_id: str, as_of_ms: int) -> None:
        ledger_manual_trades.persist_manual_adjust_events(
            repo,
            [
                {
                    "record_id": lot_id,
                    "fields": repo.get_position_lot_fields(lot_id),
                    "strategy_group_id": group_id,
                    "as_of_ms": as_of_ms,
                }
            ],
        )

    # The group is bound by the event; the payload never carries it.
    adjust(first_id, "group-a", 2_000)
    assert "strategy_group_id" not in repo.get_position_lot_fields(first_id)

    # Handing that same group to another lot is refused from either direction.
    with pytest.raises(
        ValueError, match="strategy_group_id is already assigned to another position lot"
    ):
        adjust(second_id, "group-a", 3_000)
    adjust(second_id, "group-b", 4_000)
    with pytest.raises(
        ValueError, match="strategy_group_id is already assigned to another position lot"
    ):
        adjust(first_id, "group-b", 5_000)

    # A group of its own is still a legitimate single binding, and the bindings
    # survive the refused attempts on both lots.
    assert "strategy_group_id" not in repo.get_position_lot_fields(second_id)
    assert repo.get_position_lot_fields(first_id)


def test_manual_strategy_snapshot_adjustment_supersedes_retired_mode(tmp_path: Path) -> None:
    """§7: the snapshot the adjust writes lives on the event, not the payload.

    The assertion this test used to make -- ``resolve_position_strategy`` reading
    ``strategy_source == "position_snapshot"`` off the adjusted lot -- cannot hold
    for a converged payload: the whole strategy family left ``fields_json``
    (``write-side-definition.md`` §2 RECONSTRUCTIBLE), and ``strategy_policy``
    still reads the retired flat keys off the payload it is handed, so it resolves
    ``template_default`` instead. That read point is outside this pass's editable
    surface; it is reported, and the payload half of the claim is pinned here.
    """

    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    repo.upsert_trade_event(
        _trade_event(
            event_id="legacy-combo-open",
            contract_key=_contract_key(expiration_ymd="2026-06-19"),
            price=2.5,
            source="legacy_import",
            lot_id="legacy-combo-lot",
            raw_payload={
                "fields": {
                    "symbol": "NVDA",
                    "option_type": "put",
                    "side": "short",
                    "status": "open",
                    "contracts": 1,
                    "contracts_open": 1,
                    "contracts_closed": 0,
                    "currency": "USD",
                    "strike": 100.0,
                    "multiplier": 100,
                    "expiration": 1781827200000,
                    "strategy": "combo_yield",
                    "leg_role": "sell_put",
                    "yield_enhancement_mode": "vol_convexity_enhancement",
                }
            },
        )
    )
    projection = ledger_writer.project_stored_trade_events_to_position_lots(repo.list_trade_events())
    repo.replace_position_lots(projection.lots)
    lot = repo.list_position_lots()[0]

    ledger_manual_trades.persist_manual_adjust_event(
        repo,
        lot_id=lot["record_id"],
        fields=lot["fields"],
        strategy_snapshot={"strategy_family": "sell_put", "strategy_profile": "return_first"},
        as_of_ms=2000,
    )

    adjusted = repo.get_position_lot_fields(lot["record_id"])
    # The legacy snapshot's retired family does not survive on the payload, and
    # neither does the family the adjust patch carried.
    for retired in (
        "strategy",
        "strategy_snapshot",
        "leg_role",
        "strategy_group_id",
        "yield_enhancement_mode",
    ):
        assert retired not in adjusted, retired
    # The adjust event's patch is the carrier now, and it is what supersedes the
    # retired mode for the read points that have moved with it.
    adjust_events = [
        event
        for event in repo.list_trade_events()
        if event["event_id"] != "legacy-combo-open"
    ]
    assert len(adjust_events) == 1
    assert adjust_events[0]["raw_payload"]["patch"]["strategy_snapshot"] == {
        "strategy_family": "sell_put",
        "strategy_profile": "return_first",
    }

    # And the read side follows: the ledger read model reconstructs the family
    # from the events (``write-side-definition.md`` §2 RECONSTRUCTIBLE), so a
    # consumer that resolves a strategy off the read-model payload finds the
    # snapshot the adjust wrote instead of falling back to ``template_default``.
    from src.application.ledger.read_model import (
        canonicalize_position_lot_fields,
        load_position_lot_records,
    )
    from src.application.strategy_policy import resolve_position_strategy

    read_model_fields = canonicalize_position_lot_fields(
        load_position_lot_records(repo)[0]["fields"]
    )
    assert read_model_fields["strategy_snapshot"] == {
        "strategy_family": "sell_put",
        "strategy_profile": "return_first",
    }
    resolution = resolve_position_strategy(position=read_model_fields, config=None)
    assert resolution.strategy_source == "position_snapshot"
    assert resolution.strategy_profile == "return_first"


def test_persist_manual_adjust_event_is_idempotent_on_retry(tmp_path: Path) -> None:
    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    ledger_manual_trades.persist_manual_open_event(
        repo,
        **_open_kwargs(),
    )
    lot = repo.list_position_lots()[0]

    result1 = ledger_manual_trades.persist_manual_adjust_event(
        repo,
        lot_id=lot["record_id"],
        fields=lot["fields"],
        premium_per_share=3.1,
        as_of_ms=2000,
    )
    result2 = ledger_manual_trades.persist_manual_adjust_event(
        repo,
        lot_id=lot["record_id"],
        fields=repo.get_position_lot_fields(lot["record_id"]),
        premium_per_share=3.1,
        as_of_ms=3000,
    )

    assert result1.created is True
    assert result2.created is False
    assert result1.event_id == result2.event_id
    assert repo.get_position_lot_fields(lot["record_id"])["premium_open"] == "3.1"
    assert len(repo.list_trade_events()) == 2


def test_persist_manual_adjust_event_rejects_mismatched_record_id_and_fields(tmp_path: Path) -> None:
    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    ledger_manual_trades.persist_manual_open_event(
        repo,
        **_open_kwargs(symbol="AAPL", strike=150.0, premium_per_share=1.0),
    )
    ledger_manual_trades.persist_manual_open_event(
        repo,
        **_open_kwargs(premium_per_share=1.1, opened_at_ms=1100),
    )
    lots = repo.list_position_lots()

    with pytest.raises(ValueError, match="manual_adjust target fields do not match current lot state"):
        ledger_manual_trades.persist_manual_adjust_event(
            repo,
            lot_id=lots[0]["record_id"],
            fields=lots[1]["fields"],
            premium_per_share=2.0,
            as_of_ms=2000,
        )


def test_voiding_adjust_event_restores_prior_projection_state(tmp_path: Path) -> None:
    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    ledger_manual_trades.persist_manual_open_event(
        repo,
        **_open_kwargs(),
    )
    lot = repo.list_position_lots()[0]
    adjust_result = ledger_manual_trades.persist_manual_adjust_event(
        repo,
        lot_id=lot["record_id"],
        fields=lot["fields"],
        premium_per_share=3.1,
        as_of_ms=2000,
    )
    ledger_interventions.persist_manual_void_event(
        repo,
        target_event_id=str(adjust_result.event_id),
        void_reason="adjustment_was_wrong",
        as_of_ms=3000,
    )

    restored = repo.get_position_lot_fields(lot["record_id"])
    assert restored["premium_open"] == "2.5"
    assert restored["contracts_opened"] == 1


@pytest.mark.parametrize(
    ("feishu", "retired_bootstrap", "expected_status"),
    [
        ({"app_id": "app_only"}, False, "sqlite_only_no_feishu_bootstrap"),
        ({"app_id": "app_only"}, True, "sqlite_only_feishu_bootstrap_retired"),
        ("invalid", False, "sqlite_only_no_feishu_bootstrap"),
        ("invalid", True, "sqlite_only_feishu_bootstrap_retired"),
    ],
    ids=[
        "incomplete_config-when_bootstrap_disabled",
        "malformed_config-when_retired_bootstrap_enabled",
        "non_object_config-when_bootstrap_disabled",
        "non_object_config-when_retired_bootstrap_enabled",
    ],
)
def test_load_option_positions_repo_ignores_incomplete_or_malformed_feishu_config(
    tmp_path: Path,
    feishu: object,
    retired_bootstrap: bool,
    expected_status: str,
) -> None:
    option_positions: dict[str, object] = {"sqlite_path": str(tmp_path / "option_positions.sqlite3")}
    if retired_bootstrap:
        option_positions["bootstrap_from_feishu"] = {"enabled": True}
    data_config = tmp_path / "data.json"
    data_config.write_text(
        json.dumps(
            {"option_positions": option_positions, "feishu": feishu},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    repo = ledger_bootstrap.load_option_positions_repo(data_config)

    assert repo.bootstrap_status == expected_status


@pytest.mark.parametrize(
    ("bootstrap_from_feishu", "with_feishu"),
    [
        ({"enabled": False}, True),
        ({"enabled": True}, True),
        ({"enabled": "yes"}, False),
    ],
    ids=["defaults_false", "reads_boolean", "ignores_retired_config_shape"],
)
def test_option_positions_bootstrap_from_feishu_enabled_stays_disabled(
    tmp_path: Path,
    bootstrap_from_feishu: object,
    with_feishu: bool,
) -> None:
    data_config = _write_data_config(
        tmp_path / "data.json",
        sqlite_path=tmp_path / "option_positions.sqlite3",
        with_feishu=with_feishu,
    )
    payload = json.loads(data_config.read_text(encoding="utf-8"))
    payload["option_positions"]["bootstrap_from_feishu"] = bootstrap_from_feishu
    data_config.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    assert ledger_repository.option_positions_bootstrap_from_feishu_enabled(data_config) is False


def _converged_option_lot(
    lot_id: str,
    *,
    strike: str | None,
    expiration_ymd: str = "2026-06-29",
) -> dict:
    """A §1/§3 converged option payload: the contract under ``contract_key``."""

    return {
        "lot_id": lot_id,
        "open_event_id": f"open-{lot_id}",
        "contract_key": {
            "broker": "富途",
            "account": "lx",
            "underlying_symbol": "0700.HK",
            "option_type": "put",
            "strike": strike,
            "expiration_ymd": expiration_ymd,
            "asset_type": "option",
        },
        "position_side": "short",
        "position_key": f"富途|lx|0700.HK|{expiration_ymd}|{strike}P|short",
        "opened_at_ms": 1000,
        "contracts_opened": 1,
        "contracts_open": 1,
        "contracts_closed": 0,
        "status": "open",
        "currency": "HKD",
        "premium_open": "1.2",
        "multiplier": 100,
        "realized_pnl": "0",
        "last_event_id": f"open-{lot_id}",
        "close_event_ids": [],
        "asset_type": "option",
    }


def test_replace_position_lots_rejects_incomplete_option_lots_atomically(tmp_path: Path) -> None:
    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    repo.replace_position_lots(
        [
            PositionLotRecord(
                lot_id="lot_existing",
                fields=_converged_option_lot("lot_existing", strike="470"),
            ),
        ]
    )

    with pytest.raises(ValueError) as _caught:
        repo.replace_position_lots(
            [
                PositionLotRecord(
                    lot_id="lot_bad_option",
                    fields=_converged_option_lot(
                        "lot_bad_option",
                        strike=None,
                        expiration_ymd="",
                    ),
                ),
                PositionLotRecord(
                    lot_id="lot_good_option",
                    fields=_converged_option_lot("lot_good_option", strike="480"),
                ),
            ]
        )
    exc = _caught.value
    assert "missing expiration, strike" in str(exc)

    lots = repo.list_position_lots()
    lot_ids = {row["record_id"] for row in lots}
    assert lot_ids == {"lot_existing"}


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
    fixture_path = BASE / "tests" / "fixtures" / "option_positions_projection_replay_case.json"
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    for raw_event in fixture["events"]:
        ledger_writer.persist_trade_event_object(repo, LegacyTradeEvent(**raw_event))

    rebuild_result = ledger_writer.rebuild_position_lots_from_trade_events(repo)

    lots = repo.list_position_lots()
    assert len(lots) == 1
    assert lots[0]["record_id"] == fixture["expected"]["record_id"]
    assert lots[0]["fields"]["contracts_open"] == 0
    assert lots[0]["fields"]["contracts_closed"] == 2
    assert lots[0]["fields"]["status"] == "close"
    assert rebuild_result.unmatched_explicit_close_count == 0

    context = build_context(lots, broker="富途", account="sy", rates={})
    assert context["open_positions_min"] == []


def test_projection_matches_explicit_close_with_legacy_hk_symbol_alias() -> None:
    from tests.ledger_legacy_helpers import project_position_lot_records_with_diagnostics

    events = [
        _bootstrap_event(
            event_id="bootstrap:hk:legacy-700",
            symbol="00700.HK",
            strike=480.0,
            expiration_ymd="2026-04-29",
            currency="HKD",
            raw_payload={
                "lot_record_id": "rec_legacy_700",
                "fields": _seed_fields(symbol="00700.HK", currency="HKD", strike=480.0, expiration=1777420800000, position_key="00700.HK_20260429_480P_short", note="exp=2026-04-29;premium_per_share=1.0"),
            },
        ),
        _manual_close_event(
            event_id="manual-close-legacy-700",
            symbol="00700.HK",
            price=0.2,
            strike=480.0,
            expiration_ymd="2026-04-29",
            currency="HKD",
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


def test_ledger_api_exposes_economic_allocations(tmp_path: Path) -> None:
    from src.application.ledger.api import trade_event_economic_allocations

    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "economic_allocations.sqlite3")
    key = _contract_key(broker="futu")
    open_event = _trade_event(
        event_id="open-economic",
        contract_key=key,
        price=2,
        lot_id="lot-economic",
        raw_payload={"side": "sell", "fee_provenance": {"basis": "actual", "source": "test"}},
    )
    close_event = _trade_event(
        event_id="close-economic",
        event_type="close",
        event_time_ms=2000,
        contract_key=key,
        lot_id=None,
        target_lot_id="lot-economic",
        raw_payload={"side": "buy", "fee_provenance": {"basis": "actual", "source": "test"}},
    )
    repo.upsert_trade_event(open_event)
    repo.upsert_trade_event(close_event)

    allocations = trade_event_economic_allocations(repo)

    assert len(allocations) == 1
    assert allocations[0].realized_pnl_gross == Decimal("100.000000")


def test_event_codec_preserves_top_level_fee_provenance_for_compatibility() -> None:
    from src.application.ledger.event_codec import import_stored_trade_events

    key = _contract_key(broker="futu")
    payload = _trade_event(
        event_id="open-fee",
        contract_key=key,
        source="manual",
        lot_id="lot-open-fee",
        fees=0.0,
        # §9.2 step 3: the short put side travels as the trade side.
        raw_payload={"side": "sell"},
    ).to_dict()
    payload["fee_provenance"] = {"basis": "actual", "source": "manual-zero"}

    imported, diagnostics = import_stored_trade_events([payload])

    assert diagnostics == []
    assert imported[0].raw_payload["fee_provenance"] == {"basis": "actual", "source": "manual-zero"}


def test_multi_lot_close_writer_conserves_frozen_formula_fee_across_allocations(tmp_path: Path) -> None:
    from src.application.ledger.api import trade_event_economic_allocations

    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "multi_lot_close_fees.sqlite3")
    key = _contract_key(broker="futu")
    for index in (1, 2):
        ledger_writer.persist_trade_event_object(
            repo,
            _trade_event(
                event_id=f"open-fee-{index}",
                event_time_ms=1000 + index,
                contract_key=key,
                price=2,
                lot_id=f"lot-fee-{index}",
                fees=0,
                # §9.2 step 3: the short put side travels as the trade side.
                raw_payload={"side": "sell"},
            ),
        )

    ledger_writer.persist_trade_event_object(
        repo,
        _trade_event(
            event_id="close-fee-both",
            event_type="close",
            event_time_ms=2000,
            contract_key=key,
            contracts=2,
            lot_id=None,
            fees=0,
            raw_payload={"side": "buy"},
        ),
    )

    allocations = trade_event_economic_allocations(repo)

    assert len(allocations) == 2
    assert [item.contracts for item in allocations] == [1, 1]
    assert [item.close_fee.amount for item in allocations] == [Decimal("1.508300"), Decimal("1.508300")]
    assert sum(item.close_fee.amount or Decimal(0) for item in allocations) == Decimal("3.016600")
    assert all(item.realized_pnl_net is None for item in allocations)


def test_multi_lot_close_writer_allocates_one_trusted_actual_total(tmp_path: Path) -> None:
    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "multi-lot-actual.sqlite3")
    key = _contract_key()
    for index in (1, 2):
        ledger_writer.persist_trade_event_object(
            repo,
            _trade_event(
                event_id=f"open-actual-{index}",
                event_time_ms=1000 + index,
                contract_key=key,
                price=2,
                source="manual",
                lot_id=f"lot-actual-{index}",
                # §9.2 step 3: the short put side travels as the trade side.
                raw_payload={"side": "sell"},
            ),
        )

    ledger_writer.persist_trade_event_object(
        repo,
        _trade_event(
            event_id="close-actual-both",
            event_type="close",
            event_time_ms=2000,
            contract_key=key,
            contracts=2,
            source="opend_push",
            lot_id=None,
            raw_payload={
                # §9.2 step 3: the short put side travels as the trade side.
                "side": "buy",
                "source_type": "broker_trade_event",
                "futu_account_id": "123",
                "order_id": "order-close-actual",
                "source_deal_id": "deal-close-actual",
                "fee_amount": "3.000001",
            },
        ),
    )

    closes = sorted(
        (
            TradeEvent.from_dict(row)
            for row in repo.list_trade_events()
            if row["event_type"] == "close"
        ),
        key=lambda event: event.event_id,
    )
    assert [fee_fact_for_event(event).amount for event in closes] == [
        Decimal("1.500001"),
        Decimal("1.500000"),
    ]
    assert sum((fee_fact_for_event(event).amount or Decimal(0)) for event in closes) == Decimal(
        "3.000001"
    )


def test_atomic_close_split_without_expected_contract_evidence_keeps_fee_missing(
    tmp_path: Path,
) -> None:
    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "missing-split-size.sqlite3")
    key = _contract_key()
    for index in (1, 2):
        ledger_writer.persist_trade_event_object(
            repo,
            _trade_event(
                event_id=f"open-missing-size-{index}",
                event_time_ms=1000 + index,
                contract_key=key,
                price=2,
                lot_id=f"lot-missing-size-{index}",
                # §9.2 step 3: the short put side travels as the trade side.
                raw_payload={"side": "sell"},
            ),
        )
    ledger_writer.persist_trade_event_objects_atomically(
        repo,
        [
            _trade_event(
                event_id=f"close-missing-size-{index}",
                event_type="close",
                event_time_ms=2000,
                contract_key=key,
                lot_id=None,
                target_lot_id=f"lot-missing-size-{index}",
                raw_payload={"side": "buy", "source_deal_id": "deal-missing-size"},
            )
            for index in (1, 2)
        ],
    )

    facts = [
        fee_fact_for_event(TradeEvent.from_dict(row))
        for row in repo.list_trade_events()
        if row["event_type"] == "close"
    ]
    assert [fact.basis.value for fact in facts] == ["missing", "missing"]
    assert {fact.reason for fact in facts} == {"source_deal_fee_contracts_unavailable"}


def test_close_writer_preserves_invalid_explicit_fee_amount_as_missing(tmp_path: Path) -> None:
    from src.application.ledger.api import trade_event_economic_allocations

    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "invalid_close_fee.sqlite3")
    key = _contract_key(broker="futu")
    ledger_writer.persist_trade_event_object(
        repo,
        _trade_event(
            event_id="open-invalid-close-fee",
            contract_key=key,
            price=2,
            lot_id="lot-invalid-close-fee",
            fees=0,
            raw_payload={"side": "sell", "fee_provenance": {"basis": "actual", "source": "test"}},
        ),
    )
    ledger_writer.persist_trade_event_object(
        repo,
        _trade_event(
            event_id="close-invalid-fee",
            event_type="close",
            event_time_ms=2000,
            contract_key=key,
            lot_id=None,
            fees=1,
            raw_payload={"side": "buy", "fee_provenance": {"basis": "actual", "amount": "bad", "source": "test"}},
        ),
    )

    allocations = trade_event_economic_allocations(repo)

    assert repo.list_position_lots()[0]["fields"]["contracts_open"] == 0
    assert len(allocations) == 1
    assert allocations[0].realized_pnl_gross == Decimal("100.000000")
    assert allocations[0].close_fee.amount is None
    assert allocations[0].close_fee.reason == "actual_fee_evidence_not_admitted"
    assert allocations[0].realized_pnl_net is None


@pytest.mark.parametrize(
    ("fee_fields", "fees", "expected"),
    [
        (
            {
                "fee_provenance": {
                    "basis": "actual",
                    "amount": "2.500000",
                    "source": "opend.order_fee_query",
                },
                "fee_amount": "2.5",
                "commission": "999",
            },
            2.5,
            Decimal("2.500000"),
        ),
        (
            {"commission": "-1.99", "platform_fee": "-0.6"},
            0,
            Decimal("2.590000"),
        ),
        (
            {"fee_provenance": {"basis": "actual", "source": "opend.order_fee_query"}},
            0,
            Decimal("0.000000"),
        ),
    ],
)
def test_writer_admits_only_consistent_actual_fee_candidates_from_trusted_broker_receipt(
    tmp_path: Path,
    fee_fields: dict[str, Any],
    fees: float,
    expected: Decimal,
) -> None:
    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "actual-fee.sqlite3")
    event = _trade_event(
        event_id="trusted-actual",
        contract_key=_contract_key(),
        price=2,
        source="opend_push",
        lot_id="trusted-actual-lot",
        fees=fees,
        raw_payload={
            # §9.2 step 3: the short put side travels as the trade side.
            "side": "sell",
            "source_type": "broker_trade_event",
            "futu_account_id": "123",
            "order_id": "order-actual",
            "source_deal_id": "deal-actual",
            **fee_fields,
        },
    )

    ledger_writer.persist_trade_event_object(repo, event)

    persisted = TradeEvent.from_dict(repo.list_trade_events()[0])
    fact = fee_fact_for_event(persisted)
    assert fact.basis.value == "actual"
    assert fact.amount == expected
    assert Decimal(str(persisted.fees)).quantize(Decimal("0.000001")) == expected


def test_writer_marks_conflicting_trusted_actual_candidates_missing_without_blocking_intake(
    tmp_path: Path,
) -> None:
    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "actual-fee-conflict.sqlite3")
    event = _trade_event(
        event_id="trusted-conflict",
        contract_key=_contract_key(),
        price=2,
        source="opend_push",
        lot_id="trusted-conflict-lot",
        fees=2,
        raw_payload={
            # §9.2 step 3: the short put side travels as the trade side.
            "side": "sell",
            "source_type": "broker_trade_event",
            "futu_account_id": "123",
            "order_id": "order-conflict",
            "source_deal_id": "deal-conflict",
            "fee_amount": "3",
        },
    )

    result = ledger_writer.persist_trade_event_object(repo, event)

    persisted = TradeEvent.from_dict(repo.list_trade_events()[0])
    fact = fee_fact_for_event(persisted)
    assert result.created is True
    assert fact.basis.value == "missing"
    assert fact.reason == "actual_fee_candidates_conflict"
    assert persisted.fees == 0
    assert persisted.raw_payload["fee_provenance"]["candidate_diagnostics"] == [
        {"source": "legacy_top_level", "amount": "2.000000"},
        {"source": "raw_payload.fee_amount", "amount": "3.000000"},
    ]


def test_writer_recomputes_incoming_estimate_and_rejects_manual_actual(
    tmp_path: Path,
) -> None:
    key = _contract_key()
    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "fee-admission.sqlite3")
    ledger_writer.persist_trade_event_object(
        repo,
        _trade_event(
            event_id="incoming-estimate",
            contract_key=key,
            price=2,
            source="manual",
            lot_id="incoming-estimate-lot",
            raw_payload={
                # §9.2 step 3: the short put side travels as the trade side.
                "side": "sell",
                "fee_provenance": {"basis": "estimated", "amount": "999"}
            },
        ),
    )
    ledger_writer.persist_trade_event_object(
        repo,
        _trade_event(
            event_id="manual-actual",
            event_time_ms=2000,
            contract_key=key,
            price=2,
            source="manual",
            lot_id="manual-actual-lot",
            fees=2,
            # §9.2 step 3: the short put side travels as the trade side.
            raw_payload={"side": "sell"},
        ),
    )

    events = {
        row["event_id"]: TradeEvent.from_dict(row)
        for row in repo.list_trade_events()
    }
    estimate = fee_fact_for_event(events["incoming-estimate"])
    manual = fee_fact_for_event(events["manual-actual"])
    assert estimate.basis.value == "estimated"
    assert estimate.amount != Decimal("999.000000")
    assert events["incoming-estimate"].fees == 0
    assert manual.basis.value == "missing"
    assert manual.reason == "actual_fee_evidence_not_admitted"


def test_manual_assignment_request_retry_returns_original_result_after_lot_closed(
    tmp_path: Path,
) -> None:
    from src.application.ledger.commands import record_manual_assignment

    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    ledger_manual_trades.persist_manual_open_event(
        repo,
        **_open_kwargs(symbol="TIGR", strike=6.0, expiration_ymd="2026-08-21", premium_per_share=0.2),
    )
    lot_id = str(repo.list_position_lots()[0]["record_id"])
    kwargs = {
        "lot_id": lot_id,
        "contracts_to_close": 1,
        "stock_side": "buy",
        "stock_qty": 100,
        "stock_price": 6.0,
        "as_of_ms": 2000,
        "request_id": "manual-assignment-request-001",
    }

    first = record_manual_assignment(repo, **kwargs)
    second = record_manual_assignment(repo, **kwargs)

    assert first["result"]["created"] is True
    assert second["result"]["created"] is False
    assert first["result"]["event_id"] == second["result"]["event_id"]
    assert repo.get_position_lot_fields(lot_id)["status"] == "close"
    assert len(repo.list_trade_events()) == 2
    assignment = next(
        TradeEvent.from_dict(row)
        for row in repo.list_trade_events()
        if row["event_type"] == "assignment"
    )
    fee = fee_fact_for_event(assignment)
    assert fee.basis.value == "actual"
    assert fee.amount == 0
    assert assignment.raw_payload["fee_provenance"]["reason"] == (
        "assignment_without_option_trade"
    )
    repair = ledger_interventions.build_manual_repair_preview(
        repo,
        target_event_id=assignment.event_id,
        overrides={"currency": "USD"},
        repair_reason="preserve lifecycle type",
        as_of_ms=3000,
    )
    assert repair.repair_event is not None
    assert repair.repair_event["event_type"] == "assignment"
    with pytest.raises(ValueError, match="manual request conflict"):
        record_manual_assignment(repo, **(kwargs | {"stock_qty": 200}))


@pytest.mark.parametrize(
    ("terminal_type", "option_type", "position_side", "stock_side"),
    [
        ("assignment", "put", "short", "buy"),
        ("exercise", "call", "long", "buy"),
    ],
)
def test_manual_multi_lot_terminal_persists_conserved_settlement_and_replays_source(
    tmp_path: Path,
    terminal_type: str,
    option_type: str,
    position_side: str,
    stock_side: str,
) -> None:
    from src.application.ledger.commands import (
        preview_manual_assignment,
        preview_manual_exercise,
        record_manual_assignment,
        record_manual_exercise,
    )

    repo = ledger_repository.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    for opened_at_ms in (1000, 1100):
        ledger_manual_trades.persist_manual_open_event(
            repo,
            **_open_kwargs(
                symbol="TIGR",
                option_type=option_type,
                side=position_side,
                strike=6.0,
                expiration_ymd="2026-08-21",
                premium_per_share=0.2,
                opened_at_ms=opened_at_ms,
            ),
        )
    kwargs = {
        "lot_id": None,
        "broker": "富途",
        "account": "lx",
        "symbol": "TIGR",
        "option_type": option_type,
        "position_side": position_side,
        "strike": 6.0,
        "expiration_ymd": "2026-08-21",
        "contracts_to_close": 2,
        "stock_side": stock_side,
        "stock_qty": 200,
        "stock_price": 6.0,
        "as_of_ms": 2000,
        "request_id": f"manual-{terminal_type}-multi-lot",
    }

    preview_fn = (
        preview_manual_assignment if terminal_type == "assignment" else preview_manual_exercise
    )
    record_fn = record_manual_assignment if terminal_type == "assignment" else record_manual_exercise
    preview = preview_fn(repo, **kwargs)
    assert preview["stock_settlement"]["shares"] == 200
    assert {
        operation["stock_settlement"]["shares"]
        for operation in preview["operations"]
    } == {100}
    assert {
        operation["stock_settlement_source"]["shares"]
        for operation in preview["operations"]
    } == {200}

    first = record_fn(repo, **kwargs)
    event_ids = {
        row["event_id"]
        for row in repo.list_trade_events()
        if row.get("event_type") == terminal_type
    }
    replay = record_fn(repo, **kwargs)
    events = [
        row for row in repo.list_trade_events() if row.get("event_type") == terminal_type
    ]

    assert first["stock_settlement"]["shares"] == 200
    assert replay["stock_settlement"]["shares"] == 200
    assert replay["idempotent_duplicate"] is True
    assert len(events) == 2
    assert {row["event_id"] for row in events} == event_ids
    assert sum(int(row["raw_payload"]["stock_settlement"]["shares"]) for row in events) == 200
    assert {row["raw_payload"]["stock_settlement"]["shares"] for row in events} == {100}
    assert {
        row["raw_payload"]["stock_settlement_source"]["shares"] for row in events
    } == {200}
    assert len(repo.list_trade_events()) == 4


def test_trade_event_repair_recovers_assignment_type_from_lifecycle_payload() -> None:
    repaired = ledger_interventions._repair_trade_event(
        event_id="repair-assignment",
        core={
            "event_type": "close",
            "position_effect": "close",
            "trade_time_ms": 2000,
            "broker": "富途",
            "account": "sy",
            "symbol": "PDD",
            "option_type": "put",
            "side": "buy",
            "contracts": 1,
            "price": 0,
            "strike": 100,
            "multiplier": 100,
            "expiration_ymd": "2026-08-21",
            "currency": "USD",
        },
        raw_payload={
            "close_type": "assignment",
            "target_lot_id": "lot-pdd",
            "stock_settlement": {
                "side": "buy",
                "shares": 100,
                "price": 100,
                "currency": "USD",
            },
        },
    )

    assert repaired.event_type == "assignment"
    assert repaired.target_lot_id == "lot-pdd"


def _identity_probe_lot(lot_id: str, *, contracts: int) -> PositionLotRecord:
    """A minimal payload in the converged shape (``PositionLot.to_dict()``)."""
    return PositionLotRecord(
        lot_id=lot_id,
        fields={
            "contract_key": {
                "account": "lx",
                "broker": "富途",
                "underlying_symbol": "0700.HK",
                "option_type": "put",
                "strike": "470",
                "expiration_ymd": "2026-06-30",
            },
            "position_side": "short",
            "contracts_open": contracts,
            "multiplier": 100,
            "asset_type": "option",
        },
    )


def test_apply_position_lot_diff_updates_the_row_its_loop_key_names(tmp_path: Path) -> None:
    """The diff updates the row named by the one identity slot it iterates on.

    This pair used to diverge a second, trailing identity slot to prove the loop
    key was not read off it. That slot is retired -- ``_position_lot_storage_values``
    returns one identity -- so the divergence control has nothing left to model and
    the property is pinned against the single slot instead.
    """
    database = tmp_path / "option_positions.sqlite3"
    repo = ledger_repository.SQLiteOptionPositionsRepository(database)
    repo.replace_position_lots(
        [_identity_probe_lot("lot-a", contracts=1), _identity_probe_lot("lot-b", contracts=3)]
    )

    diff = repo.apply_position_lot_diff([_identity_probe_lot("lot-a", contracts=2)])

    assert (diff.added, diff.changed, diff.removed) == (0, 1, 1)
    with sqlite3.connect(database) as conn:
        rows = conn.execute("SELECT record_id, fields_json FROM position_lots ORDER BY record_id ASC").fetchall()
    assert [row[0] for row in rows] == ["lot-a"]
    assert json.loads(rows[0][1])["contracts_open"] == 2


def test_apply_position_lot_diff_looks_up_the_row_its_loop_key_names(tmp_path: Path) -> None:
    """Same, on the ``remove_missing=False`` lookup path."""
    database = tmp_path / "option_positions.sqlite3"
    repo = ledger_repository.SQLiteOptionPositionsRepository(database)
    repo.replace_position_lots([_identity_probe_lot("lot-a", contracts=1)])

    diff = repo.apply_position_lot_diff([_identity_probe_lot("lot-a", contracts=2)], remove_missing=False)

    assert (diff.added, diff.changed) == (0, 1)
    assert repo.count_position_lots() == 1
    with sqlite3.connect(database) as conn:
        fields_json = conn.execute("SELECT fields_json FROM position_lots").fetchone()[0]
    assert json.loads(fields_json)["contracts_open"] == 2
