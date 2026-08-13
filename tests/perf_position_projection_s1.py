from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.application.ledger.position_projection_publication import (
    publish_full_position_projection,
)
from src.application.ledger.position_records import PositionLotRecord
from src.application.ledger.projector_implementation import (
    loaded_projector_implementation_fingerprint,
)
from src.application.ledger.repository import SQLiteOptionPositionsRepository


def _record(index: int) -> PositionLotRecord:
    account = "lx" if index % 2 == 0 else "sy"
    return PositionLotRecord(
        record_id=f"lot-{index:06d}",
        fields={
            "account": account,
            "broker": "futu",
            "symbol": "NVDA" if account == "lx" else "AAPL",
            "option_type": "put",
            "side": "short",
            "contracts": 1,
            "contracts_open": 1,
            "contracts_closed": 0,
            "currency": "USD",
            "status": "open",
            "strike": 100 + (index % 20),
            "multiplier": 100,
            "expiration": 1781827200000,
            "expiration_ymd": "2026-06-19",
        },
    )


def _profile_diff_and_fingerprint() -> None:
    records = [_record(index) for index in range(10_000)]
    with tempfile.TemporaryDirectory(prefix="om-s1-perf-") as temp_dir:
        repo = SQLiteOptionPositionsRepository(Path(temp_dir) / "ledger.sqlite3")
        first = publish_full_position_projection(repo, records)
        started = time.perf_counter()
        second = publish_full_position_projection(repo, records)
        unchanged_ms = (time.perf_counter() - started) * 1_000
        changed = list(records)
        changed[0] = _record(0).with_fields({**_record(0).fields, "contracts_open": 0, "status": "close"})
        started = time.perf_counter()
        third = publish_full_position_projection(repo, changed)
        changed_ms = (time.perf_counter() - started) * 1_000
        print(
            json.dumps(
                {
                    "first_added": first.added,
                    "unchanged_rows": second.unchanged,
                    "unchanged_ms": round(unchanged_ms, 3),
                    "changed_rows": third.changed,
                    "changed_ms": round(changed_ms, 3),
                },
                sort_keys=True,
            )
        )


def _profile_populated_startup() -> None:
    with tempfile.TemporaryDirectory(prefix="om-s1-startup-") as temp_dir:
        db_path = Path(temp_dir) / "legacy.sqlite3"
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
            fields_json = json.dumps(_record(0).fields, sort_keys=True)
            conn.executemany(
                "INSERT INTO position_lots VALUES (?, ?, NULL, 1)",
                ((f"legacy-{index:06d}", fields_json) for index in range(10_000)),
            )
            conn.commit()
        started = time.perf_counter()
        repo = SQLiteOptionPositionsRepository(db_path)
        startup_ms = (time.perf_counter() - started) * 1_000
        with repo._connect() as conn:  # type: ignore[attr-defined]
            count = int(conn.execute("SELECT COUNT(*) FROM position_lots").fetchone()[0])
            null_sidecars = int(
                conn.execute(
                    "SELECT COUNT(*) FROM position_lots "
                    "WHERE account IS NULL AND expiration IS NULL AND strike IS NULL AND multiplier IS NULL"
                ).fetchone()[0]
            )
        print(
            json.dumps(
                {
                    "populated_startup_ms": round(startup_ms, 3),
                    "rows": count,
                    "untouched_rows": null_sidecars,
                },
                sort_keys=True,
            )
        )


if __name__ == "__main__":
    loaded_projector_implementation_fingerprint()
    _profile_diff_and_fingerprint()
    _profile_populated_startup()
