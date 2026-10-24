from __future__ import annotations

from src.application import query_cursor

import errno
import json
import os
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

    monkeypatch.setattr(query_cursor, "_key", lambda: "test-key")
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


@pytest.fixture
def window_tool(tmp_path: Path, monkeypatch):
    import src.application.agent_tools.notification_perception as notification_tools
    from src.application.tool_execution import execute_tool

    root = tmp_path / "runtime"
    root.mkdir()
    monkeypatch.setattr(notification_tools, "repo_base", lambda: root)
    monkeypatch.setattr(query_cursor, "_key", lambda: "isolated-window-fixture-key")

    def query(**payload):
        return execute_tool("notification_perception_read", {
            "start_utc": "2026-09-23T00:00:00Z",
            "end_utc": "2026-09-24T00:00:00Z",
            **payload,
            "runtime_root": str(root),
        })

    return root, query


def _window_source(root: Path, source_scope: str) -> tuple[Path, dict]:
    if source_scope == "run":
        return root / "output_runs/window-run/state/audit_events.jsonl", {"run_id": "window-run"}
    return root / "output_shared/state/audit_events.jsonl", {}


def _window_inventory(root: Path) -> dict:
    return {
        str(path.relative_to(root)): None if path.is_dir() else path.read_bytes()
        for path in root.rglob("*")
    }


def _window_row(run_id: str, action: str, conversation_id: str) -> dict:
    row = _row(run_id, action, conversation_id)
    row["event_at_utc"] = "2026-09-23T12:00:00+00:00"
    row["extra"]["created_at_utc"] = row["event_at_utc"]
    return row


@pytest.mark.parametrize("source_scope", ["run", "shared"])
@pytest.mark.parametrize("source_state", ["missing_leaf", "missing_parent", "empty"])
def test_window_tool_distinguishes_missing_sources_from_empty_files(
    window_tool, source_scope: str, source_state: str,
) -> None:
    root, query = window_tool
    audit, scope = _window_source(root, source_scope)
    if source_state != "missing_parent":
        audit.parent.mkdir(parents=True)
    if source_state == "empty":
        audit.write_bytes(b"")
    before = _window_inventory(root)

    response = query(**scope)

    assert response["ok"] is True
    data = response["data"]
    empty = source_state == "empty"
    exact_total = 0 if empty else None
    assert data["summary"]["ok"] is empty
    assert data["summary"]["status"] == ("ok" if empty else "missing")
    assert data["summary"]["missing_count"] == (0 if empty else 1)
    assert data["summary"]["malformed_count"] == 0
    assert data["summary"]["unreadable_count"] == 0
    assert (
        data["summary"]["total_count"], data["pagination"]["total_count"],
        data["pagination"]["matched_count"], data["coverage"]["total_count"],
        data["coverage"]["omitted_count"],
    ) == (exact_total,) * 5
    assert data["summary"]["returned_count"] == 0
    assert data["pagination"]["returned_count"] == 0
    assert data["pagination"]["scanned_count"] == 0
    assert data["coverage"]["included_count"] == 0
    assert data["coverage"]["scanned_bytes"] == 0
    assert data["coverage"]["status"] == ("complete" if empty else "partial")
    assert data["coverage"]["complete_for"] == "requested_window"
    assert data["coverage"]["stop_reason"] == (None if empty else "missing")
    assert data["pagination"]["has_more"] is False
    assert data["coverage"]["has_more"] is False
    assert data["next_cursor"] is None
    assert data["events"] == data["event_summaries"] == []
    assert [item["status"] for item in data["read_statuses"]] == ["ok" if empty else "missing"]
    assert _window_inventory(root) == before
    assert audit.exists() is empty
    assert audit.parent.exists() is (source_state != "missing_parent")
    if empty:
        assert audit.read_bytes() == b""


@pytest.mark.parametrize("source_scope", ["run", "shared"])
@pytest.mark.parametrize("change", ["append", "truncate"])
def test_window_tool_signed_cursor_handles_same_inode_size_change(
    window_tool, monkeypatch, source_scope: str, change: str,
) -> None:
    # Native truncation is CI-only under the local fixture-retention policy.
    root, query = window_tool
    audit, scope = _window_source(root, source_scope)
    audit.parent.mkdir(parents=True)
    _append(audit, _window_row("window-run", "notification_prepared", "chat"))
    _append(audit, _window_row("window-run", "notification_delivery_decided", "chat"))
    original = audit.read_bytes()
    before = audit.stat()
    initial_inventory = _window_inventory(root)

    first = query(**scope, limit=1)

    assert first["ok"] is True
    initial = first["data"]
    assert initial["summary"]["total_count"] == 2
    assert initial["summary"]["returned_count"] == 1
    assert [event["event_kind"] for event in initial["events"]] == ["notification_delivery_decided"]
    cursor = initial["next_cursor"]
    assert isinstance(cursor, str) and cursor
    assert _window_inventory(root) == initial_inventory
    if change == "truncate":
        prefix = original.splitlines(keepends=True)[0]
        with audit.open("r+b") as handle:
            handle.truncate(len(prefix))
        assert audit.read_bytes() == prefix
        assert json.loads(prefix) == _window_row("window-run", "notification_prepared", "chat")
        assert audit.stat().st_size == len(prefix) < before.st_size
    else:
        _append(audit, _window_row("window-run", "notification_delivery_completed", "appended-chat"))
        assert audit.read_bytes().startswith(original)
        assert audit.stat().st_size > before.st_size
    after = audit.stat()
    assert (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino)
    changed_inventory = _window_inventory(root)
    original_open = os.open
    opened = []

    def observed_open(name, flags, mode=0o777, *, dir_fd=None):
        descriptor = original_open(name, flags, mode, dir_fd=dir_fd)
        if name == audit.name:
            opened.append(descriptor)
        return descriptor

    monkeypatch.setattr(os, "open", observed_open)
    monkeypatch.setattr(os, "supports_dir_fd", {*os.supports_dir_fd, observed_open})

    response = query(**scope, limit=1, cursor=cursor)

    if change == "truncate":
        assert response["ok"] is False
        assert response["error"]["code"] == "INPUT_ERROR"
        assert response["error"]["message"] == "source_changed"
        assert response["data"] == {}
    else:
        assert response["ok"] is True
        data = response["data"]
        assert data["summary"]["status"] == "ok"
        assert data["summary"]["total_count"] == 2
        assert data["summary"]["returned_count"] == 1
        assert data["source_hash"] == initial["source_hash"]
        assert data["coverage"]["scanned_bytes"] == len(original)
        assert [(event["run_id"], event["event_kind"]) for event in data["events"]] == [
            ("window-run", "notification_prepared"),
        ]
        assert data["pagination"]["has_more"] is False
        assert data["next_cursor"] is None
    assert _window_inventory(root) == changed_inventory
    assert opened
    for descriptor in opened:
        with pytest.raises(OSError) as caught:
            os.fstat(descriptor)
        assert caught.value.errno == errno.EBADF


@pytest.mark.parametrize(
    ("start_utc", "stop_reason"),
    [
        pytest.param("2026-09-23T00:00:00Z", "missing", id="missing"),
        pytest.param("2026-09-22T00:00:00Z", "history_before_first_segment", id="earlier_history_gap"),
    ],
)
def test_window_tool_keeps_scoped_segment_events_when_current_is_missing(
    window_tool, start_utc: str, stop_reason: str,
) -> None:
    root, query = window_tool
    current, _ = _window_source(root, "shared")
    current.parent.mkdir(parents=True)
    segment = current.parent / "audit_events.20260923.000001.jsonl"
    _append(segment, _window_row("visible-run", "notification_prepared", "visible-chat"))
    _append(segment, _window_row("hidden-run", "notification_prepared", "hidden-chat"))
    _append(segment, _window_row("other-kind-run", "notification_delivery_completed", "visible-chat"))
    before = _window_inventory(root)

    response = query(
        start_utc=start_utc,
        conversation_id="visible-chat",
        authenticated_conversation_id="visible-chat",
        event_kind="notification_prepared",
    )

    assert response["ok"] is True
    data = response["data"]
    assert data["summary"]["ok"] is False
    assert data["summary"]["status"] == "partial"
    assert data["summary"]["missing_count"] == 1
    assert (
        data["summary"]["total_count"], data["pagination"]["total_count"],
        data["pagination"]["matched_count"], data["coverage"]["total_count"],
        data["coverage"]["omitted_count"],
    ) == (None,) * 5
    assert data["summary"]["returned_count"] == 1
    assert data["pagination"]["returned_count"] == 1
    assert data["pagination"]["scanned_count"] == 1
    assert data["coverage"]["included_count"] == 1
    assert data["coverage"]["scanned_bytes"] == len(segment.read_bytes())
    assert data["coverage"]["status"] == "partial"
    assert data["coverage"]["complete_for"] == "requested_window"
    assert data["coverage"]["stop_reason"] == stop_reason
    assert data["coverage"]["available_from_utc"] == "2026-09-23T00:00:00+00:00"
    assert data["pagination"]["has_more"] is False
    assert data["coverage"]["has_more"] is False
    assert data["next_cursor"] is None
    assert [event["run_id"] for event in data["events"]] == ["visible-run"]
    assert data["event_summaries"] == [{
        "run_id": "visible-run",
        "created_at_utc": "2026-09-23T12:00:00+00:00",
        "event_kind": "notification_prepared",
    }]
    assert data["events"][0]["conversation_scope"] == {"channel": "wechat"}
    assert data["scope"]["conversation_ref"].startswith("conversation:sha256:")
    assert [item["status"] for item in data["read_statuses"]] == ["ok", "missing"]
    assert all(item["line_count"] is None for item in data["read_statuses"])
    serialized = json.dumps(data, ensure_ascii=False)
    for hidden in ("hidden-run", "other-kind-run", "hidden-chat", "visible-chat", "must_not_leak"):
        assert hidden not in serialized
    assert not current.exists()
    assert _window_inventory(root) == before


def test_window_tool_returns_exact_zero_for_existing_filtered_audit(window_tool) -> None:
    root, query = window_tool
    audit, scope = _window_source(root, "run")
    audit.parent.mkdir(parents=True)
    _append(audit, _row("window-run", "notification_prepared", "chat"))
    before = _window_inventory(root)

    response = query(**scope)

    assert response["ok"] is True
    data = response["data"]
    assert data["summary"]["ok"] is True
    assert data["summary"]["status"] == "ok"
    assert data["summary"]["missing_count"] == 0
    assert (
        data["summary"]["total_count"], data["pagination"]["total_count"],
        data["pagination"]["matched_count"], data["coverage"]["total_count"],
        data["coverage"]["omitted_count"],
    ) == (0,) * 5
    assert data["coverage"]["status"] == "complete"
    assert data["coverage"]["complete_for"] == "requested_window"
    assert data["coverage"]["stop_reason"] is None
    assert data["coverage"]["scanned_bytes"] == len(audit.read_bytes()) > 0
    assert data["read_statuses"][0]["status"] == "ok"
    assert data["read_statuses"][0]["line_count"] == 1
    assert data["events"] == data["event_summaries"] == []
    assert data["pagination"]["has_more"] is False
    assert data["coverage"]["has_more"] is False
    assert data["next_cursor"] is None
    assert _window_inventory(root) == before


@pytest.mark.parametrize("source_scope", ["run", "shared"])
def test_window_tool_signed_cursor_rejects_missing_leaf(window_tool, source_scope: str) -> None:
    root, query = window_tool
    audit, scope = _window_source(root, source_scope)
    audit.parent.mkdir(parents=True)
    for _ in range(2):
        _append(audit, _window_row("window-run", "notification_prepared", "chat"))
    before = _window_inventory(root)

    first = query(**scope, limit=1)

    assert first["ok"] is True
    assert first["data"]["summary"]["total_count"] == 2
    assert first["data"]["summary"]["returned_count"] == 1
    assert first["data"]["pagination"]["has_more"] is True
    cursor = first["data"]["next_cursor"]
    assert isinstance(cursor, str) and cursor
    assert _window_inventory(root) == before
    retained = audit.with_name("audit_events.retained.jsonl")
    assert not retained.exists()
    original_bytes = audit.read_bytes()
    audit.rename(retained)
    after_rename = _window_inventory(root)

    response = query(**scope, limit=1, cursor=cursor)

    assert response["ok"] is False
    assert response["error"]["code"] == "INPUT_ERROR"
    assert response["error"]["message"] == "source_changed"
    assert not audit.exists()
    assert retained.read_bytes() == original_bytes
    assert _window_inventory(root) == after_rename


@pytest.mark.parametrize(
    ("context_kind", "error_code", "error_message"),
    [
        pytest.param("cancelled", "CANCELLED", "cancelled", id="cancelled"),
        pytest.param("expired", "BUDGET_EXHAUSTED", "time_deadline", id="expired"),
    ],
)
def test_window_tool_context_failure_resets_before_missing_read(
    window_tool, context_kind: str, error_code: str, error_message: str,
) -> None:
    from src.application.agent_tools.project import project_query_context

    root, query = window_tool
    audit, _ = _window_source(root, "shared")
    audit.parent.mkdir(parents=True)
    before = _window_inventory(root)
    context = {"cancelled": lambda: True} if context_kind == "cancelled" else {"deadline_monotonic": 0.0}

    with project_query_context(**context):
        response = query()

    assert response["ok"] is False
    assert response["error"]["code"] == error_code
    assert response["error"]["message"] == error_message
    assert _window_inventory(root) == before

    retry = query()

    assert retry["ok"] is True
    assert retry["data"]["summary"]["ok"] is False
    assert retry["data"]["summary"]["status"] == "missing"
    assert retry["data"]["summary"]["missing_count"] == 1
    assert retry["data"]["summary"]["total_count"] is None
    assert retry["data"]["coverage"]["status"] == "partial"
    assert retry["data"]["coverage"]["stop_reason"] == "missing"
    assert retry["data"]["events"] == []
    assert not audit.exists()
    assert _window_inventory(root) == before


@pytest.fixture
def guarded_window_tool(window_tool, monkeypatch):
    from src.application import runtime_paths

    root, query = window_tool
    forbidden_calls = []

    def forbidden(name):
        def fail(*args, **kwargs):
            forbidden_calls.append(name)
            raise AssertionError(f"unexpected window query boundary: {name}")
        return fail

    monkeypatch.setattr(runtime_paths, "build_effective_env", forbidden("effective_env"))
    monkeypatch.setattr(runtime_paths, "read_runtime_root_record", forbidden("home_runtime_record"))
    monkeypatch.setattr(query_cursor, "resolve_secret", forbidden("cursor_secret"))

    def guarded_query(**payload):
        before = _window_inventory(root)
        response = query(**payload)
        assert forbidden_calls == []
        assert _window_inventory(root) == before
        return response

    return root, guarded_query


def _write_window_cap_source(root: Path, count: int, *, wide_tail: bool = False) -> Path:
    audit, _ = _window_source(root, "shared")
    audit.parent.mkdir(parents=True)
    with audit.open("w", encoding="utf-8") as stream:
        for index in range(count):
            row = _window_row(f"r{index:04d}", "notification_prepared", "visible-chat")
            if wide_tail and index < 5:
                row["extra"]["detail"] = "界" * 1000
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    assert audit.stat().st_size < 64 * 1024 * 1024
    return audit


def _seed_window_cap_cursor(query, index: int, **payload) -> tuple[str, dict]:
    from src.application.ledger.api import decode_evidence_cursor

    first = query(**payload)
    assert first["ok"] is True, first
    data = first["data"]
    assert isinstance(data["next_cursor"], str) and data["next_cursor"]
    state = decode_evidence_cursor(data["next_cursor"], "isolated-window-fixture-key")
    assert state["index"] == data["summary"]["returned_count"]
    # Seed a real signed snapshot near the bound; this does not simulate prior requests.
    state["index"] = index
    return query_cursor.encode_cursor(state), data


def _assert_window_pagination_limit(response: dict, *, stop_reason: str = "pagination_limit") -> None:
    assert response["ok"] is True, response
    data = response["data"]
    assert data["summary"]["ok"] is False
    assert data["summary"]["status"] == "partial"
    assert data["coverage"]["status"] == "partial"
    assert data["coverage"]["complete_for"] == "requested_window"
    assert data["coverage"]["stop_reason"] == stop_reason
    assert data["continuation_status"] == "pagination_limit"
    assert data["next_cursor"] is None
    assert data["pagination"]["has_more"] is True
    assert data["coverage"]["has_more"] is True
    assert (
        data["summary"]["total_count"], data["pagination"]["total_count"],
        data["pagination"]["matched_count"], data["coverage"]["total_count"],
        data["coverage"]["omitted_count"],
    ) == (None,) * 5
    assert data["summary"]["returned_count"] == len(data["events"]) > 0
    assert data["pagination"]["returned_count"] == len(data["events"])
    assert data["coverage"]["included_count"] == len(data["events"])
    assert "通知历史结果不完整：已达到分页上限，请缩小时间窗口或过滤范围。" in response["warnings"]


@pytest.mark.parametrize("limit", [1, 3, 50])
def test_window_tool_stops_only_unusable_signed_continuation(guarded_window_tool, limit: int) -> None:
    from src.application.ledger.api import decode_evidence_cursor

    root, query = guarded_window_tool
    audit = _write_window_cap_source(root, 5105)
    _append(audit, _window_row("hidden-run", "notification_prepared", "hidden-chat"))
    _append(audit, _window_row("other-kind", "notification_delivery_completed", "visible-chat"))
    payload = {"limit": limit, "authenticated_conversation_id": "visible-chat",
               "event_kind": "notification_prepared"}
    offset = 5000 - limit
    cursor, initial = _seed_window_cap_cursor(query, offset, **payload)
    expected = [f"r{index:04d}" for index in reversed(range(5105))]
    seen = []
    for _ in range(limit + 1):
        response = query(**payload, cursor=cursor)
        assert response["ok"] is True, response
        data = response["data"]
        page = [event["run_id"] for event in data["events"]]
        assert page == expected[offset:offset + len(page)]
        assert 0 < len(page) <= limit <= 50
        assert len(json.dumps(data["events"], ensure_ascii=False).encode()) <= 6500
        assert data["source_hash"] == initial["source_hash"]
        assert data["pagination"]["scanned_count"] == 5105
        assert data["coverage"]["scanned_bytes"] == audit.stat().st_size
        assert all(item["line_count"] is None for item in data["read_statuses"])
        serialized = json.dumps(data, ensure_ascii=False)
        for hidden in ("hidden-run", "other-kind", "hidden-chat", "visible-chat", "must_not_leak"):
            assert hidden not in serialized
        seen.extend(page)
        offset += len(page)
        cursor = data["next_cursor"]
        if cursor is None:
            assert offset > 5000
            _assert_window_pagination_limit(response)
            break
        assert data["summary"]["ok"] is True
        assert data["coverage"]["status"] == "complete"
        assert "continuation_status" not in data
        assert response["warnings"] == []
        state = decode_evidence_cursor(cursor, "isolated-window-fixture-key")
        assert state["index"] == offset <= 5000
    else:
        pytest.fail("bounded window did not stop at its accepted cursor limit")
    assert seen == expected[5000 - limit:offset]
    assert len(seen) == len(set(seen))


def test_window_tool_limit_uses_actual_unicode_shrunken_page(guarded_window_tool) -> None:
    from src.application.bot.tools import model_observation
    from src.application.ledger.api import decode_evidence_cursor

    root, query = guarded_window_tool
    _write_window_cap_source(root, 5004, wide_tail=True)
    cursor, _ = _seed_window_cap_cursor(query, 4999, limit=3)

    first = query(limit=3, cursor=cursor)

    assert first["ok"] is True, first
    data = first["data"]
    assert data["summary"]["ok"] is True
    assert data["summary"]["returned_count"] == 1
    assert [event["run_id"] for event in data["events"]] == ["r0004"]
    assert len(json.dumps(data["events"], ensure_ascii=False).encode()) <= 6500
    assert len(json.dumps(data["events"] * 2, ensure_ascii=False).encode()) > 6500
    assert data["pagination"]["has_more"] is True
    state = decode_evidence_cursor(data["next_cursor"], "isolated-window-fixture-key")
    assert state["index"] == 5000
    assert "continuation_status" not in data
    assert first["warnings"] == []

    terminal = query(limit=3, cursor=data["next_cursor"])

    _assert_window_pagination_limit(terminal)
    assert [event["run_id"] for event in terminal["data"]["events"]] == ["r0003"]
    assert terminal["data"]["summary"]["returned_count"] == 1
    assert len(json.dumps(terminal["data"]["events"], ensure_ascii=False).encode()) <= 6500
    observation = model_observation("notification_perception_read", terminal)
    assert observation["ok"] is True
    assert observation["data"]["coverage"]["status"] == "partial"
    assert observation["data"]["continuation_status"] == "pagination_limit"
    assert observation["data"]["next_cursor"] is None
    assert observation["warnings"] == terminal["warnings"]


@pytest.mark.parametrize(
    ("count", "offset", "limit"),
    [pytest.param(5000, 4999, 1, id="ends-at-5000"),
     pytest.param(5001, 5000, 1, id="last-start-5000"),
     pytest.param(5002, 4999, 3, id="last-page-crosses-5000")],
)
def test_window_tool_exact_tail_is_complete_at_offset_boundary(
    guarded_window_tool, count: int, offset: int, limit: int,
) -> None:
    root, query = guarded_window_tool
    _write_window_cap_source(root, count)
    cursor, _ = _seed_window_cap_cursor(query, offset, limit=limit)

    response = query(limit=limit, cursor=cursor)

    assert response["ok"] is True, response
    data = response["data"]
    returned = count - offset
    assert [event["run_id"] for event in data["events"]] == [f"r{index:04d}" for index in reversed(range(returned))]
    assert data["summary"]["ok"] is True
    assert data["summary"]["status"] == "ok"
    assert data["summary"]["returned_count"] == returned
    assert data["coverage"]["status"] == "complete"
    assert data["coverage"]["stop_reason"] is None
    assert (
        data["summary"]["total_count"], data["pagination"]["total_count"],
        data["pagination"]["matched_count"], data["coverage"]["total_count"],
    ) == (count,) * 4
    assert data["coverage"]["omitted_count"] == count - returned
    assert data["pagination"]["has_more"] is False
    assert data["coverage"]["has_more"] is False
    assert data["next_cursor"] is None
    assert "continuation_status" not in data
    assert response["warnings"] == []


@pytest.mark.parametrize("degradation", ["malformed", "earlier_history"])
def test_window_tool_preserves_primary_degradation_at_pagination_limit(
    guarded_window_tool, degradation: str,
) -> None:
    root, query = guarded_window_tool
    audit = _write_window_cap_source(root, 5004)
    payload = {"limit": 1}
    if degradation == "malformed":
        with audit.open("a", encoding="utf-8") as stream:
            stream.write("{broken\n")
        reason = "malformed"
    else:
        segment = audit.with_name("audit_events.20260923.000001.jsonl")
        assert not segment.exists()
        audit.rename(segment)
        audit.write_bytes(b"")
        payload["start_utc"] = "2026-09-22T00:00:00Z"
        reason = "history_before_first_segment"
    cursor, _ = _seed_window_cap_cursor(query, 5000, **payload)

    response = query(**payload, cursor=cursor)

    _assert_window_pagination_limit(response, stop_reason=reason)
    assert response["data"]["pagination"]["scanned_count"] == 5004
    assert response["data"]["summary"]["missing_count"] == 0
    assert response["data"]["summary"]["malformed_count"] == (1 if degradation == "malformed" else 0)
    if degradation == "malformed":
        assert response["warnings"][0] == "Notification perception audit is partially corrupt; malformed_rows=1."
        assert len(response["warnings"]) == 2
    else:
        assert response["data"]["coverage"]["available_from_utc"] == "2026-09-23T00:00:00+00:00"
        assert len(response["warnings"]) == 1


@pytest.mark.parametrize("invalid", ["index", "binding", "signature"])
def test_window_tool_rejects_invalid_boundary_cursor_before_source_open(
    guarded_window_tool, monkeypatch, invalid: str,
) -> None:
    from src.application.agent_tools import project_reader

    root, query = guarded_window_tool
    _write_window_cap_source(root, 5004)
    cursor, _ = _seed_window_cap_cursor(query, 5001 if invalid == "index" else 5000, limit=1)
    payload = {"limit": 2 if invalid == "binding" else 1}
    if invalid == "signature":
        body, signature = cursor.split(".")
        cursor = body + "." + ("A" if signature[0] != "A" else "B") + signature[1:]
    opened = []

    def forbidden_open(*args, **kwargs):
        opened.append(True)
        raise AssertionError("invalid cursor attempted to open its source")

    monkeypatch.setattr(project_reader, "_directory", forbidden_open)

    response = query(**payload, cursor=cursor)

    assert response["ok"] is False
    assert response["error"]["code"] == "INPUT_ERROR"
    assert response["error"]["message"] == "cursor_invalidated"
    assert opened == []
    assert response["data"] == {}


@pytest.mark.parametrize("change", ["append", "replace", "missing"])
def test_window_tool_keeps_source_identity_at_pagination_limit(guarded_window_tool, change: str) -> None:
    root, query = guarded_window_tool
    audit = _write_window_cap_source(root, 5004)
    cursor, initial = _seed_window_cap_cursor(query, 5000, limit=1)
    original = audit.read_bytes()
    if change == "append":
        _append(audit, _window_row("new-append", "notification_prepared", "visible-chat"))
    else:
        retained = audit.with_name("audit_events.retained.jsonl")
        assert not retained.exists()
        audit.rename(retained)
        if change == "replace":
            audit.write_bytes(original)

    response = query(limit=1, cursor=cursor)

    if change == "append":
        _assert_window_pagination_limit(response)
        assert response["data"]["source_hash"] == initial["source_hash"]
        assert response["data"]["pagination"]["scanned_count"] == 5004
        assert response["data"]["coverage"]["scanned_bytes"] == len(original)
        assert [event["run_id"] for event in response["data"]["events"]] == ["r0003"]
        assert audit.read_bytes().startswith(original)
    else:
        assert response["ok"] is False
        assert response["error"]["code"] == "INPUT_ERROR"
        assert response["error"]["message"] == "source_changed"
        assert response["data"] == {}
        assert retained.read_bytes() == original
        assert audit.exists() is (change == "replace")


@pytest.mark.parametrize("stop", ["cancelled", "expired", "cancel_during_read"])
def test_window_tool_boundary_context_failure_does_not_become_partial(guarded_window_tool, stop: str) -> None:
    from src.application.agent_tools.project import project_query_context

    root, query = guarded_window_tool
    _write_window_cap_source(root, 5004)
    cursor, _ = _seed_window_cap_cursor(query, 5000, limit=1)
    checks = []

    def cancelled():
        checks.append(True)
        return stop == "cancelled" or len(checks) > 20

    context = {"deadline_monotonic": 0.0} if stop == "expired" else {"cancelled": cancelled}
    with project_query_context(**context):
        response = query(limit=1, cursor=cursor)

    assert response["ok"] is False
    assert response["error"]["code"] == ("BUDGET_EXHAUSTED" if stop == "expired" else "CANCELLED")
    assert response["error"]["message"] == ("time_deadline" if stop == "expired" else "cancelled")
    assert response["data"] == {}
    if stop == "cancel_during_read":
        assert len(checks) == 21

    retry = query(limit=1, cursor=cursor)

    _assert_window_pagination_limit(retry)
    assert [event["run_id"] for event in retry["data"]["events"]] == ["r0003"]
