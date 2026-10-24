"""Read facades distinguish unavailable ledger tables from valid zero rows."""
from __future__ import annotations

from contextlib import redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3

import pytest

from src.application.ledger import api, store_resolution


def _make_db(root: Path, shape: tuple[int | None, int | None] | str | None) -> Path:
    path = root / store_resolution.LEDGER_DB_RELATIVE_PATH
    path.parent.mkdir(parents=True)
    if shape is None:
        return path
    if shape == "invalid":
        path.write_bytes(b"synthetic non-SQLite ledger\n")
        return path
    if shape == "directory":
        path.mkdir()
        return path
    conn = sqlite3.connect(path)
    try:
        # Materialize even the zero-table fixture as a readable SQLite file.
        conn.execute("PRAGMA user_version=718")
        for table, count in zip(("trade_events", "position_lots"), shape):
            if count is not None:
                conn.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY)")
                conn.executemany(f"INSERT INTO {table} VALUES (?)", [(i,) for i in range(count)])
        conn.commit()
    finally:
        conn.close()
    return path


def _case(monkeypatch: pytest.MonkeyPatch, root: Path) -> tuple[Path, Path, Path]:
    from src.application.agent_tools import diagnostics, healthcheck_impl

    active = root / "active"
    repository = root / "repository"
    active.mkdir()
    repository.mkdir()
    env = root / "empty.env"
    env.write_text("")
    monkeypatch.setattr(os, "environ", {
        "HOME": str(root), "OM_ENV_FILE": str(env), "OM_RUNTIME_ROOT": str(active),
        "OM_SECRET_BACKEND": "env", "OM_QUALITY_ONBOARDED": "true",
    })
    monkeypatch.setattr(store_resolution, "REPO_BASE", repository)
    monkeypatch.setattr(store_resolution, "SYSTEMD_DEFAULT_RUNTIME_ROOT", root / "systemd")
    monkeypatch.setattr(diagnostics, "repo_base", lambda: repository)
    monkeypatch.setattr(healthcheck_impl, "repo_base", lambda: repository)

    class Gateway:
        def close(self) -> None:
            pass

    def no_bootstrap(*args, **kwargs):
        raise AssertionError("read facades must not bootstrap a ledger")

    monkeypatch.setattr(diagnostics, "run_futu_doctor", lambda **kw: {
        "ok": True, "sdk": {"ok": True}, "watchdog": {"ok": True},
    })
    monkeypatch.setattr(diagnostics, "build_ready_futu_broker_gateway", lambda **kw: Gateway())
    monkeypatch.setattr(diagnostics, "load_option_positions_repo", no_bootstrap)
    data = repository / "portfolio.runtime.json"
    data.write_text("{}")
    config = repository / "config.us.json"
    config.write_text(json.dumps({
        "_generated": {"schema_version": "1.0", "generator": "options-monitor", "source_format": "yaml", "market": "us"},
        "_resolved": {"source_format": "yaml", "market": "us", "runtime_schema": "config-json-v1"},
        "accounts": ["synthetic"],
        "account_settings": {"synthetic": {"type": "futu", "futu": {"account_id": "999000000000000002"}}},
        "portfolio": {"broker": "富途", "source": "futu", "account": "synthetic", "data_config": data.name},
        "trade_intake": {"enabled": True, "mode": "dry-run", "account_mapping": {"futu": {"999000000000000002": "synthetic"}}},
        "templates": {"put_base": {"sell_put": {"min_annualized_net_return": 0.1, "min_net_income": 50, "min_open_interest": 10, "min_volume": 1, "max_spread_ratio": 0.3}}},
        "symbols": [{"symbol": "NVDA", "market": "US", "fetch": {"source": "futu", "host": "127.0.0.1", "port": 11111, "limit_expirations": 8}, "use": ["put_base"], "sell_put": {"enabled": True, "min_dte": 20, "max_dte": 45, "min_strike": 100, "max_strike": 120}, "sell_call": {"enabled": False}}],
    }))
    return config, data, active


def _snapshot(root: Path) -> tuple[list[str], dict[str, str]]:
    paths = sorted(root.rglob("*"))
    return ([str(p.relative_to(root)) for p in paths], {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in paths if p.is_file() and p.name.endswith((".sqlite3", ".sqlite3-wal"))
    })


def _counts(value: dict) -> tuple[int | None, int | None]:
    return value["trade_event_count"], value["position_lot_count"]


@pytest.mark.parametrize("shape,expected,status", [
    ((None, None), (None, None), "warn"),
    ((0, None), (0, None), "warn"),
    ((5, None), (5, None), "warn"),
    ((None, 0), (None, 0), "warn"),
    ((None, 7), (None, 7), "warn"),
    ((0, 0), (0, 0), "ok"),
    ((5, 7), (5, 7), "ok"),
    (None, (None, None), "warn"),
    ("invalid", (None, None), "warn"),
    ("directory", (None, None), "warn"),
])
def test_healthcheck_and_inspect_preserve_missing_counts(monkeypatch, tmp_path, shape, expected, status):
    from src.application.tool_execution import execute_tool

    config, data, active = _case(monkeypatch, tmp_path)
    db = _make_db(active, shape)
    before = _snapshot(tmp_path)
    result = execute_tool("healthcheck", {
        "config_path": str(config), "env_file": str(tmp_path / "empty.env"),
        "audit_db": str(tmp_path / "absent-audit.sqlite3"), "include_service_status": False,
    })
    assert result["ok"], result
    checks = {item["name"]: item for item in result["data"]["checks"]}
    assert _counts(checks["ledger_store"]["value"]) == expected
    assert checks["ledger_store"]["status"] == status
    assert checks["option_positions_bootstrap"]["status"] == status
    assert checks["option_positions_bootstrap"]["value"]["status"] == "read_only_inspection"
    inspection = api.inspect_ledger_stores(data, runtime_root=active, config_path=config)
    assert _counts(inspection["active"]) == expected
    assert _counts(next(c for c in inspection["candidates"] if c["is_active"])) == expected
    assert _counts(api.ledger_store_payload(data)) == expected
    assert _snapshot(tmp_path) == before
    assert db.exists() is (shape is not None)


@pytest.mark.parametrize("active_shape,other_shape,allowed,multiple,other_populated", [
    ((None, None), (11, 0), False, False, True),
    ((5, None), (0, 13), False, True, False),
    ((None, 7), (11, 0), False, True, False),
    ((5, 7), (0, 0), True, False, False),
    ((5, 7), "invalid", True, False, False),
    ((None, None), None, True, False, False),
])
def test_existing_positive_row_guard_policy(monkeypatch, tmp_path, active_shape, other_shape, allowed, multiple, other_populated):
    config, data, active = _case(monkeypatch, tmp_path)
    _make_db(active, active_shape)
    _make_db(tmp_path / "systemd", other_shape)
    before = _snapshot(tmp_path)
    guard = api.ledger_store_write_guard(data, runtime_root=active, config_path=config)
    assert guard["ok"] is allowed
    assert guard["summary"]["multiple_populated"] is multiple
    assert guard["summary"]["active_empty_but_other_populated"] is other_populated
    assert _snapshot(tmp_path) == before


@pytest.mark.parametrize("shape,raise_events,expected", [
    ((5, 7), False, (19, 23)),
    ((5, 7), True, (5, 0)),
    ((None, 7), True, (None, 0)),
])
def test_repository_count_overrides_keep_existing_contract(monkeypatch, tmp_path, shape, raise_events, expected):
    _config, data, active = _case(monkeypatch, tmp_path)
    db = _make_db(active, shape)
    resolution = api.resolve_ledger_store(data, runtime_root=active)

    class Repo:
        ledger_store = resolution
        db_path = db

        def count_trade_events(self):
            if raise_events:
                raise ValueError("synthetic unavailable")
            return "19"

        def count_position_lots(self):
            return 0 if raise_events else 23

    before = _snapshot(tmp_path)
    payload = api.ledger_store_payload(data, Repo())
    assert _counts(payload) == expected
    assert payload["sqlite_path"] == str(db)
    assert _snapshot(tmp_path) == before


@pytest.mark.parametrize("shape,expected", [
    ((None, None), (None, None)), ((5, None), (5, None)),
    ((0, 0), (0, 0)), ((5, 7), (5, 7)), ("invalid", (None, None)),
])
def test_runtime_and_cli_read_facades_preserve_counts(monkeypatch, tmp_path, shape, expected):
    from src.application.agent_tools import runtime_status_impl
    from src.application.runtime_status_cli import format_runtime_status_summary
    from src.application.tool_execution import execute_tool
    from src.interfaces.cli.option_positions import main

    config, data, active = _case(monkeypatch, tmp_path)
    _make_db(active, shape)
    # Installed-service observation is external to this read-only ledger contract.
    monkeypatch.setattr(runtime_status_impl, "service_drift_status", lambda **kw: {
        "checked": False, "supported": False, "reason": "isolated_fixture",
        "summary": {"ok": False, "status": "unknown"},
    })
    before = _snapshot(tmp_path)
    result = execute_tool("runtime_status", {
        "config_path": str(config), "env_file": str(tmp_path / "empty.env"),
        "shared_state_dir": str(active / "output_shared/state"),
        "state_dir": str(active / "output_shared/state"),
        "report_dir": str(active / "output_shared/reports"),
        "runs_root": str(active / "output_runs"), "accounts_root": str(active / "output_accounts"),
        "audit_db": str(tmp_path / "absent-audit.sqlite3"), "include_service_status": False,
    })
    assert result["ok"], result
    ledger = result["data"]["ledger_store"]
    summary = result["data"]["summary"]
    # The existing public status projection omits unknown fields; it retains zero.
    for field, count in zip(("trade_event_count", "position_lot_count"), expected):
        assert ledger.get(field) == summary.get("ledger_" + field) == count
        assert (field in ledger) is (count is not None)
        assert ("ledger_" + field in summary) is (count is not None)
    events, lots = ("-" if count is None else str(count) for count in expected)
    assert f"events={events} lots={lots}" in format_runtime_status_summary(result)
    for output_format in ("json", "text"):
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(["store", "inspect", "--data-config", str(data), "--runtime-root", str(active), "--format", output_format])
        assert code == 0
        if output_format == "json":
            assert _counts(json.loads(output.getvalue())["active"]) == expected
        else:
            assert f"trade_events={expected[0]} position_lots={expected[1]}" in output.getvalue()
    assert _snapshot(tmp_path) == before
