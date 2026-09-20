from __future__ import annotations

from pathlib import Path
from datetime import datetime, timedelta, timezone
import json

BASE = Path(__file__).resolve().parents[1]


def test_state_repo_normalize_audit_event_requires_minimal_fields() -> None:
    from domain.storage.repositories.state_repo import normalize_audit_event

    out = normalize_audit_event(
        {
            "event_type": "tool_call",
            "action": "scan_scheduler",
            "status": "ok",
            "run_id": "r-1",
            "idempotency_key": "k-1",
            "extra": {"returncode": 0},
        }
    )
    assert out["schema_kind"] == "audit_event"
    assert out["schema_version"] == "1.0"
    assert out["event_type"] == "tool_call"
    assert out["action"] == "scan_scheduler"


def test_multi_tick_main_has_phase3_idempotency_and_audit_hooks() -> None:
    src = (BASE / "src" / "application" / "multi_account_tick.py").read_text(encoding="utf-8")
    assert "scope='tick_execution'" in src
    assert "append_audit_event" in src
    assert "execution_idempotency_key" in src
    assert "claim_idempotency_record" in src
    assert "status': 'started'" not in src


def test_state_repo_has_current_read_model_writers() -> None:
    src = (BASE / "domain" / "storage" / "repositories" / "state_repo.py").read_text(encoding="utf-8")
    assert "shared_current_read_model_dir" in src
    assert "write_shared_current_read_model" in src
    assert "tick_metrics.current.json" in src


def _claim(tmp_path: Path, run_id: str, *, stale_after_sec=60, **payload_extra):
    from domain.storage.repositories import state_repo

    return state_repo.claim_idempotency_record(
        tmp_path,
        scope="tick_execution",
        key="tick-key",
        payload={"run_id": run_id, **payload_extra},
        stale_after_sec=stale_after_sec,
    )


def test_tick_idempotency_claim_blocks_active_and_reclaims_stale(tmp_path: Path) -> None:
    from domain.storage.repositories import state_repo

    first = _claim(tmp_path, "run-1")
    assert first["claimed"] is True
    assert first["record"]["ok"] is False
    assert first["record"]["status"] == "in_progress"

    active_duplicate = _claim(tmp_path, "run-2")
    assert active_duplicate["claimed"] is False
    assert active_duplicate["stale"] is False
    assert active_duplicate["record"]["run_id"] == "run-1"

    path = first["path"]
    stale_record = dict(first["record"])
    stale_record["updated_at_utc"] = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    path.write_text(json.dumps(stale_record), encoding="utf-8")

    reclaimed = _claim(tmp_path, "run-3")
    assert reclaimed["claimed"] is True
    assert reclaimed["stale"] is True
    assert reclaimed["record"]["run_id"] == "run-3"

    state_repo.write_idempotency_record(
        tmp_path,
        scope="tick_execution",
        key="tick-key",
        payload={"ok": True, "status": "completed", "run_id": "run-3"},
    )
    completed_duplicate = _claim(tmp_path, "run-4", stale_after_sec=1)
    assert completed_duplicate["claimed"] is False
    assert completed_duplicate["record"]["status"] == "completed"


def test_tick_idempotency_claim_reclaims_dead_owner_pid(tmp_path: Path) -> None:
    first = _claim(tmp_path, "run-1", stale_after_sec=3600, pid=99999999)
    assert first["claimed"] is True

    reclaimed = _claim(tmp_path, "run-2", stale_after_sec=3600)

    assert reclaimed["claimed"] is True
    assert reclaimed["stale"] is True
    assert reclaimed["record"]["run_id"] == "run-2"
