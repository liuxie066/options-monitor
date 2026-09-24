import json
from pathlib import Path

import pytest

from domain.storage import json_io
from domain.storage.repositories import state_repo
from src.application.notification_perception_read import read_notification_perception_events


def test_shared_audit_rotation_remains_window_readable(tmp_path: Path, monkeypatch) -> None:
    line_bytes = len((json.dumps({
        "event_type": "assistant_perception", "action": "notification_prepared",
        "run_id": "first", "event_at_utc": "2026-09-23T12:00:00+00:00",
        "extra": {"event_kind": "notification_prepared"},
    }, ensure_ascii=False) + "\n").encode())
    monkeypatch.setattr(json_io, "AUDIT_SEGMENT_BYTES", line_bytes + 1)
    for run_id in ("first", "second"):
        state_repo.append_shared_audit_jsonl(tmp_path, "audit_events.jsonl", {
            "event_type": "assistant_perception", "action": "notification_prepared",
            "run_id": run_id, "event_at_utc": "2026-09-23T12:00:00+00:00",
            "extra": {"event_kind": "notification_prepared"},
        })
    state = tmp_path / "output_shared" / "state"
    segments = list(state.glob("audit_events.????????.??????.jsonl"))
    assert len(segments) == 1
    assert segments[0].stat().st_mode & 0o777 == 0o600
    out = read_notification_perception_events(repo_root=tmp_path,
        start_utc="2026-09-23T00:00:00Z", end_utc="2026-09-24T00:00:00Z")
    assert {item["run_id"] for item in out["events"]} == {"first", "second"}


def test_single_audit_record_above_segment_limit_is_rejected_on_retry(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(json_io, "AUDIT_SEGMENT_BYTES", 8)
    for _ in range(2):
        with pytest.raises(ValueError, match="segment size limit"):
            state_repo.append_shared_audit_jsonl(tmp_path, "audit_events.jsonl", {"message": "too large"})
    assert not (tmp_path / "output_shared/state/audit_events.jsonl").exists()
