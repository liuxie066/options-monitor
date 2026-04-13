from __future__ import annotations

import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from domain.domain import SchemaValidationError, SnapshotDTO
from domain.storage.repositories import state_repo


def test_write_scheduler_decision_requires_snapshot_dto_schema(tmp_path: Path) -> None:
    run_id = "20260413T000000"
    ok_payload = SnapshotDTO.from_payload(
        {
            "schema_kind": "snapshot_dto",
            "schema_version": "1.0",
            "snapshot_name": "scheduler_decision",
            "as_of_utc": "2026-04-13T00:00:00+00:00",
            "payload": {"decision": {"schema_kind": "scheduler_decision", "schema_version": "1.0"}},
        }
    ).to_payload()
    out = state_repo.write_scheduler_decision(tmp_path, run_id, ok_payload)
    assert out.exists()

    try:
        state_repo.write_scheduler_decision(
            tmp_path,
            run_id,
            {"schema_kind": "bad", "schema_version": "1.0", "payload": {}},
        )
        raise AssertionError("expected SchemaValidationError")
    except SchemaValidationError:
        pass
