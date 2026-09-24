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
    assert data["summary"]["conversation_ref"].startswith("conversation:sha256:")
    assert data["events"][0]["conversation_scope"] == {"channel": "wechat"}
    assert "wechat:group_1" not in json.dumps(data, ensure_ascii=False)


def test_notification_perception_read_can_read_run_scoped_audit(tmp_path: Path) -> None:
    audit = tmp_path / "output_runs" / "run_2" / "state" / "audit_events.jsonl"
    audit.parent.mkdir(parents=True)
    _append(audit, _row("run_2", "quiet_hours_skipped", "wechat:group_1"))

    data = read_notification_perception_events(repo_root=tmp_path, run_id="run_2", limit=1)

    assert data["summary"]["returned_count"] == 1
    assert data["events"][0]["run_id"] == "run_2"
    assert data["events"][0]["source_path"] == "output_runs/run_2/state/audit_events.jsonl"


def test_large_shared_audit_returns_recent_evidence_with_partial_coverage(tmp_path: Path, capsys) -> None:
    audit = tmp_path / "output_shared" / "state" / "audit_events.jsonl"
    audit.parent.mkdir(parents=True)
    with audit.open("w", encoding="utf-8") as stream:
        old = json.dumps(_row("old", "notification_prepared", "chat"), ensure_ascii=False)
        for _ in range(4000):
            stream.write(old + "\n")
        recent = _row("recent", "notification_delivery_completed", "chat")
        recent["event_at_utc"] = "2026-06-23T14:01:00+00:00"
        stream.write(json.dumps(recent, ensure_ascii=False) + "\n")
    assert audit.stat().st_size > 1024 * 1024

    data = read_notification_perception_events(repo_root=tmp_path, limit=10)
    assert data["summary"]["status"] == "partial"
    assert data["events"][0]["run_id"] == "recent"
    assert data["coverage"]["status"] == "partial"
    assert data["pagination"]["matched_count"] is None
    assert data["read_statuses"][0]["tail_truncated"] is True
    assert "<3>READ_DIAGNOSTIC_DEGRADED reason=partial" in capsys.readouterr().err
    from src.application.agent_tool_registry import get_tool_definition
    _, warnings, _ = get_tool_definition("notification_perception_read").call(
        {"runtime_root": str(tmp_path), "limit": 10}
    )
    assert warnings == ["Notification perception audit covers only the recent file tail."]


def test_window_streams_segments_and_keeps_conversation_scope(tmp_path: Path) -> None:
    state = tmp_path / "output_shared" / "state"
    state.mkdir(parents=True)
    old = state / "audit_events.20260923.000001.jsonl"
    current = state / "audit_events.jsonl"
    for path, run_id, conversation in ((old, "old", "chat-a"), (current, "current", "chat-a"),
                                        (current, "hidden", "chat-b")):
        row = _row(run_id, "notification_prepared", conversation)
        row["event_at_utc"] = "2026-09-23T12:00:00+00:00"
        _append(path, row)
    data = read_notification_perception_events(repo_root=tmp_path, conversation_id="chat-a",
        start_utc="2026-09-23T00:00:00Z", end_utc="2026-09-24T00:00:00Z", limit=1)
    assert data["summary"]["total_count"] == 2
    assert data["summary"]["returned_count"] == 1
    assert data["pagination"]["has_more"] is True
    assert "chat-b" not in json.dumps(data, ensure_ascii=False)


def test_window_marks_history_before_first_retained_segment_partial(tmp_path: Path, capsys) -> None:
    state = tmp_path / "output_shared" / "state"
    state.mkdir(parents=True)
    row = _row("new", "notification_prepared", "chat")
    row["event_at_utc"] = "2026-09-24T12:00:00+00:00"
    _append(state / "audit_events.20260924.000001.jsonl", row)
    data = read_notification_perception_events(
        repo_root=tmp_path, start_utc="2026-09-23T00:00:00Z", end_utc="2026-09-24T23:59:59Z",
    )
    assert data["summary"]["status"] == "partial"
    assert data["coverage"]["stop_reason"] == "history_before_first_segment"
    assert data["coverage"]["available_from_utc"] == "2026-09-24T00:00:00+00:00"
    assert "<3>READ_DIAGNOSTIC_DEGRADED reason=partial" in capsys.readouterr().err


def test_window_reports_budget_partial_without_loading_whole_file(tmp_path: Path, monkeypatch) -> None:
    audit = tmp_path / "output_shared" / "state" / "audit_events.jsonl"
    audit.parent.mkdir(parents=True)
    with audit.open("wb") as handle:
        handle.write((b' ' * 1023 + b'\n') * 65537)
    data = read_notification_perception_events(repo_root=tmp_path,
        start_utc="2026-09-23T00:00:00Z", end_utc="2026-09-24T00:00:00Z")
    assert data["summary"]["status"] == "partial"
    assert data["coverage"]["scanned_bytes"] <= 64 * 1024 * 1024


def test_window_cursor_survives_append_but_rejects_replacement(tmp_path: Path, monkeypatch) -> None:
    from src.application.agent_tools import project_reader

    monkeypatch.setattr(project_reader, "_key", lambda: "test-key")
    audit = tmp_path / "output_shared" / "state" / "audit_events.jsonl"
    audit.parent.mkdir(parents=True)
    for name in ("a", "b"):
        row = _row(name, "notification_prepared", "chat")
        row["event_at_utc"] = "2026-09-23T12:00:00+00:00"
        _append(audit, row)
    query = dict(repo_root=tmp_path, start_utc="2026-09-23T00:00:00Z",
                 end_utc="2026-09-24T00:00:00Z", limit=1)
    first = read_notification_perception_events(**query)
    assert first["next_cursor"]
    row = _row("new", "notification_prepared", "chat")
    row["event_at_utc"] = "2026-09-23T13:00:00+00:00"
    _append(audit, row)
    second = read_notification_perception_events(**query, cursor=first["next_cursor"])
    assert second["events"][0]["run_id"] in {"a", "b"}
    audit.rename(audit.with_suffix(".old"))
    audit.write_text("", encoding="utf-8")
    with pytest.raises(AgentToolError, match="source_changed"):
        read_notification_perception_events(**query, cursor=first["next_cursor"])


def test_window_rejects_one_oversized_public_event(tmp_path: Path) -> None:
    audit = tmp_path / "output_shared" / "state" / "audit_events.jsonl"
    audit.parent.mkdir(parents=True)
    row = _row("large", "notification_prepared", "chat")
    row["event_at_utc"] = "2026-09-23T12:00:00+00:00"
    row["extra"]["detail"] = "x" * 7000
    _append(audit, row)
    with pytest.raises(AgentToolError, match="detail_too_large"):
        read_notification_perception_events(repo_root=tmp_path,
            start_utc="2026-09-23T00:00:00Z", end_utc="2026-09-24T00:00:00Z")


def test_window_rejects_intermediate_symlink_outside_root(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    row = _row("outside", "notification_prepared", "chat")
    row["event_at_utc"] = "2026-09-23T12:00:00+00:00"
    _append(outside / "audit.jsonl", row)
    root = tmp_path / "root"
    root.mkdir()
    (root / "linked").symlink_to(outside, target_is_directory=True)

    data = read_notification_perception_events(
        repo_root=root, audit_path="linked/audit.jsonl",
        start_utc="2026-09-23T00:00:00Z", end_utc="2026-09-24T00:00:00Z",
    )
    assert data["summary"]["status"] == "partial"
    assert data["events"] == []
    assert data["read_statuses"][0]["status"] == "unreadable"


def test_notification_perception_read_tool_is_registered_and_read_only(monkeypatch, tmp_path: Path) -> None:
    import src.application.agent_tools.notification_perception as notification_tools
    from src.application.agent_tool_registry import get_tool_definition

    audit = tmp_path / "output_shared" / "state" / "audit_events.jsonl"
    audit.parent.mkdir(parents=True)
    _append(audit, _row("run_3", "notification_delivery_completed", "wechat:group_1"))

    tool = get_tool_definition("notification_perception_read")
    assert tool is not None
    assert tool.is_pure_read()
    monkeypatch.setattr(notification_tools, "repo_base", lambda: tmp_path)
    data, warnings, meta = tool.call({"limit": 1})
    assert warnings == []
    assert data["summary"]["returned_count"] == 1
    assert meta["audit_paths"]


def test_notification_perception_read_tool_uses_runtime_root_from_env(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import src.application.agent_tools.notification_perception as notification_tools
    from src.application.agent_tool_registry import get_tool_definition

    repo_root = tmp_path / "release"
    runtime_root = tmp_path / "runtime"
    repo_audit = (
        repo_root / "output_shared" / "state" / "audit_events.jsonl"
    )
    runtime_audit = (
        runtime_root
        / "output_shared"
        / "state"
        / "audit_events.jsonl"
    )
    repo_audit.parent.mkdir(parents=True)
    runtime_audit.parent.mkdir(parents=True)
    _append(repo_audit, _row("repo-run", "notification_prepared", "c"))
    _append(
        runtime_audit,
        _row("runtime-run", "notification_delivery_completed", "c"),
    )
    monkeypatch.setattr(notification_tools, "repo_base", lambda: repo_root)
    monkeypatch.setenv("OM_RUNTIME_ROOT", str(runtime_root))

    tool = get_tool_definition("notification_perception_read")
    assert tool is not None
    data, warnings, meta = tool.call({"limit": 10})

    assert warnings == []
    assert [item["run_id"] for item in data["events"]] == [
        "runtime-run"
    ]
    assert data["runtime_root"]["source"] == "env:OM_RUNTIME_ROOT"
    assert meta["runtime_root_source"] == "env:OM_RUNTIME_ROOT"


def test_notification_perception_reader_reports_partial_corruption(
    tmp_path: Path,
) -> None:
    audit = tmp_path / "output_shared" / "state" / "audit_events.jsonl"
    audit.parent.mkdir(parents=True)
    audit.write_text(
        json.dumps(
            _row("run-ok", "notification_prepared", "conversation")
        )
        + "\n{broken-json\n[]\n",
        encoding="utf-8",
    )

    data = read_notification_perception_events(
        repo_root=tmp_path,
        limit=10,
    )

    assert data["summary"]["ok"] is False
    assert data["summary"]["status"] == "partial"
    assert data["summary"]["malformed_count"] == 2
    assert data["summary"]["returned_count"] == 1
    assert data["read_statuses"][0]["status"] == "partially_corrupt"


def test_notification_perception_reader_reports_unreadable(
    monkeypatch,
    tmp_path: Path,
) -> None:
    audit = tmp_path / "output_shared" / "state" / "audit_events.jsonl"
    audit.parent.mkdir(parents=True)
    audit.write_text("placeholder\n", encoding="utf-8")
    import os
    import errno
    import src.application.agent_tools.project_reader as reader
    original_open = os.open

    def denied_open(path, *args, **kwargs):
        if str(path) == "audit_events.jsonl":
            raise PermissionError(errno.EACCES, "denied")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(reader.os, "open", denied_open)
    monkeypatch.setattr(reader.os, "supports_dir_fd", {*os.supports_dir_fd, denied_open})
    data = read_notification_perception_events(
        repo_root=tmp_path,
        limit=10,
    )

    assert data["summary"]["ok"] is False
    assert data["summary"]["status"] == "failed"
    assert data["summary"]["unreadable_count"] == 1
    assert data["read_statuses"][0]["status"] == "unreadable"


def test_notification_perception_read_tool_rejects_explicit_audit_path(tmp_path: Path) -> None:
    from src.application.agent_tool_registry import get_tool_definition

    tool = get_tool_definition("notification_perception_read")
    assert tool is not None

    with pytest.raises(AgentToolError) as exc:
        tool.call({"audit_path": str(tmp_path / "audit_events.jsonl")})

    assert exc.value.code == "INPUT_ERROR"


def test_notification_perception_reader_rejects_paths_outside_repo_root(tmp_path: Path) -> None:
    outside = tmp_path.parent / "audit_events.jsonl"
    outside.write_text("", encoding="utf-8")

    with pytest.raises(ValueError, match="under repo_root"):
        read_notification_perception_events(repo_root=tmp_path, audit_path=outside)


def test_notification_perception_run_id_cannot_escape_runtime_root(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="not a safe path component"):
        read_notification_perception_events(
            repo_root=tmp_path,
            run_id="../../outside",
        )


def test_notification_perception_run_id_cannot_traverse_within_repo(
    tmp_path: Path,
) -> None:
    # 横向穿越：目标仍在 repo root 内（旧 containment 放行），必须被安全组件校验拒绝，
    # 且不读任何文件。
    planted = tmp_path / "secrets" / "state" / "audit_events.jsonl"
    planted.parent.mkdir(parents=True)
    _append(planted, _row("planted", "notification_prepared", "c"))

    with pytest.raises(ValueError, match="not a safe path component"):
        read_notification_perception_events(
            repo_root=tmp_path,
            run_id="../secrets",
        )


def test_notification_perception_run_id_rejects_symlinked_output_runs(
    tmp_path: Path,
) -> None:
    # output_runs 是指向仓库外目录的符号链接时，containment resolve 后会放行，
    # 必须显式拒绝，且外部文件不被读取。
    external = tmp_path / "external" / "output_runs"
    run_state = external / "20260808T000000Z-abcdef" / "state"
    run_state.mkdir(parents=True)
    planted = run_state / "audit_events.jsonl"
    _append(planted, _row("planted", "notification_prepared", "c"))

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "output_runs").symlink_to(external, target_is_directory=True)

    with pytest.raises(ValueError, match="must stay under output_runs"):
        read_notification_perception_events(
            repo_root=repo,
            run_id="20260808T000000Z-abcdef",
        )


def test_notification_perception_run_id_rejects_symlinked_run_dir(
    tmp_path: Path,
) -> None:
    external = tmp_path / "external"
    run_state = external / "state"
    run_state.mkdir(parents=True)
    _append(run_state / "audit_events.jsonl", _row("planted", "notification_prepared", "c"))

    repo = tmp_path / "repo"
    (repo / "output_runs").mkdir(parents=True)
    (repo / "output_runs" / "20260808T000000Z-abcdef").symlink_to(
        external, target_is_directory=True
    )

    with pytest.raises(ValueError, match="must stay under output_runs"):
        read_notification_perception_events(
            repo_root=repo,
            run_id="20260808T000000Z-abcdef",
        )


def test_notification_perception_shared_branch_rejects_symlinked_output_shared(
    tmp_path: Path,
) -> None:
    # 默认 shared 分支：output_shared 为符号链接时同样拒绝，不跟随读取。
    external = tmp_path / "external_shared" / "state"
    external.mkdir(parents=True)
    _append(external / "audit_events.jsonl", _row("planted", "notification_prepared", "c"))

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "output_shared").symlink_to(tmp_path / "external_shared", target_is_directory=True)

    with pytest.raises(ValueError, match="must stay under output_shared"):
        read_notification_perception_events(repo_root=repo)


def test_notification_perception_rejects_symlinked_state_dir(tmp_path: Path) -> None:
    # run/shared 两个分支的中间 state 目录为符号链接时同样显式拒绝（对齐先例逐组件姿态）。
    external = tmp_path / "external_state"
    external.mkdir()
    _append(external / "audit_events.jsonl", _row("planted", "notification_prepared", "c"))

    run_repo = tmp_path / "run_repo"
    run_dir = run_repo / "output_runs" / "20260808T000000Z-abcdef"
    run_dir.mkdir(parents=True)
    (run_dir / "state").symlink_to(external, target_is_directory=True)
    with pytest.raises(ValueError, match="must stay under output_runs"):
        read_notification_perception_events(
            repo_root=run_repo,
            run_id="20260808T000000Z-abcdef",
        )

    shared_repo = tmp_path / "shared_repo"
    (shared_repo / "output_shared").mkdir(parents=True)
    (shared_repo / "output_shared" / "state").symlink_to(external, target_is_directory=True)
    with pytest.raises(ValueError, match="must stay under output_shared"):
        read_notification_perception_events(repo_root=shared_repo)


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


def test_internal_report_iterator_preserves_scope_and_incomplete_audit(tmp_path: Path) -> None:
    from src.application.notification_perception_read import iter_notification_perception_events

    audit = tmp_path / 'output_shared/state/audit_events.jsonl'
    audit.parent.mkdir(parents=True)
    _append(audit, _row('my-run', 'notification_delivery_completed', 'my-chat'))
    _append(audit, _row('other-run', 'notification_delivery_completed', 'other-chat'))
    with audit.open('a') as stream:
        stream.write('{broken newest row\n')
    data = iter_notification_perception_events(repo_root=tmp_path, conversation_id='my-chat')
    assert [row['run_id'] for row in data['events']] == ['my-run']
    assert data['summary']['status'] == 'partial'
    assert data['summary']['malformed_count'] == 1
    audit.write_bytes(b'\xff')
    assert iter_notification_perception_events(repo_root=tmp_path)['summary']['status'] == 'failed'
