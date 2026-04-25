from __future__ import annotations

import importlib
import json
import sqlite3
import sys
from pathlib import Path
from typing import Callable, cast

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))


AuditFn = Callable[..., object]


def _load_audit_fn(name: str) -> AuditFn:
    module = importlib.import_module("scripts.backfill_option_positions_broker")
    return cast(AuditFn, getattr(module, name))


def _as_dict(value: object) -> dict[str, object]:
    return cast(dict[str, object], value)


def _write_data_config(path: Path, *, sqlite_path: Path) -> Path:
    _ = path.write_text(
        json.dumps({"option_positions": {"sqlite_path": str(sqlite_path)}}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def _insert_legacy_rows(db_path: Path, rows: list[tuple[str, dict[str, object]]]) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        _ = conn.execute(
            """
            CREATE TABLE IF NOT EXISTS option_positions (
              record_id TEXT PRIMARY KEY,
              fields_json TEXT NOT NULL,
              created_at_ms INTEGER NOT NULL,
              updated_at_ms INTEGER NOT NULL
            )
            """
        )
        for record_id, fields in rows:
            _ = conn.execute(
                "INSERT INTO option_positions (record_id, fields_json, created_at_ms, updated_at_ms) VALUES (?, ?, ?, ?)",
                (record_id, json.dumps(fields, ensure_ascii=False), 1000, 1000),
            )
        conn.commit()
    finally:
        conn.close()


def _read_fields(db_path: Path, table_name: str, record_id: str) -> dict[str, object]:
    conn = sqlite3.connect(str(db_path))
    try:
        row = cast(
            tuple[object] | None,
            conn.execute(
            f"SELECT fields_json FROM {table_name} WHERE record_id = ?",
            (record_id,),
            ).fetchone(),
        )
    finally:
        conn.close()
    assert row is not None
    return cast(dict[str, object], json.loads(str(row[0])))


def test_build_option_positions_broker_backfill_audit_finds_market_only_rows(tmp_path: Path) -> None:
    import scripts.option_positions_core.service as svc

    build_option_positions_broker_backfill_audit = _load_audit_fn("build_option_positions_broker_backfill_audit")

    db_path = tmp_path / "option_positions.sqlite3"
    repo = svc.SQLiteOptionPositionsRepository(db_path)
    _ = repo.replace_position_lots(
        [
            {
                "record_id": "lot_market_only",
                "fields": {"market": "FUTU", "account": "lx", "symbol": "AAPL"},
            },
            {
                "record_id": "lot_canonical",
                "fields": {"broker": "富途", "market": "FUTU", "account": "lx", "symbol": "TSLA"},
            },
            {
                "record_id": "lot_conflict",
                "fields": {"broker": "老虎", "market": "FUTU", "account": "lx", "symbol": "NVDA"},
            },
        ]
    )
    _insert_legacy_rows(
        db_path,
        [
            ("legacy_market_only", {"market": "富途证券", "account": "sy", "symbol": "0700.HK"}),
            ("legacy_canonical", {"broker": "富途", "account": "sy", "symbol": "BABA"}),
        ],
    )
    data_config = _write_data_config(tmp_path / "data.json", sqlite_path=db_path)

    audit = _as_dict(build_option_positions_broker_backfill_audit(base=BASE, data_config=data_config, sample_limit=5))
    summary = _as_dict(audit["summary"])
    tables = cast(dict[str, dict[str, object]], audit["tables"])
    updates = cast(list[dict[str, object]], audit["updates"])

    assert summary["candidate_rows"] == 2
    assert summary["conflict_rows"] == 1
    assert tables["position_lots"]["candidate_rows"] == 1
    assert tables["option_positions"]["candidate_rows"] == 1
    assert tables["position_lots"]["conflict_rows"] == 1
    assert {cast(str, item["record_id"]) for item in updates} == {"lot_market_only", "legacy_market_only"}
    assert cast(dict[str, object], updates[0]["fields"])["broker"] == "富途"


def test_apply_option_positions_broker_backfill_updates_candidates_only(tmp_path: Path) -> None:
    import scripts.option_positions_core.service as svc

    apply_option_positions_broker_backfill = _load_audit_fn("apply_option_positions_broker_backfill")

    db_path = tmp_path / "option_positions.sqlite3"
    repo = svc.SQLiteOptionPositionsRepository(db_path)
    _ = repo.replace_position_lots(
        [
            {
                "record_id": "lot_market_only",
                "fields": {"market": "FUTU", "account": "lx", "symbol": "AAPL"},
            },
            {
                "record_id": "lot_canonical",
                "fields": {"broker": "富途", "market": "FUTU", "account": "lx", "symbol": "TSLA"},
            },
        ]
    )
    _insert_legacy_rows(
        db_path,
        [("legacy_market_only", {"market": "富途证券", "account": "sy", "symbol": "0700.HK"})],
    )
    data_config = _write_data_config(tmp_path / "data.json", sqlite_path=db_path)

    result = _as_dict(apply_option_positions_broker_backfill(base=BASE, data_config=data_config, sample_limit=5))
    post_audit = _as_dict(result["post_audit"])
    post_summary = _as_dict(post_audit["summary"])

    assert result["updated_rows"] == 2
    assert result["backup_path"]
    assert Path(cast(str, result["backup_path"])).exists()
    assert post_summary["candidate_rows"] == 0

    lot_market_only = _read_fields(db_path, "position_lots", "lot_market_only")
    lot_canonical = _read_fields(db_path, "position_lots", "lot_canonical")
    legacy_market_only = _read_fields(db_path, "option_positions", "legacy_market_only")

    assert lot_market_only["broker"] == "富途"
    assert lot_market_only["market"] == "FUTU"
    assert lot_canonical["broker"] == "富途"
    assert legacy_market_only["broker"] == "富途"


def test_build_option_positions_broker_backfill_audit_reports_missing_db_without_creating_one(tmp_path: Path) -> None:
    build_option_positions_broker_backfill_audit = _load_audit_fn("build_option_positions_broker_backfill_audit")

    db_path = tmp_path / "missing.sqlite3"
    data_config = _write_data_config(tmp_path / "data.json", sqlite_path=db_path)

    audit = _as_dict(build_option_positions_broker_backfill_audit(base=BASE, data_config=data_config, sample_limit=5))

    assert audit["missing_db"] is True
    assert db_path.exists() is False
