from contextlib import closing
import sqlite3

import pytest
from cash_evidence_helpers import cash_config, cash_portfolio

from src.application.ledger.api import read_position_query_ledger
from src.application.ledger.decision_snapshot import _read_position_query_snapshot as read_position_query_snapshot
from src.application.ledger.repository import SQLiteOptionPositionsRepository
from src.application.ledger.manual_trades import persist_manual_open_event


def forbid(*args, **kwargs):
    raise AssertionError("read query entered writer/bootstrap")


def initialized(tmp_path, monkeypatch, *, populated=False):
    monkeypatch.setenv("OM_RUNTIME_ROOT", str(tmp_path))
    path = tmp_path / "output_shared/state/option_positions.sqlite3"
    writer = SQLiteOptionPositionsRepository(path)
    if populated:
        persist_manual_open_event(
            writer, broker="futu", account="lx", symbol="NVDA", option_type="put",
            side="short", contracts=1, currency="USD", strike=100, multiplier=100,
            expiration_ymd="2099-09-18", premium_per_share=2, opened_at_ms=1000,
            request_id="readonly-fixture",
        )
    return path, writer


def test_missing_query_ledger_creates_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("OM_RUNTIME_ROOT", str(tmp_path / "missing"))
    with pytest.raises(sqlite3.Error):
        read_position_query_ledger(base=tmp_path, accounts=["lx"], data_config=tmp_path / "data.json")
    assert not (tmp_path / "missing").exists()


@pytest.mark.parametrize("populated", [False, True])
def test_verified_snapshot_uses_no_writer_and_preserves_db(tmp_path, monkeypatch, populated):
    path, writer = initialized(tmp_path, monkeypatch, populated=populated)
    from src.application.ledger.api import decision_state_snapshot
    from domain.domain.portfolio_scope import portfolio_scope_id
    expected = decision_state_snapshot(writer, account="lx", portfolio_scope_id=portfolio_scope_id("lx"))
    before = path.read_bytes()
    lock = path.with_name(path.name + ".writer.lock")
    if lock.exists():
        lock.unlink()
    monkeypatch.setattr(SQLiteOptionPositionsRepository, "_connect", forbid)
    monkeypatch.setattr(SQLiteOptionPositionsRepository, "_writer_lock", forbid)
    monkeypatch.setattr(SQLiteOptionPositionsRepository, "_init_db", forbid)
    repo, snapshots = read_position_query_snapshot(path, accounts=["lx"])
    assert snapshots["lx"]["snapshot_status"] == "trusted"
    assert snapshots["lx"]["decision_state_fingerprint"] == expected["decision_state_fingerprint"]
    assert snapshots["lx"]["account_lifecycle_resolution"] == expected["account_lifecycle_resolution"]
    assert len(repo.list_position_lots()) == int(populated)
    assert path.read_bytes() == before
    assert not lock.exists()


@pytest.mark.parametrize("mutation", [
    "DROP TABLE trade_events", "DROP TABLE position_lots", "DROP TABLE trade_lifecycle_cases",
    "ALTER TABLE position_lots RENAME COLUMN lot_id TO record_id",
    "DELETE FROM position_lots",
])
def test_bad_schema_or_projection_is_unavailable_without_repair(tmp_path, monkeypatch, mutation):
    path, writer = initialized(tmp_path, monkeypatch, populated=True)
    with closing(sqlite3.connect(path)) as conn:
        conn.create_function("om_trade_attribution_writer_v1", 0, lambda: 1)
        conn.execute(mutation)
        conn.commit()
        before = list(conn.iterdump())
        monkeypatch.setattr(SQLiteOptionPositionsRepository, "_connect", forbid)
        with pytest.raises((sqlite3.Error, ValueError, RuntimeError)):
            read_position_query_snapshot(path, accounts=["lx"])
        assert list(conn.iterdump()) == before
        from src.application import cash_headroom_query as query
        monkeypatch.setattr(query, "load_account_portfolio_context", lambda **kw: cash_portfolio({"cash_by_currency": {"CNY": 10000}}))
        result = query.query_sell_put_cash(base_dir=tmp_path, account="lx", runtime_config={},
            data_config=tmp_path / "data.json", no_exchange_rates=True, write_cache=False, output_format="json")
        assert result["cash_available_cny"] == 10000
        assert result["cash_secured_usage_reliable"] is False
        assert result["cash_secured_used_cny"] is None
        assert result["cash_free_cny"] is None
        assert "ledger_unavailable" in result["cash_secured_unavailable_reason"]
        assert list(conn.iterdump()) == before


def test_cash_tool_preserves_cash_but_missing_occupancy_is_not_zero(tmp_path, monkeypatch):
    from src.application import cash_headroom_query as query
    from src.application.agent_tools.materialization_impl import query_cash_headroom_tool
    monkeypatch.setenv("OM_RUNTIME_ROOT", str(tmp_path / "missing"))
    monkeypatch.setattr(query, "fetch_futu_portfolio_context", lambda **kw: cash_portfolio({"cash_by_currency": {"CNY": 10000}}))
    monkeypatch.setattr(query, "resolve_data_config_path", lambda **kw: tmp_path / "data.json")
    result, warnings, meta = query_cash_headroom_tool(
        {"account": "lx", "no_exchange_rates": True},
        load_runtime_config=lambda **kw: (tmp_path / "config.json", cash_config()),
        resolve_public_data_config_path=lambda *a: tmp_path / "data.json", normalize_broker=lambda _: "futu",
        resolve_output_root=lambda _: tmp_path / "tool-output", query_sell_put_cash=query.query_sell_put_cash,
        repo_base=lambda: tmp_path, mask_path=str,
    )
    assert result["cash_available_cny"] == 10000
    assert result["cash_secured_usage_reliable"] is False
    assert result["cash_secured_used_cny"] is None
    assert result["cash_free_cny"] is None
    assert result["cash_free_total_cny"] is None
    assert "ledger_unavailable" in result["cash_secured_unavailable_reason"]
    assert not (tmp_path / "missing").exists()
    assert not (tmp_path / "tool-output").exists()


def test_valid_empty_ledger_is_proven_zero_occupancy(tmp_path, monkeypatch):
    from src.application import cash_headroom_query as query
    initialized(tmp_path, monkeypatch)
    monkeypatch.setattr(query, "load_account_portfolio_context", lambda **kw: cash_portfolio({"cash_by_currency": {"CNY": 10000}}))
    result = query.query_sell_put_cash(base_dir=tmp_path, account="lx", runtime_config={},
        data_config=tmp_path / "data.json", no_exchange_rates=True, write_cache=False, output_format="json")
    assert result["cash_secured_usage_reliable"] is True
    assert result["cash_secured_used_cny"] == 0
    assert result["cash_free_cny"] == 10000


def test_assignment_facade_missing_ledger_does_not_initialize(tmp_path, monkeypatch):
    from src.application import portfolio_assignment_scenario as assignment
    monkeypatch.setenv("OM_RUNTIME_ROOT", str(tmp_path / "missing"))
    monkeypatch.setattr(assignment, "repo_base", lambda: tmp_path)
    monkeypatch.setattr(assignment, "load_runtime_config", lambda **kw: (tmp_path / "config.us.json", {"accounts": ["lx"]}))
    result = assignment.query_portfolio_assignment_scenario(["lx"])
    assert result["status"] == "unavailable"
    assert not (tmp_path / "missing").exists()


def test_live_wal_and_other_account_do_not_trigger_recovery_or_scope_reads(tmp_path, monkeypatch):
    from src.application.ledger import decision_snapshot as decision
    path, writer = initialized(tmp_path, monkeypatch, populated=True)
    original = decision._read_account_trade_source_constraints
    accounts = []
    def scoped(repo, *, account, rows):
        accounts.append(account)
        return original(repo, account=account, rows=rows)
    monkeypatch.setattr(decision, "_read_account_trade_source_constraints", scoped)
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        before = list(conn.iterdump())
        repo, snapshots = read_position_query_snapshot(path, accounts=["lx"])
        assert len(repo.list_position_lots()) == 1
        assert snapshots["lx"]["snapshot_status"] == "trusted"
        assert accounts == ["lx"]
        assert list(conn.iterdump()) == before


def test_invalid_lot_json_is_not_silently_dropped(tmp_path, monkeypatch):
    path, writer = initialized(tmp_path, monkeypatch, populated=True)
    with closing(sqlite3.connect(path)) as conn:
        conn.create_function("om_trade_attribution_writer_v1", 0, lambda: 1)
        for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='position_lots'").fetchall():
            conn.execute('DROP TRIGGER "' + name.replace('"', '""') + '"')
        conn.execute("UPDATE position_lots SET fields_json='[]'")
        conn.commit()
        with pytest.raises(ValueError, match="JSON value must be an object"):
            read_position_query_snapshot(path, accounts=["lx"])
