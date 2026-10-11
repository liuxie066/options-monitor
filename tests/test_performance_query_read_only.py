from contextlib import closing
import sqlite3

import pytest

from src.application.ledger import api as ledger_api
from src.application.ledger.read_only_evidence import open_performance_query_repo
from src.application.ledger.repository import SQLiteOptionPositionsRepository
from src.application.performance.service import OptionPerformanceReadError
from test_performance_service_v2 import _build_report, _event


def _forbid(*args, **kwargs):
    raise AssertionError('query entered a writer/bootstrap path')


def test_missing_database_is_not_created(tmp_path):
    path = tmp_path / 'missing' / 'ledger.sqlite3'
    with pytest.raises(OptionPerformanceReadError, match='unavailable'):
        _build_report(open_performance_query_repo(path))
    assert not path.parent.exists()


@pytest.mark.parametrize('mutation', [
    'DROP TABLE trade_events',
    'DROP TABLE position_lots',
    'ALTER TABLE position_lots RENAME COLUMN lot_id TO record_id',
    'ALTER TABLE trade_events RENAME COLUMN event_json TO broken',
    'ALTER TABLE trade_events RENAME COLUMN trade_time_ms TO broken',
])
def test_incomplete_or_unsupported_schema_fails_even_when_empty(tmp_path, mutation):
    path = tmp_path / 'ledger.sqlite3'
    SQLiteOptionPositionsRepository(path)
    with sqlite3.connect(path) as conn:
        conn.execute(mutation)
    with pytest.raises(OptionPerformanceReadError) as exc:
        _build_report(open_performance_query_repo(path))
    assert exc.value.reason_codes == ('ledger_read_failed',)


@pytest.mark.parametrize('payload', ['not json', '[]', 'null', '{}'])
def test_invalid_stored_event_is_an_explicit_failure(tmp_path, payload):
    path = tmp_path / 'ledger.sqlite3'
    SQLiteOptionPositionsRepository(path)
    with sqlite3.connect(path) as conn:
        for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='trade_events'").fetchall():
            conn.execute('DROP TRIGGER "' + name.replace('"', '""') + '"')
        for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='trade_events' AND sql IS NOT NULL").fetchall():
            conn.execute('DROP INDEX "' + name.replace('"', '""') + '"')
        conn.execute('INSERT INTO trade_events(event_id,event_json,trade_time_ms,created_at_ms,updated_at_ms) VALUES (?,?,?,?,?)', ('bad', payload, 1, 1, 1))
    with pytest.raises(OptionPerformanceReadError):
        _build_report(open_performance_query_repo(path))


def test_initialized_empty_ledger_is_valid_and_query_only(tmp_path, monkeypatch):
    path = tmp_path / 'ledger.sqlite3'
    SQLiteOptionPositionsRepository(path)
    before = path.read_bytes()
    monkeypatch.setattr(SQLiteOptionPositionsRepository, '__init__', _forbid)
    monkeypatch.setattr(SQLiteOptionPositionsRepository, '_connect', _forbid)
    reader = open_performance_query_repo(path)
    report = _build_report(reader, include_rows=True)
    assert report['rows'] == []
    assert report['option_net_cashflow']['cny_total']['amount'] == 0
    with closing(reader._connect()) as conn:
        assert conn.execute('PRAGMA query_only').fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            conn.execute('DELETE FROM trade_events')
    assert path.read_bytes() == before


def test_populated_wal_matches_canonical_without_projection_repair(tmp_path, monkeypatch):
    path = tmp_path / 'ledger.sqlite3'
    writer = SQLiteOptionPositionsRepository(path)
    writer.upsert_trade_event(_event('open', 'open', '2026-09-01T10:00:00'))
    expected = _build_report(writer, include_rows=True)
    with sqlite3.connect(path) as conn:
        conn.execute('PRAGMA journal_mode=WAL')
        before = list(conn.iterdump())
        assert conn.execute('SELECT COUNT(*) FROM position_lots').fetchone()[0] == 0
        monkeypatch.setattr(SQLiteOptionPositionsRepository, '__init__', _forbid)
        monkeypatch.setattr(SQLiteOptionPositionsRepository, '_connect', _forbid)
        monkeypatch.setattr(ledger_api, 'refresh_position_lot_projection', _forbid)
        reader = open_performance_query_repo(path)
        assert _build_report(reader, include_rows=True) == expected
        assert list(conn.iterdump()) == before


def test_public_opener_resolves_custom_local_path_without_bootstrap(tmp_path, monkeypatch):
    monkeypatch.setenv('OM_RUNTIME_ROOT', str(tmp_path))
    path = tmp_path / 'output_shared/state/option_positions.sqlite3'
    path.parent.mkdir(parents=True)
    SQLiteOptionPositionsRepository(path)
    monkeypatch.setattr(SQLiteOptionPositionsRepository, '__init__', _forbid)
    data_config = tmp_path / 'custom.runtime.json'
    resolved, reader = ledger_api.open_performance_query_ledger_from_data_config(base=tmp_path, data_config=data_config)
    assert resolved == data_config
    assert reader.db_path == path
    assert _build_report(reader, include_rows=True)['rows'] == []
