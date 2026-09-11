from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import src.application.ledger.repository_assigned_stock as assigned_stock_repository
import src.application.ledger.store_resolution as store_resolution
from src.application.ledger.api import (
    open_wheel_activation_repository,
    read_wheel_activation_windows_read_only,
    resolve_position_ledger_sqlite_path,
)
from src.application.ledger.repository import SQLiteOptionPositionsRepository


def _open_window(
    repo: SQLiteOptionPositionsRepository,
    *,
    market: str = "us",
    account: str = "lx",
    expected_generation: int = 0,
    policy_hash: str = "a" * 64,
    request_id: str = "activate-1",
    request_hash: str = "b" * 64,
) -> dict:
    with repo._writer_connection(begin_immediate=True) as conn:
        return repo.open_wheel_activation_window(
            market=market,
            account=account,
            expected_current_generation=expected_generation,
            policy_hash=policy_hash,
            request_id=request_id,
            request_hash=request_hash,
            conn=conn,
        )


def _close_window(
    repo: SQLiteOptionPositionsRepository,
    *,
    market: str = "us",
    account: str = "lx",
    expected_generation: int = 1,
    policy_hash: str = "a" * 64,
    request_id: str = "deactivate-1",
    request_hash: str = "c" * 64,
) -> dict:
    with repo._writer_connection(begin_immediate=True) as conn:
        return repo.close_wheel_activation_window(
            market=market,
            account=account,
            expected_current_generation=expected_generation,
            policy_hash=policy_hash,
            request_id=request_id,
            request_hash=request_hash,
            conn=conn,
        )


def test_wheel_activation_windows_are_transaction_timed_historical_and_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    timestamps = iter((1_000, 2_000, 3_000, 4_000))
    monkeypatch.setattr(assigned_stock_repository, "now_ms", lambda: next(timestamps))

    opened = _open_window(repo)
    replayed_open = _open_window(repo)

    assert opened["write_applied"] is True
    assert opened["window"]["generation"] == 1
    assert opened["window"]["activated_at_ms"] == 1_000
    assert replayed_open == {
        "action": "activate",
        "write_applied": False,
        "idempotent": True,
        "window": opened["window"],
    }
    assert repo.get_wheel_activation_window_for_event(
        market="us", account="lx", occurred_at_ms=999
    ) is None
    assert repo.get_wheel_activation_window_for_event(
        market="us", account="lx", occurred_at_ms=1_000
    ) == opened["window"]

    closed = _close_window(repo)
    replayed_close = _close_window(repo)

    assert closed["write_applied"] is True
    assert closed["window"]["deactivated_at_ms"] == 2_000
    assert replayed_close == {
        "action": "deactivate",
        "write_applied": False,
        "idempotent": True,
        "window": closed["window"],
    }
    assert repo.get_current_wheel_activation_window(market="us", account="lx") is None
    assert repo.get_wheel_activation_window_for_event(
        market="us", account="lx", occurred_at_ms=1_999
    ) == closed["window"]
    assert repo.get_wheel_activation_window_for_event(
        market="us", account="lx", occurred_at_ms=2_000
    ) is None

    reopened = _open_window(
        repo,
        expected_generation=1,
        policy_hash="d" * 64,
        request_id="activate-2",
        request_hash="e" * 64,
    )
    hk_opened = _open_window(
        repo,
        market="hk",
        policy_hash="f" * 64,
        request_id="activate-hk-1",
        request_hash="1" * 64,
    )

    assert reopened["window"]["generation"] == 2
    assert reopened["window"]["activated_at_ms"] == 3_000
    assert hk_opened["window"]["generation"] == 1
    assert hk_opened["window"]["activated_at_ms"] == 4_000
    assert [
        item["generation"]
        for item in repo.list_wheel_activation_windows(market="us", account="lx")
    ] == [1, 2]


def test_wheel_activation_cas_rejects_conflicts_and_immutable_boundary_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    monkeypatch.setattr(assigned_stock_repository, "now_ms", lambda: 1_000)
    opened = _open_window(repo)

    with pytest.raises(ValueError, match="request conflict"):
        _open_window(repo, request_hash="9" * 64)
    with pytest.raises(ValueError, match="generation conflict"):
        _close_window(repo, expected_generation=0)
    with pytest.raises(ValueError, match="policy hash conflict"):
        _close_window(repo, policy_hash="8" * 64)

    with repo._connect() as conn, pytest.raises(
        sqlite3.IntegrityError, match="boundaries are immutable"
    ):
        conn.execute(
            """
            UPDATE wheel_activation_windows
            SET policy_hash = ?
            WHERE market = 'us' AND account = 'lx' AND generation = 1
            """,
            ("7" * 64,),
        )
    assert repo.get_current_wheel_activation_window(
        market="us", account="lx"
    ) == opened["window"]
    with repo._connect() as conn, pytest.raises(
        sqlite3.IntegrityError, match="append-only"
    ):
        conn.execute(
            "DELETE FROM wheel_activation_windows WHERE market = 'us' AND account = 'lx'"
        )

    closed = _close_window(repo)
    assert closed["window"]["deactivated_at_ms"] == 1_001
    with repo._connect() as conn, pytest.raises(
        sqlite3.IntegrityError, match="must not overlap"
    ):
        conn.execute(
            """
            INSERT INTO wheel_activation_windows (
              market, account, generation, activated_at_ms, deactivated_at_ms,
              policy_hash, activation_request_id, activation_request_hash,
              deactivation_request_id, deactivation_request_hash
            ) VALUES ('us', 'lx', 2, 1000, 1500, ?, 'overlap-open', ?, 'overlap-close', ?)
            """,
            ("6" * 64, "5" * 64, "4" * 64),
        )
    with repo._connect() as conn, pytest.raises(
        sqlite3.IntegrityError, match="boundaries are immutable"
    ):
        conn.execute(
            """
            UPDATE wheel_activation_windows
            SET deactivated_at_ms = 2000
            WHERE market = 'us' AND account = 'lx' AND generation = 1
            """
        )


def test_wheel_activation_write_rolls_back_with_caller_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    monkeypatch.setattr(assigned_stock_repository, "now_ms", lambda: 1_000)

    with pytest.raises(RuntimeError, match="forced rollback"):
        with repo._writer_connection(begin_immediate=True) as conn:
            repo.open_wheel_activation_window(
                market="us",
                account="lx",
                expected_current_generation=0,
                policy_hash="a" * 64,
                request_id="activate-rollback",
                request_hash="b" * 64,
                conn=conn,
            )
            raise RuntimeError("forced rollback")

    assert repo.list_wheel_activation_windows(market="us", account="lx") == []


def test_wheel_activation_read_only_history_is_exact_and_explicit_when_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing_path = tmp_path / "missing.sqlite3"
    assert read_wheel_activation_windows_read_only(
        missing_path, "us", "lx"
    ) == {"windows": [], "source_status": "missing_database"}
    assert not missing_path.exists()

    no_table_path = tmp_path / "no-table.sqlite3"
    with sqlite3.connect(no_table_path) as conn:
        conn.execute("CREATE TABLE unrelated (value INTEGER)")
    assert read_wheel_activation_windows_read_only(
        no_table_path, "us", "lx"
    ) == {"windows": [], "source_status": "missing_table"}

    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    timestamps = iter((1_000, 2_000, 3_000, 4_000))
    monkeypatch.setattr(assigned_stock_repository, "now_ms", lambda: next(timestamps))
    first = _open_window(repo)
    closed = _close_window(repo)
    second = _open_window(
        repo,
        expected_generation=1,
        policy_hash="d" * 64,
        request_id="activate-2",
        request_hash="e" * 64,
    )
    _open_window(
        repo,
        market="hk",
        policy_hash="f" * 64,
        request_id="activate-hk",
        request_hash="1" * 64,
    )

    result = read_wheel_activation_windows_read_only(repo.db_path, "US", "lx")

    assert result == {
        "windows": [closed["window"], second["window"]],
        "source_status": "available",
    }
    assert result["windows"][0]["activation_request_id"] == first["window"][
        "activation_request_id"
    ]
    assert result["windows"][0]["deactivation_request_id"] == "deactivate-1"


def test_wheel_activation_read_only_does_not_create_wal_sidecars(
    tmp_path: Path,
) -> None:
    repo = SQLiteOptionPositionsRepository(tmp_path / "ledger.sqlite3")
    _open_window(repo)
    active_connection = repo._connect()
    try:
        wal = Path(f"{repo.db_path}-wal")
        shm = Path(f"{repo.db_path}-shm")
        assert wal.exists() and shm.exists()
        before_names = {item.name for item in tmp_path.iterdir()}
        before_database = Path(repo.db_path).read_bytes()
        before_wal = wal.read_bytes()

        result = read_wheel_activation_windows_read_only(repo.db_path, "us", "lx")

        assert result["source_status"] == "available"
        assert {item.name for item in tmp_path.iterdir()} == before_names
        assert Path(repo.db_path).read_bytes() == before_database
        assert wal.read_bytes() == before_wal
    finally:
        active_connection.close()

    Path(f"{repo.db_path}-wal").unlink(missing_ok=True)
    assert read_wheel_activation_windows_read_only(
        repo.db_path, "us", "lx"
    ) == {"windows": [], "source_status": "unreadable"}
    Path(f"{repo.db_path}-shm").unlink(missing_ok=True)
    assert not Path(f"{repo.db_path}-wal").exists()
    assert not Path(f"{repo.db_path}-shm").exists()


def test_wheel_activation_path_resolution_is_pure_and_writer_skips_bootstrap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        store_resolution.sqlite3,
        "connect",
        lambda *_args, **_kwargs: pytest.fail("path resolution opened SQLite"),
    )
    runtime_root = tmp_path / "runtime"
    path = resolve_position_ledger_sqlite_path(
        base=tmp_path,
        data_config=tmp_path / "portfolio.runtime.json",
        runtime_root=runtime_root,
    )
    assert path == (
        runtime_root / "output_shared" / "state" / "option_positions.sqlite3"
    ).resolve()

    monkeypatch.undo()
    import src.application.ledger.bootstrap as ledger_bootstrap

    monkeypatch.setattr(
        ledger_bootstrap,
        "load_option_positions_repo",
        lambda *_args, **_kwargs: pytest.fail("activation writer ran bootstrap"),
    )
    repo = open_wheel_activation_repository(path)
    assert repo.db_path == path
