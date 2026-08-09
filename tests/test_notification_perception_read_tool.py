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
    original_read_text = Path.read_text

    def _read_text(path: Path, *args, **kwargs):  # type: ignore[no-untyped-def]
        if path == audit:
            raise PermissionError("denied")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _read_text)
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
    # 必须显式拒绝，且外部文件不被读取（与 position_advice_runner 先例一致）。
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
