from contextlib import closing
import json
import sqlite3

import pytest

from src.application.ledger import api
from src.application.ledger.repository import SQLiteOptionPositionsRepository
from src.application.tool_execution import execute_tool
from tests.test_agent_plugin_smoke import _public_cfg_with_futu
from tests.test_trade_event_pagination import _event


INVALID_JSON_PAYLOADS = [
    pytest.param("[]", id="array"),
    pytest.param("null", id="null"),
    pytest.param("1", id="number"),
    pytest.param("true", id="boolean"),
    pytest.param('"text"', id="string"),
    pytest.param("not json", id="syntax"),
]
QUERY_JSON_COLUMNS = [("position_lots", "fields_json"), ("trade_events", "event_json")]


def _forbid(*_args, **_kwargs):
    raise AssertionError("public query entered the ledger writer/bootstrap")


@pytest.fixture
def query_store(tmp_path, monkeypatch):
    monkeypatch.setenv("OM_RUNTIME_ROOT", str(tmp_path))
    monkeypatch.setattr("src.application.agent_tools.positions.assert_quality_allows", lambda *a, **kw: None)
    monkeypatch.setattr("src.application.agent_tools.operations_impl.resolve_secret", lambda _name: "test-only-key")
    path = tmp_path / "output_shared/state/option_positions.sqlite3"
    data_config = tmp_path / "portfolio.runtime.json"
    data_config.write_text("{}")
    config = tmp_path / "config.us.json"
    cfg = _public_cfg_with_futu(str(data_config))
    cfg["accounts"] = ["lx"]
    config.write_text(json.dumps(cfg))
    return path, data_config, config


def _payload(config, action):
    payload = {"config_path": str(config), "action": action, "account": "lx"}
    if action == "history":
        payload["record_id"] = "lot_open"
    if action == "assigned-stock":
        payload["refresh_quotes"] = False
    return payload


def _corrupt_query_payload(path, table, column, payload):
    targets = {
        ("position_lots", "fields_json"): ("lot_id", "lot_open"),
        ("trade_events", "event_json"): ("event_id", "open"),
        ("assigned_stock_events", "event_json"): ("stock_event_id", "bad"),
    }
    key, target = targets[(table, column)]
    with closing(sqlite3.connect(path)) as conn:
        for (name,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name=?", (table,),
        ).fetchall():
            conn.execute('DROP TRIGGER "' + name.replace('"', '""') + '"')
        for (name,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL", (table,),
        ).fetchall():
            conn.execute('DROP INDEX "' + name.replace('"', '""') + '"')
        if table == "assigned_stock_events":
            conn.execute(
                "INSERT INTO assigned_stock_events(stock_event_id,account,event_json,trade_time_ms,created_at_ms,updated_at_ms) "
                "VALUES ('bad','lx',?,1,1,1)", (payload,),
            )
        else:
            changed = conn.execute(f"UPDATE {table} SET {column}=? WHERE {key}=?", (payload, target))
            assert changed.rowcount == 1
        conn.commit()


@pytest.mark.parametrize("action", ["list", "events", "history", "inspect", "assigned-stock"])
def test_public_tool_missing_store_never_creates_database(query_store, action):
    path, _, config = query_store
    result = execute_tool("option_positions_read", _payload(config, action))
    assert result["ok"] is False
    assert not path.parent.exists()


@pytest.mark.parametrize("action", ["list", "events", "history", "inspect", "assigned-stock"])
def test_public_tool_reads_pending_events_without_projection_recovery(query_store, monkeypatch, action):
    path, _, config = query_store
    writer = SQLiteOptionPositionsRepository(path)
    writer.upsert_trade_event(_event("open", event_time_ms=1_780_000_000_000))
    with sqlite3.connect(path) as conn:
        before = list(conn.iterdump())
        assert conn.execute("SELECT COUNT(*) FROM position_lots").fetchone()[0] == 0
        monkeypatch.setattr(SQLiteOptionPositionsRepository, "__init__", _forbid)
        monkeypatch.setattr(SQLiteOptionPositionsRepository, "_connect", _forbid)
        monkeypatch.setattr("src.application.ledger.bootstrap.load_option_positions_repo", _forbid)
        result = execute_tool("option_positions_read", _payload(config, action))
        assert result["ok"] is True, result
        assert result["data"]["bootstrap"]["status"] == "read_only"
        if action == "events":
            assert [row["event_id"] for row in result["data"]["rows"]] == ["open"]
        elif action == "history":
            assert result["data"]["event_count"] == 1
        elif action == "inspect":
            assert result["data"]["current_lots"] == []
            assert len(result["data"]["projected_lots"]) == 1
        elif action == "list":
            assert result["data"]["rows"] == []
        assert list(conn.iterdump()) == before


@pytest.mark.parametrize("action", ["list", "events", "history", "inspect"])
def test_cli_missing_store_does_not_initialize(query_store, action):
    from src.interfaces.cli.option_positions import main
    path, data_config, _ = query_store
    selectors = ["--record-id", "lot_open"] if action == "history" else ["--account", "lx"]
    with pytest.raises(sqlite3.OperationalError):
        main(["--data-config", str(data_config), action, *selectors])
    assert not path.parent.exists()


@pytest.mark.parametrize("action", ["list", "events", "history", "inspect"])
def test_cli_reads_pending_store_without_writer(query_store, monkeypatch, capsys, action):
    from src.interfaces.cli.option_positions import main
    path, data_config, _ = query_store
    writer = SQLiteOptionPositionsRepository(path)
    writer.upsert_trade_event(_event("open", event_time_ms=1_780_000_000_000))
    with sqlite3.connect(path) as conn:
        before = list(conn.iterdump())
        monkeypatch.setattr(SQLiteOptionPositionsRepository, "__init__", _forbid)
        monkeypatch.setattr(SQLiteOptionPositionsRepository, "_connect", _forbid)
        selectors = ["--record-id", "lot_open"] if action == "history" else ["--account", "lx"]
        assert main(["--data-config", str(data_config), action, *selectors, "--format", "json"]) == 0
        json.loads(capsys.readouterr().out)
        assert list(conn.iterdump()) == before


def test_query_reader_reuses_canonical_fields_and_paging(query_store, monkeypatch):
    path, data_config, _ = query_store
    writer = SQLiteOptionPositionsRepository(path)
    for index in range(3):
        writer.upsert_trade_event(_event(f"event{index}", event_time_ms=1_780_000_000_000 + index))
    api.refresh_position_lot_projection(writer)
    expected = writer.list_position_lots()
    expected_events = writer.list_trade_events()
    before = path.read_bytes()
    monkeypatch.setattr(SQLiteOptionPositionsRepository, "__init__", _forbid)
    monkeypatch.setattr(SQLiteOptionPositionsRepository, "_connect", _forbid)
    _, reader = api.open_position_query_ledger_from_data_config(base=data_config.parent, data_config=data_config)
    lots = reader.list_position_lots()
    assert lots == expected
    assert [row["lot_id"] for row in lots] == ["lot_event2", "lot_event1", "lot_event0"]
    assert reader.list_trade_events() == expected_events
    monkeypatch.setattr(reader, "list_trade_events", _forbid)
    first = api.trade_event_page(reader, payload={"limit": 2}, account="lx", market="us",
                                authorized_accounts=["lx"], cursor_key="test-key")
    second = api.trade_event_page(reader, payload={"cursor": first["next_cursor"]}, account="lx", market="us",
                                 authorized_accounts=["lx"], cursor_key="test-key")
    assert [row["event_id"] for row in first["rows"] + second["rows"]] == ["event2", "event1", "event0"]
    with closing(reader._connect()) as conn:
        assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM position_lots")
    assert path.read_bytes() == before


def test_custom_runtime_root_is_preserved(tmp_path, monkeypatch):
    monkeypatch.setenv("OM_RUNTIME_ROOT", str(tmp_path / "wrong"))
    target = tmp_path / "selected"
    path = target / "output_shared/state/option_positions.sqlite3"
    SQLiteOptionPositionsRepository(path)
    _, reader = api.open_position_query_ledger_from_data_config(
        base=tmp_path, data_config=tmp_path / "portfolio.runtime.json", runtime_root=target,
    )
    assert reader.db_path == path
    assert api.ledger_store_payload(tmp_path / "portfolio.runtime.json", reader)["runtime_root"] == str(target)
    assert reader.list_position_lots() == []
    assert not (tmp_path / "wrong").exists()


@pytest.mark.parametrize("mutation", ["DROP TABLE position_lots", "DROP TABLE trade_events"])
def test_required_missing_schema_fails_without_repair(query_store, mutation):
    path, data_config, _ = query_store
    SQLiteOptionPositionsRepository(path)
    with sqlite3.connect(path) as conn:
        conn.execute(mutation)
        before = list(conn.iterdump())
        _, reader = api.open_position_query_ledger_from_data_config(base=data_config.parent, data_config=data_config)
        with pytest.raises(sqlite3.OperationalError):
            api.list_position_lot_snapshots(reader)
        assert list(conn.iterdump()) == before


@pytest.mark.parametrize("payload", INVALID_JSON_PAYLOADS)
@pytest.mark.parametrize("table,column,read", [
    ("position_lots", "fields_json", "list_position_lots"),
    ("trade_events", "event_json", "list_trade_events"),
    ("assigned_stock_events", "event_json", "list_assigned_stock_events"),
])
def test_invalid_json_is_not_silently_dropped(query_store, monkeypatch, table, column, read, payload):
    path, data_config, _ = query_store
    writer = SQLiteOptionPositionsRepository(path)
    writer.upsert_trade_event(_event("open", event_time_ms=1_780_000_000_000))
    api.refresh_position_lot_projection(writer)
    _corrupt_query_payload(path, table, column, payload)
    monkeypatch.setattr(SQLiteOptionPositionsRepository, "__init__", _forbid)
    monkeypatch.setattr(SQLiteOptionPositionsRepository, "_connect", _forbid)
    monkeypatch.setattr("src.application.ledger.bootstrap.load_option_positions_repo", _forbid)
    _, reader = api.open_position_query_ledger_from_data_config(base=data_config.parent, data_config=data_config)
    with closing(sqlite3.connect(path)) as conn:
        before = list(conn.iterdump())
        with pytest.raises(ValueError) as error:
            getattr(reader, read)()
        if payload != "not json":
            assert "stored ledger JSON value must be an object" in str(error.value)
        assert list(conn.iterdump()) == before


@pytest.mark.parametrize("payload", INVALID_JSON_PAYLOADS)
@pytest.mark.parametrize("table,column", QUERY_JSON_COLUMNS)
@pytest.mark.parametrize("action", ["list", "history", "inspect"])
def test_public_tool_rejects_corrupt_query_payload(query_store, monkeypatch, action, table, column, payload):
    path, _, config = query_store
    writer = SQLiteOptionPositionsRepository(path)
    writer.upsert_trade_event(_event("good", symbol="AAPL", event_time_ms=1_780_000_000_000))
    writer.upsert_trade_event(_event("open", event_time_ms=1_780_000_000_001))
    api.refresh_position_lot_projection(writer)
    request = _payload(config, action)
    valid = execute_tool("option_positions_read", request)
    assert valid["ok"] is True, valid
    if action == "list":
        assert {row["symbol"] for row in valid["data"]["rows"]} == {"NVDA", "AAPL"}
    elif action == "history":
        assert [row["event_id"] for row in valid["data"]["events"]] == ["open"]
    else:
        assert {row["record_id"] for row in valid["data"]["current_lots"]} == {"lot_open", "lot_good"}

    _corrupt_query_payload(path, table, column, payload)
    monkeypatch.setattr(SQLiteOptionPositionsRepository, "__init__", _forbid)
    monkeypatch.setattr(SQLiteOptionPositionsRepository, "_connect", _forbid)
    monkeypatch.setattr("src.application.ledger.bootstrap.load_option_positions_repo", _forbid)
    with closing(sqlite3.connect(path)) as conn:
        before = list(conn.iterdump())
        result = execute_tool("option_positions_read", request)
        assert result["ok"] is False, result
        assert result["data"] == {}
        assert result["error"]["code"] == ("INPUT_ERROR" if action == "history" else "INTERNAL_ERROR")
        if payload != "not json":
            assert "stored ledger JSON value must be an object" in result["error"]["message"]
        else:
            assert "Expecting value" in result["error"]["message"]
        assert list(conn.iterdump()) == before


@pytest.mark.parametrize("payload", INVALID_JSON_PAYLOADS)
@pytest.mark.parametrize("action,table,column", [
    (action, table, column)
    for action in ("list", "history", "inspect")
    for table, column in QUERY_JSON_COLUMNS
] + [("events", "trade_events", "event_json")])
def test_cli_rejects_corrupt_query_payload(query_store, monkeypatch, capsys, action, table, column, payload):
    from src.interfaces.cli.option_positions import main

    path, data_config, _ = query_store
    writer = SQLiteOptionPositionsRepository(path)
    writer.upsert_trade_event(_event("good", symbol="AAPL", event_time_ms=1_780_000_000_000))
    writer.upsert_trade_event(_event("open", event_time_ms=1_780_000_000_001))
    api.refresh_position_lot_projection(writer)
    selectors = ["--record-id", "lot_open"] if action == "history" else ["--account", "lx"]
    argv = ["--data-config", str(data_config), action, *selectors, "--format", "json"]
    assert main(argv) == 0
    valid = json.loads(capsys.readouterr().out)
    if action == "inspect":
        assert len(valid["current_lots"]) == 2
    else:
        assert len(valid) == (1 if action == "history" else 2)

    _corrupt_query_payload(path, table, column, payload)
    monkeypatch.setattr(SQLiteOptionPositionsRepository, "__init__", _forbid)
    monkeypatch.setattr(SQLiteOptionPositionsRepository, "_connect", _forbid)
    monkeypatch.setattr("src.application.ledger.bootstrap.load_option_positions_repo", _forbid)
    with closing(sqlite3.connect(path)) as conn:
        before = list(conn.iterdump())
        with pytest.raises(SystemExit if action == "history" else ValueError) as error:
            main(argv)
        if payload != "not json":
            assert "stored ledger JSON value must be an object" in str(error.value)
        else:
            assert "Expecting value" in str(error.value)
        assert capsys.readouterr().out == ""
        assert list(conn.iterdump()) == before


def test_public_empty_query_is_not_corruption(query_store, monkeypatch, capsys):
    from src.interfaces.cli.option_positions import main

    path, data_config, config = query_store
    SQLiteOptionPositionsRepository(path)
    monkeypatch.setattr(SQLiteOptionPositionsRepository, "__init__", _forbid)
    monkeypatch.setattr(SQLiteOptionPositionsRepository, "_connect", _forbid)
    monkeypatch.setattr("src.application.ledger.bootstrap.load_option_positions_repo", _forbid)
    with closing(sqlite3.connect(path)) as conn:
        before = list(conn.iterdump())
        for action in ("list", "history"):
            result = execute_tool("option_positions_read", _payload(config, action))
            assert result["ok"] is True, result
            data = result["data"]
            assert data["coverage"]["status"] == "complete"
            assert data["coverage"]["complete_for"] == "full_query"
            if action == "list":
                assert data["rows"] == []
                assert data["row_count"] == 0
            else:
                assert data["events"] == []
                assert data["event_count"] == 0
                assert data["read_status"] == "not_found"
        for action in ("list", "events"):
            assert main(["--data-config", str(data_config), action, "--account", "lx", "--format", "json"]) == 0
            assert json.loads(capsys.readouterr().out) == []
        with pytest.raises(SystemExit, match="position lot or event history not found: lot_open"):
            main(["--data-config", str(data_config), "history", "--record-id", "lot_open", "--format", "json"])
        assert capsys.readouterr().out == ""
        assert list(conn.iterdump()) == before


@pytest.mark.parametrize("table,column,read", [
    ("position_lots", "fields_json", "list_position_lots"),
    ("trade_events", "event_json", "list_trade_events"),
])
def test_query_reader_accepts_empty_json_object(query_store, monkeypatch, table, column, read):
    path, data_config, _ = query_store
    writer = SQLiteOptionPositionsRepository(path)
    writer.upsert_trade_event(_event("open", event_time_ms=1_780_000_000_000))
    api.refresh_position_lot_projection(writer)
    _corrupt_query_payload(path, table, column, "{}")
    monkeypatch.setattr(SQLiteOptionPositionsRepository, "__init__", _forbid)
    monkeypatch.setattr(SQLiteOptionPositionsRepository, "_connect", _forbid)
    monkeypatch.setattr("src.application.ledger.bootstrap.load_option_positions_repo", _forbid)
    _, reader = api.open_position_query_ledger_from_data_config(base=data_config.parent, data_config=data_config)
    with closing(sqlite3.connect(path)) as conn:
        before = list(conn.iterdump())
        rows = getattr(reader, read)()
        assert len(rows) == 1
        assert (rows[0]["fields"] if table == "position_lots" else rows[0]) == {}
        assert list(conn.iterdump()) == before


@pytest.mark.parametrize("corrupt", [False, True])
@pytest.mark.parametrize("table,column,read", [
    ("position_lots", "fields_json", "list_position_lots"),
    ("trade_events", "event_json", "list_trade_events"),
])
def test_query_readers_preserve_supplied_connection(query_store, monkeypatch, table, column, read, corrupt):
    path, data_config, _ = query_store
    writer = SQLiteOptionPositionsRepository(path)
    writer.upsert_trade_event(_event("open", event_time_ms=1_780_000_000_000))
    api.refresh_position_lot_projection(writer)
    if corrupt:
        _corrupt_query_payload(path, table, column, "[]")
    monkeypatch.setattr(SQLiteOptionPositionsRepository, "__init__", _forbid)
    monkeypatch.setattr(SQLiteOptionPositionsRepository, "_connect", _forbid)
    monkeypatch.setattr("src.application.ledger.bootstrap.load_option_positions_repo", _forbid)
    _, reader = api.open_position_query_ledger_from_data_config(base=data_config.parent, data_config=data_config)
    with closing(sqlite3.connect(path)) as observer, closing(reader._connect()) as conn:
        before = list(observer.iterdump())
        assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
        conn.execute("BEGIN")
        statements = []
        conn.set_trace_callback(statements.append)
        monkeypatch.setattr(reader, "_connect", _forbid)
        if corrupt:
            with pytest.raises(ValueError, match="stored ledger JSON value must be an object"):
                getattr(reader, read)(conn=conn)
        else:
            assert len(getattr(reader, read)(conn=conn)) == 1
        assert conn.in_transaction
        assert conn.execute("SELECT 1").fetchone()[0] == 1
        assert not any(sql.strip().upper().startswith(("COMMIT", "ROLLBACK")) for sql in statements)
        assert sum(f"FROM {table}" in sql for sql in statements) == 1
        assert list(observer.iterdump()) == before


def test_public_query_keeps_account_scope(query_store, monkeypatch):
    path, _, config = query_store
    writer = SQLiteOptionPositionsRepository(path)
    writer.upsert_trade_event(_event("open", account="lx", event_time_ms=1_780_000_000_000))
    writer.upsert_trade_event(_event("foreign", account="sy", event_time_ms=1_780_000_000_001))
    api.refresh_position_lot_projection(writer)
    monkeypatch.setattr(SQLiteOptionPositionsRepository, "__init__", _forbid)
    monkeypatch.setattr(SQLiteOptionPositionsRepository, "_connect", _forbid)
    monkeypatch.setattr("src.application.ledger.bootstrap.load_option_positions_repo", _forbid)
    with closing(sqlite3.connect(path)) as conn:
        before = list(conn.iterdump())
        result = execute_tool("option_positions_read", _payload(config, "list"))
        assert result["ok"] is True, result
        assert [row["account"] for row in result["data"]["rows"]] == ["lx"]
        denied = execute_tool("option_positions_read", {**_payload(config, "list"), "account": "sy"})
        assert denied["ok"] is False, denied
        assert denied["error"]["code"] == "INPUT_ERROR"
        history = execute_tool("option_positions_read", {**_payload(config, "history"), "record_id": "lot_foreign"})
        assert history["ok"] is False, history
        assert history["error"]["code"] == "PERMISSION_DENIED"
        assert list(conn.iterdump()) == before


def test_public_events_does_not_install_pagination_migration(query_store):
    from src.application.ledger.repository_trade_schema import TRADE_EVENT_PAGINATION_TRIGGERS
    path, _, config = query_store
    SQLiteOptionPositionsRepository(path)
    with sqlite3.connect(path) as conn:
        conn.execute(f'DROP TRIGGER "{TRADE_EVENT_PAGINATION_TRIGGERS[0]}"')
        before = list(conn.iterdump())
        result = execute_tool("option_positions_read", _payload(config, "events"))
        assert result["ok"] is False
        assert result["error"]["code"] == "DEPENDENCY_MISSING"
        assert list(conn.iterdump()) == before


@pytest.mark.parametrize("mutation", [
    "ALTER TABLE position_lots RENAME COLUMN lot_id TO record_id",
    "ALTER TABLE position_lots ADD COLUMN unexpected TEXT",
    "ALTER TABLE wheel_events RENAME COLUMN lot_id TO stock_lot_id",
])

@pytest.mark.parametrize("action", ["list", "events", "history", "inspect", "assigned-stock"])
def test_unsupported_schema_in_active_wal_is_rejected_without_repair(query_store, mutation, action):
    path, _, config = query_store
    SQLiteOptionPositionsRepository(path)
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(mutation)
        conn.commit()
        assert path.with_name(path.name + "-wal").exists()
        before = list(conn.iterdump())
        result = execute_tool("option_positions_read", _payload(config, action))
        assert result["ok"] is False, result
        assert list(conn.iterdump()) == before


def test_missing_assigned_stock_storage_is_unavailable(query_store):
    path, data_config, config = query_store
    SQLiteOptionPositionsRepository(path)
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TABLE assigned_stock_events")
        before = list(conn.iterdump())
        _, reader = api.open_position_query_ledger_from_data_config(base=data_config.parent, data_config=data_config)
        with pytest.raises(sqlite3.OperationalError, match="assigned_stock_events"):
            reader.list_assigned_stock_events()
        result = execute_tool("option_positions_read", _payload(config, "assigned-stock"))
        assert result["ok"] is False, result
        assert list(conn.iterdump()) == before


def test_public_assigned_stock_preserves_wheel_and_sale_snapshot(query_store, monkeypatch):
    from domain.domain.wheel import build_legacy_wheel_event
    from src.application.ledger.manual_trades import persist_manual_open_event
    from src.application.positions.workflows import execute_manual_assigned_stock_sale
    from src.application.wheel import build_wheel_read_model

    path, data_config, config = query_store
    writer = SQLiteOptionPositionsRepository(path)
    instant = 1_780_000_000_000
    persist_manual_open_event(
        writer, broker="富途", account="lx", symbol="NVDA", option_type="put", side="short",
        contracts=1, currency="USD", strike=100, multiplier=100, expiration_ymd="2026-12-18",
        premium_per_share=2.5, opened_at_ms=instant,
    )
    lot = writer.list_position_lots()[0]
    api.record_manual_assignment(
        writer, lot_id=lot["record_id"], contracts_to_close=1,
        stock_side="buy", stock_qty=100, stock_price=100, as_of_ms=instant + 1000,
    )
    assignment = next(row for row in writer.list_trade_events() if row.get("event_type") == "assignment")
    stock_lot = f"assigned-stock-{assignment['event_id']}"
    event = build_legacy_wheel_event(
        event_id="wheel-start", account="lx", lot_id=stock_lot, event_type="wheel_started",
        occurred_at_ms=instant + 1000, recorded_at_ms=instant + 1001,
        source_trade_event_id=assignment["event_id"], payload={"request_id": "test-start"},
    )
    with writer._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        writer.append_wheel_event_once(event, conn=conn)
        conn.commit()
    execute_manual_assigned_stock_sale(
        writer, target_lot_id=stock_lot, account="lx", broker="富途", symbol="NVDA", currency="USD",
        shares=40, price=105, trade_time_ms=instant + 2000, dry_run=False,
    )
    expected_snapshot = writer.read_lifecycle_account_rows(account="lx")
    expected_wheel = build_wheel_read_model(writer, "lx", instant + 3000, market="us")
    assert expected_wheel["wheel_branches"]
    assert expected_snapshot["account_assigned_stock_events"]
    with sqlite3.connect(path) as conn:
        before = list(conn.iterdump())
        monkeypatch.setattr(SQLiteOptionPositionsRepository, "__init__", _forbid)
        monkeypatch.setattr(SQLiteOptionPositionsRepository, "_connect", _forbid)
        _, reader = api.open_position_query_ledger_from_data_config(base=data_config.parent, data_config=data_config)
        assert reader.read_lifecycle_account_rows(account="lx") == expected_snapshot
        result = execute_tool("option_positions_read", {
            **_payload(config, "assigned-stock"), "as_of_ms": instant + 3000,
            "quote_snapshots": [{"symbol": "NVDA", "spot": 105, "quote_time_ms": instant + 3000}],
        })
        assert result["ok"] is True, result
        assert result["data"]["wheel_branches"] == expected_wheel["wheel_branches"]
        assert result["data"]["rows"][0]["shares_remaining"] == 60
        assert result["data"]["rows"][0]["wheel"] == expected_wheel["batches"][0]
        assert list(conn.iterdump()) == before


@pytest.mark.parametrize("explicit_account", [True, False])
def test_public_inspect_authorizes_canonical_current_lot(query_store, monkeypatch, explicit_account):
    path, _, config = query_store
    writer = SQLiteOptionPositionsRepository(path)
    writer.upsert_trade_event(_event("open", event_time_ms=1_780_000_000_000))
    api.refresh_position_lot_projection(writer)
    payload = _payload(config, "inspect")
    payload["record_id"] = "lot_open"
    if not explicit_account:
        payload.pop("account")
    with sqlite3.connect(path) as conn:
        before = list(conn.iterdump())
        monkeypatch.setattr(SQLiteOptionPositionsRepository, "__init__", _forbid)
        monkeypatch.setattr(SQLiteOptionPositionsRepository, "_connect", _forbid)
        monkeypatch.setattr("src.application.ledger.bootstrap.load_option_positions_repo", _forbid)
        result = execute_tool("option_positions_read", payload)
        assert result["ok"] is True, result
        fields = result["data"]["current_lots"][0]["fields"]
        assert fields["contract_key"]["account"] == "lx"
        assert "account" not in fields
        assert list(conn.iterdump()) == before


@pytest.mark.parametrize("stored_account", ["sy", ""])
def test_public_inspect_denies_foreign_or_unknown_current_account(query_store, monkeypatch, stored_account):
    path, _, config = query_store
    writer = SQLiteOptionPositionsRepository(path)
    writer.upsert_trade_event(_event("open", event_time_ms=1_780_000_000_000))
    api.refresh_position_lot_projection(writer)
    payload = {"config_path": str(config), "action": "inspect", "record_id": "lot_open"}
    from src.application.agent_tools import positions
    inspect = positions.inspect_projection_state

    def inspect_with_invalid_account(*args, **kwargs):
        result = inspect(*args, **kwargs)
        fields = result["current_lots"][0]["fields"]
        fields["contract_key"]["account"] = stored_account
        if stored_account:
            fields["account"] = "lx"
        return result

    monkeypatch.setattr(positions, "inspect_projection_state", inspect_with_invalid_account)
    with sqlite3.connect(path) as conn:
        before = list(conn.iterdump())
        monkeypatch.setattr(SQLiteOptionPositionsRepository, "__init__", _forbid)
        monkeypatch.setattr(SQLiteOptionPositionsRepository, "_connect", _forbid)
        monkeypatch.setattr("src.application.ledger.bootstrap.load_option_positions_repo", _forbid)
        result = execute_tool("option_positions_read", payload)
        assert result["ok"] is False, result
        assert result["error"]["code"] == "PERMISSION_DENIED"
        assert list(conn.iterdump()) == before
