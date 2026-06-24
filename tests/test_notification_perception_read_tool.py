from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.application.agent_tool_contracts import AgentToolError
from src.application.notification_perception_read import read_notification_perception_events


def test_notification_perception_read_filters_by_conversation_and_kind(tmp_path: Path) -> None:
    audit = tmp_path / "output_shared" / "state" / "audit_events.jsonl"
    audit.parent.mkdir(parents=True)
    _append(audit, _row("run_1", "notification_prepared", "wechat:group_1"))
    _append(audit, _row("run_1", "notification_delivery_decided", "wechat:group_2"))
    _append(audit, {"event_type": "notify", "action": "delivery_decision", "extra": {}})

    data = read_notification_perception_events(
        repo_root=tmp_path,
        conversation_id="wechat:group_1",
        event_kind="notification_prepared",
        limit=10,
    )

    assert data["summary"]["total_count"] == 1
    assert data["events"][0]["event_kind"] == "notification_prepared"
    assert data["events"][0]["conversation_scope"]["conversation_id"] == "wechat:group_1"


def test_notification_perception_read_can_read_run_scoped_audit(tmp_path: Path) -> None:
    audit = tmp_path / "output_runs" / "run_2" / "state" / "audit_events.jsonl"
    audit.parent.mkdir(parents=True)
    _append(audit, _row("run_2", "quiet_hours_skipped", "wechat:group_1"))

    data = read_notification_perception_events(repo_root=tmp_path, run_id="run_2", limit=1)

    assert data["summary"]["returned_count"] == 1
    assert data["events"][0]["run_id"] == "run_2"
    assert data["events"][0]["source_path"] == "output_runs/run_2/state/audit_events.jsonl"


def test_notification_perception_read_tool_is_registered_and_read_only(tmp_path: Path) -> None:
    from src.application.agent_tool_registry import get_tool_definition

    audit = tmp_path / "output_shared" / "state" / "audit_events.jsonl"
    audit.parent.mkdir(parents=True)
    _append(audit, _row("run_3", "notification_delivery_completed", "wechat:group_1"))

    tool = get_tool_definition("notification_perception_read")
    assert tool is not None
    assert tool.is_pure_read()
    data, warnings, meta = tool.call(_Context(tmp_path), {"limit": 1})
    assert warnings == []
    assert data["summary"]["returned_count"] == 1
    assert meta["audit_paths"]


def test_notification_perception_read_tool_rejects_explicit_audit_path(tmp_path: Path) -> None:
    from src.application.agent_tool_registry import get_tool_definition

    tool = get_tool_definition("notification_perception_read")
    assert tool is not None

    with pytest.raises(AgentToolError) as exc:
        tool.call(_Context(tmp_path), {"audit_path": str(tmp_path / "audit_events.jsonl")})

    assert exc.value.code == "INPUT_ERROR"


def test_notification_perception_reader_rejects_paths_outside_repo_root(tmp_path: Path) -> None:
    outside = tmp_path.parent / "audit_events.jsonl"
    outside.write_text("", encoding="utf-8")

    with pytest.raises(ValueError, match="under repo_root"):
        read_notification_perception_events(repo_root=tmp_path, audit_path=outside)


def _append(path: Path, payload: dict) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _row(run_id: str, action: str, conversation_id: str) -> dict:
    return {
        "event_type": "assistant_perception",
        "action": action,
        "run_id": run_id,
        "event_at_utc": "2026-06-23T14:00:00+00:00",
        "extra": {
            "event_kind": action,
            "run_id": run_id,
            "created_at_utc": "2026-06-23T14:00:00+00:00",
            "conversation_scope": {"channel": "wechat", "conversation_id": conversation_id},
            "safe_slots": {"run_id": [run_id], "action": [action]},
            "summary": f"notification {action}",
            "target": "must_not_leak",
        },
    }


class _Context:
    def __init__(self, base: Path) -> None:
        self._base = base

    def repo_base(self) -> Path:
        return self._base

    def mask_path(self, value) -> str:
        return str(value)
