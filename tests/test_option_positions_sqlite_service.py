from __future__ import annotations

import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))


def _write_data_config(path: Path, *, sqlite_path: Path, with_feishu: bool = True) -> Path:
    payload: dict[str, object] = {
        "option_positions": {"sqlite_path": str(sqlite_path)},
    }
    if with_feishu:
        payload["feishu"] = {
            "app_id": "app_id",
            "app_secret": "app_secret",
            "tables": {"option_positions": "app_token/table_id"},
        }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def test_load_option_positions_repo_bootstraps_position_lots_from_feishu(tmp_path: Path) -> None:
    import scripts.option_positions_core.service as svc

    data_config = _write_data_config(tmp_path / "data.json", sqlite_path=tmp_path / "option_positions.sqlite3")
    old_list = svc._list_feishu_option_position_records
    try:
        svc._list_feishu_option_position_records = lambda _ref: [  # type: ignore[assignment]
            {
                "record_id": "rec_1",
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
            }
        ]
        repo = svc.load_option_positions_repo(data_config)
    finally:
        svc._list_feishu_option_position_records = old_list  # type: ignore[assignment]

    records = repo.list_records(page_size=10)
    assert len(records) == 1
    assert records[0]["record_id"] == "rec_1"
    assert repo.count_position_lots() == 1


def test_load_option_positions_repo_migrates_legacy_rows_when_lots_missing(tmp_path: Path) -> None:
    import scripts.option_positions_core.service as svc

    db_path = tmp_path / "option_positions.sqlite3"
    repo = svc.SQLiteOptionPositionsRepository(db_path)
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
    loaded = svc.load_option_positions_repo(data_config)

    rows = loaded.list_records(page_size=10)
    assert len(rows) == 1
    assert rows[0]["record_id"] == "legacy_1"
    assert rows[0]["fields"]["symbol"] == "AAPL"


def test_load_option_positions_repo_supports_sqlite_only_mode(tmp_path: Path) -> None:
    import scripts.option_positions_core.service as svc
    from scripts.option_positions_core.domain import OpenPositionCommand

    data_config = _write_data_config(
        tmp_path / "data.json",
        sqlite_path=tmp_path / "option_positions.sqlite3",
        with_feishu=False,
    )
    repo = svc.load_option_positions_repo(data_config)
    created = svc.persist_manual_open_event(
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


def test_persist_trade_event_builds_position_lots_projection(tmp_path: Path) -> None:
    import scripts.option_positions_core.service as svc
    from scripts.trade_event_normalizer import NormalizedTradeDeal

    repo = svc.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
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

    first = svc.persist_trade_event(repo, open_deal)
    second = svc.persist_trade_event(repo, close_deal)

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


def test_persist_manual_open_event_builds_position_lot(tmp_path: Path) -> None:
    import scripts.option_positions_core.service as svc
    from scripts.option_positions_core.domain import OpenPositionCommand

    repo = svc.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    result = svc.persist_manual_open_event(
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
    lots = repo.list_position_lots()
    assert len(lots) == 1
    assert lots[0]["fields"]["contracts_open"] == 2
    assert lots[0]["fields"]["status"] == "open"


def test_persist_manual_close_event_updates_position_lot(tmp_path: Path) -> None:
    import scripts.option_positions_core.service as svc
    from scripts.option_positions_core.domain import OpenPositionCommand

    repo = svc.SQLiteOptionPositionsRepository(tmp_path / "option_positions.sqlite3")
    svc.persist_manual_open_event(
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

    fields = repo.list_position_lots()[0]["fields"]
    result = svc.persist_manual_close_event(
        repo,
        record_id="lot_manual",
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


def test_load_option_positions_repo_raises_on_malformed_feishu_config(tmp_path: Path) -> None:
    import pytest
    import scripts.option_positions_core.service as svc

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

    with pytest.raises(SystemExit, match="data config missing feishu app_id/app_secret/option_positions"):
        svc.load_option_positions_repo(data_config)


def test_load_option_positions_repo_rejects_non_object_feishu_config(tmp_path: Path) -> None:
    import pytest
    import scripts.option_positions_core.service as svc

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

    with pytest.raises(SystemExit, match="data config feishu must be a JSON object"):
        svc.load_option_positions_repo(data_config)
