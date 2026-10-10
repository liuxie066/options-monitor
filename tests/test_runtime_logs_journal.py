import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.application.agent_tool_contracts import AgentToolError
from src.application.runtime_logs_cli import collect_runtime_logs, format_runtime_logs


def test_empty_service_log_directory_points_to_journal(tmp_path: Path) -> None:
    data = collect_runtime_logs(repo_root=tmp_path, runs_root=tmp_path / "runs",
                                logs_root=tmp_path / "logs", kind="service")
    assert data["summary"]["log_source"] == "journal_only"
    assert "journalctl" in data["journal_hint"]
    assert "journalctl" in format_runtime_logs(data)


def test_large_audit_file_reads_only_bounded_tail(tmp_path: Path) -> None:
    audit = tmp_path / "runs" / "run-1" / "state" / "audit_events.jsonl"
    audit.parent.mkdir(parents=True)
    with audit.open("wb") as stream:
        stream.truncate(92 * 1024 * 1024)
        stream.seek(-len(b"\nlast-line\n"), 2)
        stream.write(b"\nlast-line\n")
    data = collect_runtime_logs(repo_root=tmp_path, runs_root=tmp_path / "runs",
                                run_id="run-1", kind="audit", lines=1)
    assert data["files"][0]["tail"] == ["last-line"]
    assert data["files"][0]["tail_truncated"] is True


def test_log_read_failure_emits_local_meta_signal_on_retry(tmp_path: Path, monkeypatch, capsys) -> None:
    from src.application import runtime_logs_cli

    path = tmp_path / "logs" / "service.log"
    path.parent.mkdir()
    path.write_text("data\n", encoding="utf-8")

    def unreadable(*_args, **_kwargs):
        raise OSError("fixture read failure")

    monkeypatch.setattr(runtime_logs_cli, "_tail_lines", unreadable)
    for _ in range(2):
        out = collect_runtime_logs(repo_root=tmp_path, logs_root=path.parent,
                                   log_file=path, lines=1)
        assert out["summary"]["ok"] is False
        assert out["files"][0]["error_code"] == "LOG_READ_FAILED"
    assert capsys.readouterr().err.count("<3>READ_DIAGNOSTIC_DEGRADED") == 2


@pytest.mark.parametrize(
    ("profile", "expected_logs", "expected_runs"),
    [
        ({"paths": {"logs_root": "nested/logs", "runs_root": "nested/runs"},
          "logs_root": "ignored/logs", "runtime_root": "ignored/runtime"},
         "nested/logs", "nested/runs"),
        ({"runtime_root": "nested/runtime"},
         "nested/runtime/logs", "nested/runtime/output_runs"),
    ],
)
def test_public_logs_resolves_profile_roots(tmp_path, profile, expected_logs, expected_runs):
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(json.dumps(profile), encoding="utf-8")
    logs = tmp_path / expected_logs
    logs.mkdir(parents=True)
    (logs / "service.log").write_text("profile-selected\n", encoding="utf-8")

    data = collect_runtime_logs(repo_root=tmp_path, profile_path="profile.json", kind="service")

    assert data["logs_root"] == str(logs)
    assert data["runs_root"] == str(tmp_path / expected_runs)
    assert data["summary"]["ok"] is True
    assert data["files"][0]["tail"] == ["profile-selected"]


@pytest.mark.parametrize(
    ("contents", "message"),
    [(None, "profile not found"), ("{", "profile is not valid JSON"),
     ("[]", "profile must be a JSON object")],
)
def test_public_logs_preserves_profile_config_errors(tmp_path, contents, message):
    profile_path = tmp_path / "profile.json"
    if contents is not None:
        profile_path.write_text(contents, encoding="utf-8")
    with pytest.raises(AgentToolError, match=message) as exc:
        collect_runtime_logs(repo_root=tmp_path, runs_root=tmp_path / "runs",
                             profile_path=profile_path, kind="service")
    assert exc.value.code == "CONFIG_ERROR"


def test_public_logs_preserves_unreadable_profile_error(tmp_path, monkeypatch):
    profile_path = tmp_path / "profile.json"
    original = Path.read_text

    def unreadable(path, *args, **kwargs):
        if path == profile_path:
            raise PermissionError("fixture profile unreadable")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", unreadable)
    with pytest.raises(PermissionError, match="fixture profile unreadable"):
        collect_runtime_logs(repo_root=tmp_path, runs_root=tmp_path / "runs",
                             profile_path=profile_path, kind="service")


def test_public_logs_without_profile_skips_profile_loader(tmp_path, monkeypatch):
    from src.application import runtime_logs_cli

    monkeypatch.setattr(runtime_logs_cli, "_load_profile",
                        lambda *_args, **_kwargs: pytest.fail("absent profile was loaded"))
    monkeypatch.setattr(runtime_logs_cli, "resolve_runtime_root",
                        lambda **_kwargs: SimpleNamespace(runtime_root=tmp_path / "runtime"))
    data = collect_runtime_logs(repo_root=tmp_path, runs_root=tmp_path / "runs", kind="service")
    assert data["logs_root"] == str(tmp_path / "runtime" / "logs")
    assert data["summary"]["log_source"] == "journal_only"
